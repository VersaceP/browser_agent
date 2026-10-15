"""Owned page recovery keeps the interrupted trial's program and checkpoints.

The Builder judges UI state and remaining work. This service checks identity and
evidence ownership only; it never chooses a close button or certifies an effect.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid

from harness.skill_builder.runtime import trial_owner
from harness.skill_runtime.plan import digest, read_source, validate_plan, step_identity
from harness.storage.sqlite_store import parse_resource_uri


def _context(session):
    state = session.store.load()
    value = {key: copy.deepcopy(state.get(key)) for key in
        ("plan", "bindings", "steps", "browserBinding", "trialSource")}
    value["browserBinding"] = {key: state.get("browserBinding", {}).get(key) for key in ("pageId", "fleetId")}
    return value


def _page(builder, session, *, execution=False):
    binding = session.state.get("browserBinding", {})
    page = binding.get("pageId")
    if (page not in builder.browser.pages
            or builder.browser.page_fleets.get(page) != binding.get("fleetId")):
        raise PermissionError("Recovery page is outside the Builder's existing authorization")
    if getattr(builder, "execution_owner", None) and page != builder.test_page_id:
        raise PermissionError("Validation recovery must stay on the selected trial page")
    if execution:
        from harness.tools.path_authorization import inherit_read_permissions
        inherit_read_permissions(builder.browser, session)
    # Even a standalone Builder's recovery remains on this trial page.
    session.execution_pages = {page}
    return page


def _evidence(builder, session, references):
    if not references:
        raise ValueError("Recovery needs recorded observations or execution evidence")
    result = []
    for reference in references:
        task, resource = parse_resource_uri(reference)
        if task not in {builder.logger.task_id, session.logger.task_id}:
            raise PermissionError("Recovery evidence must belong to this author or its owned trial")
        row = builder.storage.read_resource(current_task_id=task, resource_uri=reference)
        if not row or not row.get("content_available", True):
            raise ValueError(f"Recovery evidence is unavailable: {reference}")
        if task == session.logger.task_id:
            result.append(reference)
            continue
        value = row.get("content_json") or row.get("content_text")
        if value is None:
            raise ValueError("Use an indexed observation or receipt as recovery evidence")
        result.append(session.store.write("skill/recovery-evidence/" + resource + ".json",
            {"sourceReference": reference, "sha256": row.get("sha256"), "content": value},
            "skill_recovery_evidence"))
    return result


def _step(session, step_id):
    state = session.store.load()
    step = next((s for s in state.get("plan", {}).get("steps", []) if s["id"] == step_id), None)
    if not step or step["kind"] != "workflow":
        raise ValueError("Recovery must identify a Workflow in the preserved trial plan")
    if any(s.get("status") == "running" for s in state.get("steps", {}).values()):
        raise ValueError("Wait for or cancel the active trial before page recovery")
    return step


async def _close(builder, session, status):
    try:
        await session.close()
    finally:
        builder.storage.finish_run(task_id=session.logger.task_id,
            run_id=session.logger.run_id, status=status)


async def read_trial_state(builder, *, trial_task_id):
    session = trial_owner(builder, trial_task_id)
    try:
        # Ownership was checked by trial_owner. Historical state remains readable
        # after a page replacement; reading it never grants mutation of that page.
        state = session.store.load()
        return {"trialTaskId": trial_task_id, **_context(session),
            "pageRecoveries": state.get("pageRecoveries", {}),
            "next": "Inspect raw receipts and current page. Recovery scripts use run_trial_recovery; "
                    "resume_trial_recovery continues the preserved plan, not a failed JS stack."}
    finally:
        await session.close()


async def run_trial_recovery(builder, *, trial_task_id, step_id, path, reason,
                             evidence_refs, variables=None, recovery_id=None):
    if not reason.strip():
        raise ValueError("Explain the observed state and intended recovery")
    # Read through the Builder's path boundary, including its separate trials/.
    document = read_source(builder._path(path).parent, builder._path(path).name)
    session = trial_owner(builder, trial_task_id, writing=True)
    status, item = "failed", None
    try:
        page = _page(builder, session, execution=True)
        _step(session, step_id)
        refs = _evidence(builder, session, evidence_refs)
        state = session.store.load()
        if recovery_id:
            item = state.get("pageRecoveries", {}).get(recovery_id)
            if not item or item["stepId"] != step_id or item["status"] != "returned":
                raise ValueError("Continue a returned recovery; inspect/reconcile uncertain effects first")
            if digest(_context(session)) != item["contextHash"]:
                raise ValueError("Trial context changed; read_trial_state and reconcile the new plan")
        else:
            if any(r["status"] in {"running", "uncertain", "returned", "resuming"}
                   for r in state.get("pageRecoveries", {}).values()):
                raise ValueError("Resolve the existing page recovery before starting another")
            recovery_id = uuid.uuid4().hex
            original = _context(session)
            context_ref = session.store.write("skill/page-recovery/" + recovery_id + "-context.json", original)
            item = {"recoveryId": recovery_id, "stepId": step_id, "pageId": page,
                "contextHash": digest(original), "contextRef": context_ref, "executions": []}
        execution_id = uuid.uuid4().hex
        directory = session.logger.task_dir / "skill" / "page-recovery" / execution_id
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text((session.source_root() / "SKILL.md").read_text())
        (directory / "workflow.json").write_text(json.dumps(document, ensure_ascii=False))
        item.update(status="running", reason=reason, evidenceRefs=refs)
        item["executions"].append(execution_id)
        state.setdefault("pageRecoveries", {})[recovery_id] = item
        session.store.record(state, "skill.page_recovery.started", copy.deepcopy(item))
        try:
            result = await session._execute_probe(directory, path="workflow.json", page_id=page,
                variables=variables or {}, probe_key="page-recovery-" + recovery_id, execution_id=execution_id)
        except BaseException:
            state = session.store.load()
            state["pageRecoveries"][recovery_id]["status"] = "uncertain"
            session.store.save(state)
            raise
        state = session.store.load()
        item = state["pageRecoveries"][recovery_id]
        item.update(status="returned", result=result)
        ref = session.store.record(state, "skill.page_recovery.returned", copy.deepcopy(item))
        status = "completed"
        return {"trialTaskId": trial_task_id, "recoveryId": recovery_id, "receiptRef": ref,
            "result": result, "interruptedContextRef": item["contextRef"],
            "next": "Read current page and reconcile prior effects. Choose continue/complete/defer with "
                    "resume_trial_recovery. Recovery execution is not proof of original-program success."}
    except asyncio.CancelledError:
        status = "interrupted"
        raise
    finally:
        await _close(builder, session, status)


async def resume_trial_recovery(builder, *, trial_task_id, recovery_id, decision,
                                reason, evidence_refs, remaining_input=None, output=None):
    if decision not in {"continue", "complete", "defer"} or not reason.strip():
        raise ValueError("Choose continue/complete/defer and explain current effects and the next start state")
    if remaining_input is not None and decision != "continue":
        raise ValueError("Remaining input only applies to a fresh continuation invocation")
    session = trial_owner(builder, trial_task_id, writing=True)
    status = "failed"
    try:
        _page(builder, session, execution=True)
        state = session.store.load()
        item = state.get("pageRecoveries", {}).get(recovery_id)
        if not item or item["status"] not in {"returned", "uncertain"}:
            raise ValueError("Inspect an owned returned/uncertain recovery before deciding")
        refs = _evidence(builder, session, evidence_refs)
        if digest(_context(session)) != item["contextHash"] and decision != "defer":
            raise ValueError("Trial plan, version, input, page or checkpoints changed; read_trial_state. "
                             "Defer this stale recovery and reconcile the current plan instead")
        # Reject malformed/stale decisions before consuming the recovery. These
        # checks do not decide whether the observed business effects are enough.
        plan = None
        if decision != "defer":
            step = _step(session, item["stepId"])
            if remaining_input is not None:
                plan = copy.deepcopy(state["plan"])
                next(s for s in plan["steps"] if s["id"] == item["stepId"])["input"] = remaining_input
                validate_plan(plan, session.source_root())
            if decision == "complete":
                results = {sid: r["output"] for sid, r in state["steps"].items() if "output" in r}
                if output is not None:
                    results[item["stepId"]] = output
                step_identity(session.source_root(), step, state["bindings"], results, state.get("browserBinding", {}))
        # This is the Builder's explicit evidence-based assessment, including
        # uncertain side effects. A successful probe alone never triggers retry.
        for execution_id in item["executions"]:
            probe = state.get("workflowTrials", {}).get(execution_id)
            if probe and probe["status"] in {"running", "uncertain", "returned"}:
                probe.update(status="reconciled", reason=reason, evidenceRefs=refs)
        item.update(status="deferred" if decision == "defer" else "resuming",
                    decision=decision, resolutionReason=reason, resolutionEvidenceRefs=refs)
        decision_ref = session.store.record(state, "skill.page_recovery.decided", copy.deepcopy(item))
        if decision == "defer":
            status = "completed"
            return {"status": "deferred", "receiptRef": decision_ref, "trialTaskId": trial_task_id}
        try:
            session.resolve_step(item["stepId"], "retry" if decision == "continue" else "complete", reason, refs, output)
            # Execute the saved version. Workspace changes require the normal
            # revision trial path, never implicit activation during recovery.
            execution = await session.run_workflows(attempt_inputs=
                {item["stepId"]: remaining_input} if remaining_input is not None else None)
        except BaseException:
            state = session.store.load()
            state["pageRecoveries"][recovery_id]["status"] = "uncertain"
            for step in state["steps"].values():
                if step["status"] == "running":
                    step.update(status="uncertain", reason="recovery_continuation_interrupted")
            session.store.save(state)
            raise
        state = session.store.load()
        item = state["pageRecoveries"][recovery_id]
        item.update(status="continued", execution=execution)
        ref = session.store.record(state, "skill.page_recovery.continued", copy.deepcopy(item))
        session.export_package()
        status = "completed"
        return {"status": "returned", "trialTaskId": trial_task_id, "receiptRef": ref,
            "execution": execution, "programEvidence": session.program_evidence(),
            "next": "Inspect actual output and remaining work. This was a fresh invocation of the saved "
                    "program, not continuation from a failed JavaScript instruction. Remaining input never replaces the full plan; reconcile the full stage before proceeding."}
    except asyncio.CancelledError:
        status = "interrupted"
        raise
    finally:
        await _close(builder, session, status)
