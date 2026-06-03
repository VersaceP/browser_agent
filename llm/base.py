"""
llm.base - Shared LLM provider interface.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple

from llm.config import ModelConfig


class BaseLLMProvider(ABC):
    """
    大模型路由抽象层。
    
    负责将 V4 系统统一的 Messages 和 Tools 格式，
    翻译成各个厂商的协议，最终保证外围系统获得统一格式的产物。
    """

    def __init__(self, config: ModelConfig):
        self.config = config
        self._cache_control_disabled_after_reject = False

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
