"""Shared Tau model loop for Workflow task and author sessions.

Sessions own tools, permissions, facts, transcript paths and terminal decisions.
"""
from __future__ import annotations

from harness._vendor.tau_agent import AgentHarness, AgentHarnessConfig
from harness._vendor.tau_agent.events import MessageEndEvent
from harness.runtime.session_output import progress_events
from harness.runtime.tau_bridge import SessionCompactor
from harness.runtime.steering import current_steering
from harness._vendor.tau_agent.messages import AssistantMessage, UserMessage


def stop_facts(message):
    """Protocol facts only; never infer task completion from a model stop."""
    return {"stopReason": message.stop_reason, "rawStopReason": message.raw_stop_reason,
            "responseId": message.response_id, "model": message.model,
            "outputTokens": message.usage.output if message.usage_available else None,
            "error": message.error_message or (message.text if message.stop_reason == "error" else None)}


async def agent_events(*, logger, runtime_config, raw_provider, provider, model,
                       system, tools, messages, prompt, context_path, actor, facts,
                       prepare_context, label, quiet=False, usage_sink=None,
                       completion_pending=None, persist_context=None):
    manager = current_steering()
    role = manager.register(actor, primary=manager.primary is None) if manager else None
    def current_facts():
        value = facts() if facts else {}
        if manager and manager.records:
            value = {**value, "attributedUserInput": manager.latest_inputs(""),
                     "inputRevision": manager.revision}
        return value
    compactor = SessionCompactor(logger=logger, config=runtime_config, provider=raw_provider,
        system=system, tools=tools, context_path=context_path, actor=actor, facts=current_facts,
        usage_sink=usage_sink, persist_context=persist_context)

    async def before_model(current):
        if manager: await manager.wait_runnable()
        current = await compactor(await prepare_context(current))
        # Guidance arriving during compaction is injected below. Retire its
        # queued wake-ups so the fresh model response is not skipped again.
        while role and brain.pop_latest_steering() is not None:
            pass
        return role.prepare(current, typed=True) if role else current

    async def before_tool(call):
        return (True, 'not_executed: user_input_pending') if role and role.pending else (False, None)

    brain = AgentHarness(AgentHarnessConfig(provider=provider, model=model,
        system=system, tools=tools, before_model=before_model, before_tool_call=before_tool,
        session_id=logger.task_id), messages=messages)
    def deliver(record):
        brain.steer_message(UserMessage(content=manager.render([record])))
    if role: role.listeners.append(deliver)
    try:
        while True:
            last = None
            async for event in progress_events(brain.prompt(prompt), label=label,
                    task_id=logger.task_id, quiet=quiet):
                active = None
                if isinstance(event, MessageEndEvent):
                    if isinstance(event.message, AssistantMessage):
                        last = event.message
                        if role and last.stop_reason not in {"error", "aborted", "length"}:
                            role.acknowledge()
                    active = list(brain.messages)
                    if not any(message is event.message for message in active):
                        active.append(event.message)
                yield event, active
            if last is None:
                break
            if last.stop_reason not in {"length", "error", "aborted"}:
                if completion_pending is not None and completion_pending():
                    logger.write("agent.completion.missing", {"actor": actor, **stop_facts(last)})
                    prompt = ("[Harness protocol recovery] Your response ended without the required "
                        "completion tool. The task remains open. Keep all completed executions and "
                        "their saved results; do not replay actions to fix this protocol omission. "
                        "Continue authorized work from those facts. If you need user input, use ask_user "
                        "when available and wait for the answer in this task; do not end merely to ask "
                        "a question in a final report. When ready to hand off, submit finish (or save_draft "
                        "for an author) with your supported conclusion and remaining work. "
                        "If a tool rejected the conclusion, correct its structure or evidence "
                        "references. A natural-language final answer does not record completion.")
                    continue
                break
            stopped = stop_facts(last)
            logger.write("agent.model.stopped", {"actor": actor, **stopped})
            if not quiet:
                from harness.runtime.session_output import block
                block(label, "模型输出截断，保留已完成操作并继续" if last.stop_reason == "length"
                      else "模型停止，任务未完成", [
                    ("原因", last.raw_stop_reason or last.stop_reason),
                    ("响应", last.response_id), ("输出 token", stopped["outputTokens"]),
                    ("错误", stopped["error"])])
            if last.stop_reason != "length":
                break
            # Tau paired truncated calls with skipped results. Keep the same
            # brain, completed receipts and steering role; never replay actions.
            prompt = ("[Harness protocol recovery] The previous model response was truncated "
                "(max_tokens). None of its tool calls was executed. Earlier completed tool "
                "results and saved files remain authoritative. Continue the unfinished task "
                "in this same session. Use smaller concrete file edits or tool calls for the "
                "next bounded piece, then observe its result. Do not reconstruct or replay "
                "completed work. Only the explicit completion tool can finish the task.")
    finally:
        if role: role.close()
