"""Wait for explicit human control handoff through HITL events or terminal input.

Page lifecycle and title changes never release a pause. The helper confirms the
platform pause state after a human handoff; task accessibility and further help
are decisions for the BrowserAgent using the original goal and current evidence.
"""

import asyncio
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from abcp_client import ABCPClient
from harness.diagnostics import WorkerDiagnostics
from harness.utils import RunLogger


PAGE_SETTLED_AFTER_HITL = "page_settled_after_hitl"
STALE_PAUSE_DEADLOCK = "stale_pause_deadlock"
RESOLVE_PAUSE_PURPOSE = (
    "Resolve HITL pause after an explicit HITL resume notification."
)

def _normalize_notification_type(value: Any) -> str:
    """Normalize ABCP event spellings such as Page.loaded/page_loaded."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


# Explicit HITL notifications mean the platform reported a user-driven resume.
# Page lifecycle notifications are deliberately not resume signals: challenge
# pages can load, fail, and retitle themselves without human intervention.
# `Hitl.resumed` is the only resume event the platform publishes, and the
# harness synthesizes no alias of its own; the snake_case spelling is kept
# because the normalizer folds both into one token.
_RESUME_NOTIFICATION_TYPES = frozenset(_normalize_notification_type(item) for item in {
    "hitl_resumed",
    "Hitl.resumed",
})
_PAUSED_STATE_MARKERS = ("paused", "err_page_paused", "human intervention")


def _notification_view(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized event view for both legacy and current envelopes.

    Legacy/current ABCP notifications may look like:
      params.type == "event"
      params.data.event == "Page.loaded"
      params.data.payload.pageId/url/title == ...

    New control-stream notifications look like:
      params.type == "control.event"
      params.data.kind == "pageLoaded" / "hitlResumed"
      params.data.payload.sourceEvent == "Page.loaded" / "Hitl.resumed"
      params.data.payload.page.pageId/url/title == ...

    Some tests and older dispatchers use the flatter form:
      params.type == "Page.loaded"
      params.data.pageId/url/title == ...
    """
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    data = params.get("data") if isinstance(params.get("data"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}

    raw_type = (
        data.get("event")
        or data.get("sourceEvent")
        or payload.get("sourceEvent")
        or data.get("kind")
        or params.get("type")
    )
    ntype = _normalize_notification_type(raw_type)
    return {
        "type": ntype,
        "pageId": payload.get("pageId") or page.get("pageId") or data.get("pageId"),
        "title": payload.get("title") or page.get("title") or data.get("title"),
        "url": payload.get("url") or page.get("url") or data.get("url"),
    }


def _make_resume_predicate_strict(page_id: str):
    """Match ONLY explicit HITL resume notifications.

    Safe to apply to the replay buffer because explicit hitl_resumed-style
    events should not occur except after a Hitl.requestPause, so a recent
    replay match is almost certainly the resume we're waiting for.
    """
    page_id_str = str(page_id)

    def predicate(msg: Dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        view = _notification_view(msg)
        ntype = view.get("type")
        if ntype not in _RESUME_NOTIFICATION_TYPES:
            return False
        msg_page_id = view.get("pageId")
        if not msg_page_id:
            return True
        return str(msg_page_id) == page_id_str

    return predicate


def _is_paused_error_text(value: Any) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in _PAUSED_STATE_MARKERS)


async def _close_deadlocked_page(
    browser: ABCPClient,
    page_id: str,
    reason: str,
) -> Dict[str, Any]:
    try:
        closed = await browser.call(
            "Page.close",
            {
                "pageId": page_id,
                "purpose": "Close stale HITL-paused page after control-channel deadlock.",
            },
        )
        return {
            "method": "Page.close",
            "result": _slim_evidence(closed),
            "reason": reason,
        }
    except Exception as exc:
        return {
            "method": "Page.close",
            "errorType": type(exc).__name__,
            "error": str(exc)[:300],
            "reason": reason,
        }


def _stale_pause_deadlock_payload(
    checks: List[Any],
    close_result: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": STALE_PAUSE_DEADLOCK,
        "checks": checks,
        "reason": (
            "Hitl.resolvePause is itself blocked by ERR_PAGE_PAUSED;"
            " the page pause flag is stale or the running ABCP build"
            " does not exempt resolvePause."
        ),
        "deadlock": True,
        "closed": not bool(close_result.get("error")),
    }


async def _resolve_after_verified_settlement(
    *,
    browser: ABCPClient,
    page_id: str,
    deadline: float,
    poll_interval_seconds: float,
    purpose: str = RESOLVE_PAUSE_PURPOSE,
) -> Dict[str, Any]:
    """Release control after explicit terminal feedback, then confirm pause state.

    The panel never clears the pause flag on its own, so unlike the
    explicit-resume path we resolve first and only then poll Page.getState.
    A resolvePause rejected with a paused-state error is the deadlock case.
    """
    checks: List[Any] = []
    resolve_error_text = ""
    try:
        resolved = await browser.call(
            "Hitl.resolvePause",
            {"pageId": page_id, "purpose": purpose},
        )
        checks.append({
            "method": "Hitl.resolvePause",
            "attempt": 1,
            "result": _slim_evidence(resolved),
        })
    except Exception as exc:
        error_text = str(exc)[:300]
        checks.append({
            "method": "Hitl.resolvePause",
            "attempt": 1,
            "errorType": type(exc).__name__,
            "error": error_text,
        })
        if _is_paused_error_text(error_text):
            close_result = await _close_deadlocked_page(browser, page_id, error_text)
            checks.append(close_result)
            return _stale_pause_deadlock_payload(checks, close_result)
        # Non-paused resolve errors (e.g. "not paused"/"already resolved")
        # can mean the platform cleared the pause on its own — let the
        # Page.getState confirmation decide instead of failing outright,
        # but keep the error as first-class evidence in case it doesn't.
        resolve_error_text = error_text
    # Give the confirmation a small grace window even if the outer wait is
    # near its deadline: the resolve already happened, the page state read is
    # the cheap part.
    confirm_deadline = max(deadline, time.monotonic() + 10.0)
    confirmation = await _confirm_unpaused_after_settlement(
        browser=browser,
        page_id=page_id,
        deadline=confirm_deadline,
        poll_interval_seconds=poll_interval_seconds,
    )
    merged = dict(confirmation)
    merged["checks"] = checks + list(confirmation.get("checks") or [])
    if resolve_error_text and merged.get("status") != "resumed":
        merged["resolveError"] = resolve_error_text
        prior_reason = str(merged.get("reason") or "").strip()
        merged["reason"] = (
            f"Hitl.resolvePause failed with a non-paused error: {resolve_error_text}."
            + (f" {prior_reason}" if prior_reason else "")
        )
    return merged


def _page_state_body(state_response: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not isinstance(state_response, dict):
        return {}, {}
    response = state_response.get("response")
    body = response if isinstance(response, dict) else state_response
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    return body, data


def _meaningful_error_text(source: Any) -> str:
    if source is None or source is False:
        return ""
    if isinstance(source, str):
        return source.strip().lower()
    if isinstance(source, dict):
        for key in ("message", "error", "data", "description", "errorMessage"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
            if isinstance(value, dict):
                nested = _meaningful_error_text(value)
                if nested:
                    return nested
        # Some successful Page.getState responses include a benign shape like
        # {"code": -3, "description": ""}. Treat code-only empty descriptions
        # as non-evidence; non-empty text above remains conservative.
        return ""
    return str(source).strip().lower()


def _pause_marker_reason(state_response: Any) -> str:
    if not isinstance(state_response, dict):
        return ""
    body, data = _page_state_body(state_response)
    hitl = data.get("hitl") if isinstance(data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True:
        context_parts: List[str] = []
        for key in ("title", "url", "status", "errorMessage"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                context_parts.append(f"{key}={value.strip()[:120]!r}")
        suffix = f"; {' '.join(context_parts)}" if context_parts else ""
        return f"Page.getState data.hitl.isPaused=true{suffix}"

    haystack_parts: List[str] = []
    for key in ("observation", "suggested_prompt"):
        value = body.get(key)
        if isinstance(value, str) and value:
            haystack_parts.append(value.lower())
    for key in ("status", "errorMessage"):
        value = data.get(key)
        if isinstance(value, str) and value:
            haystack_parts.append(value.lower())
    for source in (state_response.get("error"), body.get("error"), data.get("error")):
        text = _meaningful_error_text(source)
        if text:
            haystack_parts.append(text)

    haystack = " ".join(haystack_parts).strip()
    if any(marker in haystack for marker in _PAUSED_STATE_MARKERS):
        return haystack[:300]
    return ""


def _state_indicates_still_paused(state_response: Any) -> bool:
    """Inspect a Page.getState response and return True iff the page is
    almost certainly still in a HITL-paused state.

    Conservative defaults — any of the following keeps us in "paused":
      - observation / suggested_prompt / error contains a paused marker
      - data.status contains a paused marker
      - response carries an `error` (top-level or in data) whose text does
        NOT obviously indicate "not paused" — unrecognized errors should
        keep us waiting rather than be interpreted as a recovery signal.
      - state is not a dict (we couldn't parse it)

    This matters because the legacy detector was bivalent: anything not
    obviously paused was treated as resumed, and an opaque error like
    `{"error": "ERR_PAGE_PAUSED..."}` returned from Page.getState would
    have been mis-read as "resumed" because the `paused` marker only
    matched on observation/data.status.
    """
    if not isinstance(state_response, dict):
        return True

    body, data = _page_state_body(state_response)
    hitl = data.get("hitl") if isinstance(data.get("hitl"), dict) else {}
    errors = [_meaningful_error_text(source) for source in
              (state_response.get("error"), body.get("error"), data.get("error"))]
    if (hitl.get("isPaused") is False and not any(errors)
            and str(data.get("status") or "").lower() != "paused"):
        return False
    haystack_parts: List[str] = []
    for key in ("observation", "suggested_prompt"):
        val = body.get(key)
        if isinstance(val, str) and val:
            haystack_parts.append(val.lower())
    hitl = data.get("hitl") if isinstance(data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True:
        return True
    for key in ("status", "errorMessage"):
        val = data.get(key)
        if isinstance(val, str) and val:
            haystack_parts.append(val.lower())

    error_payloads: List[str] = []
    for source in (state_response.get("error"), body.get("error"), data.get("error")):
        text = _meaningful_error_text(source)
        if text:
            error_payloads.append(text)

    haystack = " ".join(haystack_parts + error_payloads)
    if any(marker in haystack for marker in _PAUSED_STATE_MARKERS):
        return True

    # Unrecognized error indication — be conservative, keep waiting.
    if error_payloads:
        return True

    return False


async def _confirm_unpaused_after_settlement(
    *,
    browser: ABCPClient,
    page_id: str,
    deadline: float,
    poll_interval_seconds: float,
) -> Dict[str, Any]:
    checks: List[Any] = []
    interval = min(max(0.5, poll_interval_seconds), 2.0)
    max_resolve_attempts = 2
    resolve_attempts = 0
    last_blocker_reason = ""

    async def resolve_once(purpose: str) -> Optional[str]:
        nonlocal resolve_attempts
        resolve_attempts += 1
        try:
            resolved = await browser.call(
                "Hitl.resolvePause",
                {
                    "pageId": page_id,
                    "purpose": purpose,
                },
            )
            checks.append({
                "method": "Hitl.resolvePause",
                "attempt": resolve_attempts,
                "result": _slim_evidence(resolved),
            })
            return None
        except Exception as exc:
            error_text = str(exc)[:300]
            checks.append({
                "method": "Hitl.resolvePause",
                "attempt": resolve_attempts,
                "errorType": type(exc).__name__,
                "error": error_text,
            })
            return error_text

    for attempt in range(5):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if attempt:
            await asyncio.sleep(min(interval, remaining))
        try:
            state = await browser.call(
                "Page.getState",
                {
                    "pageId": page_id,
                    "purpose": "hitl settlement confirmation after resolvePause",
                },
            )
        except Exception as exc:
            checks.append({
                "method": "Page.getState",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            })
            error_text = str(exc).lower()
            if (
                _is_paused_error_text(error_text)
                and resolve_attempts < max_resolve_attempts
            ):
                last_blocker_reason = error_text[:300]
                resolve_error = await resolve_once(
                    f"{RESOLVE_PAUSE_PURPOSE} Retrying because Page.getState still reports paused."
                )
                if _is_paused_error_text(resolve_error):
                    close_result = await _close_deadlocked_page(
                        browser, page_id, resolve_error or ""
                    )
                    checks.append(close_result)
                    return _stale_pause_deadlock_payload(checks, close_result)
            continue
        checks.append({
            "method": "Page.getState",
            "result": _slim_evidence(state),
        })
        if not _state_indicates_still_paused(state):
            return {
                "status": "resumed",
                "state": state,
                "checks": checks,
            }
        pause_reason = _pause_marker_reason(state)
        if pause_reason:
            last_blocker_reason = pause_reason
            if resolve_attempts < max_resolve_attempts:
                resolve_error = await resolve_once(
                    f"{RESOLVE_PAUSE_PURPOSE} Retrying because Page.getState still reports paused."
                )
                if _is_paused_error_text(resolve_error):
                    close_result = await _close_deadlocked_page(
                        browser, page_id, resolve_error or ""
                    )
                    checks.append(close_result)
                    return _stale_pause_deadlock_payload(checks, close_result)
                continue
        elif _state_indicates_still_paused(state):
            last_blocker_reason = (
                "Page.getState did not confirm a usable unpaused page."
            )
    return {
        "status": PAGE_SETTLED_AFTER_HITL,
        "checks": checks,
        "reason": last_blocker_reason,
    }


from harness.runtime.wait_timing import timed_human_wait


@timed_human_wait
async def wait_for_hitl_resume(
    *,
    browser: ABCPClient,
    page_id: str,
    timeout_seconds: Optional[float] = None,
    poll_interval_seconds: float,
    diagnostics: Optional[WorkerDiagnostics],
    logger: Optional[RunLogger],
    input_handler=None,
    pause_reason: str = "等待用户协助或确认",
) -> Dict[str, Any]:
    """Race page events with terminal feedback; cancel all readers on exit."""
    from harness.runtime.hitl_input import read_hitl_input

    handler = input_handler or read_hitl_input
    terminal_available = input_handler is not None or (
        sys.stdin is not None and sys.stdin.isatty()
    )
    event_task = asyncio.create_task(_wait_for_hitl_events(
        browser=browser, page_id=page_id, timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds, diagnostics=diagnostics,
        logger=logger,
    ))
    input_task = asyncio.create_task(handler(page_id, pause_reason))
    started = time.monotonic()
    feedback = None
    input_record = None

    def retain_feedback(text):
        nonlocal input_record
        if not text or input_record is not None:
            return
        input_record = {"inputId": uuid.uuid4().hex, "source": "terminal_hitl",
                        "pageId": page_id, "question": pause_reason, "text": text,
                        "receivedAt": datetime.now(timezone.utc).isoformat()}
        if logger is not None and getattr(logger, "task_dir", None) is not None:
            from harness.planning.context import retain_operator_inputs
            retain_operator_inputs(logger, [input_record])
            logger.write("hitl.operator_input_received", input_record)
    try:
        done, _ = await asyncio.wait(
            {event_task, input_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if input_task in done:
            try:
                feedback = input_task.result()
            except Exception as exc:
                if logger is not None:
                    logger.write("hitl.input.failed", {"pageId": page_id,
                                 "errorType": type(exc).__name__})
            if isinstance(feedback, str):
                feedback = feedback.strip()
            else:
                feedback = None
        retain_feedback(feedback)
        if (event_task not in done and not feedback and terminal_available
                and timeout_seconds is None):
            # A reachable input channel ended without an answer (e.g. EOF).
            # Preserve the pause; never turn closed input into a human decision
            # or leave an unbounded event wait running after the terminal ends.
            outcome = {"status": "waiting", "pageId": page_id,
                       "reason": "terminal_input_ended",
                       "elapsedMs": int((time.monotonic() - started) * 1000)}
            if logger is not None:
                logger.write("hitl.wait.interrupted", outcome)
            return outcome
        if event_task in done or not feedback:
            outcome = await event_task
        else:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
            # Human text explicitly releases control, but does not prove the
            # protected target usable. Keep the existing state verification.
            outcome = await _resolve_after_verified_settlement(
                browser=browser, page_id=page_id,
                deadline=time.monotonic() + 10.0,
                poll_interval_seconds=poll_interval_seconds,
                purpose="User supplied terminal instructions for this HITL pause; release control.",
            )
            outcome.update(pageId=page_id, via="terminal_input",
                           elapsedMs=int((time.monotonic() - started) * 1000))
            if diagnostics is not None and outcome.get("status") == "resumed":
                diagnostics.mark_hitl_resumed()
            if logger is not None:
                logger.write("hitl.wait." + str(outcome["status"]), outcome)
        # Feedback may arrive while the event path confirms page settlement.
        if not feedback and input_task.done() and not input_task.cancelled():
            try:
                candidate = input_task.result()
                feedback = candidate.strip() if isinstance(candidate, str) else None
            except Exception:
                pass
        if feedback:
            retain_feedback(feedback)
            outcome = {**outcome, "userFeedback": feedback,
                       "feedbackSource": "terminal_user", "operatorInputRecords": [input_record]}
        return outcome
    finally:
        for task in (event_task, input_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(event_task, input_task, return_exceptions=True)


async def _wait_for_hitl_events(
    *, browser: ABCPClient, page_id: str, timeout_seconds: Optional[float],
    poll_interval_seconds: float, diagnostics: Optional[WorkerDiagnostics],
    logger: Optional[RunLogger],
) -> Dict[str, Any]:
    """Only an explicit platform resume returns control on the event path.

    Lifecycle/title changes and model availability cannot establish human intent.
    Confirm the protocol pause state, leaving task accessibility to the agent.
    """
    if diagnostics is not None:
        diagnostics.mark_hitl_wait_entered(page_id)
    if logger is not None:
        logger.write("hitl.wait.start", {
            "pageId": page_id, "timeoutSeconds": timeout_seconds,
            "pollIntervalSeconds": poll_interval_seconds,
        })
    started = time.monotonic()
    deadline = started + max(0.0, timeout_seconds) if timeout_seconds is not None else None
    predicate = _make_resume_predicate_strict(page_id)
    error = None
    try:
        message = await browser.wait_for_notification(
            predicate, timeout=max(0.0, deadline - time.monotonic()) if deadline is not None else None,
            replay_window_seconds=5.0, replay_predicate=predicate,
        )
    except Exception as exc:
        error = {"branch": "notification", "errorType": type(exc).__name__, "error": str(exc)[:500]}
        message = None
        if logger is not None:
            logger.write("hitl.wait.branch_error", {"pageId": page_id, **error})
    if not isinstance(message, dict):
        outcome = {
            "status": "timeout" if timeout_seconds is not None else "waiting",
            "pageId": page_id, "timeoutSeconds": timeout_seconds,
        }
        if timeout_seconds is None:
            outcome["reason"] = "notification_channel_failed" if error else "notification_channel_ended"
        if error:
            outcome["branch_errors"] = [error]
        if diagnostics is not None and timeout_seconds is not None:
            diagnostics.mark_hitl_wait_timed_out()
    else:
        confirmation = await _confirm_unpaused_after_settlement(
            browser=browser, page_id=page_id,
            deadline=deadline if deadline is not None else time.monotonic() + 10.0,
            poll_interval_seconds=poll_interval_seconds,
        )
        status = str(confirmation.get("status") or PAGE_SETTLED_AFTER_HITL)
        outcome = {"status": status, "via": "notification", "pageId": page_id,
                   "evidence": {"resume": _slim_evidence(message),
                                "confirmation": _slim_evidence(confirmation.get("state"))}}
        if status == "resumed":
            if diagnostics is not None:
                diagnostics.mark_hitl_resumed()
        else:
            outcome["reason"] = confirmation.get("reason") or "Platform pause state was not confirmed clear after human resume."
            outcome["evidence"]["confirmation"] = confirmation.get("checks", [])
            if status == STALE_PAUSE_DEADLOCK:
                outcome.update(deadlock=True, closed=bool(confirmation.get("closed")))
                if diagnostics is not None:
                    diagnostics.mark_hitl_stale_pause_deadlock()
            elif diagnostics is not None:
                diagnostics.mark_hitl_page_settled()
    outcome["elapsedMs"] = int((time.monotonic() - started) * 1000)
    if logger is not None:
        logger.write("hitl.wait." + outcome["status"], outcome)
    return outcome


def _slim_evidence(value: Any) -> Any:
    """Keep the evidence payload small enough to safely live in tool_result."""
    if not isinstance(value, dict):
        return value
    keep: Dict[str, Any] = {}
    for key in (
        "observation",
        "suggested_prompt",
        "method",
        "errorType",
        "gate",
    ):
        if key in value:
            keep[key] = str(value[key])[:300]
    for key in ("settlement", "confirmation"):
        if key in value:
            keep[key] = _slim_evidence(value[key])
    for key, item in value.items():
        if key.startswith(("data_", "params_")) and key not in keep:
            if isinstance(item, (bool, int, float, list, dict)):
                keep[key] = item
            else:
                keep[key] = str(item)[:300]
    error = value.get("error")
    if isinstance(error, str):
        keep["error"] = error[:300]
    elif isinstance(error, dict):
        keep["error"] = str(error.get("message") or error.get("data") or error)[:300]
    params = value.get("params") if isinstance(value.get("params"), dict) else None
    if params is not None:
        keep["params_type"] = str(params.get("type") or "")[:60]
        data = params.get("data") if isinstance(params.get("data"), dict) else None
        if data is not None:
            keep["params_data_keys"] = list(data.keys())[:10]
            for key in ("pageId", "url", "title"):
                if key in data:
                    keep[f"params_data_{key}"] = str(data.get(key) or "")[:300]
    data = value.get("data") if isinstance(value.get("data"), dict) else None
    if data is not None and "params" not in value:
        keep["data_keys"] = list(data.keys())[:10]
        for key in ("pageId", "url", "title", "status", "errorMessage"):
            if key in data:
                keep[f"data_{key}"] = str(data.get(key) or "")[:300]
        hitl = data.get("hitl")
        if isinstance(hitl, dict) and "isPaused" in hitl:
            keep["data_hitl_isPaused"] = bool(hitl.get("isPaused"))
    return keep
