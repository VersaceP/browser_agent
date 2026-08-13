"""
llm.thinking - Translate thinking/reasoning control across provider wire formats.

Three user-facing keys live in a role's ``extra_params`` and work for BOTH the
OpenAI-format and the Anthropic-format providers:

  - ``"thinking"``: the on/off switch.
      ``bool`` | ``"enabled"``/``"disabled"``/``"on"``/``"off"``/... |
      ``dict`` (passed through verbatim, e.g. ``{"type": "enabled"}`` for
      Ark/DeepSeek or ``{"type": "enabled", "budget_tokens": 8192}`` for
      Claude extended thinking).
  - ``"reasoning_effort"``: thinking depth.
      ``"none"`` | ``"minimal"`` | ``"low"`` | ``"medium"`` | ``"high"`` |
      ``"xhigh"`` | ``"max"``.
  - ``"effort"``: short alias for ``reasoning_effort`` (loses to it).

Wire shapes, and why
--------------------

OpenAI format (chat.completions):
    switch -> ``extra_body={"thinking": {"type": "enabled"/"disabled"}}``
              (`thinking` is a vendor extension, not an SDK kwarg, so it can
              only ride in extra_body — and it is emitted ONLY when the user
              actually set the key, because a real OpenAI endpoint rejects
              unknown body fields).
    effort -> top-level ``reasoning_effort=<value>`` (a native SDK kwarg).
              Values outside the SDK's Literal (`max`) are passed through:
              the SDK does not validate at runtime, and Ark documents that a
              level a model does not support simply "不生效" rather than
              erroring.

Anthropic format (messages):
    switch -> native ``thinking=`` kwarg. A dict is forwarded verbatim; the
              ``true`` shorthand becomes ``{"type": "enabled",
              "budget_tokens": N}`` because the official SDK marks
              ``budget_tokens`` as required (>=1024 and < max_tokens).
    effort -> native ``output_config={"effort": <level>}`` (SDK Literal:
              low|medium|high|xhigh|max).

Measured against Ark's Anthropic-compatible endpoint (glm-5.2 on
``https://ark.cn-beijing.volces.com/api/coding``, 2026-08-13):

    thinking={"type":"disabled"}                 -> thinking really turns OFF
    thinking={"type":"enabled"} (no budget)      -> accepted, thinking ON
    thinking={"type":"enabled","budget_tokens":} -> accepted, thinking ON
    output_config={"effort":"minimal"/"none"}    -> accepted, thinking stays ON
    extra_body {"reasoning":{"effort":"none"}}   -> accepted, thinking stays ON
    extra_body {"reasoning_effort":"none"}       -> accepted, thinking stays ON

i.e. on that endpoint ``thinking.type`` is the only lever that does anything;
every effort-shaped field is silently ignored. ``output_config`` is still the
right thing to send (it is the official SDK's effort parameter, honoured by
real Claude), but it must never be used as the on/off switch. Vendor
extensions this module does not know about — DeepSeek's Anthropic-format
``{"reasoning": {"effort": ...}}``, for one — go through the provider's
generic ``extra_params["extra_body"]`` passthrough instead of being guessed
here.

Nothing is synthesised: a key the user did not set produces no wire field.
The one thing this module does decide is contradictions — an explicit "off"
plus a thinking-on effort level drops the effort with a warning, because Ark's
Chat API documents that exact pair as an error.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


_THINKING_ENABLED_TOKENS = (
    "enabled", "enable", "on", "true", "yes", "1",
)
_THINKING_DISABLED_TOKENS = (
    "disabled", "disable", "off", "false", "no", "0",
)

# Ark/DeepSeek accept none|minimal|low|medium|high|xhigh|max; OpenAI's SDK
# Literal is none|minimal|low|medium|high|xhigh (no max). Both are unvalidated
# at runtime and a level the model does not support is documented as "不生效",
# so the union is accepted and forwarded verbatim - no per-model whitelist.
_VALID_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Effort levels that MEAN "no thinking" rather than "how much thinking".
_OFF_EFFORTS = ("none", "minimal")

# The Anthropic SDK's OutputConfigParam.effort Literal. "none"/"minimal" are
# not members: on that wire format they are expressed by the switch instead.
_ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ThinkingIntent:
    """Normalized thinking control derived from one role's extra_params."""

    # "enabled" | "disabled" | None (None = user did not touch the switch).
    state: Optional[str] = None
    # True only when the user supplied a full ``thinking`` dict.
    state_is_dict: bool = False
    # The raw dict the user supplied (when state_is_dict), for pass-through.
    state_dict: Optional[Dict[str, Any]] = None
    # Normalized effort string ("none"/"low"/...) or None.
    effort: Optional[str] = None
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def configured(self) -> bool:
        """True when any thinking control key was supplied."""
        return self.state is not None or self.effort is not None

    @property
    def enabled(self) -> bool:
        """Resolved on/off state (the switch wins over the effort level)."""
        if self.state == "enabled":
            return True
        if self.state == "disabled":
            return False
        if self.effort is not None:
            return self.effort not in _OFF_EFFORTS
        return False

    @property
    def switch_is_off(self) -> bool:
        """True when the user explicitly asked for thinking to be off."""
        if self.state is not None:
            return self.state == "disabled"
        return self.effort is not None and self.effort in _OFF_EFFORTS


def _normalize_thinking_value(
    raw: Any,
    warnings: List[str],
) -> Tuple[Optional[str], bool, Optional[Dict[str, Any]]]:
    """Return (state, state_is_dict, state_dict) for a raw ``thinking`` value."""
    if isinstance(raw, dict):
        state_dict = dict(raw)
        t_type = str(state_dict.get("type") or "").strip().lower()
        if t_type in _THINKING_ENABLED_TOKENS:
            return "enabled", True, state_dict
        if t_type in _THINKING_DISABLED_TOKENS:
            return "disabled", True, state_dict
        if t_type == "":
            # A budget_tokens/display-only dict is Claude-shaped and means on.
            if "budget_tokens" in state_dict or "display" in state_dict:
                return "enabled", True, state_dict
            warnings.append(
                "thinking dict is missing 'type'; passing through unchanged"
            )
            return None, True, state_dict
        # e.g. Ark's "auto" or Claude's "adaptive": the vendor knows this
        # spelling even though we do not - forward it and treat it as on.
        return "enabled", True, state_dict

    if isinstance(raw, bool):
        return ("enabled" if raw else "disabled"), False, None

    if raw is not None:
        token = str(raw).strip().lower()
        if token in _THINKING_ENABLED_TOKENS:
            return "enabled", False, None
        if token in _THINKING_DISABLED_TOKENS:
            return "disabled", False, None
        warnings.append(f"unknown thinking value={raw!r}; ignored")
    return None, False, None


def _normalize_effort(raw: Any, warnings: List[str]) -> Optional[str]:
    if raw is None:
        return None
    token = str(raw).strip().lower()
    if token in _VALID_EFFORTS:
        return token
    warnings.append(f"unknown reasoning_effort/effort value={raw!r}; ignored")
    return None


def resolve_thinking_intent(extra_params: Dict[str, Any]) -> ThinkingIntent:
    """Build a ThinkingIntent from one role's extra_params."""
    warnings: List[str] = []
    state, state_is_dict, state_dict = _normalize_thinking_value(
        (extra_params or {}).get("thinking"), warnings
    )
    # reasoning_effort wins over the shorter alias "effort".
    effort = _normalize_effort((extra_params or {}).get("reasoning_effort"), warnings)
    if effort is None:
        effort = _normalize_effort((extra_params or {}).get("effort"), warnings)
    return ThinkingIntent(
        state=state,
        state_is_dict=state_is_dict,
        state_dict=state_dict,
        effort=effort,
        warnings=tuple(warnings),
    )


def claude_budget_tokens(max_tokens: Any) -> Tuple[int, Optional[str]]:
    """Pick a thinking budget for the ``thinking: true`` shorthand.

    The official SDK requires ``budget_tokens`` >= 1024 and < ``max_tokens``.
    Returns (budget, warning-or-None) rather than raising: a too-small
    max_tokens is the user's to fix, and Ark-style endpoints accept the
    shorthand regardless.
    """
    try:
        mt = int(max_tokens)
    except (TypeError, ValueError):
        mt = 4096
    if mt <= 1024:
        return 1024, (
            f"thinking: true 需要 budget_tokens>=1024 且小于 max_tokens，"
            f"当前 max_tokens={mt} 容不下；已按 1024 发送，"
            f"Anthropic 官方端点会拒绝——请调大 max_tokens"
        )
    return min(max(1024, mt - 1024), 16384), None


def _resolve_effort(intent: ThinkingIntent, warnings: List[str]) -> Optional[str]:
    """The effort level to forward, after dropping contradictions."""
    effort = intent.effort
    if effort is None:
        return None
    if intent.state == "disabled" and effort not in _OFF_EFFORTS:
        # Ark's Chat API documents this pair as an error ("thinking.type 取值为
        # disabled：reasoning_effort 仅支持取值 minimal"). The switch is the
        # explicit instruction, so the effort level is what gives way.
        warnings.append(
            f"thinking 已关闭，忽略同时设置的 reasoning_effort={effort!r}"
        )
        return None
    return effort


def openai_thinking_request(
    intent: ThinkingIntent,
) -> Tuple[Dict[str, Any], Dict[str, Any], Tuple[str, ...]]:
    """Translate intent into (top_level_params, extra_body, warnings).

    ``reasoning_effort`` is a native chat.completions kwarg, so it goes top
    level. ``thinking`` is a vendor extension the SDK has no kwarg for, so it
    rides in ``extra_body`` — and only when the user set it, so that a plain
    OpenAI endpoint never sees a body field it would reject.
    """
    top: Dict[str, Any] = {}
    extra_body: Dict[str, Any] = {}
    warnings: List[str] = []
    if not intent.configured:
        return top, extra_body, ()

    if intent.state_is_dict and intent.state_dict is not None:
        extra_body["thinking"] = dict(intent.state_dict)
    elif intent.state == "enabled":
        extra_body["thinking"] = {"type": "enabled"}
    elif intent.state == "disabled":
        extra_body["thinking"] = {"type": "disabled"}

    effort = _resolve_effort(intent, warnings)
    if effort is not None:
        top["reasoning_effort"] = effort
    return top, extra_body, tuple(warnings)


def anthropic_thinking_request(
    intent: ThinkingIntent,
    max_tokens: Any,
) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """Translate intent into (native_kwargs, warnings) for messages.* calls.

    Both keys are native SDK parameters, so nothing needs extra_body here:
    ``thinking`` carries the switch and ``output_config`` the effort level.
    """
    native: Dict[str, Any] = {}
    warnings: List[str] = []
    if not intent.configured:
        return native, ()

    if intent.state_is_dict and intent.state_dict is not None:
        # Verbatim: the user wrote the vendor's own shape, and only they know
        # whether their endpoint wants budget_tokens (Claude) or not (Ark).
        thinking = dict(intent.state_dict)
        if not str(thinking.get("type") or "").strip():
            thinking["type"] = "enabled"
        native["thinking"] = thinking
    elif intent.state == "enabled":
        budget, warning = claude_budget_tokens(max_tokens)
        if warning:
            warnings.append(warning)
        native["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif intent.state == "disabled":
        native["thinking"] = {"type": "disabled"}
    elif intent.switch_is_off:
        # Effort alone asked for no thinking. `output_config` cannot express
        # that (its Literal has no none/minimal, and Ark ignores the field
        # entirely), so route it to the switch that does work.
        native["thinking"] = {"type": "disabled"}

    effort = _resolve_effort(intent, warnings)
    if effort is not None and effort in _ANTHROPIC_EFFORTS:
        native["output_config"] = {"effort": effort}
    return native, tuple(warnings)


def thinking_block_from_reasoning(
    reasoning_content: str,
    encrypted_content: str = "",
) -> Optional[Dict[str, Any]]:
    """Wrap an OpenAI-format chain of thought as an Anthropic thinking block.

    The harness stashes assistant prefix blocks in usage and prepends them to
    the next assistant turn, so representing the chain as a thinking block
    keeps the multi-turn round-trip uniform across providers.
    ``encrypted_content`` (Ark's encrypted original, which takes precedence
    over the summary when both are sent back) travels inside the block so it
    survives the same round-trip.
    """
    text = (reasoning_content or "").strip()
    encrypted = (encrypted_content or "").strip()
    if not text and not encrypted:
        return None
    block: Dict[str, Any] = {"type": "thinking", "thinking": reasoning_content or ""}
    if encrypted:
        block["encrypted_content"] = encrypted_content
    return block
