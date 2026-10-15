"""Hand off scoped JS/TS platform observations to the ordinary target guard.

Only platform call records are evidence. Business output, echoed request IDs
and the ordering of the results array are not proof of a current observation.
"""

import math
import re
from typing import Any

from harness.observation.page_observation import hydrate_page_observation_response
from .axtree_state import (
    AXTREE_INVALIDATING_METHODS,
    _invalidate_axtree_snapshot,
    _observe_axtree_state_after,
    _precompute_axtree_snapshot,
)


def prepare_workflow_observations(params: dict, response: Any) -> dict | None:
    """Read artifacts before offload/projection, after the host scoped the SDK.

The caller must have passed the package through scope_package: call records
omit input parameters, so only that host binding proves the mutation page.
This sidecar stays in process and does not enlarge the model's receipt.
"""
    data = response.get("data") if isinstance(response, dict) else None
    page_id = str(params.get("pageId") or "")
    if not page_id or not isinstance(data, dict) or "output" not in data:
        return None
    records = data.get("results")
    if not isinstance(records, list):
        return None
    actions = [row for row in records if isinstance(row, dict) and row.get("kind") == "action"]
    plan = {"pageId": page_id, "workflowId": data.get("workflowId"),
            "mutations": [], "observations": [], "ordered": True}
    timed = []
    for row in actions:
        method = str(row.get("action") or "")
        value = row.get("value")
        child = {"response": {"data": value}} if isinstance(value, dict) else {}
        # Match the ordinary action path's positive no-mutation evidence.
        mutates = method in AXTREE_INVALIDATING_METHODS and not (
            isinstance(value, dict) and (
                (method == "Page.go" and value.get("navigationStarted") is False)
                or (method == "DOM.inspectSelect" and value.get("controlKind") == "native")
            )
        )
        if mutates:
            plan["mutations"].append(method)
        start, end = row.get("startedAt"), row.get("finishedAt")
        if (not all(type(t) in (int, float) and math.isfinite(t) for t in (start, end))
                or start > end or row.get("status") not in {"succeeded", "failed"}):
            plan["ordered"] = False
            continue
        if isinstance(value, dict) and value.get("pageId", page_id) != page_id:
            plan["ordered"] = False
        timed.append((start, end, row, method, child, mutates))
    timed.sort(key=lambda item: (item[0], item[1]))
    if any(left[1] > right[0] for left, right in zip(timed, timed[1:])):
        plan["ordered"] = False
    if not plan["ordered"] or data.get("status") != "succeeded":
        return plan
    # Replay in measured execution order; a later action invalidates an earlier
    # read exactly as it would for separate browser_call invocations.
    for _start, _end, row, method, child, mutates in timed:
        if mutates:
            plan["observations"].append((method, child, None))
        if row.get("status") != "succeeded" or method not in {"DOM.getAXTree", "DOM.getDetails"}:
            continue
        value = row.get("value")
        if not isinstance(value, dict) or value.get("pageId") != page_id:
            continue
        if method == "DOM.getDetails" and value.get("source") != "live":
            continue  # Historical details cannot authorize current targets.
        reference = value.get("artifact")
        if method == "DOM.getAXTree" or isinstance(reference, dict):
            if (not isinstance(reference, dict)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256") or ""))
                    or type(reference.get("bytes")) is not int):
                continue
        child_params = {"pageId": page_id}
        if method == "DOM.getDetails":
            child_params["query"] = {}  # Only the response's live form is admitted.
        hydrated = hydrate_page_observation_response(child_params, {"data": value}, method=method)
        out = hydrated["data"]
        observation = out.get("observation") or {}
        if (out.get("observationError") or not observation.get("documentEpoch")
                or observation.get("pendingChanges") is True
                or observation.get("freshness") not in {None, "current"}):
            continue
        snapshot = _precompute_axtree_snapshot(method, child_params, hydrated)
        if method == "DOM.getAXTree" and not snapshot.get("nodes"):
            continue
        plan["observations"].append((method, {"response": hydrated}, snapshot))
    return plan


def apply_workflow_observations(agent: Any, plan: dict | None, result: dict,
                                *, event_serial_before: int) -> None:
    """Apply only after authorization fencing, without reviving raced evidence."""
    if plan is None:
        return
    data = (result.get("response") or {}).get("data") or {}
    accepted = (not result.get("error") and not result.get("isError")
                and data.get("status") == "succeeded"
                and data.get("workflowId") == plan["workflowId"])
    raced = int(getattr(agent, "axtree_event_serial", 0) or 0) != event_serial_before
    params = {"pageId": plan["pageId"]}
    if not accepted or raced or not plan["ordered"]:
        # A failed/cancelled script or overlapping calls may already have had
        # effects. Retire old targets on navigation; never declare a read fresh.
        for method in plan["mutations"]:
            _invalidate_axtree_snapshot(agent, method, params)
        applied = 0
    else:
        for method, child, snapshot in plan["observations"]:
            _observe_axtree_state_after(agent, method, params, child,
                                       precomputed_snapshot=snapshot,
                                       event_serial_before=event_serial_before)
        applied = sum(method in {"DOM.getAXTree", "DOM.getDetails"}
                      for method, _child, _snapshot in plan["observations"])
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("workflow.observation_handoff", {
            "workflowId": plan["workflowId"], "pageId": plan["pageId"],
            "observationsApplied": applied, "accepted": accepted,
            "eventRace": raced, "orderedCalls": plan["ordered"],
        })
