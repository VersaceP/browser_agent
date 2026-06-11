"""
harness.spawner - Worker BrowserAgent spawning and lifecycle management.
"""

import asyncio
import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Set

from abcp_client import ABCPClient
from harness.constants import (
    WORKER_STATUS_CANCELLED,
    WORKER_STATUS_FAILED,
)
from harness.diagnostics import status_category
from harness.render_recovery import extract_page_id_from_values
from harness.config import RuntimeConfig
from harness.lifecycle import LifecycleContext, default_lifecycle_manager
from harness.model_config import browser_agent_model_config
from harness.schema_cache import global_schemas_dir
from harness.schema_loader import CapabilityBundle, load_capability_bundle
from harness.task_control import (
    build_attempt_digest,
    classification_for_worker_status,
    contract_hash_for_phase,
    mark_phase_result,
    mark_phase_running,
    repeated_phase_attempt_guard,
    validate_worker_artifacts,
)
from harness.strategy_telemetry import append_strategy_attempt
from harness.tool_policy import ALWAYS_FORBIDDEN_ABCP_METHODS
from harness.templates import get_path
from harness.utils import (
    JsonDict,
    RunLogger,
    build_static_context_block,
    extract_offloaded_paths,
    make_browser_event_logger,
    optional_float,
    optional_int,
    safe_path_component,
    task_subdir,
    trim_large_strings,
)
from harness.worker_result import build_worker_result_levels
from llm import LLMFactory


BrowserAgentFactory = Callable[[Any, ABCPClient, RuntimeConfig, RunLogger], Any]


@dataclass
class BrowserAgentHandle:
    worker_id: str
    agent_id: str
    name: str
    task: str
    context: str
    result_contract: str
    phase_id: Optional[str]
    worker_contract: JsonDict
    async_task: Any


class BrowserAgentSpawner:
    """Creates isolated browser agents and manages their lifecycle."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        logger: RunLogger,
        browser_agent_factory: BrowserAgentFactory,
    ):
        self.runtime = runtime
        self.browser_agent_factory = browser_agent_factory
        self.logger = logger
        self._handles: Dict[str, BrowserAgentHandle] = {}
        self._counter = 0
        self.static_context_block, self.static_context_hash = build_static_context_block(
            self.runtime.harness.context_file
        )
        self.lifecycle = default_lifecycle_manager()
        self._capability_bundle: Optional[CapabilityBundle] = None
        self._capability_bundle_lock = None

    async def spawn_browser_agent(
        self,
        task: str,
        context: str = "",
        name: Optional[str] = None,
        max_steps: Optional[int] = None,
        result_contract: str = "",
        phase_id: Optional[str] = None,
        worker_contract: Optional[JsonDict] = None,
        phase: Optional[JsonDict] = None,
    ) -> JsonDict:
        effective_contract = worker_contract or {}
        current_contract_hash = contract_hash_for_phase(
            phase,
            effective_contract,
            task=task,
            result_contract=result_contract,
        )
        repeated_guard = repeated_phase_attempt_guard(
            self.logger,
            phase_id=phase_id,
            contract_hash=current_contract_hash,
        )
        if repeated_guard is not None:
            self.logger.write("spawner.browser.repeated_guard", repeated_guard)
            return repeated_guard

        running_count = sum(
            1 for handle in self._handles.values()
            if not handle.async_task.done()
        )
        if running_count >= self.runtime.harness.max_browser_agents:
            return {
                "status": "rejected",
                "error": "Reached the max_browser_agents limit",
                "running": running_count,
                "max_browser_agents": self.runtime.harness.max_browser_agents,
            }

        worker_id = self._next_id("browser")
        agent_name = name or worker_id
        agent_id = f"{self.runtime.agent_id}-{worker_id}"
        async_task = asyncio.create_task(
            self._run_browser_worker(
                worker_id=worker_id,
                agent_id=agent_id,
                name=agent_name,
                task=task,
                context=context,
                max_steps=optional_int(max_steps),
                result_contract=result_contract,
                phase_id=phase_id,
                worker_contract=effective_contract,
                phase=phase or {},
            )
        )
        mark_phase_running(
            self.logger,
            phase_id=phase_id,
            worker_id=worker_id,
            worker_name=agent_name,
        )
        self._handles[worker_id] = BrowserAgentHandle(
            worker_id=worker_id,
            agent_id=agent_id,
            name=agent_name,
            task=task,
            context=context,
            result_contract=result_contract,
            phase_id=phase_id,
            worker_contract=effective_contract,
            async_task=async_task,
        )
        self.logger.write(
            "spawner.browser.spawn",
            {
                "workerId": worker_id,
                "agentId": agent_id,
                "name": agent_name,
                "task": task,
                "resultContract": result_contract,
                "phaseId": phase_id,
                "workerContract": trim_large_strings(effective_contract, 2000),
                "contractHash": current_contract_hash,
            },
        )
        return {
            "status": "running",
            "workerId": worker_id,
            "agentId": agent_id,
            "name": agent_name,
            "phaseId": phase_id,
        }

    async def wait_browser_agents(
        self,
        worker_ids: Optional[List[str]] = None,
        mode: str = "all",
        timeout_seconds: Optional[float] = None,
    ) -> JsonDict:
        handles = self._select_handles(worker_ids)
        if not handles:
            return {"status": "empty", "completed": [], "pending": []}

        tasks = [handle.async_task for handle in handles]
        return_when = (
            asyncio.FIRST_COMPLETED if mode == "first" else asyncio.ALL_COMPLETED
        )
        done, pending = await asyncio.wait(
            tasks,
            timeout=optional_float(timeout_seconds),
            return_when=return_when,
        )

        completed = [
            self._task_result(handle)
            for handle in handles
            if handle.async_task in done or handle.async_task.done()
        ]
        pending_ids = [
            handle.worker_id
            for handle in handles
            if handle.async_task in pending and not handle.async_task.done()
        ]
        return {
            "status": "done" if not pending_ids else "partial",
            "completed": completed,
            "pending": pending_ids,
        }

    def list_browser_agents(self) -> JsonDict:
        agents = []
        for handle in self._handles.values():
            if handle.async_task.cancelled():
                status = "cancelled"
            elif handle.async_task.done():
                result = self._task_result(handle)
                status = result.get("status", "done")
            else:
                status = "running"
            agents.append({
                "workerId": handle.worker_id,
                "agentId": handle.agent_id,
                "name": handle.name,
                "phaseId": handle.phase_id,
                "status": status,
                "task": handle.task,
            })
        return {"status": "done", "agents": agents}

    async def shutdown(self) -> None:
        pending = [
            handle.async_task for handle in self._handles.values()
            if not handle.async_task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_browser_worker(
        self,
        worker_id: str,
        agent_id: str,
        name: str,
        task: str,
        context: str,
        max_steps: Optional[int],
        result_contract: str,
        phase_id: Optional[str],
        worker_contract: JsonDict,
        phase: Optional[JsonDict],
    ) -> JsonDict:
        worker_runtime = replace(
            self.runtime,
            agent_id=agent_id,
            harness=replace(
                self.runtime.harness,
                max_steps=max_steps or self.runtime.harness.worker_max_steps,
            ),
        )
        provider = LLMFactory.create_provider(
            browser_agent_model_config(worker_runtime.model)
        )
        event_logger = make_browser_event_logger(
            self.logger,
            worker_runtime.harness.log_browser_payloads,
            prefix=f"{worker_id}.transport",
        )
        worker_task = (
            f"BrowserAgent name: {name}\n"
            f"Independent context:\n{context or '(none)'}\n\n"
            f"<worker_contract>\n"
            f"{json.dumps(worker_contract or {}, ensure_ascii=False, indent=2, default=str)}\n"
            f"</worker_contract>\n\n"
            f"Result contract:\n{result_contract or 'Return a structured JSON string containing outcome, data, evidence, next_steps.'}\n\n"
            f"Assigned task:\n{task}"
        )

        try:
            async with ABCPClient(worker_runtime.browser, on_event=event_logger) as browser:
                registration = await browser.call(
                    "System.register",
                    {"agentId": worker_runtime.agent_id},
                )
                bundle = await self._capability_bundle_for_worker(
                    browser,
                    worker_runtime,
                )
                harness = self.browser_agent_factory(provider, browser, worker_runtime, self.logger)
                harness.worker_contract = worker_contract or {}
                harness.preloaded_registration = registration
                harness.preloaded_capability_bundle = bundle
                answer = await harness.run(worker_task)
            trace_path = self._write_worker_trace(worker_id, harness.trace)
            trace_summary = self._summarize_worker_trace(harness.trace)
            challenge_tracker = getattr(harness, "challenge_tracker", None)
            if challenge_tracker is not None and hasattr(challenge_tracker, "suspected_pages"):
                trace_summary["suspectedChallengePages"] = challenge_tracker.suspected_pages()
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
                task_dir=self.logger.task_dir,
            )
            terminal_classification = classification_for_worker_status(
                harness.final_status
            )
            if terminal_classification is not None:
                artifact_validation["classification"] = terminal_classification
            elif artifact_validation.get("status") != "done":
                feedback_classification = _worker_feedback_classification(
                    harness.trace,
                    answer,
                )
                if feedback_classification is not None:
                    artifact_validation["classification"] = feedback_classification
            validated_status = (
                "validated_done"
                if artifact_validation.get("status") == "done"
                else "validation_failed"
                if artifact_validation.get("status") == "failed"
                else "not_validated"
            )
            diagnostics = getattr(harness, "diagnostics", None)
            result = {
                "status": harness.final_status,
                "statusCategory": status_category(harness.final_status),
                "validatedStatus": validated_status,
                "workerId": worker_id,
                "agentId": agent_id,
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
                "tracePath": trace_path,
                "traceSummary": trace_summary,
                "progressSnapshot": progress_snapshot,
                "progressInterventionCount": progress_snapshot.get(
                    "interventionCount",
                    0,
                ),
                "offloadedFiles": offloaded_files,
                "diagnostics": diagnostics.to_log_payload()
                if diagnostics is not None
                else {},
            }
        except asyncio.CancelledError:
            result = {
                "status": WORKER_STATUS_CANCELLED,
                "statusCategory": status_category(WORKER_STATUS_CANCELLED),
                "workerId": worker_id,
                "agentId": agent_id,
                "name": name,
                "phaseId": phase_id,
            }
            result = self._prepare_worker_result(
                result,
                worker_id=worker_id,
                agent_id=agent_id,
                phase_id=phase_id,
            )
            self.logger.write(
                "spawner.browser.result",
                trim_large_strings(result, 8000),
            )
            raise
        except Exception as exc:
            result = {
                "status": WORKER_STATUS_FAILED,
                "statusCategory": status_category(WORKER_STATUS_FAILED),
                "workerId": worker_id,
                "agentId": agent_id,
                "name": name,
                "phaseId": phase_id,
                "error": str(exc),
            }

        result = self._prepare_worker_result(
            result,
            worker_id=worker_id,
            agent_id=agent_id,
            phase_id=phase_id,
        )
        attempt_digest = build_attempt_digest(
            result,
            phase=phase or {},
            worker_contract=worker_contract or {},
            task=task,
            result_contract=result_contract,
        )
        result["attemptDigest"] = attempt_digest
        worker_status = str(result.get("status") or "unknown")
        phase_result_status = (
            worker_status
            if worker_status in {
                "blocked_by_challenge",
                "hitl_required",
                "hitl_timeout",
                "page_settled_after_hitl",
            }
            else str(result.get("validatedStatus") or result.get("status") or "unknown")
        )
        mark_phase_result(
            self.logger,
            phase_id=phase_id,
            worker_id=worker_id,
            validation=result.get("artifactValidation"),
            result_status=phase_result_status,
            attempt_digest=attempt_digest,
        )
        append_strategy_attempt(
            logger=self.logger,
            worker_contract=worker_contract or {},
            result=result,
        )
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
        )
        result["resultLevels"] = levels
        result["workerResultProtocol"] = "L1/L2/L3"
        return result

    def _write_worker_trace(self, worker_id: str, trace: List[JsonDict]) -> str:
        traces_dir = task_subdir(self.logger, "traces")
        path = traces_dir / f"{safe_path_component(worker_id)}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for item in trace:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        return str(path.resolve())

    def _summarize_worker_trace(self, trace: List[JsonDict]) -> JsonDict:
        method_counts: Dict[str, int] = {}
        errors: List[str] = []
        page_ids: Set[str] = set()
        offloaded: List[str] = []
        progress_interventions: List[JsonDict] = []
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
            elif item.get("type") in {"progress_intervention", "progress_gate"}:
                result = item.get("result")
                if isinstance(result, dict):
                    progress_interventions.append({
                        "type": item.get("type"),
                        "reason": str(result.get("reason") or "")[:120],
                        "tool": str(result.get("tool") or result.get("method") or "")[:120],
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
        progress_intervention_count = len(progress_interventions)
        loop_nudge_count = len(loop_nudges)
        stall_signal_count = progress_intervention_count + loop_nudge_count
        stall_replan = None
        if stall_signal_count >= 2:
            stall_replan = {
                "type": "stall_replan_recommended",
                "signalCount": stall_signal_count,
                "progressInterventionCount": progress_intervention_count,
                "loopNudgeCount": loop_nudge_count,
                "recentProgressInterventions": progress_interventions[-3:],
                "recentLoopNudges": loop_nudges[-3:],
                "next_instruction": (
                    "This worker emitted repeated stall signals inside one"
                    " attempt. If the phase is retried, revise the approach"
                    " instead of spawning another worker with the same path."
                ),
            }
        summary = {
            "steps": max_step,
            "traceEvents": len(trace),
            "toolCalls": tool_calls,
            "methods": method_counts,
            "pageIds": sorted(page_ids),
            "errors": errors[:10],
            "progressInterventions": progress_interventions[-5:],
            "progressInterventionCount": progress_intervention_count,
            "loopNudges": loop_nudges[-5:],
            "loopNudgeCount": loop_nudge_count,
            "latestPageStats": page_stats_events[-1] if page_stats_events else None,
            "pageStatsCount": len(page_stats_events),
            "snapshotDiffs": snapshot_diffs[-5:],
            "snapshotDiffCount": len(snapshot_diffs),
            "offloadedFiles": sorted(set(offloaded))[:100],
        }
        if stall_replan is not None:
            summary["stallReplanRecommended"] = stall_replan
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


def _safe_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _worker_feedback_classification(
    trace: List[JsonDict],
    answer: str,
) -> Optional[JsonDict]:
    """Recover route-relevant classifications from worker feedback.

    Contract/tool-policy blockers are first surfaced as ordinary tool results
    inside the BrowserAgent loop. If the worker later finalizes cleanly or runs
    out of steps without a matching artifact, validation would otherwise report
    only data_missing. Preserve the more useful routing classification.
    """
    trace_classification = _classification_from_contract_violation(trace)
    if trace_classification is not None:
        return trace_classification
    return _classification_from_final_answer(answer)


def _classification_from_contract_violation(
    trace: List[JsonDict],
) -> Optional[JsonDict]:
    for item in reversed(trace or []):
        if not isinstance(item, dict) or item.get("type") != "contract_violation":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        classification = result.get("classification")
        if not isinstance(classification, dict):
            continue
        category = str(classification.get("category") or "").strip()
        if category != "blocked_cross_task_type_required":
            continue
        recovered = dict(classification)
        recovered.setdefault("hint", "LeadAgent should replan with a task_type that permits the required method.")
        recovered["source"] = "contract_violation"
        return recovered
    return None


def _classification_from_final_answer(answer: str) -> Optional[JsonDict]:
    try:
        payload = json.loads(str(answer or ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        return None
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        raw_classification = blocker.get("classification")
        if isinstance(raw_classification, dict):
            category = str(raw_classification.get("category") or "").strip()
            classification = dict(raw_classification)
        else:
            category = str(raw_classification or "").strip()
            classification = {"category": category}
        if category != "blocked_cross_task_type_required":
            continue
        hint = (
            blocker.get("hint")
            or blocker.get("message")
            or blocker.get("reason")
            or "LeadAgent should replan with a task_type that permits the required method."
        )
        classification.setdefault("hint", str(hint)[:500])
        if blocker.get("method"):
            classification.setdefault("method", blocker.get("method"))
        if blocker.get("task_type"):
            classification.setdefault("task_type", blocker.get("task_type"))
        classification["source"] = "final_answer.blockers"
        return classification
    return None


def _clone_capability_bundle(bundle: CapabilityBundle) -> CapabilityBundle:
    return CapabilityBundle(
        capabilities=list(bundle.capabilities),
        capability_methods=set(bundle.capability_methods),
        method_schemas=dict(bundle.method_schemas),
        methods_requiring_purpose=set(bundle.methods_requiring_purpose),
        purpose_hints=dict(bundle.purpose_hints),
        skills_doc=bundle.skills_doc,
    )
