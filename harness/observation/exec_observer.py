"""harness.observation.exec_observer — execution trace for a blocked Workflow.execute.

While `Workflow.execute` is in flight the ABCPClient `_call_lock` is held, so no
other RPC can be issued on that connection (see harness/skill/pause.py).
Notifications are the exception: the NotificationHub demultiplexes them off the
background reader, independent of the lock. This module turns that one live
channel into the execution record.

It is not an optimization. Live probe 2026-09-11
(docs/workflow-execute-live-contract.md) established three facts that make it
the ONLY complete source:

  1. On failure `Workflow.execute` raises -32005 carrying just
     `details.failedStepPath` — no `workflowId`, no results, no variables.
  2. `Workflow.getStatus` needs a `workflowId`, which on the failure path exists
     nowhere else than the `Workflow.progress` stream.
  3. Even when called, `getStatus` returns `variableKeys` (names only) and
     `resultCount` — never the variable VALUES or the step results.

Every `Workflow.progress` event, by contrast, carries the complete `variables`
map as of that step, plus `stepPath`, `stepType`, `action`, `status`, `duration`
and, on failure, `error`/`errorCode`.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from harness.utils import JsonDict

# A segment that produces more step events than this is already past the point
# where a model can reason about the trace; keep the head and the tail so both
# the opening actions and the failure remain visible.
DEFAULT_MAX_STEP_EVENTS = 120
_TERMINAL_PHASES = frozenset({"completed", "succeeded", "failed", "cancelled"})


def _event_envelope(msg: Any) -> JsonDict:
    """The ABCP event envelope inside a System.notification message."""
    if not isinstance(msg, dict):
        return {}
    params = msg.get("params")
    if not isinstance(params, dict):
        return {}
    data = params.get("data")
    return data if isinstance(data, dict) else {}


class ExecTrace:
    """What one Workflow.execute did, rebuilt from its progress events."""

    def __init__(self) -> None:
        self.workflow_id: Optional[str] = None
        self.phase: Optional[str] = None
        self.steps: List[JsonDict] = []
        self.variables: JsonDict = {}
        self.store_revision: Optional[int] = None
        self.failure: Optional[JsonDict] = None
        self.cursor_first: Optional[int] = None
        self.cursor_last: Optional[int] = None
        self.page_events: Dict[str, int] = {}
        self.dropped_steps = 0
        # True totals over the WHOLE run. The `steps` list keeps only the
        # head and the tail past DEFAULT_MAX_STEP_EVENTS (see _append_step),
        # so every count derived from it under-reports a long workflow.
        # Segment telemetry reads these, never len(self.steps).
        self.total_steps = 0
        self.total_succeeded = 0
        # Wall time from observer start to stop. The lifecycle tool events
        # measure the whole tool call; this isolates the Workflow.execute
        # itself, which is the unit segment telemetry is compared against
        # single-call baselines with.
        self.duration_ms: Optional[int] = None

    @property
    def completed_steps(self) -> List[JsonDict]:
        return [s for s in self.steps if s.get("status") == "success"]

    def to_receipt(self) -> JsonDict:
        """Model-facing summary. Only fields the model can act on."""
        receipt: JsonDict = {
            "workflowId": self.workflow_id,
            "phase": self.phase,
            "stepsObserved": self.total_steps,
            "stepsSucceeded": self.total_succeeded,
        }
        if self.steps:
            receipt["steps"] = self.steps
        if self.dropped_steps:
            receipt["stepsOmitted"] = self.dropped_steps
        if self.variables:
            receipt["variablesAtEnd"] = self.variables
        if self.store_revision is not None:
            receipt["storeRevision"] = self.store_revision
        if self.failure:
            receipt["failure"] = self.failure
        if self.page_events:
            receipt["pageEvents"] = dict(sorted(self.page_events.items()))
        if self.cursor_first is not None:
            receipt["eventCursorRange"] = [self.cursor_first, self.cursor_last]
        return receipt


class ExecObserver:
    """Observe-only NotificationHub subscriber for one Workflow.execute.

    Safe to run while the call is blocked: subscribers fire off the background
    reader, never through `_call_lock`. Never raises — an observer that breaks
    execution is worse than one that records nothing.
    """

    def __init__(
        self,
        browser: Any,
        *,
        page_id: Optional[str] = None,
        max_step_events: int = DEFAULT_MAX_STEP_EVENTS,
    ) -> None:
        self._browser = browser
        self._page_id = str(page_id or "") or None
        self._max_step_events = max(2, int(max_step_events))
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._started_at: Optional[float] = None
        self.trace = ExecTrace()

    # -- subscription lifecycle -------------------------------------------

    def start(self) -> "ExecObserver":
        self._started_at = time.monotonic()
        subscribe = getattr(self._browser, "subscribe_notifications", None)
        if callable(subscribe):
            try:
                self._unsubscribe = subscribe(self._on_message)
            except Exception:  # pragma: no cover - best-effort
                self._unsubscribe = None
        return self

    def stop(self) -> ExecTrace:
        started = getattr(self, "_started_at", None)
        if started is not None:
            self.trace.duration_ms = int((time.monotonic() - started) * 1000)
            self._started_at = None
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # pragma: no cover
                pass
            self._unsubscribe = None
        return self.trace

    def __enter__(self) -> "ExecObserver":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- ingestion ---------------------------------------------------------

    def _on_message(self, msg: Any) -> None:
        try:
            self._ingest(msg)
        except Exception:  # pragma: no cover - observer must never raise
            pass

    def _ingest(self, msg: Any) -> None:
        env = _event_envelope(msg)
        if not env:
            return
        event = str(env.get("event") or "")
        cursor = env.get("cursor")
        if isinstance(cursor, int):
            if self.trace.cursor_first is None:
                self.trace.cursor_first = cursor
            self.trace.cursor_last = cursor
        if event == "Workflow.progress":
            self._ingest_progress(env.get("payload"))
            return
        if not event:
            return
        # Page-side context. Scope to our page when the envelope names one;
        # Workflow.progress itself carries null fleetId/pageId/taskId, which is
        # why the scope check lives here and not above.
        env_page = env.get("pageId")
        if self._page_id and env_page and str(env_page) != self._page_id:
            return
        self.trace.page_events[event] = self.trace.page_events.get(event, 0) + 1

    def _ingest_progress(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        workflow_id = payload.get("workflowId")
        if workflow_id and not self.trace.workflow_id:
            self.trace.workflow_id = str(workflow_id)
        # A second workflow's progress on the same connection is not ours.
        if (
            workflow_id
            and self.trace.workflow_id
            and str(workflow_id) != self.trace.workflow_id
        ):
            return

        phase = str(payload.get("phase") or "")
        variables = payload.get("variables")
        if isinstance(variables, dict):
            # Every progress event carries the full map, so the last one wins
            # and is the failure-time snapshot.
            self.trace.variables = dict(variables)
        revision = payload.get("storeRevision")
        if isinstance(revision, int):
            self.trace.store_revision = revision

        if phase in _TERMINAL_PHASES:
            self.trace.phase = phase
            if phase == "failed":
                self.trace.failure = {
                    "stepPath": payload.get("stepPath"),
                    "error": payload.get("error"),
                    "errorCode": payload.get("errorCode"),
                    "durationMs": payload.get("duration"),
                }
            return
        if phase == "started":
            self.trace.phase = "running"
            return
        if phase != "step_finished":
            # `step_started` adds no fact the matching finish does not carry.
            return

        step: JsonDict = {
            "stepPath": payload.get("stepPath"),
            "stepType": payload.get("stepType"),
            "status": payload.get("status"),
        }
        for key in ("action", "duration", "error", "errorCode"):
            value = payload.get(key)
            if value is not None:
                step["durationMs" if key == "duration" else key] = value
        self._append_step(step)

    def _append_step(self, step: JsonDict) -> None:
        """Keep the head and the most recent steps; drop the bland middle.

        The opening steps establish what the segment set up and the closing ones
        contain the failure, so a long collection loop stays legible without the
        receipt growing without bound. The cumulative totals on the trace count
        EVERY step regardless of what this method drops from the list.
        """
        self.trace.total_steps += 1
        if step.get("status") == "success":
            self.trace.total_succeeded += 1
        limit = self._max_step_events
        if len(self.trace.steps) < limit:
            self.trace.steps.append(step)
            return
        head = limit // 3
        self.trace.steps = self.trace.steps[:head] + self.trace.steps[head + 1:]
        self.trace.steps.append(step)
        self.trace.dropped_steps += 1
