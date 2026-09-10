"""Runtime.evaluate request preparation and execution receipts.

The ABCP Runtime.evaluate schema is the execution contract. The Harness does
not classify model-authored JavaScript as read-only or state-changing, and it
does not infer which page-world operation is appropriate for a user goal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class RuntimePreparation:
    params: JsonDict
    receipt: JsonDict


class RuntimeEvaluationService:
    """Prepare a model-authored Runtime.evaluate call without inspecting JS."""

    def __init__(self, method_schemas: Any = None):
        # Kept for constructor compatibility with existing dispatcher callers.
        self.method_schemas = method_schemas if isinstance(method_schemas, dict) else {}

    def prepare(
        self,
        params: Any,
        policy: Any,
        *,
        origin: str,
    ) -> Tuple[Optional[RuntimePreparation], Optional[JsonDict]]:
        if not isinstance(params, dict):
            return None, self._error(
                "runtime_params_invalid", "Runtime params must be an object."
            )
        expression = str(params.get("expression") or "").strip()
        if not expression:
            return None, self._error(
                "runtime_expression_required", "Runtime.evaluate requires expression."
            )

        # `runtime_policy` remains accepted only so older callers that use its
        # JSON extraction envelope keep working. It is not authorization data:
        # no field can narrow or widen what JavaScript may do.
        legacy_policy = policy if isinstance(policy, dict) else {}
        result_mode = (
            "json" if legacy_policy.get("result_mode") == "json" else "raw"
        )
        prepared = dict(params)
        requested_world = str(prepared.get("world") or "auto").strip() or "auto"
        receipt = {
            "origin": origin,
            "requestedWorld": requested_world,
            "executedWorld": None,
            "dispatchPolicy": "platform_schema",
            "resultMode": result_mode,
            "recordName": (
                str(legacy_policy.get("record_name") or "") or None
                if result_mode == "json"
                else None
            ),
            "legacyPolicySupplied": bool(legacy_policy),
            "attempts": [],
        }
        return RuntimePreparation(params=prepared, receipt=receipt), None

    @staticmethod
    def _error(code: str, message: str) -> JsonDict:
        return {
            "status": "rejected",
            "policy_violation": code,
            "error": message,
            "tool_was_executed": False,
            "next_instruction": "Correct the Runtime.evaluate parameters and retry.",
        }
