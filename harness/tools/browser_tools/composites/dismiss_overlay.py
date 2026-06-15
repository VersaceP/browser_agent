"""dismiss_overlay composite tool."""

import asyncio
from typing import Any, List, Optional, Tuple

from harness.observation.overlay_actions import (
    backdrop_point_is_safe,
    compute_backdrop_point,
    find_close_control,
    is_sensitive_method,
    is_sensitive_target,
    normalized_point_to_css,
    visible_layers_occluded,
    vl_dismiss_target_is_safe,
)
from harness.observation.overlay_detector import detect_overlay_from_result
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


def _viewport_from_layers(layers: List[JsonDict]) -> JsonDict:
    return _bt()._viewport_from_layers(layers)


def _screenshot_saved_path(result: JsonDict) -> str:
    return _bt()._screenshot_saved_path(result)


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
) -> None:
    return _bt()._log_dismiss_overlay(agent, page_id, status, overlay, attempts)


def build_read_only_oracle(agent: Any, page_id: str, step: int) -> Any:
    return _bt().build_read_only_oracle(agent, page_id, step)


async def probe_occluder(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt().probe_occluder(*args, **kwargs)


async def probe_viewport_metrics(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt().probe_viewport_metrics(*args, **kwargs)


async def verify_overlay_gone(*args: Any, **kwargs: Any) -> Any:
    return await _bt().verify_overlay_gone(*args, **kwargs)


async def visual_verify_image(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt().visual_verify_image(*args, **kwargs)


DISMISS_OVERLAY_MAX_ATTEMPTS = 3
DISMISS_OVERLAY_MAX_DURATION_MS = 15000


async def _dismiss_overlay(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    """Composite tool: run the deterministic overlay dismiss ladder internally.

    All browser calls go through _invoke_browser_method(count_progress=False),
    so the ladder costs no model step. Returns a digest; full attempt logs go
    to agent.logger. Auth/paywall overlays return blocked with zero clicks."""
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

    oracle = build_read_only_oracle(agent, page_id, step)
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
    viewport = _viewport_from_layers(layers)
    occluded_frames = visible_layers_occluded(layers)

    if isinstance(overlay, dict) and overlay.get("subtype") in {"auth_prompt", "paywall"}:
        _log_dismiss_overlay(agent, page_id, "blocked", overlay, attempts)
        return {
            "status": "blocked",
            "subtype": overlay.get("subtype"),
            "overlay": overlay,
            "attempts": attempts,
            "next_instruction": (
                "An auth/login or paywall overlay was detected and is never"
                " auto-dismissed. Check for an existing session, request HITL,"
                " or report a blocker via final_answer."
            ),
        }

    def _interrupt_return(interrupt: JsonDict) -> JsonDict:
        # A HITL/challenge interrupt fired on a dismiss action: stop the ladder and
        # surface the human-needed summary instead of verifying / retrying.
        _log_dismiss_overlay(agent, page_id, str(interrupt.get("status")), overlay, attempts)
        return {**interrupt, "attempts": attempts[-3:]}

    success = False
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
            last_verdict = await verify_overlay_gone(oracle=oracle)
            if last_verdict.ok:
                success = True
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
        last_verdict = await verify_overlay_gone(oracle=oracle)
        if last_verdict.ok:
            success = True
            break

        backdrop_ok, backdrop_meta = await _try_backdrop_click(
            agent, page_id, oracle, last_verdict, viewport, step
        )
        if backdrop_meta.get("interrupt"):
            return _interrupt_return(backdrop_meta["interrupt"])
        attempts.append({"attempt": attempt, "rung": "backdrop", **backdrop_meta})
        if backdrop_ok:
            success = True
            break

        # Clicks/Escape invalidated the snapshot; refresh nodes for the next
        # attempt's close-control search.
        reinspect = await _invoke_browser_method(
            agent,
            "DOM.getAXTree",
            {"pageId": page_id, "purpose": "dismiss_overlay: re-inspect"},
            step,
            count_progress=False,
        )
        interrupt = _loop_interrupt_from_result(reinspect)
        if interrupt:
            return _interrupt_return(interrupt)

    vl_arbiter_meta: Optional[JsonDict] = None
    if not success:
        # Last resort: the VL arbiter. Only reached after the deterministic ladder
        # (close control -> Escape -> verified backdrop) fully failed. It locates a
        # safe dismiss control visually, back-translates to CSS coords, runs an
        # INDEPENDENT elementFromPoint safety check, then clicks. Gated by vl.enabled
        # + the per-worker VL budget; never clicks a consequential control.
        overlay_subtype = overlay.get("subtype") if isinstance(overlay, dict) else None
        vl_ok, vl_arbiter_meta = await _vl_overlay_arbiter(
            agent, page_id, oracle, step, subtype=overlay_subtype
        )
        if vl_arbiter_meta.get("interrupt"):
            return _interrupt_return(vl_arbiter_meta["interrupt"])
        attempts.append({"attempt": "vl_arbiter", **vl_arbiter_meta})
        if vl_ok:
            success = True

    if not success:
        _log_dismiss_overlay(agent, page_id, "failed", overlay, attempts)
        return {
            "status": "failed",
            "overlay": overlay,
            "occludedFrameCount": len(occluded_frames),
            "attempts": attempts[-3:],
            "vlArbiter": vl_arbiter_meta,
            "next_instruction": (
                "The dismiss ladder (including the VL arbiter) did not clear the"
                " overlay within the attempt budget. Inspect with DOM.getAXTree,"
                " try a visual_verify overlay_check, request HITL, or report a"
                " blocker."
            ),
        }

    retry = await _maybe_retry_original_action(
        agent, page_id, target_id, target_method, step
    )
    if retry.get("interrupt"):
        # The retried original action hit a HITL/challenge — do NOT report
        # dismissed_and_retried; surface human-needed.
        return _interrupt_return(retry["interrupt"])
    _log_dismiss_overlay(agent, page_id, retry["status"], overlay, attempts)
    return {
        "status": retry["status"],
        "overlay": overlay,
        "attempts": attempts[-3:],
        **{k: v for k, v in retry.items() if k != "status"},
    }


async def _try_backdrop_click(
    agent: Any,
    page_id: str,
    oracle: Any,
    verdict: Any,
    viewport: JsonDict,
    step: int,
) -> Tuple[bool, JsonDict]:
    dialog_rect = None
    dialogs = getattr(verdict, "evidence", {}).get("dialogs") if verdict else None
    if isinstance(dialogs, list) and dialogs and isinstance(dialogs[0], dict):
        dialog_rect = dialogs[0].get("rect")
    point = compute_backdrop_point(dialog_rect, viewport)
    if point is None:
        return False, {"skipped": "no_safe_backdrop"}
    x, y = point
    probe = await probe_occluder(oracle=oracle, x=x, y=y)
    if probe.get("status") != "done":
        return False, {"skipped": "occluder_unavailable"}
    if not backdrop_point_is_safe(list(probe.get("stack") or [])):
        return False, {"skipped": "unsafe_point", "point": [x, y]}
    click_result = await _invoke_browser_method(
        agent,
        "Input.click",
        {"pageId": page_id, "x": x, "y": y, "purpose": "dismiss_overlay: backdrop click"},
        step,
        count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(click_result)
    if interrupt:
        return False, {"point": [x, y], "interrupt": interrupt}
    after = await verify_overlay_gone(oracle=oracle)
    return bool(after.ok), {"point": [x, y]}


async def _vl_overlay_arbiter(
    agent: Any,
    page_id: str,
    oracle: Any,
    step: int,
    subtype: Optional[str] = None,
) -> Tuple[bool, JsonDict]:
    """Phase 7.1 visual arbiter — the dismiss ladder's last rung.

    Pipeline: viewport screenshot -> VL overlay_classify -> back-translate the
    proposed dismiss point from NORMALIZED 0-1000 coords to CSS coords ->
    INDEPENDENT elementFromPoint positive-whitelist safety check -> coordinate
    click -> verify overlay gone. The VL is an arbiter, never on the main path:
    it runs only after the deterministic ladder failed, is gated by vl.enabled +
    the per-worker VL budget, refuses any control the VL itself flags
    consequential, and clicks ONLY a positively-identified dismiss control
    (consent controls gated on the overlay subtype, same as find_close_control)."""
    vl_config = getattr(agent.runtime.harness, "vl", None)
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return False, {"rung": "vl_arbiter", "skipped": "vl_disabled"}
    raw_max = optional_int(getattr(vl_config, "max_checks_per_worker", 2), 2)
    max_checks = max(0, raw_max if raw_max is not None else 2)
    if getattr(agent, "vl_check_count", 0) >= max_checks:
        return False, {"rung": "vl_arbiter", "skipped": "vl_budget", "maxChecks": max_checks}

    before_artifacts = {str(p) for p in getattr(agent, "artifacts", [])}
    screenshot = await _invoke_browser_method(
        agent,
        "Page.screenshot",
        {
            "pageId": page_id,
            "fullPage": False,
            "options": {"format": "base64"},
            "purpose": "dismiss_overlay: VL arbiter screenshot",
        },
        step,
        count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(screenshot)
    if interrupt:
        return False, {"rung": "vl_arbiter", "interrupt": interrupt}
    image_path = _screenshot_saved_path(screenshot)
    if not image_path:
        after_new = [
            str(p) for p in getattr(agent, "artifacts", [])
            if str(p) not in before_artifacts
        ]
        image_path = after_new[-1] if after_new else ""
    if not image_path:
        return False, {"rung": "vl_arbiter", "skipped": "no_screenshot"}

    metrics = await probe_viewport_metrics(oracle=oracle)
    if metrics.get("status") != "done":
        return False, {"rung": "vl_arbiter", "skipped": "viewport_unavailable"}
    css_viewport = {"width": metrics.get("width"), "height": metrics.get("height")}

    agent.vl_check_count = getattr(agent, "vl_check_count", 0) + 1
    verdict = await visual_verify_image(
        config=vl_config,
        image_path=image_path,
        expected={},
        mode="overlay_classify",
        question=(
            "An automated browser action was blocked by an overlay. Find the safe"
            " control that dismisses it."
        ),
    )
    agent.logger.write(
        "vl.overlay_classify",
        {key: value for key, value in verdict.items() if key not in {"usage", "visible_evidence"}},
    )
    if verdict.get("status") != "done":
        return False, {"rung": "vl_arbiter", "skipped": "vl_failed", "error": verdict.get("error")}
    if verdict.get("verdict") != "dismiss_found":
        return False, {"rung": "vl_arbiter", "skipped": "no_dismiss", "verdict": verdict.get("verdict")}
    if verdict.get("is_consequential"):
        return False, {
            "rung": "vl_arbiter",
            "skipped": "consequential_control",
            "label": str(verdict.get("control_label") or "")[:60],
        }

    css_point = normalized_point_to_css(verdict.get("dismiss_point"), css_viewport)
    if css_point is None:
        return False, {"rung": "vl_arbiter", "skipped": "point_untranslatable"}
    cx, cy = css_point

    probe = await probe_occluder(oracle=oracle, x=cx, y=cy)
    if probe.get("status") != "done":
        return False, {"rung": "vl_arbiter", "skipped": "occluder_unavailable"}
    stack = list(probe.get("stack") or [])
    top = stack[0] if stack and isinstance(stack[0], dict) else {}
    if not vl_dismiss_target_is_safe(
        top, str(verdict.get("control_label") or ""), subtype=subtype
    ):
        return False, {
            "rung": "vl_arbiter",
            "skipped": "unsafe_point",
            "point": [cx, cy],
            "top": str(top.get("text") or "")[:60] if isinstance(top, dict) else "",
        }

    click_result = await _invoke_browser_method(
        agent,
        "Input.click",
        {"pageId": page_id, "x": cx, "y": cy, "purpose": "dismiss_overlay: VL arbiter click"},
        step,
        count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(click_result)
    if interrupt:
        return False, {"rung": "vl_arbiter", "point": [cx, cy], "interrupt": interrupt}
    after = await verify_overlay_gone(oracle=oracle)
    return bool(after.ok), {
        "rung": "vl_arbiter",
        "point": [cx, cy],
        "label": str(verdict.get("control_label") or "")[:60],
        "confidence": verdict.get("confidence"),
        "verified": bool(after.ok),
    }


async def _maybe_retry_original_action(
    agent: Any,
    page_id: str,
    target_id: str,
    target_method: str,
    step: int,
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
    return {
        "status": "dismissed" if failed else "dismissed_and_retried",
        "retried": not failed,
        "retryTarget": {"id": target_id, "method": target_method},
    }
