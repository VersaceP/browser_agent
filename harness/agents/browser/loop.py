"""Browser execution loop."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any, List, Optional
from harness.constants import WORKER_STATUS_CONTEXT_LIMIT, WORKER_STATUS_DONE, WORKER_STATUS_INCOMPLETE, WORKER_STATUS_RUNNING
from harness.diagnostics import classify_terminal_status
from harness.tools.path_authorization import wait_for_local_authorization
from harness.runtime.lifecycle import LifecycleContext
from harness.context.offload import model_visible_screenshot_attachment
from harness.observation.page_fingerprint import render_page_stats_for_prompt, render_snapshot_diff_for_prompt
from harness.observation.browser_call import build_browser_call_runner
from harness.tools.browser_tools import _close_agent_watches, build_browser_agent_tool_specs, build_browser_tool_dispatcher
from harness.workflow.workflow_runtime import workflow_execution_enabled
from harness.utils import JsonDict, exception_payload, write_context_snapshot
from llm import LLMConnectionError, LLMEmptyResponseError, LLMProviderProtocolError, LLMRateLimitError, LLMRequestTimeoutError, input_moderation_rejection, retry_usage_from_attempts
from harness.runtime.model_support import (
    INFRA_STREAK_LIMIT,
    MODEL_TIMEOUT_ATTEMPT_LIMIT,
    TRUNCATION_STREAK_LIMIT,
    _assistant_message_from_parts,
    _compact_before_multimodal_request,
    _deferred_tool_result,
    _effective_streak_limit,
    _expire_multimodal_image_blocks,
    _is_context_limit_exception,
    _store_received_model_output,
    _tool_call_state_boundary,
    _tool_result_digest,
    _tool_result_is_error,
    _truncation_info,
    generate_response_surviving_moderation,
    llm_rate_limit_terminal_result,
    log_model_visible_tool_result,
    offload_tool_result_for_model,
)

async def run_browser_agent(self, task: str) -> str:
    control = self.task_control
    step = 0
    final_answer = ""
    final_status = WORKER_STATUS_RUNNING
    should_finish = False
    completed = False
    model_reported_status: Optional[str] = None
    system_prompt = ""
    tools: List[JsonDict] = []
    messages: List[JsonDict] = []
    task_memory_heartbeat: Optional[asyncio.Task] = None
    self.base_max_steps = (
        0 if control is not None
        else max(0, int(self.runtime.harness.max_steps or 0))
    )
    self.effective_max_steps = self.base_max_steps
    self._step_extension_granted_steps = 0
    self._step_extension_locked = False
    self._recent_tool_outcomes = []
    self._current_step = 0
    recorder = self.lifecycle_events
    recorder.agent_start(
        label=str(self.worker_id or self.runtime.agent_id),
        max_steps=self.base_max_steps,
        agent_id=str(self.runtime.agent_id or "") or None,
    )

    try:
        bootstrap = await self._bootstrap_browser(task)
        task_memory_heartbeat = self._start_task_memory_heartbeat(
            bootstrap,
            task=str(
                getattr(self, "task_memory_root_task", "") or task
            ).strip(),
        )
        system_prompt = self._build_system_prompt()
        if control is not None:
            system_prompt = control.append_prompt(system_prompt)
        multimodal_enabled = bool(
            self.runtime.harness.browser_agent_multimodal_enabled
        )
        self.prompt_context_hash = hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest()
        tools = build_browser_agent_tool_specs(
            self._visible_capability_methods(),
            workflow_enabled=workflow_execution_enabled(self),
            selected_skill_available=bool(
                getattr(self.runtime.harness, "forced_skill_id", "") and
                getattr(self.runtime.harness, "forced_skill_hash", "")),
            step_extension_enabled=bool(
                self.runtime.harness.browser_agent_step_extension_enabled
                and control is None
            ),
            multimodal_enabled=multimodal_enabled,
            standalone_review_enabled=control is not None,
        )
        if control is not None:
            control.configure_tools(tools)
        dispatch_tool = build_browser_tool_dispatcher(self)
        self.browser_call_runner = build_browser_call_runner(
            browser=self.browser,
            logger=self.logger,
            capability_methods=self.capability_methods,
        )
        # Layer-0 event observer: DOM.axTreeUpdated (browser-side stale-id
        # auto-rematch) refreshes our id snapshot without a manual
        # DOM.getAXTree round-trip. Never enters the model context.
        self.event_observer.attach(self.browser)
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
        if control is not None:
            control.restore_initial_context(messages)

        truncation_streak = 0
        streak_kinds: List[str] = []
        timeout_attempt_streak = 0
        text_only_protocol_streak = 0
        checkpoint_without_action_streak = 0
        rejected_completion_without_action_streak = 0
        pending_hitl_delivery: List[JsonDict] = []
        while not should_finish and (
            control is not None or step < self.effective_max_steps
        ):
            await wait_for_local_authorization(self)
            step += 1
            self._current_step = step
            force_reason = self._forced_compaction_reason
            self._forced_compaction_reason = None
            messages_before_compaction = messages
            messages = await _compact_before_multimodal_request(
                self,
                step=step,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                force_reason=force_reason,
            )
            if control is not None and messages is not messages_before_compaction:
                control.restore_compacted_context(messages)
            self._write_agent_event("agent.step.start", {"step": step})
            recorder.turn_start(step)
            recorder.message_start()
            self.lifecycle.agent_before_step(
                LifecycleContext(
                    actor="browser_agent",
                    step=step,
                    metadata={"agent_id": self.runtime.agent_id},
                ),
                {
                    "messageCount": len(messages),
                    "toolCount": len(tools),
                },
            )
            if pending_hitl_delivery:
                # Compaction may replace older turns. Keep an unanswered
                # human instruction intact in the actual next request.
                for delivery in pending_hitl_delivery:
                    preserved = any(
                        delivery["feedbackId"] in str(message.get("content"))
                        and delivery["text"] in str(message.get("content"))
                        for message in messages
                        if isinstance(message, dict)
                        and message.get("role") == "user"
                    )
                    if not preserved:
                        messages.append(delivery["modelMessage"])
                self._write_agent_event("hitl.feedback_model_request", {
                    "step": step,
                    "feedbackIds": [item["feedbackId"] for item in pending_hitl_delivery],
                })
                pending_hitl_delivery = []
            model_call_failed = False
            model_timeout_attempts = 0
            model_result = None
            try:
                model_result = await generate_response_surviving_moderation(
                    provider=self.provider,
                    logger=self.logger,
                    return_result=True,
                    actor="browser_agent",
                    step=step,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                )
                text, tool_calls, stop_reason, usage = model_result.legacy_fields()
            except LLMEmptyResponseError as exc:
                # Mirror the lead: a degenerate response that survived the
                # provider's own retries surfaces as an empty turn for the
                # streak guard below — crashing the worker here would burn
                # the whole phase attempt on a gateway hiccup.
                self._write_agent_event("agent.model_degenerate_response", {
                    "step": step,
                    "provider": exc.provider,
                    "model": exc.model,
                    "operation": exc.operation,
                    "problem": exc.problem,
                    "providerMaxRetries": exc.max_retries,
                    "attempts": exc.attempts,
                })
                # The raising call returns no usage dict, so carry the
                # retries it did perform through to the usage summary.
                model_call_failed = True
                text, tool_calls, stop_reason, usage = (
                    "", [], "degenerate_response",
                    retry_usage_from_attempts(exc.attempts),
                )
            except LLMConnectionError as exc:
                # Same containment as above: the transport died mid-stream
                # and the provider already burned its retry budget, so hand
                # the streak guard an empty turn instead of losing the
                # whole phase attempt to a gateway hiccup.
                self._write_agent_event("agent.model_connection_error", {
                    "step": step,
                    "provider": exc.provider,
                    "model": exc.model,
                    "operation": exc.operation,
                    "reason": exc.reason,
                    "providerMaxRetries": exc.max_retries,
                    "attempts": exc.attempts,
                })
                model_call_failed = True
                text, tool_calls, stop_reason, usage = (
                    "", [], "connection_error",
                    retry_usage_from_attempts(exc.attempts),
                )
            except LLMRequestTimeoutError as exc:
                # Contained for the same reason: letting this escape ends
                # the worker as failed, which discards its whole context
                # AND spends one of the phase's attempt budget on what is
                # usually a transient upstream stall.
                self._write_agent_event("agent.model_timeout", {
                    "step": step,
                    "provider": exc.provider,
                    "model": exc.model,
                    "operation": exc.operation,
                    "timeoutSeconds": exc.timeout_seconds,
                    "providerMaxRetries": exc.max_retries,
                    "attempts": exc.attempts,
                })
                model_call_failed = True
                model_timeout_attempts = max(1, len(exc.attempts))
                text, tool_calls, stop_reason, usage = (
                    "", [], "llm_timeout",
                    retry_usage_from_attempts(exc.attempts),
                )
            except LLMProviderProtocolError as exc:
                self._write_agent_event("agent.model_protocol_error", {
                    "step": step,
                    "provider": exc.provider,
                    "model": exc.model,
                    "operation": exc.operation,
                    "fallbackAttempted": exc.fallback_attempted,
                    "fallbackSkippedReason": exc.fallback_skipped_reason,
                    "attempts": exc.attempts,
                })
                model_call_failed = True
                text, tool_calls, stop_reason, usage = (
                    "", [], "provider_protocol_error",
                    retry_usage_from_attempts(exc.attempts),
                )
            except Exception as exc:
                # Same containment as the transport faults above, for the
                # one 400 the fold could not clear. Letting it escape is
                # how a worker that had already saved every file ended as
                # `rowCount: 0` with no trace written at all: the phase
                # then looks untried, and the next worker redoes finished
                # work. An empty turn instead lets the streak guard end the
                # step with the trace and artifacts intact.
                marker = input_moderation_rejection(exc)
                if marker is None:
                    raise
                self._write_agent_event("agent.model_input_moderation_refused", {
                    "step": step,
                    "marker": marker,
                    "error": str(exc)[:500],
                })
                model_call_failed = True
                text, tool_calls, stop_reason, usage = (
                    "", [], "input_moderation_refused", {},
                )
            if not model_call_failed:
                # A complete provider response consumes each attached
                # screenshot once, regardless of whether it carries a
                # tool call, ends in text, or is truncated for recovery.
                expired_images = _expire_multimodal_image_blocks(messages)
                if expired_images:
                    self._write_agent_event("agent.multimodal_images_expired", {
                        "step": step,
                        "imageCount": expired_images,
                    })
            if model_call_failed:
                # A call that raised has no usage to report: routing it
                # through the normal path would count a call that produced
                # nothing, read its absent cache signature as signature
                # drift, read cache_read=0 as a cache miss, and reset the
                # cache state that the next real call is measured against.
                # Only the retries it performed are real.
                # The message scope closes as failed rather than as an
                # ordinary empty turn: an audit that cannot tell "the model
                # said nothing" from "the call never returned" is not an
                # audit.
                recorder.message_failed(str(stop_reason or "model_call_failed"))
                self.logger.record_llm_retries(
                    source="browser_agent", usage=usage,
                )
            else:
                usage_payload = self.logger.record_llm_usage(
                    source="browser_agent",
                    provider=self.effective_model_config.provider,
                    model=self.effective_model_config.model_id,
                    usage=usage,
                    step=step,
                    conversation_id=f"browser:{self.runtime.agent_id}",
                    context_hash=getattr(
                        self,
                        "prompt_context_hash",
                        self.static_context_hash,
                    ),
                )
                self._observe_cache_pressure(
                    usage_payload,
                    step=step,
                    max_steps=self.effective_max_steps,
                )
            # Provider-decoded ordered blocks are shared by lifecycle and history.
            assistant_message = model_result.message if model_result is not None else _assistant_message_from_parts(
                text=text,
                tool_calls=tool_calls,
                prefix_blocks=None,
                stop_reason=stop_reason,
                usage=usage if isinstance(usage, dict) else None,
            )
            self._write_agent_event(
                "agent.model",
                {
                    "step": step,
                    "text": text,
                    "tool_calls": tool_calls,
                    "stop_reason": stop_reason,
                    "outputTelemetry": {
                        "providerOutputTokens": usage.get("output") if isinstance(usage, dict) else None,
                        "thinkingBlockCount": len(assistant_message.thinking_blocks()),
                        "thinkingChars": sum(len(block.thinking or "") for block in assistant_message.thinking_blocks()),
                    },
                },
            )
            worker_truncation = None
            if stop_reason == "max_tokens":
                # One receipt per truncated turn. The no-tool-call branch
                # below reuses this one instead of writing the same prefix
                # to a second file under a second path.
                # Also reached when the turn DID emit tool calls: the model
                # was cut off mid-turn either way, and only the no-tool
                # branch used to notice.
                worker_truncation = _store_received_model_output(
                    logger=self.logger,
                    actor=str(self.runtime.agent_id or "browser_agent"),
                    step=step,
                    text=text,
                    stop_reason=str(stop_reason),
                )
            recorder.message_complete(
                assistant_message,
                stop_reason=stop_reason,
                truncation=_truncation_info(worker_truncation),
            )
            # The `model` trace entry is produced by TraceProjectionSink
            # from the same message_end event, so it is written once. When
            # lifecycle events are off the loop still owns it.
            recorder.message_end()
            if not recorder.enabled:
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
                # A no-tool turn is an incident (not a self-reported
                # completion) in two shapes: cut off by the output-token
                # limit, or entirely empty (degenerate gateway response
                # that survived provider-level retries, or a model
                # emitting a bare end_turn). Without this guard the empty
                # turn was classified done with an empty answer, bypassing
                # the step-cap fallback AND the final_answer blocker
                # channel. Retry with recovery guidance; only a streak
                # terminates the worker, as incomplete.
                # Dropped connections and provider protocol failures are
                # additional shapes: the model did
                # generate — possibly for a while, we may even have partial
                # chunks — but no complete response ever arrived. Calling
                # that "empty" misdiagnoses the blocker and tells the model
                # it produced nothing, which it did not.
                # A moderation refusal is its own shape for the same reason:
                # the model never saw the request at all, so reporting an
                # empty response would tell it that it produced nothing.
                incident = (
                    "connection" if stop_reason == "connection_error"
                    else "timeout" if stop_reason == "llm_timeout"
                    else "protocol" if stop_reason == "provider_protocol_error"
                    else "moderation"
                    if stop_reason == "input_moderation_refused"
                    else "truncated" if stop_reason == "max_tokens"
                    else "empty" if not text.strip()
                    else ""
                )
                if incident:
                    truncation_streak += 1
                    streak_kinds.append(incident)
                    if incident == "timeout":
                        timeout_attempt_streak += model_timeout_attempts or 1
                    streak_limit = _effective_streak_limit(streak_kinds)
                    timeout_budget_exhausted = (
                        incident == "timeout"
                        and timeout_attempt_streak >= MODEL_TIMEOUT_ATTEMPT_LIMIT
                    )
                    # The suffix was never generated, so no file anywhere
                    # holds it. What arrived can be saved, under a name
                    # that says so.
                    received = worker_truncation or _store_received_model_output(
                        logger=self.logger,
                        actor=str(self.runtime.agent_id or "browser_agent"),
                        step=step,
                        text=text,
                        stop_reason=str(stop_reason or incident),
                    )
                    self._write_agent_event("agent.truncated_response", {
                        "step": step,
                        "streak": truncation_streak,
                        "limit": streak_limit,
                        "strictLimit": TRUNCATION_STREAK_LIMIT,
                        **({"truncation": received} if received else {}),
                        "infraStreak": streak_limit == INFRA_STREAK_LIMIT,
                        "kind": incident,
                        "streakKinds": list(streak_kinds),
                        "timeoutAttemptsInStreak": timeout_attempt_streak,
                        "timeoutAttemptLimit": MODEL_TIMEOUT_ATTEMPT_LIMIT,
                        "timeoutBudgetExhausted": timeout_budget_exhausted,
                        "stop_reason": stop_reason,
                        "text_chars": len(text or ""),
                    })
                    if (
                        truncation_streak < streak_limit
                        and not timeout_budget_exhausted
                    ):
                        if incident in ("connection", "timeout", "moderation"):
                            # No usable response arrived, so there is
                            # nothing to quote back and no mistake to
                            # coach; the recovery exchange would be two
                            # false turns in the context. Re-ask verbatim.
                            # A moderation refusal belongs here too: the
                            # request never reached the model, and coaching
                            # it about an "empty response" it never made
                            # would be a lie it then has to reason around.
                            # Unlike the lead — whose retry loop sits
                            # inside a step and so must force compaction —
                            # this continue re-enters the step loop, where
                            # the normal size check compacts if the context
                            # really is what upstream choked on.
                            continue
                        placeholder = (
                            "[response truncated by output-token limit]"
                            if incident == "truncated"
                            else "[provider tool JSON could not be decoded]"
                            if incident == "protocol"
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
                            else "could not be decoded by the provider as"
                            " a complete tool call"
                            if incident == "protocol"
                            else "was empty (no text and no tool call)"
                        )
                        recovery_action = (
                            " The browser tool was not executed. Reissue the"
                            " intended next action as exactly one compact"
                            " tool call with a smaller argument payload."
                            if incident == "protocol" else
                            " Respond with minimal text and exactly one tool"
                            " call now — the next concrete action, or"
                            " final_answer with your best current status and"
                            " blockers."
                        )
                        messages.append({"role": "user", "content": [{
                            "type": "text",
                            "text": (
                                "<truncation_recovery>Your previous response"
                                f" {incident_detail} and was discarded. Do not"
                                " restate prior reasoning or dump large data"
                                f" inline.{recovery_action}"
                                "</truncation_recovery>"
                            ),
                        }]})
                        continue
                    model_reported_status = WORKER_STATUS_INCOMPLETE
                    blocker_type = (
                        "llm_output_truncation"
                        if incident == "truncated"
                        else "llm_connection_error"
                        if incident == "connection"
                        else "llm_timeout_error"
                        if incident == "timeout"
                        else "llm_provider_protocol_error"
                        if incident == "protocol"
                        else "llm_input_moderation_refused"
                        if incident == "moderation"
                        else "llm_empty_response"
                    )
                    blocker_detail = (
                        "hit the output-token limit"
                        if incident == "truncated"
                        else "were lost to a dropped connection"
                        if incident == "connection"
                        else "timed out before a complete response arrived"
                        if incident == "timeout"
                        else "could not be decoded as complete tool JSON"
                        if incident == "protocol"
                        else (
                            "were refused by the model provider's input"
                            " content filter, even after the harness folded"
                            " the bulky tool results it had contributed"
                        )
                        if incident == "moderation"
                        else "were empty"
                    )
                    # One streak counter spans every shape — consecutive
                    # turns without a tool call are no progress whatever
                    # caused them — but the budget it is measured against
                    # depends on the mix (see `_effective_streak_limit`),
                    # and a mixed streak must say so instead of attributing
                    # every turn to the last one's cause.
                    mixed_detail = (
                        f" (turn outcomes: {', '.join(streak_kinds)})"
                        if len(set(streak_kinds)) > 1 else ""
                    )
                    timeout_detail = (
                        f" ({timeout_attempt_streak} provider attempts"
                        " across the worker retry loop)"
                        if incident == "timeout" else ""
                    )
                    final_answer = json.dumps({
                        "blockers": [{
                            "type": blocker_type,
                            "detail": (
                                f"{truncation_streak} consecutive model"
                                f" responses {blocker_detail}"
                                f"{mixed_detail}"
                                f"{timeout_detail}"
                                " without emitting a tool call; the harness"
                                " terminated the worker."
                            ),
                        }],
                    }, ensure_ascii=False)
                    should_finish = True
                    break
                if control is not None:
                    # Direct browsing has one explicit completion channel.
                    # Text alone may be a progress note or unfinished plan.
                    text_only_protocol_streak += 1
                    if text_only_protocol_streak >= 3:
                        model_reported_status = WORKER_STATUS_INCOMPLETE
                        final_answer = json.dumps({"blockers": [{
                            "type": "llm_tool_protocol_stall",
                            "detail": (
                                "The model returned three consecutive text-only"
                                " turns without a browser action or final_answer."
                            ),
                        }]}, ensure_ascii=False)
                        should_finish = True
                        break
                    messages.append(assistant_message)
                    messages.append({"role": "user", "content": [{
                        "type": "text",
                        "text": (
                            "Continue from the original user goal and current"
                            " evidence. If the goal is complete, call"
                            " final_answer(status='done') with evidence."
                            " Otherwise take the next useful action or"
                            " report a concrete blocker through final_answer."
                            " A text-only response does not end this task."
                        ),
                    }]})
                    continue
                final_answer = text.strip()
                model_reported_status = WORKER_STATUS_DONE
                should_finish = True
                break
            text_only_protocol_streak = 0
            truncation_streak = 0
            streak_kinds.clear()
            timeout_attempt_streak = 0

            messages.append(assistant_message)

            tool_results: List[JsonDict] = []
            latest_snapshot_diff: Optional[JsonDict] = None
            if control is not None:
                control.begin_turn()
            for tool_index, tool_call in enumerate(tool_calls):
                self.loop_nudge.record_action(tool_call, step=step)
                recorder.tool_start(
                    tool_call_id=str(tool_call.get("id") or ""),
                    tool_name=str(tool_call.get("name") or "tool"),
                    arguments=(
                        tool_call.get("input")
                        if isinstance(tool_call.get("input"), dict) else None
                    ),
                )
                try:
                    completion_proposal = (
                        control is not None
                        and tool_call.get("name") == "final_answer"
                        and isinstance(tool_call.get("input"), dict)
                        and str(tool_call["input"].get("status") or "done") == "done"
                    )
                    if completion_proposal:
                        review = await control.completion_review(str(tool_call["input"].get("answer") or ""))
                        if (review.get("status") == "reviewed"
                                and review.get("verdict") == "complete"):
                            rejected_completion_without_action_streak = 0
                            result, should_stop = await dispatch_tool(tool_call, step)
                        else:
                            rejected_completion_without_action_streak += 1
                            result, should_stop = ({
                                "status": "completion_review_pending",
                                "tool_was_executed": False,
                                "review": review,
                                "next_instruction": (
                                    "The browser transport disconnected during"
                                    " independent review. Do not infer page"
                                    " deletion or repeat side effects. Report"
                                    " the concrete connection blocker with"
                                    " final_answer(incomplete) if it cannot"
                                    " be recovered in this run."
                                    if review.get("reason") == "browser_transport_disconnected"
                                    else "The completion proposal did not pass"
                                    " independent review. Recheck the cited"
                                    " original goal, source files and live"
                                    " results; correct the work or provide"
                                    " missing evidence, then propose done again."
                                ),
                            }, False)
                            self.trace.append({
                                "type": "browser_completion_proposal",
                                "step": step, "result": review,
                            })
                    else:
                        result, should_stop = await dispatch_tool(
                            tool_call, step,
                        )
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
                if control is not None:
                    if (tool_call.get("name") != "final_answer"
                            and isinstance(result, dict)
                            and result.get("status") not in {
                                "failed", "rejected", "unavailable",
                            }):
                        rejected_completion_without_action_streak = 0
                    if rejected_completion_without_action_streak >= 3:
                        model_reported_status = WORKER_STATUS_INCOMPLETE
                        final_answer = json.dumps({"blockers": [{
                            "type": "completion_review_protocol_stall",
                            "detail": (
                                "Three completion proposals were rejected"
                                " without an intervening useful action."
                            ),
                        }]}, ensure_ascii=False)
                        should_finish = True
                        break
                    control.observe_tool_result(tool_call, result)
                    if (
                        tool_call.get("name") == "final_answer"
                        and isinstance(result, dict)
                        and result.get("status") == "continuing"
                    ):
                        checkpoint_without_action_streak += 1
                        if checkpoint_without_action_streak >= 3:
                            model_reported_status = WORKER_STATUS_INCOMPLETE
                            final_answer = json.dumps({"blockers": [{
                                "type": "llm_checkpoint_protocol_stall",
                                "detail": (
                                    "Three partial checkpoints were emitted"
                                    " without an intervening action."
                                ),
                            }]}, ensure_ascii=False)
                            should_finish = True
                            break
                    elif tool_call.get("name") != "final_answer":
                        checkpoint_without_action_streak = 0
                self._observe_tool_result(tool_call, result)
                page_observation = self.page_observer.observe_result(
                    tool_call,
                    result,
                    step=step,
                    agent=self,
                )
                page_stats = page_observation.get("pageStats")
                if isinstance(page_stats, dict):
                    self.logger.write("page_stats.detected", page_stats)
                    self.trace.append({
                        "type": "page_stats",
                        "step": step,
                        "result": page_stats,
                    })
                snapshot_diff = page_observation.get("snapshotDiff")
                if isinstance(snapshot_diff, dict):
                    self.logger.write("snapshot_diff.detected", snapshot_diff)
                    self.trace.append({
                        "type": "snapshot_diff",
                        "step": step,
                        "result": snapshot_diff,
                    })
                    latest_snapshot_diff = snapshot_diff
                nudge = self.loop_nudge.observe_result(
                    tool_call,
                    result,
                    step=step,
                    agent=self,
                    fingerprint=page_observation.get("fingerprint"),
                )
                if nudge is not None:
                    self.logger.write("loop_nudge.detected", nudge)
                    self.trace.append({
                        "type": "loop_nudge",
                        "step": step,
                        "result": nudge,
                    })
                model_result = offload_tool_result_for_model(
                    logger=self.logger,
                    runtime=self.runtime,
                    tool_call=tool_call,
                    result=result,
                    step=step,
                )
                _tool_input = (
                    tool_call.get("input")
                    if isinstance(tool_call.get("input"), dict) else {}
                )
                image_attachment: Optional[JsonDict] = None
                image_block: Optional[JsonDict] = None
                method = str(_tool_input.get("method") or "")
                if multimodal_enabled and method == "Page.screenshot":
                    image_block, image_attachment = (
                        model_visible_screenshot_attachment(
                            result,
                            max_raw_bytes=(
                                self.runtime.harness
                                .browser_agent_max_multimodal_image_bytes
                            ),
                        )
                    )
                    if image_block is not None:
                        image_block, image_attachment = (
                            self._fit_image_to_context_window(
                                image_block,
                                image_attachment,
                                system_prompt=system_prompt,
                                messages=[
                                    *messages,
                                    {"role": "user", "content": tool_results},
                                ],
                                tools=tools,
                            )
                        )
                    self._write_agent_event(
                        "agent.multimodal_screenshot",
                        {"step": step, **image_attachment},
                    )
                    if image_block is not None:
                        # The capability-layer receipt says pixels are not
                        # ordinarily model-visible. Correct that factual
                        # field only in this ephemeral model-facing copy.
                        if isinstance(model_result, dict):
                            model_result = dict(model_result)
                            visibility = model_result.get(
                                "screenshotVisibility"
                            )
                            model_result["screenshotVisibility"] = {
                                **(
                                    visibility
                                    if isinstance(visibility, dict) else {}
                                ),
                                "modelVisible": True,
                                "fact": (
                                    "Screenshot pixels are attached to this"
                                    " model request only."
                                ),
                            }
                    elif (
                        image_attachment.get("reason")
                        == "image_exceeds_context_window"
                        and isinstance(model_result, dict)
                    ):
                        model_result = dict(model_result)
                        visibility = model_result.get("screenshotVisibility")
                        model_result["screenshotVisibility"] = {
                            **(
                                visibility
                                if isinstance(visibility, dict) else {}
                            ),
                            "modelVisible": False,
                            "fact": (
                                "Screenshot pixels were not attached: with"
                                " this model transport the next request"
                                " would be about"
                                f" {image_attachment['estimatedRequestTokens']}"
                                " tokens, over the"
                                f" {image_attachment['contextWindowTokens']}"
                                "-token context window. Continue from"
                                " structured page evidence."
                            ),
                        }
                content = self._to_model_json(model_result)
                model_content: Any = content
                if image_block is not None:
                    model_content = [
                        {"type": "text", "text": content},
                        image_block,
                    ]
                log_model_visible_tool_result(
                    self.logger,
                    actor=str(self.runtime.agent_id),
                    step=step,
                    tool_name=str(tool_call.get("name") or "tool"),
                    raw_result=result,
                    model_result=model_result,
                    final_content=content,
                    worker_id=str(getattr(self, "worker_id", "") or ""),
                    method=method,
                    image_attachment=image_attachment,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call["id"],
                        "content": model_content,
                    }
                )
                boundary = _tool_call_state_boundary(tool_call, result)
                if (should_stop or boundary) and tool_index + 1 < len(tool_calls):
                    reason = (
                        "preceding_tool_terminated_agent"
                        if should_stop
                        else "preceding_tool_may_change_browser_state"
                    )
                    batch_workflow_enabled = workflow_execution_enabled(self)
                    for deferred in tool_calls[tool_index + 1:]:
                        tool_results.append(_deferred_tool_result(
                            deferred,
                            after_tool_call=tool_call,
                            reason=reason,
                            workflow_enabled=batch_workflow_enabled,
                        ))
                    self.logger.write("tool_batch.deferred", {
                        "step": step,
                        "afterTool": tool_call.get("name"),
                        "reason": reason,
                        "deferredCount": len(tool_calls) - tool_index - 1,
                        "deferredTools": [
                            str(item.get("name") or "")
                            for item in tool_calls[tool_index + 1:]
                        ],
                        "segmentAdviceOffered": batch_workflow_enabled,
                    })
                if should_stop:
                    final_answer = result.get("answer", "")
                    model_reported_status = (
                        str(result.get("status")) if result.get("status") else None
                    )
                    should_finish = True
                    break
                if boundary:
                    break

            if not should_finish:
                if control is not None:
                    tool_results.extend(await control.review_boundary())
                page_stats = self.page_observer.consume_page_stats()
                if page_stats is not None:
                    tool_results.append({
                        "type": "text",
                        "text": render_page_stats_for_prompt(page_stats),
                    })
                if latest_snapshot_diff is not None:
                    tool_results.append({
                        "type": "text",
                        "text": render_snapshot_diff_for_prompt(
                            latest_snapshot_diff
                        ),
                    })
                nudge = self.loop_nudge.consume_nudge()
                if nudge is not None:
                    tool_results.append({
                        "type": "text",
                        "text": (
                            "<loop_nudge>\n"
                            f"{json.dumps(nudge, ensure_ascii=False, default=str)}\n"
                            "</loop_nudge>"
                        ),
                    })
                for block in self._consume_reality_check_blocks():
                    tool_results.append(block)
                reminder = self._step_cap_reminder_block(
                    current_step=step,
                    max_steps=self.effective_max_steps,
                )
                if reminder is not None:
                    tool_results.append(reminder)
            messages.append({"role": "user", "content": tool_results})
            for feedback in getattr(self, "hitl_user_messages", []):
                feedback_id = feedback.get("feedbackId") or uuid.uuid4().hex
                model_message = {"role": "user", "content": (
                    f"[HITL 用户意见，feedbackId={feedback_id}，"
                    f"pageId={feedback['pageId']}，"
                    f"pauseId={feedback.get('pauseId') or 'unknown'}，"
                    f"assistanceKind={feedback.get('assistanceKind') or 'unspecified'}]\n"
                    f"请求：{feedback.get('requestPurpose') or ''}\n"
                    + feedback["text"]
                    + "\n请结合原始目标与当前证据判断下一步；恢复控制不代表请求已满足。"
                )}
                messages.append(model_message)
                pending_hitl_delivery.append({
                    "feedbackId": feedback_id,
                    "text": feedback["text"],
                    "modelMessage": model_message,
                })
            self.hitl_user_messages = []
            if should_finish:
                break

        # A background reality check outlives the loop that armed it. Its
        # verdict is advisory and there is no turn left to deliver it to,
        # so cancel rather than let the task (and its VL call) run on after
        # the worker is done.
        for task in getattr(self, "reality_check_tasks", None) or []:
            if not task.done():
                task.cancel()
        self.reality_check_tasks = []
        reached_step_cap = not should_finish
        if self._step_extension_granted_steps:
            extension_event = (
                "agent.step_extension.exhausted"
                if reached_step_cap
                else "agent.step_extension.completed"
            )
            self._write_agent_event(extension_event, {
                "step": step,
                "baseMaxSteps": self.base_max_steps,
                "effectiveMaxSteps": self.effective_max_steps,
                "grantedSteps": self._step_extension_granted_steps,
                "modelReportedStatus": model_reported_status,
                "phaseCompleted": model_reported_status == WORKER_STATUS_DONE,
            })
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
        self._write_agent_event(
            "agent.cancelled",
            exception_payload(exc, last_step=step, artifacts=self.artifacts),
        )
        raise
    except LLMRateLimitError as exc:
        incident = exc.to_payload()
        self._write_agent_event("agent.model_rate_limited", {
            "step": step,
            **incident,
        })
        final_answer = json.dumps(
            llm_rate_limit_terminal_result(exc),
            ensure_ascii=False,
        )
        self.final_status = WORKER_STATUS_INCOMPLETE
        self._write_agent_final(
            final_status=WORKER_STATUS_INCOMPLETE,
            final_answer=final_answer,
            model_reported_status=None,
            override_reason=f"llm_{exc.kind}",
            reached_step_cap=False,
        )
        completed = True
        return final_answer
    except Exception as exc:
        self.diagnostics.record_exception(exc)
        self._write_agent_event(
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
        recorder.close(
            status=str(self.final_status or final_status or "unknown"),
            reason=None if completed else "interrupted",
            step_count=step,
        )
        await self._stop_task_memory_heartbeat(task_memory_heartbeat)
        try:
            self.event_observer.detach()
        except Exception:
            pass
        # Watches outlive no worker: their pollers re-read the page.
        _close_agent_watches(self)
        try:
            write_context_snapshot(
                self.logger,
                actor="browser_agent",
                name=(
                    f"{self.runtime.agent_id}-{self.worker_id}"
                    if str(
                        getattr(self.logger, "context_run_id", "") or ""
                    )
                    else self.runtime.agent_id
                ),
                system_prompt=system_prompt or "(not initialized)",
                messages=messages,
                tools=tools,
                metadata={
                    "agent_id": self.runtime.agent_id,
                    "worker_id": self.worker_id,
                    "slot_id": self.slot_id,
                    "phase_id": self.phase_id,
                    "last_step": step,
                    "completed": completed,
                    "final_status": self.final_status,
                    "final_answer": final_answer,
                    "artifacts": self.artifacts,
                },
                run_id=str(
                    getattr(self.logger, "context_run_id", "") or ""
                ) or None,
            )
        except Exception as exc:
            self.logger.write(
                "context.snapshot.failed",
                exception_payload(exc, actor="browser_agent"),
            )
        if not completed:
            self._write_agent_event(
                "agent.interrupted",
                {
                    "last_step": step,
                    "has_final_answer": bool(final_answer),
                    "artifacts": self.artifacts,
                },
            )
