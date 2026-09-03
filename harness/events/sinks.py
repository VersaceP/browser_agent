"""harness.events.sinks - Where an emitted event is projected to.

A sink observes; it never changes what other sinks see. That is not a style
rule: the storage sink runs before the console sink, and a console formatter
that mutated the payload would silently rewrite the audit trail.

Failure policy is per sink, not global. Losing an audit row is a real failure
and stays fatal; failing to print a progress line is not, and must never be the
reason a task result is thrown away.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from typing_extensions import Protocol, runtime_checkable

from harness.events.models import LegacyLogEvent, PersistedRunEvent


@runtime_checkable
class EventSink(Protocol):
    name: str

    def handle(self, event: Any) -> None: ...


class SinkBinding:
    """A sink plus what happens when it raises."""

    __slots__ = ("sink", "critical")

    def __init__(self, sink: Any, *, critical: bool) -> None:
        self.sink = sink
        self.critical = bool(critical)

    @property
    def name(self) -> str:
        return str(getattr(self.sink, "name", type(self.sink).__name__))


class StorageEventSink:
    """Persist the event. Critical: a dropped audit row is a real failure.

    Prefers the typed ``append_run_event`` entry point when the backend has one
    and falls back to the historical ``append_event`` otherwise, so the storage
    layer can gain envelope columns without this sink changing again.
    """

    name = "storage"

    def __init__(
        self,
        storage_provider: Callable[[], Any],
        *,
        persist_message_content: bool = False,
    ) -> None:
        self._storage_provider = storage_provider
        self._persist_message_content = bool(persist_message_content)

    def set_persist_message_content(self, enabled: bool) -> None:
        self._persist_message_content = bool(enabled)

    def handle(self, event: Any) -> None:
        if getattr(event, "type", "") in self.LIVE_ONLY_EVENTS:
            return
        event = self._project(event)
        row = PersistedRunEvent.from_event(event)
        storage = self._storage_provider()
        typed = getattr(storage, "append_run_event", None)
        if callable(typed):
            typed(row)
            return
        storage.append_event(
            task_id=row.task_id,
            run_id=row.run_id,
            event_type=row.event_type,
            payload=row.payload,
            worker_id=row.worker_id,
        )

    # Declared live-only in the persistence policy, so the sink has to be the
    # thing that enforces it. A token-level delta stream writing one row per
    # delta is a storage flood, and the closing event already carries the
    # finished content.
    LIVE_ONLY_EVENTS = frozenset({"message_update", "tool_execution_update"})

    def _project(self, event: Any) -> Any:
        """Drop assistant text unless persisting it was asked for.

        The event still carries the full message for other sinks; what this
        decides is only whether a second copy of text the legacy
        ``agent.model`` event already holds reaches the database.
        """

        if self._persist_message_content:
            return event
        if getattr(event, "type", "") != "message_end":
            return event
        if getattr(event, "content", None) is None:
            return event
        return event.model_copy(update={"content": None})


class ConsoleEventSink:
    """Adapt the historical ``(event_type, payload)`` callback.

    Canonical lifecycle events are handed over under their dotted names, which
    no existing formatter matches, so console output is unchanged until a
    formatter is deliberately taught about them.
    """

    name = "console"

    def __init__(self, callback: Optional[Callable[[str, dict], None]]) -> None:
        self._callback = callback

    @property
    def active(self) -> bool:
        return self._callback is not None

    def handle(self, event: Any) -> None:
        if self._callback is None:
            return
        if isinstance(event, LegacyLogEvent):
            self._callback(event.legacy_event_type, event.payload)
            return
        row = PersistedRunEvent.from_event(event)
        self._callback(row.event_type, row.payload)


class TraceProjectionSink:
    """Rebuild the legacy ``agent.trace`` entries a consumer still expects.

    Only the ``model`` entry is projected here, and that is a finding rather
    than a limitation: the other trace entries are NOT duplicates of run
    events. ``browser_call`` carries the post-offload model copy of a result
    while the matching ``browser.call.result`` log carries a differently
    trimmed pre-offload copy, and three consumers read those entries for their
    CONTENT - the worker handoff summary, the step-extension loop-nudge
    lookback, and skills/_tools/distill_trace.py, which reads the on-disk
    file. Collapsing them into one record would have to pick one fidelity and
    silently change all three.
    """

    def __init__(
        self,
        trace: list,
        *,
        agent_id: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> None:
        self._trace = trace
        # Named per actor, because the emitter is shared by the whole run: an
        # anonymous sink per spawned worker would accumulate on it forever and
        # keep receiving events long after that worker finished.
        self.name = "trace_projection:" + (worker_id or agent_id or "main")
        # One emitter serves the whole run, so a sink holding one agent's list
        # must refuse everyone else's events or concurrent workers cross over.
        self._agent_id = agent_id or None
        self._worker_id = worker_id or None

    def _mine(self, event: Any) -> bool:
        context = getattr(event, "context", None)
        if context is None:
            return False
        if self._worker_id is not None:
            return context.worker_id == self._worker_id
        # A lead shares its agent_id with the workers it spawned, so matching
        # on agent_id alone would pull every worker's turn into the lead's
        # trace. Only an event with no worker at all belongs to the lead.
        if context.worker_id is not None:
            return False
        if self._agent_id is not None:
            return context.agent_id == self._agent_id
        return True

    def handle(self, event: Any) -> None:
        if getattr(event, "type", "") != "message_end" or not self._mine(event):
            return
        message = getattr(event, "content", None)
        if message is None:
            return
        self._trace.append({
            "type": "model",
            "step": int(getattr(event, "turn_index", 0) or 0),
            "text": message.text(),
            "tool_calls": [
                {"name": call.name, "input": call.arguments}
                for call in message.tool_calls()
            ],
        })


class RecordingSink:
    """Keeps events in memory. For tests and shadow comparisons."""

    name = "recording"

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event: Any) -> None:
        self.events.append(event)


__all__ = [
    "ConsoleEventSink",
    "EventSink",
    "RecordingSink",
    "SinkBinding",
    "StorageEventSink",
    "TraceProjectionSink",
]
