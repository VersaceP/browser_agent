"""Explicit Browser evidence exports, copied while the child store is open.

This grants no filesystem access to the author. Only returned artifacts owned
by this child are imported into an ordinary parent receipt. Content is evidence,
not instructions, and export failures remain visible.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.utils import read_task_file_text


def export_artifacts(logger: Any, outcome: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(logger.task_dir).resolve()
    exported = []
    paths = outcome.get("artifacts") or []
    if not isinstance(paths, list):
        return [{"status": "unavailable", "error": "Child artifacts is not an array"}]
    for raw in dict.fromkeys(p for p in paths if isinstance(p, str)):
        item = {"sourceTaskId": logger.task_id, "sourcePath": raw}
        try:
            path = Path(raw)
            path = (path if path.is_absolute() else root / path).resolve()
            relative = path.relative_to(root)
            if not relative.parts or relative.parts[0] != "artifacts":
                raise ValueError("Only explicitly returned child artifacts may be exported")
            text = read_task_file_text(logger, str(path))
            if text is None:
                raise ValueError("Artifact text is unavailable in the child storage")
            try:
                value = json.loads(text)
            except ValueError:
                value = text
            item.update(status="exported", value=value,
                        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        except (OSError, ValueError, PermissionError, UnicodeError) as exc:
            item.update(status="unavailable", error=str(exc))
        exported.append(item)
    return exported
