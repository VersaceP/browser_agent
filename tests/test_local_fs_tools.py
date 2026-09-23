"""
test_local_fs_tools.py - Worktree read/search/offload helper tests.
"""

import json
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from agent_harness import (  # noqa: E402
    BrowserAgent,
    local_fs_read,
    local_fs_search,
    offload_large_response_fields,
    offload_large_tool_result,
)
from harness.utils import RunLogger  # noqa: E402
from harness.tools.file_tools import local_fs_batch  # noqa: E402
from harness.tools.path_authorization import authorize_path
from harness.results.worker_result import build_worker_result_levels  # noqa: E402
from harness.storage.sqlite_store import SqliteStore  # noqa: E402


class TestLocalFsTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logger = RunLogger(self.tmp.name, task_id="task")

    def _write_jsonl(self, relative_path, rows):
        path = self.logger.task_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
            encoding="utf-8",
        )
        return path

    def test_search_filters_jsonl_event_type_and_returns_multiple_lines(self):
        self._write_jsonl(
            "traces/worker.jsonl",
            [
                {"type": "browser_call", "method": "DOM.getSemanticTree", "step": 1},
                {"type": "browser_call", "method": "Page.navigate", "step": 2},
                {"type": "browser_call", "method": "DOM.getSemanticTree", "step": 3},
                {
                    "type": "browser.transport.response",
                    "payload": {"method": "DOM.getSemanticTree", "blob": "x" * 5000},
                },
            ],
        )

        result = local_fs_search(
            self.logger,
            glob_pattern="traces/*.jsonl",
            pattern="DOM.getSemanticTree",
            event_type="browser_call",
            max_results=10,
            max_bytes_per_hit=200,
            max_total_bytes=2000,
        )

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["count"], 2)
        self.assertEqual([hit["line"] for hit in result["results"]], [1, 3])
        self.assertTrue(
            all(len(hit["snippet"].encode("utf-8")) <= 200 for hit in result["results"])
        )

    def test_batch_writes_copies_stats_and_registers_manifest(self):
        agent = SimpleNamespace(
            logger=self.logger,
            worker_id="browser-1",
            worker_contract={"phase_id": "delivery"},
            artifacts=[],
            file_action_evidence=[],
            _register_external_file=lambda *_: None,
        )
        result = local_fs_batch(agent, [
            {"op": "mkdir", "path": "deliverables/item", "source": None, "content": None, "overwrite": False},
            {"op": "write_text", "path": "deliverables/item/info.txt", "source": None, "content": "汉服", "overwrite": False},
            {"op": "write_json", "path": "deliverables/item/info.json", "source": None, "content": {"title": "汉服"}, "overwrite": False},
            {"op": "copy", "path": "deliverables/archive/info.txt", "source": "deliverables/item/info.txt", "content": None, "overwrite": False},
            {"op": "stat", "path": "deliverables/archive/info.txt", "source": None, "content": None, "overwrite": False},
        ])
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(result["files"]), 3)
        self.assertEqual(len(agent.artifacts), 3)
        self.assertEqual(agent.file_action_evidence[0]["method"], "local_fs_batch")
        manifest = json.loads(Path(result["manifestPath"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["protocol"], "browser-file-manifest-v1")
        self.assertEqual(manifest["phaseId"], "delivery")
        self.assertEqual(
            (self.logger.task_dir / "deliverables/item/info.txt").read_text(encoding="utf-8"),
            "汉服",
        )
        self.assertEqual(result["results"][-1]["sha256"], result["results"][3]["sha256"])

    def test_batch_manifest_is_readable_in_db_mode_without_a_manifest_file(self):
        store = SqliteStore(
            Path(self.tmp.name) / "harness.db",
            worktree_dir=self.logger.worktree_dir,
        )
        self.addCleanup(store.close)
        store.create_task(task_id="task", harness_version="test")
        store.start_run(task_id="task", harness_version="test", run_id="run-1")
        self.logger.run_id = "run-1"
        self.logger.attach_storage(store)
        agent = SimpleNamespace(
            logger=self.logger, worker_id="browser-1",
            worker_contract={"phase_id": "delivery"}, artifacts=[],
            file_action_evidence=[], _register_external_file=lambda *_: None,
        )
        result = local_fs_batch(agent, [{
            "op": "write_json", "path": "deliverables/result.json",
            "source": None, "content": {"ok": True}, "overwrite": False,
        }])
        manifest_path = Path(result["manifestPath"])
        self.assertFalse(manifest_path.exists())
        loaded = local_fs_read(
            self.logger, path=str(manifest_path), max_bytes=100000,
            line_limit=1000,
        )
        self.assertEqual(loaded["status"], "done")
        self.assertEqual(json.loads(loaded["content"])["protocol"], "browser-file-manifest-v1")

    def test_batch_rejects_control_files_escape_and_implicit_overwrite(self):
        agent = SimpleNamespace(
            logger=self.logger, worker_id="browser-1", worker_contract={},
            artifacts=[], file_action_evidence=[], _register_external_file=lambda *_: None,
        )
        existing = self.logger.task_dir / "observations" / "existing.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("keep", encoding="utf-8")
        result = local_fs_batch(agent, [
            {"op": "write_text", "path": "task_state.json", "source": None, "content": "bad", "overwrite": True},
            {"op": "write_text", "path": "../escape.txt", "source": None, "content": "bad", "overwrite": True},
            {"op": "write_text", "path": "observations/existing.txt", "source": None, "content": "replace", "overwrite": False},
        ])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failedCount"], 3)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

    def test_batch_can_create_nested_desktop_delivery_without_shell(self):
        agent = SimpleNamespace(
            logger=self.logger, worker_id="browser-1",
            worker_contract={"phase_id": "delivery"}, artifacts=[],
            file_action_evidence=[], _register_external_file=lambda *_: None,
        )
        fake_home = Path(self.tmp.name) / "home"
        desktop = fake_home / "Desktop"
        desktop.mkdir(parents=True)
        destination = desktop / "商品A" / "商品信息" / "标题.txt"
        with patch("harness.runtime.hitl_input.read_terminal_input", new=AsyncMock(return_value="yes")):
            asyncio.run(authorize_path(agent, str(desktop), "write"))
        with patch("harness.tools.file_tools.Path.home", return_value=fake_home):
            result = local_fs_batch(agent, [{
                "op": "write_text", "path": str(destination), "source": None,
                "content": "标题", "overwrite": False,
            }])
        self.assertEqual(result["status"], "done")
        self.assertEqual(destination.read_text(encoding="utf-8"), "标题")

    def test_batch_desktop_base_and_alias_support_directory_stat(self):
        agent = SimpleNamespace(
            logger=self.logger, worker_id="browser-1",
            worker_contract={"phase_id": "delivery"}, artifacts=[],
            file_action_evidence=[], _register_external_file=lambda *_: None,
        )
        fake_home = Path(self.tmp.name) / "home"
        desktop = fake_home / "Desktop"
        desktop.mkdir(parents=True)
        with patch("harness.runtime.hitl_input.read_terminal_input", new=AsyncMock(return_value="yes")):
            asyncio.run(authorize_path(agent, str(desktop), "write"))
            asyncio.run(authorize_path(agent, str(desktop), "read"))
        with patch("harness.tools.file_tools.Path.home", return_value=fake_home):
            result = local_fs_batch(agent, [
                {"op": "mkdir", "path": "商品A/图片", "base": "desktop", "overwrite": False},
                {"op": "write_text", "path": "Desktop/商品A/info.txt", "content": "ok", "overwrite": False},
                {"op": "stat", "path": "商品A", "base": "desktop", "overwrite": False},
            ])
        self.assertEqual(result["status"], "done", result)
        self.assertTrue((desktop / "商品A/图片").is_dir())
        self.assertEqual(result["results"][-1]["kind"], "directory")

    def test_batch_rejects_symlink_destination(self):
        agent = SimpleNamespace(
            logger=self.logger, worker_id="browser-1", worker_contract={},
            artifacts=[], file_action_evidence=[], _register_external_file=lambda *_: None,
        )
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        link = self.logger.task_dir / "deliverables" / "link.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        result = local_fs_batch(agent, [{
            "op": "write_text", "path": str(link), "source": None,
            "content": "replace", "overwrite": True,
        }])
        self.assertEqual(result["status"], "partial")
        self.assertIn("symlink", result["results"][0]["error"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_workflow_file_steps_are_attributed_individually(self):
        agent = SimpleNamespace(
            artifacts=[], file_action_evidence=[],
            _register_external_file=lambda *_: None,
        )
        BrowserAgent._capture_file_action(agent, "Workflow.execute", {}, {
            "data": {
                "workflowId": "wf-1",
                "results": [
                    {
                        "status": "success", "stepPath": "steps[0]",
                        "step": {
                            "type": "action", "action": "Download.start",
                            "params": {"savePath": "/tmp/product-a.jpg"},
                        },
                        "result": {
                            "downloadId": "d1", "state": "completed",
                            "savePath": "/tmp/product-a.jpg",
                        },
                    },
                    {
                        "status": "error", "stepPath": "steps[1]",
                        "step": {
                            "type": "action", "action": "Download.start",
                            "params": {"savePath": "/tmp/product-b.jpg"},
                        },
                        "result": {"savePath": "/tmp/product-b.jpg"},
                    },
                ],
            },
        })
        self.assertEqual(agent.artifacts, ["/tmp/product-a.jpg"])
        self.assertEqual(len(agent.file_action_evidence), 1)
        receipt = agent.file_action_evidence[0]
        self.assertEqual(receipt["method"], "Download.start")
        self.assertEqual(receipt["workflowStepPath"], "steps[0]")
        self.assertEqual(receipt["workflowId"], "wf-1")
        download_receipts = {
            id(value): value
            for value in agent.download_operation_receipts.values()
        }
        self.assertEqual(len(download_receipts), 1)
        download = next(iter(download_receipts.values()))
        self.assertEqual(download["downloadId"], "d1")
        self.assertEqual(download["source"], "Workflow.execute")

    def test_workflow_download_control_does_not_adopt_fleet_inventory(self):
        agent = SimpleNamespace(
            artifacts=["/tmp/own.jpg"], file_action_evidence=[],
            download_operation_receipts={},
            _register_external_file=lambda *_: None,
        )
        BrowserAgent._capture_file_action(agent, "Workflow.execute", {}, {
            "data": {
                "workflowId": "wf-control",
                "results": [{
                    "status": "success", "stepPath": "steps[0]",
                    "step": {
                        "type": "action", "action": "Download.control",
                        "params": {"downloadId": "own", "command": "resume"},
                    },
                    "result": {"downloads": [
                        {"downloadId": "own", "state": "completed", "savePath": "/tmp/own.jpg"},
                        {"downloadId": "other", "state": "completed", "savePath": "/tmp/old.jpg"},
                    ]},
                }],
            },
        })
        self.assertEqual(agent.artifacts, ["/tmp/own.jpg"])
        self.assertNotIn('["downloadId", "other"]', agent.download_operation_receipts)

    def test_offload_lines_as_text_and_tree_as_json(self):
        response = {
            "data": {
                "lines": [f"[{index}] link item {index}" for index in range(20)],
                "tree": {"tag": "body", "children": [{"tag": "a", "id": 1}]},
            }
        }

        result = offload_large_response_fields(
            logger=self.logger,
            method="DOM.getAXTree",
            params={"pageId": "page-1"},
            response=response,
            step=8,
            threshold_bytes=10,
        )

        lines_stub = result["data"]["lines"]
        tree_stub = result["data"]["tree"]
        self.assertEqual(lines_stub["format"], "text_lines")
        self.assertEqual(lines_stub["query_with"], "local_fs_search")
        self.assertEqual(Path(lines_stub["savedPath"]).suffix, ".txt")
        self.assertEqual(tree_stub["format"], "json_tree")
        self.assertEqual(tree_stub["query_with"], "local_fs_read")
        self.assertEqual(Path(tree_stub["savedPath"]).suffix, ".json")

    def test_local_fs_read_rejects_path_escape_and_symlink_escape(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        symlink = self.logger.task_dir / "link-out"
        symlink.symlink_to(outside)

        cases = [
            "../outside.txt",
            str(outside),
            "subdir/../../outside.txt",
            str(symlink),
        ]

        for raw_path in cases:
            with self.subTest(path=raw_path):
                result = local_fs_read(
                    self.logger,
                    path=raw_path,
                    line_offset=0,
                    line_limit=10,
                    max_bytes=1000,
                )
                self.assertEqual(result["status"], "failed")
                self.assertTrue("worktree" in result["error"] or "confirmation_required" in result["error"])

    def test_generic_large_tool_result_offload_preserves_small_fields(self):
        result = {
            "method": "Runtime.evaluate",
            "response": {
                "observation": "ok",
                "suggested_prompt": "continue",
                "data": {"items": ["x" * 1000 for _ in range(100)]},
            },
        }

        stub = offload_large_tool_result(
            logger=self.logger,
            tool_name="Runtime.evaluate",
            result=result,
            step=4,
            prefix="agent-1",
            threshold_bytes=1000,
        )

        self.assertTrue(stub["_offloaded"])
        self.assertEqual(stub["format"], "json_response")
        self.assertEqual(stub["query_with"], "local_fs_read")
        self.assertEqual(stub["method"], "Runtime.evaluate")
        self.assertEqual(stub["response"]["observation"], "ok")
        # 2026-07-07 skillsGuide alignment: a benign success suggested_prompt is
        # KEPT so the model reads next-step advice on success too. Since the
        # 1.1.9 alignment it is kept in full rather than truncated; see
        # offload.compact_model_facing_tool_result.
        self.assertEqual(stub["response"]["suggested_prompt"], "continue")
        saved = Path(stub["savedPath"])
        self.assertEqual(saved.suffix, ".json")
        self.assertTrue(saved.read_text(encoding="utf-8"))

    def test_large_wait_result_keeps_semantic_worker_handoff_inline(self):
        result = {
            "completed": [{
                "resultLevels": {
                    "l1": {
                        "status": "partial",
                        "statusCategory": "done",
                        "validatedStatus": "not_validated",
                        "workerId": "browser-1",
                        "phaseId": "comments",
                    },
                    "l2": {
                        "answer": {"format": "text", "raw": "collection incomplete"},
                        "data": {"totalExtractedRows": 20},
                        "blockers": [{"type": "pager_remaining"}],
                        "nextSteps": ["click the remaining pager"],
                        "evidence": {"tracePath": "/tmp/trace.jsonl"},
                        "traceSummary": {
                            "contentCompletenessPages": [{
                                "pageId": "p1",
                                "shellPresent": True,
                                "observedRegions": ["comment-list"],
                                "missingRegions": ["older-comments"],
                                "materializationAttempts": ["Input.scroll"],
                                "regionRecordCounts": {"comments": 20},
                                "regionCollectionStates": {
                                    "comments": "materialization_stalled"
                                },
                                "decision": "route_recovery_required",
                                "decisionNextInstruction": "site-specific verdict",
                            }],
                        },
                    },
                    "l3": {"artifactValidation": {
                        "status": "failed",
                        "fileArtifacts": ["/tmp/delivery-a"],
                        "priorFileArtifacts": ["/tmp/prior-a"],
                        "failures": [{
                            "type": "file_integrity",
                            "message": "declared delivery file is missing",
                        }],
                    }},
                },
                "large": ["x" * 1000 for _ in range(100)],
            }],
        }
        stub = offload_large_tool_result(
            logger=self.logger,
            tool_name="wait_browser_agents",
            result=result,
            step=8,
            threshold_bytes=1000,
        )
        handoff = stub["workerHandoffs"][0]
        self.assertLessEqual(
            len(json.dumps(handoff, ensure_ascii=False).encode("utf-8")),
            4096,
        )
        self.assertEqual(handoff["rawReceipts"]["status"], "partial")
        self.assertEqual(handoff["rawReceipts"]["deliveryFileCount"], 1)
        self.assertEqual(handoff["rawReceipts"]["priorDeliveryFileCount"], 1)
        self.assertEqual(
            handoff["rawReceipts"]["artifactFailures"][0]["type"],
            "file_integrity",
        )
        self.assertNotIn("statusCategory", json.dumps(handoff))
        self.assertEqual(
            handoff["unresolvedCounterevidence"][0]["type"],
            "pager_remaining",
        )
        self.assertEqual(
            handoff["suggestedNextExperiment"], ["click the remaining pager"]
        )
        observations = handoff["rawReceipts"][
            "contentCompletenessObservations"
        ]
        self.assertEqual(observations[0]["missingRegions"], ["older-comments"])
        self.assertNotIn("decision", json.dumps(observations))
        self.assertEqual(
            handoff["unresolvedCounterevidence"][-1]["source"],
            "tracker_observation_not_verdict",
        )

    def test_worker_result_uses_validator_row_count_instead_of_attempt_sum(self):
        with patch(
            "harness.results.worker_result.summarize_extraction_artifacts",
            side_effect=[
                [{"savedPath": "/tmp/current.json", "rowCount": 3}],
                [{"savedPath": "/tmp/prior.json", "rowCount": 3}],
            ],
        ):
            levels = build_worker_result_levels(
                status="done", status_category="done", validated_status="done",
                worker_id="w", agent_id="a", name="worker", phase_id="p",
                answer="done", artifacts=["/tmp/current.json"],
                extraction_attempt_artifacts=["/tmp/prior.json"],
                artifact_validation={
                    "status": "done", "rowCount": 3,
                    "validExtractionArtifacts": ["/tmp/current.json"],
                },
                trace_path="/tmp/trace.jsonl", trace_summary={},
                progress_snapshot={}, offloaded_files=[], diagnostics={},
                task_dir=None,
            )
        self.assertEqual(levels["l2"]["data"]["totalExtractedRows"], 3)
        self.assertEqual(
            levels["l2"]["evidence"]["validatedExtractionArtifacts"],
            ["/tmp/current.json"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
