"""
harness.storage.factory - Database bootstrap and backend selection.

Configuration is passed explicitly rather than read from ``runtime_config``:
this phase adds no business-code coupling, and step 2 wires a thin
``from_runtime_config`` helper on top once the stores exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

from harness.storage.base import (
    BACKEND_DB,
    BACKEND_DUAL,
    BACKEND_FILE,
    VALID_BACKENDS,
    Storage,
    StorageError,
)
from harness.storage.migrations import SCHEMA_VERSION, apply_migrations
from harness.storage.sqlite_connection import (
    DEFAULT_BUSY_TIMEOUT_MS,
    ConnectionRegistry,
    connect,
)


# Relative paths resolve against worktree_dir, not the process working
# directory. A resume relocates worktree_dir to the recovered task's parent,
# and a cwd-relative database would silently stay pointed at a different one.
DEFAULT_SQLITE_PATH = "harness.db"


def resolve_sqlite_path(sqlite_path: Path | str, worktree_dir: str) -> Path:
    path = Path(sqlite_path or DEFAULT_SQLITE_PATH).expanduser()
    if path.is_absolute():
        return path
    return Path(worktree_dir or "worktree").expanduser() / path


def open_database(
    database_path: Path | str = DEFAULT_SQLITE_PATH,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a connection and bring it up to the current schema version."""

    connection = connect(database_path, busy_timeout_ms=busy_timeout_ms)
    apply_migrations(connection)
    return connection


def open_registry(
    database_path: Path | str = DEFAULT_SQLITE_PATH,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> ConnectionRegistry:
    """Build a per-thread connection registry with the schema already applied.

    Migrations run once here on a throwaway connection so that every thread
    handed a connection later finds a ready database.
    """

    registry = ConnectionRegistry(database_path, busy_timeout_ms=busy_timeout_ms)
    apply_migrations(registry.connection())
    return registry


def normalize_backend(value: Optional[str]) -> str:
    backend = str(value or BACKEND_FILE).strip().lower()
    if backend not in VALID_BACKENDS:
        raise StorageError(
            f"unknown storage_backend {value!r}; expected one of {', '.join(VALID_BACKENDS)}"
        )
    return backend


def create_storage(
    *,
    backend: str = BACKEND_FILE,
    worktree_dir: str = "worktree",
    sqlite_path: Path | str = DEFAULT_SQLITE_PATH,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    dual_verify: bool = True,
    on_revision_conflict: Optional[Callable[[dict], None]] = None,
    on_verify: Optional[Callable[[dict], None]] = None,
    resource_compression: str = "zlib",
    resource_compression_min_bytes: int = 16384,
    resource_compression_level: int = 6,
) -> Storage:
    """Build the configured backend.

    Imports are local so that ``file`` mode never touches the SQLite modules -
    the default path must not be able to fail on a database problem.
    """

    resolved = normalize_backend(backend)
    from harness.storage.file_store import FileStore

    if resolved == BACKEND_FILE:
        return FileStore(worktree_dir=worktree_dir)

    from harness.storage.sqlite_store import SqliteStore

    sqlite_store = SqliteStore(
        resolve_sqlite_path(sqlite_path, worktree_dir),
        worktree_dir=worktree_dir,
        busy_timeout_ms=busy_timeout_ms,
        on_revision_conflict=on_revision_conflict,
        resource_compression=resource_compression,
        resource_compression_min_bytes=resource_compression_min_bytes,
        resource_compression_level=resource_compression_level,
    )
    if resolved == BACKEND_DB:
        return sqlite_store

    from harness.storage.dual_store import DualStore

    return DualStore(
        FileStore(worktree_dir=worktree_dir),
        sqlite_store,
        verify=dual_verify,
        on_verify=on_verify,
    )


def create_storage_from_config(
    harness_config: Any,
    *,
    worktree_dir: str = "worktree",
    on_revision_conflict: Optional[Callable[[dict], None]] = None,
    on_verify: Optional[Callable[[dict], None]] = None,
) -> Storage:
    """Build a backend from a ``HarnessConfig``-shaped object."""

    return create_storage(
        backend=getattr(harness_config, "storage_backend", BACKEND_FILE),
        worktree_dir=worktree_dir,
        sqlite_path=getattr(harness_config, "storage_sqlite_path", DEFAULT_SQLITE_PATH),
        busy_timeout_ms=getattr(
            harness_config, "storage_busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS
        ),
        dual_verify=getattr(harness_config, "storage_dual_verify", True),
        on_revision_conflict=on_revision_conflict,
        on_verify=on_verify,
        resource_compression=getattr(harness_config, "resource_compression", "zlib"),
        resource_compression_min_bytes=getattr(
            harness_config, "resource_compression_min_bytes", 16384
        ),
        resource_compression_level=getattr(
            harness_config, "resource_compression_level", 6
        ),
    )


__all__ = [
    "BACKEND_DB",
    "BACKEND_DUAL",
    "BACKEND_FILE",
    "DEFAULT_SQLITE_PATH",
    "SCHEMA_VERSION",
    "create_storage",
    "create_storage_from_config",
    "resolve_sqlite_path",
    "normalize_backend",
    "open_database",
    "open_registry",
]
