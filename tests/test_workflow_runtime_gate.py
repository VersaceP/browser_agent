"""Workflow visibility obeys the runtime switch and live capability."""

from types import SimpleNamespace

from harness.tools.browser_tools.dispatch import build_browser_agent_tool_specs
from harness.workflow.workflow_runtime import workflow_execution_enabled
from runtime_config import HarnessConfig


def test_master_switch_defaults_closed_and_parses_explicit_value():
    assert HarnessConfig().workflow_execution_enabled is False
    assert HarnessConfig.from_dict({"workflow_execution_enabled": True}).workflow_execution_enabled
    assert workflow_execution_enabled(SimpleNamespace()) is False


def test_published_skill_workflow_requires_switch_and_live_capability():
    capability = {"Page.getState", "Workflow.execute"}
    def names(methods, enabled, selected=True):
        return {item["name"] for item in build_browser_agent_tool_specs(
            methods, workflow_enabled=enabled, selected_skill_available=selected)}
    assert "execute_published_skill_workflow" in names(capability, True)
    assert "execute_published_skill_workflow" not in names(capability, False)
    assert "execute_published_skill_workflow" not in names({"Page.getState"}, True)
    assert "execute_published_skill_workflow" not in names(capability, True, selected=False)
    assert "execute_selected_skill" not in names(capability, True)
