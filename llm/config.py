"""
llm.config - Model connection configuration.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


DEFAULT_LLM_API_TIMEOUT_SECONDS = 180.0
DEFAULT_LLM_TIMEOUT_MAX_RETRIES = 1
DEFAULT_LLM_TIMEOUT_BACKOFF_SECONDS = 1.0


def _float_config(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _optional_float_config(value: Any, *, minimum: float = 0.0) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, parsed)


def _int_config(value: Any, default: int, *, minimum: int = 0, maximum: int = 10) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclass
class ModelConfig:
    """模型配置 — 定义 LLM 的连接参数"""
    provider: str = "anthropic"
    model_id: str = "claude-sonnet-4-20250514"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)
    llm_api_timeout_seconds: float = DEFAULT_LLM_API_TIMEOUT_SECONDS
    llm_timeout_max_retries: int = DEFAULT_LLM_TIMEOUT_MAX_RETRIES
    llm_timeout_backoff_seconds: float = DEFAULT_LLM_TIMEOUT_BACKOFF_SECONDS
    llm_timeout_retry_interval_seconds: Optional[float] = None

    @classmethod
    def load_from_file(cls, filepath: str) -> "ModelConfig":
        """从 JSON 配置文件加载配置，敏感字段通过环境变量名间接获取"""
        if not os.path.exists(filepath):
            return cls()

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        extra_params = (
            data.get("extra_params")
            if isinstance(data.get("extra_params"), dict)
            else {}
        )

        def model_value(key: str, default: Any) -> Any:
            return data.get(key, extra_params.get(key, default))

        return cls(
            provider=data.get("provider", "anthropic"),
            model_id=data.get("model_id", "claude-sonnet-4-20250514"),
            api_key=data.get("api_key") or cls._env(data.get("api_key_env")),
            base_url=data.get("base_url") or cls._env(data.get("base_url_env")),
            extra_params=extra_params,
            llm_api_timeout_seconds=_float_config(
                model_value(
                    "llm_api_timeout_seconds",
                    DEFAULT_LLM_API_TIMEOUT_SECONDS,
                ),
                DEFAULT_LLM_API_TIMEOUT_SECONDS,
                minimum=1.0,
            ),
            llm_timeout_max_retries=_int_config(
                model_value(
                    "llm_timeout_max_retries",
                    DEFAULT_LLM_TIMEOUT_MAX_RETRIES,
                ),
                DEFAULT_LLM_TIMEOUT_MAX_RETRIES,
                minimum=0,
                maximum=10,
            ),
            llm_timeout_backoff_seconds=_float_config(
                model_value(
                    "llm_timeout_backoff_seconds",
                    DEFAULT_LLM_TIMEOUT_BACKOFF_SECONDS,
                ),
                DEFAULT_LLM_TIMEOUT_BACKOFF_SECONDS,
                minimum=0.0,
            ),
            llm_timeout_retry_interval_seconds=_optional_float_config(
                model_value("llm_timeout_retry_interval_seconds", None),
                minimum=0.0,
            ),
        )

    @staticmethod
    def _env(key: Optional[str]) -> Optional[str]:
        """从系统环境变量中读取指定 key 的值"""
        if not key:
            return None
        return os.environ.get(key)
