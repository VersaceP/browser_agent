"""
llm.base - Shared LLM provider interface.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm.config import ModelConfig


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

    async def _request_with_timeout_retries(
        self,
        request_factory: Callable[[], Any],
        *,
        provider: str,
        operation: str,
        response_validator: Optional[
            Callable[[Any], Optional[Dict[str, Any]]]
        ] = None,
    ) -> Tuple[Any, List[Dict[str, Any]]]:
        """Run the request with timeout retries; optionally validate the shape.

        response_validator inspects the RAW provider response and returns a
        problem dict when the response is degenerate (gateway truncation:
        missing usage / empty content). Degenerate responses consume the same
        retry budget as timeouts and raise LLMEmptyResponseError once
        exhausted — they must not surface as normal empty completions.
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
                response = await asyncio.wait_for(
                    request_factory(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
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
