"""
harness.model_config - Model config helpers for harness-controlled agents.
"""

from dataclasses import replace

from llm.config import ModelConfig


def browser_agent_model_config(model: ModelConfig) -> ModelConfig:
    extra_params = dict(model.extra_params)
    extra_params.setdefault("tool_choice", "required")
    return replace(model, extra_params=extra_params)


def lead_agent_model_config(model: ModelConfig) -> ModelConfig:
    extra_params = dict(model.extra_params)
    extra_params.setdefault("tool_choice", "required")
    return replace(model, extra_params=extra_params)
