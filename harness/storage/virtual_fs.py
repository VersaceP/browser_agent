"""
harness.storage.virtual_fs - Database rows rendered as the files agents expect.

``local_fs_read`` and ``local_fs_search`` are model-facing tools whose contract
is paths and globs: offloaded results hand the Lead a ``savedPath``, skills and
prompts talk about ``run.jsonl`` and ``traces/*.jsonl``. Changing that contract
would mean rewriting prompts, so instead the database is presented through the
same names it replaced.

Three tables back three path families:

    run.jsonl                 -> run_events
    traces/<worker>.jsonl     -> worker_trace_events
    strategy_attempts.jsonl   -> strategy_attempts
    everything else           -> task_resources, keyed by logical_path

Rendering reproduces the exact line a file backend would have written, so a
model reading in ``db`` mode sees what it saw in ``file`` mode.
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any, Iterator, List, Optional, Tuple

from harness.storage.base import glob_matches as _glob_matches
from harness.storage.resource_codec import logical_text_from_row, normalize_text_file
from harness.utils import JsonDict


RUN_EVENTS_PATH = "run.jsonl"
STRATEGY_ATTEMPTS_PATH = "strategy_attempts.jsonl"
TRACES_PREFIX = "traces/"

# One row per logical_path: the newest run wins, then the newest version within
# it. Ordering by run_number rather than run_id matters because run ids are
# opaque and carry no sequence.
RESOURCE_CURRENT_SELECT = """
SELECT {columns}
FROM task_resources AS r
JOIN task_runs AS run ON run.task_id = r.task_id AND run.run_id = r.run_id
WHERE r.task_id = ?
  AND r.is_current = 1
  AND run.run_number = (
        SELECT MAX(inner_run.run_number)
        FROM task_resources AS inner_r
        JOIN task_runs AS inner_run
          ON inner_run.task_id = inner_r.task_id AND inner_run.run_id = inner_r.run_id
        WHERE inner_r.task_id = r.task_id
          AND inner_r.logical_path = r.logical_path
          AND inner_r.is_current = 1
  )
"""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class VirtualTaskFs:
    """A read-only file view over one task's rows.

    Only meaningful for a backend that owns a SQL connection; ``available`` is
    False for the file backend, whose real files are already on disk.
    """

    def __init__(self, storage: Any, task_id: str) -> None:
        self.storage = storage
        self.task_id = str(task_id or "")

    @property
    def available(self) -> bool:
        return self.task_id != "" and getattr(self.storage, "connection", None) is not None

    @property
    def _connection(self) -> Any:
        return self.storage.connection

    # -- listing -----------------------------------------------------------
    def list_files(self) -> List[Tuple[str, int, bool]]:
        """Return ``(logical_path, byte_size, size_is_approximate)`` triples.

        Sizes for the synthesised JSONL views come from the stored payload
        totals rather than from rendering every row: a model uses the number
        to decide whether to page, and rescanning a whole table on every
        listing to make it exact would cost far more than it is worth.
        """

        if not self.available:
            return []
        files: List[Tuple[str, int, bool]] = []
        connection = self._connection

        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(payload_byte_size), 0)"
            " FROM run_events WHERE task_id = ?",
            (self.task_id,),
        ).fetchone()
        if row and int(row[0]) > 0:
            files.append((RUN_EVENTS_PATH, int(row[1]), True))

        for trace_row in connection.execute(
            "SELECT worker_id, COALESCE(SUM(LENGTH(trace_json)), 0)"
            " FROM worker_trace_events WHERE task_id = ?"
            " GROUP BY worker_id ORDER BY worker_id",
            (self.task_id,),
        ).fetchall():
            files.append((f"{TRACES_PREFIX}{trace_row[0]}.jsonl", int(trace_row[1]), True))

        row = connection.execute(
            "SELECT COUNT(*) FROM strategy_attempts WHERE task_id = ?",
            (self.task_id,),
        ).fetchone()
        if row and int(row[0]) > 0:
            files.append((STRATEGY_ATTEMPTS_PATH, int(row[0]) * 400, True))

        # Resources are unique per (task, run, logical_path), so a resumed task
        # legitimately holds several rows for the same path. A path names one
        # file to a reader, so collapse to the newest run's newest version -
        # otherwise a listing repeats the path and a read can return the older
        # run's bytes.
        for resource_row in connection.execute(
            RESOURCE_CURRENT_SELECT.format(columns="r.logical_path, COALESCE(r.byte_size, 0), r.content_encoding")
            # Blob rows are listed only when the blob *is* the text - i.e. a
            # compressed resource. A native-binary row (identity blob) stays
            # out of the listing, as it always was: the file-view contract here
            # is textual files, and quietly adding every binary capture would
            # change what a model sees in a scan. Missing the encoding guard
            # is how a compressed resource silently vanishes from this list.
            + " AND (r.content_json IS NOT NULL OR r.content_text IS NOT NULL"
            "     OR (r.content_blob IS NOT NULL AND r.content_encoding <> 'identity'))"
            " ORDER BY r.logical_path",
            (self.task_id,),
        ).fetchall():
            files.append((str(resource_row[0]), int(resource_row[1]), False))
        return files

    def match_files(self, glob_pattern: str) -> List[Tuple[str, int, bool]]:
        pattern = glob_pattern or "**/*"
        matches: List[Tuple[str, int, bool]] = []
        for logical_path, size, approximate in self.list_files():
            if _glob_matches(pattern, logical_path):
                matches.append((logical_path, size, approximate))
        return matches

    def exists(self, logical_path: str) -> bool:
        return any(path == logical_path for path, _size, _approx in self.list_files())

    def size_of(self, logical_path: str) -> Optional[Tuple[int, bool]]:
        """``(byte_size, approximate)`` for one path, or None when absent.

        A read reports the size of the whole file, not of the slice it
        returned - that is what the on-disk reader's ``stat().st_size`` means
        and what a model pages against. Derived from the same listing so the
        two can never disagree about a path's size.
        """

        for path, size, approximate in self.list_files():
            if path == logical_path:
                return size, approximate
        return None

    # -- reading -----------------------------------------------------------
    def iter_lines(self, logical_path: str) -> Optional[Iterator[str]]:
        """Yield the file's lines, or None when this path is not backed here.

        Lines carry their terminator, exactly as iterating a real file handle
        does. A reader charging a byte budget has to see the same bytes the
        on-disk reader sees, and whether the final line ends in a newline is
        the difference between "complete" and "truncated" at an exact-fit
        budget - JSONL views terminate every line, a rendered JSON document
        does not terminate its last one.
        """

        if not self.available:
            return None
        path = str(logical_path or "").strip()
        if path == RUN_EVENTS_PATH:
            return self._iter_run_events()
        if path == STRATEGY_ATTEMPTS_PATH:
            return self._iter_strategy_attempts()
        if path.startswith(TRACES_PREFIX) and path.endswith(".jsonl"):
            worker_id = path[len(TRACES_PREFIX):-len(".jsonl")]
            return self._iter_worker_trace(worker_id)
        return self._iter_resource(path)

    def _iter_run_events(self) -> Iterator[str]:
        """Rebuild each event exactly as RunLogger.write would have written it."""

        cursor = 0
        while True:
            rows = self._connection.execute(
                "SELECT event_id, run_id, event_time, event_type, payload_json,"
                " payload_resource_id FROM run_events"
                " WHERE task_id = ? AND event_id > ? ORDER BY event_id LIMIT 500",
                (self.task_id, cursor),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                cursor = int(row["event_id"])
                payload = self._payload_for(row)
                event: JsonDict = {
                    "ts": row["event_time"],
                    "taskId": self.task_id,
                    "type": row["event_type"],
                    "payload": payload,
                }
                if row["run_id"]:
                    event["runId"] = row["run_id"]
                yield _dump(event) + "\n"

    def _payload_for(self, row: Any) -> Any:
        """Inline payloads parse directly; oversized ones were moved aside."""

        if row["payload_json"] is not None:
            try:
                return json.loads(row["payload_json"])
            except (TypeError, ValueError):
                return row["payload_json"]
        resource_id = row["payload_resource_id"]
        if not resource_id:
            return {}
        stored = self._connection.execute(
            # byte_size is not decoration: the codec cross-checks a decompressed
            # stream against it, and without the column that check silently
            # turns off - a valid-but-wrong blob would render into run.jsonl as
            # if it were the payload the event referred to.
            "SELECT content_json, content_text, content_blob, content_encoding,"
            " byte_size"
            " FROM task_resources"
            " WHERE task_id = ? AND resource_id = ?",
            (self.task_id, resource_id),
        ).fetchone()
        if stored is None:
            return {"_offloaded": True, "resourceId": resource_id, "error": "resource missing"}
        raw = logical_text_from_row(stored) or "{}"
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    def _iter_worker_trace(self, worker_id: str) -> Iterator[str]:
        cursor = 0
        while True:
            rows = self._connection.execute(
                "SELECT trace_event_id, trace_json FROM worker_trace_events"
                " WHERE task_id = ? AND worker_id = ? AND trace_event_id > ?"
                " ORDER BY trace_event_id LIMIT 500",
                (self.task_id, worker_id, cursor),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                cursor = int(row["trace_event_id"])
                yield str(row["trace_json"]) + "\n"

    def _iter_strategy_attempts(self) -> Iterator[str]:
        rows = self._connection.execute(
            "SELECT * FROM strategy_attempts WHERE task_id = ? ORDER BY attempt_id",
            (self.task_id,),
        ).fetchall()
        for row in rows:
            record = dict(row)
            try:
                strategy_ids = json.loads(record.get("strategy_ids_json") or "[]")
            except (TypeError, ValueError):
                strategy_ids = []
            yield _dump({
                "ts": record.get("created_at"),
                "taskId": self.task_id,
                "phaseId": record.get("phase_id"),
                "workerId": record.get("worker_id"),
                "strategy_ids": strategy_ids,
                "status": record.get("status"),
                "statusCategory": record.get("status_category"),
                "validatedStatus": record.get("validated_status"),
                "failureClassification": record.get("failure_classification"),
                "rowCount": record.get("row_count"),
                "artifactCount": record.get("artifact_count"),
            }) + "\n"

    def _iter_resource(self, logical_path: str) -> Optional[Iterator[str]]:
        row = self._connection.execute(
            RESOURCE_CURRENT_SELECT.format(
                # byte_size is what the codec measures a decompressed stream
                # against. This is the path behind local_fs_read, so leaving it
                # out is how a damaged row gets served to a model as a short but
                # complete-looking file instead of raising.
                columns="r.content_json, r.content_text, r.content_blob,"
                " r.content_encoding, r.byte_size"
            )
            + " AND r.logical_path = ?",
            (self.task_id, logical_path),
        ).fetchone()
        if row is None:
            return None
        # The codec produces the logical file - rendered indent=2 for JSON,
        # newline-normalised for text, decompressing first when needed. Line
        # numbers, read budgets and search hits are all defined against that
        # form; a single line of compact JSON would break all three.
        text = logical_text_from_row(row)
        if text is None:
            return None
        return iter(_universal_lines(text))


def _universal_lines(text: str) -> List[str]:
    """Split text the way opening a file in universal-newline mode does.

    Two differences from ``str.splitlines(keepends=True)``, and both of them
    showed up as file-versus-database divergence:

    * Python's text reader translates ``\\r\\n`` and ``\\r`` to ``\\n``. Keeping
      them made a Windows-style CSV read back with different bytes, a
      different digest and a different shape than the same file on disk.
    * ``splitlines`` also breaks on form feeds and Unicode separators, which a
      file reader does not, so a document containing one had a different line
      count depending on where it was stored.

    Terminators are kept, so joining the result reproduces the text exactly -
    including the fact that a final line without a newline has none.
    """

    parts = normalize_text_file(text).split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def virtual_fs_for(logger: Any) -> Optional[VirtualTaskFs]:
    """Build a virtual view for a logger, or None when the backend has no rows.

    In dual mode the secondary carries the tables while the primary keeps the
    real files, so the view is taken from whichever side owns a connection.
    """

    from harness.utils import storage_for_logger

    storage, task_id = storage_for_logger(logger)
    candidate = getattr(storage, "secondary", storage)
    view = VirtualTaskFs(candidate, task_id)
    return view if view.available else None
