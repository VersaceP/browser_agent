"""Lead execution loop."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, List
from harness.constants import WORKER_STATUS_INCOMPLETE
from harness.tools.path_authorization import wait_for_local_authorization
from harness.runtime.lifecycle import LifecycleContext
from harness.task_control import load_task_state
from harness.results.completion_receipt import build_completion_receipt, persist_completion_receipt
from harness.tools.lead_tools import build_lead_agent_tool_specs, build_lead_tool_dispatcher
from harness.version import version_info
from harness.utils import JsonDict, exception_payload, trim_large_strings, write_context_snapshot
from llm import LLMConnectionError, LLMEmptyResponseError, LLMProviderProtocolError, LLMRateLimitError, LLMRequestTimeoutError, retry_usage_from_attempts
from harness.runtime.model_support import (
    MODEL_TIMEOUT_ATTEMPT_LIMIT,
    TRUNCATION_STREAK_LIMIT,
    _assistant_message_from_parts,
    _deferred_tool_result,
    _store_received_model_output,
    _tool_result_digest,
    _tool_result_is_error,
    _truncation_info,
    compact_and_track_prefix_rebuild,
    generate_response_surviving_moderation,
    llm_rate_limit_terminal_result,
    log_model_visible_tool_result,
    offload_tool_result_for_model,
)

async def run_lead_agent(self, task: str) -> str:
    system_prompt = ""
    messages: List[JsonDict] = []
    tools: List[JsonDict] = []
    step = 0
    final_answer = ""
    final_trigger = ""
    final_status = ""
    final_completion_receipt: JsonDict = {}
    should_finish = False
    completed = False
    recorder = self.lifecycle_events
    recorder.agent_start(
        label="lead",
        max_steps=int(self.runtime.harness.lead_max_steps or 0),
        agent_id="lead",
    )
    self.final_status = ""
    self.final_trigger = ""
    self.terminal_error = None
    # Record the effective offload ceilings once per run for telemetry.
    self.logger.write(
        "harness.config",
        {
            **version_info(),
            "agentId": str(self.runtime.agent_id or ""),
            "offloadThresholdBytes": (
                self.runtime.harness.offload_threshold_bytes
            ),
            "toolResultOffloadThresholdBytes": (
                self.runtime.harness.tool_result_offload_threshold_bytes
            ),
        },
    )
    if self.resume is not None:
        base_task = str(self.resume.original_user_task or task or "").strip()
        resume_instruction = str(self.resume.instruction or "").strip()
        self.original_user_task = base_task
    else:
        base_task = str(task or "")
        resume_instruction = ""
        self.original_user_task = base_task
    self.spawner.root_task = self.original_user_task

    await self._bootstrap_schema_cache()
    runtime_limits = json.dumps(
        {
            "max_browser_agent_instances": (
                self.runtime.harness.max_browser_agent_instances
            ),
            "max_browser_agents": self.runtime.harness.max_browser_agents,
            "max_task_fleets": self.runtime.harness.max_task_fleets,
            "lead_max_steps": self.runtime.harness.lead_max_steps,
            "worker_max_steps": self.runtime.harness.worker_max_steps,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    pinned_context = (
        json.dumps(
            self.pinned_browser_context.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if self.pinned_browser_context is not None
        else ""
    )
    resumed_block = ""
    if self.resume is not None:
        resumed_block = (
            "<resumed_state>\n"
            + json.dumps(
                self.resume.prompt_payload(),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n</resumed_state>\n"
            "This is a phase-level resume in a fresh process. Preserve"
            " validated phases and artifacts listed above. Never assume"
            " that a prior worker coroutine, AXTree id, page observation,"
            " or model conversation is still live. Re-perceive any reused"
            " browser page before acting. resumeProjection is a read-only"
            " recovery map: artifactRefs are pointers, not copied values;"
            " partialObservedControlKeys are uncredited observations, not"
            " phase completion. Preserve taskSessionContinuity when it is"
            " required; the spawner owns its fleet/page ids. If worker"
            " dispatch reports that this restored plan lacks an operator"
            " approval receipt, resubmit spawn_browser_agent with that phase_id;"
            " Harness obtains the missing approval without rewriting its contract."
            + (
                " The attributed user inputs in operatorInputs include"
                " resume amendments from this and earlier runs. Compare them with"
                " the original request and current results."
                " If an amendment requires more work, submit a new or replacement"
                " assignment through the normal review and approval path;"
                " preserve prior execution evidence and do not blindly"
                " replay a completed side effect. If one merely clarifies"
                " an unfinished assignment, continue that assignment only"
                " when its existing contract still fits. Do not treat the"
                " new input as a browser permission or a page-resume signal."
                if resume_instruction or (self.resume.report.get("operatorInputs") or []) else
                " Continue the accepted original plan; this resume has no"
                " new user instruction."
            )
            + (
                " Phases listed in"
                " hitlReactivatedPhases were interrupted by a pending human"
                " decision (browser challenge pause or local-file"
                " authorization) and have been reopened as pending; spawn"
                " them directly under their original phase ids — do NOT"
                " replan or rename phases just to make them startable."
                " pendingHumanInterventions shows what decision each phase"
                " is waiting for. Local-file authorization will be asked"
                " again in this terminal when a worker reaches the file"
                " operation; a browser Hitl resume never grants file"
                " permissions. operatorInputReceipts preserves historical"
                " operator text with its candidate or pause identity;"
                " judge whether each answer is still relevant to the"
                " original goal and current evidence. An answer or resume"
                " event alone grants no new file or browser permission."
            )
            + "\n\n"
        )
    known_skills_block = ""
    messages = [
        {
            "role": "user",
            "content": (
                f"<user_task>\n{base_task}\n</user_task>\n\n"
                + known_skills_block
                + (
                    f"<resume_instruction>\n{resume_instruction}\n"
                    "</resume_instruction>\n\n"
                    if resume_instruction else ""
                )
                + resumed_block
                + f"<runtime_limits>\n{runtime_limits}\n</runtime_limits>\n\n"
                + (
                    "<pinned_browser_context>\n"
                    f"{pinned_context}\n"
                    "</pinned_browser_context>\n"
                    "This routing context is immutable control-plane input."
                    " Reuse it and never plan Fleet.create or substitute"
                    " another fleet. When pageId is present, do not plan"
                    " Page.create/Page.close or substitute that page; when"
                    " pageId is absent, Page.create inside the pinned fleet"
                    " remains allowed.\n\n"
                    if pinned_context
                    else ""
                )
                +
                "Act as the LeadAgent: decompose the task, spawn BrowserAgent phases as needed, "
                "and call final_answer with the final result."
            ),
        }
    ]
    dispatch_tool = build_lead_tool_dispatcher(self)
    tools: List[JsonDict] = []
    system_prompt = ""
    prompt_stage = ""
    self.logger.write("lead.model.effective_config", {
        "provider": self.effective_model_config.provider,
        "model": self.effective_model_config.model_id,
        "maxTokens": self.effective_model_config.extra_params.get("max_tokens"),
        "temperature": self.effective_model_config.extra_params.get("temperature"),
        "reasoningEffort": self.effective_model_config.extra_params.get(
            "reasoning_effort"
        ),
        "thinking": (
            self.effective_model_config.extra_params.get("thinking", {}).get("type")
            if isinstance(
                self.effective_model_config.extra_params.get("thinking"), dict
            ) else self.effective_model_config.extra_params.get("thinking")
        ),
    })
    try:
        lead_timeout_step_retries = max(
            0,
            int(
                getattr(
                    self.runtime.harness,
                    "lead_model_timeout_step_retries",
                    1,
                )
                or 0
            ),
        )
    except (TypeError, ValueError):
        lead_timeout_step_retries = 1

    try:
        empty_response_streak = 0
        connection_failure = getattr(self, "_browser_connection_failure", None)
        if connection_failure:
            final_status = "blocked"
            final_trigger = "browser_connection_unavailable"
            final_answer = json.dumps({
                "status": final_status, **connection_failure,
                "next_instruction": "Restore the configured WebCross transport, then /resume this task. The next connection re-reads the runtime descriptor in local mode. No worker was dispatched and no task attempts were consumed.",
            }, ensure_ascii=False)
            self.logger.write("lead.connection_blocked", connection_failure)
        for step in range(1, 1 if connection_failure else self.runtime.harness.lead_max_steps + 1):
            await wait_for_local_authorization(self)
            # Delegation is incremental: the Lead always sees the same
            # execution prompt and assignment interface. A planning-stage
            # context rebuild would hide user supplements and make an
            # accepted assignment look like a new task.
            next_prompt_stage = "execution"
            if next_prompt_stage != prompt_stage:
                prompt_stage = next_prompt_stage
                system_prompt = (
                    self._build_system_prompt()
                    if prompt_stage == "execution"
                    else self._build_planning_system_prompt()
                )
                tools = build_lead_agent_tool_specs(
                    include_resume=self.resume is not None,
                    stage=prompt_stage,
                )
                self.prompt_context_hash = hashlib.sha256(
                    system_prompt.encode("utf-8")
                ).hexdigest()
                self.logger.write("lead.prompt_stage", {
                    "stage": prompt_stage,
                    "step": step,
                    "promptChars": len(system_prompt),
                    "toolCount": len(tools),
                    "toolNames": [tool.get("name") for tool in tools],
                    "contextHash": self.prompt_context_hash,
                })
            force_reason = self._forced_compaction_reason
            self._forced_compaction_reason = None
            messages = await compact_and_track_prefix_rebuild(
                self,
                actor="lead_agent",
                step=step,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                force_reason=force_reason,
            )
            remaining = self.runtime.harness.lead_max_steps - step
            step_reason = (
                "cap_reached"
                if remaining <= 0
                else "near_cap"
                if remaining <= 3
                else "running"
            )
            self.logger.write(
                "lead.step.start",
                {
                    "step": step,
                    "max_steps": self.runtime.harness.lead_max_steps,
                    "remaining": remaining,
                    "reason": step_reason,
                },
            )
            recorder.turn_start(step)
            recorder.message_start()
            self._current_step = step
            self.lifecycle.agent_before_step(
                LifecycleContext(
                    actor="lead_agent",
                    step=step,
                    metadata={"agent_id": self.runtime.agent_id},
                ),
                {
                    "messageCount": len(messages),
                    "toolCount": len(tools),
                },
            )
            model_attempt = 0
            model_call_failed = False
            lead_timeout_attempts_used = 0
            while True:
                model_attempt += 1
                model_call_failed = False
                model_call_started = time.monotonic()
                model_result = None
                self.logger.write("lead.model.request", {
                    "step": step,
                    "attempt": model_attempt,
                    "promptStage": prompt_stage,
                    "systemPromptChars": len(system_prompt),
                    "toolSchemaChars": len(json.dumps(
                        tools, ensure_ascii=False, separators=(",", ":"), default=str,
                    )),
                    "messageChars": len(json.dumps(
                        messages, ensure_ascii=False, separators=(",", ":"), default=str,
                    )),
                })
                try:
                    model_result = await generate_response_surviving_moderation(
                        provider=self.provider,
                        return_result=True,
                        logger=self.logger,
                        actor="lead_agent",
                        step=step,
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=tools,
                    )
                    text, tool_calls, stop_reason, usage = model_result.legacy_fields()
                    break
                except LLMEmptyResponseError as exc:
                    # The provider already burned its own retry budget on
                    # degenerate responses; give the step a fresh provider
                    # call before surfacing. Never raise: an unhandled
                    # degenerate response must end as an explicit
                    # empty_model_response final, not a crashed run.
                    will_retry = model_attempt <= lead_timeout_step_retries
                    self.logger.write(
                        "lead.model_degenerate_response",
                        {
                            "step": step,
                            "attempt": model_attempt,
                            "maxStepRetries": lead_timeout_step_retries,
                            "willRetry": will_retry,
                            "provider": exc.provider,
                            "model": exc.model,
                            "operation": exc.operation,
                            "problem": exc.problem,
                            "providerMaxRetries": exc.max_retries,
                            "attempts": exc.attempts,
                            "messageCount": len(messages),
                        },
                    )
                    # The raising call returns no usage dict; account the
                    # retries it did perform either way, since the
                    # will_retry path never reaches record_llm_usage.
                    retry_usage = retry_usage_from_attempts(exc.attempts)
                    if will_retry:
                        self.logger.record_llm_retries(
                            source="lead_agent", usage=retry_usage,
                        )
                        continue
                    # Surface as an empty turn; the streak guard below owns
                    # recovery and, eventually, the incomplete final.
                    model_call_failed = True
                    text, tool_calls, stop_reason, usage = (
                        "", [], "degenerate_response", retry_usage,
                    )
                    break
                except LLMRequestTimeoutError as exc:
                    lead_timeout_attempts_used += max(1, len(exc.attempts))
                    will_retry = (
                        model_attempt <= lead_timeout_step_retries
                        and lead_timeout_attempts_used
                        < MODEL_TIMEOUT_ATTEMPT_LIMIT
                    )
                    self.logger.write(
                        "lead.model_timeout",
                        {
                            "step": step,
                            "attempt": model_attempt,
                            "maxStepRetries": lead_timeout_step_retries,
                            "willRetry": will_retry,
                            "errorType": type(exc).__name__,
                            "error": str(exc),
                            "provider": exc.provider,
                            "model": exc.model,
                            "operation": exc.operation,
                            "timeoutSeconds": exc.timeout_seconds,
                            "providerMaxRetries": exc.max_retries,
                            "timeoutAttempts": exc.attempts,
                            "timeoutAttemptsInStep": lead_timeout_attempts_used,
                            "timeoutAttemptLimit": MODEL_TIMEOUT_ATTEMPT_LIMIT,
                            "messageCount": len(messages),
                            "toolCount": len(tools),
                        },
                    )
                    self.logger.record_llm_retries(
                        source="lead_agent",
                        usage=retry_usage_from_attempts(exc.attempts),
                    )
                    if not will_retry:
                        raise
                    reason = "llm_timeout_step_retry"
                    self.logger.write(
                        "context.compaction_requested",
                        {
                            "actor": "lead_agent",
                            "step": step,
                            "reason": reason,
                            "triggerStep": step,
                            "triggerAttempt": model_attempt,
                        },
                    )
                    messages = await compact_and_track_prefix_rebuild(
                        self,
                        actor="lead_agent",
                        step=step,
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=tools,
                        force_reason=reason,
                    )
                except LLMConnectionError as exc:
                    # A mid-stream disconnect that outlived the provider's
                    # retry budget gets the same step-level recovery as a
                    # timeout: compact and re-ask. Oversized turns are the
                    # ones gateways cut off, so compaction is treatment,
                    # not just ceremony.
                    will_retry = model_attempt <= lead_timeout_step_retries
                    self.logger.write(
                        "lead.model_connection_error",
                        {
                            "step": step,
                            "attempt": model_attempt,
                            "maxStepRetries": lead_timeout_step_retries,
                            "willRetry": will_retry,
                            "errorType": type(exc).__name__,
                            "error": str(exc),
                            "provider": exc.provider,
                            "model": exc.model,
                            "operation": exc.operation,
                            "reason": exc.reason,
                            "providerMaxRetries": exc.max_retries,
                            "connectionAttempts": exc.attempts,
                            "messageCount": len(messages),
                            "toolCount": len(tools),
                        },
                    )
                    self.logger.record_llm_retries(
                        source="lead_agent",
                        usage=retry_usage_from_attempts(exc.attempts),
                    )
                    if not will_retry:
                        raise
                    reason = "llm_connection_step_retry"
                    self.logger.write(
                        "context.compaction_requested",
                        {
                            "actor": "lead_agent",
                            "step": step,
                            "reason": reason,
                            "triggerStep": step,
                            "triggerAttempt": model_attempt,
                        },
                    )
                    messages = await compact_and_track_prefix_rebuild(
                        self,
                        actor="lead_agent",
                        step=step,
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=tools,
                        force_reason=reason,
                    )
                except LLMProviderProtocolError as exc:
                    will_retry = model_attempt <= lead_timeout_step_retries
                    self.logger.write(
                        "lead.model_protocol_error",
                        {
                            "step": step,
                            "attempt": model_attempt,
                            "maxStepRetries": lead_timeout_step_retries,
                            "willRetry": will_retry,
                            "errorType": type(exc).__name__,
                            "error": str(exc),
                            "provider": exc.provider,
                            "model": exc.model,
                            "operation": exc.operation,
                            "fallbackAttempted": exc.fallback_attempted,
                            "fallbackSkippedReason": exc.fallback_skipped_reason,
                            "protocolAttempts": exc.attempts,
                        },
                    )
                    self.logger.record_llm_retries(
                        source="lead_agent",
                        usage=retry_usage_from_attempts(exc.attempts),
                    )
                    if not will_retry:
                        raise
                    reason = "llm_protocol_step_retry"
                    messages = await compact_and_track_prefix_rebuild(
                        self,
                        actor="lead_agent",
                        step=step,
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=tools,
                        force_reason=reason,
                    )
            if model_call_failed:
                # See the worker: a call that raised carries no usage, so
                # the normal path would invent a call and two cache-drift
                # warnings out of its absent numbers.
                recorder.message_failed(str(stop_reason or "model_call_failed"))
                self.logger.record_llm_retries(
                    source="lead_agent", usage=usage,
                )
            else:
                usage_payload = self.logger.record_llm_usage(
                    source="lead_agent",
                    provider=self.effective_model_config.provider,
                    model=self.effective_model_config.model_id,
                    usage=usage,
                    step=step,
                    conversation_id=f"lead:{self.runtime.agent_id}",
                    context_hash=getattr(
                        self,
                        "prompt_context_hash",
                        self.static_context_hash,
                    ),
                )
                self._observe_cache_pressure(
                    usage_payload,
                    step=step,
                    max_steps=self.runtime.harness.lead_max_steps,
                )
            assistant_message = model_result.message if model_result is not None else _assistant_message_from_parts(
                text=text,
                tool_calls=tool_calls,
                prefix_blocks=None,
                stop_reason=stop_reason,
                usage=usage if isinstance(usage, dict) else None,
            )
            self.logger.write(
                "lead.model",
                {
                    "step": step,
                    "text": text,
                    "tool_calls": tool_calls,
                    "stop_reason": stop_reason,
                    "requestTelemetry": {
                        "promptStage": prompt_stage,
                        "modelAttempt": model_attempt,
                        "modelElapsedMs": int(
                            (time.monotonic() - model_call_started) * 1000
                        ),
                        "systemPromptChars": len(system_prompt),
                        "toolSchemaChars": len(json.dumps(
                            tools, ensure_ascii=False, separators=(",", ":"), default=str,
                        )),
                        "messageChars": len(json.dumps(
                            messages, ensure_ascii=False, separators=(",", ":"), default=str,
                        )),
                    },
                    "outputTelemetry": {
                        # Providers expose a total completion count but not
                        # a stable thinking/tool-argument split.  Record
                        # the available total and content sizes separately;
                        # chars are intentionally not mislabeled as tokens.
                        "providerOutputTokens": (
                            usage.get("output") if isinstance(usage, dict) else None
                        ),
                        "textChars": len(text or ""),
                        "toolArgumentChars": sum(
                            len(json.dumps(
                                item.get("input") if isinstance(item, dict) else {},
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            ))
                            for item in tool_calls if isinstance(item, dict)
                        ),
                        "toolCallCount": len(tool_calls),
                        "prefixBlockTypes": [
                            str(item.get("type") or "")
                            for item in [
                                {"type": "redacted_thinking" if block.redacted else "thinking", "thinking": block.thinking}
                                for block in assistant_message.thinking_blocks()
                            ]
                            if isinstance(item, dict)
                        ],
                        "thinkingChars": sum(
                            len(str(item.get("thinking") or ""))
                            for item in [
                                {"type": "redacted_thinking" if block.redacted else "thinking", "thinking": block.thinking}
                                for block in assistant_message.thinking_blocks()
                            ]
                            if isinstance(item, dict)
                            and str(item.get("type") or "") == "thinking"
                        ),
                    },
                },
            )
            lead_truncation = None
            if stop_reason == "max_tokens":
                # Before message_end, not after: a message_complete() call
                # made once the scope has closed is a no-op, so the lead's
                # truncated turns carried no truncation at all.
                lead_truncation = _store_received_model_output(
                    logger=self.logger, actor="lead", step=step,
                    text=text, stop_reason=str(stop_reason),
                )
            recorder.message_complete(
                assistant_message,
                stop_reason=stop_reason,
                truncation=_truncation_info(lead_truncation),
            )
            recorder.message_end()

            if not tool_calls:
                # A no-tool lead turn with real text is a self-reported
                # final answer. A no-tool turn that is empty or truncated
                # is an incident: task 9d5655d3's lead accepted a
                # degenerate empty end_turn as "done" at step 10/50,
                # silently orphaning a pending phase. Retry with recovery
                # guidance (listing pending phases); only a streak
                # terminates, explicitly labeled — never as step_cap.
                incident = (
                    "truncated" if stop_reason == "max_tokens"
                    else "empty" if not text.strip()
                    else ""
                )
                if incident:
                    empty_response_streak += 1
                    pending_ids = self._pending_phase_ids()
                    # Reuses the receipt written before the scope closed;
                    # an empty (not truncated) turn still needs one.
                    received = lead_truncation or _store_received_model_output(
                        logger=self.logger,
                        actor="lead",
                        step=step,
                        text=text,
                        stop_reason=str(stop_reason or incident),
                    )
                    self.logger.write("lead.empty_model_response", {
                        "step": step,
                        "streak": empty_response_streak,
                        "limit": TRUNCATION_STREAK_LIMIT,
                        "kind": incident,
                        "stop_reason": stop_reason,
                        "text_chars": len(text or ""),
                        "pendingPhases": pending_ids,
                        **({"truncation": received} if received else {}),
                    })
                    if empty_response_streak < TRUNCATION_STREAK_LIMIT:
                        placeholder = (
                            "[response truncated by output-token limit]"
                            if incident == "truncated"
                            else "[empty model response discarded]"
                        )
                        messages.append({"role": "assistant", "content": [{
                            "type": "text",
                            "text": text.strip() or placeholder,
                        }]})
                        incident_detail = (
                            "hit the output-token limit before emitting"
                            " any tool call"
                            if incident == "truncated"
                            else "was empty (no text and no tool call)"
                        )
                        if pending_ids:
                            next_action = (
                                " The task plan still has pending phase(s): "
                                + ", ".join(pending_ids)
                                + ". Either call spawn_browser_agent for the"
                                " next pending phase, or call final_answer"
                                " explaining why you are stopping early."
                            )
                        else:
                            next_action = (
                                " If the task is complete, call final_answer"
                                " with the final result now."
                            )
                        messages.append({"role": "user", "content": [{
                            "type": "text",
                            "text": (
                            "<empty_response_recovery>Your previous"
                            f" response {incident_detail} and was"
                            " discarded. Respond with minimal text and"
                            " exactly one tool call now. Delegate one coherent"
                            " assignment with spawn_browser_agent; reuse recorded"
                            " input and output references instead of copying history."
                            f"{next_action}"
                            "</empty_response_recovery>"
                            ),
                        }]})
                        continue
                    final_trigger = "empty_model_response"
                    final_status = "failed"
                    final_answer = (
                        f"LeadAgent terminated after {empty_response_streak}"
                        " consecutive empty/truncated model responses"
                        + (
                            "; pending phases not executed: "
                            + ", ".join(pending_ids)
                            if pending_ids
                            else ""
                        )
                        + f". See run log: {self.logger.path}"
                    )
                    should_finish = True
                    break
                final_answer = text.strip()
                final_trigger = "model_text"
                final_status = "done"
                should_finish = True
                break
            empty_response_streak = 0

            messages.append(assistant_message)

            tool_results: List[JsonDict] = []
            for tool_index, tool_call in enumerate(tool_calls):
                recorder.tool_start(
                    tool_call_id=str(tool_call.get("id") or ""),
                    tool_name=str(tool_call.get("name") or "tool"),
                    arguments=(
                        tool_call.get("input")
                        if isinstance(tool_call.get("input"), dict) else None
                    ),
                )
                try:
                    result, should_stop = await dispatch_tool(tool_call)
                except BaseException as exc:
                    recorder.tool_failed(
                        f"{type(exc).__name__}: {exc}",
                        status=(
                            "aborted"
                            if isinstance(exc, asyncio.CancelledError)
                            else "error"
                        ),
                    )
                    recorder.tool_end()
                    raise
                recorder.tool_complete(
                    is_error=_tool_result_is_error(result),
                    result_chars=len(str(result)),
                    result_digest=_tool_result_digest(result),
                )
                recorder.tool_end()
                model_result = offload_tool_result_for_model(
                    logger=self.logger,
                    runtime=self.runtime,
                    tool_call=tool_call,
                    result=result,
                    step=step,
                )
                self.logger.write(
                    "lead.tool.result",
                    summarize_lead_tool_result_for_log(
                        tool_call=tool_call,
                        result=result,
                        model_result=model_result,
                        step=step,
                    ),
                )
                content = json.dumps(
                    trim_large_strings(
                        model_result,
                        self.runtime.harness.max_observation_chars,
                    ),
                    ensure_ascii=False,
                    default=str,
                )
                log_model_visible_tool_result(
                    self.logger,
                    actor=str(self.runtime.agent_id),
                    step=step,
                    tool_name=str(tool_call.get("name") or "tool"),
                    raw_result=result,
                    model_result=model_result,
                    final_content=content,
                    method=str(
                        (tool_call.get("input") or {}).get("method") or ""
                    )
                    if isinstance(tool_call.get("input"), dict)
                    else "",
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call["id"],
                    "content": content,
                })
                if should_stop:
                    for deferred in tool_calls[tool_index + 1:]:
                        tool_results.append(_deferred_tool_result(
                            deferred,
                            after_tool_call=tool_call,
                            reason="preceding_tool_terminated_agent",
                        ))
                    final_answer = result.get("answer", "")
                    final_trigger = str(result.get("trigger") or "lead_decided")
                    final_status = str(
                        result.get("status") or "failed"
                    ).strip().lower()
                    receipt = result.get("completionReceipt")
                    if isinstance(receipt, dict):
                        final_completion_receipt = dict(receipt)
                    should_finish = True
                    break

            if not should_finish:
                reminder = self._step_cap_reminder_block(
                    current_step=step,
                    max_steps=self.runtime.harness.lead_max_steps,
                )
                if reminder is not None:
                    tool_results.append(reminder)
            messages.append({"role": "user", "content": tool_results})
            if should_finish:
                break
        # Normalize BEFORE the finally-snapshot so lead.final and the
        # context snapshot carry the same trigger. step_cap is reserved
        # for genuinely exhausting lead_max_steps — task 9d5655d3's empty
        # response at step 10/50 was mislabeled step_cap by the old
        # unconditional fallback, which sent the investigation down the
        # wrong path.
        if not final_answer:
            if step >= self.runtime.harness.lead_max_steps:
                final_trigger = "step_cap"
                final_status = "failed"
                final_answer = (
                    "LeadAgent reached the maximum orchestration step count"
                    " without an explicit completion. "
                    f"See run log: {self.logger.path}"
                )
            else:
                final_trigger = final_trigger or "no_completion"
                final_status = "failed"
                final_answer = (
                    f"LeadAgent stopped at step {step}/"
                    f"{self.runtime.harness.lead_max_steps} without an"
                    f" explicit completion (trigger: {final_trigger})."
                    f" See run log: {self.logger.path}"
                )
        completed = True
    except asyncio.CancelledError as exc:
        self.logger.write(
            "lead.cancelled",
            exception_payload(exc, last_step=step),
        )
        raise
    except LLMRateLimitError as exc:
        incident = exc.to_payload()
        self.terminal_error = incident
        final_trigger = f"llm_{exc.kind}"
        final_status = WORKER_STATUS_INCOMPLETE
        final_answer = json.dumps(
            llm_rate_limit_terminal_result(exc),
            ensure_ascii=False,
        )
        self.logger.write(
            "lead.model_rate_limited",
            {
                "step": step,
                **incident,
            },
        )
        # This is a handled terminal outcome, not an interrupted coroutine.
        # The normal finally block still persists the completion receipt and
        # context snapshot before lead.final is emitted below.
        completed = True
    except Exception as exc:
        self.logger.write(
            "lead.error",
            exception_payload(exc, last_step=step),
        )
        raise
    finally:
        recorder.close(
            status=str(final_status or "unknown"),
            reason=None if completed else "interrupted",
            step_count=step,
        )
        try:
            receipt_state = load_task_state(self.logger)
            receipt_run_id = str(
                getattr(self.logger, "run_id", "") or ""
            ).strip()
            if receipt_run_id:
                persisted_receipt = persist_completion_receipt(
                    logger=self.logger,
                    state=receipt_state,
                    spawner=self.spawner,
                    run_id=receipt_run_id,
                )
                # New CLI runs also persist a run-scoped receipt so a later
                # resume can accumulate downloads/HITL, but keep their
                # long-standing public receipt shape.  Only an actual
                # resumed run exposes currentRun/cumulative at the surface.
                final_completion_receipt = (
                    persisted_receipt
                    if str(
                        getattr(self.logger, "resumed_from", "") or ""
                    ).strip()
                    else persisted_receipt["currentRun"]
                )
            elif not final_completion_receipt:
                final_completion_receipt = build_completion_receipt(
                    state=receipt_state,
                    spawner=self.spawner,
                )
            self.logger.write(
                "lead.completion_receipt",
                {
                    **final_completion_receipt,
                    "generatedForTrigger": (
                        final_trigger or
                        ("interrupted" if not completed else "no_completion")
                    ),
                },
            )
        except Exception as receipt_exc:
            self.logger.write(
                "lead.completion_receipt_failed",
                exception_payload(receipt_exc, last_step=step),
            )
        try:
            # Normal completion normalized final_answer/final_trigger above;
            # on exception paths final_answer may legitimately be empty and
            # the snapshot records it as-is (completed=False tells the story).
            snapshot_final_answer = final_answer
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
                    "final_trigger": final_trigger,
                    "final_status": final_status,
                    "completion_receipt": final_completion_receipt,
                    "has_task_plan": self.task_plan is not None,
                },
                run_id=str(
                    getattr(self.logger, "context_run_id", "") or ""
                ) or None,
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
                    "completionReceipt": final_completion_receipt or None,
                },
            )

    if not final_answer:
        # Defensive only: normal completion always normalizes above.
        final_answer = (
            "LeadAgent finished without an explicit completion. "
            f"See run log: {self.logger.path}"
        )
        final_trigger = final_trigger or "no_completion"
        final_status = final_status or "failed"
    self.logger.write(
        "lead.final",
        {
            "answer": final_answer,
            "status": final_status or "failed",
            "trigger": final_trigger or "unknown",
            "last_step": step,
            "max_steps": self.runtime.harness.lead_max_steps,
            "completionReceipt": final_completion_receipt or None,
        },
    )
    self.final_trigger = final_trigger or "unknown"
    self.final_status = final_status or (
        WORKER_STATUS_INCOMPLETE
        if self.terminal_error is not None
        else "failed"
    )
    return final_answer


def summarize_lead_tool_result_for_log(
    *,
    tool_call: JsonDict,
    result: Any,
    model_result: Any,
    step: int,
) -> JsonDict:
    name = str(tool_call.get("name") or "tool")
    tool_input = tool_call.get("input") if isinstance(tool_call.get("input"), dict) else {}
    source = model_result if isinstance(model_result, dict) else result
    summary: JsonDict = {
        "step": step,
        "tool": name,
    }

    if isinstance(tool_input, dict):
        for key in ("path", "expr", "mode", "phase_id", "name"):
            if tool_input.get(key) is not None:
                summary[key] = tool_input.get(key)
        if name == "wait_browser_agents":
            worker_ids = tool_input.get("worker_ids")
            if isinstance(worker_ids, list):
                summary["workerIds"] = worker_ids[:10]
            summary["waitMode"] = tool_input.get("mode") or "all"
            if tool_input.get("timeout_seconds") is not None:
                summary["timeoutSeconds"] = tool_input.get("timeout_seconds")

    if not isinstance(source, dict):
        summary["resultType"] = type(source).__name__
        return summary

    for key in (
        "status",
        "count",
        "truncated",
        "relativePath",
        "path",
        "expr",
        "mode",
        "maxBytesPerNode",
        "_offloaded",
        "savedPath",
        "byteSize",
        "originalBytes",
        "query_with",
        "phaseCount",
        "currentPhase",
        "error",
        "next_instruction",
    ):
        if key in source:
            summary[key] = source.get(key)

    # Keep structural diagnostics visible even when the model projection is
    # offloaded. Never include the rejected argument values in this summary.
    diagnostic_source = result if isinstance(result, dict) else source
    issues = diagnostic_source.get("issues")
    if isinstance(issues, list):
        schema_issues = [item for item in issues if isinstance(item, dict)]
        summary["issueCount"] = len(schema_issues)
        summary["issues"] = [
            {key: item[key] for key in ("path", "keyword", "message") if key in item}
            for item in schema_issues[:12]
        ]

    completed = source.get("completed")
    if isinstance(completed, list):
        summary["completedCount"] = len(completed)
        summary["workerStatuses"] = [
            {
                "workerId": item.get("workerId"),
                "status": item.get("status"),
                "validatedStatus": item.get("validatedStatus"),
                "phaseId": item.get("phaseId"),
            }
            for item in completed[:10]
            if isinstance(item, dict)
        ]
    pending = source.get("pending")
    if isinstance(pending, list):
        summary["pendingCount"] = len(pending)
    artifacts = source.get("artifacts")
    if isinstance(artifacts, list):
        summary["artifactCount"] = len(artifacts)
    result_levels = source.get("resultLevels")
    if isinstance(result_levels, dict):
        l1 = result_levels.get("l1")
        if isinstance(l1, dict):
            summary["resultL1"] = {
                key: l1.get(key)
                for key in (
                    "status",
                    "statusCategory",
                    "validatedStatus",
                    "workerId",
                    "phaseId",
                    "artifactCount",
                    "extractionArtifactCount",
                    "errorCount",
                )
                if key in l1
            }
    return trim_large_strings(summary, 1000)
