"""
harness.storage.migrations - Ordered, versioned schema migrations.

The schema version lives in its own ``schema_migrations`` table rather than
being inferred from ``tasks``, so an empty database and an unmigrated one are
distinguishable.  Each migration applies inside a single ``BEGIN IMMEDIATE``
transaction together with its version row: a crash mid-migration leaves the
database at the previous version, never half-applied.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, NamedTuple, Sequence

from harness.storage.sqlite_connection import write_transaction


PACKAGE_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = PACKAGE_DIR / "schema.sql"

CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version        INTEGER PRIMARY KEY,
    migration_name TEXT NOT NULL,
    applied_at     TEXT NOT NULL
)
"""


class Migration(NamedTuple):
    version: int
    name: str
    statements: Sequence[str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_sql_statements(script: str) -> List[str]:
    """Split a SQL script using SQLite's own statement parser.

    Naive ``split(";")`` breaks on semicolons inside string literals and on
    trailing comments; ``sqlite3.complete_statement`` is the same check the
    shell uses, so comment- and literal-heavy DDL survives intact.
    """

    statements: List[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    remainder = buffer.strip()
    if remainder:
        statements.append(remainder)
    return statements


def _load_initial_migration() -> Migration:
    return Migration(
        version=1,
        name="initial_schema",
        statements=split_sql_statements(SCHEMA_FILE.read_text(encoding="utf-8")),
    )


# Recorded per run, not per task: the hand-written HARNESS_VERSION is bumped
# at release time and will sometimes be stale, and this answers "which code
# actually produced these rows" without depending on anyone remembering.
MIGRATION_0002_GIT_SHA = Migration(
    version=2,
    name="run_git_sha",
    statements=["ALTER TABLE task_runs ADD COLUMN git_sha TEXT"],
)

# byte_size keeps naming the logical file's size - what a reader pages through
# - so this records the compact column's size separately rather than
# overloading one number with two meanings.  NULL on rows written before the
# split, which is honest: their stored size was never measured apart.
MIGRATION_0003_STORED_SIZE = Migration(
    version=3,
    name="resource_stored_byte_size",
    statements=["ALTER TABLE task_resources ADD COLUMN stored_byte_size INTEGER"],
)

MIGRATIONS: List[Migration] = [
    _load_initial_migration(),
    MIGRATION_0002_GIT_SHA,
    MIGRATION_0003_STORED_SIZE,
]

SCHEMA_VERSION = max(migration.version for migration in MIGRATIONS)


def applied_versions(connection: sqlite3.Connection) -> List[int]:
    # Inside the write lock: this is the first DDL every process issues, so on
    # a first launch they all issue it at once. IF NOT EXISTS makes it safe to
    # repeat, but only a transaction makes the contention retryable.
    with write_transaction(connection):
        connection.execute(CREATE_MIGRATIONS_TABLE)
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row[0]) for row in rows]


def _version_is_applied(connection: sqlite3.Connection, version: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (int(version),)
    ).fetchone()
    return row is not None


def current_schema_version(connection: sqlite3.Connection) -> int:
    versions = applied_versions(connection)
    return versions[-1] if versions else 0


def apply_migrations(connection: sqlite3.Connection) -> List[int]:
    """Apply every pending migration in order. Returns the versions applied.

    Safe to call on every startup: already-applied versions are skipped, so a
    second call on an up-to-date database is a no-op returning ``[]``.
    """

    already = set(applied_versions(connection))
    newly_applied: List[int] = []
    for migration in sorted(MIGRATIONS, key=lambda item: item.version):
        if migration.version in already:
            continue
        # DDL and the version row commit together; executescript() would force
        # its own COMMIT and break that atomicity.
        with write_transaction(connection):
            # Re-check under the write lock. The check above ran without one,
            # so on a first launch several processes can all see the migration
            # as pending; whichever loses the race would otherwise replay the
            # DDL and fail with "table tasks already exists".
            if _version_is_applied(connection, migration.version):
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, migration_name, applied_at)"
                " VALUES (?, ?, ?)",
                (migration.version, migration.name, _utc_now_iso()),
            )
        newly_applied.append(migration.version)
    return newly_applied
