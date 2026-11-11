"""Direct WebCross access for one Workflow task: read-only observation and scoped execution.

Every request and response is saved as a task receipt before the model sees a
projection. Executions go through the Harness scope wrapper: actions target the
bound page, files must be authorized canonical paths, and package capabilities
come from contract.json. This module never closes Fleets or pages it did not
create; on shutdown it cancels only executions it started and records the
platform's actual answer.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from abcp_client import ABCPClient, ABCPTransportError
from harness.tools.tool_policy import disabled_reason_for_method, sanitize_transport_payload
from harness.workflow_mode.evidence import child_pointer, evidence_locations

OBSERVE_METHODS = frozenset({
    "Page.getState", "Page.list", "Fleet.list", "Fleet.status", "DOM.getAXTree", "DOM.getDetails",
    "DOM.inspectSelect", "Page.screenshot", "Network.readApi", "Network.readBody", "Download.list",
})
CONTROL_METHODS = frozenset({"Page.create", "Page.switchTo", "Page.navigate"})
CATALOG_METHODS = frozenset({"System.describeAction", "Workflow.getGuide", "Workflow.getStatus", "Workflow.readResult"})
_PURPOSE_DEFAULT = "Read current page state for the Workflow task."
TERMINAL = {"succeeded", "failed", "cancelled", "completed"}


def platform_error(exc: ABCPTransportError) -> dict[str, Any]:
    return {"status": "platform_error", "transportCode": exc.transport_code, "rpcCode": exc.rpc_code,
            "rpcMethod": exc.rpc_method, "requestSent": exc.request_sent, "requestId": exc.request_id,
            "details": sanitize_transport_payload(exc.rpc_data), "message": str(exc)[:800],
            "connectionRecovery": getattr(exc, "connection_recovery", None)}


def data_of(response: Any) -> Any:
    """WebCross wraps results as {observation, suggested_prompt, data}."""
    return response.get("data") if isinstance(response, dict) and "data" in response else response


class WorkflowPlatform:
    def __init__(self, runtime: Any, logger: Any, store: Any, *, label: str = "workflow", identity: str = ""):
        self.runtime, self.logger, self.store, self.label = runtime, logger, store, label
        from harness.runtime.browser_connection import managed_client

        def record_transport(kind: str, payload: Any) -> None:
            data = payload if isinstance(payload, dict) else {}
            event = {"kind": kind, "requestId": data.get("id") or data.get("requestId"),
                     "method": data.get("method")}
            # ABCPClient emits this scrubbed response before hydration removes
            # artifact.path. Retain the wire reference separately from derived data.
            body = data.get("data")
            if kind == "response" and isinstance(body, dict) and isinstance(body.get("artifact"), dict):
                event["response"] = data
            logger.write("workflow.transport", event)
        self.client = managed_client(ABCPClient(runtime.browser, on_event=record_transport), identity=identity)
        self.agent_id = identity
        self.connected = False
        self.capability_methods: set[str] = set()
        self.capabilities: list[dict[str, Any]] = []
        self.agent_guide = ""
        self.catalog: dict[str, Any] = {}
        self.artifacts: dict[str, str] = {}
        self.live: dict[str, dict[str, Any]] = {}
        self.created_pages: set[str] = set()
        self.schema_bootstrap: dict[str, Any] = {}
        self.on_receipt: Callable[[str, str], None] | None = None
        self._lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()

    async def refresh_schema_cache(self) -> dict[str, Any]:
        """The same global_schema_cache bootstrap Browser runs before every task.

        It rebuilds global_schema_cache/schemas when the live catalog revision
        changed, so local validation and the scope wrapper use current contracts.
        """
        from types import SimpleNamespace
        from harness.capabilities.bootstrap import bootstrap_schema_cache
        owner = SimpleNamespace(runtime=self.runtime, logger=self.logger)
        await bootstrap_schema_cache(owner)
        self.schema_bootstrap = {"degraded": bool(getattr(owner, "_schema_bootstrap_degraded", False)),
                                 "connectionFailure": getattr(owner, "_browser_connection_failure", None)}
        return self.schema_bootstrap

    def requires_purpose(self, method: str) -> bool:
        """Whether the live schema requires a purpose (Page.getState does since 0.9.78)."""
        schema = self._cached_input_schema(method)
        if schema is None:
            return method.startswith(("DOM.", "Page.screenshot", "Hitl.", "Network."))
        branches = [schema, *(schema.get("anyOf") or []), *(schema.get("oneOf") or [])]
        return any("purpose" in (b.get("required") or []) for b in branches if isinstance(b, dict))

    def _cached_input_schema(self, method: str) -> dict[str, Any] | None:
        from harness.capabilities.schema_cache import global_schemas_dir
        path = global_schemas_dir(self.runtime.harness.worktree_dir) / f"{method}.json"
        try:
            schema = json.loads(path.read_text(encoding="utf-8")).get("inputSchema")
        except (OSError, ValueError):
            return None
        return schema if isinstance(schema, dict) else None

    def local_param_issues(self, method: str, params: dict[str, Any]) -> list[str]:
        """Check params against the cached live schema before sending."""
        from harness.tools.argument_pipeline import validate_schema
        schema = self._cached_input_schema(method)
        if schema is None:
            return []
        return [f"{'.'.join(map(str, i.path)) or '(root)'}: {i.message}" for i in validate_schema(params, schema)[:10]]

    async def connect(self) -> None:
        async with self._lock:
            if self.connected:
                return
            if not self.schema_bootstrap:
                await self.refresh_schema_cache()
                failure = self.schema_bootstrap.get("connectionFailure")
                if failure:
                    self.schema_bootstrap = {}
                    raise ABCPTransportError(f"WebCross is unavailable: {failure.get('message')}",
                        transport_code=failure.get("reasonCode") or "ABCP_TRANSPORT_CONNECT_FAILED",
                        connection_fatal=True, request_sent=False)
            await self.client.connect()
            self.connected = True
            try:
                registration = await self.client.call("System.register", {})
                capabilities = await self.client.call("System.getCapabilities", {"guide": "content"})
            except BaseException:
                await self.client.close()
                self.connected = False
                raise
            self.logger.write("workflow.platform.registered", sanitize_transport_payload(registration))
            self.agent_id = str((data_of(registration) or {}).get('agentId') or self.agent_id)
            data = data_of(capabilities) or {}
            from harness.capabilities.schema_loader import _agent_guide_from_capabilities_response
            actions = data.get("actions")
            self.capabilities = [a for a in actions if isinstance(a, dict)
                and isinstance(a.get("method"), str) and a["method"].strip()] if isinstance(actions, list) else []
            guide = data.get("agentGuide")
            self.agent_guide = (_agent_guide_from_capabilities_response(capabilities)
                if isinstance(guide, str) or (isinstance(guide, dict) and guide.get("format") == "content") else "")
            self.capability_methods = {a["method"] for a in self.capabilities}
            self.catalog = {"catalogRevision": data.get("catalogRevision"),
                            "guideRevision": data.get("guideRevision")}

    async def close(self) -> dict[str, Any]:
        """Cancel only this task's still-running executions; record the platform answer."""
        outcomes = {}
        if self.connected:
            for workflow_id, record in list(self.live.items()):
                if record.get("status") in TERMINAL:
                    continue
                outcomes[workflow_id] = await self.cancel(workflow_id, reason="Workflow task closed")
            await self.client.close()
            self.connected = False
        return outcomes

    def _receipt(self, event: str, value: dict[str, Any]) -> str:
        state = self.store.load()
        ref = self.store.receipt(state, event, value)
        if self.on_receipt:
            self.on_receipt(ref, event)
        return ref

    def _remember_artifacts(self, value: Any) -> None:
        if isinstance(value, dict):
            artifact = value.get("artifact")
            if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                self.artifacts[artifact["path"]] = str(artifact.get("sha256") or "")
            path = value.get("path")
            if isinstance(path, str) and value.get("mimeType"):
                self.artifacts[path] = ""
            for item in value.values():
                self._remember_artifacts(item)
        elif isinstance(value, list):
            for item in value:
                self._remember_artifacts(item)

    async def call(self, method: str, params: dict[str, Any], *, allowed: frozenset[str],
                   purpose: str | None = None) -> dict[str, Any]:
        """Send one admitted Action and save its full receipt."""
        if method not in allowed:
            return {"ok": False, "status": "rejected", "error": f"{method} is not available to this tool"}
        reason = disabled_reason_for_method(method)
        if reason:
            return {"ok": False, "status": "rejected", "error": reason}
        await self.connect()
        if self.capability_methods and method not in self.capability_methods:
            return {"ok": False, "status": "unavailable", "error": f"{method} is not in the live Action catalog"}
        params = copy.deepcopy(params or {})
        if "purpose" not in params and (purpose or self.requires_purpose(method)):
            params["purpose"] = purpose or _PURPOSE_DEFAULT
        if method == "Page.screenshot":
            params.setdefault("fullPage", False)
            params["options"] = {**(params.get("options") or {}), "format": "file"}
        issues = self.local_param_issues(method, params)
        if issues:
            ref = self._receipt("workflow.platform.rejected", {"method": method, "params": sanitize_transport_payload(params),
                                                               "status": "invalid_params", "issues": issues})
            return {"ok": False, "status": "invalid_params", "method": method, "issues": issues, "receiptRef": ref,
                    "next": "Not sent. Fix the values against the live schema (describe_action); Fleet and page IDs "
                            "are UUIDs from list_pages, never placeholders such as 'default'."}
        started = time.monotonic()
        try:
            response = await self.client.call(method, params)
            error = None
        except ABCPTransportError as exc:
            response, error = None, platform_error(exc)
        record = {"method": method, "params": sanitize_transport_payload(params),
                  "response": response, "error": error, "status": "failed" if error else "returned",
                  "durationMs": round((time.monotonic() - started) * 1000)}
        ref = self._receipt("workflow.platform.call", record)
        if error:
            return {"ok": False, **error, "receiptRef": ref}
        data = data_of(response)
        self._remember_artifacts(data)
        if method == "Page.create":
            page = (data or {}).get("pageId") if isinstance(data, dict) else None
            if page:
                self.created_pages.add(page)
        paths = {"response": "/response", "data": "/response/data"}
        if isinstance(data, dict):
            paths.update({key: child_pointer("/response/data", key) for key in data})
        return {"ok": True, "data": data, "observation": (response or {}).get("observation"),
                "suggested_prompt": (response or {}).get("suggested_prompt"), "receiptRef": ref,
                "evidenceLocations": evidence_locations(ref, record, paths)}

    def read_artifact(self, path: str, *, offset: int = 0, max_chars: int = 20000) -> dict[str, Any]:
        """Only artifacts returned by this task's own observation receipts."""
        if path not in self.artifacts:
            return {"ok": False, "error": "Artifact was not returned by this task's observations",
                    "next": "For hydrated page evidence use the observation receipt's evidenceLocations "
                            "with read_receipt; do not guess host paths or request broader file access."}
        file = Path(path)
        if not file.is_file():
            return {"ok": False, "error": "Artifact is no longer available; observe again"}
        body = file.read_bytes()
        expected = self.artifacts[path]
        if expected and hashlib.sha256(body).hexdigest() != expected:
            return {"ok": False, "error": "Artifact changed since it was observed; observe again"}
        if file.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            return {"ok": True, "image": True, "path": path, "bytes": len(body)}
        text = body.decode("utf-8", "replace")
        offset = max(0, int(offset or 0))
        size = max(1000, min(int(max_chars or 20000), 50000))
        return {"ok": True, "path": path, "offset": offset, "totalChars": len(text),
                "text": text[offset:offset + size], "nextOffset": offset + size if offset + size < len(text) else None}

    async def execute(self, document, business_input, *, page_id, fleet_id, stop_check=None, **kwargs):
        from harness.workflow_mode.concurrency import lease, hold, AdmissionStopped
        queued = time.monotonic()
        try:
            async with hold(self._execution_lock, stop_check):
                async with lease(self.runtime, ['page:'+fleet_id+':'+page_id], capacity=True, stop_check=stop_check):
                    if stop_check and stop_check():
                        raise AdmissionStopped('Stopped while queued')
                    self.logger.write('workflow.execution.admitted', {
                        'pageId': page_id, 'fleetId': fleet_id,
                        'queueMs': round((time.monotonic()-queued)*1000)})
                    return await self._execute_admitted(document, business_input, page_id=page_id,
                                                         fleet_id=fleet_id, **kwargs)
        except AdmissionStopped as exc:
            return {'ok': False, 'status': 'not_dispatched', 'error': str(exc)}

    async def _execute_admitted(self, document: dict[str, Any], business_input: Any, *, page_id: str, fleet_id: str,
                      files: list[str], content_hash: str, on_started: Callable[[str], None] | None = None,
                      write_policy: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one scoped execution and save the request, scope and complete result."""
        from harness.workflow.script_runtime import scope_package, script_result_facts
        await self.connect()
        if "Workflow.execute" not in self.capability_methods:
            return {"ok": False, "status": "unavailable", "error": "Workflow.execute is not in the live catalog"}
        try:
            guarded, scope = scope_package(document, page_id=page_id, fleet_id=fleet_id,
                                           methods=self.capability_methods, files=files,
                                           write_policy=write_policy)
        except ValueError as exc:
            return {"ok": False, "status": "invalid_workflow_package", "error": str(exc)}
        package = dict(guarded)
        params = {"workflow": package, "input": copy.deepcopy(business_input),
                  "binding": copy.deepcopy(scope["platformBinding"])}
        execution_id = uuid.uuid4().hex
        record = {"executionId": execution_id, "status": "running", "pageId": page_id, "fleetId": fleet_id,
                  "contentHash": content_hash, "sourceHash": scope["sourceHash"],
                  "executionPackageHash": scope["executionPackageHash"], "workflowId": None}
        def progress(message: dict[str, Any]) -> None:
            envelope = (message.get("params") or {}).get("data") or {}
            payload = envelope.get("payload") or {}
            identity = str(payload.get("workflowId") or "")
            if envelope.get("event") != "Workflow.progress" or not identity:
                return
            # One task runs one execution at a time on its own connection, so
            # the first unseen id after this request is this execution.
            if record["workflowId"] is None and identity not in self.live:
                record["workflowId"] = identity
                self.live[identity] = record
                if on_started:
                    on_started(identity)
            if identity == record["workflowId"]:
                record["phase"] = payload.get("status") or payload.get("phase")

        unsubscribe = self.client.subscribe_notifications(progress)
        started = time.monotonic()
        self.logger.write("workflow.execute.started", {k: record[k] for k in
            ("executionId", "pageId", "fleetId", "contentHash", "sourceHash", "executionPackageHash")})
        try:
            response = await self.client.call("Workflow.execute", params)
            error = None
        except ABCPTransportError as exc:
            response, error = None, platform_error(exc)
        except asyncio.CancelledError:
            record["status"] = "uncertain"
            raise
        finally:
            unsubscribe()
        data = data_of(response) if response is not None else {}
        data = data if isinstance(data, dict) else {}
        delivery_response = None
        result_delivery = response.get("resultDelivery") if isinstance(response, dict) else None
        delivery = data.get("delivery") or {}
        if delivery.get("mode") == "reference":
            from harness.workflow.result_delivery import read_retained_result
            delivery_response = response
            try:
                if "Workflow.readResult" not in self.capability_methods:
                    raise ValueError("Runtime returned a result reference without Workflow.readResult capability")
                identity = data.get("workflowId")
                if not identity or (record.get("workflowId") and identity != record["workflowId"]):
                    raise ValueError("Result delivery does not match the observed workflow identity")
                data, result_delivery = await read_retained_result(self.client, identity, delivery.get("reference"))
                response = {**response, "data": data} if "data" in response else data
            except (ABCPTransportError, ValueError, TypeError) as exc:
                error = (platform_error(exc) if isinstance(exc, ABCPTransportError) else
                         {"status": "result_delivery_failed", "message": str(exc), "requestSent": True})
                error["recovery"] = "Read the same workflow with read_execution_result; do not replay its browser actions."
        failure_details = (error or {}).get("details") or {}
        failure_details = failure_details if isinstance(failure_details, dict) else {}
        identity = data.get("workflowId") or record.get("workflowId") or failure_details.get("workflowId")
        status = data.get("status") or failure_details.get("status") or "unknown"
        record.update(status=status, workflowId=identity)
        if identity:
            self.live[identity] = record
        facts = script_result_facts({"response": {"data": data}}) if data and not error else {}
        receipt = {"method": "Workflow.execute", "executionId": execution_id, "status": status,
                   "workflowId": identity, "pageId": page_id, "fleetId": fleet_id, "contentHash": content_hash,
                   "scope": scope, "input": business_input, "authorizedFiles": files,
                   "durationMs": round((time.monotonic() - started) * 1000),
                   "response": response, "error": error, "facts": facts,
                   "deliveryResponse": delivery_response, "resultDelivery": result_delivery}
        ref = self._receipt("workflow.execute.result", receipt)
        return {"ok": error is None, "status": status, "workflowId": identity, "receiptRef": ref,
                "facts": facts, "output": data.get("output"), "error": error or data.get("error"),
                "timing": data.get("timing"), "scope": {k: scope[k] for k in
                    ("sourceHash", "executionPackageHash", "allowedMethods", "declaredCapabilities",
                     "deniedCapabilities", "authorizedFileCount", "writeMode")}}

    async def read_result(self, workflow_id: str) -> dict[str, Any]:
        """Recover evidence by execution identity; never rerun its source or browser actions."""
        from harness.workflow.result_delivery import read_retained_result
        from harness.workflow.script_runtime import script_result_facts
        await self.connect()
        if "Workflow.readResult" not in self.capability_methods:
            return {"ok": False, "status": "unavailable", "error": "Runtime does not advertise Workflow.readResult"}
        started = time.monotonic()
        try:
            data, delivery = await read_retained_result(self.client, workflow_id)
        except (ABCPTransportError, ValueError, TypeError) as exc:
            error = platform_error(exc) if isinstance(exc, ABCPTransportError) else {"message": str(exc)}
            ref = self._receipt("workflow.result.read_failed", {"workflowId": workflow_id, "error": error})
            return {"ok": False, "status": "result_delivery_failed", "workflowId": workflow_id,
                    "error": error, "receiptRef": ref,
                    "next": "Missing evidence does not prove absence of effects. Inspect the same execution and page; do not replay for a receipt."}
        facts = script_result_facts({"response": {"data": data}})
        ref = self._receipt("workflow.execute.recovered", {"method": "Workflow.readResult", "workflowId": workflow_id,
            "status": data["status"], "response": {"data": data}, "facts": facts, "resultDelivery": delivery,
            "durationMs": round((time.monotonic() - started) * 1000)})
        if workflow_id in self.live:
            self.live[workflow_id]["status"] = data["status"]
        return {"ok": True, "status": data["status"], "workflowId": workflow_id, "receiptRef": ref,
                "facts": facts, "output": data["output"], "error": data.get("error"), "timing": data.get("timing"),
                "resultDelivery": delivery}

    async def status(self, workflow_id: str) -> dict[str, Any]:
        result = await self.call("Workflow.getStatus", {"workflowId": workflow_id}, allowed=CATALOG_METHODS)
        if result.get("ok") and workflow_id in self.live:
            self.live[workflow_id]["status"] = (result.get("data") or {}).get("status", "unknown")
        return result

    async def cancel(self, workflow_id: str, *, reason: str) -> dict[str, Any]:
        """Cancellation request plus the platform's subsequent status; not proof of undo."""
        await self.connect()
        try:
            receipt = await asyncio.wait_for(self.client.call("Workflow.cancel",
                {"workflowId": workflow_id, "reason": reason}), timeout=15)
            error = None
        except (ABCPTransportError, asyncio.TimeoutError) as exc:
            receipt = None
            error = platform_error(exc) if isinstance(exc, ABCPTransportError) else {"status": "timeout"}
        status = None
        try:
            status = await asyncio.wait_for(self.client.call("Workflow.getStatus", {"workflowId": workflow_id}),
                                            timeout=15)
        except (ABCPTransportError, asyncio.TimeoutError) as exc:
            status = {"error": platform_error(exc) if isinstance(exc, ABCPTransportError) else "timeout"}
        observed = (data_of(status) or {}).get("status") if isinstance(data_of(status), dict) else None
        if workflow_id in self.live:
            self.live[workflow_id]["status"] = observed or "uncertain"
        ref = self._receipt("workflow.execute.cancel", {"method": "Workflow.cancel", "workflowId": workflow_id,
            "reason": reason, "response": receipt, "error": error, "statusAfter": status,
            "status": observed or "uncertain"})
        return {"workflowId": workflow_id, "cancelAccepted": error is None, "statusAfter": observed,
                "receiptRef": ref, "interpretation": "Cancellation stops further platform work; it does not "
                "undo effects that already happened. Inspect the page before continuing."}

    async def guide(self) -> dict[str, Any]:
        result = await self.call("Workflow.getGuide", {}, allowed=CATALOG_METHODS)
        if result.get("ok"):
            data = result.get("data") or {}
            content = (data.get("skillsGuide") or {}).get("value") or data.get("content") or ""
            return {"ok": True, "guideRevision": data.get("guideRevision"), "content": content[:60000],
                    "receiptRef": result["receiptRef"]}
        return result

    async def describe(self, method: str) -> dict[str, Any]:
        return await self.call("System.describeAction", {"action": method}, allowed=CATALOG_METHODS)
