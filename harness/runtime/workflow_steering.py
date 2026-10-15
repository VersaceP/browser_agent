"""Pause a real in-flight workflow without cancelling its execute request."""
from __future__ import annotations

import asyncio
import hashlib
import json

from harness.runtime.steering import current_steering
from harness.task_control import load_task_state, write_task_state

TERMINAL = {"succeeded", "failed", "cancelled", "completed"}


def _control_data(receipt, workflow_id):
    data = receipt.get("data") or {}
    if data.get("workflowId") != workflow_id:
        raise ValueError("Platform control response does not identify the requested owned Workflow")
    return data


def _save(manager, workflow_id, values):
    state = load_task_state(manager.logger)
    record = state.setdefault("user_steering", {}).setdefault("workflows", {}).setdefault(workflow_id, {})
    record.update(values)
    write_task_state(manager.logger, state)
    return record


def owned_workflows(manager):
    return load_task_state(manager.logger).get("user_steering", {}).get("workflows", {})


async def controlled_call(agent, runner, params, observer, **kwargs):
    from harness.runtime.steering import dispatch_boundary
    manager = current_steering()
    if manager is None or observer is None:
        return await runner.call("Workflow.execute", params, **kwargs)
    started_revision = manager.revision
    pending = dispatch_boundary(agent, started_revision, "Workflow.execute")
    if pending: return pending
    task = asyncio.create_task(runner.call("Workflow.execute", params, **kwargs))
    changed = asyncio.Event()
    workflow_id = None
    background = False
    requested_revision = None
    monitor = None
    observed_status = None

    def changed_event(): changed.set()
    def progress(message):
        nonlocal observed_status
        env = (message.get("params") or {}).get("data") or {}
        payload = env.get("payload") or {}
        if env.get("event") == "Workflow.progress" and payload.get("workflowId") == workflow_id:
            observed_status = payload.get("status") or payload.get("phase")
        changed.set()
    subscribe = getattr(agent.browser, "subscribe_notifications", None)
    unsubscribe = subscribe(progress) if callable(subscribe) else lambda: None
    manager.workflow_controls.add(changed_event)
    identity = {"pageId": params.get("binding", {}).get("pageId") or params.get("pageId"),
                "fleetId": params.get("binding", {}).get("fleetId") or params.get("fleetId"),
                "executionInputHash": hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()}

    def save_final(done):
        try:
            result = done.result()
            body = {"method": "Workflow.execute", "workflowId": workflow_id, "response": result,
                    "executionIdentity": identity}
        except BaseException as exc:
            body = {"method": "Workflow.execute", "workflowId": workflow_id,
                    "error": str(exc), "errorType": type(exc).__name__, "executionIdentity": identity,
                    "rpcData": getattr(exc, "rpc_data", None)}
        try:
            ref = manager.logger.storage.save_resource(task_id=manager.logger.task_id,
                run_id=manager.logger.run_id, resource_type="steering_workflow_result",
                logical_path=f"steering/workflows/{workflow_id}/result.json", content=body)["saved_path"]
            data = (body.get("response") or {}).get("data") or {}
            prior = owned_workflows(manager)[workflow_id].get("status")
            status = data.get("status") or (observed_status if observed_status in TERMINAL else
                                           prior if prior in TERMINAL else "unknown")
            _save(manager, workflow_id, {"status": status, "resultRef": ref})
            manager.show(f"Workflow {workflow_id} · 在途执行已返回；完整回执 {ref}")
        except Exception as exc:
            manager.show(f"Workflow {workflow_id} 回执保存失败：{type(exc).__name__}: {exc}")
        manager.workflow_tasks.pop(workflow_id, None)
        manager.workflow_runners.pop(workflow_id, None)
        manager.workflow_controls.discard(changed_event)
        unsubscribe()
        if monitor: monitor.cancel()

    async def background_monitor():
        nonlocal requested_revision
        saved_status = "paused"
        while not task.done():
            if observed_status and observed_status != saved_status:
                _save(manager, workflow_id, {"status": observed_status})
                saved_status = observed_status
            if (manager.revision != requested_revision and manager.records
                    and manager.records[-1]["text"] != "/continue"):
                requested_revision = manager.revision
                try:
                    if "Workflow.pause" not in agent.capability_methods:
                        raise PermissionError("Workflow.pause capability unavailable")
                    receipt = await runner.call("Workflow.pause", {"workflowId": workflow_id,
                        "reason": "Additional task-owned human guidance"})
                    _save(manager, workflow_id, {"pauseReceipt": receipt})
                    _save(manager, workflow_id, {"pauseReceipt": receipt,
                        "status": _control_data(receipt, workflow_id).get("status", "unknown")})
                except Exception as exc:
                    _save(manager, workflow_id, {"status": "unknown", "pauseError": str(exc)})
                    manager.show(f"在途 Workflow 暂停未确认：{exc}")
                if observed_status == "paused" or observed_status in TERMINAL:
                    _save(manager, workflow_id, {"status": observed_status})
                    saved_status = observed_status
            changed.clear()
            await changed.wait()

    try:
        while True:
            if task.done():
                try:
                    result = task.result()
                except BaseException:
                    if workflow_id: _save(manager, workflow_id, {"status": observer.trace.phase or "unknown"})
                    raise
                if workflow_id:
                    _save(manager, workflow_id, {"status": (result.get("data") or {}).get("status", "unknown")})
                return result
            if observer.trace.workflow_id and workflow_id is None:
                workflow_id = observer.trace.workflow_id
                manager.workflow_tasks[workflow_id] = task
                manager.workflow_runners[workflow_id] = runner
                _save(manager, workflow_id, {**identity, "status": observer.trace.phase or "running"})
            if (manager.paused or manager.revision != started_revision) and workflow_id:
                if requested_revision != manager.revision:
                    requested_revision = manager.revision
                    try:
                        if "Workflow.pause" not in agent.capability_methods:
                            raise PermissionError("Workflow.pause capability unavailable")
                        receipt = await runner.call("Workflow.pause", {"workflowId": workflow_id,
                            "reason": "New task-owned human guidance; pause at a platform job boundary"})
                        _save(manager, workflow_id, {"pauseReceipt": receipt})
                        data = _control_data(receipt, workflow_id)
                        _save(manager, workflow_id, {"pauseReceipt": receipt,
                            "status": data.get("status", "unknown"), "inputRevision": manager.revision})
                        manager.show(f"Workflow {workflow_id} · 暂停请求：{data.get('status', 'unknown')}；原执行仍在途")
                    except Exception as exc:
                        _save(manager, workflow_id, {"pauseError": str(exc), "status": "unknown"})
                        manager.show(f"Workflow 暂停未确认：{exc}；停止后续派发，等待原回执")
                status = observed_status or observer.trace.phase
                pause = owned_workflows(manager)[workflow_id].get("pauseReceipt", {}).get("data") or {}
                status = "paused" if pause.get("status") == "paused" else status
                pause_failed = owned_workflows(manager)[workflow_id].get("pauseError")
                if status == "paused" or pause_failed:
                    yielded_status = "paused" if status == "paused" else "unknown"
                    _save(manager, workflow_id, {"status": yielded_status})
                    task.add_done_callback(save_final)
                    monitor = asyncio.create_task(background_monitor())
                    manager.control_tasks.add(monitor)
                    monitor.add_done_callback(manager.control_tasks.discard)
                    background = True
                    return {"data": {"workflowId": workflow_id, "status": yielded_status,
                        "taskId": pause.get("taskId"), "output": None},
                        "steering": {"inputRevision": manager.revision, "executeStillInFlight": True,
                        "pauseConfirmed": yielded_status == "paused", "pauseError": pause_failed,
                        "instruction": "Inspect actual state/effects; use control_workflow_execution to resume/cancel/status. unknown is NOT paused: the old program may still act. A new invocation is not continuation of this JS stack."}}
            changed.clear()
            waiter = asyncio.create_task(changed.wait())
            try:
                await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    finally:
        if not background:
            manager.workflow_controls.discard(changed_event)
            unsubscribe()
            if workflow_id: manager.workflow_tasks.pop(workflow_id, None)
            if workflow_id: manager.workflow_runners.pop(workflow_id, None)
            # Caller cancellation is not proof of remote cancellation. Retain
            # identity and uncertainty for explicit recovery on resume.
            if not task.done():
                if workflow_id: _save(manager, workflow_id, {"status": "uncertain", "reason": "caller_interrupted"})
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def control_owned_workflow(agent, workflow_id, action):
    manager = current_steering()
    if manager is None: raise ValueError("No active task input controller")
    owned = owned_workflows(manager).get(workflow_id)
    if owned is None: raise PermissionError("Workflow is not owned by this task")
    if action not in {"pause", "resume", "cancel", "status"}: raise ValueError("Unknown workflow control action")
    if owned.get("fleetId") and owned["fleetId"] != agent.assigned_fleet_id:
        raise PermissionError("Workflow belongs to another Fleet")
    method = "Workflow.getStatus" if action == "status" else "Workflow." + action
    if method not in agent.capability_methods: raise PermissionError("Workflow control capability unavailable")
    params = {"workflowId": workflow_id}
    if action in {"pause", "cancel"}: params["reason"] = "Task owner reconciled human guidance and current effects"
    receipt = await agent.browser_call_runner.call(method, params)
    _save(manager, workflow_id, {"controlReceipt": receipt})
    data = _control_data(receipt, workflow_id)
    record = _save(manager, workflow_id, {"status": data.get("status", "unknown"), "controlReceipt": receipt})
    return {"receipt": receipt, "execution": record,
            "next": "Use the original execution's final resultRef when available; inspect effects before reconciling the stage. Do not replay uncertain actions."}


async def shutdown_workflows(manager):
    """End only task-owned live jobs; local cancellation is never remote proof."""
    for workflow_id, task in list(manager.workflow_tasks.items()):
        runner = manager.workflow_runners.get(workflow_id)
        if runner and not task.done():
            try:
                if "Workflow.cancel" not in runner.capability_methods:
                    raise PermissionError("Workflow.cancel capability unavailable during shutdown")
                receipt = await asyncio.wait_for(runner.call("Workflow.cancel", {"workflowId": workflow_id,
                    "reason": "Task owner ended; stop this owned in-flight execution"}), timeout=10)
                _save(manager, workflow_id, {"shutdownReceipt": receipt})
                _save(manager, workflow_id, {"shutdownReceipt": receipt,
                    "status": _control_data(receipt, workflow_id).get("status", "unknown")})
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except Exception as exc:
                _save(manager, workflow_id, {"status": "uncertain", "shutdownError": str(exc)})
                manager.show(f"Workflow {workflow_id} 结束未确认：{exc}；保留原始状态供恢复")
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
