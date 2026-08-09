"""harness.vl.captcha - Role C engine: bounded VL CAPTCHA auto-solve.

Connection-agnostic core shared by every caller that wants "let the VL try N
times before a human is asked":

  * the worker hot path (``harness.tools.browser_tools.captcha_autosolve``),
    which drives the page over the agent's own primary connection before any
    ``Hitl.requestPause`` is issued;
  * the skill workflow control channel (``harness.skill.control``), which drives
    a page that is already HITL-paused over a second connection.

The engine owns only the ORDER of operations and the budget; every I/O leg is
injected as a zero/low-arg awaitable so both callers keep their own transport,
safety oracle, and clearance verifier.

Safety contract (all enforced outside the model's discretion):
  * ``vl._finalize_captcha_solve`` already forces behavioral-risk challenges
    (reCAPTCHA / hCaptcha / Turnstile) to ``verdict=unsolvable`` with an empty
    plan — attempting those escalates difficulty or triggers bans, so the loop
    simply never receives a plan for them.
  * Every coordinate-bearing step is re-checked against the LIVE element at that
    point by the caller's ``safety_fn``; one rejection aborts the whole solve.
  * Clearance is decided by the caller's ``verify_fn`` (an independent oracle),
    never by the VL claiming success.
  * The loop is bounded twice: ``max_attempts`` and a wall-clock ``budget``.

Any non-``solved``/``not_a_challenge`` outcome is a hand-off: the caller falls
back to the human path with the receipt attached as evidence.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

JsonDict = Dict[str, Any]

# qwen-VL grounding convention: coordinates are normalized to 0-1000 (top-left
# origin), so css = norm/1000 * innerWidth — DPR and image size independent.
VL_NORMALIZED_SCALE = 1000.0

SOLVED = "solved"
NOT_A_CHALLENGE = "not_a_challenge"
UNSOLVABLE = "unsolvable"
UNSAFE_ABORT = "unsafe_abort"
EXHAUSTED = "exhausted"
NO_SCREENSHOT = "no_screenshot"
VIEWPORT_UNAVAILABLE = "viewport_unavailable"
BUDGET_EXCEEDED = "budget_exceeded"
LOW_CONFIDENCE = "low_confidence"
VERIFICATION_UNAVAILABLE = "verification_unavailable"
CHALLENGE_PERSISTS = "challenge_persists"
ERROR = "error"
VL_UNAVAILABLE = "vl_unavailable"

# The ONLY outcomes that may skip the human. Both are reached exclusively
# through the independent clearance verifier — including `not_a_challenge`,
# which is a VL opinion and never clears a page on its own.
CLEARED_STATUSES = frozenset({SOLVED, NOT_A_CHALLENGE})

# challenge_type → the single Input action that type is allowed to produce.
# A grid plan under a `slider` label (or any other mismatch) is a
# misclassification, not something to execute.
TYPE_ACTIONS = {
    "slider": {"drag"},
    "rotate": {"drag_arc"},
    "grid": {"click"},
    "click_target": {"click"},
    "text_ocr": {"type"},
}
OCR_CHALLENGE_TYPE = "text_ocr"
# Allow-list: every other type (behavioral, hybrid, unknown, anything new the
# model invents) is never driven automatically.  OCR is deliberately excluded:
# current native AXTree snapshots do not prove which element owns focus, so an
# automated Input.type could write into a credential field.
SOLVABLE_CHALLENGE_TYPES = frozenset(TYPE_ACTIONS) - {OCR_CHALLENGE_TYPE}


def _log(logger: Any, event: str, payload: JsonDict) -> None:
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:  # pragma: no cover - logging must never break a solve
            pass


def viewport_size(metrics: Any) -> Tuple[float, float]:
    """Accept both viewport spellings in use: {"w","h"} (skill control channel)
    and {"width","height"} (observation.verifiers.probe_viewport_metrics)."""
    source = metrics if isinstance(metrics, dict) else {}

    def _num(*keys: str) -> float:
        for key in keys:
            try:
                value = float(source.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0

    return _num("w", "width"), _num("h", "height")


def _norm_to_css(point: Any, w: float, h: float) -> Tuple[float, float]:
    """Map a normalized 0-1000 grounding point to CSS px."""
    source = point if isinstance(point, dict) else {}

    def _num(key: str) -> float:
        try:
            return float(source.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    return _num("x") / VL_NORMALIZED_SCALE * w, _num("y") / VL_NORMALIZED_SCALE * h


def solve_plan_to_input_calls(
    solve_plan: List[Dict[str, Any]],
    page_id: str,
    metrics: Dict[str, Any],
    *,
    purpose: str = "VL-driven CAPTCHA solve step",
) -> List[Tuple[str, JsonDict]]:
    """Pure translation: normalized SolveSteps → ordered (method, params) Input
    calls, using viewport metrics to back-translate to CSS px."""
    w, h = viewport_size(metrics)
    calls: List[Tuple[str, JsonDict]] = []
    for step in solve_plan or []:
        if not isinstance(step, dict):
            continue
        action = step.get("action")
        if action == "drag":  # slider: source + relative offset
            x, y = _norm_to_css(step.get("from"), w, h)
            dx = float(step.get("dx", 0.0) or 0.0) / VL_NORMALIZED_SCALE * w
            dy = float(step.get("dy", 0.0) or 0.0) / VL_NORMALIZED_SCALE * h
            calls.append(("Input.drag", {"pageId": page_id, "x": x, "y": y,
                                         "dx": dx, "dy": dy, "purpose": purpose}))
        elif action == "drag_arc":  # rotate: source → destination
            x, y = _norm_to_css(step.get("from"), w, h)
            tx, ty = _norm_to_css(step.get("to"), w, h)
            calls.append(("Input.drag", {"pageId": page_id, "x": x, "y": y,
                                         "toX": tx, "toY": ty, "purpose": purpose}))
        elif action == "click":  # grid / click_target
            x, y = _norm_to_css(step.get("at"), w, h)
            calls.append(("Input.click", {"pageId": page_id, "x": x, "y": y,
                                          "clickCount": 1, "purpose": purpose}))
        elif action == "type":  # text_ocr: focus then type
            x, y = _norm_to_css(step.get("into"), w, h)
            calls.append(("Input.click", {"pageId": page_id, "x": x, "y": y,
                                          "clickCount": 1, "purpose": purpose}))
            calls.append(("Input.type", {"pageId": page_id, "text": step.get("text", ""),
                                         "clear": True, "delay": 40, "purpose": purpose}))
    return calls


def confidence_floor(
    challenge_type: Any,
    min_confidence: float,
    min_confidence_ocr: float,
) -> float:
    """OCR carries a higher bar: a misread string is submitted as a real answer
    (burning one of the site's own attempts), whereas a misplaced drag usually
    just fails to move the puzzle."""
    if str(challenge_type or "").strip().lower() == OCR_CHALLENGE_TYPE:
        return max(min_confidence, min_confidence_ocr)
    return min_confidence


def plan_matches_type(challenge_type: Any, solve_plan: Any) -> bool:
    """True when every step's action is the one this challenge type produces.

    A `slider` verdict carrying grid clicks means the model contradicted itself;
    executing the plan anyway would act on a page nobody classified correctly.
    """
    allowed = TYPE_ACTIONS.get(str(challenge_type or "").strip().lower())
    if not allowed:
        return False
    steps = solve_plan if isinstance(solve_plan, list) else []
    if not steps:
        return False
    return all(
        isinstance(step, dict) and str(step.get("action") or "") in allowed
        for step in steps
    )


async def run_captcha_solve_loop(
    *,
    page_id: str,
    vl_config: Any,
    screenshot_fn: Callable[[], Awaitable[Optional[str]]],
    captcha_solve_fn: Callable[[Any, str], Awaitable[JsonDict]],
    metrics_fn: Callable[[], Awaitable[JsonDict]],
    safety_fn: Callable[[float, float], Awaitable[bool]],
    exec_fn: Callable[[str, JsonDict], Awaitable[Any]],
    verify_fn: Callable[[], Awaitable[Optional[bool]]],
    max_attempts: int = 2,
    budget_seconds: float = 0.0,
    min_confidence: float = 0.0,
    min_confidence_ocr: float = 0.0,
    logger: Any = None,
    log_prefix: str = "vl.captcha",
) -> JsonDict:
    """Try to clear a visual CAPTCHA in at most ``max_attempts`` VL rounds.

    One round = screenshot → VL ``captcha_solve`` → confidence gate → type /
    action consistency → per-point safety gate → drive the page → INDEPENDENT
    clearance check. Returns a receipt; it never raises, because every caller's
    fall-back (the human path) must stay reachable.

    ``verify_fn`` is tri-state and load-bearing: ``True`` = cleared, ``False`` =
    still blocked (retry allowed), ``None`` = could not be determined. Only
    ``True`` ever skips the human — an unavailable verifier hands off, because
    "the probes saw nothing" is not evidence a challenge is gone.

    Budget semantics: no NEW round and no Input action starts once
    ``budget_seconds`` is spent, and the (side-effect-free) screenshot + VL call
    are cancelled at the remaining time. An Input sequence already in flight and
    the final clearance read are allowed to finish, so the wall-clock ceiling is
    "no new action after N seconds", not a hard kill mid-drag.
    """
    started = time.monotonic()
    attempts: List[JsonDict] = []
    # Two consecutive identical unsolvable reads end the retry loop early.
    last_unsolvable_signature: Optional[Tuple[str, str, str]] = None
    receipt: JsonDict = {
        "status": ERROR,
        "pageId": page_id,
        "attempts": attempts,
        "vlCalls": 0,
        "maxAttempts": max(1, int(max_attempts or 1)),
    }

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def _finish(status: str, reason: str = "") -> JsonDict:
        receipt["status"] = status
        receipt["elapsedMs"] = _elapsed_ms()
        if reason:
            receipt["reason"] = reason
        return receipt

    def _remaining() -> Optional[float]:
        if budget_seconds and budget_seconds > 0:
            return budget_seconds - (time.monotonic() - started)
        return None

    def _budget_left() -> bool:
        remaining = _remaining()
        return remaining is None or remaining > 0

    async def _within_budget(awaitable: Awaitable[Any]) -> Any:
        """Bound ONLY the model call by the remaining time.

        Restricted to `captcha_solve_fn` on purpose. Both bindings implement it as
        an HTTP request to the VL provider, which owns its own connection and is
        safe to cancel. Browser legs must NEVER be wrapped: ABCPClient pairs a
        response to the single in-flight request partly BY SHAPE (the panel does
        not always echo the request id), so cancelling a sent ws call releases the
        call lock while its response is still on the wire — the late reply would
        then resolve the NEXT call's future and desync every following read.
        Browser legs are bounded by checking the budget before they start instead.
        """
        remaining = _remaining()
        if remaining is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=max(0.01, remaining))

    async def _verify() -> Tuple[Optional[bool], Optional[JsonDict]]:
        """Run the independent clearance check. Returns (cleared, early_finish)."""
        cleared = await verify_fn()
        if cleared is None:
            _log(logger, f"{log_prefix}.vl_verification_unavailable",
                 {"pageId": page_id})
            return None, _finish(
                VERIFICATION_UNAVAILABLE,
                "the independent clearance check could not read the page;"
                " refusing to claim the challenge is gone",
            )
        return bool(cleared), None

    try:
        for attempt in range(1, max(1, int(max_attempts or 1)) + 1):
            if not _budget_left():
                return _finish(BUDGET_EXCEEDED, "wall-clock solve budget exhausted")

            # Re-read the viewport EVERY round, alongside the fresh screenshot:
            # an interstitial replaced by the real page, or a challenge iframe that
            # mounts after round 1, changes innerWidth/innerHeight — and a stale
            # scale would silently offset every mapped point (the safety gate would
            # check the same wrong coordinates, so it reads as "unsolvable").
            metrics = await metrics_fn()
            width, height = viewport_size(metrics)
            if width <= 0 or height <= 0:
                # A fresh tab reports innerWidth 0 until layout is forced (panel
                # quirk). Without a viewport every normalized point maps to 0,0, so
                # refuse to act rather than click the top-left corner.
                _log(logger, f"{log_prefix}.vl_viewport_unavailable",
                     {"pageId": page_id, "attempt": attempt, "metrics": metrics})
                return _finish(
                    VIEWPORT_UNAVAILABLE,
                    "CSS viewport is 0 or unreadable; normalized VL coordinates"
                    " cannot be mapped safely",
                )
            receipt["viewport"] = {"width": width, "height": height}

            # Browser leg: bounded by the pre-round budget check above, never
            # cancelled mid-flight (see _within_budget).
            shot = await screenshot_fn()
            if not shot:
                _log(logger, f"{log_prefix}.vl_no_screenshot", {"pageId": page_id})
                attempts.append({"attempt": attempt, "outcome": NO_SCREENSHOT})
                return _finish(NO_SCREENSHOT, "screenshot did not produce a saved path")

            verdict_payload = await _within_budget(captcha_solve_fn(vl_config, shot))
            if not isinstance(verdict_payload, dict):
                verdict_payload = {}
            receipt["vlCalls"] = int(receipt.get("vlCalls") or 0) + 1
            if str(verdict_payload.get("status") or "") == "failed":
                # A provider/parse failure is not a semantic CAPTCHA verdict and
                # must not spend another solve-plan attempt.
                record = {
                    "attempt": attempt,
                    "outcome": VL_UNAVAILABLE,
                    "errorType": str(
                        verdict_payload.get("errorType") or "model_invalid_response"
                    ),
                    "error": str(verdict_payload.get("error") or "")[:300],
                    "elapsedMs": verdict_payload.get("elapsedMs"),
                    "timeoutSeconds": verdict_payload.get("timeoutSeconds"),
                    "screenshotPath": shot,
                }
                attempts.append(record)
                receipt.update({
                    "errorType": record["errorType"],
                    "providerElapsedMs": record["elapsedMs"],
                    "timeoutSeconds": record["timeoutSeconds"],
                })
                return _finish(
                    VL_UNAVAILABLE,
                    record["error"] or "VL provider did not return a usable verdict",
                )
            verdict = str(verdict_payload.get("verdict") or "uncertain")
            category = str(verdict_payload.get("challenge_category") or "unknown")
            challenge_type = str(verdict_payload.get("challenge_type") or "unknown")
            plan = verdict_payload.get("solve_plan") or []
            try:
                confidence = float(verdict_payload.get("confidence"))
            except (TypeError, ValueError):
                confidence = 0.0  # missing / malformed confidence is no confidence
            floor = confidence_floor(challenge_type, min_confidence, min_confidence_ocr)
            record: JsonDict = {
                "attempt": attempt,
                "verdict": verdict,
                "challengeCategory": category,
                "challengeType": challenge_type,
                "confidence": confidence,
                "confidenceFloor": floor,
                "planSteps": len(plan) if isinstance(plan, list) else 0,
                "screenshotPath": shot,
            }
            attempts.append(record)
            receipt["challengeCategory"] = category
            receipt["challengeType"] = challenge_type
            receipt["confidence"] = confidence
            _log(logger, f"{log_prefix}.vl_verdict", {
                "pageId": page_id, "attempt": attempt, "verdict": verdict,
                "challenge_category": category, "challenge_type": challenge_type,
                "confidence": confidence, "confidence_floor": floor,
                "steps": record["planSteps"],
            })

            if verdict == "not_a_challenge":
                # A VL opinion never clears a page by itself — an unconfident one
                # least of all. Both gates below can only hand off, never act.
                if confidence < floor:
                    record["outcome"] = LOW_CONFIDENCE
                    return _finish(
                        LOW_CONFIDENCE,
                        f"VL claimed no challenge with confidence {confidence:.2f}"
                        f" < {floor:.2f}",
                    )
                cleared, early = await _verify()
                if early is not None:
                    record["outcome"] = VERIFICATION_UNAVAILABLE
                    return early
                if not cleared:
                    record["outcome"] = CHALLENGE_PERSISTS
                    return _finish(
                        CHALLENGE_PERSISTS,
                        "VL reported no challenge, but the independent check still"
                        " sees one",
                    )
                record["outcome"] = NOT_A_CHALLENGE
                return _finish(
                    NOT_A_CHALLENGE,
                    str(verdict_payload.get("reason") or "VL sees no challenge on this page"),
                )
            if verdict != "solvable" or not isinstance(plan, list) or not plan:
                # behavioral_risk / unknown / uncertain / empty plan — the honest
                # short-circuit in _finalize_captcha_solve lands here.
                #
                # Re-classifying is free of page side effects: it is a fresh
                # screenshot and one more model call, and _finalize_captcha_solve
                # has already emptied any plan, so nothing can be driven. A
                # single unlucky read (a half-mounted challenge iframe, a widget
                # the model did not recognise) therefore must not burn the whole
                # budget and send the run to a human. Retry while rounds remain,
                # but stop as soon as two consecutive reads agree — a stable
                # classification is the model's real answer, not a hiccup.
                unsolvable_reason = str(
                    verdict_payload.get("short_circuit_reason")
                    or verdict_payload.get("reason")
                    or f"VL verdict={verdict}, category={category}"
                )
                signature = (verdict, category, challenge_type)
                repeated = signature == last_unsolvable_signature
                last_unsolvable_signature = signature
                record["outcome"] = UNSOLVABLE
                if repeated or attempt >= max(1, int(max_attempts or 1)):
                    return _finish(UNSOLVABLE, unsolvable_reason)
                record["retriedClassification"] = True
                _log(logger, f"{log_prefix}.vl_reclassify", {
                    "pageId": page_id,
                    "attempt": attempt,
                    "verdict": verdict,
                    "challenge_category": category,
                    "challenge_type": challenge_type,
                    "reason": unsolvable_reason[:300],
                })
                continue
            # Any round that produced a usable classification breaks the run of
            # unsolvable reads: "two consecutive" must mean consecutive, or an
            # attempt that drove the page and merely failed to clear would be
            # invisible and end the loop a round early.
            last_unsolvable_signature = None
            if challenge_type not in SOLVABLE_CHALLENGE_TYPES:
                record["outcome"] = UNSOLVABLE
                return _finish(
                    UNSOLVABLE,
                    f"challenge_type={challenge_type} is not on the auto-solvable"
                    f" allow-list {sorted(SOLVABLE_CHALLENGE_TYPES)}",
                )
            if not plan_matches_type(challenge_type, plan):
                record["outcome"] = UNSOLVABLE
                return _finish(
                    UNSOLVABLE,
                    f"solve plan actions do not match challenge_type={challenge_type};"
                    " the classification contradicts itself",
                )
            if confidence < floor:
                record["outcome"] = LOW_CONFIDENCE
                return _finish(
                    LOW_CONFIDENCE,
                    f"VL confidence {confidence:.2f} < {floor:.2f} for"
                    f" challenge_type={challenge_type}",
                )

            calls = solve_plan_to_input_calls(plan, page_id, metrics)
            if not calls:
                record["outcome"] = UNSOLVABLE
                return _finish(UNSOLVABLE, "solve plan contained no executable step")
            if not _budget_left():
                # Classification ate the budget: hand off rather than start
                # driving the page with no time left to verify the result.
                record["outcome"] = BUDGET_EXCEEDED
                return _finish(
                    BUDGET_EXCEEDED,
                    "wall-clock solve budget was spent before any action started",
                )

            aborted_point: Optional[Tuple[float, float]] = None
            for method, params in calls:
                # Only coordinate-bearing calls go through the live safety gate.
                # A coordinate-less Input.type (text_ocr: focus-then-type) follows
                # an already-checked Input.click; gating it on absent x/y would
                # abort every OCR solve.
                x, y = params.get("x"), params.get("y")
                if x is not None and y is not None and not await safety_fn(x, y):
                    aborted_point = (x, y)
                    break
                await exec_fn(method, params)

            if aborted_point is not None:
                record["outcome"] = UNSAFE_ABORT
                record["unsafePoint"] = [aborted_point[0], aborted_point[1]]
                _log(logger, f"{log_prefix}.vl_unsafe_point",
                     {"pageId": page_id, "x": aborted_point[0], "y": aborted_point[1]})
                return _finish(
                    UNSAFE_ABORT,
                    "a consequential control sat under a VL-proposed point;"
                    " the solve was abandoned without clicking it",
                )

            record["executedCalls"] = [method for method, _ in calls]
            cleared, early = await _verify()
            if early is not None:
                record["outcome"] = VERIFICATION_UNAVAILABLE
                return early
            record["verified"] = bool(cleared)
            if cleared:
                _log(logger, f"{log_prefix}.vl_solved",
                     {"pageId": page_id, "attempt": attempt})
                return _finish(SOLVED, "")
            record["outcome"] = "not_cleared"
            # Not cleared → retry (slider micro-adjust / next grid pass).

        _log(logger, f"{log_prefix}.vl_exhausted",
             {"pageId": page_id, "attempts": len(attempts)})
        return _finish(EXHAUSTED, "the challenge was still present after every attempt")
    except asyncio.TimeoutError:
        _log(logger, f"{log_prefix}.vl_budget_exceeded", {"pageId": page_id})
        return _finish(
            BUDGET_EXCEEDED,
            "the wall-clock solve budget expired during a screenshot or VL call",
        )
    except Exception as exc:  # the human fall-back must always stay reachable
        _log(logger, f"{log_prefix}.vl_solve_error",
             {"pageId": page_id, "errorType": type(exc).__name__, "error": str(exc)[:300]})
        receipt["errorType"] = type(exc).__name__
        return _finish(ERROR, str(exc)[:300])
