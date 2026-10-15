"""Adapt the existing complete-turn compactor to durable Tau sessions."""
from __future__ import annotations

from harness._vendor.tau_agent import messages as tau
from harness.context.compaction import compact_messages_if_needed
from harness.messages.models import CompactionSummaryMessage
from harness.skill_builder.provider import _neutral_message, _request_message
from harness.skill_runtime.usage import record_usage
from llm.contracts import LLMRequest
from harness.messages.models import TextContent, UserMessage


class SessionCompactor:
    def __init__(self, *, logger, config, provider, system, tools, context_path,
                 actor, facts=lambda: {}, usage_sink=None):
        self.logger, self.config, self.provider = logger, config, provider
        self.system, self.tools, self.path, self.actor = system, tools, context_path, actor
        self.facts, self.usage_sink, self.turn = facts, usage_sink, 0

    async def __call__(self, messages):
        self.turn += 1
        neutral = [_request_message(m) if isinstance(m, tau.AssistantMessage) and
                   m.stop_reason == "length" else _neutral_message(m) for m in messages]
        # Preserve original Tau objects, including opaque thinking and usage.
        originals = {id(n): m for n, m in zip(neutral, messages)}

        async def summarize(*, prompt, **_):
            result = await self.provider.generate(LLMRequest(
                system=[TextContent(text="Preserve task facts accurately. Do not call tools.")],
                messages=[UserMessage(content=prompt)], tools=[]))
            record_usage(self.logger, result, "context_compaction", conversation_id=self.actor)
            if self.usage_sink:
                self.usage_sink(result)
            if result.message.tool_calls() or result.message.stop_reason in {"max_tokens", "length", "error"}:
                raise ValueError("Compaction response is incomplete or requested tools")
            return result.message.text()

        compacted = await compact_messages_if_needed(logger=self.logger, actor=self.actor,
            step=self.turn, system_prompt=self.system, messages=neutral,
            tools=[t.to_tool_spec() if hasattr(t, "to_tool_spec") else
                   {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                   for t in self.tools], config=self.config, summary_generator=summarize,
            browser_continuity_factory=self.facts)
        if compacted is neutral:
            return messages
        result = []
        for message in compacted:
            if id(message) in originals:
                result.append(originals[id(message)])
            elif isinstance(message, CompactionSummaryMessage):
                result.append(tau.CompactionSummaryMessage(summary=message.content,
                    tokens_before=message.tokens_before or 0, checkpoint_id=message.checkpoint_id,
                    details=message.details, replaced_message_count=message.replaced_message_count))
            else:
                raise ValueError("Compactor returned an unknown message")
        self.logger.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
            resource_type="agent_context", logical_path=self.path,
            content=[m.model_dump(by_alias=True, exclude_none=True) for m in result])
        return result
