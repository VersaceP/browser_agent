"""Runtime-owned delegation contracts, independent of a worker implementation.

The persisted ledger uses the runtime's phase/artifact storage format.
It is not a model-authored definition of the whole user's goal. Assignments
are appended; execution history is never rewritten to make a revision pass.
"""
from __future__ import annotations

import copy

from harness.task_control import find_phase, load_task_state
from harness.tools.registry import ToolContext


def spawn_schema(output_schema, routing_schema):
    return {
        "type": "object",
        "properties": {
            "assignment": {
                "type": "object",
                "description": "New work or a revision. Omit when continuing an existing phase_id.",
                "properties": {
                    "task": {"type": "string", "minLength": 1},
                    "stage_hint": {"type": "string", "description": "Optional capability stage; defaults to generic."},
                    "inputs": {"type": "object", "description": "Optional direct:{rows,identity_fields} or artifact:{phase_id,artifact_name,selector?}."},
                    "depends_on": {"type": "array", "items": {"type": "string"},
                                   "description": "Existing producer assignment IDs. Omit for independent work."},
                    "output": output_schema,
                    "checks": {"type": "array", "items": {"type": "object"},
                               "description": "Optional objective artifact checks (count, identity, file integrity, provenance)."},
                    "policy": {"type": "object", "description": "Optional worker capability/session/skill restrictions. Cannot grant permissions or override the output."},
                    "budget": {"type": "object", "properties": {
                        "max_steps": {"type": "integer", "minimum": 1},
                        "max_attempts": {"type": "integer", "minimum": 1},
                    }, "additionalProperties": False},
                    "replaces": {"type": "string", "description": "Existing inactive assignment ID being revised. Preserves its history and spent budget."},
                    "reason": {"type": "string", "description": "Why this revision is needed; required with replaces."},
                },
                "required": ["task"], "additionalProperties": False,
            },
            "phase_id": {"type": "string", "minLength": 1,
                         "description": "Existing accepted assignment ID to continue. Mutually exclusive with assignment."},
            **routing_schema,
        },
        "additionalProperties": False,
    }


def _failure(code, detail, **facts):
    return {"status": code, "tool_was_executed": False, "error": detail, **facts}


def _assignment_candidate(agent, assignment):
    old = agent.task_plan or {}
    phases = copy.deepcopy(old.get("phases") or [])
    state = load_task_state(agent.logger)
    replaces = str(assignment.get("replaces") or "")
    predecessor = find_phase(old, replaces) if replaces else None
    if replaces and predecessor is None:
        return None, _failure("unknown_assignment", "replaces must identify an existing assignment")
    if predecessor and not str(assignment.get("reason") or "").strip():
        return None, _failure("revision_reason_required", "Explain the revision in assignment.reason")
    previous_state = (state.get("phases") or {}).get(replaces, {})
    if predecessor and (previous_state.get("status") == "running" or previous_state.get("superseded_by")):
        return None, _failure("assignment_not_revisable", "Wait for a live worker; revise the latest version only.", phaseId=replaces)
    ident = f"assignment_{len(phases) + 1:04d}"
    while find_phase(old, ident):
        ident += "_new"
    policy = copy.deepcopy(assignment.get("policy") or {})
    forbidden = {"_delegation", "expected_artifact", "validators", "phase_id", "task_type",
                 "fleet_id", "max_steps", "max_attempts"}.intersection(policy)
    if forbidden:
        return None, _failure("invalid_assignment_policy", "Policy cannot overwrite runtime identity, output or budget.", fields=sorted(forbidden))
    previous_meta = ((predecessor or {}).get("worker_contract") or {}).get("_delegation") or {}
    lineage = previous_meta.get("lineage") or replaces or ident
    policy["_delegation"] = {"id": ident, "lineage": lineage, "replaces": replaces or None,
                             "reason": assignment.get("reason") or ""}
    budget = assignment.get("budget") or {}
    max_attempts = budget.get("max_attempts", (predecessor or {}).get("max_attempts"))
    # The default receipt records observations, not one invented row per UI field.
    output = copy.deepcopy(assignment.get("output") or {
        "name": f"{ident}_receipt", "fields": ["observation", "evidence"],
        "required_fields": ["observation", "evidence"],
        "nonempty_fields": ["observation", "evidence"], "min_rows": 1,
        "provenance_required": ["observation"],
    })
    phase = {"id": ident, "type": "browser_worker",
             "objective": assignment["task"], "worker_task": assignment["task"],
             "stage_hint": assignment.get("stage_hint") or "generic",
             "expected_artifact": output, "worker_contract": policy,
             "depends_on": list(assignment.get("depends_on") or []),
             "max_steps": budget.get("max_steps", (predecessor or {}).get("max_steps")),
             "max_attempts": max_attempts}
    if assignment.get("inputs"):
        phase["inputs"] = copy.deepcopy(assignment["inputs"])
        refs = assignment["inputs"].get("artifact")
        for ref in (refs if isinstance(refs, list) else [refs]):
            producer = ref.get("phase_id") if isinstance(ref, dict) else None
            if producer and producer not in phase["depends_on"]:
                phase["depends_on"].append(producer)
    if assignment.get("checks"):
        phase["additional_checks"] = copy.deepcopy(assignment["checks"])
    phases.append(phase)
    return {**copy.deepcopy(old), "version": "v1", "execution_mode": "delegated",
            "goal": old.get("goal") or agent.original_user_task,
            "phases": phases, "replan_reason": assignment.get("reason") or "Register the next delegation"}, None


def delegation_budget(agent, phase_id, *, plan=None):
    plan = plan if plan is not None else agent.task_plan
    phase = find_phase(plan, phase_id) or {}
    meta = (phase.get("worker_contract") or {}).get("_delegation") or {}
    lineage = meta.get("lineage")
    if not lineage:
        return None
    state = load_task_state(agent.logger)
    used = 0
    for item in plan.get("phases") or []:
        item_meta = (item.get("worker_contract") or {}).get("_delegation") or {}
        if item_meta.get("lineage", item.get("id")) == lineage:
            used += len(((state.get("phases") or {}).get(item["id"]) or {}).get("attempts") or [])
    limit = phase.get("max_attempts")
    return {"lineageId": lineage, "attemptsUsed": used, "maxAttempts": limit,
            "remainingAttempts": max(0, limit - used) if isinstance(limit, int) else None}


def _dispatch_readiness(agent, plan, phase_id):
    """Read-only scheduling check; the actual spawn checks again after awaits."""
    from harness.task_control.phase_lifecycle import phase_start_rejection
    phase_state = (load_task_state(agent.logger).get("phases") or {}).get(phase_id, {})
    if phase_state.get("superseded_by"):
        return _failure("assignment_superseded", "Continue the current assignment version.",
                        phaseId=phase_state["superseded_by"])
    rejection = phase_start_rejection(plan, agent.logger, phase_id=phase_id,
                                      persist_dependency_failure=False)
    if rejection:
        return rejection
    budget = delegation_budget(agent, phase_id, plan=plan)
    if budget and budget["remainingAttempts"] == 0:
        return _failure("assignment_budget_exhausted",
                        "The lineage budget is spent. A reviewed revision can change its allocation.", budget=budget)
    return None


async def dispatch_assignment(ctx, execute):
    """One model-facing path for fresh, continuing, revised and restored work."""
    agent = ctx.agent
    args = copy.deepcopy(ctx.tool_input)
    assignment = args.pop("assignment", None)
    phase_id = args.get("phase_id")
    if bool(assignment) == bool(phase_id):
        return _failure("assignment_input_invalid", "Supply exactly one of assignment or phase_id.")
    accepted = {}
    if assignment:
        candidate, error = _assignment_candidate(agent, assignment)
        if error:
            return error
        compiled, errors, issues, _ = agent._compile_assignment_candidate(candidate)
        if compiled is None:
            return _failure("assignment_rejected", "Correct the submitted assignment.", errors=errors, repairIssues=issues)
        readiness = _dispatch_readiness(agent, compiled, compiled["phases"][-1]["id"])
        if readiness:
            return {**readiness, "assignmentAccepted": False, "acceptedAssignmentsUnchanged": True}
        review = await agent.review_assignment_candidate(candidate)
        if review.get("status") in {"mechanical_invalid", "rejected"}:
            return _failure("assignment_rejected", "Correct the submitted assignment using these findings.", review=review)
        accepted = await agent.approve_and_accept_assignment(candidate, review=review)
        if accepted.get("status") != "done":
            return accepted
        phase_id = candidate["phases"][-1]["id"]
        args["phase_id"] = phase_id
    elif not find_phase(agent.task_plan, str(phase_id)):
        return _failure("unknown_assignment", "No accepted assignment has this phase_id.")
    else:
        readiness = _dispatch_readiness(agent, agent.task_plan, str(phase_id))
        if readiness:
            return {**readiness, "assignmentId": phase_id, "assignmentAccepted": True}
        # Resume approval is a runtime responsibility; the model never copies a
        # recovered contract merely to obtain the missing approval receipt.
        approval_error = agent.task_plan_user_approval_rejection()
        if approval_error:
            if approval_error.get("status") != "plan_user_approval_required":
                return approval_error
            accepted = await agent.approve_existing_assignment(str(phase_id))
            if accepted.get("status") != "done":
                return accepted
    state = load_task_state(agent.logger)
    phase_state = (state.get("phases") or {}).get(phase_id, {})
    if phase_state.get("superseded_by"):
        return _failure("assignment_superseded", "Continue the current assignment version.",
                        phaseId=phase_state["superseded_by"])
    budget = delegation_budget(agent, phase_id)
    if budget and budget["remainingAttempts"] == 0:
        return _failure("assignment_budget_exhausted", "The lineage budget is spent. A reviewed revision can change its allocation.", budget=budget)
    result = await execute(ToolContext(agent=agent, tool_call=ctx.tool_call,
                                      tool_input=args, step=ctx.step))
    if isinstance(result, dict):
        phase = find_phase(agent.task_plan, phase_id)
        result["assignmentId"] = phase_id
        result["assignmentAccepted"] = True
        if result.get("tool_was_executed") is False:
            result["resumeInstruction"] = (
                f"After resolving this blocker, use phase_id={phase_id} to continue the accepted assignment."
            )
        result["operatorInputRecords"] = accepted.get("operatorInputRecords") or []
        result["budget"] = delegation_budget(agent, phase_id)
        result["reviewScope"] = "Assignment contract only; Lead must judge the original goal from returned evidence."
        result["assignmentReview"] = accepted.get("assignmentReview") or {}
    return result


def worker_return_receipt(result):
    """No new verdict: expose distinct facts alongside the legacy status."""
    if not isinstance(result, dict):
        return result
    validation = result.get("artifactValidation") or {}
    return {"workerStatus": result.get("status"),
            "contractChecks": validation,
            "artifacts": result.get("artifacts") or [],
            "continuation": result.get("continuation"),
            "goalCompletion": "requires_lead_judgment"}
