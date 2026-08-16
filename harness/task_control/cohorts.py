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
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.evidence.artifact_evidence import VALIDATOR_SCOPE
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import load_task_json

def _tc():
    import harness.task_control as tc

    return tc

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
    if not isinstance(source, dict):
        return None
    artifact_name = str(source.get("artifact_name") or "").strip()
    state = _tc().load_task_state(logger)
    ledger_paths = [
        str(path) for path in state.get("artifacts") or [] if str(path).strip()
    ]
    extraction_root = (logger.task_dir / "artifacts" / "extractions").resolve()
    matches: List[Tuple[Path, JsonDict, str]] = []
    for raw_path in ledger_paths:
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(extraction_root)
        except ValueError:
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
            "matchCount": len(matches),
            "tool_was_executed": False,
            "next_instruction": (
                "Wait for the declared upstream artifact to become uniquely"
                " validated, or replan batch_source.artifact_name. Do not copy"
                " unvalidated rows into batch_rows manually."
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
    if role == "probe" and len(rows) > 1:
        error = "probe selected more than one row"
    elif role == "validation" and len(rows) > 2:
        error = "validation selected more than two rows"
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
