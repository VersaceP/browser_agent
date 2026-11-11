"""Challenge observations and explicit human pause/resume execution.

Component detection reports facts. Only the BrowserAgent decides whether the
current task requires human help; this module never requests a pause itself.
"""

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional
from abcp_client import ABCPTransportError
from harness.observation.challenge_detector import ChallengeTracker, extract_page_id
from harness.diagnostics.error_classification import attach_error_classification
from harness.fleet.auth import verify_protected_auth_target
from harness.fleet.runtime import FleetClickGateTimeout
from harness.observation.browser_call import build_browser_call_runner
from harness.tools.parsers import attach_method_schema
from harness.utils import JsonDict, exception_payload
from .axtree_state import _axtree_lines_from_value


def _bt():
    import harness.tools.browser_tools as bt

    return bt


async def _observe_challenge_signals(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Attach attributed observations; the model owns any decision to pause."""
    if method.startswith("Hitl."):
        return result
    if _result_has_paused_error(result):
        return {
            **result,
            "pausedState": {"type": "hitl_paused_state",
                            "pageId": extract_page_id(params, result),
                            "triggerMethod": method},
            "next_instruction": (
                "This page is already paused. Wait for human feedback or the matching"
                " Hitl.resumed event; do not request another pause."
            ),
        }
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is None:
        tracker = ChallengeTracker()
        agent.challenge_tracker = tracker
    tracker.cleanup_stale(step)
    state = tracker.feed(method=method, params=params, result=result, step=step)
    if state is None or not state.recent_signals or state.last_signal_step != step:
        return result
    return {
        **result,
        "suspected_challenge": {
            **state.to_summary(),
            "blockingStatus": "unconfirmed",
            "decisionOwner": "browser_agent",
            "observedInThisCall": state.last_signal_step == step,
        },
    }


def _result_has_paused_error(value: Any, *, depth: int = 0) -> bool:
    """Read protocol pause/error fields, never arbitrary page or request text."""
    if depth > 5:
        return False
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict) and isinstance(data.get("hitl"), dict):
            if data["hitl"].get("isPaused") is True:
                return True
        return any(_result_has_paused_error(value[key], depth=depth + 1)
                   for key in ("error", "message", "code", "response") if key in value)
    text = str(value or "").lower()
    return "err_page_paused" in text or "paused for human intervention" in text


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
        runner = getattr(agent, "browser_call_runner", None)
        if runner is None:
            runner = build_browser_call_runner(
                browser=agent.browser,
                logger=agent.logger,
                capability_methods=agent.capability_methods,
            )
            agent.browser_call_runner = runner
        response = await runner.call(method, params)
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
        if bool(getattr(exc, "connection_fatal", False)):
            raise
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


def _hitl_pause_rounds(agent: Any) -> Dict[str, int]:
    """Observed pause counts by pageId; the empty key is the worker total."""
    rounds = getattr(agent, "hitl_pause_rounds", None)
    if not isinstance(rounds, dict):
        rounds = {}
        agent.hitl_pause_rounds = rounds
    return rounds


def _count_hitl_pause_round(agent: Any, page_id: str) -> Dict[str, int]:
    """Record one dispatched pause, without a quota. Called from one place.

    `_claim_fleet_auth_barrier_for_hitl` is the single choke point every
    dispatched Hitl.requestPause crosses, so counting there — and only there —
    keeps the auto path (which claims the barrier itself first, then dispatches
    through the same guard) from being counted twice for one pause.
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
    """Honor explicitly unattended deployments; counts do not decide intent."""
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
    return None


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


def _clear_challenge_observations(agent: Any, page_id: str) -> None:
    """A human handoff ends the old observation episode, not the business task."""
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is not None:
        tracker.clear_page(str(page_id))


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
    harness_config = getattr(getattr(agent, "runtime", None), "harness", None)
    try:
        max_rounds = max(1, int(getattr(
            harness_config, "hitl_post_resume_confirm_max_rounds", 3,
        ) or 1))
    except (TypeError, ValueError):
        max_rounds = 3
    try:
        poll_seconds = max(0.0, float(getattr(
            harness_config, "hitl_poll_interval_seconds", 2.0,
        ) or 0.0))
    except (TypeError, ValueError):
        poll_seconds = 2.0
    try:
        settlement_seconds = max(0.0, float(getattr(
            harness_config, "page_settlement_timeout_seconds", 15.0,
        ) or 0.0))
    except (TypeError, ValueError):
        settlement_seconds = 15.0
    if poll_seconds > 0:
        # Use the existing page-settlement window as the time boundary. The
        # base read-attempt bound may not cover a real post-HITL navigation.
        # Introducing a second lifecycle timer would let the two gates
        # disagree again.
        max_rounds = max(
            max_rounds,
            int(settlement_seconds / poll_seconds) + 1,
        )

    # A resume notification means that human control was returned; it does
    # not mean the renderer has already reached a DOM-readable lifecycle.
    # Keep that progress while performing a bounded, read-only readiness
    # confirmation.  In particular, do not turn `loading -> page-not-ready`
    # into evidence that the human failed to clear the challenge.
    attempts: List[JsonDict] = []
    state: JsonDict = {}
    tree: JsonDict = {}
    state_data: JsonDict = {}
    loading_statuses = {"loading", "navigating", "startedloading", "pending"}
    tracker = getattr(agent, "page_lifecycle", None)
    deadline = time.monotonic() + settlement_seconds
    for round_index in range(max_rounds):
        # Reuse the subscriber's state: it includes Page.loaded/loadFailed
        # received BEFORE Hitl.resumed. Do not subscribe after resume or wait
        # for an event that has already arrived. Unknown state gets one probe;
        # a known loading page waits without polling the browser.
        settlement = None
        if tracker is not None:
            settlement = await tracker.wait_for_settlement(
                page_id, max(0.0, deadline - time.monotonic()),
            )
            if settlement == "closed":
                return {
                    "enabled": True, "opened": False, "reason": "page_closed",
                    "confirmationAttempts": attempts,
                }
        state = await _bt()._post_hitl_raw_browser_call(
            agent,
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": (
                    "Verify shared fleet state before opening the"
                    " authentication barrier."
                ),
            },
            step,
        )
        if _bt()._invoke_result_failed(state):
            attempts.append({
                "round": round_index + 1,
                "status": "state_unavailable",
            })
        else:
            state_data = _bt()._response_data(state)
            if tracker is not None:
                tracker.observe_state_response(page_id, state)
            hitl = (
                state_data.get("hitl")
                if isinstance(state_data.get("hitl"), dict) else {}
            )
            if hitl.get("isPaused") is True:
                return {
                    "enabled": True,
                    "opened": False,
                    "reason": "page_still_paused",
                    "confirmationAttempts": attempts + [{
                        "round": round_index + 1,
                        "status": "page_still_paused",
                    }],
                }
            lifecycle = str(state_data.get("status") or "").strip().lower()
            if lifecycle in {"failed", "loadfailed", "error", "crashed", "closed"}:
                return {
                    "enabled": True, "opened": False,
                    "reason": "page_not_available",
                    "state": state,
                    "confirmationAttempts": attempts + [{
                        "round": round_index + 1, "status": "page_not_available",
                        "lifecycle": lifecycle,
                    }],
                }
            if lifecycle in loading_statuses:
                attempts.append({
                    "round": round_index + 1,
                    "status": "page_not_ready",
                    "lifecycle": lifecycle,
                })
            else:
                tree = await _bt()._post_hitl_raw_browser_call(
                    agent,
                    "DOM.getAXTree",
                    {
                        "pageId": page_id,
                        "purpose": (
                            "Refresh page perception before opening the"
                            " shared authentication barrier."
                        ),
                    },
                    step,
                    capture_axtree_text=True,
                )
                if not _bt()._invoke_result_failed(tree):
                    attempts.append({
                        "round": round_index + 1,
                        "status": "confirmed",
                        "lifecycle": lifecycle or None,
                    })
                    break
                attempts.append({
                    "round": round_index + 1,
                    "status": "tree_unavailable",
                    "lifecycle": lifecycle or None,
                })
        if tracker is not None and (
            settlement == "timeout" or time.monotonic() >= deadline
        ):
            return {
                "enabled": True, "opened": False,
                "reason": "clearance_confirmation_timeout",
                "confirmationAttempts": attempts,
            }
        tracked = tracker.state(page_id) if tracker is not None else None
        # Loading is event-driven on the next iteration. Poll only when the
        # lifecycle channel cannot currently resolve readiness (legacy client,
        # failed state probe or an AX read race after apparent settlement).
        if (
            round_index < max_rounds - 1 and poll_seconds > 0
            and (tracked is None or tracked.status != "loading")
        ):
            await asyncio.sleep(poll_seconds)
    else:
        return {
            "enabled": True,
            "opened": False,
            "reason": "clearance_confirmation_timeout",
            "confirmationAttempts": attempts,
        }

    resolved = await barrier.resolve(fleet_id, worker_id)
    if resolved.get("resolved"):
        agent.fleet_reperception_pending = True
        agent.fleet_reperception_state_seen = True
        agent.fleet_reperception_tree_seen = True
        agent.fleet_barrier_generation = int(resolved.get("generation") or 0)
        agent.fleet_reperception_pending = False
    contract = getattr(agent, "worker_contract", None)
    verification_contract = (
        contract.get("auth_verification")
        if isinstance(contract, dict)
        else None
    )
    verification_evidence = {
        "fleetId": fleet_id,
        "pageId": page_id,
        "url": state_data.get("url"),
        "title": state_data.get("title"),
        "sessionKey": getattr(agent, "fleet_session_key", ""),
        "verificationContract": verification_contract,
        # Both the task-local proof and the durable ledger use this only for an
        # in-memory marker match; raw AX text is never persisted in receipts.
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
    task_auth_verification = verify_protected_auth_target(
        verification_evidence,
    )
    callback = getattr(agent, "auth_session_verified_handler", None)
    ledger_receipt: JsonDict = {}
    if resolved.get("resolved") and callable(callback):
        try:
            value = callback(verification_evidence)
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
    task_binding_callback = getattr(agent, "task_session_binding_handler", None)
    task_binding_receipt: JsonDict = {}
    if resolved.get("resolved") and callable(task_binding_callback):
        try:
            value = task_binding_callback(
                {
                    "phaseId": str(getattr(agent, "phase_id", "") or ""),
                    "fleetId": fleet_id,
                    "pageId": page_id,
                    "sessionGeneration": int(
                        getattr(agent, "fleet_session_generation", 0) or 0
                    ),
                    "url": state_data.get("url"),
                    "title": state_data.get("title"),
                    # A generic human-resumed CAPTCHA establishes at most
                    # same-Fleet continuity. Only the independently checked
                    # auth contract may label the binding authenticated; exact
                    # Page scope is promoted later from unfinished page-local
                    # work, never from HITL alone.
                    "bindingScope": "fleet",
                    "authVerified": bool(task_auth_verification.get("verified")),
                    "reason": (
                        "verified_auth_task_continuity"
                        if task_auth_verification.get("verified")
                        else "hitl_resumed_fleet_continuity"
                    ),
                }
            )
            if hasattr(value, "__await__"):
                value = await value
            if isinstance(value, dict):
                task_binding_receipt = value
        except Exception as exc:
            task_binding_receipt = {
                "recorded": False,
                "reason": "task_session_binding_handler_failed",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            }
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "spawner.task_session_binding.handler_failed",
                    task_binding_receipt,
                )
    return {
        "enabled": True,
        "opened": bool(resolved.get("resolved")),
        "generation": resolved.get("generation"),
        "taskAuthVerification": task_auth_verification,
        "ledger": ledger_receipt or None,
        "taskSessionBinding": task_binding_receipt or None,
        "reason": resolved.get("reason"),
        "confirmationAttempts": attempts,
    }


def _hitl_resumed_suggested_prompt(wait_result: Any) -> str:
    return (
        "Human control was returned. Read userFeedback before deciding the next action."
        " This resume receipt does not prove that a challenge is gone or the target is"
        " accessible. Combine the original task, the user's feedback and current page"
        " evidence; refresh stale observations when needed. Decide whether to continue,"
        " inspect a specific uncertainty, or request further human help."
    )


async def _enrich_pause_with_wait(
    agent: Any,
    params: JsonDict,
    response: JsonDict,
    step: int,
    *,
    assistance_kind: str = "",
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
    pause_data = response.get("data") if isinstance(response.get("data"), dict) else {}
    pause_id = str(pause_data.get("pauseId") or "")
    wait_result = await _bt().wait_for_hitl_resume(
        browser=agent.browser,
        page_id=str(page_id),
        timeout_seconds=None,
        poll_interval_seconds=getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0),
        diagnostics=diagnostics,
        logger=agent.logger,
        pause_reason=str(params.get("reason") or params.get("purpose") or "Awaiting confirmation"),
        input_handler=getattr(agent, "hitl_input_handler", None),
    )
    feedback = wait_result.get("userFeedback")
    if feedback:
        feedback_id = uuid.uuid4().hex
        feedback_record = {
            "feedbackId": feedback_id,
            "pageId": str(page_id),
            "pauseId": pause_id,
            "assistanceKind": assistance_kind or "unspecified",
            "requestPurpose": str(params.get("purpose") or params.get("reason") or ""),
            "text": feedback,
            "feedbackSource": wait_result.get("feedbackSource") or "unknown",
        }
        if getattr(agent, "logger", None) is not None:
            agent.logger.write("hitl.feedback_received", feedback_record)
            if getattr(agent, "standalone_browser_mode", False):
                # Only the wait channel supplies these records. Model summaries
                # and a bare resume/timeout cannot become operator decisions.
                from harness.planning.context import retain_operator_inputs
                retain_operator_inputs(agent.logger, [{
                    **feedback_record, "inputId": feedback_id,
                    "source": "hitl_user_feedback",
                }])
        pending = getattr(agent, "hitl_user_messages", None)
        if pending is None:
            pending = []
            agent.hitl_user_messages = pending
        pending.append(feedback_record)
    if wait_result.get("status") == "resumed":
        wait_result = dict(wait_result)
        wait_result["fleetAuthBarrier"] = await _verify_and_open_fleet_auth_barrier(
            agent,
            str(page_id),
            step,
        )
    enriched = dict(response)
    enriched["hitlRequestBinding"] = {
        "pageId": str(page_id), "pauseId": pause_id or None,
        "assistanceKind": assistance_kind or "unspecified",
        "feedbackReceived": bool(feedback),
    }
    enriched["hitl_wait"] = wait_result
    if getattr(agent, "standalone_browser_mode", False):
        if wait_result.get("status") in {"timeout", "waiting"}:
            agent.standalone_hitl_timeout = {
                "pageId": str(page_id), "pauseId": pause_id,
                "assistanceKind": assistance_kind,
            }
        elif wait_result.get("status") == "resumed":
            agent.standalone_hitl_timeout = None
    if wait_result.get("status") == "resumed":
        _clear_challenge_observations(agent, str(page_id))
        enriched["suggested_prompt"] = _hitl_resumed_suggested_prompt(wait_result)
    elif wait_result.get("status") == "stale_pause_deadlock":
        enriched["suggested_prompt"] = (
            "HITL pause is deadlocked: Hitl.resolvePause is blocked by"
            " ERR_PAGE_PAUSED. Report the protocol failure and retained userFeedback;"
            " requesting another pause cannot release this existing pause."
        )
    elif wait_result.get("status") == "page_settled_after_hitl":
        enriched["suggested_prompt"] = (
            "A human resume signal was received, but the platform pause state"
            " was not confirmed clear. Preserve userFeedback and inspect the"
            " confirmation checks and reason. Do not issue business actions"
            " while the platform pause remains unresolved."
        )
    else:
        enriched["suggested_prompt"] = (
            "HITL wait ended without a reply or a confirmed resume; inspect"
            " hitl_wait.status and reason. The page is still paused. Do NOT call other"
            " Hitl.* methods; report status=incomplete via final_answer."
        )
    return enriched
