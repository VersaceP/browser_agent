"""Strict, filesystem-only helpers used to bootstrap task resumption.

This module intentionally does not construct a ``RunLogger``.  Resume callers
must validate an existing task directory before anything has a chance to
recreate a deleted worktree.
"""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from harness.task_control import TASK_PLAN_FILE, TASK_STATE_FILE


JsonDict = Dict[str, Any]
TaskDirLike = Union[str, Path, Any]
TASK_MANIFEST_FILE = "task_manifest.json"
RUN_LOCK_DIR = ".run.lock"
RUN_LOCK_OWNER_FILE = "owner.json"
RUN_LOCK_OWNER_GRACE_SECONDS = 30.0
_USER_TASK_RE = re.compile(r"<user_task>\s*(.*?)\s*</user_task>", re.DOTALL)


class ResumeStateError(RuntimeError):
    """An existing task cannot be resumed safely from its files."""


class RunLockError(ResumeStateError):
    """A task run lock could not be acquired safely."""

    def __init__(self, message: str, *, owner: Optional[JsonDict] = None) -> None:
        super().__init__(message)
        self.owner = dict(owner or {})


@dataclass(frozen=True)
class RunLock:
    task_dir: Path
    lock_dir: Path
    owner_token: str
    owner: JsonDict


def _task_dir(value: TaskDirLike) -> Path:
    candidate = getattr(value, "task_dir", value)
    return Path(candidate).expanduser().resolve(strict=False)


def _require_task_dir(value: TaskDirLike) -> Path:
    task_dir = _task_dir(value)
    if not task_dir.is_dir():
        raise ResumeStateError(
            f"resume worktree does not exist or is not a directory: {task_dir}"
        )
    return task_dir


def _load_json_object_strict(path: Path, *, label: str) -> JsonDict:
    if not path.is_file():
        raise ResumeStateError(f"{label} is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ResumeStateError(f"cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ResumeStateError(f"malformed {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResumeStateError(f"{label} must contain a JSON object: {path}")
    return payload


def load_task_plan_strict(value: TaskDirLike) -> JsonDict:
    """Load an existing current plan, raising on every unsafe fallback case."""

    task_dir = _require_task_dir(value)
    plan = _load_json_object_strict(task_dir / TASK_PLAN_FILE, label="task plan")
    if not isinstance(plan.get("phases"), list):
        raise ResumeStateError(f"task plan has no phases list: {task_dir / TASK_PLAN_FILE}")
    return plan


def load_initial_task_plan_strict(value: TaskDirLike) -> JsonDict:
    """Load the immutable first accepted plan rather than the current alias."""

    task_dir = _require_task_dir(value)
    record = _load_json_object_strict(
        task_dir / "task_plan_history" / "plan.0001.json",
        label="initial task plan",
    )
    # Plan history files are immutable audit records whose executable plan is
    # nested under ``plan``.  Accept a direct-plan shape only for older runs.
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else record
    if not isinstance(plan.get("phases"), list):
        raise ResumeStateError(
            "initial task plan history record has no executable phases list: "
            f"{task_dir / 'task_plan_history' / 'plan.0001.json'}"
        )
    return plan


def load_task_state_strict(value: TaskDirLike) -> JsonDict:
    """Load existing state without the legacy fail-open ``{}`` behaviour."""

    task_dir = _require_task_dir(value)
    state = _load_json_object_strict(task_dir / TASK_STATE_FILE, label="task state")
    if not isinstance(state.get("phases"), dict):
        raise ResumeStateError(
            f"task state has no phases object: {task_dir / TASK_STATE_FILE}"
        )
    return state


def _atomic_write_json(path: Path, payload: JsonDict) -> None:
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
            json.dump(payload, temporary, ensure_ascii=False, indent=2, default=str)
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


def _atomic_create_json(path: Path, payload: JsonDict) -> bool:
    """Publish a complete JSON file only when ``path`` does not exist.

    A hard link gives this create-once file the two properties that an
    ``exists()``/``replace()`` sequence cannot provide together: competing
    writers cannot overwrite the winner, and readers never observe the
    temporary file while it is only partly written.
    """

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
            json.dump(payload, temporary, ensure_ascii=False, indent=2, default=str)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            return False
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def write_task_manifest(
    value: TaskDirLike,
    *,
    original_user_task: str,
    task_id: str = "",
    config_path: Optional[str] = None,
    forced_skill_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    startup_args: Optional[JsonDict] = None,
) -> JsonDict:
    """Create the immutable task identity manifest once.

    An existing valid manifest is returned unchanged so a resume instruction
    can never overwrite the original user request.
    """

    task_dir = _require_task_dir(value)
    path = task_dir / TASK_MANIFEST_FILE
    if path.exists():
        return _load_json_object_strict(path, label="task manifest")
    task_text = str(original_user_task or "").strip()
    if not task_text:
        raise ResumeStateError("original_user_task is required for a new task manifest")
    payload: JsonDict = {
        "version": "v1",
        "taskId": str(task_id or getattr(value, "task_id", "") or task_dir.name),
        "original_user_task": task_text,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_path": str(config_path or ""),
        "forced_skill_id": str(forced_skill_id or ""),
        "agent_id": str(agent_id or ""),
    }
    if isinstance(startup_args, dict) and startup_args:
        payload["startup_args"] = dict(startup_args)
    if _atomic_create_json(path, payload):
        return payload
    # A concurrent creator won.  Its immutable identity is authoritative.
    return _load_json_object_strict(path, label="task manifest")


def reconcile_torn_plan_alias(
    value: TaskDirLike,
    *,
    current_plan: JsonDict,
    state: JsonDict,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    """Rollback the one safe replan crash window, otherwise fail closed.

    An accepted replan is persisted as (1) an immutable history record,
    (2) the mutable ``task_plan.json`` alias, then (3) task state.  If the
    process stops between 2 and 3, the alias is exactly the next history
    version while state still names its predecessor.  That relationship is
    mechanical, so restoring the predecessor is safe; every other mismatch
    remains an error instead of being guessed through.
    """

    from harness.plan_validator import plan_hash

    task_dir = _require_task_dir(value)
    recorded_hash = str(state.get("plan_hash") or "").strip()
    if not recorded_hash:
        # Legacy worktrees predate the version/hash fields.  There is no
        # contradictory generation to reconcile, so later evidence validation
        # may proceed against the sole current plan.
        return current_plan, None
    if recorded_hash == plan_hash(current_plan):
        return current_plan, None
    try:
        recorded_version = int(state.get("plan_version") or 0)
    except (TypeError, ValueError):
        recorded_version = 0
    if recorded_version <= 0:
        raise ResumeStateError(
            "task_plan.json and task_state.json disagree, but state has no "
            "recoverable plan version"
        )

    history_dir = task_dir / "task_plan_history"
    state_path = history_dir / f"plan.{recorded_version:04d}.json"
    next_path = history_dir / f"plan.{recorded_version + 1:04d}.json"
    state_record = _load_json_object_strict(
        state_path, label="state plan history record"
    )
    next_record = _load_json_object_strict(
        next_path, label="interrupted replan history record"
    )
    state_plan = state_record.get("plan")
    next_plan = next_record.get("plan")
    if not isinstance(state_plan, dict) or not isinstance(state_plan.get("phases"), list):
        raise ResumeStateError(f"state plan history record has no plan: {state_path}")
    if not isinstance(next_plan, dict) or not isinstance(next_plan.get("phases"), list):
        raise ResumeStateError(f"interrupted replan history record has no plan: {next_path}")

    state_record_hash = str(state_record.get("planHash") or plan_hash(state_plan))
    next_record_hash = str(next_record.get("planHash") or plan_hash(next_plan))
    try:
        previous_version = int(next_record.get("previousVersion"))
    except (TypeError, ValueError):
        previous_version = 0
    later_records = []
    for candidate in history_dir.glob("plan.*.json"):
        try:
            candidate_version = int(candidate.stem.split(".")[-1])
        except (TypeError, ValueError):
            continue
        if candidate_version > recorded_version + 1:
            later_records.append(candidate)
    mechanically_torn = (
        state_record_hash == recorded_hash
        and plan_hash(state_plan) == recorded_hash
        and int(state_record.get("planVersion") or recorded_version) == recorded_version
        and previous_version == recorded_version
        and int(next_record.get("planVersion") or 0) == recorded_version + 1
        and next_record_hash == plan_hash(current_plan)
        and plan_hash(next_plan) == plan_hash(current_plan)
        and not later_records
    )
    if not mechanically_torn:
        raise ResumeStateError(
            "task_plan.json and task_state.json generations disagree and do "
            "not match the recoverable one-step replan crash window"
        )

    _atomic_write_json(task_dir / TASK_PLAN_FILE, state_plan)
    return state_plan, {
        "status": "rolled_back_interrupted_replan_alias",
        "restoredPlanVersion": recorded_version,
        "discardedAliasPlanVersion": recorded_version + 1,
        "historyPath": str(next_path.resolve()),
    }


def load_task_manifest(
    value: TaskDirLike,
    *,
    required: bool = False,
) -> Optional[JsonDict]:
    task_dir = _require_task_dir(value)
    path = task_dir / TASK_MANIFEST_FILE
    if not path.exists() and not required:
        return None
    return _load_json_object_strict(path, label="task manifest")


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            for item in value
            if isinstance(item, dict)
        )
    return ""


def recover_legacy_user_task(value: TaskDirLike) -> str:
    """Best-effort extraction for pre-manifest worktrees; return ``""`` if absent."""

    task_dir = _require_task_dir(value)
    contexts = task_dir / "contexts"
    candidates = [contexts / "lead_agent-final-context.json"]
    if contexts.is_dir():
        candidates.extend(sorted(contexts.glob("lead_agent-*-final-context.json")))
    seen = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            match = _USER_TASK_RE.search(_message_text(message.get("content")))
            if match and match.group(1).strip():
                return match.group(1).strip()
    return ""


def original_user_task_for_resume(value: TaskDirLike) -> str:
    manifest = load_task_manifest(value)
    if manifest is not None:
        task = str(manifest.get("original_user_task") or "").strip()
        if not task:
            raise ResumeStateError("task manifest does not contain original_user_task")
        return task
    task = recover_legacy_user_task(value)
    if not task:
        raise ResumeStateError(
            "the legacy worktree has no recoverable original user task; "
            "the user must restate it"
        )
    return task


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_owner(lock_dir: Path) -> JsonDict:
    try:
        owner = json.loads((lock_dir / RUN_LOCK_OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunLockError(
            f"run lock owner is missing or malformed: {lock_dir}"
        ) from exc
    if not isinstance(owner, dict):
        raise RunLockError(f"run lock owner must be a JSON object: {lock_dir}")
    return owner


def _cleanup_stale_ownerless_lock(lock_dir: Path) -> bool:
    """Remove a crash-left lock only after a grace period and recheck.

    ``mkdir`` publishes ownership before ``owner.json`` can be atomically
    installed. A fresh owner-less directory may therefore belong to a live
    contender and is never touched. For an old directory, re-read the owner
    and compare the directory identity/mtime immediately before cleanup; any
    concurrent progress sends the caller back through normal lock arbitration.
    """

    owner_path = lock_dir / RUN_LOCK_OWNER_FILE
    # A malformed published owner is corruption rather than the mkdir/write
    # crash window. Without a trustworthy token it cannot be removed safely.
    if owner_path.exists():
        return False
    try:
        before = lock_dir.stat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RunLockError(f"cannot inspect run lock: {lock_dir}: {exc}") from exc
    age_seconds = max(0.0, time.time() - before.st_mtime)
    if age_seconds < RUN_LOCK_OWNER_GRACE_SECONDS:
        return False

    try:
        _read_lock_owner(lock_dir)
    except RunLockError:
        if owner_path.exists():
            return False
    else:
        return True

    try:
        after = lock_dir.stat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RunLockError(f"cannot recheck run lock: {lock_dir}: {exc}") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        return True

    try:
        lock_dir.rmdir()
    except FileNotFoundError:
        return True
    except OSError as exc:
        # An owner/temp file appearing after the recheck makes rmdir fail
        # closed. Never unlink unknown contents or recursively delete a lock.
        raise RunLockError(
            f"stale owner-less run lock cannot be removed safely: {lock_dir}: {exc}"
        ) from exc
    return True


def acquire_run_lock(value: TaskDirLike) -> RunLock:
    """Atomically acquire ``.run.lock``, safely competing for stale takeover."""

    task_dir = _require_task_dir(value)
    lock_dir = task_dir / RUN_LOCK_DIR
    local_host = socket.gethostname()
    owner_token = uuid.uuid4().hex
    owner: JsonDict = {
        "pid": os.getpid(),
        "host": local_host,
        "ownerToken": owner_token,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Prepare complete bytes outside the lock directory. After mkdir succeeds,
    # owner publication is one rename; a crash before it leaves an empty lock
    # that the aged-ownerless recovery path can remove. No internal temp file
    # can make that crash window permanently non-empty.
    prepared_owner = task_dir / f".run-lock-owner.{owner_token}.tmp"
    _atomic_write_json(prepared_owner, owner)
    try:
        while True:
            try:
                lock_dir.mkdir()
            except FileExistsError:
                try:
                    existing = _read_lock_owner(lock_dir)
                except RunLockError:
                    if _cleanup_stale_ownerless_lock(lock_dir):
                        # Cleanup never grants ownership. Compete for mkdir again,
                        # or observe the valid owner that appeared during recheck.
                        continue
                    raise
                existing_host = str(existing.get("host") or "")
                try:
                    existing_pid = int(existing.get("pid") or 0)
                except (TypeError, ValueError):
                    existing_pid = 0
                if existing_host != local_host:
                    raise RunLockError(
                        "run lock belongs to another host and cannot be probed safely",
                        owner=existing,
                    )
                if _pid_is_alive(existing_pid):
                    raise RunLockError(
                        f"task is already running with pid {existing_pid}", owner=existing
                    )

                # Re-read immediately before removal. If another contender already
                # replaced the owner, go back through the normal live-owner check.
                current = _read_lock_owner(lock_dir)
                if (
                    str(current.get("ownerToken") or "")
                    != str(existing.get("ownerToken") or "")
                    or current.get("pid") != existing.get("pid")
                    or current.get("host") != existing.get("host")
                ):
                    continue
                try:
                    (lock_dir / RUN_LOCK_OWNER_FILE).unlink()
                    lock_dir.rmdir()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise RunLockError(
                        f"stale run lock cannot be removed safely: {lock_dir}: {exc}",
                        owner=existing,
                    ) from exc
                # Never assume stale cleanup granted ownership: race mkdir again.
                continue
            except OSError as exc:
                raise RunLockError(f"cannot create run lock {lock_dir}: {exc}") from exc

            try:
                os.replace(prepared_owner, lock_dir / RUN_LOCK_OWNER_FILE)
            except Exception:
                try:
                    lock_dir.rmdir()
                except OSError:
                    pass
                raise
            return RunLock(task_dir, lock_dir, owner_token, owner)
    finally:
        try:
            prepared_owner.unlink()
        except FileNotFoundError:
            pass


def release_run_lock(lock: RunLock) -> bool:
    """Release only when the on-disk owner token is still ours."""

    try:
        owner = _read_lock_owner(lock.lock_dir)
    except RunLockError:
        return False
    if str(owner.get("ownerToken") or "") != lock.owner_token:
        return False
    try:
        (lock.lock_dir / RUN_LOCK_OWNER_FILE).unlink()
        lock.lock_dir.rmdir()
    except OSError:
        return False
    return True
