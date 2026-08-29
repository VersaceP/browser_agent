"""
harness.diagnostics.error_classification - Structured browser/tool error hints.
"""

from typing import Any, Optional

from harness.results.call_outcome import action_runtime_info
from harness.results.call_outcome import public_action_failure
from harness.constants import (
    API_CONTRACT_ERROR_MARKERS,
    PAGE_DEAD_OBSERVATION_MARKERS,
    RENDER_LOST_MARKERS,
)
from harness.utils import JsonDict


# Rebuilt select contract (2026-08 platform generation). Two rules govern
# every entry: (1) codes whose semantics are "the popup/option window moved"
# must NOT license an automatic Input.select replay - the platform's own
# suggested prompts say to continue with ONE generic Input.click/type/scroll;
# (2) only codes that mean "your request didn't match the current window"
# allow exactly one reinspect-then-retry.
SELECT_FAILURE_ACTIONS = {
    # One bounded retry: re-read the current window, then retry once with
    # fields that window actually returned.
    "select-option-not-in-current-window": "reinspect_current_window_then_retry_once_with_returned_fields",
    "select-option-id-unavailable": "reinspect_current_window_then_retry_once_with_returned_fields",
    "select-option-label-ambiguous": "read_popup_semantic_tree_then_retry_once_with_optionIds",
    # No Input.select replay: continue with generic input actions.
    "select-agent-takeover-required": "keep_popup_state_and_continue_with_one_generic_input_action",
    "select-popup-not-ready": "observe_popup_with_ax_and_semantic_tree_then_one_bounded_generic_action",
    "select-popup-not-found": "reobserve_page_then_use_generic_input_actions",
    "select-popup-ambiguous": "reobserve_page_then_use_generic_input_actions",
    "select-popup-relation-changed": "reobserve_control_and_popup_then_one_generic_input_action",
    "select-option-id-proof-unavailable": "reobserve_control_state_before_any_further_input",
    # Terminal / contract mismatches: stop and report.
    "select-option-disabled": "stop_and_report_requested_option_unavailable",
    "select-target-not-select": "stop_select_and_use_generic_input_actions",
    "select-selection-mode-unknown": "stop_and_report_platform_select_contract_failure",
    "select-multiple-unsupported": "stop_and_report_unsupported_multi_select",
    "select-control-kind-mismatch": "stop_and_reinspect_the_control_kind",
    "select-state-restore-failed": "reinspect_before_continuing",
    "select-final-state-unproven": "inspect_control_before_any_correction",
}


# --- Structured runtime classification -------------------------------------
#
# Current ABCP builds expose a stable public ``error.code``. Older builds may
# still attach ``runtime``; prose matching remains the last compatibility path.
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
    # Unknown select codes default to guidance, not an automatic Input.select
    # replay: on this contract generation a failed select may already have
    # moved the popup, and the platform's own prompts never say "just retry".
    ("select-", "select_failure", "reinspect_select_then_follow_returned_guidance"),
    ("scroll-", "scroll_failed", "inspect_viewport_then_correct_the_scroll_request"),
    ("input-", "input_surface_unavailable", "inspect_page_state_before_retrying_input"),
    ("semantic-tree-", "contract_error", "switch_method_or_report_platform_contract_bug"),
)

_TIMEOUT_SUFFIX = "-timeout"

# When the browser had already begun dispatching input, no classification may
# recommend a retry: the action may have taken effect and the receipt simply
# never arrived.
_SIDE_EFFECT_ACTION = "inspect_page_state_and_do_not_replay"


# These are client-generated transport codes, not ABCP action-runtime codes.
# Keep them separate from the platform's ``runtime.code`` taxonomy: a socket
# that has already lost its reader cannot be recovered by another browser tool
# call, while an RPC action failure can often be handled by the normal action
# recovery policies above.
_TRANSPORT_ERROR_TYPES = {
    "ABCP_TRANSPORT_CONNECT_FAILED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_NOT_CONNECTED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_CLOSED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_READER_FAILED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_SEND_FAILED": (
        "transport_connection_lost",
        "reconnect_browser_transport_before_rescheduling",
    ),
    "ABCP_TRANSPORT_CALL_TIMEOUT": (
        "transport_timeout",
        "treat_action_outcome_as_unknown_and_do_not_replay_automatically",
    ),
    "ABCP_RPC_ERROR": (
        "rpc_error",
        "follow_method_specific_error_guidance",
    ),
}


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


def classify_public_action_failure(
    failure: Any,
    *,
    method: str = "",
) -> Optional[JsonDict]:
    """Classify the stable public failure envelope without private metadata."""
    if not isinstance(failure, dict):
        return None
    code = str(failure.get("code") or "").strip()
    if not code:
        return None
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
        mapped = (
            "action_failure",
            "inspect_page_state_then_follow_platform_guidance",
        )
    error_type, suggested_action = mapped
    classification: JsonDict = {
        "type": error_type,
        "errorCode": code,
        "suggested_action": suggested_action,
        "method": str(method or failure.get("method") or ""),
        "source": "public_action_failure",
        # With private sideEffectStarted removed, replay safety is unknown.
        "replayForbidden": True,
    }
    platform_prompt = str(failure.get("suggested_prompt") or "").strip()
    if platform_prompt:
        classification["platformSuggestedPrompt"] = platform_prompt[:1000]
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

    if (
        method_name == "DOM.getAXTree"
        and "nodecount=" in lower
        and "no parseable axtree nodes" in lower
    ):
        return {
            "type": "axtree_data_inconsistent",
            "errorCode": "axtree_node_count_parse_mismatch",
            "suggested_action": (
                "report_platform_axtree_data_error_and_do_not_reuse_prior_ids"
            ),
            "method": method_name,
        }

    if method_name == "DOM.inspectSelect":
        # Rebuilt inspect contract: the retired not-visible/unsupported codes
        # are gone. The remaining inspect failures describe the popup binding
        # or the control itself; all of them route to observation, never to an
        # immediate repeat of the same inspect.
        for error_code, suggested_action in (
            ("select-popup-not-found", "reobserve_page_then_use_generic_input_actions"),
            ("select-popup-ambiguous", "reobserve_page_then_use_generic_input_actions"),
            ("select-popup-not-ready", "read_popup_semantic_tree_before_any_further_input"),
            ("select-popup-relation-changed", "reobserve_control_and_popup_then_one_generic_input_action"),
            ("select-target-not-select", "stop_select_and_use_generic_input_actions"),
            ("select-selection-mode-unknown", "stop_and_report_platform_select_contract_failure"),
            ("select-option-id-unavailable", "reinspect_current_window_then_retry_once_with_returned_fields"),
            ("select-option-not-in-current-window", "reinspect_current_window_then_retry_once_with_returned_fields"),
            ("select-state-restore-failed", "reinspect_before_continuing"),
        ):
            if error_code in lower:
                return {
                    "type": error_code.replace("-", "_"),
                    "errorCode": error_code,
                    "suggested_action": suggested_action,
                    "method": method_name,
                }
        # Some platform builds collapse an inspect implementation failure to
        # the bare JSON-RPC envelope, with no public select-* reason code.
        # This is not an unknown application error: repeating the same inspect
        # cannot add information and a successful generic input receipt alone
        # does not prove that a popup ever opened.
        if (
            "-32005" in lower
            and "action dom.inspectselect failed" in lower
        ):
            return {
                "type": "inspect_select_platform_failure",
                "errorCode": "inspect-select-platform-action-failed",
                "suggested_action": (
                    "reobserve_control_then_use_one_generic_input_action"
                    "_with_visible_popup_proof"
                ),
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
        # Family fallback mirrors the runtime classifier: an unrecognized
        # select code still yields a structured classification carrying the
        # code verbatim, and never recommends an automatic Input.select
        # replay (a failed select may already have moved the popup).
        import re as _re

        match = _re.search(r"(select-[a-z]+(?:-[a-z]+)*)", lower)
        if match:
            return {
                "type": "select_failure",
                "errorCode": match.group(1),
                "suggested_action": (
                    "reinspect_select_then_follow_returned_guidance"
                ),
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
    transport_code = str(result.get("transportCode") or "").strip()
    # A server-side JSON-RPC failure still travels through
    # ABCPTransportError, but its action/runtime payload is more specific
    # than this transport wrapper. Keep the wrapper code for audit only and
    # continue into the established method-specific classifier below.
    if transport_code and transport_code != "ABCP_RPC_ERROR":
        error_type, suggested_action = _TRANSPORT_ERROR_TYPES.get(
            transport_code,
            ("transport_error", "report_transport_diagnostics_to_lead"),
        )
        classification: JsonDict = {
            "type": error_type,
            "errorCode": transport_code,
            "suggested_action": suggested_action,
            "method": str(method or result.get("method") or ""),
            "source": "abcp_transport",
        }
        if result.get("exceptionType"):
            classification["exceptionType"] = str(result["exceptionType"])
        if result.get("connectionFatal") is True:
            classification["connectionFatal"] = True
        if isinstance(result.get("requestSent"), bool):
            classification["requestSent"] = result["requestSent"]
        result["errorClassification"] = classification
        return result
    message = _extract_error_message(result)
    public_failure = public_action_failure(result)
    runtime = action_runtime_info(result)
    if message and _contains(
        message.lower(), "err_page_paused", "paused for human intervention"
    ):
        result["errorClassification"] = classify_browser_error(message, method=method)
        return result
    structured = classify_public_action_failure(public_failure, method=method)
    if structured is None:
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
