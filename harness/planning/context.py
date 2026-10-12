"""Shared factual context for task owners and focused reviewers."""
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

