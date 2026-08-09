"""collect_items composite tool."""

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.constants import COLLECTION_CONTRACT_REPLAN_REQUIRED
from harness.observation.overlay_detector import detect_overlay_from_result
from harness.observation.verifiers import probe_collection_state
from harness.utils import JsonDict, optional_int


def _bt() -> Any:
    import harness.tools.browser_tools as bt

    return bt


async def _invoke_browser_method(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt()._invoke_browser_method(*args, **kwargs)


def _loop_interrupt_from_result(result: Any) -> Optional[JsonDict]:
    return _bt()._loop_interrupt_from_result(result)


def _invoke_result_failed(result: Any) -> bool:
    return _bt()._invoke_result_failed(result)


def _result_occlusion_blocked(result: Any) -> bool:
    return _bt()._result_occlusion_blocked(result)


def build_read_only_oracle(agent: Any, page_id: str, step: int) -> Any:
    return _bt().build_read_only_oracle(agent, page_id, step)


def build_collection_oracle(agent: Any, page_id: str, step: int) -> Any:
    return _bt().build_collection_oracle(agent, page_id, step)


async def collect_rows(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt().collect_rows(*args, **kwargs)


async def verify_overlay_gone(*args: Any, **kwargs: Any) -> Any:
    return await _bt().verify_overlay_gone(*args, **kwargs)


async def _dismiss_overlay(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt()._dismiss_overlay(*args, **kwargs)


def _record_extraction(*args: Any, **kwargs: Any) -> JsonDict:
    return _bt()._record_extraction(*args, **kwargs)


def _record_extraction_persisted(result: JsonDict) -> bool:
    return _bt()._record_extraction_persisted(result)


async def discover_selector_candidates(*args: Any, **kwargs: Any) -> List[JsonDict]:
    return await _bt().discover_selector_candidates(*args, **kwargs)


COLLECT_ITEMS_MAX_ROUNDS = 12
COLLECT_ITEMS_STABILITY_THRESHOLD = 3
COLLECT_ITEMS_MAX_DURATION_MS = 120000
COLLECT_ITEMS_MIN_DURATION_MS = 5000
COLLECT_ITEMS_MAX_CONFIGURED_DURATION_MS = 300000
COLLECT_ITEMS_SETTLE_MS = 600
COLLECT_ITEMS_HARVEST_LIMIT = 200
COLLECT_ITEMS_MAX_WINDOWS = 10  # per-round harvest windows (10 * 200 = 2000 rows/round)
COLLECT_ITEMS_DEFAULT_FIELDS = {"title": "text", "href": "href"}

COLLECTION_COMPLETE_STATES = frozenset({"target_reached", "explicitly_exhausted"})
COLLECTION_BLOCKED_REASONS = frozenset({"overlay_blocked", "overlay_unresolved"})
COLLECTION_TRUNCATION_DOMINATED_REASONS = frozenset({
    "stagnant",
    "max_rounds",
    "scroll_bottom",
    "load_more_exhausted",
    "load_more_click_failed",
    "materialization_action_failed",
})


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


def _collection_state(stop_reason: str, exhaustion_evidence: Optional[JsonDict]) -> str:
    if stop_reason in COLLECTION_BLOCKED_REASONS:
        return "blocked"
    if stop_reason == "target_met":
        return "target_reached"
    if exhaustion_evidence:
        return "explicitly_exhausted"
    return "materialization_stalled"


def _collection_duration_ms(started_at: float) -> int:
    return max(0, int(round((_loop_time() - started_at) * 1000)))


def _may_certify_exhaustion(*, accumulated_count: int, truncated: bool) -> bool:
    """Shared hard precondition for every explicit-exhaustion proof."""
    return accumulated_count > 0 and not truncated


def _validated_base_row(agent: Any, base_row_ref: Any) -> tuple[Optional[JsonDict], Optional[JsonDict]]:
    """Resolve one row from the task's durable validated-artifact ledger.

    An arbitrary local path, a needs-fix attempt, or a current-worker artifact
    is not an acceptable source.  This preflight is intentionally independent
    of browser state so callers can reject before issuing the first RPC.
    """
    if not isinstance(base_row_ref, dict):
        return None, {"status": "rejected", "error": "baseRowRef is required with collectionField"}
    saved_path = str(base_row_ref.get("savedPath") or "").strip()
    row_index = base_row_ref.get("rowIndex")
    if not saved_path:
        return None, {"status": "rejected", "error": "baseRowRef.savedPath is required"}
    if isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0:
        return None, {
            "status": "rejected",
            "error": "baseRowRef.rowIndex must be a non-negative integer",
        }

    task_dir = Path(getattr(getattr(agent, "logger", None), "task_dir", "") or ".").resolve()
    try:
        candidate = Path(saved_path).expanduser().resolve()
        candidate.relative_to((task_dir / "artifacts" / "extractions").resolve())
    except (OSError, ValueError):
        return None, {
            "status": "rejected",
            "error": "baseRowRef must point to an extraction artifact in this task",
        }

    from harness.task_control import load_task_state

    state = load_task_state(agent.logger)
    ledger: set[str] = {
        str(Path(path).expanduser().resolve())
        for path in (state.get("artifacts") or [])
        if str(path or "").strip()
    }
    phases = state.get("phases")
    for phase_state in phases.values() if isinstance(phases, dict) else []:
        if (
            not isinstance(phase_state, dict)
            or str(phase_state.get("status") or "") != "validated_done"
        ):
            continue
        for path in phase_state.get("validated_artifacts") or []:
            if str(path or "").strip():
                ledger.add(str(Path(path).expanduser().resolve()))
    if str(candidate) not in ledger:
        return None, {
            "status": "rejected",
            "error": "baseRowRef.savedPath is not in the task's validated artifact ledger",
        }
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, {"status": "rejected", "error": f"baseRowRef could not be read: {exc}"}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or row_index >= len(rows):
        return None, {"status": "rejected", "error": "baseRowRef.rowIndex is out of range"}
    row = rows[row_index]
    if not isinstance(row, dict):
        return None, {"status": "rejected", "error": "baseRowRef row is not an object"}
    return dict(row), None


def _scroll_exhaustion_evidence(
    probe: JsonDict,
    *,
    stable_rounds: int,
    accumulated_count: int,
    truncated: bool,
    minimum_required_count: int = 0,
) -> Optional[JsonDict]:
    geometry = probe.get("collectionGeometry") if isinstance(probe, dict) else None
    if (
        not isinstance(geometry, dict)
        or not _may_certify_exhaustion(
            accumulated_count=accumulated_count,
            truncated=truncated,
        )
    ):
        return None
    values = [geometry.get("scrollTop"), geometry.get("clientHeight"), geometry.get("scrollHeight")]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    scroll_top, client_height, scroll_height = (float(value) for value in values)
    if not all(math.isfinite(value) and value >= 0 for value in values):
        return None
    item_count = optional_int(geometry.get("itemCount"), 0) or 0
    inside_count = optional_int(geometry.get("itemsInsideContainer"), 0) or 0
    scope = str(geometry.get("scope") or "selector")
    if (
        not geometry.get("present")
        or not geometry.get("connected")
        or scroll_height < client_height
        or item_count <= 0
        or inside_count != item_count
        or (
            scope == "document"
            and minimum_required_count > 0
            and accumulated_count < minimum_required_count
        )
    ):
        return None
    tolerance = max(2.0, client_height * 0.01)
    if scroll_top + client_height < scroll_height - tolerance:
        return None
    return {
        "kind": "scroll_bottom",
        "observed": {
            "scrollTop": scroll_top,
            "clientHeight": client_height,
            "scrollHeight": scroll_height,
            "tolerance": tolerance,
            "stableRounds": stable_rounds,
            "itemCount": item_count,
            "itemsInsideContainer": inside_count,
            "scope": scope,
        },
    }


def _collect_dedup_key(row: JsonDict, key_field: str) -> str:
    """Stable per-row identity for cross-round dedup. Prefer the JS-derived
    __key; fall back to the named key field; finally the whole row so distinct
    keyless rows are not collapsed together."""
    for candidate in (row.get("__key"), row.get(key_field) if key_field else None):
        text = str(candidate or "").strip()
        if text:
            return text
    return json.dumps({k: v for k, v in row.items() if k != "__key"}, sort_keys=True, ensure_ascii=False)


def _collect_expected_exact_rows(agent: Any) -> Optional[int]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return None
    expected = (
        contract.get("expected_artifact")
        if isinstance(contract.get("expected_artifact"), dict)
        else {}
    )
    value = optional_int(expected.get("exact_rows"))
    if value is not None and value > 0:
        return value
    validators = contract.get("validators")
    if not isinstance(validators, list):
        return None
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "exact_rows":
            continue
        value = optional_int(validator.get("value"))
        if value is not None and value > 0:
            return value
    return None


def _collect_expected_fields(agent: Any) -> List[str]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return []
    expected = (
        contract.get("expected_artifact")
        if isinstance(contract.get("expected_artifact"), dict)
        else {}
    )
    fields = expected.get("required_fields")
    if not isinstance(fields, list) or not fields:
        fields = expected.get("fields")
    if not isinstance(fields, list):
        return []
    out: List[str] = []
    seen = set()
    for item in fields:
        if isinstance(item, dict):
            raw = item.get("name") or item.get("field") or item.get("key")
        else:
            raw = item
        text = str(raw or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _collect_expected_artifact(agent: Any) -> JsonDict:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return {}
    expected = contract.get("expected_artifact")
    return dict(expected) if isinstance(expected, dict) else {}


def _collect_declared_fields(expected: JsonDict) -> List[str]:
    """All declared outer fields, including optional fields.

    ``_collect_expected_fields`` intentionally prefers required_fields.  A
    collectionField may be declared but optional, so binding validation needs
    the union instead.
    """
    out: List[str] = []
    seen = set()
    for key in ("fields", "required_fields"):
        raw_fields = expected.get(key)
        if not isinstance(raw_fields, list):
            continue
        for item in raw_fields:
            if isinstance(item, dict):
                raw = item.get("name") or item.get("field") or item.get("key")
            else:
                raw = item
            name = str(raw or "").strip()
            if name and name not in seen:
                out.append(name)
                seen.add(name)
    return out


def _collect_field_spec(expected: JsonDict, field_name: str) -> JsonDict:
    raw_fields = expected.get("fields")
    if not isinstance(raw_fields, list):
        return {}
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("name") or item.get("field") or item.get("key") or ""
        ).strip()
        if name == field_name:
            return dict(item)
    return {}


def _collect_nested_required_fields(field_spec: JsonDict) -> List[str]:
    """Read only explicitly required nested item fields from a field spec."""
    items = field_spec.get("items")
    items = items if isinstance(items, dict) else {}
    candidates: List[Any] = []
    for owner in (field_spec, items):
        for key in ("itemFields", "item_fields", "required_fields", "required"):
            value = owner.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                candidates.extend(value.keys())
    out: List[str] = []
    seen = set()
    for item in candidates:
        if isinstance(item, dict):
            raw = item.get("name") or item.get("field") or item.get("key")
        else:
            raw = item
        name = str(raw or "").strip()
        if name and name not in seen:
            out.append(name)
            seen.add(name)
    return out


def _collect_contract_preflight(
    agent: Any,
    *,
    record_name: str,
    collection_field: str,
    fields: JsonDict,
    base_row: Optional[JsonDict],
) -> Optional[JsonDict]:
    """Reject statically impossible persistence mappings before browser I/O.

    This validates names and row shape only.  It does not certify selector
    quality or invent extraction specs; those still require live evidence.
    """
    if not record_name:
        return None
    expected = _collect_expected_artifact(agent)
    if not expected:
        return None

    expected_name = str(expected.get("name") or "").strip()
    declared_fields = _collect_declared_fields(expected)
    required_fields = _collect_expected_fields(agent)
    supplied_fields = [str(name) for name in fields]
    reasons: List[JsonDict] = []

    if expected_name and record_name != expected_name:
        reasons.append({
            "code": "record_name_mismatch",
            "expected": expected_name,
            "supplied": record_name,
        })

    nested_required: List[str] = []
    if collection_field:
        field_is_declared = collection_field in declared_fields
        if declared_fields and not field_is_declared:
            reasons.append({
                "code": "collection_field_not_declared",
                "field": collection_field,
                "declaredFields": declared_fields,
            })
        field_spec = _collect_field_spec(expected, collection_field)
        declared_type = str(field_spec.get("type") or "").strip().lower()
        if (
            field_is_declared
            and declared_type
            and declared_type not in {"array", "list"}
        ):
            reasons.append({
                "code": "collection_field_not_array",
                "field": collection_field,
                "declaredType": declared_type,
            })
        elif (
            field_is_declared
            and declared_type not in {"array", "list"}
            and not isinstance(field_spec.get("items"), dict)
        ):
            # A bare/unknown field is not proof of a nested collection.  Fail
            # closed before replacing a scalar business field with list rows,
            # but distinguish missing type information from an explicit scalar
            # declaration so the Lead can repair the contract accurately.
            reasons.append({
                "code": "collection_field_array_contract_required",
                "field": collection_field,
                "expectedShape": {
                    "name": collection_field,
                    "type": "array",
                    "items": {"required": ["<nested-field>"]},
                },
            })
        nested_required = _collect_nested_required_fields(field_spec)
        missing_nested = sorted(set(nested_required) - set(supplied_fields))
        if missing_nested:
            reasons.append({
                "code": "nested_item_fields_missing",
                "field": collection_field,
                "missing": missing_nested,
                "supplied": supplied_fields,
            })
        if base_row is not None:
            required_outer = [
                field for field in required_fields if field != collection_field
            ]
            missing_outer = [
                field for field in required_outer if field not in base_row
            ]
            if missing_outer:
                reasons.append({
                    "code": "base_row_required_fields_missing",
                    "missing": missing_outer,
                    "baseRowFields": sorted(str(key) for key in base_row),
                })
    else:
        missing_flat = sorted(set(required_fields) - set(supplied_fields))
        if missing_flat:
            reasons.append({
                "code": "flat_output_fields_missing",
                "missing": missing_flat,
                "supplied": supplied_fields,
            })

    if not reasons:
        return None
    array_contract_reason = next(
        (
            item for item in reasons
            if item.get("code") == "collection_field_array_contract_required"
        ),
        None,
    )
    next_instruction = (
        "Correct recordName and field mapping before retrying. For a"
        " nested collection, collectionField must be a declared outer"
        " array field, fields maps each nested item, and baseRowRef must"
        " resolve to a validated row containing every other required"
        " outer field. For a flat collection, fields must produce every"
        " required artifact field. If this collection can produce only"
        " part of the final row, do not omit recordName: unpersisted"
        " results expose only a sample. Split the collectible fields"
        " into a separate upstream phase/artifact, validate it, then"
        " enrich it in a dependent phase (use baseRowRef for a nested"
        " collection). Do not start materialization until this"
        " preflight passes."
    )
    if isinstance(array_contract_reason, dict):
        next_instruction += (
            " The immutable worker expected_artifact is missing the nested"
            " array contract, so this worker cannot repair it. Stop retrying"
            " and final_answer with status=incomplete and classification="
            f"{COLLECTION_CONTRACT_REPLAN_REQUIRED}; include field and"
            " expectedShape so LeadAgent can replan."
        )
    result: JsonDict = {
        "status": "rejected",
        "error": "collect_items contract preflight failed",
        "contractPreflight": {
            "status": "rejected",
            "reasons": reasons,
            "expectedArtifactName": expected_name or None,
            "expectedOuterFields": required_fields,
            "collectionField": collection_field or None,
            "requiredNestedItemFields": nested_required,
            "suppliedItemFields": supplied_fields,
            "next_instruction": next_instruction,
        },
    }
    if isinstance(array_contract_reason, dict):
        result["classification"] = {
            "category": COLLECTION_CONTRACT_REPLAN_REQUIRED,
            "field": array_contract_reason.get("field"),
            "expectedShape": array_contract_reason.get("expectedShape"),
            "hint": (
                "LeadAgent must replan expected_artifact.fields with the"
                " declared nested array shape before collect_items can persist"
                " this collection."
            ),
        }
    return result


def _collect_contract_warning(
    agent: Any,
    *,
    result: JsonDict,
    record_result: JsonDict,
) -> Optional[JsonDict]:
    exact_rows = _collect_expected_exact_rows(agent)
    expected_fields = _collect_expected_fields(agent)
    validation = (
        record_result.get("artifactValidation")
        if isinstance(record_result.get("artifactValidation"), dict)
        else {}
    )
    failures = [
        failure for failure in (validation.get("failures") or [])
        if isinstance(failure, dict)
    ]
    schema_warnings = [
        warning for warning in (record_result.get("schemaWarnings") or [])
        if isinstance(warning, dict)
    ]
    reasons: List[str] = []
    actual = optional_int(record_result.get("rowCount"), result.get("rowCount"))
    if exact_rows is not None and actual is not None and actual > exact_rows:
        reasons.append("row_count_exceeds_exact_rows")
    failure_types = {
        str(failure.get("type") or "")
        for failure in failures
        if str(failure.get("type") or "")
    }
    if "artifact_required" in failure_types:
        reasons.append("artifact_name_mismatch")
    if "required_fields" in failure_types or "field_nonempty" in failure_types:
        reasons.append("required_fields_missing")
    if schema_warnings:
        reasons.append("schema_warnings")
    if record_result.get("status") == "needs_fix" and not reasons:
        reasons.append("record_extraction_needs_fix")
    if not reasons:
        return None
    warning: JsonDict = {
        "type": "collect_items_contract_mismatch",
        "reasons": sorted(set(reasons)),
        "expectedRows": exact_rows,
        "actualRows": actual,
        "expectedFields": expected_fields,
        "recordStatus": record_result.get("status"),
        "next_instruction": (
            "collect_items gathered rows, but the saved artifact does not satisfy"
            " the worker_contract yet. Do not final_answer with this artifact as"
            " complete target data. If actualRows is greater than expectedRows,"
            " treat the selector as too broad; narrow to the repeated card/row"
            " container or transform/slice trusted DOM-order rows into the exact"
            " expected fields before saving again."
        ),
    }
    if failure_types:
        warning["validationFailureTypes"] = sorted(failure_types)
    return warning


async def _collect_items_materialize(
    agent: Any,
    *,
    mode: str,
    page_id: str,
    step: int,
    direction: str,
    amount: float,
    container_id: str,
    container_selector: str,
    load_more_id: str,
    load_more_selector: str,
) -> JsonDict:
    """Perform one expansion action.

    Action failure is never collection exhaustion.  The caller may certify a
    disappeared/disabled load-more control only through a fresh read-only
    probe and only after at least one successful click.
    """
    if mode == "click_load_more":
        if not load_more_id and not load_more_selector:
            return {"ok": False, "exhausted": False, "detail": "no load-more target"}
        # Try the id first; if it is stale/gone OR the click fails, fall back to
        # the selector before declaring the button exhausted. An occlusion_blocked
        # failure is NOT exhaustion — the button is just covered by an overlay,
        # so signal occlusion and let the caller dismiss + retry.
        if load_more_id:
            result = await _invoke_browser_method(
                agent, "Input.click",
                {"pageId": page_id, "id": load_more_id, "purpose": "collect_items: load more"},
                step, count_progress=False, allow_rematch=True,
            )
            interrupt = _loop_interrupt_from_result(result)
            if interrupt:
                return {"ok": False, "exhausted": False, "interrupt": interrupt}
            if not _invoke_result_failed(result):
                return {"ok": True, "exhausted": False, "detail": "clicked load-more (id)"}
            if _result_occlusion_blocked(result):
                return {"ok": False, "exhausted": False, "occlusion": True, "detail": "load-more occluded (id)"}
            if not load_more_selector:
                return {"ok": False, "exhausted": False, "detail": "load_more id gone/failed"}
            # id failed but a selector fallback exists -> try it below.
        result = await _invoke_browser_method(
            agent, "Input.click",
            {"pageId": page_id, "selector": load_more_selector, "purpose": "collect_items: load more"},
            step, count_progress=False,
        )
        interrupt = _loop_interrupt_from_result(result)
        if interrupt:
            return {"ok": False, "exhausted": False, "interrupt": interrupt}
        if _result_occlusion_blocked(result):
            return {"ok": False, "exhausted": False, "occlusion": True, "detail": "load-more occluded (selector)"}
        if _invoke_result_failed(result):
            return {"ok": False, "exhausted": False, "detail": "load-more click failed"}
        return {"ok": True, "exhausted": False, "detail": "clicked load-more (selector)"}

    # default: scroll. A stale container id must still go through the seen-id
    # rematch guard (Phase 2), not bypass it -> allow_rematch=True.
    params = {"pageId": page_id, "direction": direction or "down",
              "amount": amount, "purpose": "collect_items: scroll"}
    if container_id:
        params["id"] = container_id
    elif container_selector:
        params["selector"] = container_selector
    result = await _invoke_browser_method(
        agent, "Input.scroll", params, step, count_progress=False,
        allow_rematch=bool(container_id),
    )
    interrupt = _loop_interrupt_from_result(result)
    if interrupt:
        return {"ok": False, "exhausted": False, "interrupt": interrupt}
    return {"ok": not _invoke_result_failed(result), "exhausted": False, "detail": "scrolled"}


async def _collect_overlay_recovery(
    agent: Any, page_id: str, oracle: Any, step: int, *, force: bool,
) -> JsonDict:
    """Shared overlay handling for collect_items. `force=True` (used when a
    click reported occlusion_blocked) dismisses directly; force=False (used on a
    plain stall, which may just be end-of-list) first probes verify_overlay_gone
    so we don't run the ladder when there is no overlay. Returns
    {handled, status?, subtype?}."""
    if not force:
        inspect = await _invoke_browser_method(
            agent,
            "DOM.getAXTree",
            {"pageId": page_id, "purpose": "collect_items: native overlay check after stall"},
            step,
            count_progress=False,
        )
        if _invoke_result_failed(inspect) or not isinstance(
            detect_overlay_from_result(inspect), dict
        ):
            return {"handled": False}
    dismissed = await _dismiss_overlay(
        agent, {"pageId": page_id, "targetId": "", "targetMethod": "",
                "maxAttempts": 0, "maxDurationMs": 0}, step,
    )
    recovery = {"handled": True, "status": dismissed.get("status"),
                "subtype": (dismissed.get("overlay") or {}).get("subtype")}
    if dismissed.get("loopInterrupted"):
        # The dismiss ladder itself hit a HITL/challenge interrupt; surface it so
        # the collect loop stops and reports human-needed, not a generic reason.
        recovery["interrupt"] = dismissed
    return recovery


def _collect_overlay_stop_reason(status: Any) -> Optional[str]:
    """Map a dismiss_overlay status to a terminal collect stopReason, or None to
    keep going. Both an auth/paywall refusal (blocked) and a dismiss that ran
    the ladder but could not clear the overlay (failed) must STOP the loop with
    an explicit reason — otherwise an un-cleared overlay keeps getting clicked
    through until max_rounds/stagnant, hiding the real blocker."""
    if status == "blocked":
        return "overlay_blocked"
    if status == "failed":
        return "overlay_unresolved"
    return None


def _collect_interrupt_result(
    agent: Any,
    interrupt: JsonDict,
    *,
    accumulator: Dict[str, JsonDict],
    mode: str,
    selector: str,
    rounds_log: List[JsonDict],
    started_at: float,
    step: int,
) -> JsonDict:
    """collect_items' return when a HITL/challenge interrupt aborts the loop.
    Carries the human-needed summary + rows collected so far; deliberately does
    NOT resync the AXTree (the page is paused) or auto-record the partial rows."""
    collected = list(accumulator.values())
    result: JsonDict = {
        **interrupt,
        "stopReason": interrupt.get("status"),
        "mode": mode,
        "selector": selector,
        "rowCount": len(collected),
        "partial": True,
        "collectionState": "blocked",
        "exhaustionEvidence": None,
        "durationMs": _collection_duration_ms(started_at),
        "roundsUsed": len(rounds_log),
        "rounds": rounds_log[-COLLECT_ITEMS_MAX_ROUNDS:],
        "sample": collected[:3],
    }
    if collected:
        agent.pending_unrecorded_extraction = {
            "source": "collect_items",
            "step": step,
            "rowCount": len(collected),
            "turns": 0,
            "collectionState": "blocked",
        }
        result["next_step"] = (
            "Collection was interrupted. The partial rows were not persisted;"
            " resolve the blocker and resume collection."
        )
    agent.logger.write(
        "collect_items.result", {k: v for k, v in result.items() if k not in {"sample"}}
    )
    return result


async def _collect_items(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    """Composite tool: materialize -> harvest -> dedup loop, internally, costing
    no model step. Each round harvests rows through the read-only oracle and
    dedups by stable key; an overlay is probed when a round stalls (the cheap
    signal that expansion is blocked) or handled directly when a load-more click
    reports occlusion_blocked."""
    page_id = str(tool_input.get("pageId") or "").strip()
    selector = str(tool_input.get("selector") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not selector:
        return {"status": "failed", "error": "selector is required"}

    record_name = str(tool_input.get("recordName") or "").strip()
    collection_field = str(tool_input.get("collectionField") or "").strip()
    base_row: Optional[JsonDict] = None
    base_row_source = ""
    if collection_field:
        if not record_name:
            return {
                "status": "rejected",
                "error": "recordName is required with collectionField",
            }
        base_ref = tool_input.get("baseRowRef")
        base_row, base_error = _validated_base_row(agent, base_ref)
        if base_error is not None:
            return base_error
        base_row_source = str(base_ref.get("savedPath") or "") if isinstance(base_ref, dict) else ""

    mode = str(tool_input.get("mode") or "scroll").strip() or "scroll"
    if mode not in {"scroll", "click_load_more"}:
        return {
            "status": "failed",
            "error": f"unsupported mode {mode!r}; v1 supports scroll | click_load_more",
        }
    fields = tool_input.get("fields")
    if not isinstance(fields, dict) or not fields:
        fields = dict(COLLECT_ITEMS_DEFAULT_FIELDS)
    contract_error = _collect_contract_preflight(
        agent,
        record_name=record_name,
        collection_field=collection_field,
        fields=fields,
        base_row=base_row,
    )
    if contract_error is not None:
        return contract_error
    key_field = str(tool_input.get("keyField") or "href").strip()
    direction = str(tool_input.get("direction") or "down").strip() or "down"
    amount = float(tool_input.get("amount") or 0) or 800.0
    container_id = str(tool_input.get("containerId") or "").strip()
    container_selector = str(tool_input.get("containerSelector") or "").strip()
    load_more_id = str(tool_input.get("loadMoreId") or "").strip()
    load_more_selector = str(tool_input.get("loadMoreSelector") or "").strip()
    target_count = optional_int(tool_input.get("targetCount"), 0) or 0
    collection_contract: JsonDict = {}
    tracker = getattr(agent, "content_completeness_tracker", None)
    if (
        tracker is not None
        and getattr(tracker, "enabled", False)
        and hasattr(tracker, "collection_contract")
    ):
        try:
            candidate = tracker.collection_contract(tool_input)
            if isinstance(candidate, dict):
                collection_contract = dict(candidate)
        except (TypeError, ValueError):
            collection_contract = {}
    declared_min_records = optional_int(
        collection_contract.get("minRecords"), 0
    ) or 0
    effective_target_count = max(target_count, declared_min_records)
    max_rounds = optional_int(tool_input.get("maxRounds"), 0) or 0
    if max_rounds <= 0:
        max_rounds = COLLECT_ITEMS_MAX_ROUNDS
    max_rounds = max(1, min(max_rounds, 50))
    stability_threshold = optional_int(tool_input.get("stabilityThreshold"), 0) or 0
    if stability_threshold <= 0:
        stability_threshold = COLLECT_ITEMS_STABILITY_THRESHOLD
    settle_ms = optional_int(tool_input.get("settleMs"), COLLECT_ITEMS_SETTLE_MS) or COLLECT_ITEMS_SETTLE_MS
    harvest_limit = optional_int(tool_input.get("harvestLimit"), COLLECT_ITEMS_HARVEST_LIMIT) or COLLECT_ITEMS_HARVEST_LIMIT
    harvest_max_windows = max(1, min(optional_int(tool_input.get("harvestMaxWindows"), COLLECT_ITEMS_MAX_WINDOWS) or COLLECT_ITEMS_MAX_WINDOWS, 50))
    max_duration_ms = optional_int(tool_input.get("maxDurationMs"), 0) or COLLECT_ITEMS_MAX_DURATION_MS
    max_duration_ms = max(
        COLLECT_ITEMS_MIN_DURATION_MS,
        min(max_duration_ms, COLLECT_ITEMS_MAX_CONFIGURED_DURATION_MS),
    )
    started_at = _loop_time()
    deadline = started_at + max_duration_ms / 1000.0

    collection_oracle = build_collection_oracle(agent, page_id, step)

    accumulator: Dict[str, JsonDict] = {}
    rounds_log: List[JsonDict] = []
    overlay_encountered: Optional[JsonDict] = None
    exhaustion_evidence: Optional[JsonDict] = None
    successful_load_more_clicks = 0
    truncated = False

    # Force a layout viewport (fresh tabs have none until DOM.getAXTree) so the
    # scroll has a viewport and rect-dependent overlay checks are valid.
    layout = await _invoke_browser_method(
        agent, "DOM.getAXTree",
        {"pageId": page_id, "purpose": "collect_items: force layout + baseline"},
        step, count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(layout)
    if interrupt:
        # Page is already challenge-paused before collection began.
        return _collect_interrupt_result(
            agent, interrupt, accumulator=accumulator,
            mode=mode, selector=selector, rounds_log=rounds_log,
            started_at=started_at, step=step,
        )

    async def harvest() -> JsonDict:
        # Window through the full matched count: a plain (non-virtualized)
        # append list can hold more than harvest_limit nodes at once, and a
        # single slice(0, limit) would re-read the same head every round while
        # the tail never enters the accumulator. Page by offset until we have
        # covered `count` (bounded by harvest_max_windows so a pathological
        # page can't loop unboundedly — then report truncated).
        nonlocal truncated
        all_rows: List[JsonDict] = []
        offset = 0
        count = 0
        last: JsonDict = {}
        for _ in range(harvest_max_windows):
            last = await collect_rows(
                collection_oracle=collection_oracle,
                selector=selector, fields=fields,
                key_field=key_field, offset=offset, limit=harvest_limit,
            )
            if last.get("status") != "done":
                return last if not all_rows else {
                    "status": "done", "count": count, "rows": all_rows,
                    "partialWindowError": last.get("reason"),
                    "partialWindowErrorCode": last.get("oracleErrorCode"),
                }
            count = int(last.get("count") or 0)
            window = last.get("rows") or []
            all_rows.extend(window)
            offset += len(window)
            if len(window) < harvest_limit or offset >= count:
                break
        else:
            # Loop exhausted windows without covering count.
            if offset < count:
                truncated = True
        return {"status": "done", "count": count, "rows": all_rows}

    def absorb(rows: List[JsonDict]) -> int:
        added = 0
        for row in rows:
            key = _collect_dedup_key(row, key_field)
            if key not in accumulator:
                accumulator[key] = {k: v for k, v in row.items() if k != "__key"}
                added += 1
        return added

    baseline = await harvest()
    if baseline.get("status") != "done":
        return {
            "status": "failed",
            "error": f"baseline harvest failed: {baseline.get('reason') or baseline.get('status')}",
            "oracleErrorCode": baseline.get("oracleErrorCode"),
            "selector": selector,
            "collectionState": "materialization_stalled",
            "exhaustionEvidence": None,
            "durationMs": _collection_duration_ms(started_at),
        }
    absorb(baseline.get("rows") or [])

    stop_reason = "max_rounds"
    stagnant = 0
    for round_index in range(1, max_rounds + 1):
        round_started_at = _loop_time()
        if _loop_time() >= deadline:
            stop_reason = "time_budget"
            break
        if effective_target_count and len(accumulator) >= effective_target_count:
            stop_reason = "target_met"
            break

        action = await _bt()._collect_items_materialize(
            agent, mode=mode, page_id=page_id, step=step, direction=direction,
            amount=amount, container_id=container_id, container_selector=container_selector,
            load_more_id=load_more_id, load_more_selector=load_more_selector,
        )
        if action.get("interrupt"):
            # HITL/challenge fired mid-scroll: stop now with a human-needed
            # summary instead of continuing to scroll a paused page.
            return _collect_interrupt_result(
                agent, action["interrupt"], accumulator=accumulator,
                mode=mode, selector=selector, rounds_log=rounds_log,
                started_at=started_at, step=step,
            )
        if action.get("occlusion"):
            # The expansion control is covered by an overlay — recover rather
            # than mistaking it for exhaustion, then retry on the next round.
            recovery = await _bt()._collect_overlay_recovery(agent, page_id, None, step, force=True)
            if recovery.get("interrupt"):
                return _collect_interrupt_result(
                    agent, recovery["interrupt"], accumulator=accumulator,
                    mode=mode, selector=selector, rounds_log=rounds_log,
                    started_at=started_at, step=step,
                )
            overlay_encountered = {"round": round_index, "status": recovery.get("status"),
                                   "subtype": recovery.get("subtype")}
            rounds_log.append({"round": round_index, "action": action.get("detail"),
                               "occlusion": True, "overlay": overlay_encountered,
                               "durationMs": _collection_duration_ms(round_started_at)})
            terminal = _collect_overlay_stop_reason(recovery.get("status"))
            if terminal:
                stop_reason = terminal
                break
            continue
        if not action.get("ok"):
            if (
                mode == "click_load_more"
                and successful_load_more_clicks > 0
                and load_more_selector
                and _may_certify_exhaustion(
                    accumulated_count=len(accumulator),
                    truncated=truncated,
                )
            ):
                probe = await probe_collection_state(
                    collection_oracle=collection_oracle,
                    item_selector=selector,
                    load_more_selector=load_more_selector,
                )
                control = probe.get("loadMore") if isinstance(probe, dict) else None
                if (
                    probe.get("status") == "done"
                    and isinstance(control, dict)
                    and (not control.get("present") or control.get("disabled"))
                ):
                    exhaustion_evidence = {
                        "kind": "load_more_absent",
                        "observed": {
                            "successfulClicks": successful_load_more_clicks,
                            "present": bool(control.get("present")),
                            "connected": bool(control.get("connected")),
                            "disabled": bool(control.get("disabled")),
                        },
                    }
                    stop_reason = "load_more_exhausted"
                else:
                    stop_reason = "load_more_click_failed"
            else:
                stop_reason = (
                    "load_more_click_failed"
                    if mode == "click_load_more"
                    else "materialization_action_failed"
                )
            rounds_log.append({
                "round": round_index,
                "action": action.get("detail"),
                "actionFailed": True,
                "exhaustionEvidence": exhaustion_evidence,
                "durationMs": _collection_duration_ms(round_started_at),
            })
            break
        if mode == "click_load_more":
            successful_load_more_clicks += 1
        await asyncio.sleep(max(0.0, settle_ms / 1000.0))

        snapshot = await harvest()
        if snapshot.get("status") != "done":
            rounds_log.append({
                "round": round_index,
                "harvest": snapshot.get("status"),
                "durationMs": _collection_duration_ms(round_started_at),
            })
            stagnant += 1
            if stagnant >= stability_threshold:
                stop_reason = "harvest_unavailable"
                break
            continue
        added = absorb(snapshot.get("rows") or [])
        rounds_log.append({
            "round": round_index, "added": added,
            "domCount": snapshot.get("count"), "total": len(accumulator),
            "durationMs": _collection_duration_ms(round_started_at),
        })

        if added > 0:
            stagnant = 0
            continue

        # Stall: before counting it, check whether an overlay is blocking
        # expansion. This ties the (cheap) overlay probe to the stall signal.
        recovery = await _bt()._collect_overlay_recovery(agent, page_id, None, step, force=False)
        if recovery.get("interrupt"):
            return _collect_interrupt_result(
                agent, recovery["interrupt"], accumulator=accumulator,
                mode=mode, selector=selector, rounds_log=rounds_log,
                started_at=started_at, step=step,
            )
        if recovery.get("handled"):
            overlay_encountered = {"round": round_index, "status": recovery.get("status"),
                                   "subtype": recovery.get("subtype")}
            rounds_log[-1]["overlay"] = overlay_encountered
            terminal = _collect_overlay_stop_reason(recovery.get("status"))
            if terminal:
                stop_reason = terminal
                break
            # Overlay cleared; don't count this round as stagnation.
            continue

        stagnant += 1
        if stagnant >= stability_threshold:
            # A deadline reached before stable exhaustion is certified wins;
            # bottom geometry observed incidentally at timeout is not evidence.
            if _loop_time() >= deadline:
                stop_reason = "time_budget"
                break
            if mode == "scroll":
                probe = await probe_collection_state(
                    collection_oracle=collection_oracle,
                    item_selector=selector,
                    container_selector=container_selector,
                )
                if probe.get("status") == "done":
                    exhaustion_evidence = _scroll_exhaustion_evidence(
                        probe,
                        stable_rounds=stagnant,
                        accumulated_count=len(accumulator),
                        truncated=truncated,
                        minimum_required_count=effective_target_count,
                    )
            if exhaustion_evidence:
                stop_reason = "scroll_bottom"
                if rounds_log:
                    rounds_log[-1]["exhaustionEvidence"] = exhaustion_evidence
            else:
                stop_reason = "stagnant"
            break

    # Resync the AXTree id cache that scroll/click invalidated.
    resync = await _invoke_browser_method(
        agent, "DOM.getAXTree",
        {"pageId": page_id, "purpose": "collect_items: resync after collection"},
        step, count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(resync)
    if interrupt:
        # A challenge surfaced as collection finished. Keep the rows gathered so
        # far but report human-needed (and do not auto-record partial data).
        return _collect_interrupt_result(
            agent, interrupt, accumulator=accumulator,
            mode=mode, selector=selector, rounds_log=rounds_log,
            started_at=started_at, step=step,
        )

    collected = list(accumulator.values())
    if truncated and (
        exhaustion_evidence is not None
        or stop_reason in COLLECTION_TRUNCATION_DOMINATED_REASONS
    ):
        # The DOM held more matched items than the per-round window budget could
        # read, so the tail was never harvested.  This final invariant catches
        # both current and future evidence kinds even if an entry guard regresses.
        stop_reason = "harvest_limit_reached"
        exhaustion_evidence = None
    collection_state = _collection_state(stop_reason, exhaustion_evidence)
    result: JsonDict = {
        "status": "blocked" if collection_state == "blocked" else "done",
        "stopReason": stop_reason,
        "collectionState": collection_state,
        "exhaustionEvidence": exhaustion_evidence,
        "mode": mode,
        "selector": selector,
        "rowCount": len(collected),
        "requestedTargetCount": target_count,
        "declaredMinRecords": declared_min_records,
        "effectiveTargetCount": effective_target_count,
        "truncated": truncated,
        "maxDurationMs": max_duration_ms,
        "roundsUsed": len(rounds_log),
        "rounds": rounds_log[-COLLECT_ITEMS_MAX_ROUNDS:],
        "sample": collected[:3],
        "overlayEncountered": overlay_encountered,
    }
    if truncated:
        result["next_step"] = (
            "More matched items exist than were harvested (raise harvestLimit or"
            " harvestMaxWindows, or narrow the selector); collection is incomplete."
        )
    # Offer repeated-row selector candidates ONLY on a genuine selector-no-match
    # terminal: 0 rows, a clean "done" exhaustion (not blocked/interrupted), no
    # truncation, and no overlay blocker — otherwise the real blocker (overlay,
    # time budget, harvest limit) must stand, not be masked by "try another
    # selector". Gated internal getSemanticTree, one-shot+cached, digest only.
    selector_no_match = (
        not collected
        and result["status"] == "done"
        and collection_state == "materialization_stalled"
        and stop_reason in {
            "stagnant",
            "max_rounds",
            "load_more_click_failed",
            "materialization_action_failed",
        }
        and not truncated
        and overlay_encountered is None
    )
    if selector_no_match:
        candidates = await discover_selector_candidates(agent, page_id)
        if candidates:
            result["selectorCandidates"] = candidates
            # Only recommend USABLE (non-truncated) selectors for a retry; a
            # truncated one is incomplete CSS.
            usable = [c for c in candidates if not c.get("truncated")][:3]
            action_failure_prefix = (
                f"Materialization action failed ({stop_reason}). "
                if stop_reason in {
                    "load_more_click_failed",
                    "materialization_action_failed",
                }
                else ""
            )
            if usable:
                top = ", ".join(f"{c['cssSelector']} (x{c['count']})" for c in usable)
                result["next_step"] = (
                    f"{action_failure_prefix}Selector {selector!r} matched 0 rows."
                    " Repeated page structures"
                    f" suggest these row selectors: {top}. Re-call collect_items with"
                    " one of them (verify it carries the fields you need)."
                )
            else:
                result["next_step"] = (
                    f"{action_failure_prefix}Selector {selector!r} matched 0 rows."
                    " Repeated structures exist"
                    " but their class selectors are abnormally long/unstable; inspect"
                    " the page or use a structural (nth-child/tag) selector."
                )

    rows_to_record: List[JsonDict]
    if collection_field and base_row is not None:
        outer_row = dict(base_row)
        outer_row[collection_field] = collected
        rows_to_record = [outer_row]
    else:
        rows_to_record = collected

    may_persist = collection_state in COLLECTION_COMPLETE_STATES
    if record_name and may_persist:
        record_result = _record_extraction(
            agent,
            {
                "name": record_name,
                "rows": rows_to_record,
                "schema": {"source": "collect_items", "selector": selector,
                           "mode": mode, "fields": fields,
                           "collectionField": collection_field or None,
                           "baseRowSource": base_row_source or None,
                           "collectionState": collection_state,
                           "exhaustionEvidence": exhaustion_evidence},
                "description": f"collect_items rows for {selector!r} via {mode}",
            },
        )
        result["recordExtraction"] = record_result
        contract_warning = _collect_contract_warning(
            agent, result=result, record_result=record_result
        )
        if contract_warning is not None:
            candidates = await discover_selector_candidates(agent, page_id, top=5)
            if candidates:
                contract_warning["selectorCandidates"] = candidates[:5]
            result["contractWarning"] = contract_warning
            result["next_step"] = contract_warning["next_instruction"]
        if _record_extraction_persisted(record_result):
            agent.pending_unrecorded_extraction = None
    elif collected or (collection_field and base_row is not None):
        agent.pending_unrecorded_extraction = {
            "source": "collect_items", "step": step,
            "rowCount": len(collected), "turns": 0,
            "collectionState": collection_state,
            "collectionField": collection_field or None,
        }
        if may_persist:
            result["next_step"] = (
                "Rows were collected but recordName was not provided, so only"
                " a 3-row sample is visible and nothing was persisted. Re-run"
                " this collection with recordName bound to the declared"
                " artifact, or split it into its own upstream phase/artifact."
                " Do not record the sample as target data."
            )
        else:
            result["next_step"] = (
                "Collection is incomplete and was not persisted. Correct the"
                " materialization target or resolve the blocker, then continue;"
                " do not save the current rows as complete target data."
            )
    result["durationMs"] = _collection_duration_ms(started_at)
    agent.logger.write("collect_items.result", {
        k: v for k, v in result.items() if k not in {"sample"}
    })
    return result
