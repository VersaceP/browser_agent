"""Task-local guided fast-path evidence.

Stage 6B-A deliberately stops at an auditable candidate.  Nothing in this
module executes browser actions.  A later live-validated stage may consume the
candidate, but only after adding its own generation/binding checks.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional

from harness.tools.browser_tools.composites.collect_items import (
    COLLECTION_COMPLETE_STATES,
)


JsonDict = Dict[str, Any]


_COLLECT_SCALAR_KEYS = frozenset({
    "selector",
    "keyField",
    "mode",
    "direction",
    "amount",
    "containerSelector",
    "loadMoreSelector",
    "targetCount",
    "maxRounds",
    "stabilityThreshold",
    "settleMs",
    "harvestLimit",
    "harvestMaxWindows",
    "maxDurationMs",
    "regionId",
    "collectionField",
    "recordName",
})
def trace_params_for_fast_path(tool_name: str, tool_input: Any) -> JsonDict:
    """Return the stable, non-handle subset allowed into a task trace.

    pageId, canonical element ids, coordinates and baseRowRef are intentionally
    absent.  The trace cannot become an executable recipe by accident.
    """

    if tool_name != "collect_items" or not isinstance(tool_input, dict):
        return {}
    params: JsonDict = {}
    for key in _COLLECT_SCALAR_KEYS:
        value = tool_input.get(key)
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            params[key] = value
    fields = tool_input.get("fields")
    if isinstance(fields, dict):
        clean_fields = {
            str(name): str(selector)
            for name, selector in fields.items()
            if str(name).strip() and isinstance(selector, str) and selector.strip()
        }
        if clean_fields:
            params["fields"] = clean_fields
    if str(tool_input.get("collectionField") or "").strip():
        # A different row needs its own validated baseRowRef.  Preserve only
        # the requirement, never the prior row's artifact path.
        params["baseRowBinding"] = "validated_ref_required"
    return params


def _successful_collection_event(event: Any) -> Optional[JsonDict]:
    if not isinstance(event, dict) or event.get("type") != "collect_items":
        return None
    # Trace producers should already persist this allowlisted shape, but compile
    # defensively as well so a legacy/raw trace can never promote page handles,
    # coordinates or baseRowRef paths into an executable receipt.
    params = trace_params_for_fast_path("collect_items", event.get("params"))
    result = event.get("result")
    if not params:
        return None
    if not str(params.get("recordName") or "").strip():
        # Without embedded persistence the model sees only collect_items.sample
        # and a later record step cannot mechanically prove that it preserved
        # the full collection.
        return None
    if not isinstance(result, dict) or str(result.get("status") or "") != "done":
        return None
    collection_state = str(result.get("collectionState") or "")
    if collection_state not in COLLECTION_COMPLETE_STATES:
        return None
    if result.get("contractWarning"):
        return None
    record = result.get("recordExtraction")
    if not isinstance(record, dict) or str(record.get("status") or "") != "done":
        return None
    validation_pending = record.get("validationPending")
    if isinstance(validation_pending, list) and validation_pending:
        return None
    exhaustion_evidence = _safe_exhaustion_evidence(
        result.get("exhaustionEvidence")
    )
    if collection_state == "explicitly_exhausted" and not exhaustion_evidence:
        return None
    return {
        "params": copy.deepcopy(params),
        "collectionState": collection_state,
        "rowCount": max(0, _as_int(result.get("rowCount"))),
        "exhaustionEvidence": exhaustion_evidence,
    }


def _safe_exhaustion_evidence(value: Any) -> Optional[JsonDict]:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip()
    if not kind:
        return None
    return {"kind": kind}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _route_candidate(trace_summary: Any) -> JsonDict:
    if not isinstance(trace_summary, dict):
        return {"preference": "direct"}
    pages = trace_summary.get("contentCompletenessPages")
    if not isinstance(pages, list):
        return {"preference": "direct"}
    for page in reversed(pages):
        if not isinstance(page, dict):
            continue
        receipt = page.get("routePreference")
        if not isinstance(receipt, dict):
            continue
        if str(receipt.get("mode") or "") != "listing_link_click":
            continue
        return {
            "preference": "click_through",
            "cohortKey": str(receipt.get("cohortKey") or "") or None,
            "strategyScope": str(receipt.get("strategyScope") or "") or None,
            "sourceTemplate": str(receipt.get("sourceTemplate") or "") or None,
            "regions": [
                str(item) for item in (receipt.get("regions") or [])
                if str(item).strip()
            ],
            "authGeneration": _as_int(receipt.get("authGeneration")) or None,
        }
    return {"preference": "direct"}


def _declared_ready_markers(
    worker_contract: Any,
    collection_params: JsonDict,
) -> List[str]:
    if not isinstance(worker_contract, dict):
        return []
    config = worker_contract.get("content_completeness")
    if not isinstance(config, dict):
        return []
    region_id = str(collection_params.get("regionId") or "").strip()
    collection_field = str(
        collection_params.get("collectionField") or ""
    ).strip()
    markers: List[str] = []
    for region in config.get("expected_regions") or []:
        if not isinstance(region, dict):
            continue
        fields = {
            str(item).strip()
            for item in (region.get("fields") or [])
            if str(item).strip()
        }
        if (
            region_id
            and str(region.get("id") or "").strip() != region_id
        ):
            continue
        if not region_id and collection_field and collection_field not in fields:
            continue
        for marker in region.get("markers") or []:
            text = str(marker).strip()
            if text and text not in markers:
                markers.append(text)
    return markers[:20]


def assess_fast_path_candidate(
    *,
    trace: Iterable[Any],
    trace_summary: Any,
    worker_contract: Any,
    phase: Any,
    validation: Any,
) -> JsonDict:
    """Compile a non-executable candidate from validated mechanical evidence."""

    role = str(
        (phase.get("execution_role") if isinstance(phase, dict) else "")
        or (
            worker_contract.get("execution_role")
            if isinstance(worker_contract, dict)
            else ""
        )
        or ""
    )
    assessment: JsonDict = {
        "status": "not_compilable",
        "executionPolicy": "not_executable_stage_6b_a",
        "executionRole": role or None,
        "reasons": [],
    }
    if role not in {"probe", "validation", "bulk", "continuation"}:
        assessment["reasons"].append("execution_role_not_checkpoint_advancing")
        return assessment
    if not isinstance(validation, dict) or validation.get("status") != "done":
        assessment["reasons"].append("artifact_validation_not_done")
        return assessment

    successful = [
        candidate
        for event in trace
        if (candidate := _successful_collection_event(event)) is not None
    ]
    if not successful:
        assessment["reasons"].append(
            "no_validated_complete_collect_items_trace"
        )
        return assessment

    collection = successful[-1]
    contract = worker_contract if isinstance(worker_contract, dict) else {}
    strategy_ids = [
        str(item)
        for item in (contract.get("strategy_ids") or [])
        if str(item).strip()
    ]
    candidate = {
        "version": "v1",
        "status": "candidate",
        "executionPolicy": "not_executable_stage_6b_a",
        "coverage": "collection_contract_only",
        "requiresGuidedPrefixDiscovery": True,
        "strategyIds": strategy_ids,
        "route": _route_candidate(trace_summary),
        "detailReadyMarkers": _declared_ready_markers(
            contract,
            collection["params"],
        ),
        "collectionParams": collection["params"],
        "validationEvidence": {
            "collectionState": collection["collectionState"],
            "rowCount": collection["rowCount"],
            "exhaustionEvidence": collection["exhaustionEvidence"],
        },
    }
    assessment.update({
        "status": "candidate",
        "reasons": [],
        "candidate": candidate,
    })
    return assessment
