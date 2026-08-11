"""Mechanical, read-only task completion summary.

The receipt is derived from coordinator state and worker ledgers.  Model prose
cannot set or override any field in it.
"""

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from harness.utils import JsonDict


def _finished_worker_results(spawner: Any) -> Iterable[JsonDict]:
    handles = getattr(spawner, "_handles", None)
    for handle in (handles.values() if isinstance(handles, dict) else []):
        task = getattr(handle, "async_task", None)
        if task is None or not task.done() or task.cancelled():
            continue
        try:
            result = task.result()
        except Exception:
            continue
        if isinstance(result, dict):
            yield result


def _resolved(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return text


def _apply_supersessions(state: JsonDict, paths: List[str]) -> List[str]:
    """Swap absorbed sources for the artifact that consolidated them.

    A Lead `reference_merge` that copies every row of its sources verbatim is
    a new generation of the same data, not an addition to it. Until this ran,
    the merged artifact was in no ledger at all: the completion receipt could
    not count it and the numeric gate resolved claims about it against the
    sources instead, so a merge that lost rows was checked against the very
    data it had just dropped.

    Only artifacts recorded as fully absorbed are retired, and only from this
    view — `state["artifacts"]` keeps the whole history for the consumers that
    need it (batch_source materialization, replan checkpoints, collect_items).
    """
    entries = state.get("artifact_supersessions")
    if not isinstance(entries, list) or not entries:
        return paths
    absorbed: set = set()
    deliverables: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        deliverable = str(entry.get("deliverable") or "").strip()
        retired = [
            _resolved(item) for item in (entry.get("absorbed") or [])
            if str(item or "").strip()
        ]
        if not deliverable or not retired:
            continue
        absorbed.update(retired)
        deliverables.append(deliverable)
    if not absorbed:
        return paths
    # A deliverable absorbed by a later merge is itself retired, so filter the
    # deliverables through the same set rather than assuming merge order.
    kept = [path for path in paths if _resolved(path) not in absorbed]
    for deliverable in deliverables:
        if _resolved(deliverable) in absorbed:
            continue
        if all(_resolved(deliverable) != _resolved(path) for path in kept):
            kept.append(deliverable)
    return kept


def _validated_artifacts(state: JsonDict) -> List[str]:
    active_ledger = state.get("artifacts")
    if isinstance(active_ledger, list) and active_ledger:
        # task_state.artifacts is the coordinator's active validated generation
        # ledger. Prefer it over historical per-phase paths so replacements are
        # not double-counted across replans/remediation.
        return _apply_supersessions(state, list(dict.fromkeys(
            str(value) for value in active_ledger if value
        )))
    paths: List[str] = []
    phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    for phase in phases.values():
        if not isinstance(phase, dict) or phase.get("status") != "validated_done":
            continue
        values = phase.get("validated_artifacts")
        if isinstance(values, list):
            paths.extend(str(value) for value in values if value)
    return _apply_supersessions(state, list(dict.fromkeys(paths)))


def _artifact_row_count(paths: List[str]) -> int:
    count = 0
    for raw_path in paths:
        try:
            payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            count += len(rows)
    return count


def build_completion_receipt(*, state: Any, spawner: Any) -> JsonDict:
    task_state = state if isinstance(state, dict) else {}
    phases = task_state.get("phases") if isinstance(task_state.get("phases"), dict) else {}
    validated_phase_count = sum(
        1 for value in phases.values()
        if isinstance(value, dict) and value.get("status") == "validated_done"
    )
    artifact_paths = _validated_artifacts(task_state)

    download_operations: Dict[str, JsonDict] = {}
    observed_challenge = False
    observed_challenge_count = 0
    vl_attempts = 0
    vl_solved = 0
    hitl_requests = 0
    hitl_resumes = 0
    unresolved_challenges = 0
    for result in _finished_worker_results(spawner):
        for receipt in result.get("downloadOperationReceipts") or []:
            if not isinstance(receipt, dict):
                continue
            key = json.dumps([
                str(receipt.get("url") or ""),
                str(receipt.get("savePath") or ""),
            ], ensure_ascii=False)
            download_operations[key] = dict(receipt)
        challenge = result.get("challengeReceipt")
        if isinstance(challenge, dict):
            observed_challenge = observed_challenge or bool(challenge.get("observed"))
            observed_challenge_count += int(challenge.get("observedCount") or 0)
            vl_attempts += int(challenge.get("vlSolveAttempts") or 0)
            vl_solved += int(challenge.get("vlSolvedCount") or 0)
            hitl_requests += int(challenge.get("hitlRequests") or 0)
            hitl_resumes += int(challenge.get("hitlResumes") or 0)
            unresolved_challenges += int(bool(challenge.get("unresolved")))

    completed = sum(
        1 for receipt in download_operations.values()
        if receipt.get("state") == "completed"
    )
    active = sum(
        1 for receipt in download_operations.values()
        if receipt.get("state") in {"downloading", "paused"}
    )
    ambiguous = sum(
        1 for receipt in download_operations.values()
        if receipt.get("state") in {
            "ambiguous", "unknown", "timeout_unverified", "",
        }
    )
    challenge_status = (
        "observed_unresolved" if unresolved_challenges > 0
        else "resolved_by_vl" if vl_solved > 0
        else "resolved_by_hitl" if hitl_resumes > 0
        else "observed_unresolved" if observed_challenge
        else "not_observed"
    )
    return {
        "artifact": {
            "validatedPhases": validated_phase_count,
            "validatedRows": _artifact_row_count(artifact_paths),
            "validatedArtifacts": len(artifact_paths),
        },
        "downloads": {
            "completed": completed,
            "active": active,
            "ambiguous": ambiguous,
        },
        "challenge": {
            "status": challenge_status,
            "observedCount": observed_challenge_count,
            "vlSolveAttempts": vl_attempts,
            "vlSolvedCount": vl_solved,
            "hitlRequests": hitl_requests,
            "hitlResumes": hitl_resumes,
        },
        "source": "harness_mechanical_state",
        "modelOverrideAllowed": False,
    }
