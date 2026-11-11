"""Workflow task, author and coordinator sessions on the shared Tau loop.

run         business delivery with a pinned package, optional item dispatch and read-only observation
author      one draft: code, execute, observe, fix in a single context
coordinator split a /workflow run request into concurrent run tasks
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from harness._vendor.tau_agent.events import MessageEndEvent, ToolExecutionStartEvent, ToolExecutionEndEvent
from harness._vendor.tau_agent.messages import AgentMessage, AssistantMessage, ToolResultMessage, TextContent
from harness.planning.fleet_reference import extract_fleet_reference
from harness.runtime.model_config import browser_agent_model_config, workflow_agent_model_config
from harness.runtime.steering import current_steering, steerable_task
from harness.runtime.tau_bridge import HarnessProviderAdapter, record_usage
from harness.runtime.wait_timing import workflow_timing
from harness.workflow_mode import acceptance, authoring, coverage, guidance, package as pkg
from harness.workflow_mode.input_reference import resolve_input, reference_for, manifest_reference
from harness.workflow_mode.projection import (checkpoint_view, failure_evidence, preflight_view,
                                              summary_evidence_scope)
from harness.workflow_mode.evidence import resolve_reference, short_reference
from harness.workflow_mode.usage import Usage, migrate_usage
from harness.workflow_mode.platform import CONTROL_METHODS, OBSERVE_METHODS, TERMINAL, WorkflowPlatform
from harness.workflow_mode.prompt import AUTHOR_SYSTEM, COORDINATOR_SYSTEM, RUN_SYSTEM, OBSERVER_SYSTEM
from harness.workflow_mode.registry import Registry, RegistryError
from harness.workflow_mode.runs import RunRegistry
from harness.workflow_mode.state import StateStore
from harness.workflow_mode.conversation import ConversationStore
from harness.workflow_mode.tools import (A, B, I, O, S, compact, image_content, local_file_tools,
                                         evidence_reference, observation_view, pointer, receipt_tool,
                                         result, schema, sha256_text, tool, ToolResultError)

PROFILES = {"run": RUN_SYSTEM, "author": AUTHOR_SYSTEM, "coordinator": COORDINATOR_SYSTEM,
            "observer": OBSERVER_SYSTEM}
_MAX_CONCURRENT_TASKS = 8


def offline_tests_usage() -> str:
    from harness.workflow_mode.offline_tests import USAGE
    return USAGE


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _result_delivery_summary(receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Keep transport integrity/completeness visible without echoing retained output."""
    response = receipt.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    delivered = receipt.get("resultDelivery") or {}
    wire = data.get("delivery") or {} if isinstance(data, dict) else {}
    reference = delivered.get("reference") or wire.get("reference")
    if not isinstance(reference, dict):
        return None
    return {"mode": wire.get("mode") or "retained", "verified": delivered.get("verified"),
            "pages": delivered.get("pages"), "reference": {key: reference.get(key) for key in
                ("workflowId", "taskId", "bytes", "sha256", "callCount", "omittedValueCount", "expiresAt")}}


def _strings(value: Any) -> set[str]:
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


class StopRequested(RuntimeError):
    """The user asked this task to stop."""


class WorkflowSession:
    def __init__(self, runtime: Any, logger: Any, *, profile: str, host: Any = None,
                 label: str | None = None, quiet: bool = False, batch_id: str | None = None):
        if profile not in PROFILES:
            raise ValueError(f"Unknown Workflow profile {profile}")
        self.runtime, self.logger, self.profile, self.host = runtime, logger, profile, host
        self.label = label or {"run": "Workflow", "author": "Workflow 作者", "coordinator": "Workflow 调度",
                               "observer": "Browser 取证"}[profile]
        self.quiet, self.batch_id = quiet, batch_id
        self.store = StateStore(logger)
        self.conversation = ConversationStore(self.store)
        self.state = migrate_usage(self.store.load())
        self.registry = Registry.for_runtime(runtime)
        # Local data tools cannot become a second package editor, including
        # when the trusted registry lives outside the Harness installation.
        from harness.tools.path_authorization import state as local_policy
        local_policy(self)["protected"].update({str(self.registry.root.resolve()),
            str((Path(logger.task_dir) / "workflow").resolve())})
        self.runs = RunRegistry(runtime.harness.worktree_dir)
        self.platform = WorkflowPlatform(runtime, logger, self.store, label=self.label)
        self.platform.on_receipt = self._receipt_added
        self.usage = Usage(profile, spent=self.state.get("usage"))
        self.finished: dict[str, Any] | None = None
        self.children: dict[str, dict[str, Any]] = {}
        self._batch_platforms = set()
        self._execution_platforms = {}
        self.stop_reason: str | None = None
        self._author_guidance: dict[str, Any] = {}
        self._author_entry_facts: dict[str, Any] = {}
        from harness.workflow_mode.local_execution import LocalScriptRunner
        self.local_runner = LocalScriptRunner(self)
        from harness.workflow_mode.batch import recover_interrupted
        if recover_interrupted(self.state):
            self.save()

    # The Browser local-file handlers read these attributes.
    @property
    def worker_contract(self) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------ state
    def _receipt_added(self, ref: str, event: str) -> None:
        receipts = self.state.setdefault("receipts", [])
        if not any(item.get("ref") == ref for item in receipts):
            receipts.append({"ref": ref, "event": event})

    def save(self) -> None:
        """Persist in-memory state without dropping receipts written by other writers."""
        stored = self.store.load().get("receipts", []) if self.state else []
        known = {item.get("ref") for item in self.state.get("receipts", [])}
        merged = list(self.state.get("receipts", [])) + [r for r in stored if r.get("ref") not in known]
        self.state["receipts"] = merged
        self.state["usage"] = self.usage.persisted()
        self.store.save(self.state)

    def reload(self) -> dict[str, Any]:
        self.state = migrate_usage(self.store.load() or self.state)
        return self.state

    def initialize(self, goal: str, *, package_row: dict[str, Any] | None = None,
                   draft: dict[str, Any] | None = None, fleet_id: str | None = None) -> None:
        if self.state:
            return
        self.state = {"schemaVersion": 1, "profile": self.profile, "goal": goal, "status": "active",
                      # Task-local grants, recorded after Fleet.list confirms a full ID.
                      "authorizedFleets": [], "fleetReference": fleet_id,
                      "binding": None, "pageHistory": [], "executions": [], "acceptancePoints": [],
                      "acceptanceHistory": [], "operatorInputs": [], "defects": [], "delegations": [],
                      "versions": [], "children": {}, "receipts": []}
        if package_row:
            self.state["package"] = {k: package_row.get(k) for k in
                                     ("slug", "templateId", "displayName", "contentHash", "origin", "sourceDraft",
                                      "workshopId", "revision", "manifestHash", "lookupReceipt")}
        if draft:
            self.state["draft"] = {k: draft.get(k) for k in
                                   ("taskId", "slug", "templateId", "kind", "baseHash", "sourceDraft", "sourceHash")}
        self.save()

    def package_dir(self) -> Path:
        """Files the agent reads and edits: the draft (author) or the pinned version."""
        if self.profile == "author":
            return self.registry.draft_path(self.state["draft"]["taskId"])
        info = self.state.get("package") or {}
        path = Path(self.logger.task_dir) / "workflow" / "versions" / str(info.get("contentHash"))
        if not path.is_dir():
            if info.get("origin") == "draft":
                raise RegistryError("The historical execution snapshot is missing; recover that exact version. "
                                    "Use /workflow-edit for current author work; it cannot replace this run's evidence.")
            self.registry.restore_snapshot(info, Path(self.logger.task_dir))
        return path

    def known_refs(self) -> set[str]:
        refs = {item["ref"] for item in self.state.get("receipts", [])}
        refs.update(e["receiptRef"] for e in self.state.get("executions", []) if e.get("receiptRef"))
        return refs

    def fleet_scope(self) -> dict[str, Any]:
        """Task selection/registration facts; WebCross permissions live in Action receipts."""
        return {"userReference": self.state.get("fleetReference"),
                "confirmedFleetIds": list(self.state.get("authorizedFleets") or []),
                "bindingStatus": "bound" if self.state.get("binding") else "unbound"}

    def projection(self) -> dict[str, Any]:
        from harness.workflow_mode import materials, work_notes
        state = self.state
        executions = state.get("executions", [])
        return {"taskId": self.logger.task_id, "profile": self.profile, "status": state.get("status"),
                "pendingQuestion": state.get("pendingQuestion"),
                "pendingInteraction": state.get("pendingInteraction"),
                "package": state.get("package") or state.get("draft"), "binding": state.get("binding"),
                "packageContract": state.get("packageContract"),
                "fleetScope": self.fleet_scope(),
                "executions": [{k: e.get(k) for k in ("receiptRef", "status", "workflowId", "outcome",
                                                      "contentHash", "stages", "pageId", "identityIssues")}
                               for e in executions[-6:]], "executionCount": len(executions),
                "acceptancePoints": state.get("acceptancePoints"), "defects": state.get("defects", [])[-5:],
                "browserCompletions": [d for d in state.get("delegations", []) if d.get("mode") == "completion"][-3:],
                "acceptanceReview": self.acceptance_review(),
                "children": {k: {f: v.get(f) for f in ("label", "status", "conclusion", "resultRef")}
                             for k, v in (state.get("children") or {}).items()},
                "workNotes": work_notes.projection(self),
                "materialManifest": state.get("materialManifest"),
                "materialHandoff": materials.projection(self),
                "originalGoalLocation": {"path": "task_manifest.json", "pointer": "/original_user_task"},
                "executionBatches": [{"batchId": b['batchId'], "status": b['status'],
                    "contentHash": b['contentHash'], "items": [{k:i.get(k) for k in
                    ('id','status','pageId','receiptRef','workflowId')} for i in b['items']]}
                    for b in state.get('executionBatches', [])[-2:]],
                "localExecutions": state.get("localExecutions", [])[-6:],
                **({"authorExperiments": state.get("authorExperiments", [])[-3:],
                    "authorCost": state.get("authorCostLatest"),
                    "authorEntry": self._author_entry_facts,
                    "authorTaskInitialization": state.get("authorTaskInitialization")}
                   if self.profile == "author" else {}),
                "usage": self.usage.snapshot()}

    # ------------------------------------------------------- user inputs
    def record_input(self, text: str, *, source: str, question: str | None = None) -> dict[str, Any]:
        item = {"inputId": uuid.uuid4().hex, "source": source, "text": text,
                "question": question, "taskId": self.logger.task_id}
        self.state.setdefault("operatorInputs", []).append(item)
        self.save()
        from harness.planning.context import retain_operator_inputs
        retain_operator_inputs(self.logger, [item])
        return item

    def all_inputs(self) -> list[dict[str, Any]]:
        """Original goal, attributed answers and live terminal guidance."""
        inputs = [{"inputId": "original-goal", "source": "original_goal", "text": self.state["goal"],
                   "taskId": self.logger.task_id}, *self.state.get("operatorInputs", [])]
        manager = current_steering()
        if manager is not None:
            inputs += list(manager.records)
        return inputs

    def acceptance_review(self) -> dict[str, Any]:
        """Expose unreviewed user changes; the model decides their semantic effect."""
        revision = self.state.get("acceptanceRevision") or {}
        reviewed = set(revision.get("reviewedUserInputIds") or [])
        pending = {i["inputId"]: i for i in self.all_inputs()
                   if i.get("inputId") and i.get("source") in acceptance.USER_SOURCES
                   and i["inputId"] not in reviewed}
        return {"revision": revision.get("number", 0), "pendingUserInputs": list(pending.values()),
                "instruction": "Decide whether these inputs change acceptance. Use set_acceptance_points "
                               "with reviewed_user_input_ids and reason; unchanged points are allowed. "
                               "This is a review reminder, not an automatic business verdict."}

    async def ask_terminal(self, prompt: str, *, kind: str = "business_input") -> str | None:
        from harness.runtime.hitl_input import read_terminal_input
        with self.waiting(kind):
            answer = await self._await_interaction(read_terminal_input(
                f"[{self.label} · @{self.logger.task_id[:8]}] {prompt}"))
        return answer if isinstance(answer, str) else None

    async def _await_interaction(self, awaitable):
        """Keep user stops responsive during a wait, including stops from another CLI."""
        pending = asyncio.ensure_future(awaitable)
        try:
            while not pending.done():
                self._check_stop()
                await asyncio.wait({pending}, timeout=1)
            self._check_stop()
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    @contextmanager
    def waiting(self, kind: str):
        """Account for a suspended session without calling child work human wait."""
        wait_id = uuid.uuid4().hex
        self.logger.write("workflow.wait.started", {"waitId": wait_id, "kind": kind})
        try:
            with (self.usage.waiting_for_human() if kind != "child_tasks" else nullcontext()):
                yield
        finally:
            self.logger.write("workflow.wait.finished", {"waitId": wait_id, "kind": kind})

    # ---------------------------------------------------------- binding
    async def known_fleets(self) -> list[str]:
        listed = await self.platform.call("Fleet.list", {}, allowed=OBSERVE_METHODS)
        if not listed.get("ok"):
            raise ToolResultError({**listed, "method": "Fleet.list", "inventoryStatus": "unconfirmed"})
        data = listed.get("data")
        rows = data if isinstance(data, list) else data.get("fleets") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ToolResultError({"status": "invalid_inventory", "method": "Fleet.list",
                "receiptRef": listed.get("receiptRef"),
                "error": "Fleet.list did not return an inventory; Fleet existence is unconfirmed."})
        return [str(r.get("fleetId") or r.get("id")) for r in rows if isinstance(r, dict) and (r.get("fleetId") or r.get("id"))]

    async def authorize_fleet(self, fleet_id: str) -> bool:
        """Authorize only a Fleet that exists, by its full ID; never a placeholder."""
        fleets = self.state.setdefault("authorizedFleets", [])
        if fleet_id in fleets:
            return True
        existing = await self.known_fleets()
        if fleet_id not in existing:
            raise ValueError(f"Fleet {fleet_id!r} does not exist in WebCross. Use a full Fleet ID from list_pages; "
                             "if none fits, ask the user for an @Fleet.")
        reference = self.state.get("fleetReference")
        if reference and fleet_id.startswith(reference):
            fleets.append(fleet_id)
            self.save()
            return True
        if reference:
            return False
        if getattr(self.runtime.harness, "local_access_auto_approve", False):
            # Auto-approve: the Fleet was verified to exist above; record the grant.
            self.logger.write("workflow.fleet.authorization", {"fleetId": fleet_id, "approved": True,
                                                                 "selection": "auto"})
            fleets.append(fleet_id)
            self.save()
            return True
        answer = await self.ask_terminal(
            f"任务要在 Fleet {fleet_id} 上执行 Workflow（可能修改页面上的业务数据）。输入 yes 授权本任务使用该 Fleet。")
        approved = str(answer or "").strip().lower() in {"y", "yes"}
        self.logger.write("workflow.fleet.authorization", {"fleetId": fleet_id, "approved": approved})
        if approved:
            fleets.append(fleet_id)
            self.save()
        return approved

    # ------------------------------------------------------------- tools
    def tools(self) -> list:
        if self.profile == "observer":
            from harness.workflow_mode.observer import observer_tools
            return observer_tools(self)
        from harness.workflow_mode.materials import prepare_tool
        common = local_file_tools(self) + [prepare_tool(self),
            tool("read_contract", "Read the package contract, metadata and params schema together. "
                 "Includes the coverage inventory summary; read coverage.json for specific controls/scenarios. "
                 "Request include_inventory only when you need source/test file names.",
                 schema({"include_inventory": B}), self._read_contract),
            tool("read_package_file", "Read one package file (README, src, tests, contract) by relative path.",
                 schema({"path": S, "offset": I, "max_chars": I}, ("path",)), self._read_package_file),
            tool("ask_user", "Wait for user input without ending this task; returns the answer and its inputId. "
                 "While authoring, ask early about ambiguous reuse scenarios and later about newly discovered scope changes.",
                 schema({"question": S}, ("question",)), self._ask_user),
            receipt_tool(self),
        ]
        if self.profile == "coordinator":
            return common + [
                tool("run_here", "For a single business item, continue executing the pinned package in "
                     "this same context. Preserve the original goal, prepared facts and delivery destination; "
                     "do not call start_tasks too. This changes roles, not package version.",
                     schema({}), self._run_here),
                tool("start_tasks", "Start one concurrent run task per business item. Each item: label, "
                     "request (complete goal for that item, including materials and @Fleet if any).",
                     schema({"items": A(schema({"label": S, "request": S}, ("label", "request")))}, ("items",)),
                     self._start_tasks),
                tool("wait_tasks", "Wait until tasks finish or timeout_seconds passes; returns conclusions.",
                     schema({"task_ids": A(), "timeout_seconds": I}), self._wait_tasks),
                tool("stop_tasks", "Stop tasks: stop dispatch, cancel their live WebCross executions, keep evidence.",
                     schema({"task_ids": A(), "reason": S}, ("task_ids", "reason")), self._stop_tasks),
                tool("finish", "Finish with each task's conclusion and next step. A question enters HITL "
                     "and continues this context after the answer; it does not finish the task.",
                     schema({"summary": S, "question": S,
                             "conclusion": {"type": "string", "enum": sorted(acceptance.CONCLUSIONS)}},
                            ("summary", "conclusion")), self._finish_coordinator),
            ]
        evidence = {"anyOf": [S, schema({"ref": S, "pointer": S}, ("ref",))]}
        binding_schema = schema({"path": S, "ref": S, "pointer": S,
            "offset": I, "limit": I}, ("path", "ref", "pointer"))
        input_props = {"input_bindings": A(binding_schema), "input": O, "input_ref": schema({"ref": S, "sha256": S}, ("ref", "sha256")),
                       "input_overrides": O,
                       "input_patch": A(schema({"op": {"type": "string", "enum": ["add", "replace", "remove"]},
                                                "path": S, "value": {}}, ("op", "path")))}
        execute_props = {**input_props, "reason": S}
        if self.profile == "author":
            execute_props["experiment"] = schema({
                "hypothesis": S, "baseline_ref": S, "evidence_refs": A(evidence),
                "previous_assessment": schema({
                    "judgment": {"type": "string", "enum": ["supports_hypothesis", "refutes_hypothesis", "inconclusive"]},
                    "evidence_refs": A(evidence), "rationale": S, "next_action": S},
                    ("judgment", "evidence_refs", "rationale", "next_action")),
                "checks": A(schema({"pointer": S, "expected": S}, ("pointer", "expected")))},
                ("hypothesis", "evidence_refs", "checks"))
        page_tools = [
            tool("list_pages", "Discover live Fleet/page IDs with URL, title and opener before binding. "
                 "Uses fleetId, existing task grants or the user's @Fleet; without a selection lists Fleets. "
                 "An empty task grant list does not prevent discovery.",
                 schema({"fleetId": S}), self._list_pages),
            tool("observe_page", "Read-only observation of the bound page or a listed page in an authorized "
                 "Fleet. method: Page.getState, DOM.getAXTree, DOM.getDetails, DOM.inspectSelect, "
                 "Page.screenshot, Network.readApi, Network.readBody, Download.list, Fleet.status.",
                 schema({"method": S, "params": O, "purpose": S}, ("method",)), self._observe_page),
            tool("read_artifact", "Read an observation artifact (AX text, screenshot) returned by this task.",
                 schema({"path": S, "offset": I, "max_chars": I}, ("path",)), self._read_artifact),
            tool("create_page", "Create a page after confirming the Fleet exists and registering task access "
                 "from the user's @Fleet or the existing approval policy. Does not bind the page.",
                 schema({"fleetId": S, "url": S}, ("fleetId", "url")), self._create_page),
            tool("bind_page", "Confirm the Fleet exists, register task access from the user's @Fleet or "
                 "the existing approval policy, then bind a live page. reason: start | transfer | user. transfer needs the "
                 "receipt that reported the new page; the Harness checks opener/Fleet before binding.",
                 schema({"pageId": S, "fleetId": S, "reason": {"type": "string", "enum": ["start", "transfer", "user"]},
                         "evidence_ref": S, "note": S}, ("pageId", "fleetId", "reason")), self._bind_page),
            tool("describe_action", "Read the live schema of one WebCross Action, for example after invalid_params.",
                 schema({"method": S}, ("method",)), self._describe),
            tool("validate_input", "Batch local preflight without browser actions: params.schema.json, "
                 "explicit input-file facts and the package's optional pure preflight checker. Supply "
                 "files together, each {path,kind:file|image,role?,source?}; role/source are declarations. Never list output destinations. Image "
                 "metadata reads common headers locally; unavailable reads stay unconfirmed. "
                 "Returns a saved receipt with all results; no new access is granted. "
                 "Provide input OR a returned input_ref. Optional input_overrides replaces complete "
                 "top-level fields (null is a value). Supply files OR returned files_ref to reuse explicit "
                 "input roles; every call rechecks permission and current file facts. Input references "
                 "alone do not include a file manifest. files_ref is accepted by prepare_materials or validate_input, "
                 "never execute_workflow. Instead of files/files_ref, files_manifest reads an explicit "
                 "authorized portable manifest {schemaVersion:1,files:[...]}; no roles are inferred. "
                 "Optional manifest_output saves that portable JSON using the existing local write policy; "
                 "export failure is reported separately from preflight.",
                 schema({**input_props, "files": A(schema({"path": S, "kind": {
                     "type": "string", "enum": ["file", "image"]}, "role": S, "source": S}, ("path",))),
                     "files_ref": schema({"ref": S, "sha256": S}, ("ref", "sha256")),
                     "files_manifest": schema({"path": S, "sha256": S}, ("path",)),
                     "manifest_output": schema({"path": S, "overwrite": B}, ("path",)),
                     "acceptance_points": A(schema({"id": S, "requirement": S, "source": S}, ("id", "requirement"))),
                     "reviewed_user_input_ids": A()}),
                 self._validate_input),
            tool("execute_workflow", "Run the package on the bound page with complete business input. Use "
                 "input.stages for the affected range when the contract declares stages. Returns facts and a "
                 "receiptRef. Audit the available facts; read exact receipt sections only for gaps. "
                 "Provide input OR a returned input_ref; optional input_overrides replaces complete "
                 "top-level fields. Accepts no files_ref/files_manifest/manifest_output; those belong to "
                 "validate_input. Reuse does not reuse file permissions or page evidence."
                 + (" For a repair, attach experiment: hypothesis, owned evidence_refs, optional baseline_ref "
                    "(default previous execution), checks [{pointer relative to output, expected observation}]. "
                    "When continuing a repair, include previous_assessment with your judgment, evidence_refs, "
                    "rationale and next_action. This is your assessment, not a Harness verification. "
                    "Returns version/input/result differences without judging success."
                    if self.profile == "author" else ""),
                 schema(execute_props, ("reason",)), self._execute_workflow),
            tool("read_execution_result", "Read an existing execution without replay. Prefer a complete receipt "
                 "already saved by this task; otherwise use platform retrieval only when supported. "
                 "Older runtimes cannot reconstruct a lost result from getStatus. "
                 "Returns evidence pointers and result source.",
                 schema({"workflow_id": S}, ("workflow_id",)), self._read_execution_result),
            tool("workflow_status", "Workflow.getStatus for an execution this task started.",
                 schema({"workflowId": S}, ("workflowId",)), self._workflow_status),
            tool("set_acceptance_points", "Record acceptance points derived from the original goal, "
                 "materials and effective input, before judging results. Replaces the current set with "
                 "a recorded revision. Review later user inputs explicitly; unchanged points are allowed.",
                 schema({"points": A(schema({"id": S, "requirement": S, "source": S}, ("id", "requirement"))),
                         "reviewed_user_input_ids": A(), "reason": S},
                        ("points",)), self._set_acceptance_points),
            tool("request_hitl", "Pause automation on the bound page for the user (login, captcha, "
                 "verification) and wait until they finish.",
                 schema({"reason": S}, ("reason",)), self._request_hitl),
            tool("delegate_browser",
                 ("Delegate a focused read-only observation on the bound page. Reuse evidence_refs. "
                  "The observer cannot navigate, perform business actions, edit packages or complete remaining work. "
                  "Prefer observe_page directly for a known missing fact; delegate only when useful interpretation is needed."
                  if self.profile == "run" else
                  "Delegate bounded Browser exploration on the bound page while authoring. "
                  "Pass targeted evidence_refs to reuse prior observations. Returns parent-readable "
                  "exports with exact evidence pointers; use read_receipt, not child filesystem paths. "
                  "Use observed transitions and final page state to improve and verify the draft."),
                 schema({"goal": S, "reason": S, "mode": {"type": "string", "enum": ["observation"]},
                         "evidence_refs": A(
                     {"anyOf": [S, schema({"ref": S, "pointer": S}, ("ref",))]})},
                        ("goal", "reason", "evidence_refs") if self.profile == "run" else ("goal", "reason")),
                 self._delegate_browser),
        ]
        if self.profile in {"run", "author"}:
            page_tools.append(tool("execute_workflow_batch",
                "Execute explicit independent inputs with bounded workers and no extra model agents. "
                "Provide a common input/input_ref and per-item overrides or input_bindings from owned receipt "
                "pointers (optional array offset/limit). Each item names an already authorized live Fleet/page. "
                "Reuse pages across queued items; different pages permit overlap. resource_keys explicitly "
                "serialize shared destinations/resources; declarations do not prove business independence. "
                "Every item retains its input and result receipt. Unknown effects are never replayed automatically.",
                schema({**input_props, "reason": S, "concurrency": I,
                    "items": A(schema({"id": S, "pageId": S, "fleetId": S,
                        "input_overrides": O, "input_bindings": A(binding_schema),
                        "resource_keys": A(), **({"experiment": O} if self.profile=="author" else {})},
                        ("id", "pageId", "fleetId")))}, ("items", "reason")), self._execute_workflow_batch))
        evidence = {"anyOf": [S, schema({"ref": S, "pointer": S}, ("ref",))]}
        point_result = schema({"id": S, "status": {"type": "string", "enum": sorted(acceptance.POINT_STATUSES)},
                               "evidenceRefs": A(evidence), "userInputIds": A(), "note": S}, ("id", "status"))
        finish_props = {"summary": S, "conclusion": {"type": "string", "enum": sorted(acceptance.CONCLUSIONS)},
                        "points": A(point_result), "remaining_work": A(), "template_gaps": A(),
                        "question": S, "skipConsentInputIds": A()}
        if self.profile == "run":
            batch_tools = [] if self.batch_id else [
                tool("start_tasks", "Split into independent business items only when needed. "
                     "Each child receives this pinned package and owns its own page and preparation. "
                     "Single-item requests continue here without dispatch.",
                     schema({"items": A(schema({"label": S, "request": S}, ("label", "request")))}, ("items",)),
                     self._start_tasks)]
            return common + page_tools + batch_tools + [
                tool("report_defect", "Record an evidenced issue and your attribution, not a proven root cause. "
                     "Distinguish observed facts from hypotheses; unknown is valid. This does not edit the package.",
                     schema({"summary": S, "evidence_refs": A(), "stage": S,
                         "kind": {"type": "string", "enum": ["package", "platform", "business", "unknown"]}},
                                            ("summary", "evidence_refs")), self._report_defect),
                tool("finish", "Finish with per-point results and evidence references. A question enters "
                     "HITL and continues this context after the answer. On rejection, reuse judgment_ref "
                     "and send only corrected point records; all points and evidence are checked again. "
                     "Use returned shortRef values instead of constructing receipt URIs.",
                     schema({**finish_props, "judgment_ref": S}), self._finish_run),
            ]
        return common + page_tools + [
            tool("list_files", "List draft files with sizes and sha256.", schema({}), self._list_files),
            tool("write_file", "Create or replace a draft file. expected_sha256 must match the current file "
                 "(omit for a new file).", schema({"path": S, "content": S, "expected_sha256": S},
                                                  ("path", "content")), self._write_file),
            tool("edit_file", "Replace unique text in a draft file.",
                 schema({"path": S, "expected_sha256": S, "edits": A(schema({"old_text": S, "new_text": S},
                                                                           ("old_text", "new_text")))},
                        ("path", "expected_sha256", "edits")), self._edit_file),
            tool("check_package", "Structure check plus installed WebCross Workshop validation and compile.",
                 schema({}), self._check_package),
            tool("run_offline_tests", offline_tests_usage(),
                 schema({}), self._run_offline_tests),
            tool("read_workflow_guide", "Read the live WebCross Workflow guide.", schema({}), self._guide),
            tool("save_draft", "Save the draft. If question is supplied, checkpoint and wait through HITL, "
                 "then continue in this task with the user's answer. Without a question, finish the authoring "
                 "task only when ready to hand off. State verified scope with "
                 "evidence, untested paths and known limitations. Summarizes coverage.json separately from "
                 "the sample's result; unknown or incomplete coverage may be saved without claiming support. "
                 "Reconcile original goal and later inputs with current criteria, evidence, omissions and remaining work. "
                 "On rejection reuse judgment_ref and send only corrected points or changed fields.",
                 schema({**finish_props,
                         "judgment_ref": S,
                         "conclusion": {"type": "string", "enum": sorted(acceptance.AUTHOR_CONCLUSIONS)},
                         "verified_scope": A(), "known_limitations": A()}), self._save_draft),
        ]

    # ----------------------------------------------------- package tools
    def _coverage_report(self) -> dict[str, Any]:
        try:
            value = coverage.read(self.package_dir())
        except (OSError, ValueError) as exc:
            return {"status": "invalid", "path": coverage.FILE, "error": str(exc)}
        report = coverage.summarize(value)
        if value is not None:
            report["references"] = coverage.reference_facts(value, task_id=self.logger.task_id,
                known_refs=self.known_refs(), read_receipt=self.store.read_ref,
                user_input_ids={i["inputId"] for i in self.all_inputs()
                                if i.get("inputId") and i.get("source") in acceptance.USER_SOURCES})
        return report

    def _contract_snapshot(self, *, include_inventory=False):
        root = self.package_dir()
        digest = pkg.content_hash(root)
        previous = self.state.get("packageContract") or {}
        if previous.get("contentHash") == digest and previous.get("receiptRef") in self.known_refs():
            value = self.store.read_ref(previous["receiptRef"])
            ref = previous["receiptRef"]
        else:
            value = {name: pkg.read_json(root / name) for name in
                ("workflow.json", "contract.json", "params.schema.json") if (root / name).is_file()}
            value["contentHash"] = digest
            ref = self.store.receipt(self.reload(), "workflow.contract.snapshot", value)
            self._receipt_added(ref, "workflow.contract.snapshot")
            self.state["packageContract"] = {"contentHash": digest, "receiptRef": ref}
            self.save()
        view = {"receiptRef": ref, "contentHash": digest,
            "workflow": value.get("workflow.json"), "contract": compact(value.get("contract.json"), 16000),
            "paramsSchema": compact(value.get("params.schema.json"), 10000),
            "evidenceLocations": {name: {"ref": ref, "pointer": "/" + name} for name in value if name.endswith(".json")},
            "interpretation": "Version-bound contract claims, not live page facts or permissions. Read exact saved schema sections if the first view is reduced."}
        if include_inventory:
            view["files"] = pkg.file_inventory(root)
        return view

    async def _read_contract(self, args):
        view = self._contract_snapshot(include_inventory=args.get("include_inventory", False))
        view["coverage"] = self._coverage_report()
        view["goalReview"] = acceptance.review_facts(self.state, self.all_inputs())
        history=self.package_dir()/'history'
        if history.is_dir():
            view['versionHistory']={'changesPath':'history/changes.jsonl',
                                    'head':pkg.read_json(history/'head.json') if (history/'head.json').is_file() else None,
                                    'diffPattern':'history/<contentHash>.diff',
                                    'note':'Use read_package_file for local version changes. Historical declarations are not fresh verification.'}
        return result(view)

    async def _read_package_file(self, args):
        root = self.package_dir()
        path = pkg.safe_relative(root, args["path"])
        if not path.is_file():
            raise FileNotFoundError(args["path"])
        text = path.read_text(encoding="utf-8", errors="replace")
        offset = max(0, int(args.get("offset") or 0))
        size = max(1000, min(int(args.get("max_chars") or 20000), 60000))
        return result({"path": args["path"], "sha256": sha256_text(text), "totalChars": len(text),
                       "offset": offset, "text": text[offset:offset + size],
                       "nextOffset": offset + size if offset + size < len(text) else None})

    async def _list_files(self, args):
        return result({"files": pkg.file_inventory(self.package_dir())})

    async def _write_file(self, args):
        if self.profile != "author":
            raise PermissionError("Only workflow-create/edit may modify a package")
        root = self.package_dir()
        path = pkg.safe_relative(root, args["path"], writable=True)
        if path.exists():
            current = sha256_text(path.read_text(encoding="utf-8"))
            if args.get("expected_sha256") != current:
                return result({"status": "conflict", "path": args['path'], "sha256": current,
                               "next": "Read the current file with read_package_file before revising; do not overwrite unseen changes."}, error=True)
        elif args.get("expected_sha256"):
            raise ValueError("File does not exist; omit expected_sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        self.logger.write("workflow.draft.write", {"path": args["path"], "sha256": sha256_text(args["content"])})
        return result({"path": args["path"], "sha256": sha256_text(args["content"]),
                       "draftHash": pkg.content_hash(root)})

    async def _edit_file(self, args):
        if self.profile != "author":
            raise PermissionError("Only workflow-create/edit may modify a package")
        root = self.package_dir()
        path = pkg.safe_relative(root, args["path"], writable=True)
        text = path.read_text(encoding="utf-8")
        if sha256_text(text) != args["expected_sha256"]:
            return result({"status": "conflict", "path": args['path'], "sha256": sha256_text(text),
                           "next": "Read the current file before rebasing these edits."}, error=True)
        for index, edit in enumerate(args["edits"]):
            if not edit["old_text"] or text.count(edit["old_text"]) != 1:
                return result({"status": "rejected", "path": args['path'],
                               "sha256": sha256_text(path.read_text(encoding='utf-8')),
                               "matchCount": text.count(edit['old_text']) if edit['old_text'] else None,
                               "editIndex": index,
                               "error": "Each old_text must be nonempty and occur exactly once; no edits were written."}, error=True)
            text = text.replace(edit["old_text"], edit["new_text"], 1)
        path.write_text(text, encoding="utf-8")
        self.logger.write("workflow.draft.edit", {"path": args["path"], "sha256": sha256_text(text)})
        return result({"path": args["path"], "sha256": sha256_text(text), "draftHash": pkg.content_hash(root)})

    async def _check_package(self, args):
        value = await asyncio.to_thread(pkg.check, self.package_dir())
        value["coverage"] = self._coverage_report()
        ref = self.store.receipt(self.reload(), "workflow.package.check", value)
        return result({**compact(value, 12000), "receiptRef": ref}, error=value["status"] != "valid")

    async def _run_offline_tests(self, args):
        from harness.workflow_mode import offline_tests
        root = self.package_dir()
        value = await asyncio.to_thread(offline_tests.run, root, content_hash=pkg.content_hash(root))
        ref = self.store.receipt(self.reload(), "workflow.offline_tests", value)
        return result({**offline_tests.summary(value, ref), "receiptRef": ref},
                      error=value["status"] not in {"passed", "absent"})

    async def _guide(self, args):
        return result(await self.platform.guide())

    async def _describe(self, args):
        value = await self.platform.describe(args["method"])
        data = value.get("data")
        if value.get("ok") and isinstance(data, dict) and "inputSchema" in data:
            return result({"method": args["method"], "description": data.get("description"),
                           "inputSchema": compact(data["inputSchema"], 20000),
                           "receiptRef": value.get("receiptRef"),
                           "evidenceLocations": value.get("evidenceLocations", {}),
                           "next": "Full input/output schemas remain in the saved receipt."})
        return result(observation_view(value, 20000), error=not value.get("ok"))

    # -------------------------------------------------------- page tools
    async def _list_pages(self, args):
        fleets = [args["fleetId"]] if args.get("fleetId") else list(self.state.get("authorizedFleets") or [])
        reference = self.state.get("fleetReference")
        if not fleets and reference:
            fleets = [f for f in await self.known_fleets() if f.startswith(reference)]
        if not fleets:
            listed = await self.platform.call("Fleet.list", {}, allowed=OBSERVE_METHODS)
            if not listed.get("ok"):
                return result({**listed, "method": "Fleet.list", "inventoryStatus": "unconfirmed",
                               "fleetScope": self.fleet_scope()}, error=True)
            return result({"fleets": compact(listed.get("data"), 8000), "receiptRef": listed.get("receiptRef"),
                           "fleetScope": self.fleet_scope(),
                           "note": "No Fleet has been confirmed in this task. Inspect the returned candidates; "
                                   "create_page or bind_page verifies the Fleet and handles task registration. "
                                   "WebCross access is evidenced by individual Action receipts."}, error=not listed.get("ok"))
        pages = {}
        for fleet in fleets:
            listed = await self.platform.call("Page.list", {"fleetId": fleet}, allowed=OBSERVE_METHODS)
            data = listed.get("data")
            failure = None if listed.get("ok") else listed
            if failure is None and not isinstance(data, list):
                failure = {"status": "invalid_inventory", "method": "Page.list",
                           "receiptRef": listed.get("receiptRef"),
                           "error": "Page.list did not return an inventory; page membership is unconfirmed."}
            pages[fleet] = {"pages": data if failure is None else None,
                            "receiptRef": listed.get("receiptRef"), "error": failure}
        self.state.setdefault("listedPages", {}).update(
            {p.get("pageId"): fleet for fleet, item in pages.items() for p in (item["pages"] or [])
             if isinstance(p, dict) and p.get("pageId")})
        self.save()
        view = compact(pages, 12000)
        failures = [{"fleetId": fleet, "method": "Page.list", **item["error"]}
                    for fleet, item in pages.items() if item["error"] is not None]
        return result({"fleets": view, "failures": failures}, error=True) if failures else result(view)

    def _page_allowed(self, page_id: str) -> bool:
        binding = self.state.get("binding") or {}
        fleet = (self.state.get("listedPages") or {}).get(page_id)
        return page_id == binding.get("pageId") or fleet in (self.state.get("authorizedFleets") or [])

    async def _observe_page(self, args):
        method = args["method"]
        if method not in OBSERVE_METHODS - {"Page.list", "Fleet.list"}:
            raise ValueError("observe_page accepts read-only page methods; use list_pages for inventories")
        params = dict(args.get("params") or {})
        binding = self.state.get("binding") or {}
        if self.profile == "observer":
            if params.get("pageId", binding.get("pageId")) != binding.get("pageId"):
                raise PermissionError("The observer is bound to one page")
            if params.get("fleetId", binding.get("fleetId")) != binding.get("fleetId"):
                raise PermissionError("The observer is bound to one Fleet")
        if method == "Fleet.status":
            params.setdefault("fleetId", binding.get("fleetId"))
            if params["fleetId"] not in (self.state.get("authorizedFleets") or []):
                raise PermissionError("Fleet is not authorized for this task")
        else:
            params.setdefault("pageId", binding.get("pageId"))
            if not params.get("pageId") or not self._page_allowed(params["pageId"]):
                raise PermissionError("Observe the bound page or a page listed in an authorized Fleet")
        value = await self.platform.call(method, params, allowed=OBSERVE_METHODS, purpose=args.get("purpose"))
        images = []
        data = value.get("data")
        if method == "Page.screenshot" and isinstance(data, dict) and data.get("path"):
            try:
                images.append(image_content(Path(data["path"])))
            except (OSError, ValueError) as exc:
                value["imageError"] = str(exc)
        return result(observation_view(value), error=not value.get("ok")
                      or bool(isinstance(data, dict) and data.get("observationError")), images=images)

    async def _read_artifact(self, args):
        value = self.platform.read_artifact(args["path"], offset=args.get("offset", 0),
                                            max_chars=args.get("max_chars", 20000))
        if value.get("image"):
            return result(value, images=[image_content(Path(args["path"]))])
        return result(value, error=not value.get("ok"))

    async def _create_page(self, args):
        if not await self.authorize_fleet(args["fleetId"]):
            raise PermissionError("Fleet is not authorized for this task")
        value = await self.platform.call("Page.create", {"fleetId": args["fleetId"], "url": args["url"]},
                                         allowed=CONTROL_METHODS)
        page = (value.get("data") or {}).get("pageId") if isinstance(value.get("data"), dict) else None
        if page:
            self.state.setdefault("listedPages", {})[page] = args["fleetId"]
            self.state.setdefault("createdPages", []).append(page)
            self.save()
        return result(compact(value, 6000), error=not value.get("ok"))

    async def _bind_page(self, args):
        page_id, fleet_id, reason = args["pageId"], args["fleetId"], args["reason"]
        if not await self.authorize_fleet(fleet_id):
            raise PermissionError("Fleet is not authorized for this task; ask the user for an @Fleet")
        listed = await self.platform.call("Page.list", {"fleetId": fleet_id}, allowed=OBSERVE_METHODS)
        if not listed.get("ok"):
            return result({**listed, "method": "Page.list", "inventoryStatus": "unconfirmed"}, error=True)
        if not isinstance(listed.get("data"), list):
            return result({"status": "invalid_inventory", "method": "Page.list",
                           "receiptRef": listed.get("receiptRef"),
                           "error": "Page.list did not return an inventory; page membership is unconfirmed."}, error=True)
        pages = {p.get("pageId"): p for p in listed["data"] if isinstance(p, dict)}
        page = pages.get(page_id)
        if page is None:
            raise ValueError(f"Page {page_id} is not open in Fleet {fleet_id} (see {listed.get('receiptRef')})")
        current = self.state.get("binding") or {}
        if reason == "transfer":
            if not current:
                raise ValueError("transfer requires an existing binding; use reason=start")
            opener = page.get("openerPageId") or page.get("openedBy")
            evidence = args.get("evidence_ref")
            if evidence:
                evidence = resolve_reference(evidence, self.known_refs())
                args = {**args, "evidence_ref": evidence}
            if evidence and evidence not in self.known_refs():
                raise ValueError("evidence_ref is not a receipt of this task")
            if opener != current.get("pageId") and not evidence:
                raise ValueError("The new page's opener is not the bound page; cite the receipt that reports it")
            if fleet_id != current.get("fleetId"):
                raise PermissionError("A transfer stays inside the bound Fleet")
        owner = self.runs.claim_page(self.logger.task_id, page_id)
        if owner:
            raise PermissionError(f"Page {page_id} is bound by live task {owner}")
        binding = {"pageId": page_id, "fleetId": fleet_id, "reason": reason, "url": page.get("url") or
                   page.get("currentUrl"), "evidenceRef": args.get("evidence_ref"), "note": args.get("note"),
                   "listReceipt": listed.get("receiptRef")}
        self.state["binding"] = binding
        self.state.setdefault("pageHistory", []).append(binding)
        self.state.setdefault("listedPages", {})[page_id] = fleet_id
        self.save()
        self.logger.write("workflow.page.bound", binding)
        return result({"binding": binding})

    # ---------------------------------------------------------- execution
    def _pinned(self) -> tuple[str, Path]:
        if self.profile == "author":
            digest, path = self.registry.snapshot_directory(self.package_dir(), Path(self.logger.task_dir))
            versions = self.state.setdefault("versions", [])
            if digest not in versions:
                versions.append(digest)
            return digest, path
        path = self.package_dir()
        digest = pkg.content_hash(path)
        if digest != (self.state.get("package") or {}).get("contentHash"):
            raise RegistryError("Pinned version snapshot changed on disk; refusing to execute")
        return digest, path

    def _input_files(self, business_input: Any) -> tuple[list[str], list[str]]:
        from harness.workflow.script_runtime import input_files
        allowed = input_files(self, business_input)
        missing = sorted(s for s in _strings(business_input)
                         if s.startswith("/") and Path(s).is_file() and s not in allowed)
        return allowed, missing

    async def _validate_input(self, args):
        if "acceptance_points" in args:
            await self._set_acceptance_points({"points": args["acceptance_points"],
                "reviewed_user_input_ids": args.get("reviewed_user_input_ids", []),
                "reason": "Goal criteria recorded with the same input preparation"})
        from harness.workflow_mode import preflight
        business_input = resolve_input(args, known_refs=self.known_refs(), read=self.store.read_ref)
        root = self.package_dir()
        problems = []
        schema_path = root / "params.schema.json"
        if schema_path.is_file():
            from harness.tools.argument_pipeline import validate_schema
            issues = validate_schema(business_input, pkg.read_json(schema_path), max_issues=1000)
            problems += [i.as_dict() for i in issues]
        allowed, missing = self._input_files(business_input)
        from harness.workflow_mode import materials
        prepared = await materials.inspect(self, args)
        manifest, facts = prepared["inputFiles"], prepared["files"]
        changes, portable = prepared["fileFactChanges"], prepared["materialManifest"]
        manifest_source = prepared["manifestImport"]
        absent = [f["path"] for f in facts if f["status"] == "missing"]
        file_issues = [f for f in facts if f["status"] != "observed"
                       or (f.get("image") or {}).get("status") == "unconfirmed"]
        checks = await asyncio.to_thread(preflight.run, root, business_input, facts)
        status = ("problems" if problems or missing or any(f["status"] in {"missing", "not_file"} for f in facts)
                  or checks["status"] == "issues"
                  else "unconfirmed" if file_issues or checks["status"] == "unconfirmed" else "valid")
        value = {"status": status, "contentHash": pkg.content_hash(root),
                 "input": business_input, "inputFiles": manifest,
                 "materialManifest": portable, "manifestImport": manifest_source,
                 "fileFactChanges": changes,
                 "materialManifestStatus": "available" if portable is not None else "unconfirmed",
                 "manifestPortabilityNote": (None if portable is not None else
                     "A relative declared path lacks an authorized canonical path; no portable manifest is available"),
                 "declarationScope": "role/source are caller declarations, not verified semantic identity or access grants; source files must be listed separately to inspect them",
                 "inputHash": sha256_text(json.dumps(business_input, sort_keys=True, ensure_ascii=False)),
                 "schemaProblems": problems, "schemaIssuesMayBeTruncated": len(problems) == 1000,
                 "unauthorizedFiles": missing, "missingFiles": absent,
                 "authorizedFiles": len(allowed), "files": facts, "preflight": checks,
                 "note": "Local observations only; dynamic page requirements are unchecked here. "
                         "No input is changed. Judge applicability against the original goal; "
                         "authorize missing access or collect unknown facts before deciding."}
        export = (await materials.export_manifest(self, portable, args["manifest_output"])
                  if "manifest_output" in args else None)

        value["manifestExport"] = export
        ref = self.store.receipt(self.reload(), "workflow.input.validated", value)
        registration = materials.register_and_save(self, value, ref)
        return result({"status": status, "contentHash": value["contentHash"], "inputHash": value["inputHash"],
                       "input_ref": reference_for(value, ref),
                       "acceptanceRevision": self.state.get("acceptanceRevision"),
                       "files_ref": manifest_reference(manifest, ref),
                       **({"materialRegistration": registration} if not registration["updated"] else {}),
                       "referenceUsage": {"input_ref": ["validate_input", "execute_workflow"],
                                          "files_ref": ["prepare_materials", "validate_input"]},
                       "manifestImport": manifest_source, "manifestExport": export,
                       "materialManifestStatus": value["materialManifestStatus"],
                       "manifestPortabilityNote": value["manifestPortabilityNote"],
                       "next": "To execute, pass input_ref and your reason to execute_workflow. "
                               "For another validation, pass input_ref and files_ref to validate_input. "
                               "files_ref is not an execute_workflow argument.",
                       "schemaProblemCount": len(problems),
                       "schemaProblems": compact(problems, 6000),
                       "schemaIssuesMayBeTruncated": value["schemaIssuesMayBeTruncated"],
                       "unauthorizedFiles": missing, "missingFiles": absent, "authorizedFiles": len(allowed),
                       "fileCount": len(facts), "fileIssueCount": len(file_issues),
                       "fileFactChanges": {**{key: item for key, item in changes.items() if key != "items"},
                                           "items": compact(changes["items"][:10], 4000),
                                           "hasMore": len(changes["items"]) > 10,
                                           "evidence": {"ref": ref, "pointer": "/fileFactChanges"}},
                       "fileIssues": compact(file_issues, 6000),
                       "preflightStatus": checks["status"], "preflightCounts": checks.get("counts"),
                       "preflight": preflight_view(checks, ref), "receiptRef": ref,
                       "evidenceLocations": {name: {"ref": ref, "pointer": path} for name, path in
                           (("files", "/files"), ("checks", "/preflight/items"),
                            ("input", "/input"), ("schema", "/schemaProblems"),
                            ("materialManifest", "/materialManifest"), ("inputFiles", "/inputFiles"),
                            ("fileFactChanges", "/fileFactChanges"))}, "note": value["note"]},
                      error=status != "valid")

    async def _execute_workflow(self, args, *, _prepared=None, _binding=None, _platform=None, _on_started=None):
        from harness.tools.path_authorization import write_policy
        self._check_stop()
        binding = _binding or self.state.get("binding")
        platform = _platform or self.platform
        if not binding:
            raise ValueError("Bind a page first (bind_page)")
        experiment = None
        baseline = None
        if self.profile == "author":
            experiment = authoring.prepare_experiment(args.get("experiment"), known_refs=self.known_refs(),
                executions=self.state.get("executions", []), read=self.store.read_ref)
            if experiment.get("baseline_ref"):
                baseline = self.store.read_ref(experiment["baseline_ref"])
        if _prepared is None:
            digest, root = self._pinned()
            document = pkg.execution_document(root)
            business_input = resolve_input(args, known_refs=self.known_refs(), read=self.store.read_ref)
            files, missing = self._input_files(business_input)
            if missing:
                return result({"status": "rejected", "unauthorizedFiles": missing[:50],
                               "next": "Authorize these files or their directory with local_fs_authorize."}, error=True)
        else:
            digest, root, document, business_input, files = _prepared

        def started(workflow_id: str) -> None:
            self.runs.add_workflow(self.logger.task_id, workflow_id)
            self._execution_platforms[workflow_id] = platform
            if _on_started:
                _on_started(workflow_id)
        task = asyncio.create_task(platform.execute(document, business_input, page_id=binding["pageId"],
            fleet_id=binding["fleetId"], files=files, content_hash=digest, on_started=started,
            write_policy=write_policy(self), stop_check=self._stop_flag))
        stopped = None
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=2)
                if not task.done() and self._stop_flag():
                    if stopped is None:
                        stopped = {}
                    for workflow_id, record in list(platform.live.items()):
                        if record.get("status") not in TERMINAL and workflow_id not in stopped:
                            stopped[workflow_id] = await platform.cancel(workflow_id, reason="User /workflow-stop")
        except asyncio.CancelledError:
            try:
                await platform.close()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        value = task.result()
        receipt = self.store.read_ref(value["receiptRef"]) if value.get("receiptRef") else {}
        facts = acceptance.execution_facts(receipt, content_hash=digest, page_id=binding["pageId"]) if receipt else {}
        summary = acceptance.output_summary(receipt) if receipt else None
        from harness.workflow_mode.protocol import report_view, report_facts
        integrity = report_facts(((receipt.get("response") or {}).get("data") or {}).get("output"))
        parts = report_view(receipt, value.get("receiptRef"), pkg.load_contract(root)) if receipt else []
        stages = None
        if isinstance(business_input, dict):
            stages = business_input.get("stages") or business_input.get("stage")
        record = {"receiptRef": value.get("receiptRef"), "status": value.get("status"),
                  "platformAgentId": platform.agent_id,
                  "workflowId": value.get("workflowId"), "outcome": facts.get("outcome"), "contentHash": digest,
                  "stages": stages, "pageId": binding["pageId"], "reason": args["reason"],
                  "identityIssues": facts.get("identityIssues"), "stopped": bool(stopped),
                  "summary": {k: summary.get(k) for k in ("outcome", "stages", "skipped")} if summary else None}
        self.reload().setdefault("executions", []).append(record)
        self.save()
        experiment_view = None
        if experiment is not None and value.get('receiptRef'):
            source_changes = None
            baseline_hash = baseline.get("contentHash") if baseline else None
            # Resolve only a hash already bound to this task's immutable snapshot.
            if baseline_hash and baseline_hash in {e.get("contentHash") for e in self.state["executions"]}:
                old_root = Path(self.logger.task_dir) / "workflow" / "versions" / baseline_hash
                if old_root.is_dir():
                    inventories = [{item["path"]: item["sha256"] for item in pkg.file_inventory(p)}
                                   for p in (old_root, root)]
                    source_changes = authoring.changes(*inventories)
            compared = authoring.comparison(experiment, current=receipt,
                current_ref=value["receiptRef"], baseline=baseline, source_changes=source_changes)
            experiment_ref = self.store.receipt(self.reload(), "workflow.authoring.experiment", compared)
            self.state.setdefault("authorExperiments", []).append({
                "receiptRef": experiment_ref, "executionRef": value["receiptRef"],
                "baselineRef": experiment.get("baseline_ref"), "hypothesis": experiment.get("hypothesis"),
                "contentHash": digest, "platform": guidance.identity(self.platform.catalog),
                "executionMetrics": compared["executionMetrics"]["after"]})
            self.save()
            experiment_view = {"receiptRef": experiment_ref,
                "comparability": compared['comparability'],
                "changeCounts": {key: compared[key]["count"] if compared[key] is not None else None
                                 for key in ("sourceChanges", "inputChanges", "outputChanges")},
                "checkCount": len(compared["checks"]),
                "data": compact(compared, 6000),
                "evidenceLocations": {key: {"ref": experiment_ref, "pointer": "/" + key}
                                      for key in ("identity", "sourceChanges", "inputChanges", "outputChanges", "checks", "executionMetrics")}}
        output = value.get("output")
        if summary and isinstance(output, dict):
            # The summary is shown whole; the rest of the report stays in the receipt.
            output = {k: v for k, v in output.items() if k != "summary"}
        skips = acceptance.open_skips(self.state.get("executions", []))
        locations = {}
        for name, path in {"output": "/response/data/output", "calls": "/response/data/results",
                           "facts": "/facts", "failureGroups": "/facts/callFacts/failureGroups",
                           "input": "/input", "scope": "/scope"}.items():
            try:
                pointer(receipt, path)
            except ValueError:
                continue
            locations[name] = {"ref": value.get("receiptRef"), "pointer": path}
        view = {"status": value.get("status"), "workflowId": value.get("workflowId"),
                "receiptRef": value.get("receiptRef"), "contentHash": digest, "facts": facts,
                "input_ref": reference_for(receipt, value.get("receiptRef")),
                "failureEvidence": failure_evidence(receipt, value.get("receiptRef")),
                "evidenceLocations": locations,
                "platformError": value.get("error"), "timing": value.get("timing"),
                "resultDelivery": _result_delivery_summary(receipt),
                "summary": compact(summary, 12000) if summary else None,
                "summaryEvidenceScope": summary_evidence_scope(receipt, value.get("receiptRef")) if summary else None,
                "reportParts": parts, "reportIntegrity": integrity,
                "openSkips": len(skips) or None,
                "output": compact(output, 4000 if summary else 8000), "scope": value.get("scope"),
                "stopped": stopped or None,
                "next": "Audit returned facts against the goal. Resolve only material gaps/conflicts with "
                        "targeted evidence reads. Discovered pages need bind_page."
                        + (" Reconcile reported skips against the goal and later evidence; already-held values are not omissions."
                           if skips else "")}
        if experiment_view is not None:
            view["authorExperiment"] = experiment_view
        return result(view, error=not value.get("ok"))

    async def _execute_workflow_batch(self, args):
        from harness.workflow_mode.batch import execute_batch
        return await execute_batch(self, args)

    def _execution_record(self, workflow_id):
        for row in reversed(self.state.get('executions', [])):
            if row.get('workflowId') == workflow_id:
                return row
        for batch in reversed(self.state.get('executionBatches', [])):
            for row in batch['items']:
                if row.get('workflowId') == workflow_id:
                    return {**row, 'contentHash': batch['contentHash']}
        return None

    def _execution_platform(self, workflow_id):
        if workflow_id in self._execution_platforms:
            return self._execution_platforms[workflow_id]
        record = self._execution_record(workflow_id)
        if record is None and workflow_id not in self.platform.live:
            raise PermissionError('Workflow was not started by this task')
        identity = (record or {}).get('platformAgentId')
        if identity and identity != self.platform.agent_id and self.runtime.browser.transport == 'local':
            platform = WorkflowPlatform(self.runtime, self.logger, self.store, label=self.label, identity=identity)
            platform.on_receipt = self._receipt_added
            self._execution_platforms[workflow_id] = platform
            return platform
        return self.platform

    async def _read_execution_result(self, args):
        """Recover evidence for the existing execution; never create another run."""
        from harness.workflow_mode.protocol import report_view
        workflow_id = args["workflow_id"]
        contract = pkg.load_contract(self.package_dir())
        execution = self._execution_record(workflow_id)
        if execution is None and workflow_id not in self.platform.live:
            raise PermissionError("Workflow was not started by this task")
        if execution and execution.get("receiptRef"):
            local_ref = execution["receiptRef"]
            local = self.store.read_ref(local_ref)
            response = local.get("response") or {}
            data = response.get("data") if isinstance(response, dict) else None
            from harness.workflow.call_facts import call_facts
            calls = data.get("results") if isinstance(data, dict) else None
            complete = (isinstance(data, dict) and "output" in data and isinstance(calls, list)
                        and call_facts(data).get("valuesComplete") is True
                        and all(isinstance(call, dict) and type(call.get("callId")) is int
                                and call.get("status") in {"succeeded", "failed", "cancelled"}
                                for call in calls)
                        and len({call["callId"] for call in calls}) == len(calls))
            local_facts = acceptance.execution_facts(local, content_hash=execution.get("contentHash"),
                                                      page_id=execution.get("pageId"))
            if (complete and not local.get("error") and not local_facts["identityIssues"]
                    and local_facts.get("callMetadataComplete") is not False
                    and local_facts.get("callValuesComplete") is not False
                    and local.get("workflowId") == workflow_id == data.get("workflowId")
                    and local.get("status") == data.get("status")
                    and data.get("status") in {"succeeded", "failed", "cancelled"}):
                from harness.workflow_mode.evidence import evidence_locations
                return result({"status": data["status"], "workflowId": workflow_id,
                    "receiptRef": local_ref, "source": "local_receipt", "facts": local_facts,
                    "input_ref": reference_for(local, local_ref),
                    "reportParts": report_view(local, local_ref, contract),
                    "failureEvidence": failure_evidence(local, local_ref),
                    "summary": compact(acceptance.output_summary(local), 12000),
                    "summaryEvidenceScope": summary_evidence_scope(local, local_ref) if acceptance.output_summary(local) else None,
                    "output": compact(data["output"], 8000), "platformError": data.get("error"),
                    "evidenceLocations": evidence_locations(local_ref, local, {
                        "output": "/response/data/output", "calls": "/response/data/results",
                        "facts": "/facts", "failureGroups": "/facts/callFacts/failureGroups",
                        "input": "/input", "scope": "/scope"}),
                    "next": "Read the original saved execution; no platform request or business action was sent. "
                            "Historical evidence does not establish current page state."})
        value = await self._execution_platform(workflow_id).read_result(workflow_id)
        if not value.get("ok"):
            return result({**value, "savedReceiptRef": execution.get("receiptRef") if execution else None,
                "next": "No complete result was recovered. workflow_status can query lifecycle only; "
                        "use public page/resource observations for remaining facts. Missing output does not "
                        "prove that actions failed or did not occur; do not replay merely to obtain a receipt."}, error=True)
        recovered = self.store.read_ref(value["receiptRef"])
        original_ref = (execution.get("originalReceiptRef") or execution.get("receiptRef")) if execution else None
        original = self.store.read_ref(original_ref) if original_ref else {}
        merged = {**original, "method": "Workflow.execute", "workflowId": workflow_id,
            "status": value["status"], "response": recovered["response"], "facts": recovered["facts"],
            "error": None, "originalDeliveryError": original.get("error"),
            "originalReceiptRef": original_ref, "recoveryReceiptRef": value["receiptRef"],
            "resultReadDurationMs": recovered.get("durationMs"),
            "resultDelivery": recovered.get("resultDelivery"), "recoveredAt": _now()}
        facts = acceptance.execution_facts(merged,
            content_hash=execution.get("contentHash") if execution else None,
            page_id=execution.get("pageId") if execution else None)
        summary = acceptance.output_summary(merged)
        ref = self.store.receipt(self.reload(), "workflow.execute.result_recovered", merged)
        for row in self.state.get("executions", []):
            if row.get("workflowId") == workflow_id:
                row.update(receiptRef=ref, originalReceiptRef=original_ref, status=value["status"],
                    outcome=facts.get("outcome"), identityIssues=facts.get("identityIssues"),
                    summary={key: summary.get(key) for key in ("outcome", "stages", "skipped")} if summary else None)
        for batch in self.state.get('executionBatches', []):
            for row in batch['items']:
                if row.get('workflowId') == workflow_id:
                    row.update(receiptRef=ref, status=value['status'], facts=facts)
        self.save()
        from harness.workflow_mode.evidence import evidence_locations
        locations = evidence_locations(ref, merged, {"output": "/response/data/output",
            "calls": "/response/data/results", "facts": "/facts",
            "failureGroups": "/facts/callFacts/failureGroups", "input": "/input", "scope": "/scope",
            "originalDeliveryError": "/originalDeliveryError"})
        return result({"status": value["status"], "workflowId": workflow_id, "receiptRef": ref,
            "originalReceiptRef": original_ref, "recoveryReceiptRef": value["receiptRef"],
            "facts": facts, "evidenceLocations": locations, "platformError": value.get("error"),
            "source": "platform_retained_result", "input_ref": reference_for(merged, ref),
            "reportParts": report_view(merged, ref, contract),
            "failureEvidence": failure_evidence(merged, ref),
            "summary": compact(summary, 12000) if summary else None,
            "summaryEvidenceScope": summary_evidence_scope(merged, ref) if summary else None,
            "output": compact(value.get("output"), 8000), "timing": value.get("timing"),
            "resultDelivery": _result_delivery_summary(merged),
            "next": "Recovered the original execution; no business actions were repeated. "
                    "Judge the evidence against the original goal and reconcile current state before further actions."})

    async def _workflow_status(self, args):
        platform = self._execution_platform(args['workflowId'])
        return result(compact(await platform.status(args["workflowId"]), 12000))

    async def _read_receipt(self, args):
        from harness.workflow_mode.evidence import receipt_selection
        reference, selected, paths = receipt_selection(args, known_refs=self.known_refs())
        value = self.store.read_ref(reference)
        limit = max(2000, min(int(args.get("max_chars") or 20000), 60000))
        from harness.workflow_mode.evidence import read_evidence
        def read(path, budget):
            return {"ref": reference, "pointer": path,
                    **read_evidence(pointer(value, path), path, max_chars=budget,
                                    offset=args.get("offset"), limit=args.get("limit"),
                                    search=args.get("search"))}
        if paths is None:
            return result(read(selected, limit))
        items = []
        for path in paths:
            try:
                items.append(read(path, max(512, limit // len(paths))))
            except ValueError as exc:
                items.append({"ref": reference, "pointer": path, "error": str(exc)})
        failed = any("error" in item for item in items)
        return result({"status": "partial" if failed else "done", "results": items}, error=failed)

    async def _set_acceptance_points(self, args):
        points = acceptance.validate_points(args["points"])
        inputs = {i["inputId"] for i in self.all_inputs()
                  if i.get("inputId") and i.get("source") in acceptance.USER_SOURCES}
        reviewed = args.get("reviewed_user_input_ids") or []
        if not isinstance(reviewed, list) or any(not isinstance(i, str) or i not in inputs for i in reviewed):
            raise ValueError("reviewed_user_input_ids must name this task's user inputs")
        previous = self.state.get("acceptanceRevision") or {}
        if self.state.get("acceptancePoints"):
            self.state.setdefault("acceptanceHistory", []).append(self.state["acceptancePoints"])
        revision = {"number": previous.get("number", 0) + 1, "createdAt": _now(),
                    "reason": args.get("reason") or "Acceptance points recorded by the model",
                    "reviewedUserInputIds": sorted(set(previous.get("reviewedUserInputIds") or []) | set(reviewed)),
                    "points": points,
                    "changes": acceptance.review_changes(self.state.get('acceptancePoints') or [], points)}
        self.state["acceptancePoints"] = points
        self.state["acceptanceRevision"] = revision
        self.state.setdefault("acceptanceRevisions", []).append(revision)
        self.save()
        return result({"acceptancePoints": points, "acceptanceReview": self.acceptance_review(),
                       "goalReview": acceptance.review_facts(self.state, self.all_inputs())})

    def _begin_interaction(self, question: str, *, kind: str = "question", **details) -> None:
        self._check_stop()
        self.state.update(status="awaiting_user", pendingQuestion=question,
                          pendingInteraction={"kind": kind, **details})
        self.save()

    def _clear_interaction(self) -> None:
        self.state.pop("pendingQuestion", None)
        self.state.pop("pendingInteraction", None)
        self.state.pop("stopReason", None)
        self.state["status"] = "active"

    def _suspend_for_user(self, question: str, *, checkpoint=None):
        # No live input channel: persist an unfinished task, without pretending
        # that EOF is an answer or asking the model to emit another finish call.
        view = checkpoint or self._finalize({"summary": "Waiting for user input; progress is saved.",
                                             "conclusion": "needs_user", "question": question}, "awaiting_user")
        self.finished = {**view, "status": "awaiting_user", "conclusion": "needs_user",
                         "question": question, "suspensionReason": "input_unavailable",
                         "next": [f"/resume {self.logger.task_id} <回答或修改意见>"]}
        return result(self.finished, terminate=True)

    async def _ask_user(self, args, *, checkpoint=None):
        question = str(args.get("question") or "").strip()
        if not question:
            raise ValueError("ask_user requires a nonempty question")
        checkpoint_ref = (self.store.receipt(self.reload(), "workflow.interaction.checkpoint", checkpoint)
                          if checkpoint else None)
        self._begin_interaction(question)
        while True:
            answer = await self.ask_terminal(question)
            self._check_stop()
            if answer is None:
                return self._suspend_for_user(question, checkpoint=checkpoint)
            if answer.strip():
                break
        self._clear_interaction()
        item = self.record_input(answer, source="ask_user", question=question)
        return result({"status": "answered", "answer": answer, "inputId": item["inputId"],
                       **({"checkpoint": checkpoint_view(checkpoint, checkpoint_ref)} if checkpoint_ref else {}),
                       "next": "Continue this task using the answer and existing evidence. "
                               "Decide its meaning against the original goal; it is not a completion verdict."})

    async def _request_hitl(self, args):
        binding = self.state.get("binding") or {}
        if not binding:
            raise ValueError("Bind the page that needs the user first")
        self._check_stop()
        pending = self.state.get("pendingInteraction") or {}
        paused = pending.get("pause") if (pending.get("kind") == "browser"
            and pending.get("pageId") == binding["pageId"]
            and self.state.get("pendingQuestion") == args["reason"]) else None
        if paused:
            await self.platform.connect()
        else:
            paused = await self.platform.call("Hitl.requestPause", {"pageId": binding["pageId"],
                "reason": args["reason"], "purpose": "User action required: " + args["reason"][:200]},
                allowed=frozenset({"Hitl.requestPause"}))
        if not paused.get("ok"):
            return result(paused, error=True)
        self._begin_interaction(args["reason"], kind="browser", pageId=binding["pageId"], pause=paused)
        resumed = asyncio.get_running_loop().create_future()

        def watch(message: dict[str, Any]) -> None:
            envelope = (message.get("params") or {}).get("data") or {}
            payload = envelope.get("payload") or {}
            if (envelope.get("event") in {"Hitl.resumed", "Page.close"}
                    and payload.get("pageId") == binding["pageId"] and not resumed.done()):
                resumed.set_result(envelope.get("event"))
        unsubscribe = self.platform.client.subscribe_notifications(watch)
        question = None
        feedback = {}
        try:
            with self.waiting("hitl"):
                from harness.runtime.hitl_input import read_terminal_input
                question = asyncio.create_task(read_terminal_input(
                    f"[{self.label} · @{self.logger.task_id[:8]}] 请在浏览器中完成：{args['reason']}\n"
                    "完成后在浏览器点击恢复，或在此输入 done。"))
                done, _ = await self._await_interaction(
                    asyncio.wait({resumed, question}, return_when=asyncio.FIRST_COMPLETED))
                if question in done and not resumed.done():
                    answer = question.result()
                    if not isinstance(answer, str) or not answer.strip():
                        return self._suspend_for_user(args["reason"])
                    item = self.record_input(answer, source="hitl", question=args["reason"])
                    feedback = {"answer": answer, "inputId": item["inputId"]}
                    await self.platform.call("Hitl.resolvePause", {"pageId": binding["pageId"],
                        "purpose": "User confirmed the requested action in the terminal"},
                        allowed=frozenset({"Hitl.resolvePause"}))
                    event = "terminal"
                else:
                    question.cancel()
                    event = resumed.result()
        finally:
            unsubscribe()
            for waiting in (question, resumed):
                if waiting is not None and not waiting.done():
                    waiting.cancel()
            if question is not None:
                await asyncio.gather(question, return_exceptions=True)
        self._clear_interaction()
        self.save()
        return result({"event": event, **feedback, "pause": paused.get("data"), "receiptRef": paused.get("receiptRef"),
                       "next": "Resumed does not mean done: observe the page and verify the needed state."})

    async def _delegate_browser(self, args):
        from harness.workflow_mode.run_policy import require_observation
        if self.profile not in {'run', 'author'}:
            raise PermissionError('This profile cannot delegate Browser work')
        require_observation(args.get('mode', 'observation'))
        if self.profile == "run":
            from harness.workflow_mode.observer import delegate_observation
            return await delegate_observation(self, args)
        binding = self.state.get("binding") or {}
        if not binding:
            raise ValueError("Bind the page first")
        if self.host is None:
            raise ValueError("Browser delegation is unavailable in this embedding")
        evidence_context = []
        for reference in args.get("evidence_refs") or []:
            ref, selected = evidence_reference(reference, known_refs=self.known_refs())
            if ref not in self.known_refs():
                raise PermissionError("Delegation evidence must belong to this task")
            evidence_context.append({"ref": ref, "pointer": selected,
                                     "value": pointer(self.store.read_ref(ref), selected)})
        from harness.agents.browser.task import BrowserTaskRunner, _run_browser_mode
        from harness.runtime.resume_state import write_task_manifest
        child_runtime = dataclasses.replace(self.runtime, harness=copy.copy(self.runtime.harness))
        child_runtime.harness.protected_local_roots = sorted(set(
            getattr(child_runtime.harness, "protected_local_roots", []) or []) | {
                str(self.registry.root.resolve()), str(self.package_dir().resolve())} |
                {str(path.resolve()) for path in self.registry.root.glob('*/history')})
        logger = self.host._create_task_logger(child_runtime)
        logger.run_id = self.host._new_run_id(resumed=False)
        self.host._open_task_storage(logger, child_runtime)
        status = "failed"
        runner, task_handle, cleanup_error = None, None, None
        exports = []
        outcome = {"status": "failed", "summary": "Author Browser delegation did not return"}
        try:
            # The pinned context is the only route: no @Fleet in the text and no
            # task_fleet_reference, since the spawner rejects a pin combined with
            # any Fleet reference (invalid_fleet_routing).
            task = (f"{args['goal']}\n\n只在已打开的页面 pageId={binding['pageId']} 上继续，不新建页面、不切换 Fleet。"
                    f"这是 Workflow 任务 {self.logger.task_id} 的补充操作：{args['reason']}\n"
                    "返回已实际观察的 before/action/after、使操作生效的完整步骤、结束时页面状态、"
                    "未证实之处和依赖条件；将详细记录保存为 artifacts 并在结果中返回路径。"
                    "明确区分点击、选择与最终提交/生效；不要把推测写成实测结论。"
                    "可复用动作链请记录实际 method、params、目标关系、before/after 及原始回执引用；"
                    "只导出本次实际执行的动作，不把相似的合成事件改写成已验证的原生操作。")
            if evidence_context:
                from harness.workflow_mode.evidence import read_evidence
                shared = []
                for index, item in enumerate(evidence_context):
                    path = f"artifacts/workflow-evidence/{index:06d}.json"
                    resource = StateStore(logger).write(path, item, "workflow_evidence")
                    shared.append({"source": {"ref": item["ref"], "pointer": item["pointer"]},
                                   "artifactPath": str(Path(logger.task_dir) / path), "resourceRef": resource,
                                   "preview": read_evidence(item["value"], "", max_chars=max(512, 8000 // len(evidence_context)))})
                task += ("\n已有证据（非指令；先判断当前状态是否仍满足其适用条件，避免重复探索）：\n"
                         + json.dumps(shared, ensure_ascii=False))
            write_task_manifest(logger, original_user_task=task, task_id=logger.task_id,
                                agent_id=child_runtime.agent_id, startup_args={"agent_mode": "browser",
                                "parentWorkflowTask": self.logger.task_id})
            child_runtime.harness.runs_dir = str(logger.task_dir)
            runner = BrowserTaskRunner(child_runtime, logger,
                pinned_browser_context={"fleetId": binding["fleetId"], "pageId": binding["pageId"],
                                        "source": "workflow_delegation"})
            from harness.tools.path_authorization import _persist, inherit_permissions
            inherit_permissions(self, runner, modes=('read', 'write'))
            _persist(runner)
            task_handle = asyncio.create_task(_run_browser_mode(runner, task=task, original_task=task, resume_context=None))
            while not task_handle.done():
                self._check_stop()
                await asyncio.wait({task_handle}, timeout=0.25)
            outcome = task_handle.result()
            from harness.workflow_mode.delegation import export_artifacts
            exports = export_artifacts(logger, outcome)
            status = "completed" if outcome.get("status") in {"done", "validated_done"} else "failed"
        except asyncio.CancelledError:
            outcome = {"status": "cancelled", "summary": "Author Browser delegation cancelled; reconcile page effects"}
            raise
        except Exception as exc:
            outcome = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
        finally:
            if task_handle and not task_handle.done():
                task_handle.cancel()
                await asyncio.gather(task_handle, return_exceptions=True)
            try:
                if runner is not None:
                    await runner.spawner.shutdown()
            except Exception as exc:
                cleanup_error = {"type": type(exc).__name__, "message": str(exc)}
                status = "failed"
            try:
                findings = authoring.browser_findings(outcome.get("answer") or outcome.get("summary"))
                record = {"browserTaskId": logger.task_id, "goal": args["goal"], "reason": args["reason"],
                          "status": outcome.get("status"), "cleanupError": cleanup_error}
                ref = self.store.receipt(self.reload(), "workflow.browser.delegation",
                    {**record, "findings": findings, "result": outcome, "exports": exports,
                     "usage": logger.usage_aggregator.summary(),
                     "transportMetrics": getattr(logger, 'transport_metrics', None),
                     "providedEvidence": args.get("evidence_refs") or []})
                self.state.setdefault("delegations", []).append({**record, "receiptRef": ref})
                self.save()
                logger.write_usage_summary()
            finally:
                self.host._close_task_storage(logger, status=status)
        self._check_stop()
        from harness.workflow_mode.evidence import read_evidence
        return result({**record, "receiptRef": ref,
                       "findings": read_evidence(findings, "/findings", max_chars=8000),
                       "exports": [{**{k: v for k, v in item.items() if k != "value"},
                                    **({"evidence": {"ref": ref, "pointer": f"/exports/{i}/value"}}
                                       if item.get("status") == "exported" else {})}
                                   for i, item in enumerate(exports)],
                       "evidenceLocations": {"findings": {"ref": ref, "pointer": "/findings"},
                                             "exports": {"ref": ref, "pointer": "/exports"},
                                             "rawResult": {"ref": ref, "pointer": "/result"}},
                       "next": "Compare the reported conclusion with its action-by-action observations. "
                               "Conflicting measurements remain unresolved; do not copy a suggested success "
                               "predicate without checking those observations. Use targeted receipt reads "
                               "for gaps. Browser success alone does not verify the generated implementation."}, error=status != "completed")

    async def _report_defect(self, args):
        kind = args.get('kind', 'unknown')
        if kind not in {'package', 'platform', 'business', 'unknown'}:
            raise ValueError('Issue kind must be package, platform, business or unknown')
        args = {**args, "evidence_refs": [resolve_reference(ref, self.known_refs())
                                          for ref in args["evidence_refs"]]}
        unknown = [r for r in args["evidence_refs"] if r not in self.known_refs()]
        if unknown:
            raise ValueError(f"Evidence references are not receipts of this task: {unknown[:3]}")
        defect = {"summary": args["summary"], "evidenceRefs": args["evidence_refs"], "stage": args.get("stage"),
                  "kind": kind, "assessmentSource": "run_agent", "attributionVerified": False,
                  "contentHash": (self.state.get("package") or {}).get("contentHash")}
        self.state.setdefault("defects", []).append(defect)
        self.save()
        self.runs.update(self.logger.task_id, defect=args["summary"][:300])
        info = self.state.get("package") or {}
        repair = (f"/workflow-edit {info['sourceDraft']}" if info.get("origin") == "draft"
                  else "/workflow-edit " + str(info.get('workshopId') or info.get("slug") or "<workflow>"))
        if kind != 'package':
            repair = None
        if not self.quiet:
            from harness.runtime.session_output import block
            block(self.label, "问题记录（Agent 归因）", [("任务", "@" + self.logger.task_id), ("摘要", args["summary"]),
                  ("处理", f"其它独立任务可继续运行；需要停止时用 /workflow-stop。"
                   + (f"包修复用 {repair}。" if repair else "先核对责任层与实际效果，不自动归因为包缺陷。"))])
        return result({"recorded": defect, "repairCommand": repair,
                       "next": "Retain the observed issue and uncertain attribution. Inspect effects and use "
                       "supported package recovery only when repetition is safe and still necessary. Continue "
                       "independent work. With no supported path, finish partial/blocked with exact remaining identities. "
                       "Package repair is a separate author task; do not edit it here."})

    # ------------------------------------------------------------ finish
    def _judgment(self, args) -> dict[str, Any]:
        known = self.known_refs()
        completion_refs = {d["receiptRef"] for d in self.state.get("delegations", [])
                           if d.get("mode") == "completion" and d.get("receiptRef")}
        checked = acceptance.validate_judgment(args, points=self.state.get("acceptancePoints") or [],
            known_refs=known, execution_refs={e["receiptRef"] for e in self.state.get("executions", [])
                                                          if e.get("receiptRef")} | completion_refs,
            operator_inputs=self.all_inputs(), skips=acceptance.open_skips(self.state.get("executions", [])),
            read_evidence=self.store.read_ref)
        if not checked["ok"]:
            return checked
        invalid_refs = {e.get("receiptRef") for e in self.state.get("executions", []) if e.get("identityIssues")}
        for point in args.get("points", []):
            refs = {evidence_reference(ref, known_refs=known)[0] for ref in point.get("evidenceRefs", [])}
            if point.get("status") == "verified" and refs and refs <= invalid_refs:
                checked["problems"].append(f"{point['id']}: verification relies only on execution evidence with identity issues")
                checked["ok"] = False
        return checked

    def _finalize(self, args, status: str) -> dict[str, Any]:
        self.state.update(status=status, judgment=args,
                          judgmentRevision=(self.state.get('acceptanceRevision') or {}).get('number'))
        if status == "awaiting_user":
            self.state["pendingQuestion"] = args["question"]
            self.state.setdefault("pendingInteraction", {"kind": "question"})
        self.save()
        return {"status": status, "conclusion": args.get("conclusion"), "taskId": self.logger.task_id,
                "summary": args.get("summary"), "remainingWork": args.get("remaining_work") or [],
                "templateGaps": args.get("template_gaps") or [], "question": args.get("question"),
                "defects": self.state.get("defects", []), "package": self.state.get("package") or self.state.get("draft"),
                "judgment": args,
                "goalReview": acceptance.review_facts(self.state, self.all_inputs(), args),
                "acceptanceRevision": self.state.get("acceptanceRevision"),
                "acceptanceReview": self.acceptance_review(),
                "executionReceipts": [{k: e.get(k) for k in
                    ("receiptRef", "workflowId", "contentHash", "outcome", "pageId", "stages")}
                    for e in self.state.get("executions", [])],
                "localExecutions": self.state.get("localExecutions", []),
                "fileManifests": self.state.get("fileManifests", []),
                "executions": len(self.state.get("executions", [])),
                "browserCompletions": [d for d in self.state.get("delegations", []) if d.get("mode") == "completion"],
                "usage": self.usage.snapshot(),
                "next": [f"/resume {self.logger.task_id} <补充>"]}

    def _resolve_judgment(self, arguments):
        args = copy.deepcopy(arguments)
        reference = args.pop("judgment_ref", None)
        if reference:
            reference = resolve_reference(reference, self.known_refs())
            if not any(row.get("ref") == reference and row.get("event") == "workflow.judgment.draft"
                       for row in self.state.get("receipts", [])):
                raise PermissionError("judgment_ref must be this task's rejected finish draft")
            base = self.store.read_ref(reference)
            if base.get("kind") != "workflow.judgment.draft":
                raise ValueError("Not a saved judgment draft")
            candidate = copy.deepcopy(base["judgment"])
            updates = args.pop("points", None)
            candidate.update(args)
            if updates is not None:
                if len({item["id"] for item in updates}) != len(updates):
                    raise ValueError("Each updated acceptance point must occur once")
                by_id = {item["id"]: item for item in candidate.get("points", [])}
                by_id.update({item["id"]: item for item in updates})
                candidate["points"] = list(by_id.values())
            args = candidate
        if not all(key in args for key in ("summary", "conclusion", "points")):
            raise ValueError("finish needs summary, conclusion and points, or judgment_ref for a saved draft")
        # Canonicalize addresses, never repair a malformed URI or alter the claim.
        for item in args.get("points", []):
            normalized = []
            for value in item.get("evidenceRefs", []):
                try:
                    ref, path = evidence_reference(value)
                    ref = resolve_reference(ref, self.known_refs())
                    normalized.append({"ref": ref, "pointer": path})
                except (ValueError, PermissionError, TypeError):
                    normalized.append(value)
            if "evidenceRefs" in item:
                item["evidenceRefs"] = normalized
        return args

    async def _finish_run(self, args):
        args = self._resolve_judgment(args)
        checked = self._judgment(args)
        if not checked["ok"]:
            ref = self.store.receipt(self.reload(), "workflow.judgment.draft",
                                     {"kind": "workflow.judgment.draft", "judgment": args})
            return result({"status": "rejected", "problems": checked["problems"], "receiptRef": ref,
                           "judgment_ref": short_reference(ref) or ref,
                           "next": "Reuse judgment_ref and send only corrected points (full records by id), "
                                   "or explicit changed fields; omitted fields are preserved. All current "
                                   "criteria, permissions and evidence are checked again."}, error=True)
        if str(args.get("question") or "").strip():
            checkpoint = self._finalize(args, "awaiting_user")
            checkpoint.pop("next", None)
            return await self._ask_user(args, checkpoint=checkpoint)
        self.finished = self._finalize(args, "done" if args["conclusion"] == "complete" else "incomplete")
        return result(self.finished, terminate=True)

    async def _save_draft(self, args):
        args = self._resolve_judgment(args)
        if args.get("conclusion") not in acceptance.AUTHOR_CONCLUSIONS:
            raise ValueError("save_draft conclusion is draft_saved, partial, needs_user or blocked")
        checked = self._judgment({**args, "conclusion": "partial" if args["conclusion"] == "draft_saved"
                                  else args["conclusion"]})
        if not checked["ok"]:
            ref = self.store.receipt(self.reload(), "workflow.judgment.draft",
                                     {"kind": "workflow.judgment.draft", "judgment": args})
            return result({"status": "rejected", "problems": checked["problems"],
                           "judgment_ref": short_reference(ref) or ref, "receiptRef": ref,
                           "next": "Reuse judgment_ref with corrected points or changed fields; omitted fields are retained."}, error=True)
        root = self.package_dir()
        structure = pkg.check_structure(root)
        coverage_report = self._coverage_report()
        draft = self.registry.read_draft(self.state["draft"]["taskId"])
        digest = pkg.content_hash(root)
        self.registry.checkpoint(draft['taskId'], reason='save', summary=args.get('summary'))
        self.registry.write_draft({**draft, "status": "saved" if structure["status"] == "valid" else "development",
            "savedHash": digest, "savedAt": _now(), "verifiedScope": args.get("verified_scope") or [],
            "knownLimitations": args.get("known_limitations") or [],
            "coverage": coverage_report,
            "verifiedVersions": None,  # Deprecated; versions below are attempts, not verdicts.
            "executedVersions": sorted({e["contentHash"] for e in self.state.get("executions", [])
                                        if e.get("contentHash")})})
        question = str(args.get("question") or "").strip()
        saved = {**self._finalize(args, "awaiting_user" if question else "draft_saved"), "draftHash": digest,
                 "executedOnCurrentHash": any(e.get("contentHash") == digest for e in self.state.get("executions", [])),
                 "verifiedOnCurrentHash": None,  # Deprecated: an execution attempt is not verification.
                 "verificationNote": "executedOnCurrentHash records any execution attempt on this hash, including failed ones. It is not business verification; see per-point evidence and limitations.",
                 "structure": structure, "verifiedScope": args.get("verified_scope") or [],
                 "knownLimitations": args.get("known_limitations") or [],
                 "coverage": coverage_report}
        if question:
            saved.pop("next", None)
            return await self._ask_user(args, checkpoint=saved)
        self.finished = {**saved, "next": [f"/workflow-publish {self.logger.task_id}",
                                          f"/resume {self.logger.task_id} <修改意见>"]}
        return result(self.finished, terminate=True)

    # -------------------------------------------------------- coordinator
    async def _run_here(self, args):
        if self.profile != "coordinator" or self.children or self.state.get("children"):
            raise ValueError("run_here is only available before dispatching any item tasks")
        self._check_stop()
        self.state["profile"] = "run"
        self.state["runHere"] = True
        self.save()
        # The startup manifest is immutable. Resume resolves this transition
        # from durable session state under the task lock, preserving identity.
        self._role_transition = True
        return result({"status": "continue_here", "taskId": self.logger.task_id,
                       "next": "Continue as run Agent with the same goal, history, package and permissions."},
                      terminate=True)

    async def _start_tasks(self, args):
        if self.profile not in {"run", "coordinator"} or self.batch_id:
            raise PermissionError("Only a top-level run may dispatch business items")
        items = args["items"]
        if not items:
            raise ValueError("Supply independent business items")
        running = [c for c in self.children.values() if not c["task"].done()]
        if len(running) + len(items) > _MAX_CONCURRENT_TASKS:
            raise ValueError(f"At most {_MAX_CONCURRENT_TASKS} concurrent tasks")
        package_row = self.state["package"]
        expected_hash, parent_snapshot = self._pinned()
        started = []
        for item in items:
            fleet, error = extract_fleet_reference(item["request"])
            if error:
                raise ValueError(error)
            fleet = fleet or self.state.get("fleetReference")
            child_runtime = dataclasses.replace(self.runtime, harness=copy.copy(self.runtime.harness))
            logger = self.host._create_task_logger(child_runtime)
            logger.run_id = self.host._new_run_id(resumed=False)
            self.host._open_task_storage(logger, child_runtime)
            from harness.runtime.resume_state import write_task_manifest
            write_task_manifest(logger, original_user_task=item["request"], task_id=logger.task_id,
                agent_id=child_runtime.agent_id, startup_args={"agent_mode": "workflow", "workflowProfile": "run",
                "workflowSlug": package_row["slug"], "batchId": self.logger.task_id})
            child = WorkflowSession(child_runtime, logger, profile="run", host=self.host,
                                    label=f"Workflow·{item['label'][:16]}", quiet=self.quiet,
                                    batch_id=self.logger.task_id)
            digest, _ = self.registry.snapshot_directory(parent_snapshot, Path(logger.task_dir))
            if digest != expected_hash:
                raise RegistryError("The batch's pinned version changed while starting an item")
            child.initialize(item["request"], package_row=package_row, fleet_id=fleet)

            async def run_child(child=child, logger=logger):
                status = "failed"
                try:
                    outcome = await child.run(child.state["goal"])
                    self._record_child_result(logger.task_id, outcome)
                    status = "completed" if outcome.get("status") == "done" else "interrupted"
                    return outcome
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                finally:
                    await child.close()
                    self.host._close_task_storage(logger, status=status)
            record = {"label": item["label"], "taskId": logger.task_id, "status": "running",
                      "task": asyncio.create_task(run_child()), "session": child}
            self.children[logger.task_id] = record
            self.state.setdefault("children", {})[logger.task_id] = {"label": item["label"], "status": "running",
                                                                     "request": item["request"]}
            started.append({"taskId": logger.task_id, "label": item["label"], "fleet": fleet})
        transition = self.profile == "run"
        if transition:
            self.state["profile"] = "coordinator"
            self._role_transition = "coordinator"
        self.save()
        return result({"started": started, "next": "wait_tasks; tasks run concurrently, one page each."},
                      terminate=transition)

    def _record_child_result(self, task_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        saved = self.reload().setdefault("children", {}).setdefault(task_id, {})
        if not saved.get("resultRef"):
            saved["resultRef"] = self.store.receipt(self.state, "workflow.child.result",
                {"childTaskId": task_id, "result": outcome})
        saved.update(result=outcome, status=outcome.get("status"), conclusion=outcome.get("conclusion"))
        self.save()
        return saved

    def _child_view(self, task_id: str) -> dict[str, Any]:
        record = self.children.get(task_id)
        saved = self.state.get("children", {}).get(task_id, {})
        if record is None:
            outcome = saved.get("result") or {}
            return {"taskId": task_id, "label": saved.get("label"),
                    "status": saved.get("status", "unavailable"),
                    "conclusion": outcome.get("conclusion"), "resultRef": saved.get("resultRef"),
                    "resultPointer": "/result", "summary": compact(outcome.get("summary"), 1500),
                    "remainingWork": outcome.get("remainingWork"), "question": outcome.get("question"),
                    "executionReceipts": outcome.get("executionReceipts")}
        view = {"taskId": task_id, "label": record["label"], "status": "running"}
        if record["task"].done():
            if record["task"].cancelled():
                view.update(status="cancelled")
            elif record["task"].exception():
                exc = record["task"].exception()
                view.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            else:
                outcome = record["task"].result()
                saved = self._record_child_result(task_id, outcome)
                view.update(status=outcome.get("status"), conclusion=outcome.get("conclusion"),
                            summary=compact(outcome.get("summary"), 1500), defects=outcome.get("defects"),
                            remainingWork=outcome.get("remainingWork"), question=outcome.get("question"),
                            templateGaps=outcome.get("templateGaps"),
                            resultRef=saved["resultRef"],
                            resultPointer="/result",
                            executionReceipts=outcome.get("executionReceipts"))
            self.state["children"][task_id].update({k: view.get(k) for k in ("status", "conclusion")})
        return view

    async def _wait_tasks(self, args):
        known = self.state.get("children", {}).keys() | self.children.keys()
        ids = args.get("task_ids") or list(known)
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ValueError(f"Unknown task ids {unknown}")
        tasks = [self.children[i]["task"] for i in ids
                 if i in self.children and not self.children[i]["task"].done()]
        if tasks:
            # Child compute is part of end-to-end elapsed time, not human waiting.
            with self.waiting("child_tasks"):
                await asyncio.wait(tasks, timeout=max(5, min(int(args.get("timeout_seconds") or 600), 3600)))
        views = [self._child_view(i) for i in ids]
        self.save()
        return result({"tasks": views, "running": [v["taskId"] for v in views if v["status"] == "running"]})

    async def stop_children(self, ids: list[str], reason: str) -> list[dict[str, Any]]:
        outcomes = []
        for task_id in ids:
            record = self.children.get(task_id)
            if record is None or record["task"].done():
                continue
            child = record["session"]
            child.stop_reason = reason
            self.runs.update(task_id, stopRequested=True)
            cancelled = {}
            for workflow_id, live in list(child.platform.live.items()):
                if live.get("status") not in TERMINAL:
                    cancelled[workflow_id] = await child.platform.cancel(workflow_id, reason=reason)
            outcomes.append({"taskId": task_id, "cancelled": cancelled})
        return outcomes

    async def _stop_tasks(self, args):
        ids = list(self.children) if args["task_ids"] == ["all"] else args["task_ids"]
        return result({"stopped": await self.stop_children(ids, args["reason"])})

    async def _finish_coordinator(self, args):
        question = str(args.get("question") or "").strip()
        if args.get("conclusion") == "needs_user" and not question:
            raise ValueError("needs_user requires the question for the user")
        ids = self.state.get("children", {}).keys() | self.children.keys()
        views = [self._child_view(i) for i in sorted(ids)]
        running = [v["taskId"] for v in views if v.get("status") == "running"]
        if running:
            return result({"status": "rejected", "problems": [f"tasks still running: {running}"],
                           "next": "wait_tasks or stop_tasks first"}, error=True)
        if question:
            checkpoint = {**self._finalize(args, "awaiting_user"), "tasks": views}
            checkpoint.pop("next", None)
            return await self._ask_user(args, checkpoint=checkpoint)
        self.finished = {**self._finalize(args, "done" if args["conclusion"] == "complete" else "incomplete"),
                         "tasks": views, "next": [f"/resume {v['taskId']}" for v in views
                                                  if v.get("status") != "done"]}
        return result(self.finished, terminate=True)

    # -------------------------------------------------------------- loop
    def _stop_flag(self) -> bool:
        parent = getattr(self, "_observation_parent", None)
        return (bool(self.stop_reason) or self.runs.stop_requested(self.logger.task_id)
                or (parent is not None and parent._stop_flag()))

    def _check_stop(self) -> None:
        if self._stop_flag():
            raise StopRequested(self.stop_reason or "User requested /workflow-stop")

    def _usage(self, outcome: Any) -> None:
        source = "browser_agent" if self.profile == "observer" else f"workflow_{self.profile}_agent"
        record_usage(self.logger, outcome, source, conversation_id=self.logger.task_id)
        self.usage.charge(outcome)
        self.save()

    async def _before_model(self, messages):
        self._check_stop()
        from harness.workflow_mode.author_cost import capture
        previous = (self.state.get('authorCostLatest') or {}).get('receiptRef')
        capture(self)
        latest = self.state.get('authorCostLatest')
        if latest and latest['receiptRef'] != previous:
            # Tool accounting completes after the handler returns. Add the
            # measured interval to the existing tool message, not a new turn.
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if isinstance(message, ToolResultMessage):
                    messages = list(messages)
                    messages[index] = message.model_copy(update={'content': [*message.content, TextContent(
                        text=json.dumps({'authorCost': latest,
                            'note': 'Harness measurements, not user instructions. Compare equivalent validation scope; '
                                    'auxiliary model cost is not attributed to this interval.'}, ensure_ascii=False))]})
                    break
        return messages

    async def _prepare_author_guidance(self) -> None:
        """Refresh once per entry/resume, retaining facts instead of rereading per model turn."""
        from abcp_client import ABCPTransportError
        from harness.workflow_mode.platform import platform_error
        previous = self.state.get("authorGuidance")
        self._author_guidance = {}
        try:
            await self.platform.connect()
            self._author_guidance = guidance.snapshot(self.platform)
        except ABCPTransportError as exc:
            self._author_entry_facts = {"guidanceStatus": "unavailable", "error": platform_error(exc),
                "interpretation": "Live capabilities are unknown; previous guidance is historical."}
            return
        current = self._author_guidance
        self._author_entry_facts = {"capturedAt": _now(), "platform": guidance.identity(current),
            "revisions": guidance.revision_changes(previous, current),
            "guideStatus": current["guideStatus"], "schemasMatched": current["schemasMatched"],
            "capabilityCount": current["capabilityCount"]}
        try:
            inventory = coverage.read(self.package_dir())
            source_hashes = {row["path"]: row["sha256"] for row in pkg.file_inventory(self.package_dir())
                             if row["path"].startswith("src/")}
            self._author_entry_facts["strategies"] = guidance.strategy_facts(inventory,
                current=current, source_hashes=source_hashes)
        except (OSError, ValueError) as exc:
            self._author_entry_facts["strategies"] = {"status": "unavailable", "error": str(exc)}
        self.state["authorGuidance"] = {**guidance.identity(current), "methods": current["methods"]}
        self.save()

    def _initial_prompt(self, resume_instruction: str) -> str:
        contract = {}
        previous = (self.state.get("packageContract") or {}).get("contentHash")
        if self.profile in {"run", "author"}:
            try:
                contract = self._contract_snapshot()
            except (OSError, ValueError) as exc:
                contract = {"status": "unavailable", "error": str(exc)}
        resumed = bool(resume_instruction or self.state.get("messagesSaved"))
        base = ("[Resumed task] Reconcile earlier effects; do not replay uncertain actions.\n" if resumed
                else "Original request:\n" + self.state["goal"] + "\n\n")
        base += "Task state:\n" + json.dumps(self.projection(), ensure_ascii=False)
        if not resumed or contract.get("contentHash") != previous:
            base += "\n\nVersion-bound package contract (reuse; read exact sections only for gaps):\n" + json.dumps(contract, ensure_ascii=False)
        if resume_instruction:
            base += "\nNew user input: " + resume_instruction
        return base

    def _cost_snapshot(self):
        from harness.workflow_mode.cost import report as cost_report
        try:
            cost = cost_report(self)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            cost = {"status": "unavailable", "error": str(exc), "sessionUsage": self.usage.snapshot()}
        self.state["costRef"] = self.store.write("workflow/cost.json", cost, "workflow_cost")
        return cost

    @steerable_task
    @workflow_timing
    async def run(self, goal: str, *, resume_instruction: str = "") -> dict[str, Any]:
        from harness.runtime.agent_loop import agent_events
        from harness.runtime.session_output import message, tool_result
        from llm.factory import LLMFactory
        self.initialize(goal)
        if self.profile == 'author':
            from harness.workflow_mode.author_cost import checkpoint
            self._author_cost_anchor = checkpoint(self)
        self.finished = None
        installer = getattr(self, "_terminal_stop_installer", None)
        if installer is not None and self.batch_id is None:
            installer(self)
        # Older finish tools saved an explicit question as a terminal judgment.
        # Recover that question as an interaction, without inferring business intent.
        if (not self.state.get("pendingQuestion") and self.state.get("status") in {"draft_saved", "incomplete"}
                and self.profile != "observer"):
            question = str((self.state.get("judgment") or {}).get("question") or "").strip()
            if question:
                self.state.update(pendingQuestion=question, pendingInteraction={"kind": "question"})
        if resume_instruction:
            question = self.state.get("pendingQuestion")
            self._clear_interaction()
            self.record_input(resume_instruction, source="resume_cli", question=question)
        register = self.runs.update if getattr(self, "_continuing_here", False) else self.runs.register
        register(self.logger.task_id, profile=self.profile, label=self.label, batchId=self.batch_id,
            slug=(self.state.get("package") or self.state.get("draft") or {}).get("slug"),
            contentHash=(self.state.get("package") or {}).get("contentHash"),
            pageId=(self.state.get("binding") or {}).get("pageId"))
        config = (browser_agent_model_config(self.runtime) if self.profile == "observer"
                  else workflow_agent_model_config(self.runtime))
        raw_provider = LLMFactory.create_provider(config)
        adapter = HarnessProviderAdapter(raw_provider, on_result=self._usage)
        history_json, context_json = self.conversation.load()
        history = [TypeAdapter(AgentMessage).validate_python(m) for m in history_json]
        context = [TypeAdapter(AgentMessage).validate_python(m) for m in context_json]
        status, stop = "incomplete", None
        tool_starts = {}
        try:
            if self.state.get("pendingQuestion") and self.profile != "observer":
                question = self.state["pendingQuestion"]
                if (self.state.get("pendingInteraction") or {}).get("kind") == "browser":
                    await self._request_hitl({"reason": question})
                else:
                    await self._ask_user({"question": question})
                if self.finished is not None:
                    return self.finished
            system = PROFILES[self.profile]
            if self.profile == "author":
                await self._prepare_author_guidance()
                if self._author_guidance:
                    system += guidance.system_block(self._author_guidance)
            async for event, active in agent_events(logger=self.logger, runtime_config=self.runtime.harness,
                    raw_provider=raw_provider, provider=adapter, model=config.model_id, system=system,
                    tools=self.tools(), messages=context, prompt=self._initial_prompt(resume_instruction),
                    context_path="workflow/context.json", actor=f"workflow.{self.profile}.{self.logger.task_id[:8]}",
                    facts=self.projection, prepare_context=self._before_model, label=self.label, quiet=self.quiet,
                    persist_context=self.conversation.save_context,
                    completion_pending=lambda: self.finished is None and not getattr(self, "_role_transition", False)):
                if isinstance(event, MessageEndEvent):
                    history.append(event.message)
                    self.conversation.save(history, active)
                    if not self.state.get("messagesSaved"):
                        self.reload()["messagesSaved"] = True
                        self.save()
                    if isinstance(event.message, AssistantMessage):
                        if event.message.text and not self.quiet:
                            message(self.label, event.message.text)
                        if event.message.error_message:
                            stop = event.message.error_message
                elif isinstance(event, ToolExecutionStartEvent):
                    tool_starts[event.tool_call_id] = time.monotonic()
                elif isinstance(event, ToolExecutionEndEvent):
                    if not self.quiet:
                        tool_result(self.label, event.tool_name, event.result, error=event.is_error)
                    self.logger.write("workflow.tool", {"tool": event.tool_name, "isError": event.is_error})
                    begun = tool_starts.pop(event.tool_call_id, None)
                    if begun is not None:
                        self.usage.record_tool(event.tool_name, failed=event.is_error,
                            duration_ms=round((time.monotonic() - begun) * 1000))
                        self.logger.write("lifecycle.tool.end", {"toolName": event.tool_name,
                            "toolCallId": event.tool_call_id,
                            "durationMs": round((time.monotonic() - begun) * 1000),
                            "status": "failed" if event.is_error else "returned"})
        except StopRequested as exc:
            status, stop = "stopped", str(exc)
        if getattr(self, "_role_transition", False):
            self._role_transition = False
            if self._stop_flag():
                status, stop = "stopped", self.stop_reason or "User requested /workflow-stop"
            else:
                self.profile = self.state.get("profile", "run")
                self.label = "Workflow 调度" if self.profile == "coordinator" else "Workflow"
                self._continuing_here = True
                return await self.run(goal)
        if self.finished:
            from harness.workflow_mode.author_cost import capture
            capture(self, final=True)
            if self.profile == 'author':
                self.finished['authorCost'] = self.state.get('authorCostLatest')
            self.finished["cost"] = self._cost_snapshot()
            self.finished["usage"] = self.usage.snapshot()
            self.finished["costRef"] = self.state["costRef"]
            self.save()
            return self.finished
        self.reload().update(status=status, stopReason=stop or "No finish call was returned")
        from harness.workflow_mode.author_cost import capture
        capture(self, final=True)
        cost = self._cost_snapshot()
        self.save()
        return {"status": status, "taskId": self.logger.task_id, "summary": self.state["stopReason"],
                "cost": cost, "costRef": self.state.get("costRef"),
                "question": self.state.get("pendingQuestion"),
                "conclusion": None, "defects": self.state.get("defects", []), "usage": self.usage.snapshot(),
                **({'authorCost': self.state.get('authorCostLatest')} if self.profile == 'author' else {}),
                "package": self.state.get("package") or self.state.get("draft"),
                "children": self.state.get("children", {}),
                "next": [f"/resume {self.logger.task_id} <补充>"]}

    async def close(self) -> None:
        try:
            await self.local_runner.close()
            if self.children:
                await self.stop_children([i for i, c in self.children.items() if not c["task"].done()],
                                         "Coordinator closed")
                await asyncio.gather(*(c["task"] for c in self.children.values()), return_exceptions=True)
                # A suspended coordinator may reopen its question later. Keep
                # the actual terminal child states instead of stale "running".
                for task_id in self.children:
                    self._child_view(task_id)
                self.save()
            for platform in self._batch_platforms | set(self._execution_platforms.values()) - {self.platform}:
                await platform.close()
            cancelled = await self.platform.close()
            if cancelled:
                self.reload().setdefault("closeCancellations", []).append(cancelled)
                self.save()
        finally:
            from harness.tools.local_search import close_searches
            close_searches(self.logger)
            final = {"done": "completed", "draft_saved": "completed"}.get(self.state.get("status"), "interrupted")
            self.runs.finish(self.logger.task_id, final)
