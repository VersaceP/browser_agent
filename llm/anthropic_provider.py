"""
llm.anthropic_provider - Anthropic messages API adapter.
"""

import asyncio
import os
import traceback
from typing import Any, Dict, List, Optional, Tuple

try:
    from anthropic import AsyncAnthropic
except ImportError:
    AsyncAnthropic = None

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


def _degenerate_response_problem(response: Any) -> Optional[Dict[str, Any]]:
    """Detect a gateway-degenerate messages.create response, else None.

    Degenerate = NO payload block (no tool_use, no non-empty text, no
    thinking) AND anomalous usage (missing, or output_tokens==0). A
    well-formed "model chose to say nothing" keeps a real usage meter; a
    truncated/aborted gateway response does not. Responses with payload are
    never flagged here even when usage is missing — content wins, and the
    getattr defaults below keep parsing alive.
    """
    content = getattr(response, "content", None) or []
    has_payload = False
    for block in content:
        block_type = getattr(block, "type", "")
        if block_type == "tool_use":
            has_payload = True
            break
        if block_type == "text" and (getattr(block, "text", "") or "").strip():
            has_payload = True
            break
        if block_type in ("thinking", "redacted_thinking"):
            has_payload = True
            break
    if has_payload:
        return None
    usage = getattr(response, "usage", None)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    if usage is not None and output_tokens > 0:
        return None
    return {
        "provider": "anthropic",
        "content_blocks": len(content),
        "usage_missing": usage is None,
        "output_tokens": output_tokens,
        "stop_reason": getattr(response, "stop_reason", None),
    }


def _is_anthropic_stream_tool_decode_error(exc: BaseException) -> bool:
    """Identify the SDK's incremental tool-input JSON decoder failure.

    Do not turn arbitrary ValueError exceptions into retryable protocol
    incidents.  The observed failure originates in Anthropic's streaming
    message accumulator while parsing a partial tool input.
    """
    if not isinstance(exc, ValueError):
        return False
    frames = traceback.extract_tb(exc.__traceback__)
    sdk_stream_frame = any(
        "anthropic" in str(frame.filename).lower()
        and "stream" in str(frame.filename).lower()
        for frame in frames
    )
    message = str(exc).lower()
    json_shape = any(
        marker in message
        for marker in ("json", "expected value", "decode", "column", "partial")
    )
    return sdk_stream_frame and json_shape


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, config: ModelConfig):
        from dataclasses import replace
        super().__init__(replace(config, base_url=resolve_target(config).base_url))

        if AsyncAnthropic is None:
            raise ImportError(
                "[LLM Gateway] 缺少 anthropic SDK，请先安装: pip install anthropic"
            )

        # api_key / base_url 已由 ModelConfig 统一从环境变量解析
        # 这里仅做最终的空值兜底（直接构造 ModelConfig 而未经 config.json 段解析的场景）
        api_key = self.config.api_key or (
            os.getenv("ANTHROPIC_AUTH_TOKEN") if self.config.provider == "anthropic" else None
        )
        base_url = self.config.base_url or os.getenv("ANTHROPIC_BASE_URL")

        if not api_key:
            raise ValueError(
                "[LLM Gateway] Anthropic API 秘钥缺失！\n"
                "  方式 1: 在 config.json 中设置 api_key_env 指向你的环境变量名\n"
                "  方式 2: 直接设置系统环境变量 ANTHROPIC_AUTH_TOKEN"
            )

        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)

    async def generate(self, request: LLMRequest) -> LLMResult:
        cache_decision = _resolve_cache_control_decision("anthropic", self.config)

        def build_kwargs(cache_enabled: bool) -> Dict[str, Any]:
            return encode_request(request, self.config, cache_enabled=cache_enabled)

        effective_cache_enabled = (
            cache_decision.enabled
            and not self._cache_control_disabled_after_reject
        )
        prior_reject_fallback = (
            "disabled_after_prior_reject"
            if cache_decision.enabled and self._cache_control_disabled_after_reject
            else None
        )
        kwargs = build_kwargs(effective_cache_enabled)
        cache_diagnostics = _with_cache_control_diagnostics(
            _build_cache_diagnostics("anthropic", kwargs, max_markers=4),
            cache_decision,
            actual_enabled=effective_cache_enabled,
            accepted=True if effective_cache_enabled
            else False if prior_reject_fallback else None,
            fallback=prior_reject_fallback,
        )
        timeout_attempts: List[Dict[str, Any]] = []

        async def collect_streamed_message(request_kwargs: Dict[str, Any]):
            """Consume the SSE stream and return the SDK's final Message.

            Streaming stays an implementation detail of the provider: callers
            continue to receive the same fully-assembled response shape.  Using
            messages.stream() also avoids the Anthropic SDK's non-streaming
            ten-minute guard for large max_tokens values.
            """
            manager = self.client.messages.stream(**request_kwargs)
            stream = await asyncio.wait_for(
                manager.__aenter__(),
                timeout=self._llm_timeout_seconds(),
            )
            active_error: Optional[BaseException] = None
            try:
                if hasattr(stream, "__aiter__"):
                    async for _event in self._iterate_with_llm_idle_timeout(stream):
                        pass
                return await asyncio.wait_for(
                    stream.get_final_message(),
                    timeout=self._llm_timeout_seconds(),
                )
            except BaseException as exc:
                if _is_anthropic_stream_tool_decode_error(exc):
                    wrapped = LLMStreamDecodeError(
                        "Anthropic stream contained malformed incremental tool JSON"
                    )
                    active_error = wrapped
                    raise wrapped from exc
                active_error = exc
                raise
            finally:
                exit_args = (
                    (type(active_error), active_error, active_error.__traceback__)
                    if active_error is not None
                    else (None, None, None)
                )
                try:
                    await asyncio.wait_for(
                        manager.__aexit__(*exit_args),
                        timeout=self._llm_timeout_seconds(),
                    )
                except Exception:
                    if active_error is None:
                        raise

        async def request_with_timeout(request_kwargs: Dict[str, Any]):
            async def collect_nonstream_message():
                return await self.client.messages.create(**request_kwargs)

            response, attempts = await self._request_with_timeout_retries(
                lambda: collect_streamed_message(request_kwargs),
                provider=resolve_target(self.config).profile,
                operation="messages.stream",
                response_validator=_degenerate_response_problem,
                timeout_managed=True,
                reserved_nonstream_fallback_factory=collect_nonstream_message,
                fallback_response_validator=_degenerate_response_problem,
            )
            if attempts:
                timeout_attempts.extend(attempts)
                _emit_cache_log(
                    "[Anthropic] messages.stream recovered after "
                    f"{len(attempts)} failed attempt(s) "
                    f"({_attempt_reason_summary(attempts)})"
                )
            return response

        try:
            response = await request_with_timeout(kwargs)
        except Exception as exc:
            if not effective_cache_enabled or not _is_cache_control_rejection(exc):
                raise
            fallback_kwargs = build_kwargs(False)
            cache_diagnostics = _with_cache_control_diagnostics(
                _build_cache_diagnostics("anthropic", fallback_kwargs, max_markers=4),
                cache_decision,
                actual_enabled=False,
                accepted=False,
                fallback="disabled_after_provider_reject",
                reject_error=exc,
            )
            _emit_cache_log(
                "[Anthropic Cache] cache_control rejected; retrying without markers"
            )
            response = await request_with_timeout(fallback_kwargs)
            self._cache_control_disabled_after_reject = True

        # 缓存命中观测(设置环境变量 LLM_CACHE_DEBUG=1 打开)
        # usage 可能为 None(部分网关的截断/异常响应不带 usage)——全部走
        # getattr 默认值,不能直接点属性,否则 AttributeError 直接打死 worker。
        usage = getattr(response, "usage", None)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        uncached_input = int(getattr(usage, "input_tokens", 0) or 0)  # 已扣除 cache 部分
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_meta = cache_diagnostics.get("cache_control", {})
        _emit_cache_log(
            f"[Anthropic Cache] new={uncached_input} "
            f"create={cache_creation} read={cache_read} out={output_tokens} "
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
