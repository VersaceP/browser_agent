"""
llm.config - Model connection configuration.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


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
            api_key=data.get("api_key") or cls._env(data.get("api_key_env")),
            base_url=data.get("base_url") or cls._env(data.get("base_url_env")),
            extra_params=data.get("extra_params", {}),
        )

    @staticmethod
    def _env(key: Optional[str]) -> Optional[str]:
        """从系统环境变量中读取指定 key 的值"""
        if not key:
            return None
        return os.environ.get(key)
