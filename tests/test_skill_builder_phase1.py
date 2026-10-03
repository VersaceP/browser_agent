"""End-to-end local contracts for the first Skill Builder phase."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

from harness.messages.models import AssistantMessage, ToolCallContent
from harness.skill_builder.catalog import SkillCatalog
from harness.skill_builder.commands import execute as execute_skill_command
from harness.skill_builder.context import SourceTaskContext
from harness.skill_builder.session import BuilderSession
from harness.skill_builder.workflow_scope import workflow_action_scope_error
from harness.storage.sqlite_store import SqliteStore
from harness.tools.registry import ToolContext
from harness.tools.browser_tools.dispatch import _browser_execute_published_skill_workflow
from harness.workflow.workflow_wire import to_platform_execute_params
from llm.contracts import LLMResult, TokenUsage


class ScriptedProvider:
    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            content = [ToolCallContent(
                tool_call_id="write-1", name="write_work_file",
                arguments={"path": "SKILL.md", "content":
                           "---\nname: demo\nversion: '1'\ndescription: demo\n---\nUse the browser facts.\n",
                           "expected_hash": None},
            )]
            stop = "tool_use"
        else:
            content = [ToolCallContent(
                tool_call_id="finish-1", name="finish_skill_create",
                arguments={"summary": "Work file is ready; no live browser trial ran."},
            )]
            stop = "tool_use"
        return LLMResult(
            message=AssistantMessage(content=content, stop_reason=stop,
                                     provider="test", api="scripted", model="test"),
            usage=TokenUsage(input_tokens=100, cache_read_tokens=20,
                             cache_write_tokens=10, output_tokens=15),
        )


class SkillBuilderPhaseOneTests(unittest.TestCase):
    def test_cli_skill_create_then_user_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            worktree = base / "worktree"
            worktree.mkdir()
            skills = base / "skills"
            source_id = "b" * 32
            store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree))
            store.create_task(task_id=source_id, harness_version="test")
            store.close()
            runtime = SimpleNamespace(
                harness=SimpleNamespace(worktree_dir=str(worktree),
                                        storage_sqlite_path="harness.db"),
                browser=SimpleNamespace(), worker=SimpleNamespace(model_id="test"))
            real_catalog = SkillCatalog
            output = io.StringIO()
            with patch("harness.skill_builder.commands.load_runtime_config",
                       return_value=runtime), patch(
                       "harness.skill_builder.commands._catalog",
                       side_effect=lambda _runtime: real_catalog(skills, worktree / "harness.db")), patch(
                       "harness.skill_builder.session.SkillCatalog",
                       side_effect=lambda _root, db: real_catalog(skills, db)), patch(
                       "harness.skill_builder.session.LLMFactory.create_provider",
                       return_value=ScriptedProvider()), contextlib.redirect_stdout(output):
                self.assertEqual(execute_skill_command(
                    f"/skill-create @{source_id} Create a reusable Skill",
                    run_blocking=asyncio.run), 0)
                # The command emits one JSON object after any model text.
                text = output.getvalue()
                created = json.loads(text[text.index("{\n"):])
                builder_id = created["builderTaskId"]
                self.assertEqual(created["skill"]["metadata"]["name"], "demo")
                self.assertEqual(len(created["skill"]["hash"]), 64)
                output.seek(0)
                output.truncate(0)
                self.assertEqual(execute_skill_command(
                    f"/skill-publish @{builder_id}", run_blocking=asyncio.run), 0)
            self.assertTrue((skills / "demo" / "SKILL.md").is_file())

    def test_browser_execution_records_the_pinned_skill_version(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            worktree = base / "worktree"
            worktree.mkdir()
            source = base / "draft"
            source.mkdir()
            (source / "SKILL.md").write_text("---\nname: demo\nversion: 1\n---\nDemo\n")
            (source / "workflow.json").write_text(json.dumps({
                "schemaVersion": 1, "name": "demo-workflow", "steps": [
                    {"type": "action", "action": "Page.getState", "purpose": "Observe"},
                ],
            }))
            from harness.skill_builder.catalog import inspect_skill
            database = worktree / "harness.db"
            real_catalog = SkillCatalog
            catalog = real_catalog(base / "skills", database)
            published = catalog.publish("demo", source,
                                        expected_hash=inspect_skill(source)["hash"],
                                        expected_current_hash=None)
            catalog.close()
            events = []
            runtime = SimpleNamespace(harness=SimpleNamespace(
                workflow_execution_enabled=True,
                forced_skill_id="demo", forced_skill_hash=published["hash"],
                worktree_dir=str(worktree), storage_sqlite_path="harness.db"))
            logger = SimpleNamespace(task_id="task-1", run_id="run-1",
                                     write=lambda kind, payload: events.append((kind, payload)))
            agent = SimpleNamespace(runtime=runtime, logger=logger)
            ctx = ToolContext(agent=agent, tool_call={"name": "execute_published_skill_workflow"},
                              tool_input={"path": "workflow.json", "pageId": "page-1",
                                          "fleetId": "fleet-1", "variables": {}}, step=1)
            receipt = {"response": {"data": {"status": "completed", "workflowId": "wf-1"}},
                       "tool_was_executed": True}
            with patch("harness.skill_builder.catalog.SkillCatalog",
                       side_effect=lambda _root, db: real_catalog(base / "skills", db)), patch(
                       "harness.tools.browser_tools._execute_browser_capability_tool",
                       new=AsyncMock(return_value=(receipt, False))) as execute:
                result = asyncio.run(_browser_execute_published_skill_workflow(ctx))
            self.assertEqual(result["skillInvocation"]["hash"], published["hash"])
            self.assertTrue(execute.await_args.kwargs["published_skill_workflow"])
            self.assertTrue(any(kind == "skill.invocation.result" for kind, _ in events))
            catalog = real_catalog(base / "skills", database)
            try:
                row = catalog.connection.execute("SELECT skill_hash, status FROM skill_invocations").fetchone()
                self.assertEqual((row["skill_hash"], row["status"]),
                                 (published["hash"], "completed"))
            finally:
                catalog.close()

    def test_published_definition_keeps_platform_shape_without_legacy_step_limit(self):
        workflow = {"schemaVersion": 1, "name": "long-example",
                    "initialVariables": {"seed": 1},
                    "steps": [{"type": "action", "action": "Page.getState",
                               "purpose": "Observe"} for _ in range(120)]}
        self.assertIsNone(workflow_action_scope_error(
            workflow, capability_methods={"Page.getState"},
            page_id="page-1", fleet_id="fleet-1"))
        wire = to_platform_execute_params({"_platformWorkflow": workflow,
                                           "pageId": "page-1", "fleetId": "fleet-1"})
        self.assertEqual(wire["workflow"], workflow)
        self.assertEqual(wire["binding"], {"pageId": "page-1", "fleetId": "fleet-1"})
        self.assertIsNot(wire["workflow"], workflow)

    def test_nested_action_needing_independent_permission_is_rejected(self):
        workflow = {"steps": [{"type": "loop", "body": [
            {"type": "action", "action": "Network.readApi"}]}]}
        reason = workflow_action_scope_error(
            workflow, capability_methods={"Network.readApi"})
        self.assertIn("independently authorized", reason)

    def test_builder_draft_publish_pin_and_historical_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            worktree = base / "worktree"
            worktree.mkdir()
            skills = base / "skills"
            database = worktree / "harness.db"
            source_id = "a" * 32
            store = SqliteStore(database, worktree_dir=str(worktree))
            store.create_task(task_id=source_id, harness_version="test")
            (worktree / source_id).mkdir()
            (worktree / source_id / "task_manifest.json").write_text(
                json.dumps({"original_user_task": "Collect a page"}), encoding="utf-8")
            store.close()
            runtime = SimpleNamespace(
                harness=SimpleNamespace(worktree_dir=str(worktree),
                                        storage_sqlite_path="harness.db"),
                browser=SimpleNamespace(),
                worker=SimpleNamespace(model_id="test"),
            )
            provider = ScriptedProvider()
            real_catalog = SkillCatalog
            with patch("harness.skill_builder.session.SkillCatalog",
                       side_effect=lambda _root, db: real_catalog(skills, db)), patch(
                       "harness.skill_builder.session.LLMFactory.create_provider",
                       return_value=provider):
                builder = BuilderSession(runtime, source_task_id=source_id)
                draft = asyncio.run(builder.run("Create a reusable Skill"))
            self.assertEqual(draft["skill"]["metadata"]["name"], "demo")
            self.assertEqual(len(provider.requests), 2)
            self.assertIn("write_work_file", [tool.name for tool in provider.requests[0].tools])

            # /skill-edit resumes the persisted Tau conversation and keeps the
            # same work files. The model sees prior tool calls and results.
            resumed_provider = ScriptedProvider()
            resumed_provider.requests.append(None)
            with patch("harness.skill_builder.session.SkillCatalog",
                       side_effect=lambda _root, db: real_catalog(skills, db)), patch(
                       "harness.skill_builder.session.LLMFactory.create_provider",
                       return_value=resumed_provider):
                resumed = BuilderSession(runtime, source_task_id=source_id,
                                         builder_task_id=draft["builderTaskId"])
                edited = asyncio.run(resumed.run("Keep the current files"))
            self.assertEqual(edited["builderTaskId"], draft["builderTaskId"])
            self.assertEqual(edited["skill"]["hash"], draft["skill"]["hash"])
            self.assertGreater(len(resumed_provider.requests[1].messages), 1)

            catalog = SkillCatalog(skills, database)
            try:
                published = catalog.publish(
                    "demo", Path(draft["workDir"]),
                    expected_hash=draft["skill"]["hash"],
                    expected_current_hash=None, source_task_id=draft["builderTaskId"],
                )
                pinned = published["hash"]
                self.assertEqual(catalog.get("demo")["current_hash"], pinned)
                invocation = catalog.begin_invocation(
                    task_id=source_id, run_id="run-1", skill_id="demo",
                    content_hash=pinned,
                )
                catalog.finish_invocation(invocation["invocationId"], status="failed")
                # Editing the current published Skill produces a new version;
                # the failed source still names its actual historical bytes.
                current = skills / "demo" / "SKILL.md"
                current.write_text(current.read_text() + "\nNew current version.\n")
                catalog.sync_existing()
                self.assertNotEqual(catalog.get("demo")["current_hash"], pinned)
                self.assertIsNotNone(catalog.version_path("demo", pinned))
                reader = SqliteStore(database, worktree_dir=str(worktree))
                try:
                    source = SourceTaskContext(source_id, worktree, reader)
                    self.assertEqual(source.invocations("demo")[0]["skill_hash"], pinned)
                finally:
                    reader.close()
            finally:
                catalog.close()

    def test_publish_rejects_changed_work_file_and_target_version(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "draft"
            source.mkdir()
            (source / "SKILL.md").write_text("---\nname: demo\n---\nOne\n")
            from harness.skill_builder.catalog import inspect_skill
            catalog = SkillCatalog(base / "skills", base / "db.sqlite")
            try:
                first = inspect_skill(source)["hash"]
                (source / "SKILL.md").write_text("---\nname: demo\n---\nTwo\n")
                with self.assertRaisesRegex(ValueError, "工作文件"):
                    catalog.publish("demo", source, expected_hash=first,
                                    expected_current_hash=None)
                second = inspect_skill(source)["hash"]
                catalog.publish("demo", source, expected_hash=second,
                                expected_current_hash=None)
                with self.assertRaisesRegex(ValueError, "正式 Skill"):
                    catalog.publish("demo", source, expected_hash=second,
                                    expected_current_hash=None)
            finally:
                catalog.close()

    def test_interrupted_delete_restores_current_and_explicit_delete_keeps_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "draft"
            source.mkdir()
            (source / "SKILL.md").write_text("---\nname: demo\nversion: 1\n---\nOne\n")
            from harness.skill_builder.catalog import inspect_skill
            catalog = SkillCatalog(base / "skills", base / "db.sqlite")
            published = catalog.publish("demo", source,
                                        expected_hash=inspect_skill(source)["hash"],
                                        expected_current_hash=None)
            catalog.close()
            nonce = "a" * 32
            deleted = base / "skills" / ".deleted"
            deleted.mkdir()
            os.replace(base / "skills" / "demo", deleted / f"demo-{nonce}")
            (deleted / f"{nonce}.json").write_text(json.dumps({
                "skill_id": "demo", "hash": published["hash"]}))
            catalog = SkillCatalog(base / "skills", base / "db.sqlite")
            try:
                self.assertTrue((base / "skills" / "demo").is_dir())
                self.assertFalse((deleted / f"{nonce}.json").exists())
                catalog.delete("demo", expected_hash=published["hash"])
                self.assertTrue(catalog.get("demo")["deleted"])
                self.assertIsNotNone(catalog.version_path("demo", published["hash"]))
            finally:
                catalog.close()


if __name__ == "__main__":
    unittest.main()
