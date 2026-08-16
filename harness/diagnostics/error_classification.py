"""
harness.diagnostics.error_classification - Structured browser/tool error hints.
"""

from typing import Any, Optional

from harness.results.call_outcome import action_runtime_info
from harness.constants import (
    API_CONTRACT_ERROR_MARKERS,
    PAGE_DEAD_OBSERVATION_MARKERS,
    RENDER_LOST_MARKERS,
)
from harness.utils import JsonDict


SELECT_FAILURE_ACTIONS = {
    "select-option-stale": "reinspect_then_retry_once_with_returned_fields",
    "select-option-not-found": "reinspect_query_or_load_more_then_retry_once",
    "select-option-disabled": "stop_and_report_requested_option_unavailable",
    "select-popup-lost": "stop_repeating_and_report_platform_select_failure",
    "select-navigation-stalled": "stop_repeating_and_report_platform_cascade_failure",
}


# --- Structured runtime classification -------------------------------------
#
# ABCP attaches `runtime: {code, phase, sideEffectStarted, actionKind}` to a
# failed action. The prose matching further down predates that block and is
# kept only as the fallback for envelopes that carry no runtime — a message is
# a rendering, and classifying a rendering means re-deriving something the
# platform already decided.
#
# The code enum is ~70 entries and grows with the platform, so it is NOT
# transcribed here. Only codes that change what the harness DOES get an entry;
# everything else is classified by its family (prefix/suffix), which is a
# property of the naming contract rather than of any one code. An unrecognized
# code still produces a structured classification carrying the code verbatim,
# which is strictly more actionable than the "unknown" prose matching returns.

_RUNTIME_CODE_TYPES = {
    "occluded": ("occlusion_blocked", "refresh_dom_dismiss_overlay_then_retry_once"),
    "select-option-occluded": ("occlusion_blocked", "refresh_dom_dismiss_overlay_then_retry_once"),
    "renderer-lost": ("render_lost", "retry_with_render_recovery_or_rebuild_page"),
    "input-host-destroyed": ("render_lost", "retry_with_render_recovery_or_rebuild_page"),
    "stale-target": ("stale_target", "refresh_ax_tree_then_retarget_once"),
    "target-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "scroll-target-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "scroll-container-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "target-frame-not-found": ("target_frame_not_found", "refresh_ax_tree_then_retarget_once"),
    "invalid-input": ("contract_error", "switch_method_or_report_platform_contract_bug"),
    "invalid-selector": ("contract_error", "switch_method_or_report_platform_contract_bug"),
    "selector-ambiguous": ("target_ambiguous", "narrow_the_selector_or_use_a_canonical_id"),
    "coordinate-conversion-failed": (
        "coordinate_unavailable", "stop_using_coordinates_and_target_by_id_or_selector",
    ),
    # Not every drag-* code is a same-document mistake. These two describe an
    # endpoint that went away or a destination that cannot be pinned down, and
    # "keep both endpoints in one document" would be useless advice for them.
    "drag-source-frame-unavailable": (
        "drag_endpoint_lost", "refresh_ax_tree_then_retarget_once",
    ),
    "drag-destination-frame-ambiguous": (
        "drag_endpoint_ambiguous",
        "name_the_destination_with_a_canonical_id_from_the_source_frame",
    ),
}

# Family rules, applied in order when no exact entry matched. Each is a
# statement about the naming contract: drag endpoint errors are unsupported
# geometry rather than transient, a scroll code means the scroll request itself
# was wrong, and anything ending in -timeout timed out.
_RUNTIME_CODE_FAMILIES = (
    ("cross-frame-drag", "drag_unsupported", "stop_and_keep_both_endpoints_in_one_document"),
    ("cross-document-drag", "drag_unsupported", "stop_and_keep_both_endpoints_in_one_document"),
    ("drag-", "drag_unsupported", "stop_and_keep_both_endpoints_in_one_document"),
    ("select-", "select_failure", "reinspect_select_then_retry_once_with_returned_fields"),
    ("scroll-", "scroll_failed", "inspect_viewport_then_correct_the_scroll_request"),
    ("input-", "input_surface_unavailable", "inspect_page_state_before_retrying_input"),
    ("semantic-tree-", "contract_error", "switch_method_or_report_platform_contract_bug"),
)

_TIMEOUT_SUFFIX = "-timeout"

# When the browser had already begun dispatching input, no classification may
# recommend a retry: the action may have taken effect and the receipt simply
# never arrived.
_SIDE_EFFECT_ACTION = "inspect_page_state_and_do_not_replay"


def classify_runtime_error(runtime: Any, *, method: str = "") -> Optional[JsonDict]:
    """Classify a failure from the platform's structured runtime block.

    Returns None when there is no usable code, so the caller can fall back to
    prose rather than manufacturing a verdict from an empty block.
    """
    if not isinstance(runtime, dict):
        return None
    code = str(runtime.get("code") or "").strip()
    if not code or code == "unknown":
        return None
    phase = str(runtime.get("phase") or "").strip()
    side_effect_started = runtime.get("sideEffectStarted") is True

    mapped = _RUNTIME_CODE_TYPES.get(code)
    if mapped is None and code in SELECT_FAILURE_ACTIONS:
        mapped = ("select_failure", SELECT_FAILURE_ACTIONS[code])
    if mapped is None:
        for prefix, error_type, action in _RUNTIME_CODE_FAMILIES:
            if code.startswith(prefix):
                mapped = (error_type, action)
                break
    if mapped is None and code.endswith(_TIMEOUT_SUFFIX):
        mapped = ("timeout", "retry_with_backoff_or_reduce_surface")
    if mapped is None:
        mapped = ("action_runtime_error", "inspect_page_state_then_choose_another_approach")

    error_type, suggested_action = mapped
    if code.endswith(_TIMEOUT_SUFFIX) and error_type == "action_runtime_error":
        error_type = "timeout"
    classification: JsonDict = {
        "type": error_type,
        "errorCode": code,
        "suggested_action": (
            _SIDE_EFFECT_ACTION if side_effect_started else suggested_action
        ),
        "method": str(method or ""),
        "source": "action_runtime",
        "sideEffectStarted": side_effect_started,
    }
    if phase:
        classification["phase"] = phase
    action_kind = str(runtime.get("actionKind") or "").strip()
    if action_kind:
        classification["actionKind"] = action_kind
    return classification


def classify_browser_error(
    error_text: Any,
    *,
    method: str = "",
) -> JsonDict:
    """Classify an error string without replacing the original `error` field.

    HITL/paused signals intentionally win over render/page-dead markers when a
    message contains both. Treating human-intervention state as the primary
    blocker prevents the agent from issuing more browser actions while the page
    may still be gated by a user-visible challenge.
    """
    text = str(error_text or "")
    lower = text.lower()
    method_name = str(method or "")

    if method_name == "DOM.inspectSelect":
        if "select-control-not-visible" in lower:
            return {
                "type": "select_control_not_visible",
                "errorCode": "select-control-not-visible",
                "suggested_action": "refresh_ax_and_target_only_a_visible_select_control",
                "method": method_name,
            }
        if (
            "select control was not found" in lower
            or "no supported accessibility semantics" in lower
        ):
            return {
                "type": "select_control_unsupported",
                "errorCode": "select-control-unsupported",
                "suggested_action": "use_fresh_ax_guided_interaction_for_non_select_ui",
                "method": method_name,
            }

    if method_name == "Input.select":
        for error_code, suggested_action in SELECT_FAILURE_ACTIONS.items():
            if error_code in lower:
                return {
                    "type": error_code.replace("-", "_"),
                    "errorCode": error_code,
                    "suggested_action": suggested_action,
                    "method": method_name,
                }

    if (
        method_name == "Page.create"
        and "-32005" in lower
        and "page.create" in lower
    ):
        return {
            "type": "page_create_failed",
            "suggested_action": "probe_existing_pages_then_reuse_or_abort_worker",
            "method": method_name,
        }
    if _contains(lower, "err_page_paused", "paused for human intervention"):
        return {
            "type": "hitl_paused_state",
            "suggested_action": "wait_for_explicit_hitl_resume_or_quarantine_stale_page",
            "method": method_name,
        }
    if _contains(
        lower,
        "mouse action blocked",
        "target element is occluded",
        "element is occluded",
        "is occluded",
    ):
        return {
            "type": "occlusion_blocked",
            "suggested_action": "refresh_dom_dismiss_overlay_then_retry_once",
            "method": method_name,
        }
    if _contains(lower, "err_render_lost") or _contains_any(text, RENDER_LOST_MARKERS):
        return {
            "type": "render_lost",
            "suggested_action": "retry_with_render_recovery_or_rebuild_page",
            "method": method_name,
        }
    if _contains_any(text, PAGE_DEAD_OBSERVATION_MARKERS):
        return {
            "type": "page_crashed",
            "suggested_action": "rebuild_fleet_or_open_fresh_page",
            "method": method_name,
        }
    if _contains(lower, "err_timeout", "timeout", "timed out"):
        return {
            "type": "timeout",
            "suggested_action": "retry_with_backoff_or_reduce_surface",
            "method": method_name,
        }
    if _contains_any(lower, API_CONTRACT_ERROR_MARKERS):
        return {
            "type": "contract_error",
            "suggested_action": "switch_method_or_report_platform_contract_bug",
            "method": method_name,
        }
    return {
        "type": "unknown",
        "suggested_action": "report_to_lead_with_context",
        "method": method_name,
    }


def attach_error_classification(result: JsonDict, *, method: str = "") -> JsonDict:
    """Mutate and return result with `errorClassification` when an error exists.

    Structured first: when the platform stated a runtime code, that is the
    verdict. HITL/pause is the one exception that still wins over it — a paused
    page blocks every further action regardless of which code the interrupted
    one reported, and treating it as an ordinary action failure would send the
    worker back to clicking a gated page.
    """
    if isinstance(result.get("errorClassification"), dict):
        return result
    message = _extract_error_message(result)
    runtime = action_runtime_info(result)
    if message and _contains(
        message.lower(), "err_page_paused", "paused for human intervention"
    ):
        result["errorClassification"] = classify_browser_error(message, method=method)
        return result
    structured = classify_runtime_error(runtime, method=method)
    if structured is not None:
        result["errorClassification"] = structured
        return result
    if not message:
        return result
    result["errorClassification"] = classify_browser_error(message, method=method)
    return result


def _extract_error_message(result: JsonDict) -> Optional[str]:
    direct = result.get("error")
    rpc_data = result.get("rpcData")
    if direct:
        rendered = _stringify_error(direct)
        if rpc_data is not None:
            rendered = f"{rendered} {_stringify_error(rpc_data)}"
        return rendered
    if rpc_data is not None:
        return _stringify_error(rpc_data)
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return _stringify_error(response.get("error"))
        observation = response.get("observation")
        if isinstance(observation, str) and _looks_like_error(observation):
            return observation
    return None


def _stringify_error(value: Any) -> str:
    if isinstance(value, dict):
        parts = [
            str(value.get(key))
            for key in ("code", "message", "data", "error")
            if value.get(key) is not None
        ]
        return " ".join(parts) if parts else str(value)
    return str(value)


def _looks_like_error(text: str) -> bool:
    lower = text.lower()
    return any(
        marker in lower
        for marker in (
            "err_",
            "error",
            "timed out",
            "timeout",
            "paused for human",
            "occluded",
            "mouse action blocked",
            "method not found",
        )
    )


def _contains(text: str, *needles: str) -> bool:
    return any(needle.lower() in text for needle in needles)


def _contains_any(text: str, markers: Any) -> bool:
    lower = text.lower()
    return any(str(marker).lower() in lower for marker in markers)
