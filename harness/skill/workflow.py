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

import copy
import uuid
from typing import Any, Dict, List, Optional

from harness.fleet_runtime import FleetClickGateTimeout
from harness.task_types import resolve_task_type_fail_closed
from harness.workflow_policy import validate_workflow_params
from harness.workflow_runtime import (
    workflow_execution_disabled_result,
    workflow_execution_enabled,
)


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
    capability_methods: Optional[Any] = None,
    method_schemas: Optional[Dict[str, Any]] = None,
    workflow_runtime: Any = None,
) -> Dict[str, Any]:
    """Run a skill's workflow; return a normalized result dict.

    ``workflow_runtime`` is mandatory authorization context. Omitting it fails
    closed before schema discovery or any browser RPC.

    Success: {"succeeded": True, runId, variables, results, observation}
    Failure: {"succeeded": False, runId, failedStepPath, failedError,
              failedPurpose, variables (failure-time snapshot), priorResults, exc}
    """
    run_id = run_id or f"skill-{skill.skill_id}-{uuid.uuid4().hex[:8]}"
    if not workflow_execution_enabled(workflow_runtime):
        disabled = workflow_execution_disabled_result(
            source="run_skill_workflow",
        )
        return {
            "succeeded": False,
            "runId": run_id,
            "failedStepPath": None,
            "failedError": dict(disabled),
            "failedPurpose": "workflow runtime gate",
            "variables": variables or {},
            "priorResults": [],
            "exc": "Workflow execution disabled by Harness runtime",
            **disabled,
        }
    params = build_execute_params(
        skill, run_id=run_id, page_id=page_id, fleet_id=fleet_id, variables=variables
    )
    # Preflight the frozen recipe before any workflow request is sent.
    prepared_steps, policy_error = _prepare_frozen_runtime_steps(
        params.get("steps") or [],
        method_schemas=method_schemas,
    )
    if policy_error is None:
        params["steps"] = prepared_steps
        normalized, policy_error = validate_workflow_params(
            params,
            capability_methods=capability_methods,
            task_type=resolve_task_type_fail_closed(
                getattr(skill, "task_type", None)
            ),
            allow_runtime=False,
            enforce_lifecycle=True,
            allow_legacy_listen_events=True,
        )
        if policy_error is None and isinstance(normalized, dict):
            # Preserve legacy timeout omission while executing the policy-
            # normalized copy of frozen steps (notably path-only screenshots).
            params["steps"] = normalized["steps"]
    if policy_error is not None:
        return {
            "succeeded": False,
            "runId": run_id,
            "failedStepPath": None,
            "failedError": policy_error,
            "failedPurpose": "workflow preflight policy",
            "variables": variables or {},
            "priorResults": [],
            "exc": "Frozen workflow rejected by harness policy",
        }
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
    except FleetClickGateTimeout as exc:
        return {
            "succeeded": False,
            "runId": run_id,
            "failedStepPath": None,
            "failedError": dict(exc.receipt),
            "failedPurpose": "Fleet click gate admission",
            "variables": variables or {},
            "priorResults": [],
            "exc": str(exc),
            **exc.receipt,
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


def _prepare_frozen_runtime_steps(
    steps: List[Any],
    *,
    method_schemas: Optional[Dict[str, Any]],
) -> tuple[List[Any], Optional[Dict[str, Any]]]:
    """Reject frozen recipes containing page-world JavaScript."""
    _ = method_schemas
    prepared_steps = copy.deepcopy(steps)
    runtime_paths = [
        path
        for path, step in _walk_workflow_steps(prepared_steps)
        if str(step.get("action") or "") == "Runtime.evaluate"
    ]
    if not runtime_paths:
        return prepared_steps, None
    return prepared_steps, {
        "status": "rejected",
        "policy_violation": "frozen_workflow_runtime_forbidden",
        "errors": [
            f"{path}: Runtime.evaluate is forbidden in frozen workflows"
            for path in runtime_paths
        ],
        "tool_was_executed": False,
        "next_instruction": (
            "Migrate the skill to native Page/DOM/Input actions before retrying."
        ),
    }

def _audit_frozen_runtime_steps(
    steps: List[Any],
    *,
    method_schemas: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper for callers that only need an audit verdict."""
    _prepared, error = _prepare_frozen_runtime_steps(
        steps,
        method_schemas=method_schemas,
    )
    return error


def _walk_workflow_steps(
    steps: List[Any],
    path: str = "steps",
) -> List[tuple[str, Dict[str, Any]]]:
    found: List[tuple[str, Dict[str, Any]]] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            continue
        step_path = f"{path}[{index}]"
        found.append((step_path, raw))
        for key in ("then", "else", "body"):
            nested = raw.get(key)
            if isinstance(nested, list):
                found.extend(_walk_workflow_steps(nested, f"{step_path}.{key}"))
    return found


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
    expected_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """Persistence-side success_contract (the half check_success_contract documents
    it does NOT cover): the built row must satisfy persisted_rows_at_least /
    fields_required / fields_nonempty, and record_extraction's artifact validation
    must not have flagged needs_fix OR left anything validationPending. Returns
    {"ok": bool, "failed_checks": [...]}. Empty contract ⇒ ok.

    NOTE on provenance/advisory: record_extraction classifies field_provenance
    (and row-count shortfalls) as *advisory* — validationPending, not
    status=needs_fix — because an ITERATING worker fixes those by enriching rows
    on a LATER save. The fast path has no later save: this check is its terminal
    verdict, so anything still pending here is blocking FOR IT. Task 2ed5a466 p3
    proved the alternative: rows missing rankEvidenceText self-approved here,
    then the same validator (blocking at phase level) failed the attempt — three
    times. Advisory is a property of the consumer's ability to continue, not of
    the validator."""
    contract = skill.success_contract
    row = row or {}
    failed: List[str] = []

    # fields comparison is CANONICAL (productUrl≡detailUrl): fast-path persistence
    # aligns row keys to the PLAN's field names (dispatch._align_row_fields_to_
    # expected), so the skill's own contract naming may legitimately differ from
    # the row's literal keys by a synonym — that must not read as "missing".
    from harness.skill.registry import canonical_field

    def _row_value(field: str):
        if field in row:
            return True, row.get(field)
        canon = canonical_field(field)
        for key, value in row.items():
            if canonical_field(key) == canon:
                return True, value
        return False, None

    min_rows = contract.get("persisted_rows_at_least")
    if isinstance(min_rows, int) and row_count < min_rows:
        failed.append(f"persisted_rows_at_least:{min_rows}(got {row_count})")
    # PHASE row-count gate: the skill's own persisted_rows_at_least (typically 1)
    # knows nothing about the phase's exact_rows/min_rows, and record_extraction
    # treats an under-count as ADVISORY (a worker mid-collection keeps going).
    # Without this check a single-run fast path on a 5-row phase could satisfy
    # its own contract with 1 row, self-approve, skip the slow path, and burn a
    # whole phase attempt at spawner-level validation instead (the exact trap
    # the 9d5655d3 review flagged against merge-only repair).
    if isinstance(expected_rows, int) and expected_rows > 0 and row_count < expected_rows:
        failed.append(f"phase_rows:{expected_rows}(got {row_count})")
    for field in contract.get("fields_required") or []:
        present, _ = _row_value(str(field))
        if not present:
            failed.append(f"fields_required:{field}")
    for field in contract.get("fields_nonempty") or []:
        _, val = _row_value(str(field))
        if val is None or (isinstance(val, str) and not val.strip()):
            failed.append(f"fields_nonempty:{field}")
    if isinstance(artifact, dict):
        if artifact.get("status") == "needs_fix":
            failed.append("artifact_validation:needs_fix")
        # Terminal-consumer rule: validationPending lists validator types the
        # record path deferred for a later, enriched save. The fast path never
        # saves again, so a pending validator here is a failed one at phase
        # validation — read the already-computed verdict instead of re-running
        # validators (a second executor is how the two judgements drifted apart
        # in the first place).
        pending = artifact.get("validationPending")
        if isinstance(pending, list):
            pending_types = sorted({str(p) for p in pending if str(p).strip()})
            if pending_types:
                failed.append(
                    "artifact_validation_pending:" + ",".join(pending_types)
                )

    return {"ok": not failed, "failed_checks": failed}
