"""
harness.vl - Narrow visual verification helper for screenshot verdicts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from runtime_config import VLConfig
from harness.utils import JsonDict
# Single source of truth for "which challenge types may be driven, with which
# action" — the harness solve loop imports the same table.
from harness.vl.captcha import TYPE_ACTIONS as _TYPE_ACTIONS


VISUAL_VERIFY_SYSTEM = (
    "You are a visual verification component inside a browser automation harness. "
    "Use the screenshot only to verify page state or action outcome. Do not extract "
    "long tables/lists or invent unseen data. Return JSON only."
)


def build_visual_verify_prompt(
    *,
    expected: JsonDict,
    mode: str,
    question: str,
) -> str:
    if mode == "repair_absence":
        return (
            "Determine whether the screenshot shows that the exact repair-target"
            " fields below are ABSENT from their expected page. This is a"
            " fail-closed browser repair check, not a request to extract or"
            " summarize page content.\n"
            f"repair_targets: {json.dumps((expected or {}).get('repair_targets') or [], ensure_ascii=False, default=str)}\n"
            f"context: {question or '(none)'}\n\n"
            "Return absent only when the screenshot gives sufficient coverage of"
            " the relevant page region and the expected field content is not"
            " present. Return present when that content or section is visibly"
            " present, even if its value is incomplete. Return uncertain when the"
            " target region is off-screen, collapsed, obscured, still loading, or"
            " otherwise cannot be judged. Do not infer absence from a missing"
            " screenshot crop.\n"
            "Return exactly one JSON object with keys:\n"
            "- verdict: one of absent, present, uncertain\n"
            "- confidence: number from 0 to 1\n"
            "- visible_evidence: short array of visible screenshot observations\n"
            "- reason: one short sentence\n"
        )
    if mode == "overlay_classify":
        return (
            "An automated browser action was blocked by an overlay covering the"
            " page (a modal, popup, cookie/consent banner, or newsletter dialog)."
            " Locate the SAFE control that dismisses the overlay so automation can"
            " continue: a close/X button, or 'Skip' / 'Not now' / 'Maybe later' /"
            " 'No thanks' / 'Got it' / 'Continue without' (for a cookie/consent"
            " banner, 'Reject all' or 'Accept all' is also safe).\n"
            "SAFETY: Never pick a login, sign-up, subscribe, pay, purchase,"
            " checkout, or 'continue with Google/Apple' control. If the ONLY way"
            " past the overlay is such a consequential control, return"
            " no_safe_dismiss.\n"
            f"question: {question or '(none)'}\n"
            f"expected: {json.dumps(expected or {}, ensure_ascii=False, default=str)}\n\n"
            "Coordinates are NORMALIZED integers from 0 to 1000, origin at the"
            " top-left corner: x = round(1000 * pixelX / imageWidth), y = round("
            "1000 * pixelY / imageHeight). Point at the CENTER of the chosen"
            " control.\n"
            "Return exactly one JSON object with keys:\n"
            "- verdict: one of dismiss_found, no_overlay, no_safe_dismiss, uncertain\n"
            "- dismiss_point: object {x: number, y: number} normalized 0-1000"
            " (omit or null unless verdict is dismiss_found)\n"
            "- control_label: short visible text/label on the chosen control\n"
            "- is_consequential: boolean, true if the control could log in, pay,"
            " subscribe, or otherwise act on the user's behalf\n"
            "- confidence: number from 0 to 1\n"
            "- visible_evidence: short array of visible screenshot observations\n"
            "- reason: one short sentence\n"
        )
    if mode == "region_reality":
        return (
            "Report what this screenshot shows about ONE region of ONE page.\n"
            f"{question or '(no region described)'}\n\n"
            "Judge ONLY the region described above, on THIS page. Do not judge"
            " any other item, do not judge how many items the wider task needs,"
            " and do not count anything outside the region.\n\n"
            "Choose exactly one classification:\n"
            "- content_present: the region is visible and holds at least one"
            " content item.\n"
            "- explicit_empty_state: the region is visible and THE PAGE ITSELF"
            " says it is empty — a message like 'No reviews yet', 'Be the first"
            " to comment', '0 results', or an empty-state illustration with"
            " text. Blank space, whitespace, or a section you cannot find is"
            " NOT this class.\n"
            "- auth_overlay_present: a login, sign-up, subscribe, or paywall"
            " overlay covers the content.\n"
            "- region_not_in_capture: the region is not inside this screenshot"
            " — cut off, below the fold, collapsed behind a tab/accordion,"
            " still loading, or simply not findable here. Use this whenever"
            " your honest answer is 'I cannot see it'.\n"
            "- uncertain: the region is in frame but you cannot tell which of"
            " the above applies.\n\n"
            "The single most costly error is reporting emptiness for a region"
            " you could not actually see. 'I don't see it' is"
            " region_not_in_capture, never explicit_empty_state.\n"
            "Return exactly one JSON object with keys:\n"
            "- classification: one of the five values above\n"
            "- item_count: integer count of items visible IN THE REGION (0 when"
            " none are visible; omit or null when not countable)\n"
            "- region_found: boolean, whether you located the region at all\n"
            "- confidence: number from 0 to 1\n"
            "- visible_evidence: short array of literal text you can read in"
            " the region (quote the page, do not paraphrase)\n"
            "- reason: one short sentence\n"
        )
    if mode == "contract_verify":
        return (
            "Judge whether this browser screenshot SATISFIES the structured success"
            " checks below. A browser action returning no error does NOT mean the"
            " task succeeded — verify the visible end state.\n"
            f"checks: {json.dumps((expected or {}).get('visual_checks') or expected or [], ensure_ascii=False, default=str)}\n"
            f"context: {question or '(none)'}\n\n"
            "Each check is one of:\n"
            "  {type:'text_present', text:'...'}   — the text must be visibly present\n"
            "  {type:'text_absent',  text:'...'}   — the text must NOT be present\n"
            "  {type:'challenge_gone'}             — no CAPTCHA/verification/anti-bot visible\n"
            "  {type:'element_visible', description:'...'}  — the described element is visible\n"
            "  {type:'state', description:'...'}    — the page is in the described state\n"
            "Be strict: if a required check is clearly not met, it is violated. If you"
            " genuinely cannot tell from the screenshot, use uncertain (do NOT guess"
            " 'satisfied').\n"
            "Return exactly one JSON object with keys:\n"
            "- verdict: one of satisfied, violated, uncertain\n"
            "- failed_checks: array of the checks (or their text/description) NOT met\n"
            "- visible_evidence: short array of visible screenshot observations\n"
            "- confidence: number from 0 to 1\n"
            "- reason: one short sentence\n"
        )
    if mode == "visual_locate":
        return (
            "Locate a specific target element in this browser screenshot so an"
            " automation harness can act on it. The target may be a visual element"
            " the accessibility tree can't reach (canvas drawing, text inside an"
            " image, a purely visual control).\n"
            f"target: {question or '(the element described in expected)'}\n"
            f"expected: {json.dumps(expected or {}, ensure_ascii=False, default=str)}\n\n"
            "Coordinates are NORMALIZED integers 0..1000, origin top-left:"
            " x = round(1000 * pixelX / imageWidth), y = round(1000 * pixelY /"
            " imageHeight). Point at the CENTER of the target.\n"
            "SAFETY: set is_consequential=true if the target could log in, pay,"
            " subscribe, submit, delete, or otherwise act on the user's behalf.\n"
            "Return exactly one JSON object with keys:\n"
            "- verdict: one of located, not_found, uncertain\n"
            "- point: object {x: number, y: number} normalized 0-1000"
            " (omit or null unless verdict is located)\n"
            "- control_label: short visible text/label on the target\n"
            "- is_consequential: boolean\n"
            "- confidence: number from 0 to 1\n"
            "- visible_evidence: short array of visible screenshot observations\n"
            "- reason: one short sentence\n"
        )
    if mode == "captcha_solve":
        return (
            "This browser screenshot may be blocked by a CAPTCHA / anti-bot"
            " challenge. Classify it and, ONLY for a purely visual puzzle, output"
            " an ordered solve plan so the harness can drive it.\n"
            "BE HONEST ABOUT SOLVABILITY (critical):\n"
            "- visual_self_consistent: everything needed to finish is visible —"
            " slider gap, pick-the-tiles grid, rotate-to-upright,"
            " click-the-target, read the distorted text (OCR). These are"
            " solvable; give a solve_plan.\n"
            "  A plain drag track with NO puzzle image (a handle at one end of a"
            " bar reading 'slide to verify' / '请按住滑块拖动' or similar) also"
            " belongs here: the whole task is 'move the handle to the far end',"
            " and it is fully determined by what you can see. Classify it"
            " challenge_type=slider, challenge_category=visual_self_consistent,"
            " and give the drag step. Do NOT call it behavioral_risk merely"
            " because the site may also score the drag, and do NOT report a"
            " missing gap as 'target not rendered' — a bare track has no gap by"
            " design.\n"
            "- behavioral_risk: reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile —"
            " these score mouse trajectory / timing / fingerprint / entropy and"
            " expose NO visible task to complete. You CANNOT solve these. Set"
            " challenge_category=behavioral_risk, verdict=unsolvable, and"
            " solve_plan=[] (empty). Do not guess a plan — wrong attempts"
            " escalate difficulty or trigger bans.\n"
            "- unknown / not a challenge: set solve_plan=[] and the matching verdict.\n"
            f"question: {question or '(none)'}\n"
            f"expected: {json.dumps(expected or {}, ensure_ascii=False, default=str)}\n\n"
            "Coordinates are NORMALIZED integers 0..1000, origin top-left:"
            " x = round(1000 * pixelX / imageWidth), y = round(1000 * pixelY /"
            " imageHeight). Point at element CENTERS.\n"
            "Return exactly one JSON object with keys:\n"
            "- verdict: one of solvable, unsolvable, not_a_challenge, uncertain\n"
            "- challenge_type: one of slider, grid, rotate, click_target, text_ocr,"
            " behavioral, hybrid, unknown\n"
            "- challenge_category: one of visual_self_consistent, behavioral_risk, unknown\n"
            "- solve_plan: array of steps (NON-EMPTY only for visual_self_consistent)."
            " Each step is one of:\n"
            "    slider:       {action:'drag', from:{x,y}, dx:number, dy:number}\n"
            "                  (from = the handle's CENTER; dx = how far it must"
            " travel. For a bare track that is the full remaining width of the"
            " track, not a guess.)\n"
            "    rotate:       {action:'drag_arc', from:{x,y}, to:{x,y}}\n"
            "    grid:         {action:'click', at:{x,y}, label:string}\n"
            "    click_target: {action:'click', at:{x,y}, label:string}\n"
            "    text_ocr:     {action:'type', text:string, into:{x,y}}\n"
            "- confidence: number from 0 to 1\n"
            "- visible_evidence: short array of visible screenshot observations\n"
            "- reason: one short sentence\n"
        )
    if mode == "challenge_detection":
        return (
            "Decide whether this browser screenshot is blocked by an anti-bot,"
            " CAPTCHA, Cloudflare/security verification, login wall, or other"
            " page that requires human action before automation can continue.\n"
            f"question: {question or '(none)'}\n"
            f"expected: {json.dumps(expected or {}, ensure_ascii=False, default=str)}\n\n"
            "Return exactly one JSON object with keys:\n"
            "- verdict: one of confirmed_challenge, normal_loading, unrelated_block, uncertain\n"
            "- confidence: number from 0 to 1\n"
            "- visible_evidence: short array of visible screenshot observations\n"
            "- recommended_recovery: one of hitl, continue, retry_navigation, use_dom\n"
            "- reason: one short sentence\n"
        )
    return (
        "Verify the browser screenshot against this expected state.\n"
        f"mode: {mode or 'action_outcome'}\n"
        f"question: {question or '(none)'}\n"
        f"expected: {json.dumps(expected or {}, ensure_ascii=False, default=str)}\n\n"
        "Return exactly one JSON object with keys:\n"
        "- verdict: one of match, mismatch, blocked, uncertain\n"
        "- confidence: number from 0 to 1\n"
        "- visible_evidence: short array of visible screenshot observations\n"
        "- recommended_recovery: one of continue, retry_click, close_overlay, hitl, use_dom, retry_navigation\n"
        "- reason: one short sentence\n"
    )


async def visual_verify_image(
    *,
    config: VLConfig,
    image_path: str,
    expected: JsonDict,
    mode: str = "action_outcome",
    question: str = "",
) -> JsonDict:
    if not config.enabled:
        return {"status": "disabled", "reason": "vl.enabled is false"}
    if not config.model_id:
        return {"status": "failed", "error": "vl.model_id is required"}

    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return {"status": "failed", "error": "screenshot file is missing", "path": image_path}

    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    prompt = build_visual_verify_prompt(
        expected=expected,
        mode=mode,
        question=question,
    )

    provider = (config.provider or "openai").strip().lower()
    started = time.monotonic()
    timeout_seconds = (
        float(getattr(config, "captcha_solve_timeout_seconds", 150.0) or 150.0)
        if mode == "captcha_solve"
        else float(config.default_timeout_seconds)
    )
    role_extra_params = (
        getattr(config, "captcha_solve_extra_params", {}) or {}
        if mode == "captcha_solve"
        else {}
    )
    try:
        if provider == "openai":
            raw_text, usage = await _call_openai_compatible(
                config=config,
                image_b64=image_b64,
                mime_type=mime_type,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                role_extra_params=role_extra_params,
            )
        elif provider == "anthropic":
            raw_text, usage = await _call_anthropic_compatible(
                config=config,
                image_b64=image_b64,
                mime_type=mime_type,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                role_extra_params=role_extra_params,
            )
        else:
            return {
                "status": "failed",
                "error": "vl.provider must be openai or anthropic",
                "provider": config.provider,
            }
    except Exception as exc:
        error_type = _vl_provider_error_type(exc)
        error_text = str(exc).strip() or error_type
        return {
            "status": "failed",
            "error": error_text,
            "errorType": error_type,
            "elapsedMs": int((time.monotonic() - started) * 1000),
            "timeoutSeconds": timeout_seconds,
            "provider": provider,
            "model": config.model_id,
        }

    parsed = _parse_json_object(raw_text)
    if not isinstance(parsed, dict):
        return {
            "status": "failed",
            "error": "VL response was not valid JSON",
            "raw": raw_text[:2000],
            "usage": usage,
        }

    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    if mode == "overlay_classify":
        return _finalize_overlay_classify(parsed, usage)
    if mode == "captcha_solve":
        return _finalize_captcha_solve(parsed, usage)
    if mode == "visual_locate":
        return _finalize_visual_locate(parsed, usage)
    if mode == "contract_verify":
        return _finalize_contract_verify(parsed, usage)
    if mode == "repair_absence":
        return _finalize_repair_absence(parsed, usage)
    if mode == "region_reality":
        return _finalize_region_reality(parsed, usage)
    allowed_verdicts = (
        {"confirmed_challenge", "normal_loading", "unrelated_block", "uncertain"}
        if mode == "challenge_detection"
        else {"match", "mismatch", "blocked", "uncertain"}
    )
    if verdict not in allowed_verdicts:
        verdict = "uncertain"
    recovery = str(parsed.get("recommended_recovery") or "use_dom").strip().lower()
    allowed_recoveries = {
        "continue",
        "retry_click",
        "close_overlay",
        "hitl",
        "use_dom",
        "retry_navigation",
    }
    if recovery not in allowed_recoveries:
        recovery = "use_dom"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []

    return {
        "status": "done",
        "verdict": verdict,
        "confidence": confidence,
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "recommended_recovery": recovery,
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }


def _finalize_repair_absence(parsed: JsonDict, usage: JsonDict) -> JsonDict:
    """Normalize the dedicated, fail-closed repair absence verdict."""
    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    if verdict not in {"absent", "present", "uncertain"}:
        verdict = "uncertain"
    try:
        confidence = max(0.0, min(float(parsed.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "status": "done",
        "mode": "repair_absence",
        "verdict": verdict,
        "confidence": confidence,
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }


def _finalize_region_reality(parsed: JsonDict, usage: JsonDict) -> JsonDict:
    """Normalize a region_reality verdict into the scored class enum.

    Two normalizations matter beyond clamping. An unknown class becomes
    `uncertain` rather than being passed through, so the offline precision
    evaluation and the runtime reconciliation always score the same five
    values. And a content/empty claim from a model that also answered
    `region_found: false` is demoted to `region_not_in_capture` — the model
    contradicted itself, and the reading that keeps work going is the safe one.
    """
    from harness.vl.capture_geometry import (
        CLASS_AUTH_OVERLAY,
        CLASS_REGION_NOT_IN_CAPTURE,
        CLASS_UNCERTAIN,
        REGION_CLASSES,
    )

    classification = str(
        parsed.get("classification") or parsed.get("verdict") or ""
    ).strip().lower()
    if classification not in REGION_CLASSES:
        classification = CLASS_UNCERTAIN
    region_found = parsed.get("region_found")
    if (
        region_found is False
        and classification not in {CLASS_REGION_NOT_IN_CAPTURE, CLASS_AUTH_OVERLAY}
    ):
        classification = CLASS_REGION_NOT_IN_CAPTURE

    item_count: Optional[int] = None
    raw_count = parsed.get("item_count")
    if not isinstance(raw_count, bool) and isinstance(raw_count, (int, float)):
        item_count = max(0, int(raw_count))

    try:
        confidence = max(0.0, min(float(parsed.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "status": "done",
        "mode": "region_reality",
        # `verdict` is kept as a mirror of the class so every consumer of a VL
        # result (loggers, the reality-check row, the arbiter) keeps working
        # without learning a second field name.
        "verdict": classification,
        "classification": classification,
        "itemCount": item_count,
        "regionFound": bool(region_found) if isinstance(region_found, bool) else None,
        "confidence": confidence,
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }


def _finalize_overlay_classify(parsed: JsonDict, usage: JsonDict) -> JsonDict:
    """Normalize an overlay_classify VL response. dismiss_point stays in the
    model's NORMALIZED 0-1000 grounding space; the caller back-translates to CSS
    coordinates and runs an independent elementFromPoint safety check before any
    click."""
    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    if verdict not in {"dismiss_found", "no_overlay", "no_safe_dismiss", "uncertain"}:
        verdict = "uncertain"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    point: Optional[JsonDict] = None
    raw_point = parsed.get("dismiss_point")
    if isinstance(raw_point, dict):
        try:
            point = {"x": float(raw_point.get("x")), "y": float(raw_point.get("y"))}
        except (TypeError, ValueError):
            point = None
    elif isinstance(raw_point, (list, tuple)) and len(raw_point) >= 2:
        try:
            point = {"x": float(raw_point[0]), "y": float(raw_point[1])}
        except (TypeError, ValueError):
            point = None
    # A dismiss_found verdict without a usable point is downgraded: the caller
    # has nothing safe to click.
    if verdict == "dismiss_found" and point is None:
        verdict = "uncertain"

    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "status": "done",
        "mode": "overlay_classify",
        "verdict": verdict,
        "confidence": confidence,
        "dismiss_point": point,
        "control_label": str(parsed.get("control_label") or "")[:200],
        "is_consequential": bool(parsed.get("is_consequential", False)),
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }


_CAPTCHA_VERDICTS = {"solvable", "unsolvable", "not_a_challenge", "uncertain"}
_CAPTCHA_TYPES = {"slider", "grid", "rotate", "click_target", "text_ocr",
                  "behavioral", "hybrid", "unknown"}
_CAPTCHA_CATEGORIES = {"visual_self_consistent", "behavioral_risk", "unknown"}
_SOLVE_ACTIONS = {"drag", "drag_arc", "click", "type"}
_AUTO_SOLVABLE_TYPES = frozenset(_TYPE_ACTIONS)


def _norm_point(raw: Any) -> Optional[JsonDict]:
    """Coerce a {x,y} (or [x,y]) into a normalized 0-1000 point dict, else None."""
    if isinstance(raw, dict):
        try:
            return {"x": float(raw.get("x")), "y": float(raw.get("y"))}
        except (TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return {"x": float(raw[0]), "y": float(raw[1])}
        except (TypeError, ValueError):
            return None
    return None


def _normalize_solve_step(step: Any) -> Optional[JsonDict]:
    """Validate one SolveStep; coords stay NORMALIZED 0-1000 (caller maps to CSS).
    Returns a clean step dict or None if malformed (dropped)."""
    if not isinstance(step, dict):
        return None
    action = str(step.get("action") or "").strip().lower()
    if action not in _SOLVE_ACTIONS:
        return None
    if action == "drag":  # slider: from + dx/dy offset
        frm = _norm_point(step.get("from"))
        if frm is None:
            return None
        try:
            dx = float(step.get("dx", 0.0))
            dy = float(step.get("dy", 0.0))
        except (TypeError, ValueError):
            return None
        return {"action": "drag", "from": frm, "dx": dx, "dy": dy,
                "label": str(step.get("label") or "")[:120]}
    if action == "drag_arc":  # rotate: from -> to
        frm, to = _norm_point(step.get("from")), _norm_point(step.get("to"))
        if frm is None or to is None:
            return None
        return {"action": "drag_arc", "from": frm, "to": to,
                "label": str(step.get("label") or "")[:120]}
    if action == "click":  # grid / click_target
        at = _norm_point(step.get("at"))
        if at is None:
            return None
        return {"action": "click", "at": at, "label": str(step.get("label") or "")[:120]}
    # type: text into a focus point
    into = _norm_point(step.get("into"))
    text = str(step.get("text") or "")
    if into is None or not text:
        return None
    return {"action": "type", "into": into, "text": text[:200],
            "label": str(step.get("label") or "")[:120]}


def _finalize_contract_verify(parsed: JsonDict, usage: JsonDict) -> JsonDict:
    """Normalize a contract_verify (VL judge) response. verdict ∈ satisfied/violated/
    uncertain; failed_checks lists the unmet checks. The caller treats only a
    definitive `violated` as a veto (VL is L4 — `uncertain` never overrides a passed
    variable contract)."""
    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    if verdict not in {"satisfied", "violated", "uncertain"}:
        verdict = "uncertain"
    try:
        confidence = max(0.0, min(float(parsed.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    failed = parsed.get("failed_checks")
    if not isinstance(failed, list):
        failed = []
    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "status": "done",
        "mode": "contract_verify",
        "verdict": verdict,
        "failed_checks": [str(c)[:200] for c in failed[:12]],
        "confidence": confidence,
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }


def _finalize_visual_locate(parsed: JsonDict, usage: JsonDict) -> JsonDict:
    """Normalize a visual_locate response. `point` stays NORMALIZED 0-1000; the
    caller maps to screenshot px and runs bbox→id promotion + safety checks."""
    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    if verdict not in {"located", "not_found", "uncertain"}:
        verdict = "uncertain"
    point = _norm_point(parsed.get("point"))
    if verdict == "located" and point is None:
        verdict = "uncertain"
    try:
        confidence = max(0.0, min(float(parsed.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "status": "done",
        "mode": "visual_locate",
        "verdict": verdict,
        "point": point,
        "control_label": str(parsed.get("control_label") or "")[:200],
        "is_consequential": bool(parsed.get("is_consequential", False)),
        "confidence": confidence,
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }


def _finalize_captcha_solve(parsed: JsonDict, usage: JsonDict) -> JsonDict:
    """Normalize a captcha_solve response. SAFETY-CRITICAL honest short-circuit:
    a non-visual_self_consistent category (behavioral_risk / unknown) is forced to
    verdict=unsolvable with an EMPTY solve_plan regardless of what the model said —
    behavioral challenges score trajectory/timing/fingerprint, not a visual answer,
    so attempting them escalates difficulty or triggers bans. solve_plan steps stay
    in normalized 0-1000 space; the caller maps to CSS and runs elementFromPoint +
    is_consequential safety checks before any Input."""
    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    if verdict not in _CAPTCHA_VERDICTS:
        verdict = "uncertain"
    ctype = str(parsed.get("challenge_type") or "unknown").strip().lower()
    if ctype not in _CAPTCHA_TYPES:
        ctype = "unknown"
    category = str(parsed.get("challenge_category") or "unknown").strip().lower()
    if category not in _CAPTCHA_CATEGORIES:
        category = "unknown"
    try:
        confidence = max(0.0, min(float(parsed.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0

    raw_plan = parsed.get("solve_plan")
    solve_plan: list = []
    if isinstance(raw_plan, list):
        for step in raw_plan[:12]:
            norm = _normalize_solve_step(step)
            if norm is not None:
                solve_plan.append(norm)

    short_circuit_reason = None
    # Honest hard-distinction: only visual_self_consistent puzzles are ever solved.
    if category != "visual_self_consistent":
        if solve_plan:
            short_circuit_reason = f"category={category} is not visually solvable; plan discarded"
        solve_plan = []
        if verdict == "solvable":
            verdict = "unsolvable"
    # Type allow-list, independent of the category the model chose: `hybrid`
    # (part visual, part behavioral scoring) and `behavioral`/`unknown` are never
    # driven, even when the model also labels them visual_self_consistent.
    elif ctype not in _AUTO_SOLVABLE_TYPES:
        if solve_plan:
            short_circuit_reason = (
                f"challenge_type={ctype} is not auto-solvable; plan discarded"
            )
        solve_plan = []
        if verdict == "solvable":
            verdict = "unsolvable"
    # Type/action consistency: a `slider` verdict carrying grid clicks means the
    # model contradicted itself, and a self-contradicting classification is not
    # something to act on.
    elif solve_plan and any(
        step.get("action") not in _TYPE_ACTIONS[ctype] for step in solve_plan
    ):
        short_circuit_reason = (
            f"solve plan actions do not match challenge_type={ctype}; plan discarded"
        )
        solve_plan = []
        if verdict == "solvable":
            verdict = "uncertain"
    # A 'solvable' verdict with nothing to do is not actionable.
    elif verdict == "solvable" and not solve_plan:
        verdict = "uncertain"
        short_circuit_reason = "solvable but no usable solve_plan steps"

    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []
    out = {
        "status": "done",
        "mode": "captcha_solve",
        "verdict": verdict,
        "challenge_type": ctype,
        "challenge_category": category,
        "solve_plan": solve_plan,
        "confidence": confidence,
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }
    if short_circuit_reason:
        out["short_circuit_reason"] = short_circuit_reason
    return out


async def _call_openai_compatible(
    *,
    config: VLConfig,
    image_b64: str,
    mime_type: str,
    prompt: str,
    timeout_seconds: float,
    role_extra_params: JsonDict,
) -> tuple[str, JsonDict]:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("openai SDK is required for vl.provider=openai") from exc

    api_key = config.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("VL OpenAI-compatible api_key is missing")
    # Disable the SDK's hidden retry loop: otherwise a 429/5xx can consume the
    # whole caller timeout and be misreported as a model timeout.
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=config.base_url,
        max_retries=0,
        timeout=timeout_seconds,
    )
    params: JsonDict = {
        "model": config.model_id,
        "messages": [
            {"role": "system", "content": VISUAL_VERIFY_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_b64}",
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 800,
    }
    params.update(config.extra_params or {})
    params.update(role_extra_params or {})
    response = await client.chat.completions.create(**params)
    text = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    return text, {
        "provider": "openai",
        "model": config.model_id,
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
    }


async def _call_anthropic_compatible(
    *,
    config: VLConfig,
    image_b64: str,
    mime_type: str,
    prompt: str,
    timeout_seconds: float,
    role_extra_params: JsonDict,
) -> tuple[str, JsonDict]:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise RuntimeError("anthropic SDK is required for vl.provider=anthropic") from exc

    api_key = config.api_key or os.getenv("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise RuntimeError("VL Anthropic-compatible api_key is missing")
    client = AsyncAnthropic(
        api_key=api_key,
        base_url=config.base_url,
        max_retries=0,
        timeout=timeout_seconds,
    )
    params: JsonDict = {
        "model": config.model_id,
        "system": VISUAL_VERIFY_SYSTEM,
        "max_tokens": int((config.extra_params or {}).get("max_tokens", 800)),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_b64,
                        },
                    },
                ],
            }
        ],
    }
    # Symmetric with the OpenAI-compatible path, but still role-scoped: these
    # parameters are never present for an ordinary visual_verify call.
    params.update(role_extra_params or {})
    response = await client.messages.create(**params)
    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "") or ""
    usage = getattr(response, "usage", None)
    return text, {
        "provider": "anthropic",
        "model": config.model_id,
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
    }


def _vl_provider_error_type(exc: BaseException) -> str:
    """Stable provider-failure taxonomy; never turn transport into semantics."""

    name = type(exc).__name__.lower()
    text = str(exc or "").lower()
    status = getattr(exc, "status_code", None)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in name:
        return "model_timeout"
    if status == 429 or "ratelimit" in name or "rate limit" in text:
        return "model_rate_limited"
    if isinstance(status, int) and status >= 500:
        return "model_upstream_error"
    return "model_transport_error"


def _parse_json_object(text: str) -> Optional[JsonDict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None
