"""/workflow* command routing and the Workflow task lifecycle."""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from harness.runtime.session_output import block
from harness.workflow_mode.registry import Registry, RegistryError
from harness.workflow_mode.runs import RunRegistry

COMMANDS = ("/workflow", "/workflow-create", "/workflow-edit", "/workflow-drafts", "/workflow-publish",
            "/workflow-unpublish", "/workflow-rename", "/workflow-delete", "/workflow-stop")
USAGE = """Workflow commands (WebCross Workshop is the only marketplace):
  /workflow                              list WebCross Workshop workflows
  /workflow <name|workshop-id> [task]     inspect or run the selected remote version
  /workflow-drafts                       list local author workspaces
  /workflow-create <request>             create a local draft under workflows/
  /workflow-edit <draft|workshop-id|name> <request> edit locally; upload separately
  /workflow-rename <draft> <new name>     rename a local draft; publish to sync remotely
  /workflow-delete <draft> [--yes]        delete local working files, retaining history
  /workflow-publish <draft> [--workshop <id>] pack and upload to WebCross
  /workflow-unpublish <name|workshop-id>  remove from WebCross, retaining source history
  /workflow-stop [task-id|batch-id|all]   stop owned tasks and their WebCross executions
  /resume <task-id> [instruction]        resume the recorded task/version"""


def split(line: str) -> tuple[str, str]:
    lexer = shlex.shlex(line.strip(), posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    head = lexer.get_token() or ""
    return head, line.strip()[lexer.instream.tell():].strip()


def unquote(text: str) -> str:
    """A shell-quoted single argument, or the text as typed."""
    try:
        parts = shlex.split(text)
    except ValueError:
        return text.strip()
    return " ".join(parts) if parts else ""


def is_workflow_command(line: str) -> bool:
    first = str(line or "").strip().split(maxsplit=1)
    return bool(first) and first[0].lower() in COMMANDS


def _print(args: Any, value: Any) -> None:
    if getattr(args, "output", "auto") == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str), flush=True)


def _workshop_lines(rows):
    lines = ['WebCross Workshop:']
    for row in rows:
        lines.append(f"  {row.get('name')} | workshopId {row.get('workshopId')} | revision {row.get('revision')}")
    if not rows: lines.append('  (none)')
    return lines


def _draft_lines(rows):
    lines = ['Local Workflow drafts (author workspaces):']
    for row in rows:
        remote = row.get('remote') or {}
        lines.append(f"  {row.get('displayName')} | {row['slug']} | status {row.get('status')} | task {row['taskId']}")
        lines.append(f"    files {row.get('currentHash')} | Workshop {remote.get('workshopId') or '(not linked)'}")
        lines.append(f"    Edit: /workflow-edit {row['slug']} <request>   Upload: /workflow-publish {row['slug']}")
        if row.get('historyPath'):lines.append(f"    History: {row['historyPath']}")
    if not rows:lines.append('  (none) Create one with /workflow-create <request>.')
    return lines


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt + " ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


async def run_command(runtime: Any, args: Any, line: str, *, host: Any) -> int:
    command, rest = split(line)
    command = command.lower()
    registry = Registry.for_runtime(runtime)
    try:
        if (registry.root/'.migration-in-progress.json').exists():
            raise RegistryError('Workflow storage migration is in progress; inspect its saved inventory before using commands')
        if command == '/workflow' and rest in {'help','--help','-h'}:
            print(USAGE, flush=True); return 0
        if command == '/workflow-drafts':
            if rest: raise RegistryError('Usage: /workflow-drafts')
            rows=registry.list_drafts(); print('\n'.join(_draft_lines(rows)),flush=True); _print(args,rows); return 0
        if command == '/workflow-rename':
            reference,name=split(rest)
            if not reference or not name:raise RegistryError('Usage: /workflow-rename <draft> <name>')
            value=registry.rename(reference,unquote(name));print(f"Renamed local draft: {value['displayName']}. Publish to sync Workshop.",flush=True);_print(args,value);return 0
        if command == '/workflow-delete':
            reference,flag=split(rest);row=registry.resolve_draft(reference)
            if flag != '--yes' and not _confirm(f"Delete local working files for {row['displayName']}? History and Workshop remain. yes:"):
                return 2
            value=registry.delete(reference);print('Deleted local working files; history retained.',flush=True);_print(args,value);return 0
        if command in {'/workflow','/workflow-publish','/workflow-unpublish'}:
            from harness.workflow_mode.workshop import Workshop
            async with Workshop(runtime,registry) as workshop:
                if command == '/workflow' and not rest:
                    data,ref=await workshop.listing();print('\n'.join(_workshop_lines(data['items'])),flush=True)
                    _print(args,{'source':'WebCross Workshop',**data,'receipt':ref});return 0
                if command == '/workflow':
                    reference,request=split(rest);item=await workshop.resolve(unquote(reference))
                    if not request:
                        print('\n'.join(_workshop_lines([item])),flush=True);_print(args,item);return 0
                    row=workshop.source_snapshot(item)
                elif command == '/workflow-publish':
                    parts=shlex.split(rest)
                    if len(parts) not in {1,3} or (len(parts)==3 and parts[1]!='--workshop'):
                        raise RegistryError('Usage: /workflow-publish <draft> [--workshop <id>]')
                    value=await workshop.publish(parts[0],target=parts[2] if len(parts)==3 else None)
                    message=('Already matches WebCross Workshop (no upload sent)' if value['status']=='already_synced'
                             else 'Uploaded to WebCross Workshop')
                    print(f"{message}: {value['workshopId']} revision {value['revision']}",flush=True);_print(args,value);return 0
                else:
                    if not rest:raise RegistryError('Usage: /workflow-unpublish <name|workshop-id>')
                    value=await workshop.unpublish(unquote(rest))
                    print(('Already absent in WebCross Workshop: ' if value['status']=='already_absent' else 'Removed from WebCross Workshop: ')+value['workshopId'],flush=True)
                    _print(args,value);return 0
            return await run_session(runtime,args,host=host,profile='run',goal=request,package_row=row)
        if command == "/workflow-stop":
            stopped = RunRegistry(runtime.harness.worktree_dir).request_stop(rest or "all")
            if not stopped:
                print("No running Workflow task matches.", flush=True)
                return 0
            for item in stopped:
                print(f"Stop requested: @{item['taskId']} {item.get('label') or ''} page {item.get('pageId')}",
                      flush=True)
            print("The owning process cancels its WebCross executions, saves their status and stops "
                  "dispatching. Cancellation does not undo effects already made.", flush=True)
            _print(args, stopped)
            return 0
        if command == "/workflow-create":
            if not rest:
                raise RegistryError("Usage: /workflow-create <request>")
            return await run_session(runtime, args, host=host, profile="author", goal=rest, draft_kind="create")
        if command == "/workflow-edit":
            reference, request = split(rest)
            if not reference or not request:
                raise RegistryError("Usage: /workflow-edit <name|slug|id|draft-task-id> <request>")
            draft = registry.resolve_edit_draft(unquote(reference))
            if draft is None:
                from harness.workflow_mode.workshop import Workshop
                async with Workshop(runtime,registry) as workshop:
                    draft=await workshop.checkout(unquote(reference),request=request)
            from harness.workflow_mode.author_task import ensure_author_task
            await ensure_author_task(runtime,args,host=host,task_id=draft['taskId'],request=request)
            return await resume_task(runtime,args,request,host=host,
                directory=registry.worktree/draft['taskId'],expected_profile='author')

    except (RegistryError, ValueError) as exc:
        print(f"Workflow command failed: {exc}", flush=True)
        return host.CLI_INPUT_ERROR_EXIT_CODE
    print(USAGE, flush=True)
    return host.CLI_INPUT_ERROR_EXIT_CODE


def _install_terminal_stop(session: Any) -> None:
    from harness.runtime.steering import current_steering
    manager = current_steering()
    if manager is None:
        return

    def stop(line: str) -> None:
        _, target = split(line)
        target = (target or "all").removeprefix("@")

        async def apply() -> None:
            if session.profile == "coordinator":
                ids = [i for i in session.children if target == "all" or i.startswith(target)]
                outcomes = await session.stop_children(ids, "User /workflow-stop")
                if target in {"all", session.logger.task_id} or session.logger.task_id.startswith(target):
                    session.stop_reason = "User /workflow-stop"
                manager.show(f"已请求停止 {len(outcomes)} 个任务；已发出的取消请求见各任务回执。")
            elif target == "all" or session.logger.task_id.startswith(target):
                session.stop_reason = "User /workflow-stop"
                for workflow_id, live in list(session.platform.live.items()):
                    if live.get("status") not in {"succeeded", "failed", "cancelled", "completed"}:
                        await session.platform.cancel(workflow_id, reason="User /workflow-stop")
                manager.show("已请求停止本任务；取消不会撤销已经发生的页面效果。")
        task = asyncio.get_running_loop().create_task(apply())
        manager.control_tasks.add(task)
        task.add_done_callback(manager.control_tasks.discard)
    manager.control_handlers["/workflow-stop"] = stop


def _print_result(label: str, value: dict[str, Any]) -> None:
    status = "等待用户，进度已保存" if value.get("status") == "awaiting_user" else value.get("status")
    block(label, f"结果 · {status}", [
        ("任务", "@" + str(value.get("taskId"))), ("结论", value.get("conclusion")),
        ("包", (value.get("package") or {}).get("slug")),
        ("Workshop", (value.get("package") or {}).get("workshopId")),
        ("远端 revision", (value.get("package") or {}).get("revision")),
        ("版本", (value.get("package") or {}).get("contentHash") or value.get("draftHash")),
        ("来源草稿", (value.get("package") or {}).get("sourceDraft")),
        ("执行次数", value.get("executions")),
        ("用量", json.dumps({k: (value.get("usage") or {}).get(k) for k in
                            ("tokens", "cacheRead", "netSeconds")}, ensure_ascii=False))])
    if value.get("summary"):
        print("\n" + str(value["summary"]).strip(), flush=True)
    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        print(f"\n覆盖记录（作者声明）: {coverage.get('status')} | {coverage.get('path')}", flush=True)
        if coverage.get("status") == "recorded":
            print(f"  范围审阅: {coverage.get('scopeStatus')}；待澄清: {coverage.get('openQuestions', 0)}", flush=True)
            for label, key in (("控件", "controls"), ("场景", "scenarios")):
                print(f"  {label}: " + json.dumps(coverage.get(key), ensure_ascii=False), flush=True)
            print("  证据地址核对: " + json.dumps((coverage.get("references") or {}).get("counts", {}),
                                                ensure_ascii=False), flush=True)
            print("  以上只统计已记录条目，不代表页面已全覆盖；适用条件和证据见 coverage.json。", flush=True)
        if coverage.get("error"):
            print("  " + str(coverage["error"]), flush=True)
    sections = (("各任务", "tasks"), ("尚需处理", "remainingWork"), ("模板缺少的能力", "templateGaps"),
                ("已验证范围", "verifiedScope"), ("已知限制", "knownLimitations"), ("问题记录（Agent 归因）", "defects"),
                ("下一步", "next"))
    for title, key in sections:
        items = value.get(key) or []
        if items:
            print(f"\n{title}:", flush=True)
            for item in items if isinstance(items, list) else [items]:
                if isinstance(item, dict):
                    item = " | ".join(f"{k}={v}" for k, v in item.items() if v not in (None, "", []))
                print(f"  - {item}", flush=True)
    if value.get("question"):
        print(f"\n需要你回答: {value['question']}", flush=True)


async def run_session(runtime: Any, args: Any, *, host: Any, profile: str, goal: str,
                      package_row: dict[str, Any] | None = None, draft_kind: str | None = None,
                      draft_reference: str | None = None,
                      resume: Any = None, resume_instruction: str = "") -> int:
    from harness.runtime.resume_state import acquire_run_lock, release_run_lock, write_task_manifest
    from harness.planning.fleet_reference import extract_fleet_reference
    from harness.workflow_mode.session import WorkflowSession
    logger = session = lock = None
    opened = False
    status, exit_code, outcome = "interrupted", 0, None
    try:
        fleet, error = extract_fleet_reference(goal)
        if error:
            raise ValueError(error)
        if resume is not None:
            logger = host._create_task_logger(runtime, task_id=resume.name)
        else:
            logger = host._create_task_logger(runtime)
        host._LAST_LOGGER = logger
        logger.run_id = host._new_run_id(resumed=resume is not None)
        lock = acquire_run_lock(logger.task_dir)
        host._open_task_storage(logger, runtime)
        opened = True
        runtime.harness.runs_dir = str(logger.task_dir)
        registry = Registry.for_runtime(runtime)
        draft = None
        if resume is None:
            if profile == "author":
                draft = (registry.create_draft(logger.task_id, request=goal) if draft_kind == "create"
                         else registry.edit_draft(logger.task_id, draft_reference, request=goal))
                if draft["taskId"] != logger.task_id:
                    raise RegistryError("An author draft was opened concurrently. Continue it with "
                                        f"/workflow-edit {draft['taskId']} <request>.")
            if package_row is not None:
                registry.snapshot(package_row, logger.task_dir)
            write_task_manifest(logger, original_user_task=goal, task_id=logger.task_id,
                config_path=getattr(args, "config", None), agent_id=runtime.agent_id,
                startup_args={"agent_mode": "workflow", "workflowProfile": profile,
                              "workflowSlug": (package_row or draft or {}).get("slug"),
                              "workflowPackage": package_row and {k: package_row.get(k) for k in
                                  ("origin", "sourceDraft", "templateId", "contentHash", "workshopId", "revision", "manifestHash", "lookupReceipt")}})
        quiet = getattr(args, "output", "auto") == "json"
        session = WorkflowSession(runtime, logger, profile=profile, host=host, quiet=quiet)
        if resume is None:
            session.initialize(goal, package_row=package_row, draft=draft, fleet_id=fleet)
            if package_row and package_row.get('origin')=='workshop':
                lookup=Path(package_row['lookupReceipt'])
                if lookup.is_symlink() or lookup.parent.resolve()!=(registry.root/'.operations').resolve():
                    raise RegistryError('Workshop lookup evidence is outside this workspace')
                evidence=json.loads(lookup.read_text(encoding='utf-8'))
                ref=session.store.receipt(session.state,'workflow.workshop.lookup',evidence)
                session.state['package']['lookupReceipt']=ref
                session.save()
        elif not session.state:
            raise ValueError("Saved Workflow state is missing; this task cannot be resumed")
        elif profile == "coordinator" or session.state.get("profile") == "coordinator":
            if (session.state.get("runHere") is True and session.state.get("profile") == "run"
                    and not session.state.get("children")):
                session.profile, session.label = "run", "Workflow"
            elif (session.state.get("pendingQuestion")
                  or (session.state.get("judgment") or {}).get("question")):
                # Resume the interaction, not the old batch executions. Live
                # children belong to their original owning process.
                children = session.state.get("children") or {}
                live_ids = {r["taskId"] for r in session.runs.live()}
                if (any(c.get("status") in {None, "running"} for c in children.values())
                        or live_ids.intersection(children)):
                    raise ValueError("This coordinator still has running child tasks; "
                                     "wait for or stop those item tasks before reopening its question.")
            else:
                raise ValueError("A /workflow batch is not resumed as a whole. Resume each item task "
                                 "with /resume <task-id>; its IDs are in the batch result.")
        if profile == "run" and session.state.get("profile") == "coordinator":
            session.profile = "coordinator"
            session.label = "Workflow 调度"
        if profile == "author":
            if (session.state.get("draft") or {}).get("taskId") != logger.task_id:
                raise RegistryError("Saved author state does not own this task's draft; cannot resume it.")
            # Both entry points hold the same task lock before selecting or reopening a draft.
            draft = registry.select_draft(logger.task_id)
            session.state["draft"] = {k: draft.get(k) for k in
                ("taskId", "kind", "slug", "templateId", "displayName", "baseHash", "sourceDraft", "sourceHash")}
            session.save()
        if resume is not None and (session.state.get("package") or {}).get("origin") == "draft":
            # A missing/corrupt snapshot is a lifecycle error, not a model repair loop.
            session._pinned()
        if not quiet:
            block(session.label, f"任务 @{logger.task_id}", [
                ("包", (session.state.get("package") or session.state.get("draft") or {}).get("slug")),
                ("版本", (session.state.get("package") or {}).get("contentHash")),
                ("来源草稿", (session.state.get("package") or {}).get("sourceDraft")),
                ("草稿", str(registry.draft_path(logger.task_id)) if profile == "author" else None),
                ("停止", "/workflow-stop " + logger.task_id), ("恢复", f"/resume {logger.task_id}")])
        session._terminal_stop_installer = _install_terminal_stop
        outcome = await session.run(session.state["goal"], resume_instruction=resume_instruction)
        status = "completed" if outcome.get("status") in {"done", "draft_saved"} else "interrupted"
        exit_code = 0 if status == "completed" else host.CLI_ERROR_EXIT_CODE
    except asyncio.CancelledError:
        status, exit_code = "cancelled", host.CLI_CANCELLED_EXIT_CODE
        outcome = {"status": "cancelled", "taskId": getattr(logger, "task_id", None),
                   "summary": "Workflow task cancelled; executed effects remain recorded."}
    except (RegistryError, ValueError) as exc:
        status, exit_code = "failed", host.CLI_INPUT_ERROR_EXIT_CODE
        outcome = {"status": "failed", "taskId": getattr(logger, "task_id", None), "summary": str(exc)}
    except Exception as exc:
        status = "failed"
        outcome, exit_code = host._classify_cli_exception(exc, phase="workflow")
        if session is not None and isinstance(outcome, dict):
            # Keep usage visible even when the task crashed.
            outcome.setdefault("usage", session.usage.snapshot())
    finally:
        try:
            if session is not None:
                await session.close()
        finally:
            if logger is not None and opened:
                logger.storage.save_resource(task_id=logger.task_id, run_id=logger.run_id,
                    resource_type="workflow_task_result", logical_path="artifacts/workflow-result.json",
                    content=outcome)
                logger.write("workflow.session.final", {"status": status, "outcome": (outcome or {}).get("status")})
                logger.write_usage_summary()
                host._close_task_storage(logger, status=status, error=(outcome or {}).get("error"))
            if lock is not None:
                release_run_lock(lock)
    if outcome is not None:
        if getattr(args, "output", "auto") == "json":
            print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str), flush=True)
        elif isinstance(outcome, dict) and outcome.get("taskId"):
            _print_result(session.label if session else "Workflow", outcome)
        else:
            print(json.dumps(outcome, ensure_ascii=False, default=str), flush=True)
    return exit_code


async def resume_task(runtime: Any, args: Any, instruction: str, *, host: Any,
                      directory: Path | None = None, expected_profile: str | None = None) -> int:
    from harness.runtime.resume_state import load_task_manifest
    directory = directory if directory is not None else host._resolve_resume_directory(args.resume)
    runtime.harness.worktree_dir = str(directory.parent)
    manifest = load_task_manifest(directory) or {}
    startup = manifest.get("startup_args") or {}
    profile = startup.get("workflowProfile")
    if profile not in {"run", "author", "coordinator"}:
        print("This task is not a resumable Workflow task (no workflowProfile).", flush=True)
        return host.CLI_INPUT_ERROR_EXIT_CODE
    if expected_profile is not None and profile != expected_profile:
        print("This task is not a Workflow author task; /workflow-edit requires its author draft ID.", flush=True)
        return host.CLI_INPUT_ERROR_EXIT_CODE
    return await run_session(runtime, args, host=host, profile=profile,
                             goal=str(manifest.get("original_user_task") or ""), resume=directory,
                             resume_instruction=instruction)
