"""Shared revision execution for Workflow development and Skill repair."""
from __future__ import annotations

import uuid
from pathlib import Path

from harness.skill_builder.catalog import inspect_skill
from harness.skill_runtime.plan import digest, read_source, validate_plan


class DevelopmentExecutor:
    def _unresolved_probe(self, path, probe_key):
        trials = [t for t in self.store.load().get("workflowTrials", {}).values()
            if t.get("probeKey", t["path"]) == (probe_key or path) and t["status"] in {"running", "uncertain"}]
        return {"status": "needs_reconciliation", "trials": trials,
            "next": "Inspect effects and reconcile_workflow_trial before another invocation"} if trials else None

    async def activate_candidate(self, directory):
        directory = Path(directory).resolve()
        if not directory.is_relative_to(self.logger.task_dir.resolve()):
            raise PermissionError("Development source must belong to this task")
        info = inspect_skill(directory)
        if self.mode == "workflow":
            return self.activate(directory, expected_hash=info["hash"])
        request_id = self.store.load().get("codingSessionId")
        if not request_id or directory != self.coding_work_dir(request_id).resolve():
            raise PermissionError("Skill candidate must belong to the current owned Coding session")
        if inspect_skill(self.source_root())["hash"] == info["hash"]:
            return {"status": "applied", "hash": info["hash"]}
        return await self.apply_repair(request_id, info["hash"])

    async def execute_sequence(self, directory, *, bindings, plan=None, reason="", attempt_inputs=None):
        actual_plan = validate_plan(plan or read_source(directory, "orchestration.json"), directory)
        activation = await self.activate_candidate(directory)
        if activation.get("status") == "not_applied":
            return activation
        self.state = self.store.load()
        self.bind_plan(bindings, actual_plan, reason or "Execute saved Coding revision")
        try:
            result = await self.run_workflows(attempt_inputs=attempt_inputs)
            from harness.skill_runtime.projection import execution_delta
            result = execution_delta(result)
            return {**result, "activeSourceHash": inspect_skill(self.source_root())["hash"]}
        finally:
            self.export_package()

    async def execute_trial(self, directory, *, path, page_id, variables, probe_key=None):
        unresolved = self._unresolved_probe(path, probe_key)
        if unresolved:
            return unresolved
        self.state = self.store.load()
        if self.mode != "workflow":
            activation = await self.activate_candidate(directory)
            if activation.get("status") == "not_applied":
                return activation
            # Execute the immutable authorized copy, not mutable Coding files.
            directory = self.source_root()
        return await self._execute_probe(directory, path=path, page_id=page_id,
            variables=variables, probe_key=probe_key)

    async def _execute_probe(self, directory, *, path, page_id, variables, probe_key=None, execution_id=None):
        """Execute owned source without activating it or replacing the plan.

        Callers must admit the source first: development activation above, or
        the owned Builder recovery service. This is not an agent-facing bypass.
        """
        self.state = self.store.load()
        directory = Path(directory).resolve()
        if not directory.is_relative_to(self.logger.task_dir.resolve()):
            raise PermissionError("Probe source must belong to this execution task")
        unresolved = self._unresolved_probe(path, probe_key)
        if unresolved:
            return unresolved
        from harness.workflow_mode.artifacts import snapshot_source
        source = snapshot_source(self.logger, directory)
        root = self.logger.task_dir / source["path"]
        document = read_source(root, path)
        execution_id = execution_id or uuid.uuid4().hex
        if execution_id in self.state.get("workflowTrials", {}):
            raise ValueError("Probe execution identity has already been used")
        trial = {"executionId": execution_id, "path": path, "probeKey": probe_key or path, "source": source,
            "sourceHash": digest(document), "inputHash": digest(variables), "input": variables,
            "pageId": page_id, "status": "running"}
        self.state = self.store.load()
        self.state.setdefault("workflowTrials", {})[execution_id] = trial
        self.store.record(self.state, "workflow.trial.started", trial)
        try:
            result = await self._delegate({"kind": "workflow_trial", "executionId": execution_id,
                "sourceRoot": str(root), "path": path, "sourceHash": trial["sourceHash"],
                "pageId": page_id, "input": variables},
                "Execute the exact saved file once and return raw receipts; do not infer business completion.")
            self.state = self.store.load()
            saved = self.state["workflowTrials"][execution_id]
            if saved["status"] == "running":
                saved.update(status="uncertain", delegationRef=result["receiptRef"])
                self.store.save(self.state)
            return {**result, "executionId": execution_id,
                "checkpointEffect": "Probe only; reconcile effects before executing the same actions in a plan"}
        except BaseException:
            self.state = self.store.load()
            saved = self.state["workflowTrials"][execution_id]
            if saved["status"] == "running":
                saved["status"] = "uncertain"
                self.store.save(self.state)
            raise
        finally:
            self.export_package()

    def reconcile_trial(self, execution_id, reason, evidence_refs, step_id=None, output=None):
        self.state = self.store.load()
        trial = self.state.get("workflowTrials", {}).get(execution_id)
        if not trial or trial["status"] not in {"uncertain", "returned"} or not reason.strip():
            raise ValueError("Reconcile a returned or uncertain probe with observed effects")
        self._check_evidence(evidence_refs)
        if step_id:
            self.resolve_step(step_id, "complete", reason, evidence_refs, output)
            self.state = self.store.load()
            trial = self.state["workflowTrials"][execution_id]
        trial.update(status="reconciled", reason=reason, evidenceRefs=evidence_refs)
        ref = self.store.record(self.state, "workflow.trial.reconciled", trial)
        self.export_package()
        return {"receiptRef": ref, "trial": trial, "acceptedStepId": step_id}
