"""
harness.storage.file_store - The existing on-disk layout behind the Storage
interface.

This backend changes no file format.  It exists so ``dual`` mode has something
to compare against and so ``file`` mode keeps working untouched while the
database path is proven.  Where the file layout simply has no equivalent of a
database concept - runs, revisions - the gap is filled in memory and marked as
such rather than inventing a new on-disk file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from harness.storage.base import (
    DEFAULT_RESOURCE_GLOB,
    glob_matches,
    EXTERNAL_RESOURCE_TYPES,
    SNAPSHOT_KEY_CURRENT_PLAN,
    SNAPSHOT_KEY_TASK_STATE,
    ResourceAccessError,
    Storage,
    StorageError,
)
from harness.utils import JsonDict


RUN_EVENTS_FILE = "run.jsonl"
TASK_STATE_FILE = "task_state.json"
TASK_PLAN_FILE = "task_plan.json"
TRACES_DIR = "traces"
SNAPSHOTS_DIR = "snapshots"
PLAN_HISTORY_DIR = "task_plan_history"
PLAN_REVIEW_DIR = "task_plan_reviews"
STRATEGY_ATTEMPTS_FILE = "strategy_attempts.jsonl"

SNAPSHOT_FILES = {
    SNAPSHOT_KEY_TASK_STATE: TASK_STATE_FILE,
    SNAPSHOT_KEY_CURRENT_PLAN: TASK_PLAN_FILE,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def atomic_write_json(path: Path, value: Any) -> None:
    """Write via a temp file in the same directory, then rename.

    Mirrors task_control._atomic_replace_task_state byte for byte: indent=2,
    default=str, a trailing newline, fsync before the rename. dual mode
    compares these files against the database, so drifting here would show up
    as a false mismatch.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
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
            json.dump(value, temporary, ensure_ascii=False, indent=2, default=str)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def _stamp_plan_version(task_state: JsonDict, stored: JsonDict) -> JsonDict:
    """Write the allocated version number everywhere the state refers to it.

    The number is assigned when the generation is committed, so the state is
    built with a placeholder. Every field that cites it has to be patched
    together or the audit trail records a version that never existed.
    """

    version = stored.get("planVersion")
    if version is None:
        return task_state
    version = int(version)
    task_state = dict(task_state)
    task_state["plan_version"] = version
    task_state["plan_hash"] = str(stored.get("planHash") or task_state.get("plan_hash") or "")

    def _stamp_last(container: Any, key: str) -> None:
        items = container.get(key) if isinstance(container, dict) else None
        if isinstance(items, list) and items and isinstance(items[-1], dict):
            items[-1] = {**items[-1], "planVersion": version}

    _stamp_last(task_state, "plan_history")
    _stamp_last(task_state, "replans")
    resumes = task_state.get("resumes")
    if isinstance(resumes, list) and resumes and isinstance(resumes[-1], dict):
        _stamp_last(resumes[-1], "extensionDecisions")
    return task_state


def read_json_object(path: Path) -> JsonDict:
    """Forgiving read: a missing or torn file reads as ``{}``.

    Deliberately different from the SQLite backend, which raises. A partially
    written file was always plausible on disk and every existing caller is
    built on that assumption; corruption inside a transactional database is
    not, and hiding it there would let the next write erase a live task.
    """

    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class FileStore(Storage):
    def __init__(self, worktree_dir: str = "worktree") -> None:
        self.worktree_dir = Path(worktree_dir).expanduser()
        # Files carry no run ledger and no snapshot revisions. Both are kept
        # in memory so the interface stays uniform; neither is load-bearing
        # here because three-way merge against the file is the real guard.
        self._runs: Dict[str, List[JsonDict]] = {}
        self._revisions: Dict[Tuple[str, str], int] = {}

    # -- paths -------------------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        return self.worktree_dir / task_id

    def _snapshot_path(self, task_id: str, snapshot_key: str) -> Path:
        filename = SNAPSHOT_FILES.get(snapshot_key)
        if filename:
            return self.task_dir(task_id) / filename
        return self.task_dir(task_id) / SNAPSHOTS_DIR / f"{snapshot_key}.json"

    def _resolve_inside_task(self, task_id: str, relative: str) -> Path:
        """Reject any path that escapes the task worktree.

        The equivalent of utils.resolve_task_file. Losing this check is how a
        worker reads another task's data.
        """

        root = self.task_dir(task_id).resolve(strict=False)
        candidate = Path(relative).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ResourceAccessError(
                f"path escapes the current task worktree: {relative}"
            ) from exc
        return resolved

    # -- task lifecycle ----------------------------------------------------
    def create_task(
        self,
        *,
        task_id: str,
        harness_version: str,
        snapshot: Optional[JsonDict] = None,
    ) -> JsonDict:
        directory = self.task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "artifacts").mkdir(parents=True, exist_ok=True)
        return {
            "task_id": task_id,
            "create_time": _now_iso(),
            "created_harness_version": harness_version,
            "last_harness_version": harness_version,
            "snapshot_json": json.dumps(snapshot or {}, ensure_ascii=False),
            "is_deleted": 0,
        }

    def get_task(self, task_id: str, *, include_deleted: bool = False) -> Optional[JsonDict]:
        directory = self.task_dir(task_id)
        if not directory.is_dir():
            return None
        return {"task_id": task_id, "is_deleted": 0, "task_dir": str(directory.resolve())}

    def update_task_snapshot(self, task_id: str, snapshot: JsonDict) -> None:
        # No task index exists on disk; the per-task state file is the summary.
        return None

    def soft_delete_task(self, task_id: str) -> bool:
        # Deliberately unsupported: removing a directory is not reversible the
        # way a flag is, and operators expect soft delete to be recoverable.
        raise StorageError("file backend does not support soft delete; use the db backend")

    # -- run lifecycle -----------------------------------------------------
    def start_run(
        self,
        *,
        task_id: str,
        harness_version: str,
        run_id: Optional[str] = None,
    ) -> JsonDict:
        identifier = run_id or uuid.uuid4().hex
        runs = self._runs.setdefault(task_id, [])
        record = {
            "run_id": identifier,
            "task_id": task_id,
            "run_number": len(runs) + 1,
            "started_at": _now_iso(),
            "status": "running",
            "harness_version": harness_version,
        }
        runs.append(record)
        return dict(record)

    def finish_run(
        self,
        *,
        task_id: str,
        run_id: str,
        status: str,
        error: Optional[JsonDict] = None,
    ) -> None:
        for record in self._runs.get(task_id, []):
            if record["run_id"] == run_id:
                record["status"] = status
                record["finished_at"] = _now_iso()
                if error:
                    record["error"] = error

    # -- events ------------------------------------------------------------
    def append_event(
        self,
        *,
        task_id: str,
        run_id: str,
        event_type: str,
        payload: JsonDict,
        actor_type: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> None:
        """Append in RunLogger's exact wire format.

        Key order and the conditional runId match utils.RunLogger.write so a
        dual-mode diff of the two backends compares content, not formatting.
        """

        event: JsonDict = {
            "ts": _now_iso(),
            "taskId": task_id,
            "type": event_type,
            "payload": payload,
        }
        if run_id:
            event["runId"] = run_id
        path = self.task_dir(task_id) / RUN_EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_dump_line(event) + "\n")

    def read_events(
        self,
        *,
        task_id: str,
        after_event_id: int = 0,
        limit: int = 200,
        event_type: Optional[str] = None,
    ) -> List[JsonDict]:
        """Line number stands in for event_id, so keyset paging works here too."""

        path = self.task_dir(task_id) / RUN_EVENTS_FILE
        if not path.is_file():
            return []
        results: List[JsonDict] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle, start=1):
                if index <= after_event_id:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and event.get("type") != event_type:
                    continue
                payload = event.get("payload")
                results.append({
                    "event_id": index,
                    "task_id": event.get("taskId") or task_id,
                    "run_id": event.get("runId") or "",
                    "event_time": event.get("ts"),
                    "event_type": event.get("type"),
                    # Surfaced from the payload so both backends report the
                    # same row shape. The database promotes workerId to its own
                    # column; leaving it buried here made a dual comparison of
                    # identical writes look like content drift.
                    "worker_id": (
                        str(payload.get("workerId") or "") or None
                        if isinstance(payload, dict)
                        else None
                    ),
                    "actor_type": (
                        payload.get("actorType") if isinstance(payload, dict) else None
                    ),
                    "payload_json": _dump_line(payload),
                    "payload_resource_id": None,
                })
                if len(results) >= limit:
                    break
        return results

    # -- snapshots ---------------------------------------------------------
    def load_snapshot(self, *, task_id: str, snapshot_key: str) -> Tuple[JsonDict, int]:
        path = self._snapshot_path(task_id, snapshot_key)
        value = read_json_object(path)
        revision = self._revisions.get((task_id, snapshot_key), 1 if path.exists() else 0)
        return value, revision

    def save_snapshot(
        self,
        *,
        task_id: str,
        snapshot_key: str,
        base: Optional[JsonDict],
        proposed: JsonDict,
        updated_run_id: str,
        merge: Optional[Callable[[JsonDict, JsonDict, JsonDict], JsonDict]] = None,
        replace: bool = False,
    ) -> Tuple[JsonDict, int]:
        """Read, merge and atomically replace - the current write_task_state flow.

        There is no compare-and-swap here because there is nothing on disk to
        swap against; the caller's in-process lock plus the three-way merge are
        the whole guarantee, exactly as today.
        """

        path = self._snapshot_path(task_id, snapshot_key)
        if replace or merge is None or base is None:
            persisted = json.loads(json.dumps(proposed, ensure_ascii=False, default=str))
        else:
            persisted = merge(base, read_json_object(path), proposed)
        atomic_write_json(path, persisted)
        revision = self._revisions.get((task_id, snapshot_key), 0) + 1
        self._revisions[(task_id, snapshot_key)] = revision
        return persisted, revision

    # -- plan history ------------------------------------------------------
    def _next_sequence(self, directory: Path, prefix: str) -> int:
        maximum = 0
        if directory.is_dir():
            for path in directory.glob(f"{prefix}.*.json"):
                try:
                    maximum = max(maximum, int(path.stem.rsplit(".", 1)[-1]))
                except (TypeError, ValueError):
                    continue
        return maximum + 1

    def save_plan_version(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        directory = self.task_dir(task_id) / PLAN_HISTORY_DIR
        version = self._next_sequence(directory, "plan")
        stored = dict(record)
        stored["planVersion"] = version
        stored["previousVersion"] = version - 1 if version > 1 else None
        path = directory / f"plan.{version:04d}.json"
        atomic_write_json(path, stored)
        stored["path"] = str(path.resolve())
        return stored

    def load_plan_version(self, *, task_id: str, version: int) -> Optional[JsonDict]:
        path = self.task_dir(task_id) / PLAN_HISTORY_DIR / f"plan.{int(version):04d}.json"
        if not path.is_file():
            return None
        return read_json_object(path) or None

    def save_plan_review(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        directory = self.task_dir(task_id) / PLAN_REVIEW_DIR
        sequence = self._next_sequence(directory, "review")
        stored = dict(record)
        stored["reviewSequence"] = sequence
        path = directory / f"review.{sequence:04d}.json"
        atomic_write_json(path, stored)
        stored["path"] = str(path.resolve())
        return stored

    def commit_accepted_plan(
        self,
        *,
        task_id: str,
        run_id: str,
        plan_record: JsonDict,
        current_plan: JsonDict,
        task_state: JsonDict,
        summarize: Optional[Callable[[JsonDict], JsonDict]] = None,
    ) -> Tuple[JsonDict, JsonDict]:
        """Three separate files, in the safest order.

        Nothing here can be atomic across three paths, which is exactly why
        reconcile_torn_plan_alias exists for this backend. History is written
        first so a crash leaves a recoverable record rather than an alias with
        no provenance.

        ``summarize`` is accepted and ignored: there is no task index on disk,
        so the state file already is the summary.
        """

        stored = self.save_plan_version(
            task_id=task_id, run_id=run_id, record=plan_record
        )
        task_state = _stamp_plan_version(task_state, stored)
        atomic_write_json(self._snapshot_path(task_id, SNAPSHOT_KEY_CURRENT_PLAN), current_plan)
        persisted, _revision = self.save_snapshot(
            task_id=task_id, snapshot_key=SNAPSHOT_KEY_TASK_STATE,
            base=None, proposed=task_state, updated_run_id=run_id, replace=True,
        )
        return stored, persisted

    # -- resources ---------------------------------------------------------
    def save_resource(
        self,
        *,
        task_id: str,
        run_id: str,
        resource_type: str,
        logical_path: str,
        content: Any = None,
        media_type: str = "application/json",
        external_path: Optional[str] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        if external_path is not None:
            # The bytes were written by something else (a Download.start
            # receipt); only the reference is ours to record.
            resolved = Path(external_path)
            size = resolved.stat().st_size if resolved.is_file() else None
            return {
                "task_id": task_id,
                "run_id": run_id,
                "resource_type": resource_type,
                "logical_path": logical_path,
                "external_path": str(external_path),
                "byte_size": size,
                "saved_path": str(external_path),
            }

        path = self._resolve_inside_task(task_id, logical_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(content, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        return {
            "task_id": task_id,
            "run_id": run_id,
            "resource_type": resource_type,
            "logical_path": logical_path,
            "media_type": media_type,
            "byte_size": path.stat().st_size,
            "metadata": metadata or {},
            "saved_path": str(path.resolve()),
        }

    def read_resource(self, *, current_task_id: str, resource_uri: str) -> Optional[JsonDict]:
        path = self._resolve_inside_task(current_task_id, resource_uri)
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = None
        return {
            "task_id": current_task_id,
            "logical_path": str(path.relative_to(self.task_dir(current_task_id).resolve(strict=False))),
            "byte_size": path.stat().st_size,
            "content_text": content,
            "saved_path": str(path.resolve()),
        }

    def search_resources(
        self,
        *,
        task_id: str,
        path_glob: str = DEFAULT_RESOURCE_GLOB,
        pattern: Optional[str] = None,
        max_results: int = 20,
    ) -> List[JsonDict]:
        root = self.task_dir(task_id).resolve(strict=False)
        if not root.is_dir():
            return []
        # MULTILINE to match the database backend: the same pattern against the
        # same resource has to find the same thing, or a dual-to-db switch
        # silently changes what a search returns.
        regex = re.compile(pattern, re.MULTILINE) if pattern else None
        results: List[JsonDict] = []
        # The one glob implementation, shared with the database backend and
        # with the model-facing view. Path.glob("*") returns direct children
        # only, so the same call used to return different resources depending
        # on where they happened to be stored.
        matcher = path_glob or DEFAULT_RESOURCE_GLOB
        for candidate in sorted(root.rglob("*")):
            if not candidate.is_file():
                continue
            if not glob_matches(matcher, str(candidate.relative_to(root))):
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if regex is not None and not regex.search(text):
                continue
            results.append({
                "task_id": task_id,
                "logical_path": str(candidate.relative_to(root)),
                "byte_size": candidate.stat().st_size,
                "saved_path": str(candidate),
            })
            if len(results) >= max_results:
                break
        return results

    # -- worker traces -----------------------------------------------------
    def append_worker_trace(
        self,
        *,
        task_id: str,
        run_id: str,
        worker_id: str,
        entries: Sequence[JsonDict],
    ) -> int:
        if not entries:
            return 0
        path = self.task_dir(task_id) / TRACES_DIR / f"{worker_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(_dump_line(entry) + "\n")
        return len(entries)

    def list_worker_trace(
        self,
        *,
        task_id: str,
        run_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[JsonDict]:
        traces_dir = self.task_dir(task_id) / TRACES_DIR
        if not traces_dir.is_dir():
            return []
        files = (
            [traces_dir / f"{worker_id}.jsonl"]
            if worker_id
            else sorted(traces_dir.glob("*.jsonl"))
        )
        results: List[JsonDict] = []
        for path in files:
            if not path.is_file():
                continue
            name = path.stem
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    results.append({
                        "task_id": task_id,
                        "run_id": run_id or "",
                        "worker_id": name,
                        "sequence_no": index,
                        "trace_json": line,
                    })
                    if len(results) >= limit:
                        return results
        return results

    # -- strategy telemetry ------------------------------------------------
    def append_strategy_attempt(
        self,
        *,
        task_id: str,
        run_id: str,
        payload: JsonDict,
    ) -> None:
        """Written twice, as today: once per task, once at the repo root.

        The root copy is what gives cross-task visibility in file mode; the
        database backend gets that from a single table instead.
        """

        line = _dump_line(payload)
        for path in (
            self.task_dir(task_id) / STRATEGY_ATTEMPTS_FILE,
            Path.cwd() / STRATEGY_ATTEMPTS_FILE,
        ):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                continue

    def close(self) -> None:
        return None
