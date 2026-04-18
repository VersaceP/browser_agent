"""
denial_tracker.py — L2 权限连续拒绝熔断器

当某个 Agent 连续多次触发权限拒绝（如反复尝试越权操作），
自动触发熔断机制，强制终止该 Agent 的执行循环。

设计意图：防止恶意/失控的 LLM 通过暴力尝试绕过安全限制。
"""

import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class _AgentDenialState:
    """单个 Agent 的拒绝状态"""
    consecutive_denials: int = 0
    total_denials: int = 0
    last_denial_time: float = 0.0
    is_broken: bool = False


class DenialTracker:
    """
    L2 断路器：权限连续失败自动熔断。
    
    工作原理：
    - 每次权限校验失败时调用 record_denial()
    - 连续失败达到阈值时触发熔断
    - 任何一次成功操作调用 record_approval() 重置计数
    - 熔断后 10 分钟自动半开放（允许一次尝试）
    """

    def __init__(self, max_consecutive_denials: int = 5, cooldown_seconds: float = 600.0):
        """
        :param max_consecutive_denials: 连续拒绝次数阈值，超过则熔断
        :param cooldown_seconds: 熔断冷却时间（秒），默认 10 分钟
        """
        self.max_consecutive = max_consecutive_denials
        self.cooldown = cooldown_seconds
        self._states: Dict[str, _AgentDenialState] = {}

    def _get_state(self, agent_id: str) -> _AgentDenialState:
        """获取或创建 Agent 的拒绝状态"""
        if agent_id not in self._states:
            self._states[agent_id] = _AgentDenialState()
        return self._states[agent_id]

    def record_denial(self, agent_id: str, reason: str = "") -> bool:
        """
        记录一次权限拒绝。
        
        :param agent_id: Agent 标识
        :param reason: 拒绝原因（用于日志）
        :return: True 表示触发了熔断，False 表示仍在阈值内
        """
        state = self._get_state(agent_id)
        state.consecutive_denials += 1
        state.total_denials += 1
        state.last_denial_time = time.time()

        if state.consecutive_denials >= self.max_consecutive:
            state.is_broken = True
            print(
                f"[L2 熔断器] ⚡ Agent '{agent_id}' 连续 {state.consecutive_denials} 次权限拒绝，"
                f"触发熔断！原因: {reason}"
            )
            return True

        print(
            f"[L2 熔断器] ⚠️ Agent '{agent_id}' 权限拒绝 "
            f"({state.consecutive_denials}/{self.max_consecutive}): {reason}"
        )
        return False

    def record_approval(self, agent_id: str) -> None:
        """记录一次成功操作，重置连续拒绝计数"""
        state = self._get_state(agent_id)
        state.consecutive_denials = 0
        # 注意：不重置 is_broken，熔断需要冷却时间

    def is_circuit_broken(self, agent_id: str) -> bool:
        """
        检查 Agent 是否处于熔断状态。
        
        如果冷却时间已过，自动进入"半开放"状态（重置熔断，允许一次尝试）。
        """
        state = self._get_state(agent_id)

        if not state.is_broken:
            return False

        # 检查冷却时间是否已过
        elapsed = time.time() - state.last_denial_time
        if elapsed >= self.cooldown:
            print(
                f"[L2 熔断器] 🔄 Agent '{agent_id}' 冷却期结束，半开放恢复中..."
            )
            state.is_broken = False
            state.consecutive_denials = 0
            return False

        return True

    def get_stats(self, agent_id: str) -> dict:
        """获取 Agent 的拒绝统计信息"""
        state = self._get_state(agent_id)
        return {
            "agent_id": agent_id,
            "consecutive_denials": state.consecutive_denials,
            "total_denials": state.total_denials,
            "is_circuit_broken": state.is_broken,
            "last_denial_time": state.last_denial_time,
        }

    def reset(self, agent_id: str) -> None:
        """
        重置指定 Agent 的熔断状态（用于新 spawn 的 Agent）。

        场景：同一个 session 下重新 spawn 同类型 agent 时（如多轮质检任务），
        需要清空上一轮可能残留的熔断计数，防止"株连"。
        注意：这不会重置 total_denials（保留累计统计），仅清除连续拒绝计数和熔断标志。
        """
        if agent_id in self._states:
            old = self._states[agent_id]
            self._states[agent_id] = _AgentDenialState(total_denials=old.total_denials)
            print(f"[L2 熔断器] 🔄 已重置 Agent '{agent_id}' 的熔断状态（累计拒绝: {old.total_denials}）")

    def clear_session(self, session_id: str) -> int:
        """
        清理指定 session 下所有 Agent 的熔断状态。

        agent_id 格式为 "session_id:agent_type"，
        本方法会清除所有以 session_id 为前缀的 key。

        :param session_id: 会话 ID
        :return: 清理的 key 数量
        """
        keys_to_remove = [
            k for k in self._states if k.startswith(f"{session_id}:")
        ]
        for k in keys_to_remove:
            del self._states[k]
        if keys_to_remove:
            print(f"[L2 熔断器] 🧹 已清理 session '{session_id}' 的 {len(keys_to_remove)} 条熔断记录")
        return len(keys_to_remove)

    def clear_all(self) -> None:
        """清理所有熔断状态（全局重置）"""
        count = len(self._states)
        self._states.clear()
        print(f"[L2 熔断器] 🧹 已清理全部 {count} 条熔断记录")
