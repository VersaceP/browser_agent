"""
harness.diagnostics.error_classification - Structured browser/tool error hints.
"""

from typing import Any, Optional

from harness.constants import (
    API_CONTRACT_ERROR_MARKERS,
    PAGE_DEAD_OBSERVATION_MARKERS,
    RENDER_LOST_MARKERS,
)
from harness.utils import JsonDict


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
    """Mutate and return result with `errorClassification` when an error exists."""
    if isinstance(result.get("errorClassification"), dict):
        return result
    message = _extract_error_message(result)
    if not message:
        return result
    result["errorClassification"] = classify_browser_error(message, method=method)
    return result


def _extract_error_message(result: JsonDict) -> Optional[str]:
    direct = result.get("error")
    if direct:
        return _stringify_error(direct)
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
