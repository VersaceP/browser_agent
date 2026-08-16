"""
harness.tools.browser_tools.hitl - Challenge adjudication, HITL pause/resume and post-HITL recovery.
"""

import asyncio
import copy
import sys
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from abcp_client import ABCPTransportError
from harness.observation.challenge_detector import HIGH_CONFIDENCE_CHALLENGE_KEYWORDS
from harness.observation.challenge_detector import ChallengeTracker
from harness.observation.challenge_detector import detect_structural_challenge_from_lines
from harness.observation.challenge_detector import extract_page_id
from harness.diagnostics.error_classification import attach_error_classification
from harness.fleet.runtime import FleetClickGateTimeout
from harness.observation.render_recovery import build_render_recovery_runner
from harness.tools.parsers import attach_method_schema
from harness.utils import JsonDict
from harness.utils import exception_payload
from .axtree_state import _axtree_lines_from_value

def _bt():
    import harness.tools.browser_tools as bt

    return bt

async def _maybe_auto_hitl_for_challenge(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    if method == "Hitl.requestPause":
        return result
    if getattr(agent, "challenge_adjudicating", False):
        return result
    if _result_has_paused_error(result):
        enriched = dict(result)
        enriched["pausedState"] = {
            "type": "hitl_paused_state",
            "pageId": extract_page_id(params, result),
            "triggerMethod": method,
        }
        enriched["next_instruction"] = (
            "This is an existing HITL paused-state error, not a newly detected"
            " page challenge. Do not call Hitl.requestPause again for this"
            " page. Wait for an explicit HITL resume event, or let LeadAgent"
            " restart with a fresh page if the pause is stale."
        )
        return enriched
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is None:
        tracker = ChallengeTracker()
        agent.challenge_tracker = tracker
    tracker.cleanup_stale(step)
    page_id = extract_page_id(params, result)
    if not page_id:
        return result
    state = tracker.feed(method=method, params=params, result=result, step=step)
    if state is None:
        return result
    cooldown_until = float(getattr(agent, "hitl_no_repause_until", 0.0) or 0.0)
    if cooldown_until > time.monotonic():
        enriched = dict(result)
        enriched["suspected_challenge"] = {
            **state.to_summary(),
            "adjudication": "cooldown",
            "cooldownMs": int((cooldown_until - time.monotonic()) * 1000),
        }
        enriched["next_instruction"] = (
            "Recent HITL resume is still settling. Re-check Page.getState/DOM.getAXTree"
            " and verify the final URL before requesting another pause."
        )
        return enriched
    guard_ms = _post_hitl_repause_guard_ms(agent, page_id)
    if guard_ms > 0 and tracker.should_adjudicate(page_id, step):
        enriched = dict(result)
        enriched["suspected_challenge"] = {
            **state.to_summary(),
            "adjudication": "post_hitl_recheck",
            "guardMs": guard_ms,
        }
        enriched["next_instruction"] = (
            "This page resumed from HITL recently. Do not request another"
            " automatic pause for the same page yet; first re-check Page.getState,"
            " refresh DOM.getAXTree, and verify the active page contains target"
            " content. If it is still blocked, report the blocker to LeadAgent."
        )
        return enriched
    if not tracker.should_adjudicate(page_id, step):
        enriched = dict(result)
        enriched["suspected_challenge"] = {
            **state.to_summary(),
            "adjudication": "not_ready",
        }
        return enriched
    return await _adjudicate_and_maybe_hitl(agent, page_id, method, result, step)

def _result_has_paused_error(value: Any, *, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in {
                "error",
                "message",
                "reason",
                "status",
                "observation",
                "suggested_prompt",
            } and _result_has_paused_error(item, depth=depth + 1):
                return True
            if isinstance(item, (dict, list)) and _result_has_paused_error(item, depth=depth + 1):
                return True
        return False
    if isinstance(value, list):
        return any(_result_has_paused_error(item, depth=depth + 1) for item in value)
    text = str(value or "").lower()
    return "err_page_paused" in text or "paused for human intervention" in text

def _post_hitl_repause_guard_ms(agent: Any, page_id: str) -> int:
    guards = getattr(agent, "hitl_post_resume_guards", None)
    if not isinstance(guards, dict):
        return 0
    now = time.monotonic()
    until = float(guards.get(str(page_id)) or 0.0)
    if until <= now:
        guards.pop(str(page_id), None)
        return 0
    return int((until - now) * 1000)

def _record_post_hitl_repause_guard(agent: Any, page_id: str, seconds: float) -> None:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds <= 0:
        return
    guards = getattr(agent, "hitl_post_resume_guards", None)
    if not isinstance(guards, dict):
        guards = {}
        agent.hitl_post_resume_guards = guards
    guards[str(page_id)] = time.monotonic() + seconds

async def _post_hitl_recovery_loop(
    agent: Any,
    page_id: str,
    wait_result: JsonDict,
    step: int,
) -> JsonDict:
    vl_config = getattr(agent.runtime.harness, "vl", None)
    vl_enabled = bool(vl_config is not None and getattr(vl_config, "enabled", False))
    structural_receipts = getattr(agent, "hitl_structural_challenges", None)
    structural_expected = (
        structural_receipts.get(str(page_id))
        if isinstance(structural_receipts, dict)
        else None
    )
    if not vl_enabled and not isinstance(structural_expected, dict):
        return wait_result

    max_rounds = max(
        1,
        int(
            getattr(
                agent.runtime.harness,
                "hitl_post_resume_confirm_max_rounds",
                3,
            )
            or 1
        ),
    )
    current_wait = dict(wait_result)
    rounds: List[JsonDict] = []
    for round_index in range(max_rounds):
        if current_wait.get("status") != "resumed":
            recovery = current_wait.get("postHitlRecovery")
            if not isinstance(recovery, dict):
                wait_status = str(current_wait.get("status") or "not_resumed")
                precise_statuses = {
                    "browser_error_after_hitl",
                    "still_challenge_after_hitl",
                    "timeout",
                    "page_settled_after_hitl",
                    "stale_pause_deadlock",
                    "hitl_waiting",
                }
                recovery_status = (
                    wait_status if wait_status in precise_statuses else "not_resumed"
                )
                recovery = {"status": recovery_status}
            recovery["rounds"] = rounds
            current_wait["postHitlRecovery"] = {
                **recovery,
            }
            return current_wait

        if isinstance(structural_expected, dict):
            structural_check = await _bt()._post_hitl_structural_challenge_check(
                agent, page_id, step, round_index + 1
            )
            if structural_check.get("status") == "challenge_present":
                round_record = {
                    "round": round_index + 1,
                    "structural": structural_check,
                }
                rounds.append(round_record)
                if round_index >= max_rounds - 1:
                    return {
                        **current_wait,
                        "status": "still_challenge_after_hitl",
                        "postHitlRecovery": {
                            "status": "max_rounds_reached",
                            "verificationMode": "structural_axtree",
                            "rounds": rounds,
                        },
                    }
                next_wait = await _bt()._repause_for_structural_challenge(
                    agent,
                    page_id,
                    step,
                    round_index + 1,
                    structural_check,
                )
                round_record["retryWait"] = {
                    key: value
                    for key, value in next_wait.items()
                    if key in {"status", "via", "elapsedMs", "reason", "error"}
                }
                current_wait = next_wait
                continue
            if structural_check.get("status") == "check_failed":
                rounds.append({
                    "round": round_index + 1,
                    "structural": structural_check,
                })
                if not vl_enabled:
                    return {
                        **current_wait,
                        "status": "browser_error_after_hitl",
                        "postHitlRecovery": {
                            "status": "structural_check_failed",
                            "rounds": rounds,
                        },
                    }
            elif not vl_enabled:
                current_wait["postHitlRecovery"] = {
                    "status": "recovered_by_structural_axtree",
                    "rounds": rounds + [{
                        "round": round_index + 1,
                        "structural": structural_check,
                    }],
                }
                return current_wait

        if not vl_enabled:
            return current_wait

        vl_result = await _bt()._post_hitl_recovery_vl_check(
            agent,
            page_id,
            step,
            round_index + 1,
        )
        round_record: JsonDict = {
            "round": round_index + 1,
            "vl": _compact_vl_for_wait(vl_result),
        }
        rounds.append(round_record)
        verdict = str(vl_result.get("verdict") or "uncertain")
        recovery = str(vl_result.get("recommended_recovery") or "")
        if verdict == "normal_loading" or recovery == "continue":
            current_wait["postHitlRecovery"] = {
                "status": "recovered_by_vl",
                "rounds": rounds,
            }
            return current_wait
        if verdict != "confirmed_challenge":
            current_wait["postHitlRecovery"] = {
                "status": "uncertain_vl",
                "rounds": rounds,
            }
            return current_wait

        decision = await _prompt_post_hitl_confirmation(
            agent,
            {
                "pageId": page_id,
                "round": round_index + 1,
                "maxRounds": max_rounds,
                "vl": vl_result,
            },
        )
        round_record["humanDecision"] = decision
        if decision == "yes":
            current_wait["postHitlRecovery"] = {
                "status": "human_override_recovered",
                "humanOverride": True,
                "rounds": rounds,
            }
            return current_wait
        if decision == "error":
            return {
                **current_wait,
                "status": "browser_error_after_hitl",
                "postHitlRecovery": {
                    "status": "browser_error_after_hitl",
                    "rounds": rounds,
                },
            }

        if round_index >= max_rounds - 1:
            return {
                **current_wait,
                "status": "still_challenge_after_hitl",
                "postHitlRecovery": {
                    "status": "max_rounds_reached",
                    "rounds": rounds,
                },
            }

        next_wait = await _bt()._refresh_and_wait_for_post_hitl_retry(
            agent,
            page_id,
            step,
            round_index + 1,
        )
        round_record["retryWait"] = {
            key: value
            for key, value in next_wait.items()
            if key in {"status", "via", "elapsedMs", "reason", "error"}
        }
        current_wait = next_wait

    return current_wait

async def _post_hitl_structural_challenge_check(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
) -> JsonDict:
    tree = await _bt()._post_hitl_raw_browser_call(
        agent,
        "DOM.getAXTree",
        {
            "pageId": page_id,
            "purpose": (
                "Verify that the embedded CAPTCHA/verification frame is gone"
                f" after HITL round {round_index}."
            ),
        },
        step,
        capture_axtree_text=True,
    )
    raw_text = str(tree.pop("_authAXTreeText", "") or "")
    if _bt()._invoke_result_failed(tree) or not raw_text:
        return {
            "status": "check_failed",
            "round": round_index,
            "reason": "fresh_axtree_unavailable",
        }
    evidence = detect_structural_challenge_from_lines(
        raw_text.splitlines(), source_method="DOM.getAXTree"
    )
    if evidence:
        return {
            "status": "challenge_present",
            "round": round_index,
            "evidence": evidence,
        }
    return {
        "status": "challenge_cleared",
        "round": round_index,
        "freshAXTree": True,
    }

async def _repause_for_structural_challenge(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
    structural_check: JsonDict,
) -> JsonDict:
    state_call = await _bt()._post_hitl_raw_browser_call(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Preserve current detail state before repeating HITL for a remaining embedded challenge.",
        },
        step,
    )
    state_data = _bt()._response_data(state_call)
    pause_call = await _bt()._post_hitl_raw_browser_call(
        agent,
        "Hitl.requestPause",
        {
            "pageId": page_id,
            "purpose": (
                "The embedded verification frame and control remain after"
                " HITL; pause the same detail page again without navigation."
            ),
            "reason": "请继续完成当前详情页中仍存在的验证码/滑块验证。",
        },
        step,
    )
    response = pause_call.get("response") if isinstance(pause_call, dict) else None
    if not _hitl_pause_succeeded(response):
        return {
            "status": "browser_error_after_hitl",
            "error": "Hitl.requestPause failed for remaining structural challenge",
            "structural": structural_check,
        }
    harness_cfg = agent.runtime.harness
    return await _bt().wait_for_hitl_resume(
        browser=agent.browser,
        page_id=str(page_id),
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 900.0),
        poll_interval_seconds=getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0),
        diagnostics=getattr(agent, "diagnostics", None),
        logger=agent.logger,
        challenge_verifier=_make_hitl_challenge_verifier(agent, str(page_id), step),
        pause_snapshot={
            "url": str(state_data.get("url") or ""),
            "title": str(state_data.get("title") or ""),
            "round": round_index,
        },
    )

async def _post_hitl_recovery_vl_check(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
) -> JsonDict:
    agent.challenge_adjudicating = True
    try:
        return await _bt()._visual_verify(
            agent,
            {
                "pageId": page_id,
                "selector": "",
                "id": "",
                "fullPage": False,
                "mode": "challenge_detection",
                "_force": True,
                "question": (
                    "After the user handled HITL, has this browser page"
                    " recovered from CAPTCHA/security verification and returned"
                    " to normal website content, or is it still blocked by a"
                    " challenge?"
                ),
                "expected": {
                    "pageId": page_id,
                    "postHitlRecoveryRound": round_index,
                },
            },
            step,
        )
    finally:
        agent.challenge_adjudicating = False

def _compact_vl_for_wait(vl_result: JsonDict) -> JsonDict:
    return {
        key: value
        for key, value in vl_result.items()
        if key in {
            "status",
            "verdict",
            "confidence",
            "visible_evidence",
            "recommended_recovery",
            "reason",
            "screenshotPath",
            "mode",
        }
    }

async def _prompt_post_hitl_confirmation(agent: Any, payload: JsonDict) -> str:
    handler = getattr(agent, "post_hitl_confirmation_handler", None)
    if callable(handler):
        value = handler(payload)
        if hasattr(value, "__await__"):
            value = await value
        return _normalize_post_hitl_confirmation(value)

    vl = payload.get("vl") if isinstance(payload.get("vl"), dict) else {}
    lines = [
        "",
        "[ABCP HITL] VL still sees a challenge after user intervention.",
        f"  pageId: {payload.get('pageId')}",
        f"  round: {payload.get('round')}/{payload.get('maxRounds')}",
        f"  verdict: {vl.get('verdict')} confidence={vl.get('confidence')}",
        f"  reason: {vl.get('reason')}",
        f"  screenshot: {vl.get('screenshotPath')}",
        "  Choose: yes = browser page is actually recovered; no = refresh and keep handling HITL; error = browser/pageId is wrong or broken.",
    ]
    if not sys.stdin or not sys.stdin.isatty():
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write(
                "hitl.post_resume.confirmation_non_tty",
                {
                    "pageId": payload.get("pageId"),
                    "round": payload.get("round"),
                    "maxRounds": payload.get("maxRounds"),
                    "decision": "error",
                    "reason": "stdin is not interactive",
                },
            )
        return "error"
    print("\n".join(lines), flush=True)
    try:
        value = await asyncio.to_thread(input, "Post-HITL confirmation [yes/no/error]: ")
    except (EOFError, KeyboardInterrupt):
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write(
                "hitl.post_resume.confirmation_input_failed",
                {
                    "pageId": payload.get("pageId"),
                    "round": payload.get("round"),
                    "decision": "error",
                },
            )
        value = "error"
    return _normalize_post_hitl_confirmation(value)

def _normalize_post_hitl_confirmation(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"yes", "y", "true", "ok", "continue", "normal"}:
        return "yes"
    if normalized in {"no", "n", "false", "retry", "refresh"}:
        return "no"
    if normalized in {"error", "err", "browser_error", "broken", "abort", "stop"}:
        return "error"
    return "error"

async def _refresh_and_wait_for_post_hitl_retry(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
) -> JsonDict:
    state_call = await _bt()._post_hitl_raw_browser_call(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "post-HITL terminal confirmation requested refresh; read current URL before retry",
        },
        step,
    )
    current_url = str(_bt()._response_data(state_call).get("url") or "").strip()
    if not current_url:
        return {
            "status": "browser_error_after_hitl",
            "error": "Page.getState did not return a URL for post-HITL refresh",
            "state": state_call,
        }

    navigate_call = await _bt()._post_hitl_raw_browser_call(
        agent,
        "Page.navigate",
        {
            "pageId": page_id,
            "url": current_url,
            "purpose": "refresh page after human confirmed the HITL challenge is still visible",
        },
        step,
    )
    if navigate_call.get("error"):
        return {
            "status": "browser_error_after_hitl",
            "error": "Page.navigate failed during post-HITL retry",
            "navigate": navigate_call,
        }

    pause_call = await _bt()._post_hitl_raw_browser_call(
        agent,
        "Hitl.requestPause",
        {
            "pageId": page_id,
            "purpose": (
                "Post-HITL confirmation reported the page still shows a challenge;"
                " pause again so the user can continue handling it."
            ),
        },
        step,
    )
    response = pause_call.get("response") if isinstance(pause_call, dict) else None
    if not _hitl_pause_succeeded(response):
        return {
            "status": "browser_error_after_hitl",
            "error": "Hitl.requestPause failed during post-HITL retry",
            "pause": pause_call,
        }

    harness_cfg = agent.runtime.harness
    retry_snapshot = {
        "url": str(_bt()._response_data(navigate_call).get("url") or current_url or ""),
        "title": str(_bt()._response_data(navigate_call).get("title") or ""),
    }
    wait_result = await _bt().wait_for_hitl_resume(
        browser=agent.browser,
        page_id=str(page_id),
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 900.0),
        poll_interval_seconds=getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0),
        diagnostics=getattr(agent, "diagnostics", None),
        logger=agent.logger,
        challenge_verifier=_make_hitl_challenge_verifier(agent, str(page_id), step),
        pause_snapshot=retry_snapshot,
    )
    wait_result = dict(wait_result)
    wait_result["postHitlRetry"] = {
        "round": round_index,
        "refreshedUrl": current_url,
        "navigate": {
            "status": _bt()._response_data(navigate_call).get("status"),
            "url": _bt()._response_data(navigate_call).get("url"),
            "title": _bt()._response_data(navigate_call).get("title"),
        },
    }
    return wait_result

async def _post_hitl_raw_browser_call(
    agent: Any,
    method: str,
    params: JsonDict,
    step: int,
    *,
    capture_axtree_text: bool = False,
) -> JsonDict:
    private_axtree_text = ""
    try:
        _ensure_hitl_request_reason(method, params, str(params.get("purpose") or ""))
        runner = getattr(agent, "render_recovery_runner", None)
        if runner is None:
            runner = build_render_recovery_runner(
                browser=agent.browser,
                logger=agent.logger,
                capability_methods=agent.capability_methods,
                recent_recoveries=agent._render_recovery_recent,
            )
            agent.render_recovery_runner = runner
        response, _recovery = await runner.call(method, params)
        response = agent._capture_artifacts(method, response)
        record_file_action = getattr(agent, "_capture_file_action", None)
        if callable(record_file_action):
            record_file_action(method, params, response)
        if capture_axtree_text and method == "DOM.getAXTree":
            private_axtree_text = "\n".join(_axtree_lines_from_value(response))
        response = agent._offload_response(method, params, response, step)
        result = {"method": method, "params": params, "response": response}
    except FleetClickGateTimeout as exc:
        result = {
            "method": method,
            "params": params,
            "status": "fleet_click_gated",
            "error": str(exc),
            **exc.receipt,
        }
        attach_method_schema(
            result, method, getattr(agent, "method_schemas", {})
        )
    except ABCPTransportError as exc:
        result = {
            "method": method,
            "params": params,
            "status": "browser_error_after_hitl",
            "error": str(exc),
            **_bt()._transport_error_metadata(method, exc),
        }
        attach_method_schema(result, method, getattr(agent, "method_schemas", {}))
    except Exception as exc:
        result = {
            "method": method,
            "params": params,
            "status": "browser_error_after_hitl",
            **exception_payload(exc),
        }

    attach_error_classification(result, method=method)
    result = _bt()._apply_select_failure_guidance(agent, method, params, result)
    result = _bt()._attach_normalized_handles(result)
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        trim_for_log = getattr(agent, "_trim_for_log", lambda value: value)
        logger.write("hitl.post_resume.raw_call", trim_for_log(result))
    if private_axtree_text:
        # Ephemeral proof input for AuthFleetLedger. Attach only after logging;
        # callers must not persist or expose the raw accessibility text.
        result["_authAXTreeText"] = private_axtree_text
    return result

def _clear_challenge_state_after_recovery(agent: Any, page_id: str, *, event: str) -> None:
    """Shared bookkeeping for "this page is no longer challenged".

    Used by the HITL resume path and by a successful VL auto-solve: drop the
    accumulated suspicion, forget the structural receipt, and hold a short
    re-pause guard so residual challenge wording in the next tool result cannot
    trigger a second pause before the worker has re-perceived the page.
    """
    harness_cfg = getattr(getattr(agent, "runtime", None), "harness", None)
    cooldown_seconds = float(
        getattr(harness_cfg, "hitl_no_repause_cooldown_seconds", 8.0) or 0.0
    )
    agent.hitl_no_repause_until = time.monotonic() + max(0.0, cooldown_seconds)
    guard_seconds = float(
        getattr(harness_cfg, "hitl_post_resume_guard_seconds", 30.0) or 0.0
    )
    _record_post_hitl_repause_guard(
        agent, str(page_id), max(cooldown_seconds, guard_seconds)
    )
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is not None:
        tracker.clear_page(str(page_id))
    structural_receipts = getattr(agent, "hitl_structural_challenges", None)
    if isinstance(structural_receipts, dict):
        structural_receipts.pop(str(page_id), None)
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write(event, {"pageId": str(page_id)})

async def _maybe_autosolve_before_hitl(
    agent: Any,
    page_id: str,
    step: int,
    *,
    trigger: str,
    vl_only_detection: bool,
    reason: str,
) -> JsonDict:
    """Bounded VL attempt to clear a detected challenge BEFORE asking a human.

    Returns {} when the role is switched off, so a disabled deployment keeps the
    exact pre-existing straight-to-HITL behavior. Never raises: the human path
    must stay reachable no matter how the solve fails.
    """
    from harness.tools.browser_tools.captcha_autosolve import (
        autosolve_enabled,
        maybe_autosolve_captcha,
    )

    if not autosolve_enabled(agent):
        return {}
    try:
        return await maybe_autosolve_captcha(
            agent,
            str(page_id),
            step,
            trigger=trigger,
            vl_only_detection=vl_only_detection,
            reason=reason,
        )
    except Exception as exc:
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.captcha_autosolve.failed", {
                "pageId": str(page_id),
                "trigger": trigger,
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            })
        return {
            "status": "error",
            "attempted": False,
            "trigger": trigger,
            "errorType": type(exc).__name__,
            "reason": str(exc)[:300],
        }

def _autosolve_cleared(receipt: Any) -> bool:
    from harness.tools.browser_tools.captcha_autosolve import CLEARED_STATUSES

    return bool(
        isinstance(receipt, dict)
        and receipt.get("attempted")
        and str(receipt.get("status") or "") in CLEARED_STATUSES
    )

def _autosolve_cleared_result(
    agent: Any,
    enriched: JsonDict,
    page_id: str,
    step: int,
    solve: JsonDict,
    *,
    pause_skipped: bool = False,
) -> JsonDict:
    """Build the "solved without a human" result and reset the page's challenge
    bookkeeping so the next observation starts from a clean slate."""
    out = dict(enriched)
    suspected = dict(out.get("suspected_challenge") or {})
    suspected["adjudication"] = "auto_solved_by_vl"
    out["suspected_challenge"] = suspected
    out["captchaAutoSolve"] = solve
    _clear_challenge_state_after_recovery(
        agent, page_id, event="challenge.autosolve_cleared"
    )
    out["next_instruction"] = (
        (
            "Your Hitl.requestPause was NOT executed: the harness cleared this"
            " challenge automatically with a bounded VL solve first, so no human"
            " was interrupted and no pause is pending."
            if pause_skipped
            else "The harness cleared this challenge automatically with a bounded"
            " VL solve; no human pause was requested and no Hitl.* call is pending."
        )
        + " Treat the page as unverified until you re-perceive it: refresh"
        " Page.getState and DOM.getAXTree, confirm the target content is"
        " actually present, then continue the original action. If the challenge"
        " is still there, report it — this page will not be auto-solved again."
    )
    return out

def _reason_with_autosolve(reason: str, solve: Any) -> str:
    """Tell the human why automation gave up, in the pause reason they read."""
    from harness.tools.browser_tools.captcha_autosolve import solve_summary

    summary = solve_summary(solve)
    return f"{reason} ({summary})" if summary else reason

def _model_pause_challenge_evidence(agent: Any, params: JsonDict) -> Optional[JsonDict]:
    """Decide whether a model-issued pause is CAPTCHA-shaped enough to try solving.

    Login walls, SMS/QR/2FA and payment confirmations are human-only by nature —
    spending a screenshot plus a VL round-trip on them would only make the person
    wait longer. The evidence is reused, never re-invented: the page's own
    accumulated challenge state, or the model's stated reason matching the shared
    high-confidence challenge vocabulary. Returns None when it is not worth trying.
    """
    page_id = str(params.get("pageId") or "").strip()
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if (tracker is not None and page_id) else None
    if state is not None and state.structural_challenge:
        return {"source": "structural_challenge", "vlOnly": False}
    if state is not None and state.high_confidence_hit:
        return {"source": "high_confidence_signal", "vlOnly": True}
    haystack = " ".join(
        str(params.get(key) or "") for key in ("reason", "purpose")
    ).lower()
    if any(keyword in haystack for keyword in HIGH_CONFIDENCE_CHALLENGE_KEYWORDS):
        return {"source": "model_pause_reason", "vlOnly": True}
    return None

async def _maybe_autosolve_before_model_pause(
    agent: Any,
    method: str,
    params: JsonDict,
    step: int,
) -> Optional[JsonDict]:
    """Intercept a model-issued Hitl.requestPause for a visual challenge.

    Returns a short-circuit result when the challenge was solved (the pause is
    never issued), else None so the pause proceeds exactly as before — with the
    solve attempt appended to the reason the human reads.
    """
    if method != "Hitl.requestPause" or not isinstance(params, dict):
        return None
    from harness.tools.browser_tools.captcha_autosolve import autosolve_enabled

    if not autosolve_enabled(agent):
        return None
    page_id = str(params.get("pageId") or "").strip()
    if not page_id:
        return None
    evidence = _model_pause_challenge_evidence(agent, params)
    if evidence is None:
        return None
    reason = str(params.get("reason") or params.get("purpose") or "")
    solve = await _maybe_autosolve_before_hitl(
        agent,
        page_id,
        step,
        trigger="model_request_pause",
        vl_only_detection=bool(evidence.get("vlOnly", True)),
        reason=reason,
    )
    if isinstance(solve, dict):
        solve = {**solve, "detectionEvidence": evidence}
    if not _autosolve_cleared(solve):
        # The pause the model asked for still happens; the human just gets to
        # see that automation already tried and how it failed.
        enriched_reason = _reason_with_autosolve(reason, solve)
        if enriched_reason != reason:
            params["reason"] = enriched_reason
        return None
    return _autosolve_cleared_result(
        agent,
        {
            "method": method,
            "status": "captcha_auto_solved",
            "tool_was_executed": False,
            "pageId": page_id,
        },
        page_id,
        step,
        solve,
        pause_skipped=True,
    )

async def _adjudicate_and_maybe_hitl(
    agent: Any,
    page_id: str,
    trigger_method: str,
    result: JsonDict,
    step: int,
) -> JsonDict:
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if tracker is not None else None
    summary = state.to_summary() if state is not None else {"pageId": page_id}
    vl_config = getattr(agent.runtime.harness, "vl", None)
    vl_enabled = bool(vl_config is not None and getattr(vl_config, "enabled", False))
    enriched = copy.deepcopy(result)
    enriched["suspected_challenge"] = {
        **summary,
        "adjudication": "pending",
        "triggerMethod": trigger_method,
    }

    # A challenge-labelled embedded root plus an actionable verification
    # control is stronger than a whole-page visual verdict.  Small iframes can
    # be visually inconspicuous while still blocking one business subrequest;
    # do not let VL "normal_loading" suppress deterministic AX evidence.
    if state is not None and state.structural_challenge:
        evidence = state.structural_evidence or {}
        controls = (
            evidence.get("controls")
            if isinstance(evidence.get("controls"), list)
            else []
        )
        control_labels = [
            str(control.get("label") or control.get("role") or "").strip()
            for control in controls
            if isinstance(control, dict)
        ]
        control_summary = ", ".join(label for label in control_labels if label)[:160]
        enriched["suspected_challenge"]["adjudication"] = "structural_confirmed"
        challenge_reason = (
            "Embedded verification frame detected: "
            f"{evidence.get('rootLabel') or 'challenge'}"
            + (f"; control: {control_summary}" if control_summary else "")
        )
        solve = await _maybe_autosolve_before_hitl(
            agent,
            page_id,
            step,
            trigger="structural_challenge",
            vl_only_detection=False,
            reason=challenge_reason,
        )
        if _autosolve_cleared(solve):
            return _autosolve_cleared_result(agent, enriched, page_id, step, solve)
        if solve:
            enriched["captchaAutoSolve"] = solve
        enriched["autoHitl"] = await _bt()._request_hitl_for_challenge(
            agent,
            page_id,
            trigger_method,
            step,
            reason=_reason_with_autosolve(challenge_reason, solve),
            trigger_result=result,
        )
        enriched["next_instruction"] = (
            "A cross-frame AXTree challenge and an actionable verification"
            " control were detected. The harness requested HITL without"
            " allowing a whole-page VL verdict to override that evidence."
            " After resume, follow autoHitl.resumeCheckpoint and revalidate"
            " the original business content."
        )
        return enriched

    if vl_enabled:
        agent.challenge_adjudicating = True
        try:
            vl_result = await _bt()._visual_verify(
                agent,
                {
                    "pageId": page_id,
                    "selector": "",
                    "id": "",
                    "fullPage": False,
                    "mode": "challenge_detection",
                    "question": (
                        "Is this page blocked by CAPTCHA, Cloudflare/security"
                        " verification, or another challenge requiring human"
                        " action?"
                    ),
                    "expected": {
                        "pageId": page_id,
                        "triggerMethod": trigger_method,
                        "suspectedChallenge": summary,
                    },
                },
                step,
            )
        finally:
            agent.challenge_adjudicating = False
        verdict = str(vl_result.get("verdict") or "uncertain")
        if tracker is not None:
            tracker.record_vl_verdict(page_id, step, verdict)
        enriched["challengeAdjudication"] = vl_result
        if verdict == "confirmed_challenge":
            challenge_reason = str(vl_result.get("reason") or "VL confirmed challenge")
            solve = await _maybe_autosolve_before_hitl(
                agent,
                page_id,
                step,
                trigger="vl_confirmed_challenge",
                vl_only_detection=True,
                reason=challenge_reason,
            )
            if _autosolve_cleared(solve):
                return _autosolve_cleared_result(agent, enriched, page_id, step, solve)
            if solve:
                enriched["captchaAutoSolve"] = solve
            enriched["autoHitl"] = await _bt()._request_hitl_for_challenge(
                agent,
                page_id,
                trigger_method,
                step,
                reason=_reason_with_autosolve(challenge_reason, solve),
                trigger_result=result,
            )
            enriched["next_instruction"] = (
                "The visual adjudicator confirmed a challenge and the harness"
                " requested human intervention. Inspect autoHitl.hitl_wait."
            )
        elif verdict == "normal_loading":
            enriched["next_instruction"] = (
                "Visual adjudicator classified the page as normal loading. Continue"
                " without requesting HITL during the cooldown window."
            )
        elif verdict == "unrelated_block":
            enriched["next_instruction"] = (
                "Visual adjudicator found an unrelated block. Record the blocker or"
                " let LeadAgent pivot strategy."
            )
        else:
            enriched["next_instruction"] = (
                "Challenge suspicion remains uncertain after visual adjudication."
                " Observe once more or hand the blocker to LeadAgent."
            )
        return enriched

    if state is not None and state.high_confidence_hit:
        enriched["autoHitl"] = await _bt()._request_hitl_for_challenge(
            agent,
            page_id,
            trigger_method,
            step,
            reason="High-confidence CAPTCHA/challenge keyword with VL disabled",
            trigger_result=result,
        )
        enriched["next_instruction"] = (
            "VL is disabled, but a high-confidence challenge keyword was found."
            " The harness requested human intervention."
        )
        return enriched

    enriched["suspected_challenge"]["adjudication"] = "vl_unavailable"
    enriched["suspected_challenge"]["vl_unavailable_reason"] = (
        "vl.enabled=false in config"
    )
    enriched["next_instruction"] = (
        "This page is suspected to be blocked by a challenge, but visual"
        " adjudication is unavailable and no high-confidence CAPTCHA keyword"
        " was found. Do not poll indefinitely; report the blocker or let"
        " LeadAgent decide."
    )
    return enriched

def _hitl_pause_rounds(agent: Any) -> Dict[str, int]:
    """Pause rounds spent, keyed by pageId; the "" key is the worker total."""
    rounds = getattr(agent, "hitl_pause_rounds", None)
    if not isinstance(rounds, dict):
        rounds = {}
        agent.hitl_pause_rounds = rounds
    return rounds

def _count_hitl_pause_round(agent: Any, page_id: str) -> Dict[str, int]:
    """Charge one pause round. Called from exactly one place.

    `_claim_fleet_auth_barrier_for_hitl` is the single choke point every
    dispatched Hitl.requestPause crosses, so counting there — and only there —
    keeps the auto path (which claims the barrier itself first, then dispatches
    through the same guard) from being charged twice for one pause.
    """
    rounds = _hitl_pause_rounds(agent)
    key = str(page_id or "")
    if key:
        rounds[key] = int(rounds.get(key, 0)) + 1
    rounds[""] = int(rounds.get("", 0)) + 1
    return rounds

async def _refuse_hitl(
    agent: Any,
    admission: JsonDict,
    page_id: str,
    trigger_method: str,
) -> JsonDict:
    """Hand back a refusal, releasing the lease without opening the gate."""
    released = await _release_fleet_auth_after_hitl_refusal(
        agent, f"HITL refused: {admission['reasonKind']}"
    )
    if released:
        admission = {**admission, "fleetAuthBarrier": released}
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("hitl.refused", {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "reasonKind": admission["reasonKind"],
            "budgetScope": admission.get("budgetScope"),
            "pauseRoundsUsed": admission.get("pauseRoundsUsed"),
        })
    return admission

def _hitl_admission(agent: Any, page_id: str) -> Optional[JsonDict]:
    """Decide whether asking a human is still worth doing.

    Returns None to proceed, or a terminal receipt to hand back instead. Both
    refusals are returned BEFORE the fleet auth barrier is claimed: a pause
    nobody will answer must not also shut the gate on every sibling worker.
    """
    harness_cfg = getattr(getattr(agent, "runtime", None), "harness", None)
    attendance = str(
        getattr(harness_cfg, "hitl_attendance", "attended") or "attended"
    ).strip().lower()
    if attendance == "unattended":
        return {
            "status": "hitl_unattended",
            "reasonKind": "hitl_unattended",
            "tool_was_executed": False,
            "retryable": False,
            "pageId": page_id,
            "next_instruction": (
                "This deployment is configured as unattended (hitl_attendance)."
                " No human will resolve this challenge. Do not pause, retry, or"
                " navigate around it: report it as a blocker and finish."
            ),
        }
    rounds = _hitl_pause_rounds(agent)
    per_page = max(0, int(getattr(harness_cfg, "hitl_max_pause_rounds_per_page", 3) or 0))
    per_worker = max(0, int(getattr(harness_cfg, "hitl_max_pause_rounds_per_worker", 3) or 0))
    page_used = int(rounds.get(str(page_id), 0))
    worker_used = int(rounds.get("", 0))
    if per_page and page_used >= per_page:
        scope, used, budget = "page", page_used, per_page
    elif per_worker and worker_used >= per_worker:
        scope, used, budget = "worker", worker_used, per_worker
    else:
        return None
    return {
        "status": "hitl_budget_exhausted",
        "reasonKind": "hitl_budget_exhausted",
        "tool_was_executed": False,
        "retryable": False,
        "pageId": page_id,
        "budgetScope": scope,
        "pauseRoundsUsed": used,
        "pauseRoundsBudget": budget,
        "next_instruction": (
            f"The cumulative HITL budget for this {scope} is spent"
            f" ({used}/{budget} pauses). A human did not resolve the challenge"
            " in the earlier rounds and re-pausing holds the fleet gate shut"
            " for every sibling worker. Report this as a blocker and finish."
        ),
    }

async def _release_fleet_auth_after_hitl_refusal(agent: Any, reason: str) -> JsonDict:
    """Hand the gate back when this worker will not be asking a human."""
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {}
    try:
        return await barrier.relinquish(fleet_id, worker_id, reason=reason) or {}
    except Exception:  # noqa: BLE001 - a refusal must never raise
        return {}

async def _request_hitl_for_challenge(
    agent: Any,
    page_id: str,
    trigger_method: str,
    step: int,
    *,
    reason: str,
    trigger_result: Optional[JsonDict] = None,
) -> JsonDict:
    # Checked here as well as at the dispatch guard, because this path claims
    # the barrier BEFORE dispatching: a pause nobody will answer must not shut
    # the gate on every sibling worker first and be refused afterwards.
    # Releasing the lease is not the same as opening the gate — the fleet is
    # still challenged, so waiters keep getting a terminal verdict rather than
    # a pass onto a cookie jar that is still under risk control. The round is
    # NOT charged here; the dispatch guard owns accounting.
    admission = _hitl_admission(agent, page_id)
    if admission is not None:
        return await _refuse_hitl(agent, admission, page_id, trigger_method)
    structural_evidence = (
        trigger_result.get("structuralChallenge")
        if isinstance(trigger_result, dict)
        and isinstance(trigger_result.get("structuralChallenge"), dict)
        else None
    )
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    barrier_claim: JsonDict = {}
    if barrier is not None and fleet_id and worker_id:
        barrier_claim = await barrier.claim(fleet_id, worker_id, reason)
        if not barrier_claim.get("claimed"):
            return {
                "status": "fleet_auth_gated",
                "reasonKind": "fleet_auth_gated",
                "fleetId": fleet_id,
                "resolverWorkerId": barrier_claim.get("resolverWorkerId"),
                "tool_was_executed": False,
                "retryable": True,
            }
    if isinstance(structural_evidence, dict):
        receipts = getattr(agent, "hitl_structural_challenges", None)
        if not isinstance(receipts, dict):
            receipts = {}
            agent.hitl_structural_challenges = receipts
        receipts[str(page_id)] = dict(structural_evidence)
    # Capture the pre-pause surface here so every auto-HITL path (VL-confirmed,
    # VL-disabled high-confidence, future callers) records the snapshot that the
    # verified-settlement title gate compares against.
    trigger_data = _bt()._response_data(trigger_result) if trigger_result else {}
    snapshot = {
        "url": str(trigger_data.get("url") or ""),
        "title": str(trigger_data.get("title") or ""),
    }
    if snapshot["url"] or snapshot["title"]:
        snapshots = getattr(agent, "hitl_pause_snapshots", None)
        if not isinstance(snapshots, dict):
            snapshots = {}
            agent.hitl_pause_snapshots = snapshots
        snapshots[str(page_id)] = snapshot
    rounds = _hitl_pause_rounds(agent)
    agent.logger.write(
        "hitl.auto_request_pause",
        {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "reason": reason,
            "structuralEvidence": structural_evidence,
            "pauseSnapshot": snapshot,
            "authBarrier": barrier_claim or None,
            # Charged by the dispatch guard below, so this reports the rounds
            # already spent before this one.
            "pauseRoundsBefore": int(rounds.get(str(page_id), 0)),
            "workerPauseRoundsBefore": int(rounds.get("", 0)),
        },
    )
    agent.challenge_adjudicating = True
    try:
        pause_result = await _bt()._invoke_browser_method(
            agent,
            "Hitl.requestPause",
            {
                "pageId": page_id,
                "purpose": (
                    "Anti-bot verification or CAPTCHA-like challenge was"
                    " detected; pause for user intervention."
                ),
                "reason": reason,
            },
            step,
        )
    finally:
        agent.challenge_adjudicating = False
    if isinstance(pause_result, dict):
        pause_result = dict(pause_result)
        pause_result["resumeCheckpoint"] = {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "challengeReason": reason,
            "structuralEvidence": structural_evidence,
            "requiredSequence": [
                "Page.getState",
                "DOM.getAXTree",
                "retry_original_materialization_if_needed",
                "DOM.getSemanticTree",
                "validate_requested_record_count",
            ],
            "successCondition": (
                "The challenge frame is absent and the original task-required"
                " content or requested record count is materialized."
            ),
            "doNotAccept": [
                "normal page title alone",
                "drawer shell alone",
                "loading skeleton",
                "preview rows outside the target subtree",
            ],
        }
    return pause_result

def _hitl_pause_succeeded(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("error"):
        return False
    obs = str(response.get("observation") or "").lower()
    if "paused for human intervention" in obs:
        return True
    data = response.get("data") if isinstance(response.get("data"), dict) else None
    if data is not None and data.get("paused") is True:
        return True
    return False

async def _capture_hitl_pause_snapshot(
    agent: Any,
    runner: Any,
    page_id: str,
    step: int,
) -> None:
    page_id = str(page_id or "").strip()
    if not page_id:
        return
    try:
        response, _recovery = await runner.call(
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "Capture URL/title before HITL pause for resume verification.",
            },
        )
    except Exception as exc:
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "hitl.pause_snapshot.failed",
                {
                    "pageId": page_id,
                    "step": step,
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
        return
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return
    snapshot = {
        "url": str(data.get("url") or data.get("currentUrl") or "").strip(),
        "title": str(data.get("title") or "").strip(),
    }
    if not (snapshot["url"] or snapshot["title"]):
        return
    snapshots = getattr(agent, "hitl_pause_snapshots", None)
    if not isinstance(snapshots, dict):
        snapshots = {}
        agent.hitl_pause_snapshots = snapshots
    snapshots[page_id] = snapshot
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "hitl.pause_snapshot.captured",
            {"pageId": page_id, "step": step, **snapshot},
        )

def _ensure_hitl_request_reason(method: str, params: JsonDict, reason: str = "") -> None:
    if method != "Hitl.requestPause" or not isinstance(params, dict):
        return
    if str(params.get("reason") or "").strip():
        return
    purpose = str(params.get("purpose") or "").strip()
    fallback = str(reason or "").strip()
    text = purpose or fallback
    if text:
        params["reason"] = text

def _make_hitl_challenge_verifier(agent: Any, page_id: str, step: int):
    """Build the verified-settlement adjudicator for wait_for_hitl_resume.

    Called by the HITL wait loop when lifecycle events suggest the human may
    have finished the challenge. Returns the VL verdict dict; any failure mode
    (VL disabled, screenshot blocked while paused, budget) degrades to
    verdict="unavailable" so the wait falls back to title evidence.
    """
    async def verify(evidence: JsonDict) -> JsonDict:
        agent.challenge_adjudicating = True
        try:
            vl_result = await _bt()._visual_verify(
                agent,
                {
                    "pageId": page_id,
                    "selector": "",
                    "id": "",
                    "fullPage": False,
                    "mode": "challenge_detection",
                    "question": (
                        "This page was paused for a human to handle a"
                        " CAPTCHA/Cloudflare-style challenge and has since"
                        " emitted load/title events. Is a challenge still"
                        " visible, or is this the normal target page now?"
                    ),
                    "expected": {
                        "pageId": page_id,
                        "settlementEvidence": evidence,
                    },
                    "_force": True,
                },
                step,
            )
        except Exception as exc:
            return {
                "verdict": "unavailable",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            }
        finally:
            agent.challenge_adjudicating = False
        if not isinstance(vl_result, dict):
            return {"verdict": "unavailable"}
        status = str(vl_result.get("status") or "").strip().lower()
        if status in {"disabled", "rejected", "failed", "error"}:
            return {
                "verdict": "unavailable",
                "status": status,
                "reason": str(
                    vl_result.get("reason") or vl_result.get("error") or ""
                )[:300],
            }
        return vl_result

    return verify

def _hitl_pause_snapshot(agent: Any, page_id: str) -> Optional[JsonDict]:
    snapshots = getattr(agent, "hitl_pause_snapshots", None)
    if not isinstance(snapshots, dict):
        return None
    snapshot = snapshots.get(str(page_id))
    return snapshot if isinstance(snapshot, dict) else None

async def _verify_and_open_fleet_auth_barrier(
    agent: Any,
    page_id: str,
    step: int,
) -> JsonDict:
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {"enabled": False}
    state = await _bt()._post_hitl_raw_browser_call(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Verify shared fleet state before opening the authentication barrier.",
        },
        step,
    )
    tree = await _bt()._post_hitl_raw_browser_call(
        agent,
        "DOM.getAXTree",
        {
            "pageId": page_id,
            "purpose": "Refresh page perception before opening the shared authentication barrier.",
        },
        step,
        capture_axtree_text=True,
    )
    if _bt()._invoke_result_failed(state) or _bt()._invoke_result_failed(tree):
        return {
            "enabled": True,
            "opened": False,
            "reason": "clearance_perception_failed",
        }
    state_data = _bt()._response_data(state)
    hitl = state_data.get("hitl") if isinstance(state_data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True:
        return {
            "enabled": True,
            "opened": False,
            "reason": "page_still_paused",
        }
    resolved = await barrier.resolve(fleet_id, worker_id)
    if resolved.get("resolved"):
        agent.fleet_reperception_pending = True
        agent.fleet_reperception_state_seen = True
        agent.fleet_reperception_tree_seen = True
        agent.fleet_barrier_generation = int(resolved.get("generation") or 0)
        agent.fleet_reperception_pending = False
    callback = getattr(agent, "auth_session_verified_handler", None)
    ledger_receipt: JsonDict = {}
    if resolved.get("resolved") and callable(callback):
        try:
            contract = getattr(agent, "worker_contract", None)
            verification_contract = (
                contract.get("auth_verification")
                if isinstance(contract, dict)
                else None
            )
            value = callback(
                {
                    "fleetId": fleet_id,
                    "pageId": page_id,
                    "url": state_data.get("url"),
                    "title": state_data.get("title"),
                    "sessionKey": getattr(agent, "fleet_session_key", ""),
                    "verificationContract": verification_contract,
                    # The ledger uses this only for an in-memory marker match;
                    # the raw tree is never persisted or included in receipts.
                    "axTreeText": str(
                        tree.get("_authAXTreeText")
                        or "\n".join(_axtree_lines_from_value(tree))
                    ),
                    "evidence": {
                        "pageStateObserved": True,
                        "axTreeObserved": True,
                        "hitlPaused": False,
                    },
                }
            )
            if hasattr(value, "__await__"):
                value = await value
            if isinstance(value, dict):
                ledger_receipt = value
        except Exception as exc:
            # Clearing a live fleet barrier and persisting a durable reuse
            # claim are separate operations.  A ledger conflict/write failure
            # must be visible, but must not crash the resolver or strand peers.
            ledger_receipt = {
                "recorded": False,
                "reason": "auth_ledger_handler_failed",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            }
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write("auth_fleet.ledger_handler_failed", ledger_receipt)
    return {
        "enabled": True,
        "opened": bool(resolved.get("resolved")),
        "generation": resolved.get("generation"),
        "ledger": ledger_receipt or None,
        "reason": resolved.get("reason"),
    }

def _hitl_resumed_suggested_prompt(wait_result: Any) -> str:
    recovery = (
        wait_result.get("postHitlRecovery")
        if isinstance(wait_result, dict) else None
    )
    rounds = recovery.get("rounds") if isinstance(recovery, dict) else None
    structural_cleared = any(
        isinstance(item, dict)
        and isinstance(item.get("structural"), dict)
        and item["structural"].get("status") == "challenge_cleared"
        for item in (rounds if isinstance(rounds, list) else [])
    )
    if structural_cleared:
        return (
            "Page has resumed from HITL and a fresh AXTree no longer shows the"
            " blocking structural challenge. Re-check Page.getState and"
            " DOM.getAXTree, then resume the original business checkpoint. If"
            " target content is still a skeleton, retry its reveal/materialize"
            " action once and verify with DOM.getSemanticTree plus the requested"
            " record count; do not finalize from page title or drawer shell alone."
        )
    return (
        "Page has resumed from HITL. Re-check Page.getState and DOM.getAXTree"
        " before resuming the original business checkpoint; this resume receipt"
        " does not by itself prove that every prior challenge surface or target"
        " skeleton has disappeared. Retry the original reveal/materialize action"
        " when needed and verify the requested content with DOM.getSemanticTree."
    )

async def _enrich_pause_with_wait(
    agent: Any,
    params: JsonDict,
    response: JsonDict,
    step: int,
) -> JsonDict:
    """When Hitl.requestPause succeeds, harness takes over the wait so the
    model doesn't burn steps polling broken APIs. The pause response is
    extended with a `hitl_wait` field describing the outcome.
    """
    page_id = params.get("pageId") if isinstance(params, dict) else None
    if not page_id:
        return response
    diagnostics = getattr(agent, "diagnostics", None)
    harness_cfg = agent.runtime.harness
    wait_result = await _bt().wait_for_hitl_resume(
        browser=agent.browser,
        page_id=str(page_id),
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 900.0),
        poll_interval_seconds=getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0),
        diagnostics=diagnostics,
        logger=agent.logger,
        challenge_verifier=_make_hitl_challenge_verifier(agent, str(page_id), step),
        pause_snapshot=_hitl_pause_snapshot(agent, str(page_id)),
    )
    if wait_result.get("status") == "resumed":
        wait_result = await _post_hitl_recovery_loop(
            agent,
            str(page_id),
            wait_result,
            step,
        )
    if wait_result.get("status") == "resumed":
        wait_result = dict(wait_result)
        wait_result["fleetAuthBarrier"] = await _verify_and_open_fleet_auth_barrier(
            agent,
            str(page_id),
            step,
        )
    enriched = dict(response)
    enriched["hitl_wait"] = wait_result
    if wait_result.get("status") == "resumed":
        _clear_challenge_state_after_recovery(
            agent, str(page_id), event="challenge.hitl_resume_cleared"
        )
        enriched["suggested_prompt"] = _hitl_resumed_suggested_prompt(wait_result)
    elif wait_result.get("status") in {
        "still_challenge_after_hitl",
        "browser_error_after_hitl",
        "stale_pause_deadlock",
    }:
        if wait_result.get("status") == "stale_pause_deadlock":
            enriched["suggested_prompt"] = (
                "HITL pause is deadlocked: Hitl.resolvePause is blocked by"
                " ERR_PAGE_PAUSED. Do NOT call Hitl.requestPause again for this"
                " page; report status=stale_pause_deadlock and let LeadAgent"
                " continue from a fresh page/fleet."
            )
        else:
            enriched["suggested_prompt"] = (
                "Post-HITL recovery did not confirm a usable page. Do NOT call"
                " more browser tools or Hitl.* methods in this worker; call"
                " final_answer(status=\"incomplete\") and report hitl_wait.status,"
                " postHitlRecovery evidence, screenshotPath, and pageId to LeadAgent."
            )
    elif wait_result.get("status") == "page_settled_after_hitl":
        enriched["suggested_prompt"] = (
            "The page appears to be past the challenge, but ABCP still reports"
            " it as paused for human intervention. Do NOT call browser tools;"
            " report status=page_settled_after_hitl via final_answer and"
            " surface that platform auto-recovery has not released the paused"
            " page yet."
        )
    else:
        enriched["suggested_prompt"] = (
            "HITL wait timed out — page is still paused. Do NOT call other"
            " Hitl.* methods; report status=incomplete via final_answer."
        )
    return enriched
