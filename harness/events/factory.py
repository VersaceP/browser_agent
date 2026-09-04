"""harness.events.factory - Builds lifecycle events and guarantees they close.

The pairing guarantee is structural, not a runtime check bolted on afterwards:
a turn can only be opened from an agent scope, a message or a tool execution
only from a turn scope, and every scope closes in a ``finally``. There is no
way to write ``tool_execution_start`` without its ``end`` short of killing the
process, and cancellation - which is how the harness actually stops a worker -
runs finalisers like any other exception.

Sequence numbers come from a run-scoped allocator shared by every actor, so
lead and workers interleave into one totally ordered stream. Scope stacks do
NOT: each agent gets its own factory, which is why two workers can never
appear inside one another's turn.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from harness.events.models import (
    AgentEndEvent,
    AgentOutcome,
    AgentStartEvent,
    BrowserStateTransitionEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    EventContext,
    LegacyLogEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from harness.events.publisher import NullAgentEventPublisher, resolve_publisher
from harness.messages.models import AssistantMessage


class EventScopeError(RuntimeError):
    """A lifecycle scope was used in a way that cannot produce valid events."""


@dataclass(frozen=True)
class ValidationPolicy:
    """What to do when an event cannot be built.

    Strict under test so a malformed event is a failing test rather than a
    silent hole in the audit trail; lenient in production because a logging
    subsystem must never be the reason a task dies. Both halves matter: only
    the strict half keeps typed events from decaying into another untyped log.
    """

    strict: bool = False
    on_failure: Optional[Callable[[str, BaseException], None]] = None


def default_validation_policy() -> ValidationPolicy:
    override = os.environ.get("HARNESS_EVENTS_STRICT", "").strip().lower()
    if override in {"1", "true", "yes"}:
        return ValidationPolicy(strict=True)
    if override in {"0", "false", "no"}:
        return ValidationPolicy(strict=False)
    running_tests = (
        "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
    )
    return ValidationPolicy(strict=running_tests)


class RunEventSequencer:
    """Monotonic per-run counter.

    Locked rather than relying on the GIL: main.py runs coroutines on a
    private thread when the CLI already owns a loop, so two threads really can
    allocate at once.
    """

    def __init__(self, start: int = 0) -> None:
        self._value = int(start)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def current(self) -> int:
        return self._value


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class _ScopeState:
    scope_id: str
    closed: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class EventFactory:
    """Per-actor event source. Share the sequencer, never the scope stack."""

    def __init__(
        self,
        *,
        context: EventContext,
        sequencer: Optional[RunEventSequencer] = None,
        publisher: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        uid_factory: Optional[Callable[[], uuid.UUID]] = None,
        policy: Optional[ValidationPolicy] = None,
    ) -> None:
        self.context = context
        self.sequencer = sequencer or RunEventSequencer()
        self.publisher = resolve_publisher(publisher)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._uid = uid_factory or uuid.uuid4
        self.policy = policy or default_validation_policy()
        self._agent_scope: Optional[_ScopeState] = None
        self._open_turn: Optional[_ScopeState] = None
        self._quarantining = False

    # -- plumbing ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.publisher, "enabled", False))

    def bind(self, **patch: Any) -> "EventFactory":
        """A factory for a different actor, sharing this run's sequence."""
        return EventFactory(
            context=self.context.merge(**patch),
            sequencer=self.sequencer,
            publisher=self.publisher,
            clock=self._clock,
            uid_factory=self._uid,
            policy=self.policy,
        )

    def _envelope_fields(self, context: EventContext) -> Dict[str, Any]:
        return {
            "event_uid": self._uid(),
            "sequence_no": self.sequencer.next(),
            "emitted_at": self._clock(),
            "context": context,
        }

    def _emit(self, model: Any, context: EventContext, **fields: Any) -> Optional[Any]:
        if not self.enabled:
            return None
        try:
            event = model(**self._envelope_fields(context), **fields)
        except Exception as exc:  # pydantic ValidationError or a bad argument
            self._quarantine(getattr(model, "__name__", str(model)), exc, context)
            return None
        # Deliberately NOT wrapped. Whether a sink failure is fatal is the
        # emitter's decision - the storage sink is critical and must be able to
        # fail the run. Catching it here would silently downgrade every
        # critical sink to best-effort, which is exactly what it used to do.
        self.publisher.publish(event)
        return event

    def _quarantine(
        self, what: str, exc: BaseException, context: EventContext,
    ) -> None:
        """A canonical event that could not be built.

        Under test this raises, so a malformed event is a failing test rather
        than a hole in the audit trail. In production it degrades to a
        diagnostic legacy event: a logging subsystem must never be the reason
        a task dies, but the gap still has to be visible in the log.
        """

        if self.policy.on_failure is not None:
            try:
                self.policy.on_failure(what, exc)
            except Exception:
                pass
        if self.policy.strict:
            raise exc
        print(
            f"[events] quarantined {what}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        if self._quarantining:
            return
        self._quarantining = True
        try:
            # Not wrapped in a bare except. The diagnostic goes through the
            # same critical storage sink as everything else, and swallowing
            # its failure re-created exactly the bug this method exists beside:
            # a run continuing after the audit trail stopped being written,
            # just in the narrower window where a validation error happened at
            # the same time. Degrading the ValidationError is the decision that
            # was made; degrading a disk failure is not.
            self.legacy(
                "events.validation_failed",
                {
                    "event": str(what),
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                },
                severity="error",
                context=context,
            )
        finally:
            self._quarantining = False

    def _report_failure(self, what: str, exc: BaseException) -> None:
        if self.policy.on_failure is not None:
            try:
                self.policy.on_failure(what, exc)
            except Exception:
                pass
        if self.policy.strict:
            raise exc
        print(
            f"[events] dropped {what}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

    def _scope_violation(self, message: str) -> None:
        error = EventScopeError(message)
        if self.policy.strict:
            raise error
        self._report_failure("scope", error)

    # -- legacy ------------------------------------------------------------

    def legacy(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        severity: str = "info",
        context: Optional[EventContext] = None,
    ) -> Optional[LegacyLogEvent]:
        """Wrap an existing ``logger.write()`` call.

        The payload is passed through untouched and unvalidated - it is an
        arbitrary diagnostic dict, there is nothing to validate it against, and
        this is the hottest path in the harness.
        """
        if not self.enabled:
            return None
        active = context or self.context
        try:
            event = LegacyLogEvent.model_construct(
                schema_version=1,
                type="legacy_log",
                category="legacy",
                severity=severity,
                parent_event_uid=None,
                legacy_event_type=str(event_type),
                payload=dict(payload or {}),
                **self._envelope_fields(active),
            )
        except Exception as exc:
            self._report_failure("LegacyLogEvent", exc)
            return None
        # Same rule as _emit: sink failure policy belongs to the emitter.
        self.publisher.publish(event)
        return event

    # -- browser -----------------------------------------------------------

    def browser_state_transition(
        self,
        *,
        reducer: str,
        transition: str,
        page_id: Optional[str] = None,
        before_digest: Optional[str] = None,
        after_digest: Optional[str] = None,
        source_browser_event_id: Optional[str] = None,
        attribution: str = "unattributed",
    ) -> Optional[Any]:
        return self._emit(
            BrowserStateTransitionEvent,
            self.context,
            reducer=reducer,
            transition=transition,
            page_id=page_id,
            before_digest=before_digest,
            after_digest=after_digest,
            source_browser_event_id=source_browser_event_id,
            attribution=attribution,
        )

    @contextmanager
    def compaction_scope(
        self,
        *,
        reason: str,
        trigger_detail: Optional[str] = None,
        estimated_tokens_before: int,
        threshold_tokens: int,
        message_count_before: int,
    ) -> Iterator["CompactionScope"]:
        """Emit a paired compaction event even though it sits between turns."""
        compaction_id = _short_id("compaction")
        state = _ScopeState(scope_id=compaction_id)
        scope = CompactionScope(self, self.context, state, reason, trigger_detail)
        start = self._emit(
            CompactionStartEvent,
            self.context,
            compaction_id=compaction_id,
            reason=reason,
            trigger_detail=trigger_detail,
            estimated_tokens_before=estimated_tokens_before,
            threshold_tokens=threshold_tokens,
            message_count_before=message_count_before,
        )
        if start is not None:
            scope._parent_event_uid = start.event_uid
        try:
            yield scope
        except BaseException as exc:
            scope.fail(type(exc).__name__, status="aborted")
            raise
        finally:
            scope.close()

    # -- scopes ------------------------------------------------------------

    @contextmanager
    def agent_scope(
        self,
        *,
        label: Optional[str] = None,
        max_steps: Optional[int] = None,
        agent_id: Optional[str] = None,
    ) -> Iterator["AgentScope"]:
        if self._agent_scope is not None and not self._agent_scope.closed:
            self._scope_violation("agent scope already open on this factory")
        context = self.context.merge(agent_id=agent_id)
        state = _ScopeState(scope_id=agent_id or _short_id("agent"))
        self._agent_scope = state
        scope = AgentScope(self, context, state)
        self._emit(AgentStartEvent, context, label=label, max_steps=max_steps)
        try:
            yield scope
        except BaseException as exc:
            scope.mark_aborted(type(exc).__name__)
            raise
        finally:
            scope.close()
            self._agent_scope = None


class CompactionScope:
    def __init__(
        self, factory: EventFactory, context: EventContext, state: _ScopeState,
        reason: str, trigger_detail: Optional[str],
    ) -> None:
        self._factory = factory
        self.context = context
        self._state = state
        self._reason = reason
        self._trigger_detail = trigger_detail
        self._status = "completed"
        self._error: Optional[str] = None
        self._message_count_after = 0
        self._estimated_tokens_after = 0
        self._checkpoint_ref: Optional[str] = None
        self._summary_mode = "semantic"
        self._summary_error: Optional[str] = None
        self._parent_event_uid: Optional[uuid.UUID] = None

    def complete(
        self, *, message_count_after: int, estimated_tokens_after: int,
        checkpoint_ref: Optional[str] = None,
        summary_mode: str = "semantic",
        summary_error: Optional[str] = None,
    ) -> None:
        self._message_count_after = int(message_count_after)
        self._estimated_tokens_after = int(estimated_tokens_after)
        self._checkpoint_ref = checkpoint_ref
        self._summary_mode = summary_mode
        self._summary_error = summary_error

    def fail(self, error: str, *, status: str = "error") -> None:
        self._status = status
        self._error = str(error)[:1000]

    def close(self) -> None:
        if self._state.closed:
            return
        self._state.closed = True
        self._factory._emit(
            CompactionEndEvent,
            self.context,
            parent_event_uid=self._parent_event_uid,
            compaction_id=self._state.scope_id,
            reason=self._reason,
            trigger_detail=self._trigger_detail,
            status=self._status,
            message_count_after=self._message_count_after,
            estimated_tokens_after=self._estimated_tokens_after,
            checkpoint_ref=self._checkpoint_ref,
            summary_mode=self._summary_mode,
            summary_error=self._summary_error,
            error=self._error,
        )


class AgentScope:
    def __init__(
        self, factory: EventFactory, context: EventContext, state: _ScopeState
    ) -> None:
        self._factory = factory
        self.context = context
        self._state = state
        self._turn_index = 0
        self._message_count = 0
        self._final_message_id: Optional[str] = None
        self._outcome = AgentOutcome(status="running")
        self._aborted_by: Optional[str] = None

    def set_outcome(
        self, status: str, *, reason: Optional[str] = None, step_count: int = 0
    ) -> None:
        self._outcome = AgentOutcome(
            status=str(status), reason=reason, step_count=int(step_count or 0)
        )

    def mark_aborted(self, reason: str) -> None:
        self._aborted_by = reason

    @contextmanager
    def turn(self, *, turn_index: Optional[int] = None) -> Iterator["TurnScope"]:
        factory = self._factory
        if factory._open_turn is not None and not factory._open_turn.closed:
            factory._scope_violation("a turn is already open for this agent")
        self._turn_index = (
            int(turn_index) if turn_index is not None else self._turn_index + 1
        )
        turn_id = _short_id("turn")
        context = self.context.merge(turn_id=turn_id)
        state = _ScopeState(scope_id=turn_id)
        factory._open_turn = state
        scope = TurnScope(factory, self, context, state, self._turn_index)
        factory._emit(TurnStartEvent, context, turn_index=self._turn_index)
        try:
            yield scope
        except BaseException as exc:
            scope.fail(type(exc).__name__, status="aborted")
            raise
        finally:
            scope.close()
            factory._open_turn = None

    def note_message(self, message_id: str) -> None:
        self._message_count += 1
        self._final_message_id = message_id

    def close(self) -> None:
        if self._state.closed:
            return
        self._state.closed = True
        outcome = self._outcome
        if self._aborted_by and outcome.status == "running":
            outcome = AgentOutcome(
                status="aborted",
                reason=self._aborted_by,
                step_count=outcome.step_count,
            )
        self._factory._emit(
            AgentEndEvent,
            self.context,
            outcome=outcome,
            turn_count=self._turn_index,
            message_count=self._message_count,
            final_message_id=self._final_message_id,
        )


class TurnScope:
    def __init__(
        self,
        factory: EventFactory,
        agent: AgentScope,
        context: EventContext,
        state: _ScopeState,
        turn_index: int,
    ) -> None:
        self._factory = factory
        self._agent = agent
        self.context = context
        self._state = state
        self._turn_index = turn_index
        self._assistant_message_id: Optional[str] = None
        self._tool_call_ids: List[str] = []
        self._status = "completed"
        self._error: Optional[str] = None

    def fail(self, error: str, *, status: str = "error") -> None:
        self._status = status
        self._error = error

    @contextmanager
    def message(self, *, role: str = "assistant") -> Iterator["MessageScope"]:
        message_id = _short_id("msg")
        context = self.context.merge(message_id=message_id)
        state = _ScopeState(scope_id=message_id)
        scope = MessageScope(
            self._factory, context, state, role, self._turn_index
        )
        self._factory._emit(MessageStartEvent, context, role=role)
        if role == "assistant":
            self._assistant_message_id = message_id
        self._agent.note_message(message_id)
        try:
            yield scope
        except BaseException as exc:
            scope.fail(type(exc).__name__)
            raise
        finally:
            scope.close()

    @contextmanager
    def tool_execution(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Iterator["ToolExecutionScope"]:
        context = self.context.merge(tool_call_id=tool_call_id)
        state = _ScopeState(scope_id=tool_call_id)
        self._tool_call_ids.append(tool_call_id)
        scope = ToolExecutionScope(self._factory, context, state, tool_name)
        self._factory._emit(
            ToolExecutionStartEvent,
            context,
            tool_name=tool_name,
            argument_keys=sorted(str(key) for key in (arguments or {}).keys()),
        )
        try:
            yield scope
        except BaseException as exc:
            scope.fail(f"{type(exc).__name__}: {exc}", status="aborted")
            raise
        finally:
            scope.close()

    def close(self) -> None:
        if self._state.closed:
            return
        self._state.closed = True
        self._factory._emit(
            TurnEndEvent,
            self.context,
            turn_index=self._turn_index,
            status=self._status,
            assistant_message_id=self._assistant_message_id,
            tool_call_ids=list(self._tool_call_ids),
            error=self._error,
        )


class MessageScope:
    def __init__(
        self,
        factory: EventFactory,
        context: EventContext,
        state: _ScopeState,
        role: str,
        turn_index: int = 0,
    ) -> None:
        self._factory = factory
        self.context = context
        self._state = state
        self._role = role
        self._turn_index = int(turn_index)
        self._message: Optional[AssistantMessage] = None
        self._stop_reason: Optional[str] = None
        self._error: Optional[str] = None
        self._truncation = None

    @property
    def message_id(self) -> str:
        return self._state.scope_id

    def update(
        self, *, delta_kind: str = "text", delta_chars: int = 0, block_index: int = 0
    ) -> None:
        """Live-only by default: a token delta is not an audit fact."""
        self._factory._emit(
            MessageUpdateEvent,
            self.context,
            delta_kind=delta_kind,
            delta_chars=int(delta_chars),
            block_index=int(block_index),
        )

    def complete(
        self,
        message: Optional[AssistantMessage] = None,
        *,
        stop_reason: Optional[str] = None,
        truncation: Optional[Any] = None,
    ) -> None:
        self._message = message
        self._stop_reason = stop_reason or (
            message.stop_reason if message is not None else None
        )
        self._truncation = truncation

    def fail(self, error: str) -> None:
        self._error = error

    def close(self) -> None:
        if self._state.closed:
            return
        self._state.closed = True
        message = self._message
        thinking = message.thinking_blocks() if message is not None else []
        self._factory._emit(
            MessageEndEvent,
            self.context,
            role=self._role,
            stop_reason=self._stop_reason or self._error,
            block_kinds=message.block_kinds() if message is not None else [],
            structure_digest=(
                message.structure_digest() if message is not None else None
            ),
            text_chars=len(message.text()) if message is not None else 0,
            thinking_chars=sum(len(block.thinking) for block in thinking),
            thinking_blocks=len(thinking),
            thinking_signed=any(bool(block.signature) for block in thinking),
            tool_call_count=len(message.tool_calls()) if message is not None else 0,
            turn_index=self._turn_index,
            content=message,
            truncation=self._truncation,
        )


class ToolExecutionScope:
    def __init__(
        self,
        factory: EventFactory,
        context: EventContext,
        state: _ScopeState,
        tool_name: str,
    ) -> None:
        self._factory = factory
        self.context = context
        self._state = state
        self._tool_name = tool_name
        self._started = datetime.now(timezone.utc)
        self._status = "completed"
        self._is_error = False
        self._result_chars = 0
        self._result_digest: Optional[str] = None
        self._payload_ref = None
        self._error: Optional[str] = None
        self._updates = 0

    def update(self, progress: Optional[str] = None) -> None:
        self._updates += 1
        self._factory._emit(
            ToolExecutionUpdateEvent,
            self.context,
            tool_name=self._tool_name,
            progress=progress,
            sequence_in_execution=self._updates,
        )

    def complete(
        self,
        *,
        is_error: bool = False,
        result_chars: int = 0,
        result_digest: Optional[str] = None,
        payload_ref: Optional[Any] = None,
    ) -> None:
        self._is_error = bool(is_error)
        self._status = "error" if is_error else "completed"
        self._result_chars = int(result_chars or 0)
        self._result_digest = result_digest
        self._payload_ref = payload_ref

    def fail(self, error: str, *, status: str = "error") -> None:
        self._status = status
        self._is_error = True
        self._error = error

    def close(self) -> None:
        if self._state.closed:
            return
        self._state.closed = True
        elapsed = datetime.now(timezone.utc) - self._started
        self._factory._emit(
            ToolExecutionEndEvent,
            self.context,
            tool_name=self._tool_name,
            status=self._status,
            is_error=self._is_error,
            duration_ms=int(elapsed.total_seconds() * 1000),
            result_chars=self._result_chars,
            result_digest=self._result_digest,
            payload_ref=self._payload_ref,
            error=self._error,
        )


def null_factory(task_id: str = "", run_id: str = "") -> EventFactory:
    """A factory that builds nothing - the default for every construction site."""
    return EventFactory(
        context=EventContext(task_id=task_id, run_id=run_id),
        publisher=NullAgentEventPublisher(),
    )


__all__ = [
    "AgentScope",
    "EventFactory",
    "EventScopeError",
    "MessageScope",
    "RunEventSequencer",
    "ToolExecutionScope",
    "TurnScope",
    "ValidationPolicy",
    "default_validation_policy",
    "null_factory",
]
