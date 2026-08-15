import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from abcp_client import ABCPClientConfig
from agent_harness import LeadAgent, ResumeContext
from harness.plan_validator import plan_hash, write_plan_version
from harness.resume_state import (
    RUN_LOCK_DIR,
    configure_resume_storage,
    load_task_state_strict,
    write_task_manifest,
)
from harness.storage import create_storage
from harness.task_control import (
    load_task_state,
    validate_task_plan,
    write_task_plan,
    write_task_state,
)
from harness.tools.lead_tools import (
    build_lead_agent_tool_specs,
    execute_lead_tool,
)
from harness.utils import RunLogger, load_task_json
from runtime_config import HarnessConfig, ModelConfig, RuntimeConfig


def _plan():
    return {
        "goal": "collect one row",
        "task_type": "web_scrape",
        "phases": [{
            "id": "p1",
            "type": "browser_worker",
            "task_type": "web_scrape",
            "objective": "collect one row",
            "worker_task": "open https://example.com/a and collect one row",
            "stage_hint": "collection",
            "stage_hint_reason": (
                "This phase collects one structured record from the declared "
                "source page before any downstream processing."
            ),
            "depends_on": [],
            "expected_artifact": {"name": "rows", "fields": ["name"]},
            "validators": [{"type": "min_rows", "value": 1}],
            "max_attempts": 3,
        }],
    }


def _make_task(root: str, *, running: bool = False, backend: str = "file") -> Path:
    """Seed a resumable task through whichever backend owns it.

    This used to write the four files directly, which quietly made the whole
    class a file-mode test: once config.json declared `storage_backend: "db"`
    all three cases failed, not because resume was broken but because a db-mode
    task keeps nothing on disk and deliberately refuses to fall back to files.
    Seeding through the storage API instead means each backend is set up the
    way it is actually used, and the CLI is asserted against all three.
    """
    logger = RunLogger(root, task_id="existing-task", run_id="run-1")
    storage = create_storage(backend=backend, worktree_dir=root)
    logger.attach_storage(storage)
    storage.create_task(task_id=logger.task_id, harness_version="test")
    storage.start_run(
        task_id=logger.task_id, harness_version="test", run_id="run-1",
    )
    plan = _plan()
    write_plan_version(
        logger, plan=plan, previous_plan=None, replan_reason="",
        user_task="the original request", validator_review=None,
    )
    write_task_plan(logger, plan)
    phase_state = {
        "status": "running" if running else "pending",
        "attempts": ([{
            "workerId": "browser-1",
            "status": "running",
            "started_at": "before",
        }] if running else []),
        "validated_artifacts": [],
    }
    write_task_state(
        logger,
        {
            "goal": plan["goal"],
            "phases": {"p1": phase_state},
            "artifacts": [],
            "plan_hash": plan_hash(plan),
            "plan_version": 1,
        },
        replace=True,
    )
    write_task_manifest(logger, original_user_task="the original request")
    storage.close()
    return logger.task_dir


class _FakeLead:
    last = None

    def __init__(self, provider, runtime, logger, **kwargs):
        self.provider = provider
        self.runtime = runtime
        self.logger = logger
        self.kwargs = kwargs
        self.task = ""
        type(self).last = self

    async def run(self, task):
        self.task = task
        return "fake resumed answer"


BACKENDS = ("file", "dual", "db")


class ResumeCliBootstrapTest(unittest.TestCase):
    """The CLI bootstrap, asserted against every backend it can be run with.

    Nothing here reads a process file any more. Which files exist is the
    backend's business - db mode keeps none - and a test that hard-codes the
    file layout silently becomes a test of whoever's config.json is on disk.
    """

    def setUp(self):
        self.addCleanup(configure_resume_storage, backend=None, sqlite_path=None)

    def _runtime(self, root, backend):
        config_path = str(Path(main.__file__).with_name("config.json"))
        runtime = main.load_runtime_config(config_path)
        runtime.harness.worktree_dir = root
        runtime.harness.storage_backend = backend
        return config_path, runtime

    def _run(self, argv, runtime):
        args = main.build_arg_parser().parse_args(argv)
        with patch.object(main, "load_runtime_config", return_value=runtime), patch.object(
            main.LLMFactory, "create_provider", return_value=object()
        ), patch.object(main, "LeadAgent", _FakeLead):
            return asyncio.run(main.run_cli(args))

    def test_resume_reuses_directory_and_injects_original_plus_instruction(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as root:
                task_dir = _make_task(root, backend=backend)
                config_path, runtime = self._runtime(root, backend)
                result = self._run([
                    "--config", config_path,
                    "--resume", str(task_dir),
                    "--task", "use the narrower source",
                ], runtime)
                self.assertEqual(result, 0)
                self.assertEqual(
                    _FakeLead.last.logger.task_dir.resolve(), task_dir.resolve()
                )
                self.assertEqual(_FakeLead.last.task, "the original request")
                resume = _FakeLead.last.kwargs["resume"]
                self.assertEqual(resume.instruction, "use the narrower source")
                self.assertEqual(resume.initial_plan, _plan())
                self.assertTrue(resume.initial_plan_recovered)
                self.assertFalse((task_dir / RUN_LOCK_DIR).exists())
                state = load_task_state_strict(task_dir)
                self.assertEqual(len(state["resumes"]), 1)

    def test_noninteractive_interrupted_phase_requires_explicit_replay_flag(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as root:
                task_dir = _make_task(root, running=True, backend=backend)
                config_path, runtime = self._runtime(root, backend)
                fake_stdin = SimpleNamespace(isatty=lambda: False)
                with patch.object(main.sys, "stdin", fake_stdin):
                    result = self._run([
                        "--config", config_path,
                        "--resume", str(task_dir),
                    ], runtime)
                self.assertEqual(result, 2)
                state = load_task_state_strict(task_dir)
                self.assertEqual(state["phases"]["p1"]["status"], "running")
                self.assertFalse((task_dir / RUN_LOCK_DIR).exists())

                _, runtime = self._runtime(root, backend)
                result = self._run([
                    "--config", config_path,
                    "--resume", str(task_dir),
                    "--resume-retry-interrupted",
                ], runtime)
                self.assertEqual(result, 0)
                state = load_task_state_strict(task_dir)
                self.assertEqual(state["phases"]["p1"]["status"], "pending")
                self.assertEqual(
                    state["phases"]["p1"]["attempts"][0]["status"],
                    "interrupted",
                )

    def test_new_cli_run_records_the_manifest_and_releases_the_lock(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as root:
                config_path, runtime = self._runtime(root, backend)
                result = self._run([
                    "--config", config_path,
                    "--task", "a brand new task",
                ], runtime)

                self.assertEqual(result, 0)
                task_dir = _FakeLead.last.logger.task_dir
                self.assertFalse((task_dir / RUN_LOCK_DIR).exists())

                # run_cli closes the backend it opened, so read the run back
                # the way any later inspection would: through a fresh one.
                reader = RunLogger(root, task_id=task_dir.name)
                store = create_storage(backend=backend, worktree_dir=root)
                self.addCleanup(store.close)
                reader.attach_storage(store)

                manifest = load_task_json(reader, "task_manifest.json")
                self.assertEqual(
                    manifest["original_user_task"], "a brand new task"
                )
                # Every event carries the run it belongs to, whichever store
                # holds it. `run_id` is the relational field the storage API
                # returns; the camelCase `runId` this used to assert on is the
                # spelling inside a run.jsonl line, i.e. the file layout again.
                events = store.read_events(task_id=task_dir.name, limit=200)
                self.assertTrue(events)
                self.assertTrue(all(event.get("run_id") for event in events))

    def test_legacy_file_task_is_refused_rather_than_half_resumed(self):
        """A file-mode task directory opened under db mode.

        db mode makes the database authoritative and says so instead of
        reading the files next to it. That refusal is the contract; this
        pins it so a future fallback cannot be added by accident.
        """
        with tempfile.TemporaryDirectory() as root:
            task_dir = _make_task(root, backend="file")
            config_path, runtime = self._runtime(root, "db")
            result = self._run([
                "--config", config_path,
                "--resume", str(task_dir),
            ], runtime)

        self.assertEqual(result, 2)


class _Log:
    def __init__(self):
        self.events = []

    def write(self, name, payload):
        self.events.append((name, payload))


class ResumeInstructionGateTest(unittest.TestCase):
    def test_spawn_is_blocked_until_keep_plan_acknowledgement(self):
        agent = SimpleNamespace(
            resume=SimpleNamespace(run_id="resume-1"),
            _resume_instruction_pending=True,
            _current_step=1,
            recent_tool_signatures=[],
            logger=_Log(),
        )
        blocked, should_stop = asyncio.run(execute_lead_tool(agent, {
            "name": "spawn_browser_agent",
            "input": {"phase_id": "p1"},
        }))
        self.assertFalse(should_stop)
        self.assertEqual(blocked["status"], "resume_instruction_review_required")

        kept, should_stop = asyncio.run(execute_lead_tool(agent, {
            "name": "resume_keep_plan",
            "input": {"reason": "the source and evidence contract are unchanged"},
        }))
        self.assertFalse(should_stop)
        self.assertEqual(kept["decision"], "keep_plan")
        self.assertFalse(agent._resume_instruction_pending)

    def test_final_answer_is_blocked_until_resume_instruction_is_reviewed(self):
        agent = SimpleNamespace(
            resume=SimpleNamespace(run_id="resume-1"),
            _resume_instruction_pending=True,
            _current_step=1,
            recent_tool_signatures=[],
            logger=_Log(),
        )

        blocked, should_stop = asyncio.run(execute_lead_tool(agent, {
            "name": "final_answer",
            "input": {"status": "done", "answer": "Use the prior result."},
        }))

        self.assertFalse(should_stop)
        self.assertEqual(blocked["status"], "resume_instruction_review_required")
        self.assertFalse(blocked["tool_was_executed"])

    def test_keep_plan_persists_decision_in_latest_resume_audit(self):
        with tempfile.TemporaryDirectory() as root:
            logger = RunLogger(root, task_id="task")
            write_task_state(logger, {
                "phases": {},
                "resumes": [
                    {"at": "old", "instruction": "old instruction"},
                    {"at": "current", "instruction": "current instruction"},
                ],
            })
            agent = SimpleNamespace(
                resume=SimpleNamespace(run_id="resume-2"),
                _resume_instruction_pending=True,
                _current_step=1,
                recent_tool_signatures=[],
                logger=logger,
            )

            kept, should_stop = asyncio.run(execute_lead_tool(agent, {
                "name": "resume_keep_plan",
                "input": {"reason": "the evidence contract is unchanged"},
            }))

            self.assertFalse(should_stop)
            self.assertEqual(kept["decision"], "keep_plan")
            resumes = load_task_state(logger)["resumes"]
            self.assertNotIn("instructionDecision", resumes[0])
            self.assertEqual(resumes[-1]["instructionDecision"], {
                "decision": "keep_plan",
                "reason": "the evidence contract is unchanged",
                "runId": "resume-2",
            })

    def test_resume_tool_is_hidden_from_normal_runs(self):
        normal_names = {
            item["name"] for item in build_lead_agent_tool_specs(include_resume=False)
        }
        resumed_names = {
            item["name"] for item in build_lead_agent_tool_specs(include_resume=True)
        }
        self.assertNotIn("resume_keep_plan", normal_names)
        self.assertIn("resume_keep_plan", resumed_names)


class ResumeLeadReplanTest(unittest.TestCase):
    def test_successful_replan_mechanically_retires_changed_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            normalized, errors = validate_task_plan(_plan())
            self.assertEqual(errors, [])
            assert normalized is not None
            logger = RunLogger(root, task_id="task")
            artifact = logger.artifacts_dir / "rows.json"
            artifact.write_text('{"rows": [{"name": "one"}]}', encoding="utf-8")
            write_task_state(logger, {
                "phases": {
                    "p1": {
                        "status": "validated_done",
                        "attempts": [{"status": "validated_done"}],
                        "validated_artifacts": [str(artifact)],
                    },
                },
                "artifacts": [str(artifact)],
                "resumes": [{"at": "resume-start"}],
            })
            runtime = RuntimeConfig(
                agent_id="test-agent",
                model=ModelConfig(provider="fake", model_id="fake"),
                browser=ABCPClientConfig(),
                harness=HarnessConfig(worktree_dir=root),
            )
            resume = ResumeContext(
                original_user_task="the original request",
                current_plan=normalized,
                initial_plan=normalized,
                instruction="require at least two rows",
                run_id="resume-1",
            )
            agent = LeadAgent(object(), runtime, logger, resume=resume)
            agent.original_user_task = (
                "the original request\n<resume_instruction>"
                "require at least two rows</resume_instruction>"
            )
            revised = json.loads(json.dumps(normalized))
            revised["phases"][0]["validators"] = [
                {"type": "min_rows", "value": 2},
            ]
            revised["replan_reason"] = "the user strengthened the row contract"

            result = agent.accept_task_plan(revised)

            self.assertEqual(result["status"], "done")
            self.assertFalse(agent._resume_instruction_pending)
            state = load_task_state(logger)
            self.assertEqual(state["phases"]["p1"]["status"], "pending")
            self.assertEqual(state["artifacts"], [])
            self.assertEqual(
                result["resumeReconciliation"]["changedEvidencePhases"],
                ["p1"],
            )


if __name__ == "__main__":
    unittest.main()
