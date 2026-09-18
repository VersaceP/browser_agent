"""Fleet-auth generation fencing for opaque Workflow.execute calls."""

from __future__ import annotations

from typing import Any, Dict


JsonDict = Dict[str, Any]


def _identity(agent: Any, fleet_id: str) -> tuple[Any, str, str, int]:
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet = str(
        fleet_id or getattr(agent, "assigned_fleet_id", "") or ""
    ).strip()
    worker = str(getattr(agent, "worker_id", "") or "").strip()
    seen = int(getattr(agent, "fleet_barrier_generation", 0) or 0)
    return barrier, fleet, worker, seen


def _log(agent: Any, event: str, payload: JsonDict) -> None:
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:
            pass


def _event_payload(
    receipt: JsonDict,
    *,
    source: str,
    run_id: str,
    worker_id: str,
) -> JsonDict:
    """Return the stable cross-path telemetry envelope for workflow fences."""

    return {
        **dict(receipt),
        "source": str(source or "workflow"),
        "method": "Workflow.execute",
        "runId": str(run_id or "") or None,
        "workerId": worker_id,
    }


async def workflow_auth_fence_before(
    agent: Any,
    *,
    fleet_id: str,
    run_id: str,
    source: str,
) -> JsonDict:
    barrier, fleet, worker, seen = _identity(agent, fleet_id)
    if barrier is None or not fleet or not worker:
        return {
            "allowed": True,
            "generation": seen,
            "generationChanged": False,
            "unmanaged": True,
        }
    receipt = await barrier.workflow_fence_before(
        fleet,
        worker,
        seen_generation=seen,
    )
    payload = _event_payload(
        receipt,
        source=source,
        run_id=run_id,
        worker_id=worker,
    )
    _log(agent, "workflow.auth_fence.before", payload)
    return payload


async def workflow_auth_fence_after(
    agent: Any,
    *,
    fleet_id: str,
    run_id: str,
    started_generation: int,
    source: str,
) -> JsonDict:
    barrier, fleet, worker, _seen = _identity(agent, fleet_id)
    if barrier is None or not fleet or not worker:
        return {
            "valid": True,
            "generation": int(started_generation),
            "generationChanged": False,
            "unmanaged": True,
        }
    receipt = await barrier.workflow_fence_after(
        fleet,
        started_generation=int(started_generation),
    )
    payload = _event_payload(
        receipt,
        source=source,
        run_id=run_id,
        worker_id=worker,
    )
    if not receipt.get("valid"):
        if receipt.get("generationChanged"):
            _log(agent, "workflow.auth_generation_changed", payload)
        _log(agent, "workflow.row_quarantined", payload)
    return payload


def auth_fence_failure_result(receipt: JsonDict, *, run_id: str) -> JsonDict:
    reason = str(
        receipt.get("reasonKind")
        or receipt.get("status")
        or "workflow_auth_fenced"
    )
    return {
        "succeeded": False,
        "runId": run_id,
        "failedStepPath": None,
        "failedError": reason,
        "failedPurpose": "workflow auth-generation fence",
        "variables": {},
        "priorResults": [],
        "exc": reason,
        "authFence": dict(receipt),
    }


def auth_fence_outcome(run_result: Any) -> JsonDict | None:
    if not isinstance(run_result, dict):
        return None
    receipt = run_result.get("authFence")
    return dict(receipt) if isinstance(receipt, dict) else None


async def reperceive_workflow_auth_generation(
    agent: Any,
    *,
    page_id: str,
    target_generation: int,
    source: str,
) -> bool:
    """Refresh state/tree through the ordinary guarded browser-call path."""

    if not page_id:
        return False
    override = getattr(agent, "_workflow_auth_reperceive", None)
    if callable(override):
        result = override(
            page_id=page_id,
            target_generation=int(target_generation),
            source=source,
        )
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)
    from harness.results.call_outcome import classify_call_outcome
    from harness.tools.browser_tools import _invoke_browser_method

    state = await _invoke_browser_method(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": (
                "Re-perceive page state after shared authentication generation"
                f" changed during {source}"
            ),
        },
        0,
        count_progress=False,
    )
    # Same contract as the barrier's own re-perception: this asks "did this
    # worker re-read the page", a fact about the CALL. `_invoke_result_failed`
    # also fails on `response.data.error`, which for Page.getState is the
    # PAGE's last-navigation error — permanent on a risk-controlled page, so a
    # workflow could never clear its fence by re-reading. Same deadlock as
    # task 48b4d7d7, different site in the code.
    if not classify_call_outcome(state).succeeded:
        return False
    tree = await _invoke_browser_method(
        agent,
        "DOM.getAXTree",
        {
            "pageId": page_id,
            "purpose": (
                "Refresh DOM identity after shared authentication generation"
                f" changed during {source}"
            ),
        },
        0,
        count_progress=False,
    )
    if not classify_call_outcome(tree).succeeded:
        return False
    observed = int(getattr(agent, "fleet_barrier_generation", -1) or -1)
    return observed == int(target_generation)
