"""
harness.hitl - Shared HITL wait helper for BrowserAgent and plan_executor.

After Hitl.requestPause succeeds, the agent must NOT continue calling tools —
ABCP rejects most tools while the page is paused (see prior investigation:
getTaskSummary routing-error, resumeEvent method-not-found, getActionLog
ERR_PAGE_PAUSED). The reliable continuation signal is the System.notification
stream. When a lifecycle event shows the page has settled after HITL, we call
Hitl.resolvePause to release the ABCP control-channel pause, then do a short
Page.getState confirmation; we do not long-poll while paused.

This helper reports which notification fired (for telemetry and post-mortem
analysis).
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

from abcp_client import ABCPClient
from harness.challenge_detector import is_lingering_loading_title
from harness.diagnostics import WorkerDiagnostics
from harness.utils import RunLogger


PAGE_SETTLED_AFTER_HITL = "page_settled_after_hitl"
RESOLVE_PAUSE_PURPOSE = (
    "Resolve HITL pause after page lifecycle indicates the user passed the challenge."
)


def _normalize_notification_type(value: Any) -> str:
    """Normalize ABCP event spellings such as Page.loaded/page_loaded."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


# Explicit HITL notifications mean the platform unpaused the page. Page
# lifecycle notifications only prove the user got past the challenge UI; a
# short Page.getState confirmation below decides whether ABCP also unpaused.
_RESUME_NOTIFICATION_TYPES = frozenset(_normalize_notification_type(item) for item in {
    "hitl_resumed",
    "page_resumed",
    "hitl_completed",
    "human_intervention_completed",
})
_PAGE_TITLE_UPDATED_TYPES = frozenset(_normalize_notification_type(item) for item in {
    "page_title_updated",
    "Page.titleUpdated",
})
_PAGE_LOADED_TYPES = frozenset(_normalize_notification_type(item) for item in {
    "page_loaded",
    "Page.loaded",
})
_CHALLENGE_URL_MARKERS = (
    "__cf_chl",
    "cf_chl",
    "cdn-cgi/challenge-platform",
)

_PAUSED_STATE_MARKERS = ("paused", "err_page_paused", "human intervention")


def _is_challenge_url(url: Any) -> bool:
    value = str(url or "").lower()
    return bool(value and any(marker in value for marker in _CHALLENGE_URL_MARKERS))


def _notification_signal(msg: Dict[str, Any], page_id: str) -> Optional[str]:
    if not isinstance(msg, dict):
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    ntype = _normalize_notification_type(params.get("type"))
    if not ntype:
        return None

    data = params.get("data") if isinstance(params.get("data"), dict) else {}
    msg_page_id = data.get("pageId")

    if ntype in _RESUME_NOTIFICATION_TYPES:
        if not msg_page_id or str(msg_page_id) == str(page_id):
            return "explicit_resume"
        return None

    if not msg_page_id or str(msg_page_id) != str(page_id):
        return None

    if ntype in _PAGE_TITLE_UPDATED_TYPES:
        title = data.get("title")
        if isinstance(title, str) and title.strip() and not is_lingering_loading_title(title):
            return "page_settled"
        return None

    if ntype in _PAGE_LOADED_TYPES:
        url = data.get("url")
        if isinstance(url, str) and url.strip() and not _is_challenge_url(url):
            return "page_settled"

    return None


def _make_resume_predicate_broad(page_id: str):
    """Match either an explicit HITL resume notification or a lifecycle event
    on the same page (signals that the user navigated past the challenge).

    Use for LIVE waiters only — lifecycle events recur during normal
    operation, so applying this to the replay buffer would mis-fire on stale
    events from before the pause.
    """
    def predicate(msg: Dict[str, Any]) -> bool:
        return _notification_signal(msg, page_id) is not None

    return predicate


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
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        ntype = _normalize_notification_type(params.get("type"))
        if ntype not in _RESUME_NOTIFICATION_TYPES:
            return False
        data = params.get("data") if isinstance(params.get("data"), dict) else {}
        msg_page_id = data.get("pageId")
        if not msg_page_id:
            return True
        return str(msg_page_id) == page_id_str

    return predicate


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

    haystack_parts: List[str] = []
    for key in ("observation", "suggested_prompt"):
        val = state_response.get(key)
        if isinstance(val, str) and val:
            haystack_parts.append(val.lower())
    data = state_response.get("data") if isinstance(state_response.get("data"), dict) else {}
    hitl = data.get("hitl") if isinstance(data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True:
        return True
    for key in ("status", "errorMessage"):
        val = data.get(key)
        if isinstance(val, str) and val:
            haystack_parts.append(val.lower())

    error_payloads: List[str] = []
    for source in (state_response.get("error"), data.get("error")):
        if isinstance(source, str) and source:
            error_payloads.append(source.lower())
        elif isinstance(source, dict):
            try:
                error_payloads.append(
                    str(source.get("message") or source.get("data") or source).lower()
                )
            except Exception:
                error_payloads.append("[unparseable error]")

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

    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            resolved = await browser.call(
                "Hitl.resolvePause",
                {
                    "pageId": page_id,
                    "purpose": RESOLVE_PAUSE_PURPOSE,
                },
            )
            checks.append({
                "method": "Hitl.resolvePause",
                "result": _slim_evidence(resolved),
            })
        except Exception as exc:
            checks.append({
                "method": "Hitl.resolvePause",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            })

    for attempt in range(3):
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
    return {
        "status": PAGE_SETTLED_AFTER_HITL,
        "checks": checks,
    }


async def wait_for_hitl_resume(
    *,
    browser: ABCPClient,
    page_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    diagnostics: Optional[WorkerDiagnostics],
    logger: Optional[RunLogger],
) -> Dict[str, Any]:
    """Block until the paused page resumes or `timeout_seconds` elapses.

    Returns a structured outcome consumed by the caller (BrowserAgent enriches
    the tool_result; plan_executor stores it on step_result.hitl).
    """
    if diagnostics is not None:
        diagnostics.mark_hitl_wait_entered()
    if logger is not None:
        logger.write(
            "hitl.wait.start",
            {
                "pageId": page_id,
                "timeoutSeconds": timeout_seconds,
                "pollIntervalSeconds": poll_interval_seconds,
            },
        )

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    broad_predicate = _make_resume_predicate_broad(page_id)
    strict_predicate = _make_resume_predicate_strict(page_id)

    async def via_notification() -> Optional[Dict[str, Any]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Replay scan uses STRICT predicate so stale lifecycle events from
        # before the pause don't false-positive as resume. Live waiter uses
        # the broad predicate, accepting settled Page.loaded/Page.titleUpdated
        # events on the same pageId as a post-HITL settlement signal.
        msg = await browser.wait_for_notification(
            broad_predicate,
            timeout=remaining,
            replay_window_seconds=5.0,
            replay_predicate=strict_predicate,
        )
        if msg is None:
            return None
        return {
            "signal": _notification_signal(msg, page_id),
            "message": msg,
        }

    # IMPORTANT: surface unexpected exceptions from the notification branch
    # instead of silently dropping them. A swallowed TypeError here previously masked a
    # signature mismatch between ABCPClient.wait_for_notification and the
    # underlying hub (the helper looked like it "timed out" in 30ms when in
    # fact the notification branch had blown up on an unknown kwarg).
    task_errors: List[Dict[str, Any]] = []
    via: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    signal: Optional[str] = None
    try:
        value = await via_notification()
    except Exception as exc:
        branch = "notification"
        task_errors.append({
            "branch": branch,
            "errorType": type(exc).__name__,
            "error": str(exc)[:500],
        })
        if logger is not None:
            logger.write("hitl.wait.branch_error", {
                "pageId": page_id,
                "branch": branch,
                "errorType": type(exc).__name__,
                "error": str(exc)[:500],
            })
        value = None
    if isinstance(value, dict):
        via = "notification"
        signal = str(value.get("signal") or "")
        message = value.get("message")
        evidence = message if isinstance(message, dict) else value

    elapsed_ms = int((time.monotonic() - (deadline - max(0.0, timeout_seconds))) * 1000)
    if via is None:
        if diagnostics is not None:
            diagnostics.mark_hitl_wait_timed_out()
        outcome = {
            "status": "timeout",
            "pageId": page_id,
            "elapsedMs": elapsed_ms,
            "timeoutSeconds": timeout_seconds,
        }
        if task_errors:
            # A sub-millisecond "timeout" usually means a branch errored out
            # before doing real work. Surface it so callers know to look.
            outcome["branch_errors"] = task_errors
        if logger is not None:
            logger.write("hitl.wait.timeout", outcome)
        return outcome

    if via == "notification" and signal == "page_settled":
        confirmation = await _confirm_unpaused_after_settlement(
            browser=browser,
            page_id=page_id,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
        )
        elapsed_ms = int((time.monotonic() - (deadline - max(0.0, timeout_seconds))) * 1000)
        if confirmation.get("status") != "resumed":
            if diagnostics is not None:
                diagnostics.mark_hitl_page_settled()
            outcome = {
                "status": PAGE_SETTLED_AFTER_HITL,
                "via": via,
                "pageId": page_id,
                "elapsedMs": elapsed_ms,
                "evidence": {
                    "settlement": _slim_evidence(evidence),
                    "confirmation": confirmation.get("checks", []),
                },
                "reason": (
                    "The page appears to have loaded past the challenge, but"
                    " ABCP still reports the tab as paused for human"
                    " intervention. The ABCP control channel has not released"
                    " this paused page yet; wait for platform auto-recovery or"
                    " restart the browser phase with a fresh page."
                ),
            }
            if logger is not None:
                logger.write("hitl.wait.page_settled_after_hitl", outcome)
            return outcome
        evidence = {
            "settlement": _slim_evidence(evidence),
            "confirmation": _slim_evidence(confirmation.get("state")),
        }

    if diagnostics is not None:
        diagnostics.mark_hitl_resumed()
    outcome = {
        "status": "resumed",
        "via": via,
        "pageId": page_id,
        "elapsedMs": elapsed_ms,
        "evidence": _slim_evidence(evidence),
    }
    if logger is not None:
        logger.write("hitl.wait.resumed", outcome)
    return outcome


def _slim_evidence(value: Any) -> Any:
    """Keep the evidence payload small enough to safely live in tool_result."""
    if not isinstance(value, dict):
        return value
    keep: Dict[str, Any] = {}
    for key in ("observation", "suggested_prompt", "method", "errorType"):
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
