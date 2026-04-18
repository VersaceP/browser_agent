"""
hook_registry.py — 5 大生命周期 Hook 事件总线

V4 核心设计：通过 Hook 机制实现执行流的可扩展拦截。
5 个核心事件：
- SESSION_START: 会话启动（用于资源分配，如拉起浏览器环境）
- PRE_TOOL_EXECUTE: 工具执行前（用于安全校验，InputSanitizer 挂载点）
- POST_TOOL_EXECUTE: 工具执行后（用于日志记录、数据清洗）
- PRE_COMPACT: 上下文压缩前（用于自定义压缩逻辑）
- PRE_TURN_COMPLETE: Turn 结束前（用于触发 Verification Agent）

Hook 处理器可以返回 allow/block/modify 三种动作来控制执行流。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional


class HookEvent(Enum):
    """8 个核心生命周期事件"""
    SESSION_START = "session_start"              # 会话启动
    PRE_SKILL_INJECT = "pre_skill_inject"        # 技能注入前（新增）
    POST_SKILL_INJECT = "post_skill_inject"      # 技能注入后（新增）
    PRE_TOOL_EXECUTE = "pre_tool_execute"        # 工具执行前
    POST_TOOL_EXECUTE = "post_tool_execute"      # 工具执行后
    PRE_COMPACT = "pre_compact"                  # 上下文压缩前（可 BLOCK 阻止压缩）
    POST_COMPACT = "post_compact"                # 上下文压缩后（用于可观测性）
    PRE_TURN_COMPLETE = "pre_turn_complete"      # Turn 结束前（可 BLOCK 触发质检）
    POST_AGENT_COMPLETE = "post_agent_complete"  # Agent 循环结束（含质检结果）


class HookAction(Enum):
    """Hook 处理器的返回动作"""
    ALLOW = "allow"         # 放行，继续执行
    BLOCK = "block"         # 拦截，中止当前操作
    MODIFY = "modify"       # 修改 payload 后继续执行
    AUTO_COMPACT = "auto_compact"  # 通知执行引擎压缩中间重复消息


@dataclass
class HookResult:
    """
    Hook 处理器的返回结果。
    
    - action: 决定执行流的走向（allow/block/modify）
    - reason: 拦截/修改的原因说明
    - updated_payload: 修改后的 payload（仅 action=MODIFY 时有效）
    """
    action: HookAction = HookAction.ALLOW
    reason: str = ""
    updated_payload: Optional[Dict[str, Any]] = None


# Hook 处理器的类型签名：接收 payload 字典，返回 HookResult
HookHandler = Callable[[Dict[str, Any]], Coroutine[Any, Any, HookResult]]


class HookRegistry:
    """
    生命周期拦截器事件总线。
    
    使用方式：
    1. 在初始化阶段注册 Hook 处理器:
       hook_registry.register(HookEvent.PRE_TOOL_EXECUTE, sanitizer_hook)
    
    2. 在执行引擎中触发事件:
       result = await hook_registry.emit(HookEvent.PRE_TOOL_EXECUTE, payload)
       if result.action == HookAction.BLOCK:
           return result.reason  # 中止工具执行
    
    同一事件可注册多个处理器，按注册顺序依次执行。
    任何一个处理器返回 BLOCK 则立即中止后续处理器。
    """

    def __init__(self):
        self._handlers: Dict[HookEvent, List[HookHandler]] = {
            event: [] for event in HookEvent
        }

    def register(self, event: HookEvent, handler: HookHandler) -> None:
        """
        注册 Hook 处理器。
        
        :param event: 要监听的生命周期事件
        :param handler: 异步处理函数，签名: async def handler(payload: dict) -> HookResult
        """
        self._handlers[event].append(handler)

    async def emit(self, event: HookEvent, payload: Optional[Dict[str, Any]] = None) -> HookResult:
        """
        触发生命周期事件，依次执行所有注册的处理器。
        
        执行规则：
        - 按注册顺序依次执行
        - 任何处理器返回 BLOCK → 立即停止，返回该结果
        - 处理器返回 MODIFY → 更新 payload 后传递给后续处理器
        - 所有处理器都返回 ALLOW → 最终返回 ALLOW
        
        :param event: 触发的事件类型
        :param payload: 传递给处理器的数据负载
        :return: 最终的 Hook 处理结果
        """
        if payload is None:
            payload = {}

        handlers = self._handlers.get(event, [])
        if not handlers:
            return HookResult(action=HookAction.ALLOW)

        current_payload = payload.copy()

        for handler in handlers:
            try:
                result = await handler(current_payload)
            except Exception as e:
                print(f"[HookRegistry] ❌ Hook 处理器异常 ({event.value}): {e}")
                continue

            if result.action in (HookAction.BLOCK, HookAction.AUTO_COMPACT):
                return result

            if result.action == HookAction.MODIFY and result.updated_payload:
                current_payload = result.updated_payload

        return HookResult(action=HookAction.ALLOW, updated_payload=current_payload)

    def list_handlers(self, event: HookEvent) -> int:
        """返回指定事件的处理器数量"""
        return len(self._handlers.get(event, []))

    def clear(self, event: Optional[HookEvent] = None) -> None:
        """清除指定事件（或所有事件）的处理器"""
        if event:
            self._handlers[event] = []
        else:
            for e in HookEvent:
                self._handlers[e] = []


# ══════════════════════════════════════════════════════════════
#  RepetitionCompactor — 重复失败自动压缩 Hook
# ══════════════════════════════════════════════════════════════

from typing import Callable, Awaitable


class RepetitionCompactor:
    """
    监听 POST_TOOL_EXECUTE，跟踪连续重复失败和成功重复。

    当同一工具连续失败超过阈值时（跨错误类型累积）：
    1. 向上下文注入一条压缩摘要消息（替代中间 N 条重复记录）
    2. 返回 HookAction.AUTO_COMPACT，通知执行引擎跳过重复轮次

    当同一工具连续成功但输出高度相似时（成功重复检测）：
    1. 向上下文注入策略提醒，建议 Agent 切换策略
    2. 不触发 AUTO_COMPACT（成功的输出可能是有用的），仅提供建议

    触发条件：
    - 失败：同一工具（同一 session/agent 下）连续失败 threshold 次
    - 成功重复：同一工具连续成功 success_repetition_threshold 次，且输出相似度 > similarity_threshold

    使用方式：
        compactor = RepetitionCompactor(
            threshold=5,
            success_repetition_threshold=3,
            get_context=lambda: current_context,
        )
        hook_registry.register(HookEvent.POST_TOOL_EXECUTE, compactor.handler)

    阈值默认：失败 5 次，成功重复 3 次。
    """

    def __init__(
        self,
        threshold: int = 5,
        success_repetition_threshold: int = 3,
        similarity_threshold: float = 0.7,
        get_context: Callable[[], "TeammateContext"] = None,  # type: ignore
    ):
        self.threshold = threshold
        self.success_repetition_threshold = success_repetition_threshold
        self.similarity_threshold = similarity_threshold
        self._get_context = get_context
        self._current_context = None  # 由 AgentSpawner 在每次执行前注入

        # {(session_id, agent_type, tool_name, error_type): count}  — 错误类型维度
        self._failure_counts: dict = {}
        # {(session_id, agent_type, tool_name, error_type): last_tool_input}
        self._failure_inputs: dict = {}
        # {(session_id, agent_type, tool_name, error_type): start_turn}
        self._failure_turns: dict = {}
        # {(session_id, agent_type, tool_name): total_count}  — 工具维度（跨错误类型）
        self._tool_total_counts: dict = {}
        # {(session_id, agent_type, tool_name): error_summary}  — 工具维度错误摘要
        self._tool_error_summaries: dict = {}

        # ── 成功重复检测状态 ──
        # {(session_id, agent_type, tool_name): [fingerprint_str, ...]}  — 最近的输出指纹
        self._success_fingerprints: dict = {}
        # {(session_id, agent_type, tool_name): consecutive_success_count}
        self._success_counts: dict = {}
        # {(session_id, agent_type, tool_name): [tool_input, ...]}  — 最近的工具输入
        self._success_inputs: dict = {}

    def set_context(self, ctx: "TeammateContext") -> None:  # type: ignore
        """由 AgentSpawner 在每次 execute_turn 前调用，注入当前 context"""
        self._current_context = ctx

    def get_context(self) -> "TeammateContext":  # type: ignore
        """优先返回注入的上下文，其次返回回调"""
        return self._current_context or (self._get_context() if self._get_context else None)

    def _error_key(self, payload: dict) -> tuple:
        """从 payload 提取失败指纹键"""
        tool_name = payload.get("tool_name", "")
        result: str = payload.get("tool_result", "")
        session_id = payload.get("session_id", "")
        agent_type = payload.get("agent_type", "")

        # 提取错误类型（去掉具体路径/数字等变化部分）
        if result.startswith("[") and ":" in result:
            error_type = result.split(":", 1)[0].strip("[] ")
        else:
            error_type = result[:60]

        return (session_id, agent_type, tool_name, error_type)

    def _tool_key(self, payload: dict) -> tuple:
        """提取工具维度键（跨错误类型）"""
        return (
            payload.get("session_id", ""),
            payload.get("agent_type", ""),
            payload.get("tool_name", ""),
        )

    def _is_failure(self, result: str) -> bool:
        """判断工具结果是否为失败"""
        failure_markers = (
            "[读取失败]", "[写入失败]", "[执行失败]",
            "[权限拒绝]", "[安全拦截]", "[系统错误]",
            "[工具执行错误]", "[L2 熔断器]", "[审批拦截]",
        )
        return any(result.startswith(m) for m in failure_markers)

    @staticmethod
    def _result_fingerprint(result: str) -> str:
        """
        生成工具结果的指纹（用于相似度比对）。
        
        策略：
        - 截取前 300 字符作为指纹（覆盖大部分工具输出的关键信息）
        - 去除尾部溢出标记（spill/truncated 相关）以避免噪声
        - 标准化空白字符
        """
        # 去除溢出标记部分
        cutoff_markers = ["━━━━━━", "...[输出已截断]", "...[输出已截断", "📤 [输出已截断]", "📤 [大文本已结构化分割落盘]"]
        clean = result
        for marker in cutoff_markers:
            idx = clean.find(marker)
            if idx != -1:
                clean = clean[:idx]
        # 标准化：合并空白、去除首尾空白
        import re
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean[:300]

    @staticmethod
    def _compute_similarity(fp1: str, fp2: str) -> float:
        """
        计算两个指纹的相似度（简单字符级比对）。
        
        使用最长公共子序列比率作为相似度指标。
        对于大多数工具输出，前 300 字符的 LCS 比率足以判断是否为重复操作。
        """
        if not fp1 or not fp2:
            return 0.0
        # 快速路径：完全相同
        if fp1 == fp2:
            return 1.0
        # LCS 长度计算
        m, n = len(fp1), len(fp2)
        # 对长指纹进行截断以控制计算量
        max_len = 300
        fp1, fp2 = fp1[:max_len], fp2[:max_len]
        m, n = len(fp1), len(fp2)

        # 使用两行 DP 优化空间
        prev = [0] * (n + 1)
        curr = [0] * (n + 1)
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if fp1[i - 1] == fp2[j - 1]:
                    curr[j] = prev[j - 1] + 1
                else:
                    curr[j] = max(prev[j], curr[j - 1])
            prev, curr = curr, [0] * (n + 1)

        lcs_len = prev[n] if n > 0 else 0
        return lcs_len / max(m, n) if max(m, n) > 0 else 0.0

    def _check_success_repetition(
        self,
        tool_key: tuple,
        tool_name: str,
        tool_result: str,
        tool_input: dict,
        turn: int,
    ) -> Optional[str]:
        """
        检测成功重复调用。
        
        :return: 如果检测到重复，返回提醒消息；否则返回 None
        """
        fp = self._result_fingerprint(tool_result)
        fingerprints = self._success_fingerprints.get(tool_key, [])
        inputs = self._success_inputs.get(tool_key, [])
        count = self._success_counts.get(tool_key, 0) + 1

        # 记录本次指纹和输入
        fingerprints.append(fp)
        inputs.append(tool_input)
        # 只保留最近 success_repetition_threshold + 2 次的记录
        max_keep = self.success_repetition_threshold + 2
        if len(fingerprints) > max_keep:
            fingerprints = fingerprints[-max_keep:]
            inputs = inputs[-max_keep:]

        self._success_fingerprints[tool_key] = fingerprints
        self._success_inputs[tool_key] = inputs
        self._success_counts[tool_key] = count

        # 未达次数阈值
        if count < self.success_repetition_threshold:
            return None

        # 检查最近 N 次输出的相似度
        recent = fingerprints[-self.success_repetition_threshold:]
        # 计算相邻两两相似度，取最小值
        min_sim = 1.0
        for i in range(len(recent) - 1):
            sim = self._compute_similarity(recent[i], recent[i + 1])
            min_sim = min(min_sim, sim)

        if min_sim < self.similarity_threshold:
            # 相似度不够高，不认为是重复
            return None

        # ── 检测到成功重复 ──
        # 生成策略提醒
        avg_sim = min_sim  # 使用最低相似度作为代表

        # 构建工具输入摘要（帮助 Agent 理解自己在重复什么）
        recent_inputs = inputs[-self.success_repetition_threshold:]
        input_summaries: list[Any] = []
        for inp in recent_inputs:
            # 截取关键参数（selector, url, code 等）
            key_params: list[Any] = []
            for k in ("selector", "url", "code", "filename", "query"):
                if k in inp and inp[k]:
                    val = str(inp[k])[:80]
                    key_params.append(f"{k}={val}")
            if key_params:
                input_summaries.append(", ".join(key_params))
            else:
                input_summaries.append(str(inp)[:80])

        input_summary_str = " | ".join(input_summaries[-3:])

        advice = (
            f"[系统重复检测] 工具 '{tool_name}' 已连续成功调用 {count} 次，"
            f"且输出高度相似（相似度 {avg_sim:.0%}），可能陷入了重复操作循环。\n"
            f"  最近调用参数: {input_summary_str}\n"
            f"建议策略调整：\n"
            f"  • 如果目标数据已获取到，直接进入下一步或输出结果\n"
            f"  • 如果页面未变化，尝试 scroll_page 查看更多内容，或切换到不同区域\n"
            f"  • 如果当前选择器未命中目标，使用 run_js 诊断 DOM 结构或 screenshot 视觉分析\n"
            f"  • 避免对同一元素/页面反复执行相同操作"
        )

        # 重置成功计数（提醒后重新开始计数，避免反复提醒）
        self._success_counts[tool_key] = 0
        self._success_fingerprints[tool_key] = []
        self._success_inputs[tool_key] = []

        return advice

    def handler(self, payload: dict) -> Awaitable[HookResult]:
        """
        Hook 处理器签名（供 HookRegistry 调用）。
        必须为 async 函数。
        """
        return self._handle(payload)

    async def _handle(self, payload: dict) -> HookResult:
        """内部处理逻辑"""
        tool_name = payload.get("tool_name", "")
        tool_result: str = payload.get("tool_result", "")
        tool_input = payload.get("tool_input", {})

        # 只处理失败结果
        if not self._is_failure(tool_result):
            # 成功 → 重置该工具的所有失败计数（同一 session/agent/tool 下不同 error_type 一并清除）
            session_id = payload.get("session_id", "")
            agent_type = payload.get("agent_type", "")
            tool_name = payload.get("tool_name", "")
            prefix = (session_id, agent_type, tool_name)
            for k in list(self._failure_counts.keys()):
                if k[:3] == prefix:
                    self._failure_counts.pop(k, None)
                    self._failure_inputs.pop(k, None)
                    self._failure_turns.pop(k, None)
            # 工具维度计数也重置
            tk = (session_id, agent_type, tool_name)
            self._tool_total_counts.pop(tk, None)
            self._tool_error_summaries.pop(tk, None)

            # ── 成功重复检测 ──
            advice = self._check_success_repetition(
                tool_key=tk,
                tool_name=tool_name,
                tool_result=tool_result,
                tool_input=tool_input,
                turn=payload.get("turn", 0),
            )
            if advice:
                # 不再直接注入 user 消息（会破坏 tool_use→tool_result 配对），
                # 改为通过 updated_payload 传递给 execution_loop，
                # 由其在 tool_result 写入后再注入（方案A：延迟注入）
                return HookResult(
                    action=HookAction.ALLOW,
                    updated_payload={"inject_after_tool_result": advice},
                )

            return HookResult(action=HookAction.ALLOW)

        key = self._error_key(payload)
        tool_key = self._tool_key(payload)

        # 失败 → 重置成功重复计数
        self._success_counts.pop(tool_key, None)
        self._success_fingerprints.pop(tool_key, None)
        self._success_inputs.pop(tool_key, None)

        # ── 错误类型维度计数（用于摘要展示）──
        type_count = self._failure_counts.get(key, 0) + 1
        self._failure_counts[key] = type_count
        if type_count == 1:
            self._failure_inputs[key] = tool_input
            self._failure_turns[key] = payload.get("turn", 1)

        # ── 工具维度计数（跨错误类型累积，这才是触发压缩的条件）──
        total_count = self._tool_total_counts.get(tool_key, 0) + 1
        self._tool_total_counts[tool_key] = total_count

        # 收集错误摘要（最多保留最后 3 种不同错误类型）
        if tool_key not in self._tool_error_summaries:
            self._tool_error_summaries[tool_key] = []
        summaries = self._tool_error_summaries[tool_key]
        error_type = key[3]
        if error_type not in summaries:
            summaries.append(error_type)
            if len(summaries) > 3:
                summaries.pop(0)

        # 未达阈值，继续
        if total_count < self.threshold:
            return HookResult(action=HookAction.ALLOW)

        # ── 达到阈值 → 触发 AUTO_COMPACT ──
        repeated = total_count - 1  # 实际重复次数
        error_summary_str = " / ".join(summaries)

        summary = (
            f"[系统自动压缩] Agent 在工具 '{tool_name}' 上连续失败 {total_count} 次，"
            f"尝试的错误类型包括：{error_summary_str}。"
            f"中间 {repeated} 条重复失败记录已被压缩省略。"
            f"建议：检查工具调用参数是否正确，或该操作在当前环境下不可行。"
        )

        # 向上下文注入压缩摘要消息——改为延迟注入
        # 不再直接调用 ctx.append_message()（会破坏 tool_use→tool_result 配对），
        # 改为通过 HookResult.updated_payload 传递给 execution_loop，
        # 由其在 tool_result 写入后再注入（方案A：延迟注入）

        # 重置所有该工具相关的计数（压缩后重新开始计数）
        for k in list(self._failure_counts.keys()):
            if k[:3] == tool_key:
                self._failure_counts.pop(k, None)
        self._failure_inputs = {k: v for k, v in self._failure_inputs.items() if k[:3] != tool_key}
        self._failure_turns = {k: v for k, v in self._failure_turns.items() if k[:3] != tool_key}
        self._tool_total_counts.pop(tool_key, None)
        self._tool_error_summaries.pop(tool_key, None)

        return HookResult(
            action=HookAction.AUTO_COMPACT,
            reason=f"连续失败 {total_count} 次，触发自动压缩",
            updated_payload={"inject_after_tool_result": summary},
        )
