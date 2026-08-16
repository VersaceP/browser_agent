"""
harness.tools.browser_tools.auto_intercept - Automatic overlay interception before pausing for HITL.
"""

from typing import Any
from typing import List
from typing import Optional
from harness.results.call_outcome import replay_forbidden
from harness.observation.overlay_actions import visible_layers_occluded
from harness.utils import JsonDict
from .axtree_state import _invalidate_axtree_snapshot

def _bt():
    import harness.tools.browser_tools as bt

    return bt

AUTO_INTERCEPT_MAX_PER_PAGE = 3

def _auto_intercept_mode(agent: Any) -> str:
    harness = getattr(getattr(agent, "runtime", None), "harness", None)
    mode = str(getattr(harness, "auto_intercept", "p0p1") or "p0p1")
    return mode if mode in {"off", "suggest", "p0", "p0p1"} else "p0p1"

def _blocked_target_id(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    for key in ("id", "nodeId", "targetId", "selector"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

def _record_microloop_telemetry(
    agent: Any,
    loop: str,
    outcome: str,
    detail: Optional[JsonDict] = None,
) -> None:
    """Per-loop micro-loop telemetry. Granularity (one row per loop invocation
    with trigger/outcome) does not fit strategy_telemetry's worker-result rows,
    so this is a dedicated in-memory aggregate + an auditable log event."""
    agg = getattr(agent, "_microloop_telemetry", None)
    if not isinstance(agg, dict):
        agg = {}
        agent._microloop_telemetry = agg
    bucket = agg.setdefault(loop, {})
    bucket["attempts"] = int(bucket.get("attempts", 0)) + 1
    bucket[outcome] = int(bucket.get(outcome, 0)) + 1
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "microloop.telemetry",
            {"loop": loop, "outcome": outcome, **(detail or {})},
        )

async def _maybe_auto_intercept_overlay(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Phase 7.2 auto-intercept. When an action is overlay-blocked and config
    permits, run dismiss_overlay automatically (saving the model a step) instead
    of only suggesting it, then fold an honest digest into the result.

    Triggers, by escalating config mode:
      p0  -> P0: errorClassification == occlusion_blocked on this result
      p0p1 -> also P1: an AXTree layer reports occlusionState == occluded
    P2 (text soft-detect) and P3 (observation keywords) are never auto-run:
    soft text has false positives, so they keep the suggest-only hint.

    Auth/paywall login/provider/payment controls are still never auto-clicked;
    dismiss_overlay runs only its safe rungs there and returns `policy_refused`
    when they do not clear it, and the original error/hint is preserved."""
    if not isinstance(result, dict):
        return result
    mode = _auto_intercept_mode(agent)
    if mode in {"off", "suggest"}:
        return result

    p0 = _bt()._result_occlusion_blocked(result)
    p1 = False
    if mode == "p0p1" and not p0:
        p1 = bool(visible_layers_occluded(_bt()._layers_from_result(result)))
    if not (p0 or p1):
        return result

    page_id = str(params.get("pageId") or "").strip() if isinstance(params, dict) else ""
    if not page_id:
        return result

    counts = getattr(agent, "_auto_intercept_counts", None)
    if not isinstance(counts, dict):
        counts = {}
        agent._auto_intercept_counts = counts
    if int(counts.get(page_id, 0)) >= AUTO_INTERCEPT_MAX_PER_PAGE:
        _record_microloop_telemetry(
            agent, "auto_intercept", "capped", {"pageId": page_id}
        )
        enriched = dict(result)
        enriched["autoIntercept"] = {
            "trigger": "occlusion_blocked" if p0 else "occluded_layers",
            "mode": mode,
            "skipped": "per_page_cap_reached",
            "cap": AUTO_INTERCEPT_MAX_PER_PAGE,
        }
        return enriched
    counts[page_id] = int(counts.get(page_id, 0)) + 1

    trigger = "occlusion_blocked" if p0 else "occluded_layers"
    blocked_target = _blocked_target_id(params)
    # Only Input.click is auto-retry-safe; dismiss_overlay re-checks the target's
    # sensitivity before any retry and returns dismissed_pending_action otherwise.
    # A click whose failure reports `sideEffectStarted` is NOT auto-retry-safe no
    # matter how safe the target looks: input dispatch had already begun, so the
    # click may have landed under the overlay and the retry would be a second
    # one. Clear the overlay anyway — that is useful and side-effect-free — but
    # hand back an unretried action for the caller to judge.
    side_effect_started = replay_forbidden(result)
    target_method = (
        method if method == "Input.click" and not side_effect_started else ""
    )
    dismiss = await _bt()._dismiss_overlay(
        agent,
        {"pageId": page_id, "targetId": blocked_target, "targetMethod": target_method},
        step,
    )
    dismiss_status = str(dismiss.get("status") or "")
    resolved = dismiss_status == "dismissed_and_retried"
    cleared = dismiss_status in {"dismissed", "dismissed_and_retried", "dismissed_pending_action"}
    # The dismiss interacted with the page (clicks/Escape) or could not clear it;
    # either way any snapshot recorded for THIS call (e.g. a DOM.getAXTree tree
    # written by _observe_axtree_state_after just before this) is now stale.
    # Invalidate so the next action re-fetches rather than trusting a pre-dismiss
    # tree. Only a receipt that dispatched nothing at all leaves the snapshot
    # valid: an auth/paywall refusal now still runs the safe rungs, so
    # `policy_refused` normally DID mutate the page. ("blocked" is the legacy
    # zero-attempt shape.)
    dismissed_nothing = (
        dismiss_status == "blocked"
        or str(dismiss.get("dismissOutcome") or "") == "not_attempted"
    )
    if not dismissed_nothing:
        _invalidate_axtree_snapshot(
            agent, "auto_intercept", params if isinstance(params, dict) else {}
        )
    # If the model's own call was DOM.getAXTree and we cleared the overlay, the
    # lines it would read are the PRE-dismiss tree. Re-fetch a fresh tree (no
    # model step), which both replaces those lines below and re-establishes a
    # clean current snapshot, so the model sees the post-dismiss page map and its
    # next action does not trip the stale guard on an obsolete id.
    tree_refreshed = False
    fresh_lines: List[Any] = []
    fresh_data: JsonDict = {}
    if cleared and method == "DOM.getAXTree":
        fresh = await _bt()._invoke_browser_method(
            agent,
            "DOM.getAXTree",
            {"pageId": page_id, "purpose": "auto_intercept: refresh tree after overlay cleared"},
            step,
            count_progress=False,
        )
        candidate_data = _bt()._response_data(fresh)
        fresh_data = candidate_data if isinstance(candidate_data, dict) else {}
        fresh_lines = list(getattr(agent, "axtree_lines", []) or [])
        tree_refreshed = bool(fresh_lines)
    outcome = (
        "resolved" if resolved
        else "cleared" if cleared
        else "blocked" if dismiss_status in {"policy_refused", "blocked"}
        else "failed"
    )
    _record_microloop_telemetry(
        agent,
        "auto_intercept",
        outcome,
        {"pageId": page_id, "trigger": trigger, "dismissStatus": dismiss_status},
    )

    enriched = dict(result)
    # Replace the stale pre-dismiss tree the model would otherwise read with the
    # freshly re-fetched post-dismiss tree. Swap the WHOLE data block (so
    # layers/nodeCount/truncated no longer contradict the refreshed lines — the
    # P1 trigger was a stale layers.occlusionState), then overlay the raw,
    # never-offloaded lines/nodes from the agent snapshot.
    if tree_refreshed:
        response = enriched.get("response")
        if isinstance(response, dict) and isinstance(response.get("data"), dict):
            if fresh_data:
                new_data = dict(fresh_data)
            else:
                new_data = dict(response["data"])
            new_data["lines"] = fresh_lines
            new_data["nodes"] = list(getattr(agent, "axtree_nodes", []) or [])
            response["data"] = new_data
    enriched["autoIntercept"] = {
        "trigger": trigger,
        "mode": mode,
        "dismissStatus": dismiss_status,
        "resolved": resolved,
        "cleared": cleared,
        "retried": bool(dismiss.get("retried")),
        "treeRefreshed": tree_refreshed,
        "overlay": dismiss.get("overlay"),
        "vlArbiter": dismiss.get("vlArbiter"),
        **(
            {"replayForbidden": True, "retrySuppressed": "side_effect_started"}
            if side_effect_started else {}
        ),
    }
    stale_tree_note = ""
    if cleared and method == "DOM.getAXTree" and not tree_refreshed:
        # Could not refresh: be explicit that the returned map is pre-dismiss.
        stale_tree_note = (
            " NOTE: response.data.lines is the PRE-dismiss tree and is now stale;"
            " call DOM.getAXTree again before using any element id from it."
        )
    if resolved:
        instruction = (
            "Occlusion auto-intercept: the overlay was dismissed and your original"
            " action was retried successfully. Continue — do NOT re-issue it."
        )
    elif cleared:
        if method == "DOM.getAXTree" and tree_refreshed:
            instruction = (
                "Occlusion auto-intercept: the overlay was dismissed and"
                " response.data.lines was refreshed to the post-dismiss tree. Use"
                " these ids."
            )
        elif side_effect_started:
            instruction = (
                "Occlusion auto-intercept: the overlay was dismissed, but your"
                " action was NOT retried because the platform reported that"
                " input dispatch had already started — it may have taken effect"
                " under the overlay. Read the page (Page.getState plus a fresh"
                " DOM.getAXTree, or the field/row you were changing) and decide"
                " from what you see; do not re-issue it blind."
            ) + stale_tree_note
        else:
            instruction = (
                "Occlusion auto-intercept: the overlay was dismissed but your action"
                " was not auto-retried (not auto-retry-safe or a consequential"
                " target). Re-issue the action if it is still needed."
            ) + stale_tree_note
    else:
        # blocked (auth/paywall) or failed: keep the original suggest hint intent.
        instruction = (
            "Occlusion auto-intercept ran dismiss_overlay but could not clear the"
            f" overlay (status={dismiss_status or 'unknown'}). It may be an"
            " auth/paywall wall (never auto-clicked); request HITL or report a"
            " blocker."
        )
    existing = str(enriched.get("next_instruction") or "").strip()
    enriched["next_instruction"] = f"{existing} {instruction}".strip() if existing else instruction
    return enriched
