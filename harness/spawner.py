"""
harness.spawner - Worker BrowserAgent spawning and lifecycle management.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from abcp_client import ABCPClient
from harness.constants import (
    WORKER_STATUS_CANCELLED,
    WORKER_STATUS_FAILED,
)
from harness.diagnostics import status_category
from harness.render_recovery import extract_page_id_from_values
from harness.config import RuntimeConfig
from harness.model_config import browser_agent_model_config
from harness.plan_executor import ABCPPlanExecutor
from harness.task_control import (
    mark_phase_result,
    mark_phase_running,
    validate_worker_artifacts,
)
from harness.templates import get_path, render_templates
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
    """Creates isolated browser agents and direct ABCP executors."""

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
        self.plan_executor = ABCPPlanExecutor(runtime, logger, self._next_id)

    async def spawn_browser_agent(
        self,
        task: str,
        context: str = "",
        name: Optional[str] = None,
        max_steps: Optional[int] = None,
        result_contract: str = "",
        phase_id: Optional[str] = None,
        worker_contract: Optional[JsonDict] = None,
    ) -> JsonDict:
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
                worker_contract=worker_contract or {},
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
            worker_contract=worker_contract or {},
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
                "workerContract": trim_large_strings(worker_contract or {}, 2000),
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

    async def run_browser_batch(
        self,
        items: List[Any],
        task_template: str,
        context_template: str = "",
        concurrency: Optional[int] = None,
        max_steps: Optional[int] = None,
    ) -> JsonDict:
        if not isinstance(items, list):
            return {"status": "failed", "error": "items must be a JSON array"}

        default_concurrency = self.runtime.harness.default_worker_concurrency
        effective_concurrency = optional_int(concurrency, default_concurrency)
        if effective_concurrency is None:
            effective_concurrency = default_concurrency
        effective_concurrency = max(1, min(
            effective_concurrency,
            self.runtime.harness.max_browser_agents,
        ))
        semaphore = asyncio.Semaphore(effective_concurrency)

        async def run_one(index: int, raw_item: Any) -> JsonDict:
            item = raw_item if isinstance(raw_item, dict) else {"value": raw_item}
            variables = {"item": item, "index": index}
            task = str(render_templates(task_template, variables))
            context = str(render_templates(context_template, variables))
            worker_id = self._next_id("batch")
            agent_id = f"{self.runtime.agent_id}-{worker_id}"
            async with semaphore:
                return await self._run_browser_worker(
                    worker_id=worker_id,
                    agent_id=agent_id,
                    name=worker_id,
                    task=task,
                    context=context,
                    max_steps=optional_int(max_steps),
                    result_contract=(
                        "Return a JSON string: "
                        "{\"outcome\":\"done|partial|blocked|failed\","
                        "\"data\":<structured result requested by the task>,"
                        "\"evidence\":[{\"step\":<step number>,\"page_id\":\"...\",\"why\":\"...\"}],"
                        "\"next_steps\":[]}"
                    ),
                    phase_id=None,
                    worker_contract={},
                )

        results = await asyncio.gather(
            *(run_one(index, item) for index, item in enumerate(items)),
            return_exceptions=True,
        )
        normalized = [
            result if isinstance(result, dict)
            else {"status": "failed", "error": str(result)}
            for result in results
        ]
        failed_count = sum(
            1 for result in normalized
            if result.get("status") != "done"
        )
        return {
            "status": "done" if failed_count == 0 else "partial_failed",
            "concurrency": effective_concurrency,
            "count": len(normalized),
            "success_count": len(normalized) - failed_count,
            "failed_count": failed_count,
            "results": normalized,
        }

    async def run_skill_agent(
        self,
        task: str,
        input_context: str = "",
        output_schema: str = "",
        evidence_artifacts: Optional[List[str]] = None,
    ) -> JsonDict:
        provider = LLMFactory.create_provider(self.runtime.model)
        system_prompt = """You are the ABCP SkillAgent.

Your job is to distil a verified browser execution trace into a reusable extraction / form-filling strategy, an ABCP step template, or a worker-task template.
Prefer producing a deterministic ABCP-steps template — declare variable placeholders, success criteria, field-integrity checks, and recoverable failure points. In the `steps_json` consumed by LeadAgent tools, every step has `method`, `params`, and optional `save_as` fields; `params` MUST be a JSON object; for extraction templates, persist the final lightweight field set with `save_as="output"`. Only fall back to a BrowserAgent worker-task template when the page structure is unstable or genuine semantic judgement is unavoidable.
Your output must be drop-in usable by LeadAgent. If the user asked for JSON, output JSON only — no markdown wrapping.

**Evidence contract (mandatory)**:
1. Every URL / href / id / slug / primary-key field in your output MUST be findable verbatim in the `<evidence>` block. Never infer values from product names or naming conventions.
2. If the relevant field is absent from `<evidence>`, write the literal string `"<unverified>"` or `null`. Do not fabricate.
3. Within a single record, fields with evidence keep their verbatim value; fields without evidence are marked `"<unverified>"`.
4. The `<evidence>` block is read-only input. If it is empty or carries no artifact, mark EVERY field that requires external evidence as `"<unverified>"`.
""" + self.static_context_block

        evidence_block, evidence_summary = self._render_evidence_block(
            evidence_artifacts or []
        )
        sections = [f"Task:\n{task}"]
        if evidence_block:
            sections.append(f"<evidence>\n{evidence_block}\n</evidence>")
        else:
            sections.append("<evidence>\n(empty — mark every externally sourced field as \"<unverified>\")\n</evidence>")
        if input_context:
            sections.append(f"Input context:\n{input_context}")
        sections.append(
            f"Expected output format:\n{output_schema or 'a clear set of executable steps or a template'}"
        )

        messages = [{"role": "user", "content": "\n\n".join(sections)}]
        text, tool_calls, stop_reason, usage = await provider.generate_response(
            system_prompt=system_prompt,
            messages=messages,
            tools=[],
        )
        self.logger.record_llm_usage(
            source="skill_agent",
            provider=self.runtime.model.provider,
            model=self.runtime.model.model_id,
            usage=usage,
            conversation_id=f"skill:{uuid.uuid4().hex}",
            context_hash=self.static_context_hash,
        )
        result = {
            "status": "done",
            "answer": text.strip(),
            "tool_calls_ignored": tool_calls,
            "stop_reason": stop_reason,
            "evidence_summary": evidence_summary,
        }
        self.logger.write("spawner.skill.result", trim_large_strings(result, 8000))
        return result

    def _render_evidence_block(self, paths: List[str]) -> tuple:
        """Read evidence files into a bounded inline block plus a summary
        list for the result envelope. Returns (block_text, summary_list).

        Security: evidence paths are model-controlled. We:
          1. Resolve each path (following symlinks) and reject anything that
             escapes the current task worktree (`logger.task_dir`).
          2. Further constrain to `<task_dir>/artifacts/extractions/` so the
             only files an evidence_artifacts arg can surface are ones a
             BrowserAgent legitimately wrote via record_extraction.
          3. Refuse non-regular files (avoid /dev/*, FIFOs, etc.).
        """
        if not paths:
            return "", []
        max_total_bytes = max(
            8000,
            min(80000, self.runtime.harness.max_observation_chars),
        )
        per_file_cap = max(2000, max_total_bytes // max(1, len(paths)))
        rendered_parts: List[str] = []
        summary: List[JsonDict] = []
        total = 0

        allowed_root = self._evidence_allowed_root()

        for raw_path in paths:
            raw_str = str(raw_path)
            try:
                # Resolve once we know the base. Reject obviously malformed.
                if Path(raw_str).is_absolute():
                    resolved = Path(raw_str).resolve(strict=False)
                else:
                    base = allowed_root or Path(
                        self.runtime.harness.runs_dir or "."
                    ).resolve(strict=False)
                    resolved = (base / raw_str).resolve(strict=False)
            except (OSError, ValueError) as exc:
                summary.append({
                    "path": raw_str,
                    "status": "rejected",
                    "error": f"path resolution failed: {exc}",
                })
                rendered_parts.append(
                    f"### evidence: {raw_str}\n(rejected: path resolution failed)"
                )
                continue

            if allowed_root is None:
                # No task_dir configured — refuse model-controlled paths.
                summary.append({
                    "path": str(resolved),
                    "status": "rejected",
                    "error": "no task_dir configured; evidence sandbox unavailable",
                })
                rendered_parts.append(
                    f"### evidence: {resolved}\n(rejected: sandbox unavailable)"
                )
                continue

            try:
                resolved.relative_to(allowed_root)
            except ValueError:
                summary.append({
                    "path": str(resolved),
                    "status": "rejected",
                    "error": (
                        f"path escapes evidence sandbox {allowed_root}"
                    ),
                })
                rendered_parts.append(
                    f"### evidence: {resolved}\n"
                    f"(rejected: outside {allowed_root})"
                )
                continue

            if not resolved.exists() or not resolved.is_file():
                summary.append({"path": str(resolved), "status": "missing"})
                rendered_parts.append(
                    f"### evidence: {resolved}\n"
                    "(missing — treat all fields it should provide as <unverified>)"
                )
                continue

            try:
                raw_bytes = resolved.read_bytes()
            except OSError as exc:
                summary.append({
                    "path": str(resolved),
                    "status": "unreadable",
                    "error": str(exc),
                })
                rendered_parts.append(f"### evidence: {resolved}\n(unreadable: {exc})")
                continue

            original_size = len(raw_bytes)
            if total + min(original_size, per_file_cap) > max_total_bytes:
                summary.append({
                    "path": str(resolved),
                    "status": "skipped_budget",
                    "originalBytes": original_size,
                })
                rendered_parts.append(
                    f"### evidence: {resolved}\n"
                    f"(skipped — would exceed inline evidence budget; "
                    f"originalBytes={original_size})"
                )
                continue
            text = raw_bytes.decode("utf-8", errors="replace")
            truncated = False
            if len(text) > per_file_cap:
                text = text[:per_file_cap] + f"\n... <truncated {len(text) - per_file_cap} chars>"
                truncated = True
            rendered_parts.append(f"### evidence: {resolved}\n{text}")
            total += len(text)
            summary.append({
                "path": str(resolved),
                "status": "included",
                "bytes": min(original_size, per_file_cap),
                "originalBytes": original_size,
                "truncated": truncated,
            })
        return "\n\n".join(rendered_parts), summary

    def _evidence_allowed_root(self) -> Optional[Path]:
        """The directory tree evidence_artifacts paths must resolve under.
        Restricted to <task_dir>/artifacts/extractions/ — the only place
        record_extraction is allowed to write.
        """
        runs_dir = self.runtime.harness.runs_dir
        if not runs_dir:
            return None
        try:
            base = Path(runs_dir).resolve(strict=False)
        except (OSError, ValueError):
            return None
        return base / "artifacts" / "extractions"

    async def execute_abcp_plan(
        self,
        steps: List[JsonDict],
        variables: Optional[JsonDict] = None,
        agent_name: Optional[str] = None,
        context: str = "",
    ) -> JsonDict:
        return await self.plan_executor.execute_abcp_plan(
            steps=steps,
            variables=variables,
            agent_name=agent_name,
            context=context,
        )

    async def run_abcp_plan_batch(
        self,
        items: List[Any],
        steps: List[JsonDict],
        variables: Optional[JsonDict] = None,
        context_template: str = "",
        concurrency: Optional[int] = None,
        validate_first_n: Optional[int] = None,
    ) -> JsonDict:
        return await self.plan_executor.run_abcp_plan_batch(
            items=items,
            steps=steps,
            variables=variables,
            context_template=context_template,
            concurrency=concurrency,
            validate_first_n=validate_first_n,
        )

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
                harness = self.browser_agent_factory(provider, browser, worker_runtime, self.logger)
                harness.worker_contract = worker_contract or {}
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
                task_dir=self.logger.task_dir,
            )
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
        )
        self.logger.write("spawner.browser.result", trim_large_strings(result, 8000))
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
        return {
            "steps": max_step,
            "traceEvents": len(trace),
            "toolCalls": tool_calls,
            "methods": method_counts,
            "pageIds": sorted(page_ids),
            "errors": errors[:10],
            "offloadedFiles": sorted(set(offloaded))[:100],
        }

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
            return {
                "status": WORKER_STATUS_CANCELLED,
                "statusCategory": status_category(WORKER_STATUS_CANCELLED),
                "workerId": handle.worker_id,
                "agentId": handle.agent_id,
                "name": handle.name,
                "phaseId": handle.phase_id,
            }
        except Exception as exc:
            return {
                "status": WORKER_STATUS_FAILED,
                "statusCategory": status_category(WORKER_STATUS_FAILED),
                "workerId": handle.worker_id,
                "agentId": handle.agent_id,
                "name": handle.name,
                "phaseId": handle.phase_id,
                "error": str(exc),
            }

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:03d}"
