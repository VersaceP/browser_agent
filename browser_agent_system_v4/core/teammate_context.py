"""
teammate_context.py — 纯数据会话状态容器

V4 核心设计：将 Session 状态从执行逻辑中彻底解耦。
TeammateContext 是一个纯数据容器，不包含任何业务逻辑，
只负责承载会话消息、任务描述、WorkTree 路径等运行时状态。
支持在不丢失 Session 记忆的前提下热切换大模型底座。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TeammateContext:
    """
    会话状态的纯数据容器。

    设计意图：
    - agent_type: 关联到 AgentDefinition，决定行为模式
    - task: 当前任务描述文本
    - session_messages: Anthropic Messages API 格式的对话历史
    - worktree_path: 本次任务的物理隔离沙箱路径
    - env_vars: 环境变量（如 PROFILE_ID 用于浏览器环境绑定）
    - token_usage: 累计 Token 消耗量（用于压缩水位判断）
    - max_tokens: Token 上限阈值
    - created_at: 创建时间戳
    - metadata: 扩展字段（用于 Hook 间传递临时数据）
    """
    agent_type: str
    task: str
    session_messages: List[Dict[str, Any]] = field(default_factory=list)
    worktree_path: str = ""
    env_vars: Dict[str, str] = field(default_factory=dict)
    token_usage: int = 0
    max_tokens: int = 180_000  # Claude 的典型上下文窗口
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def append_message(self, role: str, content: Any) -> None:
        """
        追加一条消息到会话历史。
        
        :param role: "user" | "assistant"
        :param content: 字符串或 Anthropic content blocks 列表
        """
        self.session_messages.append({"role": role, "content": content})

    def get_messages(self) -> List[Dict[str, Any]]:
        """获取完整的会话消息列表（Anthropic Messages API 格式）"""
        return self.session_messages

    def estimate_tokens(self) -> int:
        """
        粗略估算当前会话的 Token 消耗量。
        使用简单的字符数 / 3 近似（中英文混合场景下的经验值）。
        """
        total_chars = 0
        for msg in self.session_messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        # 文本块
                        total_chars += len(block.get("text", ""))
                        total_chars += len(block.get("content", ""))
                        # 工具调用块
                        total_chars += len(str(block.get("input", "")))
        self.token_usage = total_chars // 3
        return self.token_usage

    def get_token_ratio(self) -> float:
        """返回当前 Token 使用量占上限的比例（0.0 ~ 1.0+）"""
        self.estimate_tokens()
        return self.token_usage / self.max_tokens if self.max_tokens > 0 else 0.0

    def to_summary(self) -> Dict[str, Any]:
        """
        将当前上下文导出为精简摘要（用于 Lead Agent 汇总子任务结果）。
        不包含完整对话历史，只保留关键元信息。
        """
        # 提取最后一条 assistant 消息作为最终结果
        final_answer = ""
        for msg in reversed(self.session_messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str):
                    final_answer = content
                elif isinstance(content, list):
                    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    final_answer = "\n".join(texts)
                if final_answer:
                    break

        return {
            "agent_type": self.agent_type,
            "task": self.task,
            "worktree_path": self.worktree_path,
            "token_usage": self.estimate_tokens(),
            "message_count": len(self.session_messages),
            "final_answer": final_answer[:2000],  # 截断保护
            "created_at": self.created_at,
        }
