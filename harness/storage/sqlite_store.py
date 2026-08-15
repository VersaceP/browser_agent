"""
harness.storage.sqlite_store - The SQLite-backed Storage implementation.

Two rules shape everything here:

* A resource is addressed by ``sqlite://tasks/<task_id>/resources/<id>`` and
  every read re-checks the task, because replacing filesystem paths removed
  the boundary that ``utils.resolve_task_file`` used to enforce.
* Search filters in SQL and then applies the caller's regex in Python.  SQLite
  has no regex engine, and a LIKE prefilter cannot be derived safely from an
  arbitrary pattern - ``foo|bar`` has no single literal to filter on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import uuid
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from harness.storage import dao
from harness.storage.base import (
    DEFAULT_RESOURCE_GLOB,
    EXTERNAL_RESOURCE_TYPES,
    glob_matches,
    glob_sql_prefilter,
    SNAPSHOT_KEY_CURRENT_PLAN,
    SNAPSHOT_KEY_TASK_STATE,
    ResourceAccessError,
    Storage,
    StorageError,
    normalize_external_path,
)
from harness.storage.migrations import SCHEMA_VERSION, apply_migrations
from harness.storage.resource_codec import (
    DEFAULT_COMPRESSION_LEVEL,
    DEFAULT_COMPRESSION_MIN_BYTES,
    ENCODING_IDENTITY,
    encode_resource,
    logical_text_from_row,
    restore_row_content,
)
from harness.storage.sqlite_connection import ConnectionRegistry, write_transaction
from harness.utils import JsonDict


RESOURCE_URI_SCHEME = "sqlite"
RESOURCE_URI_PREFIX = f"{RESOURCE_URI_SCHEME}://tasks/"

# Payloads above this move into task_resources so the event row stays small
# and event scans stay cheap. Measured p99 of a real run.jsonl line is ~26KB.
EVENT_PAYLOAD_OFFLOAD_THRESHOLD = 65536

RESOURCE_TYPE_EVENT_PAYLOAD = "event_payload"


def build_resource_uri(task_id: str, resource_id: str) -> str:
    return f"{RESOURCE_URI_PREFIX}{task_id}/resources/{resource_id}"


def parse_resource_uri(uri: str) -> Tuple[str, str]:
    """Return ``(task_id, resource_id)`` or raise.

    Parsing alone grants nothing: the caller still has to compare the task id
    against the one it is running as.
    """

    text = str(uri or "").strip()
    if not text.startswith(RESOURCE_URI_PREFIX):
        raise ResourceAccessError(f"not a task resource URI: {uri!r}")
    remainder = text[len(RESOURCE_URI_PREFIX):]
    parts = remainder.split("/")
    if len(parts) != 3 or parts[1] != "resources" or not parts[0] or not parts[2]:
        raise ResourceAccessError(f"malformed task resource URI: {uri!r}")
    return parts[0], parts[2]


def _git_sha() -> str:
    from harness.version import git_revision

    return git_revision()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validated_resource_compression(value: object) -> str:
    mode = str(value or "none").strip().lower()
    if mode not in {"none", "zlib"}:
        raise ValueError(
            f"resource_compression must be 'none' or 'zlib'; got {value!r}"
        )
    return mode


class SqliteStore(Storage):
    def __init__(
        self,
        database_path: Path | str,
        *,
        worktree_dir: str = "worktree",
        busy_timeout_ms: int = 5000,
        registry: Optional[ConnectionRegistry] = None,
        on_revision_conflict: Optional[Callable[[JsonDict], None]] = None,
        resource_compression: str = "zlib",
        resource_compression_min_bytes: int = DEFAULT_COMPRESSION_MIN_BYTES,
        resource_compression_level: int = DEFAULT_COMPRESSION_LEVEL,
    ) -> None:
        self.worktree_dir = Path(worktree_dir).expanduser()
        self.registry = registry or ConnectionRegistry(
            database_path, busy_timeout_ms=busy_timeout_ms
        )
        self.on_revision_conflict = on_revision_conflict
        # Fail-fast like storage_backend: a typo here would silently change
        # what future rows look like, and "none" is the rollback switch whose
        # byte-identical guarantee depends on the value being exact.
        self.resource_compression = _validated_resource_compression(resource_compression)
        self.resource_compression_min_bytes = max(0, int(resource_compression_min_bytes))
        self.resource_compression_level = max(0, min(9, int(resource_compression_level)))
        apply_migrations(self.registry.connection())

    @property
    def connection(self):
        return self.registry.connection()

    def task_dir(self, task_id: str) -> Path:
        return self.worktree_dir / task_id

    # -- task lifecycle ----------------------------------------------------
    def create_task(
        self,
        *,
        task_id: str,
        harness_version: str,
        snapshot: Optional[JsonDict] = None,
    ) -> JsonDict:
        return dao.insert_task(
            self.connection,
            task_id=task_id,
            harness_version=harness_version,
            schema_version=SCHEMA_VERSION,
            snapshot=snapshot,
        )

    def get_task(self, task_id: str, *, include_deleted: bool = False) -> Optional[JsonDict]:
        return dao.get_task(
            self.connection, task_id=task_id, include_deleted=include_deleted
        )

    def update_task_snapshot(self, task_id: str, snapshot: JsonDict) -> None:
        dao.update_task_snapshot(self.connection, task_id=task_id, snapshot=snapshot)

    def soft_delete_task(self, task_id: str) -> bool:
        return dao.soft_delete_task(self.connection, task_id=task_id)

    def list_tasks(self, *, include_deleted: bool = False, limit: int = 50) -> List[JsonDict]:
        return dao.list_tasks(
            self.connection, include_deleted=include_deleted, limit=limit
        )

    # -- run lifecycle -----------------------------------------------------
    def start_run(
        self,
        *,
        task_id: str,
        harness_version: str,
        run_id: Optional[str] = None,
    ) -> JsonDict:
        record = dao.start_run(
            self.connection,
            task_id=task_id,
            harness_version=harness_version,
            run_id=run_id,
            process_id=os.getpid(),
            host_name=socket.gethostname(),
            git_sha=_git_sha(),
        )
        dao.touch_task_run(
            self.connection, task_id=task_id, harness_version=harness_version
        )
        return record

    def finish_run(
        self,
        *,
        task_id: str,
        run_id: str,
        status: str,
        error: Optional[JsonDict] = None,
    ) -> None:
        dao.finish_run(
            self.connection,
            task_id=task_id,
            run_id=run_id,
            status=status,
            error=error,
        )

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
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        byte_size = len(payload_json.encode("utf-8"))
        connection = self.connection
        if byte_size <= EVENT_PAYLOAD_OFFLOAD_THRESHOLD:
            dao.insert_event(
                connection,
                task_id=task_id, run_id=run_id, event_type=event_type,
                payload_json=payload_json, payload_resource_id=None,
                payload_byte_size=byte_size,
                actor_type=actor_type, worker_id=worker_id,
            )
            return

        # Oversized payloads still live in the database, just not inline:
        # keeping them out of run_events is what makes event scans cheap. The
        # resource and the event that points at it commit together, or a crash
        # between them would leave an orphan resource and a lost event.
        resource_id = uuid.uuid4().hex
        # Encoded before the transaction opens (as everything here is): the
        # write lock must never be held across compression, which costs up
        # to ~100 ms on the largest live payloads.
        encoded = encode_resource(
            payload,
            resource_type=RESOURCE_TYPE_EVENT_PAYLOAD,
            compression=self.resource_compression,
            min_bytes=self.resource_compression_min_bytes,
            level=self.resource_compression_level,
        )
        with write_transaction(connection):
            self._insert_resource_rows(
                connection, task_id, run_id, resource_id,
                RESOURCE_TYPE_EVENT_PAYLOAD,
                f"events/{event_type}-{resource_id[:12]}.json",
                "application/json",
                {
                    "content_json": encoded.content_json,
                    "content_text": encoded.content_text,
                    "content_blob": encoded.content_blob,
                    "external_path": None,
                },
                {}, encoded.logical_byte_size, encoded.logical_sha256,
                encoded.stored_byte_size,
                content_encoding=encoded.content_encoding,
            )
            connection.execute(
                "INSERT INTO run_events("
                " task_id, run_id, event_time, event_type, actor_type, worker_id,"
                " payload_json, payload_resource_id, payload_byte_size)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    task_id, run_id, dao.utc_now_iso(), event_type,
                    actor_type, worker_id, resource_id, byte_size,
                ),
            )

    def read_events(
        self,
        *,
        task_id: str,
        after_event_id: int = 0,
        limit: int = 200,
        event_type: Optional[str] = None,
    ) -> List[JsonDict]:
        return dao.read_events(
            self.connection,
            task_id=task_id,
            after_event_id=after_event_id,
            limit=limit,
            event_type=event_type,
        )

    # -- snapshots ---------------------------------------------------------
    def load_snapshot(self, *, task_id: str, snapshot_key: str) -> Tuple[JsonDict, int]:
        return dao.load_snapshot(
            self.connection, task_id=task_id, snapshot_key=snapshot_key
        )

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
        return dao.save_snapshot(
            self.connection,
            task_id=task_id,
            snapshot_key=snapshot_key,
            base=base,
            proposed=proposed,
            updated_run_id=updated_run_id,
            merge=merge,
            replace=replace,
            on_conflict=self.on_revision_conflict,
        )

    # -- plan history ------------------------------------------------------
    def save_plan_version(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        """Allocate the next version and store the record under it.

        Version allocation happens inside the write transaction so two writers
        cannot be handed the same number; the file backend gets the same
        guarantee from its directory scan only because a single process writes.
        """

        stored = dict(record)
        connection = self.connection
        with write_transaction(connection):
            # An incoming planVersion wins: in dual mode the file backend has
            # already allocated it, and recomputing here would let the two
            # ledgers drift into numbering the same plan differently.
            supplied = stored.get("planVersion")
            if supplied:
                version = int(supplied)
            else:
                row = connection.execute(
                    "SELECT COALESCE(MAX(plan_version), 0) + 1 FROM task_plan_versions"
                    " WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                version = int(row[0] or 1)
            stored["planVersion"] = version
            stored["previousVersion"] = version - 1 if version > 1 else None
            self._insert_plan_version_row(connection, task_id, run_id, stored)
        stored["path"] = build_resource_uri(task_id, f"plan-version-{version}")
        return stored

    @staticmethod
    def _insert_plan_version_row(connection, task_id: str, run_id: str, stored: JsonDict) -> None:
        connection.execute(
            "INSERT INTO task_plan_versions("
            " task_id, plan_version, run_id, accepted_at, plan_hash,"
            " previous_plan_version, replan_reason, plan_json, diff_json,"
            " validator_review_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                int(stored["planVersion"]),
                run_id,
                str(stored.get("acceptedAt") or dao.utc_now_iso()),
                str(stored.get("planHash") or ""),
                stored.get("previousVersion"),
                stored.get("replanReason"),
                json.dumps(stored.get("plan") or {}, ensure_ascii=False, default=str),
                json.dumps(stored.get("diff") or [], ensure_ascii=False, default=str),
                (
                    json.dumps(stored["validatorReview"], ensure_ascii=False, default=str)
                    if stored.get("validatorReview") is not None
                    else None
                ),
            ),
        )

    def load_plan_version(self, *, task_id: str, version: int) -> Optional[JsonDict]:
        row = self.connection.execute(
            "SELECT * FROM task_plan_versions WHERE task_id = ? AND plan_version = ?",
            (task_id, int(version)),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        return {
            "planVersion": int(record["plan_version"]),
            "acceptedAt": record["accepted_at"],
            "planHash": record["plan_hash"],
            "previousVersion": record["previous_plan_version"],
            "replanReason": record["replan_reason"],
            "plan": json.loads(record["plan_json"] or "{}"),
            "diff": json.loads(record["diff_json"] or "[]"),
            "validatorReview": (
                json.loads(record["validator_review_json"])
                if record["validator_review_json"]
                else None
            ),
        }

    def save_plan_review(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        stored = dict(record)
        connection = self.connection
        with write_transaction(connection):
            supplied = stored.get("reviewSequence")
            if supplied:
                sequence = int(supplied)
            else:
                row = connection.execute(
                    "SELECT COALESCE(MAX(review_sequence), 0) + 1 FROM task_plan_reviews"
                    " WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                sequence = int(row[0] or 1)
            stored["reviewSequence"] = sequence
            review = stored.get("review") or {}
            connection.execute(
                "INSERT INTO task_plan_reviews("
                " task_id, run_id, review_sequence, reviewed_at, candidate_hash,"
                " replan_reason, decision, candidate_plan_json, review_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    run_id,
                    sequence,
                    str(stored.get("reviewedAt") or dao.utc_now_iso()),
                    str(stored.get("candidateHash") or ""),
                    stored.get("replanReason"),
                    str(review.get("status") or review.get("decision") or "unknown"),
                    json.dumps(stored.get("candidatePlan") or {}, ensure_ascii=False, default=str),
                    json.dumps(review, ensure_ascii=False, default=str),
                ),
            )
        stored["path"] = build_resource_uri(task_id, f"plan-review-{sequence}")
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
        """Version row, current-plan alias and reset state in one transaction.

        No compare-and-swap here: accepting a plan deliberately rebuilds the
        whole generation, and the version allocated inside this transaction is
        stamped into the state before either is written, so the two can never
        disagree about which generation they describe.
        """

        from harness.storage.file_store import _stamp_plan_version

        stored = dict(plan_record)
        connection = self.connection
        with write_transaction(connection):
            supplied = stored.get("planVersion")
            if supplied:
                version = int(supplied)
            else:
                row = connection.execute(
                    "SELECT COALESCE(MAX(plan_version), 0) + 1 FROM task_plan_versions"
                    " WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                version = int(row[0] or 1)
            stored["planVersion"] = version
            stored["previousVersion"] = version - 1 if version > 1 else None
            self._insert_plan_version_row(connection, task_id, run_id, stored)

            persisted = _stamp_plan_version(task_state, stored)
            timestamp = dao.utc_now_iso()
            for key, value in (
                (SNAPSHOT_KEY_CURRENT_PLAN, current_plan),
                (SNAPSHOT_KEY_TASK_STATE, persisted),
            ):
                connection.execute(
                    "INSERT INTO task_snapshots("
                    " task_id, snapshot_key, value_json, revision, updated_at, updated_run_id)"
                    " VALUES (?, ?, ?, 1, ?, ?)"
                    " ON CONFLICT(task_id, snapshot_key) DO UPDATE SET"
                    "   value_json = excluded.value_json,"
                    "   revision = task_snapshots.revision + 1,"
                    "   updated_at = excluded.updated_at,"
                    "   updated_run_id = excluded.updated_run_id",
                    (
                        task_id,
                        key,
                        json.dumps(value, ensure_ascii=False, default=str),
                        timestamp,
                        run_id,
                    ),
                )
            if summarize is not None:
                # In the same transaction as the state it describes: a listing
                # must never show a summary for a generation that rolled back.
                connection.execute(
                    "UPDATE tasks SET snapshot_json = ?"
                    " WHERE task_id = ? AND is_deleted = 0",
                    (json.dumps(summarize(persisted), ensure_ascii=False, default=str),
                     task_id),
                )
        stored["path"] = build_resource_uri(task_id, f"plan-version-{version}")
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
        """Store a resource, superseding whatever currently holds that path.

        The previous version keeps its row and its id, so a historical
        ``run_events.payload_resource_id`` still resolves to the bytes that
        event actually referred to.
        """

        resource_id = uuid.uuid4().hex
        metadata = dict(metadata or {})
        columns = {
            "content_json": None,
            "content_text": None,
            "content_blob": None,
            "external_path": None,
        }
        byte_size: Optional[int] = None
        digest: Optional[str] = None
        # Only JSON content is stored in a different shape than it reads back;
        # for every other kind the stored bytes are the logical bytes.
        stored_byte_size: Optional[int] = None
        content_encoding = ENCODING_IDENTITY

        if external_path is not None:
            normalized, unmanaged, probe = normalize_external_path(
                self.task_dir(task_id), external_path
            )
            columns["external_path"] = normalized
            metadata["mutable_external"] = True
            if unmanaged:
                # Outside the task directory: never delete it during a purge.
                metadata["external_unmanaged"] = True
            if probe.is_file():
                try:
                    raw = probe.read_bytes()
                    byte_size, digest = len(raw), _sha256(raw)
                except OSError as exc:
                    metadata["hash_unavailable"] = str(exc)
            else:
                # The receipt can arrive before the file is readable; size and
                # hash stay NULL rather than being invented.
                metadata["hash_unavailable"] = "file not readable at record time"
        elif isinstance(content, bytes):
            # Binary stays binary: already opaque to readers, so compressing
            # it would only blur the line between "the harness captured
            # text" and "the harness captured bytes".
            columns["content_blob"] = content
            byte_size, digest = len(content), _sha256(content)
        else:
            # Text and JSON both go through the one encoder, which decides
            # compression, measures the logical bytes and hashes them - all
            # before the transaction below opens. Size and digest describe
            # the bytes on disk, the same number FileStore's st_size reports,
            # so the two backends never disagree about a resource's metadata.
            encoded = encode_resource(
                content,
                resource_type=resource_type,
                compression=self.resource_compression,
                min_bytes=self.resource_compression_min_bytes,
                level=self.resource_compression_level,
            )
            columns["content_json"] = encoded.content_json
            columns["content_text"] = encoded.content_text
            columns["content_blob"] = encoded.content_blob
            byte_size = encoded.logical_byte_size
            digest = encoded.logical_sha256
            stored_byte_size = encoded.stored_byte_size
            content_encoding = encoded.content_encoding

        connection = self.connection
        with write_transaction(connection):
            version = self._insert_resource_rows(
                connection, task_id, run_id, resource_id, resource_type,
                logical_path, media_type, columns, metadata, byte_size, digest,
                stored_byte_size, content_encoding=content_encoding,
            )
        return self._resource_result(
            resource_id, task_id, run_id, resource_type, logical_path,
            byte_size, digest, version,
        )

    def _insert_resource_rows(
        self, connection, task_id, run_id, resource_id, resource_type,
        logical_path, media_type, columns, metadata, byte_size, digest,
        stored_byte_size=None, content_encoding=ENCODING_IDENTITY,
    ) -> int:
        """Write one resource version. Caller owns the transaction."""

        if True:
            previous = connection.execute(
                "SELECT resource_id, resource_version FROM task_resources"
                " WHERE task_id = ? AND run_id = ? AND logical_path = ? AND is_current = 1",
                (task_id, run_id, logical_path),
            ).fetchone()
            version = 1
            supersedes = None
            if previous is not None:
                version = int(previous["resource_version"]) + 1
                supersedes = previous["resource_id"]
                connection.execute(
                    "UPDATE task_resources SET is_current = 0 WHERE resource_id = ?",
                    (supersedes,),
                )
            connection.execute(
                "INSERT INTO task_resources("
                " resource_id, task_id, run_id, resource_type, logical_path, media_type,"
                " content_encoding,"
                " content_json, content_text, content_blob, external_path,"
                " metadata_json, byte_size, stored_byte_size, sha256, created_at,"
                " resource_version, is_current, supersedes_resource_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    resource_id,
                    task_id,
                    run_id,
                    resource_type,
                    logical_path,
                    media_type,
                    content_encoding,
                    columns["content_json"],
                    columns["content_text"],
                    columns["content_blob"],
                    columns["external_path"],
                    json.dumps(metadata, ensure_ascii=False, default=str),
                    byte_size,
                    stored_byte_size,
                    digest,
                    dao.utc_now_iso(),
                    version,
                    supersedes,
                ),
            )
        return version

    @staticmethod
    def _resource_result(
        resource_id, task_id, run_id, resource_type, logical_path,
        byte_size, digest, version: int = 1,
    ) -> JsonDict:
        return {
            "resource_id": resource_id,
            "task_id": task_id,
            "run_id": run_id,
            "resource_type": resource_type,
            "logical_path": logical_path,
            "resource_version": version,
            "byte_size": byte_size,
            "sha256": digest,
            "saved_path": build_resource_uri(task_id, resource_id),
            "relativePath": logical_path,
        }

    def _normalize_external_path(self, task_id: str, external_path: str) -> Tuple[str, bool]:
        """Prefer a task-relative path; flag anything outside as unmanaged."""

        normalized, unmanaged, _resolved = normalize_external_path(
            self.task_dir(task_id), external_path
        )
        return normalized, unmanaged

    def read_resource(self, *, current_task_id: str, resource_uri: str) -> Optional[JsonDict]:
        """Read one resource. A URI naming another task is refused outright."""

        uri_task_id, resource_id = parse_resource_uri(resource_uri)
        if uri_task_id != current_task_id:
            raise ResourceAccessError(
                f"resource {resource_id} belongs to task {uri_task_id},"
                f" not the running task {current_task_id}"
            )
        # The task id is repeated in the WHERE clause on purpose: a globally
        # unique resource id must never be sufficient on its own.
        row = self.connection.execute(
            "SELECT * FROM task_resources WHERE task_id = ? AND resource_id = ?",
            (current_task_id, resource_id),
        ).fetchone()
        if row is None:
            return None
        # The codec restores the logical content into the column it came from
        # and drops the physical blob, so every existing consumer of
        # content_json / content_text sees what it saw before compression -
        # including a decode failure surfacing as StorageCorruptError here
        # rather than as a silent fallback to some other copy.
        record = restore_row_content(dict(row))
        record["saved_path"] = build_resource_uri(current_task_id, resource_id)
        if record.get("external_path"):
            record.update(self._reread_external(current_task_id, record))
        return record

    def _reread_external(self, task_id: str, record: JsonDict) -> JsonDict:
        """Re-hash an external file so drift is visible rather than assumed.

        Download targets can be rewritten in place by a later run; the harness
        does not control the save path, so the honest guarantee is
        "intact as of this read", not immutability.
        """

        stored_hash = record.get("sha256")
        candidate = Path(record["external_path"])
        if not candidate.is_absolute():
            candidate = self.task_dir(task_id) / candidate
        if not candidate.is_file():
            return {"content_available": False, "content_drifted": bool(stored_hash)}
        try:
            raw = candidate.read_bytes()
        except OSError:
            return {"content_available": False, "content_drifted": False}
        current_hash = _sha256(raw)
        return {
            "content_available": True,
            "current_sha256": current_hash,
            "content_drifted": bool(stored_hash) and current_hash != stored_hash,
            "resolved_path": str(candidate),
        }

    def search_resources(
        self,
        *,
        task_id: str,
        path_glob: str = DEFAULT_RESOURCE_GLOB,
        pattern: Optional[str] = None,
        max_results: int = 20,
        scan_batch: int = 200,
    ) -> List[JsonDict]:
        """SQL narrows the candidate set; Python decides the regex match.

        No LIKE prefilter is derived from ``pattern``: an alternation such as
        ``foo|bar`` has no single literal that must appear, so prefiltering on
        one branch would silently drop the other's hits.
        """

        # MULTILINE because the text being searched is a file: a caller's ^ and
        # $ mean line boundaries, the same as they do in local_fs_search, which
        # gets it for free by matching one line at a time.
        regex = re.compile(pattern, re.MULTILINE) if pattern else None
        # SQL narrows, Python decides. SQLite's GLOB and the shared matcher do
        # not agree on ``**/`` - GLOB requires a literal "/" and so drops files
        # at the task root - so the prefilter is widened to a guaranteed
        # superset and the exact rule is applied to each row.
        matcher = path_glob or DEFAULT_RESOURCE_GLOB
        sql_glob = glob_sql_prefilter(matcher)
        results: List[JsonDict] = []
        # Keyed on the path, not the rowid: the file backend walks the tree in
        # path order, and with max_results the two backends otherwise return
        # different first-N results for the same query. The rowid stays as the
        # tiebreaker so the cursor is still unique.
        cursor: Tuple[str, int] = ("", 0)
        while len(results) < max_results:
            rows = self.connection.execute(
                "SELECT r.rowid AS rowid, r.resource_id AS resource_id,"
                " r.logical_path AS logical_path, r.resource_type AS resource_type,"
                " r.media_type AS media_type, r.byte_size AS byte_size,"
                " r.sha256 AS sha256, r.created_at AS created_at,"
                " r.content_json AS content_json, r.content_text AS content_text,"
                " r.content_blob AS content_blob,"
                " r.content_encoding AS content_encoding,"
                " r.external_path AS external_path"
                " FROM task_resources AS r"
                " JOIN task_runs AS run"
                "   ON run.task_id = r.task_id AND run.run_id = r.run_id"
                " WHERE r.task_id = ? AND r.is_current = 1"
                "   AND (r.logical_path, r.rowid) > (?, ?)"
                "   AND r.logical_path GLOB ?"
                # Same current-version rule the virtual view uses: a resumed
                # task holds one row per run for a path, and search must agree
                # with what a read of that path returns.
                "   AND run.run_number = ("
                "     SELECT MAX(ir.run_number) FROM task_resources AS ur"
                "     JOIN task_runs AS ir ON ir.task_id = ur.task_id AND ir.run_id = ur.run_id"
                "     WHERE ur.task_id = r.task_id AND ur.logical_path = r.logical_path"
                "       AND ur.is_current = 1)"
                " ORDER BY r.logical_path, r.rowid LIMIT ?",
                (task_id, cursor[0], cursor[1], sql_glob, int(scan_batch)),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                cursor = (str(row["logical_path"]), int(row["rowid"]))
                if not glob_matches(matcher, str(row["logical_path"])):
                    continue
                # Match against the logical file, not the stored column: the
                # caller's pattern was written for the text a read returns,
                # and anchors like ^ mean nothing against compact JSON. The
                # codec owns that rendering - and the decompression it may
                # take to get there.
                text = logical_text_from_row(row) or ""
                if regex is not None and not regex.search(text):
                    continue
                results.append({
                    "resource_id": row["resource_id"],
                    "task_id": task_id,
                    "logical_path": row["logical_path"],
                    "resource_type": row["resource_type"],
                    "byte_size": row["byte_size"],
                    "saved_path": build_resource_uri(task_id, row["resource_id"]),
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
        return dao.insert_trace_events(
            self.connection,
            task_id=task_id,
            run_id=run_id,
            worker_id=worker_id,
            entries=list(entries),
        )

    def list_worker_trace(
        self,
        *,
        task_id: str,
        run_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[JsonDict]:
        return dao.list_trace_events(
            self.connection,
            task_id=task_id,
            run_id=run_id,
            worker_id=worker_id,
            limit=limit,
        )

    # -- strategy telemetry ------------------------------------------------
    def append_strategy_attempt(
        self,
        *,
        task_id: str,
        run_id: str,
        payload: JsonDict,
    ) -> None:
        dao.insert_strategy_attempt(
            self.connection, task_id=task_id, run_id=run_id, payload=payload
        )

    def list_strategy_attempts(
        self,
        *,
        task_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[JsonDict]:
        return dao.list_strategy_attempts(self.connection, task_id=task_id, limit=limit)

    def close(self) -> None:
        self.registry.close_all()
