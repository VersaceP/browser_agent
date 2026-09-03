"""
harness.evidence.extraction_artifacts - Shared extraction artifact persistence.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, List, Optional

from harness.offload import store_offloaded
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


ARRAY_FIELD_TYPES = frozenset({"array", "list"})


def resolve_required_field_specs(expected_artifact: Any) -> JsonDict:
    """Resolve each required field to the spec that declares its type.

    A contract may spell the same thing three ways, and only one of them keeps
    the type next to the requirement:

      A  required_fields=[{name, type}]
      B  fields=[{name, type}] + required_fields=["name"]
      C  required_fields=["name"] and no spec anywhere

    B is by far the most common in practice, so any rule that reads only the
    objects inside `required_fields` sees almost nothing: of 325 required
    arrays across the stored plans and plan history, 309 are shaped like B.
    Every reader of "which required fields are arrays" has to go through here,
    or the rule and the exemption that waives it will disagree about what the
    contract even says.

    C is deliberately left unresolved rather than guessed. Nothing mechanical
    says `reviews` is an array; inferring it from the name would be exactly the
    site knowledge this layer must not invent.

    Returns `specs` (name -> declaring spec), `unresolved` (required names with
    no resolvable type) and `declared`, which says whether required_fields was
    stated outright or only recovered from `fields`.
    """
    expected = expected_artifact if isinstance(expected_artifact, dict) else {}
    base: JsonDict = {}
    raw_fields = expected.get("fields")
    for item in raw_fields if isinstance(raw_fields, list) else []:
        if isinstance(item, dict):
            name = field_name_from_spec(item)
            if name:
                base[name] = item

    raw_required = expected.get("required_fields")
    declared = isinstance(raw_required, list) and bool(raw_required)
    if not declared:
        raw_required = raw_fields

    specs: JsonDict = {}
    unresolved: List[str] = []
    for item in raw_required if isinstance(raw_required, list) else []:
        name = field_name_from_spec(item)
        if not name or name in specs or name in unresolved:
            continue
        # An object in required_fields overrides the base spec; a bare name
        # refers to it.
        spec = item if isinstance(item, dict) and item.get("type") else base.get(name)
        if isinstance(spec, dict) and str(spec.get("type") or "").strip():
            specs[name] = spec
        else:
            unresolved.append(name)
    return {"specs": specs, "unresolved": unresolved, "declared": declared}


def required_array_field_specs(expected_artifact: Any) -> JsonDict:
    """The resolved required fields whose declared type is an array."""
    resolved = resolve_required_field_specs(expected_artifact)
    resolved["specs"] = {
        name: spec for name, spec in resolved["specs"].items()
        if str(spec.get("type") or "").strip().lower() in ARRAY_FIELD_TYPES
    }
    return resolved


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
    row_lineage: Optional[List[JsonDict]] = None,
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
    if row_lineage:
        # Per-row provenance for a reference merge: which source artifact and
        # which row index each row was copied from. Without it a consolidated
        # artifact records only that some sources were cited, not that THIS row
        # came from one of them unchanged.
        payload["rowLineage"] = [dict(item) for item in row_lineage]

    # Extraction artifacts are cited by path across phases (batch_source, the
    # validated-artifact ledger), so the address stays a path either way and
    # only the backend behind it changes.
    store_offloaded(logger, file_path, resource_type="extraction", content=payload)
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
