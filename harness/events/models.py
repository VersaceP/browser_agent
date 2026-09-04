"""harness.events.models - Typed agent lifecycle events and their storage row.

Two kinds of record live here and they are not equals:

``CanonicalEvent``  a lifecycle fact with a fixed schema - an agent started, a
                    turn closed, a tool execution ended. Strictly validated.
``LegacyLogEvent``  the 370-odd existing ``logger.write("some.name", {...})``
                    call sites. The envelope around them is strict; the payload
                    is not validated at all, because a free-form diagnostic dict
                    has nothing to validate against and paying Pydantic to walk
                    it would be pure cost on the hottest path in the harness.

Both carry the same envelope, and both reach storage through exactly one
mapping - :meth:`PersistedRunEvent.from_event`. A second mapping is how the
existing ``actor_type`` column ended up dead: the database promotes a column
the writer never fills while the file backend reads the same name out of a
payload key nothing produces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing_extensions import Annotated, Literal

from harness.messages.models import AssistantMessage, PayloadRef, TruncationInfo

_MODEL_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    extra="forbid",
    frozen=True,
)

ActorType = Literal["lead", "browser", "system", "browser_platform"]
Severity = Literal["debug", "info", "warning", "error"]
Category = Literal[
    "agent", "turn", "message", "tool", "browser",
    "storage", "diagnostic", "compaction", "legacy",
]
ScopeStatus = Literal["completed", "error", "aborted", "truncated"]


class EventContext(BaseModel):
    """Who produced the event, at which nesting level.

    These are relational fields, not payload. Copying ``taskId``/``runId``/
    ``workerId`` back into an arbitrary payload dict - which is how worker
    identity travels today - lets a caller spoof another actor's identity by
    accident and forces every consumer to guess where to look.
    """

    model_config = _MODEL_CONFIG

    task_id: str
    run_id: str = ""
    actor_type: ActorType = "system"
    agent_id: Optional[str] = None
    worker_id: Optional[str] = None
    slot_id: Optional[str] = None
    phase_id: Optional[str] = None
    turn_id: Optional[str] = None
    message_id: Optional[str] = None
    tool_call_id: Optional[str] = None

    def merge(self, **patch: Any) -> "EventContext":
        """Return a validated copy with non-None patch values applied.

        Re-validated rather than ``model_copy(update=...)``: that path skips
        validation entirely, so an actor_type of "bogus" travelled all the way
        into a database column that has a fixed vocabulary.
        """

        clean = {key: value for key, value in patch.items() if value is not None}
        if not clean:
            return self
        return EventContext.model_validate(
            {**self.model_dump(by_alias=False), **clean}
        )


class EventEnvelope(BaseModel):
    """Fields every event carries. Subclasses add the payload for their type.

    Inheritance rather than a nested ``payload`` object: the storage row is
    flat, so a flat model is one mapping instead of two.
    """

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    event_uid: UUID
    sequence_no: int = Field(ge=1)
    emitted_at: datetime
    category: Category
    severity: Severity = "info"
    context: EventContext
    parent_event_uid: Optional[UUID] = None


class AgentOutcome(BaseModel):
    model_config = _MODEL_CONFIG

    status: str
    reason: Optional[str] = None
    step_count: int = 0


# -- lifecycle events -------------------------------------------------------
#
# Deliberately NOT carrying transcripts. Pi's `agent_end` hands subscribers the
# whole message list because its subscriber is an in-process UI that renders it
# and drops it. Here every published event is also persisted, so shipping the
# transcript on agent_end writes every message a second time, and turn_end a
# third. The transcript is reconstructable from the message events; the closing
# events carry identity and counts.


class AgentStartEvent(EventEnvelope):
    type: Literal["agent_start"] = "agent_start"
    category: Category = "agent"
    label: Optional[str] = None
    max_steps: Optional[int] = None


class AgentEndEvent(EventEnvelope):
    type: Literal["agent_end"] = "agent_end"
    category: Category = "agent"
    outcome: AgentOutcome
    turn_count: int = 0
    message_count: int = 0
    final_message_id: Optional[str] = None


class TurnStartEvent(EventEnvelope):
    type: Literal["turn_start"] = "turn_start"
    category: Category = "turn"
    turn_index: int = 0


class TurnEndEvent(EventEnvelope):
    type: Literal["turn_end"] = "turn_end"
    category: Category = "turn"
    turn_index: int = 0
    status: ScopeStatus = "completed"
    assistant_message_id: Optional[str] = None
    tool_call_ids: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class CompactionStartEvent(EventEnvelope):
    """A compaction attempt started outside a model turn."""

    type: Literal["compaction_start"] = "compaction_start"
    category: Category = "compaction"
    compaction_id: str
    reason: Literal[
        "manual", "threshold", "overflow", "cache_pressure", "provider_recovery",
    ]
    trigger_detail: Optional[str] = None
    estimated_tokens_before: int = 0
    threshold_tokens: int = 0
    message_count_before: int = 0


class CompactionEndEvent(EventEnvelope):
    type: Literal["compaction_end"] = "compaction_end"
    category: Category = "compaction"
    compaction_id: str
    reason: Literal[
        "manual", "threshold", "overflow", "cache_pressure", "provider_recovery",
    ]
    trigger_detail: Optional[str] = None
    status: Literal["completed", "skipped", "error", "aborted"] = "completed"
    message_count_after: int = 0
    estimated_tokens_after: int = 0
    checkpoint_ref: Optional[str] = None
    summary_mode: Literal["semantic", "mechanical_fallback"] = "semantic"
    summary_error: Optional[str] = None
    error: Optional[str] = None


class MessageStartEvent(EventEnvelope):
    type: Literal["message_start"] = "message_start"
    category: Category = "message"
    role: str = "assistant"


class MessageUpdateEvent(EventEnvelope):
    """A streaming delta. Published live, not persisted by default."""

    type: Literal["message_update"] = "message_update"
    category: Category = "message"
    delta_kind: Literal["text", "thinking", "tool_call"] = "text"
    delta_chars: int = 0
    block_index: int = 0


class MessageEndEvent(EventEnvelope):
    """The finished assistant turn, as ordered structure.

    ``content`` is always attached in memory and each sink decides what to keep
    of it. That split is the point of having sinks: the trace projection needs
    the text to rebuild its legacy entry, while the storage sink must not write
    a second copy of text the legacy ``agent.model`` event already carries.
    What every sink records unconditionally is what the legacy event never had
    - block ORDER, thinking presence and signature round-trip - none of which
    requires copying reasoning text anywhere.
    """

    type: Literal["message_end"] = "message_end"
    category: Category = "message"
    role: str = "assistant"
    turn_index: int = 0
    stop_reason: Optional[str] = None
    block_kinds: List[str] = Field(default_factory=list)
    structure_digest: Optional[str] = None
    text_chars: int = 0
    thinking_chars: int = 0
    thinking_blocks: int = 0
    thinking_signed: bool = False
    tool_call_count: int = 0
    content: Optional[AssistantMessage] = None
    truncation: Optional[TruncationInfo] = None


class ToolExecutionStartEvent(EventEnvelope):
    type: Literal["tool_execution_start"] = "tool_execution_start"
    category: Category = "tool"
    tool_name: str
    # Bounded preview only. Full arguments already reach storage through the
    # tool's own request event.
    argument_keys: List[str] = Field(default_factory=list)


class ToolExecutionUpdateEvent(EventEnvelope):
    type: Literal["tool_execution_update"] = "tool_execution_update"
    category: Category = "tool"
    tool_name: str
    progress: Optional[str] = None
    sequence_in_execution: int = 0


class ToolExecutionEndEvent(EventEnvelope):
    type: Literal["tool_execution_end"] = "tool_execution_end"
    category: Category = "tool"
    tool_name: str
    status: ScopeStatus = "completed"
    is_error: bool = False
    duration_ms: Optional[int] = None
    result_chars: int = 0
    result_digest: Optional[str] = None
    payload_ref: Optional[PayloadRef] = None
    error: Optional[str] = None


class BrowserStateTransitionEvent(EventEnvelope):
    """A reducer changed harness state because of a browser notification."""

    type: Literal["browser_state_transition"] = "browser_state_transition"
    category: Category = "browser"
    reducer: str
    transition: str
    page_id: Optional[str] = None
    before_digest: Optional[str] = None
    after_digest: Optional[str] = None
    source_browser_event_id: Optional[str] = None
    # Weakest-first attribution. Time overlap is never promoted to causation:
    # in a fleet, an event that arrived during worker A's call routinely
    # belongs to worker B's page.
    attribution: Literal[
        "caused_by_execution",
        "correlated_tool_call",
        "observed_during_tool_call",
        "unattributed",
    ] = "unattributed"


class LegacyLogEvent(EventEnvelope):
    """An existing ``logger.write()`` call, wrapped in a strict envelope."""

    type: Literal["legacy_log"] = "legacy_log"
    category: Category = "legacy"
    legacy_event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


CanonicalEvent = Annotated[
    Union[
        AgentStartEvent,
        AgentEndEvent,
        TurnStartEvent,
        TurnEndEvent,
        CompactionStartEvent,
        CompactionEndEvent,
        MessageStartEvent,
        MessageUpdateEvent,
        MessageEndEvent,
        ToolExecutionStartEvent,
        ToolExecutionUpdateEvent,
        ToolExecutionEndEvent,
        BrowserStateTransitionEvent,
    ],
    Field(discriminator="type"),
]

AgentEvent = Annotated[
    Union[
        AgentStartEvent,
        AgentEndEvent,
        TurnStartEvent,
        TurnEndEvent,
        CompactionStartEvent,
        CompactionEndEvent,
        MessageStartEvent,
        MessageUpdateEvent,
        MessageEndEvent,
        ToolExecutionStartEvent,
        ToolExecutionUpdateEvent,
        ToolExecutionEndEvent,
        BrowserStateTransitionEvent,
        LegacyLogEvent,
    ],
    Field(discriminator="type"),
]

# Wire names for the ``event_type`` column. The models keep Pi's vocabulary;
# storage gets dotted names in a namespace no existing event occupies, so a
# canonical event can never be mistaken for `agent.model` or `agent.final`.
EVENT_TYPE_NAMES: Dict[str, str] = {
    "agent_start": "lifecycle.agent.start",
    "agent_end": "lifecycle.agent.end",
    "turn_start": "lifecycle.turn.start",
    "turn_end": "lifecycle.turn.end",
    "compaction_start": "lifecycle.compaction.start",
    "compaction_end": "lifecycle.compaction.end",
    "message_start": "lifecycle.message.start",
    "message_update": "lifecycle.message.update",
    "message_end": "lifecycle.message.end",
    "tool_execution_start": "lifecycle.tool.start",
    "tool_execution_update": "lifecycle.tool.update",
    "tool_execution_end": "lifecycle.tool.end",
    "browser_state_transition": "lifecycle.browser.state_transition",
}

# Envelope fields are relational columns, never payload keys.
_ENVELOPE_FIELDS = {
    "schema_version", "event_uid", "sequence_no", "emitted_at",
    "category", "severity", "context", "parent_event_uid", "type",
}
_CAMEL_ENVELOPE_KEYS = {to_camel(name) for name in _ENVELOPE_FIELDS} | _ENVELOPE_FIELDS


class PersistedRunEvent(BaseModel):
    """The one shape both storage backends write and read.

    Every field here is a column or a documented JSONL key. Anything an event
    adds beyond the envelope lands in ``payload``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_uid: str
    schema_version: int
    sequence_no: int
    task_id: str
    run_id: str
    event_time: str
    event_type: str
    category: str
    actor_type: Optional[str] = None
    agent_id: Optional[str] = None
    worker_id: Optional[str] = None
    slot_id: Optional[str] = None
    phase_id: Optional[str] = None
    turn_id: Optional[str] = None
    message_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)

    def identity_digest(self) -> str:
        """Hash of everything that makes this row this row.

        Used to answer one question: is a second write under the same
        ``event_uid`` the retry we expected, or two different events that
        collided? Comparing only type and payload answered "retry" for two
        events that differed in actor, sequence and scope.
        """

        import hashlib
        import json as _json

        canonical = _json.dumps(
            self.model_dump(mode="json"), sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_event(cls, event: Any) -> "PersistedRunEvent":
        context = event.context
        if isinstance(event, LegacyLogEvent):
            event_type = event.legacy_event_type
            payload: Dict[str, Any] = dict(event.payload or {})
        else:
            event_type = EVENT_TYPE_NAMES.get(event.type, f"lifecycle.{event.type}")
            dumped = event.model_dump(mode="json", by_alias=True, exclude_none=True)
            payload = {
                key: value
                for key, value in dumped.items()
                if key not in _CAMEL_ENVELOPE_KEYS
            }
        # Neither has a column: nothing in the harness sets a severity other
        # than "info" or a parent uid at all, and a column no writer fills is
        # how run_events.actor_type spent its whole life. They ride in the
        # payload so a future writer loses nothing.
        if str(event.severity) != "info":
            payload = {**payload, "severity": str(event.severity)}
        if event.parent_event_uid is not None:
            payload = {**payload, "parentEventUid": str(event.parent_event_uid)}
        # model_construct, not __init__: every value below is copied from an
        # envelope Pydantic already validated, and this runs on every one of
        # the harness's ~370 logging call sites.
        return cls.model_construct(
            event_uid=str(event.event_uid),
            schema_version=int(event.schema_version),
            sequence_no=int(event.sequence_no),
            task_id=context.task_id,
            run_id=context.run_id,
            event_time=event.emitted_at.isoformat(),
            event_type=event_type,
            category=str(event.category),
            actor_type=context.actor_type,
            agent_id=context.agent_id,
            worker_id=context.worker_id,
            slot_id=context.slot_id,
            phase_id=context.phase_id,
            turn_id=context.turn_id,
            message_id=context.message_id,
            tool_call_id=context.tool_call_id,
            payload=payload,
        )


__all__ = [
    "ActorType",
    "AgentEndEvent",
    "AgentEvent",
    "AgentOutcome",
    "AgentStartEvent",
    "BrowserStateTransitionEvent",
    "CanonicalEvent",
    "Category",
    "CompactionEndEvent",
    "CompactionStartEvent",
    "EVENT_TYPE_NAMES",
    "EventContext",
    "EventEnvelope",
    "LegacyLogEvent",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "PersistedRunEvent",
    "ScopeStatus",
    "Severity",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "TurnEndEvent",
    "TurnStartEvent",
]
