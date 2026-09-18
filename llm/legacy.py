"""One-way migration of the existing Harness transcript into neutral types.

No provider encoder consumes these dictionaries. Session-only records are
projected here before LLMRequest is constructed; no transport policy lives here.
"""
from copy import deepcopy
from typing import Any, Iterable

from harness.messages.models import (
    AssistantMessage, BrowserObservationMessage, CompactionSummaryMessage,
    ImageContent, TextContent, ThinkingContent, ToolCallContent,
    ToolResultMessage, UserMessage,
)
from llm.contracts import LLMRequest, ToolDefinition


def result_from_legacy(response: tuple):
    """Bridge for external tuple providers only, never for built-in adapters."""
    from llm.contracts import LLMResult, TokenUsage
    from harness.events.recorder import assistant_message_from_parts
    text, calls, stop, usage = response
    usage = usage or {}
    return LLMResult(
        message=assistant_message_from_parts(text=text, tool_calls=calls, stop_reason=stop,
                                            prefix_blocks=usage.get("_assistant_prefix_blocks")),
        usage=TokenUsage(
            input_tokens=sum(usage.get(k, 0) or 0 for k in ("uncached_input", "cache_read", "cache_creation")),
            output_tokens=usage.get("output"), cache_read_tokens=usage.get("cache_read"),
            cache_write_tokens=usage.get("cache_creation"),
        ), diagnostics={k: v for k, v in usage.items() if not k.startswith("_assistant")},
    )


def input_content(value: Any) -> Any:
    if isinstance(value, str):
        return value
    result = []
    for block in value or []:
        if isinstance(block, (TextContent, ImageContent)):
            result.append(block)
        elif block.get("type") == "text":
            result.append(TextContent(text=block.get("text", "")))
        elif block.get("type") == "image":
            source = block.get("source", {})
            result.append(ImageContent(
                media_type=source.get("media_type", "image/png"),
                data=source.get("data"), url=source.get("url"),
            ))
        elif block.get("type") == "image_url":
            result.append(ImageContent(url=block["image_url"]["url"]))
        else:
            raise ValueError(f"unsupported legacy input block: {block.get('type')!r}")
    return result


def assistant_content(value: Any) -> list:
    if isinstance(value, str):
        return [TextContent(text=value)]
    result = []
    for block in value or []:
        kind = block.get("type")
        if kind == "text":
            result.append(TextContent(text=block.get("text", "")))
        elif kind in {"thinking", "redacted_thinking"}:
            result.append(ThinkingContent(
                thinking=block.get("thinking", ""), signature=block.get("signature"),
                redacted=kind == "redacted_thinking",
                encrypted=block.get("data") if kind == "redacted_thinking" else block.get("encrypted_content"),
            ))
        elif kind == "tool_use":
            result.append(ToolCallContent(tool_call_id=block["id"], name=block["name"], arguments=block.get("input", {})))
        else:
            raise ValueError(f"unsupported legacy assistant block: {kind!r}")
    return result


def project_messages(messages: Iterable[Any]) -> list:
    projected = []
    for message in messages:
        if isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage)):
            projected.append(message.model_copy(deep=True))
        elif isinstance(message, CompactionSummaryMessage):
            projected.append(UserMessage(content=message.content))
        elif isinstance(message, BrowserObservationMessage):
            if message.context_policy == "include":
                projected.append(UserMessage(content="\n".join(o.summary for o in message.observations)))
        elif isinstance(message, dict):
            role = message.get("role")
            content = message.get("content", "")
            if role == "assistant":
                projected.append(AssistantMessage(content=assistant_content(content)))
            elif role == "user":
                if isinstance(content, str):
                    projected.append(UserMessage(content=content))
                    continue
                pending = []
                for block in content:
                    if block.get("type") == "tool_result":
                        if pending:
                            projected.append(UserMessage(content=input_content(pending)))
                            pending = []
                        projected.append(ToolResultMessage(
                            tool_call_id=block["tool_use_id"], tool_name=block.get("tool_name", ""),
                            is_error=block.get("is_error", False), content=input_content(block.get("content", "")),
                        ))
                    else:
                        pending.append(block)
                if pending or not content:
                    projected.append(UserMessage(content=input_content(pending)))
            else:
                raise ValueError(f"unsupported legacy message role: {role!r}")
        else:
            raise TypeError(f"unsupported transcript message: {type(message).__name__}")
    return projected


def request_from_legacy(system_prompt: str, messages: Iterable[Any], tools: Iterable[Any]) -> LLMRequest:
    return LLMRequest(
        system=[TextContent(text=system_prompt)], messages=project_messages(messages),
        tools=[tool if isinstance(tool, ToolDefinition) else ToolDefinition(
            name=tool["name"], description=tool.get("description", ""),
            parameters=deepcopy(tool.get("input_schema", {"type": "object", "properties": {}})),
            strict=tool.get("strict"),
        ) for tool in tools],
    )
