"""
harness.schema_cache - Global ABCP capability schema cache helpers.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, Iterable, Optional, Set

from harness.utils import JsonDict


GLOBAL_SCHEMA_CACHE_DIR = "global_schema_cache"
CAPABILITY_HASH_FILE = "capability_hash.json"
AGENT_GUIDE_FILE = "agent_guide.md"
SCHEMAS_DIR = "schemas"
SCHEMA_BOOTSTRAP_LOCK_DIR = ".bootstrap.lock"

# Bump when a known platform schema generation changed for methods whose
# System.getCapabilities entries (method name + description) may be unchanged
# - the capability-only digest cannot see describeAction-level schema
# changes, and a stale cached schema would then be served forever.
# 2026-08: Input.select / DOM.inspectSelect rebuilt (one-array contract),
# Download domain rebuilt (Download.start union, Download.control,
# File.download removed).
SCHEMA_CONTRACT_GENERATION = "2026-08-select-download-rebuild"


class SchemaCacheStatus(str, Enum):
    NOT_LOADED = "not_loaded"
    LOADED_EMPTY = "loaded_empty"
    LOADED_OK = "loaded_ok"


def global_schema_cache_dir(worktree_dir: str) -> Path:
    worktree = Path(worktree_dir or "worktree").expanduser()
    if not worktree.is_absolute():
        worktree = Path.cwd() / worktree
    return worktree.resolve(strict=False).parent / GLOBAL_SCHEMA_CACHE_DIR


def global_schemas_dir(worktree_dir: str) -> Path:
    return global_schema_cache_dir(worktree_dir) / SCHEMAS_DIR


def schema_bootstrap_lock_dir(cache_dir: Path) -> Path:
    return cache_dir / SCHEMA_BOOTSTRAP_LOCK_DIR


def capability_hash_path(cache_dir: Path) -> Path:
    return cache_dir / CAPABILITY_HASH_FILE


def write_cached_agent_guide(cache_dir: Path, guide: str) -> Optional[str]:
    value = str(guide or "")
    if not value.strip():
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / AGENT_GUIDE_FILE
    path.write_text(value, encoding="utf-8")
    return str(path.resolve())


def capability_hash(
    capabilities: Any,
    *,
    policy_fingerprint: Any = None,
    generation: Optional[str] = None,
    catalog_revision: Optional[str] = None,
) -> str:
    caps = capabilities if isinstance(capabilities, list) else []
    normalized = sorted(
        [item for item in caps if isinstance(item, dict)],
        key=lambda item: str(item.get("method") or ""),
    )
    # policy_fingerprint folds the harness-side method policy (e.g. the
    # blocked-methods set) into the digest so that un-banning/re-banning a method
    # changes the cache key even though the raw System.getCapabilities response is
    # unchanged. Without it, a cache built while a method was blocked would never
    # describe that method after it is un-banned -> Lead plan validation reports
    # "unknown method". Omitted (None) keeps the legacy capability-only hash.
    # `generation` additionally folds a known schema-contract generation in:
    # describeAction-level schema changes that leave capability entries
    # identical must still force a one-time full cache refresh.
    payload: Any = normalized
    if (
        policy_fingerprint is not None
        or generation is not None
        or catalog_revision is not None
    ):
        fingerprint = (
            sorted(str(item) for item in policy_fingerprint)
            if isinstance(policy_fingerprint, (set, frozenset, list, tuple))
            else str(policy_fingerprint)
            if policy_fingerprint is not None
            else None
        )
        payload = {
            "capabilities": normalized,
            "policy": fingerprint,
            "generation": generation,
            "catalogRevision": str(catalog_revision or "") or None,
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_cached_capability_hash(cache_dir: Path) -> Optional[str]:
    data = read_cached_capability_metadata(cache_dir)
    digest = data.get("hash") if isinstance(data, dict) else None
    return str(digest) if digest else None


def read_cached_capability_metadata(cache_dir: Path) -> JsonDict:
    """Read the cache manifest, accepting the legacy hash-only shape."""
    path = capability_hash_path(cache_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cached_capability_hash(
    cache_dir: Path,
    *,
    digest: str,
    capability_count: int,
    generation: Optional[str] = None,
    catalog_revision: str = "",
    guide_revision: str = "",
) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = capability_hash_path(cache_dir)
    path.write_text(
        json.dumps(
            {
                "hash": digest,
                "capability_count": capability_count,
                **({"generation": generation} if generation is not None else {}),
                **({"catalog_revision": catalog_revision} if catalog_revision else {}),
                **({"guide_revision": guide_revision} if guide_revision else {}),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(path.resolve())


def read_schema_methods_from_dirs(dirs: Iterable[Path]) -> Set[str]:
    methods: Set[str] = set()
    for schemas_dir in dirs:
        if not schemas_dir.exists() or not schemas_dir.is_dir():
            continue
        for path in schemas_dir.glob("*.json"):
            try:
                data: JsonDict = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            method = str(data.get("method") or path.stem).strip()
            if method:
                methods.add(method)
    return methods


def clear_schema_json_files(schemas_dir: Path) -> None:
    if not schemas_dir.exists():
        return
    for path in schemas_dir.glob("*.json"):
        try:
            path.unlink()
        except OSError:
            pass


@contextmanager
def schema_bootstrap_lock(
    cache_dir: Path,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.1,
) -> Iterator[bool]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = schema_bootstrap_lock_dir(cache_dir)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    acquired = False
    while True:
        try:
            lock_dir.mkdir()
            acquired = True
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.01, poll_interval_seconds))
    try:
        yield acquired
    finally:
        if acquired:
            shutil.rmtree(lock_dir, ignore_errors=True)
