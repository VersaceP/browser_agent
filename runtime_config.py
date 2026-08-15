"""
runtime_config.py - 全项目统一配置表（single source of truth）。

config.json 的四张配置表全部定义在这一个文件里：

    顶层             -> ModelConfig       （LLM 连接：provider/model_id/api_key/超时重试/extra_params）
    "lead"/"worker"         -> RoleModelConfig     （角色级模型覆盖：可换厂商/模型，extra_params 浅合并）
    "vl": {...}             -> VLConfig            （视觉模型连接 + 各 VL 角色开关）
    "plan_validator": {...} -> PlanValidatorConfig （独立计划审计模型）
    "browser": {...}        -> ABCPClientConfig    （ABCP WebSocket 连接）
    "harness": {...}        -> HarnessConfig       （编排/步数预算/offload/HITL/skill 等运行时行为）

历史位置 llm/config.py、abcp_client.py、harness/config.py 仍从这里 re-export，
旧 import 路径全部兼容。load_runtime_config() 是唯一装载入口；装载时对
config.json 里不认识的字段打印告警，不再静默吞掉写了也不生效的键。

本模块只依赖标准库（不 import 项目内任何模块），避免循环依赖。
"""

import json
import os
import sys
from dataclasses import dataclass, field, fields, replace
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


HITL_ATTENDED = "attended"
HITL_UNATTENDED = "unattended"


def _validated_storage_backend(value: Any) -> str:
    backend = str(value or "").strip().lower()
    if backend not in {"file", "dual", "db"}:
        raise ValueError(
            f"harness.storage_backend must be 'file', 'dual' or 'db'; got {value!r}"
        )
    return backend


def _validated_resource_compression(value: Any) -> str:
    """Fail-fast like the backend switch: "none" is the rollback path.

    A typo silently falling through to either value changes what every
    future row looks like, so it must be rejected rather than guessed.
    """

    mode = str(value or "").strip().lower()
    if mode not in {"none", "zlib"}:
        raise ValueError(
            f"harness.resource_compression must be 'none' or 'zlib'; got {value!r}"
        )
    return mode


def _validated_resource_compression_min_bytes(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "harness.resource_compression_min_bytes must be a non-negative integer;"
            f" got {value!r}"
        ) from exc
    if parsed < 0:
        raise ValueError(
            "harness.resource_compression_min_bytes must be a non-negative integer;"
            f" got {value!r}"
        )
    return parsed


def _validated_resource_compression_level(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "harness.resource_compression_level must be an integer from 0 to 9;"
            f" got {value!r}"
        ) from exc
    if not 0 <= parsed <= 9:
        raise ValueError(
            "harness.resource_compression_level must be an integer from 0 to 9;"
            f" got {value!r}"
        )
    return parsed


def _normalize_hitl_attendance(value: Any) -> str:
    """Unknown spellings fall back to `attended`.

    Deliberately fail-safe rather than fail-fast: mis-reading a typo as
    `unattended` would silently stop asking a human who IS there, which loses
    work no retry can recover.
    """
    mode = str(value or "").strip().lower()
    return mode if mode in (HITL_ATTENDED, HITL_UNATTENDED) else HITL_ATTENDED


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
        provider_value = data.get("provider")
        if not isinstance(provider_value, str) or not provider_value.strip():
            raise ValueError(
                "顶层 provider 为必填字段，必须显式设置为"
                " 'anthropic' 或 'openai'"
            )
        provider = provider_value.strip().lower()
        if provider not in {"anthropic", "openai"}:
            raise ValueError(
                "顶层 provider 仅支持 'anthropic' 或 'openai'，"
                f"当前值: {provider_value!r}"
            )
        extra_params = (
            data.get("extra_params")
            if isinstance(data.get("extra_params"), dict)
            else {}
        )

        def model_value(key: str, default: Any) -> Any:
            return data.get(key, extra_params.get(key, default))

        return cls(
            provider=provider,
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
# "lead" / "worker" 段：角色级模型覆盖
# ---------------------------------------------------------------------------

@dataclass
class RoleModelConfig:
    """Per-role override of the top-level model, for the lead and the workers.

    Every field is optional and falls back to the top-level ModelConfig, so a
    section can be as small as one key. Two different merge rules, on purpose:

      - scalars (provider/model_id/api_key/base_url/timeouts) *replace* —
        a role can run on an entirely different vendor;
      - ``extra_params`` *shallow-merges*, so setting one knob (thinking,
        temperature, ...) does not wipe the rest of the top-level params.

    Switching ``provider`` without also giving that vendor's ``base_url`` /
    ``api_key`` inherits the other vendor's credentials, which fails at call
    time; audit_config_keys warns about exactly that.
    """

    provider: Optional[str] = None
    model_id: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    llm_api_timeout_seconds: Optional[float] = None
    llm_timeout_max_retries: Optional[int] = None
    llm_timeout_backoff_seconds: Optional[float] = None
    llm_timeout_retry_interval_seconds: Optional[float] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "RoleModelConfig":
        if not isinstance(data, dict):
            return cls()
        api_key_env = str(data.get("api_key_env") or "").strip() or None
        base_url_env = str(data.get("base_url_env") or "").strip() or None
        provider = data.get("provider")
        if isinstance(provider, str) and provider.strip():
            provider = provider.strip().lower()
            if provider not in {"anthropic", "openai"}:
                raise ValueError(
                    "角色段 provider 仅支持 'anthropic' 或 'openai'，"
                    f"当前值: {data.get('provider')!r}"
                )
        else:
            provider = None
        return cls(
            provider=provider,
            model_id=(str(data["model_id"]).strip() if data.get("model_id") else None),
            api_key=(
                data.get("api_key")
                or (os.environ.get(api_key_env) if api_key_env else None)
            ),
            api_key_env=api_key_env,
            base_url=(
                data.get("base_url")
                or (os.environ.get(base_url_env) if base_url_env else None)
            ),
            base_url_env=base_url_env,
            llm_api_timeout_seconds=_optional_float_config(
                data.get("llm_api_timeout_seconds"), minimum=1.0
            ),
            llm_timeout_max_retries=(
                _int_config(data.get("llm_timeout_max_retries"), 0, minimum=0, maximum=10)
                if data.get("llm_timeout_max_retries") is not None
                else None
            ),
            llm_timeout_backoff_seconds=_optional_float_config(
                data.get("llm_timeout_backoff_seconds"), minimum=0.0
            ),
            llm_timeout_retry_interval_seconds=_optional_float_config(
                data.get("llm_timeout_retry_interval_seconds"), minimum=0.0
            ),
            extra_params=(
                dict(data.get("extra_params"))
                if isinstance(data.get("extra_params"), dict)
                else {}
            ),
        )

    def apply_to(self, model: ModelConfig) -> ModelConfig:
        """Resolve this role's effective connection against the top-level model."""
        merged_extra_params = dict(model.extra_params or {})
        merged_extra_params.update(self.extra_params)
        overrides: Dict[str, Any] = {"extra_params": merged_extra_params}
        for name in (
            "provider",
            "model_id",
            "api_key",
            "base_url",
            "llm_api_timeout_seconds",
            "llm_timeout_max_retries",
            "llm_timeout_backoff_seconds",
            "llm_timeout_retry_interval_seconds",
        ):
            value = getattr(self, name)
            if value is not None:
                overrides[name] = value
        return replace(model, **overrides)


# 历史名（只覆盖 extra_params 时的旧叫法），保持旧 import 可用。
RoleOverrideConfig = RoleModelConfig


# ---------------------------------------------------------------------------
# "plan_validator" 段：独立计划审计模型
# ---------------------------------------------------------------------------

@dataclass
class PlanValidatorConfig:
    enabled: bool = False
    provider: str = "openai"
    model_id: str = ""
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    max_tokens: int = 8000
    llm_api_timeout_seconds: float = DEFAULT_LLM_API_TIMEOUT_SECONDS
    llm_timeout_max_retries: int = DEFAULT_LLM_TIMEOUT_MAX_RETRIES
    llm_timeout_backoff_seconds: float = DEFAULT_LLM_TIMEOUT_BACKOFF_SECONDS
    llm_timeout_retry_interval_seconds: Optional[float] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "PlanValidatorConfig":
        if not isinstance(data, dict):
            return cls()
        api_key_env = str(data.get("api_key_env") or "").strip() or None
        base_url_env = str(data.get("base_url_env") or "").strip() or None
        return cls(
            enabled=bool(data.get("enabled", cls.enabled)),
            provider=str(data.get("provider", cls.provider) or cls.provider),
            model_id=str(data.get("model_id", cls.model_id) or "").strip(),
            api_key=(
                data.get("api_key")
                or (os.environ.get(api_key_env) if api_key_env else None)
            ),
            api_key_env=api_key_env,
            base_url=(
                data.get("base_url")
                or (os.environ.get(base_url_env) if base_url_env else None)
            ),
            base_url_env=base_url_env,
            max_tokens=_int_config(
                data.get("max_tokens", cls.max_tokens),
                cls.max_tokens,
                minimum=256,
                maximum=65536,
            ),
            llm_api_timeout_seconds=_float_config(
                data.get(
                    "llm_api_timeout_seconds",
                    cls.llm_api_timeout_seconds,
                ),
                cls.llm_api_timeout_seconds,
                minimum=1.0,
            ),
            llm_timeout_max_retries=_int_config(
                data.get(
                    "llm_timeout_max_retries",
                    cls.llm_timeout_max_retries,
                ),
                cls.llm_timeout_max_retries,
                minimum=0,
                maximum=10,
            ),
            llm_timeout_backoff_seconds=_float_config(
                data.get(
                    "llm_timeout_backoff_seconds",
                    cls.llm_timeout_backoff_seconds,
                ),
                cls.llm_timeout_backoff_seconds,
                minimum=0.0,
            ),
            llm_timeout_retry_interval_seconds=_optional_float_config(
                data.get("llm_timeout_retry_interval_seconds"),
                minimum=0.0,
            ),
            extra_params=(
                dict(data.get("extra_params"))
                if isinstance(data.get("extra_params"), dict)
                else {}
            ),
        )

    def model_config(self) -> ModelConfig:
        extra_params = dict(self.extra_params)
        extra_params["max_tokens"] = int(self.max_tokens)
        extra_params["tool_choice"] = "required"
        extra_params.setdefault("temperature", 0)
        return ModelConfig(
            provider=self.provider,
            model_id=self.model_id,
            api_key=self.api_key,
            base_url=self.base_url,
            extra_params=extra_params,
            llm_api_timeout_seconds=self.llm_api_timeout_seconds,
            llm_timeout_max_retries=self.llm_timeout_max_retries,
            llm_timeout_backoff_seconds=self.llm_timeout_backoff_seconds,
            llm_timeout_retry_interval_seconds=(
                self.llm_timeout_retry_interval_seconds
            ),
        )


# ---------------------------------------------------------------------------
# "claim_extractor" 段：独立观测模型（把散文里的数量绑定到可重算的指标）
# ---------------------------------------------------------------------------


@dataclass
class ClaimExtractorConfig(PlanValidatorConfig):
    """Independent model that binds quantities in prose to checkable metrics.

    Same shape as the plan validator on purpose: both are read-only auditors
    that must not be the Lead model. Deciding whether a bound number is right
    is code's job (harness.numeric_facts); this model only says which metric a
    sentence is talking about. When this section is absent the LeadAgent falls
    back to the plan_validator provider rather than adding a second key.
    """

    # One short entry per enumerated span. 51 spans on a dense report needs
    # roughly 3k; the headroom is for reports with many more. Kept explicit
    # because inheriting the plan validator's budget once let a truncated
    # response return no tool call at all, which reads as "extractor
    # unavailable" and silently fails the gate open (task 857616aa).
    max_tokens: int = 16000

    @classmethod
    def derived_from(
        cls, validator: "PlanValidatorConfig",
    ) -> "ClaimExtractorConfig":
        """Reuse the auditor's model and credentials, not its thinking budget.

        Binding a number in prose to a metric is a lookup; judging whether a
        plan quietly dropped an objective is not. Inheriting the validator's
        provider object gave the extractor `effort=high` and a shared 12k
        output budget, and in task 5b91bd44 the extractor spent all of it
        thinking and returned no tool call at all — which reads as "extractor
        unavailable" and costs three minutes for nothing.
        """
        extra = {
            key: value
            for key, value in (validator.extra_params or {}).items()
            if key not in _REASONING_PARAM_KEYS
        }
        return cls(
            enabled=True,
            provider=validator.provider,
            model_id=validator.model_id,
            api_key=validator.api_key,
            base_url=validator.base_url,
            llm_api_timeout_seconds=validator.llm_api_timeout_seconds,
            llm_timeout_max_retries=validator.llm_timeout_max_retries,
            llm_timeout_backoff_seconds=validator.llm_timeout_backoff_seconds,
            llm_timeout_retry_interval_seconds=(
                validator.llm_timeout_retry_interval_seconds
            ),
            extra_params=extra,
        )


# Spellings different providers use for the same "spend tokens deliberating"
# switch. Stripped when a read-only lookup inherits an auditor's connection.
_REASONING_PARAM_KEYS = frozenset({
    "thinking",
    "reasoning",
    "reasoning_effort",
    "effort",
})


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

def _clamped_unit(value: Any, default: float) -> float:
    """Coerce a config value into [0, 1]; malformed input keeps the default so a
    typo can never silently disable a safety threshold."""
    if value is None:
        return max(0.0, min(float(default), 1.0))
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return max(0.0, min(float(default), 1.0))


def _one_of(value: Any, allowed: set, default: str) -> str:
    """Coerce a config value to one of `allowed`; anything else keeps the
    default, for the same reason as _clamped_unit — a typo must not decide a
    safety setting."""
    text = str(value if value is not None else default).strip().lower()
    return text if text in allowed else default


def _optional_positive_int(value: Any, *, name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer or null; got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer or null; got {value!r}")
    return parsed


@dataclass
class VLConfig:
    enabled: bool = False
    provider: str = "openai"
    model_id: str = ""
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    max_checks_per_worker: int = 2
    default_timeout_seconds: float = 60.0
    # captcha_solve: bounded solve-plan attempts before short-circuiting to HITL.
    # Behavioral-risk / unknown challenges are NEVER retried (honest short-circuit
    # in vl._finalize_captcha_solve); this only bounds the visual-self-consistent
    # solve loop.
    captcha_solve_max_retries: int = 3
    # A CAPTCHA grounding request is materially heavier than an ordinary visual
    # assertion.  Keep its provider timeout independent so routine visual checks
    # still fail fast.  Provider failure ends the episode; this is not a retry
    # budget for HTTP failures.
    captcha_solve_timeout_seconds: float = 150.0
    # Wall-clock ceiling for ONE auto-solve episode (screenshots + VL calls +
    # Input + clearance checks). The human is waiting behind this, so a stuck or
    # slow VL must not delay the HITL fall-back indefinitely.
    captcha_solve_budget_seconds: float = 240.0
    # Provider-specific CAPTCHA parameters (for example OpenAI-compatible
    # extra_body.enable_thinking) must not silently alter ordinary VL roles.
    captcha_solve_extra_params: Dict[str, Any] = field(default_factory=dict)
    # What this ENDPOINT accepts in one request body, not what some model can
    # understand. Declared per endpoint because it is a transport fact the
    # server states in its own error text; the harness must not keep a table of
    # model names and picture sizes. `None` means the endpoint has declared
    # nothing and every image is sent, which is the historical behaviour.
    max_encoded_image_bytes: Optional[int] = None
    # How many auto-solve episodes one worker may spend in total. A page whose
    # episode failed is additionally never retried — it belongs to the human path.
    captcha_solve_max_episodes_per_worker: int = 2
    # Minimum VL self-reported confidence to act on a solve plan, to accept a
    # "no challenge here" claim, or to accept a post-solve clearance verdict.
    # Anything below hands the page to a human instead. Clamped to [0, 1]; a
    # missing/malformed confidence counts as 0.
    captcha_solve_min_confidence: float = 0.8
    # OCR carries a higher bar: a misread string is submitted as a real answer
    # and burns one of the site's own attempts, whereas a misplaced drag usually
    # just fails to move the puzzle.
    captcha_solve_min_confidence_ocr: float = 0.9
    # Auto-solve gate, ANDed with `enabled` (operator decision, 2026-08-05,
    # superseding the 2026-07-31 "one switch for every role" decision). Both
    # must be true: `enabled` stays the master VL switch, and this turns the
    # CAPTCHA role off without giving up the other VL roles. Default True so an
    # existing deployment keeps today's behaviour (VL on ⇒ auto-solve on).
    # Read it through `captcha_autosolve_allowed()`, never on its own.
    # The other independent controls are the per-skill `allow_auto_captcha`
    # frontmatter flag, the confidence floors, and the budgets above.
    captcha_solve_enabled: bool = True
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
    # Second, independent arming condition: tool calls made without persisting
    # anything (ProgressAccountant.turns_since_artifact_progress). The
    # shortfall streak only counts tools that report a row yield, so a worker
    # looping on DOM.getAXTree / DOM.getSemanticTree / local_fs_read spends its
    # whole budget with the streak at 0 and the check never arms — seen live in
    # task e3173b5b. Kept below PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT (30) so
    # the visual second opinion arrives while the worker can still act on it,
    # not after the harness has already forced it to finalize. 0 disables.
    reality_check_stall_turns: int = 15
    # How much a reality-check verdict counts. "advisory" (the default) means
    # the VL may direct further work but never closes anything: an absence
    # still has to discharge the mechanical obligations in harness.row_ledger,
    # and a verdict alone is not a reason to stop. Raise to "corroborating"
    # only after harness.vl.precision_eval reports the per-class thresholds
    # met on a labelled fixture — an unmeasured model asserting "nothing here"
    # is precisely how task 5324506f concluded that reviews needed a login.
    reality_check_evidence_mode: str = "advisory"
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def captcha_autosolve_allowed(self) -> bool:
        """The one place the CAPTCHA auto-solve gate is evaluated.

        Both switches must be on: `enabled` is the master VL gate, and
        `captcha_solve_enabled` scopes the auto-solve role alone. Call sites
        must not read either field directly, or the AND drifts.
        """
        return bool(self.enabled and self.captcha_solve_enabled)

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
            captcha_solve_enabled=bool(
                data.get("captcha_solve_enabled", cls.captcha_solve_enabled)
            ),
            captcha_solve_max_retries=int(
                data.get("captcha_solve_max_retries", cls.captcha_solve_max_retries)
            ),
            captcha_solve_timeout_seconds=float(
                data.get(
                    "captcha_solve_timeout_seconds",
                    cls.captcha_solve_timeout_seconds,
                )
            ),
            captcha_solve_budget_seconds=float(
                data.get(
                    "captcha_solve_budget_seconds",
                    cls.captcha_solve_budget_seconds,
                )
            ),
            captcha_solve_extra_params=(
                dict(data.get("captcha_solve_extra_params") or {})
                if isinstance(data.get("captcha_solve_extra_params"), dict)
                else {}
            ),
            max_encoded_image_bytes=_optional_positive_int(
                data.get("max_encoded_image_bytes", cls.max_encoded_image_bytes),
                name="vl.max_encoded_image_bytes",
            ),
            captcha_solve_max_episodes_per_worker=int(
                data.get(
                    "captcha_solve_max_episodes_per_worker",
                    cls.captcha_solve_max_episodes_per_worker,
                )
            ),
            captcha_solve_min_confidence=_clamped_unit(
                data.get("captcha_solve_min_confidence"),
                cls.captcha_solve_min_confidence,
            ),
            captcha_solve_min_confidence_ocr=_clamped_unit(
                data.get("captcha_solve_min_confidence_ocr"),
                cls.captcha_solve_min_confidence_ocr,
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
            reality_check_stall_turns=int(
                data.get(
                    "reality_check_stall_turns",
                    cls.reality_check_stall_turns,
                )
            ),
            reality_check_evidence_mode=_one_of(
                data.get("reality_check_evidence_mode"),
                {"advisory", "corroborating"},
                cls.reality_check_evidence_mode,
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
    max_steps: int = 40
    lead_max_steps: int = 40
    worker_max_steps: int = 40
    max_browser_agent_instances: int = 3
    max_browser_agents: int = 3
    # Deterministic fleet routing.  When enabled, the spawner assigns every
    # BrowserAgent a Dispatcher-observed fleet before browser work begins and
    # the browser-tool boundary injects that fleetId into fleetless Page.create
    # calls.  Unknown/cross-assignment fleet ids fail closed instead of letting
    # the Dispatcher silently create or adopt a fleet.
    fleet_reuse_enabled: bool = True
    # Allow different BrowserAgent slots in one task to use distinct pages in
    # the same coordinator-owned fleet. The owner socket remains connected and
    # its notifications are relayed in-process to delegated workers.
    same_fleet_multiworker_enabled: bool = False
    # Give every worker its own Fleet — its own cookie jar — unless its contract
    # explicitly asks to share (session_key / fleet_id / reuse_from_worker_id /
    # an explicit needs_isolated_session).
    #
    # Turning `same_fleet_multiworker_enabled` off is NOT equivalent: that only
    # drops the cross-slot task group, while two workers on one slot still
    # converge on that slot's fleet via the eligible/slot_default fallback in
    # FleetCoordinator.choose_existing.
    #
    # What this buys and what it does not: in task 48b4d7d7 three workers hit
    # 1688 detail pages within 16 seconds from ONE cookie jar and all three were
    # bounced, then one CAPTCHA froze the whole fleet for 84 minutes. Isolation
    # removes the shared-fate half. It does NOT stop the site correlating them,
    # because Fleet.setProxy / Fleet.setFppPolicy are unavailable here, so every
    # Fleet still shares one egress IP and fingerprint — pacing, not identity,
    # is the remaining lever.
    #
    # Cost: one Fleet is one browser instance.
    worker_session_isolation_enabled: bool = False
    # Upper bound on how many distinct Fleets ONE task may occupy (0 disables
    # the cap). The harness never closes a Fleet, so a fleet a worker creates
    # holds its budget slot until the platform stops reporting it: task
    # 7a8d72db opened seven browser instances for seven sequential workers
    # because per-worker isolation asks for a fresh cookie jar every time and
    # nothing counted the total.
    #
    # Counted over the fleets actually bound to this task's workers, never over
    # Fleet.list — that inventory is Agent-global and includes other tasks. A
    # fleet that vanishes from the authoritative owner inventory stops counting,
    # so the budget can be released as well as spent.
    # An explicitly selected fleet (pinned browser context, worker_contract
    # .fleet_id, a bound session_key, reuse_from_worker_id) is always honored
    # and is never blocked by the cap; it does consume budget, so fewer fresh
    # fleets remain for the rest of the task.
    #
    # At the cap the coordinator reuses one of the task's existing idle fleets
    # instead of creating another. A request carrying a real identity boundary
    # — an explicitly declared needs_isolated_session, or a new session_key —
    # fails closed with task_fleet_limit_reached instead of quietly sharing a
    # cookie jar; deployment-default isolation is not an identity boundary and
    # degrades to reuse. Reuse prefers a fleet no running worker holds but will
    # still share a busy one — ordinary routing already puts two live workers in
    # one fleet, so the cap must not be stricter than the rule it degrades from.
    max_task_fleets: int = 3
    # Hold BrowserAgent construction until the coordinator-assigned Fleet has
    # answered Fleet.status successfully. Fleet.ready is only a wake-up signal;
    # the barrier always confirms readiness with an authoritative RPC. This is
    # a soft signal-wait budget, not a total wall-clock timeout: up to two
    # uncancellable ABCP status calls may extend the observed elapsed time.
    fleet_readiness_barrier_enabled: bool = True
    fleet_readiness_wait_seconds: float = 45.0
    fleet_auth_barrier_enabled: bool = True
    fleet_auth_barrier_wait_seconds: float = 120.0
    # A quarantined page leaves the assignable pool until Page.getState proves
    # it usable again. When the platform keeps reporting `paused` for a page
    # whose challenge is actually over (the page_settled_after_hitl shape),
    # that proof never arrives and the page leaks: still open, still holding
    # fleet capacity, assignable to nobody. After this TTL the registry sync
    # stops re-quarantining such a page and retires it instead, so a fresh one
    # can be created. 0 disables retirement and restores indefinite quarantine.
    page_quarantine_ttl_seconds: float = 300.0
    # Consecutive failed re-checks (Page.getState itself raising) tolerated
    # before an expired quarantine is retired without a clean verdict.
    page_quarantine_recheck_max_failures: int = 2
    # Page ownership and opaque Workflow exclusion never wait silently forever.
    # A timed-out waiter receives a retryable fleet_busy receipt; the owning
    # call is not cancelled.
    page_lease_wait_timeout_seconds: float = 30.0
    # Enforced by default. This is an operator emergency escape hatch, not a
    # routing mode: disabling it removes process-local click serialization for
    # workers sharing one Fleet and is therefore deliberately noisy.
    fleet_click_gate_enabled: bool = True
    # Process-local FleetClickGate. A waiter never blocks indefinitely behind
    # ordinary lock contention. Opaque Workflow HITL is transferred to the
    # FleetAuthBarrier instead; this longer bound is only a final backstop.
    fleet_click_gate_acquire_timeout_seconds: float = 30.0
    # Link/unknown targets retain the conservative popup settlement window.
    fleet_click_gate_navigation_settlement_seconds: float = 0.75
    # A fresh AX target that is mechanically known not to be a link gets a
    # shorter observation window. The Fleet lock still covers the full call.
    fleet_click_gate_non_link_settlement_seconds: float = 0.10
    # A submitting key press (Enter in a search box) round-trips to the server
    # before its result page exists, so it needs a longer observation window
    # than a click. This bounds the Fleet lock only; a popup that lands after
    # the window is still adopted from the Page.open notification stream.
    fleet_click_gate_submit_settlement_seconds: float = 2.5
    # Recently dispatched clicks leave a non-locking tombstone so a late popup
    # or same-page navigation cannot be attributed to the next click.
    fleet_click_gate_late_guard_seconds: float = 5.0
    # Whether the click gate may REPORT that opener-compatible pages appeared
    # during its window. It is an observation, never attribution: the gate no
    # longer emits a confirmed landing page, because same-opener + same-
    # sourceUrl + single-candidate is equally satisfied by a page that opens an
    # ad on a timer. Turning it off only silences the observation; page
    # discovery still runs through Page.list plus an atomic lease claim.
    fleet_click_gate_popup_inventory_observation_enabled: bool = True
    # ABCP can deliver an opaque Workflow HITL control event after the action
    # RPC has already failed. Keep only its owner provenance longer; this does
    # not retain the Fleet lock or delay another command.
    fleet_click_gate_workflow_hitl_late_guard_seconds: float = 15.0
    auth_fleet_ledger_path: str = ".auth_fleet_ledger.json"
    # A transport failure quarantines the slot, then reconnects with the same
    # agentId before the coordinator is allowed to tombstone its session fleets.
    fleet_slot_reconnect_attempts: int = 2
    fleet_slot_reconnect_backoff_seconds: float = 0.25
    fleet_slot_manual_reset_after_failures: int = 3
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
    # Subordinate future flag: try a matching skill's frozen Workflow.execute
    # fast path before the worker LLM loop. It has no effect while the master
    # workflow_execution_enabled switch is false.
    skill_fast_path_enabled: bool = True
    # How a skill gets selected for a run. "manual" (default, 2026-07-06 user
    # decision): ONLY an explicit user choice (`--skill <id>` / `/skill <id>` →
    # forced_skill_id, or an explicit worker_contract.skill_id) engages a skill —
    # registry auto-match, the LeadAgent skill_selection_required gate, and
    # enrich auto-stamping are all disabled, so an uncalibrated draft can never
    # steal execution. "auto": restore the pre-07-06 behavior (deterministic
    # unique match + Lead selection gate).
    skill_selection_mode: str = "manual"
    # Master control-plane gate for every Harness-owned Workflow.execute path.
    # Keep disabled until ABCP supports pre-armed action events plus dynamic
    # collection/state primitives required by portable hybrid skills. Workflow
    # skill markdown remains available as guidance while this is false.
    workflow_execution_enabled: bool = False
    # Runtime-only operator override (NOT read from config.json): set per run from
    # the terminal via `--skill <id>` or the interactive `/skill <id>` command,
    # which main.run_cli writes here. When set, it forces that skill for every
    # browser worker spawn, bypassing auto-match, LeadAgent selection, and decline;
    # a phase whose required variables are not derivable falls back to the normal
    # loop (fail-safe). Empty = off.
    forced_skill_id: str = ""
    # When the enabled skill fast path falls back AND the BrowserAgent slow path then
    # succeeds for a degraded (recently-failed) skill, distill the successful
    # trace into a candidate workflow and run skill_heal (write candidate →
    # canary → promote). Closes the rotted-skill self-healing loop; best-effort,
    # gated, and canary-validated so a bad candidate never promotes.
    # It is also subordinate to workflow_execution_enabled.
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
    # Whether a human is actually reachable for this deployment. There is no
    # platform signal to infer it from (ABCP exposes only Hitl.requestPause /
    # Hitl.resolvePause, nothing about operator presence), so it is declared.
    #   attended   - pause and wait; the budgets below apply.
    #   unattended - nobody will answer, so do not pause at all: hand back a
    #                terminal needs_human verdict immediately and leave the
    #                fleet gate untouched.
    # Task 48b4d7d7 spent 82 of its 128 minutes waiting for a human who was
    # never there, and the wait held the whole fleet's auth barrier shut.
    hitl_attendance: str = "attended"
    hitl_wait_timeout_seconds: float = 900.0
    # Cumulative pause budget (attended only). `hitl_post_resume_confirm_max_rounds`
    # bounds rounds WITHIN one tool call; these bound them across calls, which is
    # what browser-004 escaped by re-pausing four times from four separate calls.
    hitl_max_pause_rounds_per_page: int = 3
    hitl_max_pause_rounds_per_worker: int = 3
    hitl_no_repause_cooldown_seconds: float = 8.0
    hitl_post_resume_guard_seconds: float = 30.0
    hitl_post_resume_confirm_max_rounds: int = 3
    # Event-driven page settlement gate. DOM probes wait for Page.loaded (or a
    # terminal lifecycle event); on timeout the dispatcher performs exactly one
    # Page.getState resynchronization and never polls.
    page_settlement_timeout_seconds: float = 15.0
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

    # Where task process data (events, traces, state, offloaded resources) is
    # written.
    #   "file" -> the historical per-task JSONL/JSON layout
    #   "dual" -> both, files authoritative, backends compared on demand
    #   "db"   -> SQLite only
    # "db" writes no process files at all and makes the database authoritative;
    # it never falls back to adjacent legacy files during resume. In "dual",
    # files remain authoritative while the database is verified as a mirror.
    # Model-facing readers present database events/traces/resources under the
    # same logical paths the historical file backend used.
    storage_backend: str = "db"
    # Relative to worktree_dir, so it follows a relocated worktree.
    storage_sqlite_path: str = "harness.db"
    storage_dual_verify: bool = True
    storage_busy_timeout_ms: int = 5000

    # Physical-layer compression for the four harness-internal bulk resource
    # types (event_payload / observation / tool_result / context_compaction).
    # Purely physical: byte_size and sha256 stay logical, extraction and
    # every operator-facing table stays readable in place, and "none" writes
    # rows byte-identical to the pre-compression shape - the rollback switch.
    resource_compression: str = "zlib"
    # Logical bytes below this stay uncompressed: measured on live data the
    # threshold costs 0.7 MB of the 71 MB saved while keeping 82 small rows
    # readable in place.
    resource_compression_min_bytes: int = 16384
    resource_compression_level: int = 6

    vl: VLConfig = field(default_factory=VLConfig)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "HarnessConfig":
        return cls(
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
            fleet_reuse_enabled=bool(
                data.get("fleet_reuse_enabled", cls.fleet_reuse_enabled)
            ),
            same_fleet_multiworker_enabled=bool(
                data.get(
                    "same_fleet_multiworker_enabled",
                    cls.same_fleet_multiworker_enabled,
                )
            ),
            worker_session_isolation_enabled=bool(
                data.get(
                    "worker_session_isolation_enabled",
                    cls.worker_session_isolation_enabled,
                )
            ),
            max_task_fleets=max(
                0,
                int(data.get("max_task_fleets", cls.max_task_fleets)),
            ),
            fleet_readiness_barrier_enabled=bool(
                data.get(
                    "fleet_readiness_barrier_enabled",
                    cls.fleet_readiness_barrier_enabled,
                )
            ),
            fleet_readiness_wait_seconds=max(
                0.01,
                float(data.get(
                    "fleet_readiness_wait_seconds",
                    cls.fleet_readiness_wait_seconds,
                )),
            ),
            fleet_auth_barrier_enabled=bool(
                data.get(
                    "fleet_auth_barrier_enabled",
                    cls.fleet_auth_barrier_enabled,
                )
            ),
            fleet_auth_barrier_wait_seconds=max(
                0.01,
                float(
                    data.get(
                        "fleet_auth_barrier_wait_seconds",
                        cls.fleet_auth_barrier_wait_seconds,
                    )
                ),
            ),
            page_quarantine_ttl_seconds=max(
                0.0,
                float(
                    data.get(
                        "page_quarantine_ttl_seconds",
                        cls.page_quarantine_ttl_seconds,
                    )
                ),
            ),
            page_quarantine_recheck_max_failures=max(
                0,
                int(
                    data.get(
                        "page_quarantine_recheck_max_failures",
                        cls.page_quarantine_recheck_max_failures,
                    )
                ),
            ),
            page_lease_wait_timeout_seconds=max(
                0.01,
                float(data.get(
                    "page_lease_wait_timeout_seconds",
                    cls.page_lease_wait_timeout_seconds,
                )),
            ),
            fleet_click_gate_enabled=bool(
                data.get(
                    "fleet_click_gate_enabled",
                    cls.fleet_click_gate_enabled,
                )
            ),
            fleet_click_gate_acquire_timeout_seconds=max(
                0.01,
                float(data.get(
                    "fleet_click_gate_acquire_timeout_seconds",
                    cls.fleet_click_gate_acquire_timeout_seconds,
                )),
            ),
            fleet_click_gate_navigation_settlement_seconds=max(
                0.0,
                float(data.get(
                    "fleet_click_gate_navigation_settlement_seconds",
                    cls.fleet_click_gate_navigation_settlement_seconds,
                )),
            ),
            fleet_click_gate_non_link_settlement_seconds=max(
                0.0,
                float(data.get(
                    "fleet_click_gate_non_link_settlement_seconds",
                    cls.fleet_click_gate_non_link_settlement_seconds,
                )),
            ),
            fleet_click_gate_submit_settlement_seconds=max(
                0.0,
                float(data.get(
                    "fleet_click_gate_submit_settlement_seconds",
                    cls.fleet_click_gate_submit_settlement_seconds,
                )),
            ),
            fleet_click_gate_late_guard_seconds=max(
                0.0,
                float(data.get(
                    "fleet_click_gate_late_guard_seconds",
                    cls.fleet_click_gate_late_guard_seconds,
                )),
            ),
            fleet_click_gate_popup_inventory_observation_enabled=bool(
                data.get(
                    "fleet_click_gate_popup_inventory_observation_enabled",
                    data.get(
                        # Accept both pre-rename keys so an existing config that
                        # explicitly silenced this keeps doing so.
                        "fleet_click_gate_popup_inventory_attribution_enabled",
                        data.get(
                            "fleet_click_gate_legacy_popup_inventory_attribution_enabled",
                            cls.fleet_click_gate_popup_inventory_observation_enabled,
                        ),
                    ),
                )
            ),
            fleet_click_gate_workflow_hitl_late_guard_seconds=max(
                0.0,
                float(data.get(
                    "fleet_click_gate_workflow_hitl_late_guard_seconds",
                    cls.fleet_click_gate_workflow_hitl_late_guard_seconds,
                )),
            ),
            auth_fleet_ledger_path=str(
                data.get(
                    "auth_fleet_ledger_path",
                    cls.auth_fleet_ledger_path,
                )
            ),
            fleet_slot_reconnect_attempts=max(
                1,
                int(data.get(
                    "fleet_slot_reconnect_attempts",
                    cls.fleet_slot_reconnect_attempts,
                )),
            ),
            fleet_slot_reconnect_backoff_seconds=max(
                0.0,
                float(data.get(
                    "fleet_slot_reconnect_backoff_seconds",
                    cls.fleet_slot_reconnect_backoff_seconds,
                )),
            ),
            fleet_slot_manual_reset_after_failures=max(
                1,
                int(data.get(
                    "fleet_slot_manual_reset_after_failures",
                    cls.fleet_slot_manual_reset_after_failures,
                )),
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
            workflow_execution_enabled=bool(
                data.get(
                    "workflow_execution_enabled",
                    cls.workflow_execution_enabled,
                )
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
            hitl_attendance=_normalize_hitl_attendance(
                data.get("hitl_attendance", cls.hitl_attendance)
            ),
            hitl_max_pause_rounds_per_page=max(0, int(
                data.get(
                    "hitl_max_pause_rounds_per_page",
                    cls.hitl_max_pause_rounds_per_page,
                )
            )),
            hitl_max_pause_rounds_per_worker=max(0, int(
                data.get(
                    "hitl_max_pause_rounds_per_worker",
                    cls.hitl_max_pause_rounds_per_worker,
                )
            )),
            page_settlement_timeout_seconds=float(
                data.get(
                    "page_settlement_timeout_seconds",
                    cls.page_settlement_timeout_seconds,
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
            # Deliberately fail-fast, unlike most options here: this one
            # decides where a task's only copy of its data goes. Falling back
            # on a typo would send "duel" to the db backend and quietly stop
            # writing the files the operator thought they still had.
            storage_backend=_validated_storage_backend(
                data.get("storage_backend", cls.storage_backend)
            ),
            storage_sqlite_path=str(
                data.get("storage_sqlite_path", cls.storage_sqlite_path)
                or cls.storage_sqlite_path
            ),
            storage_dual_verify=bool(
                data.get("storage_dual_verify", cls.storage_dual_verify)
            ),
            storage_busy_timeout_ms=max(
                100,
                int(data.get("storage_busy_timeout_ms", cls.storage_busy_timeout_ms)),
            ),
            resource_compression=_validated_resource_compression(
                data.get("resource_compression", cls.resource_compression)
            ),
            resource_compression_min_bytes=_validated_resource_compression_min_bytes(
                data.get(
                    "resource_compression_min_bytes",
                    cls.resource_compression_min_bytes,
                )
            ),
            resource_compression_level=_validated_resource_compression_level(
                data.get(
                    "resource_compression_level",
                    cls.resource_compression_level,
                )
            ),
        )


@dataclass
class RuntimeConfig:
    agent_id: str
    model: ModelConfig
    browser: ABCPClientConfig
    harness: HarnessConfig
    plan_validator: PlanValidatorConfig = field(
        default_factory=PlanValidatorConfig
    )
    claim_extractor: ClaimExtractorConfig = field(
        default_factory=ClaimExtractorConfig
    )
    lead: RoleModelConfig = field(default_factory=RoleModelConfig)
    worker: RoleModelConfig = field(default_factory=RoleModelConfig)


# ---------------------------------------------------------------------------
# 未知字段审计 + 装载入口
# ---------------------------------------------------------------------------

# 顶层除 ModelConfig 字段外还认识的键（env 间接键 + 三个子段 + 顶层 agent_id 兜底）。
_TOP_LEVEL_EXTRA_KEYS = {
    "api_key_env",
    "base_url_env",
    "agent_id",
    "vl",
    "plan_validator",
    "claim_extractor",
    "lead",
    "worker",
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
    check(
        "plan_validator",
        raw.get("plan_validator"),
        _field_names(PlanValidatorConfig),
    )
    check(
        "claim_extractor",
        raw.get("claim_extractor"),
        _field_names(ClaimExtractorConfig),
    )
    top_provider = str(raw.get("provider") or "").strip().lower()
    for _role in ("lead", "worker"):
        section = raw.get(_role)
        check(_role, section, _field_names(RoleModelConfig))
        if not isinstance(section, dict):
            continue
        role_provider = str(section.get("provider") or "").strip().lower()
        if role_provider and top_provider and role_provider != top_provider:
            # Inheriting the other vendor's endpoint/key is never what anyone
            # means; it fails at call time with an opaque auth error.
            missing = [
                key
                for key in ("base_url", "api_key")
                if not section.get(key) and not section.get(f"{key}_env")
            ]
            if missing:
                warnings.append(
                    f"{_role}.provider 是 {role_provider}，与顶层 {top_provider} 不同，"
                    f"但没配 {'、'.join(f'{_role}.{k}' for k in missing)}，"
                    "会沿用顶层另一家厂商的连接（调用时才报错）"
                )

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
        plan_validator=PlanValidatorConfig.from_dict(
            raw.get("plan_validator", {})
        ),
        claim_extractor=ClaimExtractorConfig.from_dict(
            raw.get("claim_extractor", {})
        ),
        lead=RoleModelConfig.from_dict(raw.get("lead", {})),
        worker=RoleModelConfig.from_dict(raw.get("worker", {})),
    )
