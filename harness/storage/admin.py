"""
harness.storage.admin - Operator-only task deletion.

Deliberately not part of the ``Storage`` interface and never exposed as an
agent tool: this destroys data, and nothing an LLM can reach should be able to
call it. Reach it from a CLI or an operator RPC.

Hard delete cannot be atomic, because external files live outside the database
and no transaction spans both. It is therefore staged and restartable: the task
is marked ``purging`` first, so a crash halfway leaves a task that is still
recognisably mid-deletion rather than one that looks runnable again.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import List, Optional

from harness.storage.base import StorageError
from harness.storage.sqlite_connection import write_transaction
from harness.utils import JsonDict


class PurgeRefused(StorageError):
    """A hard delete was blocked by a precondition."""


def _require_row(store, task_id: str) -> JsonDict:
    row = store.get_task(task_id, include_deleted=True)
    if row is None:
        raise PurgeRefused(f"task {task_id} is not registered in this database")
    return row


def soft_delete_task(store, task_id: str) -> bool:
    """Mark a task deleted. Reversible; children stay queryable to operators."""

    _require_row(store, task_id)
    return store.soft_delete_task(task_id)


def purge_task(
    store,
    task_id: str,
    *,
    expected_deleted: bool = True,
    delete_external_files: bool = True,
) -> JsonDict:
    """Permanently remove a task, its rows and the files it owns.

    ``expected_deleted`` defaults to True so a purge cannot be the first thing
    that happens to a live task: soft delete is the reviewable step, and this
    only carries it out.
    """

    connection = store.connection
    row = _require_row(store, task_id)
    if expected_deleted and not int(row.get("is_deleted") or 0):
        raise PurgeRefused(
            f"task {task_id} is not soft-deleted; soft delete it first or pass"
            " expected_deleted=False"
        )
    running = connection.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND status = 'running'",
        (task_id,),
    ).fetchone()
    if running and int(running[0]):
        raise PurgeRefused(f"task {task_id} still has a running run")
    lock_dir = store.task_dir(task_id) / ".run.lock"
    if lock_dir.is_dir():
        raise PurgeRefused(f"task {task_id} still holds a run lock at {lock_dir}")

    _set_purge_status(connection, task_id, "purging")
    removed, skipped, failed = (
        _delete_owned_files(store, task_id) if delete_external_files else ([], [], [])
    )
    task_directory = store.task_dir(task_id)
    directory_removed = False
    if delete_external_files and not failed:
        directory_removed, directory_failure = _remove_task_directory(task_directory)
        if directory_failure:
            failed.append(directory_failure)

    if failed:
        # Something we own is still on disk. Dropping the rows now would strand
        # it with nothing recording what it belonged to, so the task stays
        # marked failed and a later purge finishes the job.
        _set_purge_status(connection, task_id, "failed")
        raise PurgeRefused(
            f"task {task_id} still owns {len(failed)} undeletable path(s);"
            f" purge_status set to failed: {failed[:3]}"
        )

    # Every file and the directory are confirmed gone before the rows go. The
    # reverse order would leave orphaned bytes with no record of what they
    # belonged to, which is the one outcome a purge must never produce.
    with write_transaction(connection):
        connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    return {
        "taskId": task_id,
        "status": "purged",
        "filesRemoved": removed,
        # Deliberately left alone rather than failed: the agent chose a path
        # outside this worktree, so the file is not ours to delete.
        "filesSkipped": skipped,
        "taskDirectoryRemoved": directory_removed,
    }


def _remove_task_directory(task_directory: Path) -> tuple[bool, str]:
    """Delete the task directory. Returns ``(removed, failure_description)``.

    No ``ignore_errors``: a directory that survives deletion is the whole
    reason the caller must keep the database rows, so the failure has to be
    visible rather than folded into a "purged" result.
    """

    if not task_directory.is_dir():
        return False, ""
    try:
        shutil.rmtree(task_directory)
    except OSError as exc:
        return False, f"{task_directory}: {exc}"
    if task_directory.exists():
        return False, f"{task_directory}: still present after removal"
    return True, ""


def _set_purge_status(connection, task_id: str, status: str) -> None:
    with write_transaction(connection):
        connection.execute(
            "UPDATE tasks SET purge_status = ?, purge_requested_at = COALESCE("
            "  purge_requested_at, datetime('now'))"
            " WHERE task_id = ?",
            (status, task_id),
        )


def _delete_owned_files(store, task_id: str) -> tuple[List[str], List[str], List[str]]:
    """Delete only external files inside this task's directory.

    Returns ``(removed, skipped, failed)``. ``skipped`` is deliberate - a
    resource marked ``external_unmanaged`` points outside the worktree, so the
    agent chose that path and it is not ours to delete. ``failed`` is an error:
    a file we do own that could not be removed.
    """

    root = store.task_dir(task_id).resolve(strict=False)
    removed: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    rows = store.connection.execute(
        "SELECT external_path, metadata_json FROM task_resources"
        " WHERE task_id = ? AND external_path IS NOT NULL",
        (task_id,),
    ).fetchall()
    for row in rows:
        raw = str(row["external_path"] or "")
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("external_unmanaged"):
            skipped.append(raw)
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            skipped.append(str(candidate))
            continue
        try:
            candidate.unlink()
            removed.append(str(candidate))
        except FileNotFoundError:
            continue
        except OSError:
            failed.append(str(candidate))
    return removed, skipped, failed


def list_tasks(store, *, include_deleted: bool = False, limit: int = 50) -> List[JsonDict]:
    return store.list_tasks(include_deleted=include_deleted, limit=limit)
