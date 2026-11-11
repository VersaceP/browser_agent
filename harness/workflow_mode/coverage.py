"""Package-owned scope and control inventory; no automatic business coverage verdict.

The model chooses scope and reports progress. Checks below cover record shape,
internal references and evidence ownership, never which controls a task needs.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from harness.workflow_mode.evidence import evidence_reference, pointer

FILE = "coverage.json"
STATUSES = ("unknown", "discovered", "documented", "implemented", "verified")
SCOPES = ("included", "excluded", "undecided")


def starter(goal: str) -> dict[str, Any]:
    return {"schemaVersion": 1,
            "scope": {"status": "unreviewed", "goal": goal, "included": [], "excluded": [],
                      "openQuestions": [], "userInputIds": []},
            "controls": [], "scenarios": []}


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


def _strings(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    for item in value:
        _text(item, label)


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise ValueError("coverage.json must be an object with schemaVersion 1")
    scope = value.get("scope")
    if not isinstance(scope, dict) or scope.get("status") not in ("unreviewed", "reviewed"):
        raise ValueError("coverage.scope needs status unreviewed or reviewed")
    _text(scope.get("goal"), "coverage.scope.goal")
    for key in ("included", "excluded", "openQuestions", "userInputIds"):
        _strings(scope.get(key, []), f"coverage.scope.{key}")
    ids: dict[str, set[str]] = {}
    for section, title in (("controls", "label"), ("scenarios", "description")):
        rows = value.get(section)
        if not isinstance(rows, list):
            raise ValueError(f"coverage.{section} must be an array")
        ids[section] = set()
        for index, row in enumerate(rows):
            label = f"coverage.{section}[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{label} must be an object")
            _text(row.get("id"), label + ".id")
            _text(row.get(title), label + "." + title)
            if row["id"] in ids[section]:
                raise ValueError(f"Duplicate {section} ID: {row['id']}")
            ids[section].add(row["id"])
            if row.get("scope") not in SCOPES or row.get("status") not in STATUSES:
                raise ValueError(f"{label} needs scope {SCOPES} and status {STATUSES}")
            for key in ("conditions", "options", "interaction", "context"):
                if key in row and not isinstance(row[key], dict):
                    raise ValueError(f"{label}.{key} must be an object")
            strategy = (row.get("interaction") or {}).get("strategy")
            if strategy is not None:
                if not isinstance(strategy, dict):
                    raise ValueError(f"{label}.interaction.strategy must be an object")
                for key in ("methods", "assumptions"):
                    if key in strategy:
                        _strings(strategy[key], f"{label}.interaction.strategy.{key}")
                for key in ("rationale", "catalogRevision", "guideRevision"):
                    if key in strategy:
                        _text(strategy[key], f"{label}.interaction.strategy.{key}")
                hashes = strategy.get("sourceHashes", {})
                if not isinstance(hashes, dict):
                    raise ValueError(f"{label}.interaction.strategy.sourceHashes must be an object")
                for name, digest in hashes.items():
                    if name not in row.get("sourceFiles", []):
                        raise ValueError(f"{label}.interaction.strategy source must appear in sourceFiles")
                    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                        raise ValueError(f"{label}.interaction.strategy source hash must be lowercase SHA-256")
            for key in ("dependsOn", "controlIds", "sourceFiles"):
                _strings(row.get(key, []), label + "." + key)
            for name in row.get("sourceFiles", []):
                path = PurePosixPath(name)
                if (path.is_absolute() or path.parts[:1] != ("src",) or ".." in path.parts
                        or "\\" in name or any(p.startswith(".") for p in path.parts)):
                    raise ValueError(f"{label}.sourceFiles must stay under src/")
            evidence = row.get("evidence", [])
            if not isinstance(evidence, list):
                raise ValueError(f"{label}.evidence must be an array")
            if row["status"] == "verified" and not evidence:
                raise ValueError(f"{label}: a verified claim needs evidence; otherwise keep it unverified")
            for item in evidence:
                if not isinstance(item, dict):
                    raise ValueError(f"{label}.evidence entries need taskId, ref and optional pointer")
                _text(item.get("taskId"), label + ".evidence.taskId")
                evidence_reference({k: item[k] for k in ("ref", "pointer") if k in item})
    for section in ("controls", "scenarios"):
        for row in value[section]:
            for key in ("dependsOn", "controlIds"):
                missing = set(row.get(key, [])) - ids["controls"]
                if missing:
                    raise ValueError(f"{section}.{row['id']}.{key} references unknown control IDs: {sorted(missing)}; "
                                     "record an unknown control or describe the unresolved condition in context")
    return value


def read(root: Path) -> dict[str, Any] | None:
    path = Path(root) / FILE
    if path.is_symlink():
        raise ValueError("coverage.json must not be a symbolic link")
    if not path.exists():
        return None
    return validate(json.loads(path.read_text(encoding="utf-8")))


def summary(root: Path) -> dict[str, Any]:
    try:
        value = read(root)
    except (OSError, ValueError) as exc:
        return {"status": "invalid", "path": FILE, "error": str(exc)}
    return summarize(value)


def summarize(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {"status": "not_recorded", "path": FILE,
                "interpretation": "No inventory recorded; this does not establish either support or absence."}
    view: dict[str, Any] = {"status": "recorded", "path": FILE,
        "scopeStatus": value["scope"]["status"],
        "openQuestions": len(value["scope"].get("openQuestions", [])),
        "interpretation": "Statuses are author claims about recorded entries, not proof of complete page "
                          "coverage or current applicability. Read coverage.json for conditions and evidence. "
                          "Historical verification does not verify changed source or a new page context."}
    for section in ("controls", "scenarios"):
        rows = value[section]
        view[section] = {"total": len(rows), "reportedStatuses": dict(Counter(r["status"] for r in rows)),
                         "scopes": dict(Counter(r["scope"] for r in rows))}
    return view


def reference_facts(value: dict[str, Any], *, task_id: str, known_refs: set[str],
                    read_receipt: Callable[[str], Any], user_input_ids: set[str]) -> dict[str, Any]:
    """Resolve owned references only. Historical references never grant read access."""
    counts: Counter = Counter()
    issues, loaded = [], {}
    for section in ("controls", "scenarios"):
        for row in value[section]:
            for index, evidence in enumerate(row.get("evidence", [])):
                label = f"{section}.{row['id']}.evidence[{index}]"
                if evidence["taskId"] != task_id:
                    counts["historicalUnresolved"] += 1
                    continue
                try:
                    ref, selected = evidence_reference({k: evidence[k] for k in ("ref", "pointer") if k in evidence})
                    if ref not in known_refs:
                        raise ValueError("reference is not an owned receipt")
                    if ref not in loaded:
                        loaded[ref] = read_receipt(ref)
                    pointer(loaded[ref], selected)
                    counts["ownedResolved"] += 1
                except (ValueError, KeyError, PermissionError, OSError) as exc:
                    counts["invalid"] += 1
                    issues.append({"entry": label, "error": str(exc)})
    for input_id in value["scope"].get("userInputIds", []):
        if input_id not in user_input_ids:
            counts["unresolvedUserInputs"] += 1
            issues.append({"entry": "scope.userInputIds", "inputId": input_id,
                           "error": "not a user input of the current author task; do not infer authorization"})
    return {"counts": dict(counts), "issues": issues[:20], "issuesTruncated": len(issues) > 20,
            "interpretation": "Address checks only, not evidence sufficiency. Historical references stay "
                              "unresolved here; use authorized evidence before making a current claim. "
                              "Neither this inventory nor its input IDs grant action permissions."}
