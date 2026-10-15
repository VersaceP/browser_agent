"""Loss-aware bridge between the Harness LLM contract and Tau's loop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
import time
from typing import Any

from harness._vendor.tau_agent import messages as tau
from harness._vendor.tau_agent.provider_events import AssistantDoneEvent, AssistantStartEvent
from harness._vendor.tau_agent.tools import AgentTool
from harness.messages import models as neutral
from llm.contracts import LLMRequest, ToolDefinition


def _neutral_message(message: tau.AgentMessage) -> neutral.UserMessage | neutral.AssistantMessage | neutral.ToolResultMessage | neutral.CompactionSummaryMessage:
    if isinstance(message, tau.CompactionSummaryMessage):
        return neutral.CompactionSummaryMessage(content=message.summary,
            tokens_before=message.tokens_before, checkpoint_id=message.checkpoint_id,
            details=message.details, replaced_message_count=message.replaced_message_count)
    if isinstance(message, tau.UserMessage):
        if isinstance(message.content, str):
            content: Any = message.content
        else:
            content = [
                neutral.TextContent(text=block.text) if isinstance(block, tau.TextContent)
                else neutral.ImageContent(data=block.data, media_type=block.mime_type)
                for block in message.content
            ]
        return neutral.UserMessage(content=content)
    if isinstance(message, tau.ToolResultMessage):
        content = [
            neutral.TextContent(text=block.text) if isinstance(block, tau.TextContent)
            else neutral.ImageContent(data=block.data, media_type=block.mime_type)
            for block in message.content
        ]
        return neutral.ToolResultMessage(
            tool_call_id=message.tool_call_id, tool_name=message.tool_name,
            content=content, is_error=message.is_error,
        )
    if isinstance(message, tau.AssistantMessage):
        blocks: list[Any] = []
        for block in message.content:
            if isinstance(block, tau.TextContent):
                blocks.append(neutral.TextContent(text=block.text))
            elif isinstance(block, tau.ThinkingContent):
                blocks.append(neutral.ThinkingContent(
                    thinking=block.thinking, signature=block.thinking_signature,
                    redacted=block.redacted, encrypted=block.encrypted,
                ))
            elif isinstance(block, tau.ToolCall):
                blocks.append(neutral.ToolCallContent(
                    tool_call_id=block.id, name=block.name,
                    arguments=dict(block.arguments),
                ))
        return neutral.AssistantMessage(
            content=blocks, stop_reason={
                "stop": "end_turn", "toolUse": "tool_use", "length": "max_tokens",
            }.get(message.stop_reason, message.stop_reason),
            provider=message.provider, api=message.api, model=message.model,
            response_id=message.response_id, raw_stop_reason=message.raw_stop_reason,
        )
    return neutral.UserMessage(content=tau.message_text(message))


def _request_message(message: tau.AgentMessage) -> neutral.UserMessage | neutral.AssistantMessage | neutral.ToolResultMessage:
    """Project session-only checkpoints at the model request boundary.

    Keep the checkpoint identity and facts in the transcript used by subsequent
    compaction. LLMRequest accepts ordinary conversational messages; its summary
    projection matches harness.messages.convert.to_model_messages.
    """
    converted = _neutral_message(message)
    if isinstance(message, tau.AssistantMessage) and message.stop_reason == "length":
        # Raw output remains in durable history. Incomplete thinking cannot be
        # replayed as a completed provider response. Keep tool-result pairing.
        calls = [b for b in converted.content if isinstance(b, neutral.ToolCallContent)]
        return neutral.AssistantMessage(content=[neutral.TextContent(text=
            "[Harness: response truncated; its tool calls were not executed. "
            "Consult saved files and completed tool receipts before continuing.]"), *calls],
            stop_reason="tool_use" if calls else "end_turn")
    if isinstance(converted, neutral.CompactionSummaryMessage):
        return neutral.UserMessage(content=converted.content)
    return converted


def _tau_assistant(result: Any, model: str) -> tau.AssistantMessage:
    source = result.message
    blocks: list[Any] = []
    for block in source.content:
        if isinstance(block, neutral.TextContent):
            blocks.append(tau.TextContent(text=block.text))
        elif isinstance(block, neutral.ThinkingContent):
            blocks.append(tau.ThinkingContent(
                thinking=block.thinking, thinking_signature=block.signature,
                redacted=block.redacted, encrypted=block.encrypted,
            ))
        elif isinstance(block, neutral.ToolCallContent):
            if block.partial:
                continue
            blocks.append(tau.ToolCall(id=block.tool_call_id, name=block.name,
                                       arguments=block.arguments))
    stop = str(source.stop_reason or "")
    normalized = (
        "length" if stop in {"max_tokens", "length", "max_output_tokens"}
        else "toolUse" if stop in {"tool_use", "tool_calls", "toolUse"}
        else "aborted" if stop in {"aborted", "cancelled"}
        else "error" if stop == "error"
        else "stop"
    )
    # Tau's legacy Usage shape has numeric defaults. The neutral usage record
    # remains authoritative and is logged separately with unknowns intact.
    usage = result.usage
    return tau.AssistantMessage(
        content=blocks, model=source.model or model,
        api=source.api or "unknown", provider=source.provider or "unknown",
        response_id=source.response_id, raw_stop_reason=source.raw_stop_reason,
        stop_reason=normalized,
        usage_available=usage.input_tokens is not None or usage.output_tokens is not None,
        usage=tau.Usage(
            input=usage.input_tokens or 0, output=usage.output_tokens or 0,
            cache_read=usage.cache_read_tokens or 0,
            cache_write=usage.cache_write_tokens or 0,
            reasoning=usage.reasoning_tokens,
            total_tokens=(usage.input_tokens or 0) + (usage.output_tokens or 0),
        ),
    )


class HarnessProviderAdapter:
    def __init__(self, provider: Any, *, on_result: Callable[[Any], None] | None = None):
        self.provider = provider
        self.on_result = on_result

    async def stream_response(
        self, *, model: str, system: str, messages: list[tau.AgentMessage],
        tools: list[AgentTool], signal: Any = None, session_id: str | None = None,
    ) -> AsyncIterator[Any]:
        if signal is not None and signal.is_cancelled():
            yield AssistantDoneEvent(reason="stop", message=tau.AssistantMessage(
                model=model, content=[], stop_reason="stop"))
            return
        request = LLMRequest(
            system=[neutral.TextContent(text=system)],
            messages=[_request_message(message) for message in messages],
            tools=[ToolDefinition(name=tool.name, description=tool.description,
                                  parameters=dict(tool.parameters)) for tool in tools],
        )
        started = time.monotonic()
        result = await self.provider.generate(request)
        result.diagnostics.setdefault("duration_ms", round((time.monotonic() - started) * 1000))
        if self.on_result is not None:
            self.on_result(result)
        assistant = _tau_assistant(result, model)
        # The current Harness provider returns a complete response. Emit only
        # real start/end boundaries; do not simulate token deltas.
        yield AssistantStartEvent(partial=tau.AssistantMessage(model=model))
        yield AssistantDoneEvent(reason=assistant.stop_reason if assistant.stop_reason in
                                 {"stop", "length", "toolUse"} else "stop", message=assistant)
