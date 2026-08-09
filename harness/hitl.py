"""
harness.hitl - Shared HITL wait helper for BrowserAgent callers.

After Hitl.requestPause succeeds, the agent must NOT continue calling tools —
ABCP rejects most tools while the page is paused (see prior investigation:
getTaskSummary routing-error, resumeEvent method-not-found, getActionLog
ERR_PAGE_PAUSED).

Protocol boundary: HITL progress is notification-driven. Page state may confirm
that a notification has settled, but Page.getState never creates a resume
signal on its own. This helper resumes via two notification paths:

  1. Explicit `Hitl.resumed`-style notification — kept as a fast path for
     builds/panels that do emit it.
  2. Verified settlement — lifecycle events (Page.loaded / Page.titleUpdated
     on the paused pageId) act as *verification triggers*, never as resume
     signals by themselves: the optional `challenge_verifier` (VL adjudicator)
     makes the final call that the challenge surface is gone; if no usable
     verdict is available, a qualifying post-pause title that no longer looks
     like a challenge is required. Only after that evidence does the helper
     call Hitl.resolvePause and confirm via Page.getState.
Without a `challenge_verifier`, lifecycle events are ignored entirely (strict
explicit-resume contract — challenge pages load, fail, and retitle themselves
without human intervention).

This helper reports which notification fired (for telemetry and post-mortem
analysis).
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from abcp_client import ABCPClient
from harness.challenge_detector import (
    VL_CLEARANCE_VERDICTS,
    is_lingering_loading_title,
    title_looks_like_challenge,
)
from harness.diagnostics import WorkerDiagnostics
from harness.utils import RunLogger


PAGE_SETTLED_AFTER_HITL = "page_settled_after_hitl"
STALE_PAUSE_DEADLOCK = "stale_pause_deadlock"
RESOLVE_PAUSE_PURPOSE = (
    "Resolve HITL pause after an explicit HITL resume notification."
)
VERIFIED_RESOLVE_PURPOSE = (
    "Human completed the challenge: post-pause lifecycle events show a"
    " non-challenge surface and the verifier confirmed it; resolving the"
    " HITL pause to resume automation."
)
_SETTLEMENT_VERIFIER_MAX_CALLS = 3


def _normalize_notification_type(value: Any) -> str:
    """Normalize ABCP event spellings such as Page.loaded/page_loaded."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


# Explicit HITL notifications mean the platform reported a user-driven resume.
# Page lifecycle notifications are deliberately not resume signals: challenge
# pages can load, fail, and retitle themselves without human intervention.
_RESUME_NOTIFICATION_TYPES = frozenset(_normalize_notification_type(item) for item in {
    "hitl_resumed",
    "Hitl.resumeEvent",
    "Hitl.resumed",
    "page_resumed",
    "Page.resumed",
    "hitl_completed",
    "Hitl.completed",
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
_PAGE_NAVIGATED_TYPES = frozenset(_normalize_notification_type(item) for item in {
    "page_navigate",
    "Page.navigate",
})
_CHALLENGE_URL_MARKERS = (
    "__cf_chl",
    "cf_chl",
    "cdn-cgi/challenge-platform",
)

_PAUSED_STATE_MARKERS = ("paused", "err_page_paused", "human intervention")
_CHALLENGE_STATE_MARKERS = (
    "captcha",
    "cf-chl",
    "cloudflare security",
    "performing security verification",
    "verify you are human",
    "验证码",
    "人机验证",
)


def _is_challenge_url(url: Any) -> bool:
    value = str(url or "").lower()
    return bool(value and any(marker in value for marker in _CHALLENGE_URL_MARKERS))


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


def _notification_signal(
    msg: Dict[str, Any],
    page_id: str,
    pause_snapshot: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    if not isinstance(msg, dict):
        return None
    view = _notification_view(msg)
    ntype = view.get("type")
    if not ntype:
        return None

    msg_page_id = view.get("pageId")

    if ntype in _RESUME_NOTIFICATION_TYPES:
        if not msg_page_id or str(msg_page_id) == str(page_id):
            return "explicit_resume"
        return None

    if not msg_page_id or str(msg_page_id) != str(page_id):
        return None

    if ntype in _PAGE_TITLE_UPDATED_TYPES:
        title = view.get("title")
        if isinstance(title, str) and title.strip() and not is_lingering_loading_title(title):
            return "page_settled"
        return None

    if ntype in _PAGE_LOADED_TYPES:
        url = view.get("url")
        if isinstance(url, str) and url.strip() and not _is_challenge_url(url):
            return "page_settled"

    if ntype in _PAGE_NAVIGATED_TYPES:
        url = view.get("url")
        if not isinstance(url, str) or not url.strip() or _is_challenge_url(url):
            return None
        paused_url = (
            str(pause_snapshot.get("url") or "").strip()
            if isinstance(pause_snapshot, dict)
            else ""
        )
        if paused_url and url.strip() == paused_url:
            return None
        if not paused_url:
            return None
        return "page_settled"

    return None


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


def _title_clears_challenge(title: Any, pause_snapshot: Optional[Dict[str, Any]]) -> bool:
    """True iff a post-pause title is strong evidence the challenge is gone:
    non-empty, no challenge/interstitial wording from the shared union
    vocabulary (covers lingering titles, "Attention Required! | Cloudflare",
    etc.), and (when the pre-pause snapshot is known) actually different from
    the paused title.
    """
    text = str(title or "").strip()
    if not text:
        return False
    if title_looks_like_challenge(text):
        return False
    if isinstance(pause_snapshot, dict):
        paused_title = str(pause_snapshot.get("title") or "").strip()
        if paused_title and paused_title == text:
            return False
    return True


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
) -> Dict[str, Any]:
    """Issue Hitl.resolvePause after verified settlement, then confirm.

    The panel never clears the pause flag on its own, so unlike the
    explicit-resume path we resolve first and only then poll Page.getState.
    A resolvePause rejected with a paused-state error is the deadlock case.
    """
    checks: List[Any] = []
    resolve_error_text = ""
    try:
        resolved = await browser.call(
            "Hitl.resolvePause",
            {"pageId": page_id, "purpose": VERIFIED_RESOLVE_PURPOSE},
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


def _state_looks_like_challenge_surface(state_response: Any) -> bool:
    if not isinstance(state_response, dict):
        return False
    body, data = _page_state_body(state_response)
    title = str(data.get("title") or "")
    url = str(data.get("url") or "")
    if title and title_looks_like_challenge(title):
        return True
    if _is_challenge_url(url):
        return True

    haystack_parts: List[str] = []
    for key in ("observation", "suggested_prompt"):
        value = body.get(key)
        if isinstance(value, str) and value:
            haystack_parts.append(value.lower())
    for key in ("title", "url", "status", "errorMessage"):
        value = data.get(key)
        if isinstance(value, str) and value:
            haystack_parts.append(value.lower())
    haystack = " ".join(haystack_parts)
    return any(marker in haystack for marker in _CHALLENGE_STATE_MARKERS)


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
        if (
            not _state_indicates_still_paused(state)
            and not _state_looks_like_challenge_surface(state)
        ):
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
        elif _state_looks_like_challenge_surface(state):
            last_blocker_reason = (
                "Page.getState no longer looks paused, but it still looks like"
                " a challenge surface."
            )
        elif _state_indicates_still_paused(state):
            last_blocker_reason = (
                "Page.getState did not confirm a usable unpaused page."
            )
    return {
        "status": PAGE_SETTLED_AFTER_HITL,
        "checks": checks,
        "reason": last_blocker_reason,
    }


async def wait_for_hitl_resume(
    *,
    browser: ABCPClient,
    page_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    diagnostics: Optional[WorkerDiagnostics],
    logger: Optional[RunLogger],
    challenge_verifier: Optional[
        Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
    ] = None,
    pause_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Block until the paused page resumes or `timeout_seconds` elapses.

    Resume can be established by an explicit Agent-visible HITL notification or
    by a verifier-backed lifecycle notification followed by Hitl.resolvePause.
    Page.getState is used only after such a notification to confirm settlement;
    it is never polled to infer that a missing resume notification occurred.
    `pause_snapshot` ({"url", "title"} captured at pause time) supplies the
    verifier comparison baseline.

    Returns a structured outcome consumed by the caller. BrowserAgent enriches
    the tool_result with the HITL wait outcome.
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
                "hasChallengeVerifier": challenge_verifier is not None,
            },
        )

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    started = deadline - max(0.0, timeout_seconds)
    strict_predicate = _make_resume_predicate_strict(page_id)

    def settlement_predicate(msg: Dict[str, Any]) -> bool:
        return _notification_signal(msg, page_id, pause_snapshot) is not None

    # Settlement triggers are only observed when a verifier can adjudicate
    # them; otherwise stay on the strict explicit-resume contract. The replay
    # scan is always strict: stale lifecycle events from before the pause must
    # never re-fire as settlement triggers.
    live_predicate = (
        settlement_predicate if challenge_verifier is not None else strict_predicate
    )

    async def via_notification() -> Optional[Dict[str, Any]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        msg = await browser.wait_for_notification(
            live_predicate,
            timeout=remaining,
            replay_window_seconds=5.0,
            replay_predicate=strict_predicate,
        )
        if msg is None:
            return None
        return {
            "signal": _notification_signal(msg, page_id, pause_snapshot),
            "message": msg,
        }

    # IMPORTANT: surface unexpected exceptions from the notification branch
    # instead of silently dropping them. A swallowed TypeError here previously masked a
    # signature mismatch between ABCPClient.wait_for_notification and the
    # underlying hub (the helper looked like it "timed out" in 30ms when in
    # fact the notification branch had blown up on an unknown kwarg).
    task_errors: List[Dict[str, Any]] = []
    verifier_calls = 0
    # Latches for the whole pause: once the oracle has seen a challenge, weaker
    # evidence may never release it.
    challenge_ever_confirmed = False
    last_settled_title = ""
    last_settled_url = ""
    settlement_rejections: List[Dict[str, Any]] = []

    def elapsed_ms_now() -> int:
        return int((time.monotonic() - started) * 1000)

    while True:
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

        if not isinstance(value, dict):
            if diagnostics is not None:
                diagnostics.mark_hitl_wait_timed_out()
            outcome = {
                "status": "timeout",
                "pageId": page_id,
                "elapsedMs": elapsed_ms_now(),
                "timeoutSeconds": timeout_seconds,
            }
            if task_errors:
                # A sub-millisecond "timeout" usually means a branch errored out
                # before doing real work. Surface it so callers know to look.
                outcome["branch_errors"] = task_errors
            if settlement_rejections:
                outcome["settlementRejections"] = settlement_rejections[-3:]
            if logger is not None:
                logger.write("hitl.wait.timeout", outcome)
            return outcome

        via = "notification"
        signal = str(value.get("signal") or "")
        message = value.get("message")
        evidence = message if isinstance(message, dict) else value

        if signal == "explicit_resume":
            confirmation = await _confirm_unpaused_after_settlement(
                browser=browser,
                page_id=page_id,
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
            )
            if confirmation.get("status") != "resumed":
                if confirmation.get("status") == STALE_PAUSE_DEADLOCK:
                    if diagnostics is not None:
                        diagnostics.mark_hitl_stale_pause_deadlock()
                    outcome = {
                        "status": STALE_PAUSE_DEADLOCK,
                        "via": via,
                        "pageId": page_id,
                        "elapsedMs": elapsed_ms_now(),
                        "deadlock": True,
                        "closed": bool(confirmation.get("closed")),
                        "evidence": {
                            "resume": _slim_evidence(evidence),
                            "confirmation": confirmation.get("checks", []),
                        },
                        "reason": confirmation.get("reason")
                        or "HITL pause could not be resolved after explicit resume.",
                    }
                    if logger is not None:
                        logger.write("hitl.wait.stale_pause_deadlock", outcome)
                    return outcome
                if diagnostics is not None:
                    diagnostics.mark_hitl_page_settled()
                confirmation_reason = str(confirmation.get("reason") or "").strip()
                outcome = {
                    "status": PAGE_SETTLED_AFTER_HITL,
                    "via": via,
                    "pageId": page_id,
                    "elapsedMs": elapsed_ms_now(),
                    "evidence": {
                        "settlement": _slim_evidence(evidence),
                        "confirmation": confirmation.get("checks", []),
                    },
                    "reason": (
                        "An explicit HITL resume was observed, but Page.getState still reports the"
                        f" page as paused: {confirmation_reason}"
                        if confirmation_reason
                        else (
                            "The page produced an explicit HITL resume signal, but"
                            " confirmation did not find a usable unpaused page."
                            " Wait for platform auto-recovery or restart the browser"
                            " phase with a fresh page."
                        )
                    ),
                }
                if logger is not None:
                    logger.write("hitl.wait.page_settled_after_hitl", outcome)
                return outcome
            if diagnostics is not None:
                diagnostics.mark_hitl_resumed()
            outcome = {
                "status": "resumed",
                "via": via,
                "pageId": page_id,
                "elapsedMs": elapsed_ms_now(),
                "evidence": _slim_evidence({
                    "settlement": _slim_evidence(evidence),
                    "confirmation": _slim_evidence(confirmation.get("state")),
                }),
            }
            if logger is not None:
                logger.write("hitl.wait.resumed", outcome)
            return outcome

        # Settlement signals on the paused pageId. Never a resume by
        # themselves: Page.titleUpdated only caches the candidate title as
        # evidence; Page.loaded or a post-pause Page.navigate with a changed
        # URL triggers adjudication (VL first, cached title as fallback), and
        # only a verified pass leads to resolvePause.
        if challenge_verifier is None:
            continue
        view = _notification_view(evidence)
        event_title = str(view.get("title") or "").strip()
        event_url = str(view.get("url") or "").strip()
        if event_title:
            last_settled_title = event_title
        if event_url:
            last_settled_url = event_url
        if str(view.get("type") or "") not in (_PAGE_LOADED_TYPES | _PAGE_NAVIGATED_TYPES):
            if logger is not None:
                logger.write("hitl.wait.settlement_check", {
                    "decision": "cached_evidence_only",
                    "pageId": page_id,
                    "event": str(view.get("type") or ""),
                    "title": last_settled_title[:200],
                    "url": last_settled_url[:300],
                })
            continue
        evidence_view: Dict[str, Any] = {
            "pageId": page_id,
            "event": str(view.get("type") or ""),
            "title": last_settled_title[:200],
            "url": last_settled_url[:300],
        }
        if isinstance(pause_snapshot, dict):
            evidence_view["pausedTitle"] = str(pause_snapshot.get("title") or "")[:200]
            evidence_view["pausedUrl"] = str(pause_snapshot.get("url") or "")[:300]

        decision = ""
        decision_info: Dict[str, Any] = {}
        verifier_ran = False
        if verifier_calls < _SETTLEMENT_VERIFIER_MAX_CALLS:
            verifier_calls += 1
            verifier_ran = True
            try:
                raw_verdict = await challenge_verifier(dict(evidence_view))
            except Exception as exc:
                raw_verdict = {
                    "verdict": "unavailable",
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:300],
                }
            if not isinstance(raw_verdict, dict):
                raw_verdict = {"verdict": str(raw_verdict)}
            verdict = str(raw_verdict.get("verdict") or "uncertain").strip().lower()
            decision_info = {
                "gate": "vl",
                "verdict": verdict,
                "reason": str(raw_verdict.get("reason") or "")[:300],
            }
            if verdict == "confirmed_challenge":
                decision = "challenge"
                challenge_ever_confirmed = True
            elif verdict in VL_CLEARANCE_VERDICTS:
                decision = "passed"
        if not decision:
            if challenge_ever_confirmed:
                # The oracle SAW the challenge during this pause. Spending the
                # verifier budget does not make that observation go away, and a
                # later title change is not evidence that a human cleared it —
                # resuming here would hand the worker a page the run already
                # knows is blocked. Only an explicit visual clearance, or the
                # human resolving the pause, may end it now.
                decision = "insufficient"
                decision_info = {
                    **decision_info,
                    "titleEvidenceSuppressed": True,
                    "titleEvidenceSuppressedReason": (
                        "a challenge was visually confirmed during this pause;"
                        " only visual clearance can release it"
                    ),
                }
            elif verifier_ran:
                # The visual oracle ran and could not confirm a clear. A title
                # is far too weak to overrule that on its own: a page can
                # retitle for any reason while the challenge is still up, and
                # the check cannot tell a real destination title from a
                # corrupted one. Keep waiting for an event the oracle can read
                # rather than resuming the worker onto an unverified page.
                decision = "insufficient"
                decision_info = {
                    **decision_info,
                    "titleEvidenceSuppressed": True,
                    "titleEvidenceSuppressedReason": (
                        "visual clearance was not confirmed; a title change"
                        " alone cannot release a HITL pause"
                    ),
                }
            elif _title_clears_challenge(last_settled_title, pause_snapshot):
                # Bounded escape hatch: the verifier budget is spent, so the
                # only remaining evidence is the title. Reaching here already
                # means several adjudications failed to confirm a challenge.
                decision = "passed"
                decision_info = {
                    **decision_info,
                    "gate": "title_evidence_after_verifier_budget",
                    "verifierCalls": verifier_calls,
                }
            else:
                decision = "insufficient"
        if logger is not None:
            logger.write("hitl.wait.settlement_check", {
                "decision": decision,
                **evidence_view,
                **decision_info,
            })
        if decision != "passed":
            settlement_rejections.append({
                "decision": decision,
                "event": evidence_view["event"],
                "title": evidence_view["title"],
                "url": evidence_view["url"],
                **decision_info,
            })
            continue

        resolution = await _resolve_after_verified_settlement(
            browser=browser,
            page_id=page_id,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
        )
        if resolution.get("status") == STALE_PAUSE_DEADLOCK:
            if diagnostics is not None:
                diagnostics.mark_hitl_stale_pause_deadlock()
            outcome = {
                "status": STALE_PAUSE_DEADLOCK,
                "via": via,
                "signal": "verified_settlement",
                "pageId": page_id,
                "elapsedMs": elapsed_ms_now(),
                "deadlock": True,
                "closed": bool(resolution.get("closed")),
                "evidence": {
                    "settlement": {**evidence_view, **decision_info},
                    "confirmation": resolution.get("checks", []),
                },
                "reason": resolution.get("reason")
                or "HITL pause could not be resolved after verified settlement.",
            }
            if logger is not None:
                logger.write("hitl.wait.stale_pause_deadlock", outcome)
            return outcome
        if resolution.get("status") != "resumed":
            if diagnostics is not None:
                diagnostics.mark_hitl_page_settled()
            resolution_reason = str(resolution.get("reason") or "").strip()
            outcome = {
                "status": PAGE_SETTLED_AFTER_HITL,
                "via": via,
                "signal": "verified_settlement",
                "pageId": page_id,
                "elapsedMs": elapsed_ms_now(),
                "evidence": {
                    "settlement": {**evidence_view, **decision_info},
                    "confirmation": resolution.get("checks", []),
                },
                "reason": (
                    "Settlement was verified and Hitl.resolvePause was issued,"
                    " but Page.getState still reports the page as paused:"
                    f" {resolution_reason}"
                    if resolution_reason
                    else (
                        "Settlement was verified and Hitl.resolvePause was"
                        " issued, but confirmation did not find a usable"
                        " unpaused page."
                    )
                ),
            }
            if logger is not None:
                logger.write("hitl.wait.page_settled_after_hitl", outcome)
            return outcome
        if diagnostics is not None:
            diagnostics.mark_hitl_resumed()
        outcome = {
            "status": "resumed",
            "via": via,
            "signal": "verified_settlement",
            "pageId": page_id,
            "elapsedMs": elapsed_ms_now(),
            "evidence": {
                "settlement": {**evidence_view, **decision_info},
                "confirmation": _slim_evidence(resolution.get("state")),
            },
        }
        if logger is not None:
            logger.write("hitl.wait.resumed", outcome)
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
