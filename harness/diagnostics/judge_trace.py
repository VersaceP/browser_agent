"""
harness.diagnostics.judge_trace - Offline trace digest for Judge calibration.

This module does not call an LLM and does not affect runtime decisions. It
builds a bounded digest of historical worker traces so Judge prompts can be
tested offline before any spawner integration is considered.

Run as:
    python -m harness.diagnostics.judge_trace
    python -m harness.diagnostics.judge_trace /path/to/worktree
    python -m harness.diagnostics.judge_trace /path/to/traces/browser-001.jsonl --json

Programmatic use:
    from harness.diagnostics.judge_trace import build_trace_digest, find_trace_files
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, List, Optional

from harness.utils import JsonDict


FAILURE_MARKERS = (
    "could not",
    "element not found",
    "failed",
    "invalid",
    "not found",
    "page crashed",
    "stale",
)
KEY_PARAM_FIELDS = (
    "pageId",
    "url",
    "id",
    "nodeId",
    "selector",
    "attribute",
    "name",
    "purpose",
    "expectedUrlPattern",
    "expectedTitlePattern",
)


def find_trace_files(paths: Optional[Iterable[str]] = None) -> List[Path]:
    roots = [Path(item) for item in paths] if paths else [Path("worktree")]
    trace_files: List[Path] = []
    for root in roots:
        if root.is_file():
            trace_files.append(root)
            continue
        if not root.exists():
            continue
        trace_files.extend(root.rglob("traces/*.jsonl"))
    return sorted({path.resolve() for path in trace_files})


def build_trace_digest(
    trace_path: Path,
    *,
    max_events: int = 120,
    max_text: int = 500,
) -> JsonDict:
    events = read_jsonl(trace_path)
    method_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    risk_signals: List[JsonDict] = []
    digest_events: List[JsonDict] = []
    saved_paths: List[str] = []

    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "unknown")
        event_counts[event_type] += 1
        compact_event = compact_trace_event(
            event,
            max_text=max_text,
            saved_paths=saved_paths,
        )
        if len(digest_events) < max_events:
            digest_events.append(compact_event)

        method = str(event.get("method") or "")
        if method:
            method_counts[method] += 1
        risk_signals.extend(risk_signals_for_event(event, index=index, max_text=max_text))

    steps = [
        optional_int(event.get("step"))
        for event in events
        if isinstance(event, dict)
    ]
    return {
        "tracePath": str(trace_path),
        "steps": max([step for step in steps if step is not None], default=0),
        "eventCount": len(events),
        "eventsIncluded": len(digest_events),
        "eventCounts": dict(event_counts),
        "methodCounts": dict(method_counts),
        "riskSignalCount": len(risk_signals),
        "riskSignals": risk_signals[:50],
        "savedPaths": sorted(set(saved_paths))[:100],
        "events": digest_events,
        "truncated": len(events) > len(digest_events),
    }


def read_jsonl(path: Path) -> List[JsonDict]:
    events: List[JsonDict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    events.append({"type": "json_decode_error", "line": text[:500]})
                    continue
                if isinstance(value, dict):
                    events.append(value)
    except OSError as exc:
        events.append({"type": "read_error", "error": str(exc)})
    return events


def compact_trace_event(
    event: JsonDict,
    *,
    max_text: int,
    saved_paths: List[str],
) -> JsonDict:
    event_type = str(event.get("type") or "unknown")
    base: JsonDict = {
        "type": event_type,
        "step": optional_int(event.get("step")),
    }
    if event_type == "model":
        base["text"] = trim_text(event.get("text"), max_text)
        base["toolCalls"] = compact_tool_calls(event.get("tool_calls"))
        return compact_none_values(base)
    if event_type == "browser_call":
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        response = result.get("response") if isinstance(result.get("response"), dict) else {}
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        collect_saved_paths(result, saved_paths)
        base.update({
            "method": str(event.get("method") or result.get("method") or ""),
            "params": compact_params(event.get("params")),
            "observation": trim_text(response.get("observation") or result.get("error"), max_text),
            "dataKeys": sorted(str(key) for key in data.keys())[:30],
            "errorClassification": get_path(result, "errorClassification.type"),
            "challenge": compact_challenge(result),
        })
        return compact_none_values(base)
    if event_type == "page_stats":
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        base["result"] = {
            "pageId": result.get("pageId"),
            "url": trim_text(result.get("url"), 240),
            "title": trim_text(result.get("title"), 160),
            "nodes": result.get("nodes"),
            "actionable": result.get("actionable"),
            "semanticItems": result.get("semanticItems"),
            "links": result.get("links"),
            "hint": trim_text(result.get("hint"), max_text),
        }
        return compact_none_values(base)
    if event_type == "snapshot_diff":
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        base["result"] = {
            "fromStep": result.get("fromStep"),
            "toStep": result.get("toStep"),
            "crossPageDiff": result.get("crossPageDiff"),
            "semanticAdded": result.get("semanticAdded"),
            "semanticRemoved": result.get("semanticRemoved"),
            "physicalAdded": result.get("physicalAdded"),
            "physicalRemoved": result.get("physicalRemoved"),
            "totalNodeDelta": result.get("totalNodeDelta"),
            "semanticChanged": result.get("semanticChanged"),
            "physicalChanged": result.get("physicalChanged"),
        }
        return compact_none_values(base)
    # progress_intervention/progress_gate are historical: production emits the
    # observation types now. Both are read so archived traces still render.
    if event_type in {
        "record_extraction",
        "final_answer",
        "loop_guard",
        "loop_nudge",
        "progress_observation",
        "loop_observation",
        "progress_intervention",
        "progress_gate",
    }:
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        collect_saved_paths(result, saved_paths)
        base["result"] = compact_result(result, max_text=max_text)
        return compact_none_values(base)
    result = event.get("result")
    if isinstance(result, dict):
        collect_saved_paths(result, saved_paths)
        base["result"] = compact_result(result, max_text=max_text)
    return compact_none_values(base)


def compact_tool_calls(value: Any) -> List[JsonDict]:
    if not isinstance(value, list):
        return []
    calls: List[JsonDict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
        calls.append({
            "name": str(item.get("name") or ""),
            "method": str(tool_input.get("method") or ""),
            "params": compact_params(tool_input.get("params") or tool_input),
        })
    return calls


def compact_params(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    return {
        key: trim_text(value.get(key), 240)
        for key in KEY_PARAM_FIELDS
        if key in value and value.get(key) is not None
    }


def compact_result(result: JsonDict, *, max_text: int) -> JsonDict:
    compact: JsonDict = {}
    for key in (
        "status",
        "reason",
        "warning",
        "answer",
        "error",
        "name",
        "rowCount",
        "savedPath",
        "next_instruction",
    ):
        if key in result:
            compact[key] = trim_text(result.get(key), max_text)
    if "repeatCount" in result:
        compact["repeatCount"] = optional_int(result.get("repeatCount"))
    if "action" in result:
        compact["action"] = trim_text(result.get("action"), 120)
    return compact


def compact_challenge(result: JsonDict) -> Optional[JsonDict]:
    challenge = result.get("suspected_challenge")
    adjudication = result.get("challengeAdjudication")
    payload: JsonDict = {}
    if isinstance(challenge, dict):
        payload["suspicionScore"] = optional_int(challenge.get("suspicionScore"))
        payload["adjudication"] = challenge.get("adjudication")
        payload["lastVlVerdict"] = challenge.get("lastVlVerdict")
    if isinstance(adjudication, dict):
        payload["vlVerdict"] = adjudication.get("verdict")
        payload["confidence"] = adjudication.get("confidence")
        payload["recommendedRecovery"] = adjudication.get("recommended_recovery")
    return compact_none_values(payload) or None


def risk_signals_for_event(event: JsonDict, *, index: int, max_text: int) -> List[JsonDict]:
    event_type = str(event.get("type") or "")
    signals: List[JsonDict] = []
    step = optional_int(event.get("step"))
    if event_type in {"loop_nudge", "loop_guard", "progress_intervention", "progress_gate"}:
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        signals.append({
            "type": event_type,
            "step": step,
            "reason": trim_text(result.get("reason") or result.get("status"), max_text),
        })
        return signals
    if event_type != "browser_call":
        return signals

    method = str(event.get("method") or "")
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    observation = str(response.get("observation") or result.get("error") or "")
    lower_observation = observation.lower()
    if any(marker in lower_observation for marker in FAILURE_MARKERS):
        signals.append({
            "type": "browser_observation_failure",
            "step": step,
            "method": method,
            "detail": trim_text(observation, max_text),
        })

    error_type = get_path(result, "errorClassification.type")
    if error_type:
        signals.append({
            "type": "error_classification",
            "step": step,
            "method": method,
            "detail": str(error_type),
        })

    challenge = compact_challenge(result)
    if challenge and (
        str(challenge.get("vlVerdict") or "") not in {"", "normal_loading"}
        or (optional_int(challenge.get("suspicionScore")) or 0) >= 70
    ):
        signals.append({
            "type": "challenge_signal",
            "step": step,
            "method": method,
            "detail": challenge,
        })

    if response.get("error"):
        signals.append({
            "type": "response_error",
            "step": step,
            "method": method,
            "detail": trim_text(response.get("error"), max_text),
        })
    return signals


def collect_saved_paths(value: Any, output: List[str]) -> None:
    if isinstance(value, dict):
        saved_path = value.get("savedPath")
        if isinstance(saved_path, str) and saved_path:
            output.append(saved_path)
        for nested in value.values():
            collect_saved_paths(nested, output)
    elif isinstance(value, list):
        for nested in value:
            collect_saved_paths(nested, output)


def get_path(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def trim_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    text = value.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def optional_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def compact_none_values(value: JsonDict) -> JsonDict:
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def print_text_report(digests: List[JsonDict]) -> None:
    if not digests:
        print("No worker trace files found.")
        return
    for digest in digests:
        print(Path(str(digest.get("tracePath") or "")).name)
        print(
            f"  steps={digest.get('steps')} events={digest.get('eventCount')} "
            f"riskSignals={digest.get('riskSignalCount')}"
        )
        methods = digest.get("methodCounts") if isinstance(digest.get("methodCounts"), dict) else {}
        if methods:
            top = sorted(methods.items(), key=lambda item: (-int(item[1]), str(item[0])))[:8]
            print("  methods: " + ", ".join(f"{name}={count}" for name, count in top))
        risks = digest.get("riskSignals") if isinstance(digest.get("riskSignals"), list) else []
        for risk in risks[:5]:
            if isinstance(risk, dict):
                print(
                    "  risk: "
                    f"{risk.get('type')} step={risk.get('step')} "
                    f"{risk.get('method') or ''} {risk.get('reason') or risk.get('detail') or ''}"
                )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m harness.diagnostics.judge_trace",
        description="Build offline worker trace digests for Judge prompt calibration.",
    )
    parser.add_argument("paths", nargs="*", help="Worktree dirs or trace JSONL files.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--max-events", type=int, default=120)
    parser.add_argument("--max-text", type=int, default=500)
    args = parser.parse_args(argv)

    trace_files = find_trace_files(args.paths or None)
    digests = [
        build_trace_digest(path, max_events=args.max_events, max_text=args.max_text)
        for path in trace_files
    ]
    if args.json:
        print(json.dumps({"traces": digests}, ensure_ascii=False, indent=2, default=str))
    else:
        print_text_report(digests)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
