"""
worktree.py — 物理隔离沙箱管理器

为每个任务/Agent 分配独立的文件系统目录，实现物理级别的文件隔离。
V4 增强：增加 save_spilled_data() 用于工具输出溢出时的自动落盘。
"""

import os
import shutil
from pathlib import Path


class WorkTreeManager:
    """管理针对不同任务/Agent 专属分配的文件级物理软隔离沙箱"""

    def __init__(self, base_dir: str = "d:/agent_research/browser_agent_system_v3/worktrees"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_or_create_worktree(self, task_id: str) -> Path:
        """为特定任务分配并创建一个隔离的工作目录"""
        worktree_path = self.base_dir / task_id
        worktree_path.mkdir(parents=True, exist_ok=True)

        # 预设常用子目录结构
        (worktree_path / "downloads").mkdir(exist_ok=True)
        (worktree_path / "data").mkdir(exist_ok=True)
        (worktree_path / "screenshots").mkdir(exist_ok=True)

        return worktree_path

    def resolve_path(self, task_id: str, relative_path: str) -> Path:
        """
        安全地将相对路径转换为沙箱内的绝对路径。
        防止路径穿越攻击（Directory Traversal）。
        """
        worktree_path = self.get_or_create_worktree(task_id)
        target_path = (worktree_path / relative_path).resolve()

        # 确保解析后的路径仍然在 worktree 内
        if not str(target_path).startswith(str(worktree_path.resolve())):
            raise PermissionError(
                f"[安全警报] 禁止跨越 WorkTree 沙箱！尝试访问: {target_path}"
            )

        return target_path

    def save_spilled_data(self, task_id: str, filename: str, content: str) -> str:
        """
        溢出落盘：当工具输出超过 max_result_chars 限制时，
        将完整内容写入 WorkTree 的 data/ 子目录，返回文件路径。
        
        这是 V4 两级压缩管道的第一级核心机制：
        - 工具返回的海量 DOM/JSON 不直接塞入上下文
        - 而是落盘后返回摘要引用，Agent 需要时可通过 read_file 读取
        """
        worktree_path = self.get_or_create_worktree(task_id)
        data_dir = worktree_path / "data"
        data_dir.mkdir(exist_ok=True)

        filepath = data_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return str(filepath)

    def cleanup_worktree(self, task_id: str) -> None:
        """物理清除某个任务的工作区及所有产物"""
        worktree_path = self.base_dir / task_id
        if worktree_path.exists():
            shutil.rmtree(worktree_path)

    def list_worktrees(self) -> list[str]:
        """列出所有现存的工作区 ID"""
        if not self.base_dir.exists():
            return []
        return [d.name for d in self.base_dir.iterdir() if d.is_dir()]
