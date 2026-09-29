"""Stateless OpenAI Responses codec, including encrypted reasoning replay."""
import json
from copy import deepcopy

from harness.messages.models import (AssistantMessage, UserMessage, ToolResultMessage,
    TextContent, ImageContent, ThinkingContent, ToolCallContent)
from llm.adapters import field, _tool_choice
from llm.contracts import LLMResult, TokenUsage
from llm.profiles import OPENAI, resolve_target
from llm.thinking import resolve_thinking_intent
from llm.base import LLMStreamDecodeError, LLMProviderResponseError


def input_content(content):
    if isinstance(content, str):
        return content
    return [{"type": "input_text", "text": b.text} if isinstance(b, TextContent)
            else {"type": "input_image", "image_url": b.url or f"data:{b.media_type};base64,{b.data}"}
            for b in content]


def encode_responses_request(request, config, *, thinking_disabled=False):
    extra = deepcopy(config.extra_params or {})
    body = extra.pop("extra_body", {}) or {}
    protected = {"model", "input", "messages", "instructions", "system", "tools",
                 "stream", "stream_options", "previous_response_id", "conversation"}
    if (protected - {"stream"}).intersection(extra) or protected.intersection(body):
        raise ValueError("extra_params must not override Responses envelope or conversation")
    options = request.options
    intent_extra = dict(extra)
    if options.thinking is not None:
        for key in ("thinking", "reasoning_effort", "effort"):
            intent_extra.pop(key, None)
        t = options.thinking
        if t.budget_tokens is not None:
            raise ValueError("Responses uses reasoning.effort, not budget_tokens")
        if t.enabled is not None:
            intent_extra["thinking"] = {"type": "enabled" if t.enabled else "disabled"}
        if t.effort:
            intent_extra["reasoning_effort"] = t.effort
    intent = resolve_thinking_intent(intent_extra)
    native_thinking = intent_extra.get("thinking")
    if isinstance(native_thinking, dict) and native_thinking.get("budget_tokens") is not None:
        raise ValueError("Responses uses reasoning.effort, not budget_tokens")
    if {"thinking", "reasoning_effort", "enable_thinking", "thinking_budget"}.intersection(body):
        raise ValueError("Responses extra_body must use reasoning, not Chat thinking controls")
    reserved = {"thinking", "reasoning_effort", "effort", "tool_choice", "strict_tools",
                "cache_control_mode", "enable_cache_control", "stream", "max_tokens_semantics",
                "tool_result_image_placement", "max_tokens", "max_completion_tokens"}
    params = {k: v for k, v in extra.items() if k not in reserved and not k.startswith("llm_")}
    if options.answer_tokens is not None:
        raise ValueError("Responses max_output_tokens is a total limit, not answer_tokens")
    params["max_output_tokens"] = options.total_output_tokens or extra.get("max_output_tokens", extra.get("max_completion_tokens", extra.get("max_tokens", 4096)))
    reasoning = deepcopy(params.get("reasoning", body.pop("reasoning", {})))
    if thinking_disabled or options.thinking is not None:
        reasoning.pop("effort", None)
    if thinking_disabled or intent.state == "disabled":
        reasoning["effort"] = "none"
    elif intent.effort:
        reasoning["effort"] = intent.effort
    if reasoning:
        params["reasoning"] = reasoning
    if options.temperature is not None:
        params["temperature"] = options.temperature
    params.update(model=resolve_target(config).model, store=False, input=[])
    body.pop("store", None)
    include = list(params.get("include", body.pop("include", [])))
    if "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
    params["include"] = include
    if body:
        params["extra_body"] = body
    if request.system:
        params["instructions"] = "\n\n".join(b.text for b in request.system)
    items = params["input"]
    for message in request.messages:
        if isinstance(message, UserMessage):
            items.append({"role": "user", "content": input_content(message.content)})
        elif isinstance(message, ToolResultMessage):
            output = input_content(message.content)
            if message.is_error:
                if isinstance(output, str):
                    output = json.dumps({"is_error": True, "content": output}, ensure_ascii=False)
                else:
                    output.insert(0, {"type": "input_text", "text": "is_error: true"})
            items.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": output})
        else:
            for block in message.content:
                if isinstance(block, TextContent) and block.text:
                    items.append({"role": "assistant", "content": [{"type": "output_text", "text": block.text}]})
                elif isinstance(block, ToolCallContent) and not block.partial:
                    items.append({"type": "function_call", "call_id": block.tool_call_id,
                                  "name": block.name, "arguments": json.dumps(block.arguments, ensure_ascii=False)})
                elif isinstance(block, ThinkingContent):
                    if block.redacted or block.signature:
                        raise ValueError("Responses cannot replay Anthropic thinking signatures")
                    if block.encrypted:
                        items.append({"type": "reasoning", "encrypted_content": block.encrypted,
                                      "summary": [{"type": "summary_text", "text": block.thinking}] if block.thinking else []})
    if request.tools:
        target = resolve_target(config)
        params["tools"] = [{"type": "function", "name": t.name, "description": t.description,
            "parameters": deepcopy(t.schema_overrides.get(target.profile, t.schema_overrides.get(target.api, t.parameters))),
            # Responses defaults omitted strict to strict normalization. Our
            # tools contain optional fields and open dictionaries; explicitly
            # preserve non-strict semantics instead of rewriting their schemas.
            "strict": t.strict if t.strict is not None else bool(extra.get("strict_tools", False))} for t in request.tools]
        choice = _tool_choice(options.tool_choice or extra.get("tool_choice", "auto"), OPENAI)
        if isinstance(choice, dict) and choice.get("type") == "function":
            choice = {"type": "function", "name": choice.get("name") or choice["function"]["name"]}
        if choice is not None:
            params["tool_choice"] = choice
    return params


def decode_responses_response(response, config, diagnostics=None):
    status = field(response, "status")
    error = field(response, "error")
    if error:
        # A provider rejection is not corrupt tool JSON and must not consume
        # stream-decode retries or lose its diagnostic in a generic wrapper.
        raise LLMProviderResponseError(code=field(error, "code"),
            message=field(error, "message"), request_id=field(response, "_request_id"))
    if status not in {"completed", "incomplete"}:
        raise LLMStreamDecodeError(f"Responses did not complete: {status}")
    blocks = []
    for item in field(response, "output", []) or []:
        kind = field(item, "type")
        if kind == "reasoning":
            blocks.append(ThinkingContent(thinking="\n".join(field(s, "text", "") for s in field(item, "summary", []) or []), encrypted=field(item, "encrypted_content")))
        elif kind == "message":
            for b in field(item, "content", []) or []:
                if field(b, "type") in {"output_text", "refusal"}:
                    blocks.append(TextContent(text=field(b, "text", field(b, "refusal", ""))))
                else:
                    raise ValueError(f"Unsupported Responses message block: {field(b, 'type')}")
        elif kind == "function_call":
            raw = field(item, "arguments", "")
            partial = status == "incomplete"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except (ValueError, TypeError) as exc:
                if not partial:
                    raise LLMStreamDecodeError("Responses malformed tool JSON", raw_arguments=raw) from exc
                args = {}
            blocks.append(ToolCallContent(tool_call_id=field(item, "call_id"), name=field(item, "name"), arguments=args, partial=partial, raw_arguments=raw if partial else None))
        else:
            raise ValueError(f"Unsupported Responses output type: {kind}")
    u = field(response, "usage")
    usage = TokenUsage(input_tokens=field(u, "input_tokens"), output_tokens=field(u, "output_tokens"),
        cache_read_tokens=field(field(u, "input_tokens_details"), "cached_tokens"),
        reasoning_tokens=field(field(u, "output_tokens_details"), "reasoning_tokens"))
    reason = field(field(response, "incomplete_details"), "reason")
    stop = ("max_tokens" if reason == "max_output_tokens" else reason or "unknown") if status == "incomplete" else ("tool_use" if any(isinstance(b, ToolCallContent) for b in blocks) else "end_turn")
    target = resolve_target(config)
    return LLMResult(message=AssistantMessage(content=blocks, stop_reason=stop, raw_stop_reason=reason or status,
        provider=target.profile, api=target.api, model=field(response, "model") or target.model,
        response_id=field(response, "id"), usage=usage.model_dump()), usage=usage, diagnostics=diagnostics or {})
