"""Observation-only recovery shared by target actions, without replay policy."""

from harness.observation.scroll_receipt import SCROLL_RECOVERY_FIELDS

TARGET_FAILURE_CODES = frozenset({
    "stale-target", "pointer-target-stale", "target-preparation-failed",
    "target-not-found", "scroll-target-not-found", "scroll-container-not-found",
    "scroll-target-changed-after-input",
})

TARGET_RECOVERY_GUIDANCE = (
    "Inspect current target identity, value and reachability before choosing another action. "
    "A rect with positive dimensions does not prove visibility inside clipping/scroll "
    "ancestors. Locator actions handle target preparation; offscreen geometry alone "
    "does not require a separate pre-scroll. For explicit scrolling, read input.completed, "
    "observation.status and goal.status independently, then requestedDelta versus "
    "observedDelta/totalDelta, layers and resolution. Missing or unavailable observations "
    "remain unknown, not zero movement. Movement does not prove target visibility. "
    "If the target remains unreachable, inspect current identity and scroll ancestors; "
    "use only an observed container id/selector. Preserve target-resolution or execution "
    "failures as such, rather than masking them with repeated input or script fallback. "
    "Check popup expanded state rather than mounted option nodes alone. For keyboard "
    "actions, use a verified target or confirm focus. Verify the resulting selected/displayed "
    "value; action success or menu closure alone does not prove selection. No automatic replay."
)


def _current_ax_target_fact(agent, params: dict) -> dict:
    """Describe viewport intersection from the cached AX observation only."""
    page_id = str(params.get("pageId") or "")
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    container = params.get("container") if isinstance(params.get("container"), dict) else {}
    target_id = str(params.get("id") or target.get("id") or container.get("id") or "")
    if not target_id or str(getattr(agent, "axtree_page_id", "") or "") != page_id:
        return {"status": "unknown", "reason": "no_matching_current_ax_observation"}
    from .axtree_state import _current_axtree_nodes

    nodes = _current_axtree_nodes(agent)
    node = next((item for item in nodes if str(item.get("id") or "") == target_id), None)
    if not isinstance(node, dict):
        return {"status": "unknown", "reason": "target_absent_from_cached_ax_observation"}
    fact = {
        "status": "observed",
        "axtreeEpoch": getattr(agent, "axtree_epoch", None),
        "snapshotInvalidated": bool(getattr(agent, "axtree_invalidated", False)),
        "id": target_id,
        "rect": node.get("rect"),
        "flags": list(node.get("flags") or []),
        "viewportIntersection": "unknown",
        "visibilityWithinScrollAncestors": "unknown",
    }
    rect = node.get("rect") if isinstance(node.get("rect"), dict) else None
    roots = [item.get("rect") for item in nodes
             if item.get("role") == "rootwebarea" and isinstance(item.get("rect"), dict)]
    viewport = max(roots, key=lambda item: float(item.get("w") or 0) * float(item.get("h") or 0)) \
        if roots else None
    if rect and viewport:
        x1 = max(float(rect.get("x") or 0), float(viewport.get("x") or 0))
        y1 = max(float(rect.get("y") or 0), float(viewport.get("y") or 0))
        x2 = min(float(rect.get("x") or 0) + float(rect.get("w") or 0),
                 float(viewport.get("x") or 0) + float(viewport.get("w") or 0))
        y2 = min(float(rect.get("y") or 0) + float(rect.get("h") or 0),
                 float(viewport.get("y") or 0) + float(viewport.get("h") or 0))
        fact["viewportRect"] = viewport
        fact["viewportIntersection"] = "intersects" if x2 > x1 and y2 > y1 else "outside"
    return fact


def attach_target_recovery(agent, method: str, params: dict, result: dict) -> None:
    """Expose only facts from this receipt; missing geometry remains unknown."""
    if not isinstance(result, dict):
        return
    classification = result.get("errorClassification") or {}
    code = classification.get("errorCode") if isinstance(classification, dict) else None
    stale = result.get("status") == "stale_element_reference"
    if method not in {
        "Input.click", "Input.drag", "Input.type", "Input.press",
        "Input.scroll", "Input.select", "DOM.inspectSelect",
    }:
        return
    response = result.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    if method == "Input.scroll" and isinstance(data, dict) and any(
        key in data for key in SCROLL_RECOVERY_FIELDS
    ):
        result["scrollRecoveryObservation"] = {
            key: data[key] for key in SCROLL_RECOVERY_FIELDS if key in data
        }
        result["scrollRecoveryObservation"]["interpretation"] = (
            "Input completion, observation stability and goal attainment are independent. "
            "Requested/sent deltas are not measured movement; missing or unavailable "
            "observations remain unknown. Measured movement alone does not prove target "
            "visibility. Confirmed zero movement can mean a boundary or an unsuitable scroll surface."
        )
    if code not in TARGET_FAILURE_CODES and not stale:
        return
    locators = {key: params[key] for key in ("id", "selector", "target", "container")
                if params.get(key)}
    result["targetRecovery"] = {
        "method": method,
        "pageId": params.get("pageId"),
        "requestedTarget": locators,
        "errorCode": "axtree-stale-reference" if stale else code,
        "identityResolution": "not_confirmed",
        "currentAXTarget": _current_ax_target_fact(agent, params),
        "visibilityWithinScrollAncestors": "unknown",
        "interactability": "unknown",
        "dispatchPosition": "before_dispatch" if result.get("tool_was_executed") is False
                            else "not_publicly_known",
        "guidance": TARGET_RECOVERY_GUIDANCE,
    }
    if not str(result.get("next_instruction") or "").strip():
        result["next_instruction"] = TARGET_RECOVERY_GUIDANCE
