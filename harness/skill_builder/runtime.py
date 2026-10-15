"""Coding observes existing task evidence and trials through the actual Skill owner."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import uuid

from harness.skill_builder.catalog import inspect_skill
from harness.skill_runtime.plan import plan_schema, read_source
from harness.skill_runtime.session import SkillSession
from harness.skill_runtime.state import SkillStateStore
from harness.storage.sqlite_connection import write_transaction
from harness.storage.sqlite_store import build_resource_uri
from harness.utils import RunLogger


def consumer_contract(*, tools=None, mode="skill"):
    from harness.workflow_mode.prompt import DATA_BINDING_GUIDE
    from harness.workflow.script_runtime import execution_scope_contract
    return {"mode": mode, "orchestrationSchema": plan_schema(), "tools": [
        {"name": t.name, "description": t.description, "inputSchema": t.parameters}
        for t in (tools if tools is not None else SkillSession.__new__(SkillSession).tools())],
        "execution": "Programs run on WebCross. continueWhen reads /output/...; binding.pageId can use prior $result. "
                     "Business input is flat data. No continueWhen yields to the owner.",
        "repeatedRecordInputs": DATA_BINDING_GUIDE, "workflowScope": execution_scope_contract(),
        "validation": "Observe exact source/input hashes and real actions, not only workflow succeeded. "
                      "A successful Browser repair does not verify the original Workflow. "
                      "Use the actual Skill Runtime trial for newly generated or changed execution paths."}


def observe_source(builder, task_id=None, *, section=None, offset=0, max_chars=16000):
    source = builder._select_source(task_id)
    resources = source.resources(path_glob="skill/session.json", limit=10)
    resources = [r for r in resources if r.get("is_current", True)]
    if not resources:
        return {"taskId": source.task_id, "runtime": "browser_or_legacy", "manifest": source.manifest(),
                "invocations": source.invocations(), "skillRuntimeEvidence": None,
                "next": "Read the successful Browser operations; generated Workflow paths require a real Skill Runtime trial"}
    latest = max(resources, key=lambda r: (r.get("created_at", ""), r.get("resource_version", 0)))
    reference = build_resource_uri(source.task_id, latest["resource_id"])
    record = source.read_resource(reference)
    state = json.loads(record["content_json"])
    from harness.workflow_mode.artifacts import read_source_json
    package = (read_source_json(source, "workflow/package.json")
               or read_source_json(source, "skill/package.json") or {})
    from harness.skill_runtime.projection import page, evidence_summary
    if section:
        if section == "programEvidence":
            value = package.get(section)
        elif section in state:
            value = state[section]
        else:
            raise ValueError("Unknown state section")
        # Point at an immutable resource the active source reader can actually read.
        if section == "programEvidence":
            reference = None
            for path in ("workflow/package.json", "skill/package.json"):
                rows = [r for r in source.resources(path_glob=path, limit=10) if r.get("is_current", True)]
                if rows:
                    row = max(rows, key=lambda r: r.get("resource_version", 0))
                    reference = build_resource_uri(source.task_id, row["resource_id"])
                    break
        return {"taskId": source.task_id, "section": section,
            "pointer": "/" + section.replace("~", "~0").replace("/", "~1"),
            **page(value, reference=reference, offset=offset, max_chars=max_chars)}
    trials = state.get("workflowTrials") or {}
    compact_trials = {key: {k: value.get(k) for k in
        ("status", "path", "pageId", "sourceHash", "inputHash", "receiptRef", "delegationRef", "reason")}
        for key, value in trials.items()}
    return {"taskId": source.task_id, "runtime": state.get("mode", "skill"), "stateRef": reference,
            "goal": state.get("goal"), "status": state.get("status"), "skillHash": state.get("skillHash"),
            "operatorInputs": state.get("operatorInputs", []), "pendingInput": state.get("pendingInput"),
            "builderAcceptance": state.get("builderAcceptance"),
            "override": state.get("override"), "plan": state.get("plan"), "steps": state.get("steps"),
            "codingSessionId": state.get("codingSessionId"), "repairs": state.get("repairs"),
            "developmentSource": {k: (state.get("developmentSource") or {}).get(k) for k in ("path", "hash")}, "workflowTrials": compact_trials,
            "receipts": state.get("receipts"),
            "recoveries": state.get("recoveries", {}), "programEvidence": evidence_summary(package.get("programEvidence") or {}),
            "detailAccess": "observe_skill_run(section,offset,max_chars) pages stored sections; read_source_resource reads immutable stateRef and receipt refs",
            "interpretation": "These are execution facts and prior claims, not a recommendation to rewrite. "
                              "Compare exact executed versions, recovery actions and user goal; choose changes semantically."}


def active_trial(builder):
    return SkillStateStore(builder.logger).read("coding/active-trial.json", {})


def owned_trials(builder):
    """Retain earlier attempts when a new page or independent test is selected."""
    records = SkillStateStore(builder.logger).read("coding/trials.json", {})
    active = active_trial(builder)
    if active.get("taskId"):
        records.setdefault(active["taskId"], active)
    return records


async def check_trial_idle(builder, task_id):
    session = trial_owner(builder, task_id)
    try:
        state = session.store.load()
        for key in ("steps", "workflowTrials", "pageRecoveries"):
            if any(item.get("status") in {"running", "resuming"} for item in state.get(key, {}).values()):
                raise ValueError("The previous trial still has an active execution; finish or cancel and reconcile it first")
    finally:
        await session.close()


def draft_evidence(builder):
    from harness.skill_runtime.evidence import program_evidence
    from harness.skill_builder.context import SourceTaskContext
    from harness.workflow_mode.artifacts import read_source_json
    candidates = [builder.source]
    for task_id in owned_trials(builder):
        if task_id in builder.referenced_tasks:
            candidates.append(SourceTaskContext(task_id, builder.logger.task_dir.parent, builder.storage))
    imported = SkillStateStore(builder.logger).read("coding/workflow-import.json", {})
    if imported.get("taskId") in builder.referenced_tasks and imported["taskId"] not in {s.task_id for s in candidates}:
        candidates.append(SourceTaskContext(imported["taskId"], builder.logger.task_dir.parent, builder.storage))
    executions, checkpoints, recoveries, page_recoveries, origins = [], {}, {}, {}, []
    for source in candidates:
        state = read_source_json(source, "skill/session.json")
        if state:
            origins.append({"taskId": source.task_id, "mode": state.get("mode", "skill")})
            executions.extend(state.get("receipts", []))
            checkpoints.update(state.get("steps", {}))
            recoveries.update(state.get("recoveries", {}))
            page_recoveries.update(state.get("pageRecoveries", {}))
    # Imported evidence survives metadata-only edits and selecting a later run.
    executions.extend(imported.get("receipts", []))
    checkpoints = {**imported.get("steps", {}), **checkpoints}
    refs = {r["ref"]: r for r in executions}
    def read(reference):
        for source in candidates:
            try:
                row = source.read_resource(reference)
            except Exception:
                continue
            if row:
                value = row.get("content_json")
                return json.loads(value) if isinstance(value, str) else value
        raise ValueError("Draft execution evidence is outside the owned/referenced tasks")
    state = {"receipts": list(refs.values()), "steps": checkpoints, "recoveries": recoveries,
             "pageRecoveries": page_recoveries}
    result = program_evidence(state, builder.work_dir, read)
    result["sources"] = origins
    from harness.skill_builder.validation import evidence
    result["stageValidation"] = evidence(builder)
    return result


def trial_owner(builder, task_id, *, writing=False):
    if task_id not in builder.referenced_tasks:
        raise PermissionError("Trial is not owned by this Builder")
    logger = RunLogger(builder.runtime.harness.worktree_dir, task_id=task_id, storage=builder.storage)
    saved = SkillStateStore(logger).load()
    if saved.get("trialSource", {}).get("builderTaskId") != builder.logger.task_id:
        raise PermissionError("Trial belongs to another Builder")
    if writing:
        logger.run_id = "skill-trial-check-" + uuid.uuid4().hex[:12]
        builder.storage.start_run(task_id=task_id, run_id=logger.run_id, harness_version="skill-trial")
    config = copy.deepcopy(builder.runtime)
    config.harness.forced_skill_id = saved["skillId"]
    config.harness.forced_skill_hash = saved["skillHash"]
    # Reopening a Builder-owned trial must keep the same consumer profile as
    # run_trial. A Workflow parent config would look for this unpublished
    # task-local package in the published Skill catalog before dispatch.
    config.harness.agent_mode = "skill"
    config.harness.runs_dir = str(logger.task_dir)
    session = SkillSession(config, logger, skills_root=builder.catalog.root)
    if getattr(builder, "execution_owner", None):
        session.borrow_fleet_services(builder.execution_owner)
        session.execution_pages = {saved["browserBinding"]["pageId"]}
    return session


async def resolve_trial(builder, *, trial_task_id, step_id, decision, reason, evidence_refs, output=None):
    session = trial_owner(builder, trial_task_id, writing=True)
    status = "failed"
    try:
        from harness.skill_builder.recovery import _evidence, _page
        _page(builder, session)
        refs = _evidence(builder, session, evidence_refs)
        value = session.resolve_step(step_id, decision, reason, refs, output)
        status = "completed"
        return value
    finally:
        try:
            await session.close()
        finally:
            builder.storage.finish_run(task_id=trial_task_id, run_id=session.logger.run_id, status=status)


async def investigate_trial(builder, *, trial_task_id, instruction, review=False,
                            step_id=None, purpose="validation_recovery", success_criteria=None,
                            starting_state=None, evidence_refs=None):
    session = trial_owner(builder, trial_task_id, writing=True)
    status = "failed"
    try:
        from harness.skill_builder.recovery import _page, _evidence
        _page(builder, session, execution=True)
        refs = _evidence(builder, session, evidence_refs) if evidence_refs else []
        operation = {"kind": "browser"}
        if review:
            operation = {"kind": "review", "goal": session.state["goal"],
                "operatorInputs": session.state.get("operatorInputs", []), "summary": instruction,
                "programContext": {"mode": "builder", "evidence": session.program_evidence(),
                    "objective": "Assess the generated program's actual operation paths against this trial goal. "
                                 "Browser actions do not verify unexecuted code. Disclose missing coverage."}}
        if review:
            value = await session._delegate(operation, instruction)
        else:
            from harness.skill_runtime.delegation import delegate_browser
            value = await delegate_browser(session, instruction=instruction, purpose=purpose,
                step_id=step_id, success_criteria=success_criteria, starting_state=starting_state,
                evidence_refs=refs)
        status = "completed"
        return value
    finally:
        try:
            await session.close()
        finally:
            builder.storage.finish_run(task_id=trial_task_id, run_id=session.logger.run_id, status=status)


async def run_trial(builder, *, page_id, bindings, goal, plan=None, trial_task_id=None,
                    new_trial=False, reason=None, prepare_only=False):
    """Execute in the durable trial task; the calling Builder remains the brain."""
    if page_id not in builder.browser.pages:
        raise PermissionError("Authorize this page with attach_trial_page or create a Builder page first")
    if getattr(builder, "execution_owner", None) and page_id != builder.test_page_id:
        raise PermissionError("Trial must use the Builder's current test page")
    if new_trial and (trial_task_id or not isinstance(reason, str) or not reason.strip()):
        raise ValueError("A new trial needs a reason and cannot reuse trial_task_id")
    info = inspect_skill(builder.work_dir)
    config = copy.deepcopy(builder.runtime)
    config.harness.forced_skill_id = info["metadata"]["name"]
    config.harness.forced_skill_hash = info["hash"]
    config.harness.agent_mode = "skill"
    fleet = builder.browser.page_fleets[page_id]
    previous = active_trial(builder)
    if previous.get("taskId"):
        await check_trial_idle(builder, previous["taskId"])
    task_id = trial_task_id or (previous.get("taskId") if not new_trial and
        previous.get("fleetId") == fleet and previous.get("pageId", page_id) == page_id else None)
    saved = {}
    if task_id:
        prior = trial_owner(builder, task_id)
        try:
            saved = prior.state
            if saved.get("browserBinding", {}).get("fleetId") != fleet:
                raise PermissionError("Continue on the trial's originally authorized Fleet")
            if saved.get("browserBinding", {}).get("pageId") != page_id:
                raise ValueError("A different page requires a new trial; old checkpoints cannot move between pages")
            if saved["skillId"] != info["metadata"]["name"]:
                raise ValueError("Trial revision changed the package identity; start a new trial")
        finally:
            await prior.close()
    else:
        task_id = uuid.uuid4().hex
        builder.storage.create_task(task_id=task_id, harness_version="skill-trial")
    logger = RunLogger(config.harness.worktree_dir, task_id=task_id,
        run_id="skill-trial-" + uuid.uuid4().hex[:12], storage=builder.storage)
    builder.storage.start_run(task_id=task_id, run_id=logger.run_id, harness_version="skill-trial")
    config.harness.runs_dir = str(logger.task_dir)
    builder.referenced_tasks.add(task_id)
    with write_transaction(builder.storage.connection):
        builder.storage.connection.execute("UPDATE skill_builder_sessions SET referenced_tasks_json=? WHERE builder_task_id=?",
            (json.dumps(sorted(builder.referenced_tasks)), builder.logger.task_id))
    registry = owned_trials(builder)
    record = {"taskId": task_id, "fleetId": fleet, "pageId": page_id}
    if not saved:
        record.update(previousTrialId=previous.get("taskId"), reason=reason or "Initial trial on this page")
    registry[task_id] = {**registry.get(task_id, {}), **record}
    store = SkillStateStore(builder.logger)
    session, status = None, "failed"
    try:
        source = logger.task_dir / "skill" / ("versions/" + info["hash"] if saved else "source")
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(builder.work_dir, source)
        if inspect_skill(source)["hash"] != info["hash"]:
            raise ValueError("Draft changed while preparing trial")
        for file in info["files"]:
            path = source / file["path"]
            builder.storage.save_resource(task_id=task_id, run_id=logger.run_id,
                resource_type="skill_trial_source", logical_path=path.relative_to(logger.task_dir).as_posix(),
                content=path.read_text(), media_type="text/plain",
                metadata={"skillHash": info["hash"], "sha256": file["sha256"], "builderTaskId": builder.logger.task_id})
        from harness.tools.path_authorization import inherit_read_permissions
        session = SkillSession(config, logger, skills_root=builder.catalog.root)
        if getattr(builder, "execution_owner", None):
            session.borrow_fleet_services(builder.execution_owner)
            session.execution_pages = {builder.test_page_id}
        session.execution_pages = {page_id}
        inherit_read_permissions(builder.browser, session)
        session.state = saved or {"schemaVersion": 1, "skillId": info["metadata"]["name"],
            "goal": f"@{fleet} {goal}", "revision": 0, "steps": {}, "operatorInputs": [],
            "receipts": [], "repairs": {}, "workflowTrials": {}}
        session.state.update(skillHash=info["hash"], status="active",
            browserBinding={"pageId": page_id, "fleetId": fleet},
            trialSource={"path": source.relative_to(logger.task_dir).as_posix(), "hash": info["hash"],
                         "builderTaskId": builder.logger.task_id})
        for step in session.state["steps"].values():
            if step["status"] == "running":
                step.update(status="uncertain", reason="execution_interrupted")
        if getattr(builder, "execution_owner", None):
            parent = builder.execution_owner.store.load()
            session.state["goal"] = parent["goal"]
            session.state["validationAssignment"] = {"goal": goal,
                "parentTaskId": builder.execution_owner.logger.task_id, "pageId": page_id}
            existing = session.state.setdefault("operatorInputs", [])
            for answer in parent.get("operatorInputs", []):
                if answer not in existing:
                    existing.append(copy.deepcopy(answer))
        else:
            session.state.setdefault("operatorInputs", []).append({"source": "builder_trial", "text": goal})
        session.store.save(session.state)
        # A source/preparation failure must not replace a usable active pointer
        # with a task that has no reopenable session state.
        store.write("coding/trials.json", registry)
        store.write("coding/active-trial.json", record)
        session.bind_plan(bindings, plan or read_source(source, "orchestration.json"), "Builder revision trial")
        execution = ({"answer": {"status": "prepared", "pageId": page_id}, "execution": session.projection()}
                     if prepare_only else await session.run_workflows())
        result = {"status": "prepared" if prepare_only else "returned", "execution": execution, "programEvidence": session.program_evidence()}
        ref = session.store.write("skill/trial-result.json", result, "skill_trial_result")
        session.export_package()
        status = "completed"  # Execution returned; no business certification.
        from harness.skill_runtime.projection import execution_delta, evidence_summary
        projected = {"status": result["status"], "execution": execution_delta(execution),
            "programEvidence": evidence_summary(result["programEvidence"])}
        return {"trialTaskId": task_id, "pageId": page_id, "fleetId": fleet,
            "attempt": "continued" if saved else "new",
            "resultRef": ref, "sourceHash": info["hash"], "result": projected,
            "next": "Inspect raw receipts in this same Coding session. Use resolve_trial_step to reconcile effects, "
                    "then continue the trial after changing code or inputs; no nested Orchestrator."}
    except asyncio.CancelledError:
        status = "interrupted"
        if session is not None:
            interrupted = session.store.load()
            for step in interrupted.get("steps", {}).values():
                if step["status"] == "running":
                    step.update(status="uncertain", reason="execution_interrupted")
            session.store.save(interrupted)
        raise
    except Exception as exc:
        if session is not None:
            interrupted = session.store.load()
            for step in interrupted.get("steps", {}).values():
                if step["status"] == "running":
                    step.update(status="uncertain", reason="execution_interrupted")
            session.store.save(interrupted)
        logger.write("skill.trial.failed", {"trialTaskId": task_id, "error": str(exc)})
        raise ValueError(f"Skill trial {task_id} failed: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            if session is not None:
                await session.close()
        finally:
            builder.storage.finish_run(task_id=task_id, run_id=logger.run_id, status=status)
