#!/usr/bin/env python3
"""Measure v4.1 cross-task replay baselines without claiming counterfactuals.

The historical traces contain only the enforced branch.  This analyzer counts
where a shadow bypass *could* have differed and preserves the observed facts;
it intentionally refuses to infer the browser response or the model action that
would have followed a bypassed call.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator


RUNS = {
    "06fc0bb4b6c74146ad86c34b6626eb5f": (
        "active early final and numeric-gate friction"
    ),
    "0f75b23c391c4665852ae1160544eeeb": (
        "zero completion receipt followed by a false completion claim"
    ),
    "5e614adacc7048c7b3b1307c5c48c20c": (
        "plan ceremony, partial continuation, then provider quota interruption"
    ),
    "a5a9bc2bb31a4eee82f3cf16f6d6ac45": (
        "Yingdao worker duplicate Input.click plus Lead wait repetition"
    ),
    "48b4d7d71e62405a87db6fa7f1fc1404": (
        "1688 product/media collection with progress interventions"
    ),
    "7c90ee49e4b34f67b6c59454c7a81a28": (
        "Toolify listing/detail collection with broad progress interventions"
    ),
}

LEAD_LOOP_TOOLS = {
    "emit_task_plan",
    "spawn_browser_agent",
    "wait_browser_agents",
    "list_browser_agents",
    "lead_save_artifact",
}


def _jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _initial_run_events(path: Path) -> Iterable[Dict[str, Any]]:
    # A resumed run appends to the same run.jsonl with an explicit runId.  The
    # baseline is the original segment, whose events predate that field.
    return (event for event in _jsonl(path) if not event.get("runId"))


def _worker_gate_facts(
    trace_dir: Path,
    events: Iterable[Dict[str, Any]],
    *,
    trace_may_contain_resume: bool,
) -> Dict[str, Any]:
    """Read authoritative run events; use traces only for unlogged progress_gate."""
    events = list(events)
    by_type: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    blocking = 0
    analyzed_trace_count = 0
    finalized_worker_count = sum(
        1 for event in events if event.get("type") == "agent.final"
    )
    for event in events:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        normalized_type = ""
        if event_type == "progress.intervention":
            normalized_type = "progress_intervention"
        elif event_type in {"loop_guard.warn", "loop_guard.force_stop"}:
            if str(payload.get("tool") or "") not in LEAD_LOOP_TOOLS:
                normalized_type = "loop_guard"
        if not normalized_type:
            continue
        by_type[normalized_type] += 1
        by_reason[str(payload.get("reason") or event_type)] += 1
        blocking += 1

    # progress_gate has no run-level event in these historical versions. Read
    # only that type from worker traces; interventions and loop events above are
    # taken from the immutable run segment so a later /resume cannot double or
    # overwrite their baseline counts.
    trace_paths = sorted(trace_dir.glob("browser-[0-9][0-9][0-9].jsonl"))[
        :finalized_worker_count
    ]
    for path in trace_paths:
        analyzed_trace_count += 1
        for event in _jsonl(path):
            event_type = str(event.get("type") or "")
            if event_type != "progress_gate":
                continue
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            by_type[event_type] += 1
            by_reason[str(result.get("reason") or "unspecified")] += 1
            if result.get("tool_was_executed") is False:
                blocking += 1
    return {
        "workerTraceCount": analyzed_trace_count,
        "observedGateEvents": sum(by_type.values()),
        "observedBlockingEvents": blocking,
        "byType": dict(sorted(by_type.items())),
        "byReason": dict(sorted(by_reason.items())),
        "counterfactualCandidates": blocking,
        "sources": {
            "progressAndLoop": "original run.jsonl events without runId",
            "extractionProgressGate": "worker trace (not logged at run level)",
            "traceMayContainResume": trace_may_contain_resume,
        },
    }


def _lead_tool_call_count(events: Iterable[Dict[str, Any]], name: str) -> int:
    count = 0
    for event in events:
        if event.get("type") != "lead.model":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        calls = payload.get("tool_calls") if isinstance(payload.get("tool_calls"), list) else []
        count += sum(
            1 for call in calls
            if isinstance(call, dict) and call.get("name") == name
        )
    return count


def _lead_gate_facts(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    by_tool: Counter[str] = Counter()
    by_event: Counter[str] = Counter()
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in {"loop_guard.warn", "loop_guard.force_stop"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        tool = str(payload.get("tool") or "")
        if tool not in LEAD_LOOP_TOOLS:
            continue
        by_tool[tool] += 1
        by_event[event_type] += 1
    return {
        "observedGateEvents": sum(by_event.values()),
        "byEvent": dict(sorted(by_event.items())),
        "byTool": dict(sorted(by_tool.items())),
        "counterfactualCandidates": sum(by_event.values()),
    }


def analyze(root: Path) -> Dict[str, Any]:
    runs: Dict[str, Any] = {}
    for run_id, purpose in RUNS.items():
        task_dir = root / "worktree" / run_id
        all_events = list(_jsonl(task_dir / "run.jsonl"))
        events = [event for event in all_events if not event.get("runId")]
        trace_may_contain_resume = any(event.get("runId") for event in all_events)
        event_types = Counter(str(event.get("type") or "") for event in events)
        final_context = task_dir / "contexts" / "lead_agent-final-context.json"
        runs[run_id] = {
            "purpose": purpose,
            "sourceSegment": "events with no runId (original run only)",
            "acceptedPlanVersions": event_types["task_plan.accepted"],
            "rejectedPlanEmits": event_types["task_plan.rejected"],
            "reviewerCalls": (
                event_types["plan_validator.approved"]
                + event_types["plan_validator.rejected"]
                + event_types["plan_validator.error"]
            ),
            "leadFinalToolCalls": _lead_tool_call_count(events, "final_answer"),
            "leadContextBytes": final_context.stat().st_size if final_context.is_file() else None,
            "contextSnapshots": event_types["context.snapshot.saved"],
            "workerGateShadow": _worker_gate_facts(
                task_dir / "traces",
                events,
                trace_may_contain_resume=trace_may_contain_resume,
            ),
            "leadGateShadow": _lead_gate_facts(events),
        }
    return {
        "protocol": "v4.1-replay-baseline-v2",
        "mode": "historical-observation-only",
        "behavioralConclusion": None,
        "limitation": (
            "Historical traces contain only enforced outcomes. Blocking events"
            " identify replay candidates but cannot reveal the bypassed browser"
            " receipt or the model's subsequent action. A model-backed replay is"
            " required before removing production enforcement."
        ),
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(args.root.resolve())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
