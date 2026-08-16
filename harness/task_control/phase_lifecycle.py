"""
harness.task_control.phase_lifecycle - Phase running/result transitions, resume state and pacing.
"""

from __future__ import annotations

import json
import copy
import csv
import hashlib
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from harness.constants import WORKER_STATUS_DONE
from harness.constants import WORKER_STATUS_PARTIAL
from harness.evidence.artifact_evidence import FILE_VALIDATOR_TYPES
from harness.pacing import jittered_interval
from harness.pacing import merge_pacing
from harness.pacing import parse_utc_timestamp
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import read_task_file_text
from harness.utils import task_file_exists
from harness.utils import trim_large_strings

def _tc():
    import harness.task_control as tc

    return tc

def mark_phase_running(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
    worker_name: str,
) -> None:
    if not phase_id:
        return
    state = _tc().load_task_state(logger)
    phase_state = _tc()._phase_state(state, phase_id)
    if phase_state is None:
        return
    phase_state["status"] = "running"
    phase_state.setdefault("attempts", []).append({
        "workerId": worker_id,
        "name": worker_name,
        "started_at": _tc().utc_now_iso(),
        "status": "running",
    })
    state["current_phase"] = phase_id
    _tc().write_task_state(logger, state)

def cancel_phase_running_reservation(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
) -> None:
    if not phase_id:
        return
    state = _tc().load_task_state(logger)
    phase_state = _tc()._phase_state(state, phase_id)
    if phase_state is None:
        return
    attempts = phase_state.get("attempts")
    if isinstance(attempts, list):
        phase_state["attempts"] = [
            item for item in attempts
            if not (
                isinstance(item, dict)
                and str(item.get("workerId") or "") == str(worker_id)
                and str(item.get("status") or "") == "running"
            )
        ]
    if str(phase_state.get("status") or "") == "running":
        phase_state["status"] = "pending"
    _tc().write_task_state(logger, state)

def mark_phase_result(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
    validation: Optional[JsonDict],
    result_status: str,
    attempt_digest: Optional[JsonDict] = None,
    phase: Optional[JsonDict] = None,
    worker_contract: Optional[JsonDict] = None,
) -> None:
    if not phase_id:
        return
    state = _tc().load_task_state(logger)
    phase_state = _tc()._phase_state(state, phase_id)
    if phase_state is None:
        return

    attempts = phase_state.setdefault("attempts", [])
    attempt = None
    for item in reversed(attempts):
        if item.get("workerId") == worker_id:
            attempt = item
            break
    if attempt is None:
        attempt = {"workerId": worker_id}
        attempts.append(attempt)
    attempt["finished_at"] = _tc().utc_now_iso()
    attempt["status"] = result_status
    if validation:
        attempt["validation"] = trim_large_strings(validation, 2000)
    if attempt_digest:
        attempt["attemptDigest"] = trim_large_strings(
            _tc()._strip_volatile_handles(attempt_digest),
            4000,
        )

    if result_status in _tc().RECOVERABLE_ROUTING_PHASE_STATUSES:
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "recoverable_routing_failure",
            "status": result_status,
            "message": (
                "The worker's unnamed fleet assignment was lost. Retry this"
                " phase with a fresh coordinator assignment; retries remain"
                " bounded by the phase max_attempts budget."
            ),
        }]
        phase_state["last_failure_classification"] = (
            validation.get("classification")
            if isinstance(validation, dict)
            and isinstance(validation.get("classification"), dict)
            else None
        )
        # This is an orchestration/inventory failure, not evidence that the
        # objective itself is infeasible. Preserve the reason and phase retry
        # budget, but do not consume the cross-replan objective budget.
        _tc().write_task_state(logger, state)
        return

    if result_status == "cancelled":
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "worker_cancelled",
            "status": result_status,
            "message": (
                "The worker was cancelled. A retry remains bounded by the"
                " phase max_attempts budget, but cancellation is not evidence"
                " that the objective itself is infeasible."
            ),
        }]
        phase_state["last_failure_classification"] = (
            validation.get("classification")
            if isinstance(validation, dict)
            and isinstance(validation.get("classification"), dict)
            else None
        )
        _tc().write_task_state(logger, state)
        return

    if result_status in {
        "blocked_by_challenge",
        "hitl_required",
        "hitl_timeout",
        "page_settled_after_hitl",
        "stale_pause_deadlock",
        "session_fleet_lost",
    }:
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "challenge_blocker",
            "status": result_status,
            "message": (
                "Worker reported a challenge/HITL/session blocker; do not"
                " retry the same browser/session binding without user action"
                " or a deliberate auth recovery pivot."
            ),
        }]
        _tc().write_task_state(logger, state)
        return

    if (
        result_status == WORKER_STATUS_DONE
        and validation
        and validation.get("status") == "done"
    ):
        phase_state["status"] = "validated_done"
        artifacts = validation.get("artifacts") or []
        validated_artifacts = list(phase_state.get("validated_artifacts") or [])
        _tc()._append_unique(validated_artifacts, artifacts)
        phase_state["validated_artifacts"] = validated_artifacts
        phase_state["last_failure"] = None
        phase_state["last_failure_classification"] = None
        _tc()._append_unique(state.setdefault("artifacts", []), artifacts)
        artifact_digests = state.setdefault("artifact_digests", {})
        if not isinstance(artifact_digests, dict):
            artifact_digests = {}
            state["artifact_digests"] = artifact_digests
        for artifact in artifacts:
            path = _resume_artifact_path(logger, artifact)
            digest = _artifact_sha256(path, logger)
            if digest:
                artifact_digests[str(path)] = digest
        _tc()._record_objective_attempt(
            state, phase, str(phase_id),
            succeeded=True, worker_contract=worker_contract,
        )
    else:
        # A shape-valid artifact proves only that the persisted rows satisfy
        # their declared schema.  It does not override the worker's raw
        # negative outcome.  In particular, partial artifacts stay attached
        # to this attempt for continuation, but must not enter the global
        # validated-artifact ledger consumed by completion receipts and plan
        # review.
        if validation and validation.get("status") == "done":
            phase_state["status"] = result_status or "unknown"
            phase_state["last_failure"] = [{
                "type": "worker_not_done",
                "status": result_status or "unknown",
                "message": (
                    "Artifact schema validation passed, but the worker did not"
                    " report raw status=done. Persisted rows remain attempt"
                    " evidence only; continue or reassess this phase."
                ),
            }]
            phase_state["last_failure_classification"] = (
                validation.get("classification")
                if isinstance(validation.get("classification"), dict)
                else None
            )
            if result_status != WORKER_STATUS_PARTIAL:
                # Objective counters are observation-only after retirement of
                # objective_exhausted. A failed worker whose rows happen to be
                # schema-valid is still a failed attempt; a partial worker is
                # ongoing continuation evidence and is deliberately excluded.
                _tc()._record_objective_attempt(
                    state, phase, str(phase_id),
                    succeeded=False, worker_contract=worker_contract,
                )
            _tc().write_task_state(logger, state)
            return
        classification = (
            validation.get("classification")
            if isinstance(validation, dict)
            and isinstance(validation.get("classification"), dict)
            else {}
        )
        semantic_category = str(classification.get("category") or "").strip()
        if semantic_category in _tc().SEMANTIC_TERMINAL_CLASSIFICATIONS:
            phase_state["status"] = semantic_category
            phase_state[f"{semantic_category}_at"] = _tc().utc_now_iso()
            phase_state["last_failure_classification"] = classification
            phase_state["last_failure"] = [{
                "type": semantic_category,
                "classification": classification,
                "message": (
                    classification.get("hint")
                    or f"Worker classified the phase as {semantic_category}."
                ),
            }]
            _tc().write_task_state(logger, state)
            return
        phase_state["status"] = "validation_failed" if validation else result_status
        phase_state["last_failure"] = (
            validation.get("failures") if isinstance(validation, dict) else None
        )
        phase_state["last_failure_classification"] = (
            validation.get("classification") if isinstance(validation, dict) else None
        )
        # Objective-level failure accounting survives replans (per-phase
        # attempts do not: a fresh phase id resets them). Challenge/HITL and
        # semantic-terminal outcomes returned earlier and are deliberately
        # not counted — they are not evidence the objective is unreachable.
        _tc()._record_objective_attempt(
            state, phase, str(phase_id),
            succeeded=False, worker_contract=worker_contract,
        )

    _tc().write_task_state(logger, state)

def phase_prior_artifact_paths(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    exclude_worker_id: Optional[str] = None,
) -> List[str]:
    if not phase_id:
        return []
    state = _tc().load_task_state(logger)
    phase_state = _tc()._phase_state(state, str(phase_id))
    if phase_state is None:
        return []
    attempts = phase_state.get("attempts")
    if not isinstance(attempts, list):
        return []
    paths: List[Any] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        if exclude_worker_id and str(attempt.get("workerId") or "") == str(exclude_worker_id):
            continue
        validation = attempt.get("validation")
        if isinstance(validation, dict):
            for key in (
                "artifacts",
                "allExtractionArtifacts",
                "validExtractionArtifacts",
                "attemptExtractionArtifacts",
                "priorExtractionArtifacts",
            ):
                value = validation.get(key)
                if isinstance(value, list):
                    paths.extend(value)
        digest = attempt.get("attemptDigest")
        if isinstance(digest, dict) and isinstance(digest.get("artifactPaths"), list):
            paths.extend(digest.get("artifactPaths") or [])
    return [
        path for path in _tc()._unique_paths(paths)
        if "/artifacts/extractions/" in str(path)
    ]

def _normalized_depends_on(raw: Any) -> Optional[List[str]]:
    """Plan-normalization twin of _phase_dependency_ids: keep None (omitted →
    implicit serial) distinct from [] (explicitly independent); coerce a bare
    string to a one-element list; anything malformed degrades to None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return None

def _phase_dependency_ids(phase: JsonDict) -> Optional[List[str]]:
    """None ⇒ depends_on OMITTED (conservative implicit: all prior phases in
    plan order). [] ⇒ EXPLICITLY independent — startable immediately, in
    parallel with anything. Task 2ed5a466: the old falsy check collapsed [] into
    the implicit-serial default, leaving the planner no syntax at all to declare
    independence, and three logically-parallel detail phases ran serially."""
    raw = phase.get("depends_on")
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        values: List[Any] = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        return None  # malformed → conservative implicit ordering
    return [
        str(item).strip()
        for item in values
        if str(item).strip()
    ]

def _resume_plan_phase_map(plan: Optional[JsonDict]) -> JsonDict:
    phases = plan.get("phases") if isinstance(plan, dict) else None
    return {
        str(phase.get("id") or ""): phase
        for phase in phases if isinstance(phase, dict) and str(phase.get("id") or "")
    } if isinstance(phases, list) else {}

def _resume_downstream_map(plan: Optional[JsonDict]) -> Dict[str, Set[str]]:
    phases = plan.get("phases") if isinstance(plan, dict) else None
    downstream: Dict[str, Set[str]] = {}
    prior_ids: List[str] = []
    for phase in phases if isinstance(phases, list) else []:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        if not phase_id:
            continue
        dependencies = _phase_dependency_ids(phase)
        if dependencies is None:
            dependencies = list(prior_ids)
        for dependency in dependencies:
            downstream.setdefault(dependency, set()).add(phase_id)
        prior_ids.append(phase_id)
    return downstream

def _resume_artifact_path(logger: RunLogger, value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        path = logger.task_dir / path
    return path.resolve(strict=False)

def _resume_artifact_is_readable(logger: RunLogger, value: Any) -> bool:
    if not str(value or "").strip():
        return False
    path = _resume_artifact_path(logger, value)
    if path.is_file():
        try:
            with path.open("rb") as artifact_file:
                artifact_file.read(1)
        except OSError:
            return False
        return True
    return task_file_exists(logger, str(path))

def _artifact_sha256(path: Path, logger: Optional[RunLogger] = None) -> str:
    """Digest an artifact's bytes from whichever backend holds them."""

    if logger is not None and not path.is_file():
        text = read_task_file_text(logger, str(path))
        if text is None:
            return ""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    digest = hashlib.sha256()
    try:
        with path.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()

def _legacy_artifact_syntax_error(path: Path, logger: Optional[RunLogger] = None) -> str:
    """Return a terse integrity error for legacy artifacts without a digest."""

    suffix = path.suffix.lower()
    text: Optional[str] = None
    if logger is not None and not path.is_file():
        text = read_task_file_text(logger, str(path))
        if text is None:
            return "missing_or_unreadable"
    try:
        if suffix == ".json":
            json.loads(text if text is not None else path.read_text(encoding="utf-8"))
        elif suffix == ".jsonl":
            saw_record = False
            with path.open("r", encoding="utf-8") as artifact_file:
                for line_number, line in enumerate(artifact_file, start=1):
                    if not line.strip():
                        continue
                    saw_record = True
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        return f"invalid_jsonl_line:{line_number}:{exc.msg}"
            if not saw_record:
                return "empty_jsonl"
        elif suffix == ".csv":
            with path.open("r", encoding="utf-8", newline="") as artifact_file:
                rows = list(csv.reader(artifact_file, strict=True))
            if not rows or not rows[0]:
                return "empty_csv"
            width = len(rows[0])
            if any(len(row) != width for row in rows[1:]):
                return "inconsistent_csv_columns"
    except (OSError, UnicodeError, json.JSONDecodeError, csv.Error) as exc:
        return f"invalid_{suffix.lstrip('.') or 'artifact'}:{exc}"
    return ""

def _artifact_recorded_digest(
    logger: RunLogger,
    artifact_digests: JsonDict,
    artifact: Any,
) -> str:
    resolved = str(_resume_artifact_path(logger, artifact))
    return str(
        artifact_digests.get(resolved)
        or artifact_digests.get(str(artifact or ""))
        or ""
    ).strip().lower()

def _resume_artifact_integrity_error(
    logger: RunLogger,
    artifact_digests: JsonDict,
    artifact: Any,
) -> str:
    path = _resume_artifact_path(logger, artifact)
    if not _resume_artifact_is_readable(logger, artifact):
        return "missing_or_unreadable"
    expected_digest = _artifact_recorded_digest(logger, artifact_digests, artifact)
    if expected_digest:
        actual_digest = _artifact_sha256(path, logger)
        if not actual_digest:
            return "unreadable"
        if actual_digest != expected_digest:
            return "sha256_mismatch"
        return ""
    return _legacy_artifact_syntax_error(path, logger)

def _legacy_extraction_validation_error(
    logger: RunLogger,
    phase: Optional[JsonDict],
    artifacts: List[Any],
    artifact_digests: JsonDict,
) -> str:
    """Re-run existing validators for digest-less extraction artifacts."""

    if not isinstance(phase, dict):
        return ""
    all_extractions = [
        str(artifact)
        for artifact in artifacts
        if "/artifacts/extractions/" in str(_resume_artifact_path(logger, artifact))
    ]
    if not any(
        not _artifact_recorded_digest(logger, artifact_digests, artifact)
        for artifact in all_extractions
    ):
        return ""
    try:
        contract = _tc().phase_contract(phase)
        # File/download receipts are independent runtime evidence. Resume is
        # re-validating the extraction payload here, so do not manufacture a
        # file-evidence failure merely because old worker handles are gone.
        contract["validators"] = [
            validator
            for validator in contract.get("validators") or []
            if isinstance(validator, dict)
            and str(validator.get("type") or "") not in FILE_VALIDATOR_TYPES
        ]
        validation = _tc().validate_worker_artifacts(
            contract=contract,
            artifacts=all_extractions,
            task_dir=logger.task_dir,
            logger=logger,
        )
    except Exception as exc:
        return f"validator_error:{type(exc).__name__}:{exc}"
    if str(validation.get("status") or "") != "done":
        failures = validation.get("failures")
        first_type = ""
        if isinstance(failures, list) and failures and isinstance(failures[0], dict):
            first_type = str(failures[0].get("type") or "")
        return f"validator_failed:{first_type or 'unknown'}"
    return ""

def prepare_resume_state(
    logger: RunLogger,
    *,
    old_plan: JsonDict,
    new_plan: Optional[JsonDict] = None,
    instruction: str = "",
    persist: bool = True,
    record_audit: bool = True,
) -> JsonDict:
    """Reconcile interrupted state and retire mechanically stale evidence.

    The returned ``state`` is a deep-copied, reconciled ``preserve_from`` value.
    Replan callers use ``persist=False, record_audit=False`` and pass that value
    directly to :func:`initialize_task_state`, avoiding an inconsistent
    intermediate write.  Resume bootstrap uses the defaults and persists once.
    """

    state = copy.deepcopy(_tc().load_task_state(logger))
    if not state:
        raise ValueError("task_state.json is missing or unreadable")
    phases_state = state.get("phases")
    if not isinstance(phases_state, dict):
        raise ValueError("task_state.json has no phases object")

    interrupted_attempts: List[JsonDict] = []
    reset_running_phases: List[str] = []
    for phase_id, raw_phase_state in phases_state.items():
        if not isinstance(raw_phase_state, dict):
            continue
        attempts = raw_phase_state.get("attempts")
        for attempt in attempts if isinstance(attempts, list) else []:
            if not isinstance(attempt, dict) or attempt.get("status") != "running":
                continue
            attempt["status"] = "interrupted"
            attempt["finished_at"] = _tc().utc_now_iso()
            attempt["interruptionReason"] = "process_terminated_before_result"
            interrupted_attempts.append({
                "phaseId": str(phase_id),
                "workerId": str(attempt.get("workerId") or ""),
                "started_at": attempt.get("started_at"),
            })
        if str(raw_phase_state.get("status") or "") == "running":
            raw_phase_state["status"] = "pending"
            raw_phase_state["resume_reset_from"] = "running"
            reset_running_phases.append(str(phase_id))

    effective_plan = new_plan if isinstance(new_plan, dict) else old_plan
    old_phases = _resume_plan_phase_map(old_plan)
    new_phases = _resume_plan_phase_map(effective_plan)
    removed_phases = sorted(set(old_phases) - set(new_phases))
    changed_evidence_phases = sorted(
        phase_id
        for phase_id in set(old_phases) & set(new_phases)
        if _tc().evidence_contract_fingerprint(old_phases[phase_id])
        != _tc().evidence_contract_fingerprint(new_phases[phase_id])
    )
    changed_execution_phases = sorted(
        phase_id
        for phase_id in set(old_phases) & set(new_phases)
        if phase_id not in changed_evidence_phases
        and _tc().execution_contract_fingerprint(old_phases[phase_id])
        != _tc().execution_contract_fingerprint(new_phases[phase_id])
    )

    invalidated: Set[str] = set(removed_phases) | set(changed_evidence_phases)
    direct_invalidation_reasons: Dict[str, str] = {
        phase_id: "phase_removed" for phase_id in removed_phases
    }
    direct_invalidation_reasons.update({
        phase_id: "evidence_contract_changed"
        for phase_id in changed_evidence_phases
    })
    raw_digests = state.get("artifact_digests")
    artifact_digests: JsonDict = raw_digests if isinstance(raw_digests, dict) else {}
    missing_artifacts: List[str] = []
    corrupt_artifacts: List[JsonDict] = []
    for phase_id, raw_phase_state in phases_state.items():
        if not isinstance(raw_phase_state, dict):
            continue
        artifacts = raw_phase_state.get("validated_artifacts")
        artifacts = artifacts if isinstance(artifacts, list) else []
        for artifact in artifacts if isinstance(artifacts, list) else []:
            integrity_error = _resume_artifact_integrity_error(
                logger, artifact_digests, artifact,
            )
            if not integrity_error:
                continue
            artifact_text = str(artifact)
            if integrity_error == "missing_or_unreadable":
                if artifact_text not in missing_artifacts:
                    missing_artifacts.append(artifact_text)
                direct_invalidation_reasons[str(phase_id)] = (
                    "validated_artifact_missing"
                )
            else:
                corrupt_artifacts.append({
                    "path": artifact_text,
                    "reason": integrity_error,
                })
                direct_invalidation_reasons[str(phase_id)] = (
                    "validated_artifact_corrupt"
                )
            invalidated.add(str(phase_id))
        if str(phase_id) not in invalidated:
            validator_error = _legacy_extraction_validation_error(
                logger,
                new_phases.get(str(phase_id)) or old_phases.get(str(phase_id)),
                artifacts,
                artifact_digests,
            )
            if validator_error:
                corrupt_artifacts.append({
                    "path": ",".join(str(item) for item in artifacts),
                    "reason": validator_error,
                })
                invalidated.add(str(phase_id))
                direct_invalidation_reasons[str(phase_id)] = (
                    "validated_artifact_corrupt"
                )
            else:
                # The first safe resume of a digest-less legacy artifact
                # establishes an integrity baseline for every later resume.
                for artifact in artifacts:
                    if _artifact_recorded_digest(
                        logger, artifact_digests, artifact,
                    ):
                        continue
                    path = _resume_artifact_path(logger, artifact)
                    digest = _artifact_sha256(path, logger)
                    if digest:
                        artifact_digests[str(path)] = digest

    active_artifacts = state.get("artifacts")
    active_artifacts = active_artifacts if isinstance(active_artifacts, list) else []
    unattributed_missing: Set[str] = set()
    for artifact in active_artifacts:
        integrity_error = _resume_artifact_integrity_error(
            logger, artifact_digests, artifact,
        )
        if not integrity_error:
            path = _resume_artifact_path(logger, artifact)
            if not _artifact_recorded_digest(
                logger, artifact_digests, artifact,
            ):
                digest = _artifact_sha256(path, logger)
                if digest:
                    artifact_digests[str(path)] = digest
            continue
        identity = str(_resume_artifact_path(logger, artifact))
        unattributed_missing.add(identity)
        artifact_text = str(artifact)
        if integrity_error == "missing_or_unreadable":
            if artifact_text not in missing_artifacts:
                missing_artifacts.append(artifact_text)
        elif not any(
            str(item.get("path") or "") == artifact_text
            for item in corrupt_artifacts
            if isinstance(item, dict)
        ):
            corrupt_artifacts.append({
                "path": artifact_text,
                "reason": integrity_error,
            })

    raw_supersessions = state.get("artifact_supersessions")
    if isinstance(raw_supersessions, list):
        for entry in raw_supersessions:
            if not isinstance(entry, dict):
                continue
            absorbed = entry.get("absorbed")
            referenced = [entry.get("deliverable")]
            if isinstance(absorbed, list):
                referenced.extend(absorbed)
            for artifact in referenced:
                if not str(artifact or "").strip():
                    continue
                integrity_error = _resume_artifact_integrity_error(
                    logger, artifact_digests, artifact,
                )
                if not integrity_error:
                    if not _artifact_recorded_digest(
                        logger, artifact_digests, artifact,
                    ):
                        path = _resume_artifact_path(logger, artifact)
                        digest = _artifact_sha256(path, logger)
                        if digest:
                            artifact_digests[str(path)] = digest
                    continue
                identity = str(_resume_artifact_path(logger, artifact))
                unattributed_missing.add(identity)
                artifact_text = str(artifact)
                if integrity_error == "missing_or_unreadable":
                    if artifact_text not in missing_artifacts:
                        missing_artifacts.append(artifact_text)
                elif not any(
                    str(item.get("path") or "") == artifact_text
                    for item in corrupt_artifacts
                    if isinstance(item, dict)
                ):
                    corrupt_artifacts.append({
                        "path": artifact_text,
                        "reason": integrity_error,
                    })

    downstream: Dict[str, Set[str]] = {}
    for plan in (old_plan, effective_plan):
        for producer, consumers in _resume_downstream_map(plan).items():
            downstream.setdefault(producer, set()).update(consumers)
    queue = list(invalidated)
    while queue:
        producer = queue.pop(0)
        for consumer in downstream.get(producer, set()):
            if consumer not in invalidated:
                invalidated.add(consumer)
                queue.append(consumer)

    phase_order: List[str] = []
    for plan_map in (old_phases, new_phases):
        for phase_id in plan_map:
            if phase_id not in phase_order:
                phase_order.append(phase_id)
    reset_phases = [phase_id for phase_id in phase_order if phase_id in invalidated]
    reset_phases.extend(sorted(invalidated - set(reset_phases)))

    invalidated_artifacts: List[str] = []
    retired_identities: Set[str] = set(unattributed_missing)
    for phase_id in reset_phases:
        phase_state = phases_state.get(phase_id)
        if not isinstance(phase_state, dict):
            continue
        artifacts = phase_state.get("validated_artifacts")
        for artifact in artifacts if isinstance(artifacts, list) else []:
            artifact_text = str(artifact)
            if artifact_text not in invalidated_artifacts:
                invalidated_artifacts.append(artifact_text)
            retired_identities.add(str(_resume_artifact_path(logger, artifact)))
        phases_state[phase_id] = {
            **_tc()._empty_phase_state(),
            "resume_reset_reason": direct_invalidation_reasons.get(
                phase_id, "upstream_evidence_invalidated",
            ),
        }

    # A shared path remains active when another non-invalidated validated phase
    # still owns it.  Missing files are always removed regardless of ownership.
    remaining_identities: Set[str] = set()
    for phase_id, phase_state in phases_state.items():
        if phase_id in invalidated or not isinstance(phase_state, dict):
            continue
        for artifact in phase_state.get("validated_artifacts") or []:
            remaining_identities.add(str(_resume_artifact_path(logger, artifact)))
    state["artifacts"] = [
        artifact
        for artifact in active_artifacts
        if (
            str(_resume_artifact_path(logger, artifact)) not in unattributed_missing
            and (
                str(_resume_artifact_path(logger, artifact)) not in retired_identities
                or str(_resume_artifact_path(logger, artifact)) in remaining_identities
            )
        )
    ]

    # Keep integrity/supersession metadata aligned with the active generation.
    removed_identities = (
        retired_identities | unattributed_missing
    ) - remaining_identities
    state["artifact_digests"] = {
        str(path): digest
        for path, digest in artifact_digests.items()
        if str(_resume_artifact_path(logger, path)) not in removed_identities
    }
    supersessions = state.get("artifact_supersessions")
    state["artifact_supersessions"] = [
        copy.deepcopy(entry)
        for entry in supersessions if isinstance(entry, dict)
        and str(_resume_artifact_path(logger, entry.get("deliverable")))
        not in removed_identities
        and not any(
            str(_resume_artifact_path(logger, absorbed)) in removed_identities
            for absorbed in entry.get("absorbed") or []
        )
    ] if isinstance(supersessions, list) else []

    state["current_phase"] = _tc()._first_active_phase_id(effective_plan, phases_state)
    audited_reset_phases = list(reset_phases)
    audited_reset_phases.extend(
        phase_id
        for phase_id in sorted(set(reset_running_phases))
        if phase_id not in audited_reset_phases
    )
    audit: JsonDict = {
        "at": _tc().utc_now_iso(),
        "instruction": str(instruction or ""),
        "resetPhases": audited_reset_phases,
        "invalidatedArtifacts": invalidated_artifacts,
        "missingArtifacts": missing_artifacts,
        "corruptArtifacts": corrupt_artifacts,
        "interruptedAttempts": interrupted_attempts,
        "resetRunningPhases": sorted(set(reset_running_phases)),
        "changedEvidencePhases": changed_evidence_phases,
        "changedExecutionPhases": changed_execution_phases,
        "removedPhases": removed_phases,
    }
    if record_audit:
        resumes = state.setdefault("resumes", [])
        if not isinstance(resumes, list):
            resumes = []
            state["resumes"] = resumes
        resumes.append(copy.deepcopy(audit))
    if isinstance(state, _tc()._TaskStateSnapshot):
        # Resume is a deliberate one-shot reconciliation of the whole task.
        # The CLI may persist the returned state after a replay confirmation,
        # so carry replacement intent out-of-band rather than adding a field.
        state._task_state_replace = True
    if persist:
        _tc().write_task_state(logger, state, replace=True)
        logger.write("task_state.resume_prepared", {
            key: value for key, value in audit.items() if key != "instruction"
        })
    return {**audit, "state": state}

def phase_pacing_remaining_seconds(
    plan: Optional[JsonDict],
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_contract: Optional[JsonDict] = None,
    now: Optional[datetime] = None,
    random_value: Optional[float] = None,
) -> float:
    """Remaining dependency-to-start delay for a phase.

    Independent phases (depends_on=[]) have no dependency completion anchor and
    therefore never wait.  The caller invokes this only after the dependency
    gate reports ready and before reserving a BrowserAgent slot.
    """
    if not isinstance(plan, dict) or not phase_id:
        return 0.0
    plan_phases = [item for item in plan.get("phases", []) if isinstance(item, dict)]
    prior_ids: List[str] = []
    target: Optional[JsonDict] = None
    for phase in plan_phases:
        current_id = str(phase.get("id") or "")
        if current_id == str(phase_id):
            target = phase
            break
        prior_ids.append(current_id)
    if target is None:
        return 0.0
    dependencies = _phase_dependency_ids(target)
    if dependencies == []:
        return 0.0
    if dependencies is None:
        dependencies = prior_ids
    if not dependencies:
        return 0.0

    pacing = merge_pacing(
        plan.get("pacing"),
        target.get("pacing"),
        worker_contract.get("pacing") if isinstance(worker_contract, dict) else None,
    )
    interval = jittered_interval(
        pacing["phase_interval_seconds"],
        pacing["jitter_ratio"],
        random_value=random_value,
    )
    if interval <= 0.0:
        return 0.0

    state = _tc().load_task_state(logger)
    states = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    completed_at: List[datetime] = []
    for dependency_id in dependencies:
        dependency_state = states.get(dependency_id)
        if not isinstance(dependency_state, dict):
            return 0.0
        attempts = dependency_state.get("attempts")
        timestamps = [
            parse_utc_timestamp(item.get("finished_at"))
            for item in (attempts if isinstance(attempts, list) else [])
            if isinstance(item, dict) and _attempt_was_validated_done(item)
        ]
        timestamps = [item for item in timestamps if item is not None]
        if not timestamps:
            return 0.0
        completed_at.append(max(timestamps))
    anchor = max(completed_at)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (current.astimezone(timezone.utc) - anchor).total_seconds())
    return max(0.0, interval - elapsed)

def _attempt_was_validated_done(attempt: JsonDict) -> bool:
    """True only for the attempt that actually established dependency readiness."""
    if str(attempt.get("status") or "") == "validated_done":
        return True
    if str(attempt.get("validatedStatus") or "") == "validated_done":
        return True
    validation = attempt.get("validation")
    return bool(
        isinstance(validation, dict)
        and str(validation.get("status") or "") == "done"
    )

def _dependency_blocker(
    phase: JsonDict,
    phases: JsonDict,
    implicit_prior_phase_ids: List[str],
) -> Optional[JsonDict]:
    dependency_ids = _phase_dependency_ids(phase)
    if dependency_ids is None:
        dependency_ids = implicit_prior_phase_ids
    for dep_id in dependency_ids:
        dep_state = phases.get(dep_id)
        dep_status = (
            str(dep_state.get("status") or "")
            if isinstance(dep_state, dict)
            else ""
        )
        if dep_status == "validated_done":
            continue
        if dep_status in _tc().BLOCKING_DEPENDENCY_STATUSES:
            return {
                "type": "dependency_failed",
                "dependencyPhaseId": dep_id,
                "dependencyStatus": dep_status,
                "blocking": True,
                "message": (
                    f"Dependency phase {dep_id} ended with status"
                    f" {dep_status}; this phase cannot run without a revised"
                    " plan or replacement input artifact."
                ),
            }
        return {
            "type": "dependency_not_ready",
            "dependencyPhaseId": dep_id,
            "dependencyStatus": dep_status or "pending",
            "blocking": False,
            "message": (
                f"Dependency phase {dep_id} is not validated yet; wait for it"
                " before spawning this phase."
            ),
        }
    return None

def _mark_phase_blocked_by_dependency(
    phases: JsonDict,
    phase_id: str,
    blocker: JsonDict,
) -> None:
    phase_state = phases.get(phase_id)
    if not isinstance(phase_state, dict):
        return
    phase_state["status"] = "blocked_by_dependency"
    phase_state["last_failure"] = [blocker]
    phase_state["last_failure_classification"] = {
        "category": "blocked_by_dependency",
        "dependencyPhaseId": blocker.get("dependencyPhaseId"),
        "dependencyStatus": blocker.get("dependencyStatus"),
        "hint": blocker.get("message"),
    }

def _count_budgeted_phase_attempts(attempts: Any) -> int:
    """Count attempts that consume ``max_attempts``.

    A process interruption has no worker result and is retained only for
    auditability; resume must not turn that reservation into a failed business
    attempt.  Keep this predicate centralized because all three mechanical
    scheduling fences must agree.
    """

    if not isinstance(attempts, list):
        return 0
    return sum(
        1
        for attempt in attempts
        if isinstance(attempt, dict)
        and str(attempt.get("status") or "")
        not in {"interrupted", WORKER_STATUS_PARTIAL}
    )

def phase_start_rejection(
    plan: Optional[JsonDict],
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_contract: Optional[JsonDict] = None,
) -> Optional[JsonDict]:
    if not plan or not phase_id:
        return None
    state = _tc().load_task_state(logger)
    phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    plan_phases = plan.get("phases") if isinstance(plan.get("phases"), list) else []
    prior_ids: List[str] = []
    target_phase: Optional[JsonDict] = None
    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        current_id = str(phase.get("id") or "")
        if current_id == str(phase_id):
            target_phase = phase
            break
        prior_ids.append(current_id)
    if target_phase is None:
        return None
    phase_state = phases.get(str(phase_id))
    status = (
        str(phase_state.get("status") or "")
        if isinstance(phase_state, dict)
        else ""
    )
    if status in _tc().TERMINAL_PHASE_STATUSES:
        return {
            "status": "phase_not_startable",
            "phaseId": str(phase_id),
            "phaseStatus": status,
            "tool_was_executed": False,
            "next_instruction": (
                "Do not spawn this phase. Emit a revised task_plan with a new"
                " phase id/objective or final_answer with the blocker."
            ),
        }
    if status == "running":
        return {
            "status": "phase_already_running",
            "phaseId": str(phase_id),
            "tool_was_executed": False,
            "next_instruction": (
                "A worker is already running for this phase. Wait for it instead"
                " of spawning another copy."
            ),
        }
    attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
    attempts_count = _count_budgeted_phase_attempts(attempts)
    max_attempts = (
        _tc()._positive_int(target_phase.get("max_attempts"), default=1)
        if target_phase.get("max_attempts") is not None
        else None
    )
    if (
        max_attempts is not None
        and attempts_count >= max_attempts
        and status in _tc().RETRYABLE_PHASE_FAILURE_STATUSES
    ):
        return {
            "status": "phase_exhausted",
            "phaseId": str(phase_id),
            "attempts": attempts_count,
            "max_attempts": max_attempts,
            "tool_was_executed": False,
            "next_instruction": (
                "This phase has used its explicitly declared worker-attempt"
                " resource budget. The receipt does not imply that the"
                " objective is infeasible."
            ),
        }
    blocker = _dependency_blocker(target_phase, phases, prior_ids)
    if blocker is not None:
        if blocker.get("blocking"):
            _mark_phase_blocked_by_dependency(phases, str(phase_id), blocker)
            state["current_phase"] = _tc()._first_active_phase_id(plan, phases)
            _tc().write_task_state(logger, state)
            logger.write("task_phase.blocked_by_dependency", {
                "phaseId": str(phase_id),
                **blocker,
            })
        # Two very different situations share this branch: a dependency that
        # FAILED terminally (replan territory) vs one that simply has not
        # finished yet (wait territory). Now that the rejection payload
        # reaches the Lead verbatim, the wrong instruction would upgrade a
        # blind retry into a wrong replan — say the right thing per case.
        if blocker.get("blocking"):
            next_instruction = (
                "Do not spawn this phase: its dependency ended in a terminal"
                " failure and will not recover on its own. Emit a revised"
                " task_plan that fixes or replaces the dependency phase —"
                " a replan resets blocked_by_dependency and re-derives it"
                " from the new plan — or final_answer with the blocker."
            )
        else:
            next_instruction = (
                f"Dependency phase {blocker.get('dependencyPhaseId')} is"
                f" {blocker.get('dependencyStatus') or 'pending'}, not failed."
                " Do NOT replan and do not re-spawn in a loop: wait for it"
                " (wait_browser_agents if it is running), then spawn this"
                " phase once the dependency is validated_done."
            )
        return {
            "status": (
                "blocked_by_dependency"
                if blocker.get("blocking")
                else "dependency_not_ready"
            ),
            "phaseId": str(phase_id),
            "tool_was_executed": False,
            **blocker,
            "next_instruction": next_instruction,
        }
    return None

def mark_phase_exhausted_if_needed(
    plan: Optional[JsonDict],
    logger: RunLogger,
) -> List[JsonDict]:
    if not plan:
        return []
    state = _tc().load_task_state(logger)
    raw_phases_state = state.get("phases")
    phases: JsonDict = raw_phases_state if isinstance(raw_phases_state, dict) else {}
    raw_plan_phases = plan.get("phases")
    plan_phases = raw_plan_phases if isinstance(raw_plan_phases, list) else []
    exhausted: List[JsonDict] = []
    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        phase_state = phases.get(phase_id)
        if not isinstance(phase_state, dict):
            continue
        status = str(phase_state.get("status") or "")
        if status in _tc().TERMINAL_PHASE_STATUSES:
            continue
        attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
        attempts_count = _count_budgeted_phase_attempts(attempts)
        max_attempts = (
            _tc()._positive_int(phase.get("max_attempts"), default=1)
            if phase.get("max_attempts") is not None
            else None
        )
        if (
            max_attempts is None
            or attempts_count < max_attempts
            or status not in _tc().RETRYABLE_PHASE_FAILURE_STATUSES
        ):
            continue
        classification = phase_state.get("last_failure_classification")
        if not isinstance(classification, dict) and isinstance(attempts, list) and attempts:
            validation = attempts[-1].get("validation")
            if isinstance(validation, dict):
                classification = validation.get("classification")
        payload = {
            "phaseId": phase_id,
            "status": "phase_failed",
            "previousStatus": status,
            "attempts": attempts_count,
            "max_attempts": max_attempts,
            "last_failure": phase_state.get("last_failure"),
            "classification": classification if isinstance(classification, dict) else None,
        }
        phase_state["status"] = "phase_failed"
        phase_state["exhausted_at"] = _tc().utc_now_iso()
        phase_state["max_attempts"] = max_attempts
        exhausted.append(payload)

    if exhausted:
        state["current_phase"] = _tc()._first_active_phase_id(plan, phases)
        _tc().write_task_state(logger, state)
        for payload in exhausted:
            logger.write("task_phase.exhausted", payload)
    return exhausted

def next_pending_phase(plan: Optional[JsonDict], logger: RunLogger) -> Optional[JsonDict]:
    if not plan:
        return None
    state = _tc().load_task_state(logger)
    raw_phases_state = state.get("phases")
    phases: JsonDict = raw_phases_state if isinstance(raw_phases_state, dict) else {}
    raw_plan_phases = plan.get("phases")
    plan_phases = raw_plan_phases if isinstance(raw_plan_phases, list) else []
    prior_ids: List[str] = []
    state_changed = False
    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        raw_phase_state = phases.get(phase_id)
        phase_state: JsonDict = raw_phase_state if isinstance(raw_phase_state, dict) else {}
        status = phase_state.get("status")
        if status in _tc().TERMINAL_PHASE_STATUSES:
            prior_ids.append(phase_id)
            continue
        if status == "running":
            prior_ids.append(phase_id)
            continue
        blocker = _dependency_blocker(phase, phases, prior_ids)
        if blocker is not None:
            if blocker.get("blocking"):
                _mark_phase_blocked_by_dependency(phases, phase_id, blocker)
                logger.write("task_phase.blocked_by_dependency", {
                    "phaseId": phase_id,
                    **blocker,
                })
                state_changed = True
            prior_ids.append(phase_id)
            continue
        attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
        attempts_count = _count_budgeted_phase_attempts(attempts)
        max_attempts = (
            _tc()._positive_int(phase.get("max_attempts"), default=1)
            if phase.get("max_attempts") is not None
            else None
        )
        if (
            max_attempts is not None
            and attempts_count >= max_attempts
            and status in _tc().RETRYABLE_PHASE_FAILURE_STATUSES
        ):
            prior_ids.append(phase_id)
            continue
        if status not in {"validated_done"}:
            if state_changed:
                state["current_phase"] = phase_id
                _tc().write_task_state(logger, state)
            return phase
        prior_ids.append(phase_id)
    if state_changed:
        state["current_phase"] = _tc()._first_active_phase_id(plan, phases)
        _tc().write_task_state(logger, state)
    return None

def find_phase(plan: Optional[JsonDict], phase_id: Optional[str]) -> Optional[JsonDict]:
    if not plan or not phase_id:
        return None
    for phase in plan.get("phases", []):
        if isinstance(phase, dict) and str(phase.get("id")) == str(phase_id):
            return phase
    return None
