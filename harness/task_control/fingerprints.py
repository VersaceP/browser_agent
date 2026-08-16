"""
harness.task_control.fingerprints - Contract fingerprints, spawn acquisition and attempt digests.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from typing import Any
from typing import List
from typing import Optional
from typing import Set
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlsplit
from harness.task_types import normalize_task_type
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import trim_large_strings

def _tc():
    import harness.task_control as tc

    return tc

_SOURCE_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+")

def _normalized_source_urls(*texts: Any) -> List[str]:
    """Normalized source identities mentioned by a phase (host+path, scheme/
    www/query/trailing-slash and trailing sentence punctuation stripped).
    Bounded and sorted for stability.

    Caveat (by design): URLs are regex-extracted from natural-language
    worker_task/objective text, so this dimension is only as stable as the
    Lead's phrasing — it is an AUXILIARY discriminator (so "same range,
    different source" unlocks the budget). The primary objective key remains
    the numeric validators + artifact name."""
    urls: Set[str] = set()
    for text in texts:
        for raw in _SOURCE_URL_RE.findall(str(text or "")):
            # Regex capture over prose swallows sentence punctuation:
            # "... from https://x/trending/week/." must not mint a fresh
            # fingerprint via that trailing dot.
            parsed = urlparse(raw.rstrip(".,;:!?)"))
            host = str(parsed.netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if not host:
                continue
            path = str(parsed.path or "").rstrip("/")
            urls.add(f"{host}{path}")
    return sorted(urls)[:3]

def _normalized_evidence_source_urls(*texts: Any) -> List[str]:
    """Canonical source URLs for resume evidence, retaining query identity.

    Objective-attempt budgeting intentionally ignores query strings, but a
    query often identifies the actual record/page (``?id=...``). Reusing that
    looser key for resume would incorrectly bless an artifact from a different
    source. Query ordering is normalized; fragments and scheme/www noise are
    ignored.
    """

    urls: Set[str] = set()
    for text in texts:
        for raw in _SOURCE_URL_RE.findall(str(text or "")):
            parsed = urlsplit(raw.rstrip(".,;:!?)"))
            host = str(parsed.netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if not host:
                continue
            path = str(parsed.path or "").rstrip("/")
            query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True))
            query = urlencode(query_pairs, doseq=True)
            urls.add(f"{host}{path}" + (f"?{query}" if query else ""))
    return sorted(urls)[:20]

def _primary_evidence_source_url(*texts: Any) -> str:
    """Return the first URL mentioned by the first prose field that has one."""

    for text in texts:
        matches = _SOURCE_URL_RE.findall(str(text or ""))
        if not matches:
            continue
        normalized = _normalized_evidence_source_urls(matches[0])
        if normalized:
            return normalized[0]
    return ""

def _canonical_resume_contract_value(value: Any) -> Any:
    """Canonicalize declarative evidence shapes without preserving list order."""

    if isinstance(value, dict):
        return {
            str(key): _canonical_resume_contract_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_canonical_resume_contract_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ),
        )
    return value

def evidence_contract_fingerprint(phase: Optional[JsonDict]) -> str:
    """Stable identity of evidence that can mechanically validate a phase.

    Natural-language objectives are deliberately excluded.  Source URLs are
    extracted separately so changing only prose does not invalidate evidence,
    while changing the actual page cannot silently preserve an old artifact.
    """

    if not isinstance(phase, dict):
        return ""
    expected = (
        phase.get("expected_artifact")
        if isinstance(phase.get("expected_artifact"), dict)
        else {}
    )
    raw_validators = (
        phase.get("validators") if isinstance(phase.get("validators"), list) else []
    )
    validators = [
        _tc()._canonical_validator_params(item)
        for item in raw_validators
        if isinstance(item, dict)
    ]
    declared_source_blob = json.dumps(
        {
            "source_url": phase.get("source_url"),
            "source_urls": phase.get("source_urls"),
            "input_artifacts": phase.get("input_artifacts"),
            "worker_contract": phase.get("worker_contract"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    declared_source_urls = _normalized_evidence_source_urls(
        declared_source_blob
    )
    if declared_source_urls:
        source_urls = declared_source_urls
    else:
        # Existing plans do not generally declare source_url (historical plans
        # put the target in prose). Use one primary URL by field precedence so
        # changing the real target invalidates evidence, while appending an
        # explanatory/example URL does not retire hours of validated work.
        primary_source = _primary_evidence_source_url(
            phase.get("worker_task"),
            phase.get("objective"),
            phase.get("context"),
        )
        source_urls = [primary_source] if primary_source else []
    payload = {
        "taskType": normalize_task_type(phase.get("task_type")),
        "sourceUrls": source_urls,
        "inputArtifacts": _canonical_resume_contract_value(
            phase.get("input_artifacts")
        ),
        "expectedArtifact": _canonical_resume_contract_value(expected),
        "validators": _canonical_resume_contract_value(validators),
        # A phase whose producer set changes no longer proves the same output,
        # even if its row schema is unchanged. None (implicit serial) stays
        # distinct from [] (explicitly independent).
        "dependsOn": _canonical_resume_contract_value(
            _tc()._normalized_depends_on(phase.get("depends_on"))
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

def execution_contract_fingerprint(phase: Optional[JsonDict]) -> str:
    """Report-only identity of execution strategy; it never retires evidence."""

    if not isinstance(phase, dict):
        return ""
    payload = {
        "taskType": phase.get("task_type"),
        "objective": phase.get("objective"),
        "workerTask": phase.get("worker_task"),
        "stageHint": phase.get("stage_hint"),
        "stageHintReason": phase.get("stage_hint_reason"),
        "workerContract": phase.get("worker_contract"),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

def _fingerprint_num(value: Any) -> Any:
    """Numeric normalization so 40 and "40" fingerprint identically."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return int(number) if number == int(number) else number

def objective_fingerprint(
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict] = None,
) -> str:
    """Cross-replan identity of WHAT a phase is trying to obtain.

    Phase ids and artifact names drift across replans (2cb616:
    collect_trending_40_50 → _v2 → _v3, trending_week_40_50 →
    trending_week_products_40_50) while the actual objective — "rows with
    rank 40-50, exactly 11 of them, from theresanaiforthat.com/trending/week"
    — stays identical. The key combines the normalized source URLs with the
    numeric validator features (range bounds, expected row counts); the
    normalized artifact name is the fallback when a phase carries no numeric
    target. Changing any of these means genuinely changing the objective
    (different source, different range, different artifact), which is
    exactly when the accumulated budget should reset.

    When the Lead spawns with a worker_contract override, THAT is what the
    worker actually runs — its expected_artifact/validators/texts take
    precedence over the raw phase (same merge semantics as phase_contract),
    so the gate and the execution stay in sync.
    Returns "" (no fingerprint, never gated) when nothing usable exists.
    """
    if not isinstance(phase, dict):
        return ""
    contract = worker_contract if isinstance(worker_contract, dict) else {}
    expected = dict(
        phase.get("expected_artifact")
        if isinstance(phase.get("expected_artifact"), dict) else {}
    )
    contract_expected = contract.get("expected_artifact")
    if isinstance(contract_expected, dict):
        expected.update(contract_expected)
    validators = (
        contract.get("validators")
        if isinstance(contract.get("validators"), list)
        else phase.get("validators")
    )
    sources = _normalized_source_urls(
        contract.get("worker_task") or phase.get("worker_task"),
        contract.get("objective") or phase.get("objective"),
    )
    ranges: List[List[Any]] = []
    counts: List[List[Any]] = []
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        vtype = str(validator.get("type") or "")
        if vtype == "range":
            ranges.append([
                str(validator.get("field") or ""),
                _fingerprint_num(validator.get("min")),
                _fingerprint_num(validator.get("max")),
            ])
        elif vtype in {"exact_rows", "min_rows"}:
            for key in ("value", "count", "exact", "min"):
                value = validator.get(key)
                if value is None:
                    continue
                # Same tolerance as _run_validator's _positive_int: a
                # string "11" validates identically to 11, so it must
                # fingerprint identically too.
                normalized = _fingerprint_num(value)
                if isinstance(normalized, (int, float)) and normalized:
                    counts.append([vtype, int(normalized)])
                    break
    name = str(expected.get("name") or "").strip().lower()
    name = re.sub(r"[_-]v\d+$", "", name)
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    ranges.sort()
    counts.sort()
    if ranges:
        features: List[Any] = ["ranges", sources, ranges, counts]
    elif counts and name:
        # Counts alone are too weak (two detail phases may both expect 4
        # rows); anchor them with the name.
        features = ["named_counts", sources, name, counts]
    elif name:
        features = ["name", sources, name]
    else:
        return ""
    blob = json.dumps(features, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]

def spawn_acquisition_fingerprint(
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict],
    *,
    reuse_scope: str,
    page_policy: str,
    session_key: str,
    fleet_id: str = "",
    preferred_slot_id: Optional[str] = None,
    reuse_from_worker_id: Optional[str] = None,
) -> str:
    """Stable identity of one pre-worker slot/fleet acquisition path."""

    contract = worker_contract if isinstance(worker_contract, dict) else {}
    objective = objective_fingerprint(phase, contract)
    if not objective:
        objective = str((phase or {}).get("id") or "unscoped")
    payload = {
        "objective": objective,
        "reuseScope": str(reuse_scope or ""),
        "pagePolicy": str(page_policy or ""),
        "sessionKey": str(session_key or ""),
        "fleetReference": str(fleet_id or ""),
        "needsIsolatedSession": bool(contract.get("needs_isolated_session", False)),
        "preferredSlotId": str(preferred_slot_id or ""),
        "reuseFromWorkerId": str(reuse_from_worker_id or ""),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

def spawn_acquisition_error_signature(exc: BaseException) -> str:
    """Normalize volatile ids/numbers while preserving the failure class."""

    message = " ".join(str(exc or "").strip().lower().split())
    message = re.sub(r"\b[0-9a-f]{16,}\b", "<id>", message)
    message = re.sub(r"\b\d{4,}\b", "<n>", message)
    return f"{type(exc).__name__}:{message}"[:500]

def spawn_acquisition_rejection(
    logger: RunLogger,
    *,
    acquisition_fingerprint: str,
    phase_id: Optional[str],
) -> Optional[JsonDict]:
    """Reject a route whose same startup error already exhausted its budget."""

    state = _tc().load_task_state(logger)
    ledger = state.get("spawn_acquisition_failures")
    ledger = ledger if isinstance(ledger, dict) else {}
    route_entry = ledger.get(acquisition_fingerprint)
    signatures = (
        route_entry.get("signatures") if isinstance(route_entry, dict) else {}
    )
    signatures = signatures if isinstance(signatures, dict) else {}
    for signature, raw_entry in signatures.items():
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        count = int(entry.get("count") or 0)
        retry_at = float(entry.get("retryAtEpoch") or 0.0)
        if count < _tc().SPAWN_ACQUISITION_MAX_FAILURES and retry_at > time.time():
            retry_after_ms = max(1, int((retry_at - time.time()) * 1000))
            return {
                "status": "spawn_acquisition_cooldown",
                "phaseId": str(phase_id or ""),
                "acquisitionFingerprint": acquisition_fingerprint,
                "errorSignature": str(signature),
                "failures": count,
                "maxFailures": _tc().SPAWN_ACQUISITION_MAX_FAILURES,
                "retryAfterMs": retry_after_ms,
                "tool_was_executed": False,
                "next_instruction": (
                    f"Wait {retry_after_ms} ms, then spawn the SAME phase id "
                    f"{str(phase_id or '')!r}. Do not rename or replan the phase "
                    "to bypass this Fleet acquisition cooldown."
                ),
            }
        if count >= _tc().SPAWN_ACQUISITION_MAX_FAILURES:
            return {
                "status": "spawn_infrastructure_exhausted",
                "phaseId": str(phase_id or ""),
                "acquisitionFingerprint": acquisition_fingerprint,
                "errorSignature": str(signature),
                "failures": count,
                "maxFailures": _tc().SPAWN_ACQUISITION_MAX_FAILURES,
                "tool_was_executed": False,
                "next_instruction": (
                    "Do not respawn or replan the same objective with the same"
                    " fleet/slot/session routing. This is a bounded startup"
                    " infrastructure failure, not evidence that the business"
                    " objective is infeasible. Change the routing contract or"
                    " report the infrastructure blocker."
                ),
            }
    return None

def record_spawn_acquisition_failure(
    logger: RunLogger,
    *,
    acquisition_fingerprint: str,
    phase_id: Optional[str],
    exc: BaseException,
) -> JsonDict:
    """Persist one startup failure and return its bounded diagnostic receipt."""

    state = _tc().load_task_state(logger)
    ledger = state.setdefault("spawn_acquisition_failures", {})
    if not isinstance(ledger, dict):
        ledger = {}
        state["spawn_acquisition_failures"] = ledger
    route_entry = ledger.setdefault(
        acquisition_fingerprint, {"phaseIds": [], "signatures": {}}
    )
    if not isinstance(route_entry, dict):
        route_entry = {"phaseIds": [], "signatures": {}}
        ledger[acquisition_fingerprint] = route_entry
    phase_ids = route_entry.setdefault("phaseIds", [])
    if isinstance(phase_ids, list) and phase_id and phase_id not in phase_ids:
        phase_ids.append(phase_id)
    signatures = route_entry.setdefault("signatures", {})
    if not isinstance(signatures, dict):
        signatures = {}
        route_entry["signatures"] = signatures
    signature = spawn_acquisition_error_signature(exc)
    entry = signatures.setdefault(signature, {"count": 0})
    if not isinstance(entry, dict):
        entry = {"count": 0}
        signatures[signature] = entry
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["lastError"] = str(exc)[:1000]
    entry["updated_at"] = _tc().utc_now_iso()
    is_fleet_timeout = "-32012" in str(exc) and "fleet open timeout" in str(exc).lower()
    requires_cooldown = bool(
        getattr(exc, "requires_spawn_acquisition_cooldown", False)
        or is_fleet_timeout
    )
    if requires_cooldown and int(entry["count"]) < _tc().SPAWN_ACQUISITION_MAX_FAILURES:
        entry["retryAtEpoch"] = time.time() + _tc().SPAWN_ACQUISITION_FLEET_COOLDOWN_SECONDS
    route_entry["updated_at"] = _tc().utc_now_iso()
    _tc().write_task_state(logger, state)
    count = int(entry["count"])
    return {
        "status": (
            "spawn_infrastructure_exhausted"
            if count >= _tc().SPAWN_ACQUISITION_MAX_FAILURES
            else "failed"
        ),
        "phaseId": str(phase_id or ""),
        "acquisitionFingerprint": acquisition_fingerprint,
        "errorSignature": signature,
        "failures": count,
        "maxFailures": _tc().SPAWN_ACQUISITION_MAX_FAILURES,
        "retryAfterMs": (
            _tc().SPAWN_ACQUISITION_FLEET_COOLDOWN_SECONDS * 1000
            if requires_cooldown and count < _tc().SPAWN_ACQUISITION_MAX_FAILURES
            else 0
        ),
        # This receipt is produced after the startup path actually threw. The
        # next pre-check is the first non-executed rejection.
        "tool_was_executed": True,
        "next_instruction": (
            "Do not retry this unchanged startup route; change the fleet/slot/"
            "session routing or report the infrastructure blocker. The business"
            " objective attempt budget was not consumed."
            if count >= _tc().SPAWN_ACQUISITION_MAX_FAILURES
            else (
                "One Fleet startup failure was recorded. Wait 30000 ms, then retry"
                f" the SAME phase id {str(phase_id or '')!r}; do not rename it."
                if requires_cooldown
                else "One startup infrastructure failure was recorded; one bounded retry remains."
            )
        ),
    }

def clear_spawn_acquisition_failures(
    logger: RunLogger,
    *,
    acquisition_fingerprint: str,
) -> None:
    """A successfully started worker proves this acquisition route recovered."""

    state = _tc().load_task_state(logger)
    ledger = state.get("spawn_acquisition_failures")
    if not isinstance(ledger, dict) or acquisition_fingerprint not in ledger:
        return
    ledger.pop(acquisition_fingerprint, None)
    _tc().write_task_state(logger, state)

def _record_objective_attempt(
    state: JsonDict,
    phase: Optional[JsonDict],
    phase_id: str,
    *,
    succeeded: bool,
    worker_contract: Optional[JsonDict] = None,
) -> None:
    fingerprint = objective_fingerprint(phase, worker_contract)
    if not fingerprint:
        return
    attempts = state.setdefault("objective_attempts", {})
    if succeeded:
        attempts.pop(fingerprint, None)
        return
    entry = attempts.get(fingerprint)
    if not isinstance(entry, dict):
        entry = {"count": 0, "phaseIds": []}
        attempts[fingerprint] = entry
    entry["count"] = int(entry.get("count") or 0) + 1
    phase_ids = entry.setdefault("phaseIds", [])
    if phase_id not in phase_ids:
        phase_ids.append(phase_id)
    entry["updated_at"] = _tc().utc_now_iso()

def build_attempt_digest(
    worker_result: JsonDict,
    *,
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict],
    task: str = "",
    result_contract: str = "",
) -> JsonDict:
    artifact_validation = (
        worker_result.get("artifactValidation")
        if isinstance(worker_result.get("artifactValidation"), dict)
        else {}
    )
    classification = _classification_from_worker_result(worker_result)
    row_count = _attempt_row_count(worker_result, artifact_validation)
    artifact_paths = _attempt_artifact_paths(worker_result, artifact_validation)
    trace_path = str(worker_result.get("tracePath") or "")
    status = str(worker_result.get("status") or "unknown")
    status_category = str(worker_result.get("statusCategory") or "unknown")
    validated_status = str(worker_result.get("validatedStatus") or "")
    digest: JsonDict = {
        "status": status,
        "statusCategory": status_category,
        "validatedStatus": validated_status,
        "classification": classification,
        "rowCount": row_count,
        "artifactPaths": artifact_paths,
        "tracePath": trace_path,
        "blocker": _attempt_primary_blocker(worker_result),
        "failureSignature": failure_signature_from_result(worker_result),
        "contractHash": _tc().contract_hash_for_phase(
            phase,
            worker_contract,
            task=task,
            result_contract=result_contract,
        ),
    }
    return trim_large_strings(_strip_volatile_handles(digest), 4000)

def failure_signature_from_result(worker_result: JsonDict) -> List[Any]:
    classification = _classification_from_worker_result(worker_result)
    artifact_validation = (
        worker_result.get("artifactValidation")
        if isinstance(worker_result.get("artifactValidation"), dict)
        else {}
    )
    status = str(worker_result.get("status") or "")
    category = str(classification.get("category") or "").strip() if classification else ""
    if not category and status and status not in {"done", "partial"}:
        category = f"status:{status}"
    validation_failure_type = _primary_validation_failure_type(
        artifact_validation,
        classification,
    )
    hint_key = _classification_hint_key(classification)
    primary_blocker_method = (
        str(classification.get("method") or "").strip()
        if isinstance(classification, dict)
        else ""
    )
    progress_reason = _stall_signal_reason(worker_result)
    return [
        category or None,
        validation_failure_type or None,
        hint_key or None,
        primary_blocker_method or None,
        progress_reason or None,
    ]

def _classification_from_worker_result(worker_result: JsonDict) -> JsonDict:
    artifact_validation = (
        worker_result.get("artifactValidation")
        if isinstance(worker_result.get("artifactValidation"), dict)
        else {}
    )
    classification = artifact_validation.get("classification")
    if isinstance(classification, dict):
        return dict(classification)
    result_levels = (
        worker_result.get("resultLevels")
        if isinstance(worker_result.get("resultLevels"), dict)
        else {}
    )
    l1 = result_levels.get("l1") if isinstance(result_levels.get("l1"), dict) else {}
    failure = l1.get("failureClassification")
    if isinstance(failure, dict):
        return dict(failure)
    if isinstance(failure, str) and failure.strip():
        return {"category": failure.strip(), "source": "resultLevels.l1"}
    return {}

def _attempt_row_count(worker_result: JsonDict, artifact_validation: JsonDict) -> int:
    try:
        return int(artifact_validation.get("rowCount") or 0)
    except (TypeError, ValueError):
        pass
    result_levels = (
        worker_result.get("resultLevels")
        if isinstance(worker_result.get("resultLevels"), dict)
        else {}
    )
    l2 = result_levels.get("l2") if isinstance(result_levels.get("l2"), dict) else {}
    data = l2.get("data") if isinstance(l2.get("data"), dict) else {}
    try:
        return int(data.get("totalExtractedRows") or 0)
    except (TypeError, ValueError):
        return 0

def _attempt_artifact_paths(
    worker_result: JsonDict,
    artifact_validation: JsonDict,
) -> List[str]:
    paths: List[str] = []
    for raw_list in (
        worker_result.get("artifacts"),
        artifact_validation.get("artifacts"),
        artifact_validation.get("allExtractionArtifacts"),
    ):
        if not isinstance(raw_list, list):
            continue
        for item in raw_list:
            path = str(item or "").strip()
            if path and path not in paths:
                paths.append(path)
    return paths[:20]

def _attempt_primary_blocker(worker_result: JsonDict) -> Optional[JsonDict]:
    result_levels = (
        worker_result.get("resultLevels")
        if isinstance(worker_result.get("resultLevels"), dict)
        else {}
    )
    l2 = result_levels.get("l2") if isinstance(result_levels.get("l2"), dict) else {}
    blockers = l2.get("blockers") if isinstance(l2.get("blockers"), list) else []
    for blocker in blockers:
        if isinstance(blocker, dict):
            return trim_large_strings(_strip_volatile_handles(blocker), 1000)
    artifact_validation = worker_result.get("artifactValidation")
    if isinstance(artifact_validation, dict) and artifact_validation.get("status") == "failed":
        return {
            "type": "artifact_validation_failed",
            "classification": _classification_from_worker_result(worker_result),
        }
    return None

def _primary_validation_failure_type(
    artifact_validation: JsonDict,
    classification: JsonDict,
) -> str:
    failure_types = classification.get("failureTypes") if isinstance(classification, dict) else None
    if isinstance(failure_types, list):
        values = sorted(str(item) for item in failure_types if str(item).strip())
        if values:
            return values[0]
    failures = artifact_validation.get("failures")
    if isinstance(failures, list):
        for failure in failures:
            if isinstance(failure, dict) and str(failure.get("type") or "").strip():
                return str(failure.get("type")).strip()
    return ""

def _classification_hint_key(classification: JsonDict) -> str:
    if not isinstance(classification, dict):
        return ""
    for key in (
        "expectedArtifactName",
        "workerStatus",
        "source",
        "task_type",
    ):
        value = str(classification.get(key) or "").strip()
        if value:
            return f"{key}={value[:120]}"
    return ""

def _stall_signal_reason(worker_result: JsonDict) -> str:
    trace_summary = (
        worker_result.get("traceSummary")
        if isinstance(worker_result.get("traceSummary"), dict)
        else {}
    )
    loop_nudges = trace_summary.get("loopNudges")
    if isinstance(loop_nudges, list):
        for item in reversed(loop_nudges):
            if isinstance(item, dict):
                reason = str(item.get("reason") or "").strip()
                action = str(item.get("action") or "").strip()
                if reason and action:
                    return f"loop_nudge:{action}:{reason}"
                if reason:
                    return f"loop_nudge:{reason}"
    return ""

def _attempt_digest_is_failure(digest: JsonDict) -> bool:
    if not isinstance(digest, dict):
        return False
    status = str(digest.get("status") or "")
    if status == "partial":
        return False
    validated_status = str(digest.get("validatedStatus") or "")
    if validated_status == "validation_failed":
        return True
    status_category = str(digest.get("statusCategory") or "")
    return status_category in {"recoverable", "fatal"}

def _strip_volatile_handles(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: JsonDict = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _tc().VOLATILE_HANDLE_KEYS:
                continue
            cleaned[key_text] = _strip_volatile_handles(item)
        return cleaned
    if isinstance(value, list):
        return [
            _strip_volatile_handles(item)
            for item in value
            if not _is_volatile_string(item)
        ]
    if _is_volatile_string(value):
        return None
    return value

def _is_volatile_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if _tc().AXTREE_ID_ANYWHERE_RE.search(text):
        return True
    lowered = text.lower()
    return lowered.startswith("pageid=") or lowered.startswith("fleetid=")
