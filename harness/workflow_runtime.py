"""Single control-plane gate for ABCP Workflow execution.

All Harness execution entrances must consult this gate; direct diagnostic
scripts that call ABCP without the Harness are intentionally outside its scope.

The gate defaults OPEN as of 2026-09-11. It shipped closed in f89e15d
(2026-08-10), whose subject — "ban Runtime.evaluate + default-off workflow" —
records the actual reason: that commit retired `harness/skill/ephemeral.py`, the
path that distilled a workflow out of an execution trace and replayed it. That
path extracted its data through `Runtime.evaluate`, so banning page-world JS
killed it, and the master switch went down with it.

Model-authored workflows are a different path and were never removed: every step
passes `harness.workflow_policy.validate_workflow_params` and each nested Action
its own ABCP permission check. The reason later written next to the flag — that
the platform lacked "pre-armed action events plus dynamic collection/state
primitives" — was verified false on 2026-09-11 and is documented in
docs/workflow-execute-live-contract.md: `waitEvent` performs a cursor-based
gap-safe replay-and-subscribe (strictly stronger than pre-arming, since an event
emitted before the wait is replayed rather than missed), and `store` supports
set/merge/append/delete.

`workflow_execution_enabled` still fails CLOSED when the attribute is absent, so
a caller holding a config object that predates the flag never executes by
accident.
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
