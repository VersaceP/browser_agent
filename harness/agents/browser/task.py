"""Standalone Browser task entry, continuation and worker collection.

No Lead planner, delegation review or Lead tool adapter is involved. The
persisted phase ledger remains compatible with existing SQLite tasks.
"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from typing import Optional

from harness.agents.browser.agent import BrowserAgent
from harness.capabilities.bootstrap import bootstrap_schema_cache
from harness.planning.context import user_context
from harness.planning.fleet_reference import extract_fleet_reference
from harness.runtime.resume_context import ResumeContext
from harness.runtime.worker_recovery import recover_worker_transport
from harness.spawner import BrowserAgentSpawner
from harness.task_control import (accept_task_plan, find_phase, load_task_state,
    mark_phase_exhausted_if_needed, phase_contract, phase_start_rejection,
    schedule_snapshot, validate_task_plan, write_task_state)
from harness.utils import JsonDict, RunLogger


def _safe_logger_write(logger, event_type, payload):
    try:
        logger.write(event_type, payload)
    except Exception:
        pass


class BrowserTaskRunner:
    """One task owner; the BrowserAgent owns its plan and evidence review."""

    def __init__(self, runtime, logger, *, resume=None, task_fleet_reference="",
                 pinned_browser_context=None):
        self.runtime = runtime
        self.logger = logger
        self.resume = resume
        self.task_fleet_reference = str(task_fleet_reference or "").strip()
        self.task_plan = resume.current_plan if resume else None
        self.original_user_task = resume.original_user_task if resume else ""
        self._schema_bootstrap_degraded = False
        self._browser_connection_failure = None
        self.spawner = BrowserAgentSpawner(
            runtime, logger, browser_agent_factory=BrowserAgent,
            pinned_browser_context=pinned_browser_context,
            resume_browser_hint=resume.browser_hint if resume else None,
        )

    async def _bootstrap_schema_cache(self):
        await bootstrap_schema_cache(self)

    async def spawn(self, phase_id):
        phase = find_phase(self.task_plan, phase_id)
        if phase is None:
            return _browser_mode_failure(self, code="phase_not_found",
                error=f"No accepted task phase: {phase_id}")
        mark_phase_exhausted_if_needed(self.task_plan, self.logger)
        rejection = phase_start_rejection(self.task_plan, self.logger, phase_id=phase_id)
        if rejection is not None:
            return rejection
        contract = phase_contract(phase)
        contract["_user_context"] = user_context(self.logger, self.original_user_task)
        contract.pop("skill_id", None)
        contract.pop("skill_hash", None)
        selected = self.runtime.harness
        if selected.forced_skill_id and selected.forced_skill_hash:
            contract.update(skill_id=selected.forced_skill_id, skill_hash=selected.forced_skill_hash)
        context = str(phase.get("context") or "")
        state = load_task_state(self.logger)
        attempts = ((state.get("phases") or {}).get(phase_id) or {}).get("attempts") or []
        for attempt in reversed(attempts):
            digest = attempt.get("attemptDigest") if isinstance(attempt, dict) else None
            handoff = digest.get("handoff") if isinstance(digest, dict) else None
            if isinstance(handoff, dict):
                context += "\n\nPREVIOUS WORKER HANDOFF (receipts and claims retain their stated ownership):\n" + json.dumps(handoff, ensure_ascii=False, separators=(",", ":"), default=str)
                break
        spawned = await self.spawner.spawn_browser_agent(
            task=str(phase.get("worker_task") or ""), context=context,
            phase_id=phase_id, worker_contract=contract, phase=phase,
            task_plan=self.task_plan, task_fleet_reference=self.task_fleet_reference or None,
            dispatch_origin="browser_task",
        )
        if isinstance(spawned, dict):
            recovery = await recover_worker_transport(self, [spawned])
            if recovery is not None:
                spawned = {**spawned, "connectionRecovery": recovery}
        return spawned

    async def wait(self, worker_id):
        result = await self.spawner.wait_browser_agents(
            worker_ids=[worker_id], mode="all", timeout_seconds=None)
        recovery = await recover_worker_transport(self, result.get("completed") or [])
        if recovery is not None:
            result["connectionRecovery"] = recovery
        result["scheduleSnapshot"] = schedule_snapshot(self.task_plan, self.logger)
        result["operatorInputRecords"] = user_context(self.logger, self.original_user_task)["operatorInputs"]
        return result

def _browser_mode_failure(
    harness: BrowserTaskRunner,
    *,
    code: str,
    error: str,
    tool_was_executed: bool = False,
) -> JsonDict:
    """Single logged exit for every terminal browser-mode abort.

    Run 18daa415 ended on a structured early return that wrote no event at
    all, leaving run.jsonl with nothing to diagnose beyond an empty usage
    summary. Every failure exit from browser mode goes through here so a
    failed run always says why it failed in its own log.
    """
    _safe_logger_write(
        harness.logger,
        "direct_mode.failed",
        {
            "code": code,
            "error": error,
            "toolWasExecuted": tool_was_executed,
        },
    )
    return {
        "status": "failed",
        "error": error,
        "code": code,
        "tool_was_executed": tool_was_executed,
    }


async def _run_browser_mode(
    harness: BrowserTaskRunner,
    *,
    task: str,
    original_task: str,
    resume_context: Optional[ResumeContext],
) -> JsonDict:
    """Run the explicit browser entry without a Lead model turn.

    BrowserTaskRunner owns dispatch, lifecycle and persistence. The single
    phase records the user request without a model-authored delegation.
    """
    if resume_context is not None:
        plan = resume_context.current_plan
        phases = plan.get("phases") if isinstance(plan, dict) else None
        if (
            not isinstance(plan, dict)
            or plan.get("execution_mode") not in {"delegated", "direct_worker"}
            or not isinstance(phases, list)
            or not phases
        ):
            return _browser_mode_failure(
                harness,
                code="browser_mode_resume_plan_unsupported",
                error="browser mode resume requires an accepted direct task phase",
            )
        harness.original_user_task = str(resume_context.original_user_task or task)
        harness.spawner.root_task = harness.original_user_task
        harness.spawner.standalone_resuming = True
        phase_id = str(phases[-1].get("id") or "")
        state = load_task_state(harness.logger)
        pending_inputs = _unbound_browser_resume_inputs(state, phases)
        phase_state = (state.get("phases") or {}).get(phase_id) or {}
        if pending_inputs and phase_state.get("status") != "pending":
            return await _submit_browser_resume_amendment(
                harness, resume_context=resume_context, phase_id=phase_id,
                pending_inputs=pending_inputs,
            )
        if pending_inputs:
            _bind_browser_resume_inputs(harness.logger, pending_inputs, phase_id)
        await harness._bootstrap_schema_cache()
        result = await harness.spawn(phase_id)
        if not isinstance(result, dict):
            return _browser_mode_failure(
                harness, code="browser_mode_resume_no_receipt",
                error="direct resume returned no receipt",
            )
        return await _wait_for_browser_mode_result(harness, result)

    fleet_reference = str(
        getattr(harness, "task_fleet_reference", "") or ""
    ).strip()
    if not fleet_reference:
        fleet_reference, fleet_error = extract_fleet_reference(original_task)
        if fleet_error:
            return _browser_mode_failure(
                harness,
                code="browser_mode_fleet_reference_invalid",
                error=fleet_error,
            )
        # Normal CLI construction sets this once before the runner is created.
        # Keep direct programmatic callers on the same control-plane route.
        harness.task_fleet_reference = fleet_reference or ""

    harness.logger.write("direct_mode.assignment_started", {
        "fleetReferenceSource": "task_text" if fleet_reference else None,
    })
    harness.original_user_task = str(original_task or task)
    harness.spawner.root_task = harness.original_user_task
    await harness._bootstrap_schema_cache()
    result = await _submit_direct_plan(harness, original_task)
    return await _wait_for_browser_mode_result(harness, result)


def _unbound_browser_resume_inputs(state: JsonDict, phases: list) -> list:
    """Recover attributed inputs written before a continuation could be committed."""
    bound = set((state.get("resume_input_bindings") or {}).keys())
    for phase in phases:
        meta = ((phase.get("worker_contract") or {}).get("_delegation") or {})
        bound.update(meta.get("resumeInputIds") or [])
    records = state.get("operator_inputs") or {}
    order = list(dict.fromkeys([*(state.get("operator_input_order") or []), *records]))
    return [records[key] for key in order if key in records and key not in bound
            and isinstance(records[key], dict)
            and records[key].get("source") == "resume_cli"
            and str(records[key].get("text") or "").strip()]


def _bind_browser_resume_inputs(logger: RunLogger, inputs: list, phase_id: str) -> None:
    """Bind an amendment to an already-pending assignment before dispatch."""
    state = load_task_state(logger)
    bindings = state.setdefault("resume_input_bindings", {})
    for item in inputs:
        bindings[str(item["inputId"])] = phase_id
    write_task_state(logger, state)


async def _submit_browser_resume_amendment(
    harness: BrowserTaskRunner, *, resume_context: ResumeContext,
    phase_id: str, pending_inputs: list,
) -> JsonDict:
    """Append one direct continuation without replaying a terminal assignment."""
    old = resume_context.current_plan
    phases = old.get("phases") or []
    state = load_task_state(harness.logger)
    previous_status = str(((state.get("phases") or {}).get(phase_id) or {}).get("status") or "")
    if previous_status == "running":
        return _browser_mode_failure(harness, code="browser_mode_phase_running",
            error="Cannot amend a task while its previous phase is running.")
    used_ids = {str(item.get("id") or "") for item in phases if isinstance(item, dict)}
    ordinal = len(phases) + 1
    new_id = f"assignment_{ordinal:04d}"
    while new_id in used_ids:
        ordinal += 1
        new_id = f"assignment_{ordinal:04d}"
    replaces = phase_id if previous_status != "validated_done" else None
    instructions = [str(item["text"]) for item in pending_inputs]
    instruction = (instructions[0] if len(instructions) == 1 else
                   "\n\n".join(f"用户补充指令 {index}:\n{text}"
                                for index, text in enumerate(instructions, 1)))
    input_ids = [str(item["inputId"]) for item in pending_inputs]
    continuation = {
        "id": new_id, "type": "browser_worker", "objective": instruction,
        "worker_task": instruction, "stage_hint": "generic", "depends_on": [],
        "expected_artifact": {}, "validators": [],
        "worker_contract": {
            "_delegation": {"id": new_id, "lineage": phase_id,
                            "replaces": replaces, "reason": "User resume amendment",
                            "resumeInputIds": input_ids},
            "must_record_extraction": False,
            "stop_condition": "Verify the original request and ordered user amendments, then call final_answer.",
        },
        "max_steps": None, "max_attempts": None,
    }
    source_plan = {**copy.deepcopy(old), "phases": [*copy.deepcopy(phases), continuation]}
    plan, errors = validate_task_plan(
        source_plan, user_task=resume_context.original_user_task)
    if plan is None:
        return _browser_mode_failure(harness,
            code="browser_mode_amendment_contract_invalid", error="; ".join(errors))
    accept_task_plan(harness.logger, plan, previous_plan=old,
        replan_reason="User resume amendment", user_task=resume_context.original_user_task,
        validator_review=None, source_plan=source_plan, preserve_execution=True)
    _bind_browser_resume_inputs(harness.logger, pending_inputs, new_id)
    harness.task_plan = plan
    hint = harness.spawner.resume_browser_hint
    if hint is not None and hint.phase_id == phase_id:
        harness.spawner.resume_browser_hint = replace(hint, phase_id=new_id)
    harness.logger.write("direct_mode.amendment_registered", {
        "phaseId": new_id, "previousPhaseId": phase_id,
        "previousStatus": previous_status, "replaces": replaces,
        "resumeInputIds": input_ids,
    })
    await harness._bootstrap_schema_cache()
    result = await harness.spawn(new_id)
    if not isinstance(result, dict):
        return _browser_mode_failure(harness, code="browser_mode_amendment_no_receipt",
            error="Browser amendment dispatch returned no receipt.")
    return await _wait_for_browser_mode_result(harness, result)


async def _wait_for_browser_mode_result(
    harness: BrowserTaskRunner, spawned: JsonDict,
) -> JsonDict:
    """Collect exactly the dispatched worker, preserving its evidence and status.

    The spawn tool stays asynchronous for Lead orchestration. Waiting here has
    no polling or replay policy; cancellation is handled by the CLI owner while
    storage is still open.
    """
    if spawned.get("status") != "running":
        return spawned
    worker_id = spawned.get("workerId")
    if not isinstance(worker_id, str) or not worker_id.strip():
        return _browser_mode_failure(
            harness, code="browser_mode_worker_receipt_invalid",
            error="Worker startup returned running without a workerId.",
            tool_was_executed=True,
        )
    waited = await harness.wait(worker_id)
    completed = waited.get("completed") if isinstance(waited, dict) else None
    matches = [item for item in (completed if isinstance(completed, list) else [])
               if isinstance(item, dict) and item.get("workerId") == worker_id]
    phase_id = spawned.get("phaseId")
    if (len(matches) != 1
            or worker_id in (waited.get("pending") or [])
            or not matches[0].get("status")
            or matches[0]["status"] == "running"
            or (phase_id and matches[0].get("phaseId") != phase_id)):
        return _browser_mode_failure(
            harness, code="browser_mode_worker_receipt_invalid",
            error=f"Wait returned no consistent terminal receipt for {worker_id}.",
            tool_was_executed=True,
        )
    result = dict(matches[0])
    worker_status = result["status"]
    # Preserve the former direct entry's contract requirement without treating
    # contract validation alone as proof of goal completion.
    if worker_status == "done" and result.get("validatedStatus") != "validated_done":
        result["status"] = "incomplete"
        result["reason"] = (
            "Worker reported done, but the assignment contract was not validated "
            f"(validatedStatus={result.get('validatedStatus') or 'missing'})."
        )
    result["directExecution"] = {
        "mode": "browser", "workerId": worker_id,
        "phaseId": phase_id, "workerStatus": worker_status,
    }
    for key in ("assignmentId", "assignmentAccepted", "assignmentReview", "budget"):
        if key in spawned:
            result[key] = spawned[key]
    for key in ("connectionRecovery", "operatorInputRecords", "scheduleSnapshot"):
        if key in waited:
            result[key] = waited[key]
    return result


def _browser_mode_terminal_error(result: JsonDict) -> Optional[JsonDict]:
    """A durable reason for a non-success receipt, without inventing a cause."""
    status = str(result.get("status") or "unknown")
    if status in {"done", "completed", "validated_done"}:
        return None
    error = result.get("error")
    detail = error.get("message") if isinstance(error, dict) else error
    failure = {
        "code": result.get("code") or "browser_mode_not_completed",
        "message": str(detail or result.get("reason")
                       or f"Browser execution ended with status={status}.")[:2000],
        "status": status,
        "workerId": result.get("workerId"),
        "validatedStatus": result.get("validatedStatus"),
    }
    review = result.get("review")
    if isinstance(review, dict):
        failure["review"] = {
            key: review[key] for key in ("status", "auditPath", "errors", "verdict")
            if key in review
        }
    if isinstance(result.get("errors"), list):
        failure["errors"] = result["errors"]
    return failure


async def _shutdown_browser_mode(harness: BrowserTaskRunner) -> bool:
    """Drain cancellation records before storage closes, even on another cancel."""
    shutdown = asyncio.create_task(harness.spawner.shutdown())
    cancelled = False
    while True:
        try:
            await asyncio.shield(shutdown)
            return cancelled
        except asyncio.CancelledError:
            if shutdown.done():
                # An internal shutdown cancellation is a cleanup failure, not
                # permission to quietly close storage with unfinished workers.
                shutdown.result()
                return True
            cancelled = True


async def _submit_direct_plan(harness: BrowserTaskRunner, original_task: str) -> JsonDict:
    task = str(original_task or "").strip()
    if not task:
        return _browser_mode_failure(
            harness, code="browser_mode_task_missing",
            error="Browser mode requires the original user request.",
        )
    phase_id = "assignment_0001"
    phase = {
        "id": phase_id, "type": "browser_worker", "objective": task,
        "worker_task": task, "stage_hint": "generic", "depends_on": [],
        # Direct browsing has no invented extraction-row deliverable. Actual
        # user-requested files and rows are still validated by their tools.
        "expected_artifact": {}, "validators": [],
        "worker_contract": {
            "_delegation": {
                "id": phase_id, "lineage": phase_id,
                "replaces": None, "reason": "",
            },
            "must_record_extraction": False,
            "stop_condition": "Verify the original user goal, then call final_answer.",
        },
        "max_steps": None, "max_attempts": None,
    }
    source_plan = {
        "version": "v1", "execution_mode": "direct_worker",
        "goal": task, "phases": [phase],
    }
    plan, errors = validate_task_plan(source_plan, user_task=task)
    if plan is None:
        return _browser_mode_failure(
            harness, code="browser_mode_task_contract_invalid",
            error="; ".join(errors),
        )
    accept_task_plan(
        harness.logger, plan, previous_plan=None,
        replan_reason="Direct browser request", user_task=task,
        validator_review=None, source_plan=source_plan,
    )
    harness.task_plan = plan
    harness.logger.write("direct_mode.task_registered", {
        "phaseId": phase_id, "source": "original_user_request",
    })
    result = await harness.spawn(phase_id)
    return result if isinstance(result, dict) else _browser_mode_failure(
        harness,
        code="browser_mode_direct_pipeline_no_receipt",
        error="browser mode direct pipeline returned no receipt",
    )

