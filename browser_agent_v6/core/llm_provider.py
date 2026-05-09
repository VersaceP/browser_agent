"""
llm_provider.py — LLM 多厂商适配网关
"""

import os
import json
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

_LLM_API_TIMEOUT = 180


def _emit_cache_log(line: str) -> None:
    """
    缓存命中观测输出。
    LLM_CACHE_DEBUG=1 才生效:同时打到 stdout 和 V5_CACHE_LOG_PATH 指定的文件
    (由 AgentSpawner 在 session 创建时设为 shared/cache_stats.log)
    """
    if not os.getenv("LLM_CACHE_DEBUG"):
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_line = f"{ts} {line}"
    print(full_line)
    log_path = os.getenv("V5_CACHE_LOG_PATH")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(full_line + "\n")
        except OSError:
            # 文件写入失败不影响主流程(磁盘满 / 权限等)
            pass


@dataclass
class ModelConfig:
    """模型配置 — 定义 LLM 的连接参数"""
    provider: str = "anthropic"
    model_id: str = "claude-sonnet-4-20250514"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load_from_file(cls, filepath: str) -> "ModelConfig":
        """从 JSON 配置文件加载配置，敏感字段通过环境变量名间接获取"""
        if not os.path.exists(filepath):
            return cls()

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        return cls(
            provider=data.get("provider", "anthropic"),
            model_id=data.get("model_id", "claude-sonnet-4-20250514"),
            api_key= data.get("api_key") or cls._env(data.get("api_key_env")),
            base_url= data.get("base_url") or cls._env(data.get("base_url_env")),
            extra_params=data.get("extra_params", {}),
        )

    @staticmethod
    def _env(key: Optional[str]) -> Optional[str]:
        """从系统环境变量中读取指定 key 的值"""
        if not key:
            return None
        return os.environ.get(key)


class BaseLLMProvider(ABC):
    """
    大模型路由抽象层。
    
    负责将 V4 系统统一的 Messages 和 Tools 格式，
    翻译成各个厂商的协议，最终保证外围系统获得统一格式的产物。
    """

    def __init__(self, config: ModelConfig):
        self.config = config

    @abstractmethod
    async def generate_response(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict], str, Dict[str, int]]:
        """
        核心统一接口。

        :param system_prompt: 系统提示词
        :param messages: 对话消息列表（Anthropic 格式）
        :param tools: 工具 Schema 列表
        :return: (文本回复, 工具调用列表[{"id", "name", "input"}], stop_reason,
                  usage dict: {cache_read, cache_creation, uncached_input, output})
        """
        pass


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, config: ModelConfig):
        super().__init__(config)

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

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

        self.client = AsyncAnthropic(**kwargs)

    async def generate_response(
        self,
        system_prompt: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> Tuple[str, List[Dict], str]:
        system_block = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"}
            }
        ]

        # 在最后一条消息上挂缓存 marker(滚动缓存对话历史)
        # Anthropic 文档建议多轮对话把 marker 放在末尾消息上,让 breakpoint 逐轮前移
        # 当前已用 system + 末位 tool 共 2 个 breakpoint,加这个第 3 个仍在 4 上限内
        # 必须复制最后一条消息和最后一个 block,避免污染调用方持有的 messages 列表
        # (否则旧 marker 会残留在历史里,叠加导致超 4 breakpoint 上限)
        messages_with_cache = messages
        if messages:
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
                    new_last_block = {**last_block, "cache_control": {"type": "ephemeral"}}
                    new_last_msg = {
                        **last_msg,
                        "content": list(last_content[:-1]) + [new_last_block],
                    }
            if new_last_msg is not None:
                messages_with_cache = list(messages[:-1]) + [new_last_msg]

        kwargs = {
            "model": self.config.model_id,
            "system": system_block,
            "messages": messages_with_cache,
            "max_tokens": 4096,
        }
        if tools:
            tools_with_cache = list(tools[:-1]) + [
                {**tools[-1], "cache_control": {"type": "ephemeral"}}
            ]
            kwargs["tools"] = tools_with_cache

        response = await asyncio.wait_for(
            self.client.messages.create(**kwargs),
            timeout=_LLM_API_TIMEOUT,
        )

        # 缓存命中观测(设置环境变量 LLM_CACHE_DEBUG=1 打开)
        usage = response.usage
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        uncached_input = int(usage.input_tokens or 0)  # 已扣除 cache 部分
        output_tokens = int(usage.output_tokens or 0)
        _emit_cache_log(
            f"[Anthropic Cache] new={uncached_input} "
            f"create={cache_creation} read={cache_read} out={output_tokens}"
        )

        # 解析返回内容
        response_text = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                response_text += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        return response_text, tool_calls, response.stop_reason, {
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "uncached_input": uncached_input,
            "output": output_tokens,
        }


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, config: ModelConfig):
        super().__init__(config)

        # api_key / base_url 已由 ModelConfig 统一从环境变量解析
        api_key = self.config.api_key or os.getenv("OPENAI_API_KEY")
        base_url = self.config.base_url or os.getenv("OPENAI_BASE_URL")

        if not api_key:
            raise ValueError(
                "[LLM Gateway] OpenAI API 秘钥缺失！\n"
                "  方式 1: 在 config.json 中设置 api_key_env 指向你的环境变量名\n"
                "  方式 2: 直接设置系统环境变量 OPENAI_API_KEY"
            )

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

        self.client = AsyncOpenAI(**kwargs)

    def _convert_anthropic_tools_to_openai(self, tools: List[Dict]) -> List[Dict]:
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
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {
                        "type": "object",
                        "properties": {}
                    })
                }
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
                    msg_dict = {"role": "assistant"}
                    if text_parts:
                        msg_dict["content"] = "\n".join(text_parts)
                    if tool_calls:
                        msg_dict["tool_calls"] = tool_calls
                    openai_messages.append(msg_dict)
                elif role == "user" and has_user_content:
                    msg_dict = {"role": "user", "content": "\n".join(text_parts)}
                    openai_messages.append(msg_dict)
            else:
                # 其他情况直接传递
                openai_messages.append(msg)
        
        return openai_messages

    async def generate_response(
        self,
        system_prompt: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> Tuple[str, List[Dict], str, Dict[str, int]]:
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
        # 转换消息格式
        openai_messages = self._convert_anthropic_messages_to_openai(messages)

        # 在最后一条消息上挂缓存 marker(滚动缓存对话历史)
        # 显式缓存以 cache_control 位置为终点,向前回溯最多 20 个 content 块前缀匹配,
        # 在末尾再打一个 marker 可让长对话历史也走缓存
        if openai_messages:
            last_msg = openai_messages[-1]
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
                    last_block = last_content[-1]
                    if isinstance(last_block, dict):
                        last_block["cache_control"] = {"type": "ephemeral"}

        # 将 system_prompt 插入到消息列表开头
        openai_messages.insert(0, {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}
                }
            ]
        })
        
        # 转换工具格式
        openai_tools = self._convert_anthropic_tools_to_openai(tools) if tools else []
        
        # 构建请求参数
        request_params = {
            "model": self.config.model_id,
            "messages": openai_messages,
            "max_tokens": self.config.extra_params.get("max_tokens", 4096),
            "temperature": self.config.extra_params.get("temperature", 1.0),
        }
        
        # 只有在有工具时才添加 tools 参数
        if openai_tools:
            request_params["tools"] = openai_tools
            request_params["tool_choice"] = self.config.extra_params.get("tool_choice", "auto")
        
        # 调用 OpenAI API（带超时保护）
        response = await asyncio.wait_for(
            self.client.chat.completions.create(**request_params),
            timeout=_LLM_API_TIMEOUT,
        )

        # 缓存命中观测(OpenAI 自动缓存,只能从 prompt_tokens_details.cached_tokens 反查)
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None)
        cache_read = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        # OpenAI 不区分 creation vs read,首次调用就直接计入 prompt_tokens
        # 这里把"未走缓存的 input"算成 prompt - cached
        prompt_tokens = int(usage.prompt_tokens or 0)
        uncached_input = max(prompt_tokens - cache_read, 0)
        output_tokens = int(usage.completion_tokens or 0)
        _emit_cache_log(
            f"[OpenAI Cache] prompt={prompt_tokens} read={cache_read} "
            f"uncached={uncached_input} out={output_tokens}"
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
        finish_reason = response.choices[0].finish_reason
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
        }


class LLMFactory:
    """工厂模式：根据配置创建对应的 LLM Provider"""

    @staticmethod
    def create_provider(config: ModelConfig) -> BaseLLMProvider:
        if config.provider.lower() == "anthropic":
            return AnthropicProvider(config)
        elif config.provider.lower() == "openai":
            return OpenAIProvider(config)
        else:
            raise ValueError(f"[LLM Factory] 不支持的模型厂商: {config.provider}")
