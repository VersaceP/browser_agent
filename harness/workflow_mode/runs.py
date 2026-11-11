"""Live Workflow task registry: version, page and execution identities; stop requests.

One JSON record per live task under <worktree>/.workflow-runs/. Records let a
separate /workflow-stop process find tasks, and let tasks refuse to claim a page
another live task already owns. A stop request is a flag the owning process
observes; only the owner cancels its own WebCross executions.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


class RunRegistry:
    def __init__(self, worktree_dir: str | Path):
        root = Path(worktree_dir).expanduser()
        self.root = (root if root.is_absolute() else (Path.cwd() / root).resolve()) / ".workflow-runs"

    def _path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"

    def read(self, task_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, record: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        record["updatedAt"] = _now()
        path = self._path(record["taskId"])
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return record

    def register(self, task_id: str, **fields: Any) -> dict[str, Any]:
        record = self.read(task_id) or {"taskId": task_id, "startedAt": _now(), "workflowIds": []}
        record.update({k: v for k, v in fields.items() if v is not None},
                      pid=os.getpid(), status="running", stopRequested=False)
        return self._write(record)

    def update(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        record = self.read(task_id)
        if record is None:
            return None
        record.update(fields)
        return self._write(record)

    def add_workflow(self, task_id: str, workflow_id: str) -> None:
        record = self.read(task_id)
        if record is not None and workflow_id not in record.setdefault("workflowIds", []):
            record["workflowIds"].append(workflow_id)
            self._write(record)

    def finish(self, task_id: str, status: str) -> None:
        self.update(task_id, status=status, finishedAt=_now(), pid=None)

    def live(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        rows = []
        for path in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if record.get("status") == "running" and _alive(record.get("pid")):
                rows.append(record)
        return rows

    @contextmanager
    def _page_claim_lock(self):
        import fcntl
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / '.page-claims.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _page_owner(self, task_id, pages):
        for record in self.live():
            held = set(record.get('batchPageIds', [])) | {record.get('pageId')}
            if record['taskId'] != task_id and held.intersection(pages):
                return record['taskId']
        return None

    def claim_page(self, task_id: str, page_id: str) -> str | None:
        """Return the owning task when another live task already holds the page."""
        with self._page_claim_lock():
            owner = self._page_owner(task_id, {page_id})
            if owner:
                return owner
            self.update(task_id, pageId=page_id)
        return None

    def claim_pages(self, task_id: str, pages: list[str]) -> str | None:
        with self._page_claim_lock():
            owner = self._page_owner(task_id, set(pages))
            if owner:
                return owner
            record = self.read(task_id) or {}
            self.update(task_id, batchPageIds=sorted(set(record.get('batchPageIds', [])) | set(pages)))
        return None

    def release_pages(self, task_id: str, pages: list[str]) -> None:
        with self._page_claim_lock():
            record = self.read(task_id) or {}
            self.update(task_id, batchPageIds=sorted(set(record.get('batchPageIds', [])) - set(pages)))

    def request_stop(self, reference: str) -> list[dict[str, Any]]:
        text = str(reference or "").strip().removeprefix("@")
        rows = self.live()
        if text and text != "all":
            rows = [r for r in rows if r["taskId"] == text or r["taskId"].startswith(text)
                    or r.get("batchId") == text]
        stopped = []
        for record in rows:
            self.update(record["taskId"], stopRequested=True, stopRequestedAt=_now())
            stopped.append({k: record.get(k) for k in ("taskId", "slug", "label", "pageId", "workflowIds",
                                                        "batchId")})
        return stopped

    def stop_requested(self, task_id: str) -> bool:
        return bool((self.read(task_id) or {}).get("stopRequested"))
