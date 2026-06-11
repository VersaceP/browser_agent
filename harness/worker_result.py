"""
harness.worker_result - Stable L1/L2/L3 worker result envelopes.
"""

import json
from pathlib import Path
from typing import Any, List, Optional

from harness.utils import JsonDict, json_size_bytes, trim_large_strings


MAX_INLINE_ANSWER_CHARS = 4000
MAX_INLINE_ARTIFACTS = 50
MAX_SAMPLE_ROWS = 3
MAX_SAMPLE_FIELDS = 40


def build_worker_result_levels(
    *,
    status: str,
    status_category: str,
    validated_status: str,
    worker_id: str,
    agent_id: str,
    name: str,
    phase_id: Optional[str],
    answer: str,
    artifacts: List[str],
    artifact_validation: JsonDict,
    trace_path: str,
    trace_summary: JsonDict,
    progress_snapshot: JsonDict,
    offloaded_files: List[str],
    diagnostics: JsonDict,
    task_dir: Optional[Path],
    extraction_attempt_artifacts: Optional[List[str]] = None,
) -> JsonDict:
    """Return the stable worker handoff shape consumed by LeadAgent.

    L1 is for routing, L2 is the default semantic payload, and L3 is a set of
    references for on-demand recall. Top-level legacy fields remain available
    on the spawner result for compatibility.
    """
    answer_payload = parse_worker_answer(answer)
    extraction_artifacts = summarize_extraction_artifacts(
        artifacts,
        task_dir=task_dir,
    )
    attempt_artifacts = summarize_extraction_artifacts(
        extraction_attempt_artifacts or [],
        task_dir=task_dir,
    )
    error_count = len(trace_summary.get("errors", [])) if isinstance(trace_summary, dict) else 0
    artifact_count = len(artifacts) if isinstance(artifacts, list) else 0
    offload_count = len(offloaded_files) if isinstance(offloaded_files, list) else 0

    l1: JsonDict = {
        "status": status,
        "statusCategory": status_category,
        "validatedStatus": validated_status,
        "workerId": worker_id,
        "agentId": agent_id,
        "name": name,
        "phaseId": phase_id,
        "artifactCount": artifact_count,
        "extractionArtifactCount": len(extraction_artifacts),
        "errorCount": error_count,
        "offloadedFileCount": offload_count,
        "traceSaved": bool(trace_path),
    }
    classification = (
        artifact_validation.get("classification")
        if isinstance(artifact_validation, dict)
        and isinstance(artifact_validation.get("classification"), dict)
        else None
    )
    if isinstance(classification, dict) and classification.get("category"):
        l1["failureClassification"] = classification.get("category")

    l2: JsonDict = {
        "answer": answer_payload,
        "data": {
            "extractionArtifacts": extraction_artifacts,
            "extractionAttemptArtifacts": attempt_artifacts,
            "totalExtractedRows": sum(
                int(item.get("rowCount") or 0)
                for item in extraction_artifacts
                if isinstance(item, dict)
            ),
        },
        "evidence": {
            "artifacts": artifacts[:MAX_INLINE_ARTIFACTS],
            "tracePath": trace_path,
            "offloadedFiles": offloaded_files[:100],
        },
        "blockers": _worker_blockers(
            status=status,
            trace_summary=trace_summary,
            diagnostics=diagnostics,
            artifact_validation=artifact_validation,
        ),
        "traceSummary": _semantic_trace_summary(trace_summary),
        "progress": progress_snapshot,
        "nextSteps": _next_steps_from_answer(answer_payload),
    }

    l3: JsonDict = {
        "tracePath": trace_path,
        "artifacts": artifacts,
        "extractionAttemptArtifacts": extraction_attempt_artifacts or [],
        "offloadedFiles": offloaded_files[:100],
        "diagnostics": diagnostics,
        "artifactValidation": artifact_validation,
        "traceSummary": trace_summary,
    }

    return {
        "schemaVersion": "worker_result_levels.v1",
        "l1": trim_large_strings(l1, 8000),
        "l2": trim_large_strings(l2, 20000),
        "l3": trim_large_strings(l3, 20000),
    }


def parse_worker_answer(answer: str) -> JsonDict:
    text = str(answer or "").strip()
    payload: JsonDict = {
        "format": "text",
        "raw": text[:MAX_INLINE_ANSWER_CHARS],
        "truncated": len(text) > MAX_INLINE_ANSWER_CHARS,
    }
    if not text:
        return payload
    parsed = _try_parse_json_answer(text)
    if parsed is not None:
        payload["format"] = "json"
        payload["parsed"] = trim_large_strings(parsed, MAX_INLINE_ANSWER_CHARS)
    return payload


def summarize_extraction_artifacts(
    artifacts: List[str],
    *,
    task_dir: Optional[Path],
) -> List[JsonDict]:
    summaries: List[JsonDict] = []
    for raw_path in artifacts or []:
        path_text = str(raw_path)
        if "/artifacts/extractions/" not in path_text.replace("\\", "/"):
            continue
        summary = _summarize_extraction_artifact(path_text, task_dir=task_dir)
        summaries.append(summary)
    return summaries


def _summarize_extraction_artifact(path_text: str, *, task_dir: Optional[Path]) -> JsonDict:
    summary: JsonDict = {"savedPath": path_text, "status": "unknown"}
    try:
        path = Path(path_text).resolve(strict=False)
    except (OSError, ValueError) as exc:
        summary.update({"status": "unreadable", "error": str(exc)})
        return summary

    if task_dir is not None:
        try:
            path.relative_to(task_dir.resolve(strict=False))
        except (OSError, ValueError):
            summary.update({
                "status": "rejected",
                "error": f"path escapes task worktree {task_dir}",
            })
            return summary

    if not path.exists() or not path.is_file():
        summary.update({"status": "missing"})
        return summary

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        summary.update({"status": "unreadable", "error": str(exc)})
        return summary

    if not isinstance(payload, dict):
        summary.update({"status": "invalid", "error": "artifact is not a JSON object"})
        return summary

    rows = payload.get("rows")
    if not isinstance(rows, list):
        rows = []
    dict_rows = [row for row in rows if isinstance(row, dict)]
    summary.update({
        "status": "included",
        "name": payload.get("name"),
        "description": payload.get("description"),
        "rowCount": int(payload.get("rowCount") or len(dict_rows)),
        "schema": payload.get("schema") if isinstance(payload.get("schema"), (dict, list)) else None,
        "fields": _field_names(dict_rows),
        "sampleRows": trim_large_strings(dict_rows[:MAX_SAMPLE_ROWS], 4000),
        "byteSize": json_size_bytes(payload),
    })
    return summary


def _try_parse_json_answer(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _field_names(rows: List[JsonDict]) -> List[str]:
    fields: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            name = str(key)
            if name in seen:
                continue
            seen.add(name)
            fields.append(name)
            if len(fields) >= MAX_SAMPLE_FIELDS:
                return fields
    return fields


def _worker_blockers(
    *,
    status: str,
    trace_summary: JsonDict,
    diagnostics: JsonDict,
    artifact_validation: JsonDict,
) -> List[JsonDict]:
    blockers: List[JsonDict] = []
    errors = trace_summary.get("errors") if isinstance(trace_summary, dict) else []
    if isinstance(errors, list):
        blockers.extend({
            "type": "trace_error",
            "message": str(error)[:500],
        } for error in errors[:5])
    if isinstance(artifact_validation, dict) and artifact_validation.get("status") == "failed":
        blockers.append({
            "type": "artifact_validation_failed",
            "message": str(artifact_validation.get("error") or artifact_validation)[:500],
        })
    stall_replan = (
        trace_summary.get("stallReplanRecommended")
        if isinstance(trace_summary, dict)
        and isinstance(trace_summary.get("stallReplanRecommended"), dict)
        else None
    )
    if isinstance(stall_replan, dict) and status != "done":
        blockers.append({
            "type": "stall_replan_recommended",
            "message": str(
                stall_replan.get("next_instruction")
                or "Repeated stall signals were observed inside one worker attempt."
            )[:500],
            "signalCount": stall_replan.get("signalCount"),
            "progressInterventionCount": stall_replan.get("progressInterventionCount"),
            "loopNudgeCount": stall_replan.get("loopNudgeCount"),
        })
    if status not in {"done", "partial"}:
        blockers.append({
            "type": "terminal_status",
            "status": status,
            "diagnostics": trim_large_strings(diagnostics, 2000),
        })
    return blockers


def _semantic_trace_summary(trace_summary: JsonDict) -> JsonDict:
    if not isinstance(trace_summary, dict):
        return {}
    return {
        "steps": trace_summary.get("steps"),
        "traceEvents": trace_summary.get("traceEvents"),
        "toolCalls": trace_summary.get("toolCalls"),
        "methods": trace_summary.get("methods", {}),
        "pageIds": trace_summary.get("pageIds", []),
        "errors": trace_summary.get("errors", []),
        "progressInterventionCount": trace_summary.get("progressInterventionCount", 0),
        "loopNudgeCount": trace_summary.get("loopNudgeCount", 0),
        "stallReplanRecommended": trace_summary.get("stallReplanRecommended"),
        "latestPageStats": trace_summary.get("latestPageStats"),
        "snapshotDiffs": trace_summary.get("snapshotDiffs", []),
        "snapshotDiffCount": trace_summary.get("snapshotDiffCount", 0),
        "suspectedChallengePages": trace_summary.get("suspectedChallengePages", []),
    }


def _next_steps_from_answer(answer_payload: JsonDict) -> List[Any]:
    parsed = answer_payload.get("parsed")
    if isinstance(parsed, dict):
        for key in ("next_steps", "nextSteps"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value[:10]
    return []
