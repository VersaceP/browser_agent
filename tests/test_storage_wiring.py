"""The harness talking to the storage layer through its real call sites.

Backend unit tests prove the backends behave; these prove RunLogger and
task_control actually route through them, and that the file-mode behaviour
callers depend on is unchanged.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from harness.storage.base import SNAPSHOT_KEY_CURRENT_PLAN, SNAPSHOT_KEY_TASK_STATE
from harness.storage.file_store import FileStore
from harness.storage.sqlite_store import SqliteStore
from harness.task_control import (
    load_task_state,
    write_task_plan,
    write_task_state,
)
from harness.utils import RunLogger, storage_for_logger


class RunLoggerStorageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(self._tmp.cleanup)

    def test_default_backend_writes_the_historical_file(self):
        logger = RunLogger(str(self.worktree), task_id="t1")
        logger.write("demo", {"a": 1})
        line = (self.worktree / "t1" / "run.jsonl").read_text(encoding="utf-8").strip()
        self.assertEqual(json.loads(line)["type"], "demo")

    def test_run_id_is_a_top_level_field_not_payload_context(self):
        logger = RunLogger(str(self.worktree), task_id="t1", run_id="run-1")
        bound = logger.bind_context(workerId="browser-001")
        bound.write("demo", {"a": 1})
        event = json.loads(
            (self.worktree / "t1" / "run.jsonl").read_text(encoding="utf-8").strip()
        )
        self.assertEqual(event["runId"], "run-1")
        # bind_context merges into payload; run_id must never arrive that way.
        self.assertEqual(event["payload"]["workerId"], "browser-001")
        self.assertNotIn("runId", event["payload"])

    def test_attached_backend_replaces_the_default(self):
        logger = RunLogger(str(self.worktree), task_id="t1", run_id="run-1")
        store = SqliteStore(self.worktree / "harness.db", worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        logger.attach_storage(store)

        logger.write("demo", {"a": 1})
        self.assertFalse((self.worktree / "t1" / "run.jsonl").exists())
        rows = store.read_events(task_id="t1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run-1")

    def test_storage_attached_does_not_create_a_backend(self):
        logger = RunLogger(str(self.worktree), task_id="t1")
        self.assertFalse(logger.storage_attached)
        self.assertIsInstance(logger.storage, FileStore)
        self.assertTrue(logger.storage_attached)


class DuckTypedLoggerTest(unittest.TestCase):
    """Many callers pass an object carrying only task_dir."""

    def test_task_dir_alone_resolves_a_backend_and_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "abc123"
            task_dir.mkdir()

            class Fake:
                pass

            fake = Fake()
            fake.task_dir = task_dir
            storage, task_id = storage_for_logger(fake)
            self.assertEqual(task_id, "abc123")
            storage.save_snapshot(
                task_id=task_id, snapshot_key=SNAPSHOT_KEY_TASK_STATE,
                base=None, proposed={"a": 1}, updated_run_id="", replace=True,
            )
            # Resolved against task_dir, not a guess at the worktree root.
            self.assertTrue((task_dir / "task_state.json").exists())


class TaskStateRoutingTestBase:
    def make_logger(self, worktree):
        raise NotImplementedError

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(self._tmp.cleanup)
        self.logger = self.make_logger(self.worktree)

    def test_absent_state_reads_empty(self):
        state = load_task_state(self.logger)
        self.assertEqual(dict(state), {})

    def test_write_then_read_round_trips(self):
        state = load_task_state(self.logger)
        state["phases"] = {"p1": {"status": "pending"}}
        write_task_state(self.logger, state, replace=True)
        reloaded = load_task_state(self.logger)
        self.assertEqual(reloaded["phases"]["p1"]["status"], "pending")

    def test_write_back_resets_the_baseline(self):
        state = load_task_state(self.logger)
        state["a"] = 1
        write_task_state(self.logger, state, replace=True)
        # Skipping the write-back would leave a stale base and make the next
        # merge treat this caller's own committed change as someone else's.
        self.assertEqual(state["a"], 1)
        self.assertEqual(getattr(state, "_task_state_base")["a"], 1)
        self.assertFalse(getattr(state, "_task_state_replace"))

    def test_stale_snapshots_editing_different_phases_both_survive(self):
        first = load_task_state(self.logger)
        first["phases"] = {"p1": {"status": "pending"}, "p2": {"status": "pending"}}
        write_task_state(self.logger, first, replace=True)

        one = load_task_state(self.logger)
        two = load_task_state(self.logger)
        one["phases"]["p1"]["status"] = "validated_done"
        write_task_state(self.logger, one)
        two["phases"]["p2"]["status"] = "phase_failed"
        write_task_state(self.logger, two)

        final = load_task_state(self.logger)
        self.assertEqual(final["phases"]["p1"]["status"], "validated_done")
        self.assertEqual(final["phases"]["p2"]["status"], "phase_failed")

    def test_replace_drops_phases_a_replan_removed(self):
        state = load_task_state(self.logger)
        state["phases"] = {"p1": {}, "p2": {}}
        write_task_state(self.logger, state, replace=True)

        rebuilt = load_task_state(self.logger)
        rebuilt["phases"] = {"p1": {}}
        write_task_state(self.logger, rebuilt, replace=True)
        self.assertEqual(list(load_task_state(self.logger)["phases"]), ["p1"])

    def test_updated_at_is_stamped(self):
        state = load_task_state(self.logger)
        write_task_state(self.logger, state, replace=True)
        self.assertTrue(load_task_state(self.logger).get("updated_at"))

    def test_plan_is_published_as_the_current_plan_snapshot(self):
        write_task_plan(self.logger, {"phases": [{"id": "p1"}]})
        storage, task_id = storage_for_logger(self.logger)
        plan, _ = storage.load_snapshot(
            task_id=task_id, snapshot_key=SNAPSHOT_KEY_CURRENT_PLAN
        )
        self.assertEqual(plan["phases"][0]["id"], "p1")

    def test_replanning_does_not_resurrect_removed_phases(self):
        write_task_plan(self.logger, {"phases": [{"id": "p1"}, {"id": "p2"}]})
        write_task_plan(self.logger, {"phases": [{"id": "p1"}]})
        storage, task_id = storage_for_logger(self.logger)
        plan, _ = storage.load_snapshot(
            task_id=task_id, snapshot_key=SNAPSHOT_KEY_CURRENT_PLAN
        )
        self.assertEqual([phase["id"] for phase in plan["phases"]], ["p1"])


class TaskStateFileBackendTest(TaskStateRoutingTestBase, unittest.TestCase):
    def make_logger(self, worktree):
        return RunLogger(str(worktree), task_id="t1", run_id="run-1")

    def test_state_still_lands_in_task_state_json(self):
        state = load_task_state(self.logger)
        state["a"] = 1
        write_task_state(self.logger, state, replace=True)
        stored = json.loads(
            (self.worktree / "t1" / "task_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["a"], 1)


class TaskStateSqliteBackendTest(TaskStateRoutingTestBase, unittest.TestCase):
    def make_logger(self, worktree):
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        logger.attach_storage(store)
        return logger

    def test_no_state_file_is_written(self):
        state = load_task_state(self.logger)
        state["a"] = 1
        write_task_state(self.logger, state, replace=True)
        self.assertFalse((self.worktree / "t1" / "task_state.json").exists())


class WorkerTraceWiringTest(unittest.TestCase):
    def test_a_second_write_no_longer_destroys_the_first(self):
        # Worker ids come from a per-run counter, so a resumed run reissues
        # browser-001 and the old truncating write silently lost run 1.
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            storage, task_id = storage_for_logger(logger)
            storage.append_worker_trace(
                task_id=task_id, run_id="run-1", worker_id="browser-001",
                entries=[{"type": "a"}],
            )
            storage.append_worker_trace(
                task_id=task_id, run_id="run-2", worker_id="browser-001",
                entries=[{"type": "b"}],
            )
            rows = storage.list_worker_trace(task_id=task_id, worker_id="browser-001")
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
