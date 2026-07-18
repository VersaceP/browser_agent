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
import re
from typing import Any, Dict, List, Optional

from harness.runtime_evaluation import RuntimeEvaluationService
from harness.workflow_policy import validate_workflow_params


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
    method_schemas, schema_error = await _resolve_runtime_schema_for_authored_world(
        browser,
        params.get("steps") or [],
        method_schemas,
    )
    if schema_error is not None:
        return {
            "succeeded": False,
            "runId": run_id,
            "failedStepPath": None,
            "failedError": schema_error,
            "failedPurpose": "runtime capability preflight",
            "variables": variables or {},
            "priorResults": [],
            "exc": "Could not capability-gate authored Runtime world",
        }
    # Preflight the frozen recipe. Runtime ``world`` is the sole capability-
    # normalized field: authored intent is preserved on upgraded servers and
    # omitted from an execution copy for legacy servers that lack the field.
    # ``validate_workflow_params`` also supplies defaults for newly compiled
    # workflows; feeding that normalized copy to old skills silently changed
    # their timeout behavior during this compatibility check.
    prepared_steps, policy_error = _prepare_frozen_runtime_steps(
        params.get("steps") or [],
        method_schemas=method_schemas,
    )
    if policy_error is None:
        params["steps"] = prepared_steps
        _normalized, policy_error = validate_workflow_params(
            params,
            capability_methods=capability_methods,
            task_type=str(getattr(skill, "task_type", "general") or "general"),
            # Existing frozen recipes may contain audited Runtime steps. Ephemeral
            # model-authored workflows remain stricter and reject Runtime entirely.
            allow_runtime=True,
            enforce_lifecycle=True,
            allow_legacy_listen_events=True,
        )
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


_RUNTIME_MUTATION_RE = re.compile(
    # Property/index assignment is observable or alias-ambiguous; a plain
    # ``var/let/const local = ...`` declaration is not. This deliberately
    # remains conservative without classifying every local initializer as a
    # page mutation.
    r"(?:\.\s*[A-Za-z_$][\w$]*|\[[^\]\n]+\])\s*=(?!=)|\+\+|--|"
    r"\.push\s*\(|\.pop\s*\(|"
    r"\.splice\s*\(|\.setAttribute\s*\(|\.removeAttribute\s*\(|"
    r"\.append(?:Child)?\s*\(|\.remove\s*\(|"
    r"\b(?:localStorage|sessionStorage)\.(?:setItem|removeItem|clear)\s*\(",
    re.I,
)


def _prepare_frozen_runtime_steps(
    steps: List[Any],
    *,
    method_schemas: Optional[Dict[str, Any]],
) -> tuple[List[Any], Optional[Dict[str, Any]]]:
    prepared_steps = copy.deepcopy(steps)
    service = RuntimeEvaluationService(method_schemas or {})
    errors: List[str] = []
    for path, step in _walk_workflow_steps(prepared_steps):
        if str(step.get("action") or "") != "Runtime.evaluate":
            continue
        params = dict(step.get("params")) if isinstance(step.get("params"), dict) else {}
        # Frozen recipes may declare their intended world ahead of an ABCP
        # upgrade. Do not send that unsupported field to a legacy server; the
        # source skill and all non-Runtime fields remain untouched.
        if not service.supports_world():
            params.pop("world", None)
        expression = str(params.get("expression") or "")
        effect = "state_changing" if _RUNTIME_MUTATION_RE.search(expression) else "read_only"
        prepared, runtime_error = service.prepare(
            params,
            {
                "intent": "diagnostic",
                "effect": effect,
                "result_mode": "raw",
            },
            origin="harness_compatibility",
        )
        if runtime_error is not None:
            errors.append(
                f"{path}: {runtime_error.get('policy_violation')}:"
                f" {runtime_error.get('error')}"
            )
        elif prepared is not None:
            step["params"] = prepared.params
    if not errors:
        return prepared_steps, None
    return prepared_steps, {
        "status": "rejected",
        "policy_violation": "frozen_workflow_runtime_policy_rejected",
        "errors": errors,
        "tool_was_executed": False,
        "next_instruction": (
            "Migrate the frozen workflow Runtime steps/world declarations before"
            " retrying; the authored payload was not modified or executed."
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


async def _resolve_runtime_schema_for_authored_world(
    browser: Any,
    steps: List[Any],
    method_schemas: Optional[Dict[str, Any]],
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Probe Runtime schema only for non-agent callers that need world gating.

    BrowserAgent dispatch always passes its bootstrap schema map. Offline
    skill-create/heal canaries historically call this function directly; when
    a frozen recipe already declares ``world``, they must not guess whether to
    retain or strip it.
    """
    if method_schemas is not None:
        return method_schemas, None
    declares_world = any(
        str(step.get("action") or "") == "Runtime.evaluate"
        and isinstance(step.get("params"), dict)
        and bool(str(step["params"].get("world") or "").strip())
        for _path, step in _walk_workflow_steps(steps)
    )
    if not declares_world:
        return {}, None
    try:
        response = await browser.call(
            "System.describeAction", {"method": "Runtime.evaluate"}
        )
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            raise ValueError("System.describeAction returned no schema data")
        return {"Runtime.evaluate": data}, None
    except Exception as exc:
        return {}, {
            "status": "rejected",
            "policy_violation": "runtime_world_capability_unknown",
            "error": (
                "The frozen workflow declares Runtime world, but the live"
                f" Runtime.evaluate schema could not be loaded: {exc}"
            ),
            "tool_was_executed": False,
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
