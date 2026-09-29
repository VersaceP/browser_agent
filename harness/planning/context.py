"""Shared factual context for delegation, approval and focused reviewers."""
from __future__ import annotations

import copy
import hashlib
import json


def user_context(logger, original_task, *, state=None):
    if state is None:
        from harness.task_control import load_task_state
        state = load_task_state(logger)
    records = state.get("operator_inputs") or {}
    order = list(dict.fromkeys([*(state.get("operator_input_order") or []), *records]))
    # Approval/control acknowledgements are archived in the ledger, but are
    # not new task requirements and do not invalidate a semantic review.
    inputs = [copy.deepcopy(records[key]) for key in order if key in records
              and not (records[key].get("candidateHash")
                       and records[key].get("decision") in {"approved", "details"})]
    return {"originalUserTask": original_task, "operatorInputs": inputs}


def assignment_view(phase):
    """The executable assignment, without repeated runtime/user-context copies."""
    contract = phase.get("worker_contract") or {}
    meta = contract.get("_delegation") or {}
    return {
        "id": phase.get("id"),
        "task": phase.get("worker_task") or phase.get("objective"),
        "stage": phase.get("stage_hint"),
        "dependsOn": phase.get("depends_on") or [],
        "inputs": phase.get("input_artifacts") or phase.get("inputs") or {},
        "output": phase.get("expected_artifact") or {},
        "checks": phase.get("validators") or [],
        "policy": {key: value for key, value in contract.items()
                   if key not in {"_delegation", "_user_context", "expected_artifact", "validators"}},
        "budget": {"maxSteps": phase.get("max_steps"), "maxAttempts": phase.get("max_attempts")},
        "replaces": meta.get("replaces"),
        "lineage": meta.get("lineage"),
        "reason": meta.get("reason") or "",
    }


def approval_view(plan):
    """One display/classification target; selection is explicit on resume."""
    phases = [p for p in plan.get("phases", []) if isinstance(p, dict)]
    selected = plan.get("_approvalAssignmentId")
    phase = next((p for p in phases if p.get("id") == selected), None) if selected else (phases[-1] if phases else None)
    if phase is None:
        raise ValueError("approval requires an existing assignment")
    return {"scope": "assignment", "originalGoal": plan.get("goal"),
            "assignment": assignment_view(phase)}


def retain_operator_inputs(logger, records):
    """Persist raw user input once, preserving order across JSON backends."""
    from harness.task_control import load_task_state, write_task_state
    if not records:
        return
    state = load_task_state(logger)
    saved = state.setdefault("operator_inputs", {})
    order = state.setdefault("operator_input_order", list(saved))
    for record in records:
        if not isinstance(record, dict):
            continue
        key = str(record.get("inputId") or hashlib.sha256(
            json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        saved[key] = {**saved.get(key, {}), **copy.deepcopy(record)}
        if key not in order:
            order.append(key)
    write_task_state(logger, state)

