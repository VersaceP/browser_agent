import argparse
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import main


class CliSkillSelectionTests(unittest.TestCase):
    def setUp(self):
        from harness.skill_builder.catalog import SkillCatalog
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        skill = root / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\nversion: 1\n---\nDemo\n")
        self.patch_catalog = patch.object(
            main, "_skill_catalog_for_runtime",
            side_effect=lambda _runtime: SkillCatalog(root / "skills", root / "db.sqlite"),
        )
        self.patch_catalog.start()

    def tearDown(self):
        self.patch_catalog.stop()
        self.temp.cleanup()

    def test_skill_flag_parses(self):
        args = main.build_arg_parser().parse_args(["--skill", "demo", "a task"])
        self.assertEqual(args.skill, "demo")

    def test_slash_skill_selects_exact_name(self):
        ns = argparse.Namespace(skill="", config="config.json")
        inline = main._handle_skill_command("/skill demo scrape the page", ns)
        self.assertEqual(ns.skill, "demo")
        self.assertEqual(inline, "scrape the page")

    def test_slash_skill_off_and_unknown(self):
        ns = argparse.Namespace(skill="demo", config="config.json")
        main._handle_skill_command("/skill off", ns)
        self.assertEqual(ns.skill, "")
        main._handle_skill_command("/skill unknown", ns)
        self.assertEqual(ns.skill, "")

    def test_slash_skill_list_does_not_select(self):
        ns = argparse.Namespace(skill="", config="config.json")
        self.assertEqual(main._handle_skill_command("/skill", ns), "")
        self.assertEqual(ns.skill, "")


class CliAgentModeSelectionTests(unittest.TestCase):
    def test_mode_command_selects_browser_for_current_invocation(self):
        args = argparse.Namespace(agent_mode="")
        self.assertTrue(main._handle_agent_mode_command("/browser", args))
        self.assertEqual(args.agent_mode, "browser")

    def test_mode_command_rejects_inline_task(self):
        args = argparse.Namespace(agent_mode="")
        self.assertTrue(
            main._handle_agent_mode_command("/lead scrape this page", args)
        )
        self.assertEqual(args.agent_mode, "")

    def test_old_agent_mode_flag_is_not_a_cli_surface(self):
        with self.assertRaises(SystemExit):
            main.build_arg_parser().parse_args(["--agent-mode", "browser"])

    def test_interactive_task_requires_mode_before_submission(self):
        args = argparse.Namespace(
            task_option="", task="", resume="", resume_instruction="",
            agent_mode="", configured_agent_mode="lead", config="config.json",
            skill="",
        )

        class TtyInput:
            def isatty(self):
                return True

        with patch.object(main.sys, "stdin", TtyInput()), patch(
            "builtins.input", side_effect=["open the page", "/lead", "open the page"]
        ):
            self.assertEqual(main.read_task(args), "open the page")
        self.assertEqual(args.agent_mode, "lead")


class CliResumeParsingTests(unittest.TestCase):
    def test_resume_flags_parse(self):
        args = main.build_arg_parser().parse_args([
            "--resume", "worktree/task-1",
            "--resume-retry-interrupted",
            "--task", "continue with a narrower range",
        ])
        self.assertEqual(args.resume, "worktree/task-1")
        self.assertTrue(args.resume_retry_interrupted)
        self.assertEqual(args.task_option, "continue with a narrower range")

    def test_deleted_resume_directory_fails_without_recreating(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "deleted-task"
            with self.assertRaisesRegex(ValueError, "不存在或已被删除"):
                main._resolve_resume_directory(str(missing))
            self.assertFalse(missing.exists())

    def test_interactive_resume_command_rejects_new_instruction(self):
        with tempfile.TemporaryDirectory() as root:
            task_dir = Path(root) / "task"
            task_dir.mkdir()
            args = argparse.Namespace(resume="", resume_instruction="")
            instruction = main._handle_resume_command(
                f'/resume "{task_dir}" 只继续未完成部分', args,
            )
            self.assertIsNone(instruction)
            self.assertEqual(args.resume, "")


if __name__ == "__main__":
    unittest.main()
