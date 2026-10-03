"""Task-local receipt lookup for the standalone Browser reviewer.

This is a read adapter over existing receipts, not a semantic evidence judge.
The cache lives outside model context and is rebuilt after process recovery.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def is_tool_receipt(item: Any) -> bool:
    return (isinstance(item, dict) and isinstance(item.get("result"), dict)
            and item.get("type") not in {
                "model", "final_answer", "browser_task_checkpoint",
                "browser_completion_proposal", "loop_nudge", "page_stats",
                "snapshot_diff", "progress_observation"})


def receipt_view(item: dict) -> dict:
    """Remove host-file instructions from the review-facing AXTree envelope.

    Raw receipts and their identity hashes remain unchanged. Those directions
    target the Browser executor's host, not the reviewer's task read tools.
    """
    result = item.get("result")
    if not isinstance(result, dict) or result.get("method") != "DOM.getAXTree":
        return item
    response = result.get("response")
    if not isinstance(response, dict):
        return item
    return {**item, "result": {**result, "response": {
        key: value for key, value in response.items()
        if key not in {"observation", "suggested_prompt"}}}}


class ReceiptIndex:
    def __init__(self, agent: Any):
        self.agent = agent
        self.task_id = agent.logger.task_id
        self.run_id = str(agent.logger.run_id or "")
        self.worker_id = str(getattr(agent, "worker_id", "") or "")
        self.records: dict[str, dict] = {}
        self.live_ids: dict[int, str] = {}
        self.event_cursor = 0
        self.archives_loaded = False
        self.problems: list[str] = []

    def _add(self, item: dict, source: dict) -> str:
        content = canonical(item)
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        identity = {"taskId": self.task_id, **source}
        ref = "receipt:" + digest([identity, fingerprint])[:32]
        self.records[ref] = {"evidenceId": ref, "source": identity,
                             "receiptSha256": fingerprint, "content": content}
        return ref

    def refresh(self, trace: list) -> None:
        # A live index may be reused by later reviews, but a changed entry must
        # not leave its previous reference available as if it were unchanged.
        for ref in self.live_ids.values():
            self.records.pop(ref, None)
        self.live_ids = {}
        for index, item in enumerate(trace):
            if is_tool_receipt(item):
                self.live_ids[index] = self._add(item, {
                    "kind": "trace", "runId": self.run_id,
                    "workerId": self.worker_id, "index": index})

    def _payload(self, row: dict) -> dict:
        raw = row.get("payload_json")
        if raw is None and row.get("payload_resource_id"):
            from harness.storage.sqlite_store import build_resource_uri
            resource = self.agent.logger.storage.read_resource(
                current_task_id=self.task_id,
                resource_uri=build_resource_uri(self.task_id, row["payload_resource_id"]))
            if resource is None:
                raise ValueError("event payload unavailable")
            raw = resource.get("content_json") or resource.get("content_text")
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            raise ValueError("event payload is not an object")
        return value

    def load_history(self) -> None:
        store = self.agent.logger.storage
        self.problems = []
        # list_worker_trace predates cursor paging. Read its complete snapshot
        # once per execution instance, growing the bound until exhaustion; never
        # silently treat its default 1000 rows as complete task history.
        if not self.archives_loaded:
            limit = 1000
            try:
                while True:
                    rows = store.list_worker_trace(task_id=self.task_id, limit=limit)
                    if len(rows) < limit:
                        break
                    limit *= 2
                for row in rows:
                    if row.get("task_id") != self.task_id:
                        raise ValueError("trace task identity mismatch")
                    item = json.loads(row["trace_json"])
                    if not is_tool_receipt(item):
                        continue
                    run = str(row.get("run_id") or "")
                    source = {"kind": "trace" if run else "legacy_trace",
                              "runId": run, "workerId": row.get("worker_id") or "",
                              "index": int(row["sequence_no"]) - 1}
                    # File-only legacy traces lack run attribution. Never fill
                    # that gap with the currently resumed run's identity.
                    self._add(item, source)
                self.archives_loaded = True
            except Exception as exc:
                self.problems.append("archived_trace_unavailable:" + type(exc).__name__)
        try:
            while True:
                rows = store.read_events(task_id=self.task_id,
                                         after_event_id=self.event_cursor, limit=500)
                if not rows:
                    break
                for row in rows:
                    if row.get("task_id") != self.task_id:
                        raise ValueError("event task identity mismatch")
                    event_id = int(row["event_id"])
                    # Only actual browser responses; requests, model commentary
                    # and reviewer verdicts must not become action evidence.
                    if row.get("event_type") == "browser.call.result":
                        # Do not advance past an unreadable payload. A later
                        # load retries that record rather than claiming full
                        # coverage while permanently skipping the missing fact.
                        payload = self._payload(row)
                        self._add({"type": "browser_call", "result": payload}, {
                            "kind": "event", "runId": row.get("run_id") or "",
                            "workerId": row.get("worker_id") or "",
                            "eventKey": row.get("event_uid") or str(event_id)})
                    self.event_cursor = event_id
                if len(rows) < 500:
                    break
        except Exception as exc:
            self.problems.append("event_history_unavailable:" + type(exc).__name__)

    def fact(self, ref: str) -> dict | None:
        record = self.records.get(ref)
        if record is None:
            return None
        return {"kind": "receipt", "source": record["source"],
                "receiptSha256": record["receiptSha256"]}

    def read(self, ref: str, offset: int = 0) -> dict:
        record = self.records.get(ref)
        if record is None:
            return {"status": "unavailable", "reason": "receipt_not_available_in_this_task"}
        raw = canonical(receipt_view(json.loads(record["content"])))
        offset = max(0, offset)
        return {"status": "done", "evidenceId": ref, **self.fact(ref),
                "content": raw[offset:offset + 16000], "offset": offset,
                "nextOffset": offset + 16000 if offset + 16000 < len(raw) else None,
                "totalChars": len(raw), "historicalObservation": True}

    def search(self, term: str, cursor: str = "") -> dict:
        snapshot = digest([term, sorted(self.records)])[:20]
        after = ""
        if cursor:
            prefix, separator, after = cursor.partition("/")
            if not separator or prefix != snapshot or after not in self.records:
                return {"status": "unavailable", "reason": "search_cursor_mismatch",
                        "notice": "Search data or term changed. Restart this search without cursor."}
        matches = []
        for ref, record in sorted(self.records.items()):
            if after and ref <= after:
                continue
            text = record["content"]
            pos = text.casefold().find(term.casefold())
            if pos >= 0:
                matches.append({"evidenceId": ref, "source": record["source"],
                                "excerpt": text[max(0, pos-100):pos+300]})
        page = matches[:30]
        return {"status": "done", "matches": page, "truncated": len(matches) > 30,
                "nextCursor": snapshot + "/" + page[-1]["evidenceId"] if len(matches) > 30 else None,
                "coverage": {"taskId": self.task_id, "scope": "task_history_and_current_trace",
                             "eventCursor": self.event_cursor,
                             "archiveLoaded": self.archives_loaded,
                             "problems": list(dict.fromkeys(self.problems))},
                "notice": "Literal matches locate receipts, not proof of completion or absence. Historical page observations do not establish current page state."}


def receipt_index(agent: Any, trace: list) -> ReceiptIndex:
    index = getattr(agent, "_browser_review_receipts", None)
    if (not isinstance(index, ReceiptIndex) or index.task_id != agent.logger.task_id
            or index.run_id != str(agent.logger.run_id or "")
            or index.worker_id != str(getattr(agent, "worker_id", "") or "")):
        index = ReceiptIndex(agent)
        agent._browser_review_receipts = index
    index.refresh(trace)
    return index
