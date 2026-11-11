"""Evidence for author experiments; no automatic retry or business verdict.

An experiment travels with execute_workflow, so recording its hypothesis does
not require another model turn. Comparisons describe observed changes only.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from harness.workflow_mode.evidence import child_pointer, evidence_reference, pointer, value_index, resolve_reference

_MISSING = object()


def browser_findings(answer: Any) -> Any:
    """Expose JSON answers as addressable data, keeping the raw answer in result."""
    if isinstance(answer, str):
        try:
            parsed = json.loads(answer)
        except ValueError:
            return answer
        if isinstance(parsed, (dict, list)):
            return parsed
    return answer


def prepare_experiment(value: dict[str, Any] | None, *, known_refs: set[str],
                       executions: list[dict[str, Any]], read: Callable[[str], Any]) -> dict[str, Any]:
    """Validate addresses/ownership, never whether a hypothesis is sensible."""
    experiment = dict(value or {})
    assessment = experiment.get("previous_assessment") or {}
    for item in [*experiment.get("evidence_refs", []), *assessment.get("evidence_refs", [])]:
        ref, path = evidence_reference(item, known_refs=known_refs)
        if ref not in known_refs:
            raise ValueError("Experiment evidence must belong to this task")
        pointer(read(ref), path)
    baseline = experiment.get("baseline_ref")
    experiment['baselineSelection'] = 'explicit' if baseline else 'none'
    if baseline:
        baseline = resolve_reference(baseline, known_refs)
        experiment["baseline_ref"] = baseline
    if baseline and baseline not in {e.get("receiptRef") for e in executions}:
        raise ValueError("Experiment baseline must be an execution receipt of this task")
    if not baseline and executions:
        experiment["baseline_ref"] = executions[-1].get("receiptRef")
        experiment['baselineSelection'] = 'previous-execution-candidate'
    for check in experiment.get("checks", []):
        path = check["pointer"]
        if path and not path.startswith("/"):
            raise ValueError("Experiment check pointer must be relative to output and start with /")
    return experiment


def _sample(value: Any, path: str) -> dict[str, Any]:
    if len(json.dumps(value, ensure_ascii=False, default=str)) <= 400:
        return {"value": value}
    return {"truncated": True, **value_index(value, path)}


def changes(before: Any, after: Any, path: str = "", *, limit: int = 16) -> dict[str, Any]:
    """Bounded structural diff. Absence is distinct from an observed null."""
    rows, total = [], 0
    stack = [(path, before, after)]
    while stack:
        address, left, right = stack.pop()
        if type(left) is type(right) and left == right:
            continue
        if isinstance(left, dict) and isinstance(right, dict):
            keys = sorted(left.keys() | right.keys())
            stack.extend((child_pointer(address, k), left.get(k, _MISSING), right.get(k, _MISSING))
                         for k in reversed(keys))
            continue
        total += 1
        if len(rows) < limit:
            rows.append({"pointer": address,
                         "before": {"present": False} if left is _MISSING else {"present": True, **_sample(left, address)},
                         "after": {"present": False} if right is _MISSING else {"present": True, **_sample(right, address)}})
    return {"count": total, "items": rows, "truncated": total > len(rows)}


def _output(receipt: dict[str, Any]) -> Any:
    try:
        return pointer(receipt, "/response/data/output")
    except ValueError:
        return _MISSING


def execution_metrics(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    """Observed platform costs only; these are not model tokens or task wall time."""
    if receipt is None:
        return None
    data = ((receipt.get("response") or {}).get("data") or {})
    if not isinstance(data, dict):
        data = {}
    records = data.get("results")
    groups: dict[str, dict[str, Any]] = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        name = record.get("action") or record.get("kind") or "unknown"
        row = groups.setdefault(name, {"method": name, "count": 0, "failed": 0,
                                      "timedCalls": 0, "sumCallDurationMs": 0})
        row["count"] += 1
        row["failed"] += record.get("status") == "failed"
        duration = record.get("durationMs")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            row["timedCalls"] += 1
            row["sumCallDurationMs"] += duration
    from harness.workflow.call_facts import call_facts
    facts = call_facts(data)
    ordered = sorted(groups.values(), key=lambda row: (-row["sumCallDurationMs"], -row["count"], row["method"]))
    return {"requestDurationMs": receipt.get("durationMs"),
            "platformDurationMs": (data.get("timing") or {}).get("durationMs"),
            "callCoverage": {key: facts.get(key) for key in ("recordedCalls", "totalCalls", "complete", "failedCalls")},
            "methodGroups": ordered[:8], "methodGroupsTruncated": len(ordered) > 8,
            "interpretation": "Only recorded calls; incomplete records are lower bounds. Call durations may "
                "overlap and must not be summed as end-to-end time. Token usage, model rounds, human "
                "waiting and task wall time are separate. Counts alone do not prove a better strategy."}


def comparison(experiment: dict[str, Any], *, current: dict[str, Any], current_ref: str,
               baseline: dict[str, Any] | None, source_changes: dict[str, Any] | None) -> dict[str, Any]:
    """Compare requested scope and observed output without inferring progress."""
    output = _output(current)
    previous = _output(baseline) if baseline is not None else None
    checks = []
    for check in experiment.get("checks", []):
        row = {**check, "expectedIs": "author hypothesis, not an observed result"}
        for label, receipt, ref in (("before", baseline, experiment.get("baseline_ref")),
                                    ("after", current, current_ref)):
            address = "/response/data/output" + check["pointer"]
            try:
                if receipt is None:
                    raise ValueError("No baseline execution")
                found = pointer(receipt, address)
                row[label] = {"present": True, **_sample(found, address),
                              "evidence": {"ref": ref, "pointer": address}}
            except ValueError as exc:
                row[label] = {"present": False, "reason": str(exc)}
        checks.append(row)
    return {"experiment": experiment, "executionRef": current_ref,
            "assessmentSource": "author model; not an independently verified verdict",
            "baselineAvailable": baseline is not None,
            "comparability": {
                "baselineSelection": experiment.get('baselineSelection'),
                "sameInput": baseline.get('input') == current.get('input') if baseline is not None else None,
                "samePage": baseline.get('pageId') == current.get('pageId') if baseline is not None else None,
                "startingStateEquivalent": None,
                "validationScopeEquivalent": None,
                "instruction": 'Baseline is a candidate, not proof of a controlled comparison. Judge goal, '
                    'input scale, starting state, coverage and correctness. Report improvement, no improvement '
                    'or insufficient evidence; do not infer total savings from execution time or call counts alone.'},
            "identity": {"before": {k: baseline.get(k) for k in ("contentHash", "pageId")} if baseline is not None else None,
                         "after": {k: current.get(k) for k in ("contentHash", "pageId")}},
            "sourceChanges": source_changes,
            "executionMetrics": {"before": execution_metrics(baseline), "after": execution_metrics(current)},
            "inputChanges": changes(baseline.get("input", _MISSING), current.get("input", _MISSING)) if baseline is not None else None,
            "outputChanges": changes(previous, output, "/response/data/output") if baseline is not None else None,
            "checks": checks,
            "interpretation": "Changes are facts, not a progress verdict. Equal page IDs do not prove equal "
                              "starting state. Reconcile input/state changes, contradictory observations, "
                              "unverified effects and test coverage before choosing the next experiment."}
