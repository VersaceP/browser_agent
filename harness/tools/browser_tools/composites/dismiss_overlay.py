"""dismiss_overlay composite tool."""

import asyncio
from typing import Any, List, Optional, Tuple

from harness.results.call_outcome import replay_forbidden
from harness.observation.overlay_actions import (
    backdrop_click_is_safe,
    compute_backdrop_point,
    find_close_control,
    is_sensitive_method,
    is_sensitive_target,
    visible_layers_occluded,
)
from harness.observation.overlay_detector import detect_overlay_from_result
from harness.observation.verifiers import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    VerifierResult,
    build_overlay_probe_oracle,
    probe_occluder_trusted,
    probe_viewport_metrics_trusted,
)
from harness.utils import JsonDict, optional_int


def _bt() -> Any:
    import harness.tools.browser_tools as bt

    return bt


async def _invoke_browser_method(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt()._invoke_browser_method(*args, **kwargs)


def _loop_interrupt_from_result(result: Any) -> Optional[JsonDict]:
    return _bt()._loop_interrupt_from_result(result)


def _layers_from_result(result: JsonDict) -> List[JsonDict]:
    return _bt()._layers_from_result(result)


def _axtree_seen_signature(agent: Any, node_id: str, page_id: str) -> Optional[JsonDict]:
    return _bt()._axtree_seen_signature(agent, node_id, page_id)


def _invoke_result_failed(result: Any) -> bool:
    return _bt()._invoke_result_failed(result)


def _log_dismiss_overlay(
    agent: Any,
    page_id: str,
    status: str,
    overlay: Optional[JsonDict],
    attempts: List[JsonDict],
    retry: Optional[JsonDict] = None,
) -> None:
    return _bt()._log_dismiss_overlay(
        agent, page_id, status, overlay, attempts, retry=retry,
    )


async def _verify_overlay_gone_native(
    agent: Any,
    page_id: str,
    step: int,
    *,
    blocked_target: str = "",
    blocked_method: str = "",
) -> VerifierResult:
    """Did the overlay actually go away?

    The AXTree detector answers this only for overlays it can recognise, and it
    recognises them by auth/paywall/cookie keywords or a dialog role. A
    promotional mask built from anonymous `genericcontainer` nodes matches
    neither, so the detector returns None BOTH before and after every rung --
    and reading that None as "no overlay" made the ladder declare success on
    its first rung. Tasks b9a91fd2 and c281862c logged six `dismissed` verdicts
    that way, and every one of the six retried actions came straight back with
    the original `-32005 target is covered`.

    So when the ladder was armed by a blocked action, the detector's silence
    proves nothing and the blocked target itself is the measurement: replaying
    it either succeeds (the obstruction is gone, and the action the caller
    wanted is now done) or reports occlusion again (it is still there). The
    replay is bounded by the same sensitivity gate as the final retry, and a
    caller with no blocked target keeps the detector-only behaviour.
    """
    inspect = await _invoke_browser_method(
        agent,
        "DOM.getAXTree",
        {"pageId": page_id, "purpose": "dismiss_overlay: verify via native AXTree"},
        step,
        count_progress=False,
    )
    if _invoke_result_failed(inspect):
        return VerifierResult(
            ok=False,
            confidence=CONFIDENCE_LOW,
            method="native_axtree_unavailable",
            reason="DOM.getAXTree failed while verifying overlay state",
        )
    overlay = detect_overlay_from_result(inspect)
    if isinstance(overlay, dict):
        # A recognised overlay is still on the page: decisive, no probe needed.
        return VerifierResult(
            ok=False,
            confidence=CONFIDENCE_MEDIUM,
            method="native_axtree",
            evidence={"overlay": overlay},
            reason="overlay still present",
        )
    if not blocked_target or _target_replay_is_unsafe(agent, page_id, blocked_target, blocked_method):
        return VerifierResult(
            ok=True,
            confidence=CONFIDENCE_MEDIUM,
            method="native_axtree",
            evidence={"overlay": None},
            reason="no overlay in refreshed AXTree",
        )
    probe = await _invoke_browser_method(
        agent,
        blocked_method or "Input.click",
        {
            "pageId": page_id,
            "id": blocked_target,
            "purpose": "dismiss_overlay: verify by replaying the blocked action",
        },
        step,
        count_progress=False,
        allow_rematch=True,
    )
    if _loop_interrupt_from_result(probe):
        return VerifierResult(
            ok=False,
            confidence=CONFIDENCE_LOW,
            method="blocked_target_replay",
            reason="replaying the blocked action raised a challenge/HITL interrupt",
        )
    if not _invoke_result_failed(probe):
        return VerifierResult(
            ok=True,
            confidence=CONFIDENCE_HIGH,
            method="blocked_target_replay",
            evidence={"overlay": None, "replayed": True},
            reason="the blocked action went through",
        )
    if _bt()._result_occlusion_blocked(probe):
        return VerifierResult(
            ok=False,
            confidence=CONFIDENCE_HIGH,
            method="blocked_target_replay",
            evidence={"overlay": None, "stillOccluded": True},
            reason="the blocked action is still occluded",
        )
    # Failed for some other reason: that says nothing about the obstruction, so
    # fall back to what the detector saw rather than inventing a verdict.
    return VerifierResult(
        ok=True,
        confidence=CONFIDENCE_LOW,
        method="native_axtree",
        evidence={"overlay": None, "replayFailedUnrelated": True},
        reason="no overlay in refreshed AXTree; replay failed for an unrelated reason",
    )


def _target_replay_is_unsafe(
    agent: Any, page_id: str, target_id: str, target_method: str,
) -> bool:
    """The same gate the final retry applies, asked before the probe.

    Verification must never be the thing that submits a form or presses a
    login button, so a sensitive method or a consequential-looking target
    keeps the detector-only path.
    """
    _ = page_id
    signature = _axtree_seen_signature(agent, target_id, page_id) or {}
    return bool(
        is_sensitive_method(target_method or "Input.click")
        or is_sensitive_target(
            str(signature.get("role") or ""), str(signature.get("name") or "")
        )
    )


def _verdict_replayed_target(verdict: Any) -> bool:
    """Did this verdict reach `ok` by actually performing the blocked action?

    `_verify_overlay_gone_native` has two ways to say the page is clear. One
    reads a refreshed AXTree and touches nothing. The other REPLAYS the blocked
    action, and when that goes through it has both proved the obstruction is
    gone and executed the very action the caller was going to retry. Only the
    second one makes a follow-up retry a duplicate, and only the second one is
    reported here.
    """
    return bool(
        getattr(verdict, "ok", False)
        and getattr(verdict, "method", "") == "blocked_target_replay"
        and (getattr(verdict, "evidence", None) or {}).get("replayed")
    )


DISMISS_OVERLAY_MAX_ATTEMPTS = 3
DISMISS_OVERLAY_MAX_DURATION_MS = 15000


async def _dismiss_overlay(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    """Composite tool: run the deterministic overlay dismiss ladder internally.

    All browser calls go through _invoke_browser_method(count_progress=False),
    so the ladder costs no model step. Returns a digest; full attempt logs go
    to agent.logger.

    Auth/paywall overlays run the SAME safe ladder as any other overlay. The
    policy that matters is which controls may be pressed — login/provider/
    payment buttons never are, and `find_close_control` already excludes them —
    not whether we are allowed to press Escape. Refusing the ladder outright
    produced `attempts: []` receipts that downstream code read as proof the
    content itself was gated, which is a conclusion zero attempts cannot
    support. When the safe rungs do not clear it, the result says
    `policy_refused` with `targetAccess: unverified`."""
    page_id = str(tool_input.get("pageId") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    target_id = str(tool_input.get("targetId") or "").strip()
    target_method = str(tool_input.get("targetMethod") or "").strip() or "Input.click"
    max_attempts = optional_int(tool_input.get("maxAttempts"), 0) or 0
    if max_attempts <= 0:
        max_attempts = DISMISS_OVERLAY_MAX_ATTEMPTS
    max_attempts = max(1, min(max_attempts, 5))
    max_duration_ms = optional_int(tool_input.get("maxDurationMs"), 0) or 0
    if max_duration_ms <= 0:
        max_duration_ms = DISMISS_OVERLAY_MAX_DURATION_MS
    deadline = asyncio.get_running_loop().time() + max_duration_ms / 1000.0

    attempts: List[JsonDict] = []

    inspect = await _invoke_browser_method(
        agent,
        "DOM.getAXTree",
        {"pageId": page_id, "purpose": "dismiss_overlay: inspect overlay"},
        step,
        count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(inspect)
    if interrupt:
        # The page is challenge-paused / HITL-pending — the dismiss ladder cannot
        # run. Surface the human-needed summary (loopInterrupted) so callers
        # (collect_items recovery, the model) stop instead of clicking a paused page.
        _log_dismiss_overlay(agent, page_id, str(interrupt.get("status")), None, attempts)
        return {**interrupt, "attempts": attempts}
    overlay = detect_overlay_from_result(inspect)
    layers = _layers_from_result(inspect)
    occluded_frames = visible_layers_occluded(layers)

    policy_subtype = (
        str(overlay.get("subtype"))
        if isinstance(overlay, dict)
        and overlay.get("subtype") in {"auth_prompt", "paywall"}
        else ""
    )

    def _interrupt_return(interrupt: JsonDict) -> JsonDict:
        # A HITL/challenge interrupt fired on a dismiss action: stop the ladder and
        # surface the human-needed summary instead of verifying / retrying.
        _log_dismiss_overlay(agent, page_id, str(interrupt.get("status")), overlay, attempts)
        return {**interrupt, "attempts": attempts[-3:]}

    success = False
    already_performed = False
    last_verdict: Optional[Any] = None
    for attempt in range(1, max_attempts + 1):
        if asyncio.get_running_loop().time() >= deadline:
            break

        close = find_close_control(
            list(getattr(agent, "axtree_nodes", []) or []),
            subtype=overlay.get("subtype") if isinstance(overlay, dict) else None,
        )
        if close is not None:
            close_result = await _invoke_browser_method(
                agent,
                "Input.click",
                {
                    "pageId": page_id,
                    "id": close.get("id"),
                    "purpose": "dismiss_overlay: click close control",
                },
                step,
                count_progress=False,
                allow_rematch=True,
            )
            interrupt = _loop_interrupt_from_result(close_result)
            if interrupt:
                return _interrupt_return(interrupt)
            attempts.append({
                "attempt": attempt,
                "rung": "close_control",
                "id": close.get("id"),
                "name": str(close.get("name") or "")[:60],
            })
            last_verdict = await _verify_overlay_gone_native(
                agent, page_id, step,
                blocked_target=target_id, blocked_method=target_method,
            )
            if last_verdict.ok:
                success = True
                already_performed = _verdict_replayed_target(last_verdict)
                break
            if replay_forbidden(close_result):
                # The click failed AFTER input dispatch began: it may already
                # have activated the control. Another lap of the ladder would
                # click the same target again, so stop and let the caller look
                # at the page.
                attempts[-1]["replayForbidden"] = True
                break

        escape_result = await _invoke_browser_method(
            agent,
            "Input.press",
            {"pageId": page_id, "key": "Escape", "purpose": "dismiss_overlay: escape"},
            step,
            count_progress=False,
        )
        interrupt = _loop_interrupt_from_result(escape_result)
        if interrupt:
            return _interrupt_return(interrupt)
        attempts.append({"attempt": attempt, "rung": "escape"})
        last_verdict = await _verify_overlay_gone_native(
            agent, page_id, step,
            blocked_target=target_id, blocked_method=target_method,
        )
        if last_verdict.ok:
            success = True
            already_performed = _verdict_replayed_target(last_verdict)
            break

        # The Escape verifier refreshed both the native AXTree and the cached
        # canonical nodes. No extra re-inspection is needed before the next
        # attempt. Coordinate backdrop clicks are deliberately unavailable:
        # ABCP has no independent native hit-test with which to prove safety.

    backdrop_meta: Optional[JsonDict] = None
    if not success and not policy_subtype:
        # Not for auth_prompt / paywall: those refuse by policy before any
        # click, and a backdrop click on a login wall is still an interaction
        # with a login wall.
        if asyncio.get_running_loop().time() < deadline:
            backdrop_ok, backdrop_meta = await _backdrop_dismiss(
                agent, page_id, step, target_id, target_method,
            )
            attempts.append({"attempt": "backdrop", **backdrop_meta})
            if backdrop_ok:
                success = True
                already_performed = bool(
                    backdrop_meta.get("replayedBlockedTarget")
                )
        else:
            backdrop_meta = {"rung": "backdrop", "skipped": "deadline_exceeded"}
            attempts.append({"attempt": "backdrop", **backdrop_meta})

    vl_arbiter_meta: Optional[JsonDict] = None
    if not success and not policy_subtype:
        # Gated exactly like the backdrop rung: an auth_prompt / paywall overlay
        # gets the rungs that press controls the PAGE declares (close control,
        # Escape) and nothing else. This gate was absent while the rung was a
        # permanent stub -- a rung that always refused cost nothing on a login
        # wall -- and became load-bearing the moment the rung started clicking
        # for real.
        vl_ok, vl_arbiter_meta = await _vl_overlay_arbiter(
            agent, page_id, oracle=None, step=step,
            blocked_target=target_id, blocked_method=target_method,
        )
        attempts.append({"attempt": "vl_arbiter", **vl_arbiter_meta})
        if vl_ok:
            success = True
            already_performed = bool(
                vl_arbiter_meta.get("replayedBlockedTarget")
            )
    elif not success:
        # Recorded rather than silent: "refused by policy" and "ran and did not
        # work" are different facts and the receipt must keep them apart.
        vl_arbiter_meta = {"rung": "vl_arbiter", "skipped": "policy_subtype"}
        attempts.append({"attempt": "vl_arbiter", **vl_arbiter_meta})

    if not success:
        safe_rungs = sum(
            1 for item in attempts
            if str(item.get("rung") or "") in {"close_control", "escape"}
        )
        if policy_subtype:
            _log_dismiss_overlay(agent, page_id, "policy_refused", overlay, attempts)
            return {
                "status": "policy_refused",
                "subtype": policy_subtype,
                "overlay": overlay,
                "overlayPresent": True,
                "dismissAttempted": bool(safe_rungs),
                "dismissOutcome": (
                    "blocked_after_ladder" if safe_rungs else "not_attempted"
                ),
                "targetAccess": "unverified",
                "reason": "auth_or_paywall_auto_dismiss_disallowed",
                "occludedFrameCount": len(occluded_frames),
                "attempts": attempts[-3:],
                "vlArbiter": vl_arbiter_meta,
                "backdrop": backdrop_meta,
                "next_instruction": (
                    "Safe non-submit rungs (close control, Escape) ran and did"
                    " not clear this auth/paywall overlay; login, provider and"
                    " payment controls are never auto-clicked. This establishes"
                    " only that THIS interaction on THIS page epoch is"
                    " obstructed. It is NOT evidence that the target content"
                    " requires a login, and NOT evidence that the content is"
                    " absent — re-observe the target region after a fresh"
                    " navigation before reporting any blocker, and never carry"
                    " this finding over to another item."
                ),
            }
        _log_dismiss_overlay(agent, page_id, "failed", overlay, attempts)
        return {
            "status": "failed",
            "overlay": overlay,
            "overlayPresent": True,
            "dismissAttempted": bool(safe_rungs),
            "dismissOutcome": (
                "blocked_after_ladder" if safe_rungs else "not_attempted"
            ),
            "targetAccess": "unverified",
            "occludedFrameCount": len(occluded_frames),
            "attempts": attempts[-3:],
            "vlArbiter": vl_arbiter_meta,
                "backdrop": backdrop_meta,
            "next_instruction": (
                "Native close-control and Escape attempts did not clear the"
                " overlay. Coordinate backdrop and VL clicks were not attempted"
                " because no independent native point hit-test is available."
                " Refresh DOM.getAXTree, request HITL when human action is"
                " genuinely required, or report a blocker."
            ),
        }

    retry = await _maybe_retry_original_action(
        agent, page_id, target_id, target_method, step,
        already_performed=already_performed,
    )
    if retry.get("interrupt"):
        # The retried original action hit a HITL/challenge — do NOT report
        # dismissed_and_retried; surface human-needed.
        return _interrupt_return(retry["interrupt"])
    _log_dismiss_overlay(agent, page_id, retry["status"], overlay, attempts, retry=retry)
    return {
        "status": retry["status"],
        "overlay": overlay,
        "attempts": attempts[-3:],
        **{k: v for k, v in retry.items() if k != "status"},
    }


def _dialog_rect_from_stack(stack: Any, viewport: JsonDict) -> Optional[JsonDict]:
    """The dialog box sitting on the mask, read off the centre hit-test stack.

    Walking down from the top, the first element materially smaller than the
    viewport is the dialog; everything above it is mask/wrapper. Returning None
    is meaningful rather than a failure - `compute_backdrop_point` documents its
    own no-known-rect behaviour, and the safety gate still has to pass.
    """
    vw = float(viewport.get("width") or 0)
    vh = float(viewport.get("height") or 0)
    if not isinstance(stack, list) or vw <= 0 or vh <= 0:
        return None
    for element in stack:
        rect = element.get("rect") if isinstance(element, dict) else None
        if not isinstance(rect, dict):
            continue
        try:
            w = float(rect.get("w") or 0)
            h = float(rect.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        if w < vw * 0.9 or h < vh * 0.9:
            return {"x": rect.get("x") or 0, "y": rect.get("y") or 0, "w": w, "h": h}
    return None


async def _backdrop_dismiss(
    agent: Any,
    page_id: str,
    step: int,
    blocked_target: str = "",
    blocked_method: str = "",
) -> Tuple[bool, JsonDict]:
    """Rung 3: click the modal's own backdrop, proven by an element hit test.

    The rung that was missing. Rung 1 needs a close control the AX tree can
    name and rung 2 needs the page to honour Escape; a promotional mask whose
    close X is not in the AX tree satisfies neither, and the ladder then had
    nothing left. This clicks the overlay itself, which is the one target whose
    safety can be established without identifying anything: the hit test says
    what is actually under the point, and `backdrop_click_is_safe` requires it
    to be the inert full-viewport overlay and nothing else.

    Every failure path returns a reason rather than a click.
    """
    oracle = build_overlay_probe_oracle(agent, page_id, step)
    viewport_probe = await probe_viewport_metrics_trusted(overlay_oracle=oracle)
    if viewport_probe.get("status") != "done":
        return False, {"rung": "backdrop",
                       "skipped": str(viewport_probe.get("reason") or "viewport_unavailable")}
    viewport = {"width": viewport_probe["width"], "height": viewport_probe["height"]}

    centre = await probe_occluder_trusted(
        overlay_oracle=oracle,
        x=viewport["width"] / 2.0,
        y=viewport["height"] / 2.0,
    )
    if centre.get("status") != "done":
        return False, {"rung": "backdrop",
                       "skipped": str(centre.get("reason") or "hit_test_unavailable")}

    point = compute_backdrop_point(
        _dialog_rect_from_stack(centre.get("stack"), viewport), viewport
    )
    if point is None:
        return False, {"rung": "backdrop", "skipped": "dialog_fills_viewport"}
    bx, by = point

    at_point = await probe_occluder_trusted(overlay_oracle=oracle, x=bx, y=by)
    if at_point.get("status") != "done":
        return False, {"rung": "backdrop", "point": {"x": bx, "y": by},
                       "skipped": str(at_point.get("reason") or "hit_test_unavailable")}

    safe, gate = backdrop_click_is_safe(at_point.get("stack"), viewport)
    if not safe:
        return False, {"rung": "backdrop", "point": {"x": bx, "y": by}, **gate}

    click = await _invoke_browser_method(
        agent,
        "Input.click",
        {"pageId": page_id, "x": bx, "y": by,
         "purpose": "dismiss_overlay: click the proven modal backdrop"},
        step,
        count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(click)
    if interrupt:
        return False, {"rung": "backdrop", "point": {"x": bx, "y": by},
                       "interrupted": True}
    if _invoke_result_failed(click):
        return False, {"rung": "backdrop", "point": {"x": bx, "y": by},
                       "skipped": "click_failed"}
    verdict = await _verify_overlay_gone_native(
        agent, page_id, step,
        blocked_target=blocked_target, blocked_method=blocked_method,
    )
    meta: JsonDict = {
        "rung": "backdrop",
        "point": {"x": bx, "y": by},
        "element": gate,
        "verified": bool(verdict.ok),
    }
    if _verdict_replayed_target(verdict):
        meta["replayedBlockedTarget"] = True
    return bool(verdict.ok), meta


def _ladder_screenshot_fn(agent: Any, step: int) -> Any:
    """A `locate_target`-shaped capture that costs the worker no step.

    `locate_target` calls `screenshot_fn(browser, page_id)` and accepts either
    a path or `{path, receipt}`. The receipt form is the one that matters: it
    is what lets the scale be PROVEN rather than assumed, and an unproven scale
    makes the coordinate fallback refuse instead of clicking at the wrong
    point. Viewport scope only -- a document-height capture has no upper bound
    and is unreadable once scaled to model input.
    """
    async def capture(_browser: Any, page_id: str) -> Any:
        result = await _invoke_browser_method(
            agent,
            "Page.screenshot",
            {
                "pageId": page_id,
                "fullPage": False,
                "options": {"format": "file"},
                "purpose": "dismiss_overlay: capture for VL close-control locate",
            },
            step,
            count_progress=False,
        )
        if _invoke_result_failed(result):
            return None
        path = _bt()._screenshot_saved_path(result)
        if not path:
            return None
        data = _bt()._response_data(result) or _bt()._raw_response_data(result) or {}
        return {"path": path, "receipt": data if isinstance(data, dict) else {}}

    return capture


async def _vl_overlay_arbiter(
    agent: Any,
    page_id: str,
    oracle: Any = None,
    step: int = 0,
    subtype: Optional[str] = None,
    blocked_target: str = "",
    blocked_method: str = "",
) -> Tuple[bool, JsonDict]:
    """Rung 4: see the close control, when no structural surface names it.

    The rungs above need something the page declares -- a close control the AX
    tree can name, a key the page honours, or a hit test that proves what sits
    under a point. A promotional mask can satisfy none of them: in task
    b9a91fd2 the Taobao modal was `genericcontainer` all the way down, its
    close control an unnamed 80x80 box with no role and no text, so rung 1
    found nothing to click and rung 2 pressed a key nothing listened for.

    This rung is deliberately narrow, because the 09-01 refactor removed an
    earlier automatic VL lane whose gate was prose-marker matching that hit 1
    of 11 real error codes -- code that looked alive and never ran. The gate
    here is `_result_occlusion_blocked`, a structured predicate over a numeric
    transport code, and the rung arms only after every deterministic rung has
    failed on an obstruction the harness measured rather than inferred. Its
    budget comes from AUTO_INTERCEPT_MAX_PER_PAGE, so a page cannot spend more
    than a few of these no matter how often it re-masks.

    Locating is not authorisation: `locate_target` reports `is_consequential`
    for submit/pay/login-like targets and this rung refuses to click those, the
    same boundary the worker prompt states. A pixel that cannot be promoted to
    a durable id is still usable as ONE viewport CSS click, because
    `locate_target` proves scale and origin fail-closed and refuses when it
    cannot; the point is used once and never persisted.
    """
    _ = (oracle, subtype)
    vl_config = getattr(
        getattr(getattr(agent, "runtime", None), "harness", None), "vl", None,
    )
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return False, {"rung": "vl_arbiter", "skipped": "vl_disabled"}
    if not getattr(vl_config, "visual_locate_enabled", True):
        return False, {"rung": "vl_arbiter", "skipped": "visual_locate_disabled"}

    from harness.vl.locate import locate_target

    target = (
        "the control that closes or dismisses the modal, popup or mask"
        " currently covering this page (its close X, dismiss or skip control)"
    )
    try:
        located = await locate_target(
            getattr(agent, "browser", None),
            page_id,
            target,
            vl_config=vl_config,
            screenshot_fn=_ladder_screenshot_fn(agent, step),
            logger=getattr(agent, "logger", None),
        )
    except Exception as exc:  # VL is a fallback, never a new hard dependency
        return False, {"rung": "vl_arbiter", "skipped": "locate_error",
                       "error": str(exc)[:200]}

    if not located.get("ok"):
        return False, {"rung": "vl_arbiter",
                       "skipped": str(located.get("reason") or "not_located")}
    if located.get("is_consequential"):
        # Seeing where it is does not license pressing it.
        return False, {"rung": "vl_arbiter", "skipped": "consequential_target",
                       "label": str(located.get("label") or "")[:60]}

    params: JsonDict = {"pageId": page_id, "purpose": "dismiss_overlay: VL-located close control"}
    located_id = str(located.get("id") or "").strip()
    css_point = located.get("cssPoint")
    click_method = "Input.click"
    if located_id:
        params["id"] = located_id
        used = {"id": located_id}
    elif isinstance(css_point, dict) and located.get("coordinate"):
        click_method = "Page.click"
        params["x"] = css_point.get("x")
        params["y"] = css_point.get("y")
        used = {"cssPoint": dict(css_point)}
    else:
        return False, {"rung": "vl_arbiter", "skipped": "no_actionable_locator"}

    click = await _invoke_browser_method(agent, click_method, params, step, count_progress=False)
    if _loop_interrupt_from_result(click):
        return False, {"rung": "vl_arbiter", "interrupted": True, **used}
    if _invoke_result_failed(click):
        return False, {"rung": "vl_arbiter", "skipped": "click_failed", **used}
    verdict = await _verify_overlay_gone_native(
        agent, page_id, step,
        blocked_target=blocked_target, blocked_method=blocked_method,
    )
    meta: JsonDict = {
        "rung": "vl_arbiter",
        "verified": bool(verdict.ok),
        "verifiedBy": verdict.method,
        **used,
    }
    if _verdict_replayed_target(verdict):
        meta["replayedBlockedTarget"] = True
    return bool(verdict.ok), meta


async def _maybe_retry_original_action(
    agent: Any,
    page_id: str,
    target_id: str,
    target_method: str,
    step: int,
    *,
    already_performed: bool = False,
) -> JsonDict:
    if not target_id:
        return {"status": "dismissed", "retried": False, "reason": "no original target supplied"}
    signature = _axtree_seen_signature(agent, target_id, page_id) or {}
    role = str(signature.get("role") or "")
    name = str(signature.get("name") or "")
    if is_sensitive_method(target_method) or is_sensitive_target(role, name):
        return {
            "status": "dismissed_pending_action",
            "retried": False,
            "target": {"id": target_id, "method": target_method, "role": role, "name": name},
            "next_instruction": (
                "Overlay dismissed. The original action is not auto-retry-safe"
                " (typing/keypress) or its target looks consequential"
                " (submit/pay/login/delete-like). Decide whether to repeat it."
            ),
        }
    if already_performed:
        # A rung reached its verdict by replaying THIS action and it went
        # through, so what the caller wanted is already done. Dispatching it
        # again would run a non-idempotent target twice: add the item twice,
        # page twice, toggle back to where it started. The sensitivity gate
        # above cannot have been passed by a replayed target -- the verifier
        # applies the same gate before probing -- so reaching here with the
        # flag set is self-checking rather than assumed.
        return {
            "status": "dismissed_and_retried",
            "retried": True,
            "retriedBy": "overlay_verifier_replay",
            "retryTarget": {"id": target_id, "method": target_method},
        }
    result = await _invoke_browser_method(
        agent,
        target_method,
        {"pageId": page_id, "id": target_id, "purpose": "dismiss_overlay: retry original action"},
        step,
        count_progress=False,
        allow_rematch=True,
    )
    interrupt = _loop_interrupt_from_result(result)
    if interrupt:
        return {"interrupt": interrupt, "retried": False}
    failed = _invoke_result_failed(result)
    if failed and _bt()._result_occlusion_blocked(result):
        # The retry hit the SAME occlusion the ladder was supposed to clear.
        #
        # Escape reporting success and the AXTree verification agreeing are
        # both indirect readings; this is the direct measurement, and it says
        # the target is still covered. Reporting `dismissed` here merges two
        # different worlds -- "the overlay is gone and the retry failed for
        # some other reason" and "the overlay is still there" -- and hands the
        # worker the wrong one. In task b9a91fd2 both occurrences did exactly
        # that: the ladder logged `dismissed`, the retry came back with the
        # original `-32005 target is covered`, and each worker then spent four
        # steps re-deriving a route around a page it had been told was clean.
        return {
            "status": "failed",
            "retried": False,
            "stillOccluded": True,
            "retryTarget": {"id": target_id, "method": target_method},
            "next_instruction": (
                "The overlay ladder reported a clear page, but retrying the"
                " original action hit the same occlusion, so the target is"
                " still covered. Do not treat the page as clean: re-observe"
                " with a fresh DOM.getAXTree, look for a layer the ladder did"
                " not reach (another frame, or a mask that swallows pointer"
                " events without owning a close control), and request HITL"
                " only when no control can be bound at all."
            ),
        }
    return {
        "status": "dismissed" if failed else "dismissed_and_retried",
        "retried": not failed,
        "retryTarget": {"id": target_id, "method": target_method},
    }
