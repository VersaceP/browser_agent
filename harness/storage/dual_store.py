"""
harness.storage.dual_store - Write to both backends, read from files, compare.

This is the de-risking mode: files stay authoritative and keep the harness
running exactly as before, while the database accumulates the same writes so
they can be checked against each other before anything switches over.

Two rules make the comparison meaningful:

* Equality is judged on ``canonical_json`` of the same Python object, never on
  raw file bytes. The two backends serialise independently, so key order and
  spacing differ for reasons that carry no meaning and every naive byte
  comparison would report a mismatch.
* A database failure never propagates. In this mode SQLite is the one being
  evaluated, so its errors are recorded as findings rather than allowed to
  take down a task that the file backend already handled correctly.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from harness.storage.sqlite_store import RESOURCE_TYPE_EVENT_PAYLOAD
from harness.storage.resource_codec import (
    logical_text_from_row,
    normalize_text_file,
)
from harness.storage.base import (
    DEFAULT_RESOURCE_GLOB,
    SNAPSHOT_KEY_CURRENT_PLAN,
    SNAPSHOT_KEY_TASK_STATE,
    Storage,
    canonical_json,
    normalize_external_path,
)
from harness.utils import JsonDict


VerifySink = Callable[[JsonDict], None]


def semantic_sha256(value: Any) -> str:
    """Hash of the canonical form, so equal content hashes equally."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()



def _event_sequence_digest(rows: List[JsonDict]) -> str:
    """Digest the ordered (type, actor, worker, payload) tuples of a run.

    Payload is hashed through canonical_json so key order and spacing - which
    differ freely between an appended JSONL line and a stored column - cannot
    register as a mismatch, while a genuine content change does.
    """

    parts: List[Any] = []
    for row in rows:
        payload = row.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                pass
        parts.append([
            str(row.get("event_type") or ""),
            str(row.get("actor_type") or ""),
            str(row.get("worker_id") or ""),
            semantic_sha256(payload),
        ])
    return semantic_sha256(parts)


def _resource_fingerprint(
    task_dir: Path,
    logical_path: str,
    resource_type: str,
    content: Any,
    external_path: Optional[str],
) -> JsonDict:
    """What both backends must hold for one resource write.

    ``kind`` is recorded because the two sides store the same value in
    different shapes - a dict becomes a JSON file on one side and a
    ``content_json`` column on the other - and the digest can only be
    recomputed from either shape if the reader knows which it started as.
    """

    if content is None:
        normalized, _unmanaged, _resolved = normalize_external_path(
            task_dir, str(external_path or "")
        )
        return {
            "path": logical_path, "type": resource_type, "kind": "external",
            "digest": semantic_sha256(normalized),
        }
    if isinstance(content, (dict, list)):
        kind, digest = "json", semantic_sha256(content)
    elif isinstance(content, bytes):
        kind, digest = "bytes", hashlib.sha256(content).hexdigest()
    else:
        # Normalised, because the disk side of this comparison is read back
        # through Path.read_text and a CRLF file arrives there as LF.
        kind, digest = "text", semantic_sha256(normalize_text_file(str(content)))
    return {"path": logical_path, "type": resource_type, "kind": kind, "digest": digest}


def _strategy_fingerprint(payload: JsonDict) -> JsonDict:
    """Digest a strategy attempt twice, once per backend's storage shape.

    The file backend appends the whole payload; the table keeps a projection
    of it in typed columns. Hashing the payload alone would make every
    database row look wrong, and hashing the projection alone would let the
    file copy drift - so both are recorded and each side is checked against
    the one it actually promises to preserve.
    """

    return {
        "payload": semantic_sha256(payload),
        "projected": semantic_sha256([
            payload.get("phaseId"),
            payload.get("workerId"),
            payload.get("strategy_ids") or [],
            payload.get("status"),
            payload.get("statusCategory"),
            payload.get("validatedStatus"),
            payload.get("failureClassification"),
            payload.get("rowCount"),
            int(payload.get("artifactCount") or 0),
        ]),
    }


def _strategy_row_projection(row: JsonDict) -> str:
    ids = row.get("strategy_ids_json")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except (TypeError, ValueError):
            ids = []
    return semantic_sha256([
        row.get("phase_id"),
        row.get("worker_id"),
        ids or [],
        row.get("status"),
        row.get("status_category"),
        row.get("validated_status"),
        row.get("failure_classification"),
        row.get("row_count"),
        int(row.get("artifact_count") or 0),
    ])


def _trace_digest(rows: List[JsonDict]) -> str:
    parts: List[Any] = []
    for row in rows:
        body = row.get("trace_json")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (TypeError, ValueError):
                pass
        parts.append([str(row.get("worker_id") or ""), semantic_sha256(body)])
    return semantic_sha256(parts)


# How many differing entries a failed check names before it stops listing them.
# Enough to identify what went wrong; short enough that a report stays a log
# line rather than a payload.
VERIFY_SAMPLE_LIMIT = 20


def _summarize_check(name: str, expected: Any, actual: Any) -> JsonDict:
    """Record a check's verdict without recording the evidence it used.

    The digest lists behind an events or trace check run to thousands of
    entries. Writing them out made a clean report the single largest object in
    the run - 2 MB of SHA-256 that nobody reads when the answer is "ok". A
    count and an aggregate digest carry the same verdict, and a failure still
    names where and what differs, bounded.
    """

    ok = expected == actual
    entry: JsonDict = {"check": name, "ok": ok}
    if not isinstance(expected, list) or not isinstance(actual, list):
        entry["expected"] = expected
        entry["actual"] = actual
        return entry

    entry["expectedCount"] = len(expected)
    entry["actualCount"] = len(actual)
    entry["expectedDigest"] = semantic_sha256(expected)
    entry["actualDigest"] = semantic_sha256(actual)
    if ok:
        return entry

    missing = Counter(map(_hashable, expected)) - Counter(map(_hashable, actual))
    extra = Counter(map(_hashable, actual)) - Counter(map(_hashable, expected))
    # Three separate numbers because they answer different questions and a
    # single "diffCount" contradicts itself: a pure reordering has nothing
    # missing and nothing extra, yet every position differs.  The scan runs to
    # the longer length so a truncated side reports where it ran out rather
    # than reporting no differing positions at all.
    positions = [
        index for index in range(max(len(expected), len(actual)))
        if expected[index:index + 1] != actual[index:index + 1]
    ]
    entry["positionalDiffCount"] = len(positions)
    entry["missingCount"] = sum(missing.values())
    entry["extraCount"] = sum(extra.values())
    entry["firstDiffPositions"] = positions[:VERIFY_SAMPLE_LIMIT]
    entry["missingSample"] = list(missing.elements())[:VERIFY_SAMPLE_LIMIT]
    entry["extraSample"] = list(extra.elements())[:VERIFY_SAMPLE_LIMIT]
    return entry


def _hashable(value: Any) -> Any:
    """Counter keys must hash; check entries are usually strings but not always."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return canonical_json(value)


class DualStore(Storage):
    def __init__(
        self,
        primary: Storage,
        secondary: Storage,
        *,
        verify: bool = True,
        on_verify: Optional[VerifySink] = None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.verify_enabled = verify
        # The sink must not route back into this store: reporting a mismatch
        # by writing an event would itself be a dual write, which would be
        # verified, which would report again.
        self.on_verify = on_verify
        self._lock = threading.Lock()
        self._counts: Dict[Tuple[str, str], int] = {}
        self._write_errors: List[JsonDict] = []
        # What this process actually wrote, keyed by (task, run). Comparing the
        # two backends to each other cannot detect a row that BOTH are missing,
        # and cannot tell this run's data apart from a previous run's files in
        # a worktree that was never imported. Both need a ground truth.
        self._written: Dict[Tuple[str, str], Dict[str, List[Any]]] = {}

    # -- plumbing ----------------------------------------------------------
    def _count(self, task_id: str, kind: str, delta: int = 1) -> None:
        if not self.verify_enabled:
            return
        with self._lock:
            key = (task_id, kind)
            self._counts[key] = self._counts.get(key, 0) + delta

    def _record_written(self, task_id: str, run_id: str, kind: str, digest: Any) -> None:
        if not self.verify_enabled:
            return
        with self._lock:
            bucket = self._written.setdefault((task_id, str(run_id or "")), {})
            bucket.setdefault(kind, []).append(digest)

    def _expected(self, task_id: str, run_id: Optional[str], kind: str) -> List[Any]:
        with self._lock:
            if run_id is not None:
                return list(self._written.get((task_id, str(run_id)), {}).get(kind, []))
            merged: List[Any] = []
            for (task, _run), bucket in self._written.items():
                if task == task_id:
                    merged.extend(bucket.get(kind, []))
            return merged

    def _mirror(
        self,
        operation: str,
        task_id: str,
        call: Callable[[], Any],
        *,
        default: Any = None,
    ) -> Any:
        """Run a secondary operation, downgrading any failure to a finding.

        Used for reads during verification too: a broken database is exactly
        when the report is worth having, so verify() must survive it rather
        than raise on the way to producing evidence.
        """

        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
            with self._lock:
                self._write_errors.append({
                    "operation": operation,
                    "taskId": task_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            return default

    # -- task lifecycle ----------------------------------------------------
    def create_task(
        self,
        *,
        task_id: str,
        harness_version: str,
        snapshot: Optional[JsonDict] = None,
    ) -> JsonDict:
        result = self.primary.create_task(
            task_id=task_id, harness_version=harness_version, snapshot=snapshot
        )
        self._mirror("create_task", task_id, lambda: self.secondary.create_task(
            task_id=task_id, harness_version=harness_version, snapshot=snapshot
        ))
        return result

    def get_task(self, task_id: str, *, include_deleted: bool = False) -> Optional[JsonDict]:
        return self.primary.get_task(task_id, include_deleted=include_deleted)

    def update_task_snapshot(self, task_id: str, snapshot: JsonDict) -> None:
        self.primary.update_task_snapshot(task_id, snapshot)
        self._mirror(
            "update_task_snapshot", task_id,
            lambda: self.secondary.update_task_snapshot(task_id, snapshot),
        )

    def soft_delete_task(self, task_id: str) -> bool:
        # Only the database can represent this; the file backend refuses.
        return bool(self._mirror(
            "soft_delete_task", task_id,
            lambda: self.secondary.soft_delete_task(task_id),
        ))

    # -- run lifecycle -----------------------------------------------------
    def start_run(
        self,
        *,
        task_id: str,
        harness_version: str,
        run_id: Optional[str] = None,
    ) -> JsonDict:
        record = self.primary.start_run(
            task_id=task_id, harness_version=harness_version, run_id=run_id
        )
        # Reuse the primary's run_id so both ledgers agree on identity.
        self._mirror("start_run", task_id, lambda: self.secondary.start_run(
            task_id=task_id,
            harness_version=harness_version,
            run_id=record["run_id"],
        ))
        return record

    def finish_run(
        self,
        *,
        task_id: str,
        run_id: str,
        status: str,
        error: Optional[JsonDict] = None,
    ) -> None:
        self.primary.finish_run(
            task_id=task_id, run_id=run_id, status=status, error=error
        )
        self._mirror("finish_run", task_id, lambda: self.secondary.finish_run(
            task_id=task_id, run_id=run_id, status=status, error=error
        ))

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
        self.primary.append_event(
            task_id=task_id, run_id=run_id, event_type=event_type,
            payload=payload, actor_type=actor_type, worker_id=worker_id,
        )
        self._mirror("append_event", task_id, lambda: self.secondary.append_event(
            task_id=task_id, run_id=run_id, event_type=event_type,
            payload=payload, actor_type=actor_type, worker_id=worker_id,
        ))
        self._record_written(
            task_id, run_id, "events",
            semantic_sha256([event_type, actor_type or "", worker_id or "", payload]),
        )
        self._count(task_id, "events")

    def read_events(
        self,
        *,
        task_id: str,
        after_event_id: int = 0,
        limit: int = 200,
        event_type: Optional[str] = None,
    ) -> List[JsonDict]:
        return self.primary.read_events(
            task_id=task_id, after_event_id=after_event_id,
            limit=limit, event_type=event_type,
        )

    # -- snapshots ---------------------------------------------------------
    def load_snapshot(self, *, task_id: str, snapshot_key: str) -> Tuple[JsonDict, int]:
        return self.primary.load_snapshot(task_id=task_id, snapshot_key=snapshot_key)

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
        persisted, revision = self.primary.save_snapshot(
            task_id=task_id, snapshot_key=snapshot_key, base=base,
            proposed=proposed, updated_run_id=updated_run_id,
            merge=merge, replace=replace,
        )
        # The secondary receives the value the primary actually persisted, as
        # a replace. Re-running the merge there could reach a different result
        # from a different current value, and then the two would legitimately
        # disagree - which would tell us nothing about the database itself.
        self._mirror("save_snapshot", task_id, lambda: self.secondary.save_snapshot(
            task_id=task_id, snapshot_key=snapshot_key, base=None,
            proposed=persisted, updated_run_id=updated_run_id,
            merge=None, replace=True,
        ))
        self._count(task_id, f"snapshot:{snapshot_key}")
        return persisted, revision

    # -- plan history ------------------------------------------------------
    def save_plan_version(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        stored = self.primary.save_plan_version(
            task_id=task_id, run_id=run_id, record=record
        )
        # Hand the secondary the version the primary allocated, so both
        # ledgers number the same plan the same way.
        self._mirror("save_plan_version", task_id, lambda: self.secondary.save_plan_version(
            task_id=task_id, run_id=run_id, record=stored
        ))
        self._count(task_id, "plan_versions")
        return stored

    def load_plan_version(self, *, task_id: str, version: int) -> Optional[JsonDict]:
        return self.primary.load_plan_version(task_id=task_id, version=version)

    def save_plan_review(self, *, task_id: str, run_id: str, record: JsonDict) -> JsonDict:
        stored = self.primary.save_plan_review(
            task_id=task_id, run_id=run_id, record=record
        )
        self._mirror("save_plan_review", task_id, lambda: self.secondary.save_plan_review(
            task_id=task_id, run_id=run_id, record=stored
        ))
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
        stored, persisted = self.primary.commit_accepted_plan(
            task_id=task_id, run_id=run_id, plan_record=plan_record,
            current_plan=current_plan, task_state=task_state,
            summarize=summarize,
        )
        # The secondary receives the generation the primary settled on, version
        # number included, so the two ledgers cannot number it differently.
        self._mirror("commit_accepted_plan", task_id, lambda: self.secondary.commit_accepted_plan(
            task_id=task_id, run_id=run_id, plan_record=stored,
            current_plan=current_plan, task_state=persisted,
            summarize=summarize,
        ))
        self._count(task_id, "plan_versions")
        self._count(task_id, f"snapshot:{SNAPSHOT_KEY_TASK_STATE}")
        self._count(task_id, f"snapshot:{SNAPSHOT_KEY_CURRENT_PLAN}")
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
        result = self.primary.save_resource(
            task_id=task_id, run_id=run_id, resource_type=resource_type,
            logical_path=logical_path, content=content, media_type=media_type,
            external_path=external_path, metadata=metadata,
        )
        self._mirror("save_resource", task_id, lambda: self.secondary.save_resource(
            task_id=task_id, run_id=run_id, resource_type=resource_type,
            logical_path=logical_path, content=content, media_type=media_type,
            external_path=external_path, metadata=metadata,
        ))
        self._record_written(
            task_id, run_id, "resources",
            _resource_fingerprint(
                self.primary.task_dir(task_id),
                logical_path,
                resource_type,
                content,
                external_path,
            ),
        )
        self._count(task_id, "resources")
        return result

    def read_resource(self, *, current_task_id: str, resource_uri: str) -> Optional[JsonDict]:
        return self.primary.read_resource(
            current_task_id=current_task_id, resource_uri=resource_uri
        )

    def search_resources(
        self,
        *,
        task_id: str,
        path_glob: str = DEFAULT_RESOURCE_GLOB,
        pattern: Optional[str] = None,
        max_results: int = 20,
    ) -> List[JsonDict]:
        return self.primary.search_resources(
            task_id=task_id, path_glob=path_glob,
            pattern=pattern, max_results=max_results,
        )

    # -- worker traces -----------------------------------------------------
    def append_worker_trace(
        self,
        *,
        task_id: str,
        run_id: str,
        worker_id: str,
        entries: Sequence[JsonDict],
    ) -> int:
        written = self.primary.append_worker_trace(
            task_id=task_id, run_id=run_id, worker_id=worker_id, entries=entries
        )
        self._mirror("append_worker_trace", task_id, lambda: self.secondary.append_worker_trace(
            task_id=task_id, run_id=run_id, worker_id=worker_id, entries=entries
        ))
        for entry in entries:
            self._record_written(task_id, run_id, "trace", semantic_sha256(entry))
        self._count(task_id, "trace", written)
        return written

    def list_worker_trace(
        self,
        *,
        task_id: str,
        run_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[JsonDict]:
        return self.primary.list_worker_trace(
            task_id=task_id, run_id=run_id, worker_id=worker_id, limit=limit
        )

    # -- strategy telemetry ------------------------------------------------
    def append_strategy_attempt(
        self,
        *,
        task_id: str,
        run_id: str,
        payload: JsonDict,
    ) -> None:
        self.primary.append_strategy_attempt(
            task_id=task_id, run_id=run_id, payload=payload
        )
        self._mirror("append_strategy_attempt", task_id, lambda: self.secondary.append_strategy_attempt(
            task_id=task_id, run_id=run_id, payload=payload
        ))
        self._record_written(task_id, run_id, "strategy", _strategy_fingerprint(payload))
        self._count(task_id, "strategy")

    # -- verification ------------------------------------------------------
    def verify(self, *, task_id: str, run_id: Optional[str] = None) -> JsonDict:
        """Compare both backends against what this run actually wrote.

        Comparing the two to each other has two blind spots: a row missing
        from BOTH looks like agreement, and a worktree that predates the
        database legitimately has files from earlier runs that were never
        imported. Checking each side against this run's own write log fixes
        both, and makes ``run_id`` mean what it says.
        """

        checks: List[JsonDict] = []

        def record(name: str, expected: Any, actual: Any) -> None:
            checks.append(_summarize_check(name, expected, actual))

        scope = str(run_id) if run_id is not None else None

        file_events = self._events_for_run(self.primary, task_id, scope)
        db_events = self._mirror(
            "verify.read_events", task_id,
            lambda: self._events_for_run(self.secondary, task_id, scope), default=[],
        )
        expected_events = self._expected(task_id, scope, "events")
        record("events.file", expected_events, [self._event_digest(row) for row in file_events])
        record("events.db", expected_events, [self._event_digest(row, self.secondary) for row in db_events])

        for (task, kind), _count in sorted(self._counts.items()):
            if task != task_id or not kind.startswith("snapshot:"):
                continue
            key = kind.split(":", 1)[1]
            file_value, _ = self.primary.load_snapshot(task_id=task_id, snapshot_key=key)
            db_value = self._mirror(
                f"verify.load_snapshot.{key}", task_id,
                lambda k=key: self.secondary.load_snapshot(task_id=task_id, snapshot_key=k)[0],
                default=None,
            )
            record(f"snapshot.{key}.sha256",
                   semantic_sha256(file_value), semantic_sha256(db_value))

        expected_trace = sorted(self._expected(task_id, scope, "trace"))
        file_trace = self.primary.list_worker_trace(task_id=task_id, limit=100000)
        db_trace = self._mirror(
            "verify.list_worker_trace", task_id,
            lambda: self.secondary.list_worker_trace(
                task_id=task_id, run_id=scope, limit=100000
            ),
            default=[],
        )
        # The file backend cannot scope traces by run, so only require that
        # everything this run wrote is present rather than an exact match.
        # Counter, not set: two identical trace entries are two writes, and a
        # set difference would call one of them missing "already covered" by
        # the other.
        record("trace.file.contains", [], sorted((
            Counter(expected_trace)
            - Counter(self._trace_entry_digest(row) for row in file_trace)
        ).elements()))
        record("trace.db", expected_trace,
               sorted(self._trace_entry_digest(row) for row in db_trace))

        self._verify_resources(task_id, scope, record)
        self._verify_strategy(task_id, scope, record)

        for version in self._plan_versions_seen(task_id):
            file_record = self.primary.load_plan_version(task_id=task_id, version=version)
            db_record = self._mirror(
                f"verify.plan_version.{version}", task_id,
                lambda v=version: self.secondary.load_plan_version(task_id=task_id, version=v),
                default=None,
            )
            record(f"plan.v{version}.digest",
                   semantic_sha256((file_record or {}).get("plan")),
                   semantic_sha256((db_record or {}).get("plan")))

        with self._lock:
            write_errors = list(self._write_errors)

        failed = [check for check in checks if not check["ok"]]
        report: JsonDict = {
            "taskId": task_id,
            "runId": run_id,
            "status": "mismatch" if failed or write_errors else "ok",
            "checks": checks,
            "failedChecks": failed,
            "writeErrors": write_errors,
        }
        if self.on_verify is not None:
            self.on_verify(report)
        return report

    def _verify_resources(
        self, task_id: str, scope: Optional[str], record: Callable[[str, Any, Any], None]
    ) -> None:
        """Check both sides hold the content this run wrote, not just the path.

        Scoped to this run's writes in both directions. Sweeping every row the
        database holds would fail a resumed task for resources an earlier run
        wrote and later cleaned up, which says nothing about whether this run
        mirrored correctly.
        """

        # Last write wins: a path written twice in one run leaves one current
        # version, and only the final content is what both sides must hold.
        expected: Dict[str, JsonDict] = {}
        for entry in self._expected(task_id, scope, "resources"):
            if isinstance(entry, dict):
                expected[str(entry.get("path") or "")] = entry

        db_rows = self._mirror(
            "verify.search_resources", task_id,
            lambda: self.secondary.search_resources(
                task_id=task_id, path_glob=DEFAULT_RESOURCE_GLOB, max_results=100000
            ),
            default=[],
        )
        db_by_path = {
            str(row.get("logical_path") or ""): row for row in db_rows
            # An offloaded event payload lives only in the database by design;
            # it was never a file and must not be expected on disk.
            if str(row.get("resource_type") or "") != RESOURCE_TYPE_EVENT_PAYLOAD
        }

        missing_from_db: List[str] = []
        db_content_differs: List[str] = []
        for path, entry in sorted(expected.items()):
            row = db_by_path.get(path)
            if row is None:
                missing_from_db.append(path)
                continue
            actual = self._mirror(
                "verify.resource_digest", task_id,
                lambda r=row, e=entry: self._stored_resource_digest(
                    # The listing is a projection without content; the digest
                    # has to come from the stored record itself.
                    self.secondary.read_resource(
                        current_task_id=task_id,
                        resource_uri=str(r.get("saved_path") or ""),
                    ) or {},
                    str(e["kind"]),
                ),
                default="",
            )
            if actual != entry["digest"]:
                db_content_differs.append(path)
        record("resources.missingFromDb", [], missing_from_db)
        record("resources.db.content", [], db_content_differs)

        task_dir = getattr(self.primary, "task_dir", None)
        if task_dir is None:
            return
        missing_on_disk: List[str] = []
        disk_content_differs: List[str] = []
        for path, entry in sorted(expected.items()):
            if entry["kind"] == "external":
                # The bytes belong to whatever wrote them; only the reference
                # is ours, and it is not a file under the task directory.
                continue
            candidate = task_dir(task_id) / path
            if not candidate.is_file():
                missing_on_disk.append(path)
                continue
            actual = self._mirror(
                "verify.disk_digest", task_id,
                lambda p=candidate, e=entry: self._disk_resource_digest(p, str(e["kind"])),
                default="",
            )
            if actual != entry["digest"]:
                disk_content_differs.append(path)
        record("resources.missingOnDisk", [], missing_on_disk)
        record("resources.file.content", [], disk_content_differs)

    def _verify_strategy(
        self, task_id: str, scope: Optional[str], record: Callable[[str, Any, Any], None]
    ) -> None:
        expected = [
            entry for entry in self._expected(task_id, scope, "strategy")
            if isinstance(entry, dict)
        ]
        if not expected:
            return

        db_attempts = self._mirror(
            "verify.strategy_attempts", task_id,
            lambda: self._list_strategy_attempts(self.secondary, task_id), default=None,
        )
        if db_attempts is not None:
            rows = [
                row for row in db_attempts
                if scope is None or str(row.get("run_id") or "") == scope
            ]
            actual = Counter(_strategy_row_projection(row) for row in rows)
            wanted = Counter(str(entry["projected"]) for entry in expected)
            if scope is None:
                # Not run-scoped: earlier runs' rows are legitimately present,
                # so require containment rather than an exact match.
                record("strategy.db.contains", [], sorted((wanted - actual).elements()))
            else:
                record("strategy.db", sorted(wanted.elements()), sorted(actual.elements()))

        file_digests = self._mirror(
            "verify.strategy_file", task_id,
            lambda: self._file_strategy_digests(task_id), default=None,
        )
        if file_digests is not None:
            # The JSONL accumulates across runs and carries no run column, so
            # containment is the strongest claim it supports.
            missing = Counter(str(entry["payload"]) for entry in expected) - file_digests
            record("strategy.file.contains", [], sorted(missing.elements()))

    def _file_strategy_digests(self, task_id: str) -> Optional[Counter]:
        from harness.storage.file_store import STRATEGY_ATTEMPTS_FILE

        task_dir = getattr(self.primary, "task_dir", None)
        if task_dir is None:
            return None
        path = Path(task_dir(task_id)) / STRATEGY_ATTEMPTS_FILE
        if not path.is_file():
            return Counter()
        digests: Counter = Counter()
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    digests[semantic_sha256(json.loads(line))] += 1
                except (TypeError, ValueError):
                    continue
        return digests

    @staticmethod
    def _stored_resource_digest(row: JsonDict, kind: str) -> str:
        if kind == "external":
            return semantic_sha256(str(row.get("external_path") or ""))
        if kind == "bytes":
            # Binary content is not text and never was; the row's own digest
            # of those bytes is the same hash the fingerprint recorded.
            return str(row.get("sha256") or "")
        # kind comes from the write log, so a compressed JSON resource is
        # still kind == "json" here - compression changed where the bytes
        # sit, never what they are. The codec restores the logical text
        # from whichever physical shape the row happens to hold.
        text = logical_text_from_row(row)
        if text is None:
            return ""
        if kind == "json":
            try:
                return semantic_sha256(json.loads(text))
            except (TypeError, ValueError):
                return semantic_sha256(text)
        return semantic_sha256(text)

    @staticmethod
    def _disk_resource_digest(path: Path, kind: str) -> str:
        if kind == "bytes":
            return hashlib.sha256(path.read_bytes()).hexdigest()
        text = path.read_text(encoding="utf-8")
        if kind == "json":
            return semantic_sha256(json.loads(text))
        return semantic_sha256(text)

    def _event_digest(self, row: JsonDict, store: Optional[Storage] = None) -> str:
        """Digest one event the same way both backends' writes were recorded.

        An oversized payload is stored out of line, so the database row carries
        a reference instead of the JSON. Resolving it here is what stops a
        perfectly mirrored large event from reading as a mismatch.
        """

        payload = row.get("payload_json")
        if payload is None and row.get("payload_resource_id") and store is not None:
            payload = self._mirror(
                "verify.resolve_payload", str(row.get("task_id") or ""),
                lambda: self._resolve_offloaded_payload(store, row), default=None,
            )
        elif isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                pass
        return semantic_sha256([
            str(row.get("event_type") or ""),
            str(row.get("actor_type") or "") or "",
            str(row.get("worker_id") or "") or "",
            payload,
        ])

    @staticmethod
    def _resolve_offloaded_payload(store: Storage, row: JsonDict) -> Any:
        from harness.storage.sqlite_store import build_resource_uri

        task_id = str(row.get("task_id") or "")
        stored = store.read_resource(
            current_task_id=task_id,
            resource_uri=build_resource_uri(task_id, str(row["payload_resource_id"])),
        ) or {}
        raw = logical_text_from_row(stored) or "null"
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    @staticmethod
    def _trace_entry_digest(row: JsonDict) -> str:
        body = row.get("trace_json")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (TypeError, ValueError):
                pass
        return semantic_sha256(body)

    @staticmethod
    def _events_for_run(
        store: Storage, task_id: str, run_id: Optional[str]
    ) -> List[JsonDict]:
        collected: List[JsonDict] = []
        cursor = 0
        while True:
            rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=1000)
            if not rows:
                break
            for row in rows:
                if run_id is None or str(row.get("run_id") or "") == run_id:
                    collected.append(row)
            cursor = int(rows[-1]["event_id"])
        return collected

    @staticmethod
    def _list_strategy_attempts(store: Storage, task_id: str) -> Optional[List[JsonDict]]:
        lister = getattr(store, "list_strategy_attempts", None)
        if not callable(lister):
            return None
        return lister(task_id=task_id)

    def _plan_versions_seen(self, task_id: str) -> List[int]:
        versions: List[int] = []
        version = 1
        while self.primary.load_plan_version(task_id=task_id, version=version) is not None:
            versions.append(version)
            version += 1
        return versions

    @staticmethod
    def _drain_events(store: Storage, task_id: str, page: int = 1000) -> List[JsonDict]:
        collected: List[JsonDict] = []
        cursor = 0
        while True:
            rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=page)
            if not rows:
                break
            collected.extend(rows)
            cursor = int(rows[-1]["event_id"])
        return collected

    def close(self) -> None:
        self.primary.close()
        self.secondary.close()
