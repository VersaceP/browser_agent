"""
context_compactor.py — 两级上下文压缩管道

V4 核心设计：防止 Token 爆炸的两级压缩机制：
- 第一级：即时截断落盘（truncate_and_spill）
  → 工具输出超过 max_result_chars 时，截断文本并将完整内容落盘
  → 已内置于 BaseTool.safe_execute() 中
  
- 第二级：LLM 摘要压缩（compact_if_needed）
  → Token 水位超过 80% 时，将历史消息交给 LLM 生成摘要
  → 用 [COMPACTED_SUMMARY] 标记替换被压缩的消息段
"""

from typing import Any, Dict, List, Optional

from core.teammate_context import TeammateContext


class ContextCompactor:
    """
    两级上下文压缩管道。
    
    设计意图：
    - 控制上下文窗口的 Token 消耗在安全范围内
    - 避免历史消息过多导致的 API 超限错误
    - 压缩时保留最近的消息和关键信息
    """

    def __init__(self, threshold_ratio: float = 0.8, keep_recent: int = 4):
        """
        :param threshold_ratio: Token 水位阈值比例（0.0~1.0），超过此值触发压缩
        :param keep_recent: 压缩时保留最近 N 条消息不被压缩
        """
        self.threshold_ratio = threshold_ratio
        self.keep_recent = keep_recent

    def should_compact(self, context: TeammateContext) -> bool:
        """检查是否需要触发压缩"""
        ratio = context.get_token_ratio()
        return ratio >= self.threshold_ratio

    async def compact_if_needed(
        self,
        context: TeammateContext,
        llm_summarize_fn=None,
    ) -> bool:
        """
        检查 Token 水位，必要时触发第二级 LLM 摘要压缩。
        
        压缩策略：
        1. 保留第一条消息（任务指令）和最近 N 条消息
        2. 中间的历史消息交给 LLM 生成摘要
        3. 用 [COMPACTED_SUMMARY] 标记消息替换被压缩段
        
        :param context: 会话上下文
        :param llm_summarize_fn: LLM 摘要函数（如果不提供则使用规则压缩）
        :return: True 表示执行了压缩，False 表示无需压缩
        """
        messages = context.session_messages
        if len(messages) <= self.keep_recent + 1:
            return False  # 消息太少，无需压缩

        print(
            f"[ContextCompactor] 📦 Token 水位 {context.get_token_ratio():.1%}，"
            f"触发压缩（保留首条 + 最近 {self.keep_recent} 条）"
        )

        # 分割消息：首条 | 中间待压缩 | 尾部保留
        first_message = messages[0]
        to_compact = messages[1:-self.keep_recent]
        to_keep = list(messages[-self.keep_recent:])

        # 切片可能切在 tool_use ↔ tool_result 配对中间：tool_use 落在 to_compact 末尾
        # 被吃进摘要文本（id 消失），tool_result 落在 to_keep 头部成为孤儿。
        # Anthropic API 会校验 tool_result.tool_use_id 必须能匹配前文 assistant 的 tool_use 块，找不到匹配就会报错。这里把头部所有孤儿 tool_result 消息丢弃。
        # （只丢头部：to_keep 中部/尾部的 tool_result 其 tool_use 也在 to_keep 内，仍然成对。）
        dropped_orphans = 0
        while to_keep and self._is_tool_result_message(to_keep[0]):
            to_keep.pop(0)
            dropped_orphans += 1
        if dropped_orphans:
            print(f"[ContextCompactor] 🧹 剥离 to_keep 头部 {dropped_orphans} 条孤儿 tool_result")

        # 生成压缩摘要
        if llm_summarize_fn:
            try:
                summary = await llm_summarize_fn(to_compact)
                # 摘要函数可能返回 None（如 LLM 调用失败），降级为规则压缩
                if not summary:
                    summary = self._rule_based_summary(to_compact)
            except Exception as e:
                print(f"[ContextCompactor] ⚠️ LLM 摘要失败，使用规则压缩: {e}")
                summary = self._rule_based_summary(to_compact)
        else:
            summary = self._rule_based_summary(to_compact)

        # 重组消息列表
        compacted_message = {
            "role": "user",
            "content": (
                f"[COMPACTED_SUMMARY] 以下是之前 {len(to_compact)} 条对话的摘要：\n"
                f"{summary}\n"
                f"[END_COMPACTED_SUMMARY]"
            )
        }

        context.session_messages = [first_message, compacted_message] + to_keep
        context.estimate_tokens()

        print(
            f"[ContextCompactor] ✅ 压缩完成: {len(messages)} 条 → "
            f"{len(context.session_messages)} 条, "
            f"Token 水位: {context.get_token_ratio():.1%}"
        )

        return True

    @staticmethod
    def _is_tool_result_message(msg: Dict[str, Any]) -> bool:
        """判断该消息是否包含 tool_result 块（用于检测切片产生的孤儿）"""
        if msg.get("role") != "user":
            return False
        content = msg.get("content", "")
        if not isinstance(content, list):
            return False
        return any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        )

    def _rule_based_summary(self, messages: List[Dict[str, Any]]) -> str:
        """
        基于规则的消息摘要（不依赖 LLM）。
        提取每条消息的关键信息组成摘要文本。
        """
        summary_parts = []

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            if isinstance(content, str):
                # 取前 200 字符作为摘要
                snippet = content[:200].strip()
                if len(content) > 200:
                    snippet += "..."
                summary_parts.append(f"[{role}] {snippet}")

            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text = block.get("text", "")[:150]
                            summary_parts.append(f"[{role}] {text}...")
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "unknown")
                            summary_parts.append(f"[{role}] 调用工具: {tool_name}")
                        elif block.get("type") == "tool_result":
                            result = str(block.get("content", ""))[:100]
                            summary_parts.append(f"[工具结果] {result}...")

        return "\n".join(summary_parts[-20:])  # 最多保留 20 条摘要行
