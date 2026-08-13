"""
llm.openai_provider - OpenAI and OpenAI-compatible chat adapter.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from llm.base import (
    BaseLLMProvider,
    LLMStreamDecodeError,
    _attempt_reason_summary,
)
from llm.cache_control import (
    _build_cache_diagnostics,
    _emit_cache_log,
    _is_cache_control_rejection,
    _resolve_cache_control_decision,
    _with_cache_control_diagnostics,
)
from llm.thinking import (
    openai_thinking_request,
    resolve_thinking_intent,
    thinking_block_from_reasoning,
)
from runtime_config import ModelConfig


def _merge_stream_identity(current: str, incoming: Any) -> str:
    """Merge an id/name delta without duplicating full-value retransmissions."""
    value = str(incoming or "")
    if not value:
        return current
    if not current:
        return value
    if value == current:
        return current
    if value.startswith(current):
        return value
    if current.startswith(value):
        return current
    return current + value


def _is_stream_options_rejection(exc: Exception) -> bool:
    """Recognize compat gateways that reject stream_options/include_usage."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    text = str(exc).lower()
    explicitly_named = "stream_options" in text or "include_usage" in text
    rejection_word = any(
        word in text
        for word in ("unknown", "unsupported", "unrecognized", "invalid", "not allowed")
    )
    return explicitly_named and rejection_word and (status is None or status in {400, 404, 422})


def _degenerate_response_problem(response: Any) -> Optional[Dict[str, Any]]:
    """Detect a structurally degenerate chat.completions response, else None.

    Only structural gateway failures count: no choices, a None message, or a
    missing usage meter. A well-formed response whose message happens to have
    empty content but carries real usage is the model's business, not ours —
    the agent-level streak guard owns that case.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return {"provider": "openai", "reason": "no_choices"}
    message = getattr(choices[0], "message", None)
    if message is None:
        return {
            "provider": "openai",
            "reason": "no_message",
            "finish_reason": getattr(choices[0], "finish_reason", None),
        }
    if getattr(response, "usage", None) is None:
        # Payload wins (same policy as the Anthropic detector): a message
        # carrying real content, tool_calls, or reasoning_content (thinking
        # mode emits the chain here, sibling to content) is the
        # model's answer even when the gateway omitted the usage meter -
        # retrying it would throw away a good response. Only "no usage AND
        # no payload" is degenerate.
        has_payload = bool(
            str(getattr(message, "content", "") or "").strip()
        ) or bool(getattr(message, "tool_calls", None)) or bool(
            str(getattr(message, "reasoning_content", "") or "").strip()
        )
        if not has_payload:
            return {
                "provider": "openai",
                "reason": "usage_missing",
                "finish_reason": getattr(choices[0], "finish_reason", None),
            }
    return None


def _validate_tool_argument_json(response: Any) -> None:
    """Reject broken provider tool JSON before it can reach browser dispatch."""
    for choice in (getattr(response, "choices", None) or []):
        message = getattr(choice, "message", None)
        for tool_call in (getattr(message, "tool_calls", None) or []):
            function = getattr(tool_call, "function", None)
            raw = str(getattr(function, "arguments", "") or "")
            try:
                parsed = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise LLMStreamDecodeError(
                    "OpenAI-compatible response contained malformed tool JSON",
                    raw_arguments=raw,
                ) from exc
            if not isinstance(parsed, dict):
                raise LLMStreamDecodeError(
                    "OpenAI-compatible tool arguments must decode to an object",
                    raw_arguments=raw,
                )


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, config: ModelConfig):
        super().__init__(config)

        if AsyncOpenAI is None:
            raise ImportError(
                "[LLM Gateway] 缺少 openai SDK，请先安装: pip install openai"
            )

        # api_key / base_url 已由 ModelConfig 统一从环境变量解析
        api_key = self.config.api_key or os.getenv("OPENAI_API_KEY")
        base_url = self.config.base_url or os.getenv("OPENAI_BASE_URL")

        if not api_key:
            raise ValueError(
                "[LLM Gateway] OpenAI API 秘钥缺失！\n"
                "  方式 1: 在 config.json 中设置 api_key_env 指向你的环境变量名\n"
                "  方式 2: 直接设置系统环境变量 OPENAI_API_KEY"
            )

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def _convert_anthropic_tools_to_openai(
        self,
        tools: List[Dict],
        strict_tools: bool = False,
    ) -> List[Dict]:
        """
        将 Anthropic 格式的 tools 转换为 OpenAI Function Calling 格式。
        
        Anthropic 格式:
        {
            "name": "get_weather",
            "description": "Get weather info",
            "input_schema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }
        
        OpenAI 格式:
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather info",
                "parameters": {
                    "type": "object",
                    "properties": {...},
                    "required": [...]
                }
            }
        }
        """
        openai_tools = []
        for tool in tools:
            function = {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {
                    "type": "object",
                    "properties": {}
                })
            }
            if strict_tools or tool.get("strict"):
                function["strict"] = True
            openai_tools.append({
                "type": "function",
                "function": function,
            })
        return openai_tools

    def _convert_anthropic_messages_to_openai(self, messages: List[Dict]) -> List[Dict]:
        """
        将 Anthropic 格式的消息转换为 OpenAI 格式。
        
        主要处理 tool_result 类型的消息，Anthropic 使用 content 数组，
        OpenAI 使用 role="tool" + tool_call_id + content 字符串。
        """
        openai_messages = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            
            # 处理普通文本消息
            if isinstance(content, str):
                openai_messages.append({
                    "role": role,
                    "content": content
                })
            # 处理包含块结构的 content 数组
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []
                reasoning_parts = []
                encrypted_parts = []
                has_user_content = False
                
                for block in content:
                    if isinstance(block, dict):
                        b_type = block.get("type")
                        if b_type == "text":
                            text_parts.append(block.get("text", ""))
                            has_user_content = True
                        elif b_type == "tool_use":
                            tool_calls.append({
                                "id": block.get("id"),
                                "type": "function",
                                "function": {
                                    "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input", {}))
                                }
                            })
                        elif b_type == "thinking":
                            # 思考内容统一用 thinking 块承载，这里转回 OpenAI 格式
                            # 的同级字段。OpenAI SDK 会把消息里的未知字段原样透传
                            # 到请求体（已实测）。
                            #   - DeepSeek：工具调用轮次必须回传 reasoning_content，
                            #     否则 400。
                            #   - 方舟：不回传不报错，但 1.8 及之后的模型回传后思维链
                            #     可参与后续推理；encrypted_content 优先级高于
                            #     reasoning_content，两者同时回传时前者生效。
                            reasoning = block.get("thinking", "")
                            if reasoning:
                                reasoning_parts.append(str(reasoning))
                            encrypted = block.get("encrypted_content", "")
                            if encrypted:
                                encrypted_parts.append(str(encrypted))
                        elif b_type == "redacted_thinking":
                            # OpenAI/DeepSeek 格式无等价字段；丢弃以避免泄露
                            # 被模型显式遮蔽的内容。
                            pass
                        elif b_type == "tool_result":
                            # Anthropic 的 tool_result 转为 OpenAI 的 tool 消息
                            raw_content = block.get("content", "")
                            if isinstance(raw_content, list):
                                # 提取文本块，其他类型退化为 JSON 序列化
                                parts = []
                                for c in raw_content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        parts.append(c.get("text", ""))
                                    else:
                                        parts.append(json.dumps(c, ensure_ascii=False))
                                content_str = "\n".join(parts)
                            elif isinstance(raw_content, str):
                                content_str = raw_content
                            else:
                                content_str = json.dumps(raw_content, ensure_ascii=False)
                                
                            openai_messages.append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id"),
                                "content": content_str
                            })
                
                # 根据原本的 role 重构消息。思维链只是回答的附属物：没有正文也
                # 没有工具调用时不单独成一条消息，否则会造出缺 content 的
                # assistant 消息，多数 OpenAI 兼容服务端会拒。
                if role == "assistant" and (text_parts or tool_calls):
                    msg_dict: Dict[str, Any] = {"role": "assistant"}
                    if text_parts:
                        msg_dict["content"] = "\n".join(text_parts)
                    if reasoning_parts:
                        msg_dict["reasoning_content"] = "\n".join(reasoning_parts)
                    if encrypted_parts:
                        msg_dict["encrypted_content"] = "\n".join(encrypted_parts)
                    if tool_calls:
                        msg_dict["tool_calls"] = tool_calls
                    openai_messages.append(msg_dict)
                elif role == "user" and has_user_content:
                    openai_messages.append(
                        {"role": "user", "content": "\n".join(text_parts)}
                    )
            else:
                # 其他情况直接传递
                openai_messages.append(msg)
        
        return openai_messages

    async def generate_response(
        self,
        system_prompt: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> Tuple[str, List[Dict], str, Dict[str, Any]]:
        """调用 OpenAI / OpenAI-compat API 生成响应。

        关于 prompt cache(以阿里云百炼/Qwen 为目标平台):
          - 百炼支持显式 cache_control: {"type":"ephemeral"},格式与 Anthropic 同
          - cache_control 只能放在 messages 的 content 里(包括 role=system)
          - tools 不能独立缓存(自动并入 system 的缓存范围)— 所以下面不在 tools 上加 marker
          - 显式缓存最小 1024 token, TTL 5 分钟(命中后重置)
          - 命中数从 response.usage.prompt_tokens_details.cached_tokens 读

        :param system_prompt: 系统提示词
        :param messages: Anthropic 格式的消息列表
        :param tools: Anthropic 格式的工具列表
        :return: (文本回复, 工具调用列表, stop_reason, usage)
        """
        cache_decision = _resolve_cache_control_decision("openai", self.config)
        strict_tools = bool(self.config.extra_params.get("strict_tools", False))
        thinking_intent = resolve_thinking_intent(self.config.extra_params)
        thinking_top, thinking_extra_body, thinking_warnings = (
            openai_thinking_request(thinking_intent)
        )
        for _warning in (*thinking_intent.warnings, *thinking_warnings):
            _emit_cache_log(f"[OpenAI Thinking] {_warning}")

        def build_request_params(cache_enabled: bool) -> Dict[str, Any]:
            # 转换消息格式
            openai_messages = self._convert_anthropic_messages_to_openai(messages)

            # 在最后一条消息上挂缓存 marker(滚动缓存对话历史)。
            # 该显式 cache_control 是部分 OpenAI-compatible 平台扩展能力；
            # 标准 OpenAI/其它兼容服务默认关闭，避免因为未知字段被拒绝。
            if cache_enabled and openai_messages:
                last_msg = dict(openai_messages[-1])
                if last_msg.get("role") in ("user", "tool", "assistant"):
                    last_content = last_msg.get("content")
                    if isinstance(last_content, str) and last_content:
                        last_msg["content"] = [
                            {
                                "type": "text",
                                "text": last_content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ]
                    elif isinstance(last_content, list) and last_content:
                        last_blocks = list(last_content)
                        last_block = last_blocks[-1]
                        if isinstance(last_block, dict):
                            last_blocks[-1] = {
                                **last_block,
                                "cache_control": {"type": "ephemeral"},
                            }
                            last_msg["content"] = last_blocks
                    openai_messages[-1] = last_msg

            # 将 system_prompt 插入到消息列表开头
            if cache_enabled:
                system_content: Any = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                system_content = system_prompt

            openai_messages.insert(0, {
                "role": "system",
                "content": system_content,
            })

            # 转换工具格式
            openai_tools = (
                self._convert_anthropic_tools_to_openai(
                    tools,
                    strict_tools=strict_tools,
                )
                if tools else []
            )

            # 构建请求参数
            params = {
                "model": self.config.model_id,
                "messages": openai_messages,
                "max_tokens": self.config.extra_params.get("max_tokens", 4096),
                "temperature": self.config.extra_params.get("temperature", 1.0),
            }

            reserved_extra_keys = {
                "cache_control_mode",
                "enable_cache_control",
                "strict_tools",
                "max_tokens",
                "temperature",
                "tool_choice",
                "llm_api_timeout_seconds",
                "llm_timeout_max_retries",
                "llm_timeout_backoff_seconds",
                "llm_timeout_retry_interval_seconds",
                # Providers always stream internally.  Keeping this reserved
                # prevents a user-supplied stream=False from conflicting with
                # the transport contract below.
                "stream",
                # Thinking/reasoning controls are translated by llm.thinking
                # into the right wire shape (reasoning_effort top-level +
                # thinking via extra_body); reserving them here avoids a raw
                # pass-through that the OpenAI SDK would reject (`thinking`
                # is not a chat.completions kwarg).
                "thinking",
                "reasoning_effort",
                "effort",
            }
            for key, value in self.config.extra_params.items():
                if key not in reserved_extra_keys:
                    params[key] = value

            # 思考模式参数：reasoning_effort 是原生 kwarg（顶级），
            # thinking 必须走 extra_body（OpenAI SDK 不接受其为 kwarg）。
            params.update(thinking_top)
            if thinking_extra_body:
                existing_extra_body = params.get("extra_body")
                if isinstance(existing_extra_body, dict):
                    merged = dict(existing_extra_body)
                    merged.update(thinking_extra_body)
                    params["extra_body"] = merged
                else:
                    params["extra_body"] = dict(thinking_extra_body)

            # 只有在有工具时才添加 tools 参数
            if openai_tools:
                params["tools"] = openai_tools
                params["tool_choice"] = self.config.extra_params.get(
                    "tool_choice",
                    "auto",
                )
            return params

        effective_cache_enabled = (
            cache_decision.enabled
            and not self._cache_control_disabled_after_reject
        )
        prior_reject_fallback = (
            "disabled_after_prior_reject"
            if cache_decision.enabled and self._cache_control_disabled_after_reject
            else None
        )
        request_params = build_request_params(effective_cache_enabled)
        cache_diagnostics = _with_cache_control_diagnostics(
            _build_cache_diagnostics("openai", request_params),
            cache_decision,
            actual_enabled=effective_cache_enabled,
            accepted=True if effective_cache_enabled
            else False if prior_reject_fallback else None,
            fallback=prior_reject_fallback,
        )

        timeout_attempts: List[Dict[str, Any]] = []

        async def collect_streamed_completion(request_params: Dict[str, Any]):
            """Consume Chat Completions chunks into the legacy final shape.

            The OpenAI SDK's high-level chat.completions.stream() accumulator
            rejects non-strict function tools.  This project intentionally
            supports both strict and non-strict OpenAI-compatible gateways, so
            aggregate the lower-level create(stream=True) chunks here instead.
            """
            stream_params = dict(request_params)
            stream_params["stream"] = True
            # Standard OpenAI streaming only includes token usage when this is
            # requested. Compatible gateways may omit it; payload responses are
            # still accepted by the existing degenerate-response policy.
            include_stream_options = not bool(
                getattr(self, "_stream_options_disabled_after_reject", False)
            )
            if include_stream_options:
                stream_params.setdefault("stream_options", {"include_usage": True})

            try:
                stream = await asyncio.wait_for(
                    self.client.chat.completions.create(**stream_params),
                    timeout=self._llm_timeout_seconds(),
                )
            except Exception as exc:
                if not include_stream_options or not _is_stream_options_rejection(exc):
                    raise
                stream_params.pop("stream_options", None)
                _emit_cache_log(
                    "[OpenAI] stream_options rejected; retrying without include_usage"
                )
                stream = await asyncio.wait_for(
                    self.client.chat.completions.create(**stream_params),
                    timeout=self._llm_timeout_seconds(),
                )
                self._stream_options_disabled_after_reject = True
            content_parts: List[str] = []
            reasoning_parts: List[str] = []
            encrypted_parts: List[str] = []
            tool_call_parts: Dict[int, Dict[str, str]] = {}
            finish_reason = None
            usage = None
            saw_primary_choice = False

            async with stream:
                async for chunk in self._iterate_with_llm_idle_timeout(stream):
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = chunk_usage

                    for choice in (getattr(chunk, "choices", None) or []):
                        if int(getattr(choice, "index", 0) or 0) != 0:
                            continue
                        saw_primary_choice = True
                        choice_finish_reason = getattr(choice, "finish_reason", None)
                        if choice_finish_reason is not None:
                            finish_reason = choice_finish_reason

                        delta = getattr(choice, "delta", None)
                        if delta is None:
                            continue
                        content = getattr(delta, "content", None)
                        if content:
                            content_parts.append(content)
                        # 思考模式把思维链放在 delta.reasoning_content（与 content
                        # 同级），必须捕获以便工具调用轮次回传。方舟摘要类模型另有
                        # encrypted_content：流式下会在思维链输出完成、正文开始前
                        # 单独发一包，回传时它的优先级高于 reasoning_content。
                        reasoning = getattr(delta, "reasoning_content", None)
                        if reasoning:
                            reasoning_parts.append(reasoning)
                        encrypted = getattr(delta, "encrypted_content", None)
                        if encrypted:
                            encrypted_parts.append(encrypted)

                        for tool_delta in (getattr(delta, "tool_calls", None) or []):
                            index = int(getattr(tool_delta, "index", 0) or 0)
                            parts = tool_call_parts.setdefault(
                                index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            tool_id = getattr(tool_delta, "id", None)
                            if tool_id:
                                parts["id"] = _merge_stream_identity(parts["id"], tool_id)
                            function = getattr(tool_delta, "function", None)
                            if function is not None:
                                name = getattr(function, "name", None)
                                arguments = getattr(function, "arguments", None)
                                if name:
                                    parts["name"] = _merge_stream_identity(parts["name"], name)
                                if arguments:
                                    parts["arguments"] += arguments

            choices = []
            if saw_primary_choice:
                assembled_tool_calls = [
                    SimpleNamespace(
                        id=parts["id"],
                        type="function",
                        function=SimpleNamespace(
                            name=parts["name"],
                            arguments=parts["arguments"],
                        ),
                    )
                    for _, parts in sorted(tool_call_parts.items())
                ]
                choices.append(
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="".join(content_parts) or None,
                            reasoning_content="".join(reasoning_parts) or None,
                            encrypted_content="".join(encrypted_parts) or None,
                            tool_calls=assembled_tool_calls or None,
                        ),
                        finish_reason=finish_reason,
                    )
                )
            response = SimpleNamespace(choices=choices, usage=usage)
            _validate_tool_argument_json(response)
            return response

        async def request_with_timeout(request_params: Dict[str, Any]):
            async def collect_nonstream_completion():
                nonstream_params = dict(request_params)
                nonstream_params["stream"] = False
                result = await self.client.chat.completions.create(
                    **nonstream_params
                )
                _validate_tool_argument_json(result)
                return result

            response, attempts = await self._request_with_timeout_retries(
                lambda: collect_streamed_completion(request_params),
                provider="openai",
                operation="chat.completions.stream",
                response_validator=_degenerate_response_problem,
                timeout_managed=True,
                reserved_nonstream_fallback_factory=collect_nonstream_completion,
                fallback_response_validator=_degenerate_response_problem,
            )
            if attempts:
                timeout_attempts.extend(attempts)
                _emit_cache_log(
                    "[OpenAI] chat.completions.stream recovered after "
                    f"{len(attempts)} failed attempt(s) "
                    f"({_attempt_reason_summary(attempts)})"
                )
            return response

        # 调用 OpenAI API（带超时保护）
        try:
            response = await request_with_timeout(request_params)
        except Exception as exc:
            if not effective_cache_enabled or not _is_cache_control_rejection(exc):
                raise
            fallback_params = build_request_params(False)
            cache_diagnostics = _with_cache_control_diagnostics(
                _build_cache_diagnostics("openai", fallback_params),
                cache_decision,
                actual_enabled=False,
                accepted=False,
                fallback="disabled_after_provider_reject",
                reject_error=exc,
            )
            _emit_cache_log(
                "[OpenAI Cache] cache_control rejected; retrying without markers"
            )
            response = await request_with_timeout(fallback_params)
            self._cache_control_disabled_after_reject = True

        # 缓存命中观测(OpenAI 自动缓存,只能从 prompt_tokens_details.cached_tokens 反查)
        # usage 可能为 None(带 payload 的响应缺 usage 时检测器放行)——全部走
        # getattr 默认值,不能直接点属性。
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        cache_read = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        # OpenAI 不区分 creation vs read,首次调用就直接计入 prompt_tokens
        # 这里把"未走缓存的 input"算成 prompt - cached
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        uncached_input = max(prompt_tokens - cache_read, 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cache_meta = cache_diagnostics.get("cache_control", {})
        _emit_cache_log(
            f"[OpenAI Cache] prompt={prompt_tokens} read={cache_read} "
            f"uncached={uncached_input} out={output_tokens} "
            f"mode={cache_meta.get('mode')} enabled={cache_meta.get('enabled')} "
            f"markers={cache_diagnostics.get('marker_count')}"
        )

        # 解析响应
        message = response.choices[0].message
        response_text = message.content or ""
        reasoning_content = str(getattr(message, "reasoning_content", "") or "")
        encrypted_content = str(getattr(message, "encrypted_content", "") or "")
        tool_calls = []
        
        # 解析工具调用（转换回 Anthropic 格式）
        if message.tool_calls:
            for tc in message.tool_calls:
                # Whole-response validation above guarantees that malformed
                # transport JSON can never masquerade as model-authored input.
                parsed_input = (
                    json.loads(tc.function.arguments)
                    if tc.function.arguments
                    else {}
                )
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": parsed_input,
                })
        
        # 映射 finish_reason 到 Anthropic 的 stop_reason
        # finish_reason 可能为 None(流式中途/异常),兜底为 "stop" 保证返回类型为 str
        finish_reason = response.choices[0].finish_reason or "stop"
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "end_turn",
        }
        stop_reason = stop_reason_map.get(finish_reason, finish_reason)

        # 思维链通过 reasoning_content（+ 方舟摘要类模型的 encrypted_content）
        # 返回。统一包装成 thinking 块存入 _assistant_prefix_blocks，与 Anthropic
        # provider 一致，由 harness 在下一轮原样回传：DeepSeek 的工具调用轮次不
        # 回传会报 400，方舟不报错但回传后思维链可参与后续推理。
        assistant_prefix_blocks: List[Dict[str, Any]] = []
        reasoning_block = thinking_block_from_reasoning(
            reasoning_content,
            encrypted_content,
        )
        if reasoning_block is not None:
            assistant_prefix_blocks.append(reasoning_block)

        return response_text, tool_calls, stop_reason, {
            "cache_read": cache_read,
            "cache_creation": 0,        # OpenAI 不区分,首次请求的写入算 uncached
            "uncached_input": uncached_input,
            "output": output_tokens,
            "cache_diagnostics": cache_diagnostics,
            "timeout_retries": sum(
                1 for item in timeout_attempts
                if item.get("reason") == "timeout"
            ),
            "degenerate_retries": sum(
                1 for item in timeout_attempts
                if item.get("reason") == "degenerate_response"
            ),
            "connection_retries": sum(
                1 for item in timeout_attempts
                if item.get("reason") == "connection_error"
            ),
            "stream_decode_retries": sum(
                1 for item in timeout_attempts
                if item.get("reason") == "stream_decode_error"
            ),
            "nonstream_fallback_used": any(
                item.get("reason") == "nonstream_fallback"
                for item in timeout_attempts
            ),
            "timeout_attempts": timeout_attempts,
            "timeout_seconds": self._llm_timeout_seconds(),
            "timeout_max_retries": self._llm_timeout_max_retries(),
            "timeout_retry_interval_seconds": self._llm_timeout_retry_interval_seconds(),
            "_assistant_prefix_blocks": assistant_prefix_blocks,
        }
