"""Bounded Browser subgoals with the existing Worker handoff/continuation protocol."""
from __future__ import annotations

import copy
import json

from harness.skill_runtime.plan import bind, digest

BROWSER_RESULT_CONTRACT = """Return structured JSON with outcome, completedActions, observedEffects,
currentPage {pageId,url,state}, evidence (raw receipt references), remainingWork,
uncertainEffects, blockers, and next_steps. Distinguish attempted actions from
observed effects and requested scope from unverified coverage. Use the existing
continuation protocol to return unresolved decisions to the task owner. This is
a subgoal receipt, never a claim that the whole user task is done. Do not invent
permission or replace human decisions. Preserve original platform errors."""

BROWSER_DELEGATION_GUIDE = """
Browser delegation is a normal execution choice, including a planned kind=browser
stage. Use it for semantic/visual decisions, an unfamiliar control, a temporary
modal or page transition, uncertain partial effects, a one-off or unsupported
operation, or a region whose reliable scripting is not worthwhile for this task.
You need not manufacture a Workflow failure first. Use native observations for
simple reads and Workflow for already-known stable repeated actions.
For delegate_browser supply the bounded instruction, purpose, success_criteria,
step_id when applicable, evidence_refs and the expected starting page/state.
The host adds original goal, human answers, inputs, exact stage/version and
checkpoint facts. Explain allowed actions and any side effects to preserve;
these are a subgoal contract, not new authorization. Browser can inspect, restore
or complete this subgoal and must return effects, raw evidence, current state,
uncertainties and remaining work. It may ask the user directly via existing HITL.
It must not replay completed work, restart the task, silently change business
choices or repair/publish the Skill. The same page has one controller at a time.
Inspect its receipt, reconcile the affected step, then continue the sequence.
Planned Browser stages are valid mixed orchestration. Temporary Browser rescue
can complete business work but never proves the failed Workflow was reusable.
Keep code defects and validation limits explicit for later repair.
"""


async def delegate_browser(owner, *, instruction, purpose="bounded_subgoal", success_criteria=None,
                           step_id=None, evidence_refs=None, starting_state=None):
    if not instruction.strip():
        raise ValueError("Browser delegation needs a bounded instruction")
    state = owner.store.load()
    refs = evidence_refs or []
    if refs:
        owner._check_evidence(refs)
    step = next((s for s in state.get("plan", {}).get("steps", []) if s["id"] == step_id), None)
    if step_id and step is None:
        raise ValueError(f"Unknown Browser subgoal stage: {step_id}")
    if step_id and state["steps"].get(step_id, {}).get("status") == "running":
        raise ValueError("Wait for or cancel the active stage and reconcile effects before Browser delegation")
    page_id = state.get("browserBinding", {}).get("pageId")
    if step:
        results = {sid: r["output"] for sid, r in state["steps"].items() if "output" in r}
        page_id = bind(step.get("binding", {}), state.get("bindings", {}), results).get("pageId", page_id)
    if not page_id:
        raise ValueError("Bind an evidenced page before delegating a Browser subgoal")
    assignment = {"instruction": instruction, "purpose": purpose,
        "successCriteria": success_criteria or [], "startingState": starting_state or {},
        "stepId": step_id, "stage": copy.deepcopy(step),
        "stageContractHash": digest(step) if step else None,
        "checkpoint": copy.deepcopy(state.get("steps", {}).get(step_id)),
        "bindings": copy.deepcopy(state.get("bindings", {})),
        "pageId": page_id, "fleetId": state.get("browserBinding", {}).get("fleetId"),
        "evidenceRefs": refs, "source": copy.deepcopy(state.get("developmentSource") or
            state.get("trialSource") or {"skillId": state["skillId"], "hash": state["skillHash"]}),
        "scope": "Complete only this subgoal, preserve prior effects, report remaining/uncertain work. "
                 "Business choices remain with the user/owner; no implicit source edit or publication."}
    assignment_ref = owner.store.record(state, "skill.browser.assigned", assignment)
    result = await owner._delegate({"kind": "browser", "pageId": page_id,
        "executionPages": [page_id], "assignment": assignment, "assignmentRef": assignment_ref,
        "resultContract": BROWSER_RESULT_CONTRACT}, instruction)
    state = owner.store.load()
    receipt = {"assignmentRef": assignment_ref, "stepId": step_id,
        "result": result, "checkpointEffect": "Await semantic reconciliation; no automatic Workflow certification"}
    ref = owner.store.record(state, "skill.browser.returned", receipt)
    return {**result, "assignmentRef": assignment_ref, "subgoalReceiptRef": ref}
