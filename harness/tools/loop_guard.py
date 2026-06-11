"""
harness.tools.loop_guard - Detect and break "same tool, same args" loops.

A model can lock into calling the same tool with the same arguments many times
in a row even when the previous tool_result already contained the answer (we
have observed this with kimi-2.6 against local_fs_search: 23 identical calls
in 23 steps despite the first call returning 45 hits). The dispatcher cannot
fix the model's reasoning, but it can refuse to execute obviously-degenerate
work and steer the agent toward a pivot or a clean termination.

Behavior:
- Exact duplicate calls are tracked as a consecutive streak.
- Thresholds are bucketed by tool/method. `local_fs_*` and LeadAgent control
  tools stay strict; repetitive browser probes and scrolls get more room so
  advisory loop nudges can steer the model before the hard fuse blows.
- At warn threshold the tool is NOT re-executed; the dispatcher returns a
  directive telling the model to pivot or finalize. At force threshold the
  agent is hard-stopped with status=extraction_inconclusive.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, List, Optional, Tuple

from harness.utils import JsonDict


DEFAULT_WARN_AT = 4
DEFAULT_FORCE_STOP_AT = 8
STRICT_WARN_AT = 3
STRICT_FORCE_STOP_AT = 5
HISTORY_WINDOW = 24

STRICT_TOOL_PREFIXES = ("local_fs_",)
STRICT_TOOL_NAMES = {
    "emit_task_plan",
    "spawn_browser_agent",
    "wait_browser_agents",
    "list_browser_agents",
    "lead_save_artifact",
}
METHOD_THRESHOLDS = {
    "Input.scroll": (10, 20),
    "DOM.getAXTree": (6, 12),
    "Page.getState": (6, 12),
    "DOM.getText": (6, 12),
    "DOM.getAttribute": (6, 12),
    "Input.click": (5, 10),
    "Input.type": (5, 10),
    "Input.press": (5, 10),
    "Runtime.evaluate": (4, 8),
}


def tool_call_signature(name: str, tool_input: Any) -> str:
    """Stable hash over (tool_name, normalized_input). Sort keys so semantically
    identical dicts hash the same regardless of key order."""
    payload = json.dumps(
        {"name": str(name or ""), "input": tool_input if tool_input is not None else {}},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def trailing_streak(history: List[str], signature: str) -> int:
    """Count how many of the most recent history entries equal `signature`
    (including the current one once it is appended)."""
    streak = 1
    for prior in reversed(history):
        if prior == signature:
            streak += 1
        else:
            break
    return streak


def loop_guard_thresholds(name: str, tool_input: Any) -> Tuple[int, int, str]:
    """Return warn/force thresholds and a human-readable bucket label."""
    tool_name = str(name or "")
    if tool_name in STRICT_TOOL_NAMES or tool_name.startswith(STRICT_TOOL_PREFIXES):
        return STRICT_WARN_AT, STRICT_FORCE_STOP_AT, tool_name

    method = ""
    if tool_name == "browser_call" and isinstance(tool_input, dict):
        method = str(tool_input.get("method") or "")
    elif "." in tool_name:
        # Direct ABCP capability tools land here as top-level names.
        method = tool_name

    if method in METHOD_THRESHOLDS:
        warn_at, force_stop_at = METHOD_THRESHOLDS[method]
        return warn_at, force_stop_at, method

    return DEFAULT_WARN_AT, DEFAULT_FORCE_STOP_AT, method or tool_name


def check_tool_call_loop(
    agent: Any,
    *,
    name: str,
    tool_input: Any,
    step: int,
    warn_at: Optional[int] = None,
    force_stop_at: Optional[int] = None,
) -> Optional[Tuple[JsonDict, bool]]:
    """Return None to proceed with normal dispatch, or a (result, should_stop)
    tuple when the loop guard wants to short-circuit.

    The caller is responsible for storing `recent_tool_signatures: List[str]`
    on `agent` (and ideally initialising it to []). We update the list in
    place to keep the bookkeeping next to the decision.
    """
    history: List[str] = getattr(agent, "recent_tool_signatures", None) or []
    signature = tool_call_signature(name, tool_input)
    streak = trailing_streak(history, signature)
    bucket_warn_at, bucket_force_stop_at, threshold_bucket = loop_guard_thresholds(
        name,
        tool_input,
    )
    warn_at = bucket_warn_at if warn_at is None else warn_at
    force_stop_at = (
        bucket_force_stop_at if force_stop_at is None else force_stop_at
    )

    # Maintain the rolling window. We always append the new signature so the
    # *next* call can see the just-issued one.
    history.append(signature)
    if len(history) > HISTORY_WINDOW:
        del history[: len(history) - HISTORY_WINDOW]
    agent.recent_tool_signatures = history

    if streak >= force_stop_at:
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "loop_guard.force_stop",
                {
                    "step": step,
                    "tool": name,
                    "thresholdBucket": threshold_bucket,
                    "streak": streak,
                    "warnAt": warn_at,
                    "forceStopAt": force_stop_at,
                    "signature": signature[:12],
                },
            )
        result: JsonDict = {
            "status": "extraction_inconclusive",
            "answer": (
                f"Loop guard force-stopped: the same {name} call with identical "
                f"arguments was issued {streak} times in a row. The earlier "
                "tool_result already contained whatever this call would return; "
                "the model did not act on it. Aborting this worker so the "
                "LeadAgent can pivot strategy or escalate."
            ),
            "reason": "duplicate_tool_call_loop",
        }
        return result, True

    if streak >= warn_at:
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "loop_guard.warn",
                {
                    "step": step,
                    "tool": name,
                    "thresholdBucket": threshold_bucket,
                    "streak": streak,
                    "warnAt": warn_at,
                    "forceStopAt": force_stop_at,
                    "signature": signature[:12],
                },
            )
        # The tool is NOT executed this turn. We send back an explicit
        # directive so the model is forced to either pivot or finalize.
        next_stop = max(1, force_stop_at - streak)
        result = {
            "status": "loop_guard_intercepted",
            "warning": (
                f"Duplicate-call guard: this is identical call #{streak} to "
                f"{name} with the same arguments. The previous tool_result "
                "is still in your context — re-read it before retrying. "
                "If you have already inspected it, PIVOT NOW: "
                "(a) change pattern/glob/path significantly; "
                "(b) call browser_call with a different ABCP method "
                "(e.g. DOM.getText on a known selector, Runtime.evaluate with "
                "`return X;`, Input.scroll); "
                "(c) call final_answer with status=\"extraction_inconclusive\" "
                "if the data is genuinely unreachable. "
                f"Another {next_stop} identical call(s) will hard-stop this worker."
            ),
            "echoed_call": {"name": name, "input": tool_input},
            "tool_was_executed": False,
        }
        return result, False

    return None
