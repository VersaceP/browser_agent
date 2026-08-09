"""Single control-plane gate for ABCP Workflow execution.

Workflow recipes remain useful as skill guidance while the ABCP Workflow
runtime lacks the event arming and dynamic collection primitives required by
the hybrid fast path.  All Harness execution entrances must consult this gate;
direct diagnostic scripts that call ABCP without the Harness are intentionally
outside its scope.
"""
from __future__ import annotations

from typing import Any, Dict


def _harness_config(value: Any) -> Any:
    runtime = getattr(value, "runtime", None)
    if runtime is not None:
        value = runtime
    return getattr(value, "harness", value)


def workflow_execution_enabled(value: Any) -> bool:
    """Return the explicit Workflow master switch, failing closed if absent."""

    harness = _harness_config(value)
    return bool(getattr(harness, "workflow_execution_enabled", False))


def workflow_execution_disabled_result(*, source: str) -> Dict[str, Any]:
    """Stable receipt shared by model-authored and frozen-skill entrances."""

    return {
        "status": "rejected",
        "classification": "workflow_runtime_disabled",
        "reason": "workflow_execution_disabled",
        "source": str(source or "workflow"),
        "tool_was_executed": False,
        "next_instruction": (
            "ABCP Workflow execution is disabled by the Harness runtime. Use"
            " the selected skill's SKILL.md as guidance and continue with"
            " ordinary BrowserAgent calls and Harness composites. Do not copy"
            " workflow.json steps into browser_call or reconstruct an opaque"
            " workflow."
        ),
    }
