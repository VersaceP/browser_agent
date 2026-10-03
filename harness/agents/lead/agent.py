"""LeadAgent state and role-specific coordination."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Set
from runtime_config import ClaimExtractorConfig, RuntimeConfig
from harness.runtime.lifecycle import default_lifecycle_manager
from harness.runtime.model_config import lead_agent_model_config
from harness.planning.pacing import merge_pacing
from harness.capabilities.schema_cache import SchemaCacheStatus, global_schema_cache_dir, global_schemas_dir, read_cached_capability_hash, read_schema_methods_from_dirs
from harness.spawner import BrowserAgentSpawner, PinnedBrowserContext
from harness.task_control import active_replan_checkpoints, find_phase, load_task_state, mark_phase_exhausted_if_needed, schedule_snapshot, phase_contract, phase_start_rejection, reconcile_replan_checkpoints, replan_checkpoint_plan_errors, validate_task_plan, accept_task_plan, write_task_state
from harness.planning.validator import plan_candidate_changed_paths, plan_candidate_hash, plan_candidate_identity, plan_candidate_payload, plan_hash, plan_replan_reason, assignment_review_input, review_assignment, write_plan_review_audit
from harness.tools.tool_policy import HARNESS_TOOL_NAMES, filter_capability_methods, capability_policy_facts
from harness.tools.browser_tools import build_browser_agent_tool_specs
from harness.workflow.workflow_runtime import workflow_execution_enabled
from harness.utils import JsonDict, RunLogger, build_static_context_block
from llm import BaseLLMProvider, LLMFactory
from harness.runtime.model_support import (
    CachePressureState,
    _lifecycle_recorder_for,
    update_cache_pressure_state,
)
from harness.runtime.resume_context import (
    ResumeContext,
)
from harness.agents.browser.agent import (
    BrowserAgent,
)

PLAN_VALIDATOR_ERROR_CACHE_TTL_SECONDS = 90.0


PLAN_VALIDATOR_PROTOCOL_ERROR_CACHE_TTL_SECONDS = 15.0


def _assignment_prefix_errors(
    raw_plan: Any,
    accepted_plan: Any,
) -> List[str]:
    """One appended assignment; accepted execution records never change in place."""
    if not isinstance(raw_plan, dict) or raw_plan.get("execution_mode") != "delegated":
        return ["expected a delegated assignment ledger"]
    before = (accepted_plan or {}).get("phases") or []
    after = raw_plan.get("phases")
    if not isinstance(after, list) or len(after) != len(before) + 1:
        return ["submit exactly one new assignment"]
    if not isinstance(accepted_plan, dict):
        return []
    accepted_phases = accepted_plan.get("phases")
    candidate_phases = (
        raw_plan.get("phases") if isinstance(raw_plan, dict) else None
    )
    if (
        not isinstance(accepted_phases, list)
        or not isinstance(candidate_phases, list)
    ):
        return [
            "extension must preserve the accepted phases as an unchanged prefix"
        ]
    if len(candidate_phases) < len(accepted_phases):
        return ["extension removed one or more accepted phases"]
    for index, accepted_phase in enumerate(accepted_phases):
        if candidate_phases[index] != accepted_phase:
            phase_id = (
                str(accepted_phase.get("id") or "").strip()
                if isinstance(accepted_phase, dict)
                else ""
            )
            suffix = f" {phase_id!r}" if phase_id else f" at index {index}"
            return [f"extension modified accepted phase{suffix}"]
    return []


class LeadAgent:
    """Lead agent that decomposes work and spawns isolated browser agents."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        runtime: RuntimeConfig,
        logger: RunLogger,
        pinned_browser_context: Any = None,
        plan_validator_provider: Optional[BaseLLMProvider] = None,
        resume: Optional[ResumeContext] = None,
        plan_approval_handler: Any = None,
        task_fleet_reference: str = "",
    ):
        self.provider = provider
        self.runtime = runtime
        self.effective_model_config = lead_agent_model_config(runtime)
        self.logger = logger
        source_facts, _ = logger.storage.load_snapshot(
            task_id=logger.task_id, snapshot_key="lead_source_evidence"
        )
        self._source_read_facts = list(source_facts.get("reads") or [])[-16:]
        self._source_search_facts = list(source_facts.get("searches") or [])[-16:]
        self.resume = resume
        # This is parsed once from the immutable original user task.  It is
        # control-plane state, not a plan field or a model tool argument.
        self.task_fleet_reference = str(task_fleet_reference or "").strip()
        self.plan_approval_handler = plan_approval_handler
        self._user_approved_plan_hash = ""
        self._pending_plan_approval_hash = ""
        self._operator_revision_requested_hash = ""
        self._plan_execution_cancelled = False
        self._accepted_task_plan_replan_reason = ""
        self._last_reviewed_plan_candidate: Optional[JsonDict] = None
        self._last_reviewed_plan_candidate_hash = ""
        self._last_reviewed_plan_replan_reason = ""
        self.spawner = BrowserAgentSpawner(
            runtime,
            logger,
            browser_agent_factory=BrowserAgent,
            pinned_browser_context=pinned_browser_context,
            resume_browser_hint=(
                resume.browser_hint if resume is not None else None
            ),
        )
        self.pinned_browser_context = PinnedBrowserContext.from_value(
            pinned_browser_context
        )
        self.static_context_block, self.static_context_hash = build_static_context_block(
            self.runtime.harness.context_file,
            project_context_files=getattr(
                self.runtime.harness, "project_context_files", None,
            ),
            append_system_prompt=getattr(
                self.runtime.harness, "append_system_prompt", None,
            ),
        )
        self.lifecycle = default_lifecycle_manager()
        self.lifecycle_events = _lifecycle_recorder_for(
            runtime, logger, actor_type="lead",
        )
        self.task_plan: Optional[JsonDict] = (
            dict(resume.current_plan) if resume is not None else None
        )
        # A normalized executable plan deliberately excludes `replan_reason`,
        # but the reason is part of the candidate the operator approved.  Read
        # that durable companion only when it belongs to this exact plan body.
        if isinstance(self.task_plan, dict):
            approval_state = load_task_state(self.logger)
            stored_plan_matches = (
                str(approval_state.get("plan_hash") or "") == plan_hash(self.task_plan)
            )
            if stored_plan_matches:
                self._accepted_task_plan_replan_reason = str(
                    approval_state.get("plan_replan_reason") or ""
                ).strip()
            if self.plan_approval_handler is not None:
                current_hash = plan_candidate_hash(
                    self.task_plan,
                    self._accepted_task_plan_replan_reason,
                )
                approval = approval_state.get("plan_user_approval")
                if (
                    stored_plan_matches
                    and isinstance(approval, dict)
                    and str(approval.get("candidateHash") or "") == current_hash
                ):
                    self._user_approved_plan_hash = current_hash
        self.original_user_task: str = ""
        # Stable terminal metadata for in-process hosts such as the ABCP user
        # panel. The public run() return type remains str for compatibility.
        self.final_status: str = ""
        self.final_trigger: str = ""
        self.terminal_error: Optional[JsonDict] = None
        self._resume_instruction_pending = bool(
            resume is not None and str(resume.instruction or "").strip()
            )
        validator_config = self.runtime.plan_validator
        self.plan_validator_provider: Optional[BaseLLMProvider] = None
        if validator_config.enabled:
            if not validator_config.model_id:
                raise ValueError(
                    "plan_validator.enabled requires plan_validator.model_id"
                )
            if (
                validator_config.model_id.strip().lower()
                == self.effective_model_config.model_id.strip().lower()
            ):
                raise ValueError(
                    "plan_validator.model_id must differ from the Lead model"
                )
            self.plan_validator_provider = (
                plan_validator_provider
                or LLMFactory.create_provider(validator_config.model_config())
            )
        # Numeric claim extraction is a read-only observation, so it reuses the
        # independent-auditor slot rather than introducing a second key: a
        # dedicated claim_extractor section when configured, otherwise whatever
        # already audits plans. Both must differ from the Lead model — a model
        # confirming its own prose is not an independent reading of it.
        extractor_config = self.runtime.claim_extractor
        self.claim_extractor_provider: Optional[BaseLLMProvider] = None
        self.claim_extractor_model: str = ""
        self.claim_extractor_provider_name: str = ""
        if extractor_config.enabled and extractor_config.model_id:
            if (
                extractor_config.model_id.strip().lower()
                == self.effective_model_config.model_id.strip().lower()
            ):
                raise ValueError(
                    "claim_extractor.model_id must differ from the Lead model"
                )
            self.claim_extractor_provider = LLMFactory.create_provider(
                extractor_config.model_config()
            )
            self.claim_extractor_model = extractor_config.model_id
            self.claim_extractor_provider_name = extractor_config.provider
        elif (
            plan_validator_provider is None
            and validator_config.enabled
            and validator_config.model_id
        ):
            # Same auditor model and credentials, its own connection: sharing
            # the provider object also shared `thinking`/`effort` and the
            # validator's output budget, which is a plan-review setting and
            # wrong for a span-to-metric lookup. Only possible when this
            # constructor built the validator from config — an injected
            # provider is already parameterized and cannot be re-derived.
            derived = ClaimExtractorConfig.derived_from(validator_config)
            self.claim_extractor_provider = LLMFactory.create_provider(
                derived.model_config()
            )
            self.claim_extractor_model = derived.model_id
            self.claim_extractor_provider_name = derived.provider
        elif self.plan_validator_provider is not None:
            self.claim_extractor_provider = self.plan_validator_provider
            self.claim_extractor_model = validator_config.model_id
            self.claim_extractor_provider_name = validator_config.provider
        self.recent_tool_signatures: List[str] = []
        # Keep only the latest mechanically invalid plan. It is a short-lived
        # repair base, never accepted plan state: the model may patch it after a
        # repeated full-plan emission proves that regenerating the large object
        # is not changing its actual tool arguments.
        self._current_step: int = 0
        self._cache_pressure = CachePressureState()
        self._forced_compaction_reason: Optional[str] = None
        # Set True when THIS run's schema bootstrap could not (re)build the cache
        # (no browser, empty capabilities, lock timeout, exception). A stale local
        # cache may still exist on disk, but it cannot be trusted for the strict
        # unknown-method check this run, so plan validation degrades to skip it.
        self._schema_bootstrap_degraded: bool = False

    def _compile_assignment_candidate(self, raw_plan):
        """Validate the append-only execution ledger and runtime-owned identity."""
        from harness.planning.context import user_context
        prefix_errors = _assignment_prefix_errors(raw_plan, self.task_plan)
        if prefix_errors:
            return None, prefix_errors, [], []
        schema_status, methods = self._schema_cache_status()
        repair_issues, facts = [], []
        plan, errors = validate_task_plan(
            raw_plan, collection_facts=facts,
            known_abcp_methods=methods if schema_status == SchemaCacheStatus.LOADED_OK else None,
            known_harness_tools=HARNESS_TOOL_NAMES,
            user_task=json.dumps(user_context(self.logger, self.original_user_task), ensure_ascii=False),
            repair_issues=repair_issues)
        if plan is not None:
            errors = _assignment_prefix_errors(plan, self.task_plan)
            phase = plan["phases"][-1]
            meta = (phase.get("worker_contract") or {}).get("_delegation") or {}
            replaces = meta.get("replaces")
            previous = find_phase(self.task_plan, replaces) if replaces else None
            state = load_task_state(self.logger)
            prior = (state.get("phases") or {}).get(replaces, {})
            if meta.get("id") != phase["id"]:
                errors.append("assignment identity must match its runtime ledger id")
            if replaces and (previous is None or not str(meta.get("reason") or "").strip()):
                errors.append("revision requires an existing predecessor and reason")
            if replaces and (prior.get("status") == "running" or prior.get("superseded_by")):
                errors.append("cannot replace a live or already superseded assignment")
            expected_lineage = (((previous or {}).get("worker_contract") or {}).get("_delegation") or {}).get("lineage") or replaces or phase["id"]
            if meta.get("lineage") != expected_lineage:
                errors.append("revision must preserve its predecessor's budget lineage")
            if errors:
                plan = None
        return plan, errors, repair_issues, facts

    def _assignment_review_input(self, candidate, reason, state, facts):
        from harness.planning.context import user_context
        visible_methods = filter_capability_methods(
            getattr(self, "capability_methods", set())
        )
        workflow_enabled = workflow_execution_enabled(self)
        if not workflow_enabled:
            visible_methods.discard("Workflow.execute")
        available_tools = build_browser_agent_tool_specs(
            visible_methods,
            workflow_enabled=workflow_enabled,
            selected_skill_available=bool(
                getattr(self.runtime.harness, "forced_skill_id", "") and
                getattr(self.runtime.harness, "forced_skill_hash", "")),
            step_extension_enabled=bool(
                self.runtime.harness.browser_agent_step_extension_enabled
            ),
            multimodal_enabled=bool(
                self.runtime.harness.browser_agent_multimodal_enabled
            ),
        )
        return assignment_review_input(
            context=user_context(self.logger, self.original_user_task, state=state),
            previous_plan=self.task_plan, candidate_plan=candidate,
            replan_reason=reason, task_state=state, logger=self.logger,
            collection_facts=facts,
            source_read_facts=getattr(self, "_source_read_facts", ()),
            source_search_facts=getattr(self, "_source_search_facts", ()),
            runtime_capabilities={
                "availableBrowserMethods": sorted(visible_methods),
                "availableHarnessTools": sorted(
                    str(spec.get("name")) for spec in available_tools
                    if spec.get("name")
                ),
                "localFileWriter": "local_fs_batch writes text/JSON to task or desktop paths after path authorization",
                "pageImageExporter": "DOM.getImg exports page images when advertised by WebCross",
            },
            runtime_limits={
                "defaultWorkerMaxSteps": self.runtime.harness.worker_max_steps,
                "maxBrowserAgents": self.runtime.harness.max_browser_agents,
            })

    async def review_assignment_candidate(self, raw_plan):
        candidate, errors, repair_issues, facts = self._compile_assignment_candidate(raw_plan)
        if candidate is None:
            return {"status": "mechanical_invalid", "errors": errors, "repairIssues": repair_issues}
        reason = plan_replan_reason(raw_plan)
        identity = plan_candidate_identity(candidate, reason)
        self._last_reviewed_plan_candidate = copy.deepcopy(candidate)
        self._last_reviewed_plan_replan_reason = reason
        self._last_reviewed_plan_candidate_hash = identity["candidateHash"]
        state = load_task_state(self.logger)
        review_input = self._assignment_review_input(candidate, reason, state, facts)
        key = review_input["reviewContextHash"]
        if not self.runtime.plan_validator.enabled:
            return {"status": "disabled", **identity, "reviewContextHash": key}
        cache = getattr(self, "_assignment_review_cache", {})
        self._assignment_review_cache = cache
        cached = cache.get(key)
        if cached:
            cached_review = cached["review"]
            if cached_review["status"] != "error":
                self.logger.write("assignment_review.cache_hit", {**identity, "reviewContextHash": key})
                return {**copy.deepcopy(cached_review), "deduplicated": True, "providerCalled": False,
                        "retryAfterSeconds": 0}
            age = max(0, time.monotonic() - cached["at"])
            if cached_review.get("errorKind") == "verdict_invalid":
                remaining = max(0, PLAN_VALIDATOR_PROTOCOL_ERROR_CACHE_TTL_SECONDS - age)
                if remaining:
                    return {**copy.deepcopy(cached_review), "deduplicated": True, "providerCalled": False,
                            "retryAfterSeconds": round(remaining, 1)}
            elif age < PLAN_VALIDATOR_ERROR_CACHE_TTL_SECONDS:
                self.logger.write("assignment_review.cache_hit", {**identity, "reviewContextHash": key})
                return {**copy.deepcopy(cached_review), "deduplicated": True, "providerCalled": False,
                        "retryAfterSeconds": round(PLAN_VALIDATOR_ERROR_CACHE_TTL_SECONDS - age, 1)}
        # Service availability is independent of candidate/evidence identity.
        # A fresh provider (including /resume) or a changed service config can
        # retry. Unknown quota reset times never cause automatic polling.
        config = self.runtime.plan_validator
        service_key = (self.plan_validator_provider, config.provider, config.model_id,
                       getattr(config, "base_url", None), getattr(config, "api_key", None))
        failure = getattr(self, "_assignment_review_service_failure", None)
        if failure and failure["serviceKey"] == service_key:
            until = failure["retryAt"]
            if until is None or time.monotonic() < until:
                source = {name: failure["review"].get(name)
                          for name in ("candidateHash", "reviewContextHash", "auditPath")}
                self.logger.write("assignment_review.service_unavailable", {
                    **identity, "reviewContextHash": key,
                    "errorKind": failure["review"]["errorKind"], "providerCalled": False,
                    "serviceFailureSource": source,
                })
                return {**copy.deepcopy(failure["review"]), **identity, "reviewContextHash": key,
                        "deduplicated": True, "providerCalled": False,
                        "serviceFailureSource": source,
                        "retryAfterSeconds": max(0, round(until - time.monotonic(), 1)) if until else None}
        attempts = []
        limit = 1 + max(0, min(3, int(getattr(config, "review_error_retry_attempts", 1) or 0)))
        for number in range(1, limit + 1):
            if self.plan_validator_provider is None:
                review = {"status": "error", "errorKind": "transport", "errors": ["assignment reviewer provider unavailable"]}
            else:
                review = await review_assignment(self.plan_validator_provider,
                    review_input=review_input, logger=self.logger,
                    provider_name=config.provider, model_id=config.model_id)
            review.update({**identity, "reviewContextHash": key, "reviewAttempt": number})
            attempts.append({"attempt": number, "status": review["status"],
                             "errorKind": review.get("errorKind"),
                             "diagnostics": review.get("attemptDiagnostics")})
            if (review.get("status") != "error" or self.plan_validator_provider is None
                    or review.get("errorKind") != "transport" or review.get("verdictRepairAttempted")):
                break
        review["reviewAttempts"] = attempts
        audit = write_plan_review_audit(self.logger, candidate_plan=candidate, replan_reason=reason,
                                       review={**review, "reviewInput": review_input})
        review["auditPath"] = audit
        if review.get("errorKind") in {"quota_exhausted", "rate_limited"}:
            provider_failure = review["providerFailure"]
            delay = provider_failure.get("retryAfterSeconds")
            if delay is None and provider_failure.get("resetAt"):
                try:
                    reset_at = datetime.fromisoformat(provider_failure["resetAt"].replace("Z", "+00:00"))
                    if reset_at.tzinfo is not None:
                        delay = max(0, (reset_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
            self._assignment_review_service_failure = {
                "serviceKey": service_key, "review": copy.deepcopy(review),
                "retryAt": time.monotonic() + delay if delay is not None else None,
            }
        else:
            cache[key] = {"at": time.monotonic(), "review": copy.deepcopy(review)}
        # A task can have many assignments; old receipts remain in the audit log.
        while len(cache) > 32:
            del cache[next(iter(cache))]
        self.logger.write("assignment_review.result", {**identity, "reviewContextHash": key,
            "status": review["status"], "auditPath": audit, "reviewAttempts": attempts,
            "errors": review.get("errors", [])})
        return review

    def assignment_review_blocker(self, review):
        # Keep exact invalid arguments in the review audit, not in the Lead's
        # next model context. A malformed verdict can be very large, while the
        # model needs its path-specific errors and the audit reference.
        model_review = copy.deepcopy(review)
        for diagnostic in model_review.get("attemptDiagnostics") or []:
            if isinstance(diagnostic, dict):
                diagnostic.pop("toolInput", None)
        for attempt in model_review.get("reviewAttempts") or []:
            if isinstance(attempt, dict):
                for diagnostic in attempt.get("diagnostics") or []:
                    if isinstance(diagnostic, dict):
                        diagnostic.pop("toolInput", None)
        return {"status": "assignment_review_unavailable", "tool_was_executed": False,
                "errorCode": "assignment_review_unavailable", "review": model_review,
                "acceptedAssignmentsUnchanged": True,
                "next_instruction": "This assignment was not accepted. Review is unavailable after bounded retries; "
                    "report the concrete blocker or continue independent authorized work. Do not treat this as approval "
                    "or task completion. Retry this same assignment after reviewer recovery; changing its wording cannot authorize it."
                    + (" The provider reported quota/throttling. Respect retryAfterSeconds if supplied; otherwise "
                       "restore the service and /resume to establish a new provider session. New task evidence does not reset this failure."
                       if review.get("errorKind") in {"quota_exhausted", "rate_limited"} else "")}

    def accept_assignment(self, raw_plan, *, review=None, preflight=False, user_approved_candidate_hash=""):
        plan, errors, repair_issues, facts = self._compile_assignment_candidate(raw_plan)
        if plan is None:
            return {"status": "assignment_rejected", "tool_was_executed": False,
                    "errors": errors, "repairIssues": repair_issues}
        reason = plan_replan_reason(raw_plan)
        identity = plan_candidate_identity(plan, reason)
        state = reconcile_replan_checkpoints(self.logger)
        request = self._assignment_review_input(plan, reason, state, facts)
        if self.runtime.plan_validator.enabled:
            if not isinstance(review, dict) or review.get("candidateHash") != identity["candidateHash"]:
                comparable = (isinstance(review, dict)
                              and review.get("candidateHash") == self._last_reviewed_plan_candidate_hash
                              and isinstance(self._last_reviewed_plan_candidate, dict))
                differences = plan_candidate_changed_paths(
                    plan_candidate_payload(self._last_reviewed_plan_candidate, self._last_reviewed_plan_replan_reason),
                    plan_candidate_payload(plan, reason)) if comparable else {}
                return {"status": "assignment_review_identity_mismatch", "tool_was_executed": False, **identity,
                        **differences,
                        "reviewedCandidateHash": (review or {}).get("candidateHash"),
                        "next_instruction": "Resubmit the assignment for review; this receipt belongs to a different candidate."}
            if review.get("reviewContextHash") != request["reviewContextHash"]:
                return {"status": "assignment_review_stale", "tool_was_executed": False, **identity,
                        "reviewContextHash": request["reviewContextHash"],
                        "next_instruction": "User context or execution evidence changed. Re-review this assignment against current facts."}
            if review.get("status") == "error":
                return self.assignment_review_blocker(review)
            if review.get("status") != "approved":
                return {"status": "assignment_rejected", "tool_was_executed": False, "review": review}
        checkpoint_errors = replan_checkpoint_plan_errors(plan, state)
        if checkpoint_errors:
            return {"status": "assignment_rejected", "tool_was_executed": False,
                    "errors": checkpoint_errors, "replanCheckpoints": active_replan_checkpoints(state)}
        if preflight:
            return {"status": "ready_for_approval", **identity, "normalizedPlan": copy.deepcopy(plan)}
        if user_approved_candidate_hash and user_approved_candidate_hash != identity["candidateHash"]:
            return {"status": "assignment_approval_identity_mismatch", "tool_was_executed": False, **identity}
        validator_record = {key: (review or {}).get(key) for key in
                            ("status", "candidateHash", "reviewContextHash", "verdict", "auditPath")}
        plan_path, version, state = accept_task_plan(
            self.logger, plan, previous_plan=self.task_plan, replan_reason=reason,
            user_task=self.original_user_task, validator_review=validator_record,
            preserve_from=state, preserve_execution=True,
            source_plan=copy.deepcopy(raw_plan),
            user_approval=({**identity, "approvedAt": datetime.now(timezone.utc).isoformat()}
                           if user_approved_candidate_hash else None))
        self.task_plan = plan
        self._accepted_task_plan_replan_reason = reason
        if self.resume is not None:
            self._resume_instruction_pending = False
        phase = plan["phases"][-1]
        return {"status": "done", **identity, "assignmentId": phase["id"],
                "planPath": plan_path, "planVersion": version.get("planVersion"),
                "assignmentReview": validator_record,
                "methodPolicy": capability_policy_facts(),
                "warnings": plan.get("warnings") or []}


    async def request_task_plan_approval(
        self,
        raw_plan: Any,
        candidate_hash: str,
    ) -> JsonDict:
        """Ask the host to approve the exact reviewed plan candidate."""
        handler = self.plan_approval_handler
        if handler is None:
            return {"decision": "approved", "interactive": False}
        self._pending_plan_approval_hash = candidate_hash
        self.logger.write("task_plan.approval_requested", {
            "candidateHash": candidate_hash,
            "phaseCount": len(raw_plan.get("phases", []))
            if isinstance(raw_plan, dict) else 0,
        })
        try:
            outcome = handler(copy.deepcopy(raw_plan), candidate_hash)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
        except (EOFError, KeyboardInterrupt) as exc:
            outcome = {"decision": "cancelled", "reason": type(exc).__name__}
        finally:
            # The callback is synchronous from the Lead's point of view.  Once
            # it returned, this candidate is no longer *awaiting* a decision.
            # Leaving the hash behind on revision or a later commit failure
            # blocked the already accepted plan and created a resend loop.
            self._pending_plan_approval_hash = ""
        if not isinstance(outcome, dict):
            outcome = {"decision": "revision", "feedback": str(outcome or "")}
        decision = str(outcome.get("decision") or "").strip().lower()
        if decision not in {"approved", "revision", "cancelled"}:
            decision = "revision"
        result = {
            "decision": decision,
            "candidateHash": candidate_hash,
            "candidateHashKind": "normalized_plan_and_reason",
            "candidateHashVersion": 2,
            "interactive": True,
        }
        if isinstance(outcome.get("inputRecords"), list):
            result["inputRecords"] = copy.deepcopy(outcome["inputRecords"])
        if decision == "revision":
            result["feedback"] = str(outcome.get("feedback") or "").strip()
            self._operator_revision_requested_hash = candidate_hash
            if result["feedback"] and not result.get("inputRecords"):
                result["inputRecords"] = [{
                    "inputId": f"approval:{candidate_hash}:{time.time_ns()}",
                    "candidateHash": candidate_hash, "source": "approval_handler",
                    "text": result["feedback"], "decision": "revision",
                    "receivedAt": datetime.now(timezone.utc).isoformat(),
                }]
        if decision == "cancelled":
            self._plan_execution_cancelled = True
        from harness.planning.context import retain_operator_inputs
        retain_operator_inputs(self.logger, result.get("inputRecords") or [])
        self.logger.write(f"task_plan.approval_{decision}", result)
        return result

    def mark_current_task_plan_user_approved(
        self,
        *,
        candidate_hash: str = "",
        persist: bool = True,
    ) -> None:
        if self.plan_approval_handler is None or not isinstance(self.task_plan, dict):
            return
        actual_hash = plan_candidate_hash(
            self.task_plan,
            self._accepted_task_plan_replan_reason,
        )
        if candidate_hash and candidate_hash != actual_hash:
            raise ValueError("approved candidate does not match the accepted task plan")
        self._user_approved_plan_hash = actual_hash
        self._pending_plan_approval_hash = ""
        self._operator_revision_requested_hash = ""
        self._plan_execution_cancelled = False
        if persist:
            state = load_task_state(self.logger)
            state["plan_user_approval"] = {
                **plan_candidate_identity(
                    self.task_plan, self._accepted_task_plan_replan_reason,
                ),
                "approvedAt": datetime.now(timezone.utc).isoformat(),
            }
            write_task_state(self.logger, state, replace=True)
        self.logger.write("task_plan.user_approved", {
            "candidateHash": self._user_approved_plan_hash,
            "phaseCount": len(self.task_plan.get("phases", [])),
        })

    def task_plan_user_approval_rejection(self) -> Optional[JsonDict]:
        if self.plan_approval_handler is None:
            return None
        if self._plan_execution_cancelled:
            return {
                "status": "user_cancelled",
                "error": "The operator cancelled task-plan execution.",
                "tool_was_executed": False,
                "next_instruction": "Do not spawn workers; report the cancellation.",
            }
        if self._pending_plan_approval_hash:
            return {
                "status": "plan_user_approval_required",
                "error": "A task-plan candidate is awaiting operator approval or revision.",
                "candidateHash": self._pending_plan_approval_hash,
                "tool_was_executed": False,
                "next_instruction": (
                    "Do not spawn workers while plan review is pending. Submit"
                    " a corrected assignment when the operator requested changes."
                ),
            }
        if self._operator_revision_requested_hash:
            return {
                "status": "user_revision_required",
                "error": "The operator requested a revision of the displayed task plan.",
                "candidateHash": self._operator_revision_requested_hash,
                "tool_was_executed": False,
                "next_instruction": (
                    "Do not spawn workers from the prior plan. Submit the"
                    " corrected assignment for a new operator approval."
                ),
            }
        if not isinstance(self.task_plan, dict):
            return None
        current_hash = plan_candidate_hash(
            self.task_plan,
            self._accepted_task_plan_replan_reason,
        )
        if current_hash == self._user_approved_plan_hash:
            return None
        return {
            "status": "plan_user_approval_required",
            "error": "The current task-plan version has not been approved by the operator.",
            "candidateHash": current_hash,
            "tool_was_executed": False,
            "next_instruction": (
                "Do not spawn workers. On a resumed task call"
                " resubmit spawn_browser_agent with the existing phase_id so"
                " the terminal can display the assignment for operator approval."
            ),
        }

    async def approve_existing_assignment(self, phase_id: str) -> JsonDict:
        """Show and approve a durable plan on resume without re-emitting it."""
        if not isinstance(self.task_plan, dict):
            return {
                "status": "plan_required",
                "error": "there is no accepted task plan to approve",
                "tool_was_executed": False,
            }
        candidate_hash = plan_candidate_hash(
            self.task_plan,
            self._accepted_task_plan_replan_reason,
        )
        if candidate_hash == self._user_approved_plan_hash:
            return {
                "status": "done",
                "candidateHash": candidate_hash,
                "alreadyApproved": True,
            }
        approval = await self.request_task_plan_approval(
            {**self.task_plan, "_approvalAssignmentId": phase_id}, candidate_hash,
        )
        if approval.get("decision") == "revision":
            return {
                "status": "user_revision_requested",
                "candidateHash": candidate_hash,
                "operatorFeedback": approval.get("feedback") or "",
                "operatorInputRecords": approval.get("inputRecords") or [],
                "tool_was_executed": False,
                "next_instruction": (
                    "Apply the operator feedback through a new assignment with replaces"
                    " pointing to the affected assignment; retain the original execution evidence."
                ),
            }
        if approval.get("decision") == "cancelled":
            return {
                "status": "user_cancelled",
                "candidateHash": candidate_hash,
                "tool_was_executed": False,
                "next_instruction": "Do not spawn workers; report that execution was cancelled.",
            }
        if any(item.get("decision") in {"clarify", "revision"}
               for item in approval.get("inputRecords", []) if isinstance(item, dict)):
            return {"status": "operator_context_updated", "tool_was_executed": False,
                    "operatorInputRecords": approval["inputRecords"],
                    "next_instruction": "Review the new user input before continuing this assignment."}
        self.mark_current_task_plan_user_approved(candidate_hash=candidate_hash)
        return {
            "status": "done",
            "candidateHash": candidate_hash,
            "phaseCount": len(self.task_plan.get("phases") or []),
            "operatorInputRecords": approval.get("inputRecords") or [],
        }


    async def approve_and_accept_assignment(
        self,
        raw_plan: Any,
        *,
        review: Optional[JsonDict],
    ) -> JsonDict:
        """Preflight, ask for one exact candidate, then commit it.

        `accept_assignment` remains the authoritative final gate, but asking
        first used to put a user approval in front of checks that could still
        reject the candidate.  This wrapper makes the visible review target a
        preflighted normalized plan, and guarantees a failed final commit does
        not leave an approval request pending.
        """
        preflight = self.accept_assignment(
            raw_plan,
            review=review,
            preflight=True,
        )
        if preflight.get("status") != "ready_for_approval":
            return preflight
        candidate_hash = str(preflight.get("candidateHash") or "")
        approval_plan = preflight.get("normalizedPlan")
        if isinstance(approval_plan, dict) and isinstance(raw_plan, dict):
            # Display/classification share the exact compiled assignment view.
            approval_plan = {**approval_plan, "_approvalAssignmentId": approval_plan["phases"][-1]["id"]}
        # A new submission is the Lead's response to any prior revision
        # request.  It may still be rejected or sent back again, but it must be
        # allowed to reach the operator rather than leaving the older plan
        # permanently blocked by a stale revision flag.
        self._operator_revision_requested_hash = ""
        pending_receipts = getattr(self, "_delegation_approval_receipts", {})
        approval = pending_receipts.pop(candidate_hash, None)
        if approval is None:
            approval = await self.request_task_plan_approval(approval_plan, candidate_hash)
            if (approval.get("decision") == "approved"
                    and any(item.get("decision") in {"clarify", "revision"}
                            for item in approval.get("inputRecords", []) if isinstance(item, dict))):
                pending_receipts[candidate_hash] = approval
                self._delegation_approval_receipts = pending_receipts
                return {
                    "status": "operator_context_updated", "tool_was_executed": False,
                    "operatorInputRecords": approval.get("inputRecords") or [],
                    "next_instruction": "Read the operator's ordered input before dispatch. "
                        "Judge whether it changes this assignment. Resubmit the same assignment "
                        "to use its existing approval, or submit the corrected assignment for review.",
                }
        if approval.get("decision") == "revision":
            return {
                "status": "user_revision_requested",
                "candidateHash": candidate_hash,
                "candidateHashKind": "normalized_plan_and_reason",
                "candidateHashVersion": 2,
                "operatorFeedback": approval.get("feedback") or "",
                "operatorInputRecords": approval.get("inputRecords") or [],
                "tool_was_executed": False,
                "next_instruction": (
                    "Use all operator feedback to revise the assignment, then"
                    " resubmit spawn_browser_agent with the corrected assignment."
                ),
            }
        if approval.get("decision") == "cancelled":
            return {
                "status": "user_cancelled",
                "candidateHash": candidate_hash,
                "tool_was_executed": False,
                "next_instruction": "Do not spawn workers; report that execution was cancelled.",
            }
        if approval.get("decision") != "approved":
            return {"status": "assignment_approval_required", "tool_was_executed": False}
        pending_receipts[candidate_hash] = approval
        self._delegation_approval_receipts = pending_receipts
        accepted = self.accept_assignment(
            raw_plan,
            review=review,
            user_approved_candidate_hash=(
                candidate_hash if self.plan_approval_handler is not None else ""
            ),
        )
        if isinstance(accepted, dict) and accepted.get("status") == "done":
            pending_receipts.pop(candidate_hash, None)
            self.mark_current_task_plan_user_approved(
                candidate_hash=candidate_hash,
                persist=False,
            )
            accepted["operatorInputRecords"] = approval.get("inputRecords") or []
        return accepted

    def _schema_cache_status(self) -> tuple[SchemaCacheStatus, Set[str]]:
        # If this run's bootstrap failed (no browser/empty caps/lock timeout/
        # exception), a stale on-disk cache is not authoritative — it may predate
        # a policy change and would wrongly reject now-valid methods. Degrade so plan validation skips the strict
        # unknown-method check, matching the bootstrap fallback log.
        if self._schema_bootstrap_degraded:
            return SchemaCacheStatus.NOT_LOADED, set()
        cache_dir = global_schema_cache_dir(self.runtime.harness.worktree_dir)
        cached_hash = read_cached_capability_hash(cache_dir)
        global_methods = read_schema_methods_from_dirs([
            global_schemas_dir(self.runtime.harness.worktree_dir),
        ])
        if cached_hash:
            if global_methods:
                return SchemaCacheStatus.LOADED_OK, global_methods
            return SchemaCacheStatus.LOADED_EMPTY, set()
        return SchemaCacheStatus.NOT_LOADED, set()

    async def _bootstrap_schema_cache(self) -> None:
        from harness.capabilities.bootstrap import bootstrap_schema_cache
        await bootstrap_schema_cache(self)

    def resolve_phase_for_spawn_with_rejection(
        self,
        phase_id: Optional[str],
        worker_contract: Optional[JsonDict] = None,
    ) -> "Tuple[Optional[JsonDict], Optional[JsonDict]]":
        """(phase, rejection). The rejection is phase_start_rejection's
        structured payload (dependency_not_ready / blocked_by_dependency /
        phase_already_running / explicit resource exhaustion / ...) when the phase
        exists but cannot start NOW. Task 2ed5a466: collapsing every rejection
        into a generic "phase not found or no pending phase" left the Lead
        blind-retrying a dependency-gated phase — the reason and its
        next_instruction must reach the model."""
        if self.task_plan is None:
            return None, None
        mark_phase_exhausted_if_needed(self.task_plan, self.logger)
        if phase_id:
            phase = find_phase(self.task_plan, phase_id)
            if phase is None:
                return None, None
            rejection = phase_start_rejection(
                self.task_plan,
                self.logger,
                phase_id=str(phase.get("id") or ""),
                # The (raw) override is what the worker will actually run;
                # without it a spawn that genuinely changes the objective
                # would be pre-rejected against the raw phase's fingerprint.
                worker_contract=worker_contract,
            )
            if rejection is not None:
                return None, rejection
            return phase, None
        # A gateway can drop a schema-required field, so the handler refuses an
        # unnamed phase itself rather than guessing. Guessing "the next pending
        # phase" is only well defined when exactly one is startable: in task
        # eb939033 it silently consumed the first detail phase, and the second
        # spawn — the one that was supposed to run the other fleet in parallel
        # — came back as "no pending phase" twice.
        snapshot = schedule_snapshot(self.task_plan, self.logger)
        return None, {
            "status": "failed",
            "error": "spawn_browser_agent requires an explicit phase_id",
            "errorCode": "phase_id_required",
            "tool_was_executed": False,
            "scheduleSnapshot": snapshot,
            "next_instruction": (
                "Name the accepted plan phase this worker executes. "
                f"{snapshot.get('recommendedAction') or ''}"
            ).strip(),
        }

    def phase_schedule_snapshot(self) -> JsonDict:
        """Read-only view of what the Lead may start, wait for, or report."""
        return schedule_snapshot(self.task_plan, self.logger)

    def resolve_phase_for_spawn(
        self,
        phase_id: Optional[str],
        worker_contract: Optional[JsonDict] = None,
    ) -> Optional[JsonDict]:
        phase, _rejection = self.resolve_phase_for_spawn_with_rejection(
            phase_id, worker_contract=worker_contract,
        )
        return phase

    def build_worker_contract(
        self,
        phase: JsonDict,
        override: Optional[JsonDict] = None,
    ) -> JsonDict:
        contract = phase_contract(phase, override)
        plan_pacing = (
            self.task_plan.get("pacing")
            if isinstance(self.task_plan, dict) else None
        )
        contract["pacing"] = merge_pacing(
            plan_pacing,
            phase.get("pacing"),
            override.get("pacing") if isinstance(override, dict) else None,
        )
        contract["orchestration_policy"] = self._browser_worker_orchestration_policy()
        return contract

    def _browser_worker_orchestration_policy(self) -> JsonDict:
        max_instances = getattr(
            self.runtime.harness,
            "max_browser_agent_instances",
            3,
        )
        return {
            "max_browser_agent_instances": int(max_instances or 3),
            "prefer_same_instance_multi_page": True,
            "allow_same_instance_multi_page": True,
            "prefer_related_idle_slot_reuse": True,
            "tab_control_mode": "same_page_serial",
            "rules": [
                (
                    "Prefer the same idle BrowserAgent slot for related"
                    " continuation work that shares a site, session, search"
                    " result set, or artifact contract."
                ),
                (
                    "Honor explicitly delegated or pinned pages. Otherwise,"
                    " unless the task explicitly requires a new page, first use"
                    " Page.list in assignedFleetId to discover a suitable idle,"
                    " claimable, non-quarantined task page. Claim it with"
                    " Page.switchTo and verify fresh state before acting; create"
                    " a page only if none is suitable. Discovery does not inherit"
                    " another worker's handles or task state. Do not create a"
                    " second fleet."
                ),
                (
                    "Within one BrowserAgent, open additional pages with Page.create"
                    " and move focus with Page.switchTo/Page.list as needed."
                ),
                (
                    "The harness serializes calls that target the same page;"
                    " workers on different pages may share the task/session fleet."
                ),
                (
                    "After every Page.create, Page.switchTo, or Page.navigate,"
                    " re-check page state when uncertain and refresh DOM.getAXTree"
                    " before targeting elements."
                ),
                (
                    "Track pageId, URL/title, and purpose for every opened page;"
                    " close pages that are no longer needed."
                ),
                (
                    "Treat slot_context pageIds as reusable candidates only;"
                    " verify Page.getState/Page.switchTo and refresh DOM.getAXTree"
                    " before acting."
                ),
            ],
        }

    async def run(self, task: str) -> str:
        from harness.agents.lead.loop import run_lead_agent
        return await run_lead_agent(self, task)

    def _pending_phase_ids(self) -> List[str]:
        """Phase ids not yet completed, for empty-response recovery prompts.

        Best-effort: a missing/unreadable task_state must never break the
        recovery path — it only makes the prompt less specific.
        """
        if self.task_plan is None:
            return []
        try:
            state = load_task_state(self.logger)
        except Exception:
            return []
        phases = state.get("phases") if isinstance(state, dict) else None
        if not isinstance(phases, dict):
            return []
        pending: List[str] = []
        for phase_id, phase_state in phases.items():
            status = (
                str(phase_state.get("status") or "")
                if isinstance(phase_state, dict)
                else ""
            )
            if status in {"pending", "running"}:
                pending.append(str(phase_id))
        return pending

    def _step_cap_reminder_block(
        self, *, current_step: int, max_steps: int,
    ) -> Optional[JsonDict]:
        next_step = current_step + 1
        if next_step > max_steps:
            return None
        # Inclusive count of model turns still available.
        remaining = max_steps - current_step
        if remaining > 2:
            return None
        if remaining <= 0:
            remaining = 1
        reminder = (
            "[LEAD-CHECKPOINT-REMINDER]\n"
            "This reminder applies to the immediately following assistant turn only.\n"
            f"currentStep={next_step}\n"
            f"maxSteps={max_steps}\n"
            f"remainingSteps={remaining}\n"
            "These are arithmetic budget facts only. Choose the next action"
            " from the original goal and current evidence."
        )
        self.logger.write(
            "lead.step_cap.reminder",
            {
                "step": next_step,
                "max_steps": max_steps,
                "remaining": remaining,
                "injected_after_step": current_step,
                "placement": "user_message_text_block",
            },
        )
        return {"type": "text", "text": reminder}

    def _observe_cache_pressure(
        self, usage_payload: JsonDict, *, step: int, max_steps: int,
    ) -> None:
        self._cache_pressure, reason = update_cache_pressure_state(
            self._cache_pressure,
            usage_payload=usage_payload,
            config=self.runtime.harness,
            step=step,
            max_steps=max_steps,
        )
        if reason:
            self._forced_compaction_reason = reason
            self.logger.write(
                "context.compaction_requested",
                {
                    "actor": "lead_agent",
                    "step": step + 1,
                    "reason": reason,
                    "triggerStep": step,
                },
            )

    def _build_planning_system_prompt(self) -> str:
        # Compatibility for callers loading an older task; same tools/protocol.
        return self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        from harness.agents.lead.prompt import build_system_prompt
        return build_system_prompt(self)
