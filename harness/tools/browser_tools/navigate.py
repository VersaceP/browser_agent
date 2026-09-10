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
from harness.results.call_outcome import auto_hitl_is_actionable
from harness.results.call_outcome import classify_call_outcome
from harness.results.call_outcome import domain_state_read_succeeded
from harness.results.call_outcome import evaluate_grant
from harness.results.call_outcome import page_state_evidence_ok
from harness.results.call_outcome import public_failure_details
from harness.diagnostics.error_classification import SELECT_FAILURE_GUIDANCE
from harness.diagnostics.error_classification import SELECT_FAILURE_RETRY_LIMITS
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

    `_invoke_result_failed` also fails on `response.data.error`, which for a
    domain-error read is the PAGE's own last-navigation error rather than this
    call failing. On a risk-controlled page that field never clears, so every
    successful Page.getState reported an `error:` blocker and suppressed
    content-completeness observation for a page that was in fact readable.

    The exemption therefore requires that the classifier ALSO found nothing.
    `attach_error_classification` runs earlier in the same post-call chain and
    already resolves the priority order — a transport failure and, above every
    code, an ERR_PAGE_PAUSED observation. An exemption that ignored its verdict
    would silently drop a pause on a page whose data looked healthy. Where a
    classification exists, it is the answer; only its absence, on a clean read,
    licenses staying quiet.

    Either way this suppresses the ERROR branch alone — a clean read still goes
    through the auth/challenge/lifecycle detectors below, which is the whole
    reason this adapter reads a successful Page.getState at all.
    """
    classification = (
        result.get("errorClassification")
        if isinstance(result.get("errorClassification"), dict) else {}
    )
    unclassified_clean_read = (
        not classification and domain_state_read_succeeded(result, method)
    )
    if _bt()._invoke_result_failed(result) and not unclassified_clean_read:
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
        navigation_started = _bt()._response_data(result).get(
            "navigationStarted"
        )
        if navigation_started is False:
            result["progressNavigation"] = {
                "status": "history_navigation_not_started",
                "pageId": page_id,
                "navigationKind": "history",
                "navigationStarted": False,
                "creditApplied": False,
            }
        elif page_id and not _bt()._invoke_result_failed(result):
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

# The complete set of ABCP failure fields the harness will copy out of a
# JSON-RPC `error.data`. Anything else — provider diagnostics, typed values, a
# future free-form `details` object — is refused by omission rather than by a
# denylist, so a new upstream field cannot leak by default.
_PUBLIC_FAILURE_FIELDS = ("observation", "suggested_prompt")
_PUBLIC_FAILURE_ERROR_FIELDS = ("code", "message")


def _public_failure_projection(rpc_data: Any) -> JsonDict:
    """Project only ABCP's public failure fields out of a JSON-RPC error data."""

    if not isinstance(rpc_data, dict):
        return {}
    projected: JsonDict = {}
    for key in _PUBLIC_FAILURE_FIELDS:
        value = rpc_data.get(key)
        if isinstance(value, str) and value.strip():
            projected[key] = value
    details = public_failure_details(rpc_data.get("details"))
    if details:
        projected["details"] = details
    error = rpc_data.get("error")
    if isinstance(error, dict):
        public_error = {
            key: error[key]
            for key in _PUBLIC_FAILURE_ERROR_FIELDS
            if isinstance(error.get(key), str) and error[key].strip()
        }
        if public_error:
            projected["error"] = public_error
    return trim_large_strings(projected, 4000) if projected else {}


def _transport_error_metadata(
    method: str,
    exc: ABCPTransportError,
) -> JsonDict:
    """Keep machine-readable RPC failure data where recovery needs it.

    Every ABCP public failure carries its own ``observation`` and
    ``suggested_prompt``, and dropping them for all but the select pair threw
    away the platform's own recovery guidance on every other method. What made
    that unsafe was copying the whole ``rpc_data`` object, which may carry
    typed values or provider diagnostics — not the public fields themselves.
    So project a fixed public whitelist for every method and refuse the rest.

    An error ``data`` carries ``error{code,message}``, ``observation``,
    ``suggested_prompt`` and — for the codes that register detail fields —
    ``details``. The platform bounds ``details`` itself (per-code allowlist,
    scalars only); ``public_failure_details`` re-applies the shape rule here so
    a build that widens it cannot reopen the unbounded payload this whitelist
    exists to close.
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
    metadata["exceptionType"] = type(exc).__name__
    transport_code = str(getattr(exc, "transport_code", "") or "").strip()
    # Legacy callers construct ABCPTransportError for structured JSON-RPC
    # failures without a transport code. Do not let the default placeholder
    # hide their method-specific rpcData classification.
    if transport_code and transport_code != "ABCP_TRANSPORT_UNKNOWN":
        metadata["transportCode"] = transport_code
    if bool(getattr(exc, "connection_fatal", False)):
        metadata["connectionFatal"] = True
    request_sent = getattr(exc, "request_sent", None)
    if isinstance(request_sent, bool):
        metadata["requestSent"] = request_sent
    rpc_code = getattr(exc, "rpc_code", None)
    rpc_method = str(getattr(exc, "rpc_method", "") or "")
    if rpc_code is not None:
        metadata["rpcCode"] = rpc_code
    if rpc_method:
        metadata["rpcMethod"] = rpc_method
    rpc_data = getattr(exc, "rpc_data", None)
    public_failure = _public_failure_projection(rpc_data)
    if public_failure:
        metadata["rpcData"] = public_failure
    return metadata

# Harness next_instruction for a failed select Action, keyed by public code.
# The platform codes are a PROJECTION of SELECT_FAILURE_POLICY - the single
# declaration that also produces the two action maps and the retry budget - so
# a code can no longer carry a recovery action but no prose, which is how
# select-option-not-in-current-window silently lost both.
#
# The prose is method-neutral on purpose. The ladder for a given failure is the
# same whether an inspection or a selection hit it, and Input.select failures
# used to reach the model with a bare selectRecovery block and no ladder at all.
#
# The one extra key is harness-synthesized, not a platform code: ABCP can
# return a generic -32005 with no public select reason.
#
# Named for the two methods it serves, not for inspect alone: it was
# _INSPECT_SELECT_GUIDANCE while only inspect read it, and that name is part of
# why nobody noticed Input.select failures were leaving without any prose.
_SELECT_PLATFORM_ACTION_FAILED = "inspect-select-platform-action-failed"

_SELECT_FAILURE_NEXT_INSTRUCTION: Dict[str, str] = {
    **SELECT_FAILURE_GUIDANCE,
    _SELECT_PLATFORM_ACTION_FAILED: (
        "ABCP returned a generic -32005 failure without a public select"
        " reason code. Do not repeat the same Action automatically or infer"
        " a selector from this error text. The failed Action may still have"
        " re-rendered or opened the control, so discard prior element ids"
        " and re-observe first. A custom popup may be rendered through a"
        " portal outside the control subtree: use fresh"
        " DOM.getSemanticTree/AX evidence to relate aria-controls,"
        " aria-owns, or aria-activedescendant to a page-wide"
        " listbox/option surface. Only a selector independently returned by"
        " that fresh semantic evidence may be used. Then perform at most one"
        " generic Input.click/Input.press/Input.type action. Its success"
        " receipt does NOT prove a popup opened: require a fresh visible"
        " related popup before calling Input.select."
    ),
}

# These codes describe failure to bind the requested control to a current page
# identity.  They are deliberately separate from SELECT_FAILURE_POLICY: the
# latter owns select-operation failures and the replay block, while these
# records only say that native select resolution could not get started or stay
# bound.  A public failure does not state how far a custom control operation
# got, so an identity code is never proof that the page was untouched.
_SELECT_IDENTITY_FAILURE_CODES = frozenset({
    "stale-target",
    "target-not-found",
    "pointer-target-stale",
    "target-preparation-failed",
})
_SELECT_IDENTITY_STALE_AXTREE_CODE = "axtree-stale-reference"
_SELECT_IDENTITY_NATIVE_RECOVERY_BUDGET = 1
_SELECT_IDENTITY_FAILURE_FAMILY = "control-identity"

_SELECT_IDENTITY_GENERIC_UI_LADDER = (
    "This control had two identity-resolution failures with a successful fresh"
    " DOM.getAXTree between them. The current structured evidence makes ordinary"
    " UI a candidate recovery: read the current value and expanded state; only"
    " when needed, use an observed control, search field, option, paging control"
    " or scroll surface; then re-observe and verify the value. This is advice,"
    " not authorization or a ban on Select Actions. If structured evidence"
    " cannot name a visible target, visual_verify mode=visual_locate may locate"
    " it but does not make a stale id current."
)


def _select_call_locators(params: JsonDict, result: JsonDict = None) -> frozenset:
    """Every name this call used for the control, plus the one ABCP returned.

    The model may inspect by selector and select by id, so the association
    between a failure and the inspection that clears it cannot be keyed on a
    single field. `controlId` is added from a successful inspection because it
    is the platform's own name for the control and the most likely bridge
    between two differently-phrased calls.
    """
    names = set()
    for key in ("id", "selector"):
        value = str((params or {}).get(key) or "").strip()
        if value:
            names.add(value)
    if isinstance(result, dict):
        control_id = str(_bt()._response_data(result).get("controlId") or "").strip()
        if control_id:
            names.add(control_id)
    return frozenset(names)


def _select_page_epoch(agent: Any, page_id: str) -> int:
    """The navigation epoch this control identity belongs to.

    Element ids do not survive a navigation, so neither may an alias learned
    before one. ProgressAccountant already counts these per page; reading it
    here rather than inventing a second counter keeps one notion of "epoch" in
    the harness.
    """
    progress = getattr(agent, "progress", None)
    epochs = getattr(progress, "navigation_epochs", None)
    if not isinstance(epochs, dict):
        return 0
    try:
        return int(epochs.get(str(page_id or "") or "__global__", 0))
    except (TypeError, ValueError):
        return 0


def _select_drop_superseded_epochs(agent: Any, page_id: str, epoch: int) -> None:
    """Forget everything this page recorded before its current navigation.

    Both stores are per (page, epoch), and neither can be consulted again once
    the page moves on - so keeping them is growth, one set of rows per
    navigation, for the life of the worker.

    The failure ledger's staleness is not only a memory question. Its key
    carries the epoch, so an old row can no longer block or count; but leaving
    it there means a long task accumulates dead rows, and the prune has to run
    somewhere that a single-locator inspection still reaches - the alias
    learner returns early when it has fewer than two names, and doing it there
    meant a page that navigated and was then inspected by id alone kept its old
    rows forever.
    """
    for attribute in (
        "_select_control_aliases",
        "_select_failure_ledger",
        "_select_identity_failure_ledger",
    ):
        store = getattr(agent, attribute, None)
        if not isinstance(store, dict):
            continue
        for stale in [
            key for key in store
            if len(key) >= 2 and key[0] == page_id and key[1] != epoch
        ]:
            store.pop(stale, None)


def _select_learn_control_aliases(
    agent: Any, page_id: str, epoch: int, locators: frozenset
) -> None:
    """Record that these names all denote ONE control, per the platform.

    A successful DOM.inspectSelect is the only thing that can say this: the
    caller's `id`/`selector` and the `controlId` ABCP resolved them to came
    back from the same resolution. Without this the "control-level" block was
    control-level in name only - the API lets a caller pass id alone or
    selector alone, so failing by selector and then selecting by id walked
    straight past a guard that compared raw locator sets.

    What is learned here is applied at exactly one place: the moment a failure
    is recorded, which freezes every then-known name of that control into the
    ledger row. The guard and the clear path read those raw names.
    """
    if len(locators) < 2:
        return
    store = getattr(agent, "_select_control_aliases", None)
    if not isinstance(store, dict):
        store = {}
        setattr(agent, "_select_control_aliases", store)
    groups = store.setdefault((page_id, epoch), [])
    merged = set(locators)
    remaining = []
    for group in groups:
        if group & merged:
            merged |= group
        else:
            remaining.append(group)
    remaining.append(merged)
    store[(page_id, epoch)] = remaining


def _select_expand_locators(
    agent: Any, page_id: str, epoch: int, locators: frozenset
) -> frozenset:
    """Every name known to denote the same control as one of these.

    Only aliases learned in THIS page epoch count. Where nothing has linked two
    names, they stay unlinked and the block does not reach across them - the
    harness cannot prove they are the same element, and pretending otherwise
    would be a guarantee it has no basis for.
    """
    store = getattr(agent, "_select_control_aliases", None)
    if not isinstance(store, dict):
        return locators
    expanded = set(locators)
    for group in store.get((page_id, epoch), []):
        if group & expanded:
            expanded |= group
    return frozenset(expanded)


def _select_failure_count(entry: Any) -> int:
    """Failure count from a ledger entry, tolerating the pre-tuple shape."""
    if isinstance(entry, tuple) and entry:
        return int(entry[0] or 0)
    return int(entry or 0)


def _select_identity_error_code(result: JsonDict) -> str:
    """Return a stable control-identity failure code, or an empty string.

    The harness's AX snapshot guard is a first-hand pre-dispatch refusal and
    does not carry a public ABCP error.  It belongs to the same recovery family
    as the platform's identity codes, but remains distinguishable in the
    ledger.  Other entries come from the public error classification rather
    than prose matching.
    """
    if str(result.get("status") or "") == "stale_element_reference":
        return _SELECT_IDENTITY_STALE_AXTREE_CODE
    classification = result.get("errorClassification")
    code = (
        str(classification.get("errorCode") or "")
        if isinstance(classification, dict)
        else ""
    )
    return code if code in _SELECT_IDENTITY_FAILURE_CODES else ""


def _select_current_axtree_epoch(agent: Any, page_id: str) -> Optional[int]:
    """The latest successful AX observation for exactly this page, if any."""
    if str(getattr(agent, "axtree_page_id", "") or "") != page_id:
        return None
    try:
        return int(getattr(agent, "axtree_epoch", 0) or 0)
    except (TypeError, ValueError):
        return None


def _select_identity_entry_parts(
    entry: Any,
) -> Tuple[int, Optional[int], frozenset, frozenset, bool, frozenset]:
    """Read an identity-ledger entry without making its tuple layout public."""
    if not isinstance(entry, tuple) or len(entry) < 5:
        return 0, None, frozenset(), frozenset(), False, frozenset()
    failures = _select_failure_count(entry)
    observed_epoch = entry[1] if isinstance(entry[1], int) else None
    locators = entry[2] if isinstance(entry[2], frozenset) else frozenset()
    methods = entry[3] if isinstance(entry[3], frozenset) else frozenset()
    error_codes = entry[5] if len(entry) >= 6 and isinstance(entry[5], frozenset) else frozenset()
    return failures, observed_epoch, locators, methods, bool(entry[4]), error_codes


def _select_identity_recovery_for_locators(
    agent: Any, page_id: str, locators: frozenset,
) -> Optional[JsonDict]:
    """Return the unresolved repeated-identity fact for a proven locator match.

    This is intentionally a fact query rather than a gate.  Visual recovery
    uses it to avoid promoting pixels back to a control identity that the
    select path repeatedly could not use; no caller is refused by this helper.
    """
    ledger = getattr(agent, "_select_identity_failure_ledger", None)
    if not isinstance(ledger, dict) or not locators:
        return None
    epoch = _select_page_epoch(agent, page_id)
    candidates: List[JsonDict] = []
    for key, entry in ledger.items():
        if not isinstance(key, tuple) or len(key) < 4 or key[:2] != (page_id, epoch):
            continue
        failures, _, known, methods, generic_ui, error_codes = _select_identity_entry_parts(entry)
        if not generic_ui or not (known & locators):
            continue
        candidates.append({
            "errorCodes": sorted(str(code) for code in error_codes),
            "failureCount": failures,
            "methods": sorted(str(method) for method in methods),
            "genericUiRecommended": True,
        })
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item["failureCount"]))


def _select_clear_identity_failures(
    agent: Any, page_id: str, epoch: int, locators: frozenset,
) -> List[JsonDict]:
    """Retire identity failures that a successful native select read resolved."""
    ledger = getattr(agent, "_select_identity_failure_ledger", None)
    if not isinstance(ledger, dict) or not locators:
        return []
    cleared: List[JsonDict] = []
    for key, entry in list(ledger.items()):
        if not isinstance(key, tuple) or len(key) < 4 or key[:2] != (page_id, epoch):
            continue
        failures, _, known, _, _, error_codes = _select_identity_entry_parts(entry)
        if not (known & locators):
            continue
        ledger.pop(key, None)
        cleared.append({
            "errorCodes": sorted(str(code) for code in error_codes),
            "failureCount": failures,
        })
    return cleared


def _apply_select_identity_failure_guidance(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> bool:
    """Record a select control-identity failure and attach recovery facts.

    Returns whether it handled the receipt.  It does not interact with the
    Input.select replay guard: a public identity failure has no public
    dispatch-position fact, so it cannot truthfully arm or clear that guard.
    """
    code = _select_identity_error_code(result)
    if not code:
        return False
    page_id = str(params.get("pageId") or "")
    epoch = _select_page_epoch(agent, page_id)
    _select_drop_superseded_epochs(agent, page_id, epoch)
    target = str(params.get("selector") or params.get("id") or "<unknown>")
    locators = _select_expand_locators(
        agent, page_id, epoch, _select_call_locators(params),
    )
    ledger = getattr(agent, "_select_identity_failure_ledger", None)
    if not isinstance(ledger, dict):
        ledger = {}
        setattr(agent, "_select_identity_failure_ledger", ledger)
    key = (page_id, epoch, target, _SELECT_IDENTITY_FAILURE_FAMILY)
    prior = ledger.get(key)
    (
        prior_failures,
        prior_observation_epoch,
        prior_locators,
        prior_methods,
        prior_generic_ui,
        prior_codes,
    ) = (
        _select_identity_entry_parts(prior)
    )
    current_observation_epoch = _select_current_axtree_epoch(agent, page_id)
    observed_since_prior = bool(
        prior_failures
        and current_observation_epoch is not None
        and prior_observation_epoch is not None
        and current_observation_epoch > prior_observation_epoch
    )
    failures = prior_failures + 1
    # Re-observation has already proved that this control's native identity
    # did not recover.  Later failures without another AX read cannot undo
    # that fact; only a successful native inspection/selection clears this
    # ledger row.  Otherwise a third retry would re-advertise the exact id the
    # recovery path had just told the model to leave behind.
    generic_ui = prior_generic_ui or (
        failures > _SELECT_IDENTITY_NATIVE_RECOVERY_BUDGET
        and observed_since_prior
    )
    ledger[key] = (
        failures,
        current_observation_epoch,
        frozenset(set(prior_locators) | set(locators)),
        frozenset(set(prior_methods) | {method}),
        generic_ui,
        frozenset(set(prior_codes) | {code}),
    )
    recovery: JsonDict = {
        "errorCode": code,
        "errorCodes": sorted(str(item) for item in (set(prior_codes) | {code})),
        "failureCount": failures,
        "nativeRecoveryBudget": _SELECT_IDENTITY_NATIVE_RECOVERY_BUDGET,
        "observedSincePreviousFailure": observed_since_prior,
        "genericUiRecommended": generic_ui,
        "controlTarget": target,
        "methodsSeen": sorted(str(item) for item in (set(prior_methods) | {method})),
        "dispatchPosition": "not_publicly_known",
    }
    result["selectIdentityRecovery"] = recovery
    if generic_ui:
        instruction = _SELECT_IDENTITY_GENERIC_UI_LADDER
    elif failures > _SELECT_IDENTITY_NATIVE_RECOVERY_BUDGET:
        instruction = (
            "The same control identity failed again, but there is no successful"
            " fresh DOM.getAXTree after the immediately preceding failure. Read"
            " a current AX tree for this page before deciding whether to use the"
            " one native recovery pass or the ordinary UI ladder; do not treat"
            " an unchanged cached tree as new evidence."
        )
    else:
        instruction = (
            "The select control identity could not be resolved. Refresh"
            " DOM.getAXTree for this page and inspect the current control and"
            " value before deciding whether one corrected native select attempt"
            " still fits. Do not automatically replay the failed operation; the"
            " public receipt does not state how far a custom select operation"
            " progressed."
        )
    # An upstream instruction may have evidence this bookkeeping layer did not
    # see, such as a stronger recovery fact from the dispatch path.  Attach the
    # structured identity record in every case, but do not contradict that
    # instruction with a lower-layer summary.
    if not str(result.get("next_instruction") or "").strip():
        result["next_instruction"] = instruction
    return True


def _select_replay_blocked(agent: Any, params: JsonDict) -> Optional[JsonDict]:
    """The unresolved failure that forbids selecting this control, if any.

    Scoped to the CONTROL, not to the exact request. An earlier version keyed
    this on a fingerprint of the `selections` payload so that "a different
    option" would pass straight through, and that was wrong three ways at once:

      * it contradicted the harness's own L5 rule, which the worker prompt
        states in the same session - when a dispatched state-changing action
        has an uncertain outcome, changing the params does NOT make a second
        dispatch safe. A select failure never says whether keys were sent, so
        the outcome is precisely uncertain;
      * one ledger row held one fingerprint, so a second failure on the same
        control overwrote the first and un-blocked it;
      * it made the guard's own test assert the contradiction as intended
        behaviour, which is worse than having no guard.

    Blocking every selection on that control costs the model nothing it is
    entitled to: the contract already requires a fresh DOM.inspectSelect before
    the corrected attempt, and that inspection is what clears this.

    How far "the control" reaches is bounded by what the harness can PROVE. The
    API accepts an id alone or a selector alone, so two calls can name one
    element with disjoint locator sets. A successful inspection links the names
    it was given to the `controlId` ABCP resolved (see
    _select_learn_control_aliases), and the block follows that link within the
    same navigation epoch. Where no inspection ever linked two names, they stay
    unlinked and this does not reach across them - which is a real limit, not a
    hole to paper over: the harness has no evidence they are the same element,
    and claiming otherwise would be the kind of guarantee that reads as
    mechanical and is not.
    """
    ledger = getattr(agent, "_select_failure_ledger", None)
    if not isinstance(ledger, dict):
        return None
    page_id = str((params or {}).get("pageId") or "")
    epoch = _select_page_epoch(agent, page_id)
    # Raw locators, deliberately. Expansion happens ONCE, when the failure is
    # recorded, so the ledger already holds every name known to denote that
    # control at that moment. Expanding again here changed no outcome - the
    # only aliases it could add are ones learned by an inspection since, and an
    # inspection clears the block outright - so it was a second copy of the
    # rule that could not fail: dead code wearing a safeguard's clothes.
    locators = _select_call_locators(params)
    for key, entry in ledger.items():
        if not isinstance(entry, tuple) or len(entry) < 3:
            continue
        blocked, failure_locators = entry[1], entry[2]
        # The epoch is part of the key. A failure recorded before a navigation
        # describes a page that no longer exists: its uncertain side effect
        # cannot still be pending on the document now loaded, and the harness
        # already forces re-observation across a navigation by other means. An
        # earlier version compared pageId alone and called the resulting
        # over-block "fail-closed" - which held for the BLOCK and was simply
        # wrong for the COUNT, since a budget burnt on the old page then denied
        # the new one its one corrected attempt.
        if not blocked or key[:2] != (page_id, epoch):
            continue
        if not (locators & failure_locators):
            continue
        return {"errorCode": key[3], "controlTarget": key[2], "failureCount": entry[0]}
    return None


def _select_clear_replay_block(
    agent: Any, page_id: str, epoch: int, locators: frozenset
) -> List[JsonDict]:
    """Lift the replay block this inspection has just earned, and SAY so.

    An entry qualifies only when the inspection names the same control - any
    shared locator, including the platform's own controlId. No shared name
    means no proof and no lift: the model that inspects one element and selects
    a different one is exactly the case a bookkeeping shortcut would get wrong.

    "After the failure" needs no check of its own: this runs ONLY from a
    successful inspection, so an inspection that happened before the failure
    lifts nothing simply because there was no ledger entry then. An earlier
    version carried an explicit ordering comparison that could never be true -
    a safeguard in appearance, dead code in fact.

    Two things this must keep straight, because conflating them is what the
    previous version got wrong:

      * the BLOCK is about state. It asks "is the outcome of the last
        selection still unknown?", and a successful inspection answers that for
        every code, including the ones whose retry budget is zero. So the block
        lifts here regardless of budget - but it is REPORTED every single time,
        because a permission that changes silently is how a mechanical signal
        stops meaning anything. The zero-budget codes used to have their
        fingerprint retired before the budget check and then be dropped from
        the report: block gone, receipt silent.
      * the BUDGET is about advice. `retryAllowed` says whether selecting again
        is a licensed recovery for this code at all, and it stays False for a
        zero-budget code even though the block is gone. The ladder in
        next_instruction is still the recovery.

    The row is kept (blocked -> False) rather than deleted, so the failure
    COUNT survives: deleting it turned fail -> inspect -> fail -> inspect into
    an unbounded retry loop wearing the budget's clothes.
    """
    ledger = getattr(agent, "_select_failure_ledger", None)
    if not isinstance(ledger, dict):
        return []
    cleared: List[JsonDict] = []
    for key, entry in list(ledger.items()):
        if not isinstance(entry, tuple) or len(entry) < 3:
            continue
        failures, blocked, failure_locators = entry[0], entry[1], entry[2]
        if not blocked or key[:2] != (page_id, epoch):
            continue
        if not (locators & failure_locators):
            continue
        ledger[key] = (failures, False, failure_locators)
        budget = SELECT_FAILURE_RETRY_LIMITS.get(key[3], 0)
        retry_allowed = failures <= budget
        cleared.append({
            "errorCode": key[3],
            "controlTarget": key[2],
            "replayBlockLifted": True,
            "retryAllowed": retry_allowed,
            "retriesRemaining": max(0, budget - failures + 1),
            "scope": (
                "One corrected Input.select using only fields THIS response"
                " returned. It is not a licence to replay the previous"
                " request."
            ) if retry_allowed else (
                "The state is re-observed, so this control is no longer"
                " refused before dispatch - but this code licenses no"
                " corrected selection. Follow next_instruction from the"
                " failure instead of selecting again."
            ),
        })
    return cleared


def _select_replay_guard_before(
    agent: Any, method: str, params: JsonDict
) -> Optional[JsonDict]:
    """Refuse any Input.select on a control whose last selection failed.

    The one place the select contract is mechanically enforced rather than
    advised. It exists because `replayForbidden=True` was a hard claim with no
    hard backing: the model could resend the identical selection immediately,
    and the harness would dispatch it and then explain afterwards why it should
    not have.

    Refusing before dispatch is what makes the refusal worth anything - the
    keys are not sent, so `tool_was_executed` is False and the model can act on
    that without compensating for a side effect. Only a fresh
    DOM.inspectSelect lifts it; naming a different option does not, because the
    harness's own L5 rule says changing params cannot make a second dispatch
    safe while the first one's outcome is unknown.
    """
    if method != "Input.select":
        return None
    blocked = _select_replay_blocked(agent, params)
    if blocked is None:
        return None
    error_code = str(blocked.get("errorCode") or "")
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("browser.call.select_replay_blocked", blocked)
    return {
        "status": "rejected",
        "policy_violation": "select_replay_without_fresh_inspection",
        "tool_was_executed": False,
        "error": (
            f"This control's last selection failed with {error_code} on"
            f" {blocked.get('controlTarget')}, and that failure does not say"
            " whether keys were sent. Changing the option does not make a"
            " second dispatch safe while the outcome is unknown, so this"
            " Input.select was NOT dispatched. Re-observe first."
        ),
        "selectRecovery": {
            "errorCode": error_code,
            "controlTarget": blocked.get("controlTarget"),
            "failureCount": blocked.get("failureCount"),
            "retryAllowed": False,
            "retryUnlockedBy": (
                "a successful DOM.inspectSelect on this control, whose receipt"
                " will carry selectReplayBlockCleared. Until then no selection"
                " on this control is dispatched, whichever option it names."
            ),
        },
        "next_instruction": _SELECT_FAILURE_NEXT_INSTRUCTION.get(
            error_code,
            "Re-observe the control before selecting again.",
        ),
    }


def _apply_select_failure_guidance(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> JsonDict:
    """Attach select recovery, then report the block state as it now stands.

    The state has to be read AFTER the body, on every return path. The flag
    used to be written at the top as `method == "Input.select"`, which is not a
    state at all - it made a successful selection with an empty ledger report
    that it was blocked, advertising a policy as if it were a fact about this
    control right now.
    """
    from .target_recovery import attach_target_recovery

    outcome = _apply_select_failure_guidance_inner(agent, method, params, result)
    attach_target_recovery(agent, method, params, outcome)
    if isinstance(outcome, dict):
        guard = outcome.get("selectGuard")
        if isinstance(guard, dict) and method == "Input.select":
            guard["blockedNow"] = _select_replay_blocked(agent, params) is not None
    return outcome


def _apply_select_failure_guidance_inner(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> JsonDict:
    """Attach code-specific, mechanically bounded Input.select recovery."""

    if not isinstance(result, dict):
        return result
    if method in {"DOM.inspectSelect", "Input.select"}:
        # `freshInspectAssociationEnforced` used to be a hard False while the
        # very same receipt handed out retryAllowed=True beside a
        # replayForbidden=True classification - three mechanical signals, two
        # of them contradicting each other, and the tie broken only by prose.
        # Every flag here has to name something a reader can go and find. The
        # previous pair failed that twice over: `freshInspectAssociationEnforced`
        # was a hard False beside a live retryAllowed=True, and then a hard
        # True whose only backing was advisory text - `selectRetryUnlocked` was
        # written onto an inspect receipt and read by nobody.
        #
        # What is enforced now is narrow and real: while a control's last
        # selection has failed, EVERY Input.select on it is refused before
        # dispatch (_select_replay_guard_before) - naming a different option
        # does not help, because L5 says changing params cannot make a second
        # dispatch safe while the first outcome is unknown. A successful
        # DOM.inspectSelect lifts it, and says so on its receipt.
        #
        # Everything above that stays advisory: the harness cannot prove the
        # popup is the same open generation - the platform's relationId is not
        # in the public projection - so a fresh inspection proves
        # re-observation happened, not that the menu is unchanged.
        result["selectGuardMode"] = "advisory"
        result["selectGuard"] = {
            "requestShapeValidated": method == "Input.select",
            # POLICY, not state: this Action is subject to the pre-dispatch
            # block. Whether it is blocked RIGHT NOW is `blockedNow`, written
            # by the wrapper after the ledger has been updated. The two used to
            # be one field, so a successful selection reported itself blocked.
            "selectionBlockPolicyApplies": method == "Input.select",
            "popupVisibilityEnforced": False,
            "genericInputFallbackAvailable": True,
        }
    if method in {"DOM.inspectSelect", "Input.select"} and _apply_select_identity_failure_guidance(
        agent, method, params, result,
    ):
        return result
    if method == "DOM.inspectSelect":
        classification = result.get("errorClassification")
        error_code = (
            str(classification.get("errorCode") or "")
            if isinstance(classification, dict)
            else ""
        )
        if error_code in _SELECT_FAILURE_NEXT_INSTRUCTION:
            result["next_instruction"] = _SELECT_FAILURE_NEXT_INSTRUCTION[error_code]
            result["selectRecovery"] = {
                "errorCode": error_code,
                "retryAllowed": False,
            }
            return result
        if _bt()._invoke_result_failed(result):
            return result
        # Success path. The keyboard-driven contract returns controlId /
        # controlKind / selectionMode / options and nothing else: optionWindow,
        # popup and expanded were all removed, and with them every signal this
        # branch used to read. Reading them anyway did not fail loudly - it
        # silently stopped invalidating the AX snapshot.
        data = _bt()._response_data(result)
        control_kind = str(data.get("controlKind") or "")
        options = data.get("options") if isinstance(data.get("options"), list) else []
        # Facts the result states, and nothing derived from them. A custom
        # inspect USUALLY drives the menu with real key presses, but not always:
        # when the platform's exploration cache is still valid and the menu was
        # already expanded, it opens nothing, walks nothing, and restores
        # nothing. `controlKind` is what the contract guarantees, so a
        # "this call sent keys" flag would be an assertion the receipt cannot
        # support - and the reader already has controlKind.
        #
        # The snapshot itself is handled by _observe_axtree_state_after, which
        # runs after this and can see a mid-call DOM.axTreeUpdated that this
        # function cannot. Invalidating here would bypass that.
        result["selectWindow"] = {
            "controlId": data.get("controlId"),
            "controlKind": data.get("controlKind") or None,
            "selectionMode": data.get("selectionMode") or None,
            "optionCount": len(options),
        }
        # This is the event a corrected retry waits for. Matched on every name
        # this call used plus the controlId ABCP returned, because the
        # selection that failed may have named the control differently.
        #
        # Clearing the entry is what makes the lift one-shot: a second
        # inspection re-announces nothing because there is nothing still
        # blocked. A permission that can be re-harvested is not a permission.
        page_id = str(params.get("pageId") or "")
        epoch = _select_page_epoch(agent, page_id)
        locators = _select_call_locators(params, result)
        # Before anything else, and unconditionally: an inspection that returns
        # only one usable name still proves this page is on a new epoch, and
        # the alias learner would return early before pruning.
        _select_drop_superseded_epochs(agent, page_id, epoch)
        _select_learn_control_aliases(agent, page_id, epoch, locators)
        identity_cleared = _select_clear_identity_failures(
            agent, page_id, epoch, locators,
        )
        if identity_cleared:
            result["selectIdentityRecoveryCleared"] = identity_cleared
        cleared = _select_clear_replay_block(agent, page_id, epoch, locators)
        if cleared:
            result["selectReplayBlockCleared"] = cleared
        if control_kind == "custom":
            # The platform's own suggested_prompt carries the startOption
            # continuation token when exploration is incomplete; it travels in
            # the same result and must not be restated or contradicted here.
            # This says only what the platform cannot know: which harness-held
            # state a custom inspection may have invalidated.
            result["next_instruction"] = (
                "Inspecting a custom select can drive its menu with real key"
                " presses, so element ids captured before this call may be"
                " stale: read DOM.getAXTree again before targeting anything"
                " else on this page. Pass a returned option id, exact label, or"
                " explicit value straight to Input.select without converting"
                " between those fields. If this response's suggested_prompt"
                " asks to continue exploring, repeat this Action with the"
                " startOption it names instead of starting over."
            )
        return result
    if method != "Input.select":
        return result
    target = str(params.get("selector") or params.get("id") or "<unknown>")
    page_id = str(params.get("pageId") or "")
    ledger = getattr(agent, "_select_failure_ledger", None)
    if not _bt()._invoke_result_failed(result):
        locators = _select_call_locators(params)
        epoch = _select_page_epoch(agent, page_id)
        _select_drop_superseded_epochs(agent, page_id, epoch)
        identity_cleared = _select_clear_identity_failures(
            agent, page_id, epoch, locators,
        )
        if identity_cleared:
            result["selectIdentityRecoveryCleared"] = identity_cleared
        # Clear by CONTROL, matching the locator set the row carries - the same
        # comparison the guard and the clear path already use. This was the one
        # place still keying on the raw `target` string, so a selection that
        # succeeded under the control's id left a failure recorded under its
        # selector standing: the next failure counted as the second, and a
        # budget of one was spent by a control that had meanwhile worked.
        if isinstance(ledger, dict):
            # Prune first, then match on the control. An epoch comparison in
            # the loop below would change no outcome once the prune has run -
            # a stale row can neither block nor count anywhere else - so it
            # would be one more line that reads like a safeguard and cannot
            # fail. The prune, by contrast, is the only thing that clears a
            # page's rows when the worker navigates and then simply selects
            # again without inspecting.
            for key, entry in list(ledger.items()):
                if key[0] != page_id:
                    continue
                known = (
                    entry[2]
                    if isinstance(entry, tuple) and len(entry) >= 3
                    else frozenset({key[2]})
                )
                if locators & known:
                    ledger.pop(key, None)
        return result
    classification = result.get("errorClassification")
    error_code = (
        str(classification.get("errorCode") or "")
        if isinstance(classification, dict)
        else ""
    )
    max_retries = SELECT_FAILURE_RETRY_LIMITS.get(error_code)
    if max_retries is None:
        return result
    # The same ladder the inspect path gets. A selection failure used to leave
    # with a bare selectRecovery block - a code, a count and a boolean - while
    # the prose explaining what to do about that code went only to inspections.
    # `next_instruction` is set only when nothing upstream wrote one, because
    # an earlier layer that already spoke saw more of this call than this does.
    if error_code in _SELECT_FAILURE_NEXT_INSTRUCTION and not str(
        result.get("next_instruction") or ""
    ).strip():
        result["next_instruction"] = _SELECT_FAILURE_NEXT_INSTRUCTION[error_code]
    if not isinstance(ledger, dict):
        ledger = {}
        setattr(agent, "_select_failure_ledger", ledger)
    epoch = _select_page_epoch(agent, page_id)
    _select_drop_superseded_epochs(agent, page_id, epoch)
    key = (page_id, epoch, target, error_code)
    failures = int(_select_failure_count(ledger.get(key))) + 1
    ledger[key] = (
        failures,
        True,
        _select_expand_locators(agent, page_id, epoch, _select_call_locators(params)),
    )
    # retryAllowed is False on the failure itself - always, for every code.
    # The action for the retryable codes is "reinspect the current window THEN
    # retry once", and at this instant that reinspection has not happened: the
    # inspection the model is holding is the one whose data just turned out to
    # be stale. Saying True here put "you may select again" on the same receipt
    # as replayForbidden=True, and left the model to break the tie on prose.
    #
    # The permission is issued later, on the successful DOM.inspectSelect that
    # actually clears the condition, as `selectRetryUnlocked`.
    budget_left = failures <= max_retries
    recovery: JsonDict = {
        "errorCode": error_code,
        "failureCount": failures,
        "maxRetries": max_retries,
        "retryAllowed": False,
        "controlTarget": target,
    }
    if budget_left:
        recovery["retryUnlockedBy"] = (
            "a successful DOM.inspectSelect on this control. Its receipt will"
            " carry selectReplayBlockCleared. Until then no selection on this"
            " control is dispatched, whichever option it names."
        )
    else:
        recovery["retryUnlockedBy"] = (
            "nothing - the retry budget for this code is exhausted. A fresh"
            " DOM.inspectSelect still lifts the pre-dispatch block (the state"
            " becomes known again) but licenses no corrected selection:"
            " recover with the ladder in next_instruction."
        )
    result["selectRecovery"] = recovery
    return result
