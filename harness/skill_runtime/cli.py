"""Independent CLI lifecycle; no task owner or standalone Browser task loop."""
from __future__ import annotations

import asyncio
import json
import sys

from harness.runtime.resume_state import (acquire_run_lock, release_run_lock,
                                         load_task_manifest, write_task_manifest)
from harness.skill_runtime.session import SkillSession
from harness.runtime.session_output import block, result as print_result


async def run_skill_cli(runtime, args, task: str, *, host) -> int:
    mode = runtime.harness.agent_mode
    development = mode == "workflow"
    label = "Workflow" if development else "Skill"
    if sys.version_info < (3, 12):
        print(f"{label} mode requires Python 3.12+.", flush=True)
        return host.CLI_INPUT_ERROR_EXIT_CODE
    logger = None
    session = None
    lock = None
    opened = False
    result = None
    status = "interrupted"
    exit_code = 0
    cleanup_error = None
    try:
        resume = str(getattr(args, "resume", "") or "").strip()
        manifest = None
        if resume:
            directory = host._resolve_resume_directory(resume)
            manifest = load_task_manifest(directory)
            if not manifest or (manifest.get("startup_args") or {}).get("agent_mode") != mode:
                raise ValueError(f"This task is not an independent {label} session")
            runtime.harness.worktree_dir = str(directory.parent)
            skill_id = manifest.get("forced_skill_id")
            skill_hash = (manifest.get("startup_args") or {}).get("forced_skill_hash")
            if not development:
                if not skill_id or not skill_hash: raise ValueError("Saved Skill binding is missing")
                host._validate_resume_skill(runtime, getattr(args, "skill", ""), skill_id)
                runtime.harness.forced_skill_id = skill_id
                runtime.harness.forced_skill_hash = skill_hash
            logger = host._create_task_logger(runtime, task_id=directory.name)
        else:
            if not development and not runtime.harness.forced_skill_id: raise ValueError("Use /<Skill-name-or-ID> [@Fleet-ID] task or --skill <name> first")
            logger = host._create_task_logger(runtime)
        host._LAST_LOGGER = logger
        logger.run_id = host._new_run_id(resumed=bool(resume))
        lock = acquire_run_lock(logger.task_dir)
        host._open_task_storage(logger, runtime)
        opened = True
        runtime.harness.runs_dir = str(logger.task_dir)
        if not resume:
            write_task_manifest(logger, original_user_task=task, task_id=logger.task_id,
                config_path=args.config, forced_skill_id=runtime.harness.forced_skill_id,
                agent_id=runtime.agent_id, startup_args={"agent_mode": mode,
                    "forced_skill_hash": runtime.harness.forced_skill_hash})
        version = ("task-local development revisions" if development else
                   f"version: {runtime.harness.forced_skill_id}@{runtime.harness.forced_skill_hash[:12]}")
        quiet = getattr(args, "output", "auto") == "json"
        if not quiet:
            block(label, f"任务 @{logger.task_id}", [("目录", logger.task_dir), ("版本", version),
                ("恢复", f"/resume {logger.task_id}")])
        if development:
            from harness.workflow_mode.session import WorkflowSession
            session = WorkflowSession(runtime, logger)
        else:
            session = SkillSession(runtime, logger)
        session.quiet = quiet
        if resume and not session.state: raise ValueError("Saved Skill execution state is missing")
        result = await session.run((manifest or {}).get("original_user_task", task),
                                   resume_instruction=task if resume else "")
        # Run status describes this process lifecycle; the durable session and
        # result distinguish awaiting_user/needs_user from other interruptions.
        status = {"done": "completed", "needs_user": "interrupted"}.get(result.get("status"), "failed")
        exit_code = 0 if status == "completed" else host.CLI_ERROR_EXIT_CODE
    except asyncio.CancelledError:
        status = "cancelled"
        exit_code = host.CLI_CANCELLED_EXIT_CODE
        result = {"status": "cancelled", "summary": f"{label} task cancelled; execution effects remain recorded."}
    except Exception as exc:
        status = "failed"
        result, exit_code = host._classify_cli_exception(exc, phase=mode)
    finally:
        try:
            if session is not None:
                try:
                    cleanup = asyncio.create_task(session.close())
                    while not cleanup.done():
                        try: await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            status, exit_code = "cancelled", host.CLI_CANCELLED_EXIT_CODE
                    cleanup.result()
                except Exception as exc:
                    cleanup_error = {"type": type(exc).__name__, "message": str(exc)}
                    status, exit_code = "failed", host.CLI_ERROR_EXIT_CODE
                    result = {**(result or {}), "cleanupError": cleanup_error}
            if logger is not None and opened:
                logger.storage.save_resource(task_id=logger.task_id, run_id=logger.run_id,
                    resource_type=mode + "_task_result", logical_path=f"artifacts/{mode}-result.json", content=result)
                logger.write(mode + ".session.final", {"status": status, "outcome": (result or {}).get("status")})
                logger.write_usage_summary()
                logger.storage.finish_run(task_id=logger.task_id, run_id=logger.run_id, status=status,
                    error=cleanup_error or (result or {}).get("error"))
        finally:
            if logger is not None: logger.storage.close()
            if lock is not None: release_run_lock(lock)
    if result is not None:
        if getattr(args, "output", "auto") == "json": print(json.dumps(result, ensure_ascii=False, indent=2))
        else: print_result(label, result)
    if logger is not None and getattr(args, "output", "auto") != "json":
        block(label, "记录已保存", [("任务", "@" + logger.task_id), ("目录", logger.task_dir)])
    return exit_code
