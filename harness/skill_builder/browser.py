"""Builder-owned browser trial scope and platform RPC receipts."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from abcp_client import ABCPClient, ABCPTransportError
from harness.tools.tool_policy import (
    disabled_reason_for_method, sanitize_network_read_api_response,
    sanitize_transport_payload,
)


_READ_METHODS = {"System.getCapabilities", "System.describeAction", "Workflow.getStatus"}
_PAGE_PREFIXES = ("Page.", "DOM.", "Input.")


def _platform_error(exc: ABCPTransportError) -> dict[str, Any]:
    return {
        "status": "platform_error", "transportCode": exc.transport_code,
        "rpcCode": exc.rpc_code, "rpcMethod": exc.rpc_method,
        "requestSent": exc.request_sent, "requestId": exc.request_id,
        "details": sanitize_transport_payload(exc.rpc_data),
        "message": str(exc)[:500],
    }


class BuilderBrowser:
    def __init__(self, config: Any, logger: Any):
        def record_transport(kind: str, payload: Any) -> None:
            data = payload if isinstance(payload, dict) else {}
            # The transport hook runs before Network.readApi response scrubbing
            # and may also see typed input. Keep protocol identity only.
            logger.write("skill_builder.transport", {
                "kind": kind, "requestId": data.get("id") or data.get("requestId"),
                "method": data.get("method"),
            })
        self.client = ABCPClient(config, on_event=record_transport)
        self.logger = logger
        self.fleet_id: str | None = None
        self.pages: set[str] = set()
        self.workflow_ids: set[str] = set()
        self.connected = False

    async def close(self) -> None:
        if self.connected:
            try:
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
            await self.client.connect()
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

    async def _own_fleet(self) -> str:
        await self._connect()
        if self.fleet_id:
            return self.fleet_id
        proposed = str(uuid.uuid4())
        result = await self.client.call("Fleet.create", {"fleetId": proposed,
                                                          "scope": "skill-builder"})
        observed = str((result.get("data") or {}).get("fleetId") or "")
        if observed != proposed:
            raise RuntimeError("WebCross 未确认 Builder Fleet 身份")
        self.fleet_id = proposed
        return proposed

    async def create_page(self, url: str = "about:blank") -> dict[str, Any]:
        try:
            fleet_id = await self._own_fleet()
            result = await self.client.call("Page.create", {"fleetId": fleet_id,
                                                            "url": url})
        except ABCPTransportError as exc:
            return _platform_error(exc)
        data = result.get("data") or {}
        page_id = str(data.get("pageId") or "")
        if not page_id or data.get("fleetId") != fleet_id:
            return {"status": "unknown_page_binding", "response": result}
        self.pages.add(page_id)
        self.logger.write("skill_builder.page.created", {"pageId": page_id,
                                                          "fleetId": fleet_id})
        return result

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        method = str(method or "").strip()
        if disabled_reason_for_method(method):
            return {"status": "permission_denied", "reason": disabled_reason_for_method(method)}
        if method == "Page.create":
            return await self.create_page(str(params.get("url") or "about:blank"))
        if method not in _READ_METHODS and method != "Network.readApi" and not method.startswith(_PAGE_PREFIXES):
            return {"status": "permission_denied", "reason": "Builder 工具未开放该方法"}
        if method == "Workflow.getStatus":
            if str(params.get("workflowId") or "") not in self.workflow_ids:
                return {"status": "permission_denied", "reason": "Workflow 不属于此 Builder 会话"}
        elif method not in _READ_METHODS:
            if str(params.get("pageId") or "") not in self.pages:
                return {"status": "permission_denied", "reason": "pageId 不属于 Builder 创建的页面"}
            if params.get("fleetId") not in (None, self.fleet_id):
                return {"status": "permission_denied", "reason": "fleetId 与 Builder Fleet 不一致"}
        try:
            await self._connect()
            result = await self.client.call(method, params)
        except ABCPTransportError as exc:
            result = _platform_error(exc)
        if method == "Network.readApi":
            result = sanitize_network_read_api_response(result)
            if isinstance(result, dict) and result.get("status") == "platform_error":
                result["message"] = "Network.readApi failed; use rpcCode and redacted details"
        self.logger.write("skill_builder.browser.call", {
            "method": method,
            "paramsSha256": hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest(),
            "result": sanitize_transport_payload(result),
        })
        return result

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
        try:
            self._authorize_steps(workflow.get("steps"), page_id)
        except PermissionError as exc:
            return {"status": "permission_denied", "reason": str(exc)}
        if variables:
            workflow = dict(workflow)
            workflow["initialVariables"] = {**(workflow.get("initialVariables") or {}), **variables}
        self.logger.write("skill_builder.trial.start", {
            "workflowPath": str(workflow_path), "workflowHash": expected_hash,
            "pageId": page_id, "fleetId": self.fleet_id,
        })
        try:
            await self._connect()
            result = await self.client.call("Workflow.execute", {
                "workflow": workflow,
                "binding": {"pageId": page_id, "fleetId": self.fleet_id},
            })
        except ABCPTransportError as exc:
            result = _platform_error(exc)
        data = result.get("data") or {}
        workflow_id = str(data.get("workflowId") or "")
        if workflow_id:
            self.workflow_ids.add(workflow_id)
        receipt = {"workflowHash": expected_hash, "result": sanitize_transport_payload(result)}
        self.logger.write("skill_builder.trial.result", receipt)
        return receipt
