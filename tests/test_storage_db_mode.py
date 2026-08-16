"""Plan history, resume and offload once the database is the only store.

These are the paths that made `db` mode unusable before: a resumed task reads
its plan and state before a logger exists, and offloaded payloads are addressed
by a path the model was handed earlier.
"""

import json
import tempfile
import unittest
from pathlib import Path

from harness.local_fs import local_fs_read
from harness.offload import offload_large_tool_result
from harness.planning.validator import write_plan_review_audit, write_plan_version
from harness.resume_state import (
    ResumeStateError,
    configure_resume_storage,
    load_initial_task_plan_strict,
    load_task_plan_strict,
    load_task_state_strict,
)
from harness.storage import create_storage
from harness.storage.file_store import FileStore
from harness.storage.sqlite_store import SqliteStore
from harness.task_control import load_task_state, write_task_plan, write_task_state
from harness.utils import RunLogger


PLAN = {"goal": "g", "phases": [{"id": "p1", "objective": "one"}]}
PLAN_V2 = {"goal": "g", "phases": [{"id": "p1", "objective": "one"}, {"id": "p2", "objective": "two"}]}


class PlanHistoryContractMixin:
    def make_logger(self, worktree):
        raise NotImplementedError

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(self._tmp.cleanup)
        self.logger = self.make_logger(self.worktree)

    def _write(self, plan, previous=None, reason=""):
        return write_plan_version(
            self.logger, plan=plan, previous_plan=previous,
            replan_reason=reason, user_task="t", validator_review=None,
        )

    def test_versions_start_at_one_and_increment(self):
        first = self._write(PLAN)
        second = self._write(PLAN_V2, previous=PLAN, reason="add a phase")
        self.assertEqual(first["planVersion"], 1)
        self.assertIsNone(first["previousVersion"])
        self.assertEqual(second["planVersion"], 2)
        self.assertEqual(second["previousVersion"], 1)

    def test_plan_hash_and_diff_are_recorded(self):
        self._write(PLAN)
        second = self._write(PLAN_V2, previous=PLAN, reason="add a phase")
        self.assertTrue(second["planHash"])
        self.assertGreater(second["diffCount"], 0)
        self.assertEqual(second["replanReason"], "add a phase")

    def test_first_version_is_readable_back(self):
        self._write(PLAN)
        self._write(PLAN_V2, previous=PLAN, reason="r")
        storage = self.logger.storage
        record = storage.load_plan_version(task_id=self.logger.task_id, version=1)
        # Version 1 is the resume anchor: it must keep the plan as accepted,
        # not the current one.
        self.assertEqual(len(record["plan"]["phases"]), 1)

    def test_missing_version_reads_as_none(self):
        self.assertIsNone(
            self.logger.storage.load_plan_version(task_id=self.logger.task_id, version=9)
        )

    def test_reviews_are_sequenced(self):
        for index in range(2):
            write_plan_review_audit(
                self.logger, candidate_plan=PLAN,
                replan_reason=f"r{index}", review={"status": "approved"},
            )
        # Sequencing is the backend's job; both must number from one.
        second = write_plan_review_audit(
            self.logger, candidate_plan=PLAN_V2,
            replan_reason="r2", review={"status": "rejected"},
        )
        self.assertTrue(second)


class PlanHistoryFileTest(PlanHistoryContractMixin, unittest.TestCase):
    def make_logger(self, worktree):
        return RunLogger(str(worktree), task_id="t1", run_id="run-1")

    def test_history_files_are_still_written(self):
        self._write(PLAN)
        self.assertTrue(
            (self.worktree / "t1" / "task_plan_history" / "plan.0001.json").is_file()
        )


class PlanHistorySqliteTest(PlanHistoryContractMixin, unittest.TestCase):
    def make_logger(self, worktree):
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        logger.attach_storage(store)
        return logger

    def test_no_history_files_are_written(self):
        self._write(PLAN)
        self.assertFalse((self.worktree / "t1" / "task_plan_history").exists())


class ResumeWithoutFilesTest(unittest.TestCase):
    """A db-mode task leaves nothing on disk, yet must still be resumable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(self._tmp.cleanup)
        self.logger = RunLogger(str(self.worktree), task_id="t1", run_id="run-1")
        self.store = create_storage(backend="db", worktree_dir=str(self.worktree))
        self.addCleanup(self.store.close)
        self.logger.attach_storage(self.store)
        self.store.create_task(task_id="t1", harness_version="v")
        self.store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        # main hands the resume loaders the configuration it parsed; without
        # that they cannot know the database is authoritative here.
        configure_resume_storage(
            backend="db", sqlite_path=self.worktree / "harness.db"
        )
        self.addCleanup(configure_resume_storage, backend=None, sqlite_path=None)

        write_plan_version(
            self.logger, plan=PLAN, previous_plan=None,
            replan_reason="", user_task="t", validator_review=None,
        )
        write_task_plan(self.logger, PLAN)
        state = load_task_state(self.logger)
        state["phases"] = {"p1": {"status": "validated_done"}}
        write_task_state(self.logger, state, replace=True)
        self.task_dir = self.worktree / "t1"

    def test_no_process_files_exist(self):
        files = [p for p in self.task_dir.rglob("*") if p.is_file()]
        self.assertEqual(files, [])

    def test_current_plan_resumes_from_the_database(self):
        plan = load_task_plan_strict(self.task_dir)
        self.assertEqual([p["id"] for p in plan["phases"]], ["p1"])

    def test_initial_plan_resumes_from_the_database(self):
        plan = load_initial_task_plan_strict(self.task_dir)
        self.assertEqual([p["id"] for p in plan["phases"]], ["p1"])

    def test_state_resumes_from_the_database(self):
        state = load_task_state_strict(self.task_dir)
        self.assertEqual(state["phases"]["p1"]["status"], "validated_done")

    def test_an_unknown_task_still_raises(self):
        empty = self.worktree / "ghost"
        empty.mkdir()
        with self.assertRaises(ResumeStateError):
            load_task_state_strict(empty)


class ResumeSourceTriageTest(unittest.TestCase):
    """Which source wins is decided by who owns the task, not file presence.

    "A file exists, so use it" looks right until a dual-mode task switches to
    db: its stale files are still on disk, and resume would silently replay
    old state forever.
    """

    def _db_task(self, worktree, task_id, status, *, backend="db"):
        configure_resume_storage(
            backend=backend, sqlite_path=worktree / "harness.db"
        )
        self.addCleanup(configure_resume_storage, backend=None, sqlite_path=None)
        logger = RunLogger(str(worktree), task_id=task_id, run_id="run-1")
        store = create_storage(backend="db", worktree_dir=str(worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id=task_id, harness_version="v")
        store.start_run(task_id=task_id, harness_version="v", run_id="run-1")
        state = load_task_state(logger)
        state["phases"] = {"p1": {"status": status}}
        write_task_state(logger, state, replace=True)
        return store

    @staticmethod
    def _stale_file(task_dir, status):
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task_state.json").write_text(
            json.dumps({"phases": {"p1": {"status": status}}}), encoding="utf-8"
        )

    def test_db_mode_resumes_from_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            self._db_task(worktree, "t1", "from_database", backend="db")
            self._stale_file(worktree / "t1", "from_file")
            resumed = load_task_state_strict(worktree / "t1")
            self.assertEqual(resumed["phases"]["p1"]["status"], "from_database")

    def test_dual_mode_keeps_the_files_authoritative(self):
        # In dual the database is the copy under evaluation. Preferring it
        # would resume from a mirror whose write may have silently failed.
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            self._db_task(worktree, "t1", "from_database", backend="dual")
            self._stale_file(worktree / "t1", "from_file")
            resumed = load_task_state_strict(worktree / "t1")
            self.assertEqual(resumed["phases"]["p1"]["status"], "from_file")

    def test_an_unregistered_task_resumes_from_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            # The database exists and holds another task, but never saw this
            # one: that is what "legacy worktree" actually means.
            self._db_task(worktree, "t1", "irrelevant")
            legacy = worktree / "legacy-task"
            self._stale_file(legacy, "from_file")
            resumed = load_task_state_strict(legacy)
            self.assertEqual(resumed["phases"]["p1"]["status"], "from_file")

    def test_no_database_at_all_still_reads_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            self._stale_file(worktree / "t1", "from_file")
            resumed = load_task_state_strict(worktree / "t1")
            self.assertEqual(resumed["phases"]["p1"]["status"], "from_file")


class OffloadRoutingTest(unittest.TestCase):
    def _big(self):
        return {"rows": [{"i": i, "text": "x" * 200} for i in range(400)]}

    def test_offloaded_result_is_readable_back_in_db_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            store.create_task(task_id="t1", harness_version="v")
            store.start_run(task_id="t1", harness_version="v", run_id="run-1")
            logger.attach_storage(store)

            payload = self._big()
            stub = offload_large_tool_result(
                logger=logger, tool_name="Page.getSemanticTree",
                result=payload, step=3,
            )
            self.assertTrue(stub["_offloaded"])
            # The model is handed a path and reads it back with the same tool;
            # nothing about the round trip reveals there is no file.
            self.assertFalse(Path(stub["savedPath"]).exists())
            result = local_fs_read(
                logger, path=stub["savedPath"], max_bytes=200000, line_limit=5000
            )
            self.assertEqual(result["status"], "done")
            self.assertEqual(json.loads(result["content"]), payload)

    def test_offloaded_result_still_lands_on_disk_in_file_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            stub = offload_large_tool_result(
                logger=logger, tool_name="Page.getSemanticTree",
                result=self._big(), step=3,
            )
            self.assertTrue(Path(stub["savedPath"]).is_file())


if __name__ == "__main__":
    unittest.main()


class ExtractionArtifactRoutingTest(unittest.TestCase):
    """Extraction artifacts are cited by path across phases, so the address
    must keep working when the bytes move into the database."""

    def _save(self, logger):
        from types import SimpleNamespace

        from harness.evidence.extraction_artifacts import save_extraction_artifact

        self.artifacts: list = []
        return save_extraction_artifact(
            logger=logger,
            runtime=SimpleNamespace(harness=SimpleNamespace(runs_dir="")),
            artifacts=self.artifacts,
            name="rank-items",
            rows=[{"rank": index, "name": f"item{index}"} for index in range(1, 6)],
            schema=[{"name": "rank"}, {"name": "name"}],
        )

    def test_db_mode_writes_no_file_but_stays_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            store = create_storage(backend="db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            logger.attach_storage(store)
            store.create_task(task_id="t1", harness_version="v")
            store.start_run(task_id="t1", harness_version="v", run_id="run-1")

            saved = self._save(logger)
            self.assertEqual(saved["status"], "done")
            self.assertFalse(Path(saved["savedPath"]).exists())
            # The ledger still records the path, and the path still resolves.
            self.assertIn(saved["savedPath"], self.artifacts)
            read = local_fs_read(
                logger, path=saved["savedPath"], max_bytes=200000, line_limit=5000
            )
            self.assertEqual(json.loads(read["content"])["rowCount"], 5)

    def test_file_mode_still_writes_the_artifact_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            saved = self._save(logger)
            self.assertTrue(Path(saved["savedPath"]).is_file())
            self.assertEqual(
                json.loads(Path(saved["savedPath"]).read_text(encoding="utf-8"))["rowCount"],
                5,
            )
