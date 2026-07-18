"""Unified authorization boundary for Runtime.evaluate calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


JsonDict = Dict[str, Any]
EVAL_JS_REASON_KINDS = frozenset({
    "computed_geometry",
    "cross_node_relationship",
    "shadow_dom_traversal",
    "cross_frame_aggregation",
    "non_dom_state",
    "legacy_no_dom_equivalent",
})
_INTENTS = frozenset({"diagnostic", "extract", "state_change"})
_EFFECTS = frozenset({"read_only", "state_changing"})
_RESULT_MODES = frozenset({"raw", "json"})
_STRUCTURED_ORIGINS = frozenset({
    "structured_dom_composite",
    "harness_read_only_oracle",
    "harness_compatibility",
})
# Conservative defense-in-depth heuristic, not a JavaScript parser or security
# sandbox. ABCP's structured interaction/file/permission boundaries remain the
# enforcement layer; this catches common model-authored bypass spellings early.
_FORBIDDEN_INTERACTION_RE = re.compile(
    r"(?:\.(?:click|submit|requestSubmit|dispatchEvent|showOpenFilePicker)"
    r"|\[\s*['\"](?:click|submit|requestSubmit|dispatchEvent|showOpenFilePicker)['\"]\s*\])"
    r"\s*(?:\(|\.call\s*\()"
    r"|\.prototype\.(?:click|submit|requestSubmit|dispatchEvent)\.call\s*\("
    r"|\baddEventListener\s*\("
    r"|(?:^|[^\w])on(?:click|submit|change|input)\s*=(?!=)"
    r"|(?:\.files\b|\[\s*['\"]files['\"]\s*\])"
    r"|navigator(?:\.permissions\b|\[\s*['\"]permissions['\"]\s*\])",
    re.I,
)


@dataclass(frozen=True)
class RuntimePreparation:
    params: JsonDict
    receipt: JsonDict


class RuntimeEvaluationService:
    """Policy preparation shared by model, composite and compatibility paths."""

    def __init__(self, method_schemas: Any = None):
        self.method_schemas = method_schemas if isinstance(method_schemas, dict) else {}

    def supports_world(self) -> bool:
        schema = self.method_schemas.get("Runtime.evaluate")
        params = schema.get("params") if isinstance(schema, dict) else None
        return isinstance(params, dict) and "world" in params

    def prepare(
        self,
        params: Any,
        policy: Any,
        *,
        origin: str,
    ) -> Tuple[Optional[RuntimePreparation], Optional[JsonDict]]:
        if not isinstance(params, dict):
            return None, self._error("runtime_params_invalid", "Runtime params must be an object.")
        expression = str(params.get("expression") or "").strip()
        if not expression:
            return None, self._error("runtime_expression_required", "Runtime.evaluate requires expression.")
        structured = origin in _STRUCTURED_ORIGINS
        if not isinstance(policy, dict):
            return None, self._error(
                "runtime_policy_required",
                "Free-form Runtime.evaluate requires runtime_policy at the harness boundary.",
            )
        intent = str(policy.get("intent") or "").strip()
        effect = str(policy.get("effect") or "").strip()
        result_mode = str(policy.get("result_mode") or "raw").strip()
        reason_kind = str(policy.get("reason_kind") or "").strip()
        why = str(policy.get("why_structured_tools_insufficient") or "").strip()
        cross_check = str(policy.get("cross_check_plan") or "").strip()

        if intent not in _INTENTS:
            return None, self._error("runtime_intent_invalid", f"intent must be one of {sorted(_INTENTS)}")
        if effect not in _EFFECTS:
            return None, self._error("runtime_effect_invalid", f"effect must be one of {sorted(_EFFECTS)}")
        if result_mode not in _RESULT_MODES:
            return None, self._error("runtime_result_mode_invalid", "result_mode must be raw or json")
        if effect == "state_changing" and result_mode == "json":
            return None, self._error(
                "runtime_state_change_requires_raw_result",
                "State-changing scripts must use result_mode=raw so a JSON fallback can never re-execute them.",
            )
        if str(policy.get("record_name") or "").strip() and result_mode != "json":
            return None, self._error(
                "runtime_record_requires_json",
                "record_name requires result_mode=json and a rows-shaped value.",
            )
        if not structured:
            if reason_kind not in EVAL_JS_REASON_KINDS:
                return None, self._error(
                    "runtime_reason_kind_invalid",
                    f"reason_kind must be one of {sorted(EVAL_JS_REASON_KINDS)}",
                )
            if len(why) < 30:
                return None, self._error(
                    "runtime_dom_limitation_required",
                    "Explain concretely why native DOM actions cannot satisfy this operation.",
                )
            if intent == "extract" and len(cross_check) < 20:
                return None, self._error(
                    "runtime_cross_check_required",
                    "Extraction requires a concrete DOM cross-check plan.",
                )
        if _FORBIDDEN_INTERACTION_RE.search(expression):
            return None, self._error(
                "runtime_structured_interaction_bypass",
                "Runtime.evaluate cannot replace Input, form, upload, permission, or structured interaction actions.",
            )

        prepared = dict(params)
        requested_world = str(prepared.get("world") or "").strip()
        world_supported = self.supports_world()
        if requested_world and not world_supported:
            return None, self._error(
                "runtime_world_not_supported",
                "The connected ABCP Runtime.evaluate schema does not expose world yet.",
            )
        effective_world = requested_world or ("auto" if world_supported else "legacy-default")
        if (
            effect == "state_changing"
            and effective_world in {"auto", "legacy-default"}
            and not (origin == "harness_compatibility" and not world_supported)
        ):
            return None, self._error(
                "runtime_explicit_world_required",
                "State-changing scripts must select world=isolated or world=main explicitly.",
            )
        receipt = {
            "origin": origin,
            "intent": intent,
            "effect": effect,
            "resultMode": result_mode,
            "reasonKind": reason_kind or None,
            "world": effective_world,
            "worldSupported": world_supported,
            "recordName": str(policy.get("record_name") or "") or None,
        }
        return RuntimePreparation(params=prepared, receipt=receipt), None

    @staticmethod
    def _error(code: str, message: str) -> JsonDict:
        return {
            "status": "rejected",
            "policy_violation": code,
            "error": message,
            "tool_was_executed": False,
            "next_instruction": (
                "Use native DOM/Page/Input/File actions where possible; otherwise"
                " provide a complete Runtime policy without bypassing structured controls."
            ),
        }
