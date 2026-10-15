"""Pin used Skill bytes to tasks; publishing itself keeps no source history."""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from harness.skill_builder.catalog import _copy_contents, _now, inspect_skill, validate_skill_id
from harness.storage.resource_codec import decode_resource_row
from harness.storage.sqlite_connection import write_transaction


def _identity(skill_id, content_hash):
    validate_skill_id(skill_id)
    if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ValueError("Skill source requires a full content hash")


def _task_root(catalog, task_id):
    if (not isinstance(task_id, str) or not task_id or task_id.startswith(".")
            or "/" in task_id or "\\" in task_id):
        raise ValueError("Invalid task source identity")
    root = catalog.worktree / task_id
    if root.resolve() != root:
        raise ValueError("Task source cannot traverse symbolic links")
    return root


def _checked_path(root, relative):
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Skill source cannot escape its owner or traverse symbolic links")
    return path


def source_path(catalog, skill_id, content_hash, *, task_id=None):
    _identity(skill_id, content_hash)
    if task_id:
        row = catalog.connection.execute(
            "SELECT relative_path FROM skill_task_sources WHERE task_id=? AND skill_id=? AND content_hash=?",
            (task_id, skill_id, content_hash)).fetchone()
        if row:
            expected = f"skill/sources/{content_hash}"
            if row["relative_path"] != expected:
                raise ValueError("Task Skill source index does not match its identity")
            path = _checked_path(_task_root(catalog, task_id), expected)
            if not path.is_dir() or inspect_skill(path)["hash"] != content_hash:
                raise ValueError(f"Task Skill source is missing or changed: {task_id}/{expected}")
            return path
    current = catalog.get(skill_id)
    if current and not current["deleted"] and current["current_hash"] == content_hash:
        path = _checked_path(catalog.root, skill_id)
        if path.is_dir() and inspect_skill(path)["hash"] == content_hash:
            return path
    # Read-only migration fallback. No new publication writes this table/directory.
    row = catalog.connection.execute(
        "SELECT snapshot_path FROM skill_versions WHERE skill_id=? AND content_hash=?",
        (skill_id, content_hash)).fetchone()
    if row:
        expected = f".versions/{skill_id}/{content_hash}"
        if row["snapshot_path"] != expected:
            raise ValueError("Legacy Skill snapshot path does not match its identity")
        path = _checked_path(catalog.root, expected)
        if path.is_dir() and inspect_skill(path)["hash"] == content_hash:
            return path
    return None


def pin_source(catalog, skill_id, content_hash, *, task_id):
    """Copy once before execution; later publication cannot change this task."""
    _identity(skill_id, content_hash)
    relative = f"skill/sources/{content_hash}"
    target = _checked_path(_task_root(catalog, task_id), relative)
    source = source_path(catalog, skill_id, content_hash, task_id=task_id)
    if source is None:
        raise ValueError(f"Skill execution source unavailable: {skill_id}@{content_hash} (task {task_id})")
    if source == target:
        return target
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(content_hash + ".tmp-" + uuid.uuid4().hex)
        try:
            _copy_contents(source, staged)
            if inspect_skill(staged)["hash"] != content_hash:
                raise ValueError("Skill files changed while pinning the task source")
            try:
                os.rename(staged, target)
            except OSError:
                if not target.is_dir() or inspect_skill(target)["hash"] != content_hash:
                    raise
        finally:
            if staged.exists():
                shutil.rmtree(staged)
    info = inspect_skill(target)
    if info["hash"] != content_hash:
        raise ValueError("Pinned Skill source changed")
    with write_transaction(catalog.connection):
        catalog.connection.execute(
            """INSERT OR IGNORE INTO skill_task_sources
               (task_id,skill_id,content_hash,relative_path,metadata_json,files_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (task_id, skill_id, content_hash, relative, json.dumps(info["metadata"], ensure_ascii=False),
             json.dumps(info["files"], ensure_ascii=False), _now()))
    return target


def migrate_legacy_sources(catalog):
    """Move referenced legacy bytes before removing publication history.

    References include paused tasks with no invocation yet and Builder baselines.
    Do not use a current Skill as a substitute for unavailable historical bytes.
    A failed preparation leaves all legacy files/rows available for recovery.
    """
    rows = list(catalog.connection.execute("SELECT * FROM skill_versions"))
    if not rows:
        return {"migratedTaskSources": 0, "removedPublishedSnapshots": 0}
    references = set()
    for r in catalog.connection.execute("SELECT DISTINCT task_id,skill_id,skill_hash FROM skill_invocations"):
        references.add(tuple(r))
    for r in catalog.connection.execute(
            "SELECT source_task_id,source_skill_id,source_skill_hash FROM skill_builder_sessions WHERE source_skill_hash IS NOT NULL"):
        if r[1]: references.add(tuple(r))
    for row in rows:
        if row["source_task_id"]:
            references.add((row["source_task_id"], row["skill_id"], row["content_hash"]))

    def collect(task_id, value):
        if not isinstance(value, dict):
            raise ValueError(f"Invalid task manifest/session while migrating: {task_id}")
        skill = value.get("forced_skill_id") or value.get("skillId")
        sha = (value.get("startup_args") or {}).get("forced_skill_hash") or value.get("skillHash")
        if skill and sha:
            references.add((task_id, skill, sha))

    for row in catalog.connection.execute(
            """SELECT * FROM task_resources WHERE rowid IN (
                SELECT max(rowid) FROM task_resources
                WHERE logical_path IN ('task_manifest.json','skill/session.json')
                GROUP BY task_id, run_id, logical_path)"""):
        text = decode_resource_row(dict(row))
        if text: collect(row["task_id"], json.loads(text))
    # File-backend tasks may not have SQL manifests.
    for task in catalog.worktree.iterdir():
        if not task.is_dir() or task.name.startswith("."):
            continue
        for name in ("task_manifest.json", "skill/session.json"):
            path = task / name
            if path.is_file():
                _checked_path(_task_root(catalog, task.name), name)
                collect(task.name, json.loads(path.read_text()))

    old = {(r["skill_id"], r["content_hash"]) for r in rows}
    migrated = 0
    for task_id, skill, sha in sorted(references):
        if (skill, sha) in old:
            pin_source(catalog, skill, sha, task_id=task_id)
            migrated += 1
    for row in rows:
        relative = f'.versions/{row["skill_id"]}/{row["content_hash"]}'
        if row["snapshot_path"] != relative:
            raise ValueError("Legacy Skill snapshot path does not match its identity")
        path = _checked_path(catalog.root, relative)
        if path.exists(): shutil.rmtree(path)
        with write_transaction(catalog.connection):
            catalog.connection.execute("DELETE FROM skill_versions WHERE skill_id=? AND content_hash=?",
                                       (row["skill_id"], row["content_hash"]))
        if path.parent.is_dir() and not any(path.parent.iterdir()): path.parent.rmdir()
    versions = catalog.root / ".versions"
    if versions.is_dir() and not any(versions.iterdir()): versions.rmdir()
    return {"migratedTaskSources": migrated, "removedPublishedSnapshots": len(rows)}
