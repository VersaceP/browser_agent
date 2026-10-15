"""Transport recovery shared by task owners; never replays browser actions."""
import asyncio
from typing import Any, Optional
from harness.task_control import load_task_state
from harness.task_control.transport_recovery import (
    TRANSPORT_RECOVERY_KEY, note_transport_recovery_required,
    record_transport_recovery_probe,
)
from harness.utils import JsonDict

async def recover_worker_transport(
    agent: Any,
    completed: Any,
) -> Optional[JsonDict]:
    """Probe once for a new fatal transport batch before the task owner sees the wait.

    This is deliberately limited to the control-plane probe implemented by the
    spawner.  It never retries the failed browser Action, starts a worker, or
    creates a Fleet. Each run probes a fatal batch once; a fresh resume may
    probe the persisted blocker again after the external endpoint recovers.
    """
    results = completed if isinstance(completed, list) else []
    required = note_transport_recovery_required(agent.logger, results)
    if not isinstance(required, dict):
        return None
    # A live connection already spent its outage window before returning this
    # failure. Preserve that receipt instead of starting another 60-second probe.
    recovered = next((item.get("connectionRecovery") for item in results
                      if isinstance(item, dict) and isinstance(item.get("connectionRecovery"), dict)), None)
    return await _probe_worker_transport(agent, required, recovery_override=recovered)


async def resume_worker_transport(agent: Any) -> Optional[JsonDict]:
    """Recover a persisted transport blocker before the first worker dispatch."""
    required = load_task_state(agent.logger).get(TRANSPORT_RECOVERY_KEY)
    if not isinstance(required, dict) or required.get("status") not in {"pending", "blocked"}:
        return None
    return await _probe_worker_transport(agent, required)


async def _probe_worker_transport(agent: Any, required: JsonDict, recovery_override=None) -> JsonDict:
    if str(required.get("status") or "") == "ready":
        return dict(required)
    # Worker IDs start over in a fresh process. Scope the receipt to the run
    # and the originating failure batch, so neither repeated waits nor a
    # later failure with the same worker/method can reuse the wrong probe.
    key = (str(getattr(agent.logger, "run_id", "") or ""),
           str(required.get("requiredRunId") or ""),
           str(required.get("fingerprint") or ""))
    receipts = getattr(agent, "_transport_probe_receipts", None)
    if receipts is None:
        receipts = agent._transport_probe_receipts = {}
    if key in receipts:
        return dict(await asyncio.shield(receipts[key]))
    pending = asyncio.get_running_loop().create_future()
    receipts[key] = pending
    try:
        receipt = await _perform_worker_transport_probe(agent, required, recovery_override)
        pending.set_result(dict(receipt))
        return receipt
    except BaseException:
        receipts.pop(key, None)
        pending.cancel()
        raise


async def _perform_worker_transport_probe(agent: Any, required: JsonDict, recovery_override=None) -> JsonDict:
    bootstrap_failure = getattr(agent, "_browser_connection_failure", None) or {}
    if recovery_override is None and isinstance(bootstrap_failure, dict):
        recovery_override = bootstrap_failure.get("connectionRecovery")
    probe = getattr(agent.spawner, "refresh_browser_connection", None)
    if recovery_override is not None:
        recovery = recovery_override
    elif not callable(probe):
        recovery = {
            "status": "blocked",
            "reason": "transport recovery probe is unavailable",
            "businessActionsReplayed": 0,
        }
    else:
        try:
            recovery = await probe(
                str(getattr(agent, "task_fleet_reference", "") or "")
            )
        except Exception as exc:
            # A probe failure is a control-plane blocker, not a reason to let
            # the caller retry business work or lose the durable fatal receipt.
            recovery = {
                "status": "blocked",
                "reason": type(exc).__name__,
                "businessActionsReplayed": 0,
            }
    recorded = record_transport_recovery_probe(
        agent.logger, recovery, expected_batch=required,
    )
    receipt = dict(recovery) if isinstance(recovery, dict) else {
        "status": "blocked",
        "reason": "invalid recovery probe receipt",
    }
    receipt["requiredFingerprint"] = required.get("fingerprint")
    try:
        receipt["businessActionsReplayed"] = max(
            0, int(receipt.get("businessActionsReplayed") or 0)
        )
    except (TypeError, ValueError):
        receipt["businessActionsReplayed"] = 0
    if isinstance(recorded, dict):
        receipt["controlState"] = recorded.get("status")
        receipt["probeAttempts"] = recorded.get("probeAttempts")
    return receipt

