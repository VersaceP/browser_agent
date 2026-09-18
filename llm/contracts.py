"""Harness-owned request/result contracts. No SDK objects or credentials."""
from __future__ import annotations

from typing import Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field

from harness.messages.models import (
    AssistantMessage, TextContent, ThinkingContent, ToolResultMessage, UserMessage,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolDefinition(Contract):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None
    # Explicit, reviewed schema variants. Keys are provider profiles or API ids.
    # The adapter never removes unsupported JSON Schema keywords on its own.
    schema_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ThinkingOptions(Contract):
    enabled: bool | None = None
    effort: str | None = None
    budget_tokens: int | None = Field(default=None, gt=0)


class ToolChoice(Contract):
    mode: Literal["auto", "none", "required", "named"] = "auto"
    name: str | None = None


class GenerationOptions(Contract):
    temperature: float | None = None
    # Different contracts: never silently rename an answer-only limit to total.
    total_output_tokens: int | None = Field(default=None, gt=0)
    answer_tokens: int | None = Field(default=None, gt=0)
    thinking: ThinkingOptions | None = None
    tool_choice: ToolChoice | None = None


class LLMRequest(Contract):
    schema_version: Literal[1] = 1
    system: list[TextContent] = Field(default_factory=list)
    messages: list[Union[UserMessage, AssistantMessage, ToolResultMessage]] = Field(default_factory=list)
    tools: list[ToolDefinition] = Field(default_factory=list)
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class TokenUsage(Contract):
    """Unknown counters stay None. Reasoning is a subset of output_tokens."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None

    def legacy(self) -> dict[str, Any]:
        cached = self.cache_read_tokens or 0
        written = self.cache_write_tokens or 0
        return {
            "cache_read": cached, "cache_creation": written,
            "uncached_input": max(0, (self.input_tokens or 0) - cached - written),
            "output": self.output_tokens or 0,
            "usage_missing": self.input_tokens is None and self.output_tokens is None,
        }


class LLMResult(Contract):
    message: AssistantMessage
    usage: TokenUsage = Field(default_factory=TokenUsage)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def legacy_fields(self, *, include_prefix: bool = False) -> tuple:
        """Compatibility for older callers, never the provider's source format."""
        usage = {**self.usage.legacy(), **self.diagnostics}
        if include_prefix:
            from harness.messages.convert import content_block_to_wire
            usage["_assistant_prefix_blocks"] = [
                content_block_to_wire(b) for b in self.message.content if isinstance(b, ThinkingContent)
            ]
        return (
            self.message.text(),
            [{"id": b.tool_call_id, "name": b.name, "input": b.arguments}
             for b in self.message.tool_calls() if not b.partial],
            self.message.stop_reason or "unknown", usage,
        )
