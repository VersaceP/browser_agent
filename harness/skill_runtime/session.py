"""Skill owner: bind, run, delegate, recover and independently review."""
from __future__ import annotations

import asyncio
import copy
import difflib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from harness._vendor.tau_agent import AgentTool
from harness._vendor.tau_agent.events import MessageEndEvent, ToolExecutionEndEvent
from harness._vendor.tau_agent.messages import AgentMessage, AssistantMessage
from harness.agents.browser.task import BrowserTaskRunner, _wait_for_browser_mode_result
from harness.planning.fleet_reference import extract_fleet_reference
from harness.runtime.model_config import skill_orchestrator_model_config
from harness.skill_builder.catalog import SkillCatalog, inspect_skill
from harness.skill_builder.provider import HarnessProviderAdapter
from harness.skill_builder.resources import workflow_entries
from harness.skill_builder.session import _prepare_tool_arguments, _result, _schema, _STRING, _OBJECT
from harness.skill_runtime.plan import bind, digest, read_source, relative_path, validate_plan, plan_schema, step_identity
from harness.skill_runtime.prompt import ORCHESTRATOR_SYSTEM
from harness.skill_runtime.local_files import LOCAL_FILE_TOOLS, tool_definitions as local_file_definitions
from harness.skill_runtime.state import SkillStateStore
from harness.skill_runtime.development import DevelopmentExecutor
from harness.skill_runtime.evidence import program_evidence, read_receipt
from harness.skill_runtime.worker import SkillBrowserWorker
from harness.storage.factory import resolve_sqlite_path
from harness.storage.base import ResourceAccessError
from harness.task_control import accept_task_plan, validate_task_plan
from harness.runtime.steering import steerable_task, current_steering
from llm.factory import LLMFactory


class SkillSession(DevelopmentExecutor):
    mode = "skill"
    system_prompt = ORCHESTRATOR_SYSTEM
    coding_profile = "repair"

    def __init__(self, runtime, logger, *, skills_root: Path | None = None):
        self.runtime, self.logger = runtime, logger
        self.store = SkillStateStore(logger)
        self.catalog = SkillCatalog(skills_root or Path(__file__).resolve().parents[2] / "skills",
            resolve_sqlite_path(runtime.harness.storage_sqlite_path, runtime.harness.worktree_dir),
            worktree_dir=Path(runtime.harness.worktree_dir))
        self.state = self.store.load()
        self.finished = None
        self.runner = BrowserTaskRunner(runtime, logger)
        from harness.skill_runtime.executor import ExecutionService
        self.executor = ExecutionService(self)
        def worker_factory(*args, **kwargs):
            worker = SkillBrowserWorker(*args, **kwargs)
            worker.execution_service = self.executor
            return worker
        self.runner.spawner.browser_agent_factory = worker_factory
        prior_plan, _ = logger.storage.load_snapshot(task_id=logger.task_id, snapshot_key="current_task_plan")
        self.runner.task_plan = prior_plan or None
        self._capabilities_ready = False

    def borrow_fleet_services(self, owner):
        """An owned trial shares runtime isolation, with unique worker IDs."""
        spawner, shared = self.runner.spawner, owner.runner.spawner
        self._private_auth_barrier = spawner.fleet_auth_barrier
        spawner.page_lease_manager = shared.page_lease_manager
        spawner.fleet_auth_barrier = shared.fleet_auth_barrier
        spawner.fleet_click_gate = shared.fleet_click_gate
        spawner.worker_id_namespace = self.logger.task_id

    def source_root(self) -> Path:
        development = self.state.get("developmentSource")
        if development:
            root = self.logger.task_dir / relative_path(development["path"])
            if root.is_symlink() or not root.resolve().is_relative_to(self.logger.task_dir.resolve()):
                raise ValueError("Workflow source escaped its owner directory")
            if inspect_skill(root)["hash"] != development["hash"]:
                raise ValueError("Workflow source snapshot changed")
            return root
        trial = self.state.get("trialSource")
        if trial:
            root = self.logger.task_dir / relative_path(trial["path"])
            if root.is_symlink() or not root.resolve().is_relative_to(self.logger.task_dir.resolve()):
                raise ValueError("Trial Skill escaped its owner directory")
            if inspect_skill(root)["hash"] != trial["hash"]:
                raise ValueError("Trial source changed; start a new trial for the new version")
            return root
        override = self.state.get("override")
        if override:
            root = self.logger.task_dir / relative_path(override["path"])
            if root.is_symlink() or not root.resolve().is_relative_to(self.logger.task_dir.resolve()):
                raise ValueError("Task-local Skill override escaped its owner")
            if inspect_skill(root)["hash"] != override["hash"]:
                raise ValueError("Authorized Skill override changed")
            return root
        root = self.catalog.pin_source(self.state["skillId"], self.state["skillHash"],
                                       task_id=self.logger.task_id)
        if root is None or inspect_skill(root)["hash"] != self.state["skillHash"]:
            raise ValueError("Bound Skill version is unavailable or changed")
        return root

    def overview(self):
        root = self.source_root()
        return {"taskId": self.logger.task_id, "skillId": self.state["skillId"],
            "skillHash": self.state["skillHash"], "activeSourceHash": inspect_skill(root)["hash"],
            "guide": (root / "SKILL.md").read_text(),
            "workflows": workflow_entries(root),
            "orchestration": read_source(root, "orchestration.json") if (root / "orchestration.json").exists() else None,
            "execution": self.projection(), "programEvidence": self.program_evidence()}

    def program_evidence(self):
        self.state = self.store.load() or self.state
        return program_evidence(self.state, self.source_root(), lambda ref: read_receipt(self.logger, ref))

    def export_package(self):
        from harness.workflow_mode.artifacts import snapshot_source
        self.state = self.store.load() or self.state
        source = snapshot_source(self.logger, self.source_root())
        return self.store.write("skill/package.json", {
            "schemaVersion": 1, "taskId": self.logger.task_id, "goal": self.state["goal"],
            "status": self.state["status"], "delivery": self.state.get("delivery"), "source": source,
            "operatorInputs": (current_steering().latest_inputs(self.state["goal"]) if current_steering()
                               else self.state.get("operatorInputs", [])), "pendingInput": self.state.get("pendingInput"),
            "plan": self.state.get("plan"), "steps": self.state.get("steps", {}),
            "trials": self.state.get("workflowTrials", {}), "receipts": self.state.get("receipts", []),
            "codingSessionId": self.state.get("codingSessionId"),
            "programEvidence": self.program_evidence()}, "skill_execution_package")

    def read_skill_resource(self, path: str, offset: int = 0, max_chars: int = 16000):
        root = self.source_root()
        target = root / relative_path(path)
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()) or not target.is_file():
            raise ValueError("Resource is missing or outside the bound Skill")
        text = target.read_text(encoding="utf-8")
        offset, limit = max(0, offset), max(1, min(50000, max_chars))
        return {"path": path, "skillHash": inspect_skill(root)["hash"], "content": text[offset:offset+limit],
                "nextOffset": min(len(text), offset + limit), "hasMore": offset + limit < len(text)}

    def projection(self):
        result = {k: self.state.get(k) for k in ("goal", "revision", "bindings", "plan", "steps",
            "browserBinding", "operatorInputs", "repairs", "codingSessionId", "override", "status",
            "mode", "developmentSource", "workflowTrials", "recoveries", "lastObservation", "pendingInput")}
        result["workflowTrials"] = {key: {field: trial.get(field) for field in
            ("executionId", "path", "pageId", "status", "sourceHash", "inputHash", "receiptRef")}
            for key, trial in self.state.get("workflowTrials", {}).items()}
        source = self.state.get("developmentSource")
        if source:
            result["developmentSource"] = {key: source[key] for key in ("hash", "path")}
        result["historyAccess"] = "read_execution_state for full stored sections; read_task_receipt for raw evidence"
        manager = current_steering()
        if manager:
            from harness.runtime.workflow_steering import owned_workflows
            result["operatorInputs"] = manager.latest_inputs(self.state.get("goal", ""))
            result["userSteering"] = {"inputRevision": manager.revision, "paused": manager.paused,
                                      "workflows": owned_workflows(manager)}
        return result

    def read_execution_state(self, section, offset=0, max_chars=12000):
        state = self.store.load()
        if section not in state:
            raise ValueError("Unknown state section; available: " + ", ".join(sorted(state)))
        text = json.dumps(state[section], ensure_ascii=False)
        start, limit = max(0, offset), max(1, min(30000, max_chars))
        return {"section": section, "revision": state.get("revision"), "content": text[start:start+limit],
                "nextOffset": min(len(text), start+limit), "hasMore": start+limit < len(text)}

    def bind_plan(self, bindings: dict, plan: dict | None = None, reason: str = ""):
        self.state = self.store.load() or self.state
        root = self.source_root()
        plan = validate_plan(plan or read_source(root, "orchestration.json"), root)
        old = copy.deepcopy(self.state)
        records = {}
        old_steps = {s["id"]: s for s in old.get("plan", {}).get("steps", [])}
        results = {sid: r["output"] for sid, r in old.get("steps", {}).items() if "output" in r}
        for step in plan["steps"]:
            sid = step["id"]
            record = copy.deepcopy(old.get("steps", {}).get(sid, {"status": "pending"}))
            if sid in old_steps and record["status"] in {"completed", "skipped"}:
                try:
                    current = step_identity(root, step, bindings, results, self.state.get("browserBinding", {}))
                    accepted = record.get("acceptedIdentity")
                    if accepted:
                        changed = accepted != current
                    else:
                        changed = digest(old_steps[sid]) != digest(step)
                        previous_page = {**old.get("browserBinding", {}), **bind(old_steps[sid].get("binding", {}), old.get("bindings", {}), results)}
                        changed |= previous_page.get("pageId") != current.get("pageId")
                        if step["kind"] == "workflow" and record.get("inputHash"):
                            changed |= digest(bind(step.get("input", {}), bindings, results)) != record["inputHash"]
                            if record.get("sourceHash"):
                                changed |= digest(read_source(root, step["path"])) != record["sourceHash"]
                except ValueError:
                    changed = True
                if changed:
                    record.update(status="awaiting_decision", reason="completed_contract_changed")
            records[sid] = record
        if old.get("plan") == plan and old.get("bindings") == bindings and old.get("steps") == records:
            return self.projection()
        if old.get("plan"):
            self.store.write(f"skill/contracts/{old['revision']:06d}.json", old, "skill_contract")
        self.state.update(plan=plan, bindings=copy.deepcopy(bindings), steps=records,
                          revision=old.get("revision", 0) + 1)
        self.store.save(self.state)
        self.logger.write("skill.contract.bound", {"revision": self.state["revision"],
            "planHash": digest(plan), "bindingHash": digest(bindings), "reason": reason})
        return self.projection()

    async def _delegate(self, operation: dict, instruction: str):
        manager = current_steering()
        if manager and operation["kind"] in {"browser", "review"}:
            from harness.runtime.workflow_steering import owned_workflows, TERMINAL
            outstanding = {key: value for key, value in owned_workflows(manager).items()
                           if value.get("status") not in TERMINAL}
            if outstanding:
                if operation["kind"] == "review":
                    return await self.executor.execute(operation, instruction)
                return {"status": "execution_control_required", "workflows": outstanding,
                    "next": "Inspect/control this task's live or uncertain execution before transferring page ownership. Use control_workflow_execution; no Browser worker was dispatched."}
        if operation["kind"] in {"browser", "review"}:
            await self.executor.close()
            return await self._delegate_worker(operation, instruction)
        value = await self.executor.execute(operation, instruction)
        if operation["kind"] == "create_test_page":
            # Release the new page's lease before the author uses its independent trial context.
            await self.executor.close()
        return value

    async def _delegate_worker(self, operation: dict, instruction: str):
        self.state = self.store.load()
        operation = {**operation, "startPageId": operation.get("pageId") or self.state.get("browserBinding", {}).get("pageId", "")}
        owner = self.runner
        owner.original_user_task = self.state["goal"]
        owner.spawner.root_task = self.state["goal"]
        owner.task_fleet_reference = extract_fleet_reference(self.state["goal"])[0] or ""
        if not self._capabilities_ready:
            await owner._bootstrap_schema_cache()
            self._capabilities_ready = not owner._schema_bootstrap_degraded
        sequence = self.state.get("dispatchSequence", 0) + 1
        self.state["dispatchSequence"] = sequence
        self.store.save(self.state)
        phase_id = f"skill_{sequence:06d}"
        contract = {"_delegation": {"id": phase_id, "lineage": phase_id,
            "replaces": None, "reason": "Skill owner dispatch"}, "must_record_extraction": False,
            "_skill_operation": operation,
            "_user_context": {"originalUserTask": self.state["goal"],
                              "operatorInputs": self.state.get("operatorInputs", [])},
            "stop_condition": "Return the assigned subgoal's facts to the Skill owner."}
        if getattr(self, "execution_pages", None) is not None:
            operation["executionPages"] = sorted(self.execution_pages)
            if len(self.execution_pages) == 1:
                from harness.spawner.spawner_helpers import PinnedBrowserContext
                owner.spawner.pinned_browser_context = PinnedBrowserContext.from_value({
                    "pageId": next(iter(self.execution_pages)),
                    "fleetId": self.state.get("browserBinding", {}).get("fleetId")})
        phase = {"id": phase_id, "type": "browser_worker", "objective": instruction,
            "worker_task": instruction, "stage_hint": "generic", "depends_on": [],
            "expected_artifact": {}, "validators": [], "worker_contract": contract,
            "max_steps": None, "max_attempts": None}
        old_plan = owner.task_plan
        source = {"version": "v1", "execution_mode": "delegated", "goal": self.state["goal"],
                  "phases": [*((old_plan or {}).get("phases") or []), phase]}
        compiled, errors = validate_task_plan(source, user_task=self.state["goal"])
        if compiled is None:
            raise ValueError("Skill worker contract: " + "; ".join(errors))
        accept_task_plan(self.logger, compiled, previous_plan=old_plan, replan_reason="Skill dispatch",
            user_task=self.state["goal"], validator_review=None, source_plan=source,
            preserve_execution=old_plan is not None)
        owner.task_plan = compiled
        actual_phase = next(p for p in compiled["phases"] if p["id"] == phase_id)
        pinned = owner.spawner.pinned_browser_context is not None
        spawned = await owner.spawner.spawn_browser_agent(task=instruction, phase_id=phase_id,
            name="Workflow Executor" if operation["kind"] == "service" else None,
            worker_contract=actual_phase["worker_contract"], phase=actual_phase, task_plan=compiled,
            result_contract=operation.get("resultContract", ""),
            task_fleet_reference=None if pinned else owner.task_fleet_reference or None,
            session_key=None if pinned or owner.task_fleet_reference else f"skill-{self.logger.task_id}",
            page_policy="existing", reuse_scope="page",
            dispatch_origin="skill_orchestrator")
        result = await _wait_for_browser_mode_result(owner, spawned)
        self.state = self.store.load()
        ref = self.store.record(self.state, "skill.delegation.result", result)
        self._record_execution_feedback(operation, ref)
        answer = result.get("answer")
        try:
            answer = json.loads(answer) if isinstance(answer, str) else answer
        except ValueError:
            pass
        # Full worker traces are already indexed by the spawner. Only facts return to the owner.
        return {"status": result.get("status"), "workerId": result.get("workerId"),
            "phaseId": phase_id, "receiptRef": ref, "answer": answer,
            "handoff": result.get("handoff"), "error": result.get("error"),
            "execution": self.projection()}

    def _record_execution_feedback(self, operation, ref):
        self.state = self.store.load()
        coding_id = self.state.get("codingSessionId")
        job = self.state.get("repairs", {}).get(coding_id, {})
        if operation.get("kind") in {"execute", "browser", "review", "workflow_trial"} and job.get("rounds"):
            latest = job["rounds"][-1]
            if (latest.get("status") == "applied" or operation.get("kind") == "workflow_trial"
                    or latest.get("status") == "running"):
                latest.setdefault("executionFeedback", []).append({"receiptRef": ref,
                    "activeHash": inspect_skill(self.source_root())["hash"], "kind": operation["kind"],
                    "sourceHashes": operation.get("sourceHashes"), "probeSourceHash": operation.get("sourceHash")})
                self.store.save(self.state)

    async def run_workflows(self, *, attempt_inputs=None):
        if not self.state.get("plan"):
            raise ValueError("Bind an execution contract first")
        if attempt_inputs is not None:
            ids = {s["id"] for s in self.state["plan"]["steps"] if s["kind"] == "workflow"}
            if not isinstance(attempt_inputs, dict) or set(attempt_inputs) - ids or any(
                    not isinstance(v, dict) for v in attempt_inputs.values()):
                raise ValueError("attempt_inputs maps known workflow step IDs to input objects")
            if any(self.state["steps"][sid]["status"] != "pending" for sid in attempt_inputs):
                raise ValueError("Reconcile prior effects before supplying remaining input to a pending stage")
        root = self.source_root()
        hashes = {s["path"]: digest(read_source(root, s["path"])) for s in self.state["plan"]["steps"]
                  if s["kind"] == "workflow"}
        return await self._delegate({"kind": "execute", "revision": self.state["revision"], "attemptInputs": attempt_inputs or {},
            "sourceRoot": str(root), "sourceHashes": hashes, "override": bool(self.state.get("override") or self.state.get("trialSource") or self.state.get("developmentSource"))},
            "Execute the bound Skill sequence until a declared decision or unexpected result; return raw execution facts.")

    async def bind_page(self, page_id: str, refs: list[str]):
        self._check_evidence(refs)
        return await self._delegate({"kind": "bind_page", "pageId": page_id},
            "Bind the specified page after checking the assigned Fleet inventory and existing page lease.")

    def _check_evidence(self, refs: list[str]):
        if not refs:
            raise ValueError("Decision requires references to recorded facts or genuine user input")
        for ref in refs:
            self.logger.storage.read_resource(current_task_id=self.logger.task_id, resource_uri=ref) or self._missing(ref)

    @staticmethod
    def _missing(ref):
        raise ValueError(f"Evidence resource does not exist: {ref}")

    def resolve_step(self, sid: str, decision: str, reason: str, refs: list[str], output=None):
        self.state = self.store.load()
        if sid not in self.state.get("steps", {}) or decision not in {"complete", "skip", "retry"}:
            raise ValueError("Unknown step or resolution")
        if not reason.strip():
            raise ValueError("Resolution must explain its basis")
        self._check_evidence(refs)
        record = self.state["steps"][sid]
        if record["status"] == "running":
            raise ValueError("Cannot resolve a running execution")
        accepted_identity = None
        if decision in {"complete", "skip"}:
            step = next(s for s in self.state["plan"]["steps"] if s["id"] == sid)
            results = {key: r["output"] for key, r in self.state["steps"].items() if "output" in r}
            if output is not None:
                results[sid] = output
            accepted_identity = step_identity(self.source_root(), step,
                self.state["bindings"], results, self.state.get("browserBinding", {}))
        self.store.record(self.state, "skill.step.resolved", {"stepId": sid, "decision": decision,
            "reason": reason, "evidenceRefs": refs, "previous": copy.deepcopy(record)})
        record.update(status={"complete": "completed", "skip": "skipped", "retry": "pending"}[decision],
                      decisionReason=reason, evidenceRefs=refs, resolutionKind="model_resolution")
        if output is not None:
            record["output"] = output
        if accepted_identity is not None:
            record["acceptedIdentity"] = accepted_identity
        self.state["revision"] += 1
        self.store.save(self.state)
        return self.projection()

    async def recover_step(self, sid, diagnosis, action, reason, refs):
        self.state = self.store.load()
        if sid not in self.state.get("steps", {}):
            raise ValueError("Recovery must identify a bound step")
        if not reason.strip() or diagnosis not in {"input", "implementation", "business", "platform"}:
            raise ValueError("Supply a semantic diagnosis and its evidence")
        if action not in {"repair", "investigate", "rebind", "defer"}:
            raise ValueError("Unknown recovery action")
        if self.mode == "skill" and action == "repair":
            raise ValueError("Use /resume <builder-task-id> with this task's recorded evidence after business delivery")
        self._check_evidence(refs)
        recovery_id = uuid.uuid4().hex
        step = next(s for s in self.state["plan"]["steps"] if s["id"] == sid)
        item = {"recoveryId": recovery_id, "stepId": sid, "path": step.get("path"),
            "diagnosis": diagnosis, "action": action, "reason": reason,
            "evidenceRefs": refs, "status": "open", "baseHash": inspect_skill(self.source_root())["hash"]}
        self.state.setdefault("recoveries", {})[recovery_id] = item
        ref = self.store.record(self.state, "skill.recovery.opened", item)
        instruction = f"Recovery {recovery_id}, step {sid}. {reason}. Evidence: {json.dumps(refs)}"
        if action == "repair":
            if self.mode == "workflow":
                result = {"status": "awaiting_authoring", "next": "Edit the foreground workspace and execute the remaining actions; reconcile effects first"}
            else:
                result = await self.delegate_coding(instruction)
        elif action == "investigate":
            from harness.skill_runtime.delegation import delegate_browser
            result = await delegate_browser(self, instruction=instruction, purpose="recovery",
                step_id=sid, evidence_refs=refs, success_criteria=["Return the diagnosed current state and remaining subgoal facts"])
        else:
            result = {"status": "awaiting_owner", "next": "Rebind inputs or preserve the evidenced unresolved outcome"}
        return {"recoveryId": recovery_id, "receiptRef": ref, "result": result,
            "next": "Inspect the returned evidence and resolve_skill_recovery; Browser completion does not close a code defect"}

    def resolve_recovery(self, recovery_id, status, reason, refs):
        self.state = self.store.load()
        item = self.state.get("recoveries", {}).get(recovery_id)
        if not item or status not in {"repaired", "unverified", "deferred", "irrelevant"} or not reason.strip():
            raise ValueError("Resolve an owned recovery with a reason and a declared outcome")
        self._check_evidence(refs)
        if status == "repaired":
            current = next((w for w in self.program_evidence()["workflows"] if w["path"] == item.get("path")), None)
            matches = [e for e in (current or {}).get("currentExecutions", [])
                       if e["receiptRef"] in refs and e["execution"].get("status") == "succeeded"]
            if not matches:
                raise ValueError("A repaired program claim needs an actual execution receipt for its current source; use unverified or deferred otherwise")
            item = self.state["recoveries"][recovery_id]
            item["verifiedSourceHash"] = current["sourceHash"]
        item.update(status=status, resolutionReason=reason, resolutionEvidenceRefs=refs)
        ref = self.store.record(self.state, "skill.recovery.resolved", item)
        return {"receiptRef": ref, "recovery": item, "programEvidence": self.program_evidence()}

    async def delegate_coding(self, instruction: str, builder_task_id: str | None = None):
        from harness.skill_builder.session import BuilderSession
        from harness.skill_runtime.usage import record_usage
        self.state = self.store.load()
        if self.state.get("trialSource"):
            return {"status": "needs_builder_revision", "builderTaskId": self.state["trialSource"]["builderTaskId"],
                    "instruction": instruction, "execution": self.projection()}
        base_hash = inspect_skill(self.source_root())["hash"]
        builder_task_id = builder_task_id or self.state.get("codingSessionId")
        job = self.state.get("repairs", {}).get(builder_task_id) if builder_task_id else None
        if builder_task_id and not job:
            raise ValueError("Coding session must be owned by this session")
        if job:
            expected_base = job.get("appliedHash") or job["baseHash"]
            if expected_base != base_hash and self.coding_profile != "workflow":
                raise ValueError("Coding base changed; reconcile the active version before continuing this session")
            if job["status"] == "applied":
                work = self.coding_work_dir(builder_task_id)
                if inspect_skill(work)["hash"] != base_hash:
                    raise ValueError("Applied Coding workspace changed outside its recorded round")
        from harness.workflow_mode.coding import WorkflowCodingSession
        session_type, extra = WorkflowCodingSession, {"owner": self}
        repair = session_type(self.runtime, source_task_id=self.logger.task_id,
            skill_id=self.state["skillId"], owner_task_dir=self.logger.task_dir, profile=self.coding_profile,
            builder_task_id=builder_task_id, base_directory=self.source_root(),
            on_model_result=lambda result, source=None: record_usage(self.logger, result, source or self.mode + ".coding",
                conversation_id=repair.logger.task_id), **extra)
        repair.quiet = getattr(self, "quiet", False)
        request_id = repair.logger.task_id
        job = self.state.setdefault("repairs", {}).setdefault(request_id, {"rounds": []})
        round_id = uuid.uuid4().hex
        round_record = {"roundId": round_id, "baseHash": base_hash, "status": "running", "instruction": instruction}
        job.setdefault("rounds", []).append(round_record)
        job.update(status="running", baseHash=base_hash, instruction=instruction)
        self.state["codingSessionId"] = request_id
        self.store.save(self.state)
        feedback = [{"roundId": r["roundId"], "baseHash": r["baseHash"],
                     "draftHash": r.get("draftHash"), "status": r["status"],
                     "executionFeedback": r.get("executionFeedback", [])} for r in job["rounds"][-3:]]
        try:
            result = await repair.run(f"{self.mode} owner Coding request. Original user goal: " + self.state["goal"]
                + "\nExecution facts: " + json.dumps({"plan": self.state.get("plan"), "steps": self.state["steps"],
                    "browserBinding": self.state.get("browserBinding"), "codingRounds": feedback}, ensure_ascii=False)
                + "\nRepair subgoal: " + instruction, register_human_references=False)
        except BaseException:
            self.state = self.store.load()
            job = self.state["repairs"][request_id]
            round_record = job["rounds"][-1]
            job["status"] = round_record["status"] = "interrupted"
            self.store.save(self.state)
            raise
        # Coding may execute files through this owner while its turn is active.
        # Reload those durable receipts before recording the returned revision.
        self.state = self.store.load()
        job = self.state["repairs"][request_id]
        round_record = job["rounds"][-1]
        draft_hash = (result.get("skill") or {}).get("hash")
        applied = bool(draft_hash and job.get("appliedHash") == draft_hash
                       and inspect_skill(self.source_root())["hash"] == draft_hash)
        job["status"] = round_record["status"] = "applied" if applied else "returned"
        round_record["draftHash"] = draft_hash
        for file in repair.work_dir.rglob("*"):
            if file.is_file():
                self.logger.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
                    resource_type="coding_agent_output", logical_path=file.resolve().relative_to(self.logger.task_dir.resolve()).as_posix(),
                    external_path=str(file), media_type="text/plain", metadata={"repairId": request_id})
        ref = self.store.record(self.state, "skill.coding.result", {"builderTaskId": request_id, **result})
        round_record["resultRef"] = ref
        self.store.save(self.state)
        return {"builderTaskId": request_id, "roundId": round_id, "receiptRef": ref, **result}

    def coding_work_dir(self, request_id: str) -> Path:
        folder = "workflows" if self.coding_profile == "workflow" else "repairs"
        return self.logger.task_dir / "coding" / folder / request_id / "builder"

    async def apply_repair(self, request_id: str, expected_hash: str):
        from harness.runtime.hitl_input import read_terminal_input
        self.state = self.store.load()
        job = self.state.get("repairs", {}).get(request_id)
        if not job or job["status"] not in {"returned", "running", "applied"}:
            raise ValueError("Repair must belong to this Skill session and be running or returned")
        root = self.source_root()
        base_hash = inspect_skill(root)["hash"]
        if base_hash not in {job["baseHash"], job.get("appliedHash")}:
            raise ValueError("Repair base no longer matches the active Skill")
        work = self.logger.task_dir.resolve() / "coding" / "repairs" / request_id / "builder"
        info = inspect_skill(work)
        if info["hash"] != expected_hash or info["metadata"].get("name") != self.state["skillId"]:
            raise ValueError("Repair contents or Skill identity changed")
        if base_hash == expected_hash:
            job["appliedHash"] = expected_hash
            if job["status"] != "running":
                job["status"] = "applied"
                if job.get("rounds"):
                    job["rounds"][-1]["status"] = "applied"
            self.store.save(self.state)
            return {"status": "applied", "execution": self.projection()}
        diffs = []
        readable = []
        files = sorted(set(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) |
                       set(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()))
        for path in files:
            before = (root / path).read_text() if (root / path).is_file() else ""
            after = (work / path).read_text() if (work / path).is_file() else ""
            if before != after:
                diffs.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                fromfile="before/" + path, tofile="after/" + path))
                from harness.runtime.session_output import readable_diff
                readable.append(readable_diff(path, before, after))
        round_id = job.get("rounds", [{}])[-1].get("roundId", "legacy")
        diff_ref = self.store.write(f"coding/repairs/{request_id}/rounds/{round_id}/diff.json", {"diff": "".join(diffs)}, "coding_repair_diff")
        diff_path = work.parent / "rounds" / round_id / "changes.diff"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text("".join(diffs), encoding="utf-8")
        self.logger.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
            resource_type="coding_repair_diff", logical_path=diff_path.relative_to(self.logger.task_dir.resolve()).as_posix(),
            external_path=str(diff_path), media_type="text/plain", metadata={"repairId": request_id})
        from harness.runtime.session_output import block
        block("Skill repair", "任务内修订 · 等待授权", [("版本", expected_hash), ("完整原始差异", diff_path)])
        print("".join(readable)[:12000] or "文件排版或编码有变化；请查看完整原始差异。", flush=True)
        answer = await read_terminal_input(f"Apply repair {request_id} ({expected_hash}) to this task? "
            f"Full diff: {diff_path}. Enter yes or no. Publication requires /skill-publish.")
        if str(answer or "").strip().lower() not in {"yes", "y"}:
            ref = self.store.record(self.state, "skill.repair.not_applied", {
                "repairId": request_id, "diffRef": diff_ref, "answer": answer})
            return {"status": "not_applied", "diffRef": diff_ref, "decisionRef": ref}
        if inspect_skill(work)["hash"] != expected_hash or inspect_skill(root)["hash"] != base_hash:
            raise ValueError("Skill changed while repair authorization was pending")
        destination = self.logger.task_dir / "skill" / "overrides" / expected_hash
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if inspect_skill(destination)["hash"] != expected_hash:
                raise ValueError("Existing override hash conflicts")
        else:
            shutil.copytree(work, destination)
        for file in destination.rglob("*"):
            if file.is_file():
                self.logger.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
                    resource_type="coding_agent_output", logical_path=file.relative_to(self.logger.task_dir).as_posix(),
                    external_path=str(file), media_type="text/plain", metadata={"repairId": request_id})
        self.state["override"] = {"path": destination.relative_to(self.logger.task_dir).as_posix(), "hash": expected_hash}
        running = job["status"] == "running"
        job["status"] = "running" if running else "applied"
        job["appliedHash"] = expected_hash
        if job.get("rounds"):
            job["rounds"][-1].update(status="running" if running else "applied", appliedHash=expected_hash, diffRef=diff_ref)
        self.state["revision"] += 1
        ref = self.store.record(self.state, "skill.repair.authorized", {"repairId": request_id,
            "hash": expected_hash, "diffRef": diff_ref, "answer": answer})
        return {"status": "applied", "authorizationRef": ref, "execution": self.projection()}

    async def _dispatch(self, name, args):
        try:
            self.state = self.store.load() or self.state
            if name == "skill_overview": value = self.overview()
            elif name == "list_fleet_pages":
                value = await self._delegate({"kind": "inventory"}, "Read Page.list for the assigned Fleet; do not select a page")
            elif name == "read_skill_resource": value = self.read_skill_resource(args["path"], args.get("offset", 0), args.get("max_chars", 16000))
            elif name == "read_execution_state": value = self.read_execution_state(**args)
            elif name == "bind_skill_page": value = await self.bind_page(args["page_id"], args["evidence_refs"])
            elif name == "bind_skill_plan": value = self.bind_plan(args["bindings"], args.get("plan"), args.get("reason", ""))
            elif name == "run_skill_workflows": value = await self.run_workflows()
            elif name == "control_workflow_execution":
                value = await self._delegate({"kind": "workflow_control", "workflowId": args["workflow_id"],
                    "action": args["action"]}, "Control only this task's recorded in-flight Workflow; reconcile its effects")
            elif name == "delegate_browser":
                from harness.skill_runtime.delegation import delegate_browser
                value = await delegate_browser(self, **args)
            elif name == "resolve_skill_step": value = self.resolve_step(args["step_id"], args["decision"], args["reason"], args["evidence_refs"], args.get("output"))
            elif name == "recover_skill_step": value = await self.recover_step(args["step_id"], args["diagnosis"], args["action"], args["reason"], args["evidence_refs"])
            elif name == "resolve_skill_recovery": value = self.resolve_recovery(args["recovery_id"], args["status"], args["reason"], args["evidence_refs"])
            elif name == "delegate_coding": value = await self.delegate_coding(args["instruction"], args.get("builder_task_id"))
            elif name == "apply_skill_repair": value = await self.apply_repair(args["builder_task_id"], args["draft_hash"])
            elif name in LOCAL_FILE_TOOLS:
                from harness.skill_runtime.local_files import execute
                value = await execute(self, name, args)
                return _result(value, error=value.get("status") in {"failed", "needs_human"})
            elif name == "read_task_receipt":
                record = self.logger.storage.read_resource(current_task_id=self.logger.task_id, resource_uri=args["reference"])
                if not record: raise ValueError("Receipt is missing")
                text = record.get("content_json") or record.get("content_text") or json.dumps(record, ensure_ascii=False)
                if not isinstance(text, str): text = json.dumps(text, ensure_ascii=False)
                offset = max(0, args.get("offset", 0)); limit = max(1, min(30000, args.get("max_chars", 12000)))
                value = {"reference": args["reference"], "content": text[offset:offset+limit],
                    "nextOffset": offset + limit, "hasMore": offset + limit < len(text)}
            elif name == "request_skill_input":
                from harness.skill_runtime.decisions import request_input
                return await request_input(self, **args)
            elif name == "withdraw_skill_input":
                from harness.skill_runtime.decisions import withdraw_input
                value = withdraw_input(self, **args)
            elif name == "finish_skill":
                from harness.skill_runtime.terminal import finish
                return await finish(self, args)
            else: raise ValueError("Unknown Skill tool")
            if name == "delegate_coding" and value.get("status") == "needs_builder_revision":
                self.finished = {"status": "incomplete", "summary": args["instruction"],
                    "remainingWork": ["Coding Builder continues with this trial's recorded execution facts"],
                    "taskId": self.logger.task_id, "execution": self.projection()}
                self.state["status"] = "incomplete"
                self.store.save(self.state)
                return _result(value, terminate=True)
            return _result(value)
        except (ValueError, FileNotFoundError, PermissionError, ResourceAccessError) as exc:
            return _result({"status": "rejected", "type": type(exc).__name__, "reason": str(exc)}, error=True)

    def tools(self):
        definitions = [
            ("skill_overview", "Read the pinned Skill, orchestration and current execution facts.", _schema({})),
            ("list_fleet_pages", "Call WebCross Page.list in this task's assigned Fleet without a model or a page binding. Returns inventory evidence; use bind_skill_page to select an execution page.", _schema({})),
            ("read_skill_resource", "Read parameter definitions, manifest or source from the exact active Skill version. Task receipts use read_task_receipt.", _schema({"path": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("path",))),
            ("read_execution_state", "Read a full stored state section, such as workflowTrials, by bounded pages; active summaries do not delete historical evidence.",
             _schema({"section": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("section",))),
            ("bind_skill_page", "Select an evidenced page; check WebCross Fleet inventory and acquire the existing page lease, without a model call.", _schema({"page_id": _STRING, "evidence_refs": {"type": "array", "items": _STRING}}, ("page_id", "evidence_refs"))),
            ("bind_skill_plan", "Instantiate a published or task-local plan; preserve prior effects when revising. Inputs are flat business data; binding.pageId selects an execution page.", _schema({"bindings": _OBJECT, "plan": plan_schema(), "reason": _STRING}, ("bindings",))),
            ("run_skill_workflows", "Continuously execute decided Workflow steps; yield at decisions or unexpected receipts.", _schema({})),
            ("delegate_browser", "Spawn Browser for a planned browser stage, semantic/visual operation, UI restoration or unsupported subgoal. Return existing Worker handoff and raw evidence; reconcile checkpoints explicitly. No required prior Workflow failure.",
             _schema({"instruction": _STRING, "purpose": _STRING, "step_id": _STRING,
                "success_criteria": {"type": "array", "items": _STRING}, "starting_state": _OBJECT,
                "evidence_refs": {"type": "array", "items": _STRING}}, ("instruction", "purpose", "success_criteria"))),
            ("resolve_skill_step", "Record a semantic resolution grounded in task-owned evidence; retry requires side-effect reconciliation.", _schema({"step_id": _STRING, "decision": {"type": "string", "enum": ["complete", "skip", "retry"]}, "reason": _STRING,
                "evidence_refs": {"type": "array", "items": _STRING}, "output": _OBJECT}, ("step_id", "decision", "reason", "evidence_refs"))),
            ("recover_skill_step", "Record your evidence-based diagnosis. repair delegates to the persistent Coding session; investigate delegates a bounded Browser subgoal. Input/business/platform diagnoses never automatically become code defects.", _schema({
                "step_id": _STRING, "diagnosis": {"type": "string", "enum": ["input", "implementation", "business", "platform"]},
                "action": {"type": "string", "enum": ["repair", "investigate", "rebind", "defer"]}, "reason": _STRING,
                "evidence_refs": {"type": "array", "items": _STRING}}, ("step_id", "diagnosis", "action", "reason", "evidence_refs"))),
            ("resolve_skill_recovery", "Preserve repaired/unverified/deferred/irrelevant outcomes independently of business checkpoints. repaired requires a current-source execution receipt; you judge actual path coverage and acceptance.", _schema({
                "recovery_id": _STRING, "status": {"type": "string", "enum": ["repaired", "unverified", "deferred", "irrelevant"]},
                "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING}}, ("recovery_id", "status", "reason", "evidence_refs"))),
            ("delegate_coding", "Repair a task-local Skill copy, or resume this owner's repair by Builder ID; no publication.", _schema({"instruction": _STRING, "builder_task_id": _STRING}, ("instruction",))),
            ("apply_skill_repair", "Show a concrete diff and obtain human authorization for a returned repair.", _schema({"builder_task_id": _STRING, "draft_hash": _STRING}, ("builder_task_id", "draft_hash"))),
            ("read_task_receipt", "Read one task-owned resource, including full execution receipts.", _schema({"reference": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("reference",))),
            ("request_skill_input", "Ask for a missing human business decision before abandoning dependent work. Persist the question; an answer returns to this loop for rebinding/replanning, while EOF pauses for /resume without granting a decision or permission.",
             _schema({"question": _STRING, "reason": _STRING,
                "evidence_refs": {"type": "array", "items": _STRING},
                "blocked_steps": {"type": "array", "items": _STRING}}, ("question",))),
            ("withdraw_skill_input", "Withdraw an obsolete pending BUSINESS question after user guidance; supplies no answer and cannot withdraw file/page authorization requests.",
             _schema({"input_id": _STRING, "reason": _STRING}, ("input_id", "reason"))),
            ("control_workflow_execution", "Control a task-owned Workflow returned paused after human guidance; its original execute request remains in flight. status/resume/cancel/pause preserve real receipts. Inspect effects before a new invocation.",
             _schema({"workflow_id": _STRING, "action": {"type": "string", "enum": ["status", "pause", "resume", "cancel"]}}, ("workflow_id", "action"))),
            ("finish_skill", "Propose done or incomplete for independent semantic review. The reviewer can return recovery, ask the human and continue, or accept an evidenced ending.", _schema({"status": {"type": "string", "enum": ["done", "incomplete"]}, "summary": _STRING, "remaining_work": {"type": "array", "items": _STRING}, "known_limitations": {"type": "array", "items": _STRING}}, ("status", "summary", "remaining_work"))),
        ]
        definitions.extend(local_file_definitions())
        if self.mode == "skill":
            definitions = [item for item in definitions if item[0] not in {"delegate_coding", "apply_skill_repair"}]
            for name, _description, schema in definitions:
                if name == "recover_skill_step":
                    schema["properties"]["action"]["enum"] = ["investigate", "rebind", "defer"]
            definitions = [(name, "Record diagnosis and task evidence; investigate a bounded Browser subgoal, rebind inputs or defer optimization to /resume <builder-task-id>.", schema)
                           if name == "recover_skill_step" else (name, description, schema)
                           for name, description, schema in definitions]
        tools = []
        for name, description, schema in definitions:
            async def execute(call_id, arguments, signal=None, on_update=None, *, _name=name):
                if signal is not None and signal.is_cancelled(): return _result({"status": "cancelled"}, error=True)
                return await self._dispatch(_name, arguments)
            tools.append(AgentTool(name=name, label=name, description=description, parameters=schema,
                prepare_arguments=lambda value, _schema=schema: _prepare_tool_arguments(value, _schema),
                execute_fn=execute, execution_mode="sequential"))
        return tools

    def _usage(self, result):
        from harness.skill_runtime.usage import record_usage
        record_usage(self.logger, result, self.mode + ".orchestrator")

    async def prepare_model_context(self, messages):
        return messages

    @steerable_task
    async def run(self, goal: str, *, resume_instruction: str = ""):
        self.finished = None
        self.state = self.store.load() or self.state
        if not self.state:
            cfg = self.runtime.harness
            if not cfg.forced_skill_id or not cfg.forced_skill_hash: raise ValueError("Select a published Skill first")
            self.state = {"schemaVersion": 1, "skillId": cfg.forced_skill_id,
                "skillHash": cfg.forced_skill_hash, "goal": goal, "revision": 0,
                "status": "active", "steps": {}, "operatorInputs": [], "receipts": [], "repairs": {}}
        else:
            if self.mode == "skill" and (self.state["skillId"] != self.runtime.harness.forced_skill_id or self.state["skillHash"] != self.runtime.harness.forced_skill_hash):
                raise ValueError("Resume must keep the saved Skill version")
            for record in self.state.get("steps", {}).values():
                if record["status"] == "running": record.update(status="uncertain", reason="execution_interrupted")
            for repair in self.state.get("repairs", {}).values():
                if repair["status"] == "running": repair["status"] = "interrupted"
            if resume_instruction and not self.state.get("pendingInput"):
                self.state.setdefault("operatorInputs", []).append({"source": "resume_cli", "text": resume_instruction,
                    "inputId": uuid.uuid4().hex})
        self.source_root()
        self.store.save(self.state)
        if self.state.get("pendingInput"):
            from harness.skill_runtime.decisions import record_answer, request_input
            if resume_instruction.strip():
                record_answer(self, resume_instruction, source="resume_cli")
            else:
                await request_input(self, self.state["pendingInput"]["question"])
                if self.finished:
                    return self.finished
        from harness.planning.context import retain_operator_inputs
        retain_operator_inputs(self.logger, self.state.get("operatorInputs", []))
        prior = self.store.read("skill/orchestrator/context.json",
                                self.store.read("skill/orchestrator/messages.json", []))
        messages = [TypeAdapter(AgentMessage).validate_python(m) for m in prior]
        config = skill_orchestrator_model_config(self.runtime)
        raw_provider = LLMFactory.create_provider(config)
        adapter = HarnessProviderAdapter(raw_provider, on_result=self._usage)
        tools = self.tools()
        prompt = "Original goal: " + self.state["goal"] + "\nCurrent execution: " + json.dumps(self.projection(), ensure_ascii=False)
        history = [TypeAdapter(AgentMessage).validate_python(m) for m in self.store.read("skill/orchestrator/messages.json", [])]
        from harness.runtime.agent_loop import agent_events
        stop_reason = None
        async for event, active in agent_events(logger=self.logger, runtime_config=self.runtime.harness,
                raw_provider=raw_provider, provider=adapter, model=config.model_id,
                system=self.system_prompt, tools=tools, messages=messages, prompt=prompt,
                context_path="skill/orchestrator/context.json", actor=self.mode + ".orchestrator",
                facts=self.projection, prepare_context=self.prepare_model_context,
                label=self.mode.title(), quiet=getattr(self, "quiet", False)):
            if isinstance(event, MessageEndEvent):
                history.append(event.message)
                self.store.write("skill/orchestrator/messages.json", [m.model_dump(by_alias=True, exclude_none=True) for m in history])
                self.store.write("skill/orchestrator/context.json", [m.model_dump(by_alias=True, exclude_none=True) for m in active])
                if isinstance(event.message, AssistantMessage) and event.message.text:
                    from harness.runtime.session_output import message
                    if not getattr(self, "quiet", False): message(self.mode.title(), event.message.text)
                if isinstance(event.message, AssistantMessage) and event.message.error_message:
                    stop_reason = event.message.error_message
            elif isinstance(event, ToolExecutionEndEvent):
                from harness.runtime.session_output import tool_result
                if not getattr(self, "quiet", False): tool_result(self.mode.title(), event.tool_name, event.result, error=event.is_error)
                self.logger.write("skill.orchestrator.tool", {"tool": event.tool_name,
                    "toolCallId": event.tool_call_id, "isError": event.is_error})
        if self.finished:
            return self.finished
        self.state = self.store.load()
        self.state.update(status="incomplete", stopReason=stop_reason or "No reviewed completion was returned")
        self.store.save(self.state)
        self.export_package()
        return {"status": "incomplete", "summary": f"{self.mode.title()} session stopped: {self.state['stopReason']}. Resume its saved task.",
                "taskId": self.logger.task_id, "execution": self.projection(),
                "activeSourceHash": inspect_skill(self.source_root())["hash"], "programEvidence": self.program_evidence(),
                "next": [f"/resume {self.logger.task_id}"]}

    async def close(self):
        try:
            await self.executor.close()
            if hasattr(self, "_private_auth_barrier"):
                # The parent owns the shared barrier lifecycle.
                self.runner.spawner.fleet_auth_barrier = self._private_auth_barrier
            await self.runner.spawner.shutdown()
        finally:
            self.catalog.close()
