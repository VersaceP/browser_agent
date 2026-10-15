"""Independent trial state owned by the same author; no second model or copy merge."""
from __future__ import annotations
from harness.skill_builder.runtime import owned_trials, trial_owner
from harness.runtime.program_validation import stages, evidence, read_receipt, record_assessment


async def prepare_trial(owner, page_id):
    state = owner.store.load()
    page = state.get("workflowTestPages", {}).get(page_id)
    if not page or page_id == state.get("browserBinding", {}).get("pageId"):
        raise PermissionError("Use a task-owned independent page created with create_workflow_test_page")
    # Drain the main executor before transferring control to a trial. This is
    # sequential by design; the Fleet-wide authorization service stays shared.
    await owner.executor.close()
    builder = owner.foreground()
    builder.execution_owner = owner
    builder.test_page_id = page_id
    builder.browser.pages.add(page_id)
    builder.browser.page_fleets[page_id] = page["fleetId"]
    from harness.tools.path_authorization import inherit_read_permissions
    inherit_read_permissions(owner, builder.browser)
    return builder


async def prepare_owned_trial(owner, trial_task_id):
    builder = owner.foreground()
    record = owned_trials(builder).get(trial_task_id)
    if not record:
        raise PermissionError(f"Validation trial is not owned by this author: {trial_task_id}")
    return await prepare_trial(owner, record["pageId"])


def sync_trials(owner):
    state = owner.store.load()
    state["validationTrials"] = owned_trials(owner.foreground())
    owner.store.save(state)


async def inspect_page(owner, *, trial_task_id, method, params):
    builder = await prepare_owned_trial(owner, trial_task_id)
    session = trial_owner(builder, trial_task_id, writing=True)
    status = "failed"
    try:
        from harness.skill_runtime.observation import observe
        result = await observe(session, method=method, params=params)
        status = "completed"
        return result
    finally:
        await session.close()
        builder.storage.finish_run(task_id=trial_task_id, run_id=session.logger.run_id, status=status)


