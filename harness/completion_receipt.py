"""Mechanical, read-only task completion summary.

The receipt is derived from coordinator state and worker ledgers.  Model prose
cannot set or override any field in it.
"""

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from harness.utils import JsonDict


def terminal_consistency_contradictions(
    *,
    state: Any,
    plan: Any,
    final_status: str,
) -> List[JsonDict]:
    """Return mechanical contradictions between a done claim and raw attempts.

    This is deliberately not a completeness proof.  It only rejects the
    impossible combination "done" + a required artifact phase whose latest
    worker did not itself finish with a schema-valid artifact.
    """
    if str(final_status or "").strip() != "done":
        return []
    task_state = state if isinstance(state, dict) else {}
    phase_states = (
        task_state.get("phases")
        if isinstance(task_state.get("phases"), dict)
        else {}
    )
    phases = plan.get("phases") if isinstance(plan, dict) else []
    if not isinstance(phases, list) or not phases:
        # Resume/compatibility callers may only have persisted phase state.
        # Use a contract only when that state explicitly carries one; never
        # infer "artifact required" merely from a zero receipt.
        phases = []
        for phase_id, phase_state in phase_states.items():
            if not isinstance(phase_state, dict):
                continue
            expected = (
                phase_state.get("expected_artifact")
                if isinstance(phase_state.get("expected_artifact"), dict)
                else phase_state.get("expectedArtifact")
            )
            if isinstance(expected, dict):
                phases.append({"id": phase_id, "expected_artifact": expected})
    contradictions: List[JsonDict] = []
    for phase in phases if isinstance(phases, list) else []:
        if not isinstance(phase, dict):
            continue
        expected = phase.get("expected_artifact")
        if not isinstance(expected, dict) or not expected:
            continue
        # An artifact declaration with no name, fields, or row constraint is
        # not an output requirement.
        if not any(
            expected.get(key)
            for key in (
                "name", "fields", "required_fields", "exact_rows",
                "min_rows", "max_rows", "count_range",
            )
        ):
            continue
        phase_id = str(phase.get("id") or "")
        phase_state = phase_states.get(phase_id)
        phase_state = phase_state if isinstance(phase_state, dict) else {}
        attempts = phase_state.get("attempts")
        attempts = attempts if isinstance(attempts, list) else []
        latest = next(
            (item for item in reversed(attempts) if isinstance(item, dict)),
            {},
        )
        validation = latest.get("validation")
        validation = validation if isinstance(validation, dict) else {}
        raw_status = str(latest.get("status") or "")
        if raw_status == "done" and validation.get("status") == "done":
            continue
        contradictions.append({
            "phaseId": phase_id,
            "rawStatus": raw_status or None,
            "validationStatus": validation.get("status"),
            "phaseStatus": phase_state.get("status"),
            "attemptCount": len(attempts),
        })
    return contradictions


def _finished_worker_entries(spawner: Any) -> Iterable[Tuple[str, JsonDict]]:
    handles = getattr(spawner, "_handles", None)
    entries = handles.items() if isinstance(handles, dict) else []
    for handle_id, handle in entries:
        task = getattr(handle, "async_task", None)
        if task is None or not task.done() or task.cancelled():
            continue
        try:
            result = task.result()
        except Exception:
            continue
        if isinstance(result, dict):
            yield str(handle_id), result


def _finished_worker_results(spawner: Any) -> Iterable[JsonDict]:
    for _, result in _finished_worker_entries(spawner):
        yield result


def _resolved(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return text


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError):
        return ""
    return digest.hexdigest()


def _apply_supersessions(state: JsonDict, paths: List[str]) -> List[str]:
    """Apply valid merge edges to the current active artifact generation.

    A Lead `reference_merge` that copies every row of its sources verbatim is
    a new generation of the same data, not an addition to it. Until this ran,
    the merged artifact was in no ledger at all: the completion receipt could
    not count it and the numeric gate resolved claims about it against the
    sources instead, so a merge that lost rows was checked against the very
    data it had just dropped.

    Supersession records are historical evidence, not an independent active
    ledger.  Apply them in order and only while every absorbed input is still
    active and the deliverable file still exists.  Consequently, removing any
    producer during resume invalidation also disables its downstream merge;
    an old record cannot resurrect a retired deliverable.
    """
    entries = state.get("artifact_supersessions")
    if not isinstance(entries, list) or not entries:
        return paths

    active = list(paths)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        deliverable = str(entry.get("deliverable") or "").strip()
        deliverable_identity = _resolved(deliverable)
        retired = {
            _resolved(item)
            for item in (entry.get("absorbed") or [])
            if str(item or "").strip()
        }
        if not deliverable or not retired:
            continue
        active_identities = {_resolved(path) for path in active}
        if not retired.issubset(active_identities):
            continue
        # A recorded digest is durable proof that the merge output existed
        # when the edge was committed. Legacy supersessions predate digests,
        # so they retain their historical receipt semantics; resume's state
        # reconciliation separately prunes missing/corrupt deliverables before
        # this reducer runs. New records always carry a digest.
        raw_digests = state.get("artifact_digests")
        digests = raw_digests if isinstance(raw_digests, dict) else {}
        has_integrity_record = bool(
            digests.get(deliverable_identity) or digests.get(deliverable)
        )
        try:
            deliverable_exists = Path(deliverable_identity).is_file()
        except (OSError, ValueError):
            deliverable_exists = False
        if has_integrity_record and not deliverable_exists:
            continue
        if has_integrity_record:
            expected_digest = str(
                digests.get(deliverable_identity) or digests.get(deliverable) or ""
            ).strip().lower()
            actual_digest = _file_sha256(deliverable_identity)
            if actual_digest != expected_digest:
                continue

        # Remove every spelling of an absorbed path.  If the deliverable is
        # already represented in the active ledger, retain that spelling;
        # otherwise append the path certified by this applicable merge edge.
        active = [path for path in active if _resolved(path) not in retired]
        if deliverable_identity not in {_resolved(path) for path in active}:
            active.append(deliverable)
    return active


def _validated_artifacts(state: JsonDict) -> List[str]:
    active_ledger = state.get("artifacts")
    if isinstance(active_ledger, list) and active_ledger:
        # task_state.artifacts is the coordinator's active validated generation
        # ledger. Prefer it over historical per-phase paths so replacements are
        # not double-counted across replans/remediation.
        return _apply_supersessions(state, list(dict.fromkeys(
            str(value) for value in active_ledger if value
        )))
    paths: List[str] = []
    phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    for phase in phases.values():
        if not isinstance(phase, dict) or phase.get("status") != "validated_done":
            continue
        values = phase.get("validated_artifacts")
        if isinstance(values, list):
            paths.extend(str(value) for value in values if value)
    return _apply_supersessions(state, list(dict.fromkeys(paths)))


def _artifact_row_count(paths: List[str]) -> int:
    count = 0
    for raw_path in paths:
        try:
            payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            count += len(rows)
    return count


def _build_completion_receipt_from_results(
    task_state: JsonDict,
    results: Iterable[JsonDict],
) -> JsonDict:
    phases = task_state.get("phases") if isinstance(task_state.get("phases"), dict) else {}
    validated_phase_count = sum(
        1 for value in phases.values()
        if isinstance(value, dict) and value.get("status") == "validated_done"
    )
    artifact_paths = _validated_artifacts(task_state)

    download_operations: Dict[str, JsonDict] = {}
    observed_challenge = False
    observed_challenge_count = 0
    vl_attempts = 0
    vl_solved = 0
    hitl_requests = 0
    hitl_resumes = 0
    unresolved_challenges = 0
    for result in results:
        for receipt in result.get("downloadOperationReceipts") or []:
            if not isinstance(receipt, dict):
                continue
            key = json.dumps([
                str(receipt.get("url") or ""),
                str(receipt.get("savePath") or ""),
            ], ensure_ascii=False)
            download_operations[key] = dict(receipt)
        challenge = result.get("challengeReceipt")
        if isinstance(challenge, dict):
            observed_challenge = observed_challenge or bool(challenge.get("observed"))
            observed_challenge_count += int(challenge.get("observedCount") or 0)
            vl_attempts += int(challenge.get("vlSolveAttempts") or 0)
            vl_solved += int(challenge.get("vlSolvedCount") or 0)
            hitl_requests += int(challenge.get("hitlRequests") or 0)
            hitl_resumes += int(challenge.get("hitlResumes") or 0)
            unresolved_challenges += int(bool(challenge.get("unresolved")))

    completed = sum(
        1 for receipt in download_operations.values()
        if receipt.get("state") == "completed"
    )
    active = sum(
        1 for receipt in download_operations.values()
        if receipt.get("state") in {"downloading", "paused"}
    )
    ambiguous = sum(
        1 for receipt in download_operations.values()
        if receipt.get("state") in {
            "ambiguous", "unknown", "timeout_unverified", "",
        }
    )
    challenge_status = (
        "observed_unresolved" if unresolved_challenges > 0
        else "resolved_by_vl" if vl_solved > 0
        else "resolved_by_hitl" if hitl_resumes > 0
        else "observed_unresolved" if observed_challenge
        else "not_observed"
    )
    return {
        "artifact": {
            "validatedPhases": validated_phase_count,
            "validatedRows": _artifact_row_count(artifact_paths),
            "validatedArtifacts": len(artifact_paths),
        },
        "downloads": {
            "completed": completed,
            "active": active,
            "ambiguous": ambiguous,
        },
        "challenge": {
            "status": challenge_status,
            "observedCount": observed_challenge_count,
            "vlSolveAttempts": vl_attempts,
            "vlSolvedCount": vl_solved,
            "hitlRequests": hitl_requests,
            "hitlResumes": hitl_resumes,
        },
        "source": "harness_mechanical_state",
        "modelOverrideAllowed": False,
    }


def build_completion_receipt(*, state: Any, spawner: Any) -> JsonDict:
    task_state = state if isinstance(state, dict) else {}
    return _build_completion_receipt_from_results(
        task_state,
        _finished_worker_results(spawner),
    )


def _require_run_id(run_id: Any) -> str:
    value = str(run_id or "").strip()
    if not value:
        raise ValueError("run_id must be a non-empty string")
    return value


def _download_identity(receipt: JsonDict) -> str:
    """Return the stable operation identity used by cumulative receipts."""
    for field in ("operationId", "downloadId", "receiptId", "id"):
        value = str(receipt.get(field) or "").strip()
        if value:
            return json.dumps([field, value], ensure_ascii=False)
    return json.dumps(
        [
            "url_path",
            str(receipt.get("url") or ""),
            _resolved(receipt.get("savePath")),
        ],
        ensure_ascii=False,
    )


def _challenge_identity(
    *,
    run_id: str,
    handle_id: str,
    result: JsonDict,
    receipt: JsonDict,
) -> str:
    """Identify a challenge receipt without treating totals as identities.

    Challenge receipts currently contain counters rather than individual
    captcha/HITL operation IDs. Prefer an explicit ID when one is available.
    Otherwise the phase is the durable identity: an interrupted phase's old
    unresolved receipt must be replaced by its resumed, solved receipt instead
    of being counted as a second challenge merely because the process changed.
    """
    for field in ("receiptId", "challengeId", "id"):
        value = str(receipt.get(field) or "").strip()
        if value:
            return json.dumps([field, value], ensure_ascii=False)
    phase_id = str(result.get("phaseId") or "").strip()
    if phase_id:
        return json.dumps(["phase", phase_id], ensure_ascii=False)
    return json.dumps(
        [
            "worker_result",
            handle_id,
            str(result.get("workerId") or ""),
            _resolved(result.get("tracePath")),
        ],
        ensure_ascii=False,
    )


def _completion_evidence(*, state: JsonDict, spawner: Any, run_id: str) -> JsonDict:
    phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    validated_phase_ids = sorted(
        str(phase_id)
        for phase_id, phase_state in phases.items()
        if isinstance(phase_state, dict)
        and phase_state.get("status") == "validated_done"
    )

    downloads: Dict[str, JsonDict] = {}
    challenges: Dict[str, JsonDict] = {}
    for handle_id, result in _finished_worker_entries(spawner):
        for receipt in result.get("downloadOperationReceipts") or []:
            if not isinstance(receipt, dict):
                continue
            identity = _download_identity(receipt)
            downloads[identity] = {
                "identity": identity,
                "receipt": dict(receipt),
            }
        challenge = result.get("challengeReceipt")
        if not isinstance(challenge, dict):
            continue
        identity = _challenge_identity(
            run_id=run_id,
            handle_id=handle_id,
            result=result,
            receipt=challenge,
        )
        challenges[identity] = {
            "identity": identity,
            "receipt": dict(challenge),
        }

    return {
        "validatedPhaseIds": validated_phase_ids,
        "artifactPaths": _validated_artifacts(state),
        "downloadOperations": list(downloads.values()),
        "challengeOperations": list(challenges.values()),
    }


def _evidence_entries(value: Any, field: str) -> Iterable[JsonDict]:
    if not isinstance(value, dict):
        return []
    entries = value.get(field)
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def _merge_cumulative_evidence(
    *,
    state: JsonDict,
    current_evidence: JsonDict,
) -> JsonDict:
    """Build a cross-run receipt from operation evidence, not prior totals."""
    downloads: Dict[str, JsonDict] = {}
    challenges: Dict[str, JsonDict] = {}

    stored = state.get("completion_receipts")
    stored_entries = stored.items() if isinstance(stored, dict) else []
    for stored_run_id, entry in stored_entries:
        if not isinstance(entry, dict):
            continue
        evidence = entry.get("evidence")
        for item in _evidence_entries(evidence, "downloadOperations"):
            receipt = item.get("receipt")
            if not isinstance(receipt, dict):
                continue
            identity = str(item.get("identity") or _download_identity(receipt))
            downloads[identity] = dict(receipt)
        for index, item in enumerate(_evidence_entries(evidence, "challengeOperations")):
            receipt = item.get("receipt")
            if not isinstance(receipt, dict):
                continue
            identity = str(item.get("identity") or json.dumps(
                ["stored", str(stored_run_id), index], ensure_ascii=False,
            ))
            challenges[identity] = dict(receipt)

    # The live run wins over a stale snapshot of the same underlying operation.
    for item in _evidence_entries(current_evidence, "downloadOperations"):
        receipt = item.get("receipt")
        if isinstance(receipt, dict):
            identity = str(item.get("identity") or _download_identity(receipt))
            downloads[identity] = dict(receipt)
    for index, item in enumerate(_evidence_entries(current_evidence, "challengeOperations")):
        receipt = item.get("receipt")
        if isinstance(receipt, dict):
            identity = str(item.get("identity") or json.dumps(
                ["current", index], ensure_ascii=False,
            ))
            challenges[identity] = dict(receipt)

    # Artifacts are an active ledger, unlike downloads/challenge interactions:
    # an invalidated artifact must not remain cumulative merely because it was
    # valid in an earlier run.  Deduplicate the current active generation by
    # resolved path identity.
    artifact_paths: Dict[str, str] = {}
    for path in current_evidence.get("artifactPaths") or []:
        if not str(path or "").strip():
            continue
        identity = _resolved(path)
        artifact_paths.setdefault(identity, str(path))

    validated_phase_ids = current_evidence.get("validatedPhaseIds")
    if not isinstance(validated_phase_ids, list):
        validated_phase_ids = []
    cumulative_state = {
        "phases": {
            str(phase_id): {"status": "validated_done"}
            for phase_id in set(map(str, validated_phase_ids))
        },
        "artifacts": list(artifact_paths.values()),
    }
    # Feed deduplicated operation receipts through the same mechanical reducer
    # as the legacy API so current and cumulative status semantics cannot drift.
    cumulative_results = [{
        "downloadOperationReceipts": list(downloads.values()),
    }]
    cumulative_results.extend(
        {"challengeReceipt": receipt} for receipt in challenges.values()
    )
    return _build_completion_receipt_from_results(
        cumulative_state,
        cumulative_results,
    )


def _build_resume_completion_receipt(
    *,
    state: Any,
    spawner: Any,
    run_id: Any,
) -> Tuple[JsonDict, JsonDict]:
    normalized_run_id = _require_run_id(run_id)
    task_state = state if isinstance(state, dict) else {}
    current = build_completion_receipt(state=task_state, spawner=spawner)
    evidence = _completion_evidence(
        state=task_state,
        spawner=spawner,
        run_id=normalized_run_id,
    )
    cumulative = _merge_cumulative_evidence(
        state=task_state,
        current_evidence=evidence,
    )
    return {
        "runId": normalized_run_id,
        "currentRun": current,
        "cumulative": cumulative,
    }, evidence


def build_resume_completion_receipt(
    *,
    state: Any,
    spawner: Any,
    run_id: Any,
) -> JsonDict:
    """Return current-run and deduplicated cumulative mechanical receipts.

    ``build_completion_receipt`` remains the backwards-compatible, single-run
    API.  This resume-aware view consumes persisted operation evidence from
    ``state["completion_receipts"]`` and never sums prior aggregate totals.
    """
    receipt, _ = _build_resume_completion_receipt(
        state=state,
        spawner=spawner,
        run_id=run_id,
    )
    return receipt


def persist_completion_receipt(
    *,
    logger: Any,
    state: JsonDict,
    spawner: Any,
    run_id: Any,
) -> JsonDict:
    """Persist one run's receipt/evidence and return its cumulative view."""
    if not isinstance(state, dict):
        raise TypeError("state must be a dictionary")
    normalized_run_id = _require_run_id(run_id)
    receipt, evidence = _build_resume_completion_receipt(
        state=state,
        spawner=spawner,
        run_id=normalized_run_id,
    )
    resumed_from = str(getattr(logger, "resumed_from", "") or "").strip()
    if resumed_from:
        receipt["resumedFrom"] = resumed_from
    existing = state.get("completion_receipts")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("state.completion_receipts must be an object")
    receipts = dict(existing or {})
    receipts[normalized_run_id] = {
        "runId": normalized_run_id,
        "currentRun": receipt["currentRun"],
        "cumulative": receipt["cumulative"],
        "evidence": evidence,
    }
    if resumed_from:
        receipts[normalized_run_id]["resumedFrom"] = resumed_from
    state["completion_receipts"] = receipts

    # Import lazily: task_control already depends on the shared utility types,
    # while completion receipts are also used from Lead tools during teardown.
    from harness.task_control import write_task_state

    path = write_task_state(logger, state)
    logger.write(
        "completion_receipt.persisted",
        {"runId": normalized_run_id, "path": path},
    )
    return receipt
