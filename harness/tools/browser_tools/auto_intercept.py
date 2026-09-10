"""
harness.tools.browser_tools.auto_intercept - Automatic overlay interception before pausing for HITL.
"""

from typing import Any
from typing import List
from typing import Optional
from harness.observation.overlay_actions import visible_layers_occluded
from harness.utils import JsonDict
from .axtree_state import _invalidate_axtree_snapshot

def _bt():
    import harness.tools.browser_tools as bt

    return bt

AUTO_INTERCEPT_MAX_PER_PAGE = 3

def _auto_intercept_mode(agent: Any) -> str:
    harness = getattr(getattr(agent, "runtime", None), "harness", None)
    # An unreadable or misspelt mode falls back to the least intrusive setting
    # that still reports what was seen, not to the one that acts on the page.
    mode = str(getattr(harness, "auto_intercept", "suggest") or "suggest")
    return mode if mode in {"off", "suggest", "p0", "p0p1"} else "suggest"


def _attach_overlay_observation(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> JsonDict:
    """Report an occlusion signal in `suggest` mode without acting on it.

    Handing the decision back to the model is only an improvement if the model
    still learns what was seen. The P1 signal - an AX layer reporting
    occlusionState=occluded with no error classification - reaches the receipt
    by no other route, so dropping the automatic dismissal without this would
    drop the observation along with it, which is a worse trade than the
    automation it replaces.
    """

    p0 = _bt()._result_occlusion_blocked(result)
    p1 = bool(visible_layers_occluded(_bt()._layers_from_result(result)))
    if not (p0 or p1):
        return result
    page_id = (
        str(params.get("pageId") or "").strip() if isinstance(params, dict) else ""
    )
    trigger = "occlusion_blocked" if p0 else "occluded_layers"
    enriched = dict(result)
    enriched["overlayObservation"] = {
        "trigger": trigger,
        "mode": "suggest",
        "fact": (
            "An overlay signal was observed on this page. Nothing was dismissed"
            " and nothing was retried; the harness reports the signal and the"
            " next action is yours."
        ),
        # A candidate action must be directly callable: every field the
        # dismiss_overlay schema requires is present, and targetMethod carries
        # the REAL method. An empty targetMethod is read by the tool as
        # Input.click, so a blocked Input.type copied verbatim from here would
        # have clicked the element instead of declining the replay.
        "candidateActions": [{
            "tool": "dismiss_overlay",
            "pageId": page_id,
            "targetId": _blocked_target_id(params),
            "targetMethod": method,
            "maxAttempts": 0,
            "maxDurationMs": 0,
        }],
        "safetyBoundary": (
            "dismiss_overlay never auto-clicks login/payment/provider buttons"
            " and never auto-retries consequential targets."
        ),
    }
    _record_microloop_telemetry(
        agent,
        "auto_intercept",
        "suggested",
        {"pageId": page_id or None, "trigger": trigger},
    )
    return enriched

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


def _overlay_adjudication_state_key(agent: Any, page_id: str) -> tuple:
    """Bind a cached VL verdict to one observed page state.

    An occlusion failure happens before the pointer action dispatches, so a
    second attempt in the same AXTree epoch has no new visual evidence. A fresh
    AXTree observation increments the epoch and requires a new verdict.
    """
    return (
        page_id,
        str(getattr(agent, "axtree_page_id", "") or ""),
        int(getattr(agent, "axtree_epoch", 0) or 0),
        bool(getattr(agent, "axtree_invalidated", False)),
    )


async def _adjudicate_occluded_target(
    agent: Any,
    page_id: str,
    method: str,
    params: JsonDict,
    step: int,
) -> JsonDict:
    """Use VL once to classify the cover currently blocking a target."""
    harness = getattr(getattr(agent, "runtime", None), "harness", None)
    vl_config = getattr(harness, "vl", None)
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return {"status": "skipped", "reason": "vl_disabled", "reused": False}

    cache = getattr(agent, "_overlay_vl_adjudications", None)
    if not isinstance(cache, dict):
        cache = {}
        agent._overlay_vl_adjudications = cache
    state_key = _overlay_adjudication_state_key(agent, page_id)
    cached = cache.get(state_key)
    if isinstance(cached, dict):
        return {**cached, "reused": True}

    target_id = _blocked_target_id(params)
    agent.overlay_adjudicating = True
    try:
        verdict = await _bt()._visual_verify(
            agent,
            {
                "pageId": page_id,
                "selector": "",
                "id": "",
                "fullPage": False,
                "mode": "overlay_adjudicate",
                "_force": True,
                "question": (
                    "The current browser action was rejected as occluded."
                    " Determine whether the visible surface blocks this target"
                    " and whether automation can safely continue without a human."
                ),
                "expected": {
                    "pageId": page_id,
                    "triggerMethod": method,
                    "blockedTargetId": target_id or None,
                    "occlusionSignal": "browser_action_rejected",
                },
            },
            step,
        )
    finally:
        agent.overlay_adjudicating = False
    if not isinstance(verdict, dict):
        verdict = {"status": "failed", "error": "invalid_visual_verdict"}
    if str(verdict.get("status") or "") == "done":
        cache[state_key] = dict(verdict)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "vl.overlay_adjudication",
            {
                "pageId": page_id,
                "triggerMethod": method,
                "blockedTargetId": target_id or None,
                "state": {
                    "axtreePageId": state_key[1] or None,
                    "axtreeEpoch": state_key[2],
                    "axtreeInvalidated": state_key[3],
                },
                "status": verdict.get("status"),
                "surface": verdict.get("surface"),
                "purpose": verdict.get("purpose"),
                "targetAccess": verdict.get("targetAccess"),
                "recommendedAction": verdict.get("recommendedAction"),
            },
        )
    return {**verdict, "reused": False}


def _overlay_recovery_action(adjudication: JsonDict) -> str:
    """Accept a recovery only when the semantic verdict is self-consistent."""
    if str(adjudication.get("status") or "") != "done":
        # Keep the opt-in auto-intercept's existing safe-rung behavior when VL
        # is unavailable. It never submits, signs in, or retries a sensitive
        # control; an unavailable model is never treated as proof of a gate.
        return "legacy_safe_recovery"
    action = str(adjudication.get("recommendedAction") or "observe")
    purpose = str(adjudication.get("purpose") or "unknown")
    access = str(adjudication.get("targetAccess") or "uncertain")
    continuation = str(adjudication.get("canContinueWithoutUserAction") or "uncertain")
    if action == "hitl" and (
        purpose in {"authentication", "verification"}
        and access == "blocked"
        and continuation == "no"
    ):
        return "hitl"
    if action == "safe_dismiss" and (
        purpose == "routine"
        and access == "blocked"
        and continuation == "yes"
    ):
        return "safe_dismiss"
    return "observe"

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
    if mode == "off":
        return result
    if mode == "suggest":
        return _attach_overlay_observation(agent, method, params, result)

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
    if p0:
        # P0 is a real rejected action with a target whose accessibility the VL
        # can judge. P1 is merely a proactive AX-layer signal, so it retains the
        # existing safe dismissal ladder and cannot synthesize an auth HITL.
        adjudication = await _adjudicate_occluded_target(
            agent, page_id, method, params, step,
        )
        recovery_action = _overlay_recovery_action(adjudication)
    else:
        adjudication = {"status": "skipped", "reason": "no_blocked_action"}
        recovery_action = "legacy_safe_recovery"
    if recovery_action == "hitl":
        reason = str(adjudication.get("reason") or "").strip()
        gate_purpose = str(adjudication.get("purpose") or "authentication")
        auto_hitl = await _bt()._request_hitl_for_challenge(
            agent,
            page_id,
            method,
            step,
            reason=(
                reason or
                "Visual adjudication found an authentication or verification"
                " gate blocking the requested target."
            ),
            trigger_result=result,
            gate_kind=gate_purpose,
        )
        enriched = dict(result)
        enriched["overlayAdjudication"] = adjudication
        enriched["autoHitl"] = auto_hitl
        enriched["autoIntercept"] = {
            "trigger": trigger,
            "mode": mode,
            "action": "hitl",
            "blockedTargetId": blocked_target or None,
            "vlReused": bool(adjudication.get("reused")),
        }
        enriched["next_instruction"] = (
            "Visual adjudication found an authentication/verification gate"
            " blocking the target and the harness requested HITL. Inspect"
            " autoHitl.hitl_wait; do not retry the occluded action while the"
            " page is paused."
        )
        _record_microloop_telemetry(
            agent, "auto_intercept", "hitl",
            {"pageId": page_id, "trigger": trigger, "purpose": gate_purpose},
        )
        return enriched
    if recovery_action == "observe":
        enriched = dict(result)
        enriched["overlayAdjudication"] = adjudication
        enriched["autoIntercept"] = {
            "trigger": trigger,
            "mode": mode,
            "action": "deferred",
            "blockedTargetId": blocked_target or None,
            "vlReused": bool(adjudication.get("reused")),
        }
        existing = str(enriched.get("next_instruction") or "").strip()
        instruction = (
            "Visual adjudication could not establish a safe dismissal or a"
            " human-required authentication/verification gate. Do not try"
            " another equivalent target behind the cover; inspect the specific"
            " uncertainty in overlayAdjudication before choosing the next step."
        )
        enriched["next_instruction"] = (
            f"{existing} {instruction}".strip() if existing else instruction
        )
        _record_microloop_telemetry(
            agent, "auto_intercept", "deferred",
            {"pageId": page_id, "trigger": trigger},
        )
        return enriched
    # Only Input.click is auto-retry-safe; dismiss_overlay re-checks the target's
    # sensitivity before any retry and returns dismissed_pending_action otherwise.
    # The occlusion codes that arm this path (`occluded` / `target-occluded`) are
    # raised by target RESOLUTION — the browser found no usable hit-test point
    # and never dispatched a pointer event — and the platform's own prompt for
    # them says to dismiss the covering control and re-observe before retrying.
    # So the generic "a public failure has an unknown outcome" rule
    # (`replay_forbidden`) deliberately does NOT gate this one retry; applying it
    # here would disarm the recovery on every code that can arm it.
    # Pass the real method. `dismiss_overlay` reads an EMPTY targetMethod as
    # "Input.click" (its own default), so sending "" for a blocked Input.type
    # or Input.press asked it to auto-click the target instead of declining the
    # replay — the opposite of what the comment above promises. With the true
    # method, `is_sensitive_method` refuses every non-click and the tool
    # returns dismissed_pending_action, which is the intended contract.
    dismiss = await _bt()._dismiss_overlay(
        agent,
        {"pageId": page_id, "targetId": blocked_target, "targetMethod": method},
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
