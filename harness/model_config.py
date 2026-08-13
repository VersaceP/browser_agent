"""
harness.model_config - Model config helpers for harness-controlled agents.

Lead and worker share one LLM connection but not necessarily the same
per-call knobs: config.json's optional "lead" / "worker" sections carry an
``extra_params`` overlay (thinking mode, temperature, max_tokens) that is
shallow-merged over the top-level ``extra_params`` here — the single place
each role's effective parameters are assembled.
"""

from dataclasses import replace
from typing import Optional

from runtime_config import ModelConfig, RoleOverrideConfig


def _role_model_config(
    model: ModelConfig,
    override: Optional[RoleOverrideConfig],
) -> ModelConfig:
    extra_params = (
        override.apply_to(model.extra_params)
        if override is not None
        else dict(model.extra_params)
    )
    extra_params.setdefault("tool_choice", "required")
    return replace(model, extra_params=extra_params)


def browser_agent_model_config(
    model: ModelConfig,
    override: Optional[RoleOverrideConfig] = None,
) -> ModelConfig:
    return _role_model_config(model, override)


def lead_agent_model_config(
    model: ModelConfig,
    override: Optional[RoleOverrideConfig] = None,
) -> ModelConfig:
    return _role_model_config(model, override)
