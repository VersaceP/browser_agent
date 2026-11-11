"""Attach an independently created draft to its first durable author session.

This is initialization, not recovery of missing conversation or execution history.
Only an absent registration in a successfully opened database permits it. Existing
task records, unavailable databases and legacy state retain strict resume errors.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.runtime.resume_state import (
    TaskRecordMissingError, acquire_run_lock, load_task_manifest, release_run_lock,
    write_task_manifest,
)
from harness.workflow_mode import package as pkg
from harness.workflow_mode.registry import Registry, RegistryError


async def ensure_author_task(runtime: Any, args: Any, *, host: Any,
                             task_id: str, request: str) -> dict[str, Any]:
    """Keep a registered author intact, or initialize this draft under its lock."""
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        raise RegistryError("Invalid draft task ID")
    registry = Registry.for_runtime(runtime)
    directory = registry.worktree / task_id
    if any(path.is_symlink() for path in (directory, directory / "workflow", registry.draft_path(task_id))):
        raise RegistryError("Author task and draft directories must not be symbolic links")
    # Do not convert a missing database, a malformed manifest or a registered
    # task with a lost manifest into permission to start over.
    try:
        return load_task_manifest(directory, required=True)
    except TaskRecordMissingError:
        pass

    lock = acquire_run_lock(directory)
    logger = session = None
    opened = False
    status = "failed"
    try:
        # Another editor may have initialized the task before we took its lock.
        try:
            return load_task_manifest(directory, required=True)
        except TaskRecordMissingError:
            pass
        draft = registry.read_draft(task_id)
        if not draft or draft.get("taskId") != task_id or draft.get("status") == "discarded":
            raise RegistryError("This task has no editable draft to attach")
        source = registry.draft_path(task_id)
        contract = pkg.load_contract(source)
        if (contract["templateId"] != draft.get("templateId")
                or contract["slug"] != draft.get("slug")):
            raise RegistryError("Draft record identity differs from its contract")
        for relative in ("task_manifest.json", "task_plan.json", "task_state.json", "run.jsonl",
                         "workflow/session.json", "workflow/context.json", "workflow/messages.json",
                         "workflow/conversation"):
            if (directory / relative).exists():
                raise RegistryError(
                    f"Unregistered draft has saved task state at {relative}; "
                    "restore or migrate its task records before resuming"
                )
        origin = {"kind": "existing-draft", "taskId": task_id,
                  "templateId": contract["templateId"], "draftHash": pkg.content_hash(source),
                  "baseHash": draft.get("baseHash"), "draftRequest": draft.get("request"),
                  "historyRestored": False}
        logger = host._create_task_logger(runtime, task_id=task_id)
        logger.run_id = host._new_run_id(resumed=False)
        host._open_task_storage(logger, runtime)
        opened = True
        manifest = write_task_manifest(logger, original_user_task=request, task_id=task_id,
            config_path=getattr(args, "config", None), agent_id=runtime.agent_id,
            startup_args={"agent_mode": "workflow", "workflowProfile": "author",
                          "workflowSlug": draft["slug"], "workflowAuthorOrigin": origin})
        from harness.workflow_mode.session import WorkflowSession
        session = WorkflowSession(runtime, logger, profile="author", host=host, quiet=True)
        session.initialize(request, draft=draft)
        session.state["authorTaskInitialization"] = origin
        session.save()
        logger.write("workflow.author.initialized", origin)
        status = "completed"
        return manifest
    finally:
        try:
            if session is not None:
                await session.close()
        finally:
            try:
                if logger is not None:
                    if opened:
                        errors = host._close_task_storage(logger, status=status)
                        if errors:
                            raise RegistryError("Author initialization could not be persisted: " + "; ".join(errors))
                    else:
                        logger.storage.close()
            finally:
                release_run_lock(lock)
