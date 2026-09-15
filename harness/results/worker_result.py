"""
harness.results.worker_result - Stable L1/L2/L3 worker result envelopes.
"""

import json
import math
from pathlib import Path
from typing import Any, List, Optional

from harness.observation.content_completeness import content_completeness_observation_facts
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
    continuation: Optional[JsonDict] = None,
    workflow_definitions: Optional[List[JsonDict]] = None,
    file_manifests: Optional[List[JsonDict]] = None,
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
    validated_extraction_paths = (
        artifact_validation.get("validExtractionArtifacts")
        if isinstance(artifact_validation, dict)
        and isinstance(artifact_validation.get("validExtractionArtifacts"), list)
        else []
    )
    validated_row_count = (
        artifact_validation.get("rowCount")
        if isinstance(artifact_validation, dict)
        else None
    )
    if not isinstance(validated_row_count, int) or isinstance(validated_row_count, bool):
        validated_row_count = sum(
            int(item.get("rowCount") or 0)
            for item in extraction_artifacts
            if isinstance(item, dict)
        )

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
            # The validator has already merged/deduplicated authoritative rows.
            # Summing per-attempt artifact summaries double counts a row when a
            # continuation rewrites the same extraction artifact.
            "totalExtractedRows": validated_row_count,
        },
        "evidence": {
            "artifacts": artifacts[:MAX_INLINE_ARTIFACTS],
            "validatedExtractionArtifacts": validated_extraction_paths[:10],
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
    if isinstance(continuation, dict):
        l1["continuationAction"] = continuation.get("action")
        l2["continuation"] = trim_large_strings(continuation, 4000)
    if workflow_definitions:
        l1["workflowDefinitionCount"] = len(workflow_definitions)
        l2["data"]["workflowDefinitions"] = trim_large_strings(
            workflow_definitions, 8000,
        )
    if file_manifests:
        l1["fileManifestCount"] = len(file_manifests)
        l2["data"]["fileManifests"] = trim_large_strings(file_manifests, 4000)

    l3: JsonDict = {
        "tracePath": trace_path,
        "artifacts": artifacts,
        "extractionAttemptArtifacts": extraction_attempt_artifacts or [],
        "offloadedFiles": offloaded_files[:100],
        "diagnostics": diagnostics,
        "artifactValidation": artifact_validation,
        "traceSummary": trace_summary,
    }
    if isinstance(continuation, dict):
        l3["continuation"] = trim_large_strings(continuation, 4000)
    if workflow_definitions:
        l3["workflowDefinitions"] = trim_large_strings(
            workflow_definitions, 12000,
        )
    if file_manifests:
        l3["fileManifests"] = trim_large_strings(file_manifests, 8000)

    return {
        "schemaVersion": "worker_result_levels.v1",
        "l1": trim_large_strings(l1, 8000),
        "l2": trim_large_strings(l2, 20000),
        "l3": trim_large_strings(l3, 20000),
    }




def _safe_text(value: Any) -> str:
    """Text for a prose field, without trusting the value to be prose.

    The bare `str()` here ran during projection CONSTRUCTION, before any size
    check, so a drifting goal could raise CPython's integer conversion error
    and take down the reduction meant to absorb exactly that.
    """

    if isinstance(value, str):
        return value
    if not value:
        return ""
    return _bounded_scalar(value, limit=4000) or ""


def _fits_budget(value: Any) -> bool:
    """Whether `value` is inside the handoff budget, and serializable at all.

    A value that cannot be measured is not "small": `json.dumps` refuses an
    integer past CPython's 4300-digit conversion limit outright, so a drifting
    field could raise here and abort the reduction whose whole job was to keep
    this function's answer true. An unmeasurable projection is over budget by
    definition, which sends it down the same path as an oversized one.
    """

    try:
        return json_size_bytes(value) <= MAX_HANDOFF_BYTES
    except (ValueError, TypeError, RecursionError, OverflowError):
        return False


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
    # Lexical artifact advisories reach the Lead here or nowhere. Everything
    # else in `artifactValidation` is a verdict the Lead can act on from the
    # status alone; an advisory only means something if the Lead can read WHICH
    # rows and fields were flagged, and a large worker result is offloaded to
    # disk with only this projection surviving in context.
    artifact_advisories = _artifact_advisories(levels)
    artifact_validation = (
        (levels.get("l3") or {}).get("artifactValidation", {})
        if isinstance(levels.get("l3"), dict)
        and isinstance((levels.get("l3") or {}).get("artifactValidation"), dict)
        else {}
    )
    artifact_failures = artifact_validation.get("failures")
    if not isinstance(artifact_failures, list):
        artifact_failures = []

    claim: JsonDict = {
        "source": "worker_claim_unverified",
        "answer": answer.get("parsed") if answer.get("format") == "json" else answer.get("raw"),
        "failureClassification": l1.get("failureClassification"),
    }
    projection: JsonDict = {
        "workerId": l1.get("workerId") or result.get("workerId"),
        "phaseId": l1.get("phaseId") or result.get("phaseId"),
        "originalGoal": _safe_text(
            original_goal or result.get("phaseObjective") or ""
        ),
        "rawReceipts": {
            "status": l1.get("status") or result.get("status"),
            # A worker execution outcome and an artifact's schema result are
            # separate facts.  Do not expose the coordinator's legacy
            # ``validatedStatus`` label here: partial workers with shape-valid
            # rows were repeatedly misread as completed objectives.
            "artifactSchemaStatus": (
                artifact_validation.get("status")
            ),
            "artifactFailures": trim_large_strings(artifact_failures[:5], 500),
            "deliveryFileCount": len(artifact_validation.get("fileArtifacts") or []),
            "priorDeliveryFileCount": len(artifact_validation.get("priorFileArtifacts") or []),
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
            "artifactAdvisories": artifact_advisories,
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
            "validatedExtractionArtifacts": (
                evidence.get("validatedExtractionArtifacts") or
                artifact_validation.get("validExtractionArtifacts") or []
            )[:5],
            "artifacts": (evidence.get("artifacts") or [])[:10],
            "offloadedFiles": (evidence.get("offloadedFiles") or [])[:10],
        },
    }
    continuation = result.get("continuation")
    if not isinstance(continuation, dict):
        continuation = l2.get("continuation")
    if isinstance(continuation, dict):
        projection["continuationDecision"] = trim_large_strings(
            continuation, MAX_HANDOFF_SECTION_CHARS,
        )
        if result.get("continuationReceiptId"):
            projection["continuationReceiptId"] = str(
                result.get("continuationReceiptId")
            )
    workflow_definitions = data.get("workflowDefinitions")
    if isinstance(workflow_definitions, list) and workflow_definitions:
        projection["rawReceipts"]["workflowDefinitions"] = [
            {
                key: item.get(key)
                for key in (
                    "definitionRef", "definitionHash", "reused",
                    "definitionBytes", "patchBytes", "executable",
                    "requiresSensitiveRebinding",
                )
                if key in item
            }
            for item in workflow_definitions
            if isinstance(item, dict)
        ][:10]
    file_manifests = data.get("fileManifests")
    if isinstance(file_manifests, list) and file_manifests:
        projection["rawReceipts"]["fileManifests"] = trim_large_strings(
            file_manifests[:10], 500,
        )
        projection["evidencePaths"]["fileManifests"] = [
            item.get("manifestPath") for item in file_manifests
            if isinstance(item, dict) and item.get("manifestPath")
        ][:5]
    if _fits_budget(projection):
        return projection

    # Keep the six-section ownership shape, but make the exceptional oversized
    # handoff fit the model-facing budget. Full data remains reachable through
    # evidence paths and the offloaded original result.
    projection["rawReceipts"]["artifacts"] = projection["rawReceipts"][
        "artifacts"
    ][:5]
    advisories = projection["rawReceipts"].get("artifactAdvisories")
    if isinstance(advisories, list):
        projection["rawReceipts"]["artifactAdvisories"] = advisories[:3]
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
    if _fits_budget(fitted):
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
    validated_paths = fitted["evidencePaths"].get("validatedExtractionArtifacts")
    if isinstance(validated_paths, list):
        fitted["evidencePaths"]["validatedExtractionArtifacts"] = validated_paths[:2]
    manifest_paths = fitted["evidencePaths"].get("fileManifests")
    if isinstance(manifest_paths, list):
        fitted["evidencePaths"]["fileManifests"] = manifest_paths[:2]
    fitted["rawReceipts"]["artifacts"] = fitted["rawReceipts"]["artifacts"][:2]
    fitted["rawReceipts"]["latestPageStats"] = {
        "offloaded": True,
        "reason": "handoff_size_budget",
    }
    fitted["originalGoal"] = trim_large_strings(fitted["originalGoal"], 200)
    reduced = trim_large_strings(fitted, 200)
    # The blanket final trim protects every drifting prose field, but route
    # guidance has its own tighter shape and a deliberate 500-character cap.
    # Restore it after that trim so a compound route hint does not lose its
    # final operational clause (often the file destination or verification).
    route_hint = projection.get("suggestedNextExperiment")
    if isinstance(route_hint, list):
        reduced["suggestedNextExperiment"] = trim_large_strings(route_hint[:1], 500)
    return _shed_observations_to_fit(reduced)


def _shed_observations_to_fit(projection: JsonDict) -> JsonDict:
    """Make MAX_HANDOFF_BYTES an enforced bound, not a declared one.

    Every earlier stage caps a section by COUNT, which bounds nothing when the
    entries themselves are large: five lexical advisories over five fields
    each produced an 11.5KB handoff against a 3.5KB budget, and the final
    return shipped it unchecked. The three observation lists are the only
    sections that grow with page and row count, so they are shed here in
    increasing order of value, each step deterministic so two identical runs
    produce identical handoffs.
    """

    receipts = projection.get("rawReceipts")
    if not isinstance(receipts, dict):
        return projection

    def _cap(key: str, limit: int) -> bool:
        value = receipts.get(key)
        if isinstance(value, list) and len(value) > limit:
            receipts[key] = value[:limit]
            return True
        return False

    def _summarize(key: str) -> bool:
        value = receipts.get(key)
        if isinstance(value, list) and value:
            receipts[key] = [{
                "omitted": len(value),
                "reason": "handoff_size_budget",
                "type": (
                    value[0].get("type") if isinstance(value[0], dict) else None
                ),
            }]
            return True
        return False

    # Advisories shed first: they are a word-list reading, and the Lead can
    # still see the flagged rows in the artifact itself.
    steps = (
        lambda: _cap("artifactAdvisories", 2),
        lambda: _cap("progressObservations", 3),
        lambda: _cap("contentCompletenessObservations", 3),
        lambda: _cap("artifactAdvisories", 1),
        lambda: _summarize("artifactAdvisories"),
        lambda: _cap("progressObservations", 1),
        lambda: _summarize("contentCompletenessObservations"),
        lambda: _summarize("progressObservations"),
    )
    for step in steps:
        if _fits_budget(projection):
            return projection
        step()
    if _fits_budget(projection):
        return projection
    return _minimal_handoff(projection)


def _minimal_handoff(projection: JsonDict) -> JsonDict:
    """The last resort, whose size does not depend on any particular field.

    Every earlier stage names the sections it shrinks, so the budget held only
    for the growth the author happened to anticipate: a long
    `advertisedMethodsNeverCalled` walked straight past all of them and shipped
    a 16KB handoff against a 3.5KB budget. This keeps a fixed set of keys and
    then shortens what is left until it fits, so the bound follows from the
    shape rather than from a list of known offenders.
    """

    receipts = projection.get("rawReceipts")
    receipts = receipts if isinstance(receipts, dict) else {}
    evidence = projection.get("evidencePaths")
    evidence = evidence if isinstance(evidence, dict) else {}
    advisories = receipts.get("artifactAdvisories")
    artifacts = receipts.get("artifacts")

    # Scalarized, not merely selected. A fixed key set is not a fixed size:
    # these positions hold identifiers by contract, but a drifting producer
    # that puts a dict in one of them walks past `trim_large_strings`, which
    # shortens strings and cannot bound a mapping's key count.
    minimal: JsonDict = {
        "workerId": _bounded_scalar(projection.get("workerId")),
        "phaseId": _bounded_scalar(projection.get("phaseId")),
        "originalGoal": str(projection.get("originalGoal") or "")[:200],
        "rawReceipts": {
            "status": _bounded_scalar(receipts.get("status")),
            "artifactSchemaStatus": _bounded_scalar(
                receipts.get("artifactSchemaStatus")
            ),
            "artifactCount": len(artifacts) if isinstance(artifacts, list) else 0,
            "totalExtractedRows": _bounded_int(receipts.get("totalExtractedRows")),
            "artifactAdvisoryCount": (
                len(advisories) if isinstance(advisories, list) else 0
            ),
            "artifactFailureCount": (
                len(receipts.get("artifactFailures"))
                if isinstance(receipts.get("artifactFailures"), list) else 0
            ),
            "deliveryFileCount": _bounded_int(receipts.get("deliveryFileCount")),
            "priorDeliveryFileCount": _bounded_int(
                receipts.get("priorDeliveryFileCount")
            ),
            "reduced": "handoff_size_budget",
        },
        "workerClaims": {
            "source": "worker_claim_unverified",
            "reduced": "handoff_size_budget",
        },
        "unresolvedCounterevidence": [],
        # Keep one bounded worker-authored route hint. This is advisory rather
        # than a mechanical decision, but it is the useful part of a sibling
        # handoff (for example, a canonical URL or shadow-root route) and avoids
        # forcing Lead to reread the full result merely to restate it.
        "suggestedNextExperiment": trim_large_strings(
            (projection.get("suggestedNextExperiment") or [])[:1]
            if isinstance(projection.get("suggestedNextExperiment"), list)
            else projection.get("suggestedNextExperiment"),
            500,
        ),
        "evidencePaths": {
            "tracePath": evidence.get("tracePath"),
            "validatedExtractionArtifacts": (
                evidence.get("validatedExtractionArtifacts") or []
            )[:2],
            "fileManifests": (evidence.get("fileManifests") or [])[:2],
            "artifacts": (evidence.get("artifacts") or [])[:2],
            "offloadedFiles": (evidence.get("offloadedFiles") or [])[:2],
        },
    }
    # Paths are the one part that can still be arbitrarily long.
    for limit in (200, 100, 50):
        if _fits_budget(minimal):
            return minimal
        minimal = trim_large_strings(minimal, limit)
    if not _fits_budget(minimal):
        minimal["evidencePaths"] = {"reduced": "handoff_size_budget"}
    if _fits_budget(minimal):
        return minimal
    # Nothing above depends on a value the caller supplied, so this always
    # fits. Reached only if a producer breaks the scalar contract in a way the
    # bounds above did not anticipate - the Lead gets a truthful "unreadable"
    # rather than a handoff that blows its own budget.
    return {
        "workerId": _bounded_scalar(minimal.get("workerId")),
        "phaseId": _bounded_scalar(minimal.get("phaseId")),
        "originalGoal": "",
        "rawReceipts": {"reduced": "handoff_size_budget_emergency"},
        "workerClaims": {"source": "worker_claim_unverified"},
        "unresolvedCounterevidence": [],
        "suggestedNextExperiment": [],
        "evidencePaths": {"reduced": "handoff_size_budget_emergency"},
    }


def _bounded_scalar(value: Any, limit: int = 120) -> Optional[str]:
    """An identifier position, forced to an identifier-sized string."""

    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        # `str()` on a large enough int raises the same conversion-limit error
        # this function is meant to absorb, so magnitude is checked first.
        return str(value)[:limit] if abs(value) < 10 ** 30 else "<int>"
    if isinstance(value, float):
        return str(value)[:limit] if math.isfinite(value) else "<float>"
    if isinstance(value, str):
        return value[:limit]
    return f"<{type(value).__name__}>"


# A row count. Anything past this is drift, not a tally, and a bignum can be
# arbitrarily many digits of JSON on its own.
MAX_SAFE_COUNT = 1_000_000_000


def _bounded_int(value: Any) -> Optional[int]:
    """A count position, forced to a plausible count.

    This function exists to absorb contract drift, so it may not itself throw
    on drifting input: `int(float("inf"))` raises OverflowError and
    `int(float("nan"))` raises ValueError, and either one aborted the very
    fallback that was supposed to keep an oversized handoff inside its budget.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_SAFE_COUNT else None
    if isinstance(value, float) and math.isfinite(value):
        converted = int(value)
        return converted if 0 <= converted <= MAX_SAFE_COUNT else None
    return None


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
        "targetRecoveryHistory": trace_summary.get("targetRecoveryHistory", {}),
        "stepExtension": trace_summary.get("stepExtension"),
        "suspectedChallengePages": trace_summary.get("suspectedChallengePages", []),
        "contentCompletenessPages": trace_summary.get("contentCompletenessPages", []),
    }



def _artifact_advisories(levels: JsonDict) -> List[JsonDict]:
    """Bounded projection of artifact warnings that are observations, not verdicts.

    Only entries that mark themselves ``severity: advisory`` travel. A warning
    without that mark is either already reflected in the artifact status or is
    a verdict in its own right, and neither needs re-stating to the Lead as
    something to judge.
    """

    l3 = levels.get("l3") if isinstance(levels.get("l3"), dict) else {}
    validation = (
        l3.get("artifactValidation")
        if isinstance(l3.get("artifactValidation"), dict)
        else {}
    )
    warnings = validation.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []
    out: List[JsonDict] = [
        trim_large_strings({**item, "source": "worker_judgment_or_contract_observation_not_proof"}, 800)
        for item in (validation.get("semanticObservations") or [])[:5]
        if isinstance(item, dict)
    ]
    for item in warnings:
        if not isinstance(item, dict) or item.get("severity") != "advisory":
            continue
        raw_fields = item.get("fields")
        fields = []
        for spec in (raw_fields if isinstance(raw_fields, list) else [])[:3]:
            if not isinstance(spec, dict):
                continue
            # The matched regex is the observer's own internals: it is bulky,
            # it is not something the Lead can act on, and printing a pattern
            # invites reading the word list as the rule.
            fields.append({
                "field": spec.get("field"),
                "value": str(spec.get("value") or "")[:80],
            })
        out.append({
            "type": item.get("type"),
            "source": "lexical_observer_not_verdict",
            "row": item.get("row"),
            "fields": fields,
            "message": item.get("message"),
        })
        if len(out) >= 5:
            break
    return out

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
