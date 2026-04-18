"""
worktree.py — 物理隔离沙箱管理器

为每个任务/Agent 分配独立的文件系统目录，实现物理级别的文件隔离。
V4 增强：增加 save_spilled_data() 用于工具输出溢出时的自动落盘。
"""

import os
import shutil
from pathlib import Path
from typing import Optional


class WorkTreeManager:
    """管理针对不同任务/Agent 专属分配的文件级物理软隔离沙箱"""

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            # 锁定在项目根目录（core 的上一级）下的 worktrees
            project_root = Path(__file__).parent.parent.absolute()
            self.base_dir = project_root / "worktrees"
        else:
            self.base_dir = Path(base_dir)
            
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_or_create_worktree(self, session_id: str, agent_type: str) -> Path:
        """为特定会话下的特定 Agent 分配专属物理隔离沙箱"""
        worktree_path = self.base_dir / session_id / agent_type
        worktree_path.mkdir(parents=True, exist_ok=True)

        # 预设常用子目录结构
        if agent_type == "browser":
            (worktree_path / "screenshots").mkdir(exist_ok=True)
            (worktree_path / "downloads").mkdir(exist_ok=True)
        elif agent_type == "coding":
            (worktree_path / "data").mkdir(exist_ok=True)
            (worktree_path / "scripts").mkdir(exist_ok=True)
        elif agent_type == "lead":
            (worktree_path / "plans").mkdir(exist_ok=True)
        else:
            (worktree_path / "data").mkdir(exist_ok=True)
            (worktree_path / "data").mkdir(exist_ok=True)

        return worktree_path

    def resolve_path(self, session_id: str, agent_type: str, relative_path: str, is_lead: bool = False) -> Path:
        """
        安全地将相对路径转换为沙箱内的绝对路径。
        防止路径穿越攻击（Directory Traversal）。
        如果 is_lead=True，允许在当前 session 根目录内自由穿梭。
        """
        if is_lead:
            worktree_root = self.base_dir / session_id
            target_path = (self.base_dir / session_id / relative_path).resolve()
        else:
            worktree_root = self.get_or_create_worktree(session_id, agent_type)
            target_path = (worktree_root / relative_path).resolve()

        # 确保解析后的路径仍然在 worktree 范围内
        if not str(target_path).startswith(str(worktree_root.resolve())):
            raise PermissionError(
                f"[安全警报] 禁止跨越 WorkTree 沙箱！尝试访问: {target_path}"
            )

        return target_path

    def save_spilled_data(self, session_id: str, agent_type: str, filename: str, content: str) -> str:
        """
        溢出落盘：当工具输出超过 max_result_chars 限制时，
        将完整内容写入 WorkTree 的 data/ 子目录，返回文件路径。
        """
        worktree_path = self.get_or_create_worktree(session_id, agent_type)
        data_dir = worktree_path / "data"
        data_dir.mkdir(exist_ok=True)

        filepath = data_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return str(filepath)

    def cleanup_worktree(self, session_id: str) -> None:
        """物理清除某个会话级的工作区及其全部产物"""
        worktree_path = self.base_dir / session_id
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

    def list_worktrees(self) -> list[str]:
        """列出所有现存的工作区 ID"""
        if not self.base_dir.exists():
            return []
        return [d.name for d in self.base_dir.iterdir() if d.is_dir()]
