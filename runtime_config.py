"""
runtime_config.py - 全项目统一配置表（single source of truth）。

config.json 的四张配置表全部定义在这一个文件里：

    顶层             -> ModelConfig       （LLM 连接：provider/model_id/api_key/超时重试/extra_params）
    "vl": {...}      -> VLConfig          （视觉模型连接 + 各 VL 角色开关）
    "browser": {...} -> ABCPClientConfig  （ABCP WebSocket 连接）
    "harness": {...} -> HarnessConfig     （编排/步数预算/offload/HITL/skill 等运行时行为）

历史位置 llm/config.py、abcp_client.py、harness/config.py 仍从这里 re-export，
旧 import 路径全部兼容。load_runtime_config() 是唯一装载入口；装载时对
config.json 里不认识的字段打印告警，不再静默吞掉写了也不生效的键。

本模块只依赖标准库（不 import 项目内任何模块），避免循环依赖。
"""

import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


# ---------------------------------------------------------------------------
# 共享默认值（harness/constants.py 从这里 re-export）
# ---------------------------------------------------------------------------

DEFAULT_OFFLOAD_THRESHOLD_BYTES = 8000
DEFAULT_TOOL_RESULT_OFFLOAD_THRESHOLD_BYTES = 50000
DEFAULT_LOCAL_FS_READ_BYTES = 20000

DEFAULT_LLM_API_TIMEOUT_SECONDS = 180.0
DEFAULT_LLM_TIMEOUT_MAX_RETRIES = 1
DEFAULT_LLM_TIMEOUT_BACKOFF_SECONDS = 1.0


# ---------------------------------------------------------------------------
# 数值解析 helpers
# ---------------------------------------------------------------------------

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


def _normalize_selection_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in ("manual", "auto") else "manual"


# ---------------------------------------------------------------------------
# 顶层：ModelConfig（LLM 连接）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# "browser" 段：ABCPClientConfig（ABCP WebSocket 连接）
# ---------------------------------------------------------------------------

@dataclass
class ABCPClientConfig:
    ws_url: str = "ws://localhost:9300/ws"
    jwt_token: Optional[str] = None
    jwt_token_env: Optional[str] = None
    request_shape: str = "flat"
    connect_timeout_seconds: float = 15
    call_timeout_seconds: float = 60
    ping_interval_seconds: Optional[float] = 20
    max_message_size_bytes: Optional[int] = 16 * 1024 * 1024

    @classmethod
    def from_dict(cls, data: JsonDict) -> "ABCPClientConfig":
        token = data.get("jwt_token")
        token_env = data.get("jwt_token_env")
        if not token and token_env:
            token = os.environ.get(token_env)
        max_message_size = data.get(
            "max_message_size_bytes",
            cls.max_message_size_bytes,
        )

        return cls(
            ws_url=data.get("ws_url", cls.ws_url),
            jwt_token=token,
            jwt_token_env=token_env,
            request_shape=data.get("request_shape", cls.request_shape),
            connect_timeout_seconds=float(
                data.get("connect_timeout_seconds", cls.connect_timeout_seconds)
            ),
            call_timeout_seconds=float(
                data.get("call_timeout_seconds", cls.call_timeout_seconds)
            ),
            ping_interval_seconds=data.get(
                "ping_interval_seconds", cls.ping_interval_seconds
            ),
            max_message_size_bytes=(
                None if max_message_size is None else int(max_message_size)
            ),
        )


# ---------------------------------------------------------------------------
# "vl" 段：VLConfig（视觉模型 + 各 VL 角色开关）
# ---------------------------------------------------------------------------

@dataclass
class VLConfig:
    enabled: bool = False
    provider: str = "openai"
    model_id: str = ""
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    max_checks_per_worker: int = 2
    default_timeout_seconds: float = 60.0
    # captcha_solve (§13.4): bounded solve-plan retries before short-circuiting to
    # HITL. Behavioral-risk / unknown challenges are NEVER retried (honest
    # short-circuit in vl._finalize_captcha_solve); this only bounds the
    # visual-self-consistent solve loop.
    captcha_solve_max_retries: int = 2
    # VL Role C (§13.4): auto-attempt a visual-self-consistent CAPTCHA via solve-plan
    # (drag/click) before falling back to a human. Separate, default-OFF gate — this
    # is the most consequential VL action (it acts on a challenge, with ToS/legal
    # weight), so it must be opted into INDEPENDENTLY of vl.enabled / the other roles.
    # When OFF (or vl.enabled OFF), a pause always resolves via the human path.
    captcha_solve_enabled: bool = False
    # VL Role A (§13.2): locate an AXTree-blind target visually, then PROMOTE the
    # pixel back to a durable canonical id via bbox containment. Default OFF — a
    # caller (slow-path recovery) opts in before invoking harness.vl.locate.
    visual_locate_enabled: bool = False
    # VL Role B (§13.3): after a skill's variable success_contract passes, judge the
    # declared visual_checks (text_present / challenge_gone / ...) on a screenshot.
    # Low-cost confirmation, default ON; only a definitive `violated` vetoes (VL is
    # L4/weak — `uncertain` never overrides the passed variable contract).
    contract_verify_enabled: bool = True
    # VL Role D (§13.5): auto-trigger the global visual arbiter on a visually-related
    # browser_call failure (after deterministic recovery), routing it to the right VL
    # role and attaching a recovery recommendation (resolvedId / hitl / dismiss / ...)
    # to the result. Default OFF — it costs a VL call per visual failure.
    arbiter_enabled: bool = False
    # Visual reality check: when perception keeps falling short of the task
    # target (a target-shortfall streak — 0 rows/matches, OR rows persisted
    # that never satisfy the phase contract; raw non-zero yield does NOT
    # reset the streak because mis-attributed rows look productive while
    # still missing the target), auto-run a full-page screenshot + VL against
    # a claim synthesized from the worker contract, persist the observation,
    # and attach it to the tool result. Task-type agnostic: the trigger is
    # the streak, not any validator kind. Default ON but still gated behind
    # vl.enabled (the master switch).
    reality_check_enabled: bool = True
    reality_check_shortfall_threshold: int = 3
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
            captcha_solve_max_retries=int(
                data.get("captcha_solve_max_retries", cls.captcha_solve_max_retries)
            ),
            captcha_solve_enabled=bool(
                data.get("captcha_solve_enabled", cls.captcha_solve_enabled)
            ),
            visual_locate_enabled=bool(
                data.get("visual_locate_enabled", cls.visual_locate_enabled)
            ),
            contract_verify_enabled=bool(
                data.get("contract_verify_enabled", cls.contract_verify_enabled)
            ),
            arbiter_enabled=bool(
                data.get("arbiter_enabled", cls.arbiter_enabled)
            ),
            reality_check_enabled=bool(
                data.get("reality_check_enabled", cls.reality_check_enabled)
            ),
            reality_check_shortfall_threshold=int(
                data.get(
                    "reality_check_shortfall_threshold",
                    # Accept the short-lived original key name too.
                    data.get(
                        "reality_check_zero_yield_threshold",
                        cls.reality_check_shortfall_threshold,
                    ),
                )
            ),
            extra_params=(
                data.get("extra_params")
                if isinstance(data.get("extra_params"), dict)
                else {}
            ),
        )


# ---------------------------------------------------------------------------
# "harness" 段：HarnessConfig（编排与运行时行为）
# ---------------------------------------------------------------------------

@dataclass
class HarnessConfig:
    mode: str = "lead"
    max_steps: int = 40
    lead_max_steps: int = 40
    worker_max_steps: int = 40
    max_browser_agent_instances: int = 3
    max_browser_agents: int = 3
    worktree_dir: str = "worktree"
    runs_dir: str = "runs"
    context_file: Optional[str] = None
    strategy_bank_path: str = "strategy_bank/strategy_bank.json"
    memory_context: str = (
        "ABCP agent harness: drive the browser only through ABCP atomic capabilities. "
        "Trust the cached System.describeAction schemas (global_schema_cache/schemas/<Method>.json) "
        "as the source of truth for parameters; descriptions live in System.getCapabilities."
    )
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
    cache_pressure_uncached_input_threshold: int = 10000
    cache_pressure_consecutive_steps: int = 2
    cache_pressure_min_remaining_steps: int = 2
    lead_model_timeout_step_retries: int = 1
    log_browser_payloads: bool = True
    # Try a matching skill's frozen Workflow.execute fast path before the worker
    # LLM loop (skill_registry.match → run_skill_workflow → success_contract →
    # record_extraction). Fires only when a skill matches AND its required vars
    # are derivable; otherwise falls through to the normal BrowserAgent loop.
    skill_fast_path_enabled: bool = True
    # How a skill gets selected for a run. "manual" (default, 2026-07-06 user
    # decision): ONLY an explicit user choice (`--skill <id>` / `/skill <id>` →
    # forced_skill_id, or an explicit worker_contract.skill_id) engages a skill —
    # registry auto-match, the LeadAgent skill_selection_required gate, and
    # enrich auto-stamping are all disabled, so an uncalibrated draft can never
    # steal execution. "auto": restore the pre-07-06 behavior (deterministic
    # unique match + Lead selection gate).
    skill_selection_mode: str = "manual"
    # Runtime-only operator override (NOT read from config.json): set per run from
    # the terminal via `--skill <id>` or the interactive `/skill <id>` command,
    # which main.run_cli writes here. When set, it forces that skill for every
    # browser worker spawn, bypassing auto-match, LeadAgent selection, and decline;
    # a phase whose required variables are not derivable falls back to the normal
    # loop (fail-safe). Empty = off.
    forced_skill_id: str = ""
    # When the skill fast path falls back AND the BrowserAgent slow path then
    # succeeds for a degraded (recently-failed) skill, distill the successful
    # trace into a candidate workflow and run skill_heal (write candidate →
    # canary → promote). Closes the rotted-skill self-healing loop; best-effort,
    # gated, and canary-validated so a bad candidate never promotes.
    skill_auto_heal_enabled: bool = True
    # Guidance (hints) 层的防腐弱信号：worker 结束后把「结局 + 步数 + agent 上报
    # 的 guidance_stale」记进 skills/.guidance_health.json（独立软通道——显式
    # 选择绕过 .skill_health.json，07-07 语义保持）。只标 needs_review 供人工
    # 复审（/skill-create --recheck），永不禁用/否决。纯被动记账，默认开。
    skill_guidance_signal_enabled: bool = True
    # Open a SECOND ABCP connection (control channel) so the harness can issue
    # control calls (Workflow.pause/resume, Hitl.*) WHILE the primary connection is
    # blocked inside a skill's Workflow.execute — the single primary _call_lock makes
    # in-band control impossible. When a challenge/pause is observed mid-execute, the
    # control channel actively pauses → resolves (human/VL) → resumes the workflow,
    # so it finishes its remaining steps instead of handing off. Default OFF:
    # cross-connection runId/page reachability is panel-unverified; any control
    # failure degrades to the observe-only hand-off (skill_pause). Flip on once the
    # panel confirms cross-connection control works.
    skill_workflow_active_control_enabled: bool = False
    hitl_poll_interval_seconds: float = 2.0
    hitl_wait_timeout_seconds: float = 1200.0
    hitl_no_repause_cooldown_seconds: float = 8.0
    hitl_post_resume_guard_seconds: float = 30.0
    hitl_post_resume_confirm_max_rounds: int = 3
    progress_local_fs_without_extraction_limit: int = 5
    progress_no_artifact_limit: int = 8
    # Browser-side stale-id rematch policy:
    #   "off"            -> stale guard blocks every stale id (legacy behavior)
    #   "composite_only" -> only harness composite tools may pass previously
    #                       seen stale ids through to the browser rematch
    #   "on"             -> model-initiated calls may pass them through too
    browser_side_rematch: str = "composite_only"
    # Auto-intercept policy for overlay occlusion (Phase 7.2). When a browser
    # action is blocked by an overlay, how aggressively the harness handles it:
    #   "off"     -> no hint, no auto-run (legacy: model sees the raw error)
    #   "suggest" -> attach a dismiss_overlay runtimeStrategy hint only
    #   "p0"      -> auto-run dismiss_overlay on P0 (errorClassification
    #                occlusion_blocked); P1/P2/P3 stay suggest-only
    #   "p0p1"    -> also auto-run on P1 (an AXTree layer reports
    #                occlusionState=occluded); P2 (text soft-detect) / P3
    #                (observation keywords) remain suggest-only because soft
    #                text signals have false positives (a cookies article hits
    #                "we use cookies") and auto-clicking them is unacceptable.
    auto_intercept: str = "p0p1"
    # DOM.getSemanticTree usage policy (Phase B). The MODEL may now call it
    # directly as a diagnostic (un-banned in tool_policy; the model prompt limits
    # it to local diagnostics when AXTree is insufficient). It is still 3.65x
    # heavier than AXTree with no href/name/aria, so its results are offloaded.
    # This flag is independent of the model surface and only governs the
    # HARNESS-INTERNAL auto-digest path:
    #   "off"      -> never used, even internally (current safe default)
    #   "internal" -> harness may make a one-shot, render_recovery-wrapped call to
    #                 derive a tiny structure digest (scroll containers via
    #                 isScrollable, bounds) that never enters model context. The
    #                 raw tree is digested and discarded; never per-iteration.
    # NOTE: shadow-host mapping is NOT supported (getSemanticTree does not
    # traverse shadow roots on this build — see abcp-panel-quirks #8).
    semantic_tree: str = "off"
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
            worktree_dir=data.get("worktree_dir", cls.worktree_dir),
            runs_dir=data.get("runs_dir", cls.runs_dir),
            context_file=data.get("context_file"),
            strategy_bank_path=data.get(
                "strategy_bank_path",
                cls.strategy_bank_path,
            ),
            memory_context=data.get("memory_context", cls.memory_context),
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
            cache_pressure_uncached_input_threshold=int(
                data.get(
                    "cache_pressure_uncached_input_threshold",
                    cls.cache_pressure_uncached_input_threshold,
                )
            ),
            cache_pressure_consecutive_steps=int(
                data.get(
                    "cache_pressure_consecutive_steps",
                    cls.cache_pressure_consecutive_steps,
                )
            ),
            cache_pressure_min_remaining_steps=int(
                data.get(
                    "cache_pressure_min_remaining_steps",
                    cls.cache_pressure_min_remaining_steps,
                )
            ),
            lead_model_timeout_step_retries=int(
                data.get(
                    "lead_model_timeout_step_retries",
                    cls.lead_model_timeout_step_retries,
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
            skill_auto_heal_enabled=bool(
                data.get("skill_auto_heal_enabled", cls.skill_auto_heal_enabled)
            ),
            skill_guidance_signal_enabled=bool(
                data.get(
                    "skill_guidance_signal_enabled",
                    cls.skill_guidance_signal_enabled,
                )
            ),
            skill_workflow_active_control_enabled=bool(
                data.get("skill_workflow_active_control_enabled",
                         cls.skill_workflow_active_control_enabled)
            ),
            skill_fast_path_enabled=bool(
                data.get("skill_fast_path_enabled", cls.skill_fast_path_enabled)
            ),
            skill_selection_mode=_normalize_selection_mode(
                data.get("skill_selection_mode", cls.skill_selection_mode)
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
            browser_side_rematch=(
                str(data.get("browser_side_rematch", cls.browser_side_rematch))
                if str(data.get("browser_side_rematch", cls.browser_side_rematch))
                in {"off", "composite_only", "on"}
                else cls.browser_side_rematch
            ),
            auto_intercept=(
                str(data.get("auto_intercept", cls.auto_intercept))
                if str(data.get("auto_intercept", cls.auto_intercept))
                in {"off", "suggest", "p0", "p0p1"}
                else cls.auto_intercept
            ),
            semantic_tree=(
                str(data.get("semantic_tree", cls.semantic_tree))
                if str(data.get("semantic_tree", cls.semantic_tree))
                in {"off", "internal"}
                else cls.semantic_tree
            ),
        )


@dataclass
class RuntimeConfig:
    agent_id: str
    model: ModelConfig
    browser: ABCPClientConfig
    harness: HarnessConfig


# ---------------------------------------------------------------------------
# 未知字段审计 + 装载入口
# ---------------------------------------------------------------------------

# 顶层除 ModelConfig 字段外还认识的键（env 间接键 + 三个子段 + 顶层 agent_id 兜底）。
_TOP_LEVEL_EXTRA_KEYS = {
    "api_key_env",
    "base_url_env",
    "agent_id",
    "vl",
    "browser",
    "harness",
}
# browser 段里 agent_id 由 load_runtime_config 直接读取（不属于 ABCPClientConfig）。
_BROWSER_EXTRA_KEYS = {"agent_id"}
# vl 段接受的历史别名（VLConfig.from_dict 里兼容读取）。
_VL_ALIAS_KEYS = {"reality_check_zero_yield_threshold"}


def _field_names(cls: type) -> set:
    return {f.name for f in fields(cls)}


def audit_config_keys(raw: JsonDict) -> List[str]:
    """比对 config 各段的键与配置表字段，返回人话告警（每项一行，空列表=干净）。"""
    if not isinstance(raw, dict):
        return []
    warnings: List[str] = []

    def check(section_name: str, section: Any, known: set) -> None:
        if not isinstance(section, dict):
            return
        unknown = sorted(k for k in section if k not in known)
        if unknown:
            prefix = f"{section_name}." if section_name else ""
            names = "、".join(prefix + k for k in unknown)
            warnings.append(f"有不认识的字段（写了也不会生效，已忽略）: {names}")

    check("", raw, _field_names(ModelConfig) | _TOP_LEVEL_EXTRA_KEYS)
    check(
        "browser",
        raw.get("browser"),
        _field_names(ABCPClientConfig) | _BROWSER_EXTRA_KEYS,
    )
    # runtime-only 字段不算“不认识”，下面单独给更准确的专属提示。
    check("harness", raw.get("harness"), _field_names(HarnessConfig))
    check("vl", raw.get("vl"), _field_names(VLConfig) | _VL_ALIAS_KEYS)

    harness_raw = raw.get("harness")
    if isinstance(harness_raw, dict):
        if "forced_skill_id" in harness_raw:
            warnings.append(
                "harness.forced_skill_id 只在运行时通过 --skill / /skill 设置，"
                "写在 config 里不生效"
            )
        if "vl" in harness_raw:
            warnings.append("vl 配置要放在 config 顶层，放在 harness.vl 里不生效")
    return warnings


def load_runtime_config(config_path: str, *, warn: bool = True) -> RuntimeConfig:
    path = Path(config_path)
    raw: JsonDict = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))

    if warn:
        for line in audit_config_keys(raw):
            print(f"[config] {path.name} {line}", file=sys.stderr)

    model = ModelConfig.load_from_file(config_path)
    browser_raw = raw.get("browser", {})
    harness_raw = raw.get("harness", {})

    harness = HarnessConfig.from_dict(harness_raw)
    # VL is configured only at the top level of config.json:
    # {"vl": {"enabled": true, "provider": "openai", ...}}
    harness.vl = VLConfig.from_dict(raw.get("vl", {}))

    return RuntimeConfig(
        agent_id=browser_raw.get("agent_id") or raw.get("agent_id", "abcp-agent"),
        model=model,
        browser=ABCPClientConfig.from_dict(browser_raw),
        harness=harness,
    )
