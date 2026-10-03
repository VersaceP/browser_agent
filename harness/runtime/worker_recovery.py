"""Transport recovery shared by task owners; never replays browser actions."""
from typing import Any, Optional
from harness.task_control.transport_recovery import note_transport_recovery_required, record_transport_recovery_probe
from harness.utils import JsonDict

async def recover_worker_transport(
    agent: Any,
    completed: Any,
) -> Optional[JsonDict]:
    """Probe once for a new fatal transport batch before the task owner sees the wait.

    This is deliberately limited to the control-plane probe implemented by the
    spawner.  It never retries the failed browser Action, starts a worker, or
    creates a Fleet.  The durable fingerprint prevents repeated waits from
    issuing the same probe again; a new fatal worker result creates a new batch.
    """
    results = completed if isinstance(completed, list) else []
    required = note_transport_recovery_required(agent.logger, results)
    if not isinstance(required, dict):
        return None
    if str(required.get("status") or "") == "ready":
        return dict(required)
    probe = getattr(agent.spawner, "refresh_browser_connection", None)
    if not callable(probe):
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
        agent.logger, recovery,
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

