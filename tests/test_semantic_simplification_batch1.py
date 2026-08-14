import asyncio
import dataclasses
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_harness import LeadAgent
from harness.progress import (
    PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT,
    ProgressAccountant,
)
from harness.tools import browser_tools, lead_tools
from harness.tools.lead_tools import (
    _numeric_reconciliation_rejection,
    _reconcile_final_answer_numbers,
)
from harness.utils import RunLogger
from runtime_config import (
    ABCPClientConfig,
    HarnessConfig,
    ModelConfig,
    PlanValidatorConfig,
    RuntimeConfig,
)


class _Logger:
    def __init__(self):
        self.events = []

    def write(self, event, payload):
        self.events.append((event, payload))


def test_progress_observation_does_not_block_and_is_in_trace_receipt():
    called = []
    returned = {"status": "done", "rows": [{"id": "rank-15"}]}

    async def handler(_ctx):
        called.append(True)
        return returned

    action = dataclasses.replace(
        browser_tools.BROWSER_TOOLS.get("local_fs_read"),
        handler=handler,
        loop_guard=False,
        contract_check=False,
        progress_check=True,
        trace_type="test_action",
    )
    accountant = ProgressAccountant()
    accountant.turns_since_artifact_progress = (
        PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT + 1
    )
    accountant.local_fs_without_extraction = 6
    accountant.local_fs_streak = 6
    agent = SimpleNamespace(
        progress=accountant,
        logger=_Logger(),
        trace=[],
        runtime=SimpleNamespace(harness=SimpleNamespace(
            progress_local_fs_without_extraction_limit=5,
            progress_no_artifact_limit=8,
        )),
        worker_contract={"must_record_extraction": True},
        artifacts=[],
        extraction_attempt_artifacts=[],
        page_lifecycle=None,
    )

    with mock.patch.object(
        browser_tools.BROWSER_TOOLS,
        "get",
        return_value=action,
    ), mock.patch.object(
        browser_tools,
        "_call_extraction_progress_gate",
        return_value=None,
    ):
        result, should_stop = asyncio.run(browser_tools.execute_browser_tool(
            agent,
            {"name": "local_fs_read", "input": {"path": "evidence.json"}},
            12,
        ))

    assert called == [True]
    assert should_stop is False
    assert result["status"] == "done"
    assert "variant A" not in result["progressObservationNotice"]
    assert result["progressObservations"][0]["source"] == "progress_accountant"
    assert result["progressObservations"][0]["reasonObserved"]
    observation_trace = next(
        item for item in agent.trace if item.get("type") == "progress_observation"
    )
    action_trace = next(
        item for item in agent.trace if item.get("type") == "test_action"
    )
    assert observation_trace["result"] == result["progressObservations"][0]
    assert action_trace["result"]["progressObservations"] == result["progressObservations"]
    assert any(event == "progress.observed" for event, _ in agent.logger.events)


def test_optional_identifier_null_spellings_are_removed_at_lead_boundary():
    captured = {}

    async def handler(ctx):
        captured.update(ctx.tool_input)
        return {"status": "done"}

    action = dataclasses.replace(
        lead_tools.LEAD_TOOLS.get("spawn_browser_agent"),
        handler=handler,
        loop_guard=False,
    )
    agent = SimpleNamespace(logger=_Logger())
    with mock.patch.object(
        lead_tools.LEAD_TOOLS,
        "get",
        return_value=action,
    ):
        result, should_stop = asyncio.run(lead_tools.execute_lead_tool(
            agent,
            {
                "name": "spawn_browser_agent",
                "input": {
                    "phase_id": "details",
                    "fleet_id": "null",
                    "session_key": "",
                    "worker_contract": {
                        "fleet_id": None,
                        "session_key": "NULL",
                        "task_type": "web_scrape",
                    },
                },
            },
        ))

    assert should_stop is False
    assert captured["phase_id"] == "details"
    assert "fleet_id" not in captured
    assert "session_key" not in captured
    assert captured["worker_contract"] == {"task_type": "web_scrape"}
    assert result["normalizedFields"] == [
        "fleet_id",
        "session_key",
        "worker_contract.fleet_id",
        "worker_contract.session_key",
    ]


class _MalformedProvider:
    def __init__(self):
        self.calls = 0

    async def generate_response(self, *args, **kwargs):
        self.calls += 1
        return "plain text instead of the required tool call", [], "end_turn", {}


def _review_plan():
    return {
        "version": "v1",
        "goal": "Collect the requested product title.",
        "task_type": "web_scrape",
        "phases": [{
            "id": "details",
            "type": "browser_worker",
            "task_type": "web_scrape",
            "objective": "Collect the requested product title.",
            "worker_task": "Open the product and persist its observed title.",
            "stage_hint": "detail_sections",
            "depends_on": [],
            "expected_artifact": {
                "name": "product_details",
                "fields": ["title"],
                "min_rows": 1,
            },
        }],
    }


def test_identical_plan_validator_protocol_error_is_deduplicated():
    with tempfile.TemporaryDirectory() as root:
        runtime = RuntimeConfig(
            agent_id="test-agent",
            model=ModelConfig(provider="openai", model_id="lead-model"),
            browser=ABCPClientConfig(),
            harness=HarnessConfig(worktree_dir=root),
            plan_validator=PlanValidatorConfig(
                enabled=True,
                provider="openai",
                model_id="reviewer-model",
                api_key="test-only",
            ),
        )
        validator = _MalformedProvider()
        agent = LeadAgent(
            _MalformedProvider(),
            runtime,
            RunLogger(root),
            plan_validator_provider=validator,
        )
        agent.original_user_task = "Collect the requested product title."

        first = asyncio.run(agent.review_task_plan_candidate(_review_plan()))
        second = asyncio.run(agent.review_task_plan_candidate(_review_plan()))

        assert first["status"] == "error"
        assert second["status"] == "error"
        assert second["deduplicated"] is True
        assert second["providerCalled"] is False
        assert second["errors"] == first["errors"]
        assert validator.calls == 1

        emit_result, should_stop = asyncio.run(lead_tools.execute_lead_tool(
            agent,
            {"name": "emit_task_plan", "input": {"plan": _review_plan()}},
        ))
        assert should_stop is False
        assert emit_result["status"] == "failed"
        assert "response protocol" in emit_result["error"]
        assert "not a semantic rejection" in emit_result["next_instruction"]
        assert emit_result["planValidator"]["deduplicated"] is True
        assert validator.calls == 1


def test_extractor_unusable_is_visible_but_not_a_final_rejection():
    class _LoggerWithPath(_Logger):
        def __init__(self, task_dir):
            super().__init__()
            self.task_dir = Path(task_dir)

    with tempfile.TemporaryDirectory() as root:
        provider = _MalformedProvider()
        agent = SimpleNamespace(
            claim_extractor_provider=provider,
            claim_extractor_provider_name="openai",
            claim_extractor_model="extractor-model",
            logger=_LoggerWithPath(root),
        )
        report = asyncio.run(_reconcile_final_answer_numbers(
            agent,
            "Collected 18 products.",
            {},
        ))

    assert report["status"] == "extractor_unusable"
    assert report["checked"] == 0
    assert report["verifiedClaimCount"] == 0
    assert _numeric_reconciliation_rejection(report) is None
