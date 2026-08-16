"""
harness.task_control.validators - Row/file validator normalization and execution.
"""

from __future__ import annotations

import json
import hashlib
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from urllib.parse import urlparse
from harness.evidence.extraction_artifacts import field_name_from_spec
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.evidence.artifact_evidence import VALIDATOR_TYPES
from harness.evidence.artifact_evidence import cumulative_row_key as _cumulative_row_key
from harness.evidence.artifact_evidence import detect_blocker_data_rows
from harness.evidence.artifact_evidence import detect_placeholder_rows
from harness.evidence.file_evidence import saved_paths_from_value
from harness.results.row_ledger import ROW_OUTCOMES
from harness.results.row_ledger import field_absence_accepted
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import load_task_json

def _tc():
    import harness.task_control as tc

    return tc

def run_row_validator(validator: JsonDict, rows: List[JsonDict]) -> List[JsonDict]:
    """Public entry point to the validator engine.

    The shared evaluator in ``harness.evidence.artifact_evidence`` cannot import this
    module (task_control imports the evaluator's row-hygiene kernel), so the
    engine is injected instead. This alias keeps that injection from reaching
    into a private name.
    """
    return _run_validator(validator, rows)

def _run_validator(validator: JsonDict, rows: List[JsonDict]) -> List[JsonDict]:
    validator_type = str(validator.get("type") or "").strip()
    failures: List[JsonDict] = []

    if validator_type == "artifact_required":
        return []

    if validator_type == "required_fields":
        fields = _tc()._string_list(validator.get("fields"))
        for index, row in enumerate(rows):
            missing = [field for field in fields if field not in row]
            if missing:
                failures.append({
                    "type": validator_type,
                    "row": index,
                    "missing": missing,
                })
        return failures

    if validator_type == "field_nonempty":
        fields = _tc()._string_list(validator.get("fields"))
        if not fields:
            field = str(validator.get("field") or "").strip()
            fields = [field] if field else []
        for index, row in enumerate(rows):
            # A key the extractor never wrote and a key it wrote as empty are
            # different claims. The first records nothing about the field; the
            # second is an explicit "I looked and there is nothing here".
            # Collapsing them is how a field that was never fetched gets
            # reported as a confirmed absence — and an over-specified contract
            # (a product that genuinely has no detail images) becomes
            # indistinguishable from a worker that skipped the field.
            #
            # The verdict is deliberately unchanged: both still fail. Only the
            # failure's shape differs, so the worker is told which of the two
            # to fix, and repeated confirmed-empty on one field is visible as
            # evidence that the contract, not the run, is wrong.
            unverified = [field for field in fields if field not in row]
            confirmed_empty = [
                field for field in fields
                if field in row and _tc()._is_empty_value(row.get(field))
            ]
            # A field the contract declares emptiable, whose row carries a
            # complete absence proof, is a finding rather than a hole. Without
            # this branch a product that genuinely has no reviews can never
            # satisfy the contract, and the phase burns every attempt against a
            # page that will not change.
            allowance = validator.get("allow_empty_with_outcome")
            incomplete_proofs: List[JsonDict] = []
            if confirmed_empty and isinstance(allowance, dict):
                remaining: List[str] = []
                for field in confirmed_empty:
                    verdict = field_absence_accepted(
                        row, field, allowed_outcomes=allowance.get(field),
                    )
                    if verdict["accepted"]:
                        continue
                    remaining.append(field)
                    if verdict.get("absenceProof"):
                        incomplete_proofs.append({
                            "field": field,
                            "reason": verdict["reason"],
                            "absenceProof": verdict["absenceProof"],
                        })
                confirmed_empty = remaining
            if confirmed_empty or unverified:
                failure: JsonDict = {
                    "type": validator_type,
                    "row": index,
                    "empty": [*confirmed_empty, *unverified],
                }
                if confirmed_empty:
                    failure["confirmedEmpty"] = confirmed_empty
                if unverified:
                    failure["unverified"] = unverified
                if incomplete_proofs:
                    failure["absenceProofs"] = incomplete_proofs
                failures.append(failure)
        return failures

    if validator_type in {"min_rows", "max_rows", "exact_rows"}:
        raw_value = validator.get("value")
        if raw_value is None:
            if validator_type == "min_rows":
                raw_value = validator.get("min")
            elif validator_type == "max_rows":
                raw_value = validator.get("max")
            else:
                raw_value = (
                    validator.get("count")
                    or validator.get("exact")
                    or validator.get("rows")
                )
        value = _tc()._positive_int(raw_value, default=0)
        count = len(rows)
        ok = (
            count >= value if validator_type == "min_rows"
            else count <= value if validator_type == "max_rows"
            else count == value
        )
        if not ok:
            failures.append({
                "type": validator_type,
                "expected": value,
                "actual": count,
            })
        return failures

    if validator_type == "unique":
        # Plan-author validators are copied through normalization verbatim
        # and commonly use "fields": [...] (plural). Reading only "field"
        # made every row key "" — a bogus all-duplicates needs_fix.
        unique_fields: List[str] = []
        single = str(validator.get("field") or "").strip()
        if single:
            unique_fields = [single]
        else:
            raw_fields = validator.get("fields")
            if isinstance(raw_fields, list):
                unique_fields = [
                    str(item).strip() for item in raw_fields if str(item).strip()
                ]
        if not unique_fields:
            return failures
        seen: Dict[str, int] = {}
        duplicates: List[JsonDict] = []
        for index, row in enumerate(rows):
            value = "\x1f".join(
                str(row.get(name) or "") for name in unique_fields
            )
            if value in seen:
                duplicates.append({"row": index, "firstRow": seen[value], "value": value})
            else:
                seen[value] = index
        if duplicates:
            failures.append({
                "type": validator_type,
                "field": ", ".join(unique_fields),
                "duplicates": duplicates[:20],
            })
        return failures

    if validator_type == "url_pattern":
        field = str(validator.get("field") or "url").strip()
        pattern = str(validator.get("pattern") or r"^https?://").strip()
        regex = re.compile(pattern)
        bad = [
            {"row": index, "value": row.get(field)}
            for index, row in enumerate(rows)
            if not regex.search(str(row.get(field) or ""))
        ]
        if bad:
            failures.append({"type": validator_type, "field": field, "pattern": pattern, "bad": bad[:20]})
        return failures

    if validator_type == "field_pattern":
        field = str(validator.get("field") or "").strip()
        pattern = str(validator.get("pattern") or "").strip()
        regex = _compile_validator_regex(pattern, validator)
        bad = [
            {"row": index, "value": row.get(field)}
            for index, row in enumerate(rows)
            if not regex.search(str(row.get(field) or ""))
        ]
        if bad:
            failures.append({
                "type": validator_type,
                "field": field,
                "pattern": pattern,
                "bad": bad[:20],
            })
        return failures

    if validator_type == "cross_field_contains":
        field = str(validator.get("field") or "").strip()
        contains_field = str(
            validator.get("contains_field")
            or validator.get("expected_field")
            or ""
        ).strip()
        case_sensitive = bool(validator.get("case_sensitive", False))
        bad = []
        for index, row in enumerate(rows):
            haystack = str(row.get(field) or "")
            needle = str(row.get(contains_field) or "")
            if not needle:
                bad.append({
                    "row": index,
                    "field": contains_field,
                    "reason": "expected field empty",
                })
                continue
            left = haystack if case_sensitive else haystack.lower()
            right = needle if case_sensitive else needle.lower()
            if right not in left:
                bad.append({
                    "row": index,
                    "field": field,
                    "contains_field": contains_field,
                    "value": haystack[:200],
                    "expected": needle[:200],
                })
        if bad:
            failures.append({
                "type": validator_type,
                "field": field,
                "contains_field": contains_field,
                "bad": bad[:20],
            })
        return failures

    if validator_type == "action_outcome":
        failures.extend(_validate_action_outcome(validator, rows))
        return failures

    if validator_type == "field_provenance":
        failures.extend(_validate_field_provenance(validator, rows))
        return failures

    if validator_type == "allowed_domain":
        field = str(validator.get("field") or "url").strip()
        domains = set(_tc()._string_list(validator.get("domains")))
        bad = []
        for index, row in enumerate(rows):
            host = urlparse(str(row.get(field) or "")).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host not in domains:
                bad.append({"row": index, "host": host, "value": row.get(field)})
        if bad:
            failures.append({"type": validator_type, "field": field, "domains": sorted(domains), "bad": bad[:20]})
        return failures

    if validator_type == "set_equals":
        field = str(validator.get("field") or "").strip()
        expected = {str(item) for item in (validator.get("values") or [])}
        actual = {str(row.get(field)) for row in rows}
        if actual != expected:
            failures.append({
                "type": validator_type,
                "field": field,
                "expected": sorted(expected),
                "actual": sorted(actual),
            })
        return failures

    if validator_type == "range":
        field = str(validator.get("field") or "").strip()
        min_value = validator.get("min")
        max_value = validator.get("max")
        bad = []
        for index, row in enumerate(rows):
            raw_value = row.get(field)
            if raw_value is None:
                bad.append({"row": index, "value": raw_value, "reason": "not numeric"})
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                bad.append({"row": index, "value": raw_value, "reason": "not numeric"})
                continue
            if min_value is not None and value < float(min_value):
                bad.append({"row": index, "value": value, "reason": "below min"})
            if max_value is not None and value > float(max_value):
                bad.append({"row": index, "value": value, "reason": "above max"})
        if bad:
            failures.append({"type": validator_type, "field": field, "bad": bad[:20]})
        return failures

    return [{
        "type": "unknown_validator",
        "validator": validator_type,
        "message": f"validator type must be one of {sorted(VALIDATOR_TYPES)}",
    }]

def _run_file_validator(
    validator: JsonDict,
    *,
    artifacts: List[str],
    evidence: List[JsonDict],
    rows: List[JsonDict],
) -> List[JsonDict]:
    validator_type = str(validator.get("type") or "").strip()
    file_paths = _tc()._unique_paths([
        str(path) for path in artifacts
        if str(path).strip() and "/artifacts/extractions/" not in str(path)
    ])

    if validator_type == "download_completed":
        receipts = [
            item for item in evidence
            if isinstance(item, dict) and (
                str(item.get("method") or "") == "File.download"
                or str(item.get("method") or "").startswith("Download.")
            )
        ]
        completed = any(_file_receipt_completed(item) for item in receipts)
        if completed:
            return []
        return [{
            "type": validator_type,
            "message": "no successful completed download receipt was recorded",
            "receiptCount": len(receipts),
        }]

    if validator_type == "file_integrity":
        pattern = str(validator.get("path_pattern") or validator.get("pattern") or "").strip()
        extensions = {
            str(item).lower().lstrip(".")
            for item in (validator.get("extensions") or [])
            if str(item).strip()
        }
        raw_min_bytes = validator.get("min_bytes")
        min_bytes = max(0, int(1 if raw_min_bytes is None else raw_min_bytes))
        expected_sha256 = str(validator.get("sha256") or "").strip().lower()
        valid: List[str] = []
        bad: List[JsonDict] = []
        regex = re.compile(pattern) if pattern else None
        for raw_path in file_paths:
            path = Path(raw_path).expanduser()
            if regex is not None and regex.search(str(path)) is None:
                continue
            if extensions and path.suffix.lower().lstrip(".") not in extensions:
                continue
            try:
                if not path.is_file():
                    bad.append({"path": str(path), "reason": "not_a_file"})
                    continue
                size = path.stat().st_size
                if size < min_bytes:
                    bad.append({"path": str(path), "reason": "too_small", "byteSize": size})
                    continue
                if expected_sha256:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    if digest != expected_sha256:
                        bad.append({"path": str(path), "reason": "sha256_mismatch", "sha256": digest})
                        continue
                valid.append(str(path))
            except OSError as exc:
                bad.append({"path": str(path), "reason": "io_error", "error": str(exc)})
        min_files = max(1, int(validator.get("min_files") or 1))
        if len(valid) >= min_files:
            return []
        return [{
            "type": validator_type,
            "message": "downloaded/exported files did not satisfy integrity constraints",
            "requiredFiles": min_files,
            "validFiles": valid,
            "bad": bad[:20],
            "availableArtifacts": file_paths,
        }]

    if validator_type in {"upload_selected", "upload_confirmed"}:
        receipts = [
            item for item in evidence
            if isinstance(item, dict)
            and str(item.get("method") or "") == "File.handleChooser"
            and _file_receipt_succeeded(item)
        ]
        selected_count = max(
            (_selected_file_count(item.get("params")) for item in receipts),
            default=0,
        )
        min_files = max(1, int(validator.get("min_files") or 1))
        if not receipts or selected_count < min_files:
            return [{
                "type": validator_type,
                "message": "File.handleChooser did not confirm the required file selection",
                "requiredFiles": min_files,
                "selectedFiles": selected_count,
                "receiptCount": len(receipts),
            }]
        if validator_type == "upload_selected":
            return []
        field = str(validator.get("field") or "").strip()
        pattern = str(validator.get("pattern") or "").strip()
        if not field:
            return [{
                "type": validator_type,
                "message": "upload_confirmed requires a field containing post-upload page evidence",
            }]
        regex = re.compile(pattern) if pattern else None
        confirmed = any(
            not _tc()._is_empty_value(row.get(field))
            and (regex is None or regex.search(str(row.get(field) or "")) is not None)
            for row in rows
        )
        if confirmed:
            return []
        return [{
            "type": validator_type,
            "message": "file selection succeeded but post-upload page confirmation is missing",
            "field": field,
            "pattern": pattern,
        }]

    if validator_type == "image_exported":
        min_files = max(1, int(validator.get("min_files") or 1))
        # `svg` belongs here: DOM.getImg with imageFormat="auto" preserves a
        # safe self-contained inline SVG instead of rasterizing it, so a real
        # exported asset can legitimately arrive with that extension.
        image_exts = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "svg"}
        exported = [
            path for path in file_paths
            if Path(path).suffix.lower().lstrip(".") in image_exts
        ]
        receipts = [
            item for item in evidence
            if isinstance(item, dict)
            and str(item.get("method") or "") == "DOM.getImg"
            and _file_receipt_succeeded(item)
        ]
        if receipts and len(exported) >= min_files:
            return []
        return [{
            "type": validator_type,
            "message": "DOM.getImg did not produce the required image artifacts",
            "requiredFiles": min_files,
            "exportedFiles": exported,
            "receiptCount": len(receipts),
        }]

    return [{"type": "unknown_file_validator", "validator": validator_type}]

def _file_receipt_succeeded(receipt: JsonDict) -> bool:
    response = receipt.get("response")
    return isinstance(response, dict) and not response.get("error")

def _file_receipt_completed(receipt: JsonDict) -> bool:
    if not _file_receipt_succeeded(receipt):
        return False
    response = receipt.get("response")
    strings: List[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif key.lower() in {"status", "state"}:
            strings.append(str(value).strip().lower())

    walk(response)
    if any(value in {"completed", "complete", "done", "success", "succeeded"} for value in strings):
        return True
    return bool(_receipt_saved_paths(response))

def _receipt_saved_paths(value: Any) -> List[str]:
    # Compatibility wrapper retained for validator-level tests.
    return saved_paths_from_value(value)

def _selected_file_count(params: Any) -> int:
    if not isinstance(params, dict):
        return 0
    for key in ("files", "paths", "filePaths"):
        value = params.get(key)
        if isinstance(value, list):
            return len([item for item in value if str(item).strip()])
    for key in ("path", "filePath"):
        if isinstance(params.get(key), str) and str(params.get(key)).strip():
            return 1
    return 0

def _validate_action_outcome(
    validator: JsonDict,
    rows: List[JsonDict],
) -> List[JsonDict]:
    url_field = str(validator.get("url_field") or "url").strip()
    heading_field = str(validator.get("heading_field") or "heading").strip()
    expected_url_pattern = str(validator.get("expected_url_pattern") or "").strip()
    expected_heading_pattern = str(
        validator.get("expected_heading_pattern") or ""
    ).strip()
    heading_matches_field = str(
        validator.get("heading_matches_field") or ""
    ).strip()
    min_similarity = _float_value(validator.get("min_similarity"), default=0.8)

    url_regex = (
        _compile_validator_regex(expected_url_pattern, validator)
        if expected_url_pattern
        else None
    )
    heading_regex = (
        _compile_validator_regex(expected_heading_pattern, validator)
        if expected_heading_pattern
        else None
    )

    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        reasons: List[JsonDict] = []
        url_value = str(row.get(url_field) or "")
        heading_value = str(row.get(heading_field) or "")
        if url_regex and not url_regex.search(url_value):
            reasons.append({
                "reason": "url_pattern_mismatch",
                "field": url_field,
                "value": url_value[:300],
                "pattern": expected_url_pattern,
            })
        if heading_regex and not heading_regex.search(heading_value):
            reasons.append({
                "reason": "heading_pattern_mismatch",
                "field": heading_field,
                "value": heading_value[:300],
                "pattern": expected_heading_pattern,
            })
        if heading_matches_field:
            expected = str(row.get(heading_matches_field) or "")
            score = _similarity(heading_value, expected)
            if expected and score < min_similarity:
                reasons.append({
                    "reason": "heading_similarity_below_threshold",
                    "field": heading_field,
                    "matches_field": heading_matches_field,
                    "similarity": round(score, 3),
                    "min_similarity": min_similarity,
                    "value": heading_value[:300],
                    "expected": expected[:300],
                })
            elif not expected:
                reasons.append({
                    "reason": "expected_heading_field_empty",
                    "matches_field": heading_matches_field,
                })
        if reasons:
            bad.append({"row": index, "failures": reasons})

    if not bad:
        return []
    return [{"type": "action_outcome", "bad": bad[:20]}]

def _validate_field_provenance(
    validator: JsonDict,
    rows: List[JsonDict],
) -> List[JsonDict]:
    raw_fields = validator.get("fields") or validator.get("field_provenance")
    if isinstance(raw_fields, list):
        specs = {str(field): {} for field in raw_fields if str(field).strip()}
    elif isinstance(raw_fields, dict):
        specs = {
            str(field): spec if isinstance(spec, dict) else {}
            for field, spec in raw_fields.items()
            if str(field).strip()
        }
    else:
        field = str(validator.get("field") or "").strip()
        specs = {field: {}} if field else {}

    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        for field, spec in specs.items():
            evidence_field = str(
                spec.get("evidence_field")
                or validator.get("evidence_field")
                or f"{field}EvidenceText"
            ).strip()
            source_tool_field = str(
                spec.get("source_tool_field")
                or validator.get("source_tool_field")
                or "sourceTool"
            ).strip()
            selector_field = str(
                spec.get("selector_field")
                or validator.get("selector_field")
                or "sourceSelectorOrAxId"
            ).strip()
            evidence_aliases = _provenance_evidence_aliases(
                field,
                evidence_field,
                spec,
                validator,
            )
            missing = []
            if row.get(field) is None or str(row.get(field)).strip() == "":
                missing.append(field)
            if not evidence_field or not _row_has_nonempty_value(row, evidence_aliases):
                missing.append(evidence_field or "evidence_field")
            if bool(spec.get("require_source_tool", validator.get("require_source_tool", False))):
                if str(row.get(source_tool_field) or "").strip() == "":
                    missing.append(source_tool_field)
            if bool(spec.get("require_selector", validator.get("require_selector", False))):
                if str(row.get(selector_field) or "").strip() == "":
                    missing.append(selector_field)
            if missing:
                bad.append({"row": index, "field": field, "missing": missing})

    if not bad:
        return []
    return [{"type": "field_provenance", "bad": bad[:20]}]

def _provenance_evidence_aliases(
    field: str,
    evidence_field: str,
    spec: JsonDict,
    validator: JsonDict,
) -> List[str]:
    aliases: List[Any] = [
        evidence_field,
        f"{field}EvidenceText",
        f"{field}Evidence",
    ]
    raw_aliases = spec.get("evidence_aliases")
    if raw_aliases is None:
        raw_aliases = validator.get("evidence_aliases")
    if isinstance(raw_aliases, list):
        aliases.extend(raw_aliases)
    elif isinstance(raw_aliases, str):
        aliases.append(raw_aliases)
    aliases.extend(["evidence", "evidenceText"])
    return field_names_from_specs(aliases)

def _row_has_nonempty_value(row: JsonDict, fields: List[str]) -> bool:
    for field in fields:
        if str(row.get(field) or "").strip():
            return True
    return False

def _compile_validator_regex(pattern: str, validator: JsonDict) -> re.Pattern[str]:
    flags = re.I if bool(validator.get("case_insensitive", False)) else 0
    return re.compile(pattern or r"$^", flags)

def _similarity(left: str, right: str) -> float:
    left_norm = _norm_compare_text(left)
    right_norm = _norm_compare_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm in right_norm or right_norm in left_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()

def _norm_compare_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())

def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _row_count_validator_value(validator: JsonDict) -> Optional[int]:
    validator_type = str(validator.get("type") or "").strip()
    raw_value = validator.get("value")
    if raw_value is None:
        if validator_type == "min_rows":
            raw_value = validator.get("min")
        elif validator_type == "max_rows":
            raw_value = validator.get("max")
        elif validator_type == "exact_rows":
            for key in ("count", "exact", "rows"):
                if validator.get(key) is not None:
                    raw_value = validator.get(key)
                    break
    value = _tc()._positive_int(raw_value, default=0)
    return value if value > 0 else None

def _normalize_expected_artifact_contract(
    expected_artifact: JsonDict,
    validators: List[Any],
    errors: List[str],
    warnings: List[JsonDict],
    *,
    phase_id: str,
) -> JsonDict:
    """Recover one canonical expected-artifact shape from equivalent inputs.

    Gateway models sometimes put a field list under an empty JSON key (task
    51496108) even though the same list is correctly present in a
    required_fields validator. The empty key has no semantics: drop it with a
    receipt, then backfill only from an unambiguous contract source.
    """
    expected = dict(expected_artifact)
    blank_values = []
    for key in list(expected):
        if str(key).strip():
            continue
        blank_values.append(expected.pop(key))
        warnings.append({
            "type": "empty_expected_artifact_key_dropped",
            "phase": phase_id,
            "message": (
                "expected_artifact contained an empty property name; it has no"
                " contract meaning and was dropped"
            ),
        })

    field_names = field_names_from_specs(expected.get("fields") or [])
    if not field_names:
        field_names = field_names_from_specs(expected.get("required_fields") or [])
    explicit_required: List[str] = []
    seen_required = set()
    explicit_exact_values: Set[int] = set()
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        validator_type = str(validator.get("type") or "").strip()
        validator_type = _tc().VALIDATOR_TYPE_ALIASES.get(validator_type, validator_type)
        if validator_type == "required_fields":
            for field in field_names_from_specs(validator.get("fields") or []):
                if field not in seen_required:
                    explicit_required.append(field)
                    seen_required.add(field)
        elif validator_type == "exact_rows":
            value = _row_count_validator_value({**validator, "type": validator_type})
            if value is not None:
                explicit_exact_values.add(value)

    if not field_names and explicit_required:
        expected["fields"] = explicit_required
        field_names = list(explicit_required)
        warnings.append({
            "type": "expected_artifact_fields_backfilled",
            "phase": phase_id,
            "source": "required_fields",
            "fields": explicit_required,
        })
    if blank_values and not field_names and any(value not in (None, "", [], {}) for value in blank_values):
        errors.append(
            f"phase {phase_id}: expected_artifact empty-key value could not be"
            " recovered from fields/required_fields"
        )

    if expected.get("exact_rows") is None:
        if len(explicit_exact_values) == 1:
            expected["exact_rows"] = next(iter(explicit_exact_values))
            warnings.append({
                "type": "expected_artifact_exact_rows_backfilled",
                "phase": phase_id,
                "source": "exact_rows validator",
                "value": expected["exact_rows"],
            })
        elif len(explicit_exact_values) > 1:
            errors.append(
                f"phase {phase_id}: conflicting exact_rows validators:"
                f" {sorted(explicit_exact_values)}"
            )
    return expected

def _canonical_validator_params(validator: JsonDict) -> JsonDict:
    normalized = dict(validator)
    validator_type = str(normalized.get("type") or "").strip()
    if validator_type in {"min_rows", "max_rows", "exact_rows"}:
        value = _row_count_validator_value(normalized)
        if value is not None:
            normalized["value"] = value
        for alias in ("count", "exact", "rows", "min", "max"):
            normalized.pop(alias, None)
    if validator_type in {"required_fields", "field_nonempty", "unique"}:
        fields = field_names_from_specs(normalized.get("fields") or [])
        single = str(normalized.get("field") or "").strip()
        if single and single not in fields:
            fields.append(single)
        if fields:
            normalized["fields"] = fields
            normalized.pop("field", None)
    return normalized

def _validator_semantic_signature(validator: JsonDict) -> str:
    validator_type = str(validator.get("type") or "")
    if validator_type in {"min_rows", "max_rows", "exact_rows"}:
        payload: Any = [validator_type, _row_count_validator_value(validator)]
    elif validator_type in {"required_fields", "field_nonempty", "unique"}:
        payload = [
            validator_type,
            sorted(set(field_names_from_specs(validator.get("fields") or []))),
        ]
    elif validator_type == "set_equals":
        payload = [
            validator_type,
            str(validator.get("field") or ""),
            sorted({str(value) for value in (validator.get("values") or [])}),
        ]
    else:
        payload = validator
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

def _dedupe_and_check_validators(
    validators: List[JsonDict],
    errors: List[str],
    *,
    phase_id: str,
    warnings: Optional[List[JsonDict]],
) -> List[JsonDict]:
    invalid_row_constraints = [
        str(validator.get("type") or "")
        for validator in validators
        if str(validator.get("type") or "") in {
            "min_rows", "max_rows", "exact_rows",
        }
        and _row_count_validator_value(validator) is None
    ]
    for validator_type in invalid_row_constraints:
        errors.append(
            f"phase {phase_id}: {validator_type} requires a positive integer value"
        )
    exact_values = {
        value for value in (
            _row_count_validator_value(validator)
            for validator in validators
            if str(validator.get("type") or "") == "exact_rows"
        )
        if value is not None
    }
    if len(exact_values) > 1:
        errors.append(
            f"phase {phase_id}: conflicting exact_rows constraints:"
            f" {sorted(exact_values)}"
        )

    out: List[JsonDict] = []
    seen: Dict[str, JsonDict] = {}
    for validator in validators:
        signature = _validator_semantic_signature(validator)
        kept = seen.get(signature)
        if kept is not None:
            # field_nonempty's semantic signature is type+fields, so a duplicate
            # may still carry an emptiable-field declaration the kept copy lacks
            # (expected_artifact and an explicit validator each derive one).
            # Dropping it wholesale would silently reinstate the strict reading
            # and pin the phase on a row that is legitimately empty.
            allowance = validator.get("allow_empty_with_outcome")
            if (
                str(validator.get("type") or "") == "field_nonempty"
                and isinstance(allowance, dict)
            ):
                merged = dict(kept.get("allow_empty_with_outcome") or {})
                merged.update(allowance)
                kept["allow_empty_with_outcome"] = merged
            if warnings is not None:
                warnings.append({
                    "type": "duplicate_validator_dropped",
                    "phase": phase_id,
                    "validatorType": str(validator.get("type") or ""),
                })
            continue
        seen[signature] = validator
        out.append(validator)
    return out

def _normalize_validators(
    expected_artifact: JsonDict,
    validators: List[Any],
    errors: List[str],
    *,
    phase_id: str,
    warnings: Optional[List[JsonDict]] = None,
) -> List[JsonDict]:
    normalized: List[JsonDict] = []
    if expected_artifact.get("min_rows") is not None:
        normalized.append({"type": "min_rows", "value": expected_artifact.get("min_rows")})
    if expected_artifact.get("max_rows") is not None:
        normalized.append({"type": "max_rows", "value": expected_artifact.get("max_rows")})
    has_exact_rows = expected_artifact.get("exact_rows") is not None
    if has_exact_rows:
        normalized.append({"type": "exact_rows", "value": expected_artifact.get("exact_rows")})
    count_range = expected_artifact.get("count_range")
    if isinstance(count_range, list) and len(count_range) >= 2:
        min_rows = _tc()._positive_int(count_range[0], default=0)
        max_rows = _tc()._positive_int(count_range[1], default=0)
        if not has_exact_rows:
            if min_rows > 0 and min_rows == max_rows:
                normalized.append({"type": "exact_rows", "value": min_rows})
            else:
                if min_rows > 0:
                    normalized.append({"type": "min_rows", "value": min_rows})
                if max_rows > 0:
                    normalized.append({"type": "max_rows", "value": max_rows})
    fields = expected_artifact.get("required_fields")
    if not isinstance(fields, list) or not fields:
        fields = expected_artifact.get("fields")
    field_names = field_names_from_specs(fields)
    if field_names:
        normalized.append({"type": "required_fields", "fields": field_names})
        nonempty_fields = _nonempty_fields_from_expected(expected_artifact, fields)
        if nonempty_fields:
            nonempty_validator: JsonDict = {
                "type": "field_nonempty", "fields": nonempty_fields,
            }
            allowance = _allow_empty_with_outcome_from_expected(
                expected_artifact, fields, nonempty_fields,
            )
            if allowance:
                nonempty_validator["allow_empty_with_outcome"] = allowance
            normalized.append(nonempty_validator)
        provenance_fields = _provenance_required_fields(expected_artifact, field_names)
        if provenance_fields and not _has_field_provenance_validator(validators, provenance_fields):
            normalized.append({
                "type": "field_provenance",
                "fields": _provenance_field_specs(provenance_fields),
                "require_source_tool": True,
                "require_selector": True,
            })

    for index, validator in enumerate(validators):
        if not isinstance(validator, dict):
            errors.append(f"phase {phase_id}: validators[{index}] must be an object")
            continue
        validator_type = str(validator.get("type") or "").strip()
        canonical_type = _tc().VALIDATOR_TYPE_ALIASES.get(validator_type, validator_type)
        if canonical_type != validator_type:
            if warnings is not None:
                warnings.append({
                    "type": "validator_type_alias",
                    "phase": phase_id,
                    "index": index,
                    "input": validator_type,
                    "canonical": canonical_type,
                    "message": (
                        f"validator type {validator_type!r} normalized to"
                        f" {canonical_type!r}; emit the canonical name next time"
                    ),
                })
            validator_type = canonical_type
        if validator_type not in VALIDATOR_TYPES:
            errors.append(
                f"phase {phase_id}: validators[{index}].type must be one of {sorted(VALIDATOR_TYPES)}"
            )
        normalized_validator = dict(validator)
        normalized_validator["type"] = validator_type
        if validator_type == "upload_confirmed":
            confirmation_field = str(normalized_validator.get("field") or "").strip()
            if not confirmation_field:
                errors.append(
                    f"phase {phase_id}: validators[{index}] upload_confirmed"
                    " requires field"
                )
            elif confirmation_field not in field_names:
                errors.append(
                    f"phase {phase_id}: upload_confirmed field"
                    f" {confirmation_field!r} must be declared in"
                    " expected_artifact.fields so the worker records page evidence"
                )
        if validator_type == "allowed_domain":
            # Accept the singular spelling a plan naturally writes, then refuse
            # a declaration that cannot pass. An empty allowlist is not "allow
            # anything" — it is "allow nothing": every row's host is outside
            # it, on every attempt, with nothing the worker can do. That is the
            # unsatisfiable-contract shape this whole series exists to stop,
            # and it happened live in task 3189c68b: two workers extracted
            # their pages completely and were failed anyway, because the plan
            # wrote `domain` while the check reads `domains`.
            singular = str(normalized_validator.pop("domain", "") or "").strip()
            domains = _tc()._string_list(normalized_validator.get("domains"))
            if singular and singular not in domains:
                domains = [*domains, singular]
            normalized_validator["domains"] = domains
            if not domains:
                errors.append(
                    f"phase {phase_id}: validators[{index}] allowed_domain"
                    " declares no domain, so no row can ever pass it; list the"
                    " allowed hosts in `domains`"
                )
            # The field defaults to "url", which most artifacts do not carry.
            # A validator pointed at a field the artifact never declares fails
            # every row for a reason the data cannot fix.
            domain_field = str(normalized_validator.get("field") or "url").strip()
            normalized_validator["field"] = domain_field
            if field_names and domain_field not in field_names:
                errors.append(
                    f"phase {phase_id}: allowed_domain field {domain_field!r}"
                    " is not declared in expected_artifact.fields; point it at"
                    " the field that holds the URL"
                )
        if validator_type == "field_provenance":
            normalized_validator["fields"] = _normalize_provenance_validator_fields(
                normalized_validator
            )
        normalized.append(_canonical_validator_params(normalized_validator))
    normalized = [_canonical_validator_params(item) for item in normalized]
    return _dedupe_and_check_validators(
        normalized,
        errors,
        phase_id=phase_id,
        warnings=warnings,
    )

def _allow_empty_with_outcome_from_expected(
    expected_artifact: JsonDict, fields: Any, nonempty_fields: List[str],
) -> JsonDict:
    """Which non-empty fields may still be empty when their absence is proven.

    Declared per field, either on the field spec or as a top-level map, and
    only meaningful for a field that is otherwise required non-empty. The plan
    must say so explicitly: guessing which fields a site sometimes omits is the
    kind of site knowledge the harness has no business inventing.
    """
    allowance: JsonDict = {}
    wanted = set(nonempty_fields)

    def _record(name: Any, raw: Any) -> None:
        field = str(name or "").strip()
        if not field or field not in wanted:
            return
        outcomes = [
            str(item).strip()
            for item in (raw if isinstance(raw, list) else [])
            if str(item or "").strip() in ROW_OUTCOMES
        ]
        if outcomes:
            allowance[field] = outcomes

    declared = expected_artifact.get("allow_empty_with_outcome")
    if isinstance(declared, dict):
        for name, raw in declared.items():
            _record(name, raw)
    if isinstance(fields, list):
        for spec in fields:
            if isinstance(spec, dict):
                _record(
                    field_name_from_spec(spec),
                    spec.get("allow_empty_with_outcome"),
                )
    return allowance

def _nonempty_fields_from_expected(expected_artifact: JsonDict, fields: Any) -> List[str]:
    explicit = expected_artifact.get("nonempty_fields")
    if explicit is None:
        explicit = expected_artifact.get("field_nonempty")
    out = field_names_from_specs(explicit if isinstance(explicit, list) else [])
    seen = set(out)
    if not isinstance(fields, list):
        return out
    scalar_types = {"str", "string", "text", "number", "integer", "int", "float", "url"}
    for spec in fields:
        if not isinstance(spec, dict):
            continue
        name = field_name_from_spec(spec)
        if not name or name in seen:
            continue
        if spec.get("allow_empty") is True or spec.get("optional_empty") is True:
            continue
        if spec.get("nonempty") is True or spec.get("required_nonempty") is True:
            out.append(name)
            seen.add(name)
            continue
        type_name = str(spec.get("type") or "").strip().lower()
        if type_name in scalar_types and spec.get("nullable") is not True:
            out.append(name)
            seen.add(name)
    return out

def _normalize_provenance_validator_fields(validator: JsonDict) -> JsonDict:
    raw_fields = validator.get("fields") or validator.get("field_provenance")
    if isinstance(raw_fields, dict):
        specs: JsonDict = {}
        for field, raw_spec in raw_fields.items():
            field_name = str(field or "").strip()
            if not field_name:
                continue
            spec = dict(raw_spec) if isinstance(raw_spec, dict) else {}
            merged = _default_provenance_field_spec(field_name)
            merged.update({key: value for key, value in spec.items() if value is not None})
            specs[field_name] = merged
        return specs
    fields = field_names_from_specs(raw_fields if isinstance(raw_fields, list) else [])
    if not fields:
        field = str(validator.get("field") or "").strip()
        fields = [field] if field else []
    return _provenance_field_specs(fields)

def _provenance_field_specs(fields: List[str]) -> JsonDict:
    specs: JsonDict = {}
    for field in field_names_from_specs(fields):
        specs[field] = _default_provenance_field_spec(field)
    return specs

def _default_provenance_field_spec(field: str) -> JsonDict:
    return {
        "evidence_field": f"{field}EvidenceText",
        "evidence_aliases": ["evidence", f"{field}Evidence"],
        "source_tool_field": "sourceTool",
        "selector_field": "sourceSelectorOrAxId",
        "require_source_tool": True,
        "require_selector": True,
    }

def _has_field_provenance_validator(
    validators: List[Any],
    fields: List[str],
) -> bool:
    wanted = set(field_names_from_specs(fields))
    if not wanted:
        return True
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "field_provenance":
            continue
        raw_fields = validator.get("fields") or validator.get("field_provenance")
        if isinstance(raw_fields, dict):
            present = {str(field).strip() for field in raw_fields.keys()}
        elif isinstance(raw_fields, list):
            present = set(field_names_from_specs(raw_fields))
        else:
            present = {str(validator.get("field") or "").strip()}
        if wanted.issubset({field for field in present if field}):
            return True
    return False

def _provenance_required_fields(
    expected_artifact: JsonDict,
    fields: List[Any],
) -> List[str]:
    override = expected_artifact.get("provenance_required")
    if isinstance(override, list):
        return field_names_from_specs(override)
    out: List[str] = []
    for field in fields:
        text = field_name_from_spec(field)
        if not text:
            continue
        lowered = text.lower()
        tokens = {token for token in re.split(r"[_\W]+", lowered) if token}
        if lowered in _tc().SENSITIVE_PROVENANCE_FIELD_MARKERS or tokens & _tc().SENSITIVE_PROVENANCE_FIELD_MARKERS:
            out.append(text)
    return out

def _validate_cumulative_artifacts(
    *,
    validators: List[JsonDict],
    expected: JsonDict,
    candidates: List[JsonDict],
) -> Tuple[List[JsonDict], List[str], List[JsonDict], List[JsonDict]]:
    rows_by_key: Dict[str, JsonDict] = {}
    row_sources: Dict[str, str] = {}
    row_scopes: Dict[str, JsonDict] = {}
    source_paths: List[str] = []
    schema_failures: List[JsonDict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        schema_warnings = payload.get("schemaWarnings")
        if isinstance(schema_warnings, list) and schema_warnings:
            schema_failures.append({
                "type": "schema",
                "message": "cumulative artifact has schemaWarnings",
                "path": path,
                "schemaWarnings": schema_warnings[:5],
            })
            continue
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list):
            schema_failures.append({
                "type": "schema",
                "message": "cumulative artifact has no rows array",
                "path": path,
            })
            continue
        if path:
            source_paths.append(path)
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            key = _cumulative_row_key(row, expected)
            existing = rows_by_key.get(key)
            if existing is None or _prefer_cumulative_row(
                row,
                existing,
                validators=validators,
                expected=expected,
            ):
                rows_by_key[key] = row
                row_sources[key] = path
                scope = payload.get("evidenceContext")
                row_scopes[key] = dict(scope) if isinstance(scope, dict) else {}

    source_paths = _tc()._unique_paths(source_paths)
    if len(source_paths) < 2:
        return [], source_paths, schema_failures, []

    ordered_keys = list(rows_by_key)
    rows = [rows_by_key[key] for key in ordered_keys]
    provenance = [
        {"path": row_sources.get(key, ""), "scope": row_scopes.get(key, {})}
        for key in ordered_keys
    ]
    failures: List[JsonDict] = []
    for validator in validators:
        failures.extend(_run_validator(validator, rows))
    failures.extend(detect_placeholder_rows(rows))
    failures.extend(detect_blocker_data_rows(rows, expected))
    return rows, source_paths, [*schema_failures, *failures], provenance

def make_row_preference(
    *,
    validators: List[JsonDict],
    expected: JsonDict,
) -> Any:
    """Bind the authoritative row-quality reducer for an external consumer.

    The shadow ledger must resolve two rows sharing one identity exactly as the
    cumulative merge does. Last-write-wins there and quality-wins here would put
    a different row in each verdict and make the comparison meaningless.
    """

    def prefer(candidate: JsonDict, current: JsonDict) -> bool:
        return _prefer_cumulative_row(
            candidate, current, validators=validators, expected=expected,
        )

    return prefer

def _prefer_cumulative_row(
    candidate: JsonDict,
    current: JsonDict,
    *,
    validators: List[JsonDict],
    expected: JsonDict,
) -> bool:
    return _cumulative_row_quality(candidate, validators, expected) >= _cumulative_row_quality(
        current,
        validators,
        expected,
    )

def _cumulative_row_quality(
    row: JsonDict,
    validators: List[JsonDict],
    expected: JsonDict,
) -> tuple:
    """Rank two candidates for the same slot. Never rejects either of them.

    `detect_stub_rows` used to contribute here too, and it is the last place
    its invented threshold (`len(empty) >= max(2, len(present))`) decided
    anything. It is redundant rather than wrong: `nonempty_expected` and
    `nonempty_total` below already prefer the row that carries content, and
    they do it by counting fields instead of by a cutoff nobody declared.
    """
    row_failures: List[JsonDict] = []
    for validator in validators:
        validator_type = str(validator.get("type") or "").strip()
        if validator_type in {"min_rows", "max_rows", "exact_rows", "unique", "set_equals"}:
            continue
        row_failures.extend(_run_validator(validator, [row]))
    row_failures.extend(detect_placeholder_rows([row]))
    row_failures.extend(detect_blocker_data_rows([row], expected))

    expected_fields = field_names_from_specs(
        expected.get("required_fields") or expected.get("fields") or []
    )
    present = sum(1 for field in expected_fields if field in row)
    nonempty_expected = sum(
        1 for field in expected_fields
        if field in row and not _tc()._is_empty_value(row.get(field))
    )
    evidence = sum(
        1 for key, value in row.items()
        if str(key).endswith("EvidenceText") and str(value or "").strip()
    )
    source = sum(
        1 for key in ("sourceTool", "sourceSelectorOrAxId", "pageUrl")
        if str(row.get(key) or "").strip()
    )
    nonempty_total = sum(1 for value in row.values() if not _tc()._is_empty_value(value))
    return (
        -len(row_failures),
        present,
        evidence,
        source,
        nonempty_expected,
        nonempty_total,
    )

def _load_extraction_artifacts(
    paths: List[str],
    task_dir: Path,
    logger: Optional[RunLogger] = None,
) -> List[JsonDict]:
    """Load cited extraction artifacts, wherever the backend put them.

    This feeds the artifact gate that decides whether a phase completed. Left
    reading the filesystem directly, a db-mode artifact that the agent could
    read perfectly well was still judged missing here.
    """

    loaded: List[JsonDict] = []
    root = task_dir.resolve(strict=False)
    reader = logger if logger is not None else _tc()._PathOnlyLogger(root)
    for raw_path in paths:
        try:
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        payload = load_task_json(reader, str(resolved))
        if isinstance(payload, dict):
            loaded.append({"path": str(resolved), "payload": payload})
    return loaded
