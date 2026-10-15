"""One task-owned executor reuses the existing Spawner lease and transport.

Only startup acquires a slot. Each operation keeps the normal browser tool,
file authorization, platform scope and evidence path, without a model loop.
The service is drained before handing its pages to a semantic Browser worker.
"""
from __future__ import annotations

import asyncio
import uuid
from abcp_client import ABCPTransportError


class ExecutionService:
    def __init__(self, owner):
        self.owner = owner
        self.task = None
        self.queue = None
        self.ready = None
        self.lock = asyncio.Lock()

    async def _start(self):
        if self.task is not None:
            if not self.task.done():
                return
            previous = await self.task
            self.task = None
            raise RuntimeError(f"Executor stopped; inspect effects before resuming: {previous}")
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.owner._delegate_worker(
            {"kind": "service"}, "Serve task-owned deterministic operations; no Browser model loop"))
        try:
            done, _ = await asyncio.wait((self.ready, self.task), return_when=asyncio.FIRST_COMPLETED)
            if self.ready not in done:
                result = await self.task
                raise RuntimeError(f"Executor could not start: {result}")
            self.ready.result()
        except BaseException:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def execute(self, operation, instruction):
        from harness.runtime.steering import current_steering
        manager = current_steering()
        input_revision = manager.revision if manager else 0
        async with self.lock:
            await self._start()
            operation = dict(operation)
            operation["_steeringInputRevision"] = input_revision
            # These bounds come from the host, never from the model arguments.
            if getattr(self.owner, "execution_pages", None) is not None:
                operation["executionPages"] = sorted(self.owner.execution_pages)
            future = asyncio.get_running_loop().create_future()
            operation_id = uuid.uuid4().hex
            self.owner.logger.write("executor.operation.started", {
                "operationId": operation_id, "kind": operation["kind"],
                "pageId": operation.get("pageId"), "instruction": instruction})
            await self.queue.put((operation_id, operation, future))
            try:
                done, _ = await asyncio.wait((future, self.task), return_when=asyncio.FIRST_COMPLETED)
                if future not in done:
                    raise RuntimeError("Executor ended before returning this operation; reconcile its effects")
                answer, worker_id = future.result()
            except BaseException:
                future.cancel()
                if self.task is not None:
                    self.task.cancel()
                await self.close()
                raise
            state = self.owner.store.load()
            record = {"operationId": operation_id, "kind": operation["kind"],
                "executorId": worker_id, "answer": answer}
            ref = self.owner.store.record(state, "executor.operation.result", record)
            self.owner.state = self.owner.store.load()
            self.owner._record_execution_feedback(operation, ref)
            return {"status": "returned", "executorId": worker_id, "operationId": operation_id,
                "receiptRef": ref, "answer": answer, "execution": self.owner.projection()}

    async def serve(self, worker):
        self.ready.set_result(worker.worker_id)
        while True:
            item = await self.queue.get()
            if item is None:
                return {"status": "executor_closed"}
            operation_id, operation, future = item
            if future.cancelled():
                continue
            worker.worker_contract["_skill_operation"] = operation
            worker._steering_operation_revision = operation["_steeringInputRevision"]
            state = self.owner.store.load()
            worker.worker_contract["_user_context"] = {
                "originalUserTask": state["goal"], "operatorInputs": state.get("operatorInputs", [])}
            try:
                if getattr(worker.browser, "connection_usable", True) is False:
                    reconnect = getattr(worker.browser, "ensure_connected", None)
                    if reconnect is None:
                        raise RuntimeError("Executor transport disconnected; check effects before reconnecting")
                    await reconnect()
                value = await worker._run_operation(operation)
                if not future.done():
                    future.set_result((value, worker.worker_id))
            except asyncio.CancelledError:
                future.cancel()
                raise
            except ABCPTransportError as exc:
                from harness.tools.browser_tools.navigate import _transport_error_metadata
                if not future.done():
                    future.set_result(({"status": "transport_recovery_required", "error": str(exc),
                        **_transport_error_metadata(exc.rpc_method, exc)}, worker.worker_id))
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                worker._binding_checkpoint()

    async def close(self):
        task, self.task = self.task, None
        if task is None:
            return
        if not task.done():
            await self.queue.put(None)
        try:
            await task
        except asyncio.CancelledError:
            pass
