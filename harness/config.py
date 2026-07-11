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


def _normalize_selection_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in ("manual", "auto") else "manual"


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
