"""Durable control-plane state for fatal browser transport recovery.

This module records only transport facts.  A successful probe proves that a
new control connection can register, read capabilities and list Fleets; it
does not replay the browser operation that was interrupted.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from harness.utils import JsonDict, RunLogger


TRANSPORT_RECOVERY_KEY = "transport_recovery"


def _nonnegative_int(value: Any, default: int = 0) -> int:
    """Read a persisted counter without letting a malformed receipt crash recovery."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _tc():
    import harness.task_control as task_control

    return task_control


def _failure_item(result: Any) -> Optional[JsonDict]:
    if not isinstance(result, dict):
        return None
    transport = result.get("transportFailure")
    transport = transport if isinstance(transport, dict) else {}
    classification = result.get("errorClassification")
    classification = classification if isinstance(classification, dict) else {}
    recovery_facts = result.get("recoveryFacts")
    failure = (
        recovery_facts.get("failure")
        if isinstance(recovery_facts, dict)
        else None
    )
    failure = failure if isinstance(failure, dict) else {}
    connection_fatal = any(
        value is True
        for value in (
            result.get("connectionFatal"),
            transport.get("connectionFatal"),
            classification.get("connectionFatal"),
            failure.get("connectionFatal"),
        )
    )
    if not connection_fatal:
        return None
    code = str(
        result.get("transportCode")
        or transport.get("code")
        or transport.get("transportCode")
        or classification.get("errorCode")
        or failure.get("transportCode")
        or "ABCP_TRANSPORT_UNKNOWN"
    ).strip()
    method = str(
        result.get("rpcMethod")
        or transport.get("method")
        or classification.get("method")
        or failure.get("method")
        or ""
    ).strip()
    return {
        "workerId": str(result.get("workerId") or "").strip(),
        "phaseId": str(result.get("phaseId") or "").strip(),
        "slotId": str(result.get("slotId") or "").strip(),
        "transportCode": code,
        "method": method,
        "requestSent": (
            result.get("requestSent")
            if isinstance(result.get("requestSent"), bool)
            else transport.get("requestSent")
        ),
    }


def transport_failure_items(results: Iterable[Any]) -> List[JsonDict]:
    items: List[JsonDict] = []
    for result in results if isinstance(results, (list, tuple)) else []:
        item = _failure_item(result)
        if item is not None:
            items.append(item)
    items.sort(
        key=lambda item: (
            item.get("workerId") or "",
            item.get("phaseId") or "",
            item.get("slotId") or "",
            item.get("transportCode") or "",
            item.get("method") or "",
        )
    )
    return items


def transport_failure_fingerprint(items: Iterable[Any]) -> str:
    normalized = [
        {
            key: item.get(key)
            for key in (
                "workerId",
                "phaseId",
                "slotId",
                "transportCode",
                "method",
                "requestSent",
            )
        }
        for item in items
        if isinstance(item, dict)
    ]
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _recovery_record(state: JsonDict) -> Optional[JsonDict]:
    value = state.get(TRANSPORT_RECOVERY_KEY)
    return value if isinstance(value, dict) else None


def note_transport_recovery_required(
    logger: RunLogger, results: Iterable[Any],
) -> Optional[JsonDict]:
    """Persist a new fatal transport batch and return its control record."""
    items = transport_failure_items(results)
    if not items:
        return None
    fingerprint = transport_failure_fingerprint(items)
    state = _tc().load_task_state(logger)
    current = _recovery_record(state)
    if (
        isinstance(current, dict)
        and str(current.get("fingerprint") or "") == fingerprint
        and str(current.get("status") or "") in {"pending", "blocked", "ready"}
    ):
        return dict(current)
    record: JsonDict = {
        "status": "pending",
        "fingerprint": fingerprint,
        "fatalCount": len(items),
        "workers": items,
        "requiredAt": _tc().utc_now_iso(),
        "probeAttempts": 0,
        "businessActionsReplayed": 0,
    }
    state[TRANSPORT_RECOVERY_KEY] = record
    _tc().write_task_state(logger, state)
    logger.write("transport.recovery.required", dict(record))
    return dict(record)


def record_transport_recovery_probe(
    logger: RunLogger, receipt: Any,
) -> Optional[JsonDict]:
    """Attach one bounded probe result to the current required batch."""
    state = _tc().load_task_state(logger)
    current = _recovery_record(state)
    if not isinstance(current, dict):
        return None
    result = receipt if isinstance(receipt, dict) else {
        "status": "blocked",
        "reason": "invalid recovery probe receipt",
    }
    updated = dict(current)
    updated["probeAttempts"] = _nonnegative_int(current.get("probeAttempts")) + 1
    updated["probeStatus"] = str(result.get("status") or "blocked")
    updated["probedAt"] = _tc().utc_now_iso()
    updated["businessActionsReplayed"] = _nonnegative_int(
        result.get("businessActionsReplayed")
    )
    updated["status"] = (
        "ready" if str(result.get("status") or "") == "ready" else "blocked"
    )
    if result.get("reason"):
        updated["reason"] = str(result.get("reason"))[:500]
    state[TRANSPORT_RECOVERY_KEY] = updated
    _tc().write_task_state(logger, state)
    logger.write("transport.recovery.probe_recorded", {
        "status": updated["status"],
        "fingerprint": updated.get("fingerprint"),
        "probeAttempts": updated["probeAttempts"],
        "businessActionsReplayed": updated["businessActionsReplayed"],
    })
    return updated


def transport_recovery_spawn_rejection(
    logger: RunLogger, *, phase_id: str,
) -> Optional[JsonDict]:
    """Fail closed until the required transport probe has succeeded."""
    state = _tc().load_task_state(logger)
    current = _recovery_record(state)
    if not isinstance(current, dict):
        return None
    status = str(current.get("status") or "")
    if status == "ready":
        return None
    if status not in {"pending", "blocked"}:
        return None
    return {
        "status": "transport_recovery_required",
        "phaseId": str(phase_id or ""),
        "tool_was_executed": False,
        "transportRecovery": {
            "status": status,
            "fingerprint": str(current.get("fingerprint") or ""),
            "fatalCount": _nonnegative_int(current.get("fatalCount")),
            "probeAttempts": _nonnegative_int(current.get("probeAttempts")),
            "businessActionsReplayed": _nonnegative_int(
                current.get("businessActionsReplayed")
            ),
        },
        "next_instruction": (
            "A fatal browser transport failure requires a bounded connection "
            "probe before dispatch. Use list_browser_agents("
            "refresh_connection=true); it only registers, refreshes "
            "capabilities and lists Fleets. Do not replay browser actions "
            "or create a replacement Fleet. Retry the original phase only "
            "after a ready probe receipt."
        ),
    }
