"""TeammateContext — 纯数据 session 容器。

设计:
- 只承载状态(messages / token_usage / metadata),无业务逻辑
- 支持热换 LLM provider(state 与执行解耦)
- 比 v5 删掉:progress_board(用 metadata 自行存,不强制结构)
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import tiktoken

# cl100k_base 是 GPT-4 系列的分词器,中英文混合估算精度高于按字符数除 3
_ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass
class TeammateContext:
    """会话状态容器。"""
    agent_type: str
    session_id: str
    task: str
    messages: List[Dict[str, Any]] = field(default_factory=list)  # Anthropic / OpenAI 通用格式
    worktree_path: str = ""
    shared_dir: str = ""
    env_vars: Dict[str, str] = field(default_factory=dict)
    token_usage: int = 0           # 累计估算
    max_tokens: int = 200_000      # 触发 compact 的硬上限
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ── prompt cache 累计指标(每轮 LLM 调用后由 provider 累加) ──
    cache_read_total: int = 0      # 累计读自缓存的 input tokens
    cache_creation_total: int = 0  # 累计写入缓存的 input tokens(5 min 内复用)
    uncached_input_total: int = 0  # 累计未走缓存的 input tokens
    output_tokens_total: int = 0   # 累计 output tokens
    llm_call_count: int = 0        # 累计 LLM 调用次数

    def record_llm_usage(
        self,
        cache_read: int = 0,
        cache_creation: int = 0,
        uncached_input: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """provider 在 generate_response 后调用 — 记录单次调用的缓存命中情况"""
        self.cache_read_total += cache_read
        self.cache_creation_total += cache_creation
        self.uncached_input_total += uncached_input
        self.output_tokens_total += output_tokens
        self.llm_call_count += 1

    def cache_summary(self) -> Dict[str, Any]:
        """供 /stats 命令查看本 session 的缓存效果"""
        total_in = self.cache_read_total + self.cache_creation_total + self.uncached_input_total
        hit_rate = (self.cache_read_total / total_in) if total_in > 0 else 0.0
        return {
            "llm_calls": self.llm_call_count,
            "cache_read": self.cache_read_total,
            "cache_creation": self.cache_creation_total,
            "uncached_input": self.uncached_input_total,
            "output": self.output_tokens_total,
            "total_input": total_in,
            "cache_hit_rate": round(hit_rate, 3),
        }

    def append_message(self, role: str, content: Any) -> None:
        """追加一条消息(role: 'user' | 'assistant',content: str 或 content blocks)"""
        self.messages.append({"role": role, "content": content})
        # 更新 token 估算
        if isinstance(content, str):
            self.token_usage += len(_ENCODING.encode(content))
        else:
            try:
                import json as _json
                self.token_usage += len(_ENCODING.encode(_json.dumps(content, default=str)))
            except Exception:
                self.token_usage += len(str(content)) // 3  # 兜底

    def get_token_ratio(self) -> float:
        """当前 token / max_tokens"""
        return self.token_usage / max(self.max_tokens, 1)

    def to_dict(self) -> Dict[str, Any]:
        """用于 /save 持久化"""
        return {
            "agent_type": self.agent_type,
            "session_id": self.session_id,
            "task": self.task,
            "messages": self.messages,
            "worktree_path": self.worktree_path,
            "shared_dir": self.shared_dir,
            "env_vars": self.env_vars,
            "token_usage": self.token_usage,
            "max_tokens": self.max_tokens,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "cache_read_total": self.cache_read_total,
            "cache_creation_total": self.cache_creation_total,
            "uncached_input_total": self.uncached_input_total,
            "output_tokens_total": self.output_tokens_total,
            "llm_call_count": self.llm_call_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TeammateContext":
        # 兼容老 session 文件(没有这些字段)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
