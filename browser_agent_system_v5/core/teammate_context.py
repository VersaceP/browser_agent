"""
teammate_context.py — 纯数据会话状态容器

V4 核心设计：将 Session 状态从执行逻辑中彻底解耦。
TeammateContext 是一个纯数据容器，不包含任何业务逻辑，
只负责承载会话消息、任务描述、WorkTree 路径等运行时状态。
支持在不丢失 Session 记忆的前提下热切换大模型底座。
"""

import time
import tiktoken
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# cl100k_base 是 GPT-4 系列使用的分词器，中英文混合文本估算精度远高于字符数/3
_ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass
class TeammateContext:
    """
    会话状态的纯数据容器。

    设计意图：
    - agent_type: 关联到 AgentDefinition，决定行为模式
    - session_id: 顶层会话 ID（贯穿不同 Agent 的相同物理任务）
    - task: 当前任务描述文本
    - session_messages: Anthropic Messages API 格式的对话历史
    - worktree_path: 本次任务的物理隔离沙箱路径（结合 session_id 和 agent_type）
    - env_vars: 环境变量（如 PROFILE_ID 用于浏览器环境绑定）
    - token_usage: 累计 Token 消耗量（用于压缩水位判断）
    - max_tokens: Token 上限阈值
    - created_at: 创建时间戳
    - metadata: 扩展字段（用于 Hook 间传递临时数据）
    - progress_board: 任务进度板（结构化追踪已完成/待完成的子目标）
    """
    agent_type: str
    session_id: str
    task: str
    session_messages: List[Dict[str, Any]] = field(default_factory=list)
    worktree_path: str = ""
    env_vars: Dict[str, str] = field(default_factory=dict)
    token_usage: int = 0
    max_tokens: int = 200_000  # Claude 的典型上下文窗口
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    progress_board: Dict[str, Any] = field(default_factory=dict)

    def append_message(self, role: str, content: Any) -> None:
        """
        追加一条消息到会话历史。
        
        :param role: "user" | "assistant"
        :param content: 字符串或 Anthropic content blocks 列表
        """
        self.session_messages.append({"role": role, "content": content})

    # ── 进度板操作方法 ──

    def progress_init(self, goals: List[Dict[str, Any]]) -> None:
        """
        初始化进度板。

        :param goals: 子目标列表，每项为 {"id": str, "description": str, "target": int|str|None}
            - id: 目标唯一标识（如 "collect_posts"）
            - description: 目标描述（如 "收集论坛帖子"）
            - target: 目标值（如 30 表示需收集 30 个帖子；None 表示无量化目标）
        """
        self.progress_board = {
            "goals": {g["id"]: {
                "description": g.get("description", g["id"]),
                "target": g.get("target"),
                "current": 0,
                "status": "pending",  # pending / in_progress / completed
                "notes": [],
            } for g in goals},
        }

    def progress_update(
        self,
        goal_id: str,
        increment: int = 0,
        status: Optional[str] = None,
        note: Optional[str] = None,
    ) -> bool:
        """
        更新某个子目标的进度。

        :param goal_id: 目标 ID
        :param increment: 进度增量（如已多收集了 5 个帖子则传 5）
        :param status: 直接设置状态（pending/in_progress/completed）
        :param note: 附加备注（如 "已保存到 data/posts_batch1.txt"）
        :return: 是否更新成功
        """
        goals = self.progress_board.get("goals", {})
        if goal_id not in goals:
            return False

        g = goals[goal_id]
        if increment:
            g["current"] = g.get("current", 0) + increment
        if status:
            g["status"] = status
        if note:
            notes = g.get("notes", [])
            notes.append(note)
            # 只保留最近 5 条备注
            g["notes"] = notes[-5:]

        # 自动推进状态
        if g["status"] == "pending" and g.get("current", 0) > 0:
            g["status"] = "in_progress"
        if g.get("target") and g.get("current", 0) >= g["target"]:
            g["status"] = "completed"

        return True

    def progress_summary(self) -> str:
        """
        生成人类可读的进度摘要（用于注入到 Agent 上下文中）。

        :return: 格式化的进度字符串
        """
        goals = self.progress_board.get("goals", {})
        if not goals:
            return ""

        lines = ["📊 任务进度板:"]
        for gid, g in goals.items():
            desc = g.get("description", gid)
            target = g.get("target")
            current = g.get("current", 0)
            status = g.get("status", "pending")

            # 状态图标
            icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}.get(status, "⬜")

            if target:
                pct = min(current / target * 100, 100) if target else 0
                lines.append(f"  {icon} {desc}: {current}/{target} ({pct:.0f}%)")
            else:
                lines.append(f"  {icon} {desc}: {current}")

            # 最近备注
            for n in g.get("notes", [])[-2:]:
                lines.append(f"     └ {n}")

        return "\n".join(lines)

    def get_messages(self) -> List[Dict[str, Any]]:
        """获取完整的会话消息列表（Anthropic Messages API 格式）"""
        return self.session_messages

    def estimate_tokens(self) -> int:
        """
        精确估算当前会话的 Token 消耗量。

        - 有 tiktoken：使用 BPE 分词器精确计数（中英文均适用）
        - 无 tiktoken：降级为字符数/3 的经验估算
        """
        total_tokens = 0

        for msg in self.session_messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_tokens += self._count_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            total_tokens += self._count_tokens(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            # 工具调用块：name + input JSON 字符串
                            total_tokens += self._count_tokens(block.get("name", ""))
                            total_tokens += self._count_tokens(str(block.get("input", {})))
                        elif block.get("type") == "tool_result":
                            total_tokens += self._count_tokens(str(block.get("content", "")))

        self.token_usage = total_tokens
        return total_tokens

    def _count_tokens(self, text: str) -> int:
        """对单段文本计 token 数"""
        return len(_ENCODING.encode(text))

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
            "progress_board": self.progress_board,
        }
