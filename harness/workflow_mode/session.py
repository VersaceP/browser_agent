"""Workflow development uses the Skill owner, executor and recovery contract."""
from __future__ import annotations

from dataclasses import replace

from harness.skill_builder.catalog import inspect_skill
from harness.skill_builder.context import task_references
from harness.skill_runtime.session import SkillSession
from harness.workflow_mode.artifacts import snapshot_source
from harness.workflow_mode.prompt import WORKFLOW_SYSTEM
from harness.skill_builder.session import _result, _schema, _STRING, _OBJECT, _prepare_tool_arguments
from harness._vendor.tau_agent import AgentTool
from harness.storage.base import ResourceAccessError


class WorkflowSession(SkillSession):
    mode = "workflow"
    coding_profile = "workflow"
    system_prompt = WORKFLOW_SYSTEM

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._foreground = None

    def foreground(self):
        if self._foreground is None:
            from harness.workflow_mode.coding import WorkflowCodingSession
            self.state = self.store.load()
            self._foreground = WorkflowCodingSession(self.runtime, source_task_id=self.logger.task_id,
                owner=self, owner_task_dir=self.logger.task_dir, profile="workflow",
                builder_task_id=self.state.get("foregroundWorkspaceId") or self.state.get("codingSessionId"),
                base_directory=self.source_root())
            self.state["foregroundWorkspaceId"] = self._foreground.logger.task_id
            self.store.save(self.state)
            from harness.storage.sqlite_connection import write_transaction
            with write_transaction(self._foreground.storage.connection):
                self._foreground.storage.connection.execute("UPDATE skill_builder_sessions SET status='development' WHERE builder_task_id=?",
                    (self._foreground.logger.task_id,))
        return self._foreground

    def overview(self):
        value = super().overview()
        value["currentWorkspaceHash"] = inspect_skill(
            self._foreground.work_dir if self._foreground else self.source_root())["hash"]
        return value

    def projection(self):
        value = super().projection()
        value["validation"] = self.validation_evidence()
        return value

    def validation_evidence(self):
        from harness.workflow_mode.validation import evidence
        return evidence(self)

    async def create_test_page(self, url="about:blank"):
        binding = self.store.load().get("browserBinding", {})
        returned = await self._delegate({"kind": "create_test_page", "url": url},
            "Create an independent page for this task's main author to validate the program; preserve the business page binding")
        answer = returned.get("answer") or {}
        page_id = answer.get("pageId")
        if not page_id or answer.get("stopped"):
            return {"status": "page_setup_failed", "result": returned}
        from harness.runtime.program_validation import validate_test_page
        try:
            validate_test_page(answer, binding.get("fleetId"), {binding.get("pageId")})
        except ValueError:
            return {"status": "page_setup_failed", "reason": "Page creation returned an inconsistent page/Fleet identity",
                    "result": returned}
        self.state = self.store.load()
        self.state.setdefault("workflowTestPages", {})[page_id] = {
            "fleetId": answer["fleetId"], "receiptRef": returned.get("receiptRef")}
        self.store.save(self.state)
        return {"pageId": page_id, **self.state["workflowTestPages"][page_id]}

    async def _delegate(self, operation, instruction):
        test_pages = set(self.store.load().get("workflowTestPages", {}))
        requested = operation.get("pageId") or self.store.load().get("browserBinding", {}).get("pageId")
        if requested in test_pages:
            raise PermissionError("Business execution cannot claim an independent test page; use run_skill_trial or the owned trial tools")
        return await super()._delegate({**operation, "excludedPages": sorted(test_pages)}, instruction)

    async def _dispatch(self, name, args):
        try:
            if name == "describe_skill_runtime":
                from harness.skill_builder.runtime import consumer_contract
                return _result(consumer_contract(tools=self.tools(), mode="workflow"))
            if name == "inspect_workflow_page":
                from harness.skill_runtime.observation import observe
                return await observe(self, **args)
            if name == "run_skill_workflows":
                return _result({"status": "rejected", "reason": "Development uses run_workflow_sequence to execute the current saved workspace revision"}, error=True)
            if name == "create_workflow_test_page":
                result = await self.create_test_page(args.get("url", "about:blank"))
                return _result(result, error=result.get("status") == "page_setup_failed")
            if name == "activate_workflow_revision":
                from harness.skill_runtime.plan import read_source, validate_plan
                if not args["reason"].strip():
                    raise ValueError("Explain why this revision should be delivered")
                self._check_evidence(args["evidence_refs"])
                state = self.store.load()
                if any(step.get("status") == "running" for step in state.get("steps", {}).values()):
                    raise ValueError("Reconcile the active execution before changing its source")
                root = self.foreground().work_dir
                plan = validate_plan(read_source(root, "orchestration.json"), root)
                source = self.activate(root, expected_hash=args["source_hash"])
                self.bind_plan(state.get("bindings", {}), plan, args["reason"])
                return _result({"source": source, "packageRef": self.export_package(),
                    "next": "Revision saved without browser actions. Inspect changed checkpoints; validation does not replay or complete main-page work."})
            if name == "record_workflow_validation":
                from harness.workflow_mode.validation import record_assessment
                return _result(record_assessment(self, **args))
            if name == "read_validation_receipt":
                from harness.workflow_mode.validation import read_receipt
                return _result(read_receipt(self, **args))
            if name == "inspect_trial_page":
                from harness.workflow_mode.validation import inspect_page
                return await inspect_page(self, **args)
            if name in {t.name for t in self.foreground()._tools()} and name not in {"finish_skill_create", "finish_workflow_development"}:
                return await self.foreground()._dispatch(name, args)
            else:
                return await super()._dispatch(name, args)
            return _result(value)
        except (ValueError, PermissionError, FileNotFoundError, ResourceAccessError) as exc:
            return _result({"status": "rejected", "reason": str(exc)}, error=True)

    def program_evidence(self):
        evidence = super().program_evidence()
        evidence["validation"] = self.validation_evidence()
        return evidence

    def initialize(self, goal: str):
        if self.state:
            if self.state.get("mode") != "workflow":
                raise ValueError("This saved task is not a Workflow development session")
            self.state["referencedTasks"] = sorted(set(self.state.get("referencedTasks", []))
                - self._fleet_references(self.state["goal"]))
            return
        seed = self.logger.task_dir / "workflow" / "seed"
        seed.mkdir(parents=True, exist_ok=True)
        identity = "workflow-" + self.logger.task_id
        (seed / "SKILL.md").write_text(
            f"---\nname: {identity}\nversion: '0.1.0'\n---\n"
            "# Workflow under development\n\nDefine business inputs, outputs and recovery here.\n",
            encoding="utf-8")
        source = snapshot_source(self.logger, seed)
        self.state = {"schemaVersion": 1, "mode": "workflow", "skillId": identity,
            "skillHash": source["hash"], "goal": goal, "revision": 0, "status": "active",
            "steps": {}, "receipts": [], "repairs": {}, "operatorInputs": [],
            "developmentSource": source, "workflowTrials": {},
            "referencedTasks": sorted(task_references(goal) - self._fleet_references(goal))}
        self.store.save(self.state)
        self.export_package()

    def export_package(self):
        self.state = self.store.load()
        return self.store.write("workflow/package.json", {
            "schemaVersion": 1, "taskId": self.logger.task_id, "goal": self.state["goal"],
            "status": self.state["status"], "delivery": self.state.get("delivery"), "source": self.state["developmentSource"],
            "operatorInputs": self.state.get("operatorInputs", []), "pendingInput": self.state.get("pendingInput"),
            "plan": self.state.get("plan"), "steps": self.state["steps"],
            "trials": self.state.get("workflowTrials", {}),
            "codingSessionId": self.state.get("codingSessionId"),
            "validation": self.validation_evidence(),
            "foregroundWorkspaceId": self.state.get("foregroundWorkspaceId"),
            "receipts": self.state.get("receipts", []),
            "programEvidence": self.program_evidence(),
            "interpretation": "Saved development source and actual execution evidence. "
                              "Probe success and Browser recovery do not certify unexecuted program paths."},
            "workflow_package")

    def activate(self, directory, *, expected_hash: str | None = None):
        source = snapshot_source(self.logger, directory)
        self.state = self.store.load()
        info = inspect_skill(directory)
        if info["metadata"].get("name") != self.state["skillId"]:
            raise ValueError("Development revision changed the task's package identity")
        if expected_hash and source["hash"] != expected_hash:
            raise ValueError("Returned Coding revision changed before activation")
        self.state["developmentSource"] = source
        self.store.record(self.state, "workflow.revision.activated", {"sourceHash": source["hash"]})
        self.export_package()
        return source

    async def delegate_coding(self, instruction: str, builder_task_id: str | None = None):
        result = await super().delegate_coding(instruction, builder_task_id)
        if result.get("status") == "needs_owner":
            return {**result, "packageRef": self.export_package(), "execution": self.projection()}
        request_id = result["builderTaskId"]
        source = self.activate(self.coding_work_dir(request_id), expected_hash=result["skill"]["hash"])
        job = self.state["repairs"][request_id]
        job.update(status="applied", appliedHash=source["hash"])
        job["rounds"][-1].update(status="applied", appliedHash=source["hash"])
        self.store.save(self.state)
        return {**result, "activeSourceHash": source["hash"], "packageRef": self.export_package(),
                "execution": self.projection()}

    def tools(self):
        tools = [t for t in super().tools() if t.name not in {"apply_skill_repair", "delegate_coding", "run_skill_workflows"}]
        for index, tool in enumerate(tools):
            if tool.name == "skill_overview":
                tools[index] = replace(tool, description="Read the active development package, source version and execution checkpoints.")
            elif tool.name == "recover_skill_step":
                tools[index] = replace(tool, description="Record the evidenced diagnosis. repair returns to your foreground file tools; investigate delegates a bounded Browser subgoal. Reconcile effects before running changed source.")
        present = {t.name for t in tools}
        excluded = {"finish_skill_create", "finish_workflow_development", "create_workflow_test_page",
                    "inspect_trial_page", "read_validation_receipt", "record_workflow_validation"}
        for tool in self.foreground()._tools():
            if tool.name in present | excluded:
                continue
            # The agent retains these callbacks across merge/reset. Resolve the
            # current workspace on invocation, never capture its closed predecessor.
            async def execute(call_id, arguments, signal=None, on_update=None, *, _name=tool.name):
                if signal is not None and signal.is_cancelled():
                    return _result({"status": "cancelled"}, error=True)
                return await self._dispatch(_name, arguments)
            tools.append(replace(tool, execute_fn=execute))
        definitions = [
            ("activate_workflow_revision", "Deliver the exact current workspace after testing, WITHOUT repeating main-page actions. Rebind its orchestration and retain prior effects; changed contracts await reconciliation.",
             _schema({"source_hash": _STRING, "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING}},
                ("source_hash", "reason", "evidence_refs"))),
            ("inspect_workflow_page", "Read the business page through the shared scope without a Browser model. Read live action schemas first.",
             _schema({"method": _STRING, "params": _OBJECT}, ("method", "params"))),
            ("create_workflow_test_page", "Create an independent test page in the authorized Fleet. Main business binding and checkpoints stay separate; a new page does not reset server effects.", _schema({"url": _STRING})),
            ("inspect_trial_page", "Observe an owned validation page through the shared native executor, with indexed receipts. Does not select the business page.",
             _schema({"trial_task_id": _STRING, "method": _STRING, "params": _OBJECT}, ("trial_task_id", "method", "params"))),
            ("read_validation_receipt", "Read and import an attributed receipt from an owned trial; main task receipts are also accepted. No access to unrelated tasks.",
             _schema({"reference": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("reference",))),
            ("record_workflow_validation", "Record your evidence-based assessment of one current stage. Source and contract hashes bind the assessment; observed coverage and applicability are semantic judgments. Browser fallback does not verify unexecuted Workflow code.",
             _schema({"stage_id": _STRING, "verdict": {"type": "string", "enum": ["verified", "needs_work", "blocked", "not_applicable"]},
                "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING}},
                ("stage_id", "verdict", "reason", "evidence_refs"))),
        ]
        for name, description, schema in definitions:
            async def execute(call_id, arguments, signal=None, on_update=None, *, _name=name):
                if signal is not None and signal.is_cancelled():
                    return _result({"status": "cancelled"}, error=True)
                return await self._dispatch(_name, arguments)
            tools.append(AgentTool(name=name, label=name, description=description,
                parameters=schema, prepare_arguments=lambda value, _schema=schema: _prepare_tool_arguments(value, _schema),
                execute_fn=execute, execution_mode="sequential"))
        return tools

    async def close(self):
        if self._foreground:
            await self._foreground.browser.close()
            self._foreground.storage.finish_run(task_id=self._foreground.logger.task_id,
                run_id=self._foreground.logger.run_id, status="completed")
            self._foreground.catalog.close()
            self._foreground.storage.close()
        await super().close()

    async def run(self, goal: str, *, resume_instruction: str = ""):
        self.initialize(goal)
        self.state.setdefault("referencedTasks", []).extend(sorted(
            task_references(resume_instruction) - self._fleet_references(resume_instruction)))
        for trial in self.state.get("workflowTrials", {}).values():
            if trial["status"] == "running":
                trial["status"] = "uncertain"
        self.store.save(self.state)
        try:
            result = await super().run(goal, resume_instruction=resume_instruction)
            return {**result, "mode": self.mode, "packageRef": self.export_package(),
                    "next": f"/skill-create @{self.logger.task_id} <packaging request>"}
        finally:
            self.export_package()

    @staticmethod
    def _fleet_references(text):
        from harness.planning.fleet_reference import extract_fleet_reference
        reference, _ = extract_fleet_reference(text)
        return {token for token in task_references(text) if token.lower() == reference} if reference else set()
