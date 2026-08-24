"""
llm.base - Shared LLM provider interface.
"""

import asyncio
import importlib
import re
import time
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

from runtime_config import ModelConfig


def _connection_error_types() -> Tuple[type, ...]:
    """Collect the transport-failure types worth retrying, if importable.

    httpx is a hard dependency of both SDKs but the SDKs themselves are
    optional here, so every import stays guarded exactly like the providers do.
    """
    types: List[type] = [ConnectionError]
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx missing means no HTTP provider
        pass
    else:
        types.extend((
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ProtocolError,
            httpx.ProxyError,
        ))
    for module_name in ("anthropic", "openai"):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        # APITimeoutError subclasses APIConnectionError in both SDKs.
        candidate = getattr(module, "APIConnectionError", None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types.append(candidate)
    return tuple(types)


_CONNECTION_ERROR_TYPES = _connection_error_types()


def _error_response_body(exc: BaseException) -> Any:
    body = getattr(exc, "body", None)
    if body is not None:
        return body
    response = getattr(exc, "response", None)
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            return json_method()
        except Exception:
            pass
    return None


def _nested_error_value(value: Any, *names: str) -> str:
    """Read a provider error field without depending on one SDK/body shape."""
    if not isinstance(value, dict):
        return ""
    lowered = {str(key).lower(): item for key, item in value.items()}
    for name in names:
        item = lowered.get(name.lower())
        if item is not None and not isinstance(item, (dict, list)):
            rendered = str(item).strip()
            if rendered:
                return rendered
    nested = lowered.get("error")
    if isinstance(nested, dict):
        return _nested_error_value(nested, *names)
    return ""


def _response_header(exc: BaseException, name: str) -> str:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except Exception:
        return ""
    return str(value or "").strip()


def rate_limit_error_details(exc: BaseException) -> Optional[Dict[str, Any]]:
    """Return normalized metadata for an HTTP 429 provider failure.

    SDK exception classes are intentionally not used here. ABCP deployments can
    put OpenAI- or Anthropic-compatible gateways in front of several providers,
    but all of them retain the HTTP status and some combination of body/headers.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        return None
    if status != 429:
        return None

    body = _error_response_body(exc)
    provider_code = _nested_error_value(body, "code", "type")
    provider_message = _nested_error_value(body, "message", "detail")
    if not provider_message:
        provider_message = str(exc or "").strip() or "provider rate limit"
    request_id = (
        _nested_error_value(body, "request_id", "requestId")
        or _response_header(exc, "request-id")
        or _response_header(exc, "x-request-id")
    )

    marker_text = f"{provider_code} {provider_message}".lower()
    quota_markers = (
        "allocationquota",
        "allocation_quota",
        "insufficient_quota",
        "quota has been exhausted",
        "quota is exhausted",
        "quota exhausted",
        "billing hard limit",
    )
    kind = (
        "quota_exhausted"
        if any(marker in marker_text for marker in quota_markers)
        else "rate_limited"
    )

    retry_after_raw = _response_header(exc, "retry-after")
    retry_after_seconds: Optional[float] = None
    try:
        if retry_after_raw:
            retry_after_seconds = max(0.0, float(retry_after_raw))
    except (TypeError, ValueError):
        retry_after_seconds = None

    reset_at = (
        _response_header(exc, "x-ratelimit-reset")
        or _response_header(exc, "x-rate-limit-reset")
    )
    if not reset_at:
        match = re.search(
            r"\breset(?:s)?\s+at\s+"
            r"([0-9T:/+\- ]{5,32}(?:UTC|GMT|Z)?)",
            provider_message,
            flags=re.IGNORECASE,
        )
        if match:
            reset_at = match.group(1).strip(" .,;:'\"")

    return {
        "kind": kind,
        "statusCode": 429,
        "providerCode": provider_code,
        "providerMessage": provider_message,
        "requestId": request_id,
        # No automatic retry inside the current agent loop. A host may offer a
        # later retry when the provider supplied a reset/delay signal.
        "automaticRetry": False,
        "retryable": kind != "quota_exhausted" or bool(reset_at),
        "retryAfterSeconds": retry_after_seconds,
        "resetAt": reset_at,
    }


def connection_failure_reason(exc: BaseException) -> Optional[str]:
    """Return the exception type name when exc is a retryable transport failure."""
    if isinstance(exc, _CONNECTION_ERROR_TYPES):
        return type(exc).__name__
    return None


_RETRY_COUNTER_BY_REASON = {
    "timeout": "timeout_retries",
    "degenerate_response": "degenerate_retries",
    "connection_error": "connection_retries",
    "stream_decode_error": "stream_decode_retries",
}


def retry_usage_from_attempts(attempts: List[Dict[str, Any]]) -> Dict[str, int]:
    """Retry counters for a call that raised instead of returning a response.

    A call that never returns carries no usage dict, so without this the only
    retries that reach the aggregator are the ones that eventually succeeded —
    the metric would go quiet exactly when the transport is at its worst.

    ``attempts`` holds one entry per FAILED attempt. When the call eventually
    succeeds, every failure was followed by a retry, so the per-reason counts
    are the retries performed. When the budget is exhausted the final failure
    was NOT retried, so drop it — otherwise an exhausted call would report one
    more retry than actually happened.
    """
    counts = {value: 0 for value in _RETRY_COUNTER_BY_REASON.values()}
    for item in attempts:
        key = _RETRY_COUNTER_BY_REASON.get(str(item.get("reason") or ""))
        if key:
            counts[key] += 1
    if attempts:
        key = _RETRY_COUNTER_BY_REASON.get(str(attempts[-1].get("reason") or ""))
        if key and counts[key] > 0:
            counts[key] -= 1
    return counts


def _attempt_reason_summary(attempts: List[Dict[str, Any]]) -> str:
    """Render "timeout x1, connection_error x2" for recovery logs."""
    counts: Dict[str, int] = {}
    for item in attempts:
        reason = str(item.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return ", ".join(
        f"{reason} x{count}" for reason, count in sorted(counts.items())
    ) or "none"


class LLMRequestTimeoutError(asyncio.TimeoutError):
    """Raised when an LLM request exhausts its configured timeout retries."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        timeout_seconds: float,
        max_retries: int,
        attempts: List[Dict[str, Any]],
    ):
        self.provider = provider
        self.model = model
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.attempts = attempts
        super().__init__(
            f"{provider}.{operation} timed out after {len(attempts)} "
            f"attempt(s), timeout={timeout_seconds}s, max_retries={max_retries}"
        )


class LLMEmptyResponseError(Exception):
    """Raised when the provider keeps returning degenerate/empty responses.

    A degenerate response is an infrastructure incident (gateway truncation,
    aborted completion) that must never be normalized into a well-formed
    "model finished with nothing to say" success — task 9d5655d3's lead died
    silently exactly that way. Providers detect the degenerate shape via a
    response_validator, retry within the shared timeout-retry budget, and
    raise this once exhausted so the agent layer can decide.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        problem: Dict[str, Any],
        max_retries: int,
        attempts: List[Dict[str, Any]],
    ):
        self.provider = provider
        self.model = model
        self.operation = operation
        self.problem = problem
        self.max_retries = max_retries
        self.attempts = attempts
        super().__init__(
            f"{provider}.{operation} returned a degenerate/empty response "
            f"after {len(attempts)} attempt(s) "
            f"(problem={problem}, max_retries={max_retries})"
        )


class LLMConnectionError(Exception):
    """Raised when the transport keeps failing after the retry budget is spent.

    Streaming makes this failure class structurally invisible to the provider
    SDKs: their retry wrapper only covers the request that returns response
    *headers*, while the SSE body is consumed later, by us. A gateway that
    closes a chunked body mid-stream therefore escapes the SDK entirely — it
    killed a 17-step lead run with httpx.RemoteProtocolError — so the retry has
    to live at this layer. Re-sending the whole turn is safe: the aborted
    stream produced no usable completion and the request has no side effects.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        reason: str,
        max_retries: int,
        attempts: List[Dict[str, Any]],
    ):
        self.provider = provider
        self.model = model
        self.operation = operation
        self.reason = reason
        self.max_retries = max_retries
        self.attempts = attempts
        super().__init__(
            f"{provider}.{operation} lost the connection ({reason}) after "
            f"{len(attempts)} attempt(s), max_retries={max_retries}"
        )


class LLMRateLimitError(Exception):
    """Normalized provider throttling/quota failure.

    The current agent run must not blindly retry this exception. Provider SDKs
    already apply their own bounded 429 policy, and an allocation quota can be
    unavailable for days. Callers receive structured timing metadata so a host
    can offer an explicit retry/resume action without treating the incident as
    an internal harness crash.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        details: Dict[str, Any],
    ):
        self.provider = provider
        self.model = model
        self.operation = operation
        self.kind = str(details.get("kind") or "rate_limited")
        self.status_code = int(details.get("statusCode") or 429)
        self.provider_code = str(details.get("providerCode") or "")
        self.provider_message = str(details.get("providerMessage") or "")
        self.request_id = str(details.get("requestId") or "")
        self.automatic_retry = bool(details.get("automaticRetry", False))
        self.retryable = bool(details.get("retryable", True))
        self.retry_after_seconds = details.get("retryAfterSeconds")
        self.reset_at = str(details.get("resetAt") or "")
        super().__init__(
            f"{provider}.{operation} {self.kind} for model {model}"
            + (f" ({self.provider_code})" if self.provider_code else "")
            + (f"; reset_at={self.reset_at}" if self.reset_at else "")
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "statusCode": self.status_code,
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "providerCode": self.provider_code or None,
            "message": self.provider_message,
            "requestId": self.request_id or None,
            "automaticRetry": self.automatic_retry,
            "retryable": self.retryable,
            "retryAfterSeconds": self.retry_after_seconds,
            "resetAt": self.reset_at or None,
        }


class LLMStreamDecodeError(Exception):
    """A streamed tool-call payload could not be assembled as valid JSON.

    Providers must raise this before any tool call is returned to the agent.
    Treating broken transport JSON as model-authored tool input causes the
    worker to repeatedly "repair" a request that the model never produced.
    """

    def __init__(self, message: str, *, raw_arguments: str = ""):
        self.raw_arguments = raw_arguments
        super().__init__(message)


class LLMProviderProtocolError(Exception):
    """Streaming protocol recovery, including its reserved fallback, failed."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        attempts: List[Dict[str, Any]],
        fallback_attempted: bool,
        fallback_skipped_reason: str = "",
    ):
        self.provider = provider
        self.model = model
        self.operation = operation
        self.attempts = attempts
        self.fallback_attempted = fallback_attempted
        self.fallback_skipped_reason = fallback_skipped_reason
        super().__init__(
            f"{provider}.{operation} could not decode the provider tool JSON; "
            f"reserved_nonstream_fallback_attempted={fallback_attempted}"
            + (
                f", fallback_skipped_reason={fallback_skipped_reason}"
                if fallback_skipped_reason
                else ""
            )
        )


class BaseLLMProvider(ABC):
    """
    大模型路由抽象层。

    负责将 V4 系统统一的 Messages 和 Tools 格式，
    翻译成各个厂商的协议，最终保证外围系统获得统一格式的产物。
    """

    def __init__(self, config: ModelConfig):
        self.config = config
        self._cache_control_disabled_after_reject = False

    def _llm_timeout_seconds(self) -> float:
        try:
            return max(1.0, float(self.config.llm_api_timeout_seconds))
        except (TypeError, ValueError):
            return 180.0

    def _llm_timeout_max_retries(self) -> int:
        try:
            return max(0, min(10, int(self.config.llm_timeout_max_retries)))
        except (TypeError, ValueError):
            return 1

    def _llm_timeout_backoff_seconds(self) -> float:
        try:
            return max(0.0, float(self.config.llm_timeout_backoff_seconds))
        except (TypeError, ValueError):
            return 1.0

    def _llm_timeout_retry_interval_seconds(self) -> Optional[float]:
        value = getattr(self.config, "llm_timeout_retry_interval_seconds", None)
        if value is None or value == "":
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    async def _iterate_with_llm_idle_timeout(
        self,
        stream: Any,
    ) -> AsyncIterator[Any]:
        """Yield stream items while treating the configured timeout as idle time.

        Each received chunk resets the timer. This lets a healthy long response
        exceed the timeout in total wall time while still detecting a stalled
        handshake/connection between chunks.
        """
        iterator = stream.__aiter__()
        while True:
            try:
                item = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=self._llm_timeout_seconds(),
                )
            except StopAsyncIteration:
                return
            yield item

    async def _request_with_timeout_retries(
        self,
        request_factory: Callable[[], Any],
        *,
        provider: str,
        operation: str,
        response_validator: Optional[
            Callable[[Any], Optional[Dict[str, Any]]]
        ] = None,
        timeout_managed: bool = False,
        reserved_nonstream_fallback_factory: Optional[Callable[[], Any]] = None,
        fallback_response_validator: Optional[
            Callable[[Any], Optional[Dict[str, Any]]]
        ] = None,
    ) -> Tuple[Any, List[Dict[str, Any]]]:
        """Run the request with timeout retries; optionally validate the shape.

        response_validator inspects the RAW provider response and returns a
        problem dict when the response is degenerate (gateway truncation:
        missing usage / empty content). Degenerate responses consume the same
        retry budget as timeouts and raise LLMEmptyResponseError once
        exhausted — they must not surface as normal empty completions.

        Transport failures share that budget too and raise LLMConnectionError:
        under streaming the SDK's own connection retry cannot see them, so
        without this the first mid-stream disconnect kills the run.

        When ``timeout_managed`` is true, the request factory owns timeout
        boundaries (stream providers use handshake/per-chunk idle timeouts), so
        this retry layer must not impose a second total wall-clock deadline.

        The normal timeout/connection/decode attempts share ``max_retries + 1``
        slots.  If, and only if, the terminal normal failure is a stream decode
        error, one additional non-stream slot is reserved.  Timeouts and
        connection failures cannot consume that fourth slot: a persistently
        broken streaming gateway is exactly when non-stream is most valuable.
        """
        timeout_seconds = self._llm_timeout_seconds()
        max_retries = self._llm_timeout_max_retries()
        backoff_seconds = self._llm_timeout_backoff_seconds()
        retry_interval_seconds = self._llm_timeout_retry_interval_seconds()
        attempts: List[Dict[str, Any]] = []

        def _retry_delay(attempt_index: int) -> float:
            if retry_interval_seconds is None:
                return round(backoff_seconds * (2 ** attempt_index), 3)
            return round(retry_interval_seconds, 3)

        for attempt_index in range(max_retries + 1):
            attempt_number = attempt_index + 1
            started = time.monotonic()
            try:
                if timeout_managed:
                    response = await request_factory()
                else:
                    response = await asyncio.wait_for(
                        request_factory(),
                        timeout=timeout_seconds,
                    )
            except (asyncio.TimeoutError, TimeoutError) as exc:
                elapsed = round(time.monotonic() - started, 3)
                attempts.append({
                    "attempt": attempt_number,
                    "reason": "timeout",
                    "timeout_seconds": timeout_seconds,
                    "elapsed_seconds": elapsed,
                })
                if attempt_index >= max_retries:
                    raise LLMRequestTimeoutError(
                        provider=provider,
                        model=self.config.model_id,
                        operation=operation,
                        timeout_seconds=timeout_seconds,
                        max_retries=max_retries,
                        attempts=attempts,
                    ) from exc
                delay = _retry_delay(attempt_index)
                attempts[-1]["next_retry_delay_seconds"] = delay
                if delay > 0:
                    await asyncio.sleep(delay)
                continue
            except LLMStreamDecodeError as exc:
                elapsed = round(time.monotonic() - started, 3)
                attempts.append({
                    "attempt": attempt_number,
                    "reason": "stream_decode_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc) or type(exc).__name__,
                    "elapsed_seconds": elapsed,
                })
                if attempt_index < max_retries:
                    delay = _retry_delay(attempt_index)
                    attempts[-1]["next_retry_delay_seconds"] = delay
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                if reserved_nonstream_fallback_factory is None:
                    raise LLMProviderProtocolError(
                        provider=provider,
                        model=self.config.model_id,
                        operation=operation,
                        attempts=attempts,
                        fallback_attempted=False,
                        fallback_skipped_reason="fallback_factory_unavailable",
                    ) from exc

                fallback_started = time.monotonic()
                fallback_attempt = max_retries + 2
                try:
                    fallback_response = await asyncio.wait_for(
                        reserved_nonstream_fallback_factory(),
                        timeout=timeout_seconds,
                    )
                    fallback_problem = (
                        fallback_response_validator(fallback_response)
                        if fallback_response_validator
                        else None
                    )
                    if fallback_problem is not None:
                        raise LLMStreamDecodeError(
                            "non-stream fallback returned an invalid response: "
                            f"{fallback_problem}"
                        )
                except BaseException as fallback_exc:
                    if isinstance(fallback_exc, asyncio.CancelledError):
                        raise
                    rate_limit = rate_limit_error_details(fallback_exc)
                    if rate_limit is not None:
                        raise LLMRateLimitError(
                            provider=provider,
                            model=self.config.model_id,
                            operation=operation,
                            details=rate_limit,
                        ) from fallback_exc
                    attempts.append({
                        "attempt": fallback_attempt,
                        "reason": "nonstream_fallback_failed",
                        "error_type": type(fallback_exc).__name__,
                        "error": str(fallback_exc) or type(fallback_exc).__name__,
                        "elapsed_seconds": round(
                            time.monotonic() - fallback_started, 3
                        ),
                        "reserved_slot": True,
                    })
                    raise LLMProviderProtocolError(
                        provider=provider,
                        model=self.config.model_id,
                        operation=operation,
                        attempts=attempts,
                        fallback_attempted=True,
                    ) from fallback_exc
                attempts.append({
                    "attempt": fallback_attempt,
                    "reason": "nonstream_fallback",
                    "outcome": "success",
                    "elapsed_seconds": round(
                        time.monotonic() - fallback_started, 3
                    ),
                    "reserved_slot": True,
                })
                return fallback_response, attempts
            except Exception as exc:
                rate_limit = rate_limit_error_details(exc)
                if rate_limit is not None:
                    raise LLMRateLimitError(
                        provider=provider,
                        model=self.config.model_id,
                        operation=operation,
                        details=rate_limit,
                    ) from exc
                reason = connection_failure_reason(exc)
                if reason is None:
                    raise
                elapsed = round(time.monotonic() - started, 3)
                attempts.append({
                    "attempt": attempt_number,
                    "reason": "connection_error",
                    "error_type": reason,
                    "error": str(exc) or reason,
                    "elapsed_seconds": elapsed,
                })
                if attempt_index >= max_retries:
                    raise LLMConnectionError(
                        provider=provider,
                        model=self.config.model_id,
                        operation=operation,
                        reason=reason,
                        max_retries=max_retries,
                        attempts=attempts,
                    ) from exc
                delay = _retry_delay(attempt_index)
                attempts[-1]["next_retry_delay_seconds"] = delay
                if delay > 0:
                    await asyncio.sleep(delay)
                continue

            problem = response_validator(response) if response_validator else None
            if problem is None:
                return response, attempts
            elapsed = round(time.monotonic() - started, 3)
            attempts.append({
                "attempt": attempt_number,
                "reason": "degenerate_response",
                "problem": problem,
                "elapsed_seconds": elapsed,
            })
            if attempt_index >= max_retries:
                raise LLMEmptyResponseError(
                    provider=provider,
                    model=self.config.model_id,
                    operation=operation,
                    problem=problem,
                    max_retries=max_retries,
                    attempts=attempts,
                )
            delay = _retry_delay(attempt_index)
            attempts[-1]["next_retry_delay_seconds"] = delay
            if delay > 0:
                await asyncio.sleep(delay)

    @abstractmethod
    async def generate_response(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict], str, Dict[str, Any]]:
        """
        核心统一接口。

        :param system_prompt: 系统提示词
        :param messages: 对话消息列表（Anthropic 格式）
        :param tools: 工具 Schema 列表
        :return: (文本回复, 工具调用列表[{"id", "name", "input"}], stop_reason,
                  usage dict: {cache_read, cache_creation, uncached_input,
                  output, cache_diagnostics})
        """
        pass
