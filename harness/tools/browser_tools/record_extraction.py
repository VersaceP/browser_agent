"""
harness.tools.browser_tools.record_extraction - record_extraction persistence and validation.
"""

import re
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
import json
from pathlib import Path
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.evidence.extraction_artifacts import save_extraction_artifact
from harness.evidence.extraction_artifacts import validate_extraction_rows
from harness.evidence.artifact_evidence import detect_blocker_data_rows
from harness.task_control import phase_prior_artifact_paths
from harness.task_control import validate_worker_artifacts
from harness.utils import JsonDict
from harness.utils import trim_large_strings

def _bt():
    import harness.tools.browser_tools as bt

    return bt

def _record_extraction(
    agent: Any,
    tool_input: JsonDict,
) -> JsonDict:
    """Persist a structured extraction artifact."""
    return _record_extraction_persist(agent, tool_input)

def _record_extraction_persist(
    agent: Any,
    tool_input: JsonDict,
) -> JsonDict:
    """Persist a structured extraction artifact for LeadAgent consumption.

    Returns a stub describing the saved file.

    The contract is intentionally simple: name + rows (list of dicts) +
    optional schema. The agent must populate `rows` from observed evidence
    (e.g. extracted hrefs from a Runtime.evaluate result). Downstream
    consumers must read from the saved artifact rather than rely on the
    agent's narrative summary.
    """
    raw_name = str(tool_input.get("name") or "").strip()
    raw_rows = tool_input.get("rows")
    raw_schema = tool_input.get("schema")
    description = str(tool_input.get("description") or "").strip()

    if not raw_name:
        return {"status": "rejected", "error": "name required"}
    rows, error = validate_extraction_rows(raw_rows)
    if error is not None:
        return error
    rows = rows or []

    rows, repair_merge, repair_error = _merge_repair_patch_rows(
        agent,
        artifact_name=raw_name,
        patch_rows=rows,
        repair_resolutions=tool_input.get("repair_resolutions"),
    )
    if repair_error is not None:
        repair_error.setdefault(
            "next_instruction",
            (
                "This worker is in field-repair mode. Submit only manifest target"
                " rows, each with the exact identity field/value shown in the"
                " handoff plus at least one requested repair field; do not resend"
                " trusted rows or fields."
            ),
        )
        return repair_error
    if repair_merge:
        description = description or (
            "Field-level slow-path repair merged into trusted fast-path baseline"
        )

    contract = getattr(agent, "worker_contract", None)
    expected = (
        contract.get("expected_artifact")
        if isinstance(contract, dict)
        and isinstance(contract.get("expected_artifact"), dict)
        else {}
    )
    blocker_failures = detect_blocker_data_rows(rows, expected)
    if blocker_failures:
        result = {
            "status": "rejected",
            "error": (
                "blocker or challenge explanation cannot be stored in a"
                " declared business data field"
            ),
            "failures": blocker_failures,
            "next_instruction": (
                "Keep observed business values in the declared data fields."
                " Report authentication/challenge state through HITL and the"
                " worker blocker/status channel; do not pad rows with failure"
                " notes or structured blocker tokens."
            ),
        }
        agent.logger.write("tool.record_extraction.rejected", {
            "name": raw_name,
            "rowCount": len(rows),
            "reason": "blocker_as_business_data",
            "failures": blocker_failures,
        })
        return result

    schema_warnings = [
        *_record_extraction_schema_warnings(agent, rows),
        *_record_extraction_content_warnings(rows),
    ]
    result = save_extraction_artifact(
        logger=agent.logger,
        runtime=agent.runtime,
        artifacts=None if schema_warnings else agent.artifacts,
        name=raw_name,
        rows=rows,
        schema=raw_schema,
        description=description,
        schema_warnings=schema_warnings,
        event_type="tool.record_extraction",
    )
    attempts = getattr(agent, "extraction_attempt_artifacts", None)
    if isinstance(attempts, list):
        saved_path = str(result.get("savedPath") or "")
        if saved_path and saved_path not in attempts:
            attempts.append(saved_path)
    if repair_merge:
        result["repairMerge"] = repair_merge
        contract = getattr(agent, "worker_contract", None)
        manifest = (
            contract.get("_repair_manifest")
            if isinstance(contract, dict)
            and isinstance(contract.get("_repair_manifest"), dict)
            else None
        )
        if manifest is not None and result.get("savedPath"):
            # Subsequent patch saves build on the latest merged rows, so a
            # worker can repair several targets serially without resending old
            # patches or copying the full baseline through the LLM context.
            manifest["workingArtifact"] = str(result["savedPath"])
    validation = _bt()._validate_recorded_extraction(agent, str(result.get("savedPath") or ""))
    if validation:
        contract_validation = trim_large_strings(validation, 3000)
        result["artifactValidation"] = contract_validation
        # Name this boundary explicitly: spawner may later compose a separate
        # content-completeness veto, but phase credit must consume only the
        # artifact/row contract result or it would depend circularly on itself.
        result["contractValidation"] = contract_validation
        tracker = _bt()._ensure_content_completeness_tracker(agent)
        if tracker is not None and tracker.enabled:
            if validation.get("status") == "done":
                credit = tracker.observe_contract_validated_artifact(
                    rows=rows,
                    artifact_name=raw_name,
                    saved_path=str(result.get("savedPath") or ""),
                )
                result["contentRegionCredit"] = credit
                agent.logger.write(
                    "content_completeness.artifact_region_credit",
                    credit,
                )
            else:
                tracker.observe_failed_artifact_attempt()
        if validation.get("status") == "failed":
            failures = [
                failure for failure in (validation.get("failures") or [])
                if isinstance(failure, dict)
            ]
            blocking = [
                failure for failure in failures
                if not _is_advisory_record_failure(failure)
            ]
            if blocking:
                result["status"] = "needs_fix"
                result["next_instruction"] = (
                    "record_extraction saved the rows but the current worker_contract"
                    " validators failed. Fix the row keys, artifact name, or values"
                    " shown in artifactValidation before final_answer."
                )
            elif result.get("status") == "done":
                result["validationPending"] = sorted({
                    str(failure.get("type") or "") for failure in failures
                })
                result["next_instruction"] = (
                    "Rows saved. Phase validation is not satisfied yet:"
                    " keep collecting until the expected row count is reached,"
                    " and include sourceTool/sourceSelectorOrAxId/pageUrl plus"
                    " the canonical <field>EvidenceText keys (e.g. rankEvidenceText)"
                    " before final_answer."
                )
    repair_resolutions = (
        repair_merge.get("resolutions")
        if isinstance(repair_merge, dict)
        and isinstance(repair_merge.get("resolutions"), list)
        else []
    )
    if repair_resolutions:
        contract = getattr(agent, "worker_contract", None)
        manifest = (
            contract.get("_repair_manifest")
            if isinstance(contract, dict) else None
        )
        satisfied = (
            manifest.get("visualEvidenceSatisfied")
            if isinstance(manifest, dict) else None
        )
        satisfied_signatures = (
            set(satisfied) if isinstance(satisfied, dict) else set()
        )
        pending_by_signature = {
            str(item.get("signature")): dict(item)
            for item in (
                manifest.get("visualEvidencePending")
                if isinstance(manifest, dict)
                and isinstance(manifest.get("visualEvidencePending"), list)
                else []
            )
            if isinstance(item, dict) and str(item.get("signature") or "")
        }
        visual_checks_enabled = _repair_visual_checks_enabled(agent)
        for item in repair_resolutions:
            identity = item.get("identity") if isinstance(item, dict) else None
            field = item.get("field") if isinstance(item, dict) else None
            signature = _bt()._repair_visual_target_signature(identity, field)
            outcome = str(item.get("outcome") or "") if isinstance(item, dict) else ""
            if outcome == "confirmed_absent" and visual_checks_enabled:
                if signature not in satisfied_signatures:
                    pending_by_signature[signature] = {**item, "signature": signature}
                continue
            pending_by_signature.pop(signature, None)
            # Evidence for a prior absence claim must not automatically satisfy
            # a later claim after the field was observed or supplied with a value.
            if isinstance(satisfied, dict):
                satisfied.pop(signature, None)
                satisfied_signatures.discard(signature)
        unresolved_absent = list(pending_by_signature.values())
        if isinstance(manifest, dict):
            if unresolved_absent:
                manifest["visualEvidencePending"] = unresolved_absent
            else:
                manifest.pop("visualEvidencePending", None)
        if unresolved_absent:
            pending = {
                str(item) for item in (result.get("validationPending") or [])
                if str(item).strip()
            }
            pending.add("absence_visual_evidence")
            result["validationPending"] = sorted(pending)
            result["repairEvidencePending"] = unresolved_absent
            visual_instruction = (
                "Repair values marked confirmed_absent are merged, but target-"
                "bound visual evidence is still pending. Keep/reuse the relevant"
                " live page and call visual_verify with repair_targets matching"
                " the listed identity/field targets before final_answer;"
                " Page.screenshot and unrelated visual checks do not count. Cite"
                " this merged savedPath plus the visual evidence in final_answer."
                " Do not re-submit or re-scrape already merged fields."
            )
            prior_instruction = str(result.get("next_instruction") or "").strip()
            result["next_instruction"] = (
                f"{prior_instruction} {visual_instruction}".strip()
            )
    return result

def _merge_repair_patch_rows(
    agent: Any,
    *,
    artifact_name: str,
    patch_rows: List[JsonDict],
    repair_resolutions: Any = None,
) -> Tuple[List[JsonDict], JsonDict, Optional[JsonDict]]:
    """Merge model-supplied patch rows into an internal fast-path baseline.

    The manifest is injected by the spawner, never accepted from the model.
    Only named repair fields and their evidence metadata may change; every
    other baseline field is preserved byte-for-byte.
    """
    contract = getattr(agent, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest")
        if isinstance(contract, dict)
        and isinstance(contract.get("_repair_manifest"), dict)
        else None
    )
    if manifest is None or str(manifest.get("artifactName") or "") != artifact_name:
        return patch_rows, {}, None
    if manifest.get("disabledReason"):
        # A previous structural failure deliberately abandoned merge mode. The
        # worker may now record one complete replacement artifact normally.
        return patch_rows, {}, None

    def fallback(reason: str, detail: str) -> Tuple[List[JsonDict], JsonDict, JsonDict]:
        """Disable an unusable internal manifest so the next save can recover."""
        manifest["disabledReason"] = reason
        abandoned_visual = manifest.pop("visualEvidencePending", None)
        if isinstance(abandoned_visual, list) and abandoned_visual:
            manifest["visualEvidenceAbandoned"] = [
                dict(item) for item in abandoned_visual if isinstance(item, dict)
            ]
        payload = {
            "artifactName": artifact_name,
            "reason": reason,
            "detail": detail[:500],
            "baselineArtifact": str(manifest.get("baselineArtifact") or ""),
        }
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("skill.fast_path.repair_fallback", payload)
            if isinstance(abandoned_visual, list) and abandoned_visual:
                logger.write("repair.visual_evidence_abandoned", {
                    "reason": reason,
                    "targets": manifest.get("visualEvidenceAbandoned") or [],
                })
        return patch_rows, {}, {
            "status": "repair_fallback_required",
            "error": detail,
            "tool_was_executed": False,
            "next_instruction": (
                "The trusted repair baseline is unavailable or inconsistent, so"
                " field-patch mode has been disabled. Re-record ONE COMPLETE"
                " artifact under the expected name with every expected row and"
                " field; the normal phase validators will check it."
            ),
        }

    if str(manifest.get("version") or "") != "repair_manifest.v1":
        return fallback(
            "invalid_manifest_version",
            "invalid internal repair manifest version",
        )

    raw_path = str(
        manifest.get("workingArtifact") or manifest.get("baselineArtifact") or ""
    ).strip()
    try:
        path = Path(raw_path).expanduser().resolve()
        root = (agent.logger.task_dir / "artifacts" / "extractions").resolve()
    except Exception:
        return fallback("invalid_baseline_path", "repair baseline path is invalid")
    if not raw_path or (path != root and root not in path.parents):
        return fallback(
            "baseline_outside_task",
            "repair baseline must be an extraction artifact in this task",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fallback(
            "baseline_unreadable",
            f"repair baseline could not be read: {str(exc)[:300]}",
        )
    raw_baseline_rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(raw_baseline_rows, list) or not all(
        isinstance(row, dict) for row in raw_baseline_rows
    ):
        return fallback(
            "baseline_rows_invalid",
            "repair baseline has no valid rows array",
        )
    baseline_rows = [dict(row) for row in raw_baseline_rows]
    expected_count = manifest.get("rowCount")
    if isinstance(expected_count, int) and len(baseline_rows) != expected_count:
        return fallback(
            "baseline_row_count_changed",
            "repair baseline row count changed unexpectedly",
        )

    repairs = manifest.get("repairs")
    if not isinstance(repairs, list) or not repairs:
        return fallback("manifest_targets_missing", "repair manifest has no targets")

    targets: Dict[Tuple[str, str], JsonDict] = {}
    for item in repairs:
        identity = item.get("identity") if isinstance(item, dict) else None
        field = str(identity.get("field") or "") if isinstance(identity, dict) else ""
        value = identity.get("value") if isinstance(identity, dict) else None
        fields = item.get("fields") if isinstance(item, dict) else None
        if not field or not isinstance(fields, list) or not fields:
            return fallback(
                "manifest_target_invalid",
                "repair manifest contains an invalid target",
            )
        key = (field, str(value).strip() if value is not None else "")
        row_indexes = [
            index for index, row in enumerate(baseline_rows)
            if (
                str(row.get(field)).strip()
                if row.get(field) is not None else ""
            ) == key[1]
        ]
        if not key[1] or len(row_indexes) != 1 or key in targets:
            return fallback(
                "baseline_identity_mismatch",
                "repair target identity is not unique in the baseline",
            )
        targets[key] = {
            "rowIndex": row_indexes[0],
            "fields": {str(name) for name in fields if str(name).strip()},
        }

    resolutions: Dict[Tuple[Tuple[str, str], str], JsonDict] = {}
    if repair_resolutions is not None:
        if not isinstance(repair_resolutions, list):
            return patch_rows, {}, {
                "status": "rejected",
                "error": "repair_resolutions must be an array in repair mode",
            }
        for index, raw_resolution in enumerate(repair_resolutions):
            if not isinstance(raw_resolution, dict):
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": f"repair_resolutions[{index}] must be an object",
                }
            identity = raw_resolution.get("identity")
            identity_field = (
                str(identity.get("field") or "").strip()
                if isinstance(identity, dict) else ""
            )
            identity_value = (
                str(identity.get("value")).strip()
                if isinstance(identity, dict) and identity.get("value") is not None
                else ""
            )
            field = str(raw_resolution.get("field") or "").strip()
            outcome = str(raw_resolution.get("outcome") or "").strip()
            identity_key = (identity_field, identity_value)
            target = targets.get(identity_key)
            if (
                target is None
                or not field
                or field not in target["fields"]
            ):
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"repair_resolutions[{index}] does not identify one"
                        " manifest target field"
                    ),
                }
            if outcome not in {
                "value_found", "observed_empty", "confirmed_absent", "unresolved",
            }:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": f"repair_resolutions[{index}].outcome is invalid",
                }
            resolution_key = (identity_key, field)
            if resolution_key in resolutions:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": "duplicate repair resolution for one target field",
                }
            resolutions[resolution_key] = {
                "outcome": outcome,
                "evidenceArtifacts": [
                    str(path).strip()
                    for path in (raw_resolution.get("evidenceArtifacts") or [])
                    if str(path).strip()
                ] if isinstance(raw_resolution.get("evidenceArtifacts"), list) else [],
                "note": str(raw_resolution.get("note") or "").strip()[:500],
            }

    applied: List[JsonDict] = []
    ignored_fields: List[JsonDict] = []
    resolution_results: List[JsonDict] = []
    confirmed_absent: List[JsonDict] = []
    seen_targets: set[Tuple[str, str]] = set()
    shared_metadata = {"pageUrl", "sourceTool", "sourceSelectorOrAxId"}
    for patch_index, patch in enumerate(patch_rows):
        matching = [
            (key, target) for key, target in targets.items()
            if (
                str(patch.get(key[0])).strip()
                if patch.get(key[0]) is not None else ""
            ) == key[1]
        ]
        if len(matching) != 1:
            return patch_rows, {}, {
                "status": "rejected",
                "error": (
                    f"repair patch row {patch_index} must contain exactly one"
                    " manifest identity field/value"
                ),
            }
        key, target = matching[0]
        if key in seen_targets:
            return patch_rows, {}, {
                "status": "rejected",
                "error": "duplicate repair patch row for one target",
            }
        seen_targets.add(key)
        repair_fields = target["fields"]
        provided = sorted(field for field in repair_fields if field in patch)
        if not provided:
            return patch_rows, {}, {
                "status": "rejected",
                "error": (
                    f"repair patch row {patch_index} contains none of its"
                    f" requested fields: {sorted(repair_fields)}"
                ),
            }

        for field in provided:
            value_is_empty = _repair_value_is_empty(patch.get(field))
            resolution = resolutions.get((key, field))
            if value_is_empty and resolution is None:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"empty repair field {field!r} requires a matching"
                        " repair_resolutions entry with outcome observed_empty"
                        " or confirmed_absent"
                    ),
                }
            if resolution is None:
                outcome = "value_found"
                resolution = {"outcome": outcome, "evidenceArtifacts": [], "note": ""}
            else:
                outcome = str(resolution.get("outcome") or "")
            if outcome == "unresolved":
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"repair field {field!r} is unresolved and cannot be"
                        " persisted as a completed patch"
                    ),
                }
            if value_is_empty and outcome == "value_found":
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": f"empty repair field {field!r} cannot be value_found",
                }
            if not value_is_empty and outcome in {"observed_empty", "confirmed_absent"}:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"non-empty repair field {field!r} conflicts with"
                        f" outcome {outcome}"
                    ),
                }
            if (
                outcome in {"observed_empty", "confirmed_absent"}
                and _repair_field_requires_nonempty(agent, field)
            ):
                return patch_rows, {}, {
                    "status": "repair_contract_conflict",
                    "error": (
                        f"repair field {field!r} is constrained by field_nonempty"
                        f" and cannot resolve as {outcome}"
                    ),
                    "field": field,
                    "validator": "field_nonempty",
                    "outcome": outcome,
                    "tool_was_executed": False,
                    "next_instruction": (
                        "This is a deterministic contract conflict, not an"
                        " extraction retry. Do not submit the same empty patch"
                        " again; report the blocker so LeadAgent can revise the"
                        " contract or accept a partial result."
                    ),
                }
            if (
                outcome in {"observed_empty", "confirmed_absent"}
                and not _repair_resolution_has_source_evidence(
                    agent, patch, field, resolution,
                )
            ):
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"empty repair field {field!r} requires source evidence"
                        f" for outcome {outcome}"
                    ),
                }
            resolution_result = {
                "identity": {"field": key[0], "value": patch.get(key[0])},
                "field": field,
                "outcome": outcome,
            }
            resolution_results.append(resolution_result)
            if outcome == "confirmed_absent":
                confirmed_absent.append(resolution_result)

        destination = baseline_rows[target["rowIndex"]]
        allowed_evidence = {
            evidence_name
            for field in repair_fields
            for evidence_name in (f"{field}EvidenceText", f"{field}Evidence")
        }
        allowed = repair_fields | allowed_evidence | shared_metadata | {key[0]}
        ignored = sorted(str(field) for field in patch.keys() if field not in allowed)
        if ignored:
            ignored_fields.append({"patchRow": patch_index, "fields": ignored})
        for field in allowed:
            if field in patch and field != key[0]:
                destination[field] = patch[field]
        applied.append({
            "patchRow": patch_index,
            "baselineRow": target["rowIndex"],
            "identity": {"field": key[0], "value": patch.get(key[0])},
            "fields": provided,
        })

    if not applied:
        return patch_rows, {}, {
            "status": "rejected",
            "error": "repair patch did not update any manifest field",
        }
    info: JsonDict = {
        "baselineArtifact": str(manifest.get("baselineArtifact") or raw_path),
        "workingArtifact": raw_path,
        "applied": applied,
        "preservedRowCount": len(baseline_rows),
    }
    if ignored_fields:
        info["ignoredFields"] = ignored_fields
    if resolution_results:
        info["resolutions"] = resolution_results
    if confirmed_absent:
        info["confirmedAbsent"] = confirmed_absent
    return baseline_rows, info, None

def _repair_value_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False

def _repair_field_requires_nonempty(agent: Any, field: str) -> bool:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return False
    validators = contract.get("validators")
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "field_nonempty":
            continue
        fields = validator.get("fields")
        if isinstance(fields, list) and field in {str(item) for item in fields}:
            return True
        if str(validator.get("field") or "").strip() == field:
            return True
    expected = contract.get("expected_artifact")
    specs = expected.get("fields") if isinstance(expected, dict) else None
    for spec in specs if isinstance(specs, list) else []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("field") or spec.get("key") or "")
        if name != field:
            continue
        if spec.get("allow_empty") is True or spec.get("optional_empty") is True:
            return False
        return bool(spec.get("nonempty") or spec.get("required_nonempty"))
    return False

def _repair_resolution_has_source_evidence(
    agent: Any,
    patch: JsonDict,
    field: str,
    resolution: JsonDict,
) -> bool:
    if any(
        str(patch.get(name) or "").strip()
        for name in (f"{field}EvidenceText", f"{field}Evidence")
    ):
        return True
    source_tool = str(patch.get("sourceTool") or "").strip()
    source_locator = str(
        patch.get("sourceSelectorOrAxId") or patch.get("pageUrl") or ""
    ).strip()
    if source_tool and source_locator:
        return True
    ledger = {
        str(path).strip()
        for path in [
            *list(getattr(agent, "artifacts", []) or []),
            *list(getattr(agent, "extraction_attempt_artifacts", []) or []),
        ]
        if str(path).strip()
    }
    return any(
        str(path).strip() in ledger
        for path in (resolution.get("evidenceArtifacts") or [])
    )

def _repair_visual_checks_enabled(agent: Any) -> bool:
    vl_config = getattr(
        getattr(getattr(agent, "runtime", None), "harness", None), "vl", None,
    )
    return bool(
        vl_config is not None
        and getattr(vl_config, "enabled", False)
        and getattr(vl_config, "reality_check_enabled", True)
    )

def _is_advisory_record_failure(failure: JsonDict) -> bool:
    """Failures an in-progress worker resolves by continuing (row-count
    shortfall) or enriching rows on a later save (provenance) — not signals
    that the just-saved rows are wrong."""
    failure_type = str(failure.get("type") or "")
    if failure_type in {"min_rows", "field_provenance"}:
        return True
    if failure_type == "exact_rows":
        expected = failure.get("expected")
        actual = failure.get("actual")
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            return actual < expected
    return False

def _validate_recorded_extraction(agent: Any, saved_path: str) -> JsonDict:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict) or not saved_path:
        return {}
    phase_id = str(contract.get("phase_id") or "")
    try:
        prior_artifacts = phase_prior_artifact_paths(
            agent.logger,
            phase_id=phase_id,
            exclude_worker_id=getattr(agent, "worker_id", None),
        )
        return validate_worker_artifacts(
            contract=contract,
            artifacts=list(getattr(agent, "artifacts", []) or []),
            attempt_artifacts=[saved_path],
            prior_artifacts=prior_artifacts,
            file_evidence=list(getattr(agent, "file_action_evidence", []) or []),
            task_dir=agent.logger.task_dir,
            logger=agent.logger,
        )
    except Exception as exc:
        return {
            "status": "skipped",
            "reason": "record_extraction_validation_error",
            "error": str(exc)[:500],
        }

def _record_extraction_schema_warnings(agent: Any, rows: List[JsonDict]) -> List[JsonDict]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return []
    expected = contract.get("expected_artifact")
    if not isinstance(expected, dict):
        return []
    fields = expected.get("required_fields")
    if not isinstance(fields, list) or not fields:
        fields = expected.get("fields")
    expected_fields = field_names_from_specs(fields)
    if not expected_fields or not rows:
        return []

    warnings: List[JsonDict] = []
    expected_set = set(expected_fields)
    for index, row in enumerate(rows[:20]):
        keys = set(str(key) for key in row.keys())
        missing = sorted(expected_set - keys)
        if missing:
            warnings.append({
                "type": "expected_fields_missing",
                "row": index,
                "missing": missing,
                "expectedFields": expected_fields,
            })
    return warnings

PLACEHOLDER_VALUE_RE = re.compile(
    r"^\s*(?:<\s*)?(?:placeholder|sample|example|todo|tbd|n/?a)(?:\s*>)?\s*$",
    re.I,
)

PLACEHOLDER_URL_RE = re.compile(r"/(?:placeholder|sample|example)(?:[/?#]|$)", re.I)

def _record_extraction_content_warnings(rows: List[JsonDict]) -> List[JsonDict]:
    warnings: List[JsonDict] = []
    for index, row in enumerate(rows[:20]):
        placeholder_fields: List[JsonDict] = []
        if _row_reports_placeholder(row):
            placeholder_fields.append({
                "field": "placeholderDetected",
                "reason": "row_self_reported_placeholder",
            })
        for field, value in row.items():
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > 500:
                continue
            if PLACEHOLDER_VALUE_RE.search(text) or PLACEHOLDER_URL_RE.search(text):
                placeholder_fields.append({
                    "field": str(field),
                    "value": text[:120],
                    "reason": "placeholder_like_value",
                })
        if placeholder_fields:
            warnings.append({
                "type": "placeholder_like_extraction_value",
                "row": index,
                "fields": placeholder_fields[:5],
            })
    return warnings

def _row_reports_placeholder(row: JsonDict) -> bool:
    for key in (
        "placeholderDetected",
        "placeholder_detected",
        "isPlaceholder",
        "is_placeholder",
        "dataPlaceholder",
        "data_placeholder",
    ):
        value = row.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
            return True
    return False
