"""
harness.runtime.model_config - Model config helpers for harness-controlled agents.

The lead and the workers each have their own complete connection in
config.json ("lead" / "worker"); nothing is inherited from anywhere else. This
is the single place each role's effective model config is read.
"""

from dataclasses import replace

from runtime_config import ModelConfig, RuntimeConfig


def _agent_model_config(model: ModelConfig) -> ModelConfig:
    extra_params = dict(model.extra_params)
    extra_params.setdefault("tool_choice", "required")
    return replace(model, extra_params=extra_params)


def browser_agent_model_config(runtime: RuntimeConfig) -> ModelConfig:
    return _agent_model_config(runtime.worker)


def lead_agent_model_config(runtime: RuntimeConfig) -> ModelConfig:
    return _agent_model_config(runtime.lead)
