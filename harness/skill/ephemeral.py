"""Current-task trace compilation and canary-gated workflow reuse."""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from harness.pacing import wait_between_rows
from harness.skill.distiller import load_distiller
from harness.skill.registry import canonical_field
from harness.workflow_policy import validate_workflow_params


JsonDict = Dict[str, Any]
_DISALLOWED_TRACE_TYPES = frozenset({
    "visual_verify", "hitl_wait", "eval_js_json", "collect_items",
    "fill_field_verified", "dismiss_overlay", "local_fs_read", "local_fs_search",
})


def pending_ephemeral_final_rejection(
    agent: Any,
    final_status: str,
) -> Optional[JsonDict]:
    """Block successful terminal states while fallback batch rows remain.

    This lives outside the browser tool registry so both tool-based
    ``final_answer`` and the agent loop's legacy text-only terminal path share
    the same mechanical completion barrier.
    """
    pending = getattr(agent, "pending_ephemeral_rows", None)
    rows = pending.get("rows") if isinstance(pending, dict) else None
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not rows or final_status in {"incomplete", "extraction_inconclusive"}:
        return None
    return {
        "status": "ephemeral_fallback_incomplete",
        "tool_was_executed": False,
        "remainingRowCount": len(rows),
        "remainingRows": rows,
        "reason": pending.get("reason") if isinstance(pending, dict) else None,
        "next_instruction": (
            "The temporary workflow stopped before all batch rows completed."
            " Process remainingRows with ordinary browser calls, persist the"
            " combined dataset with record_extraction, then finalize. If the"
            " rows cannot be completed, finalize as incomplete with a blocker."
        ),
    }


def compile_ephemeral_workflow(
    trace: List[JsonDict],
    *,
    capability_methods: Any,
    task_type: str,
) -> Tuple[Optional[JsonDict], JsonDict]:
    blocked = sorted({
        str(item.get("type") or "")
        for item in trace
        if isinstance(item, dict) and str(item.get("type") or "") in _DISALLOWED_TRACE_TYPES
    })
    if blocked:
        return None, {"status": "not_compilable", "reason": "disallowed_trace_tools", "tools": blocked}
    steps, notes, persist, variables = load_distiller().distill(list(trace or []))
    if not steps or not persist:
        return None, {
            "status": "not_compilable",
            "reason": "no_reusable_steps_or_outputs",
            "notes": notes,
        }
    if any(
        isinstance(step, dict) and step.get("action") == "Runtime.evaluate"
        for step in _walk_steps(steps)
    ):
        return None, {"status": "not_compilable", "reason": "runtime_step_forbidden"}

    # The legacy distiller emits navigate -> Page.loaded. New ephemeral policy
    # additionally requires one state sync and a fresh AXTree before targeting.
    hardened: List[JsonDict] = []
    for step in steps:
        hardened.append(step)
        if isinstance(step, dict) and step.get("type") == "listen" and step.get("event") == "Page.loaded":
            hardened.extend([
                {"action": "Page.getState", "purpose": "Synchronize state after row navigation"},
                {"action": "DOM.getAXTree", "purpose": "Refresh DOM identity for this row"},
            ])
    hardened.append({
        "action": "Page.getState",
        "purpose": "Capture final row page binding",
        "extract": {"pageUrl": "url", "pageStatus": "status"},
    })
    binding_variable = _navigation_variable(hardened)
    if not binding_variable:
        return None, {
            "status": "not_compilable",
            "reason": "navigation_variable_not_found",
            "notes": notes,
        }
    variable_template = dict(variables or {})
    variable_template.setdefault(binding_variable, "")
    workflow = {
        "description": "Ephemeral current-task workflow distilled from a validated first row",
        "variables": variable_template,
        "pageBindingVariable": binding_variable,
        "errorConfig": {"onError": "stop", "maxRetries": 1},
        "steps": hardened,
        "timeout": 120000,
        "stepTimeout": 30000,
    }
    normalized, error = validate_workflow_params(
        workflow,
        capability_methods=capability_methods,
        task_type=task_type,
        allow_runtime=False,
        enforce_lifecycle=True,
    )
    if error is not None:
        return None, {"status": "not_compilable", "reason": "workflow_policy", "policy": error}
    normalized["persistFields"] = list(dict.fromkeys(str(item) for item in persist if str(item)))
    return normalized, {
        "status": "compiled",
        "stepCount": len(hardened),
        "persistFields": normalized["persistFields"],
        "notes": notes,
    }


async def execute_ephemeral_rows(
    agent: Any,
    workflow: JsonDict,
    rows: List[JsonDict],
    *,
    worker_contract: JsonDict,
    page_id: str,
    fleet_id: str,
    execute_workflow: Any,
) -> JsonDict:
    if len(rows) < 2:
        return {"status": "skipped", "reason": "fewer_than_two_remaining_rows"}
    persist_fields = [str(item) for item in workflow.get("persistFields") or []]
    steps = list(workflow.get("steps") or [])
    output_rows: List[JsonDict] = []
    runs: List[JsonDict] = []
    workflow_attempted = False
    if not callable(execute_workflow):
        return {
            "status": "skipped",
            "reason": "workflow_executor_required",
            "workflowAttempted": False,
        }
    for index, row in enumerate(rows):
        variables = {
            str(key): value
            for key, value in row.items()
            if isinstance(value, (str, int, float, bool))
        }
        params = {
            "runId": f"ephemeral-{uuid.uuid4().hex[:10]}",
            "description": workflow.get("description"),
            "variables": variables,
            "steps": steps,
            "timeout": workflow.get("timeout", 120000),
            "stepTimeout": workflow.get("stepTimeout", 30000),
            "errorConfig": workflow.get("errorConfig", {"onError": "stop"}),
        }
        if page_id:
            params["pageId"] = page_id
        if fleet_id:
            params["fleetId"] = fleet_id
        try:
            workflow_attempted = True
            response = await execute_workflow(params)
            if isinstance(response, dict) and response.get("method") == "Workflow.execute":
                response = response.get("response") if isinstance(response.get("response"), dict) else response
            data = response.get("data") if isinstance(response, dict) else {}
            result_variables = data.get("variables") if isinstance(data, dict) else {}
            result_variables = result_variables if isinstance(result_variables, dict) else {}
        except Exception as exc:
            return {
                "status": "canary_failed" if index == 0 else "partial",
                "reason": "workflow_call_failed",
                "error": str(exc),
                "completedRows": len(output_rows),
                "rows": output_rows,
                "runs": runs,
                "workflowAttempted": workflow_attempted,
            }
        extracted = {key: result_variables.get(key) for key in persist_fields}
        if not any(value not in (None, "", [], {}) for value in extracted.values()):
            return {
                "status": "canary_failed" if index == 0 else "partial",
                "reason": "success_contract_empty",
                "completedRows": len(output_rows),
                "rows": output_rows,
                "runs": runs,
                "workflowAttempted": workflow_attempted,
            }
        binding_variable = str(workflow.get("pageBindingVariable") or "").strip()
        expected_url = canonical_page_target(row, preferred_field=binding_variable)
        actual_url = _normalize_page_url(result_variables.get("pageUrl"))
        if not expected_url or not actual_url or expected_url != actual_url:
            return {
                "status": "canary_failed" if index == 0 else "partial",
                "reason": "page_binding_mismatch",
                "expectedUrl": expected_url,
                "actualUrl": actual_url,
                "completedRows": len(output_rows),
                "rows": output_rows,
                "runs": runs,
                "workflowAttempted": workflow_attempted,
            }
        output_rows.append({**row, **extracted})
        runs.append({
            "index": index,
            "runId": params["runId"],
            "canary": index == 0,
            "pageUrl": actual_url or None,
        })
        await wait_between_rows(
            agent,
            worker_contract,
            completed_index=index,
            total_rows=len(rows),
            source="ephemeral_workflow",
        )
    return {
        "status": "done",
        "canaryPassed": True,
        "completedRows": len(output_rows),
        "rows": output_rows,
        "runs": runs,
        "workflowAttempted": workflow_attempted,
    }


def canonical_page_target(row: Any, *, preferred_field: str = "") -> str:
    if not isinstance(row, dict):
        return ""
    if preferred_field and preferred_field in row:
        value = _normalize_page_url(row.get(preferred_field))
        if value:
            return value
    for field, value in row.items():
        if canonical_field(field) == "detailurl":
            normalized = _normalize_page_url(value)
            if normalized:
                return normalized
    return ""


def _navigation_variable(steps: List[Any]) -> str:
    for step in _walk_steps(steps):
        if step.get("action") != "Page.navigate":
            continue
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        match = re.match(r"^\$vars\.([A-Za-z0-9_]+)$", str(params.get("url") or ""))
        if match:
            return match.group(1)
    return ""


def _normalize_page_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _walk_steps(steps: List[Any]) -> List[JsonDict]:
    found: List[JsonDict] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        found.append(step)
        for key in ("then", "else", "body"):
            nested = step.get(key)
            if isinstance(nested, list):
                found.extend(_walk_steps(nested))
    return found
