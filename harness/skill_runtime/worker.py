"""Use the existing Fleet/lease/tool boundary for Skill execution and recovery."""
from __future__ import annotations

import json
from pathlib import Path
from abcp_client import ABCPTransportError

from harness.agents.browser.agent import BrowserAgent
from harness.observation.browser_call import build_browser_call_runner
from harness.skill_runtime.plan import bind, continuation_facts, digest, read_source, step_identity
from harness.skill_runtime.state import SkillStateStore
from harness.tools.browser_tools.dispatch import build_browser_tool_dispatcher
from harness.workflow.workflow_projection import workflow_execution_facts, workflow_execution_result


class SkillBrowserWorker(BrowserAgent):
    def _build_system_prompt(self):
        return super()._build_system_prompt() + (
            "\nThis profile's task owner is the independent Skill Orchestrator. "
            "Return this bounded subgoal's receipts, remaining work and recovery facts; "
            "the owner controls the execution contract and whole-task completion.\n")

    async def run(self, task: str) -> str:
        operation = self.worker_contract.get("_skill_operation", {})
        if operation.get("kind") == "browser":
            answer = await super().run(task + "\nReturn this local subgoal's facts to the Skill Orchestrator. "
                "Do not restart the whole task or independently repair/publish its Skill.")
            self._binding_checkpoint()
            return answer
        await self._bootstrap_browser(task)
        self.browser_call_runner = build_browser_call_runner(browser=self.browser,
            logger=self.logger, capability_methods=self.capability_methods)
        self.event_observer.attach(self.browser)
        try:
            if operation.get("kind") == "service":
                result = await self.execution_service.serve(self)
            else:
                result = await self._run_operation(operation)
            self.final_status = "done"
            return json.dumps(result, ensure_ascii=False)
        finally:
            self.event_observer.detach()
            self._binding_checkpoint()

    async def _run_operation(self, operation):
        if operation.get("kind") == "workflow_control":
            from harness.runtime.workflow_steering import control_owned_workflow
            return await control_owned_workflow(self, operation["workflowId"], operation["action"])
        if operation.get("kind") == "inventory":
            dispatch = build_browser_tool_dispatcher(self)
            receipt, stopped = await dispatch({"id": "executor-page-list", "name": "browser_call", "input": {
                "method": "Page.list", "params": {"fleetId": self.assigned_fleet_id},
                "reason": "Read the assigned Fleet's page inventory without changing the execution page"}}, 0)
            return {"status": "unavailable" if stopped else "returned", "fleetId": self.assigned_fleet_id,
                    "receipt": receipt}
        if operation.get("kind") == "observe":
            return await self._observe_page(operation)
        if operation.get("kind") == "create_test_page":
            dispatch = build_browser_tool_dispatcher(self)
            receipt, stopped = await dispatch({"id": "workflow-test-page", "name": "browser_call", "input": {
                "method": "Page.create", "params": {"fleetId": self.assigned_fleet_id,
                    "url": operation.get("url", "about:blank")},
                "reason": "Create the separate authorized Workflow validation page"}}, 1)
            data = (receipt.get("response") or {}).get("data") or {}
            return {"pageId": data.get("pageId"), "fleetId": self.assigned_fleet_id,
                "stopped": stopped, "receipt": receipt}
        if operation.get("kind") == "bind_page":
            result = await self._admit_page(operation["pageId"], build_browser_tool_dispatcher(self))
            return result
        if operation.get("kind") == "review":
            if self.provider is None:
                from llm.factory import LLMFactory
                from harness.runtime.model_config import browser_agent_model_config
                self.provider = LLMFactory.create_provider(browser_agent_model_config(self.runtime))
            from harness.agents.browser.review.task import review_browser_task
            review = await review_browser_task(agent=self, original_goal=operation["goal"],
                operator_inputs=operation.get("operatorInputs", []), phase="final",
                final_answer=operation["summary"], program_context=operation.get("programContext"))
            self.final_status = "done"  # This subtask delivered an audit, not business completion.
            return {"kind": "skill_review", "review": review}
        if operation.get("kind") == "workflow_trial":
            result = await self._execute_trial(operation)
            return result
        result = await self._execute_remaining(operation)
        self.final_status = "done"  # Returned execution facts, including a semantic yield.
        return result

    async def _observe_page(self, operation):
        from harness.agents.browser.review.task import _browser_read
        page_id = operation["pageId"]
        self._check_execution_page(page_id, operation)
        if page_id not in self.allowed_page_ids:
            binding = await self._admit_page(page_id, build_browser_tool_dispatcher(self))
            if binding["status"] != "bound":
                return binding
        self.axtree_page_id = page_id
        result = await _browser_read(self, {"method": operation["method"], "params": operation["params"]},
                                     "workflow-observation")
        if result.get("resourceUri"):
            result["notice"] = "Read the full task-owned resource with read_task_receipt(reference=resourceUri)."
        return result

    def _binding_checkpoint(self):
        store = SkillStateStore(self.logger)
        state = store.load()
        if not state:
            return
        state["lastObservation"] = {"fleetId": self.assigned_fleet_id,
            "pageId": str(getattr(self, "axtree_page_id", "") or ""),
            "pages": dict(self.page_fleet_ids)}
        store.save(state)

    async def _admit_page(self, page_id: str, dispatch):
        self._check_execution_page(page_id)
        store = SkillStateStore(self.logger)
        state = store.load()
        inventory, _ = await dispatch({"id": "skill-page-inventory", "name": "browser_call", "input": {
            "method": "Page.list", "params": {"fleetId": self.assigned_fleet_id},
            "reason": "Verify the requested page belongs to this task's assigned Fleet"}}, 0)
        rows = (inventory.get("response") or {}).get("data") or []
        if not isinstance(rows, list) or not any(row.get("pageId") == page_id for row in rows if isinstance(row, dict)):
            ref = store.record(state, "skill.page.binding_rejected", {"pageId": page_id, "receipt": inventory})
            return {"status": "page_binding_rejected", "pageId": page_id, "receiptRef": ref}
        receipt, stopped = await dispatch({"id": "skill-page-bind", "name": "browser_call", "input": {
            "method": "Page.getState", "params": {"pageId": page_id,
                "purpose": "Verify the execution page belongs to the assigned Fleet and claim its task lease"},
            "reason": "Bind the verified execution page through the existing page lease boundary"}}, 0)
        actual = (receipt.get("response") or {}).get("data") or {}
        ok = isinstance(actual, dict) and actual.get("pageId") == page_id and not stopped
        ref = store.record(state, "skill.page.bound" if ok else "skill.page.binding_rejected",
                           {"pageId": page_id, "receipt": receipt})
        if ok:
            self.axtree_page_id = page_id
            state["browserBinding"] = {"pageId": page_id, "fleetId": self.assigned_fleet_id,
                                       "pages": dict(self.page_fleet_ids)}
            store.save(state)
        return {"status": "bound" if ok else "page_binding_rejected", "pageId": page_id, "receiptRef": ref}

    def _check_execution_page(self, page_id, operation=None):
        operation = operation or getattr(self, "worker_contract", {}).get("_skill_operation", {})
        if "executionPages" in operation and page_id not in operation["executionPages"]:
            raise PermissionError("Page is outside this delegated execution binding")
        if page_id in operation.get("excludedPages", []):
            raise PermissionError("Page belongs to another role in this task")

    async def _execute_remaining(self, operation: dict) -> dict:
        from harness.runtime.steering import current_steering
        manager = current_steering()
        input_revision = manager.revision if manager else 0
        store = SkillStateStore(self.logger)
        state = store.load()
        if state.get("revision") != operation.get("revision"):
            raise ValueError("Skill execution contract changed before dispatch")
        dispatch = build_browser_tool_dispatcher(self)
        results = {sid: item["output"] for sid, item in state["steps"].items() if "output" in item}
        executed = []
        for index, step in enumerate(state["plan"]["steps"]):
            if manager and (manager.paused or manager.revision != input_revision):
                return {"status": "yielded", "reason": "user_input_pending", "executed": executed}
            sid = step["id"]
            progress = state["steps"][sid]
            if progress["status"] in {"completed", "skipped"}:
                continue
            if progress["status"] != "pending" or step["kind"] != "workflow":
                return {"status": "yielded", "stepId": sid, "state": progress,
                        "instruction": step.get("instruction"), "executed": executed}
            source = read_source(Path(operation["sourceRoot"]), step["path"])
            full_inputs = bind(step.get("input", {}), state["bindings"], results)
            inputs = bind(operation["attemptInputs"][sid], state["bindings"], results) if sid in operation.get("attemptInputs", {}) else full_inputs
            partial_input = digest(inputs) != digest(full_inputs)
            source_hash = digest(source)
            if source_hash != operation["sourceHashes"].get(step["path"]):
                raise ValueError("Workflow source changed after contract binding")
            binding = {**state.get("browserBinding", {}), **bind(step.get("binding", {}), state["bindings"], results)}
            page_id = binding.get("pageId", "")
            if not page_id and len(self.allowed_page_ids) == 1:
                page_id = next(iter(self.allowed_page_ids))
            if not page_id:
                return {"status": "yielded", "stepId": sid, "reason": "page_binding_required",
                        "pages": dict(self.page_fleet_ids), "executed": executed}
            if not isinstance(page_id, str):
                raise ValueError("Resolved binding.pageId must be a page ID string")
            self._check_execution_page(page_id, operation)
            if page_id not in self.allowed_page_ids:
                admitted = await self._admit_page(page_id, dispatch)
                state = store.load()
                progress = state["steps"][sid]
                if admitted["status"] != "bound":
                    return {"status": "yielded", "stepId": sid, "binding": admitted, "executed": executed}
            self.axtree_page_id = page_id
            if manager and (manager.paused or manager.revision != input_revision):
                return {"status": "yielded", "reason": "user_input_pending", "executed": executed}
            progress.update(status="running", sourceHash=source_hash, inputHash=digest(inputs))
            store.save(state)  # Commit intent before any action; crash => uncertain, never replay.
            call_input = {"path": step["path"], "pageId": page_id,
                          "fleetId": self.assigned_fleet_id, "variables": inputs}
            name = "execute_published_skill_workflow"
            if operation.get("override"):
                name = "execute_browser_workflow"
                call_input = {"workflow": source, "input": inputs, "pageId": page_id,
                              "fleetId": self.assigned_fleet_id}
            self._current_step = index + 1
            try:
                receipt, stopped = await dispatch({"id": f"skill-{sid}-{self.phase_id}",
                    "name": name, "input": call_input}, index + 1)
            except ABCPTransportError as exc:
                receipt, stopped = _interrupted_execution(exc), True
            # Other role may have added attributed user answers while this I/O awaited.
            state = store.load()
            progress = state["steps"][sid]
            control = workflow_execution_result(receipt, self.logger)
            facts, output = control["execution"], control["output"]
            view = {"output": output, "execution": facts}
            conditions = continuation_facts(step, view)
            input_changed = manager and (manager.paused or manager.revision != input_revision)
            continued = facts.get("status") == "succeeded" and conditions["matched"] and not stopped and not partial_input
            progress.update(status="completed" if continued else "awaiting_decision",
                output=view, continuation=conditions)
            if partial_input:
                progress["reason"] = "attempt_input_requires_full_contract_reconciliation"
            if continued:
                progress["acceptedIdentity"] = step_identity(Path(operation["sourceRoot"]), step,
                    state["bindings"], results, {"pageId": page_id, "fleetId": self.assigned_fleet_id})
            ref = store.record(state, "skill.step.result", {"stepId": sid,
                "path": step["path"], "contractHash": digest(step),
                "status": progress["status"], "sourceHash": source_hash,
                "inputHash": progress["inputHash"], "declaredInputHash": digest(full_inputs), "attemptInput": partial_input, "input": inputs, "binding": call_input,
                "receipt": receipt, "continuation": conditions})
            progress["receiptRef"] = ref
            store.save(state)
            results[sid] = view
            executed.append({"stepId": sid, "status": progress["status"], "output": view,
                             "continuation": conditions, "receiptRef": ref})
            if not continued or input_changed:
                return {"status": "yielded", "stepId": sid, "executed": executed,
                        **({"reason": "user_input_pending"} if input_changed else {})}
        return {"status": "sequence_finished", "executed": executed,
                "taskCompleted": None, "next": "Independent goal review is required"}

    async def _execute_trial(self, operation: dict) -> dict:
        store = SkillStateStore(self.logger)
        state = store.load()
        trial = state.get("workflowTrials", {}).get(operation["executionId"])
        if not trial or trial["status"] != "running":
            raise ValueError("Development execution must have a recorded running intent")
        root = self.logger.task_dir / trial["source"]["path"]
        if root.resolve() != Path(operation["sourceRoot"]).resolve():
            raise ValueError("Development source does not belong to this execution")
        source = read_source(root, operation["path"])
        if (digest(source) != trial["sourceHash"] or digest(operation["input"]) != trial["inputHash"]
                or operation["pageId"] != trial["pageId"] or operation["path"] != trial["path"]):
            raise ValueError("Development execution identity changed")
        dispatch = build_browser_tool_dispatcher(self)
        page_id = operation["pageId"]
        self._check_execution_page(page_id, operation)
        if page_id not in self.allowed_page_ids:
            admitted = await self._admit_page(page_id, dispatch)
            if admitted["status"] != "bound":
                state = store.load()
                trial = state["workflowTrials"][operation["executionId"]]
                trial.update(status="returned", binding=admitted)
                store.save(state)
                return admitted
        self.axtree_page_id = page_id
        self._current_step = 1
        try:
            receipt, stopped = await dispatch({"id": "development-" + operation["executionId"],
                "name": "execute_browser_workflow", "input": {"workflow": source,
                    "input": operation["input"], "pageId": page_id, "fleetId": self.assigned_fleet_id}}, 1)
        except ABCPTransportError as exc:
            receipt, stopped = _interrupted_execution(exc), True
        state = store.load()
        trial = state["workflowTrials"][operation["executionId"]]
        control = workflow_execution_result(receipt, self.logger)
        trial.update(status="returned", execution=control["execution"], output=control["output"], stopped=stopped)
        ref = store.record(state, "workflow.trial.result", {**trial, "receipt": receipt})
        trial["receiptRef"] = ref
        store.save(state)
        return {"executionId": operation["executionId"], "execution": trial["execution"],
                "receiptRef": ref, "receipt": receipt}


def _interrupted_execution(exc):
    from harness.tools.browser_tools.navigate import _transport_error_metadata
    return {"method": "Workflow.execute", "error": str(exc),
            **_transport_error_metadata("Workflow.execute", exc),
            "executionTrace": getattr(exc, "execution_trace", {})}
