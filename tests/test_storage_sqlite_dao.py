"""DAO behaviour, with emphasis on the snapshot compare-and-swap.

The CAS is where a subtle bug is most expensive: it guards the task state that
every phase decision reads, and a wrong answer silently drops another writer's
edits instead of failing.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.storage import dao
from harness.storage.base import RevisionConflictError, StorageError, canonical_json
from harness.storage.factory import open_database
from harness.storage.migrations import SCHEMA_VERSION
from harness.storage.sqlite_connection import write_transaction


HARNESS_VERSION = "test-1.0"


def three_way_merge(base, current, proposed):
    """Stand-in for task_control._three_way_merge_task_state.

    Keeps every key the other writer added, and lets this writer's changes win
    only for keys it actually touched relative to its own baseline.
    """

    merged = copy.deepcopy(current)
    for key, value in proposed.items():
        if base.get(key) != value:
            merged[key] = value
    for key in base:
        if key not in proposed and key in merged and current.get(key) == base.get(key):
            del merged[key]
    return merged


class StorageDaoTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.connection = open_database(Path(self._tmp.name) / "harness.db")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.connection.close)
        self.task = dao.insert_task(
            self.connection,
            task_id="task-1",
            harness_version=HARNESS_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        self.run = dao.start_run(
            self.connection, task_id="task-1", harness_version=HARNESS_VERSION
        )
        self.run_id = self.run["run_id"]


class TaskDaoTest(StorageDaoTestBase):
    def test_insert_task_is_idempotent(self):
        again = dao.insert_task(
            self.connection,
            task_id="task-1",
            harness_version="other-version",
            schema_version=SCHEMA_VERSION,
        )
        self.assertEqual(again["create_time"], self.task["create_time"])
        self.assertEqual(again["created_harness_version"], HARNESS_VERSION)
        row = self.connection.execute("SELECT count(*) FROM tasks").fetchone()
        self.assertEqual(int(row[0]), 1)

    def test_task_records_versions(self):
        self.assertEqual(self.task["created_harness_version"], HARNESS_VERSION)
        self.assertEqual(self.task["created_schema_version"], SCHEMA_VERSION)
        self.assertEqual(self.task["purge_status"], "none")

    def test_soft_delete_hides_the_task_by_default(self):
        self.assertTrue(dao.soft_delete_task(self.connection, task_id="task-1"))
        self.assertIsNone(dao.get_task(self.connection, task_id="task-1"))
        hidden = dao.get_task(self.connection, task_id="task-1", include_deleted=True)
        self.assertIsNotNone(hidden)
        self.assertEqual(int(hidden["is_deleted"]), 1)
        self.assertTrue(hidden["deleted_at"])

    def test_soft_delete_is_not_repeatable(self):
        self.assertTrue(dao.soft_delete_task(self.connection, task_id="task-1"))
        self.assertFalse(dao.soft_delete_task(self.connection, task_id="task-1"))

    def test_list_tasks_filters_deleted(self):
        dao.insert_task(
            self.connection,
            task_id="task-2",
            harness_version=HARNESS_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        dao.soft_delete_task(self.connection, task_id="task-2")
        live = [row["task_id"] for row in dao.list_tasks(self.connection)]
        self.assertEqual(live, ["task-1"])
        every = [row["task_id"] for row in dao.list_tasks(self.connection, include_deleted=True)]
        self.assertCountEqual(every, ["task-1", "task-2"])

    def test_soft_deleted_task_rejects_snapshot_update(self):
        dao.soft_delete_task(self.connection, task_id="task-1")
        self.assertFalse(
            dao.update_task_snapshot(self.connection, task_id="task-1", snapshot={"a": 1})
        )


class RunDaoTest(StorageDaoTestBase):
    def test_run_numbers_increment(self):
        second = dao.start_run(
            self.connection, task_id="task-1", harness_version=HARNESS_VERSION
        )
        self.assertEqual(self.run["run_number"], 1)
        self.assertEqual(second["run_number"], 2)
        self.assertNotEqual(second["run_id"], self.run_id)

    def test_finish_run_records_terminal_status(self):
        self.assertTrue(
            dao.finish_run(
                self.connection,
                task_id="task-1",
                run_id=self.run_id,
                status="interrupted",
                error={"reason": "ctrl-c"},
            )
        )
        row = dao.get_run(self.connection, task_id="task-1", run_id=self.run_id)
        self.assertEqual(row["status"], "interrupted")
        self.assertTrue(row["finished_at"])
        self.assertEqual(json.loads(row["error_json"])["reason"], "ctrl-c")


class SnapshotCasTest(StorageDaoTestBase):
    def save(self, **kwargs):
        kwargs.setdefault("task_id", "task-1")
        kwargs.setdefault("snapshot_key", "task_state")
        kwargs.setdefault("updated_run_id", self.run_id)
        return dao.save_snapshot(self.connection, **kwargs)

    def load(self):
        return dao.load_snapshot(
            self.connection, task_id="task-1", snapshot_key="task_state"
        )

    # -- gap 3: absent row reads as ({}, 0) --------------------------------
    def test_absent_snapshot_reads_as_empty_at_revision_zero(self):
        self.assertEqual(self.load(), ({}, 0))

    def test_stored_empty_state_is_indistinguishable_from_absent(self):
        self.save(proposed={}, replace=True)
        value, revision = self.load()
        self.assertEqual(value, {})
        self.assertEqual(revision, 1)

    # -- gap 1: the very first write has no row to UPDATE ------------------
    def test_first_write_inserts_instead_of_reporting_a_conflict(self):
        persisted, revision = self.save(proposed={"status": "planning"})
        self.assertEqual(persisted, {"status": "planning"})
        self.assertEqual(revision, 1)
        self.assertEqual(self.load(), ({"status": "planning"}, 1))

    def test_first_write_with_replace_is_not_a_conflict(self):
        # Plan acceptance bootstraps state with replace=True while the row
        # still does not exist; an UPDATE-only CAS would fail every new task.
        persisted, revision = self.save(proposed={"phases": {}}, replace=True)
        self.assertEqual(revision, 1)
        self.assertEqual(persisted, {"phases": {}})

    def test_second_write_bumps_the_revision(self):
        base, _ = self.save(proposed={"a": 1}, replace=True)
        _, revision = self.save(base=base, proposed={"a": 2}, merge=three_way_merge)
        self.assertEqual(revision, 2)
        self.assertEqual(self.load()[0], {"a": 2})

    # -- three-way merge is preserved, not replaced by last-writer-wins ----
    def test_concurrent_edits_to_different_keys_both_survive(self):
        self.save(proposed={"phase1": "pending", "phase2": "pending"}, replace=True)
        base, _ = self.load()

        # Another writer commits between this caller's read and its write.
        other = copy.deepcopy(base)
        other["phase2"] = "done"
        self.save(base=base, proposed=other, merge=three_way_merge)

        stale = copy.deepcopy(base)
        stale["phase1"] = "running"
        persisted, _ = self.save(base=base, proposed=stale, merge=three_way_merge)

        self.assertEqual(persisted["phase1"], "running")
        self.assertEqual(persisted["phase2"], "done")

    def test_conflict_remerges_against_the_original_base(self):
        """On a lost CAS, base and proposed must stay at their original values.

        Re-deriving base from the fresh current would make this caller's edits
        look unchanged, and the merge would silently discard them.
        """

        self.save(proposed={"a": "0"}, replace=True)
        base, _ = self.load()
        seen_bases = []
        collided = {"done": False}

        def racing_merge(merge_base, current, proposed):
            seen_bases.append(copy.deepcopy(merge_base))
            if not collided["done"]:
                collided["done"] = True
                # Simulate another connection committing right before our CAS.
                with write_transaction(self.connection):
                    self.connection.execute(
                        "UPDATE task_snapshots SET value_json = ?, revision = revision + 1"
                        " WHERE task_id = 'task-1' AND snapshot_key = 'task_state'",
                        (json.dumps({"a": "0", "b": "other"}),),
                    )
            return three_way_merge(merge_base, current, proposed)

        conflicts = []
        persisted, revision = self.save(
            base=base,
            proposed={"a": "mine"},
            merge=racing_merge,
            on_conflict=conflicts.append,
        )

        self.assertEqual(len(seen_bases), 2)
        self.assertEqual(seen_bases[0], seen_bases[1])
        self.assertEqual(seen_bases[1], base)
        self.assertEqual(persisted["a"], "mine")
        self.assertEqual(persisted["b"], "other")
        self.assertEqual(revision, 3)

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["expectedRevision"], 1)
        self.assertEqual(conflicts[0]["actualRevision"], 2)
        self.assertEqual(conflicts[0]["attempt"], 1)

    def test_conflict_budget_is_bounded(self):
        self.save(proposed={"a": 0}, replace=True)
        base, _ = self.load()

        def always_racing(merge_base, current, proposed):
            with write_transaction(self.connection):
                self.connection.execute(
                    "UPDATE task_snapshots SET revision = revision + 1"
                    " WHERE task_id = 'task-1' AND snapshot_key = 'task_state'"
                )
            return dict(current)

        conflicts = []
        with self.assertRaises(RevisionConflictError):
            self.save(
                base=base,
                proposed={"a": 1},
                merge=always_racing,
                max_attempts=3,
                on_conflict=conflicts.append,
            )
        self.assertEqual(len(conflicts), 3)

    # -- replace=True is stricter than a stale-snapshot write --------------
    def test_replace_conflict_raises_without_retrying(self):
        """A whole-state rebuild that loses its CAS must not retry.

        replace=True means "this is the authoritative shape"; racing another
        writer there is a lifecycle coordination failure, and retrying would
        overwrite whatever that writer just committed.
        """

        self.save(proposed={"a": 1}, replace=True)
        real_load = dao.load_snapshot
        conflicts = []

        def stale_load(connection, *, task_id, snapshot_key):
            # Report the revision the caller saw one commit ago, which is what
            # a concurrent commit between read and BEGIN IMMEDIATE looks like.
            value, revision = real_load(
                connection, task_id=task_id, snapshot_key=snapshot_key
            )
            return value, max(1, revision - 1)

        with mock.patch.object(dao, "load_snapshot", side_effect=stale_load):
            self.save(proposed={"a": 2}, replace=True)  # revision -> 2
            with self.assertRaises(RevisionConflictError) as caught:
                self.save(
                    proposed={"rebuilt": True},
                    replace=True,
                    on_conflict=conflicts.append,
                )

        self.assertEqual(len(conflicts), 1)  # raised on the first loss, no retry
        self.assertTrue(conflicts[0]["replace"])
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(self.load()[0], {"a": 2})  # the rebuild did not land

    def test_replace_ignores_merge_and_rebuilds_wholesale(self):
        self.save(proposed={"keep": 1, "drop": 2}, replace=True)
        base, _ = self.load()
        persisted, _ = self.save(
            base=base, proposed={"keep": 9}, merge=three_way_merge, replace=True
        )
        self.assertEqual(persisted, {"keep": 9})

    # -- gap 4: the caller needs the persisted value to reset its baseline --
    def test_save_returns_the_value_that_was_persisted(self):
        self.save(proposed={"a": 1}, replace=True)
        base, _ = self.load()
        other = copy.deepcopy(base)
        other["b"] = 2
        self.save(base=base, proposed=other, merge=three_way_merge)

        stale = copy.deepcopy(base)
        stale["a"] = 3
        persisted, revision = self.save(base=base, proposed=stale, merge=three_way_merge)
        stored, stored_revision = self.load()
        self.assertEqual(persisted, stored)
        self.assertEqual(revision, stored_revision)

    def test_corrupt_snapshot_json_raises_instead_of_resetting_state(self):
        self.save(proposed={"a": 1}, replace=True)
        with write_transaction(self.connection):
            self.connection.execute(
                "UPDATE task_snapshots SET value_json = 'not json'"
                " WHERE task_id = 'task-1'"
            )
        with self.assertRaises(StorageError):
            self.load()

    def test_snapshot_keys_are_independent(self):
        self.save(proposed={"a": 1}, replace=True)
        self.save(snapshot_key="current_task_plan", proposed={"phases": []}, replace=True)
        self.assertEqual(self.load()[0], {"a": 1})
        plan, revision = dao.load_snapshot(
            self.connection, task_id="task-1", snapshot_key="current_task_plan"
        )
        self.assertEqual(plan, {"phases": []})
        self.assertEqual(revision, 1)


class EventAndTraceDaoTest(StorageDaoTestBase):
    def test_events_paginate_by_keyset(self):
        for index in range(5):
            dao.insert_event(
                self.connection,
                task_id="task-1",
                run_id=self.run_id,
                event_type="demo",
                payload_json=json.dumps({"i": index}),
                payload_byte_size=10,
            )
        first = dao.read_events(self.connection, task_id="task-1", limit=2)
        self.assertEqual(len(first), 2)
        second = dao.read_events(
            self.connection, task_id="task-1", after_event_id=first[-1]["event_id"], limit=2
        )
        self.assertEqual(len(second), 2)
        self.assertGreater(second[0]["event_id"], first[-1]["event_id"])

    def test_events_filter_by_type(self):
        dao.insert_event(
            self.connection, task_id="task-1", run_id=self.run_id,
            event_type="wanted", payload_json="{}", payload_byte_size=2,
        )
        dao.insert_event(
            self.connection, task_id="task-1", run_id=self.run_id,
            event_type="ignored", payload_json="{}", payload_byte_size=2,
        )
        rows = dao.read_events(self.connection, task_id="task-1", event_type="wanted")
        self.assertEqual([row["event_type"] for row in rows], ["wanted"])

    def test_trace_sequence_continues_across_appends(self):
        dao.insert_trace_events(
            self.connection, task_id="task-1", run_id=self.run_id,
            worker_id="browser-001",
            entries=[{"type": "browser_call", "step": 1}],
        )
        dao.insert_trace_events(
            self.connection, task_id="task-1", run_id=self.run_id,
            worker_id="browser-001",
            entries=[{"type": "browser_call", "step": 2}],
        )
        rows = dao.list_trace_events(
            self.connection, task_id="task-1", worker_id="browser-001"
        )
        self.assertEqual([row["sequence_no"] for row in rows], [1, 2])

    def test_same_worker_id_in_a_later_run_starts_a_new_sequence(self):
        # Worker ids come from a per-run counter, so browser-001 recurs on a
        # resume. Scoping the sequence by run keeps the older trace intact
        # instead of overwriting it the way the file backend did.
        dao.insert_trace_events(
            self.connection, task_id="task-1", run_id=self.run_id,
            worker_id="browser-001", entries=[{"type": "a"}],
        )
        second_run = dao.start_run(
            self.connection, task_id="task-1", harness_version=HARNESS_VERSION
        )
        dao.insert_trace_events(
            self.connection, task_id="task-1", run_id=second_run["run_id"],
            worker_id="browser-001", entries=[{"type": "b"}],
        )
        first = dao.list_trace_events(
            self.connection, task_id="task-1", run_id=self.run_id
        )
        second = dao.list_trace_events(
            self.connection, task_id="task-1", run_id=second_run["run_id"]
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(json.loads(first[0]["trace_json"])["type"], "a")

    def test_strategy_attempts_are_queryable_across_tasks(self):
        dao.insert_strategy_attempt(
            self.connection, task_id="task-1", run_id=self.run_id,
            payload={"phaseId": "p1", "workerId": "w1", "strategy_ids": ["s1"],
                     "status": "done", "rowCount": 12, "artifactCount": 1},
        )
        dao.insert_task(
            self.connection, task_id="task-2",
            harness_version=HARNESS_VERSION, schema_version=SCHEMA_VERSION,
        )
        other_run = dao.start_run(
            self.connection, task_id="task-2", harness_version=HARNESS_VERSION
        )
        dao.insert_strategy_attempt(
            self.connection, task_id="task-2", run_id=other_run["run_id"],
            payload={"phaseId": "p1", "status": "failed"},
        )
        everything = dao.list_strategy_attempts(self.connection)
        self.assertEqual(len(everything), 2)
        scoped = dao.list_strategy_attempts(self.connection, task_id="task-1")
        self.assertEqual(len(scoped), 1)
        self.assertEqual(json.loads(scoped[0]["strategy_ids_json"]), ["s1"])


class CanonicalJsonTest(unittest.TestCase):
    def test_key_order_does_not_change_the_hash_input(self):
        self.assertEqual(
            canonical_json({"b": 1, "a": 2}),
            canonical_json({"a": 2, "b": 1}),
        )

    def test_non_ascii_is_preserved_rather_than_escaped(self):
        self.assertIn("采集", canonical_json({"goal": "采集商品详情"}))

    def test_unserialisable_values_fall_back_to_str(self):
        # default=str keeps a non-JSON value from aborting a whole write.
        self.assertEqual(canonical_json({"p": Path("/tmp/x")}), '{"p":"/tmp/x"}')


if __name__ == "__main__":
    unittest.main()
