"""harness.events.recorder - Imperative facade over the lifecycle scopes.

The scope context managers are the right shape for new code and for tests. The
agent loop is not new code: it is a ``while`` loop with a dozen ``break`` paths
inside an existing ``try/finally``, and re-indenting several hundred lines to
nest it inside four ``with`` blocks would be a far larger and riskier change
than the events are worth.

So the loop drives scopes imperatively, and the pairing guarantee is preserved
by a single rule: ``close()`` runs in the ``finally`` the loop already has, and
unwinds whatever is still open, innermost first. Cancellation, timeouts and
``return`` from the middle of the loop all reach it.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from typing import Any, Dict, List, Optional

from harness.messages.models import AssistantMessage


class LifecycleRecorder:
    """Drives agent/turn/message/tool scopes without nesting ``with`` blocks."""

    def __init__(
        self, factory: Optional[Any], on_close: Optional[Any] = None
    ) -> None:
        self._factory = factory
        # Anything registered on a shared, run-scoped emitter for this actor
        # alone has to come back off when the actor is done.
        self._on_close = on_close
        self._agent_cm: Optional[Any] = None
        self._agent: Optional[Any] = None
        self._turn_cm: Optional[Any] = None
        self._turn: Optional[Any] = None
        self._message_cm: Optional[Any] = None
        self._message: Optional[Any] = None
        self._tool_cm: Optional[Any] = None
        self._tool: Optional[Any] = None

    @property
    def enabled(self) -> bool:
        return bool(self._factory is not None and self._factory.enabled)

    # -- agent -------------------------------------------------------------

    def agent_start(
        self, *, label: Optional[str] = None, max_steps: Optional[int] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        if not self.enabled or self._agent_cm is not None:
            return
        self._agent_cm = self._factory.agent_scope(
            label=label, max_steps=max_steps, agent_id=agent_id,
        )
        self._agent = self._agent_cm.__enter__()

    def set_outcome(
        self, status: str, *, reason: Optional[str] = None, step_count: int = 0
    ) -> None:
        if self._agent is not None:
            self._agent.set_outcome(status, reason=reason, step_count=step_count)

    # -- turn --------------------------------------------------------------

    def turn_start(self, index: int) -> None:
        if self._agent is None:
            return
        # A previous turn left open by an early `continue` is closed here
        # rather than being reported as still running.
        self.turn_end()
        self._turn_cm = self._agent.turn(turn_index=index)
        self._turn = self._turn_cm.__enter__()

    def turn_end(
        self, *, status: str = "completed", error: Optional[str] = None
    ) -> None:
        if self._turn_cm is None:
            return
        self.message_end()
        self.tool_end()
        if error or status != "completed":
            self._turn.fail(error or status, status=status)
        cm, self._turn_cm, self._turn = self._turn_cm, None, None
        cm.__exit__(None, None, None)

    # -- message -----------------------------------------------------------

    def message_start(self, *, role: str = "assistant") -> None:
        if self._turn is None:
            return
        self.message_end()
        self._message_cm = self._turn.message(role=role)
        self._message = self._message_cm.__enter__()

    def message_update(
        self, *, delta_kind: str = "text", delta_chars: int = 0, block_index: int = 0
    ) -> None:
        if self._message is not None:
            self._message.update(
                delta_kind=delta_kind, delta_chars=delta_chars, block_index=block_index
            )

    def message_complete(
        self,
        message: Optional[AssistantMessage] = None,
        *,
        stop_reason: Optional[str] = None,
        truncation: Optional[Any] = None,
    ) -> None:
        if self._message is not None:
            self._message.complete(
                message, stop_reason=stop_reason, truncation=truncation,
            )

    def message_failed(self, error: str) -> None:
        if self._message is not None:
            self._message.fail(error)

    def message_end(self) -> None:
        if self._message_cm is None:
            return
        cm, self._message_cm, self._message = self._message_cm, None, None
        cm.__exit__(None, None, None)

    # -- tool --------------------------------------------------------------

    def tool_start(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._turn is None:
            return
        self.tool_end()
        self._tool_cm = self._turn.tool_execution(
            tool_call_id=tool_call_id, tool_name=tool_name, arguments=arguments,
        )
        self._tool = self._tool_cm.__enter__()

    def tool_update(self, progress: Optional[str] = None) -> None:
        if self._tool is not None:
            self._tool.update(progress)

    def tool_complete(
        self,
        *,
        is_error: bool = False,
        result_chars: int = 0,
        result_digest: Optional[str] = None,
        payload_ref: Optional[Any] = None,
    ) -> None:
        if self._tool is not None:
            self._tool.complete(
                is_error=is_error,
                result_chars=result_chars,
                result_digest=result_digest,
                payload_ref=payload_ref,
            )

    def tool_failed(self, error: str, *, status: str = "error") -> None:
        if self._tool is not None:
            self._tool.fail(error, status=status)

    def tool_end(self) -> None:
        if self._tool_cm is None:
            return
        cm, self._tool_cm, self._tool = self._tool_cm, None, None
        cm.__exit__(None, None, None)

    # -- unwind ------------------------------------------------------------

    def close(
        self, *, status: Optional[str] = None, reason: Optional[str] = None,
        step_count: int = 0,
    ) -> None:
        """Close every open scope, innermost first. Safe to call twice.

        Called from the agent loop's ``finally``, so the exception that is
        unwinding - if any - is still live in ``sys.exc_info()``. Reading it is
        what lets a cancelled or crashed run be recorded as cancelled or
        crashed. Trusting the caller's status instead is how a cancelled turn
        came out as ``status=completed``: at that point ``final_status`` is
        still its initial ``running``, because nothing ever set it.
        """

        unwinding = sys.exc_info()[1]
        if isinstance(unwinding, asyncio.CancelledError):
            terminal, terminal_reason = "aborted", "cancelled"
        elif unwinding is not None:
            terminal, terminal_reason = "error", type(unwinding).__name__
        elif status in {"aborted", "interrupted", "cancelled"}:
            terminal, terminal_reason = "aborted", reason or str(status)
        elif status in {"error", "failed"}:
            terminal, terminal_reason = "error", reason
        else:
            terminal, terminal_reason = None, reason

        if terminal is not None:
            self.tool_failed(terminal_reason or terminal, status=terminal)
            self.message_failed(terminal_reason or terminal)
        self.tool_end()
        self.message_end()
        if self._turn_cm is not None:
            self.turn_end(
                status=terminal or "completed",
                error=terminal_reason if terminal else None,
            )
        outcome = terminal or status
        if outcome in {None, "", "running"}:
            # The loop left without setting a terminal status and nothing was
            # raised. "running" in a closing event would be a lie either way.
            outcome = "incomplete"
        self.set_outcome(
            outcome, reason=terminal_reason or reason, step_count=step_count,
        )
        if self._agent_cm is not None:
            cm, self._agent_cm, self._agent = self._agent_cm, None, None
            cm.__exit__(None, None, None)
        if self._on_close is not None:
            detach, self._on_close = self._on_close, None
            try:
                detach()
            except Exception:
                pass


def result_digest(value: Any) -> str:
    """Stable short hash of a tool result, for pairing without the payload."""
    import json

    try:
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canonical = str(value)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def assistant_message_from_parts(
    *,
    text: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    prefix_blocks: Optional[List[Dict[str, Any]]] = None,
    stop_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> AssistantMessage:
    """Rebuild the ordered turn from the provider's current four-tuple.

    Until the providers emit canonical blocks themselves, this is where the
    order is recovered: thinking first (that is the order the providers hand
    the prefix blocks back in, and the only order Anthropic accepts on
    replay), then text, then tool calls.
    """
    from harness.messages.models import TextContent, ThinkingContent, ToolCallContent

    content: List[Any] = []
    for block in prefix_blocks or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind in {"thinking", "redacted_thinking"}:
            content.append(
                ThinkingContent(
                    thinking=str(block.get("thinking") or ""),
                    signature=block.get("signature"),
                    redacted=(kind == "redacted_thinking"),
                    encrypted=block.get("data") or block.get("encrypted_content"),
                )
            )
        elif kind == "text":
            content.append(TextContent(text=str(block.get("text") or "")))
    if text:
        content.append(TextContent(text=str(text)))
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        content.append(
            ToolCallContent(
                tool_call_id=str(call.get("id") or ""),
                name=str(call.get("name") or ""),
                arguments=(
                    call.get("input") if isinstance(call.get("input"), dict) else {}
                ),
            )
        )
    return AssistantMessage(
        content=content,
        stop_reason=stop_reason,
        usage={
            key: value for key, value in (usage or {}).items()
            if not str(key).startswith("_")
        },
    )


__all__ = ["LifecycleRecorder", "assistant_message_from_parts", "result_digest"]
