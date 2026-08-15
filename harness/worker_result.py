"""
harness.worker_result - Stable L1/L2/L3 worker result envelopes.
"""

import json
from pathlib import Path
from typing import Any, List, Optional

from harness.content_completeness import content_completeness_observation_facts
from harness.utils import (
    JsonDict,
    json_size_bytes,
    read_task_file_text,
    trim_large_strings,
)


class _ArtifactReaderLogger:
    """Last-resort shim for callers that only have a task directory.

    Resolves to the file backend, which is the right answer when there is no
    logger to inherit a connection from - but a caller that HAS one must pass
    it: this shim cannot see the database, and a summary built through it
    reports every db-mode artifact as missing while the artifact gate, reading
    the same path with a real logger, passes it.
    """

    def __init__(self, task_dir) -> None:
        self.task_dir = task_dir
        self.task_id = task_dir.name


MAX_INLINE_ANSWER_CHARS = 4000
MAX_INLINE_ARTIFACTS = 50
MAX_SAMPLE_ROWS = 3
MAX_SAMPLE_FIELDS = 40
# Leave headroom for coordinator-added arithmetic facts (attempt count/delta
# and the strategy-attempt ledger path) while keeping the final handoff <=4KB.
MAX_HANDOFF_BYTES = 3500
MAX_HANDOFF_SECTION_CHARS = 900


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
    row_ledger: Optional[List[JsonDict]] = None,
    logger: Optional[Any] = None,
) -> JsonDict:
    """Return the stable worker handoff shape consumed by LeadAgent.

    L1 is for routing, L2 is the default semantic payload, and L3 is a set of
    references for on-demand recall. Top-level legacy fields remain available
    on the spawner result for compatibility.
    """
    answer_payload = parse_worker_answer(answer)
    # The summary the Lead reads must agree with the artifact gate. Built from
    # a task_dir alone it cannot see the database, and every db-mode artifact
    # comes back "missing" with rowCount 0 while the gate passes the same file.
    extraction_artifacts = summarize_extraction_artifacts(
        artifacts,
        task_dir=task_dir,
        logger=logger,
    )
    attempt_artifacts = summarize_extraction_artifacts(
        extraction_attempt_artifacts or [],
        task_dir=task_dir,
        logger=logger,
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
    if row_ledger:
        # Per-row outcome and cause, derived from this worker's own receipts.
        # A row missing because the budget ran out and a row blocked by an
        # overlay look identical in prose; here they never do.
        l2["rowLedger"] = row_ledger

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


def build_worker_handoff_projection(
    result: Any,
    *,
    original_goal: str = "",
) -> Optional[JsonDict]:
    """Build the single model-facing semantic handoff for a worker result.

    The projection preserves provenance: receipts remain receipts, model prose
    remains a claim, and unresolved evidence is never silently promoted to
    completion.  Large row payloads stay behind evidence paths.
    """
    if not isinstance(result, dict):
        return None
    levels = result.get("resultLevels")
    if not isinstance(levels, dict):
        return None
    l1 = levels.get("l1") if isinstance(levels.get("l1"), dict) else {}
    l2 = levels.get("l2") if isinstance(levels.get("l2"), dict) else {}
    data = l2.get("data") if isinstance(l2.get("data"), dict) else {}
    evidence = l2.get("evidence") if isinstance(l2.get("evidence"), dict) else {}
    answer = l2.get("answer") if isinstance(l2.get("answer"), dict) else {}
    artifacts = data.get("extractionArtifacts")
    artifacts = artifacts if isinstance(artifacts, list) else []
    attempts = data.get("extractionAttemptArtifacts")
    attempts = attempts if isinstance(attempts, list) else []
    blockers = l2.get("blockers") if isinstance(l2.get("blockers"), list) else []
    next_steps = l2.get("nextSteps") if isinstance(l2.get("nextSteps"), list) else []
    trace_summary = (
        l2.get("traceSummary") if isinstance(l2.get("traceSummary"), dict) else {}
    )
    completeness_observations = _content_completeness_observations(trace_summary)
    if completeness_observations:
        unresolved_pages = [
            item for item in completeness_observations
            if item.get("missingRegions")
            or any(
                str(value or "") not in {"", "target_reached", "explicitly_exhausted"}
                for value in (item.get("regionCollectionStates") or {}).values()
            )
        ]
        if unresolved_pages:
            blockers = [
                *blockers,
                {
                    "type": "content_completeness_observations",
                    "source": "tracker_observation_not_verdict",
                    "observations": unresolved_pages,
                },
            ]
    claim: JsonDict = {
        "source": "worker_claim_unverified",
        "answer": answer.get("parsed") if answer.get("format") == "json" else answer.get("raw"),
        "failureClassification": l1.get("failureClassification"),
    }
    projection: JsonDict = {
        "workerId": l1.get("workerId") or result.get("workerId"),
        "phaseId": l1.get("phaseId") or result.get("phaseId"),
        "originalGoal": str(original_goal or result.get("phaseObjective") or ""),
        "rawReceipts": {
            "status": l1.get("status") or result.get("status"),
            # A worker execution outcome and an artifact's schema result are
            # separate facts.  Do not expose the coordinator's legacy
            # ``validatedStatus`` label here: partial workers with shape-valid
            # rows were repeatedly misread as completed objectives.
            "artifactSchemaStatus": (
                (levels.get("l3") or {}).get("artifactValidation", {}).get("status")
                if isinstance(levels.get("l3"), dict)
                and isinstance((levels.get("l3") or {}).get("artifactValidation"), dict)
                else None
            ),
            "artifacts": [
                {
                    "savedPath": item.get("savedPath"),
                    "rowCount": item.get("rowCount"),
                    "schemaStatus": item.get("status"),
                }
                for item in [*artifacts, *attempts]
                if isinstance(item, dict)
            ][:10],
            "totalExtractedRows": data.get("totalExtractedRows"),
            "methods": trace_summary.get("methods", {}),
            "advertisedMethodsNeverCalled": trace_summary.get(
                "advertisedMethodsNeverCalled", []
            ),
            "progressObservations": trace_summary.get("progressObservations", []),
            "progressObservationCount": trace_summary.get(
                "progressObservationCount", 0
            ),
            "latestPageStats": trace_summary.get("latestPageStats"),
            "contentCompletenessObservations": completeness_observations,
        },
        "workerClaims": trim_large_strings(claim, MAX_HANDOFF_SECTION_CHARS),
        "unresolvedCounterevidence": trim_large_strings(
            blockers, MAX_HANDOFF_SECTION_CHARS
        ),
        "suggestedNextExperiment": trim_large_strings(
            next_steps, MAX_HANDOFF_SECTION_CHARS
        ),
        "evidencePaths": {
            "tracePath": evidence.get("tracePath"),
            "artifacts": (evidence.get("artifacts") or [])[:10],
            "offloadedFiles": (evidence.get("offloadedFiles") or [])[:10],
        },
    }
    if json_size_bytes(projection) <= MAX_HANDOFF_BYTES:
        return projection

    # Keep the six-section ownership shape, but make the exceptional oversized
    # handoff fit the model-facing budget. Full data remains reachable through
    # evidence paths and the offloaded original result.
    projection["rawReceipts"]["artifacts"] = projection["rawReceipts"][
        "artifacts"
    ][:5]
    methods = projection["rawReceipts"].get("methods")
    if isinstance(methods, dict):
        projection["rawReceipts"]["methods"] = dict(list(methods.items())[:10])
    projection["workerClaims"] = trim_large_strings(
        projection["workerClaims"], 400
    )
    unresolved = projection["unresolvedCounterevidence"]
    if isinstance(unresolved, list):
        unresolved = unresolved[:5]
    projection["unresolvedCounterevidence"] = trim_large_strings(unresolved, 400)
    experiments = projection["suggestedNextExperiment"]
    if isinstance(experiments, list):
        experiments = experiments[:5]
    projection["suggestedNextExperiment"] = trim_large_strings(experiments, 400)
    projection["evidencePaths"]["artifacts"] = projection["evidencePaths"][
        "artifacts"
    ][:5]
    projection["evidencePaths"]["offloadedFiles"] = projection[
        "evidencePaths"
    ]["offloadedFiles"][:5]
    fitted = trim_large_strings(projection, 400)
    if json_size_bytes(fitted) <= MAX_HANDOFF_BYTES:
        return fitted
    unresolved = fitted["unresolvedCounterevidence"]
    experiments = fitted["suggestedNextExperiment"]
    fitted["unresolvedCounterevidence"] = trim_large_strings(
        unresolved[:2] if isinstance(unresolved, list) else unresolved,
        200,
    )
    fitted["suggestedNextExperiment"] = trim_large_strings(
        experiments[:2] if isinstance(experiments, list) else experiments,
        200,
    )
    fitted["evidencePaths"]["artifacts"] = fitted["evidencePaths"]["artifacts"][:2]
    fitted["evidencePaths"]["offloadedFiles"] = fitted["evidencePaths"][
        "offloadedFiles"
    ][:2]
    fitted["rawReceipts"]["artifacts"] = fitted["rawReceipts"]["artifacts"][:2]
    fitted["rawReceipts"]["latestPageStats"] = {
        "offloaded": True,
        "reason": "handoff_size_budget",
    }
    fitted["originalGoal"] = trim_large_strings(fitted["originalGoal"], 200)
    return trim_large_strings(fitted, 200)


def worker_handoff_projections(value: Any) -> List[JsonDict]:
    """Project direct results and wait_browser_agents completed entries."""
    if not isinstance(value, dict):
        return []
    candidates = [value]
    for key in ("completed", "results"):
        nested = value.get(key)
        if isinstance(nested, list):
            candidates.extend(item for item in nested if isinstance(item, dict))
    projections: List[JsonDict] = []
    for candidate in candidates:
        projection = build_worker_handoff_projection(candidate)
        if projection is not None:
            projections.append(projection)
    return projections


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
    logger: Optional[Any] = None,
) -> List[JsonDict]:
    summaries: List[JsonDict] = []
    for raw_path in artifacts or []:
        path_text = str(raw_path)
        if "/artifacts/extractions/" not in path_text.replace("\\", "/"):
            continue
        summary = _summarize_extraction_artifact(
            path_text, task_dir=task_dir, logger=logger
        )
        summaries.append(summary)
    return summaries


def _summarize_extraction_artifact(
    path_text: str,
    *,
    task_dir: Optional[Path],
    logger: Optional[Any] = None,
) -> JsonDict:
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

    # The summary the Lead reads must agree with the artifact gate: both go
    # through the same reader so a db-mode artifact is never reported missing.
    reader = logger if logger is not None else (
        _ArtifactReaderLogger(task_dir) if task_dir else None
    )
    text = read_task_file_text(reader, str(path)) if reader is not None else None
    if text is None:
        if not path.exists() or not path.is_file():
            summary.update({"status": "missing"})
            return summary
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            summary.update({"status": "unreadable", "error": str(exc)})
            return summary
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
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
        "advertisedMethodsNeverCalled": trace_summary.get(
            "advertisedMethodsNeverCalled", []
        ),
        "pageIds": trace_summary.get("pageIds", []),
        "errors": trace_summary.get("errors", []),
        "progressObservations": trace_summary.get("progressObservations", []),
        "progressObservationCount": trace_summary.get("progressObservationCount", 0),
        "loopNudgeCount": trace_summary.get("loopNudgeCount", 0),
        "latestPageStats": trace_summary.get("latestPageStats"),
        "snapshotDiffs": trace_summary.get("snapshotDiffs", []),
        "snapshotDiffCount": trace_summary.get("snapshotDiffCount", 0),
        "suspectedChallengePages": trace_summary.get("suspectedChallengePages", []),
        "contentCompletenessPages": trace_summary.get("contentCompletenessPages", []),
    }


def _content_completeness_observations(trace_summary: JsonDict) -> List[JsonDict]:
    """Project tracker output as attributed facts, never as a verdict.

    The tracker may internally retain historical decision labels for telemetry
    compatibility. They are intentionally omitted here. The model receives
    observable markers, missing regions, counts, collection/action receipts and
    evidence paths, then performs the semantic interpretation itself.
    """
    pages = trace_summary.get("contentCompletenessPages")
    if not isinstance(pages, list):
        return []
    observations: List[JsonDict] = []
    for page in pages[:10]:
        facts = content_completeness_observation_facts(page)
        if facts:
            observations.append(trim_large_strings(facts, 500))
    return observations


def _next_steps_from_answer(answer_payload: JsonDict) -> List[Any]:
    parsed = answer_payload.get("parsed")
    if isinstance(parsed, dict):
        for key in ("next_steps", "nextSteps"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value[:10]
    return []
