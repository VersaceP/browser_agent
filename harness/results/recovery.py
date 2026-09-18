"""Bounded recovery facts, never a retry decision or proof of absent effects."""
from typing import Any


def _record(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _refs(value, limit):
    return [item[:400] for item in _list(value)[:limit] if isinstance(item, str)]


def worker_recovery_facts(result: dict) -> dict:
    classification = _record(result.get("errorClassification"))
    validation = _record(result.get("artifactValidation"))
    classification = classification or _record(validation.get("classification"))
    transport = _record(result.get("transportFailure"))
    sent = result.get("requestSent", transport.get("requestSent"))
    fatal = result.get("connectionFatal", transport.get("connectionFatal"))
    method = result.get("rpcMethod") or transport.get("method") or classification.get("method")
    paths = _list(result.get("artifacts"))
    downloads = _list(result.get("downloadOperationReceipts"))
    manifests = _list(result.get("fileManifests"))
    trace = _record(result.get("traceSummary"))
    assignment = _record(result.get("fleetAssignment"))
    # Only the failed request's delivery is known. Even requestSent=False says
    # nothing about earlier worker actions, and an RPC error can follow a write.
    return {
        "workerId": result.get("workerId"), "phaseId": result.get("phaseId"),
        "status": result.get("status"), "fleetId": assignment.get("fleetId"),
        "failure": {
            "method": method, "transportCode": result.get("transportCode") or transport.get("code"),
            "rpcCode": result.get("rpcCode"), "classification": classification.get("type"),
            "connectionFatal": fatal if isinstance(fatal, bool) else None,
            "requestSent": sent if isinstance(sent, bool) else None,
        },
        "effects": {
            "failedRequestDelivery": "not_sent" if sent is False else "sent" if sent is True else "unknown",
            "failedRequestEffects": "not_executed" if sent is False else "unknown",
            "priorWorkerEffects": "not_inferred_from_failure",
            "recordedArtifactCount": len(paths), "artifactRefs": _refs(paths, 3),
            "recordedDownloadReceiptCount": len(downloads),
            "recordedFileManifestCount": len(manifests),
            "observedPageIds": _refs(trace.get("pageIds"), 5),
            "absenceOfReceiptsProvesNoEffects": False,
        },
        "tracePath": result.get("tracePath") or None,
        "decisionOwner": "lead",
        "next_instruction": (
            "Use these recorded facts first. A ready connection probe is not proof of page recovery "
            "or permission to replay. Inspect unresolved effects with available read-only receipts/state; "
            "choose whether to continue the original phase through the normal spawn gate. "
            "Do not infer that no page/file was created from a missing artifact or a failed response."
        ),
    }


def recovery_overview(result: Any, limit: int = 8) -> dict:
    """Keep current failure receipts visible when the containing wait is offloaded."""
    if not isinstance(result, dict):
        return {}
    workers = result.get("completed", result.get("agents", []))
    if not isinstance(workers, list):
        workers = []
    if result.get("workerId"):
        workers = [result]
    facts = []
    for worker in workers:
        if not isinstance(worker, dict) or not worker.get("workerId"):
            continue
        fact = worker.get("recoveryFacts")
        if not isinstance(fact, dict) and worker.get("status") not in {None, "done", "running", "validated_done"}:
            fact = worker_recovery_facts(worker)
        if isinstance(fact, dict):
            facts.append(fact)
    if not facts and not result.get("connectionRecovery"):
        return {}
    return {
        "nonSuccessfulWorkerCount": len(facts), "workers": facts[-limit:],
        "omittedWorkerCount": max(0, len(facts) - limit),
        "connectionRecovery": result.get("connectionRecovery"),
        "connectionScope": "Latest explicit probe only; old worker failures do not describe current connectivity.",
    }
