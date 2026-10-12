"""Immutable plan identity and version records shared by task runtimes."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, List, Optional

from harness.utils import JsonDict, storage_for_logger

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )

def plan_hash(plan: Any) -> str:
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()

def _summary_value(value: Any) -> Any:
    raw = canonical_json(value)
    if len(raw) <= 1000:
        return value
    return {
        "_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "_jsonChars": len(raw),
    }

def structural_plan_diff(
    previous: Any,
    candidate: Any,
    *,
    path: str = "$",
) -> List[JsonDict]:
    """Return a deterministic JSON-path diff without unbounded inline values."""

    if type(previous) is not type(candidate):
        return [{
            "path": path,
            "change": "type_changed",
            "before": _summary_value(previous),
            "after": _summary_value(candidate),
        }]
    if isinstance(previous, dict):
        changes: List[JsonDict] = []
        for key in sorted(set(previous) | set(candidate)):
            child = f"{path}.{key}"
            if key not in previous:
                changes.append({
                    "path": child,
                    "change": "added",
                    "after": _summary_value(candidate[key]),
                })
            elif key not in candidate:
                changes.append({
                    "path": child,
                    "change": "removed",
                    "before": _summary_value(previous[key]),
                })
            else:
                changes.extend(
                    structural_plan_diff(
                        previous[key],
                        candidate[key],
                        path=child,
                    )
                )
        return changes
    if isinstance(previous, list):
        changes = []
        common = min(len(previous), len(candidate))
        for index in range(common):
            changes.extend(
                structural_plan_diff(
                    previous[index],
                    candidate[index],
                    path=f"{path}[{index}]",
                )
            )
        for index in range(common, len(previous)):
            changes.append({
                "path": f"{path}[{index}]",
                "change": "removed",
                "before": _summary_value(previous[index]),
            })
        for index in range(common, len(candidate)):
            changes.append({
                "path": f"{path}[{index}]",
                "change": "added",
                "after": _summary_value(candidate[index]),
            })
        return changes
    if previous != candidate:
        return [{
            "path": path,
            "change": "changed",
            "before": _summary_value(previous),
            "after": _summary_value(candidate),
        }]
    return []

def build_plan_version_record(
    *,
    plan: JsonDict,
    previous_plan: Optional[JsonDict],
    replan_reason: str,
    user_task: str,
    validator_review: Optional[JsonDict],
    source_plan: Optional[JsonDict] = None,
) -> JsonDict:
    """Everything about an accepted plan except its version number.

    Separated so the atomic commit can allocate the number inside the same
    transaction that writes the record and the state referencing it.
    """

    record = {
        "acceptedAt": _utc_now_iso(),
        "planHash": plan_hash(plan),
        "originalUserTaskHash": hashlib.sha256(
            str(user_task or "").encode("utf-8")
        ).hexdigest(),
        "replanReason": replan_reason or None,
        "diff": structural_plan_diff(previous_plan or {}, plan),
        "validatorReview": validator_review,
        "plan": plan,
    }
    if isinstance(source_plan, dict):
        # The source declaration is audit data.  The executable `plan`
        # above is the normalized compilation, and remains the sole runtime
        # authority.
        record["sourcePlan"] = copy.deepcopy(source_plan)
        record["sourcePlanHash"] = plan_hash(source_plan)
    return record

def write_plan_version(
    logger: Any,
    *,
    plan: JsonDict,
    previous_plan: Optional[JsonDict],
    replan_reason: str,
    user_task: str,
    validator_review: Optional[JsonDict],
    source_plan: Optional[JsonDict] = None,
) -> JsonDict:
    storage, task_id = storage_for_logger(logger)
    # planVersion / previousVersion are assigned by the backend: it owns the
    # sequence and, in dual mode, propagates the number it chose so both
    # ledgers agree.
    record = storage.save_plan_version(
        task_id=task_id,
        run_id=str(getattr(logger, "run_id", "") or ""),
        record=build_plan_version_record(
            plan=plan,
            previous_plan=previous_plan,
            replan_reason=replan_reason,
            user_task=user_task,
            validator_review=validator_review,
            source_plan=source_plan,
        ),
    )
    return {
        "planVersion": record["planVersion"],
        "path": str(record.get("path") or ""),
        "planHash": record["planHash"],
        "previousVersion": record["previousVersion"],
        "replanReason": record["replanReason"],
        "diffCount": len(record["diff"]),
    }
