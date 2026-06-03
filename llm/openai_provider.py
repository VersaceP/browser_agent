"""
llm.openai_provider - OpenAI and OpenAI-compatible chat adapter.
"""

import asyncio
import json
import os
from typing import Any, Dict, List, Tuple

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

from llm.base import BaseLLMProvider
from llm.cache_control import (
    LLM_API_TIMEOUT,
    _build_cache_diagnostics,
    _emit_cache_log,
    _is_cache_control_rejection,
    _resolve_cache_control_decision,
    _with_cache_control_diagnostics,
)
from llm.config import ModelConfig


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
                
                # 根据原本的 role 重构消息
                if role == "assistant" and (text_parts or tool_calls):
                    msg_dict: Dict[str, Any] = {"role": "assistant"}
                    if text_parts:
                        msg_dict["content"] = "\n".join(text_parts)
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
            }
            for key, value in self.config.extra_params.items():
                if key not in reserved_extra_keys:
                    params[key] = value

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

        # 调用 OpenAI API（带超时保护）
        try:
            response = await asyncio.wait_for(
                self.client.chat.completions.create(**request_params),
                timeout=LLM_API_TIMEOUT,
            )
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
            response = await asyncio.wait_for(
                self.client.chat.completions.create(**fallback_params),
                timeout=LLM_API_TIMEOUT,
            )
            self._cache_control_disabled_after_reject = True

        # 缓存命中观测(OpenAI 自动缓存,只能从 prompt_tokens_details.cached_tokens 反查)
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None)
        cache_read = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        # OpenAI 不区分 creation vs read,首次调用就直接计入 prompt_tokens
        # 这里把"未走缓存的 input"算成 prompt - cached
        prompt_tokens = int(usage.prompt_tokens or 0)
        uncached_input = max(prompt_tokens - cache_read, 0)
        output_tokens = int(usage.completion_tokens or 0)
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
        tool_calls = []
        
        # 解析工具调用（转换回 Anthropic 格式）
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    parsed_input = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError as e:
                    parsed_input = {
                        "_parse_error": str(e),
                        "_raw_arguments": tc.function.arguments or "",
                    }
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

        return response_text, tool_calls, stop_reason, {
            "cache_read": cache_read,
            "cache_creation": 0,        # OpenAI 不区分,首次请求的写入算 uncached
            "uncached_input": uncached_input,
            "output": output_tokens,
            "cache_diagnostics": cache_diagnostics,
        }
