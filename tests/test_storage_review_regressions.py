"""Regressions for the defects a code review reproduced against db mode.

Each test here corresponds to a concrete failure that the storage migration
introduced and that the original test suite did not catch, mostly because it
exercised the model-facing reader while the harness's own gates read files
directly.
"""

import asyncio
import json
import multiprocessing
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.evidence.extraction_artifacts import save_extraction_artifact
from harness.local_fs import local_fs_read
from harness.storage import create_storage
from harness.storage.admin import PurgeRefused, purge_task, soft_delete_task
from harness.storage.dual_store import DualStore
from harness.storage.factory import open_database
from harness.storage.file_store import FileStore
from harness.storage.migrations import SCHEMA_VERSION
from harness.storage.sqlite_store import (
    EVENT_PAYLOAD_OFFLOAD_THRESHOLD,
    SqliteStore,
)
from harness.storage.virtual_fs import VirtualTaskFs
from harness.task_control import (
    accept_task_plan,
    load_task_state,
    validate_worker_artifacts,
    write_task_state,
)
from harness.utils import RunLogger
from runtime_config import HarnessConfig


def _open_database_once(database_path):
    from harness.storage.factory import open_database as opener

    try:
        connection = opener(database_path)
        connection.close()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


class ArtifactGateTest(unittest.TestCase):
    """The gate that decides whether a phase completed reads artifacts itself.

    Routing only local_fs_read through the database left db-mode extractions
    readable by the agent and simultaneously judged missing by the validator.
    """

    def _run(self, backend):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worktree = Path(tmp.name) / "worktree"
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        store = create_storage(backend=backend, worktree_dir=str(worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        saved = save_extraction_artifact(
            logger=logger,
            runtime=SimpleNamespace(harness=SimpleNamespace(runs_dir="")),
            artifacts=[], name="items",
            rows=[{"title": f"t{i}", "url": f"https://e.com/{i}"} for i in range(1, 11)],
            schema=[{"name": "title"}, {"name": "url"}],
        )
        validation = validate_worker_artifacts(
            contract={"expected_artifact": {
                "type": "extraction", "name": "items",
                "fields": ["title", "url"], "min_rows": 5,
            }},
            artifacts=[saved["savedPath"]],
            task_dir=logger.task_dir,
            logger=logger,
        )
        return saved, validation

    def test_every_backend_passes_the_same_artifact(self):
        for backend in ("file", "db", "dual"):
            with self.subTest(backend=backend):
                saved, validation = self._run(backend)
                self.assertEqual(validation["status"], "done")
                self.assertEqual(validation["rowCount"], 10)
                self.assertEqual(len(validation["validExtractionArtifacts"]), 1)

    def test_db_mode_passes_without_the_file_existing(self):
        saved, validation = self._run("db")
        self.assertFalse(Path(saved["savedPath"]).exists())
        self.assertEqual(validation["status"], "done")
        self.assertEqual(
            local_fs_read(
                RunLogger(str(Path(saved["savedPath"]).parents[2]), task_id="t1"),
                path=saved["savedPath"],
            )["status"],
            "failed",  # a bare logger has no backend attached
        )


class DualVerificationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"
        self.file_store = FileStore(worktree_dir=str(self.worktree))
        self.sqlite_store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(self.sqlite_store.close)
        self.store = DualStore(self.file_store, self.sqlite_store)
        self.store.create_task(task_id="t1", harness_version="v")
        self.run_id = self.store.start_run(task_id="t1", harness_version="v")["run_id"]

    def test_payload_drift_on_the_database_side_is_a_mismatch(self):
        # Counts and type lists matched, so this used to report ok.
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="demo",
            payload={"value": "written"},
        )
        self.sqlite_store.connection.execute(
            "UPDATE run_events SET payload_json = ?",
            (json.dumps({"value": "tampered"}),),
        )
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertEqual(report["status"], "mismatch")
        self.assertIn("events.db", {c["check"] for c in report["failedChecks"]})

    def test_payload_drift_on_the_file_side_is_a_mismatch(self):
        # Checked against the write log, so a file-side change is caught too -
        # comparing the backends to each other only ever said "they differ".
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="demo",
            payload={"value": "written"},
        )
        path = self.worktree / "t1" / "run.jsonl"
        rewritten = json.loads(path.read_text(encoding="utf-8").strip())
        rewritten["payload"] = {"value": "tampered"}
        path.write_text(json.dumps(rewritten) + "\n", encoding="utf-8")
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertIn("events.file", {c["check"] for c in report["failedChecks"]})

    def test_identical_writes_still_verify_clean(self):
        for index in range(3):
            self.store.append_event(
                task_id="t1", run_id=self.run_id, event_type="demo",
                payload={"i": index},
            )
        self.store.append_worker_trace(
            task_id="t1", run_id=self.run_id, worker_id="w1",
            entries=[{"type": "a", "step": 1}],
        )
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    def test_trace_content_drift_is_caught(self):
        self.store.append_worker_trace(
            task_id="t1", run_id=self.run_id, worker_id="w1",
            entries=[{"type": "a"}],
        )
        self.sqlite_store.connection.execute(
            "UPDATE worker_trace_events SET trace_json = ?", (json.dumps({"type": "b"}),)
        )
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertIn("trace.db", {c["check"] for c in report["failedChecks"]})


class MigrationConcurrencyTest(unittest.TestCase):
    def test_six_processes_can_create_the_database_at_once(self):
        # The pending-version check ran outside the write lock, so every
        # process saw the migration as unapplied and replayed the DDL.
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "harness.db")
            with multiprocessing.Pool(6) as pool:
                results = pool.map(_open_database_once, [database] * 6)
            self.assertEqual(results, ["ok"] * 6)
            connection = open_database(database)
            self.addCleanup(connection.close)
            rows = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()
            self.assertEqual(int(rows[0]), SCHEMA_VERSION)


class CrossRunResourceTest(unittest.TestCase):
    def test_a_path_resolves_to_the_newest_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            store.create_task(task_id="t1", harness_version="v")
            first = store.start_run(task_id="t1", harness_version="v")["run_id"]
            store.save_resource(
                task_id="t1", run_id=first, resource_type="extraction",
                logical_path="artifacts/x.json", content={"v": "old"},
            )
            second = store.start_run(task_id="t1", harness_version="v")["run_id"]
            store.save_resource(
                task_id="t1", run_id=second, resource_type="extraction",
                logical_path="artifacts/x.json", content={"v": "new"},
            )
            view = VirtualTaskFs(store, "t1")
            paths = [path for path, _size, _approx in view.list_files()]
            self.assertEqual(paths.count("artifacts/x.json"), 1)
            body = "\n".join(view.iter_lines("artifacts/x.json"))
            self.assertIn('"new"', body)
            self.assertEqual(len(store.search_resources(task_id="t1", pattern="old")), 0)


class AtomicPlanCommitTest(unittest.TestCase):
    def test_all_three_land_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            store = create_storage(backend="db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            logger.attach_storage(store)
            store.create_task(task_id="t1", harness_version="v")
            store.start_run(task_id="t1", harness_version="v", run_id="run-1")
            plan = {"goal": "g", "phases": [{"id": "p1"}, {"id": "p2"}]}
            path, version, state = accept_task_plan(
                logger, plan, previous_plan=None, replan_reason="",
                user_task="t", validator_review=None,
            )
            self.assertEqual(version["planVersion"], 1)
            self.assertEqual(state["plan_version"], 1)
            current, _ = store.load_snapshot(
                task_id="t1", snapshot_key="current_task_plan"
            )
            stored_state, _ = store.load_snapshot(
                task_id="t1", snapshot_key="task_state"
            )
            record = store.load_plan_version(task_id="t1", version=1)
            # One generation: all three agree on which phases exist.
            self.assertEqual([p["id"] for p in current["phases"]], ["p1", "p2"])
            self.assertEqual(sorted(stored_state["phases"]), ["p1", "p2"])
            self.assertEqual([p["id"] for p in record["plan"]["phases"]], ["p1", "p2"])

    def test_a_replan_does_not_resurrect_removed_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            store = create_storage(backend="db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            logger.attach_storage(store)
            store.create_task(task_id="t1", harness_version="v")
            store.start_run(task_id="t1", harness_version="v", run_id="run-1")
            first = {"goal": "g", "phases": [{"id": "p1"}, {"id": "p2"}]}
            _path, _v, state = accept_task_plan(
                logger, first, previous_plan=None, replan_reason="",
                user_task="t", validator_review=None,
            )
            second = {"goal": "g", "phases": [{"id": "p1"}]}
            _path, version, state = accept_task_plan(
                logger, second, previous_plan=first, replan_reason="drop p2",
                user_task="t", validator_review=None, preserve_from=state,
            )
            self.assertEqual(version["planVersion"], 2)
            self.assertEqual(sorted(state["phases"]), ["p1"])


class OversizedEventAtomicityTest(unittest.TestCase):
    def test_payload_resource_and_event_are_one_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            store.create_task(task_id="t1", harness_version="v")
            run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
            payload = {"blob": "x" * (EVENT_PAYLOAD_OFFLOAD_THRESHOLD + 500)}
            store.append_event(
                task_id="t1", run_id=run_id, event_type="huge", payload=payload
            )
            resources = store.connection.execute(
                "SELECT COUNT(*) FROM task_resources WHERE resource_type = 'event_payload'"
            ).fetchone()
            events = store.connection.execute(
                "SELECT COUNT(*) FROM run_events WHERE payload_resource_id IS NOT NULL"
            ).fetchone()
            # No orphan resource: one implies the other.
            self.assertEqual(int(resources[0]), int(events[0]))
            self.assertEqual(int(events[0]), 1)


class OperatorDeletionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"
        self.store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(self.store.close)
        self.store.create_task(task_id="t1", harness_version="v")
        self.run_id = self.store.start_run(task_id="t1", harness_version="v")["run_id"]

    def _finish(self):
        self.store.finish_run(task_id="t1", run_id=self.run_id, status="completed")

    def test_purge_refuses_a_task_that_is_not_soft_deleted(self):
        self._finish()
        with self.assertRaises(PurgeRefused):
            purge_task(self.store, "t1")

    def test_purge_refuses_while_a_run_is_still_running(self):
        soft_delete_task(self.store, "t1")
        with self.assertRaises(PurgeRefused):
            purge_task(self.store, "t1")

    def test_purge_refuses_while_a_run_lock_is_held(self):
        self._finish()
        soft_delete_task(self.store, "t1")
        (self.worktree / "t1" / ".run.lock").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(PurgeRefused):
            purge_task(self.store, "t1")

    def test_purge_removes_rows_and_owned_files_only(self):
        owned = self.worktree / "t1" / "downloads" / "mine.csv"
        owned.parent.mkdir(parents=True, exist_ok=True)
        owned.write_text("a", encoding="utf-8")
        outside = Path(self._tmp.name) / "not-mine.csv"
        outside.write_text("b", encoding="utf-8")
        self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="download",
            logical_path="downloads/mine.csv", external_path=str(owned),
        )
        self.store.save_resource(
            task_id="t1", run_id=self.run_id, resource_type="download",
            logical_path="downloads/not-mine.csv", external_path=str(outside),
        )
        self.store.append_event(
            task_id="t1", run_id=self.run_id, event_type="demo", payload={"a": 1}
        )
        self._finish()
        soft_delete_task(self.store, "t1")
        report = purge_task(self.store, "t1")

        self.assertEqual(report["status"], "purged")
        self.assertFalse(owned.exists())
        # A file the agent placed outside the worktree is not ours to delete.
        self.assertTrue(outside.exists())
        self.assertEqual(
            [str(Path(p).resolve()) for p in report["filesSkipped"]],
            [str(outside.resolve())],
        )
        self.assertIsNone(self.store.get_task("t1", include_deleted=True))
        remaining = self.store.connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE task_id = 't1'"
        ).fetchone()
        self.assertEqual(int(remaining[0]), 0)

    def test_purge_refuses_an_unknown_task(self):
        with self.assertRaises(PurgeRefused):
            purge_task(self.store, "never-existed")


class TaskSnapshotTest(unittest.TestCase):
    def test_the_listing_summary_tracks_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            store = create_storage(backend="db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            logger.attach_storage(store)
            store.create_task(task_id="t1", harness_version="v")
            store.start_run(task_id="t1", harness_version="v", run_id="run-1")
            state = load_task_state(logger)
            state["goal"] = "collect items"
            state["phases"] = {
                "p1": {"status": "validated_done"},
                "p2": {"status": "pending"},
            }
            write_task_state(logger, state, replace=True)
            snapshot = json.loads(store.get_task("t1")["snapshot_json"])
            self.assertEqual(snapshot["goal"], "collect items")
            self.assertEqual(snapshot["currentPhase"], "p2")
            self.assertEqual(snapshot["phaseCounts"]["validated_done"], 1)


class RunProvenanceTest(unittest.TestCase):
    def test_each_run_records_the_commit_that_produced_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
            self.addCleanup(store.close)
            store.create_task(task_id="t1", harness_version="v")
            store.start_run(task_id="t1", harness_version="v")
            row = store.connection.execute(
                "SELECT harness_version, git_sha FROM task_runs"
            ).fetchone()
            self.assertEqual(row["harness_version"], "v")
            # A hand-written version can go stale; the sha cannot.
            self.assertTrue(row["git_sha"])


class BackendConfigTest(unittest.TestCase):
    def test_an_unknown_backend_is_rejected_at_load(self):
        with self.assertRaises(ValueError):
            HarnessConfig.from_dict({"storage_backend": "duel"})

    def test_the_three_real_backends_load(self):
        for backend in ("file", "dual", "db"):
            self.assertEqual(
                HarnessConfig.from_dict({"storage_backend": backend}).storage_backend,
                backend,
            )

    def test_invalid_resource_compression_numbers_are_rejected_not_clamped(self):
        for config in (
            {"resource_compression_min_bytes": -1},
            {"resource_compression_min_bytes": "many"},
            {"resource_compression_level": -1},
            {"resource_compression_level": 10},
            {"resource_compression_level": "high"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                HarnessConfig.from_dict(config)

    def test_sqlite_store_direct_configuration_rejects_invalid_compression_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.db"
            with self.assertRaises(ValueError):
                SqliteStore(path, resource_compression_min_bytes=-1)
            with self.assertRaises(ValueError):
                SqliteStore(path, resource_compression_level=10)


if __name__ == "__main__":
    unittest.main()


class OffloadReadParityTest(unittest.TestCase):
    """An offloaded payload must read back identically in either backend.

    Aligning the stored JSON with what the file backend writes (indent=2) is
    what makes artifact digests comparable across a dual-to-db switch; this
    pins the consequence, which is that paging and search behave the same too.
    """

    def _read_back(self, backend):
        from harness.local_fs import local_fs_search
        from harness.offload import offload_large_tool_result

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worktree = Path(tmp.name) / "worktree"
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        store = create_storage(backend=backend, worktree_dir=str(worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        payload = {"rows": [{"rank": i, "text": "x" * 200} for i in range(400)]}
        stub = offload_large_tool_result(
            logger=logger, tool_name="Page.getSemanticTree", result=payload, step=3
        )
        full = local_fs_read(
            logger, path=stub["savedPath"], max_bytes=2_000_000, line_limit=5000
        )
        hits = local_fs_search(
            logger, glob_pattern="tool_results/*", pattern='"rank": 399'
        )
        return full, hits["count"], payload

    def test_content_and_paging_match_between_backends(self):
        from_file, file_hits, payload = self._read_back("file")
        from_db, db_hits, _ = self._read_back("db")
        self.assertEqual(from_file["content"], from_db["content"])
        self.assertEqual(from_file["linesRead"], from_db["linesRead"])
        self.assertEqual(from_file["truncated"], from_db["truncated"])
        self.assertEqual(file_hits, db_hits)
        self.assertEqual(json.loads(from_db["content"]), payload)


class SecondReviewRegressionTest(unittest.TestCase):
    """The defects a second review found in the first round of fixes.

    Each was a partial fix: the artifact gate learned to read the database
    while the summary handed to the Lead did not, the verifier learned to
    compare payloads but not offloaded ones, resume learned about ownership
    but not about backends.
    """

    def setUp(self):
        from harness.resume_state import configure_resume_storage

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(configure_resume_storage, backend=None, sqlite_path=None)

    def _db_logger(self, task_id="t1", run_id="run-1"):
        logger = RunLogger(str(self.worktree), task_id=task_id, run_id=run_id)
        store = create_storage(backend="db", worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id=task_id, harness_version="v")
        store.start_run(task_id=task_id, harness_version="v", run_id=run_id)
        return logger, store

    def _dual(self):
        file_store = FileStore(worktree_dir=str(self.worktree))
        sqlite_store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(sqlite_store.close)
        store = DualStore(file_store, sqlite_store)
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        return store, file_store, sqlite_store, run_id

    # -- 1. worker handoff summary ----------------------------------------
    def test_db_worker_summary_reports_the_rows_it_has(self):
        from harness.results.worker_result import summarize_extraction_artifacts

        logger, _store = self._db_logger()
        saved = save_extraction_artifact(
            logger=logger, runtime=SimpleNamespace(harness=SimpleNamespace(runs_dir="")),
            artifacts=[], name="items",
            rows=[{"a": 1}, {"a": 2}], schema=[{"name": "a"}],
        )
        summary = summarize_extraction_artifacts(
            [saved["savedPath"]], task_dir=logger.task_dir, logger=logger
        )[0]
        # Without the logger this said "missing"/0 while the gate said done.
        self.assertEqual(summary["status"], "included")
        self.assertEqual(summary["rowCount"], 2)

    # -- 2. dual verification ---------------------------------------------
    def test_a_mirrored_oversized_event_verifies_clean(self):
        store, _file_store, _sqlite_store, run_id = self._dual()
        store.append_event(
            task_id="t1", run_id=run_id, event_type="huge",
            payload={"blob": "x" * (EVENT_PAYLOAD_OFFLOAD_THRESHOLD + 500)},
        )
        report = store.verify(task_id="t1", run_id=run_id)
        # The database keeps this payload out of line; expecting it as a file
        # made every large event look like a mismatch.
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    def test_a_resource_lost_by_the_database_is_detected(self):
        store, _file_store, sqlite_store, run_id = self._dual()
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="observation",
            logical_path="observations/a.json", content={"a": 1},
        )
        sqlite_store.connection.execute(
            "DELETE FROM task_resources WHERE resource_type = 'observation'"
        )
        report = store.verify(task_id="t1", run_id=run_id)
        self.assertEqual(report["status"], "mismatch")
        self.assertIn(
            "resources.missingFromDb", {c["check"] for c in report["failedChecks"]}
        )

    def test_files_from_an_earlier_run_do_not_fail_verification(self):
        store, _file_store, _sqlite_store, run_id = self._dual()
        task_dir = self.worktree / "t1"
        task_dir.mkdir(parents=True, exist_ok=True)
        with (task_dir / "run.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": "x", "taskId": "t1", "type": "old",
                "payload": {}, "runId": "an-earlier-run",
            }) + "\n")
        store.append_event(
            task_id="t1", run_id=run_id, event_type="now", payload={"i": 1}
        )
        report = store.verify(task_id="t1", run_id=run_id)
        # Old worktrees are never imported, so their files are expected to be
        # there and are not this run's business.
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    # -- 3. resume ---------------------------------------------------------
    def test_dual_resume_prefers_the_file_when_the_database_lags(self):
        from harness.resume_state import configure_resume_storage, load_task_state_strict

        logger = RunLogger(str(self.worktree), task_id="t1", run_id="run-1")
        store = create_storage(backend="dual", worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        state = load_task_state(logger)
        state["phases"] = {"p1": {"status": "stale-in-db"}}
        write_task_state(logger, state, replace=True)
        # The file moves on; the mirror does not. dual runs on the files.
        (self.worktree / "t1" / "task_state.json").write_text(
            json.dumps({"phases": {"p1": {"status": "current-in-file"}}}),
            encoding="utf-8",
        )
        configure_resume_storage(
            backend="dual", sqlite_path=self.worktree / "harness.db"
        )
        resumed = load_task_state_strict(self.worktree / "t1")
        self.assertEqual(resumed["phases"]["p1"]["status"], "current-in-file")

    def test_a_custom_database_path_is_honoured(self):
        from harness.resume_state import (
            configure_resume_storage,
            load_task_state_strict,
            resume_sqlite_path,
        )

        custom = self.worktree / "custom" / "elsewhere.db"
        logger = RunLogger(str(self.worktree), task_id="t1", run_id="run-1")
        store = create_storage(
            backend="db", worktree_dir=str(self.worktree), sqlite_path=custom
        )
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        state = load_task_state(logger)
        state["phases"] = {"p1": {"status": "found"}}
        write_task_state(logger, state, replace=True)

        configure_resume_storage(backend="db", sqlite_path=custom)
        self.assertEqual(resume_sqlite_path(self.worktree / "t1"), custom)
        # Guessing "config.json in the cwd" ignored --config entirely.
        resumed = load_task_state_strict(self.worktree / "t1")
        self.assertEqual(resumed["phases"]["p1"]["status"], "found")

    def test_main_recovers_the_first_plan_not_the_current_one(self):
        from harness.resume_state import configure_resume_storage
        from main import _load_initial_plan

        logger, store = self._db_logger()
        configure_resume_storage(
            backend="db", sqlite_path=self.worktree / "harness.db"
        )
        first = {"goal": "g", "phases": [{"id": "p1"}]}
        second = {"goal": "g", "phases": [{"id": "p1"}, {"id": "p2"}]}
        accept_task_plan(
            logger, first, previous_plan=None, replan_reason="",
            user_task="t", validator_review=None,
        )
        _path, _v, state = accept_task_plan(
            logger, second, previous_plan=first, replan_reason="add",
            user_task="t", validator_review=None,
        )
        plan, recovered = _load_initial_plan(self.worktree / "t1", second)
        # Falling back to the current plan makes the immutable contract a
        # replan is audited against whatever the plan happens to be now.
        self.assertTrue(recovered)
        self.assertEqual([p["id"] for p in plan["phases"]], ["p1"])

    # -- 4. purge ----------------------------------------------------------
    def test_a_failed_unlink_keeps_the_rows_and_marks_the_task(self):
        from unittest import mock

        store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        owned = self.worktree / "t1" / "downloads" / "x.csv"
        owned.parent.mkdir(parents=True, exist_ok=True)
        owned.write_text("a", encoding="utf-8")
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="download",
            logical_path="downloads/x.csv", external_path=str(owned),
        )
        store.finish_run(task_id="t1", run_id=run_id, status="completed")
        soft_delete_task(store, "t1")

        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            with self.assertRaises(PurgeRefused):
                purge_task(store, "t1")

        row = store.get_task("t1", include_deleted=True)
        # Dropping the rows would strand the file with nothing recording what
        # it belonged to, so the task stays failed and retryable.
        self.assertIsNotNone(row)
        self.assertEqual(row["purge_status"], "failed")
        self.assertTrue(owned.exists())

    # -- 5. task snapshot --------------------------------------------------
    def test_accepting_a_plan_refreshes_the_listing_summary(self):
        logger, store = self._db_logger()
        accept_task_plan(
            logger, {"goal": "collect", "phases": [{"id": "p1"}, {"id": "p2"}]},
            previous_plan=None, replan_reason="", user_task="t",
            validator_review=None,
        )
        snapshot = json.loads(store.get_task("t1")["snapshot_json"])
        # Previously stayed {} until the first phase result was written.
        self.assertEqual(snapshot["phaseCounts"], {"pending": 2})
        self.assertEqual(snapshot["planVersion"], 1)

    # -- 6. provenance -----------------------------------------------------
    def test_a_dirty_working_tree_is_marked_in_the_revision(self):
        from harness.version import git_revision

        revision = git_revision()
        if revision:
            self.assertRegex(revision, r"^[0-9a-f]+(-dirty)?$")


class ThirdReviewRegressionTest(unittest.TestCase):
    """What a third review reproduced against the second round of fixes.

    The pattern repeats: each fix addressed the case it was written for and
    left an adjacent one open. Purge stopped deleting rows after a failed
    unlink but still deleted them after a failed rmtree; resume stopped
    preferring files by default but still fell back to them when the
    authoritative database came up empty; the verifier started checking
    payloads but still checked resources by path alone.
    """

    def setUp(self):
        from harness.resume_state import configure_resume_storage

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(configure_resume_storage, backend=None, sqlite_path=None)

    def _dual(self):
        file_store = FileStore(worktree_dir=str(self.worktree))
        sqlite_store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(sqlite_store.close)
        store = DualStore(file_store, sqlite_store)
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        return store, file_store, sqlite_store, run_id

    def _db_store(self):
        store = SqliteStore(self.worktree / "harness.db", worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        return store

    def _failed_checks(self, report):
        return {check["check"] for check in report["failedChecks"]}

    # -- 1. purge ----------------------------------------------------------
    def test_an_undeletable_directory_keeps_the_rows(self):
        from unittest import mock

        store = self._db_store()
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        store.finish_run(task_id="t1", run_id=run_id, status="completed")
        task_dir = self.worktree / "t1"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "run.jsonl").write_text("{}\n", encoding="utf-8")
        soft_delete_task(store, "t1")

        with mock.patch(
            "harness.storage.admin.shutil.rmtree", side_effect=OSError("busy")
        ):
            with self.assertRaises(PurgeRefused):
                purge_task(store, "t1")

        # Deleting the rows first and ignoring the rmtree result reported
        # "purged" over a directory that was still sitting there.
        row = store.get_task("t1", include_deleted=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["purge_status"], "failed")
        self.assertTrue(task_dir.is_dir())

    def test_a_successful_purge_leaves_neither_rows_nor_directory(self):
        store = self._db_store()
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        store.finish_run(task_id="t1", run_id=run_id, status="completed")
        task_dir = self.worktree / "t1"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "run.jsonl").write_text("{}\n", encoding="utf-8")
        soft_delete_task(store, "t1")

        result = purge_task(store, "t1")
        self.assertEqual(result["status"], "purged")
        self.assertTrue(result["taskDirectoryRemoved"])
        self.assertFalse(task_dir.exists())
        self.assertIsNone(store.get_task("t1", include_deleted=True))

    # -- 2. db mode never falls back to files ------------------------------
    def test_a_registered_task_with_no_snapshot_refuses_the_stale_file(self):
        from harness.resume_state import (
            ResumeStateError,
            configure_resume_storage,
            load_task_state_strict,
        )

        store = self._db_store()
        store.create_task(task_id="t1", harness_version="v")
        task_dir = self.worktree / "t1"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task_state.json").write_text(
            json.dumps({"phases": {"p1": {"status": "from-a-previous-life"}}}),
            encoding="utf-8",
        )
        configure_resume_storage(backend="db", sqlite_path=self.worktree / "harness.db")

        # A lost write in the authoritative store used to read as a clean
        # recovery from whatever the filesystem still held.
        with self.assertRaises(ResumeStateError):
            load_task_state_strict(task_dir)

    def test_a_registered_task_with_no_plan_row_refuses_the_history_file(self):
        from harness.resume_state import (
            ResumeStateError,
            configure_resume_storage,
            load_initial_task_plan_strict,
        )

        store = self._db_store()
        store.create_task(task_id="t1", harness_version="v")
        history = self.worktree / "t1" / "task_plan_history"
        history.mkdir(parents=True, exist_ok=True)
        (history / "plan.0001.json").write_text(
            json.dumps({"plan": {"phases": [{"id": "p1"}]}}), encoding="utf-8"
        )
        configure_resume_storage(backend="db", sqlite_path=self.worktree / "harness.db")

        with self.assertRaises(ResumeStateError):
            load_initial_task_plan_strict(self.worktree / "t1")

    def test_a_database_read_failure_is_raised_not_swallowed(self):
        from unittest import mock

        from harness.resume_state import (
            ResumeStateError,
            configure_resume_storage,
            load_task_state_strict,
        )

        store = self._db_store()
        store.create_task(task_id="t1", harness_version="v")
        task_dir = self.worktree / "t1"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task_state.json").write_text(
            json.dumps({"phases": {"p1": {"status": "from-a-previous-life"}}}),
            encoding="utf-8",
        )
        configure_resume_storage(backend="db", sqlite_path=self.worktree / "harness.db")

        with mock.patch.object(
            SqliteStore, "get_task", side_effect=RuntimeError("database is locked")
        ):
            # Returning "the database does not know this task" on any error
            # turned a broken database into a silent downgrade to files.
            with self.assertRaises(ResumeStateError):
                load_task_state_strict(task_dir)

    def test_a_legacy_worktree_still_resumes_from_files_in_db_mode(self):
        from harness.resume_state import configure_resume_storage, load_task_state_strict

        self._db_store()  # a database exists, but this task was never registered
        task_dir = self.worktree / "t1"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task_state.json").write_text(
            json.dumps({"phases": {"p1": {"status": "legacy"}}}), encoding="utf-8"
        )
        configure_resume_storage(backend="db", sqlite_path=self.worktree / "harness.db")

        # The refusal above must not strand tasks that predate the database.
        resumed = load_task_state_strict(task_dir)
        self.assertEqual(resumed["phases"]["p1"]["status"], "legacy")

    # -- 3. the verifier compares content ----------------------------------
    def test_rewritten_resource_content_in_the_database_is_a_mismatch(self):
        store, _file_store, sqlite_store, run_id = self._dual()
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="observation",
            logical_path="observations/a.json", content={"rows": 2},
        )
        sqlite_store.connection.execute(
            "UPDATE task_resources SET content_json = ? WHERE logical_path = ?",
            (json.dumps({"rows": 99}), "observations/a.json"),
        )
        report = store.verify(task_id="t1", run_id=run_id)
        # Comparing paths alone declared a silently rewritten resource fine.
        self.assertEqual(report["status"], "mismatch")
        self.assertIn("resources.db.content", self._failed_checks(report))

    def test_rewritten_resource_content_on_disk_is_a_mismatch(self):
        store, _file_store, _sqlite_store, run_id = self._dual()
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="observation",
            logical_path="observations/a.json", content={"rows": 2},
        )
        (self.worktree / "t1" / "observations" / "a.json").write_text(
            json.dumps({"rows": 99}), encoding="utf-8"
        )
        report = store.verify(task_id="t1", run_id=run_id)
        self.assertEqual(report["status"], "mismatch")
        self.assertIn("resources.file.content", self._failed_checks(report))

    def test_text_and_json_resources_both_verify_clean(self):
        store, _file_store, _sqlite_store, run_id = self._dual()
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="observation",
            logical_path="observations/a.json", content={"rows": 2},
        )
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="extraction",
            logical_path="extractions/a.txt", content="a,b\n1,2\n",
            media_type="text/csv",
        )
        report = store.verify(task_id="t1", run_id=run_id)
        # The two backends store these in different shapes; only a mismatch
        # here would mean content actually diverged.
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    def test_an_earlier_runs_deleted_resource_is_not_this_runs_problem(self):
        store, _file_store, sqlite_store, first_run = self._dual()
        store.save_resource(
            task_id="t1", run_id=first_run, resource_type="observation",
            logical_path="observations/old.json", content={"a": 1},
        )
        store.finish_run(task_id="t1", run_id=first_run, status="completed")
        (self.worktree / "t1" / "observations" / "old.json").unlink()

        second_run = store.start_run(task_id="t1", harness_version="v")["run_id"]
        store.append_event(
            task_id="t1", run_id=second_run, event_type="now", payload={"i": 1}
        )
        report = store.verify(task_id="t1", run_id=second_run)
        # Sweeping every row the database holds blamed this run for a file an
        # earlier one wrote and something later removed.
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    def test_managed_absolute_external_path_verifies_clean(self):
        store, _file_store, sqlite_store, run_id = self._dual()
        target = self.worktree / "t1" / "downloads" / "report.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        saved = store.save_resource(
            task_id="t1", run_id=run_id, resource_type="download",
            logical_path="downloads/report.csv", external_path=str(target),
        )

        report = store.verify(task_id="t1", run_id=run_id)
        self.assertEqual(report["status"], "ok", report["failedChecks"])
        rows = sqlite_store.connection.execute(
            "SELECT external_path FROM task_resources WHERE resource_type = 'download'"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["downloads/report.csv"])
        self.assertTrue(saved.get("saved_path"))

    def test_relative_external_path_is_relative_to_the_task_not_cwd(self):
        store = self._db_store()
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        target = self.worktree / "t1" / "downloads" / "relative.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value\n1\n", encoding="utf-8")

        saved = store.save_resource(
            task_id="t1", run_id=run_id, resource_type="download",
            logical_path="downloads/relative.csv",
            external_path="downloads/relative.csv",
        )
        read = store.read_resource(
            current_task_id="t1", resource_uri=saved["saved_path"]
        )
        self.assertEqual(read["external_path"], "downloads/relative.csv")
        self.assertTrue(read["content_available"])
        self.assertIsNotNone(saved["sha256"])

    def test_unmanaged_external_path_verifies_but_stays_absolute(self):
        store, _file_store, sqlite_store, run_id = self._dual()
        target = Path(self._tmp.name) / "outside.csv"
        target.write_text("outside", encoding="utf-8")
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="download",
            logical_path="external/downloads/outside.csv",
            external_path=str(target),
        )

        report = store.verify(task_id="t1", run_id=run_id)
        self.assertEqual(report["status"], "ok", report["failedChecks"])
        row = sqlite_store.connection.execute(
            "SELECT external_path, metadata_json FROM task_resources"
            " WHERE resource_type = 'download'"
        ).fetchone()
        self.assertEqual(row["external_path"], str(target.resolve()))
        self.assertTrue(json.loads(row["metadata_json"])["external_unmanaged"])

    def test_tampered_external_path_in_the_database_is_a_mismatch(self):
        store, _file_store, sqlite_store, run_id = self._dual()
        target = self.worktree / "t1" / "downloads" / "report.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("a", encoding="utf-8")
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="download",
            logical_path="downloads/report.csv", external_path=str(target),
        )
        sqlite_store.connection.execute(
            "UPDATE task_resources SET external_path = 'downloads/other.csv'"
        )

        report = store.verify(task_id="t1", run_id=run_id)
        self.assertIn("resources.db.content", self._failed_checks(report))

    def test_download_receipt_registers_once_then_refreshes_when_file_appears(self):
        from harness.tools.browser_tools import _remember_download_record

        store, _file_store, sqlite_store, run_id = self._dual()
        logger = RunLogger(str(self.worktree), task_id="t1", run_id=run_id)
        logger.attach_storage(store)
        agent = SimpleNamespace(logger=logger, download_operation_receipts={})
        target = self.worktree / "t1" / "downloads" / "late.csv"
        receipt = {
            "url": "https://example.test/late.csv",
            "savePath": str(target),
            "id": "download-1",
            "state": "completed",
            "totalBytes": 10,
            "receivedBytes": 10,
        }

        # The browser can report completion just before the file becomes
        # visible to this process. That first row intentionally has no hash.
        _remember_download_record(agent, receipt)
        _remember_download_record(agent, receipt)
        rows = sqlite_store.connection.execute(
            "SELECT resource_version, sha256 FROM task_resources"
            " WHERE resource_type = 'download' ORDER BY resource_version"
        ).fetchall()
        self.assertEqual(len(rows), 1)  # identical receipt was deduplicated
        self.assertIsNone(rows[0]["sha256"])

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("now visible", encoding="utf-8")
        _remember_download_record(agent, receipt)
        rows = sqlite_store.connection.execute(
            "SELECT resource_version, sha256, is_current FROM task_resources"
            " WHERE resource_type = 'download' ORDER BY resource_version"
        ).fetchall()
        self.assertEqual([row["resource_version"] for row in rows], [1, 2])
        self.assertEqual([row["is_current"] for row in rows], [0, 1])
        self.assertIsNotNone(rows[-1]["sha256"])
        report = store.verify(task_id="t1", run_id=run_id)
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    def test_timeout_reconciliation_registers_the_proven_download(self):
        from harness.tools.browser_tools import _reconcile_download_start_timeout

        store, _file_store, sqlite_store, run_id = self._dual()
        logger = RunLogger(str(self.worktree), task_id="t1", run_id=run_id)
        logger.attach_storage(store)
        agent = SimpleNamespace(
            logger=logger,
            assigned_fleet_id="fleet-1",
            download_operation_receipts={},
        )
        target = self.worktree / "t1" / "downloads" / "reconciled.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("reconciled", encoding="utf-8")
        params = {
            "url": "https://example.test/reconciled.csv",
            "savePath": str(target),
        }

        class Runner:
            async def call(self, method, _params):
                if method != "Download.list":
                    raise AssertionError(method)
                return ({"data": [{
                    **params,
                    "id": "download-reconciled",
                    "state": "completed",
                    "totalBytes": target.stat().st_size,
                    "receivedBytes": target.stat().st_size,
                }]}, None)

        result = asyncio.run(_reconcile_download_start_timeout(
            agent=agent, runner=Runner(), params=params,
        ))
        self.assertEqual(result["classification"], "completed")
        row = sqlite_store.connection.execute(
            "SELECT external_path, sha256 FROM task_resources"
            " WHERE resource_type = 'download'"
        ).fetchone()
        self.assertEqual(row["external_path"], "downloads/reconciled.csv")
        self.assertIsNotNone(row["sha256"])
        report = store.verify(task_id="t1", run_id=run_id)
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    # -- 4. duplicates are counted, not deduplicated -----------------------
    def test_one_of_two_identical_traces_lost_on_disk_is_detected(self):
        store, _file_store, _sqlite_store, run_id = self._dual()
        entry = {"step": 1, "action": "click"}
        store.append_worker_trace(
            task_id="t1", run_id=run_id, worker_id="w1", entries=[entry, entry]
        )
        trace_file = self.worktree / "t1" / "traces" / "w1.jsonl"
        lines = [line for line in trace_file.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2)
        trace_file.write_text(lines[0] + "\n", encoding="utf-8")

        report = store.verify(task_id="t1", run_id=run_id)
        # A set difference treated the surviving copy as covering both writes.
        self.assertEqual(report["status"], "mismatch")
        self.assertIn("trace.file.contains", self._failed_checks(report))

    # -- 5. strategy attempts are compared by content ----------------------
    def test_a_rewritten_strategy_attempt_is_a_mismatch(self):
        store, _file_store, sqlite_store, run_id = self._dual()
        store.append_strategy_attempt(
            task_id="t1", run_id=run_id,
            payload={
                "phaseId": "p1", "workerId": "w1", "strategy_ids": ["s1"],
                "status": "done", "statusCategory": "success",
                "rowCount": 12, "artifactCount": 1,
            },
        )
        sqlite_store.connection.execute(
            "UPDATE strategy_attempts SET status = 'failed', row_count = 0"
        )
        report = store.verify(task_id="t1", run_id=run_id)
        # Counting rows could not tell a rewritten attempt from a correct one.
        self.assertEqual(report["status"], "mismatch")
        self.assertIn("strategy.db", self._failed_checks(report))

    def test_a_mirrored_strategy_attempt_verifies_clean(self):
        store, _file_store, _sqlite_store, run_id = self._dual()
        store.append_strategy_attempt(
            task_id="t1", run_id=run_id,
            payload={
                "phaseId": "p1", "workerId": "w1", "strategy_ids": ["s1"],
                "status": "done", "statusCategory": "success",
                "rowCount": 12, "artifactCount": 1,
            },
        )
        report = store.verify(task_id="t1", run_id=run_id)
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    # -- 6. the listing summary rides the plan transaction -----------------
    def test_a_failed_summary_rolls_the_whole_generation_back(self):
        store = self._db_store()
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]

        def explode(_state):
            raise RuntimeError("summary is broken")

        with self.assertRaises(RuntimeError):
            store.commit_accepted_plan(
                task_id="t1", run_id=run_id,
                plan_record={"plan": {"phases": [{"id": "p1"}]}, "planHash": "h"},
                current_plan={"phases": [{"id": "p1"}]},
                task_state={"phases": {"p1": {"status": "pending"}}},
                summarize=explode,
            )

        # One transaction: the plan version, the state and the summary either
        # all land or none do.
        self.assertIsNone(store.load_plan_version(task_id="t1", version=1))
        value, _revision = store.load_snapshot(task_id="t1", snapshot_key="task_state")
        self.assertEqual(value, {})
        self.assertIn(store.get_task("t1")["snapshot_json"], (None, "", "{}"))


class CompactResourceStorageTest(unittest.TestCase):
    """Storing JSON compact must not change a single thing a reader sees.

    ``indent=2`` was 76% of the resource bytes in a real run. Removing it from
    the column is only safe if the logical file - its text, its line numbers,
    its size, its digest - stays exactly what it was, which is what every test
    here pins.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"

    def _logger(self, backend, task_id="t1"):
        logger = RunLogger(str(self.worktree), task_id=task_id, run_id="run-1")
        store = create_storage(backend=backend, worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id=task_id, harness_version="v")
        store.start_run(task_id=task_id, harness_version="v", run_id="run-1")
        return logger, store

    @staticmethod
    def _payload():
        return {"rows": [{"rank": i, "text": "x" * 80} for i in range(60)]}

    def _row(self, store, path="observations/a.json"):
        return store.connection.execute(
            "SELECT * FROM task_resources WHERE logical_path = ? AND is_current = 1",
            (path,),
        ).fetchone()

    # -- 1. old and new rows are indistinguishable to a reader -------------
    def test_a_pretty_row_and_a_compact_row_read_identically(self):
        _logger, store = self._logger("db")
        content = self._payload()
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="observations/a.json", content=content,
        )
        compact_lines = list(VirtualTaskFs(store, "t1").iter_lines("observations/a.json"))

        # Rewrite the column the way every row written before the split looks.
        store.connection.execute(
            "UPDATE task_resources SET content_json = ? WHERE logical_path = ?",
            (json.dumps(content, ensure_ascii=False, indent=2), "observations/a.json"),
        )
        pretty_lines = list(VirtualTaskFs(store, "t1").iter_lines("observations/a.json"))
        # No migration is needed precisely because these are equal.
        self.assertEqual(compact_lines, pretty_lines)
        self.assertGreater(len(compact_lines), 100)

    def test_the_column_is_compact_while_the_file_is_not(self):
        _logger, store = self._logger("db")
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="observations/a.json", content=self._payload(),
        )
        row = self._row(store)
        self.assertNotIn("\n", row["content_json"])
        self.assertGreater(
            len(list(VirtualTaskFs(store, "t1").iter_lines("observations/a.json"))), 100
        )

    # -- 2/3. the model-facing read and search are unchanged ---------------
    def _read_and_search(self, backend):
        from harness.local_fs import local_fs_search
        from harness.offload import offload_large_tool_result

        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        logger, _store = self._logger(backend)
        stub = offload_large_tool_result(
            logger=logger, tool_name="Page.getSemanticTree",
            result={"rows": [{"rank": i, "text": "y" * 200} for i in range(400)]},
            step=3,
        )
        page = local_fs_read(
            logger, path=stub["savedPath"], line_offset=40, line_limit=25,
            max_bytes=2_000_000,
        )
        budget = local_fs_read(logger, path=stub["savedPath"], max_bytes=4096)
        # Anchored per line: meaningless against one long compact line.
        hits = local_fs_search(
            logger, glob_pattern="tool_results/*", pattern=r'^\s+"rank": 399,?$'
        )
        return page, budget, hits

    def test_paging_and_truncation_match_the_file_backend(self):
        file_page, file_budget, _ = self._read_and_search("file")
        db_page, db_budget, _ = self._read_and_search("db")
        self.assertEqual(file_page["content"], db_page["content"])
        self.assertEqual(file_page["linesRead"], db_page["linesRead"])
        self.assertEqual(file_page["nextLineOffset"], db_page["nextLineOffset"])
        self.assertEqual(file_budget["content"], db_budget["content"])
        self.assertEqual(file_budget["truncated"], db_budget["truncated"])

    def test_an_anchored_regex_matches_the_same_lines(self):
        _fp, _fb, file_hits = self._read_and_search("file")
        _dp, _db, db_hits = self._read_and_search("db")
        self.assertEqual(file_hits["count"], db_hits["count"])
        self.assertEqual(file_hits["count"], 1)
        self.assertEqual(
            [hit.get("matches") for hit in file_hits["results"]],
            [hit.get("matches") for hit in db_hits["results"]],
        )

    def test_the_storage_search_api_also_matches_the_logical_text(self):
        _logger, store = self._logger("db")
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="observations/a.json", content=self._payload(),
        )
        # Regexing the stored column would find nothing: it has no line breaks.
        found = store.search_resources(
            task_id="t1", path_glob="**/*", pattern=r'^\s+"rank": 59,?$'
        )
        self.assertEqual([row["logical_path"] for row in found], ["observations/a.json"])

    # -- 4. the artifact digest is unchanged -------------------------------
    def test_an_extraction_digest_survives_compact_storage(self):
        from harness.task_control import _artifact_sha256

        digests = {}
        for backend in ("file", "db"):
            self._tmp.cleanup()
            self._tmp = tempfile.TemporaryDirectory()
            self.worktree = Path(self._tmp.name) / "worktree"
            logger, _store = self._logger(backend)
            saved = save_extraction_artifact(
                logger=logger,
                runtime=SimpleNamespace(harness=SimpleNamespace(runs_dir="")),
                artifacts=[], name="items",
                rows=[{"a": i, "b": "z" * 50} for i in range(30)],
                schema=[{"name": "a"}, {"name": "b"}],
            )
            digests[backend] = _artifact_sha256(Path(saved["savedPath"]), logger)
        self.assertTrue(digests["file"])
        # A formatting difference here reads as corruption after a switch.
        self.assertEqual(digests["file"], digests["db"])

    # -- 5. sizes and hashes describe the logical file ---------------------
    def test_byte_size_stays_the_logical_size(self):
        _logger, store = self._logger("db")
        content = self._payload()
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="observations/a.json", content=content,
        )
        row = self._row(store)
        rendered = json.dumps(content, ensure_ascii=False, indent=2)
        # A model pages against byte_size; the compact number would understate
        # every read by the size of the indentation.
        self.assertEqual(row["byte_size"], len(rendered.encode("utf-8")))
        self.assertEqual(
            row["stored_byte_size"], len(row["content_json"].encode("utf-8"))
        )
        self.assertLess(row["stored_byte_size"], row["byte_size"])

    def test_sha256_stays_the_logical_digest(self):
        import hashlib

        _logger, store = self._logger("db")
        content = self._payload()
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="observations/a.json", content=content,
        )
        rendered = json.dumps(content, ensure_ascii=False, indent=2)
        self.assertEqual(
            self._row(store)["sha256"],
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )

    # -- 6. non-JSON content is untouched ----------------------------------
    def test_text_and_binary_content_are_stored_verbatim(self):
        _logger, store = self._logger("db")
        text = "rank,name\n1,  spaced  \n2,\"quoted\"\n"
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="extraction",
            logical_path="artifacts/a.csv", content=text, media_type="text/csv",
        )
        blob = bytes(range(256))
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="extraction",
            logical_path="artifacts/a.bin", content=blob,
            media_type="application/octet-stream",
        )
        csv_row = self._row(store, "artifacts/a.csv")
        # Round-tripping these through a JSON parser would corrupt them.
        self.assertEqual(csv_row["content_text"], text)
        self.assertIsNone(csv_row["stored_byte_size"])
        self.assertEqual(csv_row["byte_size"], len(text.encode("utf-8")))
        # Lines carry their terminators, so concatenating is the file itself -
        # trailing newline included.
        self.assertEqual(
            "".join(VirtualTaskFs(store, "t1").iter_lines("artifacts/a.csv")), text
        )
        self.assertEqual(self._row(store, "artifacts/a.bin")["content_blob"], blob)

    # -- 7. dual verification is unaffected --------------------------------
    def test_dual_verification_passes_with_compact_storage(self):
        file_store = FileStore(worktree_dir=str(self.worktree))
        sqlite_store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(sqlite_store.close)
        store = DualStore(file_store, sqlite_store)
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="observation",
            logical_path="observations/a.json", content=self._payload(),
        )
        report = store.verify(task_id="t1", run_id=run_id)
        # One side pretty, one side compact - equal only semantically.
        self.assertEqual(report["status"], "ok", report["failedChecks"])


class VerificationReportSizeTest(unittest.TestCase):
    """A clean report recorded 6747 SHA-256 twice and became a 2MB log line."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"
        file_store = FileStore(worktree_dir=str(self.worktree))
        self.sqlite_store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(self.sqlite_store.close)
        self.store = DualStore(file_store, self.sqlite_store)
        self.store.create_task(task_id="t1", harness_version="v")
        self.run_id = self.store.start_run(task_id="t1", harness_version="v")["run_id"]

    def _write_events(self, count=300):
        for index in range(count):
            self.store.append_event(
                task_id="t1", run_id=self.run_id, event_type="step",
                payload={"i": index},
            )

    def test_a_clean_report_keeps_counts_not_digest_lists(self):
        self._write_events()
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertEqual(report["status"], "ok")
        events = next(c for c in report["checks"] if c["check"] == "events.db")
        self.assertEqual(events["expectedCount"], 300)
        self.assertEqual(events["actualCount"], 300)
        self.assertEqual(events["expectedDigest"], events["actualDigest"])
        self.assertNotIn("expected", events)
        self.assertNotIn("actual", events)
        # 300 events used to cost ~40KB of report; the whole thing is now small
        # enough to stay an inline event rather than an offloaded resource.
        self.assertLess(len(json.dumps(report)), 4096)

    def test_a_failing_check_still_says_what_and_where(self):
        self._write_events(count=5)
        self.sqlite_store.connection.execute(
            "DELETE FROM run_events WHERE task_id = 't1' AND event_type = 'step'"
            " AND event_id = (SELECT MIN(event_id) FROM run_events)"
        )
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        self.assertEqual(report["status"], "mismatch")
        events = next(c for c in report["failedChecks"] if c["check"] == "events.db")
        self.assertEqual(events["expectedCount"], 5)
        self.assertEqual(events["actualCount"], 4)
        self.assertEqual(events["missingCount"], 1)
        self.assertEqual(events["extraCount"], 0)
        self.assertEqual(len(events["missingSample"]), 1)
        # Dropping the first row shifts everything after it, and the scan runs
        # past the shorter list so the missing tail counts as a position too.
        self.assertEqual(events["firstDiffPositions"][0], 0)
        self.assertEqual(events["positionalDiffCount"], 5)

    def test_scalar_checks_keep_both_values(self):
        self.store.save_snapshot(
            task_id="t1", snapshot_key="task_state", base=None,
            proposed={"phases": {}}, updated_run_id=self.run_id, replace=True,
        )
        report = self.store.verify(task_id="t1", run_id=self.run_id)
        snapshot = next(
            c for c in report["checks"] if c["check"].startswith("snapshot.")
        )
        # A digest pair is already two short strings; summarising would lose
        # the only information the check has.
        self.assertIn("expected", snapshot)
        self.assertEqual(snapshot["expected"], snapshot["actual"])


class FourthReviewRegressionTest(unittest.TestCase):
    """The counterexamples a fourth review reproduced against compact storage.

    Two of them are the same shape as earlier rounds: a value derived twice
    instead of once, and an edge the parity tests missed because they only
    exercised the comfortable middle of the range.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"

    def _logger(self, backend):
        logger = RunLogger(str(self.worktree), task_id="t1", run_id="run-1")
        store = create_storage(backend=backend, worktree_dir=str(self.worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        return logger, store

    # -- 1. the stored bytes are the hashed bytes --------------------------
    def test_an_unstable_str_cannot_desynchronise_hash_and_content(self):
        from harness.storage.resource_codec import encode_json_resource

        class Drifting:
            """Stringifies differently every time - a clock, a counter, an id."""

            def __init__(self):
                self.calls = 0

            def __str__(self):
                self.calls += 1
                return f"value-{self.calls}"

        drifting = Drifting()
        encoded = encode_json_resource({"x": drifting})
        # Encoding the object twice let the row hold value-2 while its sha256
        # described value-1, which the artifact gate reads as corruption.
        self.assertEqual(json.loads(encoded.stored_text), json.loads(encoded.logical_text))
        self.assertEqual(drifting.calls, 1)

    def test_the_stored_row_round_trips_to_the_hashed_text(self):
        import hashlib

        from harness.storage.resource_codec import encode_json_resource, render_json_text

        encoded = encode_json_resource({"rows": [{"a": i} for i in range(20)]})
        self.assertEqual(render_json_text(encoded.stored_text), encoded.logical_text)
        self.assertEqual(
            encoded.logical_sha256,
            hashlib.sha256(encoded.logical_text.encode("utf-8")).hexdigest(),
        )

    # -- 2. an exact-fit read is complete, not truncated -------------------
    def _exact_fit_read(self, backend):
        logger, store = self._logger(backend)
        content = {"a": 1}
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="observations/tiny.json", content=content,
        )
        size = len(json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8"))
        return local_fs_read(
            logger, path="observations/tiny.json", max_bytes=size, line_limit=5000
        ), size

    def test_a_read_that_exactly_fills_its_budget_is_not_truncated(self):
        self.worktree = Path(self._tmp.name) / "file-wt"
        from_file, size = self._exact_fit_read("file")
        self.worktree = Path(self._tmp.name) / "db-wt"
        from_db, _ = self._exact_fit_read("db")

        # Charging the last line a newline it does not have reported a
        # complete read as truncated and pointed the next page back at it.
        self.assertFalse(from_file["truncated"])
        self.assertFalse(from_db["truncated"])
        self.assertIsNone(from_db["nextLineOffset"])
        self.assertEqual(from_file["content"], from_db["content"])
        self.assertEqual(from_db["byteSize"], size)

    def test_a_jsonl_view_still_charges_for_its_line_endings(self):
        logger, store = self._logger("db")
        for index in range(3):
            store.append_event(
                task_id="t1", run_id="run-1", event_type="e", payload={"i": index}
            )
        full = local_fs_read(logger, path="run.jsonl", max_bytes=200000)
        rendered = "".join(
            VirtualTaskFs(store, "t1").iter_lines("run.jsonl")
        )
        # Every JSONL line is terminated, so the budget must count those bytes
        # even though the content field strips them again.
        self.assertTrue(rendered.endswith("\n"))
        # byteSize describes the whole file, as stat() does on the disk path;
        # bytesRead is what this call actually returned.
        self.assertEqual(full["bytesRead"], len(rendered.encode("utf-8")))
        self.assertTrue(full["byteSizeApproximate"])
        self.assertEqual(full["linesRead"], 3)

    def test_read_task_file_text_reproduces_the_file_exactly(self):
        from harness.utils import read_task_file_text

        logger, _store = self._logger("db")
        text = "a,b\n1,2\n"
        _store.save_resource(
            task_id="t1", run_id="run-1", resource_type="extraction",
            logical_path="artifacts/a.csv", content=text, media_type="text/csv",
        )
        # "\n".join over stripped lines silently dropped the trailing newline.
        self.assertEqual(read_task_file_text(logger, "artifacts/a.csv"), text)

    # -- 3. both backends read a pattern the same way ----------------------
    def test_both_backends_apply_the_same_regex_semantics(self):
        content = {"rows": [{"rank": i} for i in range(60)]}
        found = {}
        for backend in ("file", "db"):
            self.worktree = Path(self._tmp.name) / f"{backend}-wt"
            _logger, store = self._logger(backend)
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path="observations/a.json", content=content,
            )
            found[backend] = [
                row["logical_path"] for row in store.search_resources(
                    task_id="t1", path_glob="**/*", pattern=r'^\s+"rank": 59$'
                )
            ]
        # Anchors meant line boundaries on one backend and string boundaries
        # on the other, so a dual-to-db switch changed what search returned.
        self.assertEqual(found["file"], found["db"])
        self.assertEqual(found["db"], ["observations/a.json"])

    # -- 4. the failure report does not contradict itself ------------------
    def test_a_pure_reordering_reports_positions_not_zero(self):
        from harness.storage.dual_store import _summarize_check

        entry = _summarize_check("events.db", ["a", "b"], ["b", "a"])
        self.assertFalse(entry["ok"])
        # Nothing is missing and nothing is extra, yet every slot differs.
        self.assertEqual(entry["missingCount"], 0)
        self.assertEqual(entry["extraCount"], 0)
        self.assertEqual(entry["positionalDiffCount"], 2)
        self.assertEqual(entry["firstDiffPositions"], [0, 1])

    def test_a_truncated_side_reports_where_it_ran_out(self):
        from harness.storage.dual_store import _summarize_check

        entry = _summarize_check("events.db", ["a", "b", "c"], ["a", "b"])
        # zip() stopped at the shorter list and reported no differing position
        # at all for a side that is simply missing its tail.
        self.assertEqual(entry["firstDiffPositions"], [2])
        self.assertEqual(entry["missingCount"], 1)
        self.assertEqual(entry["extraCount"], 0)

    # -- 5. a transient git failure must not poison the run ----------------
    def test_a_failed_git_call_is_not_cached(self):
        from unittest import mock

        import harness.version as version

        version._GIT_CACHE.clear()
        self.addCleanup(version._GIT_CACHE.clear)
        with mock.patch.object(
            version.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)
        ):
            self.assertEqual(version.git_sha(), "")
        # lru_cache memoised the failure, so one slow git call under load
        # blanked git_sha for every run in the process.
        self.assertTrue(version.git_sha())


class FifthReviewRegressionTest(unittest.TestCase):
    """Divergences a fifth review found between the two backends.

    All three are the same failure mode as the earlier rounds: the file and
    database paths agreed on the comfortable case and quietly disagreed at an
    edge - a lock held slightly too long, a Windows line ending, a result
    limit.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name) / "worktree"

    def _logger(self, backend, worktree=None):
        worktree = worktree or self.worktree
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        store = create_storage(backend=backend, worktree_dir=str(worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        return logger, store

    # -- 1. first-launch contention ----------------------------------------
    def test_the_configured_timeout_applies_before_any_locking_statement(self):
        from harness.storage.sqlite_connection import configure_connection

        class Recorder:
            """Records every statement on its way to a real connection."""

            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)
                object.__setattr__(self, "statements", [])

            def execute(self, sql, *args, **kwargs):
                self.statements.append(str(sql))
                return self._connection.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                setattr(self._connection, name, value)

        self.worktree.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(self.worktree / "harness.db"), isolation_level=None)
        self.addCleanup(raw.close)
        recorder = Recorder(raw)
        configure_connection(recorder, busy_timeout_ms=7000)
        # Everything issued before this was bounded by sqlite3.connect's own
        # default rather than by the timeout this harness was configured with,
        # and reading sqlite_master or converting to WAL can both contend.
        self.assertEqual(
            recorder.statements[0], "PRAGMA busy_timeout = 7000",
            f"a statement ran before busy_timeout: {recorder.statements[:2]}",
        )

    def test_initialisation_retries_a_lost_lock_race(self):
        from unittest import mock

        from harness.storage import sqlite_connection

        calls = {"n": 0}
        real = sqlite_connection._initialise_pragmas

        def flaky(connection, page_size):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(connection, page_size)

        with mock.patch.object(sqlite_connection, "_initialise_pragmas", flaky):
            connection = sqlite_connection.connect(self.worktree / "harness.db")
        self.addCleanup(connection.close)
        # One lost race on a first launch used to take the whole process down.
        self.assertEqual(calls["n"], 2)
        self.assertEqual(
            connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
        )

    def test_a_non_busy_error_is_not_retried(self):
        from unittest import mock

        from harness.storage import sqlite_connection

        calls = {"n": 0}

        def broken(connection, page_size):
            calls["n"] += 1
            raise sqlite3.OperationalError("no such column: nonsense")

        with mock.patch.object(sqlite_connection, "_initialise_pragmas", broken):
            with self.assertRaises(sqlite3.OperationalError):
                sqlite_connection.connect(self.worktree / "harness.db")
        self.assertEqual(calls["n"], 1)

    # -- 2. line endings ---------------------------------------------------
    def _text_round_trip(self, backend, text):
        from harness.utils import read_task_file_text

        worktree = Path(self._tmp.name) / f"{backend}-{abs(hash(text))}"
        logger, store = self._logger(backend, worktree=worktree)
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="extraction",
            logical_path="artifacts/a.csv", content=text, media_type="text/csv",
        )
        return (
            local_fs_read(logger, path="artifacts/a.csv", max_bytes=200000),
            read_task_file_text(logger, "artifacts/a.csv"),
        )

    def test_every_line_ending_reads_the_same_from_either_backend(self):
        for label, text in (
            ("lf", "a,b\n1,2\n"),
            ("crlf", "a,b\r\n1,2\r\n"),
            ("cr", "a,b\r1,2\r"),
            ("no trailing newline", "a,b\n1,2"),
        ):
            with self.subTest(ending=label):
                file_read, file_text = self._text_round_trip("file", text)
                db_read, db_text = self._text_round_trip("db", text)
                # A text file opened for reading translates CR and CRLF to LF;
                # keeping them made a Windows CSV a different document.
                self.assertEqual(file_read["content"], db_read["content"])
                self.assertEqual(file_read["linesRead"], db_read["linesRead"])
                self.assertEqual(file_text, db_text)
                self.assertNotIn("\r", db_read["content"])
                self.assertNotIn("\r", db_text or "")

    def test_an_exotic_separator_does_not_split_a_line(self):
        # str.splitlines breaks on a form feed; a file reader does not.
        text = "a\x0cb\nc\n"
        file_read, _ = self._text_round_trip("file", text)
        db_read, _ = self._text_round_trip("db", text)
        self.assertEqual(file_read["linesRead"], db_read["linesRead"])
        self.assertEqual(file_read["content"], db_read["content"])

    # -- 3. stable result order --------------------------------------------
    def test_both_backends_return_the_same_first_result(self):
        found = {}
        for backend in ("file", "db"):
            worktree = Path(self._tmp.name) / f"order-{backend}"
            _logger, store = self._logger(backend, worktree=worktree)
            # Written out of path order on purpose.
            for name in ("nested/b.json", "nested/a.json", "nested/c.json"):
                store.save_resource(
                    task_id="t1", run_id="run-1", resource_type="observation",
                    logical_path=name, content={"n": name},
                )
            found[backend] = [
                row["logical_path"] for row in store.search_resources(
                    task_id="t1", path_glob="**/*", max_results=1
                )
            ]
        # Insertion order on one side, path order on the other: the same call
        # returned a different resource depending on the backend.
        self.assertEqual(found["file"], found["db"])
        self.assertEqual(found["db"], ["nested/a.json"])

    def test_paging_past_the_scan_batch_stays_in_path_order(self):
        _logger, store = self._logger("db")
        names = [f"observations/{index:03d}.json" for index in range(25)]
        for name in reversed(names):
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path=name, content={"n": name},
            )
        found = [
            row["logical_path"] for row in store.search_resources(
                task_id="t1", path_glob="**/*", max_results=100, scan_batch=4
            )
        ]
        # The composite cursor has to advance correctly across batches, or a
        # path-ordered scan silently drops or repeats rows.
        self.assertEqual(found, names)

    # -- 4. CRLF reaches every path, not just the file view ----------------
    def test_crlf_is_consistent_across_every_read_surface(self):
        text = "a,b\r\n1,2\r\n"
        records, searches = {}, {}
        for backend in ("file", "db"):
            worktree = Path(self._tmp.name) / f"crlf-{backend}"
            _logger, store = self._logger(backend, worktree=worktree)
            saved = store.save_resource(
                task_id="t1", run_id="run-1", resource_type="extraction",
                logical_path="artifacts/a.csv", content=text, media_type="text/csv",
            )
            records[backend] = store.read_resource(
                current_task_id="t1", resource_uri=saved["saved_path"]
            )
            searches[backend] = [
                row["logical_path"] for row in store.search_resources(
                    task_id="t1", path_glob="**/*", pattern=r"^1,2$"
                )
            ]
        # Normalising only the virtual file view left the storage API and the
        # search API returning a different document from the same content.
        self.assertEqual(
            records["file"]["content_text"], records["db"]["content_text"]
        )
        self.assertNotIn("\r", records["db"]["content_text"])
        self.assertEqual(searches["file"], searches["db"])
        self.assertEqual(searches["db"], ["artifacts/a.csv"])

    def test_a_windows_csv_does_not_fail_dual_verification(self):
        worktree = Path(self._tmp.name) / "crlf-dual"
        file_store = FileStore(worktree_dir=str(worktree))
        sqlite_store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
        self.addCleanup(sqlite_store.close)
        store = DualStore(file_store, sqlite_store)
        store.create_task(task_id="t1", harness_version="v")
        run_id = store.start_run(task_id="t1", harness_version="v")["run_id"]
        store.save_resource(
            task_id="t1", run_id=run_id, resource_type="extraction",
            logical_path="artifacts/a.csv", content="a,b\r\n1,2\r\n",
            media_type="text/csv",
        )
        report = store.verify(task_id="t1", run_id=run_id)
        # The disk copy comes back through read_text as LF while the column
        # kept CRLF, so an ordinary Windows CSV read as corruption.
        self.assertEqual(report["status"], "ok", report["failedChecks"])

    def test_the_row_keeps_the_original_bytes(self):
        _logger, store = self._logger("db")
        text = "a,b\r\n1,2\r\n"
        store.save_resource(
            task_id="t1", run_id="run-1", resource_type="extraction",
            logical_path="artifacts/a.csv", content=text, media_type="text/csv",
        )
        row = store.connection.execute(
            "SELECT content_text, byte_size, stored_byte_size, sha256"
            " FROM task_resources WHERE logical_path = 'artifacts/a.csv'"
        ).fetchone()
        # Stored verbatim, and measured as it sits on disk - the same number
        # FileStore's st_size reports, so the two backends never disagree
        # about metadata. Newline translation is a read-time concern only.
        self.assertEqual(row["content_text"], text)
        self.assertEqual(row["byte_size"], len(text.encode("utf-8")))
        self.assertIsNone(row["stored_byte_size"])
        import hashlib
        self.assertEqual(
            row["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest()
        )

    def test_the_glob_matcher_is_shared_by_every_search_surface(self):
        from harness.storage.base import glob_matches
        from harness.storage.virtual_fs import VirtualTaskFs

        paths = ("a.csv", "nested/b.csv")
        found = {}
        for backend in ("file", "db"):
            worktree = Path(self._tmp.name) / f"glob-{backend}"
            _logger, store = self._logger(backend, worktree=worktree)
            for path in paths:
                store.save_resource(
                    task_id="t1", run_id="run-1", resource_type="extraction",
                    logical_path=path, content="x", media_type="text/plain",
                )
            found[backend] = {
                pattern: sorted(
                    row["logical_path"] for row in store.search_resources(
                        task_id="t1", path_glob=pattern, max_results=100
                    )
                )
                for pattern in ("**/*", "*", "nested/*", "*.csv")
            }
            if backend == "db":
                view = VirtualTaskFs(store, "t1")
                for pattern in ("**/*", "nested/*"):
                    self.assertEqual(
                        sorted(
                            path for path, _size, _approx in view.match_files(pattern)
                        ),
                        sorted(p for p in paths if glob_matches(pattern, p)),
                    )
        # SQLite's GLOB requires a literal "/" for "**/", so the storage API
        # dropped files at the task root while the model-facing view kept them.
        self.assertEqual(found["file"], found["db"])
        self.assertEqual(found["db"]["**/*"], ["a.csv", "nested/b.csv"])
        self.assertEqual(found["db"]["*.csv"], ["a.csv"])


class BackendTransparencyTest(unittest.TestCase):
    """The switch between file, dual and db must be invisible.

    Six review rounds each found the same shape of defect: two code paths
    answering the same question differently. These assert the property
    directly - identical input, identical answer, whichever backend holds it -
    rather than pinning one more individual symptom.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _fresh(self, backend, label):
        worktree = Path(self._tmp.name) / f"{label}-{backend}"
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        store = create_storage(backend=backend, worktree_dir=str(worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version="v")
        store.start_run(task_id="t1", harness_version="v", run_id="run-1")
        return logger, store

    def test_metadata_is_identical_for_crlf_text(self):
        text = "a,b\r\n1,2\r\n"
        seen = {}
        for backend in ("file", "db", "dual"):
            logger, store = self._fresh(backend, "meta")
            saved = store.save_resource(
                task_id="t1", run_id="run-1", resource_type="extraction",
                logical_path="a.csv", content=text, media_type="text/csv",
            )
            record = store.read_resource(
                current_task_id="t1", resource_uri=saved["saved_path"]
            )
            read = local_fs_read(logger, path="a.csv", max_bytes=200000)
            seen[backend] = (
                saved.get("byte_size"), record.get("byte_size"), read.get("byteSize")
            )
        # 10 physical bytes, 8 after newline translation. Sizing the row
        # logically made this (10, 10, 10) / (8, 8, 8) / (10, 10, 10).
        self.assertEqual(len(set(seen.values())), 1, seen)
        self.assertEqual(seen["db"], (10, 10, 10))

    def test_the_same_glob_returns_the_same_files(self):
        from harness.local_fs import local_fs_search

        seen = {}
        for backend in ("file", "db", "dual"):
            logger, store = self._fresh(backend, "glob")
            for path in ("a.csv", "nested/b.csv", "nested/deep/c.csv"):
                store.save_resource(
                    task_id="t1", run_id="run-1", resource_type="extraction",
                    logical_path=path, content="x", media_type="text/plain",
                )
            seen[backend] = {
                pattern: sorted(
                    hit["relativePath"]
                    for hit in local_fs_search(logger, glob_pattern=pattern)["results"]
                )
                for pattern in ("*", "**/*", "nested/*", "**/*.csv")
            }
        # The file branch used Path.glob while everything else used the shared
        # matcher, so "*" returned the root only in file mode and everything
        # in db mode.
        self.assertEqual(seen["file"], seen["db"])
        self.assertEqual(seen["file"], seen["dual"])
        self.assertEqual(seen["db"]["*"], ["a.csv"])
        self.assertEqual(
            seen["db"]["**/*"], ["a.csv", "nested/b.csv", "nested/deep/c.csv"]
        )
        self.assertEqual(seen["db"]["nested/*"], ["nested/b.csv"])

    def test_the_shared_matcher_agrees_with_pathlib(self):
        from harness.storage.base import glob_matches

        root = Path(self._tmp.name) / "pathlib"
        relatives = ("a.csv", "nested/b.csv", "nested/deep/c.csv", "x/foo/a.csv")
        for relative in relatives:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        for pattern in ("*", "**/*", "nested/*", "*.csv", "**/*.csv", "**/foo/*.csv"):
            with self.subTest(pattern=pattern):
                expected = sorted(
                    str(path.relative_to(root))
                    for path in root.glob(pattern) if path.is_file()
                )
                actual = sorted(
                    relative for relative in relatives
                    if glob_matches(pattern, relative)
                )
                # "**/foo/*.csv" used to miss x/foo/a.csv entirely.
                self.assertEqual(expected, actual)

    def test_character_class_globs_match_pathlib_on_every_backend(self):
        from harness.local_fs import local_fs_search
        from harness.storage.base import glob_matches

        names = ("a.csv", "b.csv", "^.csv", "x/y/a.csv", "x/foo/a.csv")
        patterns = (
            "[ab].csv", "[!a].csv", "[^a].csv", "[]", "[abc", "[]].csv",
            "**/[!x]/*.csv", "[a-b].csv", "?.csv",
        )
        # pathlib is the reference: build the same tree on disk and ask it.
        reference_root = Path(self._tmp.name) / "reference"
        for name in names:
            target = reference_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")

        seen = {}
        for backend in ("file", "db", "dual"):
            logger, store = self._fresh(backend, "charclass")
            for name in names:
                store.save_resource(
                    task_id="t1", run_id="run-1", resource_type="extraction",
                    logical_path=name, content="x", media_type="text/plain",
                )
            seen[backend] = {
                pattern: sorted(
                    hit["relativePath"]
                    for hit in local_fs_search(logger, glob_pattern=pattern)["results"]
                )
                for pattern in patterns
            }
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                expected = sorted(
                    str(path.relative_to(reference_root))
                    for path in reference_root.glob(pattern) if path.is_file()
                )
                self.assertEqual(
                    sorted(name for name in names if glob_matches(pattern, name)),
                    expected,
                )
                # SQLite's GLOB spells negation differently, so handing it
                # "[!a]" as a prefilter dropped rows before Python could judge.
                self.assertEqual(seen["file"][pattern], expected)
                self.assertEqual(seen["db"][pattern], expected)
                self.assertEqual(seen["dual"][pattern], expected)

    def test_the_sql_prefilter_is_always_a_superset(self):
        import fnmatch as _fnmatch

        from harness.storage.base import glob_matches, glob_sql_prefilter

        paths = (
            "a.csv", "b.csv", "^.csv", "nested/b.csv", "nested/deep/c.csv",
            "observations/a.json", "x/foo/a.csv",
        )
        for pattern in (
            "[!a].csv", "[ab].csv", "**/*", "*", "observations/*", "*.csv",
            "nested/deep/c.csv", "?.csv", "**/foo/*.csv",
        ):
            with self.subTest(pattern=pattern):
                prefilter = glob_sql_prefilter(pattern)
                for path in paths:
                    if glob_matches(pattern, path):
                        # SQLite GLOB's "*" spans "/" like fnmatch's does.
                        self.assertTrue(
                            _fnmatch.fnmatchcase(path, prefilter),
                            f"{pattern!r} matches {path!r} but the prefilter"
                            f" {prefilter!r} would have excluded it",
                        )

    def test_a_read_returns_the_same_fields_from_every_backend(self):
        text = "a,b\r\n1,2\r\n"
        shapes = {}
        for backend in ("file", "db", "dual"):
            logger, store = self._fresh(backend, "shape")
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="extraction",
                logical_path="a.csv", content=text, media_type="text/csv",
            )
            full = local_fs_read(logger, path="a.csv", max_bytes=200000)
            clipped = local_fs_read(logger, path="a.csv", max_bytes=4)
            shapes[backend] = (
                tuple(sorted(full)), full["byteSize"], full["bytesRead"],
                clipped["byteSize"], clipped["bytesRead"], clipped["truncated"],
            )
        # bytesRead existed only on the database branch, so a caller could tell
        # which backend answered - and could not get the number at all in file
        # mode.
        self.assertEqual(len(set(shapes.values())), 1, shapes)
        self.assertIn("bytesRead", shapes["file"][0])
        self.assertEqual(shapes["file"][1:], (10, 8, 10, 4, True))

    def test_degenerate_character_ranges_never_raise(self):
        from harness.local_fs import local_fs_search
        from harness.storage.base import glob_matches

        names = ("a.txt", "b.txt", "z.txt", "-.txt", "^.txt", "ab/c.txt")
        # Reversed, overlapping and self-referential ranges are all valid glob
        # input; CPython normalises them, a hand-rolled translator turned them
        # into invalid regex and raised at the caller.
        patterns = (
            "[z-a].txt", "[!z-a].txt", "[a--b].txt", "[b-a-c].txt",
            "[a-b].txt", "[0-9].txt", "[!x][!x]/c.txt",
        )
        reference_root = Path(self._tmp.name) / "ranges"
        for name in names:
            target = reference_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")

        logger, store = self._fresh("db", "ranges")
        for name in names:
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="extraction",
                logical_path=name, content="x", media_type="text/plain",
            )
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                expected = sorted(
                    str(path.relative_to(reference_root))
                    for path in reference_root.glob(pattern) if path.is_file()
                )
                self.assertEqual(
                    sorted(name for name in names if glob_matches(pattern, name)),
                    expected,
                )
                self.assertEqual(
                    sorted(
                        hit["relativePath"]
                        for hit in local_fs_search(logger, glob_pattern=pattern)["results"]
                    ),
                    expected,
                )

    def test_a_search_filter_never_raises_at_the_caller(self):
        from harness.storage.base import glob_matches

        # Whatever a model or a skill puts in a glob, a read-only search has to
        # answer rather than propagate a regex compilation error.
        for pattern in ("[z-a]", "[a--b]", "[[", "[!", "***", "[\\", "a[b"):
            with self.subTest(pattern=pattern):
                self.assertIsInstance(glob_matches(pattern, "a.txt"), bool)
