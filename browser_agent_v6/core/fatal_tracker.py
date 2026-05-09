"""FatalErrorTracker — LLM 致命错误熔断器。

修 task_1778062783 暴露的问题:
- LLM 配额耗尽 / API 5xx / 网络中断时,execution_loop 内部 break 没问题
- 但 lead 调 spawn_agent 拿到 failed result 后,LLM 下一轮可能又调 spawn,无限循环
- 之前 v5 的 denial_tracker 就是干这个的,我砍错了

设计:
- 简单滑动窗口:最近 cool_down_seconds 内 >= max_fatal 次致命错误 → 拒绝继续 spawn
- spawner.spawn 入口先 check,fatal 时 record
- /reset 时清空(让用户重置)
"""
import time
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class FatalErrorTracker:
    """单进程级熔断器 — 记录 LLM 致命错误,超阈值就 trip。

    Args:
        max_fatal: 窗口内多少次致命错误触发熔断(默认 3)
        cool_down_seconds: 滑动窗口长度(默认 300s = 5 分钟)
    """
    max_fatal: int = 3
    cool_down_seconds: int = 300
    errors: List[Tuple[float, str]] = field(default_factory=list)  # [(ts, reason), ...]

    def record(self, reason: str) -> None:
        """记录一次致命错误"""
        self.errors.append((time.time(), reason or "unknown fatal error"))

    def check(self) -> Tuple[bool, str]:
        """检查是否仍可继续。返回 (可继续, 拒绝原因)。

        副作用:每次 check 都会清理过期错误。
        """
        now = time.time()
        recent = [(t, r) for t, r in self.errors if now - t < self.cool_down_seconds]
        self.errors = recent
        if len(recent) >= self.max_fatal:
            last_reason = recent[-1][1][:300]
            return False, (
                f"⛔ FatalErrorTracker tripped: {len(recent)} fatal errors in last "
                f"{self.cool_down_seconds}s. Last error: {last_reason}. "
                f"Refusing to spawn more agents until cool-down or /reset."
            )
        return True, ""

    def reset(self) -> None:
        """清空记录(用于 /reset 命令)"""
        self.errors = []

    def status(self) -> dict:
        """供 /stats 命令查看"""
        now = time.time()
        recent = [(t, r) for t, r in self.errors if now - t < self.cool_down_seconds]
        return {
            "fatal_count_in_window": len(recent),
            "max_fatal": self.max_fatal,
            "cool_down_seconds": self.cool_down_seconds,
            "tripped": len(recent) >= self.max_fatal,
            "recent_errors": [
                {"seconds_ago": int(now - t), "reason": r[:120]}
                for t, r in recent[-3:]  # 最近 3 条
            ],
        }
