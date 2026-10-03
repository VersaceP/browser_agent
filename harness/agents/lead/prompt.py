"""Lead prompt construction."""
from __future__ import annotations

from harness.workflow.workflow_runtime import workflow_execution_enabled
from harness.runtime.model_support import (
    _guide_manifest_for,
)

LEAD_AUTH_PLANNING_SOP = """   Authentication, login walls, QR/SMS/2FA prompts, CAPTCHAs, and human-verification challenges are unpredictable runtime interrupts, not default task-plan phases. Do not add a speculative pre-auth probe phase or a follow-up HITL/login phase merely because a site may require authentication. Plan the protected business work directly; the worker that encounters a decisive gate must call Hitl.requestPause, verify the resumed page, and continue its original phase. A dedicated auth phase is allowed only when authentication/session setup is itself the user's explicit deliverable, account switching is required, or a task-type boundary makes the business worker unable to perform the required auth interaction. A probe-only phase is allowed only when diagnosing whether a gate exists is itself the final user objective; never chain that probe into a second HITL worker."""



def build_system_prompt(self) -> str:
    from harness.prompts.delegation import LEAD_DELEGATION_PROMPT
    workflow_context = (
        "Workflow execution requires the selected worker's runtime switch,"
        " live Workflow.execute capability and visible execution tool."
        " When available, let the worker choose segments at known decision"
        " points; do not demand a Workflow for uncertain page steps."
        if workflow_execution_enabled(self)
        else "Workflow execution is currently disabled for this run;"
        " workflow-backed skills supply guidance only."
    )
    return (LEAD_DELEGATION_PROMPT + "\n" + workflow_context + "\n"
            + LEAD_AUTH_PLANNING_SOP + "\n"
            + _guide_manifest_for("lead", getattr(self, "logger", None))
            + self.static_context_block)
