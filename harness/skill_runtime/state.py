"""Task-local durable Skill state, indexed through the existing resource store."""
from __future__ import annotations

import json
from typing import Any


class SkillStateStore:
    def __init__(self, logger: Any):
        self.logger = logger

    def read(self, path: str, default=None):
        resources = self.logger.storage.search_resources(task_id=self.logger.task_id,
            path_glob=path, max_results=100)
        current = [r for r in resources if r.get("logical_path") == path and r.get("is_current", True)]
        if not current:
            return default
        from harness.storage.sqlite_store import build_resource_uri
        item = max(current, key=lambda r: (r.get("resource_version", 0), r.get("created_at", "")))
        record = self.logger.storage.read_resource(current_task_id=self.logger.task_id,
            resource_uri=build_resource_uri(self.logger.task_id, item["resource_id"]))
        value = record.get("content_json") if record else None
        return json.loads(value) if isinstance(value, str) else value if value is not None else default

    def write(self, path: str, value: Any, kind: str = "skill_session") -> str:
        record = self.logger.storage.save_resource(task_id=self.logger.task_id,
            run_id=self.logger.run_id, resource_type=kind, logical_path=path,
            content=value, metadata={"mode": "skill"})
        return record["saved_path"]

    def load(self) -> dict:
        return self.read("skill/session.json", {})

    def save(self, state: dict) -> str:
        return self.write("skill/session.json", state)

    def record(self, state: dict, event: str, value: dict) -> str:
        ref = self.write(f"skill/receipts/{len(state.setdefault('receipts', [])):06d}.json",
                         value, "skill_execution_receipt")
        state["receipts"].append({"ref": ref, "event": event})
        self.save(state)
        self.logger.write(event, {"receiptRef": ref, **{k: value[k] for k in
            ("stepId", "status", "sourceHash", "inputHash") if k in value}})
        return ref
