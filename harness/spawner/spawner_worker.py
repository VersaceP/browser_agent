"""
harness.spawner.spawner_worker - SpawnerWorkerMixin - skill fast path, worker run loop and result preparation.
"""

import asyncio
import json
import weakref
from dataclasses import replace
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from abcp_client import ABCPClient
from abcp_client import ABCPTransportError
from harness.constants import WORKER_STATUS_CANCELLED
from harness.constants import WORKER_STATUS_FAILED
from harness.diagnostics import status_category
from harness.fleet.coordinator import FleetAssignment
from harness.observation.render_recovery import extract_page_id_from_values
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.fast_path import assess_fast_path_candidate
from harness.results.row_ledger import row_identity
from harness.results.row_ledger import derive_row_facts
from harness.results.row_ledger import derive_row_ledger
from runtime_config import RuntimeConfig
from harness.lifecycle import LifecycleContext
from harness.model_config import browser_agent_model_config
from harness.schema_cache import global_schemas_dir
from harness.schema_loader import CapabilityBundle
from harness.schema_loader import load_capability_bundle
from harness.task_control import build_attempt_digest
from harness.task_control import classification_for_worker_status
from harness.task_control import mark_phase_result
from harness.task_control import phase_prior_artifact_paths
from harness.task_control import record_replan_checkpoint
from harness.task_control import validate_worker_artifacts
from harness.task_control import load_task_state
from harness.strategy_telemetry import append_strategy_attempt
from harness.tool_policy import ALWAYS_FORBIDDEN_ABCP_METHODS
from harness.templates import get_path
from harness.utils import JsonDict
from harness.utils import extract_offloaded_paths
from harness.utils import make_browser_event_logger
from harness.utils import optional_int
from harness.utils import safe_path_component
from harness.utils import storage_for_logger
from harness.utils import task_subdir
from harness.utils import trim_large_strings
from harness.results.worker_result import build_worker_handoff_projection
from harness.results.worker_result import build_worker_result_levels
from harness.workflow_runtime import workflow_execution_enabled
from llm import LLMFactory
from .spawner_classification import _allowance_from_validators, _clone_capability_bundle, _cohort_identity_fields, _safe_str_list, _validated_rows_for_ledger, _worker_feedback_classification  # noqa: F401
from .spawner_helpers import BrowserAgentHandle, BrowserAgentSlot, _TaskContextTrackingBrowserClient, _effective_worker_status, _finalize_skill_execution_metadata, _fresh_click_settlement_class, _prompt_worker_contract, _skill_execution_metadata, _unresolved_repair_visual_evidence, _verified_workflow_hitl_settlement  # noqa: F401

def _sp():
    import harness.spawner as sp

    return sp

class SpawnerWorkerMixin:

    def _get_skill_registry(self):
        """Lazy-load the skill registry once per spawner."""
        registry = getattr(self, "_skill_registry", None)
        if registry is None:
            try:
                from harness.skill.registry import SkillRegistry
                registry = SkillRegistry.load()
            except Exception as exc:  # registry load must never break spawning
                self.logger.write("skill.registry.load_failed", {"error": str(exc)})
                registry = False  # sentinel: tried and failed
            self._skill_registry = registry
        return registry or None

    async def _try_skill_fast_path(
        self,
        harness: Any,
        *,
        worker_contract: JsonDict,
        phase: JsonDict,
        task: str,
        context: str,
        fleet_ids: List[str],
    ) -> Optional[JsonDict]:
        """Attempt a matching skill's fast path. Returns the dispatch outcome:
        {"handled": True, "answer": ...} when the skill completed the task,
        {"handled": False, "handoff_note": ...} when a batch run stopped mid-way
        (completed rows persisted; the note tells the slow path what remains),
        or None (caller runs the normal LLM loop with the original task).
        Any error falls back to the LLM loop — must never break the worker."""
        if not workflow_execution_enabled(self.runtime):
            return None
        if not getattr(self.runtime.harness, "skill_fast_path_enabled", True):
            return None
        registry = self._get_skill_registry()
        if registry is None or not registry.all():
            return None
        try:
            from harness.skill.dispatch import maybe_run_skill_fast_path
            from harness.skill.health import default_health
            from harness.tools.browser_tools import _record_extraction
            outcome = await maybe_run_skill_fast_path(
                harness,
                registry=registry,
                worker_contract=worker_contract,
                phase=phase,
                task=task,
                context=context,
                fleet_ids=fleet_ids,
                record_extraction=_record_extraction,
                health=default_health(),
            )
        except Exception as exc:  # any failure → normal loop
            self.logger.write("skill.fast_path.error", {"error": str(exc)})
            return None
        return outcome

    def _record_row_ledger(
        self,
        harness: Any,
        *,
        trace_summary: JsonDict,
        worker_contract: JsonDict,
        phase: JsonDict,
        validation: JsonDict,
        worker_id: str,
        phase_id: str,
    ) -> List[JsonDict]:
        """Persist what happened to each assigned row, per field, from receipts.

        The Lead may narrate a run; it may not decide why a row came back
        empty. Without this record one row's login modal explains three rows'
        missing data and nothing in the system disagrees.
        """
        try:
            expected = phase.get("expected_artifact")
            expected = expected if isinstance(expected, dict) else {}
            identity_fields = _cohort_identity_fields(worker_contract, phase)
            fields = field_names_from_specs(
                expected.get("required_fields") or expected.get("fields") or []
            )
            if not fields:
                return []
            rows = _validated_rows_for_ledger(validation)
            row_keys = [
                key for key in (
                    row_identity(row, identity_fields) for row in rows
                ) if key
            ]
            # The budget that ran out is the WORKER's, not the global default:
            # a phase may override max_steps, and reading the default instead
            # means a worker that stopped at its own 15-step cap is compared
            # against 40, reports budgetExhausted=False, and every row it never
            # opened loses the one cause that explains it. That substitution is
            # the whole defect this ledger was built to prevent.
            worker_harness = getattr(getattr(harness, "runtime", None), "harness", None)
            max_steps = optional_int(
                getattr(worker_harness, "max_steps", None),
                0,
            ) or int(self.runtime.harness.worker_max_steps or 0)
            steps = int(trace_summary.get("steps") or 0)
            ledger = derive_row_ledger(
                rows,
                fields=fields,
                identity_fields=identity_fields,
                allow_empty_with_outcome=_allowance_from_validators(
                    phase.get("validators")
                ),
                row_facts=derive_row_facts(
                    getattr(harness, "trace", []) or [],
                    row_keys=row_keys,
                    budget_exhausted=bool(max_steps and steps >= max_steps),
                ),
            )
        except Exception as exc:  # a ledger defect must never fail a worker
            self.logger.write("row_ledger.error", {
                "workerId": worker_id, "phaseId": phase_id, "error": str(exc),
            })
            return []
        if ledger:
            self.logger.write("row_ledger.recorded", {
                "workerId": worker_id,
                "phaseId": phase_id,
                "rows": ledger,
            })
        return ledger

    async def _maybe_autoheal_skill(
        self,
        harness: Any,
        *,
        fast_path_handled: bool,
        slow_path_succeeded: bool,
        worker_contract: JsonDict,
        phase: JsonDict,
        task: str,
        context: str,
        fleet_ids: List[str],
    ) -> None:
        """Close the self-heal loop: if the fast path fell back but the slow path
        succeeded for a degraded skill, distill the trace → candidate → canary →
        promote. Best-effort; any error is swallowed (never affects the worker)."""
        if fast_path_handled or not slow_path_succeeded:
            return
        if not workflow_execution_enabled(self.runtime):
            return
        if not getattr(self.runtime.harness, "skill_auto_heal_enabled", True):
            return
        try:
            # 07-07: a directly forced skill takes health OUT of the loop —
            # dispatch stopped recording, and health-driven autoheal must not
            # fire either. A suite route is different: its four-dimensional
            # phase match is exact, so it remains eligible for health/autoheal.
            from harness.skill.contract import is_suite_routed
            from harness.skill.dispatch import _is_explicit_selection
            suite_routed = is_suite_routed(worker_contract or {})
            # An exact suite route is health-managed and may autoheal after a
            # degraded workflow falls back successfully. A direct force remains
            # outside both health accounting and health-driven autoheal.
            if not suite_routed and _is_explicit_selection(worker_contract or {}):
                return
        except Exception:  # pragma: no cover - guard must never break the worker
            return
        registry = self._get_skill_registry()
        if registry is None or not registry.all():
            return
        try:
            from harness.skill.autoheal import maybe_autoheal_from_trace
            from harness.skill.dispatch import resolve_skill_and_variables
            from harness.skill.health import default_health

            skill, canary_variables = resolve_skill_and_variables(
                registry, worker_contract, phase=phase, task=task, context=context,
                mode=str(getattr(self.runtime.harness, "skill_selection_mode", "manual") or "manual"),
            )
            if skill is None:
                return
            await maybe_autoheal_from_trace(
                harness,
                skill=skill,
                health=default_health(),
                trace=getattr(harness, "trace", []) or [],
                canary_variables=canary_variables,
                fleet_id=next(iter(fleet_ids), "") if fleet_ids else "",
            )
        except Exception as exc:  # self-heal must never break the worker
            self.logger.write("skill.autoheal.error", {"error": str(exc)})

    def _record_guidance_signal(
        self,
        *,
        worker_contract: JsonDict,
        fast_path_handled: bool,
        validated_ok: bool,
        steps: int,
        answer: str,
    ) -> None:
        """Guidance（hints）层的防腐弱信号：结局 + 步数 + answer 里的
        guidance_stale 上报 → skills/.guidance_health.json（独立软通道，只标
        needs_review 供人工复审，永不禁用）。record_guidance_outcome 只接受
        suite_routed；直接强制单个 guidance 不记。Best-effort，绝不影响结果。"""
        if not getattr(self.runtime.harness, "skill_guidance_signal_enabled", True):
            return
        try:
            from harness.skill.guidance import record_guidance_outcome
            record_guidance_outcome(
                self._get_skill_registry(),
                worker_contract,
                validated_ok=validated_ok,
                fast_path_handled=fast_path_handled,
                steps=steps,
                answer=answer,
                logger=self.logger,
            )
        except Exception as exc:  # weak signal must never break the worker
            self.logger.write("skill.guidance.signal_error", {"error": str(exc)})

    async def _run_browser_worker(
        self,
        slot: BrowserAgentSlot,
        registration: JsonDict,
        assignment: Optional[FleetAssignment],
        expose_reusable_pages: bool,
        worker_id: str,
        name: str,
        task: str,
        context: str,
        max_steps: Optional[int],
        result_contract: str,
        phase_id: Optional[str],
        worker_contract: JsonDict,
        phase: Optional[JsonDict],
        readiness_receipt: Optional[JsonDict] = None,
    ) -> JsonDict:
        worker_runtime = replace(
            self.runtime,
            agent_id=slot.agent_id,
            harness=replace(
                self.runtime.harness,
                max_steps=max_steps or self.runtime.harness.worker_max_steps,
            ),
        )
        provider = LLMFactory.create_provider(
            browser_agent_model_config(worker_runtime.model, worker_runtime.worker)
        )
        event_logger = make_browser_event_logger(
            self.logger,
            worker_runtime.harness.log_browser_payloads,
            prefix=f"{worker_id}.transport",
        )

        harness = None
        try:
            if slot.client is None:
                raise ABCPTransportError(f"Slot {slot.slot_id} has no browser client")
            slot.client.on_event = event_logger
            bundle = await self._capability_bundle_for_worker(
                slot.client,
                worker_runtime,
            )
            slot_context = self._render_slot_context(
                slot,
                expose_reusable_pages=expose_reusable_pages,
                assignment=assignment,
            )
            if readiness_receipt:
                slot_context = (
                    f"{slot_context}\n\n<fleet_readiness>\n"
                    f"{json.dumps(readiness_receipt, ensure_ascii=False, indent=2)}\n"
                    "</fleet_readiness>"
                )
            effective_context = context or "(none)"
            if slot_context:
                effective_context = f"{effective_context}\n\n{slot_context}".strip()
            try:
                from harness.skill.contract import selected_skill_context
                skill_context = selected_skill_context(
                    self._get_skill_registry(),
                    worker_contract or {},
                    workflow_enabled=workflow_execution_enabled(worker_runtime),
                )
            except Exception as exc:
                self.logger.write("skill.context.error", {"error": str(exc)})
                skill_context = ""
            if skill_context:
                effective_context = f"{effective_context}\n\n{skill_context}".strip()
            prompt_worker_contract = _prompt_worker_contract(worker_contract)
            worker_task = (
                f"BrowserAgent name: {name}\n"
                f"Independent context:\n{effective_context}\n\n"
                f"<worker_contract>\n"
                f"{json.dumps(prompt_worker_contract, ensure_ascii=False, indent=2, default=str)}\n"
                f"</worker_contract>\n\n"
                f"Result contract:\n{result_contract or 'Return a structured JSON string containing outcome, data, evidence, next_steps.'}\n\n"
                f"Assigned task:\n{task}"
            )
            owner_client = None
            if assignment is not None and assignment.delegated:
                owner_slot = self._slots.get(assignment.owner_slot_id)
                owner_client = owner_slot.client if owner_slot is not None else None
            worker_browser = _TaskContextTrackingBrowserClient(
                slot.client,
                self.page_lease_manager,
                fleet_owner_client=owner_client,
                fleet_click_gate=self.fleet_click_gate,
                fleet_auth_barrier=(
                    self.fleet_auth_barrier
                    if getattr(
                        self.runtime.harness,
                        "fleet_auth_barrier_enabled",
                        False,
                    )
                    else None
                ),
                assigned_fleet_id=assignment.fleet_id if assignment else "",
                registered_agent_id=slot.agent_id,
                worker_id=worker_id,
                after_call=(
                    (
                        lambda method, params, result: (
                            self._observe_task_browser_call(
                                slot,
                                assignment,
                                method,
                                params,
                                result,
                                phase_id=phase_id,
                            )
                        )
                    )
                    if assignment is not None
                    else None
                ),
            )
            worker_logger = self.logger.bind_context(
                workerId=worker_id,
                slotId=slot.slot_id,
                agentId=slot.agent_id,
                phaseId=str(phase_id or ""),
            )
            harness = self.browser_agent_factory(
                provider,
                worker_browser,
                worker_runtime,
                worker_logger,
            )
            try:
                harness_ref = weakref.ref(harness)
            except TypeError:
                worker_browser.set_click_settlement_classifier(
                    lambda method, params, current=harness: (
                        _fresh_click_settlement_class(
                            current, method, params
                        )
                    )
                )
                worker_browser.set_workflow_hitl_settlement_handler(
                    lambda page_id, current=harness: (
                        _verified_workflow_hitl_settlement(
                            current,
                            page_id,
                        )
                    )
                )
            else:
                worker_browser.set_click_settlement_classifier(
                    lambda method, params, ref=harness_ref: (
                        _fresh_click_settlement_class(ref(), method, params)
                    )
                )
                worker_browser.set_workflow_hitl_settlement_handler(
                    lambda page_id, ref=harness_ref: (
                        _verified_workflow_hitl_settlement(
                            ref(),
                            page_id,
                        )
                    )
                )
            harness.worker_contract = worker_contract or {}
            batch_rows = (
                harness.worker_contract.get("batch_rows")
                if isinstance(harness.worker_contract, dict)
                else None
            )
            progress = getattr(harness, "progress", None)
            if progress is not None and hasattr(
                progress, "configure_history_navigation_credits"
            ):
                progress.configure_history_navigation_credits(
                    len(batch_rows) if isinstance(batch_rows, list) else 0
                )
            harness.preloaded_registration = registration
            harness.preloaded_capability_bundle = bundle
            harness.assigned_fleet_id = assignment.fleet_id if assignment else ""
            harness.allowed_fleet_ids = set(
                assignment.allowed_fleet_ids if assignment else ()
            )
            page_bindings = self._page_bindings_for_worker(
                slot,
                assignment=assignment,
                expose_reusable_pages=expose_reusable_pages,
            )
            harness.allowed_page_ids = set(page_bindings)
            harness.page_fleet_ids = dict(page_bindings)
            self.page_lease_manager.seed_worker_pages(worker_id, page_bindings)
            harness.fleet_page_fleet_ids = {}
            harness.pinned_browser_context = (
                self.pinned_browser_context.to_dict()
                if self.pinned_browser_context is not None
                else {}
            )
            harness.pinned_page_id = (
                self.pinned_browser_context.page_id
                if self.pinned_browser_context is not None
                else ""
            )
            harness.fleet_assignment_reason = (
                assignment.assignment_reason if assignment else ""
            )
            harness.fleet_session_key = assignment.session_key if assignment else ""
            harness.fleet_session_generation = (
                assignment.session_generation if assignment else 0
            )
            harness.fleet_is_isolated = bool(
                assignment.is_isolated if assignment else False
            )
            harness.worker_id = worker_id
            harness.slot_id = slot.slot_id
            harness.phase_id = phase_id
            harness.page_lease_manager = self.page_lease_manager
            harness.fleet_click_gate = self.fleet_click_gate
            harness.fleet_auth_barrier = (
                self.fleet_auth_barrier
                if getattr(
                    self.runtime.harness,
                    "fleet_auth_barrier_enabled",
                    False,
                )
                else None
            )
            harness.fleet_barrier_generation = (
                self.fleet_auth_barrier.generation(assignment.fleet_id)
                if assignment is not None
                else 0
            )
            harness.fleet_reperception_pending = False
            harness.fleet_reperception_generation = (
                harness.fleet_barrier_generation
            )
            harness.fleet_reperception_state_seen = False
            harness.fleet_reperception_tree_seen = False
            harness.auth_session_verified_handler = (
                (lambda payload: self._record_verified_auth_session(
                    assignment, payload
                ))
                if assignment is not None and assignment.session_key
                else None
            )
            harness.auth_session_lost_handler = (
                self._handle_auth_session_lost
                if assignment is not None and assignment.session_key
                else None
            )
            skill_outcome = await self._try_skill_fast_path(
                harness,
                worker_contract=worker_contract or {},
                phase=phase or {},
                task=task,
                context=effective_context,
                fleet_ids=([assignment.fleet_id] if assignment else sorted(slot.fleet_ids)),
            )
            skill_answer = (
                skill_outcome.get("answer")
                if skill_outcome and skill_outcome.get("handled")
                else None
            )
            execution_metadata = _skill_execution_metadata(skill_outcome)
            if skill_answer is not None:
                answer = skill_answer
                harness.final_status = _effective_worker_status(
                    harness.final_status, skill_answer,
                )
            else:
                # A batch fast path that stopped mid-way hands its progress to the
                # slow path: completed rows are already persisted, the note says
                # which rows remain and how to merge into ONE final artifact.
                handoff_note = str((skill_outcome or {}).get("handoff_note") or "")
                if handoff_note:
                    repair_manifest = (skill_outcome or {}).get("repair_manifest")
                    if isinstance(repair_manifest, dict):
                        harness.worker_contract = {
                            **(harness.worker_contract or {}),
                            "_repair_manifest": dict(repair_manifest),
                        }
                    worker_task = (
                        f"{worker_task}\n\nSKILL FAST-PATH BATCH HANDOFF:\n{handoff_note}"
                    )
                answer = await harness.run(worker_task)
            execution_metadata = _finalize_skill_execution_metadata(
                execution_metadata, harness,
            )
            trace_path = self._write_worker_trace(worker_id, harness.trace)
            trace_summary = self._summarize_worker_trace(harness.trace)
            challenge_tracker = getattr(harness, "challenge_tracker", None)
            if challenge_tracker is not None and hasattr(challenge_tracker, "suspected_pages"):
                trace_summary["suspectedChallengePages"] = challenge_tracker.suspected_pages()
            completeness_tracker = getattr(
                harness, "content_completeness_tracker", None
            )
            if completeness_tracker is not None and hasattr(
                completeness_tracker, "summaries"
            ):
                trace_summary["contentCompletenessPages"] = (
                    completeness_tracker.summaries()
                )
            # "The worker never did X" and "this phase cannot do X" are
            # different facts, and only one of them is in the trace. In task
            # a608b5e7 a worker spent its whole budget on comments without
            # calling DOM.getImg once; the Lead read the absence as a
            # capability limit and replanned the image work into a
            # file_download phase, away from the page that had the images.
            advertised = getattr(harness, "capability_methods", None)
            if isinstance(advertised, (set, frozenset)):
                called = set(trace_summary.get("methods") or {})
                trace_summary["advertisedMethodsNeverCalled"] = sorted(
                    str(method) for method in advertised if method not in called
                )
            offloaded_files = trace_summary.pop("offloadedFiles", [])
            progress = getattr(harness, "progress", None)
            progress_snapshot = (
                progress.to_log_payload()
                if progress is not None
                else {}
            )
            artifact_validation = validate_worker_artifacts(
                contract=worker_contract,
                artifacts=harness.artifacts,
                attempt_artifacts=getattr(harness, "extraction_attempt_artifacts", []),
                prior_artifacts=phase_prior_artifact_paths(
                    self.logger,
                    phase_id=phase_id,
                    exclude_worker_id=worker_id,
                ),
                file_evidence=getattr(harness, "file_action_evidence", []),
                task_dir=self.logger.task_dir,
                logger=self.logger,
            )
            unresolved_visual = _unresolved_repair_visual_evidence(harness)
            if unresolved_visual:
                artifact_validation["status"] = "failed"
                failures = artifact_validation.get("failures")
                if not isinstance(failures, list):
                    failures = []
                    artifact_validation["failures"] = failures
                failures.append({
                    "type": "repair_absence_visual_evidence",
                    "message": (
                        "repair marked fields confirmed_absent but completed no"
                        " visual_verify before worker termination"
                    ),
                    "pending": unresolved_visual,
                })
            terminal_classification = classification_for_worker_status(
                harness.final_status
            )
            if terminal_classification is not None:
                artifact_validation["classification"] = terminal_classification
            elif artifact_validation.get("status") != "done":
                feedback_classification = _worker_feedback_classification(
                    harness.trace,
                    answer,
                    persisted_artifacts=[
                        *list(getattr(harness, "artifacts", []) or []),
                        *list(
                            getattr(harness, "extraction_attempt_artifacts", [])
                            or []
                        ),
                    ],
                )
                if feedback_classification is not None:
                    artifact_validation["classification"] = feedback_classification
                    counterevidence = feedback_classification.get(
                        "counterevidence"
                    )
                    if isinstance(counterevidence, dict):
                        self.logger.write(
                            "semantic_terminal.counterevidence",
                            {
                                "workerId": worker_id,
                                "phaseId": phase_id,
                                "category": feedback_classification.get(
                                    "category"
                                ),
                                **counterevidence,
                            },
                        )
            # Completeness observations remain model-visible evidence, but do
            # not override the artifact contract mechanically.
            contract_validation = json.loads(json.dumps(artifact_validation))
            content_completeness_validation: JsonDict = {
                "status": "observed",
                "classification": None,
            }
            validated_status = (
                "validated_done"
                if artifact_validation.get("status") == "done"
                else "validation_failed"
                if artifact_validation.get("status") == "failed"
                else "not_validated"
            )
            fast_path_assessment = assess_fast_path_candidate(
                trace=getattr(harness, "trace", []) or [],
                trace_summary=trace_summary,
                worker_contract=worker_contract or {},
                phase=phase or {},
                validation=artifact_validation,
            )
            row_ledger = self._record_row_ledger(
                harness,
                trace_summary=trace_summary,
                worker_contract=worker_contract or {},
                phase=phase or {},
                validation=artifact_validation,
                worker_id=worker_id,
                phase_id=phase_id,
            )
            # Self-heal loop: the fast path fell back (skill_answer is None) but the
            # slow path produced a validated result — distill its trace into a
            # candidate workflow and canary-promote it for the degraded skill.
            await self._maybe_autoheal_skill(
                harness,
                fast_path_handled=skill_answer is not None,
                slow_path_succeeded=validated_status == "validated_done",
                worker_contract=worker_contract or {},
                phase=phase or {},
                task=task,
                context=effective_context,
                fleet_ids=([assignment.fleet_id] if assignment else sorted(slot.fleet_ids)),
            )
            self._record_guidance_signal(
                worker_contract=worker_contract or {},
                fast_path_handled=skill_answer is not None,
                validated_ok=validated_status == "validated_done",
                steps=int(trace_summary.get("toolCalls") or 0),
                answer=str(answer or ""),
            )
            diagnostics = getattr(harness, "diagnostics", None)
            captcha_receipts = list(
                getattr(harness, "captcha_autosolve_receipts", []) or []
            )
            vl_cleared_statuses = {
                "solved", "cleared", "not_a_challenge", "already_cleared",
            }
            hitl_request_count = sum(
                1 for item in (getattr(harness, "trace", []) or [])
                if isinstance(item, dict)
                and item.get("type") == "browser_call"
                and item.get("method") == "Hitl.requestPause"
            )
            challenge_receipt = {
                "observed": bool(
                    trace_summary.get("suspectedChallengePages")
                    or captcha_receipts
                    or hitl_request_count
                ),
                "observedCount": max(
                    len(trace_summary.get("suspectedChallengePages") or []),
                    len(captcha_receipts),
                    int(bool(hitl_request_count)),
                ),
                "vlSolveAttempts": sum(
                    len(item.get("attempts") or [])
                    for item in captcha_receipts if isinstance(item, dict)
                ),
                "vlSolvedCount": sum(
                    1 for item in captcha_receipts
                    if isinstance(item, dict)
                    and str(item.get("status") or "") in vl_cleared_statuses
                ),
                "hitlRequests": hitl_request_count,
                "hitlResumes": int(bool(
                    getattr(diagnostics, "hitl_resumed_observed", False)
                )),
            }
            challenge_receipt["unresolved"] = bool(
                challenge_receipt["observed"]
                and not challenge_receipt["vlSolvedCount"]
                and not challenge_receipt["hitlResumes"]
            )
            result = {
                "status": harness.final_status,
                "statusCategory": status_category(harness.final_status),
                "validatedStatus": validated_status,
                **execution_metadata,
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "name": name,
                "phaseId": phase_id,
                "answer": answer,
                "artifacts": harness.artifacts,
                "extractionAttemptArtifacts": getattr(
                    harness,
                    "extraction_attempt_artifacts",
                    [],
                ),
                "artifactValidation": artifact_validation,
                "contractValidation": contract_validation,
                "contentCompletenessValidation": content_completeness_validation,
                "finalArtifactValidation": artifact_validation,
                "tracePath": trace_path,
                "traceSummary": trace_summary,
                "progressSnapshot": progress_snapshot,
                "progressObservationCount": progress_snapshot.get(
                    "observationCount",
                    0,
                ),
                "offloadedFiles": offloaded_files,
                "diagnostics": diagnostics.to_log_payload()
                if diagnostics is not None
                else {},
                "fastPathAssessment": fast_path_assessment,
                "downloadOperationReceipts": list(
                    getattr(harness, "download_operation_receipts", {}).values()
                ),
                "challengeReceipt": challenge_receipt,
                # Per-row outcome and cause, derived from receipts. The Lead
                # reads this instead of inferring one explanation for every row
                # from the worker's prose.
                "rowLedger": row_ledger,
            }
            receipt_candidate = fast_path_assessment.get("candidate")
            if isinstance(receipt_candidate, dict):
                result["fastPathReceiptCandidate"] = receipt_candidate
            if assignment is not None:
                result["fleetAssignment"] = assignment.to_dict()
            self._update_slot_after_worker(
                slot,
                worker_id=worker_id,
                phase_id=phase_id,
                worker_contract=worker_contract,
                result=result,
                trace=getattr(harness, "trace", []),
            )
        except asyncio.CancelledError:
            harness_obj = harness
            trace = (
                getattr(harness_obj, "trace", [])
                if harness_obj is not None
                else []
            )
            result = {
                "status": WORKER_STATUS_CANCELLED,
                "statusCategory": status_category(WORKER_STATUS_CANCELLED),
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "name": name,
                "phaseId": phase_id,
            }
            if isinstance(assignment, FleetAssignment):
                result["fleetAssignment"] = assignment.to_dict()
            result = self._prepare_worker_result(
                result,
                worker_id=worker_id,
                agent_id=slot.agent_id,
                phase_id=phase_id,
            )
            self.logger.write(
                "spawner.browser.result",
                trim_large_strings(result, 8000),
            )
            self._record_slot_result(
                slot,
                worker_id=worker_id,
                phase_id=phase_id,
                worker_contract=worker_contract,
                result=result,
                trace=trace,
            )
            self._mark_slot_idle(slot, worker_id=worker_id)
            self._remove_notification_relay_for_assignment(assignment)
            await self.fleet_auth_barrier.abandon_worker(worker_id)
            mark_phase_result(
                self.logger,
                phase_id=phase_id,
                worker_id=worker_id,
                validation=None,
                result_status=WORKER_STATUS_CANCELLED,
                phase=phase,
                worker_contract=worker_contract,
            )
            raise
        except Exception as exc:
            harness_obj = harness
            trace = (
                getattr(harness_obj, "trace", [])
                if harness_obj is not None
                else []
            )
            if harness_obj is not None:
                self._update_slot_registry_from_trace(
                    slot,
                    trace,
                )
            if isinstance(exc, ABCPTransportError):
                slot.status = "broken"
                self.fleet_coordinator.mark_slot_suspect(slot.slot_id)
                slot.sync_errors.append(str(exc)[:500])
                if slot.client is not None:
                    await slot.client.close()
                    slot.client = None
            result = {
                "status": WORKER_STATUS_FAILED,
                "statusCategory": status_category(WORKER_STATUS_FAILED),
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "name": name,
                "phaseId": phase_id,
                "error": str(exc),
            }
            self._record_slot_result(
                slot,
                worker_id=worker_id,
                phase_id=phase_id,
                worker_contract=worker_contract,
                result=result,
                trace=None,
            )
            self._mark_slot_idle(slot, worker_id=worker_id)

        self._remove_notification_relay_for_assignment(assignment)
        await self.fleet_auth_barrier.abandon_worker(worker_id)
        if isinstance(assignment, FleetAssignment):
            result.setdefault("fleetAssignment", assignment.to_dict())
        result = self._prepare_worker_result(
            result,
            worker_id=worker_id,
            agent_id=slot.agent_id,
            phase_id=phase_id,
        )
        attempt_digest = build_attempt_digest(
            result,
            phase=phase or {},
            worker_contract=worker_contract or {},
            task=task,
            result_contract=result_contract,
        )
        handoff = build_worker_handoff_projection(
            result,
            original_goal=str((phase or {}).get("objective") or ""),
        )
        if isinstance(handoff, dict):
            state_before_result = load_task_state(self.logger)
            phase_state = (
                (state_before_result.get("phases") or {}).get(str(phase_id or ""))
                if isinstance(state_before_result.get("phases"), dict)
                else None
            )
            prior_attempts = (
                phase_state.get("attempts")
                if isinstance(phase_state, dict)
                and isinstance(phase_state.get("attempts"), list)
                else []
            )
            prior_rows = [
                int((item.get("attemptDigest") or {}).get("rowCount") or 0)
                for item in prior_attempts
                if isinstance(item, dict)
                and item.get("workerId") != worker_id
                and isinstance(item.get("attemptDigest"), dict)
            ]
            current_rows = int(attempt_digest.get("rowCount") or 0)
            receipts = handoff.setdefault("rawReceipts", {})
            receipts["attemptCount"] = len(prior_attempts)
            receipts["previousRowCount"] = prior_rows[-1] if prior_rows else None
            receipts["rowCountDelta"] = (
                current_rows - prior_rows[-1] if prior_rows else current_rows
            )
            handoff.setdefault("evidencePaths", {})[
                "strategyAttempts"
            ] = str(self.logger.task_dir / "strategy_attempts.jsonl")
            attempt_digest["handoff"] = handoff
        result["attemptDigest"] = attempt_digest
        mark_phase_result(
            self.logger,
            phase_id=phase_id,
            worker_id=worker_id,
            validation=result.get("artifactValidation"),
            # Lifecycle truth must use the worker's raw outcome.  The derived
            # validatedStatus is a separate artifact dimension and must never
            # turn a partial worker into a completed phase.
            result_status=str(result.get("status") or "unknown"),
            attempt_digest=attempt_digest,
            phase=phase,
            worker_contract=worker_contract,
        )
        checkpoint = record_replan_checkpoint(
            self.logger,
            phase=phase,
            worker_contract=worker_contract,
            worker_id=worker_id,
            fast_path_assessment=(
                result.get("fastPathAssessment")
                if isinstance(result.get("fastPathAssessment"), dict)
                else None
            ),
        )
        if isinstance(checkpoint, dict):
            result["replanCheckpoint"] = checkpoint
            checkpoint_assessment = checkpoint.get("fastPathAssessment")
            if isinstance(checkpoint_assessment, dict):
                result["fastPathAssessment"] = checkpoint_assessment
                receipt_candidate = checkpoint_assessment.get("candidate")
                if isinstance(receipt_candidate, dict):
                    result["fastPathReceiptCandidate"] = receipt_candidate
                else:
                    result.pop("fastPathReceiptCandidate", None)
        append_strategy_attempt(
            logger=self.logger,
            worker_contract=worker_contract or {},
            result=result,
        )
        if slot.status == "running":
            self._mark_slot_idle(slot, worker_id=worker_id)
        self.logger.write("spawner.browser.result", trim_large_strings(result, 8000))
        return result

    def _prepare_worker_result(
        self,
        result: JsonDict,
        *,
        worker_id: str,
        agent_id: str,
        phase_id: Optional[str],
    ) -> JsonDict:
        result = self._attach_worker_result_levels(result)
        return self.lifecycle.worker_before_return(
            LifecycleContext(
                actor="browser_worker",
                metadata={
                    "worker_id": worker_id,
                    "agent_id": agent_id,
                    "phase_id": phase_id,
                },
            ),
            result,
        )

    async def _capability_bundle_for_worker(
        self,
        browser: ABCPClient,
        worker_runtime: RuntimeConfig,
    ) -> CapabilityBundle:
        if self._capability_bundle_lock is None:
            self._capability_bundle_lock = asyncio.Lock()
        async with self._capability_bundle_lock:
            if self._capability_bundle is not None:
                self.logger.write(
                    "schema.bundle.reused",
                    {
                        "capability_count": len(self._capability_bundle.capability_methods),
                        "schema_count": len(self._capability_bundle.method_schemas),
                    },
                )
                return _clone_capability_bundle(self._capability_bundle)
            bundle = await load_capability_bundle(
                browser,
                logger=self.logger,
                blocked_methods=ALWAYS_FORBIDDEN_ABCP_METHODS,
                schema_cache_dir=global_schemas_dir(worker_runtime.harness.worktree_dir),
            )
            self._capability_bundle = _clone_capability_bundle(bundle)
            return _clone_capability_bundle(bundle)

    def _attach_worker_result_levels(self, result: JsonDict) -> JsonDict:
        if result.get("resultLevels"):
            return result
        status = str(result.get("status") or "unknown")
        levels = build_worker_result_levels(
            status=status,
            status_category=str(result.get("statusCategory") or status_category(status)),
            validated_status=str(result.get("validatedStatus") or "not_validated"),
            worker_id=str(result.get("workerId") or ""),
            agent_id=str(result.get("agentId") or ""),
            name=str(result.get("name") or ""),
            phase_id=(
                str(result.get("phaseId"))
                if result.get("phaseId") is not None
                else None
            ),
            answer=str(result.get("answer") or ""),
            artifacts=_safe_str_list(result.get("artifacts")),
            extraction_attempt_artifacts=_safe_str_list(
                result.get("extractionAttemptArtifacts")
            ),
            row_ledger=(
                result.get("rowLedger")
                if isinstance(result.get("rowLedger"), list)
                else None
            ),
            artifact_validation=(
                result.get("artifactValidation")
                if isinstance(result.get("artifactValidation"), dict)
                else {}
            ),
            trace_path=str(result.get("tracePath") or ""),
            trace_summary=(
                result.get("traceSummary")
                if isinstance(result.get("traceSummary"), dict)
                else {}
            ),
            progress_snapshot=(
                result.get("progressSnapshot")
                if isinstance(result.get("progressSnapshot"), dict)
                else {}
            ),
            offloaded_files=_safe_str_list(result.get("offloadedFiles")),
            diagnostics=(
                result.get("diagnostics")
                if isinstance(result.get("diagnostics"), dict)
                else {}
            ),
            task_dir=getattr(self.logger, "task_dir", None),
            logger=self.logger,
        )
        result["resultLevels"] = levels
        result["workerResultProtocol"] = "L1/L2/L3"
        return result

    def _write_worker_trace(self, worker_id: str, trace: List[JsonDict]) -> str:
        """Persist one worker's trace and return where a reader can find it.

        This used to truncate the file, which silently destroyed the earlier
        trace whenever a resumed run reissued the same worker id - the ids come
        from a per-run counter, so browser-001 recurs on every resume. Writes
        now append, and the database backend scopes them by run so the two
        attempts stay separable rather than one overwriting the other.
        """

        safe_worker = safe_path_component(worker_id)
        storage, task_id = storage_for_logger(self.logger)
        storage.append_worker_trace(
            task_id=task_id,
            run_id=str(getattr(self.logger, "run_id", "") or ""),
            worker_id=safe_worker,
            entries=trace,
        )
        return str((task_subdir(self.logger, "traces") / f"{safe_worker}.jsonl").resolve())

    def _summarize_worker_trace(self, trace: List[JsonDict]) -> JsonDict:
        method_counts: Dict[str, int] = {}
        errors: List[str] = []
        page_ids: Set[str] = set()
        offloaded: List[str] = []
        progress_observations: List[JsonDict] = []
        loop_nudges: List[JsonDict] = []
        page_stats_events: List[JsonDict] = []
        snapshot_diffs: List[JsonDict] = []
        tool_calls = 0
        max_step = 0
        for item in trace:
            if not isinstance(item, dict):
                continue
            max_step = max(max_step, optional_int(item.get("step"), 0) or 0)
            if item.get("type") == "browser_call":
                tool_calls += 1
                method = str(item.get("method") or "unknown")
                method_counts[method] = method_counts.get(method, 0) + 1
                result = item.get("result")
                offloaded.extend(extract_offloaded_paths(result))
                page_id = extract_page_id_from_values(item.get("params"), result)
                if page_id:
                    page_ids.add(page_id)
                if isinstance(result, dict):
                    if result.get("error"):
                        errors.append(str(result.get("error"))[:500])
                    response = result.get("response")
                    if isinstance(response, dict) and response.get("error"):
                        errors.append(str(response.get("error"))[:500])
            elif item.get("type") in {
                "tool_error",
                "browser_call_params_error",
                "browser_call_rejected",
            }:
                error = get_path(item, "result.error")
                if error:
                    errors.append(str(error)[:500])
            elif item.get("type") == "progress_observation":
                result = item.get("result")
                if isinstance(result, dict):
                    progress_observations.append({
                        "source": str(result.get("source") or "")[:120],
                        "reasonObserved": str(
                            result.get("reasonObserved") or ""
                        )[:120],
                        "tool": str(result.get("tool") or "")[:120],
                        **{
                            key: result[key]
                            for key in (
                                "turnsSinceArtifactProgress",
                                "toolCalls",
                                "localFsWithoutExtraction",
                                "localFsStreak",
                                "diagnosticUses",
                                "diagnosticLimit",
                            )
                            if key in result
                        },
                    })
            elif item.get("type") == "loop_nudge":
                result = item.get("result")
                if isinstance(result, dict):
                    loop_nudges.append({
                        "reason": str(result.get("reason") or "")[:120],
                        "action": str(result.get("action") or "")[:120],
                        "repeatCount": optional_int(result.get("repeatCount"), 0) or 0,
                        "pageStalledFor": optional_int(result.get("pageStalledFor"), 0) or 0,
                    })
            elif item.get("type") == "page_stats":
                result = item.get("result")
                if isinstance(result, dict):
                    page_stats_events.append({
                        "step": optional_int(item.get("step"), 0) or 0,
                        "pageId": str(result.get("pageId") or "")[:120],
                        "url": str(result.get("url") or "")[:240],
                        "title": str(result.get("title") or "")[:160],
                        "nodes": optional_int(result.get("nodes"), 0) or 0,
                        "actionable": optional_int(result.get("actionable"), 0) or 0,
                        "semanticItems": optional_int(result.get("semanticItems"), 0) or 0,
                        "links": optional_int(result.get("links"), 0) or 0,
                        "hint": str(result.get("hint") or "")[:240],
                    })
            elif item.get("type") == "snapshot_diff":
                result = item.get("result")
                if isinstance(result, dict):
                    snapshot_diffs.append({
                        "fromStep": optional_int(result.get("fromStep"), 0) or 0,
                        "toStep": optional_int(result.get("toStep"), 0) or 0,
                        "crossPageDiff": bool(result.get("crossPageDiff")),
                        "semanticAdded": optional_int(result.get("semanticAdded"), 0) or 0,
                        "semanticRemoved": optional_int(result.get("semanticRemoved"), 0) or 0,
                        "physicalAdded": optional_int(result.get("physicalAdded"), 0) or 0,
                        "physicalRemoved": optional_int(result.get("physicalRemoved"), 0) or 0,
                        "totalNodeDelta": optional_int(result.get("totalNodeDelta"), 0) or 0,
                        "semanticChanged": bool(result.get("semanticChanged")),
                        "physicalChanged": bool(result.get("physicalChanged")),
                    })
        loop_nudge_count = len(loop_nudges)
        summary = {
            "steps": max_step,
            "traceEvents": len(trace),
            "toolCalls": tool_calls,
            "methods": method_counts,
            "pageIds": sorted(page_ids),
            "errors": errors[:10],
            "progressObservations": progress_observations[-5:],
            "progressObservationCount": len(progress_observations),
            "loopNudges": loop_nudges[-5:],
            "loopNudgeCount": loop_nudge_count,
            "latestPageStats": page_stats_events[-1] if page_stats_events else None,
            "pageStatsCount": len(page_stats_events),
            "snapshotDiffs": snapshot_diffs[-5:],
            "snapshotDiffCount": len(snapshot_diffs),
            "offloadedFiles": sorted(set(offloaded))[:100],
        }
        return summary

    def _select_handles(self, worker_ids: Optional[List[str]]) -> List[BrowserAgentHandle]:
        if not worker_ids:
            return list(self._handles.values())
        return [
            self._handles[worker_id]
            for worker_id in worker_ids
            if worker_id in self._handles
        ]

    def _task_result(self, handle: BrowserAgentHandle) -> JsonDict:
        try:
            return handle.async_task.result()
        except asyncio.CancelledError:
            return self._prepare_worker_result(
                {
                    "status": WORKER_STATUS_CANCELLED,
                    "statusCategory": status_category(WORKER_STATUS_CANCELLED),
                    "workerId": handle.worker_id,
                    "agentId": handle.agent_id,
                    "slotId": handle.slot_id,
                    "name": handle.name,
                    "phaseId": handle.phase_id,
                },
                worker_id=handle.worker_id,
                agent_id=handle.agent_id,
                phase_id=handle.phase_id,
            )
        except Exception as exc:
            return self._prepare_worker_result(
                {
                    "status": WORKER_STATUS_FAILED,
                    "statusCategory": status_category(WORKER_STATUS_FAILED),
                    "workerId": handle.worker_id,
                    "agentId": handle.agent_id,
                    "slotId": handle.slot_id,
                    "name": handle.name,
                    "phaseId": handle.phase_id,
                    "error": str(exc),
                },
                worker_id=handle.worker_id,
                agent_id=handle.agent_id,
                phase_id=handle.phase_id,
            )

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:03d}"

    def _next_slot_id(self) -> str:
        self._slot_counter += 1
        return f"slot-{self._slot_counter:03d}"
