"""Small, attributed Browser resume history; never restores browser authority."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from harness.utils import load_task_json, read_task_file_text, storage_for_logger


def source_read_fact(tool_call, result):
    """Describe the returned slice, without copying source contents or guessing its role."""
    if (tool_call.get("name") != "local_fs_read" or not isinstance(result, dict)
            or result.get("status") != "done" or not isinstance(result.get("content"), str)):
        return None
    return {
        "path": result.get("path"), "toolCallId": tool_call.get("id"),
        "lineOffset": result.get("lineOffset"), "linesRead": result.get("linesRead"),
        "truncated": result.get("truncated"), "nextLineOffset": result.get("nextLineOffset"),
        "readContentSha256": hashlib.sha256(result["content"].encode("utf-8")).hexdigest(),
    }


def _lineage(plan, phase_id):
    phases = {p["id"]: p for p in (plan or {}).get("phases", [])
              if isinstance(p, dict) and isinstance(p.get("id"), str)}
    result = set()
    pending = [phase_id]
    while pending:
        current = pending.pop()
        if not current or current in result:
            continue
        result.add(current)
        meta = (phases.get(current, {}).get("worker_contract") or {}).get("_delegation") or {}
        pending.extend(value for key in ("lineage", "replaces")
                       if isinstance(value := meta.get(key), str) and value in phases)
    return result


def _resource_payload(storage, task_id, row):
    payload = row.get("payload_json")
    if payload is not None:
        return json.loads(payload) if isinstance(payload, str) else payload
    resource_id = row.get("payload_resource_id")
    if not resource_id:
        return {}
    from harness.storage.sqlite_store import build_resource_uri
    resource = storage.read_resource(current_task_id=task_id,
        resource_uri=build_resource_uri(task_id, resource_id)) or {}
    content = resource.get("content_json") or resource.get("content_text")
    return json.loads(content) if isinstance(content, str) else content or {}


def _events(storage, task_id, event_type):
    cursor = 0
    while True:
        rows = storage.read_events(task_id=task_id, event_type=event_type,
                                   after_event_id=cursor, limit=100)
        if not rows:
            return
        for row in rows:
            # File and DB event readers both carry the task/run envelope.
            if row.get("task_id") == task_id:
                yield row, _resource_payload(storage, task_id, row)
        next_cursor = int(rows[-1]["event_id"])
        if next_cursor <= cursor:
            raise ValueError("resume event cursor did not advance")
        cursor = next_cursor


def _legacy_source_reads(messages):
    """Recover only actual local read receipts, including old tasks with no read index."""
    calls = {}
    for message in messages:
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if message.get("role") == "assistant" and block.get("type") == "tool_use":
                calls[block.get("id")] = block
            elif message.get("role") == "user" and block.get("type") == "tool_result":
                call = calls.pop(block.get("tool_use_id"), {})
                if call.get("name") != "local_fs_read":
                    continue
                try:
                    receipt = json.loads(block.get("content", ""))
                except (TypeError, ValueError):
                    continue
                fact = source_read_fact(call, receipt)
                if fact:
                    yield fact


def resume_history(agent):
    """Read task-owned history across this assignment's continuation lineage.

    Paths and hashes describe historical reads, not current file identity,
    permission grants, source semantics, or verified completion.
    """
    logger = agent.logger
    storage, task_id = storage_for_logger(logger)
    current_run = str(logger.run_id or "")
    sources = {}
    history = {"previousResult": None, "sourceReads": [], "issues": []}
    try:
        phases = _lineage(load_task_json(logger, "task_plan.json"), agent.phase_id)
        for row, payload in _events(storage, task_id, "agent.final"):
            if (row.get("run_id") == current_run or not isinstance(payload, dict)
                    or (row.get("phase_id") or payload.get("phaseId")) not in phases):
                continue
            if isinstance(payload.get("answer"), str) and payload["answer"]:
                history["previousResult"] = {
                    "runId": row.get("run_id"),
                    "phaseId": row.get("phase_id") or payload.get("phaseId"),
                    "status": payload.get("status"), "answer": payload["answer"],
                    "eventId": row["event_id"],
                    "source": "previous_assistant_report_unverified",
                }
        for row, payload in _events(storage, task_id, "context.snapshot.saved"):
            if (row.get("run_id") == current_run or not isinstance(payload, dict)
                    or payload.get("actor") != "browser_agent"):
                continue
            phase_id = row.get("phase_id") or payload.get("phaseId")
            if phase_id and phase_id not in phases:
                continue
            path = payload.get("path")
            try:
                text = read_task_file_text(logger, path)
                if text is None:
                    history["issues"].append({"snapshotPath": path, "reason": "snapshot_unavailable"})
                    continue
                snapshot = json.loads(text)
                meta = snapshot.get("metadata") or {}
            except Exception as exc:
                history["issues"].append({"snapshotPath": path, "reason": "snapshot_read_failed",
                                          "errorType": type(exc).__name__})
                continue
            if (snapshot.get("actor") != "browser_agent" or meta.get("phase_id") not in phases
                    or (phase_id and phase_id != meta.get("phase_id"))
                    or (meta.get("run_id") and meta["run_id"] != row.get("run_id"))):
                history["issues"].append({"snapshotPath": path, "reason": "snapshot_identity_mismatch"})
                continue
            reads = (meta["source_reads"] if isinstance(meta.get("source_reads"), list)
                     else _legacy_source_reads(snapshot.get("messages") or []))
            for fact in reads:
                if not isinstance(fact, dict) or not isinstance(fact.get("path"), str):
                    continue
                item = {**{key: fact.get(key) for key in (
                            "path", "toolCallId", "lineOffset", "linesRead", "truncated",
                            "nextLineOffset", "readContentSha256")},
                        "snapshotPath": path, "runId": row.get("run_id"),
                        "phaseId": meta["phase_id"]}
                key = tuple(item.get(k) for k in ("path", "lineOffset", "linesRead", "readContentSha256"))
                sources.pop(key, None)
                sources[key] = item
    except Exception as exc:
        # A damaged historical record must be visible, not silently interpreted
        # as absence or made a new business blocker by the Harness.
        history["issues"].append({"reason": "history_read_failed", "errorType": type(exc).__name__})
    history["sourceReads"] = list(sources.values())
    return history


def restore_resume_messages(agent, messages):
    """Original/ordered inputs stay in the first user message; append result then new input."""
    if not getattr(agent, "standalone_resuming", False):
        return
    history = resume_history(agent)
    previous = history["previousResult"]
    if previous:
        messages.append({"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)})
    context = getattr(agent, "authoritative_user_context", {})
    inputs = [record for record in context.get("operatorInputs", [])
              if isinstance(record, dict) and record.get("source") == "resume_cli"
              and record.get("runId") == str(agent.logger.run_id or "")]
    projection = {"sourceReads": history["sourceReads"][-24:],
                  "omittedSourceReadCount": max(0, len(history["sourceReads"]) - 24),
                  "issues": history["issues"][:10]}
    if len(history["sourceReads"]) > 24 or len(history["issues"]) > 10:
        # Only overflow adds a resource write. Ordinary resume reuses the
        # already persisted contexts and adds no model or browser calls.
        try:
            digest = hashlib.sha256(json.dumps(history, ensure_ascii=False).encode()).hexdigest()
            logical_path = f"contexts/resume-history-{digest}.json"
            agent.logger.storage.save_resource(
                task_id=agent.logger.task_id, run_id=str(agent.logger.run_id or ""),
                resource_type="browser_resume_history", logical_path=logical_path,
                media_type="application/json", content=history)
            projection["fullHistoryPath"] = str(Path(agent.logger.task_dir).resolve() / logical_path)
        except Exception as exc:
            projection["issues"].append({"reason": "history_overflow_save_failed", "errorType": type(exc).__name__})
    current = "\n\n".join(str(record.get("text") or "") for record in inputs)
    messages.append({"role": "user", "content": (
        (current or "Continue the accepted task under the original request and ordered user amendments.")
        + "\n\n<browser_resume_sources>\n" + json.dumps(projection, ensure_ascii=False)
        + "\n</browser_resume_sources>\n"
        "The earlier assistant report and restored todo are historical claims. Current user corrections take precedence. "
        "Source entries identify historical read slices, not their current contents or permission to access them. "
        "Re-read relevant authorized sources when needed; a missing history entry does not prove source absence."
    )})
    agent._write_agent_event("browser.resume.context_restored", {
        "previousResultPresent": previous is not None, "sourceReadCount": len(history["sourceReads"]),
        "inlineSourceReadCount": len(projection["sourceReads"]), "historyIssueCount": len(history["issues"]),
        "currentInputCount": len(inputs),
    })
