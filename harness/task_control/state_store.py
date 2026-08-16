"""
harness.task_control.state_store - Task state merge, atomic persistence and summary.
"""

from __future__ import annotations

import json
import copy
import hashlib
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from harness.storage.base import SNAPSHOT_KEY_TASK_STATE
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import storage_for_logger
from harness.utils import trim_large_strings

def _tc():
    import harness.task_control as tc

    return tc

def _empty_phase_state() -> JsonDict:
    return {
        "status": "pending",
        "attempts": [],
        "validated_artifacts": [],
        "last_failure": None,
    }

def _ensure_phase_state_defaults(phase_state: JsonDict) -> None:
    phase_state.setdefault("status", "pending")
    phase_state.setdefault("attempts", [])
    phase_state.setdefault("validated_artifacts", [])
    phase_state.setdefault("last_failure", None)

def _first_active_phase_id(plan: JsonDict, phases_state: JsonDict) -> Optional[str]:
    for phase in plan.get("phases", []):
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        status = (
            phases_state.get(phase_id, {}).get("status")
            if isinstance(phases_state.get(phase_id), dict)
            else None
        )
        if status not in _tc().TERMINAL_PHASE_STATUSES:
            return phase_id
    return _tc()._first_phase_id(plan)

_TASK_STATE_MISSING = object()

def _state_value_token(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )

def _merge_state_lists(base: List[Any], current: List[Any], proposed: List[Any]) -> List[Any]:
    """Merge concurrent list additions/removals while retaining stable order."""

    base_tokens = [_state_value_token(item) for item in base]
    current_tokens = [_state_value_token(item) for item in current]
    proposed_tokens = [_state_value_token(item) for item in proposed]
    base_counts = Counter(base_tokens)
    current_counts = Counter(current_tokens)
    proposed_counts = Counter(proposed_tokens)

    keep_counts = {
        token: min(count, current_counts[token], proposed_counts[token])
        for token, count in base_counts.items()
    }
    result: List[Any] = []
    emitted = Counter()
    for item, token in zip(base, base_tokens):
        if emitted[token] < keep_counts.get(token, 0):
            result.append(copy.deepcopy(item))
            emitted[token] += 1

    # Concurrent appenders frequently touch artifacts, attempts, resumes, and
    # supersessions.  Add the maximum occurrence count seen on either branch,
    # rather than dropping one branch or duplicating the same append twice.
    target_additions = {
        token: max(
            0,
            current_counts[token] - base_counts[token],
            proposed_counts[token] - base_counts[token],
        )
        for token in set(current_tokens) | set(proposed_tokens)
    }
    added = Counter()
    for values, tokens in ((current, current_tokens), (proposed, proposed_tokens)):
        skipped_base = Counter()
        for item, token in zip(values, tokens):
            if skipped_base[token] < base_counts[token]:
                skipped_base[token] += 1
                continue
            if added[token] < target_additions.get(token, 0):
                result.append(copy.deepcopy(item))
                added[token] += 1
    return result

def _three_way_merge_task_state(base: Any, current: Any, proposed: Any) -> Any:
    """Apply only local changes from ``base`` onto the latest disk value."""

    if proposed == base:
        return copy.deepcopy(current)
    if current == base:
        return copy.deepcopy(proposed)
    if isinstance(base, dict) and isinstance(current, dict) and isinstance(proposed, dict):
        merged: JsonDict = {}
        keys = set(base) | set(current) | set(proposed)
        for key in keys:
            base_value = base.get(key, _TASK_STATE_MISSING)
            current_value = current.get(key, _TASK_STATE_MISSING)
            proposed_value = proposed.get(key, _TASK_STATE_MISSING)
            if proposed_value is _TASK_STATE_MISSING:
                if base_value is _TASK_STATE_MISSING:
                    if current_value is not _TASK_STATE_MISSING:
                        merged[key] = copy.deepcopy(current_value)
                # Local deletion wins over a concurrent edit of the same key.
                continue
            if current_value is _TASK_STATE_MISSING:
                if base_value is _TASK_STATE_MISSING:
                    merged[key] = copy.deepcopy(proposed_value)
                elif proposed_value != base_value:
                    # Local edit wins over a concurrent deletion.
                    merged[key] = copy.deepcopy(proposed_value)
                continue
            if base_value is _TASK_STATE_MISSING:
                if current_value == proposed_value:
                    merged[key] = copy.deepcopy(current_value)
                elif isinstance(current_value, dict) and isinstance(proposed_value, dict):
                    merged[key] = _three_way_merge_task_state(
                        {}, current_value, proposed_value,
                    )
                elif isinstance(current_value, list) and isinstance(proposed_value, list):
                    merged[key] = _merge_state_lists([], current_value, proposed_value)
                else:
                    merged[key] = copy.deepcopy(proposed_value)
                continue
            merged[key] = _three_way_merge_task_state(
                base_value, current_value, proposed_value,
            )
        return merged
    if isinstance(base, list) and isinstance(current, list) and isinstance(proposed, list):
        return _merge_state_lists(base, current, proposed)
    # Both branches changed the same scalar/type. The caller's explicit local
    # mutation wins; unrelated nested changes were already merged above.
    return copy.deepcopy(proposed)

def _read_task_state_for_merge(path: Path) -> JsonDict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

def _atomic_replace_task_state(path: Path, state: JsonDict) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(state, temporary, ensure_ascii=False, indent=2, default=str)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass

def write_task_state(
    logger: RunLogger,
    state: JsonDict,
    *,
    replace: bool = False,
) -> str:
    """Atomically persist state, merging changes made from stale snapshots.

    ``load_task_state`` returns a dict subclass carrying its baseline. Writers
    from two concurrent BrowserAgent callbacks can therefore preserve changes
    to different phases/ledgers. Plan initialization and resume bootstrap use
    ``replace=True`` (or a snapshot marked for replacement) for intentional
    whole-state reconstruction.
    """

    path = _tc()._state_path(logger)
    proposed = copy.deepcopy(dict(state))
    proposed["updated_at"] = _tc().utc_now_iso()
    force_replace = bool(
        replace or getattr(state, "_task_state_replace", False)
    )
    base = getattr(state, "_task_state_base", None)
    # The lock still spans read, merge and commit. It only serialises threads
    # inside this process; the backend adds a compare-and-swap for anything
    # that crossed a connection, and the three-way merge remains what actually
    # preserves two callers editing different phases.
    storage, task_id = storage_for_logger(logger)
    with _tc()._TASK_STATE_WRITE_LOCK:
        persisted, revision = storage.save_snapshot(
            task_id=task_id,
            snapshot_key=SNAPSHOT_KEY_TASK_STATE,
            base=base if isinstance(base, dict) else None,
            proposed=proposed,
            updated_run_id=str(getattr(logger, "run_id", "") or ""),
            merge=_three_way_merge_task_state,
            replace=force_replace,
        )

    state.clear()
    state.update(copy.deepcopy(persisted))
    if isinstance(state, _tc()._TaskStateSnapshot):
        state._task_state_base = copy.deepcopy(persisted)
        state._task_state_replace = False
        state._task_state_revision = int(revision or 0)
    # Keep the task listing readable without opening the full state document.
    try:
        storage.update_task_snapshot(task_id, task_state_summary(persisted))
    except Exception:  # noqa: BLE001 - a summary must never fail a state write
        pass
    return str(path.resolve())

def task_state_summary(state: JsonDict) -> JsonDict:
    """Small, bounded digest of a task for listings and operator tooling."""

    phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    counts: Dict[str, int] = {}
    current_phase = ""
    for phase_id, phase_state in phases.items():
        status = str((phase_state or {}).get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
        if not current_phase and status not in _tc().TERMINAL_PHASE_STATUSES:
            current_phase = str(phase_id)
    last_failure = None
    for phase_state in phases.values():
        failure = (phase_state or {}).get("last_failure")
        if failure:
            last_failure = trim_large_strings(failure, max_chars=300)
    return {
        "goal": str(state.get("goal") or "")[:200],
        "currentPhase": current_phase or None,
        "phaseCounts": counts,
        "planVersion": state.get("plan_version"),
        "lastError": last_failure,
        "updatedAt": state.get("updated_at"),
    }

def contract_hash_for_phase(
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict],
    *,
    task: str = "",
    result_contract: str = "",
) -> str:
    phase = phase if isinstance(phase, dict) else {}
    worker_contract = worker_contract if isinstance(worker_contract, dict) else {}
    payload = {
        "phaseId": str(phase.get("id") or worker_contract.get("phase_id") or ""),
        "taskType": str(phase.get("task_type") or "web_scrape"),
        "stageHint": str(
            worker_contract.get("stage_hint")
            or phase.get("stage_hint")
            or ""
        ),
        "objective": str(
            worker_contract.get("objective")
            or phase.get("objective")
            or ""
        ),
        "workerTask": str(task or phase.get("worker_task") or ""),
        "resultContract": str(result_contract or ""),
        "workerContract": worker_contract,
        "expectedArtifact": (
            worker_contract.get("expected_artifact")
            if isinstance(worker_contract.get("expected_artifact"), dict)
            else phase.get("expected_artifact")
        ),
        "validators": (
            worker_contract.get("validators")
            if isinstance(worker_contract.get("validators"), list)
            else phase.get("validators")
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
