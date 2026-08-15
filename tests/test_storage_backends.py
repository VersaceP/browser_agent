"""FileStore / SqliteStore / DualStore behaviour.

The contract tests run unchanged against both real backends. That is the point
of the exercise: if a caller can tell which one it is talking to, the switch is
not safe to flip.
"""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from harness.storage.base import ResourceAccessError, StorageError
from harness.storage.dual_store import DualStore, semantic_sha256
from harness.storage.factory import create_storage, create_storage_from_config
from harness.storage.file_store import FileStore
from harness.storage.sqlite_store import (
    EVENT_PAYLOAD_OFFLOAD_THRESHOLD,
    SqliteStore,
    build_resource_uri,
    parse_resource_uri,
)
from runtime_config import HarnessConfig


HARNESS_VERSION = "test-2.0"


def three_way_merge(base, current, proposed):
    merged = copy.deepcopy(current)
    for key, value in proposed.items():
        if base.get(key) != value:
            merged[key] = value
    return merged


class StorageContractMixin:
    """Assertions every backend must satisfy identically."""

    def make_store(self, worktree):
        raise NotImplementedError

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.store = self.make_store(self.worktree)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)
        self.store.create_task(task_id="t1", harness_version=HARNESS_VERSION)
        self.run = self.store.start_run(task_id="t1", harness_version=HARNESS_VERSION)
        self.run_id = self.run["run_id"]

    def _all_events(self):
        collected, cursor = [], 0
        while True:
            rows = self.store.read_events(task_id="t1", after_event_id=cursor, limit=50)
            if not rows:
                return collected
            collected.extend(rows)
            cursor = int(rows[-1]["event_id"])

    # -- tasks and runs ----------------------------------------------------
    def test_created_task_is_visible(self):
        self.assertIsNotNone(self.store.get_task("t1"))
        self.assertIsNone(self.store.get_task("nope"))

    def test_runs_are_numbered_from_one(self):
        self.assertEqual(self.run["run_number"], 1)
        second = self.store.start_run(task_id="t1", harness_version=HARNESS_VERSION)
        self.assertEqual(second["run_number"], 2)

    # -- events ------------------------------------------------------------
    def test_events_round_trip_in_order(self):
        for index in range(3):
            self.store.append_event(
                task_id="t1", run_id=self.run_id,
                event_type="demo", payload={"i": index},
            )
        rows = self._all_events()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [json.loads(row["payload_json"])["i"] for row in rows], [0, 1, 2]
        )

    def test_events_filter_by_type(self):
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="keep", payload={"a": 1}
        )
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="skip", payload={"a": 2}
        )
        rows = self.store.read_events(task_id="t1", event_type="keep")
        self.assertEqual([row["event_type"] for row in rows], ["keep"])

    def test_event_keyset_pagination_does_not_repeat(self):
        for index in range(5):
            self.store.append_event(
                task_id="t1", run_id=self.run_id, event_type="d", payload={"i": index}
            )
        first = self.store.read_events(task_id="t1", limit=2)
        second = self.store.read_events(
            task_id="t1", after_event_id=first[-1]["event_id"], limit=2
        )
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertGreater(second[0]["event_id"], first[-1]["event_id"])

    def test_reading_events_of_an_unknown_task_is_empty(self):
        self.assertEqual(self.store.read_events(task_id="ghost"), [])

    # -- snapshots ---------------------------------------------------------
    def test_absent_snapshot_reads_empty(self):
        value, revision = self.store.load_snapshot(task_id="t1", snapshot_key="task_state")
        self.assertEqual(value, {})
        self.assertEqual(revision, 0)

    def test_snapshot_round_trip(self):
        persisted, revision = self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"status": "running"}, updated_run_id=self.run_id, replace=True,
        )
        self.assertEqual(persisted, {"status": "running"})
        self.assertGreaterEqual(revision, 1)
        value, _ = self.store.load_snapshot(task_id="t1", snapshot_key="task_state")
        self.assertEqual(value, {"status": "running"})

    def test_three_way_merge_preserves_a_concurrent_edit(self):
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"p1": "pending", "p2": "pending"},
            updated_run_id=self.run_id, replace=True,
        )
        base, _ = self.store.load_snapshot(task_id="t1", snapshot_key="task_state")

        other = dict(base, p2="done")
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=base, proposed=other,
            updated_run_id=self.run_id, merge=three_way_merge,
        )
        stale = dict(base, p1="running")
        persisted, _ = self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=base, proposed=stale,
            updated_run_id=self.run_id, merge=three_way_merge,
        )
        self.assertEqual(persisted["p1"], "running")
        self.assertEqual(persisted["p2"], "done")

    def test_snapshot_keys_do_not_collide(self):
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"a": 1}, updated_run_id=self.run_id, replace=True,
        )
        self.store.save_snapshot(
            task_id="t1", snapshot_key="current_task_plan", base=None,
            proposed={"phases": []}, updated_run_id=self.run_id, replace=True,
        )
        self.assertEqual(
            self.store.load_snapshot(task_id="t1", snapshot_key="task_state")[0], {"a": 1}
        )
        self.assertEqual(
            self.store.load_snapshot(task_id="t1", snapshot_key="current_task_plan")[0],
            {"phases": []},
        )

    # -- resources ---------------------------------------------------------
    def test_resource_round_trip(self):
        saved = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="observation",
            logical_path="observations/a.json", content={"rows": [1, 2, 3]},
        )
        read = self.store.read_resource(
            current_task_id="t1", resource_uri=saved["saved_path"]
        )
        self.assertIsNotNone(read)
        body = read.get("content_json") or read.get("content_text")
        self.assertIn("rows", body)

    def test_reading_a_missing_resource_returns_none(self):
        saved = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="observation",
            logical_path="observations/a.json", content={"a": 1},
        )
        missing = saved["saved_path"].replace("a.json", "does-not-exist.json")
        missing = re.sub(r"resources/[0-9a-f]+$", "resources/" + "0" * 32, missing)
        self.assertIsNone(
            self.store.read_resource(current_task_id="t1", resource_uri=missing)
        )

    def test_search_finds_by_regex_alternation(self):
        # foo|bar has no single literal a LIKE prefilter could use; both hits
        # must still come back.
        self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="observation",
            logical_path="observations/one.json", content={"v": "foo"},
        )
        self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="observation",
            logical_path="observations/two.json", content={"v": "bar"},
        )
        self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="observation",
            logical_path="observations/three.json", content={"v": "baz"},
        )
        hits = self.store.search_resources(
            task_id="t1", path_glob="observations/*", pattern=r'"foo"|"bar"',
        )
        self.assertEqual(len(hits), 2)

    def test_search_respects_max_results(self):
        for index in range(5):
            self.store.save_resource(
                task_id="t1", run_id=self.run_id, resource_type="observation",
                logical_path=f"observations/{index}.json", content={"v": "hit"},
            )
        hits = self.store.search_resources(
            task_id="t1", path_glob="observations/*", pattern="hit", max_results=3
        )
        self.assertEqual(len(hits), 3)

    # -- traces ------------------------------------------------------------
    def test_trace_appends_accumulate(self):
        self.store.append_worker_trace(
            task_id="t1", run_id=self.run_id, worker_id="browser-001",
            entries=[{"type": "browser_call", "step": 1}],
        )
        self.store.append_worker_trace(
            task_id="t1", run_id=self.run_id, worker_id="browser-001",
            entries=[{"type": "browser_call", "step": 2}],
        )
        rows = self.store.list_worker_trace(task_id="t1", worker_id="browser-001")
        self.assertEqual([row["sequence_no"] for row in rows], [1, 2])

    def test_empty_trace_append_is_a_noop(self):
        self.assertEqual(
            self.store.append_worker_trace(
                task_id="t1", run_id=self.run_id, worker_id="w", entries=[]
            ),
            0,
        )

    # -- strategy telemetry ------------------------------------------------
    def test_strategy_attempt_is_accepted(self):
        self.store.append_strategy_attempt(
            task_id="t1", run_id=self.run_id,
            payload={"phaseId": "p1", "status": "done", "strategy_ids": ["s1"]},
        )


class FileStoreContractTest(StorageContractMixin, unittest.TestCase):
    def make_store(self, worktree):
        return FileStore(worktree_dir=str(worktree))

    def test_soft_delete_is_refused(self):
        # Removing a directory is not the reversible operation operators mean
        # by "soft delete", so the file backend declines rather than approximate.
        with self.assertRaises(StorageError):
            self.store.soft_delete_task("t1")

    def test_event_file_matches_the_runlogger_wire_format(self):
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="demo", payload={"a": 1}
        )
        line = (self.worktree / "t1" / "run.jsonl").read_text(encoding="utf-8").strip()
        event = json.loads(line)
        self.assertEqual(list(event), ["ts", "taskId", "type", "payload", "runId"])
        self.assertEqual(event["taskId"], "t1")
        self.assertEqual(event["runId"], self.run_id)

    def test_resource_path_escape_is_refused(self):
        with self.assertRaises(ResourceAccessError):
            self.store.save_resource(
                task_id="t1", run_id=self.run_id, resource_type="observation",
                logical_path="../../etc/passwd", content={"a": 1},
            )

    def test_snapshot_file_is_written_atomically_with_indent(self):
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"a": 1}, updated_run_id=self.run_id, replace=True,
        )
        text = (self.worktree / "t1" / "task_state.json").read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertIn("\n  ", text)  # indent=2, as task_control writes it

    def test_torn_snapshot_file_reads_as_empty(self):
        path = self.worktree / "t1" / "task_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        value, _ = self.store.load_snapshot(task_id="t1", snapshot_key="task_state")
        self.assertEqual(value, {})


class SqliteStoreContractTest(StorageContractMixin, unittest.TestCase):
    def make_store(self, worktree):
        return SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))

    def test_soft_delete_hides_the_task(self):
        self.assertTrue(self.store.soft_delete_task("t1"))
        self.assertIsNone(self.store.get_task("t1"))
        self.assertIsNotNone(self.store.get_task("t1", include_deleted=True))

    def test_corrupt_snapshot_raises_instead_of_reading_empty(self):
        # The opposite of the file backend on purpose: a torn file was always
        # plausible, corruption inside a transaction is not.
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"a": 1}, updated_run_id=self.run_id, replace=True,
        )
        with self.store.connection as _:
            pass
        self.store.connection.execute(
            "UPDATE task_snapshots SET value_json = 'nope' WHERE task_id = 't1'"
        )
        with self.assertRaises(StorageError):
            self.store.load_snapshot(task_id="t1", snapshot_key="task_state")

    # -- resource URI is a security boundary -------------------------------
    def test_reading_another_tasks_resource_is_refused(self):
        self.store.create_task(task_id="t2", harness_version=HARNESS_VERSION)
        other_run = self.store.start_run(task_id="t2", harness_version=HARNESS_VERSION)
        secret = self.store.save_resource(
            task_id="t2", run_id=other_run["run_id"], resource_type="observation",
            logical_path="observations/secret.json", content={"password": "hunter2"},
        )
        with self.assertRaises(ResourceAccessError):
            self.store.read_resource(
                current_task_id="t1", resource_uri=secret["saved_path"]
            )

    def test_malformed_resource_uri_is_refused(self):
        for bad in ("", "not-a-uri", "sqlite://tasks/t1", "sqlite://tasks/t1/blobs/x",
                    "file:///etc/passwd"):
            with self.assertRaises(ResourceAccessError):
                self.store.read_resource(current_task_id="t1", resource_uri=bad)

    def test_resource_uri_round_trips(self):
        uri = build_resource_uri("t1", "abc123")
        self.assertEqual(parse_resource_uri(uri), ("t1", "abc123"))

    # -- versioning --------------------------------------------------------
    def test_rewriting_a_path_supersedes_rather_than_overwrites(self):
        first = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="tool_result",
            logical_path="out.json", content={"v": 1},
        )
        second = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="tool_result",
            logical_path="out.json", content={"v": 2},
        )
        self.assertEqual(second["resource_version"], 2)
        # The old id still resolves to the old bytes, so a historical event
        # pointing at it is not silently rewritten.
        old = self.store.read_resource(
            current_task_id="t1", resource_uri=first["saved_path"]
        )
        self.assertIn('"v":1', old["content_json"].replace(" ", ""))
        self.assertEqual(int(old["is_current"]), 0)
        new = self.store.read_resource(
            current_task_id="t1", resource_uri=second["saved_path"]
        )
        self.assertEqual(new["supersedes_resource_id"], first["resource_id"])

    def test_search_only_returns_current_versions(self):
        self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="tool_result",
            logical_path="out.json", content={"marker": "old"},
        )
        self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="tool_result",
            logical_path="out.json", content={"marker": "new"},
        )
        self.assertEqual(
            len(self.store.search_resources(task_id="t1", pattern="old")), 0
        )
        self.assertEqual(
            len(self.store.search_resources(task_id="t1", pattern="new")), 1
        )

    # -- external files ----------------------------------------------------
    def test_external_file_records_a_path_and_a_hash(self):
        target = self.worktree / "t1" / "downloads" / "report.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        saved = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="download",
            logical_path="downloads/report.csv", external_path=str(target),
        )
        self.assertIsNotNone(saved["sha256"])
        read = self.store.read_resource(
            current_task_id="t1", resource_uri=saved["saved_path"]
        )
        self.assertEqual(read["external_path"], "downloads/report.csv")
        self.assertFalse(read["content_drifted"])
        self.assertTrue(json.loads(read["metadata_json"])["mutable_external"])

    def test_external_file_rewritten_in_place_is_reported_as_drifted(self):
        target = self.worktree / "t1" / "downloads" / "report.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original", encoding="utf-8")
        saved = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="download",
            logical_path="downloads/report.csv", external_path=str(target),
        )
        target.write_text("rewritten by a later run", encoding="utf-8")
        read = self.store.read_resource(
            current_task_id="t1", resource_uri=saved["saved_path"]
        )
        # The harness does not choose Download.start's savePath, so external
        # bytes can change under us. Detect it instead of claiming immutability.
        self.assertTrue(read["content_drifted"])

    def test_unreadable_external_file_leaves_size_and_hash_null(self):
        saved = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="download",
            logical_path="downloads/pending.bin",
            external_path=str(self.worktree / "t1" / "downloads" / "pending.bin"),
        )
        self.assertIsNone(saved["byte_size"])
        self.assertIsNone(saved["sha256"])
        read = self.store.read_resource(
            current_task_id="t1", resource_uri=saved["saved_path"]
        )
        self.assertIn("hash_unavailable", json.loads(read["metadata_json"]))

    def test_external_file_outside_the_task_is_marked_unmanaged(self):
        outside = Path(self._tmp.name) / "elsewhere.txt"
        outside.write_text("x", encoding="utf-8")
        saved = self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="download",
            logical_path="downloads/elsewhere.txt", external_path=str(outside),
        )
        read = self.store.read_resource(
            current_task_id="t1", resource_uri=saved["saved_path"]
        )
        metadata = json.loads(read["metadata_json"])
        # A purge must never delete a file it does not own.
        self.assertTrue(metadata["external_unmanaged"])

    # -- oversized payloads ------------------------------------------------
    def test_large_event_payload_moves_to_a_resource(self):
        payload = {"blob": "x" * (EVENT_PAYLOAD_OFFLOAD_THRESHOLD + 1000)}
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="huge", payload=payload
        )
        row = self.store.read_events(task_id="t1")[0]
        self.assertIsNone(row["payload_json"])
        self.assertTrue(row["payload_resource_id"])
        self.assertGreater(row["payload_byte_size"], EVENT_PAYLOAD_OFFLOAD_THRESHOLD)
        recovered = self.store.read_resource(
            current_task_id="t1",
            resource_uri=build_resource_uri("t1", row["payload_resource_id"]),
        )
        self.assertEqual(json.loads(recovered["content_json"]), payload)

    def test_small_event_payload_stays_inline(self):
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="small", payload={"a": 1}
        )
        row = self.store.read_events(task_id="t1")[0]
        self.assertIsNone(row["payload_resource_id"])
        self.assertEqual(json.loads(row["payload_json"]), {"a": 1})

    def test_trace_scopes_by_run(self):
        self.store.append_worker_trace(
            task_id="t1", run_id=self.run_id, worker_id="browser-001",
            entries=[{"type": "a"}],
        )
        second = self.store.start_run(task_id="t1", harness_version=HARNESS_VERSION)
        self.store.append_worker_trace(
            task_id="t1", run_id=second["run_id"], worker_id="browser-001",
            entries=[{"type": "b"}],
        )
        self.assertEqual(
            len(self.store.list_worker_trace(task_id="t1", run_id=self.run_id)), 1
        )
        self.assertEqual(
            len(self.store.list_worker_trace(task_id="t1", run_id=second["run_id"])), 1
        )


class DualStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.file_store = FileStore(worktree_dir=str(self.worktree))
        self.sqlite_store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.reports = []
        self.store = DualStore(
            self.file_store, self.sqlite_store, verify=True,
            on_verify=self.reports.append,
        )
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)
        self.store.create_task(task_id="t1", harness_version=HARNESS_VERSION)
        self.run_id = self.store.start_run(
            task_id="t1", harness_version=HARNESS_VERSION
        )["run_id"]

    def test_both_backends_receive_writes(self):
        for index in range(4):
            self.store.append_event(
                task_id="t1", run_id=self.run_id, event_type="d", payload={"i": index}
            )
        self.assertEqual(len(self.file_store.read_events(task_id="t1")), 4)
        self.assertEqual(len(self.sqlite_store.read_events(task_id="t1")), 4)

    def test_reads_come_from_the_file_backend(self):
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="d", payload={"i": 1}
        )
        # Only the database gets an extra row; dual reads must not see it.
        self.sqlite_store.append_event(
            task_id="t1", run_id=self.run_id, event_type="ghost", payload={}
        )
        self.assertEqual(len(self.store.read_events(task_id="t1")), 1)

    def test_verify_reports_ok_when_the_backends_agree(self):
        for index in range(3):
            self.store.append_event(
                task_id="t1", run_id=self.run_id, event_type="d", payload={"i": index}
            )
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"status": "running"}, updated_run_id=self.run_id, replace=True,
        )
        self.store.append_worker_trace(
            task_id="t1", run_id=self.run_id, worker_id="w1", entries=[{"type": "a"}],
        )
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertEqual(report["status"], "ok", report["failedChecks"])
        self.assertEqual(report["writeErrors"], [])
        self.assertEqual(self.reports[-1]["status"], "ok")

    def test_verify_detects_a_dropped_database_write(self):
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="d", payload={"i": 0}
        )
        # Simulate the database silently losing a row.
        self.sqlite_store.connection.execute("DELETE FROM run_events")
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertEqual(report["status"], "mismatch")
        names = {check["check"] for check in report["failedChecks"]}
        self.assertIn("events.db", names)

    def test_verify_detects_content_drift_not_just_counts(self):
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"status": "running"}, updated_run_id=self.run_id, replace=True,
        )
        # Same row count, different content: a count-only check would pass.
        self.sqlite_store.connection.execute(
            "UPDATE task_snapshots SET value_json = ? WHERE task_id = 't1'",
            (json.dumps({"status": "tampered"}),),
        )
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertEqual(report["status"], "mismatch")
        names = {check["check"] for check in report["failedChecks"]}
        self.assertIn("snapshot.task_state.sha256", names)

    def test_a_database_failure_does_not_break_the_run(self):
        self.sqlite_store.close()  # every later secondary write now raises
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="d", payload={"i": 0}
        )
        # The file backend still recorded it, so the task keeps running.
        self.assertEqual(len(self.file_store.read_events(task_id="t1")), 1)
        report = self.store.verify(task_id="t1")
        self.assertTrue(report["writeErrors"])
        self.assertEqual(report["status"], "mismatch")

    def test_semantic_hash_ignores_key_order(self):
        self.assertEqual(
            semantic_sha256({"b": 1, "a": [2, {"d": 4, "c": 3}]}),
            semantic_sha256({"a": [2, {"c": 3, "d": 4}]} | {"b": 1}),
        )

    def test_run_id_is_shared_by_both_backends(self):
        runs = self.sqlite_store.connection.execute(
            "SELECT run_id FROM task_runs WHERE task_id = 't1'"
        ).fetchall()
        self.assertEqual([row[0] for row in runs], [self.run_id])


class FactoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(self._tmp.cleanup)

    def build(self, backend):
        store = create_storage(
            backend=backend,
            worktree_dir=str(self.worktree),
            sqlite_path=self.worktree / "harness.db",
        )
        self.addCleanup(store.close)
        return store

    def test_each_backend_name_builds_its_store(self):
        self.assertIsInstance(self.build("file"), FileStore)
        self.assertIsInstance(self.build("db"), SqliteStore)
        self.assertIsInstance(self.build("dual"), DualStore)

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(StorageError):
            create_storage(backend="postgres", worktree_dir=str(self.worktree))

    def test_file_backend_creates_no_database(self):
        self.build("file")
        self.assertFalse((self.worktree / "harness.db").exists())

    def test_config_default_backend_builds(self):
        config = HarnessConfig.from_dict({})
        self.assertEqual(config.storage_backend, "db")
        self.assertTrue(config.storage_dual_verify)
        store = create_storage_from_config(config, worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        self.assertIsInstance(store, SqliteStore)

    def test_config_accepts_all_three_modes(self):
        for backend in ("file", "dual", "db"):
            self.assertEqual(
                HarnessConfig.from_dict({"storage_backend": backend}).storage_backend,
                backend,
            )

    def test_a_typo_is_rejected_rather_than_silently_reinterpreted(self):
        # Unlike most options here this one is fail-fast: it decides where a
        # task's only copy of its data goes, so "duel" must not quietly become
        # the db backend and stop writing the files the operator expects.
        with self.assertRaises(ValueError):
            HarnessConfig.from_dict({"storage_backend": "duel"})

    def test_relative_database_path_follows_the_worktree(self):
        # A resume relocates worktree_dir; a cwd-relative database would stay
        # pointed at whichever directory the process happened to start in.
        config = HarnessConfig.from_dict({"storage_backend": "db"})
        store = create_storage_from_config(config, worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version="v")
        self.assertTrue((self.worktree / "harness.db").exists())
        self.assertFalse((Path.cwd() / "harness.db").exists())

    def test_absolute_database_path_is_left_alone(self):
        target = self.worktree.parent / "elsewhere.db"
        store = create_storage(
            backend="db", worktree_dir=str(self.worktree), sqlite_path=target
        )
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version="v")
        self.assertTrue(target.exists())

    def test_config_carries_the_sqlite_settings(self):
        config = HarnessConfig.from_dict({
            "storage_backend": "dual",
            "storage_sqlite_path": "custom/place.db",
            "storage_dual_verify": False,
            "storage_busy_timeout_ms": 9000,
        })
        self.assertEqual(config.storage_sqlite_path, "custom/place.db")
        self.assertFalse(config.storage_dual_verify)
        self.assertEqual(config.storage_busy_timeout_ms, 9000)


if __name__ == "__main__":
    unittest.main()
