"""SessionState — 单 session 的协调状态(plan / progress / contract / approval)。

放一处而不是分散在各 tool,好处:
- /reset 一个调用清掉
- /save /load 序列化方便
- 调试 /stats 看一处
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Goal:
    """progress_board 里的单个量化目标"""
    id: str
    description: str
    target: int               # 量化目标数(50 个 / 5 个文件)
    current: int = 0
    status: str = "pending"   # pending / in_progress / completed / failed
    notes: List[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def update(self, increment: int = 0, status: Optional[str] = None, note: str = "") -> None:
        if increment:
            self.current += increment
            if self.status == "pending" and self.current > 0:
                self.status = "in_progress"
            if self.target and self.current >= self.target:
                self.status = "completed"
        if status and status in ("pending", "in_progress", "completed", "failed"):
            self.status = status
        if note:
            self.notes.append(note[:200])
            self.notes = self.notes[-5:]   # 最多留 5 条
        self.updated_at = time.time()

    def is_done(self) -> bool:
        return self.status in ("completed", "failed")


@dataclass
class ProgressBoard:
    """整个 session 的进度面板。仅用于量化任务,Lead 主动 init_progress 才创建"""
    goals: Dict[str, Goal] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def add_goal(self, goal: Goal) -> None:
        self.goals[goal.id] = goal

    def update(self, goal_id: str, increment: int = 0, status: Optional[str] = None, note: str = "") -> bool:
        g = self.goals.get(goal_id)
        if not g:
            return False
        g.update(increment=increment, status=status, note=note)
        return True

    def all_done(self) -> bool:
        return bool(self.goals) and all(g.is_done() for g in self.goals.values())

    def summary(self) -> Dict[str, Any]:
        return {
            "goals": [
                {
                    "id": g.id, "description": g.description,
                    "current": g.current, "target": g.target,
                    "status": g.status,
                    "last_note": g.notes[-1] if g.notes else "",
                }
                for g in self.goals.values()
            ],
            "all_done": self.all_done(),
        }

    def format_for_prompt(self) -> str:
        """渲染到 worker task 末尾的进度面板片段"""
        if not self.goals:
            return ""
        lines = ["", "📊 当前进度面板(请遵循,完成一批后调用 update_progress):"]
        for g in self.goals.values():
            mark = {"completed": "✓", "in_progress": "⋯", "pending": "·", "failed": "✗"}.get(g.status, "?")
            tgt = f"/{g.target}" if g.target else ""
            note = f"  -- {g.notes[-1]}" if g.notes else ""
            lines.append(f"  [{mark}] {g.id}: {g.current}{tgt}  ({g.description}){note}")
        return "\n".join(lines)


@dataclass
class Contract:
    """plan 里声明的一个交付契约"""
    name: str                          # basename of output (无 shared/ 无 .ext)
    output_path: str                   # 'shared/x.json'
    schema: str = ""
    agent_type: str = "worker"
    description: str = ""
    published: bool = False            # publish_artifact(name=...) 后置 True
    verified: bool = False             # verification agent 完成且 passed 后置 True


@dataclass
class SessionState:
    """单 session 的所有协调状态"""
    session_id: str
    plan_approved: bool = False        # plan 审批门(--require-plan-approval 启用时才起效)
    plan_md: str = ""                  # 原始 plan 文本(审批后冻结)
    contracts: Dict[str, Contract] = field(default_factory=dict)  # name -> Contract
    has_verification_step: bool = False  # plan 里是否声明了 verification
    progress: Optional[ProgressBoard] = None
    notes: List[str] = field(default_factory=list)

    def add_contract(self, contract: Contract) -> None:
        self.contracts[contract.name] = contract
        if contract.agent_type == "verification":
            self.has_verification_step = True

    def mark_published(self, name: str) -> bool:
        c = self.contracts.get(name)
        if not c:
            return False
        c.published = True
        return True

    def mark_verified(self, name: str) -> bool:
        c = self.contracts.get(name)
        if not c:
            return False
        c.verified = True
        return True

    def pending_verification(self) -> List[Contract]:
        """已 publish 但未 verified 的 contract(verification agent 应处理)"""
        return [c for c in self.contracts.values() if c.published and not c.verified]

    def status(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "plan_approved": self.plan_approved,
            "contracts": {
                name: {"published": c.published, "verified": c.verified, "output": c.output_path}
                for name, c in self.contracts.items()
            },
            "has_verification_step": self.has_verification_step,
            "pending_verification_count": len(self.pending_verification()),
            "progress": self.progress.summary() if self.progress else None,
        }
