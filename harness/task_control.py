"""
harness.task_control - Task plans, worker contracts, task state, and artifact validators.
"""

from __future__ import annotations

import json
import copy
import re
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from harness.utils import JsonDict, RunLogger, safe_path_component, trim_large_strings


TASK_PLAN_FILE = "task_plan.json"
TASK_STATE_FILE = "task_state.json"

VALIDATOR_TYPES = {
    "artifact_required",
    "required_fields",
    "field_nonempty",
    "min_rows",
    "max_rows",
    "exact_rows",
    "unique",
    "url_pattern",
    "allowed_domain",
    "set_equals",
    "range",
    "field_pattern",
    "cross_field_contains",
    "action_outcome",
}


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def validate_task_plan(
    raw_plan: Any,
    *,
    known_abcp_methods: Optional[AbstractSet[str]] = None,
    known_harness_tools: Optional[AbstractSet[str]] = None,
) -> Tuple[Optional[JsonDict], List[str]]:
    """Validate and normalize the v1 linear task plan.

    v1 intentionally supports a linear phase list only. `depends_on` and
    `fanout_from` may be present for forward compatibility, but execution does
    not schedule DAG fan-out yet.
    """
    errors: List[str] = []
    if not isinstance(raw_plan, dict):
        return None, ["plan must be a JSON object"]

    goal = str(raw_plan.get("goal") or "").strip()
    if not goal:
        errors.append("goal is required")

    raw_phases = raw_plan.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        errors.append("phases must be a non-empty array")
        raw_phases = []

    phases: List[JsonDict] = []
    seen_ids = set()
    for index, raw_phase in enumerate(raw_phases):
        if not isinstance(raw_phase, dict):
            errors.append(f"phases[{index}] must be an object")
            continue
        phase_id = str(raw_phase.get("id") or f"phase_{index + 1}").strip()
        phase_id = safe_path_component(phase_id, fallback=f"phase_{index + 1}")
        if phase_id in seen_ids:
            errors.append(f"duplicate phase id: {phase_id}")
        seen_ids.add(phase_id)

        phase_type = str(raw_phase.get("type") or "browser_worker").strip()
        if phase_type != "browser_worker":
            errors.append(
                f"phase {phase_id}: v1 only supports type='browser_worker'"
            )

        objective = str(raw_phase.get("objective") or "").strip()
        worker_task = str(raw_phase.get("worker_task") or raw_phase.get("task") or "").strip()
        if not objective:
            errors.append(f"phase {phase_id}: objective is required")
        if not worker_task:
            errors.append(f"phase {phase_id}: worker_task is required")

        expected_artifact = raw_phase.get("expected_artifact") or {}
        if expected_artifact is not None and not isinstance(expected_artifact, dict):
            errors.append(f"phase {phase_id}: expected_artifact must be an object")
            expected_artifact = {}

        validators = raw_phase.get("validators") or []
        if validators is not None and not isinstance(validators, list):
            errors.append(f"phase {phase_id}: validators must be an array")
            validators = []
        validators = _normalize_validators(
            expected_artifact if isinstance(expected_artifact, dict) else {},
            validators,
            errors,
            phase_id=phase_id,
        )

        worker_contract = raw_phase.get("worker_contract")
        if worker_contract is not None and not isinstance(worker_contract, dict):
            errors.append(f"phase {phase_id}: worker_contract must be an object")
            worker_contract = None
        if isinstance(worker_contract, dict):
            _validate_worker_contract_methods(
                worker_contract,
                errors,
                phase_id=phase_id,
                known_abcp_methods=known_abcp_methods,
                known_harness_tools=known_harness_tools,
            )

        phases.append({
            "id": phase_id,
            "type": phase_type,
            "objective": objective,
            "worker_task": worker_task,
            "context": str(raw_phase.get("context") or ""),
            "max_steps": raw_phase.get("max_steps"),
            "depends_on": raw_phase.get("depends_on") or [],
            "fanout_from": raw_phase.get("fanout_from"),
            "join": raw_phase.get("join"),
            "expected_artifact": expected_artifact,
            "validators": validators,
            "worker_contract": worker_contract or {},
            "max_attempts": _positive_int(raw_phase.get("max_attempts"), default=2),
        })

    normalized = {
        "version": "v1",
        "goal": goal,
        "task_type": str(raw_plan.get("task_type") or "general").strip() or "general",
        "phases": phases,
    }
    if errors:
        return None, errors
    return normalized, []


def _validate_worker_contract_methods(
    worker_contract: JsonDict,
    errors: List[str],
    *,
    phase_id: str,
    known_abcp_methods: Optional[AbstractSet[str]],
    known_harness_tools: Optional[AbstractSet[str]],
) -> None:
    harness_tools = known_harness_tools or set()
    for key in ("allowed_methods", "forbidden_methods"):
        raw_methods = worker_contract.get(key)
        if raw_methods is None:
            continue
        if not isinstance(raw_methods, list):
            errors.append(f"phase {phase_id}: worker_contract.{key} must be an array")
            continue
        for raw_method in raw_methods:
            method = str(raw_method or "").strip()
            if not method:
                continue
            if "*" in method:
                continue
            if method in harness_tools:
                continue
            if known_abcp_methods is not None:
                if method not in known_abcp_methods:
                    errors.append(
                        f"phase {phase_id}: unknown method in worker_contract.{key}: {method!r}"
                    )
                continue
            if "." not in method:
                errors.append(
                    f"phase {phase_id}: unknown harness tool in worker_contract.{key}: {method!r}"
                )


def phase_contract(phase: JsonDict, override: Optional[JsonDict] = None) -> JsonDict:
    contract: JsonDict = dict(phase.get("worker_contract") or {})
    if override:
        contract.update(override)

    expected_artifact = dict(phase.get("expected_artifact") or {})
    if contract.get("expected_artifact"):
        merged = dict(expected_artifact)
        merged.update(contract.get("expected_artifact") or {})
        expected_artifact = merged

    validators = contract.get("validators")
    if not isinstance(validators, list):
        validators = list(phase.get("validators") or [])

    return {
        "version": "v1",
        "phase_id": str(contract.get("phase_id") or phase.get("id") or ""),
        "task_type": str(contract.get("task_type") or phase.get("task_type") or "general"),
        "objective": str(contract.get("objective") or phase.get("objective") or ""),
        "input_artifacts": contract.get("input_artifacts") or [],
        "expected_artifact": expected_artifact,
        "validators": validators,
        "allowed_methods": _string_list(contract.get("allowed_methods")),
        "forbidden_methods": _string_list(contract.get("forbidden_methods")),
        "max_surface_attempts": (
            contract.get("max_surface_attempts")
            if isinstance(contract.get("max_surface_attempts"), dict)
            else {}
        ),
        "must_record_extraction": bool(
            contract.get("must_record_extraction")
            if "must_record_extraction" in contract
            else True
        ),
        "stop_condition": str(
            contract.get("stop_condition")
            or "Record the required extraction artifact, then call final_answer."
        ),
    }


def write_task_plan(logger: RunLogger, plan: JsonDict) -> str:
    path = logger.task_dir / TASK_PLAN_FILE
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.write("task_plan.accepted", {"path": str(path.resolve()), "phaseCount": len(plan.get("phases", []))})
    return str(path.resolve())


def initialize_task_state(
    logger: RunLogger,
    plan: JsonDict,
    *,
    preserve_from: Optional[JsonDict] = None,
    replan_reason: str = "",
) -> JsonDict:
    previous_phases_raw = preserve_from.get("phases") if isinstance(preserve_from, dict) else None
    previous_phases: JsonDict = previous_phases_raw if isinstance(previous_phases_raw, dict) else {}
    raw_phases = plan.get("phases")
    plan_phases = raw_phases if isinstance(raw_phases, list) else []
    phases_state: JsonDict = {}
    replan_audit: Optional[JsonDict] = None
    if preserve_from is not None:
        replan_audit = {
            "at": utc_now_iso(),
            "reason": replan_reason,
            "preserved_phases": [],
            "reset_phase_failed": [],
            "new_phases": [],
            "removed_phases": sorted(
                set(str(pid) for pid in previous_phases.keys())
                - set(str(phase.get("id")) for phase in plan_phases if isinstance(phase, dict))
            ),
        }

    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id"))
        previous = previous_phases.get(phase_id)
        if isinstance(previous, dict):
            previous_status = str(previous.get("status") or "")
            if previous_status == "phase_failed":
                phases_state[phase_id] = _empty_phase_state()
                if replan_audit is not None:
                    replan_audit["reset_phase_failed"].append({
                        "phaseId": phase_id,
                        "previousStatus": previous_status,
                        "previousAttemptCount": len(previous.get("attempts") or []),
                    })
            else:
                preserved = copy.deepcopy(previous)
                if preserved.get("status") == "running":
                    preserved["status"] = "pending"
                    preserved["replan_reset_from"] = "running"
                _ensure_phase_state_defaults(preserved)
                phases_state[phase_id] = preserved
                if replan_audit is not None:
                    replan_audit["preserved_phases"].append({
                        "phaseId": phase_id,
                        "status": preserved.get("status"),
                        "attemptCount": len(preserved.get("attempts") or []),
                    })
        else:
            phases_state[phase_id] = _empty_phase_state()
            if replan_audit is not None:
                replan_audit["new_phases"].append(phase_id)

    state = {
        "version": "v1",
        "created_at": (
            preserve_from.get("created_at")
            if isinstance(preserve_from, dict) and preserve_from.get("created_at")
            else utc_now_iso()
        ),
        "updated_at": utc_now_iso(),
        "goal": plan.get("goal"),
        "current_phase": _first_active_phase_id(plan, phases_state),
        "phases": phases_state,
        "artifacts": list((preserve_from or {}).get("artifacts") or []),
        "completed_items": list((preserve_from or {}).get("completed_items") or []),
        "pending_items": list((preserve_from or {}).get("pending_items") or []),
        "failed_items": list((preserve_from or {}).get("failed_items") or []),
        "learned_strategies": list((preserve_from or {}).get("learned_strategies") or []),
        "banned_strategies": list((preserve_from or {}).get("banned_strategies") or []),
        "quality": dict((preserve_from or {}).get("quality") or {}),
    }
    if preserve_from is not None:
        state["replans"] = list((preserve_from or {}).get("replans") or [])
        state["replans"].append(replan_audit or {})
    write_task_state(logger, state)
    logger.write(
        "task_state.initialized",
        {
            "path": str(_state_path(logger).resolve()),
            "preserved": preserve_from is not None,
            "replanReason": replan_reason or None,
        },
    )
    return state


def load_task_state(logger: RunLogger) -> JsonDict:
    path = _state_path(logger)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _empty_phase_state() -> JsonDict:
    return {
        "status": "pending",
        "attempts": [],
        "validated_artifacts": [],
        "last_failure": None,
    }


def _ensure_phase_state_defaults(phase_state: JsonDict) -> None:
    phase_state.setdefault("status", "pending")
    phase_state.setdefault("attempts", [])
    phase_state.setdefault("validated_artifacts", [])
    phase_state.setdefault("last_failure", None)


def _first_active_phase_id(plan: JsonDict, phases_state: JsonDict) -> Optional[str]:
    for phase in plan.get("phases", []):
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        status = (
            phases_state.get(phase_id, {}).get("status")
            if isinstance(phases_state.get(phase_id), dict)
            else None
        )
        if status not in {"validated_done", "phase_failed"}:
            return phase_id
    return _first_phase_id(plan)


def write_task_state(logger: RunLogger, state: JsonDict) -> str:
    state["updated_at"] = utc_now_iso()
    path = _state_path(logger)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path.resolve())


def mark_phase_running(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
    worker_name: str,
) -> None:
    if not phase_id:
        return
    state = load_task_state(logger)
    phase_state = _phase_state(state, phase_id)
    if phase_state is None:
        return
    phase_state["status"] = "running"
    phase_state.setdefault("attempts", []).append({
        "workerId": worker_id,
        "name": worker_name,
        "started_at": utc_now_iso(),
        "status": "running",
    })
    state["current_phase"] = phase_id
    write_task_state(logger, state)


def mark_phase_result(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
    validation: Optional[JsonDict],
    result_status: str,
) -> None:
    if not phase_id:
        return
    state = load_task_state(logger)
    phase_state = _phase_state(state, phase_id)
    if phase_state is None:
        return

    attempts = phase_state.setdefault("attempts", [])
    attempt = None
    for item in reversed(attempts):
        if item.get("workerId") == worker_id:
            attempt = item
            break
    if attempt is None:
        attempt = {"workerId": worker_id}
        attempts.append(attempt)
    attempt["finished_at"] = utc_now_iso()
    attempt["status"] = result_status
    if validation:
        attempt["validation"] = trim_large_strings(validation, 2000)

    if result_status in {
        "blocked_by_challenge",
        "hitl_required",
        "hitl_timeout",
        "page_settled_after_hitl",
    }:
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "challenge_blocker",
            "status": result_status,
            "message": (
                "Worker reported a challenge/HITL blocker; do not retry this"
                " phase with the same browser strategy without user action or"
                " a deliberate pivot."
            ),
        }]
        write_task_state(logger, state)
        return

    if validation and validation.get("status") == "done":
        phase_state["status"] = "validated_done"
        artifacts = validation.get("artifacts") or []
        phase_state["validated_artifacts"] = artifacts
        phase_state["last_failure"] = None
        _append_unique(state.setdefault("artifacts", []), artifacts)
    else:
        phase_state["status"] = "validation_failed" if validation else result_status
        phase_state["last_failure"] = (
            validation.get("failures") if isinstance(validation, dict) else None
        )

    write_task_state(logger, state)


def next_pending_phase(plan: Optional[JsonDict], logger: RunLogger) -> Optional[JsonDict]:
    if not plan:
        return None
    state = load_task_state(logger)
    raw_phases_state = state.get("phases")
    phases: JsonDict = raw_phases_state if isinstance(raw_phases_state, dict) else {}
    raw_plan_phases = plan.get("phases")
    plan_phases = raw_plan_phases if isinstance(raw_plan_phases, list) else []
    changed = False
    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        raw_phase_state = phases.get(phase_id)
        phase_state: JsonDict = raw_phase_state if isinstance(raw_phase_state, dict) else {}
        status = phase_state.get("status")
        if status in {
            "validated_done",
            "phase_failed",
            "blocked_by_challenge",
            "hitl_required",
            "hitl_timeout",
            "page_settled_after_hitl",
        }:
            continue
        attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
        attempts_count = len(attempts) if isinstance(attempts, list) else 0
        max_attempts = _positive_int(phase.get("max_attempts"), default=2)
        if attempts_count >= max_attempts and status in {
            "validation_failed",
            "failed",
            "cancelled",
            "unknown",
        }:
            phase_state["status"] = "phase_failed"
            phase_state["exhausted_at"] = utc_now_iso()
            phase_state["max_attempts"] = max_attempts
            changed = True
            continue
        if status not in {"validated_done"}:
            if changed:
                write_task_state(logger, state)
            return phase
    if changed:
        write_task_state(logger, state)
    return None


def find_phase(plan: Optional[JsonDict], phase_id: Optional[str]) -> Optional[JsonDict]:
    if not plan or not phase_id:
        return None
    for phase in plan.get("phases", []):
        if isinstance(phase, dict) and str(phase.get("id")) == str(phase_id):
            return phase
    return None


def validate_worker_artifacts(
    *,
    contract: Optional[JsonDict],
    artifacts: List[str],
    task_dir: Path,
) -> JsonDict:
    if not contract:
        return {"status": "skipped", "reason": "no worker_contract"}

    expected = contract.get("expected_artifact") if isinstance(contract, dict) else {}
    if not isinstance(expected, dict):
        expected = {}
    validators = contract.get("validators") if isinstance(contract, dict) else []
    if not isinstance(validators, list):
        validators = []
    validators = _normalize_validators(expected, validators, [], phase_id=str(contract.get("phase_id") or "worker"))

    extraction_artifacts = [
        path for path in artifacts
        if "/artifacts/extractions/" in str(path)
    ]
    failures: List[JsonDict] = []
    loaded = _load_extraction_artifacts(extraction_artifacts, task_dir)
    expected_name = str(expected.get("name") or "").strip()
    candidates = [
        item for item in loaded
        if not expected_name or item.get("payload", {}).get("name") == expected_name
    ]

    must_record = bool(contract.get("must_record_extraction", True))
    if must_record and not candidates:
        failures.append({
            "type": "artifact_required",
            "message": (
                f"expected record_extraction artifact"
                + (f" named {expected_name!r}" if expected_name else "")
            ),
            "availableArtifacts": extraction_artifacts,
        })

    selected = candidates[0] if candidates else (loaded[0] if loaded else None)
    rows: List[JsonDict] = []
    if selected:
        payload = selected.get("payload") or {}
        raw_rows = payload.get("rows")
        if isinstance(raw_rows, list):
            rows = [row for row in raw_rows if isinstance(row, dict)]
        else:
            failures.append({
                "type": "schema",
                "message": "selected artifact has no rows array",
                "path": selected.get("path"),
            })

    for validator in validators:
        failures.extend(_run_validator(validator, rows))

    status = "done" if not failures else "failed"
    return {
        "status": status,
        "phase_id": contract.get("phase_id"),
        "expectedArtifact": expected,
        "rowCount": len(rows),
        "artifacts": [selected.get("path")] if selected else [],
        "allExtractionArtifacts": extraction_artifacts,
        "failures": failures,
    }


def _run_validator(validator: JsonDict, rows: List[JsonDict]) -> List[JsonDict]:
    validator_type = str(validator.get("type") or "").strip()
    failures: List[JsonDict] = []

    if validator_type == "artifact_required":
        return []

    if validator_type == "required_fields":
        fields = _string_list(validator.get("fields"))
        for index, row in enumerate(rows):
            missing = [field for field in fields if field not in row]
            if missing:
                failures.append({
                    "type": validator_type,
                    "row": index,
                    "missing": missing,
                })
        return failures

    if validator_type == "field_nonempty":
        fields = _string_list(validator.get("fields"))
        for index, row in enumerate(rows):
            empty = [
                field for field in fields
                if row.get(field) is None or str(row.get(field)).strip() == ""
            ]
            if empty:
                failures.append({"type": validator_type, "row": index, "empty": empty})
        return failures

    if validator_type in {"min_rows", "max_rows", "exact_rows"}:
        value = _positive_int(validator.get("value"), default=0)
        count = len(rows)
        ok = (
            count >= value if validator_type == "min_rows"
            else count <= value if validator_type == "max_rows"
            else count == value
        )
        if not ok:
            failures.append({
                "type": validator_type,
                "expected": value,
                "actual": count,
            })
        return failures

    if validator_type == "unique":
        field = str(validator.get("field") or "").strip()
        seen: Dict[str, int] = {}
        duplicates: List[JsonDict] = []
        for index, row in enumerate(rows):
            value = str(row.get(field) or "")
            if value in seen:
                duplicates.append({"row": index, "firstRow": seen[value], "value": value})
            else:
                seen[value] = index
        if duplicates:
            failures.append({"type": validator_type, "field": field, "duplicates": duplicates[:20]})
        return failures

    if validator_type == "url_pattern":
        field = str(validator.get("field") or "url").strip()
        pattern = str(validator.get("pattern") or r"^https?://").strip()
        regex = re.compile(pattern)
        bad = [
            {"row": index, "value": row.get(field)}
            for index, row in enumerate(rows)
            if not regex.search(str(row.get(field) or ""))
        ]
        if bad:
            failures.append({"type": validator_type, "field": field, "pattern": pattern, "bad": bad[:20]})
        return failures

    if validator_type == "field_pattern":
        field = str(validator.get("field") or "").strip()
        pattern = str(validator.get("pattern") or "").strip()
        regex = _compile_validator_regex(pattern, validator)
        bad = [
            {"row": index, "value": row.get(field)}
            for index, row in enumerate(rows)
            if not regex.search(str(row.get(field) or ""))
        ]
        if bad:
            failures.append({
                "type": validator_type,
                "field": field,
                "pattern": pattern,
                "bad": bad[:20],
            })
        return failures

    if validator_type == "cross_field_contains":
        field = str(validator.get("field") or "").strip()
        contains_field = str(
            validator.get("contains_field")
            or validator.get("expected_field")
            or ""
        ).strip()
        case_sensitive = bool(validator.get("case_sensitive", False))
        bad = []
        for index, row in enumerate(rows):
            haystack = str(row.get(field) or "")
            needle = str(row.get(contains_field) or "")
            if not needle:
                bad.append({
                    "row": index,
                    "field": contains_field,
                    "reason": "expected field empty",
                })
                continue
            left = haystack if case_sensitive else haystack.lower()
            right = needle if case_sensitive else needle.lower()
            if right not in left:
                bad.append({
                    "row": index,
                    "field": field,
                    "contains_field": contains_field,
                    "value": haystack[:200],
                    "expected": needle[:200],
                })
        if bad:
            failures.append({
                "type": validator_type,
                "field": field,
                "contains_field": contains_field,
                "bad": bad[:20],
            })
        return failures

    if validator_type == "action_outcome":
        failures.extend(_validate_action_outcome(validator, rows))
        return failures

    if validator_type == "allowed_domain":
        field = str(validator.get("field") or "url").strip()
        domains = set(_string_list(validator.get("domains")))
        bad = []
        for index, row in enumerate(rows):
            host = urlparse(str(row.get(field) or "")).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host not in domains:
                bad.append({"row": index, "host": host, "value": row.get(field)})
        if bad:
            failures.append({"type": validator_type, "field": field, "domains": sorted(domains), "bad": bad[:20]})
        return failures

    if validator_type == "set_equals":
        field = str(validator.get("field") or "").strip()
        expected = {str(item) for item in (validator.get("values") or [])}
        actual = {str(row.get(field)) for row in rows}
        if actual != expected:
            failures.append({
                "type": validator_type,
                "field": field,
                "expected": sorted(expected),
                "actual": sorted(actual),
            })
        return failures

    if validator_type == "range":
        field = str(validator.get("field") or "").strip()
        min_value = validator.get("min")
        max_value = validator.get("max")
        bad = []
        for index, row in enumerate(rows):
            raw_value = row.get(field)
            if raw_value is None:
                bad.append({"row": index, "value": raw_value, "reason": "not numeric"})
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                bad.append({"row": index, "value": raw_value, "reason": "not numeric"})
                continue
            if min_value is not None and value < float(min_value):
                bad.append({"row": index, "value": value, "reason": "below min"})
            if max_value is not None and value > float(max_value):
                bad.append({"row": index, "value": value, "reason": "above max"})
        if bad:
            failures.append({"type": validator_type, "field": field, "bad": bad[:20]})
        return failures

    return [{
        "type": "unknown_validator",
        "validator": validator_type,
        "message": f"validator type must be one of {sorted(VALIDATOR_TYPES)}",
    }]


def _validate_action_outcome(
    validator: JsonDict,
    rows: List[JsonDict],
) -> List[JsonDict]:
    url_field = str(validator.get("url_field") or "url").strip()
    heading_field = str(validator.get("heading_field") or "heading").strip()
    expected_url_pattern = str(validator.get("expected_url_pattern") or "").strip()
    expected_heading_pattern = str(
        validator.get("expected_heading_pattern") or ""
    ).strip()
    heading_matches_field = str(
        validator.get("heading_matches_field") or ""
    ).strip()
    min_similarity = _float_value(validator.get("min_similarity"), default=0.8)

    url_regex = (
        _compile_validator_regex(expected_url_pattern, validator)
        if expected_url_pattern
        else None
    )
    heading_regex = (
        _compile_validator_regex(expected_heading_pattern, validator)
        if expected_heading_pattern
        else None
    )

    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        reasons: List[JsonDict] = []
        url_value = str(row.get(url_field) or "")
        heading_value = str(row.get(heading_field) or "")
        if url_regex and not url_regex.search(url_value):
            reasons.append({
                "reason": "url_pattern_mismatch",
                "field": url_field,
                "value": url_value[:300],
                "pattern": expected_url_pattern,
            })
        if heading_regex and not heading_regex.search(heading_value):
            reasons.append({
                "reason": "heading_pattern_mismatch",
                "field": heading_field,
                "value": heading_value[:300],
                "pattern": expected_heading_pattern,
            })
        if heading_matches_field:
            expected = str(row.get(heading_matches_field) or "")
            score = _similarity(heading_value, expected)
            if expected and score < min_similarity:
                reasons.append({
                    "reason": "heading_similarity_below_threshold",
                    "field": heading_field,
                    "matches_field": heading_matches_field,
                    "similarity": round(score, 3),
                    "min_similarity": min_similarity,
                    "value": heading_value[:300],
                    "expected": expected[:300],
                })
            elif not expected:
                reasons.append({
                    "reason": "expected_heading_field_empty",
                    "matches_field": heading_matches_field,
                })
        if reasons:
            bad.append({"row": index, "failures": reasons})

    if not bad:
        return []
    return [{"type": "action_outcome", "bad": bad[:20]}]


def _compile_validator_regex(pattern: str, validator: JsonDict) -> re.Pattern[str]:
    flags = re.I if bool(validator.get("case_insensitive", False)) else 0
    return re.compile(pattern or r"$^", flags)


def _similarity(left: str, right: str) -> float:
    left_norm = _norm_compare_text(left)
    right_norm = _norm_compare_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm in right_norm or right_norm in left_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _norm_compare_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_validators(
    expected_artifact: JsonDict,
    validators: List[Any],
    errors: List[str],
    *,
    phase_id: str,
) -> List[JsonDict]:
    normalized: List[JsonDict] = []
    if expected_artifact.get("min_rows") is not None:
        normalized.append({"type": "min_rows", "value": expected_artifact.get("min_rows")})
    if expected_artifact.get("max_rows") is not None:
        normalized.append({"type": "max_rows", "value": expected_artifact.get("max_rows")})
    if expected_artifact.get("exact_rows") is not None:
        normalized.append({"type": "exact_rows", "value": expected_artifact.get("exact_rows")})
    fields = expected_artifact.get("required_fields")
    if isinstance(fields, list) and fields:
        normalized.append({"type": "required_fields", "fields": fields})
        normalized.append({"type": "field_nonempty", "fields": fields})

    for index, validator in enumerate(validators):
        if not isinstance(validator, dict):
            errors.append(f"phase {phase_id}: validators[{index}] must be an object")
            continue
        validator_type = str(validator.get("type") or "").strip()
        if validator_type not in VALIDATOR_TYPES:
            errors.append(
                f"phase {phase_id}: validators[{index}].type must be one of {sorted(VALIDATOR_TYPES)}"
            )
        normalized.append(dict(validator))
    return normalized


def _load_extraction_artifacts(paths: List[str], task_dir: Path) -> List[JsonDict]:
    loaded: List[JsonDict] = []
    root = task_dir.resolve(strict=False)
    for raw_path in paths:
        try:
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            loaded.append({"path": str(resolved), "payload": payload})
    return loaded


def _state_path(logger: RunLogger) -> Path:
    return logger.task_dir / TASK_STATE_FILE


def _first_phase_id(plan: JsonDict) -> Optional[str]:
    for phase in plan.get("phases", []):
        if isinstance(phase, dict):
            return str(phase.get("id") or "")
    return None


def _phase_state(state: JsonDict, phase_id: str) -> Optional[JsonDict]:
    phases = state.get("phases")
    if not isinstance(phases, dict):
        return None
    phase_state = phases.get(str(phase_id))
    return phase_state if isinstance(phase_state, dict) else None


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _append_unique(target: List[Any], values: List[Any]) -> None:
    seen = {str(item) for item in target}
    for value in values:
        key = str(value)
        if key not in seen:
            target.append(value)
            seen.add(key)
