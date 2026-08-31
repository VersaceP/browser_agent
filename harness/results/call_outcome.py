"""
harness.results.call_outcome - one verdict for "did this browser call succeed?".

Every state mutation the harness performs off the back of a browser call —
consuming a model declaration, binding a landing page, crediting recovery,
discharging a signal — is only legitimate when the call actually succeeded. That
question has three independent parts:

* a harness guard may have refused before dispatch (``tool_was_executed=False``)
* the call may have failed at transport or in the browser (``error`` at the top
  level or nested in ``response``)
* the call may have been interrupted by an auto-HITL / challenge pause

Each of those used to be re-derived at every call site, and every new code path
got a slightly different subset right: one checked none of the three and
credited route recovery for a transport reset; another checked two and flattened
the third, dropping a HITL terminal state into a bare "unreadable". Both were
the same defect, found weeks apart.

The answer has two layers. ``classify_call_outcome`` fail-closes the common
execution envelope. ``evaluate_grant`` then applies a policy registered for the
specific state transition, because Page.close, Page.list, content binding, and
route-recovery credit require different method evidence. Unregistered grant
kinds are denied. This keeps new grant sites safe by default without pretending
that a generic successful RPC proves its business effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from harness.utils import JsonDict


SUCCEEDED = "succeeded"
INTERRUPTED = "interrupted"
FAILED = "failed"
NOT_DISPATCHED = "not_dispatched"


def public_action_failure(value: Any) -> Optional[JsonDict]:
    """Return ABCP's stable public ``error.code`` failure envelope.

    A top-level harness ``error`` is often a string, so only an object-shaped
    error carrying a non-empty code qualifies. Observation, suggested prompt and
    the public ``details`` are copied as bounded public guidance; private
    runtime fields are not on the wire to begin with.
    """
    for candidate in _public_failure_candidates(value):
        error = candidate.get("error") if isinstance(candidate, dict) else None
        if not isinstance(error, dict):
            continue
        raw_code = error.get("code")
        if not isinstance(raw_code, str) or not raw_code.strip():
            continue
        code = raw_code.strip()
        failure: JsonDict = {"code": code}
        message = str(error.get("message") or "").strip()
        if message:
            failure["message"] = message
        for key in ("observation", "suggested_prompt"):
            if candidate.get(key) not in (None, "", {}):
                failure[key] = candidate[key]
        details = public_failure_details(candidate.get("details"))
        if details:
            failure["details"] = details
        return failure
    return None


# ABCP's own projection already bounds `details`: it copies only the fields a
# code registered (`page-not-ready` -> status, `state-conflict` -> blockedBy /
# status / restartToken, `memory-revision-conflict` -> currentRevision,
# `invalid-params` -> issues) and only when the value is a scalar. Re-applying
# the shape rule here keeps the harness fail-closed against a build that
# widens it, without discarding the operative fact — "which field was invalid",
# "what is the current revision" — that the model needs to correct its call.
_PUBLIC_DETAIL_MAX_FIELDS = 8
_PUBLIC_DETAIL_MAX_ISSUES = 20
_PUBLIC_DETAIL_MAX_PATH_PARTS = 20
_PUBLIC_DETAIL_MAX_STRING_CHARS = 300


def public_failure_details(value: Any) -> Optional[JsonDict]:
    """Scalar-only view of a public failure's ``details``."""
    if not isinstance(value, dict) or not value:
        return None
    projected: JsonDict = {}
    for key, item in value.items():
        if len(projected) >= _PUBLIC_DETAIL_MAX_FIELDS:
            break
        name = str(key)
        if key == "issues" and isinstance(item, list):
            issues = []
            for entry in item[:_PUBLIC_DETAIL_MAX_ISSUES]:
                if not isinstance(entry, dict):
                    continue
                issue: JsonDict = {}
                path = entry.get("path")
                if isinstance(path, list):
                    issue["path"] = [
                        part[:_PUBLIC_DETAIL_MAX_STRING_CHARS]
                        if isinstance(part, str) else part
                        for part in path[:_PUBLIC_DETAIL_MAX_PATH_PARTS]
                        if isinstance(part, (str, int, float))
                    ]
                code = entry.get("code")
                if isinstance(code, str) and code.strip():
                    issue["code"] = code[:_PUBLIC_DETAIL_MAX_STRING_CHARS]
                if issue:
                    issues.append(issue)
            issues = [entry for entry in issues if entry]
            if issues:
                projected["issues"] = issues
            continue
        if isinstance(item, bool) or isinstance(item, (int, float)):
            projected[name] = item
        elif isinstance(item, str) and item.strip():
            projected[name] = item[:_PUBLIC_DETAIL_MAX_STRING_CHARS]
    return projected or None


def _public_failure_candidates(value: Any) -> Tuple[JsonDict, ...]:
    if not isinstance(value, dict):
        return ()
    candidates = [value]
    for key in ("rpcData", "response", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            candidates.extend(_public_failure_candidates(nested))
    return tuple(candidates)


def replay_forbidden(result: Any) -> bool:
    """True when a failed action's browser-side outcome is unknown.

    The public failure contract deliberately does NOT say whether input
    dispatch had already started: `runtime.sideEffectStarted`, `phase` and
    every other execution marker are stripped at the transport boundary. So a
    dispatched action that failed may have moved the page — re-issuing it can
    submit a form twice or double-click a control — and any public failure is
    enough to forbid an automatic composite replay.
    """
    if not isinstance(result, dict):
        return False
    if result.get("tool_was_executed") is False:
        return False
    if result.get("requestSent") is False:
        return False
    if public_action_failure(result) is not None:
        return True

    # A response-free transport failure and a bare JSON-RPC failure provide no
    # execution receipt. They still prove the request was dispatched (or that
    # delivery is indeterminate), so a composite must not convert the unknown
    # outcome into a second click, submit, or Escape press. A pre-dispatch
    # guard carries tool_was_executed=False and returned above.
    response = result.get("response")
    response_error = response.get("error") if isinstance(response, dict) else None
    failed = bool(result.get("error") or response_error)
    transport_or_rpc = (
        result.get("rpcCode") is not None
        or bool(str(result.get("transportCode") or "").strip())
    )
    # An explicit successful send also makes the final action state unknown,
    # even if a lower layer did not attach its usual transport/RPC code.
    return failed and (transport_or_rpc or result.get("requestSent") is True)


@dataclass(frozen=True)
class CallOutcome:
    """What a browser call is known to have done."""

    verdict: str
    error: str = ""
    auto_hitl: Optional[JsonDict] = None

    @property
    def succeeded(self) -> bool:
        return self.verdict == SUCCEEDED

    @property
    def interrupted(self) -> bool:
        return self.verdict == INTERRUPTED

    @property
    def dispatched(self) -> bool:
        return self.verdict != NOT_DISPATCHED

    def receipt(self) -> JsonDict:
        payload: JsonDict = {"callVerdict": self.verdict}
        if self.error:
            payload["error"] = self.error[:300]
        return payload


@dataclass(frozen=True)
class GrantDecision:
    """Whether one specific state-granting transition has enough evidence."""

    allowed: bool
    kind: str
    call_outcome: CallOutcome
    reason: str = ""

    def receipt(self) -> JsonDict:
        return {
            "grantKind": self.kind,
            "grantAllowed": self.allowed,
            "callVerdict": self.call_outcome.verdict,
            "grantReason": self.reason or None,
        }


FAILURE_STATUSES = {
    "aborted",
    "failed",
    "blocked",
    "cancelled",
    "canceled",
    "error",
    "not_found",
    "stale_element_reference",
    "page_busy",
    "fleet_busy",
    "page_quarantined",
    "page_binding_violation",
    "rejected",
    "timeout",
    "unavailable",
}

_ACK_KEYS = ("success", "ok", "accepted")


def _error_present(value: Any) -> Tuple[bool, str]:
    """Whether an error field is set, and its text if it has any.

    An error object carrying only a numeric code has no message; reading its
    text and testing THAT for truthiness reported such a result as clean, which
    let a bare ``-32005`` pass as success.
    """
    if value is None or value is False or value == "" or value == {}:
        return False, ""
    if isinstance(value, dict):
        text = str(value.get("message") or value.get("error") or "")
        if not text and value.get("code") is not None:
            text = f"error code {value.get('code')}"
        return True, text
    return True, str(value)


def _explicit_negative(mapping: Any) -> Tuple[bool, str]:
    """Return an explicit negative acknowledgement from one result layer."""
    if not isinstance(mapping, dict):
        return False, ""
    for key in _ACK_KEYS:
        if mapping.get(key) is False:
            return True, f"{key}=false"
    return False, ""


def _response_has_positive_evidence(response: Any) -> bool:
    """Whether a response contains a non-contradictory browser-produced value.

    Key presence alone is not evidence: ``success=False``, ``data=None`` and an
    empty execution id are all explicit counterexamples. Empty lists are kept
    as evidence because list-returning methods such as Page.list legitimately
    succeed with zero rows; an empty dict is only an envelope.
    """
    if not isinstance(response, dict):
        return False
    if any(response.get(key) is True for key in _ACK_KEYS):
        return True
    if str(response.get("observation") or "").strip():
        return True
    if str(response.get("executionId") or "").strip():
        return True
    if "result" in response and response.get("result") is not None:
        return True
    if "data" not in response:
        return False
    data = response.get("data")
    if data is None:
        return False
    if isinstance(data, dict):
        return bool(data)
    if isinstance(data, str):
        return bool(data.strip())
    # [] is a meaningful, successful empty result for list-returning methods;
    # False and 0 can likewise be legitimate scalar read results.
    return True


def response_data(result: Any) -> Any:
    if not isinstance(result, dict):
        return None
    response = result.get("response")
    return response.get("data") if isinstance(response, dict) else None


def page_list_evidence_ok(result: Any) -> bool:
    """A Page.list inventory statement, including a valid empty list."""
    data = response_data(result)
    if isinstance(data, list):
        return True
    return bool(isinstance(data, dict) and isinstance(data.get("pages"), list))


def page_state_evidence_ok(page_id: str, result: Any) -> bool:
    """Page state for the guarded target; an optional id echo must agree."""
    data = response_data(result)
    if not isinstance(data, dict):
        return False
    echoed = str(data.get("pageId") or data.get("page_id") or "").strip()
    if echoed and echoed != str(page_id or "").strip():
        return False
    # `navigationId` is gone from the agent-facing page state; `failure` is the
    # field that replaced the old error pair and is itself proof the browser
    # answered about this page.
    for key in ("url", "currentUrl", "title", "status"):
        if str(data.get(key) or "").strip():
            return True
    return any(
        isinstance(data.get(key), dict) and bool(data.get(key))
        for key in ("hitl", "blockingInteractions", "failure")
    )


def page_create_evidence_ok(page_id: str, result: Any) -> bool:
    data = response_data(result)
    if not isinstance(data, dict):
        return False
    created = str(data.get("pageId") or data.get("page_id") or "").strip()
    return bool(created and created == str(page_id or "").strip())


def page_close_evidence_ok(page_id: str, result: Any) -> bool:
    data = response_data(result)
    if not isinstance(data, dict) or data.get("closed") is not True:
        return False
    closed_page = str(data.get("pageId") or data.get("page_id") or "").strip()
    return bool(closed_page and closed_page == str(page_id or "").strip())


def route_recovery_claim_evidence_ok(page_id: str, result: Any) -> bool:
    """Stronger Page.getState evidence required before recovery credit."""
    if not page_state_evidence_ok(page_id, result):
        return False
    data = response_data(result)
    return bool(
        isinstance(data, dict)
        and any(
            str(data.get(key) or "").strip()
            for key in ("url", "currentUrl", "title", "status")
        )
    )


_STRUCTURED_READ_EVIDENCE_KEYS = {
    "DOM.getText": {"items", "text", "textContent"},
    "DOM.getAttribute": {"items", "attributes", "value", "values"},
    # getSemanticTree returns a frame graph: the payload lives in `frames`,
    # the count in `summary`. Neither `tree` nor a top-level `nodeCount`
    # appears, so scoring this read by those keys marks every success empty.
    "DOM.getSemanticTree": {"frames", "rootFrameId", "summary"},
    "DOM.getAXTree": {"lines", "nodeCount", "nodes", "outline", "tree"},
}


def _substantive_content_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def structured_read_evidence_ok(method: str, result: Any) -> bool:
    if method == "collect_items":
        return bool(
            isinstance(result, dict)
            and str(result.get("status") or "") == "done"
            and any(
                _substantive_content_value(result.get(key))
                for key in ("items", "rows", "rowCount", "collectionState")
            )
        )
    data = response_data(result)
    if method == "Runtime.evaluate":
        # Runtime may return a scalar/array directly. A successful RPC with
        # data=None (the dominant deployed shape) is not content evidence.
        if not isinstance(data, dict):
            return _substantive_content_value(data)
        return any(
            _substantive_content_value(data.get(key))
            for key in ("json", "result", "value", "variables")
        )
    if not isinstance(data, dict):
        return False
    return any(
        _substantive_content_value(data.get(key))
        for key in _STRUCTURED_READ_EVIDENCE_KEYS.get(method, set())
    )


def classify_call_outcome(result: Any) -> CallOutcome:
    """Reduce a raw _invoke_browser_method result to a single verdict.

    Fail-closed by construction: SUCCEEDED requires positive evidence that the
    browser did something, not merely the absence of a recognised error. The
    first version inverted that — "no known error" meant success — so ``{}``,
    ``{"status": "failed"}`` and an error object with only a numeric code all
    read as clean, and one of them was enough to consume a route-recovery
    declaration for a page nobody had read.

    Order matters: a guard refusal is not a browser failure, and an actionable
    auto-HITL is not an ordinary error but a terminal state whose payload
    callers must keep rather than flatten.
    """
    if not isinstance(result, dict):
        return CallOutcome(FAILED, error="no result")
    if result.get("tool_was_executed") is False:
        return CallOutcome(
            NOT_DISPATCHED,
            error=str(result.get("error") or "")[:300],
        )
    auto_hitl = result.get("autoHitl")
    if auto_hitl_is_actionable(auto_hitl):
        return CallOutcome(INTERRUPTED, auto_hitl=auto_hitl)

    present, text = _error_present(result.get("error"))
    if present:
        return CallOutcome(FAILED, error=text)
    negative, text = _explicit_negative(result)
    if negative:
        return CallOutcome(FAILED, error=text)
    response = result.get("response")
    if isinstance(response, dict):
        present, text = _error_present(response.get("error"))
        if present:
            return CallOutcome(FAILED, error=text)
        negative, text = _explicit_negative(response)
        if negative:
            return CallOutcome(FAILED, error=text)
        data = response.get("data")
        if isinstance(data, dict):
            # `response.data.error` is deliberately NOT read here. It is page /
            # domain data, not a report about this call: Page.getState returns
            # the page's LAST NAVIGATION error there while itself succeeding.
            # Treating it as a call failure is what deadlocked task 48b4d7d7 —
            # 1688 bounced the desktop URL to its m-site, the page kept
            # replaying `ERR_ABORTED (-3)`, and every subsequent Page.getState
            # read as "failed" for 31 minutes, so fleet re-perception could
            # never complete and every other method stayed gated.
            # A method that really does report failure this way must say so in
            # its own GRANT_POLICIES entry; the generic layer cannot know.
            negative, text = _explicit_negative(data)
            if negative:
                return CallOutcome(FAILED, error=text)

    status = str(result.get("status") or "").strip().lower()
    if status in FAILURE_STATUSES:
        return CallOutcome(FAILED, error=status)

    if not _response_has_positive_evidence(response):
        return CallOutcome(
            FAILED,
            error="no response evidence that the call did anything",
        )
    return CallOutcome(SUCCEEDED)


GrantPolicy = Callable[[str, Any, str], bool]


def _method_policy(
    expected_method: str,
    evidence: Callable[[str, Any], bool],
) -> GrantPolicy:
    return lambda method, result, page_id: bool(
        method == expected_method and evidence(page_id, result)
    )


def _page_list_policy(method: str, result: Any, _page_id: str) -> bool:
    return method == "Page.list" and page_list_evidence_ok(result)


def _content_binding_policy(method: str, result: Any, _page_id: str) -> bool:
    return structured_read_evidence_ok(method, result)


# Registry keys describe the STATE GRANT, not merely the browser method. One
# method can support transitions with different evidence strengths: ordinary
# Page.getState observation and route-recovery credit are intentionally not the
# same contract. An unregistered kind is denied by evaluate_grant.
GRANT_POLICIES: Dict[str, GrantPolicy] = {
    "inventory_baseline": _page_list_policy,
    "inventory_discharge_page_list": _page_list_policy,
    "inventory_discharge_page_create": _method_policy(
        "Page.create", page_create_evidence_ok
    ),
    "inventory_discharge_page_close": _method_policy(
        "Page.close", page_close_evidence_ok
    ),
    "route_recovery_page_create": _method_policy(
        "Page.create", page_create_evidence_ok
    ),
    "route_recovery_claim": _method_policy(
        "Page.getState", route_recovery_claim_evidence_ok
    ),
    "content_binding": _content_binding_policy,
}


def evaluate_grant(
    *,
    kind: str,
    method: str,
    result: Any,
    page_id: str = "",
) -> GrantDecision:
    """Fail-closed state grant: call success AND registered method evidence."""
    outcome = classify_call_outcome(result)
    if not outcome.succeeded:
        return GrantDecision(
            False,
            kind,
            outcome,
            reason=f"call_{outcome.verdict}",
        )
    policy = GRANT_POLICIES.get(str(kind or ""))
    if policy is None:
        return GrantDecision(
            False,
            kind,
            outcome,
            reason="unregistered_grant_kind",
        )
    if not policy(str(method or ""), result, str(page_id or "")):
        return GrantDecision(
            False,
            kind,
            outcome,
            reason="method_evidence_missing_or_mismatched",
        )
    return GrantDecision(True, kind, outcome, reason="verified")


def auto_hitl_is_actionable(auto: Any) -> bool:
    """True only when an autoHitl entry represents a REAL pause request.

    A skipped / not-executed adjudication is a no-op: the page was never
    paused, so treating it as an interrupt tells the model to stop working on a
    page that is perfectly usable. This contract already existed in
    browser_tools; duplicating a weaker version here is what made every
    autoHitl, actionable or not, abort a navigation.
    """
    if not isinstance(auto, dict):
        return False
    if auto.get("tool_was_executed") is False:
        return False
    if str(auto.get("status") or "").lower().startswith("skipped"):
        return False
    return True
