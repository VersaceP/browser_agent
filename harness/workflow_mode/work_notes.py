"""Pointers to model-maintained task notes; never a business-state authority.

The original local-file receipt already holds the complete submitted content.
Keep only its location and the bytes actually acknowledged by local_fs_batch.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOTE_PATH = "scratchpad/progress.json"
_KEYS = ("path", "receiptRef", "contentPointer", "sha256", "byteSize", "operation",
         "operationIndex", "contentFormat", "confirmedAt", "savedAfterExecutionCount")


def expected_path(session: Any) -> str:
    return str(Path(session.logger.task_dir).resolve() / NOTE_PATH)


def projection(session: Any) -> dict[str, Any]:
    entry = session.state.get("workNotes")
    if not isinstance(entry, dict) or not entry.get("receiptRef") or not entry.get("contentPointer"):
        return {"status": "unregistered", "path": expected_path(session),
                "recovery": "Read the original goal and owned receipts; create or rebuild notes with local_fs_batch. No execution is blocked."}
    executions = session.state.get("executions") or []
    current_count = len(executions)
    saved_count = entry.get("savedAfterExecutionCount", entry.get("executionCount"))
    valid_count = type(saved_count) is int and 0 <= saved_count <= current_count
    after = executions[max(saved_count, current_count - 6):] if valid_count else []
    return {"status": "registered", **{k: entry[k] for k in _KEYS if k in entry},
            "savedAfterExecutionCount": saved_count, "currentExecutionCount": current_count,
            "executionsRecordedSinceSave": {
                "total": current_count - saved_count if valid_count else None,
                "receiptRefs": [e["receiptRef"] for e in after
                                if isinstance(e, dict) and isinstance(e.get("receiptRef"), str)],
                "truncated": current_count - saved_count > 6 if valid_count else False},
            "fileStatus": "not_rechecked",
            "recovery": "Read the saved receipt contentPointer for the last confirmed full content. Check the live file/hash before trusting it; missing, empty or changed notes can be rebuilt."}


def _written_bytes(operation: dict[str, Any]) -> tuple[bytes, str]:
    content = operation["content"]
    if operation["op"] == "write_text":
        if not isinstance(content, str):
            raise ValueError("write_text content is not text")
        return content.encode("utf-8"), "text"
    form = "json_text" if isinstance(content, str) else "json_value"
    value = json.loads(content) if isinstance(content, str) else content
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"), form


def register_write(session: Any, arguments: dict[str, Any], result: Any,
                   receipt_ref: str) -> dict[str, Any]:
    """Register confirmed writes only, even when another batch item failed.

    A failed/unverifiable item never replaces the previous confirmed pointer.
    No note fields, non-emptiness, or business semantics are prescribed here.
    """
    operations = arguments.get("operations")
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(operations, list) or not isinstance(rows, list):
        return {"status": "unchanged", "updated": False}
    candidate = None
    issues = []
    # Operation order, rather than response order, identifies the last success.
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or operation.get("op") not in {"write_json", "write_text"}:
            continue
        successes = [row for row in rows if isinstance(row, dict)
                     and type(row.get("index")) is int and row["index"] == index
                     and row.get("status") == "done" and row.get("op") == operation["op"]]
        if not successes:
            continue
        row = successes[-1]
        if row.get("path") != expected_path(session):
            continue
        try:
            if "content" not in operation:
                raise ValueError("successful write has no complete content in its input receipt")
            data, form = _written_bytes(operation)
            digest = hashlib.sha256(data).hexdigest()
            if row.get("sha256") != digest or type(row.get("byteSize")) is not int or row["byteSize"] != len(data):
                raise ValueError("acknowledged hash/size does not match the complete receipt content")
            if not receipt_ref:
                raise ValueError("successful write has no saved receipt reference")
            candidate = {"path": row["path"], "receiptRef": receipt_ref,
                         "contentPointer": f"/input/operations/{index}/content",
                         "sha256": digest, "byteSize": len(data), "operation": operation["op"],
                         "operationIndex": index, "contentFormat": form,
                         "confirmedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "savedAfterExecutionCount": len(session.state.get("executions") or [])}
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            issues.append({"index": index, "error": str(exc)})
    if candidate is not None:
        session.state["workNotes"] = candidate
    return {"status": "registered" if candidate is not None else "unchanged",
            "updated": candidate is not None, **({"issues": issues} if issues else {})}



def compaction_block(note: Any) -> str:
    """Keep the small locator outside a model-generated or bounded summary."""
    if not isinstance(note, dict) or not isinstance(note.get("path"), str):
        return ""
    locator = {k: note[k] for k in ("status", *_KEYS, "currentExecutionCount", "executionsRecordedSinceSave") if k in note}
    return "\n\n<workflow-work-notes>\n" + json.dumps(locator, ensure_ascii=False) + "\n</workflow-work-notes>"
