"""Explicit, bounded access to one user-referenced source task."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.storage.file_store import FileStore
from harness.storage.sqlite_store import SqliteStore, build_resource_uri


class SourceTaskContext:
    def __init__(self, source_task_id: str, worktree_root: Path, sqlite_store: SqliteStore):
        self.task_id = source_task_id.removeprefix("@")
        if not self.task_id or "/" in self.task_id or "\\" in self.task_id or self.task_id in {".", ".."}:
            raise ValueError("@task_id 格式错误")
        self.worktree_root = Path(worktree_root).resolve()
        self.directory = self.worktree_root / self.task_id
        if self.directory.is_symlink() or not self.directory.resolve(strict=False).is_relative_to(self.worktree_root):
            raise ValueError("来源任务目录越界")
        self.sqlite = sqlite_store
        self.file = FileStore(worktree_dir=str(self.worktree_root))
        if self.sqlite.get_task(self.task_id) is None and not self.directory.is_dir():
            raise FileNotFoundError(f"来源任务不存在: {self.task_id}")

    def manifest(self) -> dict[str, Any]:
        path = self.directory / "task_manifest.json"
        if not path.is_file() or path.is_symlink():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def invocations(self, skill_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM skill_invocations WHERE task_id=?"
        args: list[Any] = [self.task_id]
        if skill_id:
            sql += " AND skill_id=?"
            args.append(skill_id)
        sql += " ORDER BY started_at, invocation_id"
        return [dict(row) for row in self.sqlite.connection.execute(sql, args).fetchall()]

    def events(self, *, event_type: str | None = None, after: int = 0,
               limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if self.sqlite.get_task(self.task_id) is not None:
            return self.sqlite.read_events(task_id=self.task_id, after_event_id=after,
                                           limit=limit, event_type=event_type)
        return self.file.read_events(task_id=self.task_id, after_event_id=after,
                                     limit=limit, event_type=event_type)

    def resources(self, *, path_glob: str = "**/*", pattern: str | None = None,
                  limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        backend = self.sqlite if self.sqlite.get_task(self.task_id) is not None else self.file
        return backend.search_resources(task_id=self.task_id, path_glob=path_glob,
                                        pattern=pattern, max_results=limit)

    def read_resource(self, reference: str) -> dict[str, Any] | None:
        if self.sqlite.get_task(self.task_id) is not None:
            if not reference.startswith("sqlite://"):
                reference = build_resource_uri(self.task_id, reference)
            return self.sqlite.read_resource(current_task_id=self.task_id,
                                             resource_uri=reference)
        return self.file.read_resource(current_task_id=self.task_id,
                                       resource_uri=reference)
