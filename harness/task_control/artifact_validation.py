"""
harness.task_control.artifact_validation - Worker artifact validation and classification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import List
from typing import Optional
from typing import Tuple
from harness.constants import WORKER_STATUS_API_CONTRACT_ERROR
from harness.constants import WORKER_STATUS_BLOCKED_BY_CHALLENGE
from harness.constants import WORKER_STATUS_HITL_REQUIRED
from harness.constants import WORKER_STATUS_HITL_TIMEOUT
from harness.constants import WORKER_STATUS_HITL_WAITING
from harness.constants import WORKER_STATUS_PAGE_CRASHED
from harness.constants import WORKER_STATUS_PAGE_SETTLED_AFTER_HITL
from harness.evidence.artifact_evidence import FILE_VALIDATOR_TYPES
from harness.evidence.artifact_evidence import detect_blocker_data_rows
from harness.evidence.artifact_evidence import detect_near_stub_rows
from harness.evidence.artifact_evidence import detect_placeholder_rows
from harness.evidence.artifact_evidence import detect_stub_rows
from harness.utils import JsonDict
from harness.utils import RunLogger

def _tc():
    import harness.task_control as tc

    return tc

def _empty_array_observations(
    rows: List[JsonDict],
    expected: JsonDict,
) -> List[JsonDict]:
    """Report rows whose declared array fields came back empty, as facts.

    These used to fail phase validation. Two problems with that: the detector
    never consulted `allow_empty`, so it overrode the very contract the plan
    declared; and where no `field_nonempty` validator existed it invented a
    requirement out of a threshold ("two or more empty arrays looks like a
    stub"). In task a608b5e7 a worker spent steps reading the run log to work
    out how to satisfy it, which is the shape of a harness the model has to
    reverse-engineer rather than a task it can do.

    The contract still decides: `field_nonempty` rejects mechanically and
    `allow_empty` accepts. What is left over — an empty array nobody declared
    either way — is counterevidence the model reads, not a verdict.
    """
    return [
        *detect_stub_rows(rows, expected),
        *detect_near_stub_rows(rows, expected),
    ]

def validate_worker_artifacts(
    *,
    contract: Optional[JsonDict],
    artifacts: List[str],
    task_dir: Path,
    attempt_artifacts: Optional[List[str]] = None,
    prior_artifacts: Optional[List[str]] = None,
    file_evidence: Optional[List[JsonDict]] = None,
    evidence_sink: Optional[Any] = None,
    logger: Optional[RunLogger] = None,
) -> JsonDict:
    """Validate a worker's artifacts against its contract.

    ``evidence_sink`` receives one structured payload describing the rows this
    call already loaded and merged, including why they may not be trustworthy.
    It exists so the shadow evaluator can reuse that work instead of re-reading
    every artifact, and it deliberately does not appear in the returned result:
    the rows can be large and that result is logged and partly surfaced to the
    model.
    """
    if not contract:
        return {"status": "skipped", "reason": "no worker_contract"}

    expected = contract.get("expected_artifact") if isinstance(contract, dict) else {}
    if not isinstance(expected, dict):
        expected = {}
    validators = contract.get("validators") if isinstance(contract, dict) else []
    if not isinstance(validators, list):
        validators = []
    if bool(contract.get("validators_normalized", False)):
        validators = [
            dict(validator) for validator in validators
            if isinstance(validator, dict)
        ]
    else:
        validators = _tc()._normalize_validators(
            expected,
            validators,
            [],
            phase_id=str(contract.get("phase_id") or "worker"),
        )

    row_validators = [
        validator for validator in validators
        if str(validator.get("type") or "") not in FILE_VALIDATOR_TYPES
    ]
    file_validators = [
        validator for validator in validators
        if str(validator.get("type") or "") in FILE_VALIDATOR_TYPES
    ]
    extraction_artifacts = [
        path for path in artifacts
        if "/artifacts/extractions/" in str(path)
    ]
    extraction_attempt_artifacts = [
        path for path in (attempt_artifacts or [])
        if "/artifacts/extractions/" in str(path)
    ]
    prior_extraction_artifacts = [
        path for path in (prior_artifacts or [])
        if "/artifacts/extractions/" in str(path)
    ]
    all_extraction_artifacts = _tc()._unique_paths([
        *extraction_artifacts,
        *extraction_attempt_artifacts,
        *prior_extraction_artifacts,
    ])
    failures: List[JsonDict] = []
    loaded = _tc()._load_extraction_artifacts(extraction_artifacts, task_dir, logger)
    loaded_attempts = _tc()._load_extraction_artifacts(
        [
            path for path in extraction_attempt_artifacts
            if path not in extraction_artifacts
        ],
        task_dir,
        logger,
    )
    loaded_prior = _tc()._load_extraction_artifacts(
        [
            path for path in prior_extraction_artifacts
            if path not in extraction_artifacts
            and path not in extraction_attempt_artifacts
        ],
        task_dir,
    )
    expected_name = str(expected.get("name") or "").strip()
    candidates = [
        item for item in loaded
        if not expected_name or item.get("payload", {}).get("name") == expected_name
    ]
    attempt_candidates = [
        item for item in loaded_attempts
        if not expected_name or item.get("payload", {}).get("name") == expected_name
    ]
    prior_candidates = [
        item for item in loaded_prior
        if not expected_name or item.get("payload", {}).get("name") == expected_name
    ]

    has_row_contract = bool(
        expected.get("name")
        or expected.get("fields")
        or expected.get("required_fields")
        or row_validators
    )
    must_record = bool(contract.get("must_record_extraction", has_row_contract))
    if must_record and not candidates and not attempt_candidates and not prior_candidates:
        failures.append({
            "type": "artifact_required",
            "message": (
                f"expected record_extraction artifact"
                + (f" named {expected_name!r}" if expected_name else "")
            ),
            "availableArtifacts": all_extraction_artifacts,
        })

    # Same-name artifact selection (fa86c5f6 fix): within the first non-empty
    # tier, order candidates best-first (no schemaWarnings > more rows >
    # recorded later) and pick the FIRST one that passes every validator; if
    # none passes, keep the heuristic-best and report ITS failures. The old
    # first-recorded pick validated a schema-flagged batch dump into a bogus
    # validation_failed while a clean complete artifact sat right next to it.
    def _order_best_first(items: List[JsonDict]) -> List[JsonDict]:
        def sort_key(pair):
            idx, item = pair
            payload = item.get("payload") or {}
            schema_warnings = payload.get("schemaWarnings")
            has_warnings = 1 if isinstance(schema_warnings, list) and schema_warnings else 0
            rows_list = payload.get("rows")
            n_rows = len(rows_list) if isinstance(rows_list, list) else 0
            return (has_warnings, -n_rows, -idx)
        return [item for _, item in sorted(enumerate(items), key=sort_key)]

    def _evaluate_candidate(
        item: Optional[JsonDict],
    ) -> Tuple[List[JsonDict], List[JsonDict]]:
        cand_failures: List[JsonDict] = []
        cand_rows: List[JsonDict] = []
        if item:
            payload = item.get("payload") or {}
            schema_warnings = payload.get("schemaWarnings")
            if isinstance(schema_warnings, list) and schema_warnings:
                cand_failures.append({
                    "type": "schema",
                    "message": "selected record_extraction artifact has schemaWarnings",
                    "path": item.get("path"),
                    "schemaWarnings": schema_warnings[:5],
                })
            raw_rows = payload.get("rows")
            if isinstance(raw_rows, list):
                cand_rows = [row for row in raw_rows if isinstance(row, dict)]
            else:
                cand_failures.append({
                    "type": "schema",
                    "message": "selected artifact has no rows array",
                    "path": item.get("path"),
                })
        for validator in row_validators:
            cand_failures.extend(_tc()._run_validator(validator, cand_rows))
        cand_failures.extend(detect_placeholder_rows(cand_rows))
        cand_failures.extend(detect_blocker_data_rows(cand_rows, expected))
        return cand_failures, cand_rows

    if expected_name:
        tier = candidates or attempt_candidates or prior_candidates
    else:
        tier = loaded or loaded_attempts or loaded_prior
    ordered = _order_best_first(tier)
    selected = ordered[0] if ordered else None
    selected_failures, rows = _evaluate_candidate(selected)
    if selected_failures:
        for item in ordered[1:]:
            alt_failures, alt_rows = _evaluate_candidate(item)
            if not alt_failures:
                selected, selected_failures, rows = item, alt_failures, alt_rows
                break
    failures.extend(selected_failures)
    warnings = _empty_array_observations(rows, expected)

    cumulative = False
    cumulative_sources: List[str] = []
    merged_rows: List[JsonDict] = []
    merged_sources: List[str] = []
    merged_provenance: List[JsonDict] = []
    observer_merge_error = ""
    authoritative_merge_attempted = False
    if failures:
        # This is the pre-existing authoritative recovery path. Keep its error
        # semantics unchanged: a broken cumulative validator is a validation
        # failure, not an observer failure.
        authoritative_merge_attempted = True
        cumulative_rows, cumulative_sources, cumulative_failures, merged_provenance = (
            _tc()._validate_cumulative_artifacts(
                validators=row_validators,
                expected=expected,
                candidates=[
                    *prior_candidates,
                    *attempt_candidates,
                    *candidates,
                ],
            )
        )
        merged_rows, merged_sources = cumulative_rows, cumulative_sources
        if failures and cumulative_rows and not cumulative_failures:
            rows = cumulative_rows
            failures = []
            warnings = _empty_array_observations(rows, expected)
            cumulative = True

    file_failures: List[JsonDict] = []
    for validator in file_validators:
        file_failures.extend(_tc()._run_file_validator(
            validator,
            artifacts=artifacts,
            evidence=file_evidence or [],
            rows=rows,
        ))
    failures.extend(file_failures)

    if (
        evidence_sink is not None
        and failures
        and not authoritative_merge_attempted
    ):
        # Row validation passed, but a later file validator failed. Only this
        # narrow path can need a cumulative universe solely for the shadow
        # observer. A fully passing worker uses the selected candidate directly
        # and avoids an otherwise discarded O(cumulative rows) merge. Keep the
        # observer merge isolated: it may suppress its own worker-final verdict,
        # but it may never change authoritative validation.
        try:
            cumulative_rows, cumulative_sources, _observer_failures, merged_provenance = (
                _tc()._validate_cumulative_artifacts(
                    validators=row_validators,
                    expected=expected,
                    candidates=[
                        *prior_candidates,
                        *attempt_candidates,
                        *candidates,
                    ],
                )
            )
            merged_rows, merged_sources = cumulative_rows, cumulative_sources
        except Exception as exc:
            observer_merge_error = f"{type(exc).__name__}: {exc}"

    status = "done" if not failures else "failed"
    result_artifacts = cumulative_sources if cumulative else (
        [selected.get("path")] if selected else (
            _tc()._unique_paths(artifacts) if file_validators else []
        )
    )
    valid_extraction_artifacts = cumulative_sources if cumulative else (
        [selected.get("path")] if selected and not failures else []
    )
    if evidence_sink is not None:
        if observer_merge_error:
            try:
                evidence_sink({
                    "observerError": {
                        "stage": "worker_final_cumulative_merge",
                        "error": observer_merge_error,
                    },
                })
            except Exception:
                pass
            evidence_sink = None
    if evidence_sink is not None:
        # Hand over the identity-merged rows whenever a merge was possible, even
        # though an incomplete merge is not adopted as authoritative above. A
        # 10-row attempt on top of a 9-row prior attempt is exactly the trusted
        # partial the evidence ledger exists to see; passing only this attempt's
        # rows would hide it.
        #
        # When no merge was possible the rows come from the single selected
        # candidate, which may be a schema-warning artifact the authoritative
        # path rejected. Say so explicitly: the online path already refuses
        # those saves, and an observer that trusts them here would be judging a
        # different universe than the one it is supposed to shadow.
        # Mirror the authoritative choice rather than always merging. When this
        # attempt alone satisfied the contract, the merged universe would drag
        # in earlier failed attempts the authority deliberately ignored, and the
        # observer would report a would-block for a run that actually succeeded.
        # Only when the authority failed is the merged set the interesting one,
        # because that is where a trusted partial hides.
        # Three cases, and only the first may use the single selected candidate:
        #   passed without merging  -> selected rows, so a clean run is not
        #                              polluted by earlier failed attempts
        #   passed via the merge    -> merged rows, or provenance would credit
        #                              every merged row to the current artifact
        #   failed                  -> merged rows, because that is where a
        #                              trusted partial hides
        # Deciding this from `failures` alone was wrong in the second case (a
        # successful merge clears them); deciding it from `cumulative` alone was
        # wrong in the third (an incomplete merge never sets it).
        use_merged = bool(merged_rows) and bool(cumulative or failures)
        sink_rejection = ""
        selected_payload = selected.get("payload", {}) if selected else {}
        if use_merged:
            sink_rows = merged_rows
            sink_paths = merged_sources
            sink_row_paths = [
                str(item.get("path") or "") for item in merged_provenance
            ]
            sink_row_scopes = [item.get("scope") or {} for item in merged_provenance]
            sink_scope = (
                sink_row_scopes[0]
                if sink_row_scopes
                and all(item == sink_row_scopes[0] for item in sink_row_scopes)
                else {}
            )
        else:
            sink_rows = rows
            sink_paths = result_artifacts
            selected_path = str(selected.get("path") or "") if selected else ""
            sink_row_paths = [selected_path] * len(rows)
            scope = selected_payload.get("evidenceContext")
            sink_scope = dict(scope) if isinstance(scope, dict) else {}
            sink_row_scopes = [sink_scope] * len(rows)
            if selected_payload.get("schemaWarnings"):
                sink_rejection = "schema_warning_artifact"
        try:
            evidence_sink({
                "rows": sink_rows,
                "sourcePaths": sink_paths,
                "rowArtifactIds": sink_row_paths,
                # Per-row scope, not one summary value. The authoritative merge
                # does not bucket by scope, so a merged set can legitimately mix
                # page/auth generations; collapsing that into a single reported
                # scope relabels older rows as fresh.
                "rowScopes": sink_row_scopes,
                "evidenceContext": sink_scope,
                "filesRead": len(all_extraction_artifacts),
                "rejectedReason": sink_rejection,
                "cumulative": use_merged,
            })
        except Exception:  # an observer must never fail authoritative validation
            pass

    result = {
        "status": status,
        "phase_id": contract.get("phase_id"),
        "expectedArtifact": expected,
        "rowCount": len(rows),
        "artifacts": result_artifacts,
        "allExtractionArtifacts": all_extraction_artifacts,
        "validExtractionArtifacts": valid_extraction_artifacts,
        "attemptExtractionArtifacts": extraction_attempt_artifacts,
        "priorExtractionArtifacts": prior_extraction_artifacts,
        "fileArtifacts": _tc()._unique_paths([
            path for path in artifacts
            if "/artifacts/extractions/" not in str(path)
        ]),
        "fileEvidenceCount": len(file_evidence or []),
        "failures": failures,
    }
    if cumulative:
        result["cumulative"] = True
        result["sourceArtifactCount"] = len(cumulative_sources)
    if warnings:
        result["warnings"] = warnings
    if failures:
        result["classification"] = classify_artifact_validation_failures(
            failures,
            rows=rows,
            expected_artifact=expected,
        )
    return result

def classify_artifact_validation_failures(
    failures: List[JsonDict],
    *,
    rows: Optional[List[JsonDict]] = None,
    expected_artifact: Optional[JsonDict] = None,
) -> JsonDict:
    failure_types = {
        str(item.get("type") or "")
        for item in failures
        if isinstance(item, dict)
    }
    if "data_placeholder" in failure_types:
        category = "data_placeholder"
        hint = "Observed rows look like placeholder or stub content; reveal/load the real content or report absence."
    elif "artifact_required" in failure_types:
        category = "data_missing"
        hint = "No matching record_extraction artifact was produced; collect and save the target rows."
    elif failure_types & {"schema", "required_fields", "field_provenance"}:
        category = "schema_mismatch"
        hint = "Rows exist but do not match the expected artifact schema; reshape from evidence before re-scraping."
    elif failure_types & {"min_rows", "max_rows", "exact_rows"}:
        category = "data_wrong_shape"
        hint = "The number of rows does not satisfy the expected shape; adjust range/materialization or scope."
    elif failure_types & {
        "unique",
        "url_pattern",
        "allowed_domain",
        "set_equals",
        "range",
        "field_pattern",
        "cross_field_contains",
        "action_outcome",
        "field_nonempty",
    }:
        category = "data_wrong_value"
        hint = "Rows were saved, but one or more values failed semantic validators."
    elif failure_types & FILE_VALIDATOR_TYPES:
        category = "file_validation_failed"
        hint = "The file action ran, but completion, selection, confirmation, or on-disk integrity evidence is insufficient."
    else:
        category = "data_wrong_value" if failures else "unknown"
        hint = "Validation failed; inspect failures and choose a different recovery path."
    return {
        "category": category,
        "hint": hint,
        "failureTypes": sorted(ft for ft in failure_types if ft),
        "rowCount": len(rows or []),
        "expectedArtifactName": (
            str((expected_artifact or {}).get("name") or "")
            if isinstance(expected_artifact, dict)
            else ""
        ),
    }

def classification_for_worker_status(status: str) -> Optional[JsonDict]:
    text = str(status or "")
    if text in {
        WORKER_STATUS_BLOCKED_BY_CHALLENGE,
        WORKER_STATUS_HITL_REQUIRED,
        WORKER_STATUS_HITL_WAITING,
        WORKER_STATUS_HITL_TIMEOUT,
        WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
    }:
        return {
            "category": "blocked_user_action_required",
            "hint": "Human action or challenge resolution is required before retrying this phase.",
            "workerStatus": text,
        }
    if text in {WORKER_STATUS_PAGE_CRASHED, WORKER_STATUS_API_CONTRACT_ERROR}:
        return {
            "category": "blocked_infrastructure",
            "hint": "Infrastructure or browser state failed; rebuild the page/fleet or switch platform path.",
            "workerStatus": text,
        }
    return None
