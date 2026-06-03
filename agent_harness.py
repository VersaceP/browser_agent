"""
agent_harness.py - LLM driven ABCP browser control loops.

The heavy lifting lives in the harness package. This module keeps the two
agent orchestration loops and re-exports the public harness API used by
main.py and tests.
"""

import asyncio
import json
import shutil
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from abcp_client import ABCPClient, ABCPClientConfig, ABCPTransportError
from harness.compaction import compact_messages_if_needed, validate_tool_pairing
from harness.config import HarnessConfig, RuntimeConfig, VLConfig
from harness.challenge_detector import ChallengeTracker
from harness.constants import (
    CONTEXT_LIMIT_ERROR_MARKERS,
    MODEL_ALLOWED_SOFT_STATUSES,
    WORKER_STATUS_CONTEXT_LIMIT,
    WORKER_STATUS_DONE,
    WORKER_STATUS_RUNNING,
)
from harness.diagnostics import (
    WorkerDiagnostics,
    classify_terminal_status,
    status_category,
)
from harness.local_fs import local_fs_jsonpath, local_fs_read, local_fs_search
from harness.model_config import browser_agent_model_config, lead_agent_model_config
from harness.offload import (
    offload_large_response_fields,
    offload_large_tool_result,
    strip_image_payload,
)
from harness.plan_executor import ABCPPlanExecutor
from harness.progress import ProgressAccountant
from harness.render_recovery import (
    RenderRecoveryOutcome,
    build_render_recovery_runner,
    call_with_render_recovery,
)
from harness.schema_loader import (
    CapabilityBundle,
    _capability_actions_from_response,
    build_capability_digest,
    load_capability_bundle,
)
from harness.schema_cache import (
    SCHEMA_BOOTSTRAP_AGENT_ID,
    SchemaCacheStatus,
    capability_hash,
    global_schema_cache_dir,
    global_schemas_dir,
    read_cached_capability_hash,
    read_schema_methods_from_dirs,
    schema_bootstrap_lock,
    write_cached_capability_hash,
)
from harness.spawner import BrowserAgentHandle, BrowserAgentSpawner
from harness.strategy_bank import (
    compact_strategy_bank,
    load_strategy_bank,
    render_strategy_guidance,
    select_strategies_for_phase,
)
from harness.task_control import (
    find_phase,
    initialize_task_state,
    load_task_state,
    next_pending_phase,
    phase_contract,
    validate_task_plan,
    write_task_plan,
)
from harness.tool_policy import (
    ALWAYS_FORBIDDEN_ABCP_METHODS,
    HARNESS_TOOL_NAMES,
    filter_capability_methods_for_task_type,
)
from harness.tools.browser_tools import (
    build_browser_agent_tool_specs,
    build_browser_tool_dispatcher,
)
from harness.tools.lead_tools import (
    build_lead_agent_tool_specs,
    build_lead_tool_dispatcher,
)
from harness.utils import (
    JsonDict,
    RunLogger,
    build_static_context_block,
    exception_payload,
    make_browser_event_logger,
    strip_llm_hidden_fields,
    trim_large_strings,
    write_context_snapshot,
)
from llm import BaseLLMProvider, LLMFactory, ModelConfig


# ABCP capability methods we strip from the BrowserAgent tool surface because
# they have known server-side contract bugs and burn worker steps without
# making progress. See repro_hitl_bug.py for evidence:
#   - Hitl.getTaskSummary: "Proxied actions require a fleetId for routing"
#     even with fleetId in params (schema/dispatch mismatch).
#   - Hitl.resumeEvent: listed in System.getCapabilities but the dispatcher
#     returns -32601 Method not found.
# Wait/resume is now handled by harness/hitl.py via the notification hub +
# Page.getState fallback (PR #4), so the model has no legitimate reason to
# touch these methods. Once ABCP ships a fix, drop from this set.
_BLOCKED_CAPABILITIES: Set[str] = {
    *ALWAYS_FORBIDDEN_ABCP_METHODS,
}


def _is_context_limit_exception(exc: BaseException) -> bool:
    """Provider-agnostic detection of model-context-window errors.

    Matches on the error message rather than on a specific SDK exception class
    so that swapping providers doesn't silently drop this signal.
    """
    msg = str(exc or "").lower()
    if not msg:
        return False
    return any(marker in msg for marker in CONTEXT_LIMIT_ERROR_MARKERS)


def offload_tool_result_for_model(
    *,
    logger: RunLogger,
    runtime: RuntimeConfig,
    tool_call: JsonDict,
    result: Any,
    step: int,
) -> Any:
    model_result = strip_llm_hidden_fields(result)
    return offload_large_tool_result(
        logger=logger,
        tool_name=str(tool_call.get("name") or "tool"),
        result=model_result,
        step=step,
        prefix=runtime.agent_id,
        threshold_bytes=runtime.harness.tool_result_offload_threshold_bytes,
    )


class BrowserAgent:
    def __init__(
        self,
        provider: BaseLLMProvider,
        browser: ABCPClient,
        runtime: RuntimeConfig,
        logger: RunLogger,
    ):
        self.provider = provider
        self.browser = browser
        self.runtime = runtime
        self.logger = logger
        self.capabilities: List[JsonDict] = []
        self.capability_methods: Set[str] = set()
        self.method_schemas: Dict[str, JsonDict] = {}
        self.methods_requiring_purpose: Set[str] = set()
        self.purpose_hints: Dict[str, str] = {}
        self.skills_doc: str = ""
        self.artifacts: List[str] = []
        self.trace: List[JsonDict] = []
        self.final_status = WORKER_STATUS_RUNNING
        self.diagnostics = WorkerDiagnostics()
        self.progress = ProgressAccountant()
        self.challenge_tracker = ChallengeTracker()
        self._render_recovery_recent: Dict[str, float] = {}
        self.render_recovery_runner = None
        self.recent_tool_signatures: List[str] = []
        self.static_context_block, self.static_context_hash = build_static_context_block(
            self.runtime.harness.context_file
        )

    async def run(self, task: str) -> str:
        step = 0
        final_answer = ""
        final_status = WORKER_STATUS_RUNNING
        should_finish = False
        completed = False
        model_reported_status: Optional[str] = None
        system_prompt = ""
        tools: List[JsonDict] = []
        messages: List[JsonDict] = []

        try:
            bootstrap = await self._bootstrap_browser()
            system_prompt = self._build_system_prompt()
            tools = build_browser_agent_tool_specs(self._visible_capability_methods())
            dispatch_tool = build_browser_tool_dispatcher(self)
            self.render_recovery_runner = build_render_recovery_runner(
                browser=self.browser,
                logger=self.logger,
                capability_methods=self.capability_methods,
                recent_recoveries=self._render_recovery_recent,
            )
            dynamic_context = self._build_dynamic_context(bootstrap)

            messages = [
                {
                    "role": "user",
                    "content": (
                        f"<user_task>\n{task}\n</user_task>\n\n"
                        f"<dynamic_context>\n{dynamic_context}\n</dynamic_context>\n\n"
                        "Plan autonomously and invoke browser_call to accomplish the task. Call final_answer when you are done."
                    ),
                }
            ]

            for step in range(1, self.runtime.harness.max_steps + 1):
                messages = compact_messages_if_needed(
                    logger=self.logger,
                    actor="browser_agent",
                    step=step,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                    config=self.runtime.harness,
                )
                self.logger.write("agent.step.start", {"step": step})
                step_system_prompt = self._maybe_apply_step_cap_reminder(
                    system_prompt=system_prompt,
                    step=step,
                    max_steps=self.runtime.harness.max_steps,
                )
                text, tool_calls, stop_reason, usage = await self.provider.generate_response(
                    system_prompt=step_system_prompt,
                    messages=messages,
                    tools=tools,
                )
                self.logger.record_llm_usage(
                    source="browser_agent",
                    provider=self.runtime.model.provider,
                    model=self.runtime.model.model_id,
                    usage=usage,
                    step=step,
                    conversation_id=f"browser:{self.runtime.agent_id}",
                    context_hash=self.static_context_hash,
                )
                self.logger.write(
                    "agent.model",
                    {
                        "step": step,
                        "text": text,
                        "tool_calls": tool_calls,
                        "stop_reason": stop_reason,
                    },
                )
                self.trace.append({
                    "type": "model",
                    "step": step,
                    "text": text,
                    "tool_calls": [
                        {
                            "name": item.get("name"),
                            "input": item.get("input", {}),
                        }
                        for item in tool_calls
                    ],
                })

                if not tool_calls:
                    final_answer = text.strip()
                    # Treat a text-only assistant turn as a self-reported done;
                    # the classifier below may still override if a hard signal
                    # was raised (e.g. earlier api contract errors).
                    model_reported_status = WORKER_STATUS_DONE
                    should_finish = True
                    break

                assistant_content: List[JsonDict] = []
                prefix_blocks = usage.get("_assistant_prefix_blocks") if isinstance(usage, dict) else None
                if prefix_blocks:
                    assistant_content.extend(prefix_blocks)
                if text:
                    assistant_content.append({"type": "text", "text": text})
                for tool_call in tool_calls:
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": tool_call["id"],
                            "name": tool_call["name"],
                            "input": tool_call.get("input", {}),
                        }
                    )
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results: List[JsonDict] = []
                for tool_call in tool_calls:
                    result, should_stop = await dispatch_tool(tool_call, step)
                    self._observe_tool_result(tool_call, result)
                    model_result = offload_tool_result_for_model(
                        logger=self.logger,
                        runtime=self.runtime,
                        tool_call=tool_call,
                        result=result,
                        step=step,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call["id"],
                            "content": self._to_model_json(model_result),
                        }
                    )
                    if should_stop:
                        final_answer = result.get("answer", "")
                        model_reported_status = (
                            str(result.get("status")) if result.get("status") else None
                        )
                        should_finish = True
                        break

                messages.append({"role": "user", "content": tool_results})
                if should_finish:
                    break

            reached_step_cap = not should_finish
            final_status, override_reason = classify_terminal_status(
                diagnostics=self.diagnostics,
                model_reported_status=model_reported_status,
                reached_step_cap=reached_step_cap,
                has_extraction_artifact=self._has_extraction_artifact(),
            )
            if reached_step_cap and not final_answer:
                final_answer = self._compose_step_cap_message(final_status)
            elif not final_answer:
                final_answer = "Task ended without the model providing a final answer."

            self.final_status = final_status
            self._write_agent_final(
                final_status=final_status,
                final_answer=final_answer,
                model_reported_status=model_reported_status,
                override_reason=override_reason,
                reached_step_cap=reached_step_cap,
            )
            completed = True
            return final_answer
        except asyncio.CancelledError as exc:
            self.logger.write(
                "agent.cancelled",
                exception_payload(exc, last_step=step, artifacts=self.artifacts),
            )
            raise
        except Exception as exc:
            self.diagnostics.record_exception(exc)
            self.logger.write(
                "agent.error",
                exception_payload(exc, last_step=step, artifacts=self.artifacts),
            )
            if _is_context_limit_exception(exc):
                # Promote to a hard worker status so LeadAgent can react
                # (otherwise spawner wraps as generic "failed").
                final_status, override_reason = classify_terminal_status(
                    diagnostics=self.diagnostics,
                    model_reported_status=None,
                    reached_step_cap=False,
                )
                if final_status == WORKER_STATUS_CONTEXT_LIMIT:
                    self.final_status = final_status
                    final_answer = (
                        "Model token limit hit; unable to continue."
                        f" diagnostic: {self.diagnostics.last_exception_message or ''}"
                    )[:600]
                    self._write_agent_final(
                        final_status=final_status,
                        final_answer=final_answer,
                        model_reported_status=None,
                        override_reason=override_reason,
                        reached_step_cap=False,
                    )
                    completed = True
                    return final_answer
            raise
        finally:
            try:
                write_context_snapshot(
                    self.logger,
                    actor="browser_agent",
                    name=self.runtime.agent_id,
                    system_prompt=system_prompt or "(not initialized)",
                    messages=messages,
                    tools=tools,
                    metadata={
                        "agent_id": self.runtime.agent_id,
                        "last_step": step,
                        "completed": completed,
                        "final_status": self.final_status,
                        "final_answer": final_answer,
                        "artifacts": self.artifacts,
                    },
                )
            except Exception as exc:
                self.logger.write(
                    "context.snapshot.failed",
                    exception_payload(exc, actor="browser_agent"),
                )
            if not completed:
                self.logger.write(
                    "agent.interrupted",
                    {
                        "last_step": step,
                        "has_final_answer": bool(final_answer),
                        "artifacts": self.artifacts,
                    },
                )

    async def _bootstrap_browser(self) -> JsonDict:
        registration = await self.browser.call(
            "System.register", {"agentId": self.runtime.agent_id}
        )
        bundle: CapabilityBundle = await load_capability_bundle(
            self.browser,
            logger=self.logger,
            blocked_methods=_BLOCKED_CAPABILITIES,
            schema_cache_dir=global_schemas_dir(self.runtime.harness.worktree_dir),
        )

        self.capabilities = list(bundle.capabilities)
        self.capability_methods = set(bundle.capability_methods)
        self.method_schemas = dict(bundle.method_schemas)
        self.methods_requiring_purpose = set(bundle.methods_requiring_purpose)
        self.purpose_hints = dict(bundle.purpose_hints)
        self.skills_doc = bundle.skills_doc

        vl_cfg = self.runtime.harness.vl
        bootstrap = {
            "registration": self._trim_for_log(registration),
            "capability_count": len(self.capabilities),
            "schema_count": len(self.method_schemas),
            "requires_purpose_count": len(self.methods_requiring_purpose),
            "skills_doc_chars": len(self.skills_doc),
            "vl": {
                "enabled": bool(getattr(vl_cfg, "enabled", False)),
                "provider": str(getattr(vl_cfg, "provider", "") or ""),
                "model_id": str(getattr(vl_cfg, "model_id", "") or ""),
                "max_checks_per_worker": int(
                    getattr(vl_cfg, "max_checks_per_worker", 0) or 0
                ),
            },
        }
        self.logger.write("browser.bootstrap", bootstrap)
        return bootstrap

    def _build_dynamic_context(self, bootstrap: JsonDict) -> str:
        return json.dumps(
            {
                "bootstrap": bootstrap,
                "memory_context": self.runtime.harness.memory_context,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    def _build_system_prompt(self) -> str:
        visible_methods = self._visible_capability_methods()
        bundle = CapabilityBundle(
            capabilities=[
                cap for cap in self.capabilities
                if str(cap.get("method") or "") in visible_methods
            ],
            capability_methods=visible_methods,
            method_schemas={
                method: schema
                for method, schema in self.method_schemas.items()
                if method in visible_methods
            },
            methods_requiring_purpose=self.methods_requiring_purpose,
            purpose_hints=self.purpose_hints,
            skills_doc=self.skills_doc,
        )
        digest = build_capability_digest(bundle)
        skills_section = (
            f"""

==================== ABCP Official Skills Manual (System.skillsDoc) ====================
{self.skills_doc}
=========================================================================================
"""
            if self.skills_doc
            else """

(System.skillsDoc unavailable on this server; rely on the capability list and the cached schemas below.)
"""
        )

        return f"""You are the control core of the ABCP Browser agent harness.

ABCP automation is performed by invoking atomic browser capabilities — never CDP, Playwright, pixel-coordinate guessing, or hand-crafted selector exploration.
{skills_section}
Available capabilities for this task_type (method · required params · summary; full schemas cached globally at global_schema_cache/schemas/<Method>.json):
{digest}

Harness-specific protocol (NOT covered by the official manual above, but mandatory):
- browser_call arguments always go inside `params` as a JSON object; pass {{}} when there are no params.
- ABCP `suggested_prompt` fields are intentionally hidden from the model; use the factual `observation`/`data` plus harness `next_instruction` instead.
- On call errors the tool_result attaches `methodSchema` (sourced from the cached System.describeAction); inspect its `params` field, fix the call, retry. The canonical schema cache is `global_schema_cache/schemas/<Method>.json`; do not search the task worktree for schema files.
- For methods whose schema marks `requiresPurpose: true`, the harness auto-fills `purpose` from your `browser_call.reason` when you omit it; if `reason` is empty it falls back to the schema's `purposeHint`. Provide a clear `reason` whenever you can.
- Large DOM/text/attribute/screenshot results and generic tool_results are auto-offloaded under worktree/<task>/observations/; the in-context payload only retains `savedPath`, `outline`, `format`, and `query_with`. When you need the details, follow `query_with` and call `local_fs_search`, `local_fs_read`, or `local_fs_jsonpath` against the current task worktree.
- Screenshots produce a `savedPath`. When a VL model is configured, use `visual_verify` for bounded visual checks; otherwise treat screenshots as evidence artifacts only.
- Never fabricate pageId, fleetId, downloadId, bookmarkId, or similar ids — read them from the previous browser_call's `response.data`.
- After a successful Hitl.requestPause, the harness handles the wait itself via System.notification, Hitl.resolvePause, and a short confirmation check after settled page lifecycle events. The response will include a `hitl_wait` field: continue only when `status="resumed"`, and on `status="timeout"` or `status="page_settled_after_hitl"` call final_answer with an incomplete/blocker status. **Do not call any Hitl.* method again or poll Page.getState yourself.**
- Use `extract_dom_records` for repeated lists, tables, cards, product tiles, search results, and link collections.
- Use `eval_js_json` instead of raw Runtime.evaluate when you need structured JS data returned. It wraps the expression with an explicit return and falls back to the title side-channel.
- Use `navigate_verified` instead of raw Page.navigate when the next step depends on being on the exact URL/page.
- Use `visual_verify` only as a bounded visual arbiter after click/navigation uncertainty, validator failure, overlays/CAPTCHA, or layout mismatch. Do not use it for bulk data extraction.
- The harness may consume one or more `vl.max_checks_per_worker` slots automatically for challenge adjudication. Plan explicit `visual_verify` use accordingly.
- Do not call DOM.getSemanticTree. Current ABCP builds have reproduced renderer crashes after this call; use DOM.getAXTree, extract_dom_records, or eval_js_json instead.
- Harness tools are default-allowed by contract policy: final_answer, record_extraction, local_fs_search/read/jsonpath, extract_dom_records, eval_js_json, navigate_verified, visual_verify. ABCP atomic methods are governed by task_type tool policy plus explicit forbidden_methods, not by LLM-authored allowed_methods.
- AXTree is for discovering structure and interaction anchors; do not treat a large AXTree/offload file as the final data source for bulk extraction.
- The `[id]` tokens returned by DOM.getAXTree are rigid physical anchors; prefer them as the `selector` for subsequent Input.click / Input.type / Input.scroll calls.
- **Structured data MUST be persisted as an artifact.** Any list / row / field value (URLs, ids, titles, prices, …) that you plan to hand off to LeadAgent / SkillAgent must, immediately after extraction, be passed to `record_extraction({{name, rows[, schema, description]}})`. Only data that has gone through this call counts as verified. The `answer` field of `final_answer` should reference these savedPaths instead of inlining the full row set.
- When you finish the task — or determine that it cannot continue — call `final_answer` with a concise summary of the outcome, the key page state, and any items requiring human follow-up.
""" + self.static_context_block

    def _visible_capability_methods(self) -> Set[str]:
        task_type = None
        contract = getattr(self, "worker_contract", None)
        if isinstance(contract, dict):
            task_type = contract.get("task_type")
        return filter_capability_methods_for_task_type(
            self.capability_methods,
            task_type or "general",
        )

    def _capture_artifacts(self, method: str, response: Any) -> Any:
        if not isinstance(response, dict):
            return response
        return strip_image_payload(
            logger=self.logger,
            method=method,
            response=response,
            artifacts=self.artifacts,
            prefix=self.runtime.agent_id,
        )

    def _offload_response(
        self,
        method: str,
        params: JsonDict,
        response: Any,
        step: int,
    ) -> Any:
        return offload_large_response_fields(
            logger=self.logger,
            method=method,
            params=params,
            response=response,
            step=step,
            prefix=self.runtime.agent_id,
            threshold_bytes=self.runtime.harness.offload_threshold_bytes,
        )

    def _to_model_json(self, value: Any) -> str:
        return json.dumps(
            self._clean_for_model(value),
            ensure_ascii=False,
            default=str,
        )

    def _clean_for_model(self, value: Any) -> Any:
        return trim_large_strings(
            strip_llm_hidden_fields(value),
            max_chars=self.runtime.harness.max_observation_chars,
        )

    def _trim_for_model(self, value: Any) -> Any:
        return trim_large_strings(
            value,
            max_chars=self.runtime.harness.max_observation_chars,
        )

    def _trim_for_log(self, value: Any) -> Any:
        return trim_large_strings(value, max_chars=8000)

    def _maybe_apply_step_cap_reminder(
        self, *, system_prompt: str, step: int, max_steps: int,
    ) -> str:
        """When the worker is within 2 steps of its cap, prepend a transient
        reminder so the next model turn can checkpoint cleanly via
        final_answer(status="partial"|"incomplete"). The reminder is
        appended only for this single LLM call (not stored in messages or
        the persistent system prompt) so prompt caching remains hot for
        the bulk of the run.
        """
        remaining = max_steps - step
        if remaining > 2:
            return system_prompt
        if remaining <= 0:
            remaining = 1  # we are at the last step
        reminder = (
            "\n\n[HARNESS-CHECKPOINT-REMINDER]\n"
            f"You have {remaining} step(s) left before this worker is hard-stopped.\n"
            "If the task is not finished, call final_answer immediately:\n"
            "  - status=\"partial\" if you have any usable evidence to hand off,\n"
            "  - status=\"extraction_inconclusive\" if repeated JS/AXTree calls"
            " returned null/empty,\n"
            "  - status=\"incomplete\" otherwise.\n"
            "Set the `reason` field to one short sentence describing the blocker."
            " Use `answer` to include savedPath references for any extraction"
            " artifacts you already recorded. Do NOT issue further browser_call"
            " or record_extraction in the next turn unless you are certain it"
            " unblocks completion."
        )
        self.logger.write(
            "agent.step_cap.reminder",
            {"step": step, "max_steps": max_steps, "remaining": remaining},
        )
        return system_prompt + reminder

    def _observe_tool_result(self, tool_call: JsonDict, result: Any) -> None:
        """Feed browser_call results into diagnostics for status classification."""
        if not isinstance(result, dict):
            return
        name = tool_call.get("name")
        method = result.get("method") or ""
        # Direct-capability tools (when ABCP method is wired as a top-level tool)
        # land here with name == method; treat them the same as a browser_call.
        if name == "browser_call" or method:
            if not method:
                return
            params = result.get("params") or {}
            self.diagnostics.observe_browser_call(str(method), params, result)

    def _has_extraction_artifact(self) -> bool:
        """True iff this worker wrote at least one extraction artifact via
        record_extraction. Used by classifier to decide between
        extraction_inconclusive and step_budget_exhausted: if the worker did
        manage to land structured rows somewhere, "extraction inconclusive"
        is the wrong story even if recent JS calls were noisy."""
        for path in self.artifacts:
            if "/artifacts/extractions/" in str(path).replace("\\", "/"):
                return True
        return False

    def _compose_step_cap_message(self, final_status: str) -> str:
        from harness.constants import (
            WORKER_STATUS_CONTEXT_LIMIT,
            WORKER_STATUS_EXTRACTION_INCONCLUSIVE,
            WORKER_STATUS_HITL_TIMEOUT,
            WORKER_STATUS_HITL_WAITING,
            WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
            WORKER_STATUS_PAGE_CRASHED,
            WORKER_STATUS_API_CONTRACT_ERROR,
        )
        hints = {
            WORKER_STATUS_CONTEXT_LIMIT: "Model token limit hit; trim the prompt or split the task for follow-up runs.",
            WORKER_STATUS_HITL_WAITING: "A human-pause was requested but the harness did not enter wait (should disappear once PR #4 lands).",
            WORKER_STATUS_HITL_TIMEOUT: "Human intervention was requested and the wait window elapsed without a resume signal.",
            WORKER_STATUS_PAGE_SETTLED_AFTER_HITL: "The page got past the challenge, but ABCP still reports it paused; platform auto-recovery has not released the control channel.",
            WORKER_STATUS_API_CONTRACT_ERROR: (
                "Repeated ABCP contract errors (method not found / routing / etc.); "
                "do not retry the same API path in the short term."
            ),
            WORKER_STATUS_PAGE_CRASHED: "The page lost its render context repeatedly within the window — rebuild the fleet/page before retrying.",
            WORKER_STATUS_EXTRACTION_INCONCLUSIVE: (
                "Extraction kept failing (JS/AXTree returning null/empty/timeout, etc.); switch probing strategy."
            ),
        }
        suffix = hints.get(final_status, "Reached the maximum orchestration step count without an explicit completion.")
        return f"{suffix} See run log: {self.logger.path}"

    def _write_agent_final(
        self,
        *,
        final_status: str,
        final_answer: str,
        model_reported_status: Optional[str],
        override_reason: Optional[str],
        reached_step_cap: bool,
    ) -> None:
        payload: JsonDict = {
            "status": final_status,
            "statusCategory": status_category(final_status),
            "answer": final_answer,
            "artifacts": self.artifacts,
            "reachedStepCap": reached_step_cap,
            "diagnostics": self.diagnostics.to_log_payload(),
        }
        if model_reported_status and model_reported_status != final_status:
            payload["modelReportedStatus"] = model_reported_status
        if override_reason:
            payload["statusOverrideReason"] = override_reason
        self.logger.write("agent.final", payload)


class LeadAgent:
    """Lead agent that decomposes work and spawns isolated browser agents."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        runtime: RuntimeConfig,
        logger: RunLogger,
    ):
        self.provider = provider
        self.runtime = runtime
        self.logger = logger
        self.spawner = BrowserAgentSpawner(
            runtime,
            logger,
            browser_agent_factory=BrowserAgent,
        )
        self.static_context_block, self.static_context_hash = build_static_context_block(
            self.runtime.harness.context_file
        )
        self.task_plan: Optional[JsonDict] = None
        self.strategy_bank = load_strategy_bank(
            self.runtime.harness.strategy_bank_path
        )
        self.recent_tool_signatures: List[str] = []
        self._current_step: int = 0

    def accept_task_plan(self, raw_plan: Any) -> JsonDict:
        replan_reason = ""
        if self.task_plan is not None:
            if isinstance(raw_plan, dict):
                replan_reason = str(raw_plan.get("replan_reason") or "").strip()
            if not replan_reason:
                result = {
                    "status": "failed",
                    "error": "task_plan already accepted",
                    "next_instruction": (
                        "Do not emit a fresh plan just to retry a failed phase;"
                        " spawn the next pending phase or pass replan_reason to"
                        " intentionally replace the existing task_state."
                    ),
                }
                self.logger.write("task_plan.rejected", result)
                return result

        schema_status, schema_methods = self._schema_cache_status()
        known_abcp_methods: Optional[Set[str]]
        if schema_status == SchemaCacheStatus.LOADED_OK:
            known_abcp_methods = schema_methods
        elif schema_status == SchemaCacheStatus.LOADED_EMPTY:
            known_abcp_methods = None
            self.logger.write(
                "task_plan.validate.warning",
                {
                    "reason": "schema_cache_loaded_but_empty",
                    "impact": "unknown ABCP method check is skipped",
                },
            )
        else:
            known_abcp_methods = None
            self.logger.write(
                "task_plan.validate.degraded",
                {
                    "reason": "schema_cache_not_loaded",
                    "impact": "unknown ABCP method check is skipped",
                },
            )
        plan, errors = validate_task_plan(
            raw_plan,
            known_abcp_methods=known_abcp_methods,
            known_harness_tools=HARNESS_TOOL_NAMES,
        )
        if plan is None:
            result = {
                "status": "failed",
                "errors": errors,
                "next_instruction": (
                    "Fix the task_plan schema and call emit_task_plan again before"
                    " spawning any BrowserAgent."
                ),
            }
            self.logger.write("task_plan.rejected", result)
            return result

        plan_path = write_task_plan(self.logger, plan)
        preserve_from = load_task_state(self.logger) if replan_reason else None
        state = initialize_task_state(
            self.logger,
            plan,
            preserve_from=preserve_from,
            replan_reason=replan_reason,
        )
        self.task_plan = plan
        return {
            "status": "done",
            "planPath": plan_path,
            "phaseCount": len(plan.get("phases", [])),
            "currentPhase": state.get("current_phase"),
            "next_instruction": (
                "Spawn the first pending BrowserAgent phase. Do not spawn phases"
                " that later become phase_failed."
            ),
        }

    def _cached_abcp_methods(self) -> Set[str]:
        return read_schema_methods_from_dirs([
            global_schemas_dir(self.runtime.harness.worktree_dir),
        ])

    def _schema_cache_status(self) -> tuple[SchemaCacheStatus, Set[str]]:
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
        cache_dir = global_schema_cache_dir(self.runtime.harness.worktree_dir)
        schemas_dir = global_schemas_dir(self.runtime.harness.worktree_dir)
        tmp_schemas_dir = cache_dir / f"schemas.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            browser_config = replace(
                self.runtime.browser,
                connect_timeout_seconds=min(
                    float(self.runtime.browser.connect_timeout_seconds),
                    5.0,
                ),
                call_timeout_seconds=min(
                    float(self.runtime.browser.call_timeout_seconds),
                    20.0,
                ),
            )
            event_logger = make_browser_event_logger(
                self.logger,
                self.runtime.harness.log_browser_payloads,
                prefix="schema-bootstrap.transport",
            )
            async with ABCPClient(browser_config, on_event=event_logger) as browser:
                await browser.call(
                    "System.register",
                    {"agentId": SCHEMA_BOOTSTRAP_AGENT_ID},
                )
                caps_response = await browser.call("System.getCapabilities", {})
                capabilities = _capability_actions_from_response(caps_response)
                if not capabilities:
                    self.logger.write(
                        "schema.bootstrap.failed",
                        {
                            "reason": "empty_capabilities",
                            "dataShape": (
                                type(caps_response.get("data")).__name__
                                if isinstance(caps_response, dict)
                                else type(caps_response).__name__
                            ),
                            "fallback": "validate_task_plan will skip unknown-method check",
                        },
                    )
                    return
                digest = capability_hash(capabilities)
                cached_digest = read_cached_capability_hash(cache_dir)
                cached_methods = read_schema_methods_from_dirs([schemas_dir])
                if cached_digest == digest and cached_methods:
                    self.logger.write(
                        "schema.bootstrap.cached",
                        {
                            "cacheDir": str(cache_dir.resolve()),
                            "schemaCount": len(cached_methods),
                            "capabilityHash": digest,
                        },
                    )
                    return

                with schema_bootstrap_lock(cache_dir, timeout_seconds=10.0) as acquired:
                    if not acquired:
                        cached_digest = read_cached_capability_hash(cache_dir)
                        cached_methods = read_schema_methods_from_dirs([schemas_dir])
                        if cached_digest == digest and cached_methods:
                            self.logger.write(
                                "schema.bootstrap.cached",
                                {
                                    "cacheDir": str(cache_dir.resolve()),
                                    "schemaCount": len(cached_methods),
                                    "capabilityHash": digest,
                                    "afterLockTimeout": True,
                                },
                            )
                            return
                        self.logger.write(
                            "schema.bootstrap.lock_timeout",
                            {
                                "cacheDir": str(cache_dir.resolve()),
                                "fallback": "validate_task_plan will skip unknown-method check",
                            },
                        )
                        return

                    cached_digest = read_cached_capability_hash(cache_dir)
                    cached_methods = read_schema_methods_from_dirs([schemas_dir])
                    if cached_digest == digest and cached_methods:
                        self.logger.write(
                            "schema.bootstrap.cached",
                            {
                                "cacheDir": str(cache_dir.resolve()),
                                "schemaCount": len(cached_methods),
                                "capabilityHash": digest,
                                "afterLockWait": True,
                            },
                        )
                        return

                    if tmp_schemas_dir.exists():
                        shutil.rmtree(tmp_schemas_dir, ignore_errors=True)
                    bundle = await load_capability_bundle(
                        browser,
                        logger=self.logger,
                        blocked_methods=_BLOCKED_CAPABILITIES,
                        schemas_dir=tmp_schemas_dir,
                    )
                    if not bundle.method_schemas:
                        shutil.rmtree(tmp_schemas_dir, ignore_errors=True)
                        self.logger.write(
                            "schema.bootstrap.failed",
                            {
                                "reason": "empty_schema_bundle",
                                "fallback": "validate_task_plan will skip unknown-method check",
                            },
                        )
                        return
                    if schemas_dir.exists():
                        shutil.rmtree(schemas_dir, ignore_errors=True)
                    tmp_schemas_dir.rename(schemas_dir)
                    hash_path = write_cached_capability_hash(
                        cache_dir,
                        digest=digest,
                        capability_count=len(capabilities),
                    )
                    self.logger.write(
                        "schema.bootstrap.done",
                        {
                            "cacheDir": str(cache_dir.resolve()),
                            "schemasDir": str(schemas_dir.resolve()),
                            "hashPath": hash_path,
                            "schemaCount": len(bundle.method_schemas),
                            "capabilityHash": digest,
                        },
                    )
        except Exception as exc:
            shutil.rmtree(tmp_schemas_dir, ignore_errors=True)
            self.logger.write(
                "schema.bootstrap.failed",
                {
                    "error": str(exc),
                    "fallback": "validate_task_plan will skip unknown-method check",
                },
            )

    def resolve_phase_for_spawn(self, phase_id: Optional[str]) -> Optional[JsonDict]:
        if self.task_plan is None:
            return None
        if phase_id:
            phase = find_phase(self.task_plan, phase_id)
            if phase is None:
                return None
            state = load_task_state(self.logger)
            phase_state = (
                state.get("phases", {}).get(str(phase.get("id") or ""))
                if isinstance(state.get("phases"), dict)
                else None
            )
            if isinstance(phase_state, dict) and phase_state.get("status") == "phase_failed":
                return None
            return phase
        return next_pending_phase(self.task_plan, self.logger)

    def build_worker_contract(
        self,
        phase: JsonDict,
        override: Optional[JsonDict] = None,
    ) -> JsonDict:
        contract = phase_contract(phase, override)
        if isinstance(self.task_plan, dict):
            contract.setdefault("task_type", self.task_plan.get("task_type") or "general")
        return contract

    def strategy_guidance_for_phase(self, phase: JsonDict) -> str:
        task_type = None
        if isinstance(self.task_plan, dict):
            task_type = str(self.task_plan.get("task_type") or "") or None
        strategies = select_strategies_for_phase(
            self.strategy_bank,
            task_type=task_type,
            phase=phase,
            limit=3,
        )
        return render_strategy_guidance(strategies)

    async def run(self, task: str) -> str:
        system_prompt = ""
        messages: List[JsonDict] = []
        tools: List[JsonDict] = []
        step = 0
        final_answer = ""
        should_finish = False
        completed = False

        await self._bootstrap_schema_cache()
        runtime_limits = json.dumps(
            {
                "max_browser_agents": self.runtime.harness.max_browser_agents,
                "default_worker_concurrency": (
                    self.runtime.harness.default_worker_concurrency
                ),
                "lead_max_steps": self.runtime.harness.lead_max_steps,
                "worker_max_steps": self.runtime.harness.worker_max_steps,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"<user_task>\n{task}\n</user_task>\n\n"
                    f"<runtime_limits>\n{runtime_limits}\n</runtime_limits>\n\n"
                    "Act as the LeadAgent: decompose the task, spawn BrowserAgent / SkillAgent as needed, "
                    "and call final_answer with the final result."
                ),
            }
        ]
        tools = build_lead_agent_tool_specs()
        dispatch_tool = build_lead_tool_dispatcher(self)
        system_prompt = self._build_system_prompt()

        try:
            for step in range(1, self.runtime.harness.lead_max_steps + 1):
                messages = compact_messages_if_needed(
                    logger=self.logger,
                    actor="lead_agent",
                    step=step,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                    config=self.runtime.harness,
                )
                self.logger.write("lead.step.start", {"step": step})
                self._current_step = step
                text, tool_calls, stop_reason, usage = await self.provider.generate_response(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                )
                self.logger.record_llm_usage(
                    source="lead_agent",
                    provider=self.runtime.model.provider,
                    model=self.runtime.model.model_id,
                    usage=usage,
                    step=step,
                    conversation_id=f"lead:{self.runtime.agent_id}",
                    context_hash=self.static_context_hash,
                )
                self.logger.write(
                    "lead.model",
                    {
                        "step": step,
                        "text": text,
                        "tool_calls": tool_calls,
                        "stop_reason": stop_reason,
                    },
                )

                if not tool_calls:
                    final_answer = text.strip()
                    should_finish = True
                    break

                assistant_content: List[JsonDict] = []
                prefix_blocks = usage.get("_assistant_prefix_blocks") if isinstance(usage, dict) else None
                if prefix_blocks:
                    assistant_content.extend(prefix_blocks)
                if text:
                    assistant_content.append({"type": "text", "text": text})
                for tool_call in tool_calls:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tool_call["id"],
                        "name": tool_call["name"],
                        "input": tool_call.get("input", {}),
                    })
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results: List[JsonDict] = []
                for tool_call in tool_calls:
                    result, should_stop = await dispatch_tool(tool_call)
                    model_result = offload_tool_result_for_model(
                        logger=self.logger,
                        runtime=self.runtime,
                        tool_call=tool_call,
                        result=result,
                        step=step,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_call["id"],
                        "content": json.dumps(
                            trim_large_strings(
                                model_result,
                                self.runtime.harness.max_observation_chars,
                            ),
                            ensure_ascii=False,
                            default=str,
                        ),
                    })
                    if should_stop:
                        final_answer = result.get("answer", "")
                        should_finish = True
                        break

                messages.append({"role": "user", "content": tool_results})
                if should_finish:
                    break
            completed = True
        except asyncio.CancelledError as exc:
            self.logger.write(
                "lead.cancelled",
                exception_payload(exc, last_step=step),
            )
            raise
        except Exception as exc:
            self.logger.write(
                "lead.error",
                exception_payload(exc, last_step=step),
            )
            raise
        finally:
            try:
                snapshot_final_answer = final_answer
                if not snapshot_final_answer and completed:
                    snapshot_final_answer = (
                        "LeadAgent reached the maximum orchestration step count without an explicit completion. "
                        f"See run log: {self.logger.path}"
                    )
                write_context_snapshot(
                    self.logger,
                    actor="lead_agent",
                    name="lead_agent",
                    system_prompt=system_prompt or "(not initialized)",
                    messages=messages,
                    tools=tools,
                    metadata={
                        "agent_id": self.runtime.agent_id,
                        "last_step": step,
                        "completed": completed,
                        "final_answer": snapshot_final_answer,
                        "has_task_plan": self.task_plan is not None,
                    },
                )
            except Exception as exc:
                self.logger.write(
                    "context.snapshot.failed",
                    exception_payload(exc, actor="lead_agent"),
                )
            await self.spawner.shutdown()
            if not completed:
                self.logger.write(
                    "lead.interrupted",
                    {
                        "last_step": step,
                        "has_final_answer": bool(final_answer),
                    },
                )

        if not final_answer:
            final_answer = (
                "LeadAgent reached the maximum orchestration step count without an explicit completion. "
                f"See run log: {self.logger.path}"
            )
        self.logger.write("lead.final", {"answer": final_answer})
        return final_answer

    def _build_system_prompt(self) -> str:
        strategy_bank_json = json.dumps(
            compact_strategy_bank(self.strategy_bank),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        return """You are the ABCP LeadAgent, responsible for orchestrating multiple agents to perform complex web data collection and form-filling tasks.

You cannot drive the browser directly; you MUST go through the harness tools to spawn a BrowserAgent or to run a SkillAgent / ABCP plan.

Strategy bank is read-only v1 guidance, not a hard script. Prefer matching strategies before free exploration; if they fail, summarize the failure signature and switch strategy on the next worker instead of retrying the same surface.
<strategy_bank>
""" + strategy_bank_json + """
</strategy_bank>

Recommended state flow (token-thrifty by default):
0. You MUST first call `emit_task_plan` with a v1 linear phase plan. Each phase needs objective, worker_task, expected_artifact, validators, worker_contract, and max_attempts. Do not spawn a BrowserAgent before the plan is accepted.
   Do not hand-author ABCP allow-lists. BrowserAgent tool access is governed by task_type policy plus explicit forbidden_methods; strategy_bank guidance recommends tool order but does not grant permissions.
1. spawn_browser_agent × 1 for the first pending phase: explore the list page — obtain the URL list, pagination rules, and a sample detail URL.
2. spawn_browser_agent × 1 for the next pending phase: explore the first detail page — confirm target fields, stable selectors / node ids, required wait conditions, and abnormal signals.
3. run_skill_agent: distill a deterministic ABCP-steps template from the previous two traces. The template MUST declare variables (e.g. item.url), output fields, success criteria, and recoverable failure points; capture extraction output with `save_as="output"` whenever possible.
4. Prefer `run_abcp_plan_batch(validate_first_n=2 or 3)` for the trial run. It serially validates the first N items first, and only fans out to the rest after the validation set fully passes.
5. If it returns `validation_hitl_required`, the validation sample triggered human-in-the-loop intervention. Wait for / prompt the user, then retry the same batch or the failed item — do NOT immediately fall back to an LLM.
6. If it returns `validation_failed`, the template is not yet ready to scale. Inspect `failed_details.step_results` / `failed_step`, fix the steps, and retry on a small sample.
7. If it returns `partial_hitl_required` or `partial_failed`, the template works in the aggregate but a few items failed. Treat results whose `status != done` (failed_items) as input for subsequent retries or fallbacks.
8. Only fall back to `run_browser_batch` or a per-item BrowserAgent for `failed_items` when the page structure is fundamentally different, visual/semantic judgement is required, a CAPTCHA/login/payment/anti-bot wall blocks deterministic steps, or the step cannot be repaired.
9. Do NOT run `run_browser_batch` over the remaining URLs before the template has passed reuse validation — it is a fallback for heterogeneous pages and failed deterministic plans, not the default path.
10. Stay within the concurrency limits declared in `runtime_limits` (browser agents / plan executors). Use `concurrency` to bound batch tasks.
11. Every subtask must have a clear list of target fields, an explicit output format, and an explicit stop condition.
12. LeadAgent tools use strict schemas: structured arguments (`steps`, `items`, `variables`) are passed as JSON-string fields. Pass `"{}"` for empty variables; each step's `params` in `steps_json` MUST be a JSON object.
13. When generating an ABCP plan template, every method whose schema marks `requiresPurpose: true` (e.g. Page.navigate, DOM.*, Input.*, Runtime.*, Hitl.*) must carry a `purpose` field inside `step.params`. The dispatcher enforces this; the plan executor will additionally auto-fill from the schema's `purposeHint` as a last resort, but explicit purposes lead to cleaner traces.
14. BrowserAgent returns only `answer`, `traceSummary`, `tracePath`, and artifact/offload paths — the full trace never lives in your context. Use `local_fs_search`, `local_fs_read`, or `local_fs_jsonpath` against the returned `tracePath` / `savedPath` under the current task worktree when you need the details.
15. When you spawn a BrowserAgent, use `result_contract` to spell out the fields, evidence, and blocking conditions you expect in the worker's `answer`. This stops workers from returning vague summaries.
16. **Evidence-chain enforcement**: when calling `run_skill_agent`, you MUST pass the upstream BrowserAgent's extraction artifact paths (from `spawner.browser.result.artifacts`, the entries under `/artifacts/extractions/*.json`) into the `evidence_artifacts` parameter. SkillAgent may only synthesise URLs / hrefs / ids and similar critical fields from those artifacts. If the upstream worker produced no extraction artifact, **do not spawn a SkillAgent that would have to infer the fields** — re-spawn a BrowserAgent task that explicitly calls `record_extraction` first, then continue.
17. If a phase is returned as `phase_failed` or `spawn_browser_agent` says phase not found / no pending phase, do not retry that phase id. Report the blocker or emit a new plan in a later system version.

Worker terminal-status decision table — apply this immediately after every `spawner.browser.result`:
- done / partial: data is usable; advance with the `answer`. `partial` means the worker explicitly only finished a subset — note the uncovered range in your final answer.
- step_budget_exhausted: steps ran out but progress may still exist. Check `traceSummary` for usable evidence first; if there is some, relay (spawn a more focused continuation), otherwise change strategy.
- context_limit_exceeded: context window overflowed. Do NOT retry verbatim — spawn with narrower task boundaries and a slimmer `result_contract`; ban `DOM.getSemanticTree`.
- page_crashed: the page's render context is broken. The next spawn must explicitly rebuild the fleet or open a fresh page; do not reuse the old pageId.
- extraction_inconclusive: the current extraction surface (JS / AXTree) cannot reach the data. Swap probing strategy (e.g. AXTree → JS or vice versa, or screenshot + VL when configured); otherwise route to human fallback.
- hitl_waiting: a human pause was requested but the harness did not absorb the wait — this is an infrastructure regression signal. **Do NOT auto-spawn the same task again**; report to the user and stop this subtask.
- hitl_timeout: the wait elapsed without human action. Surface the request to the user; do not retry by default.
- page_settled_after_hitl: the page appears past the challenge, but ABCP still rejects tools as paused. Treat this as platform HITL auto-recovery not yet releasing the control channel; do not auto-spawn the same task.
- browser_api_contract_error: the ABCP contract rejected the call (method missing / routing error / etc). **Do NOT retry along the same path**; switch method or report the platform-side bug.
- failed / cancelled / unknown: inspect `error` and `diagnostics`. Be conservative — do not auto-scale further execution.

The `statusCategory` field is the coarse bucket of the above (done / recoverable / needs_human / fatal / unknown) and is suitable for fast branching.

The final answer must include:
- The completed data range or the location of the collected results.
- The failing / blocking URLs and their reasons (with worker status / statusCategory).
- Whether the reusable strategy succeeded.
""" + self.static_context_block


__all__ = [
    "ABCPClient",
    "ABCPClientConfig",
    "ABCPPlanExecutor",
    "ABCPTransportError",
    "BaseLLMProvider",
    "BrowserAgent",
    "BrowserAgentHandle",
    "BrowserAgentSpawner",
    "HarnessConfig",
    "JsonDict",
    "LLMFactory",
    "LeadAgent",
    "ModelConfig",
    "RenderRecoveryOutcome",
    "RuntimeConfig",
    "RunLogger",
    "VLConfig",
    "browser_agent_model_config",
    "build_browser_agent_tool_specs",
    "build_browser_tool_dispatcher",
    "build_lead_agent_tool_specs",
    "build_lead_tool_dispatcher",
    "build_capability_digest",
    "build_render_recovery_runner",
    "call_with_render_recovery",
    "compact_messages_if_needed",
    "exception_payload",
    "lead_agent_model_config",
    "local_fs_jsonpath",
    "local_fs_read",
    "local_fs_search",
    "make_browser_event_logger",
    "offload_large_response_fields",
    "offload_large_tool_result",
    "offload_tool_result_for_model",
    "strip_image_payload",
    "trim_large_strings",
    "validate_tool_pairing",
]
