"""Regression tests for extend_task_plan.

The tool exists so that "also scrape these other URLs" does not require the
model to restate a plan it did not author.  Every test here is ultimately about
one promise: an extension appends, and never retires what is already validated.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_harness
from abcp_client import ABCPClientConfig
from agent_harness import LeadAgent, ResumeContext
from harness.completion_receipt import (
    build_completion_receipt,
    terminal_consistency_contradictions,
)
from harness.constants import WORKER_STATUS_DONE
from harness.plan_validator import plan_candidate_hash
from harness.task_control import (
    evidence_contract_fingerprint,
    load_task_state,
    mark_phase_result,
    validate_task_plan,
    write_task_state,
)
from harness.tools.lead_tools import build_lead_agent_tool_specs, execute_lead_tool
from harness.utils import RunLogger
from runtime_config import (
    HarnessConfig,
    ModelConfig,
    PlanValidatorConfig,
    RuntimeConfig,
)


def _accepted_plan():
    """A legacy-shaped plan: the only declared source lives in prose.

    Phases like this are why the extension exists.  With no source_url field,
    evidence_contract_fingerprint falls back to extracting the primary URL from
    worker_task/objective/context, so a replan that merely rewords the
    objective can move which URL comes first and retire validated evidence.
    """

    return {
        "goal": "collect reviews",
        "task_type": "web_scrape",
        "phases": [{
            "id": "p1",
            "type": "browser_worker",
            "task_type": "web_scrape",
            "objective": "collect the reviews of the first item",
            "worker_task": "open https://example.com/item/a and collect reviews",
            "stage_hint": "collection",
            "stage_hint_reason": (
                "This phase collects structured review records from the "
                "declared source page before any downstream processing."
            ),
            "depends_on": [],
            "expected_artifact": {"name": "reviews_a", "fields": ["text"]},
            "validators": [{"type": "min_rows", "value": 1}],
            "max_attempts": 3,
        }],
    }


def _new_phase(phase_id="p2", depends_on=None):
    return {
        "id": phase_id,
        "type": "browser_worker",
        "task_type": "web_scrape",
        "objective": "collect the reviews of the second item",
        "worker_task": "open https://example.com/item/b and collect reviews",
        "stage_hint": "collection",
        "stage_hint_reason": (
            "This phase collects structured review records from the second "
            "declared source page requested by the resume instruction."
        ),
        "depends_on": [] if depends_on is None else depends_on,
        "expected_artifact": {"name": "reviews_b", "fields": ["text"]},
        "validators": [{"type": "min_rows", "value": 1}],
        "max_attempts": 3,
    }


class _ExtensionFixture:
    """A finished single-phase task, resumed with an instruction."""

    def __init__(
        self,
        root,
        *,
        instruction="also collect https://example.com/item/b",
        validator_enabled=False,
        initial_plan_recovered=True,
    ):
        normalized, errors = validate_task_plan(_accepted_plan())
        assert errors == [], errors
        assert normalized is not None
        self.plan = normalized
        self.logger = RunLogger(root, task_id="finished-task")
        self.artifact = self.logger.artifacts_dir / "reviews_a.json"
        self.artifact.write_text(
            '{"rows": [{"text": "good"}]}', encoding="utf-8",
        )
        write_task_state(self.logger, {
            "phases": {
                "p1": {
                    "status": "validated_done",
                    "attempts": [{
                        "workerId": "browser-1",
                        "status": "done",
                        "validation": {
                            "status": "done",
                            "artifacts": [str(self.artifact)],
                        },
                    }],
                    "validated_artifacts": [str(self.artifact)],
                },
            },
            "artifacts": [str(self.artifact)],
            "resumes": [{"at": "resume-start", "instruction": instruction}],
        })
        validator = (
            PlanValidatorConfig(enabled=True, model_id="auditor-model")
            if validator_enabled
            else PlanValidatorConfig()
        )
        runtime = RuntimeConfig(
            agent_id="test-agent",
            model=ModelConfig(provider="fake", model_id="lead-model"),
            browser=ABCPClientConfig(),
            harness=HarnessConfig(worktree_dir=root),
            plan_validator=validator,
        )
        resume = ResumeContext(
            original_user_task="collect the reviews of the first item",
            current_plan=normalized,
            initial_plan=normalized,
            initial_plan_recovered=initial_plan_recovered,
            instruction=instruction,
            run_id="resume-1",
        )
        self.agent = LeadAgent(
            object(),
            runtime,
            self.logger,
            resume=resume,
            plan_validator_provider=object() if validator_enabled else None,
        )
        self.agent.original_user_task = (
            "collect the reviews of the first item\n<resume_instruction>"
            f"{instruction}</resume_instruction>"
        )

    def extend(self, new_phases, reason="the user asked for one more item"):
        return asyncio.run(self.agent.extend_task_plan(new_phases, reason))


class ExtendTaskPlanTest(unittest.TestCase):
    def test_appended_phase_leaves_validated_evidence_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)

            result = fixture.extend([_new_phase()])

            self.assertEqual(result["status"], "done")
            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "validated_done")
            self.assertEqual(
                state["phases"]["p1"]["validated_artifacts"],
                [str(fixture.artifact)],
            )
            self.assertEqual(state["phases"]["p2"]["status"], "pending")
            self.assertEqual(state["artifacts"], [str(fixture.artifact)])
            self.assertEqual(state["current_phase"], "p2")
            self.assertEqual(result["resumeReconciliation"]["removedPhases"], [])
            self.assertEqual(
                result["resumeReconciliation"]["changedEvidencePhases"], [],
            )
            self.assertEqual(
                result["resumeReconciliation"]["invalidatedArtifacts"], [],
            )

    def test_prose_sourced_phase_keeps_its_evidence_fingerprint(self):
        """The regression the whole design exists to prevent.

        p1 declares no source_url, so its fingerprint is derived from the URL in
        its prose.  Appending a phase whose prose carries a different URL must
        not disturb it.
        """

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            before = evidence_contract_fingerprint(fixture.plan["phases"][0])

            result = fixture.extend([_new_phase()])

            self.assertEqual(result["status"], "done")
            written = json.loads(
                (fixture.logger.task_dir / "task_plan.json").read_text(
                    encoding="utf-8",
                )
            )
            accepted, appended = written["phases"]
            self.assertEqual(accepted["id"], "p1")
            self.assertEqual(evidence_contract_fingerprint(accepted), before)
            self.assertNotEqual(
                evidence_contract_fingerprint(appended), before,
            )

    def test_a_new_phase_may_depend_on_an_accepted_phase(self):
        """An appended phase can wait for, or read the artifact of, an old one.

        This is browser work that happens to have an upstream — not a merge
        phase.  Consolidating existing artifacts is lead_save_artifact's job,
        because every plan phase runs in a browser and nothing can move a
        merge-only phase out of pending.
        """

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)

            result = fixture.extend([
                _new_phase(),
                _new_phase("p3", depends_on=["p1", "p2"]),
            ])

            self.assertEqual(result["status"], "done")
            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "validated_done")
            self.assertEqual(state["phases"]["p3"]["status"], "pending")

    def test_extension_decision_is_readable_from_task_state_alone(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)

            fixture.extend([_new_phase()])
            fixture.extend([_new_phase("p3")], reason="and one more item")

            resumes = load_task_state(fixture.logger)["resumes"]
            decisions = resumes[-1]["extensionDecisions"]
            self.assertEqual(len(decisions), 2)
            self.assertEqual(
                [item["reason"] for item in decisions],
                ["the user asked for one more item", "and one more item"],
            )
            self.assertEqual(
                {item["baselineKind"] for item in decisions},
                {"current_plan_immutable_prefix"},
            )
            self.assertEqual(
                [item["planVersion"] for item in decisions], [1, 2],
            )

    def test_records_exactly_one_extend_decision_for_the_instruction(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)

            fixture.extend([_new_phase()])

            events = [
                json.loads(line)
                for line in (
                    fixture.logger.task_dir / "run.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            decisions = [
                event for event in events
                if event.get("type") == "resume.instruction.reviewed"
            ]
            self.assertEqual(len(decisions), 1)
            payload = decisions[0].get("payload") or decisions[0]
            self.assertEqual(payload["decision"], "extend")
            self.assertEqual(
                payload["baselineKind"], "current_plan_immutable_prefix",
            )
            self.assertFalse(fixture.agent._resume_instruction_pending)

    def test_rejects_a_new_phase_id_that_collides_with_an_accepted_one(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)

            result = fixture.extend([_new_phase("p1")])

            self.assertEqual(result["status"], "invalid_extension")
            self.assertEqual(result["conflictingPhaseIds"], ["p1"])
            self.assertFalse(result["tool_was_executed"])
            state = load_task_state(fixture.logger)
            self.assertEqual(len(state["phases"]), 1)

    def test_rejects_an_empty_extension(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)

            result = fixture.extend([])

            self.assertEqual(result["status"], "invalid_extension")
            self.assertFalse(result["tool_was_executed"])

    def test_requires_a_resumed_run_carrying_a_user_instruction(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root, instruction="")

            result = fixture.extend([_new_phase()])

            self.assertEqual(result["status"], "not_resumed")
            self.assertFalse(result["tool_was_executed"])
            state = load_task_state(fixture.logger)
            self.assertNotIn("p2", state["phases"])

    def test_stays_available_after_the_first_extension_of_the_run(self):
        """The instruction gate clearing must not push the Lead to replan.

        A general replan is the dangerous path; refusing the safe one once the
        gate has cleared would steer the model straight at it.
        """

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            self.assertEqual(fixture.extend([_new_phase()])["status"], "done")
            self.assertFalse(fixture.agent._resume_instruction_pending)

            second = fixture.extend([_new_phase("p3")])

            self.assertEqual(second["status"], "done")
            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "validated_done")
            self.assertEqual(state["phases"]["p2"]["status"], "pending")
            self.assertEqual(state["phases"]["p3"]["status"], "pending")


class ExtendTaskPlanReviewTest(unittest.TestCase):
    def test_an_unavailable_reviewer_blocks_the_extension(self):
        """Whether a new target is user-authorized has no mechanical answer.

        Acceptance normally lets a mechanically valid candidate through when the
        auditor is merely unavailable.  For an extension that would leave the
        added phases with nothing checking them, and a general replan remains
        available, so it fails closed instead.
        """

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root, validator_enabled=True)

            async def unavailable(*args, **kwargs):
                return {"status": "error", "errors": ["auditor is unreachable"]}

            with patch.object(agent_harness, "review_plan_revision", unavailable):
                result = fixture.extend([_new_phase()])

            self.assertEqual(result["status"], "failed")
            self.assertIn("approving independent plan review", result["error"])
            self.assertFalse(result["tool_was_executed"])
            state = load_task_state(fixture.logger)
            self.assertNotIn("p2", state["phases"])
            self.assertEqual(state["artifacts"], [str(fixture.artifact)])

    def test_an_unavailable_reviewer_does_not_point_at_a_general_replan(self):
        """The escape hatch would be worse than the thing it escapes.

        accept_task_plan accepts a mechanically valid replacement plan without
        review in exactly this state, so telling the Lead to replan here would
        hand it a way to rewrite the phases this refusal protects.
        """

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root, validator_enabled=True)

            async def unavailable(*args, **kwargs):
                return {"status": "error", "errors": ["auditor is unreachable"]}

            with patch.object(agent_harness, "review_plan_revision", unavailable):
                result = fixture.extend([_new_phase()])

            guidance = result["next_instruction"]
            self.assertNotIn("replan_reason", guidance)
            self.assertNotIn("revised plan", guidance)
            self.assertIn("blocker", guidance)

    def test_a_rejecting_reviewer_points_at_its_own_findings(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root, validator_enabled=True)

            async def rejects(*args, **kwargs):
                return {"status": "rejected", "errors": ["unauthorized source"]}

            with patch.object(agent_harness, "review_plan_revision", rejects):
                result = fixture.extend([_new_phase()])

            self.assertIn("findings", result["next_instruction"])

    def test_a_rejecting_reviewer_blocks_the_extension(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root, validator_enabled=True)

            async def rejects(*args, **kwargs):
                return {"status": "rejected", "errors": ["unauthorized source"]}

            with patch.object(agent_harness, "review_plan_revision", rejects):
                result = fixture.extend([_new_phase()])

            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["tool_was_executed"])
            state = load_task_state(fixture.logger)
            self.assertNotIn("p2", state["phases"])

    def test_an_approved_extension_proceeds(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root, validator_enabled=True)

            async def approves(*args, **kwargs):
                # A real reviewer reports the candidate it actually read, and
                # acceptance refuses a verdict bound to anything else.
                return {
                    "status": "approved",
                    "candidateHash": plan_candidate_hash(
                        kwargs["candidate_plan"], kwargs["replan_reason"],
                    ),
                }

            with patch.object(agent_harness, "review_plan_revision", approves):
                result = fixture.extend([_new_phase()])

            self.assertEqual(result["status"], "done")
            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "validated_done")
            self.assertEqual(state["phases"]["p2"]["status"], "pending")

    def test_a_missing_initial_plan_history_still_permits_an_extension(self):
        """The accepted plan is the extension's own immutable baseline.

        A general replan still fails closed here, because there the baseline is
        what bounds how far the model may move the contract.
        """

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(
                root, validator_enabled=True, initial_plan_recovered=False,
            )

            async def approves(*args, **kwargs):
                # A real reviewer reports the candidate it actually read, and
                # acceptance refuses a verdict bound to anything else.
                return {
                    "status": "approved",
                    "candidateHash": plan_candidate_hash(
                        kwargs["candidate_plan"], kwargs["replan_reason"],
                    ),
                }

            with patch.object(agent_harness, "review_plan_revision", approves):
                result = fixture.extend([_new_phase()])

            self.assertEqual(result["status"], "done")
            events = [
                json.loads(line)
                for line in (
                    fixture.logger.task_dir / "run.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            decision = [
                event for event in events
                if event.get("type") == "resume.instruction.reviewed"
            ][-1]
            payload = decision.get("payload") or decision
            self.assertFalse(payload["initialPlanRecovered"])

            blocked = asyncio.run(
                fixture.agent.review_task_plan_candidate(fixture.plan)
            )
            self.assertEqual(blocked["status"], "error")


class ExtendAcceptanceInvariantTest(unittest.TestCase):
    """Guards on accept_task_plan itself, below the tool's own checks."""

    def test_a_candidate_that_would_retire_evidence_is_not_written(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            damaged = json.loads(json.dumps(fixture.plan))
            damaged["phases"][0]["validators"] = [{"type": "min_rows", "value": 9}]
            damaged["phases"].append(_new_phase())
            damaged["replan_reason"] = "smuggled contract change"

            result = fixture.agent.accept_task_plan(
                damaged, resume_decision="extend",
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["changedEvidencePhases"], ["p1"])
            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "validated_done")
            self.assertEqual(state["artifacts"], [str(fixture.artifact)])
            self.assertNotIn("p2", state["phases"])

    def test_a_candidate_that_drops_an_accepted_phase_is_not_written(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            replaced = json.loads(json.dumps(fixture.plan))
            replaced["phases"] = [_new_phase()]
            replaced["replan_reason"] = "smuggled removal"

            result = fixture.agent.accept_task_plan(
                replaced, resume_decision="extend",
            )

            self.assertEqual(result["status"], "failed")
            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "validated_done")

    def test_reordering_accepted_phases_is_rejected(self):
        """Fingerprints are compared per id and would not notice this.

        Reordering rewrites the meaning of every omitted depends_on, so phase
        order is checked directly.
        """

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            reordered = json.loads(json.dumps(fixture.plan))
            reordered["phases"] = [_new_phase()] + reordered["phases"]
            reordered["replan_reason"] = "prepended instead of appended"

            result = fixture.agent.accept_task_plan(
                reordered, resume_decision="extend",
            )

            self.assertEqual(result["status"], "failed")
            self.assertIn("phase order", result["error"])
            self.assertEqual(result["acceptedPhaseIds"], ["p1"])
            state = load_task_state(fixture.logger)
            self.assertNotIn("p2", state["phases"])

    def test_a_general_replan_still_retires_changed_evidence(self):
        """The extension guard must not have disarmed ordinary replans."""

        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            revised = json.loads(json.dumps(fixture.plan))
            revised["phases"][0]["validators"] = [{"type": "min_rows", "value": 2}]
            revised["replan_reason"] = "the user strengthened the row contract"

            result = fixture.agent.accept_task_plan(revised)

            self.assertEqual(result["status"], "done")
            self.assertEqual(
                result["resumeReconciliation"]["changedEvidencePhases"], ["p1"],
            )
            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "pending")
            self.assertEqual(state["artifacts"], [])


class ExtendedTaskCompletionTest(unittest.TestCase):
    """The whole point, end to end: finish the appended work and deliver both."""

    def test_appended_phase_completes_and_both_artifacts_reach_the_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            self.assertEqual(fixture.extend([_new_phase()])["status"], "done")
            accepted = json.loads(
                (fixture.logger.task_dir / "task_plan.json").read_text(
                    encoding="utf-8",
                )
            )
            appended = fixture.logger.artifacts_dir / "reviews_b.json"
            appended.write_text('{"rows": [{"text": "fine"}]}', encoding="utf-8")

            mark_phase_result(
                fixture.logger,
                phase_id="p2",
                worker_id="browser-2",
                validation={"status": "done", "artifacts": [str(appended)]},
                result_status=WORKER_STATUS_DONE,
                phase=accepted["phases"][1],
            )

            state = load_task_state(fixture.logger)
            self.assertEqual(state["phases"]["p1"]["status"], "validated_done")
            self.assertEqual(state["phases"]["p2"]["status"], "validated_done")
            self.assertEqual(
                state["artifacts"], [str(fixture.artifact), str(appended)],
            )
            receipt = build_completion_receipt(state=state, spawner=None)
            self.assertEqual(receipt["artifact"]["validatedArtifacts"], 2)
            self.assertEqual(receipt["artifact"]["validatedPhases"], 2)
            self.assertEqual(receipt["artifact"]["validatedRows"], 2)
            self.assertEqual(
                terminal_consistency_contradictions(
                    state=state, plan=accepted, final_status="done",
                ),
                [],
            )

    def test_a_pending_appended_phase_still_blocks_a_done_claim(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = _ExtensionFixture(root)
            self.assertEqual(fixture.extend([_new_phase()])["status"], "done")
            accepted = json.loads(
                (fixture.logger.task_dir / "task_plan.json").read_text(
                    encoding="utf-8",
                )
            )

            contradictions = terminal_consistency_contradictions(
                state=load_task_state(fixture.logger),
                plan=accepted,
                final_status="done",
            )

            self.assertEqual(
                [item["phaseId"] for item in contradictions], ["p2"],
            )


class ExtendToolExposureTest(unittest.TestCase):
    def test_hidden_from_runs_that_are_not_resuming(self):
        normal = {
            item["name"] for item in build_lead_agent_tool_specs(include_resume=False)
        }
        resumed = {
            item["name"] for item in build_lead_agent_tool_specs(include_resume=True)
        }
        self.assertNotIn("extend_task_plan", normal)
        self.assertIn("extend_task_plan", resumed)

    def test_schema_accepts_only_the_added_phases(self):
        spec = next(
            item for item in build_lead_agent_tool_specs(include_resume=True)
            if item["name"] == "extend_task_plan"
        )
        schema = spec.get("input_schema") or spec.get("parameters")
        self.assertEqual(
            sorted(schema["properties"]), ["new_phases", "replan_reason"],
        )
        self.assertFalse(schema["additionalProperties"])

    def test_the_instruction_gate_names_the_extension_path(self):
        agent = SimpleNamespace(
            resume=SimpleNamespace(run_id="resume-1"),
            _resume_instruction_pending=True,
            _current_step=1,
            recent_tool_signatures=[],
            logger=SimpleNamespace(write=lambda *args, **kwargs: None),
        )

        blocked, _ = asyncio.run(execute_lead_tool(agent, {
            "name": "spawn_browser_agent",
            "input": {"phase_id": "p1"},
        }))

        self.assertEqual(blocked["status"], "resume_instruction_review_required")
        self.assertIn("extend_task_plan", blocked["next_instruction"])


if __name__ == "__main__":
    unittest.main()
