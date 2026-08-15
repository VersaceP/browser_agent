import asyncio
import dataclasses
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_harness import BrowserAgent, LeadAgent, plan_candidate_hash
from harness.progress import (
    PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT,
    ProgressAccountant,
)
from harness.tools import browser_tools, lead_tools
from harness.vl.core import (
    _merged_vl_extra_params,
    visual_verify_image,
)
from harness.worker_result import build_worker_handoff_projection
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
    VLConfig,
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
    assert not any(
        event == "progress.intervention" for event, _ in agent.logger.events
    )


def test_progress_observation_is_attached_when_capability_trace_is_a_copy():
    agent = SimpleNamespace(
        trace=[],
        _pending_progress_observations=[],
    )

    async def copied_receipt_impl(target, _tool_call, _step):
        fact = {
            "source": "progress_accountant",
            "reasonObserved": "no_artifact_progress",
            "tool": "DOM.getText",
        }
        target._pending_progress_observations.append(fact)
        target.trace.append({
            "type": "progress_observation",
            "result": dict(fact),
        })
        target.trace.append({
            "type": "browser_call",
            "method": "DOM.getText",
            "result": {"status": "done", "text": "observed"},
        })
        # Capability execution returns a distinct model-facing object.
        return {"status": "done", "text": "observed"}, False

    with mock.patch.object(
        browser_tools,
        "_execute_browser_tool_impl",
        side_effect=copied_receipt_impl,
    ):
        result, should_stop = asyncio.run(browser_tools.execute_browser_tool(
            agent,
            {"name": "browser_call", "input": {}},
            7,
        ))

    assert should_stop is False
    receipt = next(
        item for item in agent.trace if item.get("type") == "browser_call"
    )["result"]
    assert receipt["progressObservations"] == result["progressObservations"]
    assert receipt["progressObservationNotice"] == result[
        "progressObservationNotice"
    ]


def test_handoff_keeps_partial_separate_from_artifact_schema_status():
    result = {
        "workerId": "browser-partial",
        "resultLevels": {
            "l1": {
                "workerId": "browser-partial",
                "phaseId": "collect",
                "status": "partial",
                "validatedStatus": "validated_done",
            },
            "l2": {
                "answer": {"format": "text", "raw": "more pages remain"},
                "data": {
                    "extractionArtifacts": [],
                    "extractionAttemptArtifacts": [],
                    "totalExtractedRows": 20,
                },
                "evidence": {},
                "blockers": [],
                "nextSteps": ["continue pagination"],
                "traceSummary": {
                    "methods": {"DOM.getText": 2},
                    "progressObservations": [{
                        "source": "progress_accountant",
                        "reasonObserved": "no_artifact_progress",
                    }],
                    "progressObservationCount": 7,
                },
            },
            "l3": {
                "artifactValidation": {"status": "done"},
            },
        },
    }

    projection = build_worker_handoff_projection(result)

    assert projection["rawReceipts"]["status"] == "partial"
    assert "validatedStatus" not in projection["rawReceipts"]
    assert projection["rawReceipts"]["artifactSchemaStatus"] == "done"
    assert projection["rawReceipts"]["progressObservationCount"] == 7
    assert projection["rawReceipts"]["progressObservations"][0][
        "reasonObserved"
    ] == "no_artifact_progress"


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
        assert emit_result["status"] == "done"
        assert emit_result["planReview"]["status"] == "error"
        assert emit_result["planReview"]["reviewed"] is False
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


def test_dom_get_img_relative_output_is_bound_to_task_artifacts():
    with tempfile.TemporaryDirectory() as root:
        agent = SimpleNamespace(logger=RunLogger(root))
        params, receipt = browser_tools._normalize_dom_get_img_output(
            agent,
            "DOM.getImg",
            {"options": {"path": "images/comments"}},
        )

        expected = (
            agent.logger.task_dir.resolve()
            / "artifacts" / "images" / "comments"
        )
        assert params["options"]["path"] == str(expected)
        assert receipt["field"] == "params.options.path"
        assert receipt["basis"] == "task_artifacts_directory"
        assert expected.is_dir()


def test_dom_get_img_absolute_output_is_preserved():
    with tempfile.TemporaryDirectory() as root:
        absolute = str((Path(root) / "exports").resolve())
        params, receipt = browser_tools._normalize_dom_get_img_output(
            SimpleNamespace(logger=RunLogger(root)),
            "DOM.getImg",
            {"options": {"path": absolute}},
        )

        assert params["options"]["path"] == absolute
        assert receipt is None


def test_plan_derives_worker_task_and_stage_hint_from_stable_intent():
    from harness.task_control import validate_task_plan

    raw = _review_plan()
    phase = raw["phases"][0]
    phase.pop("worker_task")
    phase.pop("stage_hint")

    normalized, errors = validate_task_plan(raw)

    assert errors == []
    assert normalized["phases"][0]["worker_task"] == phase["objective"]
    assert normalized["phases"][0]["stage_hint"] == "generic"


def test_near_cap_receipt_counts_remaining_turns_inclusively():
    browser = SimpleNamespace(_write_agent_event=lambda *_args, **_kwargs: None)
    browser_block = BrowserAgent._step_cap_reminder_block(
        browser, current_step=48, max_steps=50,
    )
    assert "remainingSteps=2" in browser_block["text"]
    assert "Do NOT issue" not in browser_block["text"]

    lead = SimpleNamespace(logger=_Logger())
    lead_block = LeadAgent._step_cap_reminder_block(
        lead, current_step=48, max_steps=50,
    )
    assert "remainingSteps=2" in lead_block["text"]


def _weakened_replan():
    """The a608b5e7 shape: the image objective is dropped from the plan."""
    plan = _review_plan()
    plan["replan_reason"] = "image export looked unavailable to web_scrape"
    plan["phases"] = [{
        **plan["phases"][0],
        "task_type": "file_download",
    }]
    return plan


def _agent_with_plan(root, validator):
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
    agent = LeadAgent(
        _MalformedProvider(),
        runtime,
        RunLogger(root),
        plan_validator_provider=validator,
    )
    agent.original_user_task = "Collect the requested product title."
    return agent


def test_rejected_verdict_is_never_read_as_an_absent_reviewer():
    """Task a608b5e7: `_validate_verdict` refused an approval that weakened an
    objective without citing evidence. The harness logged that finding as
    `review_unavailable` and accepted the replan anyway, dropping the image
    download. A refused verdict is a finding about the candidate, never a
    missing critic."""
    with tempfile.TemporaryDirectory() as root:
        agent = _agent_with_plan(root, _MalformedProvider())
        agent.task_plan = _review_plan()

        review = {
            "status": "error",
            "errorKind": "verdict_invalid",
            "candidateHash": plan_candidate_hash(
                _weakened_replan(),
                "image export looked unavailable to web_scrape",
            ),
            "errors": [
                "weakened/removed objectives require harness evidence ids or"
                " an explicit higher-priority user-objective authorization"
            ],
        }
        result = agent.accept_task_plan(
            _weakened_replan(), plan_validator_review=review,
        )

        assert result["status"] == "failed"
        assert result["validatorErrorKind"] == "verdict_invalid"


def test_absent_reviewer_cannot_wave_through_a_scope_changing_replan():
    with tempfile.TemporaryDirectory() as root:
        agent = _agent_with_plan(root, _MalformedProvider())
        agent.task_plan = _review_plan()

        candidate = _weakened_replan()
        review = {
            "status": "error",
            "errorKind": "transport",
            "candidateHash": plan_candidate_hash(
                candidate, candidate["replan_reason"],
            ),
            "errors": ["ConnectionError: reviewer endpoint unreachable"],
        }
        result = agent.accept_task_plan(
            candidate, plan_validator_review=review,
        )

        assert result["status"] == "failed"
        assert result["reviewScopeChanged"] is True


def test_compaction_does_not_count_the_cache_miss_it_caused():
    """Task a608b5e7 browser-002 steps 31-34.

    Compacting replaces the prompt prefix, so the next call necessarily misses
    the cache. Counting that as pressure made the detector compact again one
    step later on its own splash: step 32 compacted (uncached 44,335), step 33
    compacted again (43,152), step 34 settled at 6,263. The second compaction
    bought nothing.
    """
    from agent_harness import CachePressureState, update_cache_pressure_state
    from runtime_config import HarnessConfig

    config = HarnessConfig()
    # (step, uncached_input, cache_read), verbatim from the run log.
    observed = [
        (31, 19732, 154880),
        (32, 44335, 22528),
        (33, 43152, 22528),
        (34, 6263, 65536),
    ]

    state, forced_steps = CachePressureState(), []
    for step, uncached, cache_read in observed:
        if step == 32:
            # A compaction ran before this call, rebuilding the prefix.
            state = CachePressureState(awaiting_prefix_reuse=True)
        state, reason = update_cache_pressure_state(
            state,
            usage_payload={"uncached_input": uncached, "cache_read": cache_read},
            config=config,
            step=step,
            max_steps=50,
        )
        if reason:
            forced_steps.append(step)

    assert forced_steps == []
    # The prefix proving itself warm is what clears the suppression, not a
    # step count guessed by the harness.
    assert state.awaiting_prefix_reuse is False


def test_sustained_misses_without_a_compaction_still_force_one():
    from agent_harness import CachePressureState, update_cache_pressure_state
    from runtime_config import HarnessConfig

    config = HarnessConfig()
    state, forced_steps = CachePressureState(), []
    for step in (10, 11):
        state, reason = update_cache_pressure_state(
            state,
            usage_payload={"uncached_input": 19732, "cache_read": 80000},
            config=config,
            step=step,
            max_steps=50,
        )
        if reason:
            forced_steps.append(step)

    assert forced_steps == [11]


def test_suppression_ends_once_the_rebuilt_prefix_is_read_back():
    """A worker whose every step carries a large tool result still gets one.

    The suppression used to clear only when `uncached_input` fell back under
    the threshold. On this profile it never does, so the detector went quiet
    for the rest of the run after its first compaction. What ends the
    attribution here is the rebuilt prefix being read back — cache_read rising
    above the floor the rebuild call landed on — which is a fact about the
    prefix rather than about how expensive the step happened to be.
    """
    from agent_harness import CachePressureState, update_cache_pressure_state
    from runtime_config import HarnessConfig

    config = HarnessConfig()
    state = CachePressureState(awaiting_prefix_reuse=True)
    forced_steps = []
    cache_read = 22528
    for step in range(20, 26):
        state, reason = update_cache_pressure_state(
            state,
            usage_payload={"uncached_input": 30000, "cache_read": cache_read},
            config=config,
            step=step,
            max_steps=50,
        )
        if reason:
            forced_steps.append(step)
        # The conversation keeps growing, so the warm prefix keeps growing too.
        cache_read += 12000

    # Step 20 paid for the rebuild; 21 read it back and counted; 22 forced.
    # Nothing compacts in this unit, so the streak simply rebuilds and fires
    # again — in the agent loop the first reason rebuilds the prefix and
    # re-enters the attribution.
    assert forced_steps == [22, 24]


def test_a_prefix_that_is_never_read_back_stays_attributed_to_the_rebuild():
    from agent_harness import CachePressureState, update_cache_pressure_state
    from runtime_config import HarnessConfig

    config = HarnessConfig()
    state = CachePressureState(awaiting_prefix_reuse=True)
    forced_steps = []
    for step in range(20, 26):
        state, reason = update_cache_pressure_state(
            state,
            usage_payload={"uncached_input": 30000, "cache_read": 22528},
            config=config,
            step=step,
            max_steps=50,
        )
        if reason:
            forced_steps.append(step)

    # Nothing here says the rebuild helped, so nothing here orders another one.
    assert forced_steps == []
    assert state.rebuild_cache_read == 22528


def test_row_preference_keeps_the_row_with_content_without_the_stub_threshold():
    """Dedup across cumulative files picks one row per key; nobody is rejected.

    `detect_stub_rows` used to add a failure here, which made an invented
    cutoff decide what survived - and it was already excluded from the
    aggregate check a few lines away, so the ranking and the validation
    disagreed. The field counts do the same job by counting.
    """
    from harness.task_control import make_row_preference

    expected = {
        "required_fields": ["id", "replies"],
        "field_specs": [{"name": "replies", "type": "array"}],
    }
    prefer = make_row_preference(validators=[], expected=expected)

    filled = {"id": "a", "replies": [{"text": "hi"}]}
    empty = {"id": "a", "replies": []}

    assert prefer(filled, empty) is True
    assert prefer(empty, filled) is False


def test_row_preference_does_not_reject_a_legitimately_empty_row():
    from harness.task_control import make_row_preference

    expected = {"required_fields": ["id", "replies"]}
    prefer = make_row_preference(validators=[], expected=expected)

    empty = {"id": "a", "replies": []}
    # The only candidate for its key still wins against itself: preference is
    # a comparator, never a gate.
    assert prefer(empty, empty) is True


def test_vl_endpoint_limit_is_checked_before_reading_or_base64_encoding():
    """An oversized screenshot must be rejected from stat arithmetic alone."""

    with tempfile.TemporaryDirectory() as root:
        image = Path(root) / "large.png"
        image.write_bytes(b"x" * 12)
        config = VLConfig(
            enabled=True,
            provider="openai",
            model_id="vl-test",
            max_encoded_image_bytes=10,
        )
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("oversized image was read"),
        ):
            result = asyncio.run(visual_verify_image(
                config=config,
                image_path=str(image),
                expected={},
            ))

    assert result["reason"] == "payload_over_endpoint_limit"
    assert result["fileBytes"] == 12
    assert result["encodedBytes"] == len("data:image/png;base64,") + 16


def test_ordinary_vl_does_not_inherit_global_thinking_controls():
    config = VLConfig(extra_params={
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "temperature": 0,
    })

    ordinary = _merged_vl_extra_params(
        config,
        {},
        inherit_base_thinking=False,
    )
    captcha = _merged_vl_extra_params(
        config,
        {"thinking": {"type": "disabled"}},
        inherit_base_thinking=True,
    )

    assert ordinary == {"temperature": 0}
    assert captcha["thinking"] == {"type": "disabled"}
    assert captcha["reasoning_effort"] == "high"


def test_visual_verify_wires_thinking_only_to_the_captcha_role():
    config = VLConfig(
        enabled=True,
        provider="openai",
        model_id="vl-test",
        extra_params={"thinking": {"type": "enabled"}},
    )
    provider_result = (
        '{"verdict":"uncertain","confidence":0,"visible_evidence":[],"reason":"test"}',
        {},
    )
    with tempfile.TemporaryDirectory() as root:
        image = Path(root) / "frame.png"
        image.write_bytes(b"small")
        with mock.patch(
            "harness.vl.core._call_openai_compatible",
            new=mock.AsyncMock(return_value=provider_result),
        ) as provider:
            asyncio.run(visual_verify_image(
                config=config,
                image_path=str(image),
                expected={},
                mode="action_outcome",
            ))
            assert provider.await_args.kwargs["inherit_base_thinking"] is False

            asyncio.run(visual_verify_image(
                config=config,
                image_path=str(image),
                expected={},
                mode="captcha_solve",
            ))
            assert provider.await_args.kwargs["inherit_base_thinking"] is True


def test_vl_endpoint_limit_rejects_invalid_configuration():
    for value in (0, -1, "many"):
        try:
            VLConfig.from_dict({"max_encoded_image_bytes": value})
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid endpoint limit was accepted: {value!r}")
