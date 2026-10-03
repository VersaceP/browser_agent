"""Read the one Skill version explicitly selected for a task."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.skill_builder.catalog import SkillCatalog
from harness.storage.factory import resolve_sqlite_path


def selected_skill_context(runtime: Any) -> str:
    config = runtime.harness
    skill_id = str(getattr(config, "forced_skill_id", "") or "")
    content_hash = str(getattr(config, "forced_skill_hash", "") or "")
    if not skill_id or not content_hash:
        return ""
    catalog = SkillCatalog(
        Path(__file__).resolve().parents[2] / "skills",
        resolve_sqlite_path(config.storage_sqlite_path, config.worktree_dir),
    )
    try:
        snapshot = catalog.version_path(skill_id, content_hash)
        if snapshot is None:
            raise RuntimeError(f"选定 Skill 版本不可用: {skill_id}@{content_hash}")
        guide = (snapshot / "SKILL.md").read_text(encoding="utf-8")
        workflows = sorted(p.relative_to(snapshot).as_posix()
                           for p in snapshot.rglob("*.json") if p.is_file())
        execution_note = (
            "When useful and authorized, call execute_published_skill_workflow "
            "with a listed relative path, the assigned pageId/fleetId, and "
            "explicit variables. Interpret the receipt against the user's task; "
            "a failed call may have side effects."
            if bool(getattr(config, "workflow_execution_enabled", False))
            else "Workflow execution is disabled for this run. Use this Skill's "
                 "instructions as guidance and operate with available browser tools."
        )
        return (
            f"<selected_skill name={skill_id!r} hash={content_hash!r}>\n"
            f"{guide}\n"
            f"Workflow JSON files: {workflows}. {execution_note}\n"
            "</selected_skill>"
        )
    finally:
        catalog.close()
