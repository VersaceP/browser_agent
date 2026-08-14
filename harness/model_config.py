"""
harness.model_config - Model config helpers for harness-controlled agents.

The lead and the workers default to the top-level model, and config.json's
optional "lead" / "worker" sections override it: scalars (provider, model_id,
api_key, base_url, timeouts) replace outright, so a role can run on a
different vendor entirely, while ``extra_params`` shallow-merges so setting
one knob does not wipe the rest. This is the single place each role's
effective model config is assembled.
"""

from dataclasses import replace
from typing import Optional

from runtime_config import ModelConfig, RoleModelConfig


def _role_model_config(
    model: ModelConfig,
    override: Optional[RoleModelConfig],
) -> ModelConfig:
    resolved = override.apply_to(model) if override is not None else model
    extra_params = dict(resolved.extra_params)
    extra_params.setdefault("tool_choice", "required")
    return replace(resolved, extra_params=extra_params)


def browser_agent_model_config(
    model: ModelConfig,
    override: Optional[RoleModelConfig] = None,
) -> ModelConfig:
    return _role_model_config(model, override)


def lead_agent_model_config(
    model: ModelConfig,
    override: Optional[RoleModelConfig] = None,
) -> ModelConfig:
    return _role_model_config(model, override)
