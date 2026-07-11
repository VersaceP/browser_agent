"""
llm.anthropic_provider - Anthropic messages API adapter.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

try:
    from anthropic import AsyncAnthropic
except ImportError:
    AsyncAnthropic = None

from llm.base import BaseLLMProvider
from llm.cache_control import (
    _build_cache_diagnostics,
    _emit_cache_log,
    _is_cache_control_rejection,
    _resolve_cache_control_decision,
    _with_cache_control_diagnostics,
)
from llm.config import ModelConfig


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


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, config: ModelConfig):
        super().__init__(config)

        if AsyncAnthropic is None:
            raise ImportError(
                "[LLM Gateway] 缺少 anthropic SDK，请先安装: pip install anthropic"
            )

        # api_key / base_url 已由 ModelConfig 统一从环境变量解析
        # 这里仅做最终的空值兜底（直接构造 ModelConfig 但未走 load_from_file 的场景）
        api_key = self.config.api_key or os.getenv("ANTHROPIC_AUTH_TOKEN")
        base_url = self.config.base_url or os.getenv("ANTHROPIC_BASE_URL")

        if not api_key:
            raise ValueError(
                "[LLM Gateway] Anthropic API 秘钥缺失！\n"
                "  方式 1: 在 config.json 中设置 api_key_env 指向你的环境变量名\n"
                "  方式 2: 直接设置系统环境变量 ANTHROPIC_AUTH_TOKEN"
            )

        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def generate_response(
        self,
        system_prompt: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> Tuple[str, List[Dict], str, Dict[str, Any]]:
        strict_tools = bool(self.config.extra_params.get("strict_tools", False))
        cache_decision = _resolve_cache_control_decision("anthropic", self.config)

        def build_kwargs(cache_enabled: bool) -> Dict[str, Any]:
            if cache_enabled:
                system_value: Any = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                system_value = system_prompt

            # 在最后一条消息上挂缓存 marker(滚动缓存对话历史)。
            # 复制最后一条消息和最后一个 block,避免污染调用方持有的 messages 列表。
            messages_for_request = messages
            if cache_enabled and messages:
                last_msg = messages[-1]
                last_content = last_msg.get("content")
                new_last_msg = None
                if isinstance(last_content, str) and last_content:
                    new_last_msg = {
                        **last_msg,
                        "content": [
                            {
                                "type": "text",
                                "text": last_content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                elif isinstance(last_content, list) and last_content:
                    last_block = last_content[-1]
                    if isinstance(last_block, dict):
                        new_last_block = {
                            **last_block,
                            "cache_control": {"type": "ephemeral"},
                        }
                        new_last_msg = {
                            **last_msg,
                            "content": list(last_content[:-1]) + [new_last_block],
                        }
                if new_last_msg is not None:
                    messages_for_request = list(messages[:-1]) + [new_last_msg]

            request_kwargs: Dict[str, Any] = {
                "model": self.config.model_id,
                "system": system_value,
                "messages": messages_for_request,
                "max_tokens": self.config.extra_params.get("max_tokens", 4096),
            }
            if tools:
                normalized_tools = []
                for tool in tools:
                    normalized_tool = dict(tool)
                    if strict_tools:
                        normalized_tool.setdefault("strict", True)
                    normalized_tools.append(normalized_tool)
                request_kwargs["tools"] = (
                    list(normalized_tools[:-1]) + [
                        {
                            **normalized_tools[-1],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                    if cache_enabled else normalized_tools
                )

                tool_choice = self._convert_tool_choice(
                    self.config.extra_params.get("tool_choice")
                )
                if tool_choice:
                    request_kwargs["tool_choice"] = tool_choice
            return request_kwargs

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

        async def request_with_timeout(request_kwargs: Dict[str, Any]):
            response, attempts = await self._request_with_timeout_retries(
                lambda: self.client.messages.create(**request_kwargs),
                provider="anthropic",
                operation="messages.create",
                response_validator=_degenerate_response_problem,
            )
            if attempts:
                timeout_attempts.extend(attempts)
                _emit_cache_log(
                    "[Anthropic] messages.create recovered after "
                    f"{len(attempts)} timeout/degenerate attempt(s)"
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

        # 解析返回内容
        response_text = ""
        tool_calls = []
        assistant_prefix_blocks: List[Dict[str, Any]] = []

        for block in (response.content or []):
            if block.type == "text":
                response_text += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            elif block.type == "thinking":
                # Thinking-mode 模型(如 deepseek-v4-pro)要求下一轮把 thinking 块原样回传。
                thinking_block: Dict[str, Any] = {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", "") or "",
                }
                signature = getattr(block, "signature", None)
                if signature:
                    thinking_block["signature"] = signature
                assistant_prefix_blocks.append(thinking_block)
            elif block.type == "redacted_thinking":
                assistant_prefix_blocks.append({
                    "type": "redacted_thinking",
                    "data": getattr(block, "data", ""),
                })

        return response_text, tool_calls, response.stop_reason or "end_turn", {
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "uncached_input": uncached_input,
            "output": output_tokens,
            "cache_diagnostics": cache_diagnostics,
            "timeout_retries": sum(
                1 for item in timeout_attempts
                if item.get("reason") != "degenerate_response"
            ),
            "degenerate_retries": sum(
                1 for item in timeout_attempts
                if item.get("reason") == "degenerate_response"
            ),
            "timeout_attempts": timeout_attempts,
            "timeout_seconds": self._llm_timeout_seconds(),
            "timeout_max_retries": self._llm_timeout_max_retries(),
            "timeout_retry_interval_seconds": self._llm_timeout_retry_interval_seconds(),
            "_assistant_prefix_blocks": assistant_prefix_blocks,
        }

    def _convert_tool_choice(self, tool_choice: Any) -> Optional[Dict[str, Any]]:
        if not tool_choice:
            return None
        if isinstance(tool_choice, str):
            mapping = {
                "auto": {"type": "auto"},
                "required": {"type": "any"},
                "any": {"type": "any"},
                "none": {"type": "none"},
            }
            return mapping.get(tool_choice)
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "function":
                name = (tool_choice.get("function") or {}).get("name")
                if name:
                    return {"type": "tool", "name": name}
            if tool_choice.get("type") in {"auto", "any", "none", "tool"}:
                return tool_choice
        return None
