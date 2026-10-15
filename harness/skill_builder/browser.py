"""Builder-owned browser trial scope and platform RPC receipts."""

from __future__ import annotations

import hashlib
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from abcp_client import ABCPClient, ABCPTransportError
from harness.tools.tool_policy import (
    disabled_reason_for_method, sanitize_network_read_api_response,
    sanitize_transport_payload,
)


_READ_METHODS = {"System.getCapabilities", "System.describeAction", "Workflow.getGuide",
                 "Workflow.getStatus", "Workflow.pause", "Workflow.resume", "Workflow.cancel"}
_PAGE_PREFIXES = ("Page.", "DOM.", "Input.")


def _platform_error(exc: ABCPTransportError) -> dict[str, Any]:
    return {
        "status": "platform_error", "transportCode": exc.transport_code,
        "rpcCode": exc.rpc_code, "rpcMethod": exc.rpc_method,
        "requestSent": exc.request_sent, "requestId": exc.request_id,
        "details": sanitize_transport_payload(exc.rpc_data),
        "message": str(exc)[:500],
        "connectionRecovery": getattr(exc, "connection_recovery", None),
    }


class BuilderBrowser:
    def __init__(self, config: Any, logger: Any, runtime: Any = None):
        def record_transport(kind: str, payload: Any) -> None:
            data = payload if isinstance(payload, dict) else {}
            event = (data.get("params") or {}).get("data") or {}
            progress = event.get("payload") or {}
            if (kind == "notify" and getattr(self, "active_trial", False)
                    and event.get("event") == "Workflow.progress" and progress.get("workflowId")):
                self.workflow_ids.add(str(progress["workflowId"]))
            # The transport hook runs before Network.readApi response scrubbing
            # and may also see typed input. Keep protocol identity only.
            logger.write("skill_builder.transport", {
                "kind": kind, "requestId": data.get("id") or data.get("requestId"),
                "method": data.get("method"),
            })
        from harness.runtime.browser_connection import managed_client
        self.client = managed_client(ABCPClient(config, on_event=record_transport))
        self.logger = logger
        self.runtime = runtime
        self.fleet_id: str | None = None
        self.pages: set[str] = set()
        self.page_fleets: dict[str, str] = {}
        self.capability_methods: set[str] = set()
        self.capability_catalog = {}
        self.workflow_ids: set[str] = set()
        self.connected = False
        self.active_trial = False

    async def close(self) -> None:
        if self.connected:
            try:
                if self.active_trial:
                    for workflow_id in self.workflow_ids:
                        try:
                            await self.client.call("Workflow.cancel", {"workflowId": workflow_id})
                        except ABCPTransportError:
                            pass
                if self.fleet_id:
                    try:
                        receipt = await self.client.call("Fleet.close", {"fleetId": self.fleet_id})
                        self.logger.write("skill_builder.fleet.closed",
                                          sanitize_transport_payload(receipt))
                    except ABCPTransportError as exc:
                        self.logger.write("skill_builder.fleet.close_failed",
                                          _platform_error(exc))
            finally:
                await self.client.close()
                self.connected = False

    async def _connect(self) -> None:
        if not self.connected:
            try:
                await self.client.connect()
            except BaseException:
                await self.client.close()
                raise
            self.connected = True
            try:
                registration = await self.client.call("System.register", {})
            except BaseException:
                await self.close()
                raise
            self.logger.write("skill_builder.platform.registration",
                              sanitize_transport_payload(registration))
            try:
                capabilities = await self.client.call("System.getCapabilities", {})
            except ABCPTransportError as exc:
                capabilities = _platform_error(exc)
            self.logger.write("skill_builder.platform.capabilities",
                              sanitize_transport_payload(capabilities))
            self.capability_methods = {a["method"] for a in (capabilities.get("data") or {}).get("actions", [])}
            data = capabilities.get("data") or {}
            self.capability_catalog = {"catalogRevision": data.get("catalogRevision"),
                "actions": data.get("actions", []), "source": "live System.getCapabilities"}

    async def catalog(self):
        await self._connect()
        return self.capability_catalog

    async def describe_action(self, method: str) -> dict[str, Any]:
        await self._connect()
        result = await self.client.call("System.describeAction", {"action": method})
        schema = result.get("data") or {}
        if schema.get("method") == method:
            path = Path(__file__).resolve().parents[2] / "global_schema_cache" / "schemas" / (method + ".json")
            path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
        return result

    async def guide(self) -> dict[str, Any]:
        await self._connect()
        result = await self.client.call("Workflow.getGuide", {})
        data = result.get("data") or {}
        return {"content": (data.get("skillsGuide") or {}).get("value", ""),
                "guideRevision": data.get("guideRevision"), "source": "live Workflow.getGuide",
                "actionCatalog": self.capability_catalog}

    async def authorize_files(self, paths: list[str]) -> dict[str, Any]:
        from harness.tools.path_authorization import authorize_local_paths
        return await authorize_local_paths(self, paths, "read")

    async def attach_page(self, page_id: str, fleet_id: str) -> dict[str, Any]:
        from harness.runtime.hitl_input import read_terminal_input
        decision = await read_terminal_input(
            f"[Skill Builder] Request to use test page {page_id} (Fleet {fleet_id}). Scripts may modify this page. Enter yes to authorize this trial.")
        approved = str(decision or "").strip().lower() in {"yes", "y"}
        self.logger.write("skill_builder.page.authorization", {"pageId": page_id, "fleetId": fleet_id, "approved": approved})
        if not approved:
            return {"status": "permission_denied", "reason": "Test page was not authorized by the user"}
        await self._connect()
        if page_id not in await self._inventory(fleet_id):
            return {"status": "unknown_page_binding", "reason": "Page was not found in the authorized Fleet"}
        await self.client.call("Page.switchTo", {"pageId": page_id})
        self.pages.add(page_id); self.page_fleets[page_id] = fleet_id
        store = self._test_store()
        saved = store.read("coding/test-pages.json", {})
        saved.setdefault("pages", {})[page_id] = {"fleetId": fleet_id, "independent": False,
            "authorization": "User explicitly attached this existing test page"}
        store.write("coding/test-pages.json", saved, "builder_test_pages")
        return {"status": "authorized", "pageId": page_id, "fleetId": fleet_id}

    async def _own_fleet(self) -> str:
        await self._connect()
        if self.fleet_id:
            return self.fleet_id
        proposed = str(uuid.uuid4())
        result = await self.client.call("Fleet.create", {"fleetId": proposed,
                                                          "scope": "skill-builder"})
        observed = str((result.get("data") or {}).get("fleetId") or "")
        if observed != proposed:
            raise RuntimeError("WebCross did not confirm the Builder Fleet identity")
        self.fleet_id = proposed
        return proposed

    def _test_store(self):
        from harness.skill_runtime.state import SkillStateStore
        return SkillStateStore(self.logger)

    async def _inventory(self, fleet_id):
        await self._connect()
        raw = await self.client.call("Page.list", {"fleetId": fleet_id})
        ref = self._test_store().write("coding/test-page-inventory.json", raw, "browser_receipt")
        data = raw.get("data")
        # Native array contract; legacy envelopes remain readable explicitly.
        rows = data.get("pages") if isinstance(data, dict) else data
        if not isinstance(rows, list) or any(not isinstance(p, dict) or not p.get("pageId") for p in rows):
            raise ValueError(f"Page.list returned an invalid inventory; receipt: {ref}")
        return {p["pageId"] for p in rows}

    async def restore_page(self, page_id):
        if page_id in self.pages:
            return
        saved = self._test_store().read("coding/test-pages.json", {}).get("pages", {}).get(page_id)
        if not saved:
            raise PermissionError("Page is not recorded as this Builder's authorized test page")
        fleet = saved["fleetId"]
        if page_id not in await self._inventory(fleet):
            raise ValueError("Saved test page no longer exists; create a new independent test and restore its prerequisites")
        self.pages.add(page_id)
        self.page_fleets[page_id] = fleet

    async def create_page(self, url: str = "about:blank", *, fleet_id=None) -> dict[str, Any]:
        from harness.runtime.steering import current_steering, dispatch_boundary
        from types import SimpleNamespace
        manager = current_steering()
        revision = manager.revision if manager else 0
        store = self._test_store()
        saved = store.read("coding/test-pages.json", {})
        existing = set()
        try:
            if fleet_id:
                if fleet_id not in saved.get("authorizedFleets", []):
                    from harness.runtime.hitl_input import read_terminal_input
                    decision = await read_terminal_input(
                        f"[Skill Builder] 在 Fleet {fleet_id} 新建独立测试页并试运行草稿，可能产生真实业务副作用。"
                        "已有页面不会作为测试页。输入 yes 授权本任务使用此 Fleet 的新测试页。")
                    approved = str(decision or "").strip().lower() in {"yes", "y"}
                    self.logger.write("skill_builder.fleet.authorization", {"fleetId": fleet_id, "approved": approved})
                    if not approved:
                        return {"status": "permission_denied", "reason": "Test Fleet was not authorized"}
                    saved.setdefault("authorizedFleets", []).append(fleet_id)
                    store.write("coding/test-pages.json", saved, "builder_test_pages")
                existing = await self._inventory(fleet_id)
            else:
                fleet_id = await self._own_fleet()
            pending = dispatch_boundary(SimpleNamespace(steering_role=manager.primary if manager else None), revision, "Page.create")
            if pending:
                return pending
            result = await self.client.call("Page.create", {"fleetId": fleet_id,
                                                            "url": url, "purpose": "Create an isolated Skill Builder test page"})
        except ABCPTransportError as exc:
            return _platform_error(exc)
        data = result.get("data") or {}
        ref = store.write("coding/test-page-created.json", result, "browser_receipt")
        from harness.runtime.program_validation import validate_test_page
        try:
            page_id = validate_test_page(data, fleet_id, existing)
        except ValueError as exc:
            return {"status": "unknown_page_binding", "reason": str(exc), "response": result, "receiptRef": ref}
        saved.setdefault("pages", {})[page_id] = {"fleetId": fleet_id, "receiptRef": ref, "independent": True}
        store.write("coding/test-pages.json", saved, "builder_test_pages")
        self.pages.add(page_id)
        self.page_fleets[page_id] = fleet_id
        self.logger.write("skill_builder.page.created", {"pageId": page_id,
                                                          "fleetId": fleet_id})
        return {**result, "pageId": page_id, "fleetId": fleet_id, "receiptRef": ref}

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from harness.runtime.steering import current_steering, dispatch_boundary
        from types import SimpleNamespace
        manager = current_steering()
        input_revision = manager.revision if manager else 0
        method = str(method or "").strip()
        if disabled_reason_for_method(method):
            return {"status": "permission_denied", "reason": disabled_reason_for_method(method)}
        if method == "Page.create":
            return await self.create_page(str(params.get("url") or "about:blank"), fleet_id=params.get("fleetId"))
        if method not in _READ_METHODS and method not in {"Network.readApi", "File.handleChooser"} and not method.startswith(_PAGE_PREFIXES):
            return {"status": "permission_denied", "reason": "This method is not available to Builder tools"}
        if method in {"Workflow.getStatus", "Workflow.pause", "Workflow.resume", "Workflow.cancel"}:
            if str(params.get("workflowId") or "") not in self.workflow_ids:
                return {"status": "permission_denied", "reason": "Workflow does not belong to this Builder session"}
        elif method not in _READ_METHODS:
            if str(params.get("pageId") or "") not in self.pages:
                return {"status": "permission_denied", "reason": "pageId is not an authorized test page for this Builder"}
            if params.get("fleetId") not in (None, self.page_fleets.get(str(params.get("pageId")), self.fleet_id)):
                return {"status": "permission_denied", "reason": "fleetId does not match the Builder Fleet"}
        if method == "File.handleChooser":
            rejected = await self.authorize_files(params.get("files") or [])
            if rejected.get("status") != "authorized":
                return rejected
        try:
            await self._connect()
            pending = dispatch_boundary(SimpleNamespace(steering_role=manager.primary if manager else None), input_revision, method)
            if pending: return pending
            result = await self.client.call(method, params)
        except ABCPTransportError as exc:
            result = _platform_error(exc)
        if method == "Network.readApi":
            result = sanitize_network_read_api_response(result)
            if isinstance(result, dict) and result.get("status") == "platform_error":
                result["message"] = "Network.readApi failed; use rpcCode and redacted details"
        evidence = {
            "method": method,
            "paramsSha256": hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest(),
            "result": sanitize_transport_payload(result),
        }
        self.logger.write("skill_builder.browser.call", evidence)
        from harness.skill_runtime.state import SkillStateStore
        ref = SkillStateStore(self.logger).write("coding/observations/" + uuid.uuid4().hex + ".json",
            evidence, "skill_builder_observation")
        return {**result, "receiptRef": ref}

    def _authorize_steps(self, steps: Any, page_id: str) -> None:
        if not isinstance(steps, list):
            return  # WebCross owns DSL shape errors.
        for step in steps:
            if not isinstance(step, dict):
                continue
            method = str(step.get("action") or "")
            if method:
                reason = disabled_reason_for_method(method)
                if reason:
                    raise PermissionError(reason)
                if not method.startswith(_PAGE_PREFIXES):
                    raise PermissionError(f"trial action is outside Builder browser scope: {method}")
                # The current trial surface uses one owned page. Multi-page
                # workflows can still be authored; the trial needs a scoped
                # capability extension before they can be executed here.
                if method == "Page.create" or method in {"Page.list", "Page.switchTo", "Fleet.create"}:
                    raise PermissionError(f"trial cannot bind newly discovered pages: {method}")
                params = step.get("params")
                if isinstance(params, dict):
                    if params.get("pageId") not in (None, page_id):
                        raise PermissionError("trial step pageId is outside the owned page")
                    if params.get("fleetId") not in (None, self.fleet_id):
                        raise PermissionError("trial step fleetId is outside the owned Fleet")
            for key in ("then", "else", "body"):
                if key in step:
                    self._authorize_steps(step[key], page_id)

    async def trial(self, workflow_path: Path, page_id: str,
                    variables: dict[str, Any] | None = None) -> dict[str, Any]:
        from harness.runtime.steering import current_steering
        manager = current_steering()
        input_revision = manager.revision if manager else 0
        if self.active_trial:
            return {"status": "trial_busy", "reason": "A trial is already active in this session"}
        if page_id not in self.pages:
            return {"status": "permission_denied", "reason": "trial page is not Builder-owned"}
        raw = workflow_path.read_bytes()
        expected_hash = hashlib.sha256(raw).hexdigest()
        try:
            workflow = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {"status": "invalid_json", "error": str(exc)}
        if not isinstance(workflow, dict):
            return {"status": "invalid_document", "error": "Workflow file must hold an object"}
        from harness.workflow.script_runtime import is_script_package, scope_package, input_files
        execution_package = workflow
        scope = None
        fleet_id = self.page_fleets.get(page_id, self.fleet_id)
        try:
            await self._connect()
            if is_script_package(workflow):
                execution_package, scope = scope_package(workflow, page_id=page_id,
                    fleet_id=str(fleet_id or ""), methods=self.capability_methods,
                    files=input_files(self, variables or {}))
            else:
                self._authorize_steps(workflow.get("steps"), page_id)
        except (PermissionError, ValueError) as exc:
            return {"status": "permission_denied" if isinstance(exc, PermissionError) else "invalid_document", "reason": str(exc)}
        if variables and not scope:
            workflow = dict(workflow)
            workflow["initialVariables"] = {**(workflow.get("initialVariables") or {}), **variables}
        self.logger.write("skill_builder.trial.start", {
            "workflowPath": str(workflow_path), "workflowHash": expected_hash,
            "pageId": page_id, "fleetId": fleet_id, "scope": scope,
        })
        try:
            self.active_trial = True
            await self._connect()
            params = {
                "workflow": execution_package if scope else workflow,
                **({"input": variables or {}} if scope else {}),
                "binding": scope["platformBinding"] if scope else {"pageId": page_id, "fleetId": fleet_id},
            }
            from harness.runtime.workflow_steering import controlled_call
            from harness.observation.exec_observer import ExecObserver
            from harness.observation.browser_call import build_browser_call_runner
            from types import SimpleNamespace
            observer = ExecObserver(self.client, page_id=page_id).start()
            runner = build_browser_call_runner(browser=self.client, logger=self.logger, capability_methods=self.capability_methods)
            try:
                result = await controlled_call(SimpleNamespace(browser=self.client, capability_methods=self.capability_methods,
                    steering_role=manager.primary if manager else None, _steering_operation_revision=input_revision),
                                               runner, params, observer)
            finally:
                observer.stop()
        except ABCPTransportError as exc:
            result = _platform_error(exc)
        except asyncio.CancelledError:
            for workflow_id in self.workflow_ids:
                try:
                    await self.client.call("Workflow.cancel", {"workflowId": workflow_id})
                except ABCPTransportError:
                    pass
            raise
        finally:
            self.active_trial = False
        data = result.get("data") or {}
        workflow_id = str(data.get("workflowId") or "")
        if workflow_id:
            self.workflow_ids.add(workflow_id)
        records = data.get("results")
        known_records = isinstance(records, list) and all(isinstance(r, dict) for r in records)
        receipt = {"workflowHash": expected_hash, "result": sanitize_transport_payload(result),
                   "executionFacts": {
                       "workflowStatus": data.get("status"),
                       "callRecordCount": len(records) if known_records else None,
                       "succeededCallRecordCount": sum(r.get("status") == "succeeded" for r in records)
                           if known_records else None,
                       "failedCallRecordCount": sum(r.get("status") == "failed" for r in records)
                           if known_records else None,
                       "outputAvailable": data.get("output") is not None if "output" in data else None,
                       "recordedActions": sorted({r["action"] for r in records if isinstance(r.get("action"), str)})
                           if known_records else None,
                   }}
        self.logger.write("skill_builder.trial.result", receipt)
        return receipt
