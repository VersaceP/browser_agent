"""Pure recovery resume reopens HITL-interrupted phases and the validator
error loop is bounded instead of replaying a cached failure forever."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.task_control import (
    initialize_task_state,
    prepare_resume_state,
    write_task_plan,
    write_task_state,
)
from harness.utils import RunLogger
from agent_harness import LeadAgent
from runtime_config import (
    ABCPClientConfig,
    HarnessConfig,
    ModelConfig,
    PlanValidatorConfig,
    RuntimeConfig,
)


def _phase(phase_id, depends_on=None):
    return {
        "id": phase_id,
        "task_type": "web_scrape",
        "objective": f"Collect rows for {phase_id}",
        "worker_task": f"Open the page and collect {phase_id} rows.",
        "stage_hint": "collection",
        "stage_hint_reason": "Rows are enumerated on a listing surface.",
        "depends_on": depends_on or [],
        "expected_artifact": {
            "name": f"rows_{phase_id}",
            "fields": ["name", "url"],
        },
        "validators": [
            {"type": "required_fields", "fields": ["name", "url"]},
            {"type": "min_rows", "value": 1},
        ],
        "max_attempts": 3,
    }


def _plan(*phases):
    return {"goal": "collect", "phases": list(phases)}


def _raw_plan(*, worker_task="", replan_reason=""):
    plan = {
        "version": "v1",
        "goal": "Collect product titles and the requested review records.",
        "task_type": "web_scrape",
        "phases": [{
            "id": "details",
            "type": "browser_worker",
            "task_type": "web_scrape",
            "objective": "Collect product titles and review records.",
            "worker_task": worker_task or (
                "Open the declared product page, reveal the review collection,"
                " and persist only observed review records."
            ),
            "stage_hint": "collection",
            "stage_hint_reason": (
                "The target fields are enumerated across a listing surface and"
                " require materializing the declared collection before"
                " extraction."
            ),
            "depends_on": [],
            "expected_artifact": {
                "name": "product_details",
                "fields": ["title", "reviews"],
                "exact_rows": 20,
            },
            "validators": [
                {"type": "exact_rows", "count": 20},
                {"type": "field_nonempty", "field": "title"},
            ],
            "worker_contract": {"task_type": "web_scrape"},
        }],
    }
    if replan_reason:
        plan["replan_reason"] = replan_reason
    return plan


class _ApproveProvider:
    async def generate_response(self, system_prompt, messages, tools):
        raise AssertionError("approve provider should be patched per test")


def _runtime(root):
    return RuntimeConfig(
        agent_id="lead",
        lead=ModelConfig(provider="openai", model_id="lead-model"),
        worker=ModelConfig(provider="openai", model_id="lead-model"),
        browser=ABCPClientConfig(),
        harness=HarnessConfig(worktree_dir=str(root)),
        plan_validator=PlanValidatorConfig(
            enabled=True,
            provider="openai",
            model_id="auditor-model",
            api_key="test-only",
        ),
    )


def _agent(root):
    logger = RunLogger(str(root))
    agent = LeadAgent(
        _ApproveProvider(),
        _runtime(root),
        logger,
        plan_validator_provider=_ApproveProvider(),
    )
    agent.original_user_task = (
        "Collect product titles and exactly 20 requested review records."
    )
    return agent, logger


def _transport_error_review(provider, *args, **kwargs):
    calls = {"count": 0}

    async def _review(*_args, **_kwargs):
        calls["count"] += 1
        return {
            "status": "error",
            "errorKind": "transport",
            "errors": ["LLMConnectionError: reviewer lost the connection"],
        }

    _review.calls = calls
    return _review


class ResumeHitlReactivationTest(unittest.TestCase):
    def _task_dir(self, root):
        logger = RunLogger(root, task_id="task")
        plan = _plan(_phase("p_done"), _phase("p_hitl"), _phase("p_open"))
        write_task_plan(logger, plan)
        write_task_state(logger, {"phases": {
            "p_done": {"status": "validated_done", "attempts": [],
                       "validated_artifacts": []},
            "p_hitl": {"status": "hitl_required", "attempts": [],
                       "validated_artifacts": []},
            "p_open": {"status": "hitl_timeout", "attempts": [],
                       "validated_artifacts": []},
        }, "artifacts": []})
        return logger, plan

    def test_pure_resume_reopens_hitl_terminal_phases(self):
        with tempfile.TemporaryDirectory() as root:
            logger, plan = self._task_dir(root)
            report = prepare_resume_state(logger, old_plan=plan)
            phases = report["state"]["phases"]
            self.assertEqual(phases["p_hitl"]["status"], "pending")
            self.assertEqual(
                phases["p_hitl"]["resume_reset_from"], "hitl_required"
            )
            self.assertEqual(phases["p_open"]["status"], "pending")
            self.assertEqual(phases["p_open"]["resume_reset_from"], "hitl_timeout")
            self.assertEqual(phases["p_done"]["status"], "validated_done")
            self.assertEqual(
                report["hitlReactivatedPhases"], ["p_hitl", "p_open"]
            )

    def test_resume_with_new_instruction_leaves_hitl_phases_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            logger, plan = self._task_dir(root)
            report = prepare_resume_state(
                logger, old_plan=plan, instruction="Now collect ranks 4-6."
            )
            phases = report["state"]["phases"]
            self.assertEqual(phases["p_hitl"]["status"], "hitl_required")
            self.assertEqual(phases["p_open"]["status"], "hitl_timeout")
            self.assertEqual(report["hitlReactivatedPhases"], [])

    def test_reactivation_skips_invalidated_phases(self):
        with tempfile.TemporaryDirectory() as root:
            logger, plan = self._task_dir(root)
            changed = _plan(
                _phase("p_done"),
                # Same id but a changed validator invalidates evidence.
                {**_phase("p_hitl"), "validators": [
                    {"type": "required_fields", "fields": ["name", "url", "sku"]},
                ]},
                _phase("p_open"),
            )
            report = prepare_resume_state(
                logger, old_plan=plan, new_plan=changed,
            )
            phases = report["state"]["phases"]
            self.assertEqual(phases["p_hitl"]["status"], "pending")
            self.assertEqual(
                phases["p_hitl"].get("resume_reset_reason"),
                "evidence_contract_changed",
            )
            self.assertNotIn(
                "p_hitl", report.get("hitlReactivatedPhases") or []
            )


class PendingHumanInterventionTest(unittest.TestCase):
    def test_local_file_authorization_extracted_from_run_log_tail(self):
        from main import _pending_human_interventions
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "run.jsonl"
            events = [
                {"type": "spawner.browser.result", "payload": {
                    "phaseId": "detail_r4", "status": "hitl_required",
                    "answer": (
                        "Local file authorization is required in the task"
                        " terminal: write /Users/u/Desktop/out. No file"
                        " operation was executed."
                    ),
                }},
                {"type": "lead.model", "payload": {"step": 1}},
                {"type": "spawner.browser.result", "payload": {
                    "phaseId": "detail_r5", "status": "hitl_required",
                    "answer": "Local file authorization is required: read /x.",
                }},
            ]
            log.write_text(
                "\n".join(json.dumps(e) for e in events) + "\n",
                encoding="utf-8",
            )
            found = _pending_human_interventions(
                log, ["detail_r4", "detail_r5"]
            )
            self.assertEqual(
                [item["phaseId"] for item in found],
                ["detail_r4", "detail_r5"],
            )
            self.assertIn("Local file authorization", found[0]["message"])

    def test_no_hits_and_missing_log_are_safe(self):
        from main import _pending_human_interventions
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "run.jsonl"
            log.write_text(
                json.dumps({"type": "spawner.browser.result", "payload": {
                    "phaseId": "p", "status": "done",
                }}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(_pending_human_interventions(log, ["p"]), [])
            self.assertEqual(
                _pending_human_interventions(
                    Path(root) / "missing.jsonl", ["p"]
                ),
                [],
            )


class ValidatorErrorLoopTest(unittest.TestCase):
    def test_error_cache_expires_and_allows_retry_after_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            agent, _ = _agent(root)
            raw = _raw_plan()
            with patch(
                "agent_harness.review_plan_revision",
                new=_transport_error_review(None),
            ) as review_patch:
                first = asyncio.run(agent.review_task_plan_candidate(raw))
                self.assertEqual(first["status"], "error")
                self.assertNotIn("deduplicated", first)
                second = asyncio.run(agent.review_task_plan_candidate(raw))
                self.assertTrue(second.get("deduplicated"))
                self.assertFalse(second.get("providerCalled"))
                # Age the cached error past the TTL: the reviewer gets
                # another chance instead of replaying the failure forever.
                for entry in agent._plan_validator_error_cache.values():
                    entry["cachedAt"] = time.time() - 10_000
                third = asyncio.run(agent.review_task_plan_candidate(raw))
                self.assertNotIn("deduplicated", third)
                self.assertGreaterEqual(review_patch.calls["count"], 2)

    def test_unreviewed_replan_stops_on_first_exhausted_review(self):
        with tempfile.TemporaryDirectory() as root:
            agent, logger = _agent(root)
            initial = _raw_plan()
            with patch(
                "agent_harness.review_plan_revision",
                new=_transport_error_review(None),
            ):
                failing_review = asyncio.run(
                    agent.review_task_plan_candidate(initial)
                )
                # Bootstrap an accepted plan without the reviewer: use the
                # fail-open path by disabling the validator gate's rejection
                # through infrastructure_unreviewed (no prior plan).
                accepted = agent.accept_task_plan(
                    initial, plan_validator_review=failing_review,
                )
            self.assertEqual(accepted["status"], "done")
            replan = _raw_plan(
                worker_task="Use the validated route for the remaining rows.",
                replan_reason="Reuse the validated route for the remaining row.",
            )
            reviews = []
            with patch(
                "agent_harness.review_plan_revision",
                new=_transport_error_review(None),
            ):
                # Emit path re-reviews on every submission: the second one is
                # answered from the error cache and carries deduplicated.
                for _ in range(3):
                    reviews.append(asyncio.run(
                        agent.review_task_plan_candidate(replan)
                    ))
            self.assertFalse(reviews[0].get("deduplicated"))
            self.assertTrue(reviews[1].get("deduplicated"))
            results = [
                agent.accept_task_plan(replan, plan_validator_review=review)
                for review in reviews
            ]
            for result in results:
                self.assertEqual(result['status'], 'blocked')
                self.assertTrue(result['_terminate_lead'])
                self.assertTrue(result['acceptedPlanUnchanged'])
                self.assertEqual(result['errorCode'], 'plan_validator_unavailable')
                self.assertIn('Stop this run', result['next_instruction'])


if __name__ == "__main__":
    unittest.main()
