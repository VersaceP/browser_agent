"""
harness.storage.dao - Parameterised SQL for each table. No ORM, no string
interpolation of values.

Table and column names are literals in this module; every caller-supplied
value travels as a ``?`` placeholder.  Nothing here knows about RunLogger,
phases, or workers - that mapping belongs to the store implementations.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.storage.base import RevisionConflictError, StorageError
from harness.storage.sqlite_connection import write_transaction
from harness.utils import JsonDict


DEFAULT_MAX_CAS_ATTEMPTS = 5

# Merge signature: (base, current, proposed) -> persisted
MergeFn = Callable[[JsonDict, JsonDict, JsonDict], JsonDict]
ConflictHook = Callable[[JsonDict], None]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[JsonDict]:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def insert_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    harness_version: str,
    schema_version: int,
    snapshot: Optional[JsonDict] = None,
    now: Optional[str] = None,
) -> JsonDict:
    """Register a task once. An existing row is returned untouched."""

    timestamp = now or utc_now_iso()
    with write_transaction(connection):
        connection.execute(
            "INSERT INTO tasks("
            " task_id, create_time, last_run_at, snapshot_json,"
            " created_harness_version, last_harness_version, created_schema_version)"
            " VALUES (?, ?, NULL, ?, ?, ?, ?)"
            " ON CONFLICT(task_id) DO NOTHING",
            (
                task_id,
                timestamp,
                _dump(snapshot or {}),
                harness_version,
                harness_version,
                int(schema_version),
            ),
        )
    row = get_task(connection, task_id=task_id, include_deleted=True)
    if row is None:  # pragma: no cover - only reachable on a concurrent purge
        raise StorageError(f"task {task_id} vanished immediately after insert")
    return row


def get_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    include_deleted: bool = False,
) -> Optional[JsonDict]:
    """Soft-deleted tasks are invisible unless explicitly requested."""

    sql = "SELECT * FROM tasks WHERE task_id = ?"
    if not include_deleted:
        sql += " AND is_deleted = 0"
    return _row_to_dict(connection.execute(sql, (task_id,)).fetchone())


def list_tasks(
    connection: sqlite3.Connection,
    *,
    include_deleted: bool = False,
    limit: int = 50,
) -> List[JsonDict]:
    sql = "SELECT * FROM tasks"
    if not include_deleted:
        sql += " WHERE is_deleted = 0"
    sql += " ORDER BY create_time DESC LIMIT ?"
    return [dict(row) for row in connection.execute(sql, (int(limit),)).fetchall()]


def update_task_snapshot(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    snapshot: JsonDict,
) -> bool:
    with write_transaction(connection):
        before = connection.total_changes
        connection.execute(
            "UPDATE tasks SET snapshot_json = ? WHERE task_id = ? AND is_deleted = 0",
            (_dump(snapshot), task_id),
        )
        changed = connection.total_changes - before
    return changed == 1


def touch_task_run(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    harness_version: str,
    now: Optional[str] = None,
) -> None:
    with write_transaction(connection):
        connection.execute(
            "UPDATE tasks SET last_run_at = ?, last_harness_version = ?"
            " WHERE task_id = ? AND is_deleted = 0",
            (now or utc_now_iso(), harness_version, task_id),
        )


def soft_delete_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    now: Optional[str] = None,
) -> bool:
    with write_transaction(connection):
        before = connection.total_changes
        connection.execute(
            "UPDATE tasks SET is_deleted = 1, deleted_at = ?"
            " WHERE task_id = ? AND is_deleted = 0",
            (now or utc_now_iso(), task_id),
        )
        changed = connection.total_changes - before
    return changed == 1


# ---------------------------------------------------------------------------
# task_runs
# ---------------------------------------------------------------------------

def start_run(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    harness_version: str,
    run_id: Optional[str] = None,
    process_id: Optional[int] = None,
    host_name: Optional[str] = None,
    git_sha: str = "",
    now: Optional[str] = None,
) -> JsonDict:
    """Open a run. run_number is allocated inside the write transaction so two
    processes cannot hand out the same number."""

    identifier = run_id or uuid.uuid4().hex
    timestamp = now or utc_now_iso()
    with write_transaction(connection):
        row = connection.execute(
            "SELECT COALESCE(MAX(run_number), 0) + 1 FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        run_number = int(row[0] or 1)
        connection.execute(
            "INSERT INTO task_runs("
            " run_id, task_id, run_number, started_at, finished_at, status,"
            " harness_version, process_id, host_name, error_json, git_sha)"
            " VALUES (?, ?, ?, ?, NULL, 'running', ?, ?, ?, NULL, ?)",
            (
                identifier,
                task_id,
                run_number,
                timestamp,
                harness_version,
                process_id,
                host_name,
                git_sha or None,
            ),
        )
    return {
        "run_id": identifier,
        "task_id": task_id,
        "run_number": run_number,
        "started_at": timestamp,
        "status": "running",
    }


def finish_run(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    status: str,
    error: Optional[JsonDict] = None,
    now: Optional[str] = None,
) -> bool:
    with write_transaction(connection):
        before = connection.total_changes
        connection.execute(
            "UPDATE task_runs SET status = ?, finished_at = ?, error_json = ?"
            " WHERE task_id = ? AND run_id = ?",
            (status, now or utc_now_iso(), _dump(error) if error else None, task_id, run_id),
        )
        changed = connection.total_changes - before
    return changed == 1


def get_run(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
) -> Optional[JsonDict]:
    return _row_to_dict(
        connection.execute(
            "SELECT * FROM task_runs WHERE task_id = ? AND run_id = ?",
            (task_id, run_id),
        ).fetchone()
    )


# ---------------------------------------------------------------------------
# task_snapshots
# ---------------------------------------------------------------------------

def load_snapshot(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    snapshot_key: str,
) -> Tuple[JsonDict, int]:
    """Return ``(value, revision)``.

    An absent row reads as ``({}, 0)``, matching the file implementation where
    "never written" and "written empty" are deliberately indistinguishable -
    every caller is written against that assumption.

    Unparseable stored JSON raises instead of degrading to ``{}``: on disk a
    torn write was plausible and forgiving was right, but in the database it
    means corruption, and silently returning empty state would let the next
    write erase a real task.
    """

    row = connection.execute(
        "SELECT value_json, revision FROM task_snapshots"
        " WHERE task_id = ? AND snapshot_key = ?",
        (task_id, snapshot_key),
    ).fetchone()
    if row is None:
        return {}, 0
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"snapshot {task_id}/{snapshot_key} holds unparseable JSON: {exc}"
        ) from exc
    return (value if isinstance(value, dict) else {}), int(row["revision"])


def save_snapshot(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    snapshot_key: str,
    proposed: JsonDict,
    updated_run_id: str,
    base: Optional[JsonDict] = None,
    merge: Optional[MergeFn] = None,
    replace: bool = False,
    max_attempts: int = DEFAULT_MAX_CAS_ATTEMPTS,
    on_conflict: Optional[ConflictHook] = None,
    now: Optional[str] = None,
) -> Tuple[JsonDict, int]:
    """Persist a snapshot under compare-and-swap, preserving concurrent edits.

    Layering, from widest to narrowest:

    * ``.run.lock`` keeps two harness processes off the same task;
    * the caller's in-process lock serialises threads within one process;
    * ``merge`` folds edits made from stale snapshots into different fields;
    * this CAS is the last gate, catching anything that crossed connections.

    On a lost CAS the merge is recomputed against freshly read ``current``
    while ``base`` and ``proposed`` keep their original values.  Replacing
    ``base`` with the newer ``current`` would make this caller's edits
    indistinguishable from the other writer's and silently drop them.

    Returns ``(persisted_value, new_revision)``.  Callers own writing that back
    into their snapshot object and resetting its baseline - skipping that lets
    the next write merge against a stale base.
    """

    if replace and merge is not None:
        merge = None  # a deliberate whole-state rebuild must not re-merge

    actual_revision = 0
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        current, expected_revision = load_snapshot(
            connection, task_id=task_id, snapshot_key=snapshot_key
        )
        if merge is None or base is None:
            persisted = copy.deepcopy(proposed)
        else:
            persisted = merge(base, current, proposed)

        value_json = _dump(persisted)
        timestamp = now or utc_now_iso()

        with write_transaction(connection):
            before = connection.total_changes
            if expected_revision == 0:
                # No row yet. This is the first write of a task, not a
                # conflict - an UPDATE-only CAS would report 0 rows here and
                # every new task would fail its whole retry budget.
                connection.execute(
                    "INSERT INTO task_snapshots("
                    " task_id, snapshot_key, value_json, revision, updated_at, updated_run_id)"
                    " VALUES (?, ?, ?, 1, ?, ?)"
                    " ON CONFLICT(task_id, snapshot_key) DO NOTHING",
                    (task_id, snapshot_key, value_json, timestamp, updated_run_id),
                )
                new_revision = 1
            else:
                connection.execute(
                    "UPDATE task_snapshots"
                    " SET value_json = ?, revision = revision + 1,"
                    "     updated_at = ?, updated_run_id = ?"
                    " WHERE task_id = ? AND snapshot_key = ? AND revision = ?",
                    (
                        value_json,
                        timestamp,
                        updated_run_id,
                        task_id,
                        snapshot_key,
                        expected_revision,
                    ),
                )
                new_revision = expected_revision + 1
            changed = connection.total_changes - before

        if changed == 1:
            return persisted, new_revision

        _, actual_revision = load_snapshot(
            connection, task_id=task_id, snapshot_key=snapshot_key
        )
        if on_conflict is not None:
            on_conflict({
                "taskId": task_id,
                "snapshotKey": snapshot_key,
                "expectedRevision": expected_revision,
                "actualRevision": actual_revision,
                "attempt": attempt,
                "replace": bool(replace),
            })
        if replace:
            # A whole-state rebuild racing another writer is a lifecycle
            # coordination failure, not a stale snapshot. Retrying would
            # overwrite whatever the other writer just committed.
            raise RevisionConflictError(
                f"replace write for {task_id}/{snapshot_key} lost its CAS",
                task_id=task_id,
                snapshot_key=snapshot_key,
                expected_revision=expected_revision,
                actual_revision=actual_revision,
                attempts=attempt,
            )

    raise RevisionConflictError(
        f"snapshot {task_id}/{snapshot_key} still conflicting after {max_attempts} attempts",
        task_id=task_id,
        snapshot_key=snapshot_key,
        expected_revision=actual_revision,
        actual_revision=actual_revision,
        attempts=int(max_attempts),
    )


# ---------------------------------------------------------------------------
# run_events
# ---------------------------------------------------------------------------

def insert_event(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    event_type: str,
    payload_json: Optional[str] = None,
    payload_resource_id: Optional[str] = None,
    payload_byte_size: int = 0,
    actor_type: Optional[str] = None,
    worker_id: Optional[str] = None,
    now: Optional[str] = None,
) -> int:
    """Insert one event. Exactly one of payload_json / payload_resource_id."""

    with write_transaction(connection):
        cursor = connection.execute(
            "INSERT INTO run_events("
            " task_id, run_id, event_time, event_type, actor_type, worker_id,"
            " payload_json, payload_resource_id, payload_byte_size)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                run_id,
                now or utc_now_iso(),
                event_type,
                actor_type,
                worker_id,
                payload_json,
                payload_resource_id,
                int(payload_byte_size),
            ),
        )
        event_id = int(cursor.lastrowid or 0)
    return event_id


def read_events(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    after_event_id: int = 0,
    limit: int = 200,
    event_type: Optional[str] = None,
) -> List[JsonDict]:
    """Keyset pagination on event_id; a large OFFSET would rescan every time."""

    params: List[Any] = [task_id, int(after_event_id)]
    sql = (
        "SELECT * FROM run_events WHERE task_id = ? AND event_id > ?"
    )
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    sql += " ORDER BY event_id LIMIT ?"
    params.append(int(limit))
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# worker_trace_events
# ---------------------------------------------------------------------------

def insert_trace_events(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    worker_id: str,
    entries: List[JsonDict],
    now: Optional[str] = None,
) -> int:
    """Append trace steps, continuing this worker's existing sequence."""

    if not entries:
        return 0
    timestamp = now or utc_now_iso()
    with write_transaction(connection):
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) FROM worker_trace_events"
            " WHERE task_id = ? AND run_id = ? AND worker_id = ?",
            (task_id, run_id, worker_id),
        ).fetchone()
        next_sequence = int(row[0] or 0) + 1
        connection.executemany(
            "INSERT INTO worker_trace_events("
            " task_id, run_id, worker_id, sequence_no, trace_type, trace_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    task_id,
                    run_id,
                    worker_id,
                    next_sequence + offset,
                    str(entry.get("type") or "") or None,
                    _dump(entry),
                    timestamp,
                )
                for offset, entry in enumerate(entries)
            ],
        )
    return len(entries)


def list_trace_events(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    run_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    limit: int = 1000,
) -> List[JsonDict]:
    params: List[Any] = [task_id]
    sql = "SELECT * FROM worker_trace_events WHERE task_id = ?"
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    if worker_id:
        sql += " AND worker_id = ?"
        params.append(worker_id)
    sql += " ORDER BY worker_id, sequence_no LIMIT ?"
    params.append(int(limit))
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# strategy_attempts
# ---------------------------------------------------------------------------

def insert_strategy_attempt(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    payload: JsonDict,
    now: Optional[str] = None,
) -> int:
    with write_transaction(connection):
        cursor = connection.execute(
            "INSERT INTO strategy_attempts("
            " task_id, run_id, phase_id, worker_id, strategy_ids_json, status,"
            " status_category, validated_status, failure_classification,"
            " row_count, artifact_count, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                run_id,
                payload.get("phaseId"),
                payload.get("workerId"),
                _dump(payload.get("strategy_ids") or []),
                payload.get("status"),
                payload.get("statusCategory"),
                payload.get("validatedStatus"),
                payload.get("failureClassification"),
                payload.get("rowCount"),
                int(payload.get("artifactCount") or 0),
                now or utc_now_iso(),
            ),
        )
        attempt_id = int(cursor.lastrowid or 0)
    return attempt_id


def list_strategy_attempts(
    connection: sqlite3.Connection,
    *,
    task_id: Optional[str] = None,
    limit: int = 200,
) -> List[JsonDict]:
    """Cross-task listing is the reason this table is not per-task."""

    params: List[Any] = []
    sql = "SELECT * FROM strategy_attempts"
    if task_id:
        sql += " WHERE task_id = ?"
        params.append(task_id)
    sql += " ORDER BY created_at DESC, attempt_id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(row) for row in connection.execute(sql, params).fetchall()]
