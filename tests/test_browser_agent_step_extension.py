import tempfile
import unittest
from pathlib import Path

from agent_harness import BrowserAgent
from harness.tools.browser_tools import build_browser_agent_tool_specs
from harness.utils import RunLogger
from runtime_config import (
    ABCPClientConfig,
    HarnessConfig,
    ModelConfig,
    RuntimeConfig,
)


class _FakeBrowser:
    async def call(self, method, params):
        if method == "System.register":
            return {"data": {"agentId": params.get("agentId")}}
        if method == "System.getCapabilities":
            return {"data": []}
        return {"data": {}}


class _ExtensionThenFinalProvider:
    """Use one turn beyond the original two-step cap before finalizing."""

    def __init__(self):
        self.calls = 0
        self.saw_extension_tool = False

    async def generate_response(self, system_prompt, messages, tools):
        self.calls += 1
        self.saw_extension_tool = self.saw_extension_tool or any(
            item.get("name") == "request_step_extension" for item in tools
        )
        if self.calls == 1:
            return "", [{
                "id": "extension-1",
                "name": "request_step_extension",
                "input": {
                    "estimated_steps": 2,
                    "remaining_actions": ["verify the final state", "finish"],
                    "evidence": "The target page is already open.",
                },
            }], "tool_use", {}
        if self.calls in {2, 3}:
            # A second request must be denied but must not terminate the worker.
            return "", [{
                "id": f"extension-repeat-{self.calls}",
                "name": "request_step_extension",
                "input": {
                    "estimated_steps": 1,
                    "remaining_actions": ["finish"],
                    "evidence": "The extension is already active.",
                },
            }], "tool_use", {}
        return "", [{
            "id": "final-1",
            "name": "final_answer",
            "input": {"status": "done", "answer": "completed after extension"},
        }], "tool_use", {}


def _runtime(worktree: str, **harness_overrides):
    harness = HarnessConfig(
        worktree_dir=str(Path(worktree) / "worktree"),
        max_steps=50,
        worker_max_steps=50,
        **harness_overrides,
    )
    return RuntimeConfig(
        agent_id="extension-test-agent",
        model=ModelConfig(provider="fake", model_id="fake"),
        browser=ABCPClientConfig(),
        harness=harness,
    )


class StepExtensionConfigTests(unittest.TestCase):
    def test_defaults_preserve_fixed_step_cap(self):
        config = HarnessConfig.from_dict({})
        self.assertFalse(config.browser_agent_step_extension_enabled)
        self.assertEqual(config.browser_agent_max_extension_steps, 10)

    def test_config_is_strict_and_bounded(self):
        configured = HarnessConfig.from_dict({
            "browser_agent_step_extension_enabled": True,
            "browser_agent_max_extension_steps": 12,
        })
        self.assertTrue(configured.browser_agent_step_extension_enabled)
        self.assertEqual(configured.browser_agent_max_extension_steps, 12)

        for value in ("false", 0, 1, None):
            with self.subTest(enabled=value):
                with self.assertRaises(ValueError):
                    HarnessConfig.from_dict({
                        "browser_agent_step_extension_enabled": value,
                    })
        for value in (True, 0, 51, 1.5, "10"):
            with self.subTest(max_steps=value):
                with self.assertRaises(ValueError):
                    HarnessConfig.from_dict({
                        "browser_agent_max_extension_steps": value,
                    })

    def test_tool_is_visible_only_when_feature_is_enabled(self):
        hidden = build_browser_agent_tool_specs(set())
        visible = build_browser_agent_tool_specs(
            set(), step_extension_enabled=True,
        )
        self.assertNotIn(
            "request_step_extension", {item["name"] for item in hidden}
        )
        self.assertIn(
            "request_step_extension", {item["name"] for item in visible}
        )


class StepExtensionGuardTests(unittest.TestCase):
    def _agent(self, worktree: str) -> BrowserAgent:
        runtime = _runtime(
            worktree,
            browser_agent_step_extension_enabled=True,
            browser_agent_max_extension_steps=10,
        )
        logger = RunLogger(worktree, task_id="extension-guard")
        return BrowserAgent(object(), _FakeBrowser(), runtime, logger)

    def test_one_near_cap_request_is_granted_and_second_is_denied(self):
        with tempfile.TemporaryDirectory() as worktree:
            agent = self._agent(worktree)
            granted = agent.request_step_extension({
                "estimated_steps": 7,
                "remaining_actions": ["select option", "verify field"],
                "evidence": "All other required fields are complete.",
            }, step=48)
            self.assertEqual(granted["status"], "granted")
            self.assertEqual(agent.effective_max_steps, 57)

            denied = agent.request_step_extension({
                "estimated_steps": 1,
                "remaining_actions": ["finish"],
                "evidence": "One action remains.",
            }, step=49)
            self.assertEqual(denied["status"], "denied")
            self.assertIn("extension_already_granted", denied["reasons"])
            self.assertEqual(agent.effective_max_steps, 57)

    def test_recent_loop_nudge_denies_request(self):
        with tempfile.TemporaryDirectory() as worktree:
            agent = self._agent(worktree)
            agent.trace.append({
                "type": "loop_nudge",
                "step": 47,
                "result": {"reason": "repeated_action_page_stalled"},
            })
            denied = agent.request_step_extension({
                "estimated_steps": 5,
                "remaining_actions": ["retry selection"],
                "evidence": "The page is still open.",
            }, step=48)
            self.assertEqual(denied["status"], "denied")
            self.assertIn("recent_loop_nudge", denied["reasons"])

    def test_early_oversized_hitl_and_consecutive_failures_are_denied(self):
        with tempfile.TemporaryDirectory() as worktree:
            agent = self._agent(worktree)
            agent.diagnostics.last_pause_pageId = "page-paused"
            agent._recent_tool_outcomes = [
                {"step": 47, "tool": "browser_call", "failed": True},
                {"step": 48, "tool": "browser_call", "failed": True},
            ]
            denied = agent.request_step_extension({
                "estimated_steps": 11,
                "remaining_actions": ["continue"],
                "evidence": "Unverified.",
            }, step=20)
            self.assertEqual(denied["status"], "denied")
            self.assertTrue({
                "request_too_early",
                "estimate_exceeds_configured_limit",
                "consecutive_tool_failures",
                "hitl_unresolved",
            }.issubset(set(denied["reasons"])))

    def test_trace_summary_keeps_grant_when_a_later_request_is_denied(self):
        from harness.spawner.spawner_worker import SpawnerWorkerMixin

        trace = [
            {
                "type": "step_extension_request",
                "result": {
                    "status": "granted",
                    "step": 48,
                    "grantedSteps": 7,
                    "effectiveMaxSteps": 57,
                },
            },
            {
                "type": "step_extension_request",
                "result": {
                    "status": "denied",
                    "step": 49,
                    "reasons": ["extension_already_granted"],
                },
            },
        ]
        summary = SpawnerWorkerMixin._summarize_worker_trace(object(), trace)
        extension = summary["stepExtension"]
        self.assertEqual(extension["requestCount"], 2)
        self.assertTrue(extension["granted"])
        self.assertEqual(extension["grantedSteps"], 7)
        self.assertEqual(extension["effectiveMaxSteps"], 57)
        self.assertEqual(extension["deniedCount"], 1)


class StepExtensionRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_can_continue_past_original_cap_but_not_hard_limit(self):
        with tempfile.TemporaryDirectory() as worktree:
            runtime = _runtime(
                worktree,
                browser_agent_step_extension_enabled=True,
                browser_agent_max_extension_steps=2,
            )
            runtime.harness.max_steps = 2
            runtime.harness.worker_max_steps = 2
            provider = _ExtensionThenFinalProvider()
            logger = RunLogger(worktree, task_id="extension-run")
            agent = BrowserAgent(provider, _FakeBrowser(), runtime, logger)

            answer = await agent.run("Complete the current phase")

            self.assertEqual(answer, "completed after extension")
            self.assertEqual(agent.final_status, "done")
            self.assertEqual(provider.calls, 4)
            self.assertTrue(provider.saw_extension_tool)
            self.assertEqual(agent.base_max_steps, 2)
            self.assertEqual(agent.effective_max_steps, 4)
            self.assertEqual(agent._step_extension_granted_steps, 2)


if __name__ == "__main__":
    unittest.main()
