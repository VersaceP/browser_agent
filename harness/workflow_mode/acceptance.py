"""Objective acceptance checks; business sufficiency stays an Agent judgment.

1. The Agent records acceptance points from the original goal, materials and
   effective input before judging results (set_acceptance_points).
2. The package returns execution facts (Workflow.execute output).
3. This module checks structure, references, identity and objective
   consistency. It never decides that a business result is good enough.
4. The Agent's final judgment must address every recorded point.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from harness.workflow_mode.evidence import evidence_reference, pointer

POINT_STATUSES = {"verified", "not_met", "unverified", "accepted_omission", "not_applicable"}
CONCLUSIONS = {"complete", "partial", "needs_user", "blocked", "failed", "draft_saved"}
AUTHOR_CONCLUSIONS = {"draft_saved", "needs_user", "blocked", "partial"}
_COMPLETE_STATUSES = {"verified", "accepted_omission", "not_applicable"}


def execution_facts(receipt: dict[str, Any], *, content_hash: str, page_id: str) -> dict[str, Any]:
    """Identity and consistency facts for one saved Workflow.execute receipt."""
    scope = receipt.get("scope") or {}
    response = receipt.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    data = data if isinstance(data, dict) else {}
    output = data.get("output")
    issues: list[str] = []
    consistency: list[str] = []
    if receipt.get("contentHash") != content_hash:
        issues.append("execution used a different package version than this task pinned")
    if receipt.get("pageId") != page_id:
        issues.append("execution targeted a page other than the current binding")
    if not scope.get("sourceHash") or not scope.get("executionPackageHash"):
        issues.append("execution scope did not record source and execution package hashes")
    elif scope.get("businessEntry") is None:
        issues.append("execution scope did not record the business entry")
    status = receipt.get("status")
    outcome = output.get("outcome") if isinstance(output, dict) else None
    facts = receipt.get("facts") or {}
    calls = (facts.get("callFacts") or {})
    failed = calls.get("failedCalls")
    groups = calls.get("failureGroups")
    groups = groups if isinstance(groups, list) else []
    visible_groups, group_chars = [], 0
    for group in groups:
        size = len(json.dumps(group, ensure_ascii=False))
        if group_chars + size > 6000:
            break
        visible_groups.append(group)
        group_chars += size
    if status in {"failed", "cancelled"} and outcome == "complete":
        consistency.append(f"platform status {status} contradicts reported outcome=complete")
    return {"status": status, "workflowId": receipt.get("workflowId"), "outcome": outcome,
            "contentHash": receipt.get("contentHash"), "sourceHash": scope.get("sourceHash"),
            "executionPackageHash": scope.get("executionPackageHash"), "pageId": receipt.get("pageId"),
            "failedCalls": failed, "failureGroups": visible_groups,
            "failureGroupCount": len(groups), "failureGroupsTruncated": len(visible_groups) < len(groups),
            "recordedCalls": calls.get("recordedCalls"),
            "callRecordsComplete": calls.get("complete"),
            "callMetadataComplete": calls.get("metadataComplete", calls.get("complete")),
            "omittedValueCount": calls.get("omittedValueCount"),
            "callValuesComplete": calls.get("valuesComplete"),
            "identityIssues": issues, "consistencyIssues": consistency,
            "interpretation": "Identity issues are objective; failed calls may have been recovered and "
                              "an incomplete outcome may be a business question. Judge against the goal."}


def output_summary(receipt: dict[str, Any]) -> dict[str, Any] | None:
    """The package's own fixed-shape summary (output.summary), when it declares one."""
    response = receipt.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    output = data.get("output") if isinstance(data, dict) else None
    summary = output.get("summary") if isinstance(output, dict) else None
    return summary if isinstance(summary, dict) else None


def open_skips(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requested items the package reported as not performed, still current.

    For each stage the latest execution that actually ran it decides; a later
    run of other stages neither clears nor repeats an earlier stage's skips.
    The package states the skip and its reason; whether it is acceptable is the
    user's decision, not the Agent's written reason.
    """
    latest: dict[str, tuple[int, list[dict[str, Any]]]] = {}
    for index, execution in enumerate(executions):
        summary = execution.get("summary") or {}
        ran = {str(s.get("name")) for s in summary.get("stages") or [] if isinstance(s, dict)}
        skipped = [s for s in summary.get("skipped") or [] if isinstance(s, dict)]
        for stage in ran:
            latest[stage] = (index, [{**s, "receiptRef": execution.get("receiptRef")}
                                     for s in skipped if str(s.get("stage")) == stage])
    return [item for _, items in sorted(latest.values(), key=lambda x: x[0]) for item in items]


def validate_points(points: Any) -> list[dict[str, Any]]:
    if not isinstance(points, list) or not points:
        raise ValueError("Give at least one acceptance point")
    seen, clean = set(), []
    for item in points:
        if not isinstance(item, dict):
            raise ValueError("Each acceptance point must be an object")
        key = str(item.get("id") or "").strip()
        requirement = str(item.get("requirement") or "").strip()
        if not key or not requirement:
            raise ValueError("Each acceptance point needs id and requirement")
        if key in seen:
            raise ValueError(f"Duplicate acceptance point id {key}")
        seen.add(key)
        clean.append({"id": key, "requirement": requirement,
                      "source": str(item.get("source") or "").strip() or "original goal"})
    return clean


# Inputs typed by the user. "budget" (an extension) and "terminal_control" (/workflow-stop) are not.
USER_SOURCES = {"original_goal", "ask_user", "hitl", "resume_cli", "terminal_steering"}


def validate_judgment(judgment: dict[str, Any], *, points: list[dict[str, Any]], known_refs: set[str],
                      execution_refs: set[str], operator_inputs: list[dict[str, Any]],
                      skips: list[dict[str, Any]] | None = None,
                      read_evidence: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Return {"ok": bool, "problems": [...]} for the Agent's final judgment."""
    problems: list[str] = []
    conclusion = judgment.get("conclusion")
    if conclusion not in CONCLUSIONS:
        problems.append(f"conclusion must be one of {sorted(CONCLUSIONS)}")
    results = judgment.get("points")
    if conclusion != "draft_saved" and not points:
        problems.append("Record acceptance points with set_acceptance_points before finishing")
    by_id = {}
    for item in results if isinstance(results, list) else []:
        if isinstance(item, dict) and item.get("id"):
            key = str(item["id"])
            if key in by_id:
                problems.append(f"duplicate acceptance result {key}")
            by_id[key] = item
    input_ids = {str(r.get("inputId")) for r in operator_inputs
                 if r.get("inputId") and r.get("source") in USER_SOURCES}
    # Read each owned receipt once, including root references. Ownership alone
    # cannot establish that the evidence still exists in storage.
    receipts = {}
    for point in points:
        item = by_id.get(point["id"])
        if item is None:
            problems.append(f"acceptance point {point['id']} has no result")
            continue
        status = item.get("status")
        refs = item.get("evidenceRefs") or []
        if not isinstance(refs, list):
            problems.append(f"{point['id']}: evidenceRefs must be an array")
            refs = []
        if status not in POINT_STATUSES:
            problems.append(f"{point['id']}: status must be one of {sorted(POINT_STATUSES)}")
        for reference in refs:
            try:
                ref, selected = evidence_reference(reference, known_refs=known_refs)
                if ref not in known_refs:
                    raise ValueError("evidence references are not this task's receipts")
                if read_evidence is None:
                    raise ValueError("cannot verify evidence without its saved receipt")
                if ref not in receipts:
                    receipts[ref] = read_evidence(ref)
                pointer(receipts[ref], selected)
            except (ValueError, KeyError, OSError, IndexError, TypeError) as exc:
                problems.append(f"{point['id']}: invalid evidence {reference!r}: {exc}")
        if status == "verified" and not refs:
            problems.append(f"{point['id']}: verified requires evidence references")
        if status == "accepted_omission" and not (input_ids & {str(x) for x in item.get("userInputIds") or []}):
            problems.append(f"{point['id']}: an accepted omission must cite the user's input "
                            "(userInputIds); a written reason is not the user's permission")
    extra = sorted(set(by_id) - {p["id"] for p in points})
    if extra:
        problems.append(f"results for unrecorded points {extra}; add them with set_acceptance_points first")
    if conclusion == "complete":
        open_points = [p["id"] for p in points if (by_id.get(p["id"]) or {}).get("status") not in _COMPLETE_STATUSES]
        if open_points:
            problems.append(f"complete contradicts open points {open_points}")
        if not execution_refs:
            problems.append("complete requires Workflow or Browser execution evidence")
        # Package skip labels cannot decide whether requested work remains.
        # The Agent reconciles them with the goal and later Browser/Workflow
        # facts, then reports verified, not_applicable or accepted_omission per
        # acceptance point. Only accepted_omission needs actual user consent.
    if conclusion == "needs_user" and not str(judgment.get("question") or "").strip():
        problems.append("needs_user requires the question for the user")
    if conclusion in {"blocked", "failed", "partial"} and not str(judgment.get("summary") or "").strip():
        problems.append(f"{conclusion} requires a summary of facts and remaining work")
    return {"ok": not problems, "problems": problems}


def review_changes(previous: list[dict], current: list[dict]) -> dict:
    """Acceptance edits are review facts, never a verdict about goal coverage."""
    before = {item['id']: item for item in previous}
    after = {item['id']: item for item in current}
    return {'added': sorted(after.keys() - before.keys()),
            'removed': [before[key] for key in sorted(before.keys() - after.keys())],
            'changed': [{'id': key, 'before': before[key], 'after': after[key]}
                        for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]}


def review_facts(state: dict, inputs: list[dict], judgment: dict | None = None) -> dict:
    """Reuse the existing criteria; expose scope changes and consent provenance."""
    by_id = {item.get('inputId'): item for item in inputs}
    decisions = judgment or state.get('judgment') or {}
    omissions = []
    for point in decisions.get('points', []):
        if point.get('status') == 'accepted_omission':
            omissions.append({'id': point['id'], 'note': point.get('note'),
                              'userInputs': [by_id[key] for key in point.get('userInputIds', []) if key in by_id]})
    revision = state.get('acceptanceRevision') or {}
    edits = revision.get('changes') or {}
    return {'originalGoalLocation': {'path': 'task_manifest.json', 'pointer': '/original_user_task'},
            'revision': revision.get('number'), 'revisionReason': revision.get('reason'),
            'judgmentRevision': revision.get('number') if judgment is not None else state.get('judgmentRevision'),
            'criteriaChanges': {key: {'items': edits.get(key, [])[:8], 'total': len(edits.get(key, [])),
                                      'hasMore': len(edits.get(key, [])) > 8}
                                for key in ('added', 'removed', 'changed')},
            'fullRevisionLocation': {'path': 'workflow/session.json', 'pointer': '/acceptanceRevision'},
            'omissionClaims': omissions,
            'remainingWork': decisions.get('remaining_work', []),
            'templateGaps': decisions.get('template_gaps', []),
            'interpretation': 'Review original goal and later user inputs against existing criteria, scope edits and results. '
                'A reviewed input ID does not prove every requirement was listed; a cited user input does not '
                'prove consent to omit. Interpret its actual words. Remaining work and report issues are facts, '
                'not automatic business verdicts. Saving a local draft does not establish remote delivery.'}
