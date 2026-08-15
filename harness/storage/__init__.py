"""
harness.storage - Task process data middle layer.

Replaces per-task JSONL/JSON files (run.jsonl, traces/, task_state.json,
offloaded observations) with a single SQLite database, behind an interface
business code can use without seeing SQL or a connection.

Read the module docstrings before adding to this package:

* ``sqlite_connection`` - why transactions are ``BEGIN IMMEDIATE`` and what
  may not appear inside one.
* ``dao.save_snapshot`` - how three-way merge and the revision CAS layer.
* ``base`` - the interface business modules are allowed to depend on.
"""

from __future__ import annotations

from harness.storage.base import (
    BACKEND_DB,
    BACKEND_DUAL,
    BACKEND_FILE,
    EXTERNAL_RESOURCE_TYPES,
    SNAPSHOT_KEY_CURRENT_PLAN,
    SNAPSHOT_KEY_TASK_STATE,
    VALID_BACKENDS,
    ResourceAccessError,
    RevisionConflictError,
    Storage,
    StorageCorruptError,
    StorageError,
    canonical_json,
)
from harness.storage.factory import (
    DEFAULT_SQLITE_PATH,
    create_storage,
    create_storage_from_config,
    normalize_backend,
    open_database,
    open_registry,
)
from harness.storage.migrations import SCHEMA_VERSION, apply_migrations
from harness.storage.sqlite_connection import (
    StorageBusyError,
    maybe_incremental_vacuum,
    write_transaction,
)

__all__ = [
    "BACKEND_DB",
    "BACKEND_DUAL",
    "BACKEND_FILE",
    "DEFAULT_SQLITE_PATH",
    "EXTERNAL_RESOURCE_TYPES",
    "SCHEMA_VERSION",
    "SNAPSHOT_KEY_CURRENT_PLAN",
    "SNAPSHOT_KEY_TASK_STATE",
    "VALID_BACKENDS",
    "ResourceAccessError",
    "RevisionConflictError",
    "Storage",
    "StorageBusyError",
    "StorageCorruptError",
    "StorageError",
    "apply_migrations",
    "canonical_json",
    "create_storage",
    "create_storage_from_config",
    "maybe_incremental_vacuum",
    "normalize_backend",
    "open_database",
    "open_registry",
    "write_transaction",
]
