"""
harness.diagnostics - Worker status taxonomy and terminal-status classifier.

The classifier consumes WorkerDiagnostics that BrowserAgent fills in during a
run, plus the model's self-reported status, and produces a final status that
LeadAgent can route on.

Priority of hard signals (see WORKER_STATUS_HARD_PRIORITY in constants.py):
    context_limit_exceeded
  > stale_pause_deadlock (ABCP pause flag cannot be cleared by resolvePause)
  > page_settled_after_hitl (page passed challenge but ABCP pause remains)
  > hitl_waiting           (Hitl.requestPause issued but harness never entered wait)
  > hitl_timeout           (harness entered wait but timed out — PR #4 wires this)
  > browser_api_contract_error
  > page_crashed
  > extraction_inconclusive (weak hard, only at step cap / checkpoint)
  > step_budget_exhausted   (default for step cap when no other hard signal)

Hard signals always override soft (model-reported) statuses.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from harness.constants import (
    API_CONTRACT_ERROR_MARKERS,
    API_CONTRACT_ERROR_THRESHOLD,
    CONTEXT_LIMIT_ERROR_MARKERS,
    EXTRACTION_FAIL_THRESHOLD,
    EXTRACTION_FAILURE_OBS_MARKERS,
    EXTRACTION_LOOKBACK,
    EXTRACTION_METHODS,
    MODEL_ALLOWED_SOFT_STATUSES,
    PAGE_CRASHED_FAIL_THRESHOLD,
    PAGE_CRASHED_LOOKBACK,
    PAGE_DEAD_OBSERVATION_MARKERS,
    WORKER_STATUS_API_CONTRACT_ERROR,
    WORKER_STATUS_BLOCKED_BY_CHALLENGE,
    WORKER_STATUS_CATEGORIES,
    WORKER_STATUS_CATEGORY_UNKNOWN,
    WORKER_STATUS_CONTEXT_LIMIT,
    WORKER_STATUS_DONE,
    WORKER_STATUS_EXTRACTION_INCONCLUSIVE,
    WORKER_STATUS_FLEET_ASSIGNMENT_LOST,
    WORKER_STATUS_HITL_TIMEOUT,
    WORKER_STATUS_HITL_REQUIRED,
    WORKER_STATUS_HITL_WAITING,
    WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
    WORKER_STATUS_PAGE_CRASHED,
    WORKER_STATUS_PARTIAL,
    WORKER_STATUS_INCOMPLETE,
    WORKER_STATUS_SESSION_FLEET_LOST,
    WORKER_STATUS_STALE_PAUSE_DEADLOCK,
    WORKER_STATUS_STEP_BUDGET,
    WORKER_STATUS_UNKNOWN,
)
from harness.semantic_frames import response_node_count


@dataclass
class WorkerDiagnostics:
    """Per-worker counters/flags consumed by classify_terminal_status."""

    last_exception_type: Optional[str] = None
    last_exception_message: Optional[str] = None

    # HITL bookkeeping. Today only last_pause_pageId is set; the rest are
    # populated when PR #4 (BrowserAgent HITL wait) lands. Keeping the fields
    # here avoids another diagnostics migration when that PR ships.
    last_pause_pageId: Optional[str] = None
    hitl_wait_entered: bool = False
    hitl_wait_timed_out: bool = False
    hitl_resumed_observed: bool = False
    hitl_page_settled_observed: bool = False
    hitl_stale_pause_deadlock_observed: bool = False

    # Contract errors are always recorded for diagnostics, even if the final
    # status ends up being something else.
    contract_errors: List[Dict[str, Any]] = field(default_factory=list)

    # Recent browser_call entries (method + failure signals), bounded.
    recent_calls: List[Dict[str, Any]] = field(default_factory=list)
    _recent_calls_max: int = 50

    # Infrastructure routing failures are accepted only when a harness-owned
    # browser tool emits a terminal structured result. The model cannot create
    # this signal merely by choosing the same word in final_answer.
    routing_failure_status: Optional[str] = None

    def observe_browser_call(self, method: str, params: Dict[str, Any], result: Dict[str, Any]) -> None:
        observation, error_text = _extract_observation_and_error(result)

        routing_status = str(result.get("status") or "").strip()
        if (
            result.get("terminal") is True
            and routing_status in {
                WORKER_STATUS_SESSION_FLEET_LOST,
                WORKER_STATUS_FLEET_ASSIGNMENT_LOST,
            }
        ):
            self.routing_failure_status = routing_status

        if method == "Hitl.requestPause" and _is_pause_success(observation, result):
            page_id = (params or {}).get("pageId") or _get_page_id_from_response(result)
            if page_id:
                self.last_pause_pageId = str(page_id)

        if _is_api_contract_error(observation, error_text):
            self.contract_errors.append({
                "method": method,
                "observation": (observation or "")[:200],
                "error": (error_text or "")[:200],
            })

        is_page_dead = _is_page_dead(observation, error_text, result)
        is_extraction_failure = (
            method in EXTRACTION_METHODS
            and _is_extraction_failure(observation, result)
        )

        self.recent_calls.append({
            "method": method,
            "is_page_dead": is_page_dead,
            "is_extraction_failure": is_extraction_failure,
        })
        if len(self.recent_calls) > self._recent_calls_max:
            self.recent_calls = self.recent_calls[-self._recent_calls_max:]

    def record_exception(self, exc: BaseException) -> None:
        self.last_exception_type = type(exc).__name__
        self.last_exception_message = str(exc)

    def mark_hitl_wait_entered(self) -> None:
        self.hitl_wait_entered = True

    def mark_hitl_wait_timed_out(self) -> None:
        self.hitl_wait_timed_out = True

    def mark_hitl_resumed(self) -> None:
        self.hitl_resumed_observed = True
        # Once resumed, the pending-pause signal is cleared.
        self.last_pause_pageId = None

    def mark_hitl_page_settled(self) -> None:
        self.hitl_page_settled_observed = True

    def mark_hitl_stale_pause_deadlock(self) -> None:
        self.hitl_stale_pause_deadlock_observed = True

    def to_log_payload(self) -> Dict[str, Any]:
        return {
            "last_exception_type": self.last_exception_type,
            "last_exception_message": (self.last_exception_message or "")[:500],
            "last_pause_pageId": self.last_pause_pageId,
            "hitl_wait_entered": self.hitl_wait_entered,
            "hitl_wait_timed_out": self.hitl_wait_timed_out,
            "hitl_resumed_observed": self.hitl_resumed_observed,
            "hitl_page_settled_observed": self.hitl_page_settled_observed,
            "hitl_stale_pause_deadlock_observed": self.hitl_stale_pause_deadlock_observed,
            "contract_error_count": len(self.contract_errors),
            "contract_errors_sample": self.contract_errors[:5],
            "recent_call_count": len(self.recent_calls),
            "recent_page_dead_count": sum(
                1 for c in self.recent_calls if c.get("is_page_dead")
            ),
            "recent_extraction_failure_count": sum(
                1 for c in self.recent_calls if c.get("is_extraction_failure")
            ),
            "routing_failure_status": self.routing_failure_status,
        }


def classify_terminal_status(
    *,
    diagnostics: WorkerDiagnostics,
    model_reported_status: Optional[str],
    reached_step_cap: bool,
    has_extraction_artifact: bool = False,
) -> Tuple[str, Optional[str]]:
    """
    Returns (final_status, override_reason).

    override_reason is non-None only when a hard signal overrode a model-
    reported soft status (caller can log it for visibility).
    """
    hard = _classify_hard(diagnostics, reached_step_cap, has_extraction_artifact)

    if model_reported_status:
        soft = _normalize_model_status(model_reported_status)
        if (
            hard == WORKER_STATUS_STALE_PAUSE_DEADLOCK
            and soft in {WORKER_STATUS_DONE, WORKER_STATUS_PARTIAL}
            and not reached_step_cap
            and has_extraction_artifact
        ):
            # Deadlock recovery exception: the deadlocked page was closed by
            # the harness and the sanctioned playbook is "continue from a
            # fresh page". A worker that then finished within budget AND
            # recorded extraction artifacts demonstrably was not stuck behind
            # the pause — honor its own outcome. The event stays visible via
            # hitl_stale_pause_deadlock_observed in the diagnostics payload.
            hard = None
        if soft == WORKER_STATUS_DONE and hard is None:
            return WORKER_STATUS_DONE, None
        if hard is not None and hard != soft:
            return hard, (
                f"hard-signal override: model reported {model_reported_status!r}"
                f" but harness detected {hard!r}"
            )
        if soft == WORKER_STATUS_UNKNOWN and model_reported_status in {
            WORKER_STATUS_BLOCKED_BY_CHALLENGE,
            WORKER_STATUS_HITL_REQUIRED,
            WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
            WORKER_STATUS_STALE_PAUSE_DEADLOCK,
        }:
            return WORKER_STATUS_INCOMPLETE, (
                "model-reported challenge/HITL status had no matching pause"
                " lifecycle receipt; preserving it as an unverified claim"
            )
        if soft == WORKER_STATUS_UNKNOWN:
            return WORKER_STATUS_UNKNOWN, (
                f"model reported {model_reported_status!r} which is not in"
                " allowed soft-status set"
            )
        return soft, None

    if hard is not None:
        return hard, None

    if reached_step_cap:
        # Defensive: should not reach here because step cap always produces at
        # least step_budget_exhausted in _classify_hard. Keep for safety.
        return WORKER_STATUS_STEP_BUDGET, None

    return WORKER_STATUS_UNKNOWN, "no hard signal and no model-reported status"


def status_category(status: str) -> str:
    return WORKER_STATUS_CATEGORIES.get(status, WORKER_STATUS_CATEGORY_UNKNOWN)


# --- internals ---


def _classify_hard(
    diag: WorkerDiagnostics,
    reached_step_cap: bool,
    has_extraction_artifact: bool,
) -> Optional[str]:
    """Two tiers of hard signals:

    Inescapable signals fire regardless of whether the worker also reported
    done — claiming success while one of these is active would be a lie:
      - context_limit_exceeded: the LLM call literally failed; no answer
      - hitl_*: a page is paused for human; the agent can't have legitimately
        proceeded past it (this is the strict Q1 semantic for HITL).
        Exception (handled in classify_terminal_status): stale_pause_deadlock
        closes the paused page and sanctions recovery on a fresh one, so a
        done/partial outcome with recorded extractions is allowed to stand.

    Evidence-of-failure signals only fire when the worker did NOT successfully
    complete (i.e. it hit the step cap, or it raised before final_answer):
      - browser_api_contract_error: per Q1 strict, must not override done
      - page_crashed: by symmetry, if the agent recovered onto a new
        fleet/page and reported done, trust the success
      - extraction_inconclusive: weak hard, by definition tied to step cap
      - step_budget_exhausted: only at step cap (default bucket)
    """
    if _is_context_limit_exception(diag):
        return WORKER_STATUS_CONTEXT_LIMIT

    if diag.routing_failure_status in {
        WORKER_STATUS_SESSION_FLEET_LOST,
        WORKER_STATUS_FLEET_ASSIGNMENT_LOST,
    }:
        return diag.routing_failure_status

    hitl = _classify_hitl(diag)
    if hitl is not None:
        return hitl

    if not reached_step_cap:
        return None

    if len(diag.contract_errors) >= API_CONTRACT_ERROR_THRESHOLD:
        return WORKER_STATUS_API_CONTRACT_ERROR

    if _is_page_crashed(diag):
        return WORKER_STATUS_PAGE_CRASHED

    if _is_extraction_inconclusive(diag, has_extraction_artifact):
        return WORKER_STATUS_EXTRACTION_INCONCLUSIVE

    return WORKER_STATUS_STEP_BUDGET


def _classify_hitl(diag: WorkerDiagnostics) -> Optional[str]:
    """HITL classifier.

    Semantics:
      - hitl_timeout: ONLY when we entered the wait and the timer expired
        (hitl_wait_timed_out=True). This is the "user didn't show up" signal.
      - hitl_waiting: pause issued but no completed wait observed. Covers
        both:
          (a) harness never entered wait (PR #4 not yet wired, or regressed)
          (b) wait was entered but neither resumed nor timed out (agent was
              cancelled / crashed / classifier ran mid-wait)
        Both subcases warrant the same LeadAgent reaction: don't auto-retry,
        surface infrastructure-gap or in-progress state to the user. The
        hitl_wait_entered flag remains in diagnostics for forensics.
      - page_settled_after_hitl: lifecycle notifications show the page got
        past the challenge, but ABCP still rejects tools as paused.
      - stale_pause_deadlock: resolvePause itself is rejected as paused, usually
        because the installed ABCP build did not exempt the unlock method.
    """
    if not diag.last_pause_pageId:
        return None
    if diag.hitl_resumed_observed:
        return None
    if diag.hitl_stale_pause_deadlock_observed:
        return WORKER_STATUS_STALE_PAUSE_DEADLOCK
    if diag.hitl_page_settled_observed:
        return WORKER_STATUS_PAGE_SETTLED_AFTER_HITL
    if diag.hitl_wait_timed_out:
        return WORKER_STATUS_HITL_TIMEOUT
    return WORKER_STATUS_HITL_WAITING


def _is_context_limit_exception(diag: WorkerDiagnostics) -> bool:
    msg = (diag.last_exception_message or "").lower()
    if not msg:
        return False
    return any(marker in msg for marker in CONTEXT_LIMIT_ERROR_MARKERS)


def _is_page_crashed(diag: WorkerDiagnostics) -> bool:
    if not diag.recent_calls:
        return False
    window = diag.recent_calls[-PAGE_CRASHED_LOOKBACK:]
    if len(window) < PAGE_CRASHED_FAIL_THRESHOLD:
        return False
    fail_count = sum(1 for c in window if c.get("is_page_dead"))
    return fail_count >= PAGE_CRASHED_FAIL_THRESHOLD


def _is_extraction_inconclusive(
    diag: WorkerDiagnostics,
    has_extraction_artifact: bool,
) -> bool:
    if has_extraction_artifact:
        return False
    extraction_calls = [
        c for c in diag.recent_calls if c.get("method") in EXTRACTION_METHODS
    ][-EXTRACTION_LOOKBACK:]
    if len(extraction_calls) < EXTRACTION_FAIL_THRESHOLD:
        return False
    fail_count = sum(1 for c in extraction_calls if c.get("is_extraction_failure"))
    return fail_count >= EXTRACTION_FAIL_THRESHOLD


def _normalize_model_status(raw: str) -> str:
    value = (raw or "").strip()
    if value in MODEL_ALLOWED_SOFT_STATUSES:
        return value
    return WORKER_STATUS_UNKNOWN


def _extract_observation_and_error(result: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(result, dict):
        return None, None
    error_text = result.get("error")
    response = result.get("response")
    observation = None
    if isinstance(response, dict):
        observation = response.get("observation")
        if error_text is None:
            error_text = response.get("error")
    if isinstance(error_text, dict):
        error_text = error_text.get("message") or str(error_text)
    return (
        observation if isinstance(observation, str) else None,
        error_text if isinstance(error_text, str) else None,
    )


def _get_page_id_from_response(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if isinstance(data, dict):
        page_id = data.get("pageId")
        if isinstance(page_id, str):
            return page_id
    return None


def _is_pause_success(observation: Optional[str], result: Any) -> bool:
    if isinstance(result, dict) and result.get("error"):
        return False
    if not observation:
        # If there's no observation but no error either, trust the call.
        return isinstance(result, dict) and not result.get("error")
    return "paused for human intervention" in observation.lower()


def _is_api_contract_error(observation: Optional[str], error_text: Optional[str]) -> bool:
    haystack = " ".join(
        s.lower() for s in (observation, error_text) if isinstance(s, str)
    )
    if not haystack:
        return False
    return any(marker in haystack for marker in API_CONTRACT_ERROR_MARKERS)


def _is_page_dead(
    observation: Optional[str],
    error_text: Optional[str],
    result: Any,
) -> bool:
    """Detect that the page is functionally dead.

    Sources:
      - observation / error text contains a known marker
      - Page.getState response.data.status == "crashed"
      - response.data.status == "page_load_failed" (defensive — schema varies)
    """
    haystack = " ".join(
        s for s in (observation, error_text) if isinstance(s, str)
    )
    if haystack and any(marker in haystack for marker in PAGE_DEAD_OBSERVATION_MARKERS):
        return True
    if isinstance(result, dict):
        response = result.get("response")
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                status_field = str(data.get("status") or "").lower()
                if status_field in {"crashed", "page_load_failed", "load_failed"}:
                    return True
    return False


def _is_extraction_failure(observation: Optional[str], result: Any) -> bool:
    if isinstance(result, dict) and result.get("error"):
        return True
    if observation:
        for marker in EXTRACTION_FAILURE_OBS_MARKERS:
            if marker in observation:
                return True
    if not isinstance(result, dict):
        return False
    response = result.get("response")
    if not isinstance(response, dict):
        return False
    data = response.get("data")
    if data is None:
        return True
    if isinstance(data, (list, dict)) and len(data) == 0:
        return True
    if response_node_count(data) == 0:
        return True
    return False
