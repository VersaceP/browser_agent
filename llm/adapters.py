"""Pure codecs for the two supported APIs. They never mutate transcripts."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from harness.messages.models import (
    AssistantMessage, ImageContent, TextContent, ThinkingContent, ToolCallContent,
    ToolResultMessage, UserMessage,
)
from llm.contracts import LLMRequest, LLMResult, TokenUsage
from llm.profiles import ANTHROPIC, OPENAI, TOOL_RESULT_IMAGE_PLACEMENT, resolve_target
from llm.thinking import anthropic_thinking_request, openai_thinking_request, resolve_thinking_intent


def field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _input_blocks(content: Any, api: str) -> Any:
    if isinstance(content, str):
        return content
    blocks = []
    for block in content:
        if isinstance(block, TextContent):
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageContent):
            if api == OPENAI:
                blocks.append({"type": "image_url", "image_url": {
                    "url": block.url if block.url is not None else f"data:{block.media_type};base64,{block.data}",
                }})
            else:
                source = ({"type": "url", "url": block.url} if block.url is not None else {
                    "type": "base64", "media_type": block.media_type, "data": block.data,
                })
                blocks.append({"type": "image", "source": source})
        else:
            raise TypeError(f"unsupported input content: {type(block).__name__}")
    return blocks


def encode_messages(messages: list, api: str, *, tool_result_image_placement: str = "native") -> list[dict]:
    result: list[dict] = []
    pending_results: list[dict] = []
    pending_images: list[dict] = []

    def flush() -> None:
        if pending_results:
            result.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()
        if pending_images:
            # Keep the whole tool-result batch adjacent to its calls. Attach
            # images as user content only after all results in the batch.
            result.append({"role": "user", "content": list(pending_images)})
            pending_images.clear()

    for message in messages:
        if isinstance(message, ToolResultMessage):
            if api == ANTHROPIC:
                content = _input_blocks(message.content, api)
                if tool_result_image_placement == "user" and isinstance(content, list):
                    images = [b for b in content if b["type"] == "image"]
                    if images:
                        pending_images.extend(images)
                        pending_images.append({"type": "text", "text": f"The preceding images belong to tool result {message.tool_call_id}."})
                        content = [b for b in content if b["type"] != "image"] or ""
                block = {"type": "tool_result", "tool_use_id": message.tool_call_id,
                         "content": content}
                if message.is_error:
                    block["is_error"] = True
                pending_results.append(block)
            else:
                content = _input_blocks(message.content, api)
                if isinstance(content, list):
                    images = [b for b in content if b["type"] == "image_url"]
                    if images:
                        pending_images.extend(images)
                        pending_images.append({"type": "text", "text": f"The preceding images belong to tool result {message.tool_call_id}."})
                    content = "\n".join(b["text"] for b in content if b["type"] == "text")
                if message.is_error:
                    # Chat has no is_error field; retain the flag in tool data.
                    content = json.dumps({"is_error": True, "content": content}, ensure_ascii=False)
                result.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": content})
            continue
        flush()
        if isinstance(message, UserMessage):
            result.append({"role": "user", "content": _input_blocks(message.content, api)})
            continue
        if not isinstance(message, AssistantMessage):
            raise TypeError(f"unsupported model message: {type(message).__name__}")
        if api == ANTHROPIC:
            blocks = []
            for block in message.content:
                if isinstance(block, TextContent):
                    if block.text:
                        blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ThinkingContent):
                    if block.redacted:
                        blocks.append({"type": "redacted_thinking", "data": block.encrypted or ""})
                    else:
                        native = {"type": "thinking", "thinking": block.thinking}
                        if block.signature is not None:
                            native["signature"] = block.signature
                        if block.encrypted is not None:
                            native["encrypted_content"] = block.encrypted
                        blocks.append(native)
                elif isinstance(block, ToolCallContent) and not block.partial:
                    blocks.append({"type": "tool_use", "id": block.tool_call_id, "name": block.name,
                                   "input": deepcopy(block.arguments)})
            result.append({"role": "assistant", "content": blocks})
        else:
            native = {"role": "assistant"}
            text = message.text()
            if text:
                native["content"] = text
            calls = [{"id": b.tool_call_id, "type": "function", "function": {
                "name": b.name, "arguments": json.dumps(b.arguments, ensure_ascii=False),
            }} for b in message.tool_calls() if not b.partial]
            if calls:
                native["tool_calls"] = calls
            thinking = [b for b in message.thinking_blocks() if not b.redacted]
            if any(b.redacted for b in message.thinking_blocks()):
                raise ValueError("Chat cannot represent Anthropic redacted_thinking; project an explicit compatible history first")
            reasoning = "".join(b.thinking for b in thinking)
            if reasoning:
                native["reasoning_content"] = reasoning
            encrypted = [b.encrypted for b in thinking if b.encrypted is not None]
            if len(encrypted) > 1:
                raise ValueError("Chat cannot encode multiple opaque encrypted reasoning blocks as one field")
            if encrypted:
                native["encrypted_content"] = encrypted[0]
            if not text and not calls and reasoning:
                # Chat needs assistant content or calls. Preserve readable
                # reasoning as historical text rather than dropping the turn.
                native["content"] = reasoning
                native.pop("reasoning_content", None)
            elif not text and not calls and encrypted:
                raise ValueError("Chat cannot replay an opaque-only assistant turn without content or tool calls")
            if text or calls or reasoning or encrypted:
                result.append(native)
    flush()
    return result


def _tool_choice(value: Any, api: str) -> Any:
    if value is None:
        return None
    if hasattr(value, "mode"):
        if value.mode == "named":
            if not value.name:
                raise ValueError("named tool_choice requires name")
            value = {"type": "tool", "name": value.name}
        else:
            value = value.mode
    if api == ANTHROPIC:
        if isinstance(value, str):
            return {"type": {"required": "any"}.get(value, value)}
        if value.get("type") == "function":
            return {"type": "tool", "name": value["function"]["name"]}
    else:
        if isinstance(value, dict):
            if value.get("type") == "tool":
                return {"type": "function", "function": {"name": value["name"]}}
            if value.get("type") in {"any", "auto", "none"}:
                return {"any": "required"}.get(value["type"], value["type"])
        elif value == "any":
            return "required"
    return deepcopy(value)


def tool_result_image_placement(config: Any) -> str:
    """Layout the encoder uses for an image nested in a tool result."""
    target = resolve_target(config)
    placement = (config.extra_params or {}).get(
        "tool_result_image_placement",
        TOOL_RESULT_IMAGE_PLACEMENT.get((target.profile, target.api), "native"),
    )
    if placement not in {"native", "user"}:
        raise ValueError("tool_result_image_placement must be native or user")
    return placement


def tool_result_image_accounting(config: Any) -> str:
    """How the transport counts an image the harness keeps in a tool result.

    ``base64_text`` only when the encoder leaves the image nested on a
    transport listed as flattening that form; every other path reaches the
    model as a vision input. Token estimates must read this, not assume it.
    """
    target = resolve_target(config)
    if (
        target.api == ANTHROPIC
        and (target.profile, target.api) in TOOL_RESULT_IMAGE_PLACEMENT
        and tool_result_image_placement(config) == "native"
    ):
        return "base64_text"
    return "vision"


def encode_request(request: LLMRequest, config: Any, *, cache_enabled: bool = False,
                   thinking_disabled: bool = False) -> dict[str, Any]:
    target = resolve_target(config)
    from llm.profiles import RESPONSES
    if target.api == RESPONSES:
        from llm.responses import encode_responses_request
        return encode_responses_request(request, config, thinking_disabled=thinking_disabled)
    api = target.api
    image_placement = tool_result_image_placement(config)
    extra = deepcopy(config.extra_params or {})
    extra.pop("tool_result_image_placement", None)
    options = request.options
    if options.thinking is not None:
        thinking = options.thinking
        for key in ("thinking", "reasoning_effort", "effort"):
            extra.pop(key, None)
        if thinking.enabled is not None:
            extra["thinking"] = {"type": "enabled" if thinking.enabled else "disabled"}
            if thinking.budget_tokens is not None:
                extra["thinking"]["budget_tokens"] = thinking.budget_tokens
        elif thinking.budget_tokens is not None:
            extra["thinking"] = {"type": "enabled", "budget_tokens": thinking.budget_tokens}
        if thinking.effort is not None:
            extra["reasoning_effort"] = thinking.effort
    if thinking_disabled:
        extra["thinking"] = {"type": "disabled"}
        extra.pop("reasoning_effort", None)
        extra.pop("effort", None)
    intent = resolve_thinking_intent(extra)
    reserved = {"thinking", "reasoning_effort", "effort", "tool_choice", "strict_tools",
                "cache_control_mode", "enable_cache_control", "stream", "extra_body",
                "max_tokens_semantics", "max_output_tokens"}
    protected = {"model", "messages", "system", "tools"}
    # These are envelope identity/content invariants, not business validation.
    body = deepcopy(extra.get("extra_body") or {})
    if protected.intersection(extra) or (protected | {"stream", "stream_options"}).intersection(body):
        raise ValueError("extra_params must not override model/messages/system/tools or transport streaming")
    if options.thinking is not None or thinking_disabled:
        for key in ("thinking", "enable_thinking", "thinking_budget", "reasoning_effort", "output_config"):
            body.pop(key, None)
    params = {k: v for k, v in extra.items()
              if k not in reserved and not k.startswith("llm_")}
    params["model"] = target.model
    params["messages"] = encode_messages(request.messages, api, tool_result_image_placement=image_placement)
    # Responses names the total output limit max_output_tokens; Chat names it
    # max_tokens. A role's protocol can change while a shared extra_params block
    # keeps the other spelling - sending it verbatim makes the SDK raise
    # TypeError instead of degrading, so carry the value across.
    if "max_tokens" not in params and "max_completion_tokens" not in params:
        carried = extra.get("max_output_tokens")
        if carried is not None:
            params["max_tokens"] = carried
    if "max_tokens" not in params and "max_completion_tokens" not in params:
        params["max_tokens"] = 4096
    # A deployment may declare the documented meaning of its model's native
    # max_tokens. This is configuration, never inferred from a model name.
    max_tokens_semantics = extra.get("max_tokens_semantics")
    if max_tokens_semantics not in {None, "total", "answer"}:
        raise ValueError("max_tokens_semantics must be total or answer")
    if max_tokens_semantics is None and api == ANTHROPIC and target.profile != "qwen-token-plan":
        max_tokens_semantics = "total"
    if max_tokens_semantics is None and api == OPENAI and target.profile == "qwen-token-plan":
        max_tokens_semantics = "answer"
    if options.total_output_tokens is not None:
        params.pop("max_tokens", None)
        if api == OPENAI:
            params["max_completion_tokens"] = options.total_output_tokens
        elif max_tokens_semantics == "total":
            params["max_tokens"] = options.total_output_tokens
        else:
            raise ValueError("total_output_tokens needs a documented total limit; configure max_tokens_semantics or use native max_tokens")
    if options.answer_tokens is not None:
        if max_tokens_semantics != "answer":
            raise ValueError("answer_tokens needs a documented answer limit; configure max_tokens_semantics or use native max_tokens")
        params["max_tokens"] = options.answer_tokens
    if options.temperature is not None:
        params["temperature"] = options.temperature

    if api == OPENAI:
        native, thinking_body, _ = openai_thinking_request(intent)
        if target.profile == "qwen-token-plan":
            switch = thinking_body.pop("thinking", None)
            if switch is not None:
                thinking_body["enable_thinking"] = switch.get("type") != "disabled"
                if "budget_tokens" in switch:
                    thinking_body["thinking_budget"] = switch["budget_tokens"]
            if "reasoning_effort" in native:
                thinking_body["reasoning_effort"] = native.pop("reasoning_effort")
        params.update(native)
        body.update(thinking_body)
    else:
        native, _ = anthropic_thinking_request(intent, params.get("max_tokens", 4096))
        params.update(native)
    if body:
        params["extra_body"] = body

    system = [{"type": "text", "text": b.text} for b in request.system]
    if cache_enabled and system:
        system[-1]["cache_control"] = {"type": "ephemeral"}
    system_value = system if cache_enabled or len(system) != 1 else system[0]["text"]
    if api == ANTHROPIC:
        params["system"] = system_value
    elif system:
        params["messages"].insert(0, {"role": "system", "content": system_value})
    if cache_enabled and request.messages and params["messages"]:
        last = params["messages"][-1]
        content = last.get("content")
        if isinstance(content, str) and content:
            last["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list) and content:
            content[-1]["cache_control"] = {"type": "ephemeral"}
    if request.tools:
        tools = []
        for tool in request.tools:
            schema = {"name": tool.name, "description": tool.description,
                      "input_schema" if api == ANTHROPIC else "parameters": deepcopy(
                          tool.schema_overrides.get(target.profile, tool.schema_overrides.get(api, tool.parameters)))}
            strict = tool.strict if tool.strict is not None else extra.get("strict_tools")
            if strict:
                schema["strict"] = True
            tools.append(schema if api == ANTHROPIC else {"type": "function", "function": schema})
        if cache_enabled and api == ANTHROPIC:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        params["tools"] = tools
        choice = _tool_choice(options.tool_choice or extra.get("tool_choice", "auto"), api)
        if choice is not None:
            params["tool_choice"] = choice
    return params


def decode_response(response: Any, config: Any, diagnostics: dict | None = None) -> LLMResult:
    target = resolve_target(config)
    from llm.profiles import RESPONSES
    if target.api == RESPONSES:
        from llm.responses import decode_responses_response
        return decode_responses_response(response, config, diagnostics)
    native_usage = field(response, "usage")
    def count(key: str, obj: Any = native_usage) -> int | None:
        value = field(obj, key)
        return int(value) if value is not None else None
    blocks = []
    if target.api == ANTHROPIC:
        for block in field(response, "content", []) or []:
            kind = field(block, "type")
            if kind == "text":
                blocks.append(TextContent(text=field(block, "text", "")))
            elif kind in {"thinking", "redacted_thinking"}:
                blocks.append(ThinkingContent(
                    thinking=field(block, "thinking", ""), signature=field(block, "signature"),
                    redacted=kind == "redacted_thinking", encrypted=field(block, "data") if kind == "redacted_thinking" else field(block, "encrypted_content"),
                ))
            elif kind == "tool_use":
                blocks.append(ToolCallContent(tool_call_id=field(block, "id"), name=field(block, "name"), arguments=field(block, "input")))
            else:
                raise ValueError(f"unsupported Anthropic response content type: {kind!r}")
        raw_stop = field(response, "stop_reason")
        stop = raw_stop or "unknown"
        cache_read, cache_write = count("cache_read_input_tokens"), count("cache_creation_input_tokens")
        input_tokens = count("input_tokens")
        usage = TokenUsage(
            input_tokens=None if input_tokens is None else input_tokens + (cache_read or 0) + (cache_write or 0),
            output_tokens=count("output_tokens"), cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        )
    else:
        choices = field(response, "choices", []) or []
        if not choices:
            raise ValueError("Chat response has no choices")
        choice = choices[0]
        message = field(choice, "message")
        reasoning, encrypted = field(message, "reasoning_content"), field(message, "encrypted_content")
        if reasoning or encrypted:
            blocks.append(ThinkingContent(thinking=reasoning or "", encrypted=encrypted))
        text = field(message, "content")
        if text:
            blocks.append(TextContent(text=text))
        for call in field(message, "tool_calls", []) or []:
            function = field(call, "function")
            raw = field(function, "arguments", "")
            arguments = json.loads(raw) if raw else {}
            blocks.append(ToolCallContent(tool_call_id=field(call, "id"), name=field(function, "name"), arguments=arguments))
        raw_stop = field(choice, "finish_reason")
        stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(raw_stop, raw_stop or "unknown")
        usage = TokenUsage(
            input_tokens=count("prompt_tokens"), output_tokens=count("completion_tokens"),
            cache_read_tokens=count("cached_tokens", field(native_usage, "prompt_tokens_details")),
            reasoning_tokens=count("reasoning_tokens", field(native_usage, "completion_tokens_details")),
        )
    message = AssistantMessage(
        content=blocks, stop_reason=stop, raw_stop_reason=raw_stop,
        provider=target.profile, api=target.api, model=field(response, "model") or target.model,
        response_id=field(response, "id"), usage=usage.model_dump(),
    )
    return LLMResult(message=message, usage=usage, diagnostics=diagnostics or {})
