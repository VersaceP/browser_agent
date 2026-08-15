"""Observe exact duplicate tool calls without deciding whether they are useful.

An identical request is an arithmetic fact.  It is not proof of an identical
world state: waits, page reads and searches can legitimately return new data,
and production runs showed that blocking a repeated ``wait_browser_agents``
sent the Lead into several steps of irrelevant file archaeology.  Global run
budgets bound ordinary repetition; irreversible side effects retain their own
idempotency, confirmation and uncertain-side-effect protections.

One spend limit survives that, and it is deliberately far above anything a
working agent does.  Repetition here is judged by nobody: every call up to the
limit is dispatched, and the limit itself makes no claim about whether the
work was useful, only about how much of a run one byte-identical request may
consume.  See ``docs/tau-informed-simplification-plan.md`` for the decision.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, List, Optional, Tuple

from harness.utils import JsonDict


HISTORY_WINDOW = 24
# One number for every tool.  The per-tool table this replaced encoded guesses
# about which repetitions were suspicious - ``local_fs_*`` stopped at 5,
# ``Runtime.evaluate`` at 8 - and those guesses were the overfitting: in task
# a608b5e7 the longest identical streak across ten workers was five consecutive
# ``Input.scroll`` calls, which is what reading a feed looks like.  The
# pathology this bounds is the one seen with kimi-2.6, which issued the same
# ``local_fs_search`` 23 times in 23 steps.  Keep it below HISTORY_WINDOW or
# the streak can never be observed to reach it.
DUPLICATE_CALL_STOP_AT = 20


def tool_call_signature(name: str, tool_input: Any) -> str:
    """Stable hash over ``(tool name, normalized input)``."""
    payload = json.dumps(
        {
            "name": str(name or ""),
            "input": tool_input if tool_input is not None else {},
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def trailing_streak(history: List[str], signature: str) -> int:
    """Count consecutive occurrences, including the call being observed."""
    streak = 1
    for prior in reversed(history):
        if prior != signature:
            break
        streak += 1
    return streak


def check_tool_call_loop(
    agent: Any,
    *,
    name: str,
    tool_input: Any,
    step: int,
    warn_at: Optional[int] = None,
    force_stop_at: Optional[int] = None,
) -> Optional[Tuple[JsonDict, bool]]:
    """Record repetition, allow dispatch, and stop past the spend limit.

    ``warn_at`` is accepted for replay compatibility and ignored: there is no
    warn tier, because refusing to execute a call is where the old guard did
    its damage.  ``force_stop_at`` overrides ``DUPLICATE_CALL_STOP_AT``.
    """
    del warn_at
    stop_at = int(
        DUPLICATE_CALL_STOP_AT if force_stop_at is None else force_stop_at
    )
    history: List[str] = getattr(agent, "recent_tool_signatures", None) or []
    signature = tool_call_signature(name, tool_input)
    streak = trailing_streak(history, signature)
    history.append(signature)
    if len(history) > HISTORY_WINDOW:
        del history[: len(history) - HISTORY_WINDOW]
    agent.recent_tool_signatures = history
    if streak < 2:
        return None

    if stop_at > 0 and streak > stop_at:
        # Every one of the previous calls ran. This one does not, and the
        # worker ends here so the decision goes up rather than being made
        # about the model's reasoning down here.
        stop_result: JsonDict = {
            "status": "extraction_inconclusive",
            "reason": "duplicate_call_spend_limit",
            "tool": str(name or ""),
            "consecutiveIdenticalCalls": streak,
            "spendLimit": stop_at,
            "answer": (
                f"This worker issued {streak - 1} byte-identical {name} calls in"
                f" a row, all of which were dispatched, and stopped at the"
                f" harness spend limit of {stop_at}. No judgement is made here"
                " about whether the repetition was productive: the trace and"
                " the artifacts hold the evidence for that."
            ),
        }
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write("loop_guard.spend_limit", {
                "step": step,
                "tool": str(name or ""),
                "consecutiveIdenticalCalls": streak,
                "spendLimit": stop_at,
                "signature": signature[:12],
            })
        return stop_result, True

    observation: JsonDict = {
        "source": "duplicate_call_observer",
        "tool": str(name or ""),
        "consecutiveIdenticalCalls": streak,
        "signature": signature[:12],
        "step": step,
        "note": (
            "This is an attributed repetition fact, not a verdict. It is"
            " recorded before dispatch and does not report whether the call"
            " ran; the receipt beside it is authoritative."
        ),
    }
    pending = getattr(agent, "_pending_loop_observations", None)
    if not isinstance(pending, list):
        pending = []
        agent._pending_loop_observations = pending
    pending.append(observation)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("loop_guard.observed", observation)
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({
            "type": "loop_observation",
            "step": step,
            "result": observation,
        })
    return None
