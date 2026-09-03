"""harness.messages.convert - The single boundary between canonical and wire.

The harness's internal transcript has always been Anthropic-shaped, and the
OpenAI provider translates it on the way out. That stays true; what changes is
that exactly one function now decides what an assistant turn looks like on the
wire, instead of two agent loops each assembling the blocks by hand from a
private ``usage["_assistant_prefix_blocks"]`` key.

Block ORDER is the contract. Anthropic rejects a replayed assistant message
whose thinking does not lead, and a signature or an encrypted reasoning blob
that is re-encoded on the way through is no longer valid - so both travel
verbatim.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from harness.messages.models import (
    AssistantMessage,
    BrowserObservationMessage,
    CompactionSummaryMessage,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)

ProviderKind = str


def content_block_to_wire(block: Any) -> Optional[Dict[str, Any]]:
    """One canonical block as the harness's internal (Anthropic-shaped) dict."""

    if isinstance(block, ThinkingContent):
        if block.redacted:
            return {"type": "redacted_thinking", "data": block.encrypted or ""}
        wire: Dict[str, Any] = {"type": "thinking", "thinking": block.thinking}
        # Absent, not null: the providers only add these keys when the model
        # actually sent them, and an unexpected null is a 400 on some
        # endpoints.
        if block.signature:
            wire["signature"] = block.signature
        if block.encrypted:
            wire["encrypted_content"] = block.encrypted
        return wire
    if isinstance(block, TextContent):
        # An empty text block is not something a model produced; emitting one
        # would change the wire bytes for a tool-only turn.
        if not block.text:
            return None
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolCallContent):
        if block.partial:
            # A call whose JSON never finished parsing was never a request the
            # model completed. Replaying it as one would invent an intent.
            return None
        return {
            "type": "tool_use",
            "id": block.tool_call_id,
            "name": block.name,
            "input": block.arguments,
        }
    return None


def assistant_message_to_wire(message: AssistantMessage) -> Dict[str, Any]:
    """The ``{"role": "assistant", "content": [...]}`` entry for the transcript."""

    content: List[Dict[str, Any]] = []
    for block in message.content:
        wire = content_block_to_wire(block)
        if wire is not None:
            content.append(wire)
    return {"role": "assistant", "content": content}


def assistant_blocks_from_wire(
    blocks: Optional[Iterable[Dict[str, Any]]],
) -> List[Any]:
    """Parse harness-internal assistant content back into canonical blocks."""

    parsed: List[Any] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "thinking":
            parsed.append(
                ThinkingContent(
                    thinking=str(block.get("thinking") or ""),
                    signature=block.get("signature"),
                    encrypted=block.get("encrypted_content"),
                )
            )
        elif kind == "redacted_thinking":
            parsed.append(
                ThinkingContent(
                    thinking="", redacted=True, encrypted=str(block.get("data") or ""),
                )
            )
        elif kind == "text":
            parsed.append(TextContent(text=str(block.get("text") or "")))
        elif kind == "tool_use":
            parsed.append(
                ToolCallContent(
                    tool_call_id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=(
                        block.get("input")
                        if isinstance(block.get("input"), dict) else {}
                    ),
                )
            )
    return parsed


def tool_result_to_wire(message: ToolResultMessage) -> Dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id,
        "content": message.content,
    }


def to_model_messages(
    messages: Iterable[Any],
    provider: ProviderKind = "anthropic",
) -> List[Dict[str, Any]]:
    """Canonical transcript -> the message list a provider is handed.

    Session-only messages are dropped here and nowhere else. A browser
    observation whose policy is not ``include`` never reaches a model, which is
    the whole reason asynchronous browser events can be kept in the transcript
    for audit without being asserted to the model as facts about its own call.
    """

    wire: List[Dict[str, Any]] = []
    pending_results: List[Dict[str, Any]] = []

    def flush() -> None:
        if pending_results:
            wire.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if isinstance(message, AssistantMessage):
            flush()
            wire.append(assistant_message_to_wire(message))
        elif isinstance(message, ToolResultMessage):
            # Tool results are collected so they stay adjacent to the assistant
            # turn that asked for them; splitting them across user messages is
            # what breaks tool pairing.
            pending_results.append(tool_result_to_wire(message))
        elif isinstance(message, UserMessage):
            flush()
            wire.append({"role": "user", "content": message.content})
        elif isinstance(message, CompactionSummaryMessage):
            flush()
            wire.append({"role": "user", "content": message.content})
        elif isinstance(message, BrowserObservationMessage):
            if message.context_policy != "include":
                continue
            flush()
            wire.append({
                "role": "user",
                "content": "\n".join(
                    observation.summary for observation in message.observations
                ),
            })
        elif isinstance(message, dict):
            # Migration path: the loops still hold raw wire dicts.
            flush()
            wire.append(message)
    flush()
    return wire


__all__ = [
    "assistant_blocks_from_wire",
    "assistant_message_to_wire",
    "content_block_to_wire",
    "to_model_messages",
    "tool_result_to_wire",
]
