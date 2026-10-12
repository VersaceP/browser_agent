"""
harness.runtime.model_config - Model config helpers for harness-controlled agents.

Browser workers use "worker"; the Skill Orchestrator can configure its own
"skill_orchestrator" connection or reuse "worker". Each explicit connection
is complete. This is the single place effective agent model config is read.
"""

from dataclasses import replace

from runtime_config import ModelConfig, RuntimeConfig


def _agent_model_config(model: ModelConfig) -> ModelConfig:
    extra_params = dict(model.extra_params)
    extra_params.setdefault("tool_choice", "required")
    return replace(model, extra_params=extra_params)


def browser_agent_model_config(runtime: RuntimeConfig) -> ModelConfig:
    return _agent_model_config(runtime.worker)


def skill_orchestrator_model_config(runtime: RuntimeConfig) -> ModelConfig:
    """A separately configurable role; fallback reuses only the connection."""
    return _agent_model_config(getattr(runtime, "skill_orchestrator", None) or runtime.worker)
