"""harness.skill.workflow — run a skill's frozen workflow.

The fast path of the skill-as-container architecture: inject runtime handles +
variables into a skill's frozen Workflow.execute steps, run it, and normalize the
result for the caller (success → data; failure → Workflow.getStatus snapshot).

Live-verified contract (2026-06-26, headless ABCP):
  - success: browser.call("Workflow.execute", ...) RETURNS {observation, data:{runId,results,variables}} (no status).
  - failure: it RAISES (ABCPTransportError -32005); the rich payload (failedStepPath/
    results/variables) is NOT in the error → must re-call Workflow.getStatus(runId).
    So a stable runId is mandatory.

`browser` is anything with `async def call(method, params) -> dict` (abcp_client.ABCPClient).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional


def build_execute_params(
    skill: Any,
    *,
    run_id: str,
    page_id: Optional[str] = None,
    fleet_id: Optional[str] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge skill workflow (frozen) + runtime handles into Workflow.execute params."""
    merged_vars: Dict[str, Any] = dict(skill.variable_template)
    if variables:
        merged_vars.update(variables)
    params: Dict[str, Any] = {
        "runId": run_id,
        "steps": skill.steps,
        "errorConfig": skill.error_config,
        "variables": merged_vars,
    }
    if skill.workflow.get("description"):
        params["description"] = skill.workflow["description"]
    if page_id:
        params["pageId"] = page_id
    if fleet_id:
        params["fleetId"] = fleet_id
    return params


async def run_skill_workflow(
    browser: Any,
    skill: Any,
    *,
    run_id: Optional[str] = None,
    page_id: Optional[str] = None,
    fleet_id: Optional[str] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a skill's workflow; return a normalized result dict.

    Success: {"succeeded": True, runId, variables, results, observation}
    Failure: {"succeeded": False, runId, failedStepPath, failedError,
              failedPurpose, variables (failure-time snapshot), priorResults, exc}
    """
    run_id = run_id or f"skill-{skill.skill_id}-{uuid.uuid4().hex[:8]}"
    params = build_execute_params(
        skill, run_id=run_id, page_id=page_id, fleet_id=fleet_id, variables=variables
    )
    try:
        res = await browser.call("Workflow.execute", params)
        data = (res or {}).get("data") or {}
        return {
            "succeeded": True,
            "runId": run_id,
            "variables": data.get("variables") or {},
            "results": data.get("results") or [],
            "observation": (res or {}).get("observation"),
        }
    except Exception as exc:  # execute throws on failure; rich payload not in the error
        snapshot: Dict[str, Any] = {}
        try:
            status = await browser.call("Workflow.getStatus", {"runId": run_id})
            snapshot = (status or {}).get("data") or {}
        except Exception:  # pragma: no cover - getStatus best-effort
            snapshot = {}
        results: List[Dict[str, Any]] = snapshot.get("results") or []
        last = results[-1] if results else {}
        last_step = (last.get("step") or {}) if isinstance(last, dict) else {}
        return {
            "succeeded": False,
            "runId": run_id,
            "failedStepPath": snapshot.get("failedStepPath"),
            "failedError": snapshot.get("error") or (last.get("error") if isinstance(last, dict) else None),
            "failedPurpose": last_step.get("purpose"),
            "variables": snapshot.get("variables") or {},
            "priorResults": results[:-1] if results else [],
            "exc": str(exc),
        }


def check_success_contract(skill: Any, run_result: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate a skill's fallback.yaml success_contract against a run result.

    Returns {"ok": bool, "failed_checks": [str, ...]}. Does NOT cover the
    persistence-side checks (persisted_rows_at_least / fields_*) — those run
    after the harness performs record_extraction (workflow only fills variables).
    """
    contract = skill.success_contract
    variables: Dict[str, Any] = run_result.get("variables") or {}
    failed: List[str] = []

    if contract.get("workflow_no_error", True) and not run_result.get("succeeded"):
        failed.append("workflow_no_error")

    for var in contract.get("variables_required") or []:
        val = variables.get(var)
        if val is None or (isinstance(val, str) and not val.strip()):
            failed.append(f"variables_required:{var}")

    any_nonempty = contract.get("variables_any_nonempty")
    if any_nonempty:
        if not any(
            isinstance(variables.get(v), str) and variables.get(v).strip()
            for v in any_nonempty
        ):
            failed.append(f"variables_any_nonempty:{any_nonempty}")

    return {"ok": not failed, "failed_checks": failed}


def check_persisted_contract(
    skill: Any,
    row: Dict[str, Any],
    artifact: Optional[Dict[str, Any]] = None,
    *,
    row_count: int = 1,
) -> Dict[str, Any]:
    """Persistence-side success_contract (the half check_success_contract documents
    it does NOT cover): the built row must satisfy persisted_rows_at_least /
    fields_required / fields_nonempty, and record_extraction's artifact validation
    must not have flagged needs_fix. Returns {"ok": bool, "failed_checks": [...]}.
    Empty contract ⇒ ok.

    NOTE on provenance: record_extraction treats field_provenance (and min_rows) as
    *advisory*, not blocking (_is_advisory_record_failure), so a missing
    `<field>EvidenceText` surfaces as validationPending — NOT status=needs_fix — and
    therefore does NOT veto the fast path here. Only a blocking failure (schema /
    required_fields) flips needs_fix. So a skill whose contract names a sensitive
    field (e.g. TAAFT `rank`) still completes the fast path; the missing evidence
    column is an advisory enrichment, not a gate."""
    contract = skill.success_contract
    row = row or {}
    failed: List[str] = []

    min_rows = contract.get("persisted_rows_at_least")
    if isinstance(min_rows, int) and row_count < min_rows:
        failed.append(f"persisted_rows_at_least:{min_rows}(got {row_count})")
    for field in contract.get("fields_required") or []:
        if field not in row:
            failed.append(f"fields_required:{field}")
    for field in contract.get("fields_nonempty") or []:
        val = row.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            failed.append(f"fields_nonempty:{field}")
    if isinstance(artifact, dict) and artifact.get("status") == "needs_fix":
        failed.append("artifact_validation:needs_fix")

    return {"ok": not failed, "failed_checks": failed}
