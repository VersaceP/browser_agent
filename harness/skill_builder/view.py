"""Read-only file previews for saved drafts and published Skills."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from harness.skill_builder.catalog import (
    SkillCatalog, builder_directory, inspect_skill,
)
from harness.skill_builder.references import SkillReferences


def _file_path(directory: Path, relative: str) -> Path:
    raw = Path(relative)
    if (raw.is_absolute() or not raw.parts
            or any(part in {".", ".."} or part.startswith(".") for part in raw.parts)):
        raise ValueError("View path must be relative to the Skill directory")
    target = directory / raw
    if target.resolve() != target:
        raise ValueError("View path cannot traverse symbolic links")
    if not target.is_file():
        raise ValueError(f"Skill file not found: {relative}")
    return target


def view_skill(catalog: SkillCatalog, worktree_dir: Path, reference: str,
               relative_path: str | None = None, *, scope: str | None = None) -> dict[str, Any]:
    """Preview existing bytes even when the draft is incomplete or invalid."""
    references = SkillReferences(catalog, worktree_dir)
    if references.scope(reference, scope) == "draft":
        row = references.builder(reference)
        task_id = row["builder_task_id"]
        directory = builder_directory(worktree_dir, task_id, row["work_rel_path"], row.get("owner_task_id"))
        kind, expected_hash = "Draft", row["draft_hash"]
    else:
        row = references.published(reference)
        skill_id = row["skill_id"]
        directory = catalog.root / skill_id
        if directory.resolve() != directory:
            raise ValueError("Skill directory cannot traverse symbolic links")
        kind, expected_hash = "Published Skill", row["current_hash"]
    if not directory.is_dir():
        raise ValueError(f"{kind} work directory not found: {directory}")

    result = {"kind": kind, "reference": reference, "directory": str(directory),
              "expected_hash": expected_hash, "current_hash": None, "issue": None}
    try:
        result["current_hash"] = inspect_skill(directory)["hash"]
        if expected_hash and expected_hash != result["current_hash"]:
            result["issue"] = "Files differ from the indexed version; showing current file contents"
    except (ValueError, OSError, UnicodeError, RuntimeError, yaml.YAMLError) as exc:
        result["issue"] = str(exc)

    if relative_path is not None:
        target = _file_path(directory, relative_path)
        body = target.read_bytes()
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{relative_path} is not UTF-8 text; view the file in its work directory") from exc
        result["file"] = {"path": relative_path, "sha256": hashlib.sha256(body).hexdigest(),
                          "byte_size": len(body), "content": content}
    else:
        files = []
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory).as_posix()
            if any(part.startswith(".") for part in Path(relative).parts):
                continue
            if path.is_dir() and not path.is_symlink():
                continue
            try:
                target = _file_path(directory, relative)
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                files.append({"path": relative, "sha256": digest.hexdigest(),
                              "byte_size": target.stat().st_size})
            except (ValueError, OSError, RuntimeError) as exc:
                files.append({"path": relative, "error": str(exc)})
        result["files"] = files
    return result
