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
_INTENTS = frozenset({"diagnostic", "extract"})
_EFFECTS = frozenset({"read_only"})
_RESULT_MODES = frozenset({"raw", "json"})
MAIN_WORLD_REQUIRED_PREFIX = "ABCP_MAIN_WORLD_REQUIRED:"
_MAIN_FALLBACK_SIGNAL_RE = re.compile(
    r"\bthrow\s+new\s+ReferenceError\s*\(\s*['\"]"
    r"ABCP_MAIN_WORLD_REQUIRED:[^'\"\r\n]+['\"]\s*\)",
    re.I,
)
RUNTIME_EVIDENCE_CLASSES = {
    "structure": (
        "DOM.getAXTree",
        "DOM.getSemanticTree",
    ),
    "targeted_read": (
        "DOM.getText",
        "DOM.getAttribute",
        "DOM.getImg",
    ),
}
# Page.getState is useful epoch-local context, but it cannot substitute for
# either proof that the page structure was inspected or proof that a native
# read was attempted against the target. Keeping it in the receipt makes the
# trace legible without letting Page.getState + one cheap tree call unlock JS.
RUNTIME_STRUCTURED_ALTERNATIVES = (
    "Page.getState",
    *RUNTIME_EVIDENCE_CLASSES["structure"],
    *RUNTIME_EVIDENCE_CLASSES["targeted_read"],
)
# One method from each evidence class: useful categorical evidence, not an
# arbitrary numeric threshold or the former five-method checklist. DOM.getImg
# is a targeted native read for the image/shadow-host case behind this change.
_RUNTIME_EPOCH_BOUNDARIES = frozenset({
    "Page.create",
    "Page.navigate",
    "Page.reload",
    "Page.go",
    "Page.switchTo",
    "Input.click",
    "Input.drag",
    "Input.press",
    "Input.scroll",
    "Input.type",
    "Hitl.requestPause",
    "Hitl.resolvePause",
    "Runtime.evaluate",
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
_JSON_FUNCTION_BODY_RE = re.compile(r"^\s*return\b", re.I)
_JSON_BARE_FUNCTION_RE = re.compile(
    r"^\s*(?:async\s+)?function\b|^\s*(?:async\s+)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>",
    re.I,
)


@dataclass(frozen=True)
class RuntimePreparation:
    params: JsonDict
    receipt: JsonDict


class RuntimeEvaluationService:
    """Prepare the sole model-facing, read-only Runtime fallback."""

    def __init__(self, method_schemas: Any = None):
        self.method_schemas = method_schemas if isinstance(method_schemas, dict) else {}

    def supports_world(self, requested: str) -> bool:
        schema = self.method_schemas.get("Runtime.evaluate")
        params = schema.get("params") if isinstance(schema, dict) else None
        world = params.get("world") if isinstance(params, dict) else None
        supported = world.get("enum") if isinstance(world, dict) else None
        return isinstance(supported, list) and requested in supported

    def prepare(
        self,
        params: Any,
        policy: Any,
        *,
        origin: str,
    ) -> Tuple[Optional[RuntimePreparation], Optional[JsonDict]]:
        if not isinstance(params, dict):
            return None, self._error("runtime_params_invalid", "Runtime params must be an object.")
        if "runtime_policy" in params:
            error = self._error(
                "runtime_policy_misplaced",
                "runtime_policy is harness authorization metadata and must be a sibling of params, not nested inside Runtime.evaluate params.",
            )
            error["expected"] = {
                "method": "Runtime.evaluate",
                "params": {
                    "pageId": "<page UUID>",
                    "expression": "<value expression or invoked IIFE>",
                    "world": "isolated",
                    "purpose": "<short purpose>",
                },
                "runtime_policy": {
                    "intent": "diagnostic|extract",
                    "effect": "read_only",
                    "reason_kind": "<declared reason kind>",
                    "why_structured_tools_insufficient": "<concrete explanation>",
                    "cross_check_plan": "<DOM cross-check for extraction>",
                },
            }
            error["next_instruction"] = (
                "Move runtime_policy out of params, keep world=isolated inside"
                " params, and retry as one standalone Runtime.evaluate call."
            )
            return None, error
        expression = str(params.get("expression") or "").strip()
        if not expression:
            return None, self._error("runtime_expression_required", "Runtime.evaluate requires expression.")
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
        if result_mode == "json" and (
            _JSON_FUNCTION_BODY_RE.search(expression)
            or _JSON_BARE_FUNCTION_RE.search(expression)
        ):
            return None, self._error(
                "runtime_json_value_expression_required",
                "result_mode=json requires a value expression or an invoked IIFE; do not pass a function body, top-level return, or an uninvoked function.",
            )
        if str(policy.get("record_name") or "").strip() and result_mode != "json":
            return None, self._error(
                "runtime_record_requires_json",
                "record_name requires result_mode=json and a rows-shaped value.",
            )
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
        if not self.supports_world("isolated"):
            return None, self._error(
                "runtime_isolated_world_unavailable",
                "The connected Runtime.evaluate schema does not advertise strict isolated-world execution.",
            )
        if requested_world != "isolated":
            return None, self._error(
                "runtime_isolated_world_required",
                "Model-facing Runtime.evaluate requires explicit world=isolated. Direct main, auto, and implicit worlds are forbidden; only the harness may authorize a second strict main-world attempt.",
            )
        main_fallback_authorized = reason_kind == "non_dom_state"
        if main_fallback_authorized:
            if not self.supports_world("main"):
                return None, self._error(
                    "runtime_main_world_unavailable",
                    "non_dom_state requires the connected Runtime.evaluate schema to advertise strict main-world execution for the guarded second attempt.",
                )
            if not _MAIN_FALLBACK_SIGNAL_RE.search(expression):
                return None, self._error(
                    "runtime_main_fallback_signal_required",
                    "non_dom_state must throw ReferenceError('ABCP_MAIN_WORLD_REQUIRED:<global>') only when the required page global is absent in isolated world.",
                )
        receipt = {
            "origin": origin,
            "intent": intent,
            "effect": effect,
            "resultMode": result_mode,
            "reasonKind": reason_kind or None,
            "requestedWorld": "isolated",
            "executedWorld": None,
            "mainFallbackAuthorized": main_fallback_authorized,
            "fallbackSignal": (
                MAIN_WORLD_REQUIRED_PREFIX if main_fallback_authorized else None
            ),
            "fallbackPolicy": (
                "harness_isolated_then_explicit_main_on_dedicated_signal"
                if main_fallback_authorized else "strict_isolated_only"
            ),
            "attempts": [],
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


def runtime_last_resort_evidence(
    agent: Any,
    *,
    page_id: str,
) -> Tuple[Optional[JsonDict], Optional[JsonDict]]:
    """Prove one structure read and one targeted native read in this epoch.

    The proof is derived from harness trace entries, never from model prose.
    Recognized explicit state-boundary calls end the trace epoch so stale reads
    cannot authorize a later script. Page-driven or JavaScript-driven changes
    that emit no observed boundary are outside this trace guarantee.
    """
    page_id = str(page_id or "").strip()
    if not page_id:
        return None, RuntimeEvaluationService._error(
            "runtime_page_id_required",
            "Runtime.evaluate last-resort authorization requires pageId.",
        )
    available = set(getattr(agent, "capability_methods", set()) or set())
    required = [
        method for method in RUNTIME_STRUCTURED_ALTERNATIVES
        if method in available
    ]
    if not required:
        return None, RuntimeEvaluationService._error(
            "runtime_structured_alternatives_unavailable",
            "Runtime.evaluate cannot be authorized because no structured Page/DOM read alternatives are available for trace-based exhaustion proof.",
        )
    candidates_by_class = {
        evidence_class: [method for method in methods if method in available]
        for evidence_class, methods in RUNTIME_EVIDENCE_CLASSES.items()
    }
    unavailable_classes = [
        evidence_class
        for evidence_class, methods in candidates_by_class.items()
        if not methods
    ]
    if unavailable_classes:
        error = RuntimeEvaluationService._error(
            "runtime_structured_evidence_classes_unavailable",
            "Runtime.evaluate cannot be authorized because the connected capability surface cannot provide every required evidence class.",
        )
        error.update({
            "pageId": page_id,
            "requiredEvidenceClasses": list(RUNTIME_EVIDENCE_CLASSES),
            "unavailableEvidenceClasses": unavailable_classes,
            "candidateAlternativesByClass": candidates_by_class,
        })
        return None, error
    attempted: Dict[str, JsonDict] = {}
    trace = getattr(agent, "trace", [])
    for event in reversed(trace if isinstance(trace, list) else []):
        if not isinstance(event, dict) or event.get("type") != "browser_call":
            continue
        method = str(event.get("method") or "").strip()
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        event_page_id = str(params.get("pageId") or "").strip()
        if event_page_id != page_id:
            continue
        if method in _RUNTIME_EPOCH_BOUNDARIES:
            break
        if method not in required or method in attempted:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        attempted[method] = {
            "method": method,
            "status": str(result.get("status") or "attempted"),
            "hadError": bool(result.get("error") or result.get("policy_violation")),
        }
    satisfied_classes = [
        evidence_class
        for evidence_class, methods in candidates_by_class.items()
        if any(method in attempted for method in methods)
    ]
    remaining_classes = [
        evidence_class
        for evidence_class in RUNTIME_EVIDENCE_CLASSES
        if evidence_class not in satisfied_classes
    ]
    evidence_receipt = {
        "pageId": page_id,
        "attemptedAlternatives": [
            attempted[item] for item in required if item in attempted
        ],
        "requiredEvidenceClasses": list(RUNTIME_EVIDENCE_CLASSES),
        "satisfiedEvidenceClasses": satisfied_classes,
        "remainingEvidenceClasses": remaining_classes,
        "candidateAlternativesByClass": candidates_by_class,
    }
    if remaining_classes:
        error = RuntimeEvaluationService._error(
            "runtime_structured_alternatives_not_exhausted",
            "Runtime.evaluate requires one structure read and one targeted native read on this page in the current page epoch.",
        )
        error.update(evidence_receipt)
        choices = {
            evidence_class: candidates_by_class[evidence_class]
            for evidence_class in remaining_classes
        }
        error["next_instruction"] = (
            "Attempt one method from each remainingEvidenceClass on this pageId,"
            " then retry Runtime.evaluate. Pick any one candidate in each class;"
            f" do not call every method. Remaining candidates: {choices}."
        )
        return None, error
    return {
        **evidence_receipt,
        "epochScope": "since_last_page_or_dom_state_boundary",
        "authorized": True,
    }, None
