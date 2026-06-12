"""
harness.config - Runtime and harness configuration for ABCP agents.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from abcp_client import ABCPClientConfig
from harness.constants import (
    DEFAULT_LOCAL_FS_READ_BYTES,
    DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    DEFAULT_TOOL_RESULT_OFFLOAD_THRESHOLD_BYTES,
)
from llm.config import ModelConfig


JsonDict = Dict[str, Any]


@dataclass
class VLConfig:
    enabled: bool = False
    provider: str = "openai"
    model_id: str = ""
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    max_checks_per_worker: int = 2
    default_timeout_seconds: float = 60.0
    extra_params: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "VLConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", cls.enabled)),
            provider=str(data.get("provider", cls.provider) or cls.provider),
            model_id=str(data.get("model_id", cls.model_id) or cls.model_id),
            base_url=data.get("base_url"),
            api_key=data.get("api_key"),
            max_checks_per_worker=int(
                data.get("max_checks_per_worker", cls.max_checks_per_worker)
            ),
            default_timeout_seconds=float(
                data.get("default_timeout_seconds", cls.default_timeout_seconds)
            ),
            extra_params=(
                data.get("extra_params")
                if isinstance(data.get("extra_params"), dict)
                else {}
            ),
        )


@dataclass
class HarnessConfig:
    mode: str = "lead"
    max_steps: int = 40
    lead_max_steps: int = 40
    worker_max_steps: int = 40
    max_browser_agent_instances: int = 3
    max_browser_agents: int = 3
    default_worker_concurrency: int = 3
    worktree_dir: str = "worktree"
    runs_dir: str = "runs"
    artifacts_dir: str = "artifacts"
    context_file: Optional[str] = None
    strategy_bank_path: str = "strategy_bank/strategy_bank.json"
    memory_context: str = (
        "ABCP agent harness: drive the browser only through ABCP atomic capabilities. "
        "Trust the cached System.describeAction schemas (global_schema_cache/schemas/<Method>.json) "
        "as the source of truth for parameters; descriptions live in System.getCapabilities."
    )
    screenshot_max_base64_chars: int = 0
    max_observation_chars: int = 24000
    offload_threshold_bytes: int = DEFAULT_OFFLOAD_THRESHOLD_BYTES
    tool_result_offload_threshold_bytes: int = (
        DEFAULT_TOOL_RESULT_OFFLOAD_THRESHOLD_BYTES
    )
    local_fs_max_read_bytes: int = DEFAULT_LOCAL_FS_READ_BYTES
    model_context_window_tokens: int = 262144
    context_compaction_threshold_ratio: float = 0.85
    context_compaction_keep_head_pairs: int = 1
    context_compaction_keep_tail_pairs: int = 3
    log_browser_payloads: bool = True
    hitl_poll_interval_seconds: float = 2.0
    hitl_wait_timeout_seconds: float = 1200.0
    hitl_max_step_retries: int = 1
    hitl_no_repause_cooldown_seconds: float = 8.0
    hitl_post_resume_guard_seconds: float = 30.0
    hitl_post_resume_confirm_max_rounds: int = 3
    progress_local_fs_without_extraction_limit: int = 5
    progress_no_artifact_limit: int = 8
    vl: VLConfig = field(default_factory=VLConfig)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "HarnessConfig":
        return cls(
            mode=data.get("mode", cls.mode),
            max_steps=int(data.get("max_steps", cls.max_steps)),
            lead_max_steps=int(data.get("lead_max_steps", cls.lead_max_steps)),
            worker_max_steps=int(data.get("worker_max_steps", cls.worker_max_steps)),
            max_browser_agent_instances=int(
                data.get(
                    "max_browser_agent_instances",
                    cls.max_browser_agent_instances,
                )
            ),
            max_browser_agents=int(
                data.get("max_browser_agents", cls.max_browser_agents)
            ),
            default_worker_concurrency=int(
                data.get(
                    "default_worker_concurrency",
                    cls.default_worker_concurrency,
                )
            ),
            worktree_dir=data.get("worktree_dir", cls.worktree_dir),
            runs_dir=data.get("runs_dir", cls.runs_dir),
            artifacts_dir=data.get("artifacts_dir", cls.artifacts_dir),
            context_file=data.get("context_file"),
            strategy_bank_path=data.get(
                "strategy_bank_path",
                cls.strategy_bank_path,
            ),
            memory_context=data.get("memory_context", cls.memory_context),
            screenshot_max_base64_chars=int(
                data.get("screenshot_max_base64_chars", cls.screenshot_max_base64_chars)
            ),
            max_observation_chars=int(
                data.get("max_observation_chars", cls.max_observation_chars)
            ),
            offload_threshold_bytes=int(
                data.get("offload_threshold_bytes", cls.offload_threshold_bytes)
            ),
            tool_result_offload_threshold_bytes=int(
                data.get(
                    "tool_result_offload_threshold_bytes",
                    cls.tool_result_offload_threshold_bytes,
                )
            ),
            local_fs_max_read_bytes=int(
                data.get("local_fs_max_read_bytes", cls.local_fs_max_read_bytes)
            ),
            model_context_window_tokens=int(
                data.get(
                    "model_context_window_tokens",
                    cls.model_context_window_tokens,
                )
            ),
            context_compaction_threshold_ratio=float(
                data.get(
                    "context_compaction_threshold_ratio",
                    cls.context_compaction_threshold_ratio,
                )
            ),
            context_compaction_keep_head_pairs=int(
                data.get(
                    "context_compaction_keep_head_pairs",
                    cls.context_compaction_keep_head_pairs,
                )
            ),
            context_compaction_keep_tail_pairs=int(
                data.get(
                    "context_compaction_keep_tail_pairs",
                    cls.context_compaction_keep_tail_pairs,
                )
            ),
            log_browser_payloads=bool(
                data.get("log_browser_payloads", cls.log_browser_payloads)
            ),
            hitl_poll_interval_seconds=float(
                data.get(
                    "hitl_poll_interval_seconds",
                    cls.hitl_poll_interval_seconds,
                )
            ),
            hitl_wait_timeout_seconds=float(
                data.get(
                    "hitl_wait_timeout_seconds",
                    cls.hitl_wait_timeout_seconds,
                )
            ),
            hitl_max_step_retries=int(
                data.get("hitl_max_step_retries", cls.hitl_max_step_retries)
            ),
            hitl_no_repause_cooldown_seconds=float(
                data.get(
                    "hitl_no_repause_cooldown_seconds",
                    cls.hitl_no_repause_cooldown_seconds,
                )
            ),
            hitl_post_resume_guard_seconds=float(
                data.get(
                    "hitl_post_resume_guard_seconds",
                    cls.hitl_post_resume_guard_seconds,
                )
            ),
            hitl_post_resume_confirm_max_rounds=int(
                data.get(
                    "hitl_post_resume_confirm_max_rounds",
                    cls.hitl_post_resume_confirm_max_rounds,
                )
            ),
            progress_local_fs_without_extraction_limit=int(
                data.get(
                    "progress_local_fs_without_extraction_limit",
                    cls.progress_local_fs_without_extraction_limit,
                )
            ),
            progress_no_artifact_limit=int(
                data.get(
                    "progress_no_artifact_limit",
                    cls.progress_no_artifact_limit,
                )
            ),
        )


@dataclass
class RuntimeConfig:
    agent_id: str
    model: ModelConfig
    browser: ABCPClientConfig
    harness: HarnessConfig
