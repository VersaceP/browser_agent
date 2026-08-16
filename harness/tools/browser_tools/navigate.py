"""
harness.tools.browser_tools.navigate - navigate_verified composite implementation.
"""

import asyncio
import re
import time
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from urllib.parse import urlparse
from abcp_client import ABCPTransportError
from harness.observation.challenge_detector import extract_page_id
from harness.observation.challenge_detector import is_lingering_loading_title
from harness.observation.content_completeness import ContentCompletenessTracker
from harness.observation.content_completeness import content_completeness_observation_facts
from harness.results.call_outcome import action_runtime_info
from harness.results.call_outcome import auto_hitl_is_actionable
from harness.results.call_outcome import classify_call_outcome
from harness.results.call_outcome import evaluate_grant
from harness.results.call_outcome import page_state_evidence_ok
from harness.observation.overlay_detector import detect_overlay_from_result
from harness.observation.overlay_detector import title_looks_like_auth_page
from harness.observation.page_lifecycle import AUTOMATION_UNAVAILABLE_FAILURE
from harness.observation.page_lifecycle import PageLifecycleTracker
from harness.observation.event_observer import unwrap_notification
from harness.utils import JsonDict
from harness.utils import optional_int
from harness.utils import trim_large_strings
from .axtree_state import _invalidate_axtree_snapshot

def _bt():
    import harness.tools.browser_tools as bt

    return bt

_URL_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

NAVIGATE_VERIFIED_DEFAULT_STATE_CHECKS = 5

NAVIGATE_VERIFIED_MAX_STATE_CHECKS = 10

NAVIGATE_VERIFIED_STATE_RECHECK_SECONDS = 0.5

_NAVIGATION_IN_FLIGHT_STATUSES = {"loading", "navigating", "pending"}

_NAVIGATION_FAILED_STATUSES = {"failed", "loadfailed", "load_failed", "crashed"}

def _normalize_url_for_equivalence(raw: str) -> str:
    """Canonicalize only the URL differences no server can distinguish.

    Scheme/host case and an explicit default port are erased, and an empty path
    becomes "/" so `https://x.com` and `https://x.com/` compare equal. Path,
    query (including its order) and fragment stay byte-exact: a redirect that
    rewrites the path or appends tracking parameters must still read as a
    mismatch, because it means the caller did not land where it asked to.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        # A malformed port makes the authority unparseable; comparing the raw
        # text is wrong-but-honest, whereas guessing an authority is not.
        return text
    if not host:
        return text
    scheme = parsed.scheme.lower()
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        userinfo = f"{userinfo}@"
    netloc = f"{userinfo}{host}"
    if port is not None and port != _URL_DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    rebuilt = f"{scheme}://{netloc}{parsed.path or '/'}"
    if parsed.query:
        rebuilt = f"{rebuilt}?{parsed.query}"
    if parsed.fragment:
        rebuilt = f"{rebuilt}#{parsed.fragment}"
    return rebuilt

def _make_url_matcher(
    url_re: Any,
    target_url: str,
) -> Callable[[str], bool]:
    """Return the URL acceptance test for one navigate_verified call.

    A caller-supplied regex is used verbatim. Without one the harness compares
    normalized URLs instead of synthesizing a regex: an unanchored `re.escape`
    pattern would accept `https://phish.example/?next=<target>`, and an anchored
    one rejects a bare trailing-slash difference the browser always adds.
    """
    if url_re is not None:
        return lambda actual: bool(url_re.search(actual or ""))
    expected = _normalize_url_for_equivalence(target_url)
    return lambda actual: _normalize_url_for_equivalence(actual) == expected

def _possible_double_escape(pattern: str, actual_url: str) -> Optional[JsonDict]:
    """Flag a caller pattern that fails ONLY because it looks over-escaped.

    All three conditions must hold together, so a legitimately escaped pattern
    that simply does not describe this page is never flagged: the original does
    not match, dropping one escaping layer still compiles, and the de-escaped
    form does match. The pattern is reported, never rewritten or applied — a
    syntactically valid regex belongs to its caller.
    """
    if not pattern or "\\\\" not in pattern or not actual_url:
        return None
    try:
        if re.compile(pattern).search(actual_url):
            return None
    except re.error:
        return None
    candidate = pattern.replace("\\\\", "\\")
    if candidate == pattern:
        return None
    try:
        candidate_re = re.compile(candidate)
    except re.error:
        return None
    if not candidate_re.search(actual_url):
        return None
    return {
        "code": "possible_double_escape",
        "expectedUrlPattern": pattern[:200],
        "deEscapedCandidate": candidate[:200],
        "detail": (
            "expectedUrlPattern fails only because it appears to carry an extra"
            " escaping layer. The harness did not rewrite or apply the"
            " candidate. This is a note about how to write the pattern next"
            " time — it is NOT a reason to re-navigate to this URL, which has"
            " already loaded."
        ),
    }

async def _navigate_verified(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    """Navigate once and report what was actually observed.

    The audit fields are merged onto whatever terminal receipt the
    implementation returns, so `navigateDispatchCount` is present and truthful
    on EVERY branch — including the early input rejections, HITL handoffs, and
    AX-refresh failures that each build their own dict.
    """
    audit: JsonDict = {"navigateDispatchCount": 0}
    result = await _navigate_verified_impl(agent, tool_input, step, audit)
    if isinstance(result, dict):
        result.update(audit)
    return result

async def _navigate_verified_impl(
    agent: Any,
    tool_input: JsonDict,
    step: int,
    audit: JsonDict,
) -> JsonDict:
    page_id = str(tool_input.get("pageId") or "").strip()
    url = str(tool_input.get("url") or "").strip()
    expected_url_pattern = str(tool_input.get("expectedUrlPattern") or "").strip()
    expected_title_pattern = str(tool_input.get("expectedTitlePattern") or "").strip()
    timeout_seconds = max(1.0, min(float(tool_input.get("timeoutSeconds") or 20.0), 120.0))
    # `maxRetries` used to multiply Page.navigate dispatches, so a caller
    # expectation that could never match spent N real requests on a page that
    # had already arrived. It now only bounds read-side redirect settlement,
    # under its new name; the legacy key keeps working and says so in the receipt.
    legacy_retries = optional_int(tool_input.get("maxRetries"), None)
    max_state_checks = optional_int(
        tool_input.get("maxStateChecks"),
        legacy_retries if legacy_retries is not None else NAVIGATE_VERIFIED_DEFAULT_STATE_CHECKS,
    )
    max_state_checks = max(
        1,
        min(
            max_state_checks or NAVIGATE_VERIFIED_DEFAULT_STATE_CHECKS,
            NAVIGATE_VERIFIED_MAX_STATE_CHECKS,
        ),
    )
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not url:
        return {"status": "failed", "error": "url is required"}

    # Compile before dispatching. An expectation that cannot compile can never
    # be satisfied, so navigating first would spend a real request on a call
    # that is already doomed.
    url_re = None
    if expected_url_pattern:
        try:
            url_re = re.compile(expected_url_pattern)
        except re.error as exc:
            return _navigate_pattern_invalid_result(
                page_id=page_id,
                field="expectedUrlPattern",
                pattern=expected_url_pattern,
                error=str(exc),
            )
    title_re = None
    if expected_title_pattern:
        try:
            title_re = re.compile(expected_title_pattern)
        except re.error as exc:
            return _navigate_pattern_invalid_result(
                page_id=page_id,
                field="expectedTitlePattern",
                pattern=expected_title_pattern,
                error=str(exc),
            )

    url_matches = _make_url_matcher(url_re, url)
    expectation_mode = "caller_regex" if url_re is not None else "normalized_url_equality"
    attempts: List[JsonDict] = []
    state_resync_count = 0
    last_challenge_summary: JsonDict = {}
    audit["urlExpectationMode"] = expectation_mode
    audit["maxStateChecks"] = max_state_checks
    if legacy_retries is not None and tool_input.get("maxStateChecks") is None:
        audit["maxRetriesInterpretedAs"] = "state_checks"

    # Exactly one Page.navigate per call, unconditionally. A failed expectation
    # is not a failed navigation, and this composite must never hide a second
    # request from the model that authorized one.
    attempt = 1
    deadline = time.monotonic() + timeout_seconds
    nav = await _bt()._invoke_browser_method(
        agent,
        "Page.navigate",
        {
            "pageId": page_id,
            "url": url,
            "purpose": "Navigate and verify URL",
        },
        step,
        count_progress=False,
    )
    # The count is what actually reached transport, not what this composite
    # intended. A pre-dispatch guard answers `tool_was_executed=False` without
    # the panel ever seeing the call, and reporting 1 there would contradict
    # the `navigation_not_dispatched` status sitting beside it.
    if nav.get("tool_was_executed") is not False:
        audit["navigateDispatchCount"] = 1
    if _result_has_auto_hitl(nav):
        return _navigate_hitl_result(page_id, attempt, nav)
    if _bt()._invoke_result_failed(nav):
        return await _navigate_dispatch_failure_result(
            agent,
            page_id=page_id,
            url=url,
            nav=nav,
            step=step,
        )
    last_challenge_summary = _page_challenge_summary(agent, page_id)
    tracker = getattr(agent, "page_lifecycle", None)
    settlement = "unknown"
    if isinstance(tracker, PageLifecycleTracker):
        settlement = await tracker.wait_for_settlement(
            page_id,
            max(0.0, deadline - time.monotonic()),
        )
    redirect_settlements = 0
    state_checks_used = 0
    state_read_failed = False
    last_state: JsonDict = {}
    while True:
        # ONE budget for every Page.getState this settlement loop issues,
        # whichever path asked for it. Two separate counters let a redirect
        # keep granting reads that the recheck budget had already refused.
        if state_checks_used >= max_state_checks:
            break
        # Register the fresh settlement waiter before Page.getState so a
        # redirect that starts/finishes during the RPC cannot fall through
        # the gap. This is event-driven redirect tolerance, not polling.
        remaining = max(0.0, deadline - time.monotonic())
        redirect_waiter = None
        if state_checks_used + 1 < max_state_checks and remaining > 0:
            redirect_waiter = _fresh_page_settlement_task(
                agent, page_id, remaining
            )
        state_result = await _bt()._invoke_browser_method(
            agent,
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "Synchronize state once after navigation settlement",
            },
            step,
            count_progress=False,
        )
        state_resync_count += 1
        state_checks_used += 1
        if _result_has_auto_hitl(state_result):
            await _cancel_waiter(redirect_waiter)
            return _navigate_hitl_result(page_id, attempt, state_result)
        # A failed read yields an empty snapshot, which looks exactly like "the
        # page is at about:blank with no title". Remember that the state is
        # unknown so the terminal branch cannot report it as an arrival.
        state_outcome = classify_call_outcome(state_result)
        state_read_failed = not (
            state_outcome.succeeded
            and page_state_evidence_ok(page_id, state_result)
        )
        last_state = _navigation_state_snapshot(
            _bt()._response_data(state_result),
            url_matches=url_matches,
            title_re=title_re,
            settlement=settlement,
            redirect_settlements=redirect_settlements,
        )
        current_url = str(last_state.get("url") or "")
        title = str(last_state.get("title") or "")
        status = str(last_state.get("status") or "")
        title_is_lingering = bool(last_state.get("titleLingering"))
        url_ok = bool(last_state.get("urlOk"))
        title_ok = bool(last_state.get("titleOk"))
        last_challenge_summary = _page_challenge_summary(agent, page_id)
        # A matching URL/title is not arrival on a tab that is still fetching
        # or that reported a failed load. The harness's own doctrine forbids
        # DOM probes before settlement, so `done` in either state would
        # contradict the instruction the model is given.
        if (
            url_ok
            and title_ok
            and not title_is_lingering
            and not state_read_failed
            and status not in _NAVIGATION_IN_FLIGHT_STATUSES
            and status not in _NAVIGATION_FAILED_STATUSES
        ):
            await _cancel_waiter(redirect_waiter)
            # Page.navigate invalidates DOM identity. Refresh the AXTree before
            # returning so callers cannot inherit a clean-looking stale cache.
            # AX refresh failure is not navigation failure: retry only the
            # perception leg, never Page.navigate, after URL/title are proven.
            tree_result, tree_attempts, ax_state_resyncs, ax_latest_state = (
                await _refresh_axtree_after_verified_navigation(
                    agent,
                    page_id=page_id,
                    step=step,
                    deadline=deadline,
                    url_matches=url_matches,
                    title_re=title_re,
                )
            )
            state_resync_count += ax_state_resyncs
            if _result_has_auto_hitl(tree_result):
                return _navigate_hitl_result(page_id, attempt, tree_result)
            if isinstance(ax_latest_state, dict):
                if tree_result.get("status") == "navigation_redirected_during_ax_refresh":
                    return {
                        "status": "navigation_redirected_during_ax_refresh",
                        "error": (
                            "page URL/title changed after navigation was"
                            " verified and before AX refresh completed"
                        ),
                        "pageId": page_id,
                        "url": ax_latest_state.get("url"),
                        "title": ax_latest_state.get("title"),
                        "pageStatus": ax_latest_state.get("status"),
                        "attempt": attempt,
                        "navigationVerified": False,
                        "previousVerifiedState": last_state,
                        "currentState": ax_latest_state,
                        "stateResyncCount": state_resync_count,
                        "redirectSettlementCount": redirect_settlements,
                        "axtreeRefreshed": False,
                        "axtreeRefreshAttempts": len(tree_attempts),
                        "axtreeRefreshResults": tree_attempts,
                        "suspectedChallenge": (
                            _page_challenge_summary(agent, page_id) or None
                        ),
                        "next_instruction": (
                            "Do not report the earlier navigation as verified"
                            " and do not guess the new page's meaning. Inspect"
                            " the reported current URL/title and recover or"
                            " re-verify from the current page state."
                        ),
                    }
                last_state = ax_latest_state
                current_url = str(last_state.get("url") or "")
                title = str(last_state.get("title") or "")
                status = str(last_state.get("status") or "")
            if tree_result.get("status") == "navigation_state_resync_failed_during_ax":
                return {
                    "status": "navigation_verified_state_resync_failed",
                    "error": (
                        "navigation URL/title were verified, but page state"
                        " resynchronization failed during AX refresh"
                    ),
                    "pageId": page_id,
                    "url": current_url,
                    "title": title,
                    "pageStatus": status,
                    "attempt": attempt,
                    "navigationVerified": True,
                    "state": last_state,
                    "stateResyncCount": state_resync_count,
                    "axtreeRefreshed": bool(
                        tree_result.get("axtreeRefreshed")
                    ),
                    "axtreeRefreshAttempts": len(tree_attempts),
                    "axtreeRefreshResults": tree_attempts,
                    "next_instruction": (
                        "Do NOT call navigate_verified again for this"
                        " navigation. Complete the required Page.getState"
                        " resynchronization on this page before issuing"
                        " dependent page actions."
                    ),
                }
            if _bt()._invoke_result_failed(tree_result):
                attempt_receipt = {
                    "attempt": attempt,
                    "lastState": last_state,
                    "axtreeRefreshAttempts": len(tree_attempts),
                    "axtreeRefreshResults": tree_attempts,
                }
                attempts.append(attempt_receipt)
                last_challenge_summary = _page_challenge_summary(agent, page_id)
                if _challenge_score(last_challenge_summary) >= 80:
                    return _navigate_challenge_blocked_result(
                        page_id=page_id,
                        attempt=attempt,
                        last_state=last_state,
                        attempts=attempts,
                        state_resync_count=state_resync_count,
                        challenge_summary=last_challenge_summary,
                        expected_url_pattern=expected_url_pattern,
                        expected_title_pattern=expected_title_pattern,
                        trigger="verified_navigation_ax_refresh_failed_with_challenge",
                    )
                return {
                    "status": "navigation_verified_ax_refresh_failed",
                    "error": (
                        "navigation URL/title were verified, but the fresh"
                        " AXTree could not be obtained"
                    ),
                    "pageId": page_id,
                    "url": current_url,
                    "title": title,
                    "pageStatus": status,
                    "attempt": attempt,
                    "navigationVerified": True,
                    "navigateResult": _strip_challenge_fields(nav),
                    "state": last_state,
                    "stateResyncCount": state_resync_count,
                    "redirectSettlementCount": redirect_settlements,
                    "axtreeRefreshed": False,
                    "axtreeRefreshAttempts": len(tree_attempts),
                    "axtreeRefreshResults": tree_attempts,
                    "next_instruction": (
                        "Do NOT call navigate_verified again: the target URL"
                        " and title are already verified. Recover the current"
                        " renderer/page if needed, then retry DOM.getAXTree on"
                        " this pageId."
                    ),
                }
            _clear_navigation_challenge_state(agent, page_id)
            return {
                "status": "done",
                "pageId": page_id,
                "url": current_url,
                "title": title,
                "pageStatus": status,
                "attempt": attempt,
                "navigationCommitted": True,
                "navigateResult": _strip_challenge_fields(nav),
                "state": last_state,
                "stateResyncCount": state_resync_count,
                "redirectSettlementCount": redirect_settlements,
                "axtreeRefreshed": True,
                "axtreeRefreshAttempts": len(tree_attempts),
                "axtreeRefreshResults": tree_attempts,
            }
        settlement_event = (
            await redirect_waiter if redirect_waiter is not None else None
        )
        if settlement_event is not None:
            redirect_settlements += 1
            settlement = str(settlement_event.get("event") or "redirect_settled")
            continue
        # No settlement event arrived, but a page that is still loading or still
        # showing an interstitial title has not finished arriving. Re-read its
        # state instead of declaring a mismatch: Page.getState never touches the
        # site, unlike the Page.navigate replay this loop used to fall back on.
        if (
            state_checks_used < max_state_checks
            and time.monotonic() < deadline
            and (
                state_read_failed
                or title_is_lingering
                or status in _NAVIGATION_IN_FLIGHT_STATUSES
            )
        ):
            settlement = "state_recheck"
            await asyncio.sleep(NAVIGATE_VERIFIED_STATE_RECHECK_SECONDS)
            continue
        break
    attempts.append({"attempt": attempt, "lastState": last_state})

    if _challenge_score(last_challenge_summary) >= 80:
        return _navigate_challenge_blocked_result(
            page_id=page_id,
            attempt=attempt,
            last_state=attempts[-1].get("lastState", {}) if attempts else {},
            attempts=attempts,
            state_resync_count=state_resync_count,
            challenge_summary=last_challenge_summary,
            expected_url_pattern=expected_url_pattern,
            expected_title_pattern=expected_title_pattern,
            trigger="navigation_verification_exhausted_with_challenge",
        )

    # Verification did not pass. "The page arrived but your pattern was wrong"
    # is only ONE of the reasons that can happen, and it is the only one that
    # licenses the model to keep working from this page. Claiming it when the
    # state was unreadable, still loading, or reported a load failure would put
    # a fact in the receipt that the harness never observed.
    actual_url = str(last_state.get("url") or "")
    actual_title = str(last_state.get("title") or "")
    page_status = str(last_state.get("status") or "")
    lifecycle_state = (
        tracker.state(page_id)
        if isinstance(tracker, PageLifecycleTracker)
        else None
    )
    lifecycle_status = (
        str(getattr(lifecycle_state, "status", "") or "")
        if lifecycle_state is not None
        else ""
    )
    common: JsonDict = {
        "tool_was_executed": True,
        "pageId": page_id,
        "requestedUrl": url,
        "actualUrl": actual_url,
        "actualTitle": actual_title,
        "pageStatus": page_status,
        "lifecycleStatus": lifecycle_status or None,
        "expectedUrlPattern": expected_url_pattern or None,
        "expectedTitlePattern": expected_title_pattern or None,
        "attempts": attempts,
        "stateResyncCount": state_resync_count,
        "suspectedChallenge": last_challenge_summary or None,
    }

    if state_read_failed:
        return {
            **common,
            "status": "navigation_outcome_unknown",
            "navigationCommitted": None,
            "reason": "state_unreadable",
            "error": "Page.getState did not return a readable state",
            "next_instruction": (
                "The navigation was dispatched but the page state could not be"
                " read, so where the page landed is unknown. Do NOT call"
                " navigate_verified again for this navigation, and do not treat"
                " actualUrl as observed: recover the page or re-read its state"
                " with Page.getState first."
            ),
        }

    if lifecycle_status in {"failed", "crashed"} or page_status in _NAVIGATION_FAILED_STATUSES:
        return {
            **common,
            "status": "navigation_load_failed",
            "navigationCommitted": False,
            "error": f"page reported a failed load (status={page_status or lifecycle_status})",
            "next_instruction": (
                "The browser received the navigation and the page failed to"
                " load. Inspect the failure before deciding whether a retry is"
                " warranted; this composite will not re-dispatch it for you."
            ),
        }

    if bool(last_state.get("titleLingering")) or page_status in _NAVIGATION_IN_FLIGHT_STATUSES:
        return {
            **common,
            "status": "navigation_settlement_incomplete",
            "navigationCommitted": True,
            "titleLingering": bool(last_state.get("titleLingering")),
            "next_instruction": (
                "The navigation committed but the page had not finished"
                " settling when the read budget ran out. Do NOT call"
                " navigate_verified again for this navigation — that would"
                " re-request the URL. Call Page.getState once to see whether it"
                " settled, and do not treat actualTitle as final until it has."
            ),
        }

    result: JsonDict = {
        **common,
        "status": "navigation_arrived_expectation_mismatch",
        "navigationCommitted": True,
        "urlOk": bool(last_state.get("urlOk")),
        "titleOk": bool(last_state.get("titleOk")),
        "titleLingering": False,
        "next_instruction": (
            "The browser reached actualUrl/actualTitle; only the expectation"
            " failed. Do NOT call navigate_verified again for this navigation:"
            " the page is already here, so continue read-only with"
            " Page.getState/DOM.getAXTree. Apply any corrected expectation only"
            " to a future, genuinely different navigation."
        ),
    }
    suspect = _possible_double_escape(expected_url_pattern, actual_url)
    if suspect:
        result["expectationPatternSuspect"] = suspect
    return result

NAVIGATE_VERIFIED_AX_REFRESH_MAX_ATTEMPTS = 3

def _navigation_state_snapshot(
    data: Any,
    *,
    url_matches: Callable[[str], bool],
    title_re: Any,
    settlement: str,
    redirect_settlements: int,
) -> JsonDict:
    data = data if isinstance(data, dict) else {}
    current_url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    return {
        "url": current_url,
        "title": title,
        "status": str(data.get("status") or ""),
        "urlOk": bool(url_matches(current_url)),
        "titleOk": True if title_re is None else bool(title_re.search(title)),
        "titleLingering": is_lingering_loading_title(title),
        "settlement": settlement,
        "redirectSettlements": redirect_settlements,
    }

async def _refresh_axtree_after_verified_navigation(
    agent: Any,
    *,
    page_id: str,
    step: int,
    deadline: float,
    url_matches: Callable[[str], bool],
    title_re: Any,
) -> Tuple[JsonDict, List[JsonDict], int, Optional[JsonDict]]:
    """Refresh post-navigation DOM identity without replaying navigation.

    ``Page.navigate`` may already have committed even when AX collection hits a
    transient renderer/lifecycle failure. Replaying it can duplicate side
    effects and restart loading. Keep this recovery leg bounded by the original
    navigation attempt deadline and retry only state synchronization/AX.
    """
    attempts: List[JsonDict] = []
    state_resync_count = 0
    latest_state: Optional[JsonDict] = None
    last_result: JsonDict = {
        "status": "axtree_refresh_deadline_exhausted",
        "tool_was_executed": False,
    }
    force_next_ax = False
    for ax_attempt in range(1, NAVIGATE_VERIFIED_AX_REFRESH_MAX_ATTEMPTS + 1):
        # The first AX refresh is a required consistency check after navigation,
        # even when Page.navigate/settlement consumed the nominal deadline. Only
        # tolerance retries (attempts 2-3) are suppressed after budget expiry.
        if ax_attempt > 1 and time.monotonic() >= deadline and not force_next_ax:
            break
        force_next_ax = False
        tracker = getattr(agent, "page_lifecycle", None)
        lifecycle_before = (
            tracker.state(page_id)
            if isinstance(tracker, PageLifecycleTracker)
            else None
        )
        generation_before = (
            lifecycle_before.generation if lifecycle_before is not None else None
        )
        tree_result = await _bt()._invoke_browser_method(
            agent,
            "DOM.getAXTree",
            {
                "pageId": page_id,
                "purpose": (
                    "Refresh DOM identity after verified navigation"
                    f" (AX attempt {ax_attempt})"
                ),
            },
            step,
            count_progress=False,
        )
        last_result = tree_result
        attempt_receipt: JsonDict = {"attempt": ax_attempt, "result": tree_result}
        attempts.append(attempt_receipt)
        if _result_has_auto_hitl(tree_result):
            return tree_result, attempts, state_resync_count, latest_state

        # A redirect/recovery can begin between the verified Page.getState and
        # the AX RPC. Discharge only the newly raised state-resync obligation;
        # never convert it into another Page.navigate attempt.
        lifecycle_state = (
            tracker.state(page_id)
            if isinstance(tracker, PageLifecycleTracker)
            else None
        )
        generation_changed = bool(
            lifecycle_state is not None
            and generation_before is not None
            and lifecycle_state.generation != generation_before
        )
        crashed = bool(
            lifecycle_state is not None
            and (
                lifecycle_state.status == "crashed"
                or lifecycle_state.last_event == "Page.crashed"
            )
        )
        identity_invalidated = bool(generation_changed or crashed)
        state_resync_required = bool(
            lifecycle_state is not None
            and lifecycle_state.requires_state_resync
        )
        tree_failed = _bt()._invoke_result_failed(tree_result)
        if not tree_failed and not identity_invalidated and not state_resync_required:
            return tree_result, attempts, state_resync_count, latest_state
        if identity_invalidated:
            # Even a successful AX response is stale when navigation generation
            # changed (or the renderer crashed) during the RPC. Quarantine it
            # and require a new AX after state synchronization; never combine
            # old-tree evidence with the new page's URL/title.
            quarantine_reason = (
                "page_generation_changed_during_ax"
                if generation_changed
                else "page_crashed_during_ax"
            )
            attempt_receipt["quarantined"] = quarantine_reason
            if isinstance(tracker, PageLifecycleTracker):
                tracker.invalidate_ax_refresh(page_id)
            _invalidate_axtree_snapshot(
                agent,
                "navigate_verified.ax_identity_invalidated",
                {"pageId": page_id},
            )
            last_result = {
                "status": "axtree_refresh_invalidated_by_navigation",
                "tool_was_executed": False,
            }
        if state_resync_required:
            state_result = await _bt()._invoke_browser_method(
                agent,
                "Page.getState",
                {
                    "pageId": page_id,
                    "purpose": (
                        "Synchronize state after post-navigation AX refresh failure"
                    ),
                },
                step,
                count_progress=False,
            )
            state_resync_count += 1
            if _result_has_auto_hitl(state_result):
                return state_result, attempts, state_resync_count, latest_state
            state_outcome = classify_call_outcome(state_result)
            if not (
                state_outcome.succeeded
                and page_state_evidence_ok(page_id, state_result)
            ):
                return (
                    {
                        "status": "navigation_state_resync_failed_during_ax",
                        "tool_was_executed": False,
                        "axtreeRefreshed": bool(
                            not tree_failed and not identity_invalidated
                        ),
                    },
                    attempts,
                    state_resync_count,
                    latest_state,
                )
            latest_state = _navigation_state_snapshot(
                _bt()._response_data(state_result),
                url_matches=url_matches,
                title_re=title_re,
                settlement="ax_refresh_state_resync",
                redirect_settlements=0,
            )
            if (
                not latest_state.get("urlOk")
                or not latest_state.get("titleOk")
                or latest_state.get("titleLingering")
            ):
                if not identity_invalidated:
                    attempt_receipt["quarantined"] = (
                        "page_state_mismatch_during_ax"
                    )
                    if isinstance(tracker, PageLifecycleTracker):
                        tracker.invalidate_ax_refresh(page_id)
                    _invalidate_axtree_snapshot(
                        agent,
                        "navigate_verified.ax_state_mismatch",
                        {"pageId": page_id},
                    )
                return (
                    {
                        "status": "navigation_redirected_during_ax_refresh",
                        "tool_was_executed": False,
                    },
                    attempts,
                    state_resync_count,
                    latest_state,
                )
            if not tree_failed and not identity_invalidated:
                # Dialog/chooser/download events require state synchronization
                # but do not invalidate DOM identity. Keep the successful AX and
                # return without an unnecessary replacement AX RPC.
                return tree_result, attempts, state_resync_count, latest_state
            # The preceding AX failed or belongs to the previous lifecycle
            # generation. Its replacement is a mandatory consistency check, not
            # a tolerance retry, so it gets one bounded attempt past deadline.
            force_next_ax = True
    return last_result, attempts, state_resync_count, latest_state

def _fresh_page_settlement_task(
    agent: Any,
    page_id: str,
    timeout_seconds: float,
) -> Optional["asyncio.Task[Optional[JsonDict]]"]:
    waiter = getattr(getattr(agent, "browser", None), "wait_for_notification", None)
    if not callable(waiter):
        return None

    def predicate(message: JsonDict) -> bool:
        event = unwrap_notification(message)
        if event is None or str(event.get("event") or "") not in {
            "Page.loaded", "Page.loadFailed", "Page.crashed",
        }:
            return False
        payload = event.get("payload")
        return bool(
            isinstance(payload, dict)
            and str(payload.get("pageId") or "") == page_id
        )

    async def wait() -> Optional[JsonDict]:
        try:
            message = await waiter(predicate, timeout=max(0.0, timeout_seconds))
        except TypeError:
            message = await waiter(predicate, max(0.0, timeout_seconds))
        return unwrap_notification(message)

    return asyncio.create_task(wait())

async def _cancel_waiter(waiter: Optional["asyncio.Task[Any]"]) -> None:
    if waiter is None:
        return
    if not waiter.done():
        waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass

def _page_challenge_summary(agent: Any, page_id: str) -> JsonDict:
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if tracker is not None and page_id else None
    return state.to_summary() if state is not None else {}

def _ensure_content_completeness_tracker(
    agent: Any,
) -> Optional[ContentCompletenessTracker]:
    """Install the worker's normalized completeness contract when needed."""
    contract = getattr(agent, "worker_contract", None)
    config = (
        contract.get("content_completeness")
        if isinstance(contract, dict) else None
    )
    config_source = (
        str(contract.get("content_completeness_source") or "explicit")
        if isinstance(contract, dict) else "explicit"
    )
    tracker = getattr(agent, "content_completeness_tracker", None)
    if tracker is None or (not tracker.enabled and bool(config)):
        tracker = ContentCompletenessTracker(
            config,
            config_source=config_source,
        )
        agent.content_completeness_tracker = tracker
    return tracker

def _observe_content_completeness_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
    *,
    content_binding: Any = None,
) -> JsonDict:
    contract = getattr(agent, "worker_contract", None)
    tracker = _ensure_content_completeness_tracker(agent)
    if tracker is None or not tracker.enabled:
        return result
    if hasattr(tracker, "observe_auth_generation"):
        tracker.observe_auth_generation(
            getattr(agent, "fleet_barrier_generation", 0)
        )
    upstream_blocker = _content_completeness_upstream_blocker(
        agent,
        method,
        params,
        result,
    )
    summary = tracker.observe(
        method=method,
        params=params,
        result=result,
        step=step,
        upstream_blocker=upstream_blocker,
    )
    binding_receipt = tracker.observe_content_binding(
        method=method,
        params=params,
        result=result,
        binding=content_binding,
    ) if isinstance(content_binding, dict) else None
    if isinstance(binding_receipt, dict) and binding_receipt.get("status") in {
        "accepted", "unchanged",
    }:
        binding_page_id = str(
            params.get("pageId") if isinstance(params, dict) else ""
        )
        binding_state = getattr(tracker, "pages", {}).get(binding_page_id)
        if binding_state is not None:
            summary = binding_state.summary()
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        phase_id = str(contract.get("phase_id") or "") if isinstance(contract, dict) else ""
        for telemetry in tracker.drain_telemetry_events():
            event_name = str(telemetry.pop("event", "") or "")
            if not event_name:
                continue
            payload = {"phaseId": phase_id or None, **telemetry}
            for key in ("sourceUrl", "targetUrl"):
                raw_url = str(payload.get(key) or "")
                if not raw_url:
                    continue
                try:
                    parsed = urlparse(raw_url)
                    payload[key] = (
                        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                        if parsed.scheme and parsed.netloc else parsed.path
                    )
                except ValueError:
                    payload[key] = raw_url.split("?", 1)[0]
            logger.write(event_name, payload)
    if not isinstance(summary, dict):
        return result
    enriched = dict(result)
    enriched["contentCompleteness"] = content_completeness_observation_facts(
        summary
    )
    if isinstance(binding_receipt, dict):
        enriched["contentBinding"] = binding_receipt
        if binding_receipt.get("status") == "rejected":
            existing_next_step = str(enriched.get("next_step") or "").strip()
            enriched["next_step"] = " ".join(value for value in (
                existing_next_step,
                "Use content_binding.regionId from the declared"
                " content_completeness expected regions, or omit the binding.",
            ) if value)
    binding_instruction = str(
        summary.get("collectionBindingNextInstruction") or ""
    ).strip()
    if binding_instruction:
        existing_next_step = str(enriched.get("next_step") or "").strip()
        enriched["next_step"] = " ".join(
            value for value in (
                existing_next_step,
                binding_instruction,
            ) if value
        )
    if logger is not None and hasattr(logger, "write"):
        logger.write("content_completeness.observed", summary)
    return enriched

def _content_completeness_upstream_blocker(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> str:
    """Return an existing higher-priority page classification, if any.

    Content completeness must not reinterpret authentication, challenge,
    lifecycle, navigation, or infrastructure failures as route-sensitive
    suppression.  Vocabulary remains owned by the dedicated detectors; this
    adapter consumes their structured receipts only.
    """
    if _bt()._invoke_result_failed(result):
        classification = (
            result.get("errorClassification")
            if isinstance(result.get("errorClassification"), dict) else {}
        )
        kind = str(classification.get("type") or "browser_call_failed").strip()
        return f"error:{kind}"

    page_id = extract_page_id(params, result)
    data = _bt()._response_data(result)
    hitl = data.get("hitl") if isinstance(data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True or isinstance(result.get("pausedState"), dict):
        return "hitl_paused"

    if method == "collect_items" and str(result.get("collectionState") or "") == "blocked":
        overlay_receipt = (
            result.get("overlayEncountered")
            if isinstance(result.get("overlayEncountered"), dict) else {}
        )
        overlay_subtype = str(overlay_receipt.get("subtype") or "").strip()
        if overlay_subtype:
            return f"overlay:{overlay_subtype}"
        stop_reason = str(result.get("stopReason") or "").strip()
        if stop_reason in {"overlay_blocked", "overlay_unresolved"}:
            return f"overlay:{stop_reason.removeprefix('overlay_')}"

    lifecycle = getattr(agent, "page_lifecycle", None)
    lifecycle_state = (
        lifecycle.state(page_id)
        if lifecycle is not None and page_id and hasattr(lifecycle, "state")
        else None
    )
    lifecycle_status = str(getattr(lifecycle_state, "status", "") or "").lower()
    if lifecycle_status in {"loading", "failed", "crashed"}:
        return f"lifecycle:{lifecycle_status}"

    status = str(data.get("status") or "").strip().lower().replace("_", "")
    if status in {"loading", "navigating", "startedloading"}:
        return "lifecycle:loading"
    if status in {"failed", "loadfailed", "error", "crashed"}:
        return f"lifecycle:{status}"

    navigation_check = (
        result.get("navigationCheck")
        if isinstance(result.get("navigationCheck"), dict) else {}
    )
    navigation_status = str(navigation_check.get("status") or "")
    if navigation_status == "challenge_pending":
        return "challenge:navigation"
    if navigation_status == "off_target":
        return "navigation:off_target"

    if isinstance(result.get("structuralChallenge"), dict):
        return "challenge:structural"
    auto_hitl = result.get("autoHitl")
    if isinstance(auto_hitl, dict) and _auto_hitl_is_actionable(auto_hitl):
        return "challenge:hitl"
    challenge = _page_challenge_summary(agent, page_id)
    tracker = getattr(agent, "challenge_tracker", None)
    threshold = int(getattr(tracker, "threshold", 70) or 70)
    if (
        challenge.get("structuralChallenge")
        or challenge.get("highConfidenceHit")
        or _challenge_score(challenge) >= threshold
    ):
        return "challenge:detected"

    overlay = detect_overlay_from_result(result)
    subtype = str((overlay or {}).get("subtype") or "")
    if subtype in {"auth_prompt", "paywall"}:
        return f"overlay:{subtype}"
    # DOM responses do not always repeat the document title.  Reuse the title
    # most recently recorded by the completeness tracker, but classify it via
    # the dedicated auth detector rather than adding auth vocabulary here.
    content_tracker = getattr(agent, "content_completeness_tracker", None)
    content_state = (
        content_tracker.pages.get(page_id)
        if content_tracker is not None
        and isinstance(getattr(content_tracker, "pages", None), dict)
        and page_id
        else None
    )
    remembered_title = str(getattr(content_state, "title", "") or "")
    if title_looks_like_auth_page(remembered_title):
        return "overlay:auth_prompt"
    return ""

def _challenge_score(summary: JsonDict) -> int:
    try:
        return int(summary.get("suspicionScore") or 0)
    except (TypeError, ValueError):
        return 0

def _clear_navigation_challenge_state(agent: Any, page_id: str) -> None:
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is not None and page_id:
        tracker.clear_page(page_id)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("challenge.navigation_cleared", {"pageId": page_id})

def _notify_navigation_success(
    agent: Any,
    page_id: str,
    *,
    navigation_kind: str = "verified",
) -> Optional[JsonDict]:
    progress = getattr(agent, "progress", None)
    if progress is None or not hasattr(progress, "notify_navigation_success"):
        return None
    result = progress.notify_navigation_success(
        page_id,
        navigation_kind=navigation_kind,
    )
    logger = getattr(agent, "logger", None)
    if logger is not None:
        event = (
            "progress.history_navigation_credit_exhausted"
            if result.get("status") == "history_navigation_credit_exhausted"
            else "progress.navigation_success"
        )
        logger.write(event, result)
    return result

def _observe_navigation_progress_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> None:
    page_id = str(params.get("pageId") or "").strip()
    pending = getattr(agent, "navigation_progress_pending_pages", None)
    if not isinstance(pending, dict):
        pending = {}
        agent.navigation_progress_pending_pages = pending
    last_urls = getattr(agent, "navigation_progress_last_urls", None)
    if not isinstance(last_urls, dict):
        last_urls = {}
        agent.navigation_progress_last_urls = last_urls
    # Only the explicit history-return primitive earns a reset on this raw
    # browser-call path. Page.reload is same-route retry and raw Page.navigate
    # is not URL/title verified; either could otherwise loop with Page.getState
    # to replenish the no-artifact and heavy-diagnostic budgets indefinitely.
    # navigate_verified has its own verified reset in _observe_progress_after.
    if method == "Page.go":
        pending.pop(page_id, None)
        if page_id and not _bt()._invoke_result_failed(result):
            pending[page_id] = str(last_urls.get(page_id) or "")
        return
    if method in {"Page.navigate", "Page.reload"}:
        pending.pop(page_id, None)
        return
    if method == "Page.getState" and page_id in pending:
        current_url = str(
            _bt()._response_data(result).get("url")
            or _bt()._response_data(result).get("currentUrl")
            or ""
        ).strip()
        previous_url = str(pending.pop(page_id, "") or "").strip()
        if not _bt()._invoke_result_failed(result):
            if current_url:
                last_urls[page_id] = current_url
            if previous_url and current_url and current_url != previous_url:
                progress_receipt = _notify_navigation_success(
                    agent,
                    page_id,
                    navigation_kind="history",
                )
                if isinstance(progress_receipt, dict):
                    result["progressNavigation"] = progress_receipt
            else:
                result["progressNavigation"] = {
                    "status": "history_navigation_unverified",
                    "pageId": page_id,
                    "navigationKind": "history",
                    "previousUrl": previous_url or None,
                    "currentUrl": current_url or None,
                    "creditApplied": False,
                }
                logger = getattr(agent, "logger", None)
                if logger is not None:
                    logger.write(
                        "progress.history_navigation_unverified",
                        {
                            "pageId": page_id,
                            "previousUrl": previous_url or None,
                            "currentUrl": current_url or None,
                            "creditApplied": False,
                            "reason": (
                                "missing_pre_navigation_url"
                                if not previous_url
                                else "missing_post_navigation_url"
                                if not current_url
                                else "url_unchanged"
                            ),
                        },
                    )
        return
    if method == "Page.getState" and page_id and not _bt()._invoke_result_failed(result):
        current_url = str(
            _bt()._response_data(result).get("url")
            or _bt()._response_data(result).get("currentUrl")
            or ""
        ).strip()
        if current_url:
            last_urls[page_id] = current_url

def _strip_challenge_fields(value: Any) -> Any:
    if isinstance(value, dict):
        challenge_keys = {"suspected_challenge", "challengeAdjudication", "autoHitl"}
        stripped_keys = set(challenge_keys)
        if any(key in value for key in challenge_keys):
            stripped_keys.add("next_instruction")
        return {
            key: _strip_challenge_fields(item)
            for key, item in value.items()
            if key not in stripped_keys
        }
    if isinstance(value, list):
        return [_strip_challenge_fields(item) for item in value]
    return value

def _page_inventory_is_discoverable(agent: Any, page_id: str) -> bool:
    """Whether an unseen page is worth telling this worker to go look for.

    A page another live worker already holds is not a discovery opportunity, so
    signalling it would be pure noise. Ownership is checked here rather than
    when the event arrived because the lease is recorded only after the
    creating RPC returns — at event time every page still looks unowned.
    """
    manager = getattr(agent, "page_lease_manager", None)
    if manager is None or not hasattr(manager, "owner_for"):
        return True
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    owner = str(manager.owner_for(page_id) or "").strip()
    return not owner or owner == worker_id

def _settle_page_inventory_signal(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    *,
    page_list_shown: Optional[List[JsonDict]] = None,
) -> JsonDict:
    """Discharge pages the worker now knows about, then attach the change bit.

    Discharge runs BEFORE the receipt is built so a call that itself reveals a
    page never carries a signal about that page: Page.create names the tab it
    just made, Page.list shows the model every row, Page.close removes one.
    """
    signal = getattr(agent, "page_inventory_signal", None)
    if signal is None or not isinstance(result, dict):
        return result

    if method == "Page.create":
        # The response names the tab this worker just made. Without this the
        # worker would be told to go find its own page: the Page.open event
        # always lands BEFORE the response that identifies it.
        for page_id in _result_page_ids_for_inventory(result.get("response")):
            grant = evaluate_grant(
                kind="inventory_discharge_page_create",
                method=method,
                result=result,
                page_id=page_id,
            )
            if grant.allowed:
                signal.discharge([page_id])
    elif method == "Page.close":
        page_id = str(params.get("pageId") or "").strip()
        grant = evaluate_grant(
            kind="inventory_discharge_page_close",
            method=method,
            result=result,
            page_id=page_id,
        )
        if grant.allowed:
            signal.discharge([page_id])
    elif method == "Page.list" and page_list_shown is not None:
        grant = evaluate_grant(
            kind="inventory_discharge_page_list",
            method=method,
            result=result,
        )
        if grant.allowed:
            for row in page_list_shown:
                if not isinstance(row, dict):
                    continue
                signal.discharge(
                    [row.get("pageId")],
                    fleet_id=row.get("fleetId"),
                )

    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if not fleet_id:
        return result
    receipt = signal.receipt(
        fleet_id,
        is_discoverable=lambda page_id: _page_inventory_is_discoverable(
            agent, page_id
        ),
    )
    if receipt:
        result["pageInventoryChanged"] = True
        result["pageInventoryInstruction"] = receipt["next_instruction"]
    return result

def _result_page_ids_for_inventory(response: Any) -> List[str]:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        page_id = str(data.get("pageId") or "").strip()
        return [page_id] if page_id else []
    return []

def _navigate_pattern_invalid_result(
    *,
    page_id: str,
    field: str,
    pattern: str,
    error: str,
) -> JsonDict:
    """Reject an uncompilable expectation BEFORE spending a real navigation."""
    return {
        "status": "expectation_pattern_invalid",
        "tool_was_executed": False,
        "navigationCommitted": False,
        "pageId": page_id,
        "field": field,
        "pattern": pattern[:200],
        "error": f"{field} is not a valid regular expression: {error}"[:300],
        "next_instruction": (
            f"No navigation was dispatched. Fix {field} — or omit it, which"
            " accepts the requested URL itself (expectedUrlPattern) or skips"
            " the title check (expectedTitlePattern) — then call"
            " navigate_verified again."
        ),
    }

def _nested_response_error(result: Any) -> str:
    """Return the browser-side error text carried inside `response`."""
    if not isinstance(result, dict):
        return ""
    response = result.get("response")
    if not isinstance(response, dict):
        return ""
    for candidate in (
        response.get("error"),
        (response.get("data") or {}).get("error")
        if isinstance(response.get("data"), dict)
        else None,
    ):
        if isinstance(candidate, dict):
            text = str(candidate.get("message") or candidate.get("error") or "")
            if text:
                return text
        elif candidate:
            return str(candidate)
    return ""

async def _read_page_state_once(
    agent: Any,
    page_id: str,
    step: int,
) -> JsonDict:
    """One read-only Page.getState, reported as observation or as unreadable."""
    state_result = await _bt()._invoke_browser_method(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Observe page state after a failed navigation",
        },
        step,
        count_progress=False,
    )
    outcome = classify_call_outcome(state_result)
    if outcome.interrupted:
        # A challenge/HITL pause is a terminal state, not an unreadable page.
        # Flattening it here hid the whole hitl_wait payload and let the model
        # keep acting on a page the platform had paused.
        return {
            "observedState": "hitl_interrupted",
            "autoHitl": outcome.auto_hitl,
            "next_instruction": (
                "The page entered human-intervention handling while its state"
                " was being read. Inspect autoHitl.hitl_wait and stop acting on"
                " this page until it reports resumed."
            ),
        }
    if not outcome.succeeded or not page_state_evidence_ok(page_id, state_result):
        return {
            "observedState": "unreadable",
            "observedStateError": (
                outcome.error or "Page.getState returned no usable page state"
            ),
        }
    data = _bt()._response_data(state_result) or {}
    return {
        "observedState": "read",
        "observedUrl": str(data.get("url") or ""),
        "observedTitle": str(data.get("title") or ""),
        "observedPageStatus": str(data.get("status") or ""),
    }

async def _navigate_dispatch_failure_result(
    agent: Any,
    *,
    page_id: str,
    url: str,
    nav: JsonDict,
    step: int = 0,
) -> JsonDict:
    """Classify a failed Page.navigate by what the harness actually OBSERVED.

    Only two facts are ever available first-hand, and only they may be stated:

    * A pre-dispatch guard answered ``tool_was_executed=False``. The call never
      reached the panel, so the page is provably untouched.
    * The lifecycle tracker received ``Page.loadFailed`` for this page. The
      navigation was attempted and provably did not arrive.

    Everything else — transport exceptions, ``-32005``, a dead renderer, a
    precondition rejection, any Chrome ``net::ERR_*`` string — leaves the commit
    position genuinely unknown. Earlier revisions tried to rank those by
    parsing the error text, which meant guessing browser semantics: ERR_ABORTED
    is raised when another navigation supersedes this one, and
    ERR_BLOCKED_BY_CLIENT fires before the request leaves. Neither proves the
    page stayed put. They now share one status, with the distinction kept as
    non-load-bearing diagnostics, because the model's next move is identical in
    every case: read the page state before deciding anything.
    """
    classification = nav.get("errorClassification")
    # A transport exception lands at the top level; a browser-side failure is
    # nested in the response.
    error_text = str(nav.get("error") or _nested_response_error(nav) or "")[:300]

    if nav.get("tool_was_executed") is False:
        return {
            "status": "navigation_not_dispatched",
            "tool_was_executed": False,
            "navigationCommitted": False,
            "pageId": page_id,
            "requestedUrl": url,
            "guardStatus": str(nav.get("status") or "") or None,
            "error": error_text or None,
            "errorClassification": classification,
            "navigateResult": _strip_challenge_fields(nav),
            "next_instruction": (
                "A harness guard refused the call before it reached the"
                " browser, so the page is untouched. Read guardStatus, clear"
                " that condition, then decide whether to navigate."
            ),
        }

    challenge = _page_challenge_summary(agent, page_id)
    # Snapshot the lifecycle BEFORE reading: Page.getState feeds the tracker and
    # would overwrite the Page.loadFailed this branch exists to detect.
    lifecycle_state = (
        agent.page_lifecycle.state(page_id)
        if isinstance(getattr(agent, "page_lifecycle", None), PageLifecycleTracker)
        else None
    )
    lifecycle_reported_failure = (
        str(getattr(lifecycle_state, "status", "") or "") == "failed"
    )
    # The request was dispatched and failed, so where the page sits is a
    # question only the page can answer. Read it ONCE here rather than telling
    # the model to: Page.getState issues no network request, and a receipt that
    # merely says "go look" leaves the model to act on a state nobody observed.
    observed = await _read_page_state_once(agent, page_id, step)
    if observed.get("observedState") == "hitl_interrupted":
        # The read itself hit the human-intervention path. That outranks any
        # navigation classification: the model must handle the pause, not the
        # failed navigate.
        return {
            "status": "navigation_interrupted_by_hitl",
            "tool_was_executed": True,
            "navigationCommitted": None,
            "pageId": page_id,
            "requestedUrl": url,
            "error": error_text,
            "errorClassification": classification,
            **observed,
        }
    common: JsonDict = {
        "pageId": page_id,
        "requestedUrl": url,
        "error": error_text,
        "errorClassification": classification,
        "navigateResult": _strip_challenge_fields(nav),
        "suspectedChallenge": challenge or None,
        **observed,
    }

    if lifecycle_reported_failure:
        # ABCP states WHY the page is unusable in `failure.kind`; carry it
        # instead of leaving the caller to re-derive it from prose. An
        # `automation-unavailable` page is not a navigation the browser lost —
        # re-navigating cannot fix it.
        failure_kind = str(getattr(lifecycle_state, "failure_kind", "") or "")
        automation_unavailable = failure_kind == AUTOMATION_UNAVAILABLE_FAILURE
        return {
            **common,
            "status": "navigation_load_failed",
            "tool_was_executed": True,
            "navigationCommitted": False,
            "pageFailure": {
                "kind": failure_kind,
                "message": str(getattr(lifecycle_state, "failure_message", "") or "") or None,
                "retryableByNavigation": not automation_unavailable,
            } if failure_kind else None,
            "next_instruction": (
                "The browser reported Page.loadFailed for this navigation."
                " observedUrl/observedTitle are where the page actually sits."
                + (
                    " pageFailure.kind=automation-unavailable: the document may"
                    " be fine while automation cannot attach, so navigating"
                    " again will not change it — report the blocker instead."
                    if automation_unavailable else
                    " Decide from those whether a fresh navigation is warranted;"
                    " this composite will not re-dispatch it for you."
                )
            ),
        }

    error_type = (
        str(classification.get("type") or "")
        if isinstance(classification, dict)
        else ""
    )
    if error_type in {"page_crashed", "render_lost"}:
        reason = "page_unavailable"
    elif nav.get("error"):
        reason = "transport_error"
    else:
        reason = "browser_action_failed"
    return {
        **common,
        "status": "navigation_outcome_unknown",
        "tool_was_executed": True,
        "navigationCommitted": None,
        "reason": reason,
        "next_instruction": (
            "Page.navigate failed without proving where the page ended up, so"
            " the harness read the page for you: observedUrl/observedTitle are"
            " its actual state. Decide from those; do NOT call"
            " navigate_verified again for this navigation."
        ),
    }

def _navigate_challenge_blocked_result(
    *,
    page_id: str,
    attempt: int,
    last_state: JsonDict,
    attempts: List[JsonDict],
    state_resync_count: int,
    challenge_summary: JsonDict,
    expected_url_pattern: str,
    expected_title_pattern: str,
    trigger: str,
) -> JsonDict:
    return {
        "status": "blocked_by_challenge",
        "pageId": page_id,
        "attempt": attempt,
        "lastState": last_state,
        "attempts": attempts,
        "stateResyncCount": state_resync_count,
        "expectedUrlPattern": expected_url_pattern,
        "expectedTitlePattern": expected_title_pattern or None,
        "suspectedChallenge": challenge_summary or None,
        "trigger": trigger,
        "next_instruction": (
            "Navigation appears blocked by an anti-bot or challenge page after"
            " bounded verification. Do not keep polling Page.getState; call"
            " final_answer with status=\"blocked_by_challenge\", request HITL"
            " if the workflow supports it, or let LeadAgent pivot strategy."
        ),
    }

def _result_has_auto_hitl(result: Any) -> bool:
    return isinstance(result, dict) and isinstance(result.get("autoHitl"), dict)

def _auto_hitl_is_actionable(auto: Any) -> bool:
    """True only when an autoHitl entry represents a REAL pause request — i.e.
    `Hitl.requestPause` actually ran. A skipped / not-executed adjudication is a
    no-op: the page was never paused, so a composite loop must NOT abort on it.

    Post-97f105e the harness only writes result['autoHitl'] when it truly requests
    HITL (skipped/cooldown/stale verdicts go to `suspected_challenge.adjudication`
    instead), so in practice every autoHitl is actionable. This guard keeps
    `_loop_interrupt_from_result` honest against a future short-circuit that could
    attach a `tool_was_executed: False` / `status: "skipped*"` autoHitl.

    The rule itself lives in harness.results.call_outcome so the shared verdict and this
    loop guard cannot drift apart; a second, weaker copy of it treated every
    skipped adjudication as a pause."""
    return auto_hitl_is_actionable(auto)

def _navigate_hitl_result(page_id: str, attempt: int, result: JsonDict) -> JsonDict:
    wait = {}
    auto_hitl = result.get("autoHitl")
    if isinstance(auto_hitl, dict):
        response = auto_hitl.get("response")
        if isinstance(response, dict) and isinstance(response.get("hitl_wait"), dict):
            wait = response.get("hitl_wait") or {}
    if wait.get("status") in {"timeout", "page_settled_after_hitl", "stale_pause_deadlock"}:
        status = str(wait.get("status"))
    else:
        status = "hitl_required"
    next_instruction = (
        "The page appears to be past the challenge, but ABCP still reports it"
        " paused. Do not keep polling; call final_answer with"
        " status=\"page_settled_after_hitl\" and surface that the ABCP control"
        " channel has not released the paused page yet."
        if status == "page_settled_after_hitl" else
        "The page is in a stale HITL pause deadlock. Do not request HITL again;"
        " continue from a fresh page/fleet or report the platform blocker."
        if status == "stale_pause_deadlock" else
        "Human intervention was requested for a suspected challenge. Do not"
        " keep polling this page while it is paused; inspect autoHitl.hitl_wait."
    )
    return {
        "status": status,
        "pageId": page_id,
        "attempt": attempt,
        "autoHitl": auto_hitl,
        "triggerResult": result,
        "next_instruction": next_instruction,
    }

def _loop_interrupt_summary(
    status: str,
    *,
    autoHitl: Optional[JsonDict] = None,
    pausedState: Optional[JsonDict] = None,
) -> JsonDict:
    """Summary a composite loop returns when a HITL/challenge interrupt aborts it.

    For the blocked statuses needsHuman=True tells the LLM that resuming/retrying
    is futile until a human clears the page. The `hitl_resumed` status is
    different: a human ALREADY resolved the challenge mid-loop, so needsHuman is
    False — but the loop still STOPS (loopInterrupted) because the page may have
    changed under the human (navigation, closed dialogs, altered form state) and
    the loop's local assumptions / target ids are no longer trustworthy. The
    model must re-observe and re-issue rather than the loop blindly continuing."""
    instructions = {
        "hitl_required": (
            "A human verification (e.g. Cloudflare/CAPTCHA) blocked this page and"
            " the loop paused for HITL. Do NOT resume the loop or retry browser"
            " actions; wait for the human resume event or report the blocker to"
            " LeadAgent."
        ),
        "timeout": (
            "Human intervention was requested for a challenge but did not complete"
            " in time. Do NOT resume the loop or retry; report the blocker or hand"
            " off to LeadAgent."
        ),
        "page_settled_after_hitl": (
            "The page looks past the challenge but ABCP still reports it paused."
            " Do NOT resume the loop; surface that the control channel has not"
            " released the page."
        ),
        "stale_pause_deadlock": (
            "The page is in a stale HITL pause deadlock. Do NOT request HITL again"
            " or resume the loop; continue from a fresh page/fleet or report the"
            " platform blocker."
        ),
        "hitl_resumed": (
            "A human resolved a challenge (e.g. Cloudflare) mid-loop, so the page"
            " may have changed (navigation, closed dialogs, altered form state)."
            " The loop stopped WITHOUT acting on possibly-stale state. Re-observe"
            " with Page.getState + DOM.getAXTree, then re-issue the action/tool"
            " with fresh ids if it is still valid. Any partial results are included."
        ),
    }
    needs_human = status != "hitl_resumed"
    if status == "hitl_resumed":
        resume = "reobserve_then_reissue"
    elif status in {"hitl_required", "timeout"}:
        resume = "wait_for_human"
    else:
        resume = "fresh_page_or_report"
    summary: JsonDict = {
        "status": status,
        "loopInterrupted": True,
        "needsHuman": needs_human,
        "resumeRecommendation": resume,
        "next_instruction": instructions.get(status, instructions["hitl_required"]),
    }
    # Layer 2 discipline: surface only a compact digest to the model. The full
    # autoHitl payload (pause request, VL adjudication, nested response) is
    # verbose and already in the run log via browser.call.result; the model only
    # needs the wait status + where/why.
    if autoHitl is not None:
        summary["hitlDigest"] = _hitl_digest(autoHitl)
    if pausedState is not None:
        summary["pausedState"] = pausedState
    return summary

def _hitl_digest(auto_hitl: Any) -> JsonDict:
    """Compact, model-facing digest of an autoHitl payload."""
    if not isinstance(auto_hitl, dict):
        return {}
    response = auto_hitl.get("response") if isinstance(auto_hitl.get("response"), dict) else {}
    wait = response.get("hitl_wait") if isinstance(response.get("hitl_wait"), dict) else {}
    suspected = (
        auto_hitl.get("suspected_challenge")
        if isinstance(auto_hitl.get("suspected_challenge"), dict) else {}
    )
    recovery = wait.get("postHitlRecovery") if isinstance(wait.get("postHitlRecovery"), dict) else {}
    digest = {
        "hitlWaitStatus": wait.get("status"),
        "pageId": auto_hitl.get("pageId") or wait.get("pageId") or response.get("pageId"),
        "reason": auto_hitl.get("reason") or suspected.get("reason") or suspected.get("adjudication"),
        "postHitlRecoveryStatus": recovery.get("status"),
        "screenshotPath": auto_hitl.get("screenshotPath") or suspected.get("screenshotPath"),
    }
    return {key: value for key, value in digest.items() if value is not None}

def _loop_interrupt_from_result(result: Any) -> Optional[JsonDict]:
    """Detect a HITL/challenge interrupt on a composite-loop internal browser
    call. Composite tools run with the model OUT of the loop, so when a call
    triggers auto-HITL (Cloudflare/CAPTCHA) or hits an already-paused page, the
    loop must STOP and surface a human-needed summary rather than keep
    clicking/scrolling or degrade to a generic stagnant/failed reason.

    Returns a summary to return immediately, or None when there is no interrupt
    and the loop may continue. NOTE: a `resumed` wait is NOT None — a human
    touched the page mid-loop, so the loop stops with a non-terminal
    `hitl_resumed` summary (needsHuman=False) for the model to re-observe; the
    loop must not keep acting on possibly-stale local state. The pause+wait happen
    synchronously inside the triggering _invoke_browser_method call, so the
    outcome is on THAT result."""
    if not isinstance(result, dict):
        return None
    auto = result.get("autoHitl")
    if isinstance(auto, dict) and _auto_hitl_is_actionable(auto):
        wait: JsonDict = {}
        response = auto.get("response") if isinstance(auto, dict) else None
        if isinstance(response, dict) and isinstance(response.get("hitl_wait"), dict):
            wait = response.get("hitl_wait") or {}
        status = str(wait.get("status") or "")
        if status == "resumed":
            # A human cleared the challenge, but the page may have changed under
            # them: stop and make the model re-observe rather than continue on
            # stale ids/assumptions.
            return _loop_interrupt_summary(
                "hitl_resumed", autoHitl=auto if isinstance(auto, dict) else None
            )
        terminal = (
            status
            if status in {"timeout", "page_settled_after_hitl", "stale_pause_deadlock"}
            else "hitl_required"
        )
        return _loop_interrupt_summary(
            terminal, autoHitl=auto if isinstance(auto, dict) else None
        )
    paused_state = result.get("pausedState")
    if isinstance(paused_state, dict) or _bt()._result_has_paused_error(result):
        return _loop_interrupt_summary(
            "hitl_required",
            pausedState=paused_state if isinstance(paused_state, dict) else None,
        )
    return None

def _invoke_result_failed(result: Any) -> bool:
    """True when an _invoke_browser_method result represents a failed ACTION.

    Browser-side action errors surface in response.error / response.data.error
    (top-level `error` is only set on transport exceptions), so a check that
    only reads result["error"] would report a failed retry as succeeded.

    NOT interchangeable with `classify_call_outcome`, and the difference is
    `response.data.error`:

    * this predicate answers "did the ACTION achieve its page effect", and for
      an action method a page-level error means it did not — retry paths and
      recovery ladders want that reading;
    * `classify_call_outcome` answers "did the CALL execute and come back",
      and deliberately ignores `data.error` because for a read like
      Page.getState that field is the PAGE's last-navigation error, permanent
      on a risk-controlled page. Anything that GRANTS state — re-perception
      credit, recovery credit, content binding, inventory baselines — must use
      the verdict, not this. Task 48b4d7d7 deadlocked for 84 minutes because a
      gate whose exit condition was "re-read the page" used this predicate.

    Two general failure predicates in one tree is the shape that caused that
    bug. Collapsing the ~20 call sites onto the verdict is tracked separately;
    until then, choose by the question you are asking."""
    if not isinstance(result, dict):
        return False
    if result.get("tool_was_executed") is False:
        return True
    if result.get("error"):
        return True
    if result.get("status") == "stale_element_reference":
        return True
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return True
        data = response.get("data")
        if isinstance(data, dict) and data.get("error"):
            return True
    classification = result.get("errorClassification")
    if isinstance(classification, dict) and classification.get("type"):
        return True
    return False

def _transport_error_metadata(
    method: str,
    exc: ABCPTransportError,
) -> JsonDict:
    """Keep machine-readable RPC failure data where recovery needs it.

    ``rpcData`` is surfaced only for the select API pair. Other actions may
    carry typed or otherwise sensitive values in provider diagnostics; their
    numeric code/method remain useful without copying that opaque payload into
    the model-facing result.
    """

    metadata: JsonDict = {}
    local_receipt = getattr(exc, "receipt", None)
    if isinstance(local_receipt, dict):
        for key in (
            "status",
            "reasonKind",
            "pageId",
            "fleetId",
            "workerId",
            "ownerWorkerId",
            "methodKind",
            "retryable",
            "quarantined",
            "tool_was_executed",
            "next_instruction",
        ):
            if key in local_receipt:
                metadata[key] = local_receipt.get(key)
    rpc_code = getattr(exc, "rpc_code", None)
    rpc_method = str(getattr(exc, "rpc_method", "") or "")
    if rpc_code is not None:
        metadata["rpcCode"] = rpc_code
    if rpc_method:
        metadata["rpcMethod"] = rpc_method
    rpc_data = getattr(exc, "rpc_data", None)
    if method in {"DOM.inspectSelect", "Input.select"} and rpc_data is not None:
        metadata["rpcData"] = trim_large_strings(rpc_data, 4000)
    runtime = action_runtime_info(rpc_data)
    if runtime:
        # Four bounded scalars, no provider payload: whether the failure landed
        # before or after dispatch is the one fact a retry decision needs, and
        # inferring it from prose is guessing at something the platform states.
        metadata["actionRuntime"] = runtime
    return metadata

_SELECT_FAILURE_GUIDANCE: Dict[str, Tuple[int, str]] = {
    "select-option-stale": (
        1,
        "Call DOM.inspectSelect again, copy only fields returned for the requested"
        " option, then retry Input.select once, preferring its exact value or"
        " label when present and using option id only as fallback. Do not open"
        " or operate the popup manually or reuse an arbitrary AXTree option id.",
    ),
    "select-option-not-found": (
        1,
        "Call DOM.inspectSelect again with an appropriate query/maxOptions and"
        " inspect its loadMore/truncated state. Retry once only with an exact"
        " option descriptor returned by that inspection.",
    ),
    "select-option-disabled": (
        0,
        "The requested option is disabled. Stop retrying and report that it is"
        " unavailable; do not silently choose a different option.",
    ),
    "select-popup-lost": (
        0,
        "ABCP lost the select popup while executing the atomic Input.select"
        " action. Do not repeat the call, reload the page, or operate the popup"
        " manually; report the platform failure with this receipt.",
    ),
    "select-navigation-stalled": (
        0,
        "ABCP could not advance the cascading selection. Do not repeat the same"
        " path or replace it with manual popup clicks; report the platform"
        " failure with the DOM.inspectSelect path used.",
    ),
}

def _apply_select_failure_guidance(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> JsonDict:
    """Attach code-specific, mechanically bounded Input.select recovery."""

    if not isinstance(result, dict):
        return result
    if method == "DOM.inspectSelect":
        classification = result.get("errorClassification")
        error_code = (
            str(classification.get("errorCode") or "")
            if isinstance(classification, dict)
            else ""
        )
        if error_code == "select-control-not-visible":
            result["next_instruction"] = (
                "Refresh DOM.getAXTree and target only a currently visible"
                " select-like control. Do not retry the same hidden container"
                " selector or construct an Input.select request from hidden"
                " option rows."
            )
            result["selectRecovery"] = {
                "errorCode": error_code,
                "retryAllowed": False,
            }
        elif error_code == "select-control-unsupported":
            result["next_instruction"] = (
                "This element is not an ABCP-supported select-like control. Do"
                " not call Input.select for it. If it is an ordinary visible"
                " category/list browser, use fresh DOM.getAXTree targets and"
                " one verified Input.click per visible level; this is a"
                " non-select UI fallback, not manual popup management."
            )
            result["selectRecovery"] = {
                "errorCode": error_code,
                "retryAllowed": False,
            }
        return result
    if method != "Input.select":
        return result
    target = str(params.get("selector") or params.get("id") or "<unknown>")
    page_id = str(params.get("pageId") or "")
    ledger = getattr(agent, "_select_failure_ledger", None)
    if not _bt()._invoke_result_failed(result):
        if isinstance(ledger, dict):
            for key in list(ledger):
                if key[:2] == (page_id, target):
                    ledger.pop(key, None)
        return result
    classification = result.get("errorClassification")
    error_code = (
        str(classification.get("errorCode") or "")
        if isinstance(classification, dict)
        else ""
    )
    guidance = _SELECT_FAILURE_GUIDANCE.get(error_code)
    if guidance is None:
        return result
    max_retries, instruction = guidance
    if not isinstance(ledger, dict):
        ledger = {}
        setattr(agent, "_select_failure_ledger", ledger)
    key = (page_id, target, error_code)
    failures = int(ledger.get(key) or 0) + 1
    ledger[key] = failures
    retry_allowed = failures <= max_retries
    if max_retries and not retry_allowed:
        instruction = (
            "The one permitted recovery retry for this select/control/error has"
            " already failed. Stop retrying and report an ABCP select contract"
            " failure with the inspect and select receipts."
        )
    result["selectRecovery"] = {
        "errorCode": error_code,
        "failureCount": failures,
        "maxRetries": max_retries,
        "retryAllowed": retry_allowed,
        "controlTarget": target,
    }
    result["next_instruction"] = instruction
    return result
