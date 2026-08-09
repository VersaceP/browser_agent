"""dismiss_overlay composite tool."""

import asyncio
from typing import Any, List, Optional, Tuple

from harness.observation.overlay_actions import (
    find_close_control,
    is_sensitive_method,
    is_sensitive_target,
    visible_layers_occluded,
)
from harness.observation.overlay_detector import detect_overlay_from_result
from harness.observation.verifiers import (
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    VerifierResult,
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
) -> None:
    return _bt()._log_dismiss_overlay(agent, page_id, status, overlay, attempts)


async def _verify_overlay_gone_native(
    agent: Any,
    page_id: str,
    step: int,
) -> VerifierResult:
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
    return VerifierResult(
        ok=not isinstance(overlay, dict),
        confidence=CONFIDENCE_MEDIUM,
        method="native_axtree",
        evidence={"overlay": overlay} if isinstance(overlay, dict) else {"overlay": None},
        reason="overlay still present" if isinstance(overlay, dict) else "no overlay in refreshed AXTree",
    )


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
            last_verdict = await _verify_overlay_gone_native(agent, page_id, step)
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
        last_verdict = await _verify_overlay_gone_native(agent, page_id, step)
        if last_verdict.ok:
            success = True
            break

        # The Escape verifier refreshed both the native AXTree and the cached
        # canonical nodes. No extra re-inspection is needed before the next
        # attempt. Coordinate backdrop clicks are deliberately unavailable:
        # ABCP has no independent native hit-test with which to prove safety.

    vl_arbiter_meta: Optional[JsonDict] = None
    if not success:
        # Keep an explicit capability receipt for callers that previously
        # expected coordinate/VL fallback. Without a native point hit-test the
        # arbiter cannot safely turn a visual coordinate into an input action.
        vl_ok, vl_arbiter_meta = await _vl_overlay_arbiter(
            agent, page_id, oracle=None, step=step
        )
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
                "Native close-control and Escape attempts did not clear the"
                " overlay. Coordinate backdrop and VL clicks were not attempted"
                " because no independent native point hit-test is available."
                " Refresh DOM.getAXTree, request HITL when human action is"
                " genuinely required, or report a blocker."
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


async def _vl_overlay_arbiter(
    agent: Any,
    page_id: str,
    oracle: Any = None,
    step: int = 0,
    subtype: Optional[str] = None,
) -> Tuple[bool, JsonDict]:
    """Return an explicit receipt for the unavailable visual-coordinate rung."""
    _ = (agent, page_id, oracle, step, subtype)
    return False, {"rung": "vl_arbiter", "skipped": "native_hit_test_unavailable"}


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
