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

from runtime_config import ModelConfig
from llm.adapters import encode_request, decode_response
from llm.contracts import LLMRequest, LLMResult
from llm.profiles import resolve_target


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


def _is_thinking_tool_choice_rejection(exc: Exception) -> bool:
    """Recognize gateways that cannot force a tool while thinking is enabled."""

    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    text = str(exc).lower()
    return (
        "tool_choice" in text
        and "thinking" in text
        and any(word in text for word in ("support", "invalid", "allow"))
        and (status is None or status in {400, 422})
    )


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
        from dataclasses import replace
        super().__init__(replace(config, base_url=resolve_target(config).base_url))

        if AsyncOpenAI is None:
            raise ImportError(
                "[LLM Gateway] 缺少 openai SDK，请先安装: pip install openai"
            )

        # api_key / base_url 已由 ModelConfig 统一从环境变量解析
        api_key = self.config.api_key or (
            os.getenv("OPENAI_API_KEY") if self.config.provider == "openai" else None
        )
        base_url = self.config.base_url or os.getenv("OPENAI_BASE_URL")

        if not api_key:
            raise ValueError(
                "[LLM Gateway] OpenAI API 秘钥缺失！\n"
                "  方式 1: 在 config.json 中设置 api_key_env 指向你的环境变量名\n"
                "  方式 2: 直接设置系统环境变量 OPENAI_API_KEY"
            )

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    def _convert_anthropic_tools_to_openai(self, tools, strict_tools=False):
        """Legacy helper retained for extensions; the codec owns conversion."""
        from dataclasses import replace
        from llm.legacy import request_from_legacy
        config = replace(self.config, extra_params={"strict_tools": strict_tools})
        return encode_request(request_from_legacy("", [], tools), config).get("tools", [])

    def _convert_anthropic_messages_to_openai(self, messages):
        from llm.adapters import encode_messages
        from llm.legacy import project_messages
        from llm.profiles import OPENAI
        return encode_messages(project_messages(messages), OPENAI)

    async def generate(self, request: LLMRequest) -> LLMResult:
        cache_decision = _resolve_cache_control_decision("openai", self.config)

        def build_request_params(cache_enabled: bool) -> Dict[str, Any]:
            return encode_request(
                request, self.config, cache_enabled=cache_enabled,
                thinking_disabled=getattr(self, "_thinking_disabled_after_tool_choice_reject", False),
            )

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
            response_id = None
            response_model = None

            async with stream:
                async for chunk in self._iterate_with_llm_idle_timeout(stream):
                    response_id = getattr(chunk, "id", None) or response_id
                    response_model = getattr(chunk, "model", None) or response_model
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
            response = SimpleNamespace(choices=choices, usage=usage, id=response_id, model=response_model)
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
                provider=resolve_target(self.config).profile,
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
            if _is_thinking_tool_choice_rejection(exc):
                self._thinking_disabled_after_tool_choice_reject = True
                fallback_params = build_request_params(effective_cache_enabled)
                _emit_cache_log(
                    "[OpenAI Thinking] tool_choice rejected in thinking mode;"
                    " retrying once with thinking disabled"
                )
                response = await request_with_timeout(fallback_params)
            elif effective_cache_enabled and _is_cache_control_rejection(exc):
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
            else:
                raise

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

        diagnostics = {
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
        }

        return decode_response(response, self.config, diagnostics)
