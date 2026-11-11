"""Task-local durable state and receipts, indexed through the resource store.

Works with the database, file and dual backends: database rows carry
``resource_id``/``content_json``; file rows carry a path and ``content_text``.
"""
from __future__ import annotations

import json
from typing import Any

from harness.storage.sqlite_store import build_resource_uri
from harness.storage.base import StorageCorruptError


def _decode(record: dict[str, Any] | None, default: Any = None, *, strict: bool = False) -> Any:
    if not record:
        return default
    value = record.get("content_json")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            if strict:
                raise StorageCorruptError("Conversation resource contains invalid JSON") from exc
            raise
    if value is not None:
        return value
    text = record.get("content_text")
    if text is None:
        if strict:
            raise StorageCorruptError("Conversation resource exists without a decodable JSON payload")
        return default
    try:
        return json.loads(text)
    except ValueError as exc:
        if strict:
            raise StorageCorruptError("Conversation resource contains invalid JSON") from exc
        return {"text": text}


class StateStore:
    def __init__(self, logger: Any):
        self.logger = logger

    def _uri(self, row: dict[str, Any]) -> str:
        if row.get("resource_id"):
            return build_resource_uri(self.logger.task_id, row["resource_id"])
        return row.get("logical_path") or row["saved_path"]

    def _read_raw(self, path: str, default: Any = None, *, strict: bool = False) -> Any:
        rows = self.logger.storage.search_resources(task_id=self.logger.task_id, path_glob=path,
                                                    max_results=100)
        current = [r for r in rows if r.get("logical_path") == path and r.get("is_current", True)]
        if not current:
            return default
        row = max(current, key=lambda r: (r.get("resource_version", 0), r.get("created_at", "")))
        record = self.logger.storage.read_resource(current_task_id=self.logger.task_id,
                                                   resource_uri=self._uri(row))
        if record is None and strict:
            raise StorageCorruptError("Conversation resource disappeared after lookup")
        return _decode(record, default, strict=strict)

    def _read_ref_raw(self, reference: str, *, strict: bool = False) -> Any:
        """Read a saved receipt; only this task's own resources resolve."""
        record = self.logger.storage.read_resource(current_task_id=self.logger.task_id,
                                                   resource_uri=reference)
        if not record:
            raise ValueError(f"Unknown receipt reference: {reference}")
        logical = str(record.get("logical_path") or "")
        strict = strict or logical in {"workflow/context.json", "workflow/messages.json"} or logical.startswith("workflow/conversation/")
        return _decode(record, strict=strict)

    def read(self, path: str, default: Any = None) -> Any:
        if path in {"workflow/context.json", "workflow/messages.json"}:
            from harness.workflow_mode.conversation import ConversationStore
            missing = object()
            head = self._read_raw("workflow/context.json", missing, strict=True)
            if isinstance(head, dict):
                history, context = ConversationStore(self).restore(head)
                return context if path == "workflow/context.json" else history
            if head is not missing and not isinstance(head, list):
                raise StorageCorruptError("Legacy conversation context is not an array")
            value = self._read_raw(path, missing, strict=True)
            if value is missing:
                return default
            if not isinstance(value, list):
                raise StorageCorruptError("Legacy conversation view is not an array")
            return value
        return self._read_raw(path, default)

    def read_ref(self, reference: str) -> Any:
        value = self._read_ref_raw(reference)
        if isinstance(value, dict) and str(value.get("format", "")).startswith("workflow-conversation-v"):
            from harness.workflow_mode.conversation import ConversationStore
            return ConversationStore(self).restore(value)[1]
        return value

    def write(self, path: str, value: Any, kind: str = "workflow_session") -> str:
        record = self.logger.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
            resource_type=kind, logical_path=path, content=value, metadata={"mode": "workflow"})
        return record["saved_path"]

    def load(self) -> dict[str, Any]:
        return self.read("workflow/session.json", {}) or {}

    def save(self, state: dict[str, Any]) -> str:
        return self.write("workflow/session.json", state)

    def receipt(self, state: dict[str, Any], event: str, value: dict[str, Any]) -> str:
        index = len(state.setdefault("receipts", []))
        ref = self.write(f"workflow/receipts/{index:06d}.json", value, "workflow_receipt")
        state["receipts"].append({"ref": ref, "event": event})
        self.save(state)
        # Only Workflow.execute carries a business outcome. Other receipts may hold
        # any JSON (Fleet.list and Page.list return arrays), so read defensively.
        outcome = None
        if event == "workflow.execute.result":
            response = value.get("response")
            data = response.get("data") if isinstance(response, dict) else None
            output = data.get("output") if isinstance(data, dict) else None
            outcome = output.get("outcome") if isinstance(output, dict) else None
        self.logger.write(event, {"receiptRef": ref, **{k: value[k] for k in
            ("method", "status", "pageId", "workflowId", "contentHash") if k in value},
            **({"outcome": outcome} if outcome else {})})
        return ref
