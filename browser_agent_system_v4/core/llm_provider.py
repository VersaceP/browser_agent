"""
llm_provider.py — LLM 多厂商适配网关

V4 重构：
- 移除对 agent_config.ModelConfig 的依赖，内建 ModelConfig
- 通过 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 环境变量获取凭证
- 保留 AnthropicProvider，完善 OpenAIProvider 骨架
"""

import os
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from anthropic import AsyncAnthropic


@dataclass
class ModelConfig:
    """模型配置 — 定义 LLM 的连接参数"""
    provider: str = "anthropic"
    model_id: str = "claude-sonnet-4-20250514"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)


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
    ) -> Tuple[str, List[Dict], str]:
        """
        核心统一接口。
        
        :param system_prompt: 系统提示词
        :param messages: 对话消息列表（Anthropic 格式）
        :param tools: 工具 Schema 列表
        :return: (文本回复, 工具调用列表[{"id", "name", "input"}], stop_reason)
        """
        pass


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude 系列模型的原生适配器"""

    def __init__(self, config: ModelConfig):
        super().__init__(config)

        # 通过环境变量获取 API 凭证（ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL）
        api_key = self.config.api_key or os.getenv("ANTHROPIC_AUTH_TOKEN")
        base_url = self.config.base_url or os.getenv("ANTHROPIC_BASE_URL")

        if not api_key:
            raise ValueError(
                "[LLM Gateway] 未检测到 API 秘钥！"
                "请设置环境变量 ANTHROPIC_AUTH_TOKEN"
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
        response = await self.client.messages.create(
            model=self.config.model_id,
            system=system_prompt,
            messages=messages,
            tools=tools if tools else [],
            max_tokens=4096,
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

        return response_text, tool_calls, response.stop_reason


class OpenAIProvider(BaseLLMProvider):
    """
    OpenAI / DeepSeek 等 OpenAI-Compatible 模型的适配器。
    将 Anthropic 格式的 tools 转换为 OpenAI Function Calling 格式。
    """

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        # 预留 OpenAI SDK 初始化
        # self.client = AsyncOpenAI(api_key=..., base_url=...)

    async def generate_response(
        self,
        system_prompt: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> Tuple[str, List[Dict], str]:
        raise NotImplementedError(
            "[LLM Gateway] OpenAI 适配层骨架已备好，等待 SDK 引入。"
        )


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
