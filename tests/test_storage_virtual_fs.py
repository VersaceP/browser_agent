"""Database rows served through the model-facing file tools.

The contract these tools expose to the LLM is paths and globs. If a model can
tell whether run.jsonl came from a file or from run_events, prompts and skills
would have to change, so the strongest assertion here is that both backends
produce the same bytes.
"""

import json
import tempfile
import unittest
from pathlib import Path

from harness.local_fs import local_fs_read, local_fs_search
from harness.storage.file_store import FileStore
from harness.storage.sqlite_store import EVENT_PAYLOAD_OFFLOAD_THRESHOLD, SqliteStore
from harness.storage.virtual_fs import VirtualTaskFs, _glob_matches, virtual_fs_for
from harness.utils import RunLogger


def _seed(logger, store):
    store.create_task(task_id=logger.task_id, harness_version="v")
    store.start_run(task_id=logger.task_id, harness_version="v", run_id=logger.run_id)
    logger.attach_storage(store)
    logger.write("task_state.initialized", {"phaseCount": 2})
    logger.write("worker.spawned", {"workerId": "browser-001", "phaseId": "p1"})
    logger.write("worker.result", {"workerId": "browser-001", "status": "done"})
    store.append_worker_trace(
        task_id=logger.task_id, run_id=logger.run_id, worker_id="browser-001",
        entries=[
            {"type": "browser_call", "method": "Page.open", "step": 1},
            {"type": "browser_call", "method": "Input.click", "step": 2},
        ],
    )
    store.append_strategy_attempt(
        task_id=logger.task_id, run_id=logger.run_id,
        payload={"phaseId": "p1", "workerId": "browser-001",
                 "strategy_ids": ["s-scroll"], "status": "done", "rowCount": 7},
    )
    store.save_resource(
        task_id=logger.task_id, run_id=logger.run_id, resource_type="observation",
        logical_path="observations/axtree.json", content={"nodes": ["a", "b"]},
    )


class VirtualReadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name) / "worktree"
        self.addCleanup(self._tmp.cleanup)
        self.logger = RunLogger(str(self.worktree), task_id="t1", run_id="run-1")
        self.store = SqliteStore(
            self.worktree / "harness.db", worktree_dir=str(self.worktree)
        )
        self.addCleanup(self.store.close)
        _seed(self.logger, self.store)

    def test_run_jsonl_is_readable_without_a_file(self):
        self.assertFalse((self.worktree / "t1" / "run.jsonl").exists())
        result = local_fs_read(self.logger, path="run.jsonl")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["storage"], "sqlite")
        self.assertEqual(result["linesRead"], 3)
        types = [json.loads(line)["type"] for line in result["content"].splitlines()]
        self.assertEqual(
            types, ["task_state.initialized", "worker.spawned", "worker.result"]
        )

    def test_rendered_events_carry_the_full_envelope(self):
        line = local_fs_read(self.logger, path="run.jsonl")["content"].splitlines()[0]
        event = json.loads(line)
        self.assertEqual(
            sorted(event), sorted(["ts", "taskId", "type", "payload", "runId"])
        )
        self.assertEqual(event["taskId"], "t1")
        self.assertEqual(event["runId"], "run-1")

    def test_trace_file_is_readable(self):
        result = local_fs_read(self.logger, path="traces/browser-001.jsonl")
        self.assertEqual(result["linesRead"], 2)
        methods = [json.loads(l)["method"] for l in result["content"].splitlines()]
        self.assertEqual(methods, ["Page.open", "Input.click"])

    def test_strategy_attempts_are_readable(self):
        result = local_fs_read(self.logger, path="strategy_attempts.jsonl")
        record = json.loads(result["content"])
        self.assertEqual(record["strategy_ids"], ["s-scroll"])
        self.assertEqual(record["rowCount"], 7)

    def test_offloaded_resource_is_readable_by_logical_path(self):
        result = local_fs_read(self.logger, path="observations/axtree.json")
        self.assertEqual(json.loads(result["content"])["nodes"], ["a", "b"])

    def test_a_genuinely_missing_path_still_fails(self):
        result = local_fs_read(self.logger, path="nope/missing.json")
        self.assertEqual(result["status"], "failed")
        self.assertIn("not a file", result["error"])

    def test_path_escape_is_still_refused(self):
        result = local_fs_read(self.logger, path="../../etc/passwd")
        self.assertEqual(result["status"], "failed")
        self.assertIn("escapes", result["error"])

    def test_paging_matches_the_file_contract(self):
        first = local_fs_read(self.logger, path="run.jsonl", line_limit=2)
        self.assertEqual(first["linesRead"], 2)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["nextLineOffset"], 2)
        second = local_fs_read(
            self.logger, path="run.jsonl",
            line_offset=first["nextLineOffset"], line_limit=2,
        )
        self.assertEqual(second["linesRead"], 1)
        self.assertFalse(second["truncated"])

    def test_byte_budget_truncates_mid_read(self):
        result = local_fs_read(self.logger, path="run.jsonl", max_bytes=40)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"].encode("utf-8")), 40)

    def test_large_payload_is_inlined_again_on_read(self):
        payload = {"blob": "x" * (EVENT_PAYLOAD_OFFLOAD_THRESHOLD + 500)}
        self.logger.write("huge", payload)
        lines = local_fs_read(
            self.logger, path="run.jsonl", line_offset=3, max_bytes=200000
        )["content"].splitlines()
        # The payload moved to task_resources, but a reader must not have to
        # know that: the line reads back whole.
        self.assertEqual(json.loads(lines[0])["payload"], payload)


class VirtualSearchTest(VirtualReadTest):
    def test_search_lists_database_backed_files(self):
        result = local_fs_search(self.logger, glob_pattern="**/*")
        paths = {hit["relativePath"] for hit in result["results"]}
        self.assertIn("run.jsonl", paths)
        self.assertIn("traces/browser-001.jsonl", paths)
        self.assertIn("observations/axtree.json", paths)
        self.assertTrue(all(hit["storage"] == "sqlite" for hit in result["results"]))

    def test_glob_narrows_to_traces(self):
        result = local_fs_search(self.logger, glob_pattern="traces/*.jsonl")
        self.assertEqual(
            [hit["relativePath"] for hit in result["results"]],
            ["traces/browser-001.jsonl"],
        )

    def test_event_type_filter_selects_lines(self):
        result = local_fs_search(
            self.logger, glob_pattern="run.jsonl", event_type="worker.spawned"
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            json.loads(result["results"][0]["snippet"])["payload"]["workerId"],
            "browser-001",
        )

    def test_regex_alternation_matches_both_branches(self):
        result = local_fs_search(
            self.logger, glob_pattern="run.jsonl",
            pattern=r"task_state\.initialized|worker\.result",
        )
        self.assertEqual(result["count"], 2)

    def test_max_results_is_respected(self):
        result = local_fs_search(self.logger, glob_pattern="run.jsonl", max_results=2)
        self.assertLessEqual(result["count"], 2)

    def test_string_null_event_type_is_ignored(self):
        # Strict tool schemas make models send the literal string.
        result = local_fs_search(
            self.logger, glob_pattern="run.jsonl", event_type="null"
        )
        self.assertGreater(result["count"], 0)


class BackendParityTest(unittest.TestCase):
    """The same writes must read back identically through either backend."""

    def _read_all(self, logger, path):
        return local_fs_read(logger, path=path, line_limit=5000, max_bytes=200000)

    def test_run_jsonl_bytes_match_between_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_logger = RunLogger(str(root / "fw"), task_id="t1", run_id="run-1")
            file_store = FileStore(worktree_dir=str(root / "fw"))
            _seed(file_logger, file_store)

            db_logger = RunLogger(str(root / "dw"), task_id="t1", run_id="run-1")
            db_store = SqliteStore(root / "dw" / "harness.db", worktree_dir=str(root / "dw"))
            self.addCleanup(db_store.close)
            _seed(db_logger, db_store)

            from_file = self._read_all(file_logger, "run.jsonl")["content"]
            from_db = self._read_all(db_logger, "run.jsonl")["content"]

            def strip_timestamps(text):
                return [
                    {k: v for k, v in json.loads(line).items() if k != "ts"}
                    for line in text.splitlines()
                ]

            self.assertEqual(strip_timestamps(from_file), strip_timestamps(from_db))

    def test_trace_bytes_match_between_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_logger = RunLogger(str(root / "fw"), task_id="t1", run_id="run-1")
            _seed(file_logger, FileStore(worktree_dir=str(root / "fw")))
            db_logger = RunLogger(str(root / "dw"), task_id="t1", run_id="run-1")
            db_store = SqliteStore(root / "dw" / "harness.db", worktree_dir=str(root / "dw"))
            self.addCleanup(db_store.close)
            _seed(db_logger, db_store)

            path = "traces/browser-001.jsonl"
            self.assertEqual(
                self._read_all(file_logger, path)["content"],
                self._read_all(db_logger, path)["content"],
            )


class DualModePrecedenceTest(unittest.TestCase):
    def test_files_win_and_are_not_duplicated(self):
        from harness.storage.dual_store import DualStore

        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
            sqlite_store = SqliteStore(
                worktree / "harness.db", worktree_dir=str(worktree)
            )
            self.addCleanup(sqlite_store.close)
            dual = DualStore(FileStore(worktree_dir=str(worktree)), sqlite_store)
            _seed(logger, dual)

            result = local_fs_search(logger, glob_pattern="run.jsonl")
            paths = [hit["relativePath"] for hit in result["results"]]
            self.assertEqual(paths.count("run.jsonl"), 1)
            # The on-disk copy is authoritative while files still exist, and
            # says so: the field names the branch that answered rather than
            # being absent, so a caller never has to infer it.
            self.assertEqual(result["results"][0]["storage"], "file")

            read = local_fs_read(logger, path="run.jsonl")
            self.assertEqual(read["storage"], "file")


class LegacyWorktreeTest(unittest.TestCase):
    def test_a_task_with_files_but_no_rows_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "worktree"
            logger = RunLogger(str(worktree), task_id="legacy", run_id="")
            logger.write("demo", {"a": 1})
            # No database anywhere: the file path must behave exactly as before.
            result = local_fs_read(logger, path="run.jsonl")
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["storage"], "file")
            self.assertEqual(json.loads(result["content"])["type"], "demo")


class GlobSemanticsTest(unittest.TestCase):
    def test_double_star_spans_directories(self):
        self.assertTrue(_glob_matches("**/*.jsonl", "traces/browser-001.jsonl"))
        self.assertTrue(_glob_matches("**/*", "observations/a.json"))

    def test_bare_pattern_stays_at_the_task_root(self):
        self.assertTrue(_glob_matches("*.jsonl", "run.jsonl"))
        self.assertFalse(_glob_matches("*.jsonl", "traces/browser-001.jsonl"))

    def test_directory_pattern_matches_that_directory(self):
        self.assertTrue(_glob_matches("traces/*.jsonl", "traces/browser-001.jsonl"))
        self.assertFalse(_glob_matches("traces/*.jsonl", "run.jsonl"))


class VirtualFsAvailabilityTest(unittest.TestCase):
    def test_file_backend_has_no_virtual_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = RunLogger(str(Path(tmp) / "worktree"), task_id="t1")
            self.assertIsNone(virtual_fs_for(logger))

    def test_view_is_unavailable_without_a_task_id(self):
        self.assertFalse(VirtualTaskFs(object(), "").available)


if __name__ == "__main__":
    unittest.main()
