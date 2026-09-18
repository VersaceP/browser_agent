"""
llm.factory - LLM provider factory.
"""

from llm.anthropic_provider import AnthropicProvider
from llm.base import BaseLLMProvider
from runtime_config import ModelConfig
from llm.openai_provider import OpenAIProvider
from llm.profiles import ANTHROPIC, OPENAI, RESPONSES, resolve_target
from llm.responses_provider import OpenAIResponsesProvider


class LLMFactory:
    """工厂模式：根据配置创建对应的 LLM Provider"""

    @staticmethod
    def create_provider(config: ModelConfig) -> BaseLLMProvider:
        api = resolve_target(config).api
        if api == ANTHROPIC:
            return AnthropicProvider(config)
        elif api == RESPONSES:
            return OpenAIResponsesProvider(config)
        elif api == OPENAI:
            return OpenAIProvider(config)
        else:
            raise ValueError(f"[LLM Factory] 不支持的模型厂商: {config.provider}")
