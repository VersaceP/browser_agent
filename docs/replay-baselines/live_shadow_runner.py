#!/usr/bin/env python3
"""Run a real model/browser replay with production gates or shadow bypass.

This wrapper monkeypatches the current Python process only.  It adds no runtime
configuration field and cannot change a normal ``main.py`` invocation.  In the
B variant the existing detectors still update their counters, but their result
is converted to an attributed fact attached to the real tool result instead of
blocking the call or stopping the worker.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from docs.replay_shadow import observation_fact  # noqa: E402
import harness.tools.browser_tools as browser_tools  # noqa: E402
import harness.tools.lead_tools as lead_tools  # noqa: E402
import main as harness_main  # noqa: E402


class _CaptureLogger:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def write(self, event: str, payload: Dict[str, Any]) -> None:
        self.events.append({"event": event, "payload": dict(payload)})


def _queue(agent: Any, fact: Dict[str, Any]) -> None:
    pending = getattr(agent, "_replay_shadow_observations", None)
    if not isinstance(pending, list):
        pending = []
        agent._replay_shadow_observations = pending
    pending.append(fact)


@contextmanager
def shadow_bypass() -> Iterator[None]:
    """Install process-local detector witnesses while bypassing enforcement."""
    original_loop = browser_tools.check_tool_call_loop
    original_progress_gate = browser_tools._call_extraction_progress_gate
    original_progress = browser_tools._check_progress_before
    original_execute = browser_tools.execute_browser_tool
    original_lead_loop = lead_tools.check_tool_call_loop
    original_execute_lead = lead_tools.execute_lead_tool

    def loop_witness(
        agent: Any,
        *,
        name: str,
        tool_input: Any,
        step: int,
        warn_at: Optional[int] = None,
        force_stop_at: Optional[int] = None,
    ) -> None:
        real_logger = getattr(agent, "logger", None)
        capture = _CaptureLogger()
        agent.logger = capture
        try:
            outcome = original_loop(
                agent,
                name=name,
                tool_input=tool_input,
                step=step,
                warn_at=warn_at,
                force_stop_at=force_stop_at,
            )
        finally:
            agent.logger = real_logger
        if outcome is not None:
            intervention, should_stop = outcome
            diagnostic = capture.events[-1]["payload"] if capture.events else {}
            fact = observation_fact(
                intervention,
                gate_kind="loop_guard",
                should_stop=should_stop,
                diagnostic=diagnostic,
            )
            if not fact.get("reasonObserved") and capture.events:
                fact["reasonObserved"] = capture.events[-1]["event"]
            _queue(agent, fact)
            if real_logger is not None:
                real_logger.write("replay_shadow.loop_observed", fact)
        return None

    def extraction_gate_witness(
        agent: Any,
        next_tool: str,
        tool_input: Dict[str, Any],
    ) -> None:
        real_logger = getattr(agent, "logger", None)
        capture = _CaptureLogger()
        agent.logger = capture
        try:
            intervention = original_progress_gate(agent, next_tool, tool_input)
        finally:
            agent.logger = real_logger
        if intervention is not None:
            fact = observation_fact(
                intervention,
                gate_kind="extraction_progress",
            )
            _queue(agent, fact)
            if real_logger is not None:
                real_logger.write("replay_shadow.extraction_progress_observed", fact)
        return None

    def progress_witness(
        agent: Any,
        tool_name: str,
        tool_input: Optional[Dict[str, Any]] = None,
        step: Optional[int] = None,
        *,
        charge_diagnostic: bool = True,
    ) -> None:
        real_logger = getattr(agent, "logger", None)
        capture = _CaptureLogger()
        agent.logger = capture
        try:
            intervention = original_progress(
                agent,
                tool_name,
                tool_input,
                step,
                charge_diagnostic=charge_diagnostic,
            )
        finally:
            agent.logger = real_logger
        if intervention is not None:
            fact = observation_fact(intervention, gate_kind="progress")
            _queue(agent, fact)
            if real_logger is not None:
                real_logger.write("replay_shadow.progress_observed", fact)
        return None

    async def execute_with_observations(
        agent: Any,
        tool_call: Dict[str, Any],
        step: int,
    ) -> Tuple[Dict[str, Any], bool]:
        agent._replay_shadow_observations = []
        result, should_stop = await original_execute(agent, tool_call, step)
        facts = list(getattr(agent, "_replay_shadow_observations", []) or [])
        if facts and isinstance(result, dict):
            result = dict(result)
            result["shadowEnforcementObservations"] = facts
            result["shadowNotice"] = (
                "Replay-only B variant: the listed arithmetic observations"
                " would have blocked or stopped this call in variant A. The"
                " call executed; decide the next experiment from its real"
                " receipt and these attributed facts."
            )
        return result, should_stop

    def lead_loop_witness(
        agent: Any,
        *,
        name: str,
        tool_input: Any,
        step: int,
        warn_at: Optional[int] = None,
        force_stop_at: Optional[int] = None,
    ) -> None:
        real_logger = getattr(agent, "logger", None)
        capture = _CaptureLogger()
        agent.logger = capture
        try:
            outcome = original_lead_loop(
                agent,
                name=name,
                tool_input=tool_input,
                step=step,
                warn_at=warn_at,
                force_stop_at=force_stop_at,
            )
        finally:
            agent.logger = real_logger
        if outcome is not None:
            intervention, should_stop = outcome
            diagnostic = capture.events[-1]["payload"] if capture.events else {}
            fact = observation_fact(
                intervention,
                gate_kind="lead_loop_guard",
                should_stop=should_stop,
                diagnostic=diagnostic,
            )
            if not fact.get("reasonObserved") and capture.events:
                fact["reasonObserved"] = capture.events[-1]["event"]
            _queue(agent, fact)
            if real_logger is not None:
                real_logger.write("replay_shadow.lead_loop_observed", fact)
        return None

    async def execute_lead_with_observations(
        agent: Any,
        tool_call: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool]:
        agent._replay_shadow_observations = []
        result, should_stop = await original_execute_lead(agent, tool_call)
        facts = list(getattr(agent, "_replay_shadow_observations", []) or [])
        if facts and isinstance(result, dict):
            result = dict(result)
            result["shadowEnforcementObservations"] = facts
            result["shadowNotice"] = (
                "Replay-only B variant: Lead loop enforcement was observed but"
                " bypassed. The tool executed; use its real receipt together"
                " with the attributed repetition facts."
            )
        return result, should_stop

    browser_tools.check_tool_call_loop = loop_witness
    browser_tools._call_extraction_progress_gate = extraction_gate_witness
    browser_tools._check_progress_before = progress_witness
    browser_tools.execute_browser_tool = execute_with_observations
    lead_tools.check_tool_call_loop = lead_loop_witness
    lead_tools.execute_lead_tool = execute_lead_with_observations
    try:
        yield
    finally:
        browser_tools.check_tool_call_loop = original_loop
        browser_tools._call_extraction_progress_gate = original_progress_gate
        browser_tools._check_progress_before = original_progress
        browser_tools.execute_browser_tool = original_execute
        lead_tools.check_tool_call_loop = original_lead_loop
        lead_tools.execute_lead_tool = original_execute_lead


def historical_task(run_id: str) -> str:
    """Read only the original user task from a historical Lead context."""
    normalized = str(run_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        raise ValueError("historical task id must be a 32-character lowercase hex id")
    context_path = (
        REPO_ROOT / "worktree" / normalized / "contexts" / "lead_agent-final-context.json"
    )
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    first = messages[0].get("content") if messages and isinstance(messages[0], dict) else ""
    text = str(first or "")
    match = re.search(r"<user_task>\s*(.*?)\s*</user_task>", text, re.S)
    if not match or not match.group(1).strip():
        raise ValueError(f"historical user task not found in {context_path}")
    return match.group(1).strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay main.py with A=enforced or B=shadow-bypass gates"
    )
    parser.add_argument("--variant", choices=("A", "B"), required=True)
    parser.add_argument(
        "--historical-task-id",
        default="",
        help="Start a fresh run using only the original task text from this worktree id",
    )
    parser.add_argument("harness_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    forwarded = list(args.harness_args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if args.historical_task_id:
        if any(item == "--resume" or item.startswith("--resume=") for item in forwarded):
            parser.error("--historical-task-id cannot be combined with --resume")
        forwarded.extend(["--task", historical_task(args.historical_task_id)])
    if args.variant == "A":
        return harness_main.main(forwarded)
    with shadow_bypass():
        return harness_main.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
