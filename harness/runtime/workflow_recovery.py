"""Reconcile only the original owned Workflow after a restored transport."""
import asyncio


async def reconcile_interrupted_workflow(agent, result):
    recovery = result.get("connectionRecovery") or {}
    if recovery.get("status") != "ready":
        return
    workflow_id = (result.get("executionTrace") or {}).get("workflowId")
    if not workflow_id:
        result["workflowReconciliation"] = {"status": "unknown", "reason": "execution_id_unavailable"}
        return
    if "Workflow.getStatus" not in agent.capability_methods:
        result["workflowReconciliation"] = {"status": "unknown", "reason": "status_capability_unavailable"}
        return
    try:
        receipt = await asyncio.wait_for(agent.browser_call_runner.call(
            "Workflow.getStatus", {"workflowId": workflow_id}), timeout=10)
        data = receipt.get("data") or {}
        if data.get("workflowId") != workflow_id:
            raise ValueError("Status reply does not identify the interrupted Workflow")
        reconciliation = {"workflowId": workflow_id, "receipt": receipt,
                          "status": data.get("status", "unknown")}
        from harness.runtime.steering import current_steering
        from harness.runtime.workflow_steering import _save
        manager = current_steering()
        if manager:
            _save(manager, workflow_id, {"status": data.get("status", "unknown"),
                  "controlReceipt": receipt, "executionResultMissing": True,
                  "fleetId": agent.assigned_fleet_id})
    except Exception as exc:
        reconciliation = {"workflowId": workflow_id, "status": "unknown",
                          "reason": type(exc).__name__, "rpcCode": getattr(exc, "rpc_code", None)}
    # Lifecycle/call counts do not reconstruct the lost output or prove the
    # stage's business contract. Preserve unknown execution facts and the
    # status receipt independently for the owner's next semantic decision.
    result["workflowReconciliation"] = reconciliation
    result["next_instruction"] = (
        "Inspect this original execution's status and page effects before reconciling the stage. "
        "No Workflow.execute was replayed. Status alone does not recover the missing output."
    )
