"""harness.messages.models - Canonical, provider-neutral message types.

These exist so an assistant turn stops being a ``(text, tool_calls,
stop_reason, usage)`` tuple with thinking smuggled through a private
``usage["_assistant_prefix_blocks"]`` key. A turn is an ORDERED list of content
blocks, and the order is part of the data: Anthropic rejects a replayed
assistant message whose thinking block does not come first, and a model that
interleaved reasoning between two tool calls did not produce the same turn as
one that reasoned once up front.

Nothing here talks to a provider. Wire conversion belongs to the provider
adapters; this module only fixes the vocabulary both ends agree on.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from typing_extensions import Annotated, Literal

# Python snake_case, JSON camelCase: run.jsonl payloads have always been
# camelCase, so serialising by alias keeps new records readable by the same
# eyes and scripts as the old ones.
_MODEL_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    extra="forbid",
    frozen=True,
)


class PayloadRef(BaseModel):
    """A payload that lives on disk instead of inline in the record."""

    model_config = _MODEL_CONFIG

    kind: Literal["payload_ref"] = "payload_ref"
    path: str
    sha256: str
    byte_size: int
    media_type: str = "application/json"


class TruncationInfo(BaseModel):
    """Why a payload the reader is holding is not the whole story.

    The two truncation kinds must never be conflated, which is the entire
    reason this model exists:

    ``provider_truncated``  the LLM stopped at max_tokens. The rest was never
                            generated, so no file anywhere holds it. What we
                            can save is the prefix that did arrive -
                            ``received_output_path`` - and calling that a
                            "full output path" would be a lie.
    ``projection_truncated`` the harness shortened its own copy for the model's
                            context budget. The complete data does exist, at
                            ``saved_path``.

    ``source_complete`` is the flag that separates them: it says the harness
    once held the whole source payload.
    """

    model_config = _MODEL_CONFIG

    source_complete: bool
    provider_truncated: bool = False
    projection_truncated: bool = False
    # Only meaningful when source_complete is True.
    saved_path: Optional[str] = None
    # Only meaningful when provider_truncated is True.
    received_output_path: Optional[str] = None
    original_bytes: Optional[int] = None
    projected_bytes: Optional[int] = None
    # Hash of what was actually written to disk - which is the redacted body,
    # not the raw one. Naming it "persisted" rather than "original" keeps the
    # acceptance test satisfiable: hashing a redacted copy can never reproduce
    # the hash of the pre-redaction input.
    persisted_payload_sha256: Optional[str] = None
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _paths_match_their_kind(self) -> "TruncationInfo":
        """Enforce the distinction instead of only documenting it.

        A comment cannot stop `saved_path` being set on a provider-truncated
        record, and that single mistake is what turns this model back into the
        `fullOutputPath` lie it exists to prevent.
        """

        if self.saved_path is not None and not self.source_complete:
            raise ValueError(
                "saved_path requires source_complete: the harness never held "
                "the whole payload, so no file holds it either"
            )
        if self.received_output_path is not None and not self.provider_truncated:
            raise ValueError(
                "received_output_path is only meaningful when the provider "
                "stopped early"
            )
        if self.provider_truncated and self.source_complete:
            raise ValueError(
                "a turn the provider cut short is not a complete source"
            )
        return self


class TextContent(BaseModel):
    model_config = _MODEL_CONFIG

    type: Literal["text"] = "text"
    text: str = ""


class ThinkingContent(BaseModel):
    """Extended thinking / reasoning, in the position the model emitted it.

    ``signature`` and ``encrypted`` are opaque provider tokens that must survive
    the round trip untouched: Anthropic rejects a thinking block replayed
    without its signature, and OpenAI's encrypted reasoning is unusable if
    re-encoded.
    """

    model_config = _MODEL_CONFIG

    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    signature: Optional[str] = None
    # Anthropic redacted_thinking: content is deliberately unreadable to us.
    redacted: bool = False
    encrypted: Optional[str] = None


class ToolCallContent(BaseModel):
    model_config = _MODEL_CONFIG

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    # A stream cut off mid-JSON produces a call we could not parse. Marking it
    # keeps it out of the "the model asked for this" category: it never did.
    partial: bool = False
    raw_arguments: Optional[str] = None


ContentBlock = Annotated[
    Union[TextContent, ThinkingContent, ToolCallContent],
    Field(discriminator="type"),
]


class UserMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["user"] = "user"
    content: str = ""


class AssistantMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["assistant"] = "assistant"
    content: List[ContentBlock] = Field(default_factory=list)
    stop_reason: Optional[str] = None
    usage: Dict[str, Any] = Field(default_factory=dict)

    def block_kinds(self) -> List[str]:
        """Ordered block types - the shape of the turn, without its content."""
        return [block.type for block in self.content]

    def text(self) -> str:
        return "".join(
            block.text for block in self.content if isinstance(block, TextContent)
        )

    def thinking_blocks(self) -> List[ThinkingContent]:
        return [
            block for block in self.content if isinstance(block, ThinkingContent)
        ]

    def tool_calls(self) -> List[ToolCallContent]:
        return [
            block for block in self.content if isinstance(block, ToolCallContent)
        ]

    def structure_digest(self) -> str:
        """Stable hash of block order and sizes, with no content.

        Lets a lifecycle event prove "thinking came first, then two tool calls"
        without copying reasoning text into the audit log.
        """
        parts: List[Any] = []
        for block in self.content:
            if isinstance(block, TextContent):
                parts.append(["text", len(block.text)])
            elif isinstance(block, ThinkingContent):
                parts.append([
                    "thinking",
                    len(block.thinking),
                    bool(block.signature),
                    bool(block.redacted),
                ])
            else:
                parts.append(["tool_call", block.name, bool(block.partial)])
        canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolResultMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str = ""
    is_error: bool = False
    content: str = ""
    truncation: Optional[TruncationInfo] = None


class BrowserObservation(BaseModel):
    """One safe projection of asynchronous browser state, for the model.

    ``attribution`` is deliberately part of the observation: a page that opened
    while a click was in flight was not necessarily opened BY that click, and
    the model must be able to tell the difference.
    """

    model_config = _MODEL_CONFIG

    kind: str
    summary: str
    page_id: Optional[str] = None
    attribution: Literal[
        "caused_by_execution",
        "correlated_tool_call",
        "observed_during_tool_call",
        "unattributed",
    ] = "unattributed"
    tool_call_id: Optional[str] = None


class BrowserObservationMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["browserObservation"] = "browserObservation"
    observations: List[BrowserObservation] = Field(default_factory=list)
    source_event_ids: List[str] = Field(default_factory=list)
    # Only "include" ever reaches a provider; the rest stay in the transcript
    # for audit. The default is the safe one.
    context_policy: Literal["include", "exclude", "summary_only"] = "exclude"


class CompactionSummaryMessage(BaseModel):
    model_config = _MODEL_CONFIG

    role: Literal["compactionSummary"] = "compactionSummary"
    content: str = ""
    replaced_message_count: int = 0
    # Mechanical checkpoint details travel beside the model-facing text.  They
    # are deliberately structured so a later compaction can union facts such
    # as file references without asking an LLM to remember them verbatim.
    details: Dict[str, Any] = Field(default_factory=dict)
    checkpoint_id: Optional[str] = None
    tokens_before: Optional[int] = None


AgentMessage = Annotated[
    Union[
        UserMessage,
        AssistantMessage,
        ToolResultMessage,
        BrowserObservationMessage,
        CompactionSummaryMessage,
    ],
    Field(discriminator="role"),
]

__all__ = [
    "AgentMessage",
    "AssistantMessage",
    "BrowserObservation",
    "BrowserObservationMessage",
    "CompactionSummaryMessage",
    "ContentBlock",
    "PayloadRef",
    "TextContent",
    "ThinkingContent",
    "ToolCallContent",
    "ToolResultMessage",
    "TruncationInfo",
    "UserMessage",
]
