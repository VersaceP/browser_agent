"""Replay-only A/B fixture for progress and duplicate-call enforcement.

This module is deliberately test-owned.  It is not imported by the harness and
does not add a runtime or configuration switch.  Variant A returns the existing
intervention.  Variant B records the same arithmetic observation and lets the
scripted call proceed, so a model-backed replay can receive facts without the
harness choosing its next action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

from harness.progress import ProgressAccountant
from harness.tools.loop_guard import check_tool_call_loop


_FACT_KEYS = {
    "tool",
    "pageId",
    "localFsWithoutExtraction",
    "localFsStreak",
    "repeatedLocalResultCount",
    "resultSignature",
    "turnsSinceArtifactProgress",
    "toolCalls",
    "diagnosticScope",
    "navigationEpoch",
    "diagnosticUses",
    "diagnosticLimit",
    "rowCount",
    "source",
    "gateBlocks",
    "duplicate_of_previous",
}


@dataclass
class _CaptureLogger:
    events: List[Dict[str, Any]] = field(default_factory=list)

    def write(self, event: str, payload: Dict[str, Any]) -> None:
        self.events.append({"event": event, "payload": dict(payload)})


def observation_fact(
    intervention: Dict[str, Any],
    *,
    gate_kind: str,
    should_stop: bool = False,
    diagnostic: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Project an intervention to attributed facts, excluding instructions."""
    fact = {
        "source": "replay_shadow_observation",
        "gateKind": gate_kind,
        "reasonObserved": str(intervention.get("reason") or ""),
        "wouldBlock": intervention.get("tool_was_executed") is False,
        "wouldStopWorker": bool(should_stop),
    }
    for key in _FACT_KEYS:
        if key in intervention:
            fact[key] = intervention[key]
    if diagnostic:
        for key in ("step", "streak", "warnAt", "forceStopAt", "signature"):
            if key in diagnostic:
                fact[key] = diagnostic[key]
    return fact


def replay_duplicate_calls(
    calls: Iterable[Dict[str, Any]],
    *,
    enforce: bool,
) -> Dict[str, Any]:
    """Run an identical action stream through the real loop detector."""
    logger = _CaptureLogger()
    agent = SimpleNamespace(recent_tool_signatures=[], logger=logger)
    observations: List[Dict[str, Any]] = []
    executed = 0
    blocked = 0
    stopped = False
    for step, call in enumerate(calls, start=1):
        outcome = check_tool_call_loop(
            agent,
            name=str(call.get("name") or ""),
            tool_input=call.get("input") or {},
            step=step,
        )
        if outcome is None:
            executed += 1
            continue
        intervention, should_stop = outcome
        diagnostic = logger.events[-1]["payload"] if logger.events else {}
        observations.append(observation_fact(
            intervention,
            gate_kind="loop_guard",
            should_stop=should_stop,
            diagnostic=diagnostic,
        ))
        if enforce:
            blocked += 1
            if should_stop:
                stopped = True
                break
        else:
            executed += 1
    return {
        "variant": "A-enforced" if enforce else "B-shadow-bypass",
        "attempted": executed + blocked,
        "executed": executed,
        "blocked": blocked,
        "stopped": stopped,
        "observations": observations,
    }


def replay_progress_calls(
    calls: Iterable[Dict[str, Any]],
    *,
    enforce: bool,
    requires_artifact: bool = True,
) -> Dict[str, Any]:
    """Run scripted calls through the real ProgressAccountant before/after API."""
    accountant = ProgressAccountant()
    observations: List[Dict[str, Any]] = []
    executed = 0
    blocked = 0
    for step, call in enumerate(calls, start=1):
        name = str(call.get("name") or "")
        artifact_count = int(call.get("artifact_count") or 0)
        intervention = accountant.before_tool(
            tool_name=name,
            artifact_count=artifact_count,
            local_fs_limit=int(call.get("local_fs_limit") or 5),
            no_artifact_limit=int(call.get("no_artifact_limit") or 8),
            requires_artifact=requires_artifact,
            own_artifact_read=False,
            step=step,
            page_id=str(call.get("pageId") or ""),
        )
        if intervention is not None:
            observations.append(observation_fact(
                intervention,
                gate_kind="progress",
            ))
            if enforce:
                blocked += 1
                continue
        executed += 1
        accountant.after_tool(
            tool_name=name,
            artifact_count=artifact_count,
            result=call.get("result") or {"status": "done"},
            own_artifact_read=False,
        )
    return {
        "variant": "A-enforced" if enforce else "B-shadow-bypass",
        "attempted": executed + blocked,
        "executed": executed,
        "blocked": blocked,
        "stopped": False,
        "observations": observations,
        "finalCounters": {
            "toolCalls": accountant.tool_calls,
            "turnsSinceArtifactProgress": accountant.turns_since_artifact_progress,
            "localFsWithoutExtraction": accountant.local_fs_without_extraction,
            "localFsStreak": accountant.local_fs_streak,
        },
    }
