"""harness.vl.arbiter — VL Role D: global visual arbitration of browser_call failures.

VL is not only a workflow helper; it's the BrowserAgent's last-resort visual
arbiter. When a browser_call fails AND deterministic recovery is exhausted, IF the
failure is visual/occlusion/challenge/layout-related, route it to the right VL role
and return a concrete recovery recommendation (doc §13.5). This unifies the other
three roles behind one failure-driven entry:

    occlusion_blocked            → overlay_classify   → dismiss (point) | hitl
    hitl_paused_state / captcha  → challenge_detection → hitl | wait
    locator / target not found   → visual_locate      → retry_by_id (promote) | hitl
    render_lost / layout / visual → action_outcome     → reperceive | hitl | continue

Discipline (doc §7.1): VL is L4 — it only RECOMMENDS a recovery; every coordinate
output still passes elementFromPoint/safety downstream, and a located pixel is
promoted to a durable id (never raw coordinates). Non-visual failures (timeout,
contract_error, page_crashed) return action=none — VL is not consulted.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

# error-classification type → (VL mode, recovery intent)
_ROUTE_BY_TYPE: Dict[str, Tuple[str, str]] = {
    "occlusion_blocked": ("overlay_classify", "dismiss"),
    "hitl_paused_state": ("challenge_detection", "hitl"),
    "render_lost": ("action_outcome", "reperceive"),
}
_LOCATOR_MARKERS = ("not found", "no element", "no such element", "could not resolve",
                    "locator", "cannot find", "no matching", "target not")
_CHALLENGE_MARKERS = ("captcha", "challenge", "verify you are human", "cloudflare",
                      "are you a robot", "人机验证", "验证码")
# Non-visual failures: VL must NOT be consulted (DOM/state already answers these).
_NON_VISUAL_TYPES = frozenset({"timeout", "contract_error", "page_crashed"})


def route_failure(classification_type: str, error_text: str) -> Optional[Tuple[str, str]]:
    """Return (vl_mode, intent) for a visually-arbitrable failure, else None."""
    ctype = str(classification_type or "").strip().lower()
    if ctype in _NON_VISUAL_TYPES:
        return None
    if ctype in _ROUTE_BY_TYPE:
        return _ROUTE_BY_TYPE[ctype]
    low = str(error_text or "").lower()
    if any(m in low for m in _CHALLENGE_MARKERS):
        return ("challenge_detection", "hitl")
    if any(m in low for m in _LOCATOR_MARKERS):
        return ("visual_locate", "retry_by_id")
    return None


def is_visual_failure(classification_type: str, error_text: str) -> bool:
    return route_failure(classification_type, error_text) is not None


async def arbitrate(
    browser: Any,
    page_id: str,
    *,
    classification_type: str = "",
    error_text: str = "",
    target_description: str = "",
    vl_config: Any,
    screenshot_fn: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
    vl_call_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    locate_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    logger: Any = None,
) -> Dict[str, Any]:
    """Arbitrate one failure. Returns a recovery recommendation:
      {action:'dismiss',     point:{x,y}, label, mode, intent}        — overlay
      {action:'hitl',        reason, mode}                            — challenge / unsafe
      {action:'retry_by_id', id, label, mode}  OR coordinate fallback — locate
      {action:'reperceive',  mode}                                    — layout/render
      {action:'continue'|'none', reason}                              — benign / not visual
    All VL/IO is injectable for testing."""
    route = route_failure(classification_type, error_text)
    if route is None:
        return {"action": "none", "reason": "not_a_visual_failure"}
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return {"action": "none", "reason": "vl_disabled"}
    mode, intent = route

    # Locator failure → reuse Role A locate_target (screenshot→VL→bbox→id promotion).
    if mode == "visual_locate":
        out = await (locate_fn or _default_locate)(
            browser, page_id, target_description or error_text, vl_config, screenshot_fn)
        if not out.get("ok"):
            return {"action": "hitl", "mode": mode, "reason": out.get("reason", "not_found")}
        if out.get("consequential"):
            return {"action": "hitl", "mode": mode, "reason": "consequential_target"}
        if out.get("id"):
            return {"action": "retry_by_id", "id": out["id"], "label": out.get("label"),
                    "mode": mode, "intent": intent}
        return {"action": "coordinate", "cssPoint": out.get("cssPoint"), "mode": mode,
                "reason": "axtree_blind_spot"}

    # Other roles → one VL call, map verdict → action.
    shot = await (screenshot_fn or _default_screenshot)(browser, page_id)
    if not shot:
        return {"action": "none", "reason": "no_screenshot"}
    vl = await (vl_call_fn or _default_vl_call)(vl_config, shot, mode, target_description or error_text)
    verdict = str(vl.get("verdict") or "uncertain")
    rec = _verdict_to_action(mode, intent, vl, verdict)
    _log(logger, "vl.arbiter.result", {"pageId": page_id, "mode": mode,
                                        "verdict": verdict, "action": rec.get("action")})
    return rec


def _verdict_to_action(mode: str, intent: str, vl: Dict[str, Any], verdict: str) -> Dict[str, Any]:
    if mode == "overlay_classify":
        if verdict == "dismiss_found" and vl.get("dismiss_point") and not vl.get("is_consequential"):
            return {"action": "dismiss", "point": vl["dismiss_point"],
                    "label": vl.get("control_label"), "mode": mode, "intent": intent}
        if verdict == "no_overlay":
            return {"action": "continue", "mode": mode, "reason": "no_overlay"}
        return {"action": "hitl", "mode": mode, "reason": "no_safe_dismiss"}
    if mode == "challenge_detection":
        if verdict == "confirmed_challenge":
            return {"action": "hitl", "mode": mode, "reason": "confirmed_challenge"}
        if verdict in ("normal_loading",):
            return {"action": "reperceive", "mode": mode, "reason": "normal_loading"}
        return {"action": "none", "mode": mode, "reason": verdict}
    # action_outcome
    if verdict == "match":
        return {"action": "continue", "mode": mode}
    if verdict == "blocked":
        return {"action": "hitl", "mode": mode, "reason": "blocked"}
    return {"action": "reperceive", "mode": mode, "reason": verdict}


# ── default I/O wiring ──────────────────────────────────────────────────────────

async def _default_screenshot(browser: Any, page_id: str) -> Optional[str]:
    from harness.skill.control import _default_screenshot as shot
    return await shot(browser, page_id)


async def _default_vl_call(vl_config: Any, image_path: str, mode: str, question: str) -> Dict[str, Any]:
    from harness.vl.core import visual_verify_image
    return await visual_verify_image(config=vl_config, image_path=image_path,
                                     expected={}, mode=mode, question=question)


async def _default_locate(browser: Any, page_id: str, target: str, vl_config: Any,
                          screenshot_fn: Optional[Callable[..., Awaitable[Optional[str]]]]) -> Dict[str, Any]:
    from harness.vl.locate import locate_target
    return await locate_target(browser, page_id, target, vl_config=vl_config,
                               screenshot_fn=screenshot_fn or _default_screenshot)


def _log(logger: Any, event: str, payload: Dict[str, Any]) -> None:
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:  # pragma: no cover
            pass
