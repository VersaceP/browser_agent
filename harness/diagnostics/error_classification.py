"""
harness.diagnostics.error_classification - Structured browser/tool error hints.
"""

from typing import Any, Optional

from harness.results.call_outcome import public_action_failure
from harness.constants import (
    API_CONTRACT_ERROR_MARKERS,
    PAGE_DEAD_OBSERVATION_MARKERS,
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


# --- Structured public failure classification -------------------------------
#
# Every ABCP failure now arrives as a stable public ``error.code``. The private
# ``runtime`` block (code/phase/sideEffectStarted/actionKind) is stripped at the
# transport boundary, so there is nothing else to read; prose matching survives
# only for the few harness-generated errors that never had a code.
#
# The code enum is 263 entries and grows with the platform, so it is NOT
# transcribed here. Only codes that change what the harness DOES get an entry;
# everything else is classified by its family (prefix/suffix), which is a
# property of the naming contract rather than of any one code. An unrecognized
# code still produces a structured classification carrying the code verbatim,
# which is strictly more actionable than the "unknown" prose matching returns.

_RUNTIME_CODE_TYPES = {
    # ABCP publishes two occlusion codes and the harness has to map both:
    # `occlusion_blocked` is what arms the automatic dismiss_overlay recovery in
    # tools/browser_tools/auto_intercept.py, and a code that misses this table
    # falls through to the generic family rule, so the recovery silently never
    # fires. `target-occluded` is the one Input.click actually reports.
    "occluded": ("occlusion_blocked", "refresh_dom_dismiss_overlay_then_retry_once"),
    "target-occluded": ("occlusion_blocked", "refresh_dom_dismiss_overlay_then_retry_once"),
    "renderer-lost": ("render_lost", "rebuild_page_in_same_fleet_then_refresh_targets"),
    "input-host-destroyed": ("render_lost", "rebuild_page_in_same_fleet_then_refresh_targets"),
    "stale-target": ("stale_target", "refresh_ax_tree_then_retarget_once"),
    "target-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "scroll-target-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "scroll-container-not-found": ("target_not_found", "refresh_ax_tree_then_retarget_once"),
    "target-frame-not-found": ("target_frame_not_found", "refresh_ax_tree_then_retarget_once"),
    "invalid-input": ("contract_error", "switch_method_or_report_platform_contract_bug"),
    "invalid-selector": ("contract_error", "switch_method_or_report_platform_contract_bug"),
    "selector-matched-multiple-elements": (
        "target_ambiguous", "narrow_the_selector_or_use_a_canonical_id",
    ),
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
    ("semantic-tree-", "semantic_tree_unavailable", "reinspect_page_state_then_retry_semantic_tree"),
)

_TIMEOUT_SUFFIX = "-timeout"

# The verdict the classifier reaches when NOTHING matched: it restates "read the
# state and do what the platform said" and carries no information the platform's
# own `suggested_prompt` does not already give the model. Every public code ships
# a prompt, so on the model-facing projection this is duplication, not guidance.
# It stays in the internal classification (spawner status, compaction and
# auto-intercept all read `errorClassification`) and is hidden only from the
# model, and only when a platform prompt is present.
GENERIC_PUBLIC_FALLBACK_ACTION = "inspect_page_state_then_follow_platform_guidance"
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
        mapped = ("action_failure", GENERIC_PUBLIC_FALLBACK_ACTION)
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
    if _contains(lower, "err_render_lost"):
        return {
            "type": "render_lost",
            "suggested_action": "rebuild_page_in_same_fleet_then_refresh_targets",
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

    Structured first: when the platform stated a public error code, that is the
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
    if message and _contains(
        message.lower(), "err_page_paused", "paused for human intervention"
    ):
        result["errorClassification"] = classify_browser_error(message, method=method)
        return result
    structured = classify_public_action_failure(public_failure, method=method)
    if structured is not None:
        result["errorClassification"] = structured
        return result
    unknown_method = _unknown_method_classification(result, method=method)
    if unknown_method is not None:
        result["errorClassification"] = unknown_method
        return result
    if not message:
        return result
    result["errorClassification"] = classify_browser_error(message, method=method)
    return result


# ABCP's WebSocket transport — the one the harness uses — answers an unknown
# method with a bare `{"code": -32601, "message": "Action failed"}`: no `data`,
# no public code, no observation, no suggested_prompt. (Its MCP transport does
# return a full envelope; that path is not this one.) This is the only failure
# shape on 1.1.9 where the platform supplies no guidance at all, so it is the
# only one where the harness must author its own. Verified live against
# catalogRevision sha256:cfd8fb90….
_UNKNOWN_METHOD_RPC_CODE = -32601


def _unknown_method_classification(
    result: JsonDict, *, method: str = ""
) -> Optional[JsonDict]:
    if result.get("rpcCode") != _UNKNOWN_METHOD_RPC_CODE:
        return None
    if public_action_failure(result):
        # A build that does supply a public envelope owns the guidance.
        return None
    return {
        "type": "method_not_found",
        "errorCode": "harness:method-not-found",
        "suggested_action": "refresh_capabilities_and_use_a_current_method",
        "method": str(method or result.get("method") or ""),
        # `source` separates this from a platform verdict. The projection uses
        # it to decide that this suggestion is the harness's own and must be
        # shown rather than hidden behind a (nonexistent) platform prompt.
        "source": "harness_fallback",
        "next_instruction": (
            "The transport rejected this method name as unknown and returned no"
            " platform guidance. Call System.getCapabilities to read the"
            " current callable catalog and use a method it returns; do not"
            " guess a name, reuse a removed one, or retry this call unchanged."
        ),
    }


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
