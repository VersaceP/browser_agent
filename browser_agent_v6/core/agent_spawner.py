"""AgentSpawner — agent 注册表 + spawn / chat 入口。

vs v5 简化点:
- 删 progress_board 强制接口(metadata 字段保留,不强制结构)
- 删 _approved_plan_sessions 计划审批门(M7 处理 plan 时再加,简单实现)
- 删 spill input compactor(task 文本超长直接落盘 input_payload.md,简化)
- browser_lock 改用 asyncio.Lock,单 lock 串行所有 browser-using worker

Phase 2 扩展点:
- DaemonPool 多浏览器 → spawner 改用 helpers.acquire_browser/release_browser
- spawn_agents_parallel 真正并发(当前因 browser_lock 退化为串行)
"""
import asyncio
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.context import TeammateContext
from core.agent_definition import AgentDefinition, build_builtin_agents
from core.execution_loop import execute_turn
from core.llm_provider import BaseLLMProvider
from core.fatal_tracker import FatalErrorTracker
from core.session_state import SessionState
from tools.base import ToolRegistry, ToolContext


PAYLOAD_THRESHOLD_CHARS = 10_000  # task 文本超过这个就落盘


# ──────────────────────────────────
# Multi-record task detection
# ──────────────────────────────────
# Patterns that indicate a "scrape N similar pages" task, where N >= 3.
# When matched, the spawner prepends a directive forcing the worker to read
# workflow_multi_record skill before any extraction (closes the gap where
# workers historically skipped the explore→distill→batch pattern when a
# site-skill gave them selectors directly).

_MULTI_RECORD_PATTERNS = [
    # "前 10 个" / "前 50 条" / "前 N 项"
    (re.compile(r"前\s*(\d+)\s*[个条项]"), 1),
    # "第 11-20 名" / "第 11~20 名" / "第 11 到 20"
    (re.compile(r"第\s*(\d+)\s*[-~–到至]\s*(\d+)\s*[名个条项]?"), "range"),
    # "rank 11-20" / "ranks 11-20" / "rank 11 to 20"
    (re.compile(r"\branks?\s+(\d+)\s*[-~–]\s*(\d+)", re.IGNORECASE), "range"),
    # "top 10" / "Top 50"
    (re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE), 1),
    # "10 个工具" / "50 个产品" / "N items" / "N records" / "N pages" / "N tools"
    (re.compile(r"\b(\d+)\s+(?:items?|records?|pages?|tools?|products?|entries?|results?|details?)\b",
                re.IGNORECASE), 1),
    # "10 detail pages" 中文版:"10 个详情页" / "20 个产品页"
    (re.compile(r"(\d+)\s*个\s*[详细产工商品]"), 1),
]


def _detect_multi_record_count(task: str) -> Optional[int]:
    """Return N (>= 3) if the task description matches a multi-record pattern.

    Returns None if no multi-record signal, or if the detected count is < 3
    (the threshold below which the explore→distill→batch overhead exceeds
    its savings — single-step extraction is fine for N=1 or 2).
    """
    if not task:
        return None
    candidates: List[int] = []
    for pat, kind in _MULTI_RECORD_PATTERNS:
        for m in pat.finditer(task):
            try:
                if kind == "range":
                    lo, hi = int(m.group(1)), int(m.group(2))
                    candidates.append(max(0, hi - lo + 1))
                else:
                    candidates.append(int(m.group(int(kind))))
            except (ValueError, IndexError):
                continue
    if not candidates:
        return None
    n = max(candidates)
    return n if n >= 3 else None


def _format_multi_record_directive(n: int) -> str:
    """The directive auto-appended to a worker task when multi-record is detected."""
    return (
        "\n\n━━━ SYSTEM-INJECTED DIRECTIVE ━━━\n"
        f"This task targets N={n} similar records. The ONLY acceptable execution shape is:\n"
        "  1. read_skill('workflow_multi_record')   — the explore→distill→batch playbook\n"
        f"  2. PHASE 1: explore ONE record fully (vision-first if no site-skill covers all fields)\n"
        f"  3. PHASE 2: distill the working steps into a clean recipe; validate on ONE more record\n"
        f"  4. PHASE 3: batch_browser_actions(steps=<recipe>, iterations=<remaining N-2>,\n"
        f"             publish_to=<contract.basename>) — server-side loop, NO LLM per iter\n\n"
        "Sequential per-record extraction (looping with run_browser_python or repeated\n"
        "single-step tools) is NOT acceptable for N>=3. It wastes O(N) LLM tokens and\n"
        "loses all data when max_turns hits.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


class AgentSpawner:
    """Agent 孵化器 + 注册表。"""

    def __init__(
        self,
        registry: ToolRegistry,
        llm: BaseLLMProvider,
        worktrees_root: str,
        shared_dir_root: Optional[str] = None,
        pool=None,  # WarmPool — 给 run_browser_python 用
        daemon=None,  # BrowserDaemon — 给 PageDisconnectedError 自愈用
        summarizer=None,  # 低成本 LLM — 给 read_file 大文件摘要用
        fatal_tracker: Optional[FatalErrorTracker] = None,
        require_plan_approval: bool = False,
        require_first_iteration_approval: bool = False,
    ):
        self.registry = registry
        self.llm = llm
        self.worktrees_root = Path(worktrees_root)
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        # session 共享区根:每个 session 下挂一个 shared/
        self.shared_dir_root = Path(shared_dir_root) if shared_dir_root else self.worktrees_root
        self.pool = pool
        self.daemon = daemon
        self.summarizer = summarizer
        self.fatal_tracker = fatal_tracker or FatalErrorTracker()
        self.require_plan_approval = require_plan_approval
        self.require_first_iteration_approval = require_first_iteration_approval

        self._agent_defs: Dict[str, AgentDefinition] = {}
        self._browser_lock = asyncio.Lock()  # 全 spawner 共享 — 同时只允许一个 browser worker

        # session_id -> SessionState(plan + contracts + progress + approval)
        self.session_states: Dict[str, SessionState] = {}

    def get_session_state(self, session_id: str) -> SessionState:
        """获取或新建一个 session 的状态容器。"""
        st = self.session_states.get(session_id)
        if st is None:
            st = SessionState(session_id=session_id)
            self.session_states[session_id] = st
        return st

    def clear_session(self, session_id: str) -> None:
        """清掉某个 session 的状态(给 /reset 用)"""
        self.session_states.pop(session_id, None)
        # 同步清 read_file 调用历史 — 否则 /reset 后 LLM 还是被 throttle
        try:
            from tools.file_tools import clear_read_history
            clear_read_history(session_id)
        except Exception:
            pass

    # ───── 注册 ─────

    def register_builtin(self, force_single_step_browser: bool = False) -> None:
        """注册内建 agents。

        force_single_step_browser=True 时,worker 不能用 run_browser_python,
        必须只用单步浏览器工具(navigate/click/extract_text/...)。
        """
        self._agent_defs = build_builtin_agents(force_single_step_browser=force_single_step_browser)

    def register(self, agent_def: AgentDefinition) -> None:
        self._agent_defs[agent_def.agent_type] = agent_def

    def get_def(self, agent_type: str) -> Optional[AgentDefinition]:
        return self._agent_defs.get(agent_type)

    def list_agents(self) -> List[str]:
        return list(self._agent_defs.keys())

    # ───── spawn 入口 ─────

    async def spawn(
        self,
        agent_type: str,
        task: str,
        session_id: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
        max_turns: Optional[int] = None,
        parent_worktree: Optional[str] = None,
    ) -> Dict[str, Any]:
        """孵化并执行一个 agent,直到 stop。

        Returns:
            {
                "success": bool,
                "agent_type": str,
                "session_id": str,
                "final_text": str,
                "turns_used": int,
                "token_usage": int,
                "stop_reason": str,
                "fatal_error": bool,
                "events": [...],         # 全部 turn 事件(给 UI / 日志用)
                "context": TeammateContext,  # 用于 chat 后续轮
            }
        """
        agent_def = self._agent_defs.get(agent_type)
        if not agent_def:
            return {
                "success": False,
                "error": f"unknown agent_type: {agent_type}",
                "available": list(self._agent_defs.keys()),
            }

        # 0. 熔断器检查 — 最近多次 fatal LLM error 直接拒绝,防止配额耗尽后无限循环
        ok, reason = self.fatal_tracker.check()
        if not ok:
            print(f"\n[spawn] {reason}")
            return {
                "success": False,
                "agent_type": agent_type,
                "error": reason,
                "circuit_broken": True,
                "fatal_error": True,
                "final_text": reason,
                "turns_used": 0,
                "token_usage": 0,
                "stop_reason": "circuit_broken",
                "events": [],
            }

        # 1. session id + worktree + shared dir
        if not session_id:
            session_id = f"task_{int(time.time())}"

        # verification 复用父 worktree(只读跨 sibling)
        if parent_worktree and agent_def.readonly:
            worktree_path = parent_worktree
        else:
            worktree_path = str(self.worktrees_root / session_id / agent_type)
            Path(worktree_path).mkdir(parents=True, exist_ok=True)

        shared_dir = self.shared_dir_root / session_id / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)

        # 2. task 过长 → 落盘 + 重写 task 引导 LLM 读文件
        effective_task = task
        if len(task) > PAYLOAD_THRESHOLD_CHARS:
            payload_file = Path(worktree_path) / "input_payload.md"
            payload_file.write_text(task, encoding="utf-8")
            effective_task = (
                f"[notice] Task description was too large for direct prompt and has "
                f"been saved to {payload_file.name} in your worktree. "
                f"READ THAT FILE FIRST for full instructions.\n\n"
                f"Task snippet (first 200 chars):\n{task[:200]}..."
            )

        # 2.5. 若 session 有 progress_board 且当前 spawn 是 worker → 末尾追加进度面板片段
        # (Lead 自己看 init_progress 的返回 summary 即可,不必在 task 末尾再渲染一遍)
        st = self.session_states.get(session_id) if session_id else None
        if st and st.progress and agent_def.agent_type == "worker":
            board_snippet = st.progress.format_for_prompt()
            if board_snippet:
                effective_task = effective_task + "\n" + board_snippet

        # 2.6. Multi-record detection — if worker task is "scrape N similar records"
        # with N>=3, force-inject the explore→distill→batch directive so the worker
        # cannot accidentally fall into sequential per-record extraction.
        if agent_def.agent_type == "worker":
            n_records = _detect_multi_record_count(task)
            if n_records is not None:
                effective_task = effective_task + _format_multi_record_directive(n_records)
                print(f"  ↳ multi-record task detected (N={n_records}) — injected workflow directive")

        # 3. 组装 context
        effective_max = max_turns if (max_turns and max_turns > 0) else agent_def.max_turns
        context = TeammateContext(
            agent_type=agent_type,
            session_id=session_id,
            task=effective_task,
            worktree_path=worktree_path,
            shared_dir=str(shared_dir),
            env_vars=env_vars or {},
        )

        # 4. ToolContext — 给 dispatch 注入运行时资源
        tool_ctx = ToolContext(
            worktree=worktree_path,
            shared_dir=str(shared_dir),
            session_id=session_id,
            agent_type=agent_type,
            pool=self.pool,
            spawner=self,
            daemon=self.daemon,
            summarizer=self.summarizer,
        )

        # 5. 临时覆盖 max_turns
        if max_turns and max_turns != agent_def.max_turns:
            from dataclasses import replace as _replace
            agent_def_eff = _replace(agent_def, max_turns=effective_max)
        else:
            agent_def_eff = agent_def

        # 6. 跑 execution_loop,收集事件
        print(f"\n[spawn] 🚀 {agent_type} | session={session_id} | task={task[:60]}...")
        events: List[Dict[str, Any]] = []
        final_text = ""
        turns_used = 0
        stop_reason = ""
        fatal_error = False

        async for ev in execute_turn(
            context=context,
            agent_def=agent_def_eff,
            llm=self.llm,
            registry=self.registry,
            tool_ctx=tool_ctx,
            browser_lock=self._browser_lock,
        ):
            events.append(ev)
            if ev["type"] == "turn_start":
                print(f"  [{agent_type}] turn {ev['turn']}")
            elif ev["type"] == "tool_use":
                print(f"    🔧 {ev['name']}({_summarize_input(ev['input'])})")
            elif ev["type"] == "tool_result":
                marker = "❌" if ev["status"] == "error" else "✓"
                print(f"    {marker} {ev['name']} → {ev['preview'][:80]}")
            elif ev["type"] == "llm_text":
                if ev["content"].strip():
                    print(f"    💬 {ev['content'].strip().splitlines()[0][:120]}")
            elif ev["type"] == "agent_complete":
                final_text = ev["final_text"]
                turns_used = ev["turns_used"]
                stop_reason = ev["stop_reason"]
                fatal_error = ev["fatal_error"]
            elif ev["type"] == "llm_error":
                print(f"    ❌ LLM ERROR: {ev['error'][:150]}")

        success = (not fatal_error) and stop_reason in ("end_turn", "stop", "")

        # 致命错误 → 记到熔断器,下次 spawn 之前会被 check 拦下
        if fatal_error:
            self.fatal_tracker.record(final_text or stop_reason or "unknown")

        # 同步契约状态:用 shared/<name> 文件存在与否作为 published 真值,
        # verification 成功结束 → 把 pending_verification 全部置 verified
        if st and success:
            self._sync_contract_state(st, agent_type)

        # Lead end_turn 时若 plan 含 verification 但未跑过 → 强制提醒并续跑一轮
        if (
            agent_type == "lead"
            and not fatal_error
            and stop_reason == "end_turn"
        ):
            extra_text, extra_turns = await self._enforce_verification_if_needed(
                context, agent_def_eff, events
            )
            if extra_text:
                final_text = extra_text
                turns_used += extra_turns

        # Worker 自然停止时若有 contract 但 shared/<output> 不存在 → 强制提醒并续跑
        # 解决"worker 提取了数据但没来得及 publish_artifact"的常见失败模式。
        # 触发覆盖所有 worker 不是被 LLM/系统死亡(fatal_error / circuit_broken)
        # 砍掉的停止方式:
        #   end_turn   — LLM 主动说做完了(可能忘 publish)
        #   stop       — OpenAI 协议的"自然停"等价
        #   tool_use   — 撞 max_turns 时,LLM 正在调下一个工具(本次的核心场景)
        #   max_tokens — LLM 输出长度上限,worker 还没说完
        #   empty      — LLM 罕见返回空内容,值得给一次重试机会
        # 排除:
        #   fatal_error — LLM API 配额死/网络死,再调还是会失败
        #   circuit_broken — 熔断器,系统级保护不应被 hook 绕过
        _RECOVERABLE_STOPS = {"end_turn", "stop", "tool_use", "max_tokens", "empty"}
        if (
            agent_type == "worker"
            and not fatal_error
            and stop_reason in _RECOVERABLE_STOPS
        ):
            extra_text, extra_turns = await self._enforce_worker_publish_if_needed(
                context, agent_def_eff, events, task=task,
                trigger_stop_reason=stop_reason,
            )
            if extra_text:
                final_text = extra_text
                turns_used += extra_turns
                # 再同步一次契约状态(publish 有可能在续跑里完成)
                if st:
                    self._sync_contract_state(st, agent_type)
            # 重新计算 success(若续跑后 publish 完成,success 应保持 True)
            success = (not fatal_error) and stop_reason in ("end_turn", "stop", "tool_use", "max_tokens", "empty", "")

        print(f"  [{agent_type}] ✓ done — {turns_used} turns, {context.token_usage} tokens, stop={stop_reason}")

        return {
            "success": success,
            "agent_type": agent_type,
            "session_id": session_id,
            "worktree": worktree_path,
            "shared_dir": str(shared_dir),
            "final_text": final_text,
            "turns_used": turns_used,
            "token_usage": context.token_usage,
            "stop_reason": stop_reason,
            "fatal_error": fatal_error,
            "events": events,
            "context": context,
        }

    async def chat(self, context: TeammateContext, message: str) -> Dict[str, Any]:
        """在已有 context 的基础上继续对话(用于 lead 多轮)。"""
        agent_def = self._agent_defs.get(context.agent_type)
        if not agent_def:
            return {"success": False, "error": f"unknown agent_type: {context.agent_type}"}

        # 熔断器检查
        ok, reason = self.fatal_tracker.check()
        if not ok:
            print(f"\n[chat] {reason}")
            return {
                "success": False,
                "agent_type": context.agent_type,
                "session_id": context.session_id,
                "error": reason,
                "circuit_broken": True,
                "fatal_error": True,
                "final_text": reason,
                "turns_used": 0,
                "token_usage": context.token_usage,
                "stop_reason": "circuit_broken",
                "events": [],
                "context": context,
            }

        context.append_message("user", message)

        tool_ctx = ToolContext(
            worktree=context.worktree_path,
            shared_dir=context.shared_dir,
            session_id=context.session_id,
            agent_type=context.agent_type,
            pool=self.pool,
            spawner=self,
            daemon=self.daemon,
            summarizer=self.summarizer,
        )

        events = []
        final_text = ""
        turns_used = 0
        stop_reason = ""
        fatal_error = False

        async for ev in execute_turn(
            context=context,
            agent_def=agent_def,
            llm=self.llm,
            registry=self.registry,
            tool_ctx=tool_ctx,
            browser_lock=self._browser_lock,
        ):
            events.append(ev)
            if ev["type"] == "agent_complete":
                final_text = ev["final_text"]
                turns_used = ev["turns_used"]
                stop_reason = ev["stop_reason"]
                fatal_error = ev["fatal_error"]

        if fatal_error:
            self.fatal_tracker.record(final_text or stop_reason or "unknown")

        # Lead end_turn 时,若 plan 声明了 verification 步骤但未跑过 → 注入提醒再跑一轮
        # 直接在 chat 的 loop 外处理:让 main.py 二次 chat,但更友好的是 spawn 同步处理。
        # 这里 chat 不强制(chat 是用户多轮接续,用户可能自己想再问别的),
        # 只在 lead spawn() 主流程里强制(见 spawn 末尾)。
        if (
            context.agent_type == "lead"
            and not fatal_error
            and stop_reason == "end_turn"
        ):
            extra_text, extra_turns = await self._enforce_verification_if_needed(
                context, agent_def, events
            )
            if extra_text:
                final_text = extra_text
                turns_used += extra_turns

        return {
            "success": not fatal_error,
            "agent_type": context.agent_type,
            "session_id": context.session_id,
            "final_text": final_text,
            "turns_used": turns_used,
            "token_usage": context.token_usage,
            "stop_reason": stop_reason,
            "fatal_error": fatal_error,
            "events": events,
            "context": context,
        }

    # ───── 契约状态同步 + verification 强制 ─────

    def _sync_contract_state(self, st, agent_type: str) -> None:
        """同步 SessionState.contracts 的 published / verified 标志。

        published: shared/<output> 文件存在(物理事实)
        verified : 当 agent_type == 'verification' 且 success → 把所有 published 但未 verified 的置 verified
        """
        if not st.contracts:
            return
        shared_root = self.shared_dir_root / st.session_id / "shared"
        for c in st.contracts.values():
            if c.agent_type == "verification":
                # verification 步骤本身不算 deliverable,跳过 published 检查
                continue
            if not c.published:
                # contract.output_path 可能是 'shared/foo.json',去掉前缀映射到 shared_root
                rel = c.output_path
                if rel.startswith("shared/"):
                    rel = rel[len("shared/"):]
                target = shared_root / rel
                if target.exists() and target.is_file() and target.stat().st_size > 0:
                    c.published = True

        if agent_type == "verification":
            for c in st.contracts.values():
                if c.published and not c.verified:
                    c.verified = True

    async def _enforce_worker_publish_if_needed(
        self,
        context: TeammateContext,
        agent_def: AgentDefinition,
        events: List[Dict[str, Any]],
        task: str,
        trigger_stop_reason: str = "end_turn",
    ) -> tuple:
        """Worker end_turn 时,若该 worker 有 contract 但对应文件未落盘 → 注入提醒并续跑。

        触发条件:
          1. SessionState 里有 worker contract,且匹配本次 task
          2. shared/<output_path> 不存在(或为空)
          3. worker 有过任何"提取性"工具调用,说明它确实做了工作但没 publish

        最多续跑 3 turn(够 LLM 调一次 publish_artifact + end_turn)。
        返回 (新 final_text, 额外 turns)。无需续则返回 ("", 0)。
        """
        st = self.session_states.get(context.session_id)
        if not st or not st.contracts:
            return ("", 0)

        # 只挑 worker 的、未发布的 contract,并按 task 文本匹配
        worker_contracts = [c for c in st.contracts.values()
                            if c.agent_type == "worker" and not c.published]
        if not worker_contracts:
            return ("", 0)
        from tools.lead_tools import find_contract_for_task
        matched = find_contract_for_task(worker_contracts, "worker", task)
        if not matched:
            return ("", 0)

        # 物理验证:文件确实不存在或为空(防止 contract 标记滞后)
        rel = matched.output_path
        if rel.startswith("shared/"):
            rel = rel[len("shared/"):]
        target = self.shared_dir_root / context.session_id / "shared" / rel
        if target.exists() and target.is_file() and target.stat().st_size > 0:
            return ("", 0)  # 已经发布了,只是 sync 没跟上

        # 健全性检查:worker 至少做过一次提取性工具调用,否则提醒没意义
        # (如果它根本没动手,publish 提醒只是空喊)
        EXTRACTIVE_TOOLS = {
            "extract_text", "dom_query", "dom_outline", "dom_classes",
            "run_browser_python", "batch_browser_actions",
            "vision_page_skeleton", "analyze_screenshot", "screenshot",
        }
        did_extraction = any(
            ev.get("type") == "tool_use" and ev.get("name") in EXTRACTIVE_TOOLS
            for ev in events
        )
        if not did_extraction:
            return ("", 0)

        # 是否调过 publish_artifact?调过但没成功?
        publish_attempted = any(
            ev.get("type") == "tool_use" and ev.get("name") == "publish_artifact"
            for ev in events
        )
        if publish_attempted:
            # 调过但文件还不存在 → 可能是参数错了,提醒里要更具体
            tip = (
                "You called publish_artifact but the file still doesn't exist. "
                "Common causes: wrong `name` (must be the basename only, not 'shared/...'), "
                "or content was empty. The contract requires:"
            )
        else:
            tip = (
                "You did extraction work (browser tools, code, vision) but never called "
                "publish_artifact. Data that lives only in your worktree or print() is "
                "INVISIBLE to the system. The contract requires:"
            )

        # If we were cut off by max_turns mid-flight (stop_reason='tool_use')
        # the worker may not even know it's been forcibly stopped — be explicit.
        cutoff_note = ""
        if trigger_stop_reason == "tool_use":
            cutoff_note = (
                "[note] You hit the max_turns budget mid-task. The system has "
                "given you 3 extra turns SOLELY to publish whatever partial data "
                "you've already collected. Publish what you have NOW — don't try "
                "to extract more records, that budget is gone.\n\n"
            )
        elif trigger_stop_reason == "max_tokens":
            cutoff_note = (
                "[note] Your previous response was cut off by max_tokens. "
                "You have 3 extra turns to publish your partial data and end_turn. "
                "Don't try to continue the previous reasoning — just publish.\n\n"
            )

        reminder = (
            f"[system reminder] {tip}\n"
            f"  final_output: {matched.output_path}\n"
            f"  schema: {matched.schema or 'see task'}\n\n"
            f"{cutoff_note}"
            f"Call publish_artifact(name='{rel}', content=<your_extracted_data>) NOW. "
            f"`name` is the basename inside shared/, not a full path. `content` must be "
            f"a list / dict / string with the actual data you extracted (even if "
            f"incomplete — partial data is better than zero). Then end_turn."
        )

        print(f"\n[spawn] ⚠️ injecting publish reminder to worker — re-engaging for up to 3 more turns")
        context.append_message("user", reminder)

        # 临时缩短 max_turns,避免续跑又走 50 turn
        from dataclasses import replace as _replace
        agent_def_short = _replace(agent_def, max_turns=3)

        tool_ctx = ToolContext(
            worktree=context.worktree_path,
            shared_dir=context.shared_dir,
            session_id=context.session_id,
            agent_type=context.agent_type,
            pool=self.pool,
            spawner=self,
            daemon=self.daemon,
            summarizer=self.summarizer,
        )

        extra_final_text = ""
        extra_turns = 0
        async for ev in execute_turn(
            context=context,
            agent_def=agent_def_short,
            llm=self.llm,
            registry=self.registry,
            tool_ctx=tool_ctx,
            browser_lock=self._browser_lock,
        ):
            events.append(ev)
            if ev["type"] == "tool_use":
                print(f"    🔧 [enforce] {ev['name']}({_summarize_input(ev['input'])})")
            elif ev["type"] == "tool_result":
                marker = "❌" if ev["status"] == "error" else "✓"
                print(f"    {marker} [enforce] {ev['name']} → {ev['preview'][:80]}")
            elif ev["type"] == "agent_complete":
                extra_final_text = ev["final_text"]
                extra_turns = ev["turns_used"]
        return (extra_final_text, extra_turns)

    async def _enforce_verification_if_needed(
        self,
        context: TeammateContext,
        agent_def: AgentDefinition,
        events: List[Dict[str, Any]],
    ) -> tuple:
        """Lead end_turn 时,若 plan 含 verification 步骤但未跑 → 注入提醒并续一轮。

        最多续 1 轮(避免 LLM 死循环)。返回 (新 final_text, 额外 turns)。
        如果没必要续,返回 ("", 0)。
        """
        st = self.session_states.get(context.session_id)
        if not st or not st.contracts or not st.has_verification_step:
            return ("", 0)

        # 如果还没有任何 contract published,说明 lead 啥都没做完,verification 提醒也没意义
        published_workers = [c for c in st.contracts.values()
                              if c.agent_type != "verification" and c.published]
        if not published_workers:
            return ("", 0)

        # 检查是否已经跑过 verification:寻找 events 里有 spawn_agent(agent_type='verification')
        verif_already_spawned = any(
            ev.get("type") == "tool_use"
            and ev.get("name") in ("spawn_agent", "spawn_agents_parallel")
            and _has_verification_in_input(ev.get("input", {}))
            for ev in events
        )
        if verif_already_spawned:
            return ("", 0)

        # 注入提醒 + 续跑
        contract_lines = "\n".join(
            f"  - {c.output_path}" + (" ✓ published" if c.published else " (not yet published)")
            for c in st.contracts.values() if c.agent_type != "verification"
        )
        reminder = (
            "[system reminder] Your plan declared a [verification] step but you haven't "
            "spawned a verification agent. The following deliverables exist and need verification:\n"
            f"{contract_lines}\n\n"
            "Spawn the verification agent now (one call), then end_turn with a summary that "
            "incorporates its findings. Do NOT skip this — the plan promised verification."
        )
        print(f"\n[spawn] ⚠️ injecting verification reminder to lead — re-engaging for one more loop")
        context.append_message("user", reminder)

        tool_ctx = ToolContext(
            worktree=context.worktree_path,
            shared_dir=context.shared_dir,
            session_id=context.session_id,
            agent_type=context.agent_type,
            pool=self.pool,
            spawner=self,
            daemon=self.daemon,
            summarizer=self.summarizer,
        )

        extra_final_text = ""
        extra_turns = 0
        async for ev in execute_turn(
            context=context,
            agent_def=agent_def,
            llm=self.llm,
            registry=self.registry,
            tool_ctx=tool_ctx,
            browser_lock=self._browser_lock,
        ):
            events.append(ev)
            if ev["type"] == "tool_use":
                print(f"    🔧 {ev['name']}({_summarize_input(ev['input'])})")
            elif ev["type"] == "tool_result":
                marker = "❌" if ev["status"] == "error" else "✓"
                print(f"    {marker} {ev['name']} → {ev['preview'][:80]}")
            elif ev["type"] == "agent_complete":
                extra_final_text = ev["final_text"]
                extra_turns = ev["turns_used"]
        return (extra_final_text, extra_turns)


# ──────────────────────────────────
# helper
# ──────────────────────────────────


def _has_verification_in_input(input_dict: Dict[str, Any]) -> bool:
    """spawn_agent(agent_type='verification') 或 spawn_agents_parallel 含 verification 任务"""
    if input_dict.get("agent_type") == "verification":
        return True
    tasks = input_dict.get("tasks") or []
    return any(t.get("agent_type") == "verification" for t in tasks if isinstance(t, dict))

def _summarize_input(d: Dict[str, Any], maxlen: int = 80) -> str:
    """把 tool_input 简短显示"""
    if not d:
        return ""
    pairs = []
    for k, v in d.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        pairs.append(f"{k}={s!r}")
    out = ", ".join(pairs)
    return out[:maxlen] + ("..." if len(out) > maxlen else "")
