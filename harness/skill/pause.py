"""harness.skill.pause — P4 pause-resume as a hand-off to the slow path.

When a skill's frozen `Workflow.execute` is interrupted by a HITL/challenge
(Cloudflare, CAPTCHA, a `paused` progress event, an ERR_PAGE_PAUSED step), the
fast path cannot itself drive the resume: ABCPClient serializes every call behind
one `_call_lock` with a single `_pending_call`, so while `Workflow.execute` is
blocked at a paused step NO control call (`Workflow.pause/resume`,
`Hitl.resolvePause`) can proceed on that connection. Notifications, by contrast,
are demultiplexed through the NotificationHub independent of the lock.

So P4's contract here is: **observe** the pause (never drive it), classify the run
as `hitl_required`, and hand the live (paused) page off to the BrowserAgent slow
path — which already owns the proven VL-first + human-fallback HITL machinery
(harness/hitl.py). The paused page lives in the worker's slot fleet, so the slow
path perceives and resolves it. A HITL interruption is NOT a skill failure: it must
not record a health failure and must not trigger self-heal.

Detection has two independent legs (either ⇒ hand-off):
  1. live: an observe-only NotificationHub subscriber that records the first
     pause/challenge ONSET notification for the page during the blocked execute;
  2. post-hoc: the failed run's error/step text matches paused/challenge markers
     (ERR_PAGE_PAUSED / cloudflare / captcha / challenge url).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from harness.hitl import (
    _CHALLENGE_STATE_MARKERS,
    _is_challenge_url,
    _is_paused_error_text,
    _normalize_notification_type,
    _notification_view,
)

# Pause/HITL ONSET notification types. The workflow engine emits a `paused`
# progress event at the step boundary it stops on (engine.ts), and a HITL request
# surfaces a hitl/pause-flavored event. These are page-scoped OR page-less
# (a progress event may carry no pageId) — both count as onset.
_PAUSE_ONSET_TYPES = frozenset(_normalize_notification_type(item) for item in {
    "paused",
    "workflow_paused", "Workflow.paused",
    "hitl_paused", "Hitl.paused",
    "Hitl.requestPause", "hitl_requestpause",
    "Hitl.requested", "hitl_required",
    "human_intervention_required",
})


def _title_is_challenge(title: Any) -> bool:
    text = str(title or "").lower()
    return bool(text and any(marker in text for marker in _CHALLENGE_STATE_MARKERS))


def hitl_onset_signal(msg: Dict[str, Any], page_id: str) -> Optional[Dict[str, Any]]:
    """If `msg` is a pause/challenge ONSET for `page_id` (or a page-less pause
    progress event), return a compact signal dict; else None."""
    if not isinstance(msg, dict):
        return None
    view = _notification_view(msg)
    ntype = view.get("type") or ""
    if not ntype:
        return None
    msg_page_id = view.get("pageId")
    page_ok = (not msg_page_id) or str(msg_page_id) == str(page_id)

    if ntype in _PAUSE_ONSET_TYPES:
        return {"kind": "paused", "type": ntype, "pageId": msg_page_id,
                "url": view.get("url"), "title": view.get("title")}
    if page_ok and _is_challenge_url(view.get("url")):
        return {"kind": "challenge_url", "type": ntype, "pageId": msg_page_id,
                "url": view.get("url"), "title": view.get("title")}
    if page_ok and _title_is_challenge(view.get("title")):
        return {"kind": "challenge_title", "type": ntype, "pageId": msg_page_id,
                "url": view.get("url"), "title": view.get("title")}
    return None


class HitlOnsetMonitor:
    """Observe-only NotificationHub subscriber: records the FIRST pause/challenge
    onset signal seen for `page_id`. Safe to run concurrently with a blocked
    `Workflow.execute` (subscribers fire off the background reader, not through
    `_call_lock`). No-ops gracefully if `browser` has no subscription API."""

    def __init__(self, browser: Any, page_id: str):
        self._browser = browser
        self._page_id = str(page_id or "")
        self._unsubscribe: Optional[Callable[[], None]] = None
        self.signal: Optional[Dict[str, Any]] = None

    def _on_message(self, msg: Dict[str, Any]) -> None:
        if self.signal is not None:
            return
        try:
            sig = hitl_onset_signal(msg, self._page_id)
        except Exception:  # pragma: no cover - observer must never raise
            sig = None
        if sig is not None:
            self.signal = sig

    def start(self) -> "HitlOnsetMonitor":
        subscribe = getattr(self._browser, "subscribe_notifications", None)
        if callable(subscribe):
            try:
                self._unsubscribe = subscribe(self._on_message)
            except Exception:  # pragma: no cover - best-effort
                self._unsubscribe = None
        return self

    def stop(self) -> Optional[Dict[str, Any]]:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # pragma: no cover
                pass
            self._unsubscribe = None
        return self.signal

    def __enter__(self) -> "HitlOnsetMonitor":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def _failure_looks_like_pause(run_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Inspect a FAILED run for pause/challenge evidence in its error/step text."""
    failed_error = run_result.get("failedError")
    exc = run_result.get("exc")
    purpose = run_result.get("failedPurpose")
    for source in (failed_error, exc, purpose):
        text = str(source or "")
        if not text:
            continue
        low = text.lower()
        if _is_paused_error_text(text) or _is_challenge_url(low) or any(
            marker in low for marker in _CHALLENGE_STATE_MARKERS
        ):
            return {"kind": "paused_error", "evidence": text[:300],
                    "failedStepPath": run_result.get("failedStepPath")}
    return None


def classify_run_for_hitl(
    run_result: Dict[str, Any],
    observed_signal: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Decide whether a skill run was interrupted by HITL (⇒ hand off to slow path).

    A SUCCEEDED run is never a hand-off: any transient challenge must have cleared
    for the data to come back. A FAILED run is a hand-off iff a pause/challenge was
    observed live OR its failure text carries pause/challenge markers. A plain
    failure (selector miss, empty extract) is NOT a hand-off — it stays a genuine
    skill failure (health + self-heal eligible)."""
    if run_result.get("succeeded"):
        return None
    if observed_signal is not None:
        return observed_signal
    return _failure_looks_like_pause(run_result)
