"""Text resources and declared execution entries in a selected Skill snapshot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.skill_builder.catalog import SkillCatalog
from harness.storage.factory import resolve_sqlite_path


def workflow_entries(snapshot: Path) -> list[dict[str, Any]]:
    """Directory roles are a file protocol, never a business eligibility test."""
    entries = []
    for path in sorted(snapshot.rglob("*.json")):
        relative = path.relative_to(snapshot).as_posix()
        if relative != "workflow.json" and not relative.startswith("workflows/"):
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        script = isinstance(document.get("entry"), str) and isinstance(document.get("files"), dict)
        if not script and not isinstance(document.get("steps"), list):
            continue  # A manifest or parameter object is not executable source.
        entries.append({"path": relative, "name": document.get("name"),
                        "description": document.get("description"),
                        "format": "js-ts-source-package" if script else "legacy-steps",
                        "entry": document.get("entry"),
                        "sourceFiles": sorted(document.get("files", {})) if script else [],
                        "timeoutMs": document.get("timeoutMs")})
    return entries


def selected_snapshot(runtime: Any, *, task_id: str | None = None) -> tuple[SkillCatalog, Path]:
    config = runtime.harness
    skill_id = str(getattr(config, "forced_skill_id", "") or "")
    content_hash = str(getattr(config, "forced_skill_hash", "") or "")
    if not skill_id or not content_hash:
        raise ValueError("No explicitly selected Skill version")
    catalog = SkillCatalog(Path(__file__).resolve().parents[2] / "skills",
                           resolve_sqlite_path(config.storage_sqlite_path, config.worktree_dir),
                           worktree_dir=Path(config.worktree_dir))
    try:
        snapshot = (catalog.pin_source(skill_id, content_hash, task_id=task_id) if task_id
                    else catalog.source_path(skill_id, content_hash))
        if snapshot is None:
            raise ValueError("Selected Skill snapshot is unavailable")
        return catalog, snapshot
    except BaseException:
        catalog.close()
        raise


def read_selected_resource(runtime: Any, relative: str, *, offset: int = 0,
                           max_chars: int = 16000, task_id: str | None = None) -> dict:
    catalog, snapshot = selected_snapshot(runtime, task_id=task_id)
    try:
        path = Path(relative)
        if (path.is_absolute() or not path.parts
                or any(p in {".", ".."} or p.startswith(".") for p in path.parts)):
            raise ValueError("Path must stay inside the selected Skill version")
        target = snapshot / path
        if target.is_symlink() or not target.resolve().is_relative_to(snapshot.resolve()) or not target.is_file():
            raise ValueError("Selected Skill resource is missing or outside the snapshot")
        text = target.read_text(encoding="utf-8")
        offset = max(0, int(offset)); limit = max(1, min(int(max_chars), 50000))
        return {"path": path.as_posix(), "skill": runtime.harness.forced_skill_id,
                "hash": runtime.harness.forced_skill_hash, "content": text[offset:offset + limit],
                "offset": offset, "nextOffset": offset + min(limit, max(0, len(text) - offset)),
                "hasMore": offset + limit < len(text)}
    finally:
        catalog.close()
