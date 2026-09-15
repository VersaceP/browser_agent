"""
harness.task_control.cohorts - Batch row materialization and cohort/fast-path selection contracts.
"""

from __future__ import annotations

import json
import copy
import hashlib
from pathlib import Path
from typing import AbstractSet
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from urllib.parse import urlsplit
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.evidence.artifact_evidence import VALIDATOR_SCOPE
from harness.task_types import normalize_task_type
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import load_task_json

def _tc():
    import harness.task_control as tc

    return tc


_AUTO_BIND_URL_FIELDS = (
    "url",
    "detailUrl",
    "detail_url",
    "productUrl",
    "product_url",
    "href",
    "link",
)
_AUTO_BIND_ID_FIELDS = (
    "id",
    "itemId",
    "item_id",
    "productId",
    "product_id",
    "asin",
    "sku",
)
_AUTO_BIND_EXCLUDED_STAGES = frozenset({
    "computed_relationship",
    "form_interaction",
})
_AUTO_BIND_TASK_TYPES = frozenset({"web_search", "web_scrape"})


def _auto_bind_exact_row_count(
    expected: JsonDict,
    validators: Any,
) -> Optional[int]:
    """Return a declared *exact* row count, never infer one from prose."""

    exact = expected.get("exact_rows")
    if isinstance(exact, int) and not isinstance(exact, bool) and exact > 0:
        return exact
    count_range = expected.get("count_range")
    if (
        isinstance(count_range, list)
        and len(count_range) >= 2
        and isinstance(count_range[0], int)
        and isinstance(count_range[1], int)
        and not isinstance(count_range[0], bool)
        and not isinstance(count_range[1], bool)
        and count_range[0] == count_range[1]
        and count_range[0] > 0
    ):
        return int(count_range[0])
    if isinstance(validators, list):
        for validator in validators:
            if not isinstance(validator, dict):
                continue
            if str(validator.get("type") or "") != "exact_rows":
                continue
            value = validator.get("value", validator.get("count"))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


def _auto_bind_expected_fields(expected: JsonDict) -> Set[str]:
    raw = expected.get("required_fields")
    if not isinstance(raw, list) or not raw:
        raw = expected.get("fields")
    return {
        str(name).strip()
        for name in field_names_from_specs(raw if isinstance(raw, list) else [])
        if str(name).strip()
    }


def _auto_bind_identity_field(
    rows: List[JsonDict],
    expected_fields: Set[str],
    validators: Any,
) -> Optional[str]:
    """Find a stable key that the downstream artifact explicitly preserves."""

    candidates: List[str] = []
    if isinstance(validators, list):
        for validator in validators:
            if not isinstance(validator, dict) or str(validator.get("type") or "") != "unique":
                continue
            raw = validator.get("fields")
            if isinstance(raw, list) and len(raw) == 1:
                field = str(raw[0]).strip()
                # A reviewed unique validator is stronger than a field-name
                # convention.  Conventional URL/ID names remain a fallback,
                # but a generic stable key (for example sourceProductKey)
                # must not be rejected merely because it is not called "id".
                if field in expected_fields:
                    candidates.append(field)
    candidates.extend(
        field for field in _AUTO_BIND_URL_FIELDS + _AUTO_BIND_ID_FIELDS
        if field in expected_fields
    )
    seen: Set[str] = set()
    for field in candidates:
        if not field or field in seen:
            continue
        seen.add(field)
        values = [row.get(field) for row in rows]
        if any(value is None or isinstance(value, (dict, list)) or not str(value).strip() for value in values):
            continue
        normalized = [str(value).strip() for value in values]
        if len(set(normalized)) != len(normalized):
            continue
        if field in _AUTO_BIND_URL_FIELDS:
            try:
                if not all(
                    urlsplit(value).scheme in {"http", "https"}
                    and bool(urlsplit(value).netloc)
                    for value in normalized
                ):
                    continue
            except ValueError:
                continue
        return field
    return None


def _latest_validated_extraction_paths(
    logger: RunLogger,
    phase_id: str,
) -> List[str]:
    state = _tc().load_task_state(logger)
    phases = state.get("phases") if isinstance(state, dict) else None
    phase_state = phases.get(phase_id) if isinstance(phases, dict) else None
    attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else None
    if not isinstance(attempts, list):
        return []
    for attempt in reversed(attempts):
        if not isinstance(attempt, dict) or not _tc()._attempt_was_validated_done(attempt):
            continue
        validation = attempt.get("validation")
        candidates: List[Any] = []
        if isinstance(validation, dict):
            for key in ("validExtractionArtifacts", "artifacts", "attemptExtractionArtifacts"):
                value = validation.get(key)
                if isinstance(value, list) and value:
                    candidates.extend(value)
                    break
        if not candidates:
            digest = attempt.get("attemptDigest")
            if isinstance(digest, dict) and isinstance(digest.get("artifactPaths"), list):
                candidates.extend(digest.get("artifactPaths") or [])
        return list(dict.fromkeys(
            str(path) for path in candidates
            if "/artifacts/extractions/" in str(path)
        ))
    return []


def assess_batch_source_binding(
    logger: RunLogger,
    *,
    phase: JsonDict,
    plan: Optional[JsonDict],
    worker_contract: JsonDict,
) -> JsonDict:
    """Classify whether an ordinary downstream batch source is derivable.

    This is deliberately conservative. It only binds a phase when the plan
    explicitly names exactly one source artifact and its producing phase, that
    phase is an explicit scheduling dependency, its latest attempt has one
    matching extraction artifact, the downstream contract declares the same
    exact row count, and every row carries a unique identity field preserved
    by the downstream artifact.  Equal row counts, plan order, and matching
    field names never prove a cohort relationship. Every failed proof is an
    optional optimization miss, not a new execution gate: the ordinary
    Lead/worker path remains available. Checkpoint/replan, joins/aggregates,
    and form interaction phases remain explicit Lead responsibilities.
    """

    if not isinstance(worker_contract, dict) or any(
        key in worker_contract for key in ("batch_source", "cohort_source", "batch_rows")
    ):
        return {"status": "not_applicable"}
    if not isinstance(plan, dict):
        return {"status": "not_applicable"}
    if normalize_task_type(phase.get("task_type")) not in _AUTO_BIND_TASK_TYPES:
        return {"status": "not_applicable", "reason": "task_type_not_read_only"}
    if str(phase.get("stage_hint") or "") in _AUTO_BIND_EXCLUDED_STAGES:
        return {"status": "not_applicable"}
    # A join/fanout is not a simple one-source, row-preserving transformation.
    # Do not infer a source merely because a count happens to match.
    if phase.get("join") is not None or phase.get("fanout_from") is not None:
        return {"status": "not_applicable", "reason": "non_rowwise_plan_shape"}
    batch_policy = worker_contract.get("batch_policy")
    if not isinstance(batch_policy, dict):
        batch_policy = {}
    if (
        worker_contract.get("replan_checkpoint_id")
        or batch_policy.get("requires_isolation_per_row") is True
        or phase.get("execution_role")
    ):
        return {"status": "not_applicable"}

    references = phase.get("input_artifacts")
    if not isinstance(references, list) or not references:
        return {"status": "not_applicable", "reason": "input_artifact_not_declared"}
    if len(references) != 1 or not isinstance(references[0], dict):
        return {"status": "not_applicable", "reason": "input_artifact_count_not_one"}
    reference = references[0]
    dependency_id = str(reference.get("phase_id") or "").strip()
    declared_artifact_name = str(reference.get("artifact_name") or "").strip()
    if not dependency_id or not declared_artifact_name:
        return {"status": "not_applicable", "reason": "input_artifact_invalid"}
    dependencies = _tc()._phase_dependency_ids(phase)
    # Omitted depends_on means serial scheduling, not an assertion that an
    # earlier phase supplied the rows. The source reference independently
    # proves lineage, but must still be scheduled explicitly.
    if dependencies is None or dependency_id not in dependencies:
        return {"status": "not_applicable", "reason": "input_artifact_not_scheduled"}
    dependency_phase = next(
        (item for item in (plan.get("phases") or []) if isinstance(item, dict) and str(item.get("id") or "") == dependency_id),
        None,
    )
    if not isinstance(dependency_phase, dict):
        return {"status": "not_applicable", "reason": "dependency_not_in_plan"}
    dependency_expected = dependency_phase.get("expected_artifact")
    if (
        not isinstance(dependency_expected, dict)
        or str(dependency_expected.get("name") or "").strip()
        != declared_artifact_name
    ):
        return {"status": "not_applicable", "reason": "input_artifact_plan_mismatch"}
    # The accepted phase is the output-contract authority.  The effective
    # worker contract can include a spawn-time Lead override; it must never
    # alter the row-count or identity proof for automatic binding.
    if phase.get("validators_normalized") is not True:
        return {"status": "not_applicable", "reason": "phase_not_normalized"}
    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    reviewed_validators = phase.get("validators")
    target_count = _auto_bind_exact_row_count(expected, reviewed_validators)
    # A one-row downstream result can be a summary, join, or other semantic
    # reduction.  Leave that shape to the Lead rather than guessing it is a
    # detail phase.
    if target_count is None or target_count <= 1:
        return {"status": "not_applicable"}
    paths = _latest_validated_extraction_paths(logger, dependency_id)
    if len(paths) != 1:
        return {
            "status": "not_applicable",
            "phaseId": str(phase.get("id") or ""),
            "dependencyPhaseId": dependency_id,
            "reason": "validated_dependency_artifact_not_unique",
            "candidateCount": len(paths),
        }
    payload = load_task_json(logger, paths[0])
    if (
        not isinstance(payload, dict)
        or str(payload.get("name") or "").strip() != declared_artifact_name
    ):
        return {
            "status": "not_applicable",
            "phaseId": str(phase.get("id") or ""),
            "dependencyPhaseId": dependency_id,
            "reason": "validated_input_artifact_name_mismatch",
        }
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        return {
            "status": "not_applicable",
            "phaseId": str(phase.get("id") or ""),
            "dependencyPhaseId": dependency_id,
            "reason": "validated_dependency_artifact_has_no_object_rows",
        }
    source_rows = [dict(row) for row in rows]
    if target_count != len(source_rows):
        return {
            "status": "not_applicable",
            "phaseId": str(phase.get("id") or ""),
            "dependencyPhaseId": dependency_id,
            "reason": "upstream_row_count_does_not_match_expected_artifact",
            "upstreamRowCount": len(source_rows),
            "expectedRowCount": target_count,
        }
    identity_field = _auto_bind_identity_field(
        source_rows,
        _auto_bind_expected_fields(expected),
        reviewed_validators,
    )
    if not identity_field:
        return {
            "status": "not_applicable",
            "phaseId": str(phase.get("id") or ""),
            "dependencyPhaseId": dependency_id,
            "reason": "upstream_artifact_has_no_unique_preserved_identity_field",
        }
    return {
        "status": "derived",
        "batch_source": {
            "artifact_name": declared_artifact_name,
            "_artifact_path": str(paths[0]),
            "identity_field": identity_field,
            "selector": {"field": identity_field},
        },
        "dependencyPhaseId": dependency_id,
        "upstreamRowCount": len(source_rows),
        "identityField": identity_field,
    }


def derive_batch_source_from_upstream_artifact(
    logger: RunLogger,
    *,
    phase: JsonDict,
    plan: Optional[JsonDict],
    worker_contract: JsonDict,
) -> Optional[JsonDict]:
    """Compatibility helper returning only the derived source, if any."""

    decision = assess_batch_source_binding(
        logger,
        phase=phase,
        plan=plan,
        worker_contract=worker_contract,
    )
    source = decision.get("batch_source") if isinstance(decision, dict) else None
    return source if isinstance(source, dict) else None

def materialize_batch_rows_from_source(
    logger: RunLogger,
    *,
    phase: JsonDict,
    worker_contract: JsonDict,
) -> Optional[JsonDict]:
    """Bind batch_rows to a validated extraction artifact before spawning.

    The Lead declares selection intent; it never copies potentially large row
    payloads through model context.  Only paths already present in the task's
    validated artifact ledger are eligible.
    """

    source = worker_contract.get("batch_source")
    accepted_contract = phase.get("worker_contract")
    accepted_source = (
        accepted_contract.get("batch_source")
        if isinstance(accepted_contract, dict) else None
    )
    if isinstance(accepted_source, dict):
        # Spawn-time operational overrides cannot remove or replace the source
        # and selection authorized by the accepted plan. Internal path pins
        # are checked against the producer ledger below, not as model fields.
        def public_source(value):
            return {k: v for k, v in value.items() if k != "_artifact_path"}
        if not isinstance(source, dict) or public_source(source) != public_source(accepted_source):
            return {
                "status": "batch_source_contract_mismatch",
                "phaseId": str(phase.get("id") or ""),
                "tool_was_executed": False,
                "next_instruction": (
                    "Keep the accepted batch_source at spawn time. Changing or"
                    " removing its source or selection requires a reviewed replan."
                ),
            }
    if not isinstance(source, dict):
        return None
    artifact_name = str(source.get("artifact_name") or "").strip()
    artifact_path_hint = str(source.get("_artifact_path") or "").strip()
    resolved_artifact_path_hint: Optional[Path] = None
    if artifact_path_hint:
        try:
            resolved_artifact_path_hint = Path(artifact_path_hint).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            resolved_artifact_path_hint = None
    state = _tc().load_task_state(logger)
    ledger_paths = [
        str(path) for path in state.get("artifacts") or [] if str(path).strip()
    ]
    references = phase.get("input_artifacts")
    references = references if isinstance(references, list) else []
    producers = sorted({
        str(ref.get("phase_id") or "").strip()
        for ref in references if isinstance(ref, dict)
        and str(ref.get("artifact_name") or "").strip() == artifact_name
        and str(ref.get("phase_id") or "").strip()
    })
    binding_mode = "legacy_unique_name"
    if references:
        # batch_source names an artifact contract, while input_artifacts names
        # its producer. A row source must have one producer identity; choosing
        # between multiple declared producers would be a semantic guess.
        if len(producers) != 1:
            return {
                "status": "batch_source_provenance_ambiguous",
                "phaseId": str(phase.get("id") or ""),
                "artifactName": artifact_name,
                "producerPhaseIds": producers,
                "tool_was_executed": False,
                "next_instruction": (
                    "Make worker_contract.batch_source.artifact_name resolve to"
                    " exactly one input_artifacts producer phase in a reviewed"
                    " replan. The runtime will not choose a producer by plan"
                    " order, filename, or recency."
                ),
            }
        binding_mode = "declared_producer"
        producer = producers[0]
        producer_state = (state.get("phases") or {}).get(producer) or {}
        # Explicit provenance is authoritative. Start from this producer's
        # validated receipt and intersect it with the task ledger; never scan
        # other producers merely because their payload uses the same name.
        if producer_state.get("status") != "validated_done":
            ledger_paths = []
        else:
            validated_paths = {
                str(Path(path).expanduser().resolve())
                for path in _latest_validated_extraction_paths(logger, producer)
            }
            ledger_paths = [
                path for path in ledger_paths
                if str(Path(path).expanduser().resolve()) in validated_paths
            ]
    extraction_root = (logger.task_dir / "artifacts" / "extractions").resolve()
    matches: List[Tuple[Path, JsonDict, str]] = []
    for raw_path in ledger_paths:
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(extraction_root)
        except ValueError:
            continue
        if resolved_artifact_path_hint is not None and path != resolved_artifact_path_hint:
            continue
        payload = load_task_json(logger, str(path))
        if payload is None:
            continue
        if (
            isinstance(payload, dict)
            and str(payload.get("name") or "").strip() == artifact_name
        ):
            payload_blob = json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            matches.append((
                path,
                payload,
                hashlib.sha256(payload_blob.encode("utf-8")).hexdigest()[:16],
            ))
    if len(matches) != 1:
        return {
            "status": "batch_source_not_ready",
            "phaseId": str(phase.get("id") or ""),
            "artifactName": artifact_name,
            "producerPhaseIds": producers,
            "bindingMode": binding_mode,
            "matchCount": len(matches),
            "tool_was_executed": False,
            "next_instruction": (
                "Resolve the declared producer phase and artifact name against"
                " its validated output. Check producerPhaseIds and matchCount;"
                " do not rename or copy files to manufacture validated provenance."
            ),
        }
    path, payload, source_artifact_generation = matches[0]
    raw_rows = payload.get("rows")
    source_rows = (
        [dict(row) for row in raw_rows if isinstance(row, dict)]
        if isinstance(raw_rows, list)
        else []
    )
    cohort_selected = list(enumerate(source_rows))
    cohort_selector_missing: List[str] = []
    cohort_selector = _canonical_cohort_selector(
        source.get("cohort_selector")
    )
    cohort_field = str(cohort_selector.get("field") or "").strip()
    cohort_values = cohort_selector.get("values")
    if cohort_field and isinstance(cohort_values, list):
        cohort_wanted = {
            json.dumps(value, sort_keys=True, default=str)
            for value in cohort_values
        }
        cohort_selected = [
            (index, row) for index, row in cohort_selected
            if json.dumps(
                row.get(cohort_field), sort_keys=True, default=str
            ) in cohort_wanted
        ]
        matched = {
            json.dumps(
                row.get(cohort_field), sort_keys=True, default=str
            )
            for _, row in cohort_selected
        }
        cohort_selector_missing = sorted(cohort_wanted - matched)
    selected = list(cohort_selected)
    selector = source.get("selector")
    selector = selector if isinstance(selector, dict) else {}
    field = str(selector.get("field") or "").strip()
    values = selector.get("values")
    selector_missing: List[str] = []
    if field and isinstance(values, list):
        wanted = {json.dumps(value, sort_keys=True, default=str) for value in values}
        selected = [
            (index, row) for index, row in selected
            if json.dumps(row.get(field), sort_keys=True, default=str) in wanted
        ]
        matched = {
            json.dumps(row.get(field), sort_keys=True, default=str)
            for _, row in selected
        }
        selector_missing = sorted(wanted - matched)
    raw_indices = selector.get("indices")
    if isinstance(raw_indices, list):
        # row_selection.source_indices arrives here. Indices name positions in
        # the cohort artifact, so an index the artifact no longer has must fail
        # loudly: silently selecting fewer rows than the plan asked for is how
        # a slice quietly shrinks between generations.
        wanted_indices = [
            int(item) for item in raw_indices
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        ]
        available = {index for index, _ in selected}
        selected = [
            (index, row) for index, row in selected if index in set(wanted_indices)
        ]
        missing_indices = sorted(set(wanted_indices) - available)
        if missing_indices:
            selector_missing = [
                *selector_missing,
                *(f"index:{index}" for index in missing_indices),
            ]
    offset = selector.get("offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
        selected = selected[offset:]
    limit = selector.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        selected = selected[:limit]
    rows = [row for _, row in selected]
    selected_source_indices = [index for index, _ in selected]
    cohort_source_indices = [index for index, _ in cohort_selected]

    policy = worker_contract.get("batch_policy")
    policy = policy if isinstance(policy, dict) else {}
    raw_max_rows = policy.get("max_rows_per_phase")
    max_rows = (
        raw_max_rows
        if isinstance(raw_max_rows, int)
        and not isinstance(raw_max_rows, bool)
        and raw_max_rows > 0
        else 10
    )
    error = ""
    if cohort_selector_missing:
        error = (
            "cohort_selector values missing from validated artifact:"
            f" {cohort_selector_missing}"
        )
    elif selector_missing:
        error = (
            "selector values missing from cohort:"
            f" {selector_missing}"
        )
    elif not cohort_source_indices:
        error = "cohort_selector matched zero rows"
    elif not rows:
        error = "selector matched zero rows"
    elif len(rows) > max_rows:
        error = f"selected {len(rows)} rows, exceeding max_rows_per_phase={max_rows}"
    elif policy.get("requires_isolation_per_row") is True and len(rows) > 1:
        error = "multiple rows cross a declared per-row isolation boundary"
    role = str(phase.get("execution_role") or worker_contract.get("execution_role") or "")
    if error:
        return {
            "status": "invalid_batch_source_selection",
            "phaseId": str(phase.get("id") or ""),
            "artifactName": artifact_name,
            "error": error,
            "selectedRows": len(rows),
            "tool_was_executed": False,
            "next_instruction": (
                "Revise the batch_source selector or batch_policy. The harness"
                " will not silently split, truncate, or merge this batch."
            ),
        }

    worker_contract["batch_rows"] = rows
    worker_contract["_batch_source_receipt"] = {
        "artifactName": artifact_name,
        "bindingMode": binding_mode,
        "producerPhaseId": producers[0] if len(producers) == 1 else None,
        "producerPhaseIds": producers,
        "artifactPath": str(path),
        "sourceArtifactGeneration": source_artifact_generation,
        "rowCount": len(rows),
        "sourceRowCount": len(source_rows),
        "cohortRowCount": len(cohort_source_indices),
        "cohortSourceIndices": cohort_source_indices,
        "selectedSourceIndices": selected_source_indices,
        "cohortSelector": cohort_selector,
        "selector": selector,
        "executionRole": role,
    }
    # The one receipt above answers two unrelated questions at once — who the
    # whole cohort is, and which rows THIS worker owns — and the checkpoint
    # reads it as if both were the same fact. That coupling is why a probe
    # cannot be a single item: it would have to pretend to be a batch to
    # produce a receipt at all. Split them; the checkpoint binds the cohort,
    # the slice records the assignment.
    identity_field = str(
        (source.get("identity_field") if isinstance(source, dict) else "")
        or field
        or cohort_field
    ).strip()
    worker_contract["_source_cohort_receipt"] = {
        "receiptType": "source_cohort.v1",
        "artifactName": artifact_name,
        "artifactPath": str(path),
        "artifactGeneration": source_artifact_generation,
        "identityField": identity_field,
        "cohortSourceIndices": cohort_source_indices,
        "cohortRowKeys": _row_keys_for_indices(
            source_rows, cohort_source_indices, identity_field,
        ),
        "sourceRowCount": len(source_rows),
        "cohortSelector": cohort_selector,
    }
    worker_contract["_execution_slice_receipt"] = {
        "receiptType": "execution_slice.v1",
        "role": role,
        "artifactPath": str(path),
        "artifactGeneration": source_artifact_generation,
        "selectedSourceIndices": selected_source_indices,
        "selectedRowKeys": _row_keys_for_indices(
            source_rows, selected_source_indices, identity_field,
        ),
        "selector": selector,
    }
    logger.write(
        "batch_source.materialized",
        {
            "phaseId": str(phase.get("id") or ""),
            **worker_contract["_batch_source_receipt"],
        },
    )
    return None

def _row_keys_for_indices(
    source_rows: List[JsonDict], indices: List[int], identity_field: str,
) -> List[str]:
    """Row keys for the given source indices, empty when there is no identity.

    Indices are positions in a file that a replan may replace; a key is what
    survives that. Both are recorded because neither alone is enough: indices
    without keys cannot be checked against a new generation, and keys without
    indices cannot be checked against the one that produced them.
    """
    if not identity_field:
        return []
    keys: List[str] = []
    for index in indices:
        if 0 <= index < len(source_rows):
            value = source_rows[index].get(identity_field)
            if value is not None and not isinstance(value, (dict, list, bool)):
                text = str(value).strip()
                if text:
                    keys.append(text)
    return keys

def _canonical_cohort_selector(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    field = str(value.get("field") or "").strip()
    values = value.get("values")
    if not field or not isinstance(values, list) or not values:
        return {}
    unique: Dict[str, Any] = {}
    for item in values:
        token = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        unique.setdefault(token, item)
    return {
        "field": field,
        "values": [unique[token] for token in sorted(unique)],
    }

def _cohort_selectors_provably_disjoint(left: Any, right: Any) -> bool:
    left_selector = _canonical_cohort_selector(left)
    right_selector = _canonical_cohort_selector(right)
    if not left_selector or not right_selector:
        return False
    if left_selector.get("field") != right_selector.get("field"):
        return False
    left_values = {
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        for value in left_selector.get("values") or []
    }
    right_values = {
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        for value in right_selector.get("values") or []
    }
    return bool(left_values and right_values and left_values.isdisjoint(right_values))

_FAST_PATH_QUANTITY_EXPECTED_KEYS = frozenset({
    "exact_rows", "min_rows", "max_rows", "count_range",
})

def _fast_path_selector_identity_fields(worker_contract: JsonDict) -> Set[str]:
    source = worker_contract.get("batch_source")
    source = source if isinstance(source, dict) else {}
    fields: Set[str] = set()
    for key in ("selector", "cohort_selector"):
        selector = source.get(key)
        selector = selector if isinstance(selector, dict) else {}
        field = str(selector.get("field") or "").strip()
        if field:
            fields.add(field)
    receipt = worker_contract.get("_batch_source_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    for key in ("selector", "cohortSelector"):
        selector = receipt.get(key)
        selector = selector if isinstance(selector, dict) else {}
        field = str(selector.get("field") or "").strip()
        if field:
            fields.add(field)
    return fields

def _fast_path_validator_is_slice(
    validator: JsonDict,
    *,
    selector_fields: AbstractSet[str],
) -> bool:
    """Classify phase-local validators without duplicating scope taxonomy.

    Aggregate evaluation scope and slice mutability are different concepts.
    Row-count validators always describe the selected batch.  Field-based
    aggregate/range validators are slice-local only when they target the
    declared batch identity; otherwise they remain business obligations.
    """

    validator_type = str(validator.get("type") or "").strip()
    if (
        VALIDATOR_SCOPE.get(validator_type) == "aggregate"
        and validator_type in {"min_rows", "max_rows", "exact_rows"}
    ):
        return True
    if validator_type not in {"range", "set_equals", "unique"}:
        return False
    fields = set(field_names_from_specs(validator.get("fields") or []))
    single = str(validator.get("field") or "").strip()
    if single:
        fields.add(single)
    return bool(fields) and fields.issubset(selector_fields)

def _fast_path_validator_obligations(validator: JsonDict) -> Set[str]:
    """Expand validators into monotonic obligations for checkpoint fencing."""

    validator_type = str(validator.get("type") or "").strip()
    if validator_type in {"required_fields", "field_nonempty"}:
        fields = set(field_names_from_specs(validator.get("fields") or []))
        single = str(validator.get("field") or "").strip()
        if single:
            fields.add(single)
        return {
            json.dumps(
                [validator_type, field],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for field in fields
        }
    return {_tc()._validator_semantic_signature(validator)}

def _canonical_fast_path_business_contract(
    phase: JsonDict,
    worker_contract: JsonDict,
) -> JsonDict:
    """Stable business schema shared by probe/validation/bulk/continuation.

    Stage hints, strategy choices, row selectors and per-stage row-count bounds
    are execution profile.  They may legitimately change after observing the
    probe.  Artifact identity/shape and non-slice validators are business
    contract and must remain stable, otherwise a checkpoint could certify a
    different output merely because it reads from the same source rows.
    """

    expected = copy.deepcopy(_tc()._merged_expected_artifact(phase, worker_contract))
    for key in _FAST_PATH_QUANTITY_EXPECTED_KEYS:
        expected.pop(key, None)

    fields = expected.get("fields")
    if isinstance(fields, list):
        expected["fields"] = sorted(
            (copy.deepcopy(item) for item in fields),
            key=lambda item: json.dumps(
                item, sort_keys=True, ensure_ascii=False, default=str,
            ),
        )
    required_fields = expected.get("required_fields")
    if isinstance(required_fields, list):
        expected["required_fields"] = sorted({
            str(item).strip() for item in required_fields if str(item).strip()
        })

    validators = worker_contract.get("validators")
    if not isinstance(validators, list):
        validators = phase.get("validators")
    selector_fields = _fast_path_selector_identity_fields(worker_contract)
    validator_tokens: Dict[str, JsonDict] = {}
    obligations: Set[str] = set()
    for item in validators if isinstance(validators, list) else []:
        if not isinstance(item, dict):
            continue
        if _fast_path_validator_is_slice(
            item,
            selector_fields=selector_fields,
        ):
            continue
        normalized = copy.deepcopy(item)
        token = _tc()._validator_semantic_signature(normalized)
        validator_tokens.setdefault(token, normalized)
        obligations.update(_fast_path_validator_obligations(normalized))

    return {
        "taskType": _tc()._first_valid_task_type(
            phase.get("task_type"),
            "web_scrape",
        ),
        "expectedArtifact": expected,
        "validators": [
            validator_tokens[token] for token in sorted(validator_tokens)
        ],
        "validatorObligations": sorted(obligations),
    }

def _fast_path_business_contract_signature(
    phase: JsonDict,
    worker_contract: JsonDict,
) -> str:
    payload = _canonical_fast_path_business_contract(phase, worker_contract)
    blob = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

def _business_contract_obligations(contract: JsonDict) -> Set[str]:
    raw = contract.get("validatorObligations")
    if isinstance(raw, list):
        return {str(item) for item in raw if str(item).strip()}
    obligations: Set[str] = set()
    for validator in contract.get("validators") or []:
        if isinstance(validator, dict):
            obligations.update(_fast_path_validator_obligations(validator))
    return obligations

def _fast_path_business_contract_fence_errors(
    expected: JsonDict,
    actual: JsonDict,
) -> List[str]:
    errors: List[str] = []
    if str(actual.get("taskType") or "") != str(expected.get("taskType") or ""):
        errors.append(
            "task_type changed across the checkpoint"
        )
    if actual.get("expectedArtifact") != expected.get("expectedArtifact"):
        errors.append(
            "merged expected_artifact changed across the checkpoint"
        )
    missing = sorted(
        _business_contract_obligations(expected)
        - _business_contract_obligations(actual)
    )
    if missing:
        errors.append(
            "non-slice validator obligations were removed or weakened: "
            + json.dumps(missing, ensure_ascii=False)
        )
    return errors

def _fast_path_cohort_key(
    phase: JsonDict,
    worker_contract: JsonDict,
    batch_receipt: JsonDict,
) -> str:
    payload = {
        "sourceArtifactPath": str(batch_receipt.get("artifactPath") or ""),
        "sourceArtifactGeneration": str(
            batch_receipt.get("sourceArtifactGeneration")
            or batch_receipt.get("artifactGeneration")
            or ""
        ),
        "cohortSourceIndices": sorted(
            int(item)
            for item in (batch_receipt.get("cohortSourceIndices") or [])
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        ),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
