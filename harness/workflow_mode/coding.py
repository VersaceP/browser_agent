"""One Coding context, with direct access to the owner's deterministic executor."""
from __future__ import annotations

import json
import hashlib
import shutil
import uuid
from dataclasses import replace

from harness._vendor.tau_agent import AgentTool
from harness.skill_builder.session import BuilderSession, WorkFileWriteError, _result, _schema, _STRING, _OBJECT, _prepare_tool_arguments
from harness.skill_runtime.plan import plan_schema
from harness.skill_runtime.session import SkillSession
from harness.storage.sqlite_connection import write_transaction
from harness.workflow_mode.prompt import CODING_SYSTEM


class WorkflowCodingSession(BuilderSession):
    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        self.required_orchestration = True
        self.referenced_tasks.update(owner.state.get("referencedTasks", []))
        from harness.planning.fleet_reference import extract_fleet_reference
        fleet, _ = extract_fleet_reference(owner.state["goal"])
        self.referenced_tasks = {ref for ref in self.referenced_tasks if ref.lower() != fleet}
        with write_transaction(self.storage.connection):
            self.storage.connection.execute(
                "UPDATE skill_builder_sessions SET referenced_tasks_json=? WHERE builder_task_id=?",
                (json.dumps(sorted(self.referenced_tasks)), self.logger.task_id))

    def role_prompt(self):
        if self.profile == "repair":
            return super().role_prompt() + (
                "\nUse run_workflow_sequence to execute this saved revision on the current owner's executor. "
                "It shows the diff and requests task-local authorization before applying changed code. "
                "No nested owner loop or separate test page is needed. Inspect the result, fix in the same "
                "workspace and reconcile effects with resolve_workflow_step before retrying. "
                "Use finish_workflow_development(status=needs_owner) for a semantic or permission handoff. "
                "Browser-only recovery cannot validate this revision. No publication.")
        return CODING_SYSTEM + f"\nWorkspace: {self.work_dir}. Owner task: {self.source_task_id}."

    def _finish(self, summary):
        result = super()._finish(summary)
        result["programEvidence"] = self.owner.program_evidence()
        if self.profile == "workflow":
            with write_transaction(self.storage.connection):
                self.storage.connection.execute(
                    "UPDATE skill_builder_sessions SET status='development' WHERE builder_task_id=?",
                    (self.logger.task_id,))
            result.update(status="development_saved", next=[f"/skill-create @{self.source_task_id} <request>"])
        return result

    async def _dispatch(self, name, args):
        try:
            if name == "run_workflow_sequence":
                if "bindings" in args and "binding_updates" in args:
                    raise ValueError("Use bindings for full replacement or binding_updates for top-level changes")
                if "plan" in args and "stage" in args:
                    raise ValueError("Use a whole plan or one incremental stage, not both")
                from harness.workflow_mode.stage import prepare_files, stage_plan
                prepared = prepare_files(self, args.get("updates", []), args.get("packages", []))
                if "stage" in args:
                    args = {**args, "plan": stage_plan(self.owner, args["stage"])}
                bindings = args.get("bindings", {**self.owner.store.load().get("bindings", {}),
                    **args.get("binding_updates", {})})
                if args.get("plan") is not None:
                    # The selected plan is part of the deliverable, not a
                    # transient variant that leaves orchestration.json stale.
                    from harness.skill_runtime.plan import validate_plan
                    validate_plan(args["plan"], self.work_dir)
                    file = self._path("orchestration.json")
                    previous = hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None
                    self._write_file("orchestration.json", json.dumps(args["plan"], ensure_ascii=False, indent=2) + "\n", previous)
                value = await self.owner.execute_sequence(self.work_dir, bindings=bindings,
                    plan=args.get("plan"), reason=args.get("reason", "Coding development execution"),
                    attempt_inputs=args.get("attempt_inputs"))
                value["workspaceChanges"] = prepared
            elif name in {"resolve_trial_step", "investigate_skill_trial", "read_trial_state", "run_trial_recovery", "resume_trial_recovery"} and self.owner.mode == "workflow":
                from harness.workflow_mode.validation import prepare_owned_trial
                await prepare_owned_trial(self.owner, args["trial_task_id"])
                return await super()._dispatch(name, args)
            elif name == "run_workflow_trial":
                # Development probes have a separate workspace, so snapshot a
                # package containing only that file rather than publishing it.
                path, directory = args["path"], self.work_dir
                if path.startswith("trials/"):
                    if self.profile == "repair":
                        raise PermissionError("Repair probes must be formal files in the revision displayed for task-local authorization")
                    original = self._path(path)
                    directory = self.trial_dir / "packages" / uuid.uuid4().hex
                    directory.mkdir(parents=True)
                    shutil.copyfile(self.work_dir / "SKILL.md", directory / "SKILL.md")
                    shutil.copyfile(original, directory / "workflow.json")
                value = await self.owner.execute_trial(directory,
                    path="workflow.json" if path.startswith("trials/") else path,
                    page_id=args["page_id"], variables=args.get("variables", {}), probe_key=path)
            elif name == "resolve_workflow_step":
                value = self.owner.resolve_step(args["step_id"], args["decision"], args["reason"],
                    args["evidence_refs"], args.get("output"))
            elif name == "run_skill_trial":
                if self.owner.mode == "workflow":
                    from harness.workflow_mode.validation import prepare_trial
                    await prepare_trial(self.owner, args["page_id"])
                    try:
                        return await super()._dispatch(name, args)
                    finally:
                        from harness.workflow_mode.validation import sync_trials
                        sync_trials(self.owner)
                binding = self.owner.store.load().get("browserBinding", {})
                if args["page_id"] != binding.get("pageId"):
                    raise PermissionError("Repair execution uses the owner's current evidenced page binding")
                value = await self.owner.execute_sequence(self.work_dir, bindings=args["bindings"],
                    plan=args.get("plan"), reason=args["goal"])
            elif name == "reconcile_workflow_trial":
                value = self.owner.reconcile_trial(args["execution_id"], args["reason"], args["evidence_refs"], args.get("step_id"), args.get("output"))
            elif name == "finish_workflow_development":
                if args.get("status", "ready") not in {"ready", "needs_owner"}:
                    raise ValueError("Use ready or needs_owner")
                if args.get("status") == "needs_owner":
                    # A semantic handoff must be possible before a complete
                    # program exists. Keep partial files, without activating
                    # them or presenting their structure as a verified draft.
                    with write_transaction(self.storage.connection):
                        self.storage.connection.execute(
                            "UPDATE skill_builder_sessions SET status=?, draft_hash=NULL WHERE builder_task_id=?",
                            ("development" if self.profile == "workflow" else "active", self.logger.task_id))
                    self.finished = {"status": "needs_owner", "builderTaskId": self.logger.task_id,
                        "workDir": str(self.work_dir), "files": self._list_files()["files"],
                        "summary": args["summary"], "remainingWork": args.get("remaining_work", [])}
                    return _result(self.finished, terminate=True)
                result = self._finish(args["summary"])
                result["remainingWork"] = args.get("remaining_work", [])
                return _result(result, terminate=True)
            else:
                return await super()._dispatch(name, args)
            return _result(value)
        except WorkFileWriteError as exc:
            return _result(exc.payload, error=True)
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            return _result({"status": "rejected", "type": type(exc).__name__, "reason": str(exc)}, error=True)

    def _tools(self):
        excluded = {"finish_skill_create", "run_skill_trial", "browser_call", "attach_trial_page", "authorize_trial_files", "local_fs_authorize",
            "resolve_trial_step", "investigate_skill_trial", "read_trial_state", "run_trial_recovery", "resume_trial_recovery"}
        if self.owner.mode == "workflow":
            excluded -= {"run_skill_trial", "resolve_trial_step", "investigate_skill_trial", "read_trial_state", "run_trial_recovery", "resume_trial_recovery"}
        if self.profile == "repair":
            excluded.remove("finish_skill_create")
            excluded.remove("run_skill_trial")
        tools = [t for t in super()._tools() if t.name not in excluded]
        for index, tool in enumerate(tools):
            if tool.name == "run_workflow_trial":
                tools[index] = replace(tool, description="Execute one saved file through the owner's existing Fleet/lease/file boundary, with raw receipts and no model loop. This probe does not complete plan steps.")
            elif tool.name == "run_skill_trial" and self.profile == "repair":
                tools[index] = replace(tool, description="Execute this repair revision through the current owner's executor and checkpoints; changed code requires task-local authorization. No nested Orchestrator or separate trial page.")
        resolution = next(t.parameters for t in SkillSession.tools(self.owner) if t.name == "resolve_skill_step")
        editing = {t.name: t.parameters for t in tools}
        update_schema = _schema({**editing["write_work_file"]["properties"],
            "edits": editing["patch_work_file"]["properties"]["edits"]}, ("path",))
        for name, description, schema in [
            ("run_workflow_sequence", "Save optional source updates, assemble packages, and execute remaining stages in one call. stage adds/revises one workflow stage without needing a complete plan. updates use content OR edits with expected_hash; packages use workspace files. Prior effects/checkpoints remain; reconcile changed completed stages before retrying.",
             _schema({"bindings": _OBJECT, "binding_updates": _OBJECT, "plan": plan_schema(), "reason": _STRING,
                 "stage": plan_schema()["properties"]["steps"]["items"],
                 "attempt_inputs": {"type": "object", "description": "Per-step remaining input for THIS invocation only, with the usual $binding/$result references. Never edits full reusable input. Different input yields for full-contract reconciliation.", "additionalProperties": _OBJECT},
                 "updates": {"type": "array", "items": update_schema},
                 "packages": {"type": "array", "items": editing["build_workflow_package"]}})),
            ("resolve_workflow_step", "Record a technical checkpoint resolution from raw evidence; return new business decisions to the owner.", resolution),
            ("reconcile_workflow_trial", "Resolve an uncertain interrupted probe after checking effects, before another invocation.",
             _schema({"execution_id": _STRING, "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING},
                      "step_id": _STRING, "output": _OBJECT}, ("execution_id", "reason", "evidence_refs"))),
            ("finish_workflow_development", "Return a ready revision, or use status=needs_owner to hand back a semantic blocker with partial files. No publication or whole-task completion.",
             _schema({"summary": _STRING, "status": {"type": "string", "enum": ["ready", "needs_owner"]},
                      "remaining_work": {"type": "array", "items": _STRING}}, ("summary",))),
        ]:
            async def execute(call_id, arguments, signal=None, on_update=None, *, _name=name):
                if signal is not None and signal.is_cancelled():
                    return _result({"status": "cancelled"}, error=True)
                return await self._dispatch(_name, arguments)
            tools.append(AgentTool(name=name, label=name, description=description, parameters=schema,
                prepare_arguments=lambda value, _schema=schema: _prepare_tool_arguments(value, _schema),
                execute_fn=execute, execution_mode="sequential"))
        return tools
