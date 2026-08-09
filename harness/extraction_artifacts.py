"""
harness.extraction_artifacts - Shared extraction artifact persistence.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, List, Optional

from harness.utils import JsonDict


def field_name_from_spec(value: Any) -> str:
    """Return the stable field name from a string or schema field object."""
    if isinstance(value, dict):
        for key in ("name", "field", "key"):
            raw = value.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        return ""
    return str(value).strip()


def field_names_from_specs(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    names: List[str] = []
    seen = set()
    for item in value:
        name = field_name_from_spec(item)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def validate_extraction_rows(raw_rows: Any) -> tuple[Optional[List[JsonDict]], Optional[JsonDict]]:
    if not isinstance(raw_rows, list):
        return None, {
            "status": "rejected",
            "error": "rows must be a JSON array of records (dicts)",
        }
    rows: List[JsonDict] = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            return None, {
                "status": "rejected",
                "error": (
                    f"rows[{index}] must be a JSON object; got "
                    f"{type(raw).__name__}"
                ),
            }
        rows.append(raw)
    return rows, None


def save_extraction_artifact(
    *,
    logger: Any,
    runtime: Any,
    artifacts: Optional[List[str]],
    name: str,
    rows: List[JsonDict],
    schema: Any = None,
    description: str = "",
    schema_warnings: Optional[List[JsonDict]] = None,
    source_artifacts: Optional[List[str]] = None,
    evidence_context: Optional[JsonDict] = None,
    event_type: str = "tool.record_extraction",
) -> JsonDict:
    safe_name = (
        "".join(c if c.isalnum() or c in "-_." else "-" for c in str(name))[:80]
        or "extraction"
    )
    task_dir = Path(
        getattr(logger, "task_dir", "")
        or getattr(getattr(runtime, "harness", None), "runs_dir", "")
        or "."
    )
    out_dir = task_dir / "artifacts" / "extractions"
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"{safe_name}-{uuid.uuid4().hex[:8]}.json"

    warnings = list(schema_warnings or [])
    payload: JsonDict = {
        "name": name,
        "description": description or None,
        "schema": schema if isinstance(schema, (dict, list)) else None,
        "rowCount": len(rows),
        "rows": rows,
    }
    if warnings:
        payload["schemaWarnings"] = warnings
    if evidence_context:
        # Persist the page/auth scope these rows were observed under. A later
        # worker reading this artifact cannot otherwise tell whether the rows
        # describe the page it is looking at now, and stamping the current scope
        # onto an old file would silently relabel stale evidence as fresh.
        payload["evidenceContext"] = dict(evidence_context)
    if source_artifacts:
        payload["sourceArtifacts"] = [str(path) for path in source_artifacts]

    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    absolute_path = str(file_path.resolve())
    if artifacts is not None and absolute_path not in artifacts:
        artifacts.append(absolute_path)
    if logger is not None and hasattr(logger, "write"):
        logger.write(
            event_type,
            {
                "name": name,
                "rowCount": len(rows),
                "savedPath": absolute_path,
                "schemaWarnings": warnings,
                "sourceArtifacts": [str(path) for path in source_artifacts or []],
            },
        )

    result: JsonDict = {
        "status": "needs_fix" if warnings else "done",
        "name": name,
        "rowCount": len(rows),
        "savedPath": absolute_path,
        "next_step": (
            "Pass this savedPath as evidence_artifacts input when downstream"
            " agents need to reuse the rows."
        ),
    }
    if warnings:
        result["schemaWarnings"] = warnings
        result["next_instruction"] = (
            "The saved rows do not fully match expected_artifact fields."
            " Fix field names or reshape before treating this artifact as validated."
        )
    return result
