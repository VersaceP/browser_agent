"""
harness.storage.sqlite_connection - Connection lifecycle and transaction rules.

Every rule enforced here exists because SQLite's defaults are wrong for a
multi-process harness:

* ``isolation_level=None`` disables the driver's implicit transaction
  management so every write transaction is opened explicitly as
  ``BEGIN IMMEDIATE``.  Python's default DEFERRED transactions take a read
  lock first and upgrade on the first write; if another connection committed
  in between, that upgrade returns SQLITE_BUSY *immediately* without ever
  consulting ``busy_timeout``, because waiting could deadlock.
* ``BEGIN IMMEDIATE`` takes the write lock up front, which is the only case
  where ``busy_timeout`` actually applies.
* Connections are per-thread and never shared.  ``check_same_thread`` stays
  at its default so a leaked cross-thread handle fails loudly instead of
  corrupting state silently.

Transaction bodies must contain SQL only - no network calls, no file I/O, no
LLM calls, no ``await``.  WAL allows many readers with a single writer, so one
long transaction stalls every other process's writes for its whole duration.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_MAX_BUSY_RETRIES = 5
DEFAULT_BUSY_RETRY_BASE_SECONDS = 0.05

# SQLite's default page_size (4096) is kept deliberately.  Larger pages reduce
# overflow-chain length for big BLOBs but do not shorten the write lock, which
# is dominated by byte volume.  Changing it later requires leaving WAL mode and
# running VACUUM, so revisit only with benchmark evidence.
DEFAULT_PAGE_SIZE: Optional[int] = None

# Reclaiming freed pages is a write operation. It must never run on the task
# completion path (a normal finish frees nothing); only a maintenance pass
# after a hard delete should call it, and only past this threshold.
FREELIST_VACUUM_THRESHOLD = 1000


class StorageBusyError(RuntimeError):
    """A write could not acquire the SQLite write lock within the retry budget."""


class StorageClosedError(RuntimeError):
    """A connection was requested after the registry was closed."""


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


def database_is_empty(connection: sqlite3.Connection) -> bool:
    """True when no user objects exist yet, i.e. header pragmas still apply."""

    row = connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'index', 'view', 'trigger')"
    ).fetchone()
    return int(row[0] or 0) == 0


def configure_connection(
    connection: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    page_size: Optional[int] = DEFAULT_PAGE_SIZE,
) -> sqlite3.Connection:
    """Apply the pragmas this harness depends on.

    ``page_size`` and ``auto_vacuum`` only bind while the database is still
    empty, so they are issued before WAL is enabled and before any migration
    creates a table.  ``journal_mode`` persists in the file header; re-issuing
    it on later connections is a no-op read.
    """

    connection.row_factory = sqlite3.Row
    # Before anything that can contend. Every statement below touches a lock on
    # a first launch - reading sqlite_master, converting the journal to WAL -
    # and until this runs they are bounded by sqlite3.connect's own default
    # rather than by the timeout this harness was configured with.
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    _retry_while_busy(lambda: _initialise_pragmas(connection, page_size))
    return connection


def _initialise_pragmas(
    connection: sqlite3.Connection, page_size: Optional[int]
) -> None:
    if database_is_empty(connection):
        if page_size:
            connection.execute(f"PRAGMA page_size = {int(page_size)}")
        # Without this, pages freed by a hard delete are never returned to the
        # filesystem and the database only ever grows.
        connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")


def _retry_while_busy(
    operation: Callable[[], Any],
    *,
    max_retries: int = DEFAULT_MAX_BUSY_RETRIES,
    retry_base_seconds: float = DEFAULT_BUSY_RETRY_BASE_SECONDS,
) -> Any:
    """Retry an idempotent operation that lost a race for the database lock.

    Startup is the one moment every process contends at once, and the pragmas
    above are all idempotent, so a bounded retry is safe here in a way it is
    not inside a transaction body.
    """

    attempt = 0
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or attempt >= max_retries:
                raise
            time.sleep(
                retry_base_seconds * (2 ** attempt)
                + random.uniform(0, retry_base_seconds)
            )
            attempt += 1


def connect(
    database_path: Path | str,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    page_size: Optional[int] = DEFAULT_PAGE_SIZE,
) -> sqlite3.Connection:
    """Open a fully configured connection. Callers own its lifetime."""

    path = Path(database_path).expanduser()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        return configure_connection(
            connection,
            busy_timeout_ms=busy_timeout_ms,
            page_size=page_size,
        )
    except BaseException:
        # The caller never received this handle, so nothing else can close it.
        # Leaving it to the garbage collector holds a file descriptor - and on
        # a failed first launch, possibly a lock - for an unbounded time.
        try:
            connection.close()
        except sqlite3.Error:
            pass
        raise


@contextmanager
def write_transaction(
    connection: sqlite3.Connection,
    *,
    max_retries: int = DEFAULT_MAX_BUSY_RETRIES,
    retry_base_seconds: float = DEFAULT_BUSY_RETRY_BASE_SECONDS,
) -> Iterator[sqlite3.Connection]:
    """Run the body inside ``BEGIN IMMEDIATE`` with bounded busy retries.

    Only acquiring the lock is retried.  Once the body has started, a
    SQLITE_BUSY raised mid-transaction propagates: replaying arbitrary
    statements is not safe in general, and callers that need retry semantics
    (snapshot CAS) re-derive their value on each attempt instead.
    """

    attempt = 0
    while True:
        try:
            connection.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or attempt >= max_retries:
                raise StorageBusyError(
                    f"could not acquire the SQLite write lock after {attempt} retries: {exc}"
                ) from exc
            delay = retry_base_seconds * (2 ** attempt)
            time.sleep(delay + random.uniform(0, retry_base_seconds))
            attempt += 1
    try:
        yield connection
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    connection.execute("COMMIT")


def freelist_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA freelist_count").fetchone()
    return int(row[0] or 0) if row else 0


def maybe_incremental_vacuum(
    connection: sqlite3.Connection,
    *,
    threshold: int = FREELIST_VACUUM_THRESHOLD,
    pages: int = 1000,
) -> int:
    """Reclaim freed pages only when enough have accumulated.

    Returns the number of pages that were on the freelist before the attempt,
    or 0 when the threshold was not met and nothing ran.
    """

    pending = freelist_count(connection)
    if pending < threshold:
        return 0
    connection.execute(f"PRAGMA incremental_vacuum({int(pages)})")
    return pending


class ConnectionRegistry:
    """One connection per thread, created lazily.

    A sqlite3 connection must not cross threads or a fork.  Handing every
    thread its own handle keeps that invariant without ``check_same_thread``
    escape hatches, and keeps WAL's single-writer rule the only serialisation
    point.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        page_size: Optional[int] = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.page_size = page_size
        self._local = threading.local()
        self._all_lock = threading.Lock()
        self._all: list[sqlite3.Connection] = []
        self._closed = False

    def connection(self) -> sqlite3.Connection:
        if self._closed:
            # Lazily reopening here would make close() a no-op and hide the
            # lifecycle bug that led to a write after shutdown.
            raise StorageClosedError("connection registry is closed")
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing
        created = connect(
            self.database_path,
            busy_timeout_ms=self.busy_timeout_ms,
            page_size=self.page_size,
        )
        self._local.connection = created
        with self._all_lock:
            self._all.append(created)
        return created

    def close_all(self) -> None:
        """Close every handle. The registry is not reusable afterwards."""

        with self._all_lock:
            connections, self._all = self._all, []
            self._closed = True
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()
