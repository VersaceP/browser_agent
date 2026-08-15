"""
harness.storage.base - Backend-agnostic storage interface.

Business modules depend on this surface only.  They never receive a
``sqlite3.Connection`` and never author SQL, so swapping FileStore for
SqliteStore stays a configuration change rather than a call-site rewrite.
"""

from __future__ import annotations

import fnmatch
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from harness.utils import JsonDict


# Backend identifiers accepted by the factory.
BACKEND_FILE = "file"
BACKEND_DUAL = "dual"
BACKEND_DB = "db"
VALID_BACKENDS = (BACKEND_FILE, BACKEND_DUAL, BACKEND_DB)

# Snapshot keys are a closed set; they name mutable current-state documents
# that were previously whole-file overwrites.
SNAPSHOT_KEY_TASK_STATE = "task_state"
SNAPSHOT_KEY_CURRENT_PLAN = "current_task_plan"

# Resource kinds that store only a path because the harness never holds the
# bytes: the ABCP platform writes the file and returns a receipt.
EXTERNAL_RESOURCE_TYPES = frozenset({"download", "coding_agent_output", "file_evidence"})


class StorageError(RuntimeError):
    """Base class for storage-layer failures."""


class RevisionConflictError(StorageError):
    """A snapshot CAS lost its race and the caller must not force the write."""

    def __init__(
        self,
        message: str,
        *,
        task_id: str = "",
        snapshot_key: str = "",
        expected_revision: int = 0,
        actual_revision: int = 0,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.snapshot_key = snapshot_key
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.attempts = attempts


class ResourceAccessError(StorageError):
    """A resource was addressed from outside its owning task."""


class StorageCorruptError(StorageError):
    """A row's physical representation cannot be restored to what it claims.

    Raised when a stored resource fails to decode: an unknown
    ``content_encoding``, a truncated or bomb-sized compressed stream, or a
    decompressed length that disagrees with the row's own ``byte_size``. This
    is storage damage, not a missing row, so the db-authoritative read paths
    must let it propagate rather than fall back to stale files.
    """


def canonical_json(value: Any) -> str:
    """Stable serialisation used for semantic hashing and dual-write checks.

    File and database backends serialise the same Python object independently;
    without a fixed key order and separator set their bytes differ for reasons
    that carry no meaning, and every comparison would report a false mismatch.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def normalize_external_path(
    task_dir: Path | str,
    external_path: Path | str,
) -> Tuple[str, bool, Path]:
    """Return one canonical external-file identity for every backend.

    Paths inside the task directory are stored task-relative so moving the
    whole worktree does not invalidate them. Relative inputs are interpreted
    relative to that task directory, never relative to the harness process's
    cwd. Paths outside the task directory stay absolute and are marked
    unmanaged so operator purge cannot delete data the task does not own.

    The resolved path is returned as well because callers that probe size/hash
    must inspect the same target whose canonical identity they persisted.
    """

    root = Path(task_dir).expanduser().resolve(strict=False)
    candidate = Path(external_path).expanduser()
    resolved = (
        candidate.resolve(strict=False)
        if candidate.is_absolute()
        else (root / candidate).resolve(strict=False)
    )
    try:
        return str(resolved.relative_to(root)), False, resolved
    except ValueError:
        return str(resolved), True, resolved


DEFAULT_RESOURCE_GLOB = "**/*"

_GLOB_CACHE: dict = {}


def glob_matches(pattern: str, logical_path: str) -> bool:
    """Match a path against a glob, with pathlib.Path.glob's semantics.

    The one matcher every search surface uses - the file backend, the database
    backend, the virtual view and the model-facing scan of the real worktree.
    Four implementations of "what does this glob mean" is how the same call
    came to return different files depending on where they were stored.

    ``*`` and ``?`` stop at a directory separator and ``**`` spans any number
    of directories, so ``*`` is the task root only and ``**/*`` is everything.
    An earlier version documented that rule and then special-cased ``*`` to
    match everything, which is exactly the divergence this replaces.
    """

    return _compile_glob(pattern or DEFAULT_RESOURCE_GLOB).match(logical_path) is not None


_FNMATCH_WRAPPER = re.compile(r"^\(\?s:(?P<body>.*)\)\\Z$", re.DOTALL)


def _compile_character_class(pattern: str, index: int, parts: List[str]) -> int:
    """Translate one ``[...]`` group by asking fnmatch to translate it.

    Only the *extent* of the group is found here; the contents go to the
    standard library. Character classes carry more rules than they look like -
    a leading ``^`` is an ordinary member while ``!`` negates, ``]`` straight
    after the opener is a member, an unclosed bracket is literal text, and
    reversed or overlapping ranges like ``[z-a]`` and ``[a--b]`` need
    normalising or they compile to invalid regex. A hand-written version got
    the first three wrong and then the ranges; delegating gets all of them
    right and keeps getting them right.

    Only ``*`` and ``?`` have to be reimplemented, because fnmatch lets them
    cross "/" and a path glob must not.
    """

    end = index + 1
    length = len(pattern)
    if end < length and pattern[end] == "!":
        end += 1
    if end < length and pattern[end] == "]":
        # A ']' straight after the opener is a member, not the terminator.
        end += 1
    while end < length and pattern[end] != "]":
        end += 1
    if end >= length:
        # Never closed, so the bracket was literal text.
        parts.append(re.escape(pattern[index]))
        return index + 1

    fragment = _translate_bracket(pattern[index:end + 1])
    # A class consumes exactly one character, and in a path glob that
    # character is never the separator. fnmatch has no notion of paths, so a
    # negated class it produced would happily match "/".
    parts.append(f"(?!/){fragment}")
    return end + 1


def _translate_bracket(source: str) -> str:
    """The regex fnmatch would use for one bracket expression."""

    try:
        translated = fnmatch.translate(source)
    except re.error:
        return re.escape(source)
    match = _FNMATCH_WRAPPER.match(translated)
    fragment = match.group("body") if match else None
    if fragment is None:
        return re.escape(source)
    try:
        re.compile(fragment)
    except re.error:
        # Never reached with a stdlib fnmatch, but a malformed fragment must
        # degrade to "matches nothing" rather than reach the caller as an
        # exception from what is only a search filter.
        return "(?!)"
    return fragment


def glob_sql_prefilter(pattern: str) -> str:
    """A SQLite GLOB guaranteed to match a superset of ``pattern``.

    SQLite's GLOB is not the same language: it spells negation ``[^...]`` where
    a path glob spells it ``[!...]``, so handing a character class straight to
    SQL silently discarded rows the exact matcher would have kept. Only the
    literal prefix before the first magic character is safe to narrow on;
    everything after it is decided in Python.
    """

    text = pattern or DEFAULT_RESOURCE_GLOB
    cut = len(text)
    for magic in ("*", "?", "["):
        position = text.find(magic)
        if position != -1:
            cut = min(cut, position)
    if cut >= len(text):
        # No magic at all: an exact path, which GLOB matches literally.
        return text
    return text[:cut] + "*"


def _compile_glob(pattern: str):
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    parts: List[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        if pattern.startswith("**/", index):
            # Zero or more whole segments, so "**/a" matches "a" as well.
            parts.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("/**", index) and index + 3 == length:
            parts.append("/.*")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            index = _compile_character_class(pattern, index, parts)
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    try:
        compiled = re.compile("".join(parts) + r"\Z")
    except re.error:
        # A search filter must never raise at the caller. pathlib answers a
        # degenerate pattern with "no matches"; so does this.
        compiled = re.compile("(?!)")
    _GLOB_CACHE[pattern] = compiled
    return compiled

class Storage(ABC):
    """The full set of operations business code may perform on task data."""

    # -- task lifecycle ----------------------------------------------------
    @abstractmethod
    def create_task(
        self,
        *,
        task_id: str,
        harness_version: str,
        snapshot: Optional[JsonDict] = None,
    ) -> JsonDict:
        """Register a task. Idempotent for an existing live task_id."""

    @abstractmethod
    def get_task(self, task_id: str, *, include_deleted: bool = False) -> Optional[JsonDict]:
        """Return the task row, or None when absent or soft-deleted."""

    @abstractmethod
    def update_task_snapshot(self, task_id: str, snapshot: JsonDict) -> None:
        """Refresh the task summary shown in listings."""

    @abstractmethod
    def soft_delete_task(self, task_id: str) -> bool:
        """Mark a task deleted. Data stays queryable to operators."""

    # -- run lifecycle -----------------------------------------------------
    @abstractmethod
    def start_run(
        self,
        *,
        task_id: str,
        harness_version: str,
        run_id: Optional[str] = None,
    ) -> JsonDict:
        """Open a run. A task accumulates one run per launch or resume."""

    @abstractmethod
    def finish_run(
        self,
        *,
        task_id: str,
        run_id: str,
        status: str,
        error: Optional[JsonDict] = None,
    ) -> None:
        """Close a run with a terminal status."""

    # -- events ------------------------------------------------------------
    @abstractmethod
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
        """Append one run event. run_id is a relational field, not payload."""

    @abstractmethod
    def read_events(
        self,
        *,
        task_id: str,
        after_event_id: int = 0,
        limit: int = 200,
        event_type: Optional[str] = None,
    ) -> List[JsonDict]:
        """Keyset-paginated event read. Never uses a large OFFSET."""

    # -- mutable snapshots -------------------------------------------------
    @abstractmethod
    def load_snapshot(self, *, task_id: str, snapshot_key: str) -> Tuple[JsonDict, int]:
        """Return ``(value, revision)``; an absent row reads as ``({}, 0)``."""

    @abstractmethod
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
        """Persist a snapshot, preserving concurrent edits.

        ``merge`` receives ``(base, current, proposed)`` and must be pure. On a
        lost CAS the merge is recomputed against the fresh ``current`` while
        ``base`` and ``proposed`` stay at their original values - re-deriving
        the base would erase the record of what this caller actually changed.
        """

    # -- plan history ------------------------------------------------------
    @abstractmethod
    def save_plan_version(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        """Append an immutable accepted-plan record. Returns it with its version."""

    @abstractmethod
    def load_plan_version(self, *, task_id: str, version: int) -> Optional[JsonDict]:
        """Read one accepted-plan record; version 1 is the resume anchor."""

    @abstractmethod
    def save_plan_review(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        """Append a validator review of a candidate plan."""

    @abstractmethod
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
        """Publish a plan generation as one unit.

        The version record, the current-plan alias and the reset task state
        describe a single generation. Written separately, a crash between them
        leaves a task whose plan and state disagree about which phases exist -
        the torn generation the file backend needs a repair pass for. Returns
        ``(stored_plan_record, persisted_state)``.

        ``summarize`` receives the persisted state and returns the listing
        snapshot. It is a callable rather than a value because the plan version
        is allocated inside this call and the summary quotes it, so the
        summary cannot be computed before the transaction opens - and it runs
        inside that transaction so a task can never be listed with a summary
        describing a generation that was not committed.
        """

    # -- resources ---------------------------------------------------------
    @abstractmethod
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
        """Store a process resource, superseding any current row at that path."""

    @abstractmethod
    def read_resource(
        self,
        *,
        current_task_id: str,
        resource_uri: str,
    ) -> Optional[JsonDict]:
        """Read a resource, refusing URIs belonging to another task."""

    @abstractmethod
    def search_resources(
        self,
        *,
        task_id: str,
        path_glob: str = DEFAULT_RESOURCE_GLOB,
        pattern: Optional[str] = None,
        max_results: int = 20,
    ) -> List[JsonDict]:
        """Filter in SQL, then apply the caller's regex in Python."""

    # -- worker traces -----------------------------------------------------
    @abstractmethod
    def append_worker_trace(
        self,
        *,
        task_id: str,
        run_id: str,
        worker_id: str,
        entries: Sequence[JsonDict],
    ) -> int:
        """Append trace steps for one worker. Returns the count written."""

    @abstractmethod
    def list_worker_trace(
        self,
        *,
        task_id: str,
        run_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[JsonDict]:
        """Read trace steps in sequence order."""

    # -- strategy telemetry ------------------------------------------------
    @abstractmethod
    def append_strategy_attempt(
        self,
        *,
        task_id: str,
        run_id: str,
        payload: JsonDict,
    ) -> None:
        """Record one strategy attempt for cross-task analysis."""

    # -- lifecycle ---------------------------------------------------------
    @abstractmethod
    def close(self) -> None:
        """Release backend handles."""
