"""Standalone draft adapter for the shared program-validation services."""
from __future__ import annotations

from harness.skill_runtime.state import SkillStateStore
from harness.skill_builder.runtime import owned_trials, trial_owner
from harness.runtime import program_validation


class DraftValidationStore(SkillStateStore):
    def load(self):
        return self.read("coding/validation.json", {})

    def save(self, state):
        return self.write("coding/validation.json", state, "program_validation")


class DraftValidation:
    """Data adapter, not a second agent or a second source workspace."""
    def __init__(self, builder):
        self._foreground = builder
        self.logger = builder.logger
        self.store = DraftValidationStore(builder.logger)
        self.allowed_evidence_tasks = set(builder.referenced_tasks)

    def foreground(self):
        return self._foreground

    def source_root(self):
        return self._foreground.work_dir


def evidence(builder):
    result = program_validation.evidence(DraftValidation(builder), trials=owned_trials(builder))
    result["testPages"] = SkillStateStore(builder.logger).read("coding/test-pages.json", {}).get("pages", {})
    return result


def record_assessment(builder, **args):
    owner = DraftValidation(builder)
    state = owner.store.load()
    state["validationTrials"] = owned_trials(builder)
    state["workflowTestPages"] = SkillStateStore(builder.logger).read("coding/test-pages.json", {}).get("pages", {})
    owner.store.save(state)
    return program_validation.record_assessment(owner, **args)


def read_receipt(builder, **args):
    return program_validation.read_receipt(DraftValidation(builder), **args)


async def restore_trial(builder, trial_task_id):
    session = trial_owner(builder, trial_task_id)
    try:
        await builder.browser.restore_page(session.state["browserBinding"]["pageId"])
    finally:
        await session.close()


async def inspect_page(builder, *, trial_task_id, method, params):
    session = trial_owner(builder, trial_task_id, writing=True)
    status = "failed"
    try:
        from harness.skill_builder.recovery import _page
        await builder.browser.restore_page(session.state["browserBinding"]["pageId"])
        _page(builder, session)
        from harness.skill_runtime.observation import observe
        result = await observe(session, method=method, params=params)
        status = "completed"
        return result
    finally:
        await session.close()
        builder.storage.finish_run(task_id=trial_task_id, run_id=session.logger.run_id, status=status)
