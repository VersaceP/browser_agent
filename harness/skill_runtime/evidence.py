"""Version-bound execution facts, independent of business acceptance decisions."""
from __future__ import annotations

import json

from harness.skill_builder.resources import workflow_entries
from harness.skill_runtime.plan import digest, read_source
from harness.workflow.workflow_projection import workflow_execution_facts


def read_receipt(logger, reference):
    row = logger.storage.read_resource(current_task_id=logger.task_id, resource_uri=reference)
    if not row:
        raise ValueError("Execution receipt is unavailable")
    value = row.get("content_json")
    return json.loads(value) if isinstance(value, str) else value


def program_evidence(state, root, read):
    """A matching hash proves executed code identity, never business correctness.

    Include every source file in the WebCross package and the step contract.
    Documentation-only edits do not discard byte-identical program evidence.
    """
    executions = []
    for item in state.get("receipts", []):
        if item.get("event") not in {"skill.step.result", "workflow.trial.result"}:
            continue
        receipt = read(item["ref"])
        if not isinstance(receipt, dict):
            continue
        facts = workflow_execution_facts(receipt.get("receipt", {}))
        binding = receipt.get("binding") or {}
        executions.append({"path": receipt.get("path") or binding.get("path"), "stepId": receipt.get("stepId"),
            "sourceHash": receipt.get("sourceHash"), "inputHash": receipt.get("inputHash"),
            "contractHash": receipt.get("contractHash"), "binding": {k: binding[k] for k in ("path", "pageId", "fleetId") if k in binding},
            "receiptRef": item["ref"], "execution": facts,
            "continuation": receipt.get("continuation"), "probe": item["event"] == "workflow.trial.result"})
    planned = {s["id"]: s for s in (state.get("plan") or {}).get("steps", [])}
    workflows = []
    for entry in workflow_entries(root):
        path = entry["path"]
        source_hash = digest(read_source(root, path))
        relevant = [e for e in executions if e["path"] == path]
        current = []
        for e in relevant:
            if e["sourceHash"] == source_hash:
                step = planned.get(e["stepId"])
                current.append({**e, "contractMatches":
                    digest(step) == e["contractHash"] if step and e.get("contractHash") else None})
        workflows.append({"path": path, "sourceHash": source_hash, "currentExecutions": current,
            "otherVersionExecutions": sum(e["sourceHash"] != source_hash for e in relevant)})
    recoveries = {key: dict(value) for key, value in state.get("recoveries", {}).items()}
    for item in recoveries.values():
        current = next((w for w in workflows if w["path"] == item.get("path")), None)
        if item.get("status") == "repaired" and (not current or current["sourceHash"] != item.get("verifiedSourceHash")):
            item["versionStatus"] = "changed_since_verification"
    return {"workflows": workflows, "recoveries": recoveries,
        "pageRecoveries": state.get("pageRecoveries", {}),
        "unattributedExecutions": [e for e in executions if e["path"] is None],
        "checkpoints": {sid: {k: r.get(k) for k in
            ("status", "sourceHash", "inputHash", "resolutionKind", "decisionReason", "evidenceRefs", "receiptRef")}
            for sid, r in state.get("steps", {}).items()},
        "interpretation": "Execution identity, raw receipts and model resolutions are distinct facts. "
            "Browser completion, script success, no-action probes and old versions do not certify the final program. "
            "Judge actual operations, outputs, relevant input variations and remaining defects against the user goal."}
