"""
llm.factory - LLM provider factory.
"""

from llm.anthropic_provider import AnthropicProvider
from llm.base import BaseLLMProvider
from llm.config import ModelConfig
from llm.openai_provider import OpenAIProvider


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
