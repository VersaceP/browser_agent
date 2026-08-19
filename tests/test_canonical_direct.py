"""Canonical direct contract v2: trusted full-plan equivalence + guards.

v2 hardening (from external review of the v1 shadow prototype):
- contract embeds a COMPLETE plan; eligibility is one equivalence
  (normalized candidate == normalized contract.plan), so no field blacklist
  (objective/goal/stage_hint/session_key VALUES/methods/max_steps...) can
  drift out of coverage
- contract is bound to THIS run's original_user_task: a stale contract from
  a previous task can never grant eligibility
- side_effect_policy is a strict schema (declared_effects from a fixed
  vocabulary; empty objects and unknown fields rejected)
- the skip itself is feature-flagged OFF; when on, a persisted receipt locks
  spawn-time task/context/worker_contract overrides, and a stale receipt
  falls back to the normal spawn path
"""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_harness import LeadAgent  # noqa: E402
from harness.task_control.canonical_direct import (  # noqa: E402
    ALLOWED_SIDE_EFFECTS,
    ELIGIBLE,
    INELIGIBLE,
    UNREPLAYABLE,
    classify_canonical_direct_eligibility,
    contract_identity_hash,
    task_identity_hash,
    validate_canonical_direct_contract,
)
from harness.task_control.plan_validation import validate_task_plan  # noqa: E402
from harness.tools.lead_tools import _canonical_direct_spawn_guard  # noqa: E402
from harness.utils import RunLogger  # noqa: E402

USER_TASK = "在 https://example.com/form 填写教育经历：本科、广东工业大学、应用统计学，并提交"

TRUSTED_PLAN = {
    "version": "v1",
    "goal": "按用户任务填写教育表单并提交",
    "task_type": "form_filling",
    "phases": [{
        "id": "p1",
        "type": "browser_worker",
        "task_type": "form_filling",
        "objective": "填写教育经历三项并提交表单",
        "worker_task": USER_TASK,
        "stage_hint": "form_interaction",
        "expected_artifact": {
            "name": "form_receipt",
            "fields": ["educationLevel", "university", "major"],
        },
        "worker_contract": {
            "session_key": "yue-form",
            "allowed_methods": [
                "Page.create", "DOM.getAXTree", "Input.type",
                "Input.click", "Input.select",
            ],
        },
    }],
}

CONTRACT = {
    "version": "v2",
    "original_task": USER_TASK,
    "plan": copy.deepcopy(TRUSTED_PLAN),
    "side_effect_policy": {"declared_effects": ["navigation", "form_submission"]},
}

OTHER_TASK = "在 https://example.com/other 提取商品列表前 20 条的标题和价格"


def _normalized_plan(plan, user_task=USER_TASK):
    candidate, errors = validate_task_plan(
        copy.deepcopy(plan), user_task=user_task
    )
    assert candidate is not None, errors
    return candidate


def _validated_contract(contract=None):
    validated, errors = validate_canonical_direct_contract(
        copy.deepcopy(contract or CONTRACT)
    )
    assert validated is not None, errors
    return validated


class ContractValidationTests(unittest.TestCase):
    def test_valid_contract_normalizes_embedded_plan(self):
        validated = _validated_contract()
        self.assertIn("_normalized_plan", validated)
        self.assertEqual(len(validated["_normalized_plan"]["phases"]), 1)

    def test_invalid_embedded_plan_rejected(self):
        bad = copy.deepcopy(CONTRACT)
        bad["plan"]["phases"] = []
        _, errors = validate_canonical_direct_contract(bad)
        self.assertTrue(any("contract.plan invalid" in e for e in errors))

    def test_side_effect_policy_strict_schema(self):
        cases = [
            {},                                  # empty object
            {"declared_effects": []},            # empty list
            {"declared_effects": ["form_submission"], "note": "x"},  # unknown field
            {"declared_effects": ["hack_the_planet"]},  # unknown effect
        ]
        for policy in cases:
            bad = copy.deepcopy(CONTRACT)
            bad["side_effect_policy"] = policy
            _, errors = validate_canonical_direct_contract(bad)
            self.assertTrue(
                any("side_effect_policy" in e for e in errors), policy
            )

    def test_unknown_contract_fields_rejected(self):
        bad = copy.deepcopy(CONTRACT)
        bad["allowed_worker_contract_keys"] = ["session_key"]  # v1 leftover
        _, errors = validate_canonical_direct_contract(bad)
        self.assertTrue(any("unknown fields" in e for e in errors))

    def test_two_stage_validation_shape_then_normalize(self):
        from harness.task_control.canonical_direct import (
            normalize_contract,
            validate_contract_shape,
        )
        shaped, errors = validate_contract_shape(copy.deepcopy(CONTRACT))
        self.assertIsNotNone(shaped, errors)
        self.assertNotIn("_normalized_plan", shaped)  # init stage: no plan normalize
        normalized, errors = normalize_contract(shaped)
        self.assertIsNotNone(normalized, errors)
        self.assertIn("_normalized_plan", normalized)


class EligibilityClassifierTests(unittest.TestCase):
    """Synthetic positives + the full v2 counterexample set."""

    def _classify(self, plan, *, contract=None, user_task=USER_TASK, **kw):
        return classify_canonical_direct_eligibility(
            _normalized_plan(plan, user_task=user_task),
            contract=contract if contract is not None else _validated_contract(),
            original_user_task=user_task,
            **kw
        )

    def test_verbatim_trusted_plan_is_eligible(self):
        result = self._classify(TRUSTED_PLAN)
        self.assertEqual(result["eligibility"], ELIGIBLE, result["reasons"])

    def test_any_lead_edit_rejected_by_single_equivalence(self):
        mutations = {
            "goal": lambda p: p.update(goal="Lead 改写过的目标"),
            "objective": lambda p: p["phases"][0].update(
                objective="Lead 改写过的 objective"),
            "worker_task": lambda p: p["phases"][0].update(
                worker_task="Lead 自行改写的任务步骤"),
            "stage_hint": lambda p: p["phases"][0].update(
                stage_hint="collection"),
            "session_key_value": lambda p: p["phases"][0][
                "worker_contract"].update(session_key="lead-chosen-session"),
            "allowed_methods": lambda p: p["phases"][0][
                "worker_contract"].update(allowed_methods=["Page.create"]),
            "max_steps": lambda p: p["phases"][0].update(max_steps=5),
            "phase_id": lambda p: p["phases"][0].update(id="lead_id"),
            "task_type": lambda p: p["phases"][0].update(task_type="web_scrape"),
        }
        for name, mutate in mutations.items():
            plan = copy.deepcopy(TRUSTED_PLAN)
            mutate(plan)
            with self.subTest(mutation=name):
                result = self._classify(plan)
                self.assertEqual(result["eligibility"], INELIGIBLE, name)
                self.assertIn("plan_not_contract_plan", result["reasons"])

    def test_stale_contract_bound_to_other_task_rejected(self):
        # Contract written for USER_TASK, run's task is OTHER_TASK.
        result = classify_canonical_direct_eligibility(
            _normalized_contract_plan_for_other_task(),
            contract=_validated_contract(),
            original_user_task=OTHER_TASK,
        )
        self.assertIn("contract_task_mismatch", result["reasons"])

    def test_structural_rejections_still_reported(self):
        plan = copy.deepcopy(TRUSTED_PLAN)
        plan["phases"].append(dict(plan["phases"][0], id="p2"))
        result = self._classify(plan)
        self.assertIn("multi_phase", result["reasons"])
        self.assertIn("plan_not_contract_plan", result["reasons"])

    def test_no_contract_invalid_contract_runtime_state(self):
        base = _normalized_plan(TRUSTED_PLAN)
        no_contract = classify_canonical_direct_eligibility(
            base, contract=None, original_user_task=USER_TASK,
        )
        self.assertIn("no_canonical_contract", no_contract["reasons"])
        invalid = classify_canonical_direct_eligibility(
            base, contract={"version": "v1"}, original_user_task=USER_TASK,
        )
        self.assertIn("invalid_contract", invalid["reasons"])
        for flag, reason in (
            ("resume_active", "resume"),
            ("has_accepted_plan", "replan_not_initial_plan"),
            ("pending_hitl", "pending_hitl"),
        ):
            result = classify_canonical_direct_eligibility(
                base, contract=_validated_contract(),
                original_user_task=USER_TASK, runtime_state={flag: True},
            )
            self.assertIn(reason, result["reasons"], flag)

    def test_unreplayable_shape(self):
        result = classify_canonical_direct_eligibility(
            {"phases": "nope"}, contract=_validated_contract(),
            original_user_task=USER_TASK,
        )
        self.assertEqual(result["eligibility"], UNREPLAYABLE)


def _normalized_contract_plan_for_other_task():
    # A plan whose worker_task equals the contract's task text - eligible on
    # paper - evaluated against a run whose user task is different.
    return _normalized_plan(TRUSTED_PLAN)


class SkipAndReceiptTests(unittest.IsolatedAsyncioTestCase):
    """Flag-gated skip branch + persisted receipt + spawn override guard."""

    def _agent(self, *, contract=None, skip_enabled=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agent = LeadAgent.__new__(LeadAgent)
        agent.logger = RunLogger(tmp.name)
        agent.resume = None
        agent.original_user_task = USER_TASK
        agent.initial_task_plan = None
        agent.task_plan = None
        agent.plan_validator_provider = None
        agent.canonical_direct_contract = contract
        agent._canonical_direct_normalized = True  # contract fixtures arrive normalized
        agent.last_plan_validator_record = None
        agent.canonical_active_receipt = None
        agent.runtime = SimpleNamespace(
            plan_validator=SimpleNamespace(enabled=True),
            harness=SimpleNamespace(
                canonical_direct_skip_enabled=skip_enabled,
            ),
        )
        agent._schema_cache_status = lambda: (None, set())
        return agent

    def _events(self, agent, name):
        path = Path(agent.logger.task_dir) / "run.jsonl"
        return [
            json.loads(line) for line in open(path)
            if f'"{name}"' in line
        ]

    async def test_flag_off_never_skips_even_when_eligible(self):
        agent = self._agent(contract=_validated_contract(), skip_enabled=False)
        review = await agent.review_task_plan_candidate(
            copy.deepcopy(TRUSTED_PLAN)
        )
        # Eligible, but the reviewer flow still runs to completion.
        shadows = self._events(agent, "canonical_direct.shadow")
        self.assertEqual(len(shadows), 1)
        payload = shadows[0]["payload"]
        self.assertEqual(payload["eligibility"], ELIGIBLE)
        self.assertTrue(payload["reviewerFlowPreserved"])
        self.assertEqual(
            payload["originalTaskHash"], task_identity_hash(USER_TASK)
        )
        self.assertNotEqual(review.get("status"), "canonical_direct")
        self.assertEqual(len(self._events(agent, "canonical_direct.skip")), 0)

    async def test_flag_on_eligible_candidate_skips_with_receipt(self):
        agent = self._agent(contract=_validated_contract(), skip_enabled=True)
        review = await agent.review_task_plan_candidate(
            copy.deepcopy(TRUSTED_PLAN)
        )
        self.assertEqual(review["status"], "canonical_direct")
        self.assertTrue(review["candidateHash"])
        self.assertEqual(
            review["contractHash"],
            contract_identity_hash(agent.canonical_direct_contract),
        )
        self.assertEqual(
            review["originalTaskHash"], task_identity_hash(USER_TASK)
        )
        skips = self._events(agent, "canonical_direct.skip")
        self.assertEqual(len(skips), 1)
        self.assertFalse(skips[0]["payload"]["reviewerFlowPreserved"])

    async def test_flag_on_ineligible_candidate_still_reviews(self):
        agent = self._agent(contract=_validated_contract(), skip_enabled=True)
        plan = copy.deepcopy(TRUSTED_PLAN)
        plan["phases"][0]["worker_task"] = "Lead 改写"
        review = await agent.review_task_plan_candidate(plan)
        self.assertNotEqual(review.get("status"), "canonical_direct")
        self.assertEqual(len(self._events(agent, "canonical_direct.skip")), 0)

    def _canonical_accept(self, agent, *, with_receipt=True, with_persisted=True):
        """Simulate accepting a plan via the canonical path: the persisted
        validator record (task_plan_history) is the authorization fact; the
        sidecar receipt is diagnostic cache only."""
        agent.task_plan = _normalized_plan(TRUSTED_PLAN)
        record = {
            "status": "canonical_direct",
            "candidateHash": "x",
            "contractHash": contract_identity_hash(
                agent.canonical_direct_contract
            ),
            "originalTaskHash": task_identity_hash(USER_TASK),
        }
        agent.last_plan_validator_record = dict(record)
        if with_persisted:
            history = Path(agent.logger.task_dir) / "task_plan_history"
            history.mkdir(parents=True, exist_ok=True)
            (history / "plan.0001.json").write_text(json.dumps({
                "plan": TRUSTED_PLAN,
                "validatorReview": record,
            }), encoding="utf-8")
        if with_receipt:
            agent.canonical_active_receipt = dict(record)
            (Path(agent.logger.task_dir)
             / "canonical_direct.receipt.json").write_text(
                json.dumps(record), encoding="utf-8",
            )

    def test_spawn_guard_rejects_all_execution_routing_overrides(self):
        agent = self._agent(contract=_validated_contract())
        self._canonical_accept(agent, with_receipt=True)
        for field in (
            "task", "context", "worker_contract", "max_steps",
            "result_contract", "preferred_slot_id", "reuse_from_worker_id",
            "reuse_scope", "session_key", "fleet_id", "page_policy",
        ):
            guard = _canonical_direct_spawn_guard(agent, {field: "x"})
            self.assertIsNotNone(guard, field)
            self.assertEqual(guard["status"], "canonical_plan_lock", field)
        # phase_id / name only -> passes through.
        self.assertIsNone(_canonical_direct_spawn_guard(agent, {
            "phase_id": "p1", "name": "w1",
        }))

    def test_canonical_plan_sidecar_missing_still_locked_after_restart(self):
        # Restart/resume: memory record GONE, sidecar GONE - the persisted
        # validator record alone must keep the plan locked (it is the
        # authorization source, not the sidecar).
        agent = self._agent(contract=_validated_contract())
        self._canonical_accept(
            agent, with_receipt=False, with_persisted=True
        )
        agent.last_plan_validator_record = None  # restart wiped memory
        guard = _canonical_direct_spawn_guard(agent, {"task": "改写"})
        self.assertIsNotNone(guard)
        self.assertEqual(guard["status"], "canonical_plan_lock")

    def test_memory_canonical_without_persisted_record_fails_closed(self):
        # History lost/truncated while memory still says canonical:
        # divergence is fail-closed, not fail-open.
        agent = self._agent(contract=_validated_contract())
        self._canonical_accept(
            agent, with_receipt=True, with_persisted=False
        )
        guard = _canonical_direct_spawn_guard(agent, {"phase_id": "p1"})
        self.assertEqual(guard["status"], "canonical_receipt_required")
        self.assertEqual(
            len(self._events(agent, "canonical_direct.record_divergence")), 1
        )

    def test_persisted_contract_hash_mismatch_fails_closed(self):
        # Operator reconfigured the contract after canonical acceptance:
        # binding cannot be re-established -> fail closed.
        agent = self._agent(contract=_validated_contract())
        self._canonical_accept(agent)
        new_contract = _validated_contract()
        new_contract["plan"]["goal"] = "另一个目标"
        normalized_new, _ = validate_canonical_direct_contract(new_contract)
        agent.canonical_direct_contract = normalized_new
        guard = _canonical_direct_spawn_guard(agent, {"phase_id": "p1"})
        self.assertEqual(guard["status"], "canonical_receipt_required")
        self.assertEqual(
            len(self._events(agent, "canonical_direct.receipt_invalid")), 1
        )

    def test_normal_plan_with_stale_receipt_ignores_it(self):
        agent = self._agent(contract=_validated_contract())
        agent.task_plan = _normalized_plan(TRUSTED_PLAN)
        agent.last_plan_validator_record = {"status": "approved"}
        (Path(agent.logger.task_dir)
         / "canonical_direct.receipt.json").write_text(
            json.dumps({"status": "canonical_direct", "contractHash": "x"}),
            encoding="utf-8",
        )
        # No overrides guard and no blocking: normal spawn path.
        self.assertIsNone(_canonical_direct_spawn_guard(agent, {
            "task": "Lead 自由任务",
        }))
        self.assertEqual(
            len(self._events(agent, "canonical_direct.spawn_guard_stale")), 1
        )

    def test_spawn_shadow_records_execution_deviations_non_blocking(self):
        from harness.tools.lead_tools import _canonical_spawn_shadow
        agent = self._agent(contract=_validated_contract())
        _canonical_spawn_shadow(agent, {
            "task": "Lead 的版本", "session_key": "s9", "phase_id": "p1",
        })
        events = self._events(agent, "canonical_direct.spawn_shadow")
        self.assertEqual(len(events), 1)
        self.assertEqual(
            sorted(events[0]["payload"]["fields"]), ["session_key", "task"]
        )
        # No deviations -> no event.
        _canonical_spawn_shadow(agent, {"phase_id": "p1"})
        self.assertEqual(
            len(self._events(agent, "canonical_direct.spawn_shadow")), 1
        )
    async def test_contract_shadow_fires_even_for_l1_invalid_candidate(self):
        agent = self._agent(contract=_validated_contract())
        review = await agent.review_task_plan_candidate({"goal": ""})
        self.assertEqual(review.get("status"), "mechanical_invalid")
        shadows = self._events(agent, "canonical_direct.contract_shadow")
        self.assertEqual(len(shadows), 1)  # independent of candidate validity

    async def test_contract_shadow_replan_must_be_ineligible(self):
        agent = self._agent(contract=_validated_contract())
        agent.task_plan = _normalized_plan(TRUSTED_PLAN)  # replan context
        await agent.review_task_plan_candidate(copy.deepcopy(TRUSTED_PLAN))
        shadows = self._events(agent, "canonical_direct.contract_shadow")
        self.assertEqual(shadows[-1]["payload"]["directAdoption"], INELIGIBLE)
        self.assertIn(
            "replan_not_initial_plan", shadows[-1]["payload"]["reasons"]
        )

    async def test_contract_shadow_resume_must_be_ineligible(self):
        agent = self._agent(contract=_validated_contract())
        agent.resume = SimpleNamespace(
            initial_plan_recovered=True,
        )  # resume path that passes the history gate
        await agent.review_task_plan_candidate(copy.deepcopy(TRUSTED_PLAN))
        shadows = self._events(agent, "canonical_direct.contract_shadow")
        self.assertEqual(shadows[-1]["payload"]["directAdoption"], INELIGIBLE)
        self.assertIn("resume", shadows[-1]["payload"]["reasons"])

    async def test_contract_shadow_fires_with_validator_disabled(self):
        agent = self._agent(contract=_validated_contract())
        agent.runtime.plan_validator.enabled = False
        agent.plan_validator_provider = object()  # must never be called
        review = await agent.review_task_plan_candidate(
            copy.deepcopy(TRUSTED_PLAN)
        )
        self.assertEqual(review.get("status"), "disabled")
        # BOTH audit halves must exist on reviewer-less deployments: the
        # contract itself (adoptable?) AND the current valid candidate
        # (identical to the trusted plan?).
        contract_shadows = self._events(
            agent, "canonical_direct.contract_shadow"
        )
        candidate_shadows = self._events(agent, "canonical_direct.shadow")
        self.assertEqual(len(contract_shadows), 1)
        self.assertEqual(len(candidate_shadows), 1)
        self.assertTrue(candidate_shadows[0]["payload"]["reviewerFlowPreserved"])
        # The L1-invalid candidate still yields the contract half only
        # (no valid candidate to compare).
        agent2 = self._agent(contract=_validated_contract())
        agent2.runtime.plan_validator.enabled = False
        review2 = await agent2.review_task_plan_candidate({"goal": ""})
        self.assertEqual(review2.get("status"), "mechanical_invalid")
        self.assertEqual(
            len(self._events(agent2, "canonical_direct.contract_shadow")), 1
        )
        self.assertEqual(
            len(self._events(agent2, "canonical_direct.shadow")), 0
        )

    def test_real_accept_roundtrip_keeps_guard_locked_after_restart(self):
        """Production write path: real accept_task_plan() persists the plan
        version + validatorReview; a FRESH agent (memory wiped, plan reloaded
        from disk) must stay locked with no sidecar receipt."""
        from harness.planning.validator import plan_candidate_hash

        agent = self._agent(contract=_validated_contract())
        normalized = _normalized_plan(TRUSTED_PLAN)
        review = {
            "status": "canonical_direct",
            "candidateHash": plan_candidate_hash(normalized, ""),
            "contractHash": contract_identity_hash(
                agent.canonical_direct_contract
            ),
            "originalTaskHash": task_identity_hash(USER_TASK),
        }
        result = agent.accept_task_plan(
            copy.deepcopy(TRUSTED_PLAN), plan_validator_review=review,
        )
        self.assertEqual(result.get("status"), "done", result)

        # Fresh process: memory empty, plan reloaded from the persisted
        # task_plan.json, sidecar ignored.
        fresh = self._agent(contract=_validated_contract())
        fresh.logger = agent.logger  # same worktree
        fresh.last_plan_validator_record = None
        fresh.canonical_active_receipt = None
        fresh.task_plan = json.loads(
            (Path(agent.logger.task_dir) / "task_plan.json").read_text(
                encoding="utf-8"
            )
        )
        guard = _canonical_direct_spawn_guard(fresh, {"task": "改写"})
        self.assertIsNotNone(guard)
        self.assertEqual(guard["status"], "canonical_plan_lock")
        persisted = json.loads(
            sorted(
                (Path(agent.logger.task_dir) / "task_plan_history")
                .glob("plan.*.json")
            )[-1].read_text(encoding="utf-8")
        )["validatorReview"]
        self.assertEqual(persisted["status"], "canonical_direct")
        self.assertEqual(
            persisted["contractHash"], review["contractHash"]
        )
        self.assertEqual(
            persisted["originalTaskHash"], review["originalTaskHash"]
        )

class ConfigLoadingTests(unittest.TestCase):
    """The REAL config entry, not SimpleNamespace stubs (regression: both
    fields were once added to the dataclass but never loaded by from_dict,
    so no config could ever enable the contract)."""

    def test_from_dict_loads_contract_path(self):
        from runtime_config import HarnessConfig
        cfg = HarnessConfig.from_dict({
            "canonical_direct_contract_path": "/tmp/contract.json",
        })
        self.assertEqual(
            cfg.canonical_direct_contract_path, "/tmp/contract.json"
        )
        self.assertFalse(cfg.canonical_direct_skip_enabled)

    def test_from_dict_rejects_bad_path_values(self):
        from runtime_config import HarnessConfig
        for bad in ("", "   ", 5, []):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    HarnessConfig.from_dict({
                        "canonical_direct_contract_path": bad,
                    })

    def test_skip_flag_strict_boolean_and_not_ready(self):
        from runtime_config import HarnessConfig
        for bad in ("false", "true", 1, 0, None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    HarnessConfig.from_dict({
                        "canonical_direct_skip_enabled": bad,
                    })
        with self.assertRaises(ValueError) as raised:
            HarnessConfig.from_dict({"canonical_direct_skip_enabled": True})
        # Explicit not-ready refusal, not a silent half-enabled path.
        self.assertIn("not ready", str(raised.exception))

    def test_load_runtime_config_end_to_end(self):
        import tempfile as _tf
        from runtime_config import load_runtime_config
        with _tf.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            contract_path = str(Path(tmp) / "c.json")
            cfg_path.write_text(json.dumps({
                "provider": "anthropic",
                "model_id": "test-model",
                "api_key": "k",
                "harness": {
                    "canonical_direct_contract_path": contract_path,
                },
            }), encoding="utf-8")
            cfg = load_runtime_config(str(cfg_path), warn=False)
            self.assertEqual(
                cfg.harness.canonical_direct_contract_path, contract_path,
            )
            self.assertFalse(cfg.harness.canonical_direct_skip_enabled)

class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    """Phase-1 direct adoption: the harness itself accepts contract.plan and
    dispatches the first phase - no Lead emit round, no Plan Validator call.
    Every unmet condition is a structured fallback to the normal flow."""

    def _agent(self, *, contract=None, adoption_enabled=True,
               task=USER_TASK, resume=None, task_plan=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agent = LeadAgent.__new__(LeadAgent)
        agent.logger = RunLogger(tmp.name)
        agent.resume = resume
        agent.original_user_task = task
        agent.initial_task_plan = None
        agent.task_plan = task_plan
        agent.plan_validator_provider = None  # must stay untouched
        agent.canonical_direct_contract = contract
        agent._canonical_direct_normalized = True
        agent.last_plan_validator_record = None
        agent.canonical_active_receipt = None
        agent.runtime = SimpleNamespace(
            plan_validator=SimpleNamespace(enabled=True),
            agent_id="adoption-test-agent",
            harness=SimpleNamespace(
                canonical_direct_skip_enabled=False,
                canonical_direct_adoption_enabled=adoption_enabled,
                tool_result_offload_threshold_bytes=10_000_000,
                max_observation_chars=100_000,
            ),
        )
        agent._schema_cache_status = lambda: (None, set())
        return agent

    def _events(self, agent, name):
        path = Path(agent.logger.task_dir) / "run.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line) for line in open(path)
            if f'"{name}"' in line
        ]

    async def test_flag_off_returns_none_with_no_events(self):
        agent = self._agent(
            contract=_validated_contract(), adoption_enabled=False,
        )
        self.assertIsNone(agent._maybe_adopt_canonical_direct_plan())
        self.assertEqual(
            len(self._events(agent, "canonical_direct.fallback")), 0
        )
        self.assertIsNone(agent.task_plan)

    async def test_adoption_happy_path_persists_full_binding(self):
        contract = _validated_contract()
        agent = self._agent(contract=contract)
        # A real spy: any touch of the provider fails the test - this
        # proves adoption never consults the reviewer, not that a stub
        # attribute happened to stay None.
        class _SpyProvider:
            def __getattr__(self, name):
                raise AssertionError(
                    f"plan validator provider was touched: {name!r}"
                )

        agent.plan_validator_provider = _SpyProvider()
        adoption = agent._maybe_adopt_canonical_direct_plan()
        self.assertIsNotNone(adoption)
        self.assertEqual(adoption["phase_id"], "p1")
        # Plan accepted: task_plan IS the contract's normalized plan.
        self.assertEqual(agent.task_plan, contract["_normalized_plan"])
        # Adoption event carries the full three-hash binding.
        events = self._events(agent, "canonical_direct.adoption")
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(payload["phaseId"], "p1")
        self.assertEqual(
            payload["contractHash"], contract_identity_hash(contract)
        )
        self.assertEqual(
            payload["originalTaskHash"], task_identity_hash(USER_TASK)
        )
        # No reviewer events of any family were written.
        all_events = [
            json.loads(line)
            for line in open(
                Path(agent.logger.task_dir) / "run.jsonl"
            )
        ]
        self.assertEqual(
            [e for e in all_events if e["type"].startswith("plan_validator")],
            [],
        )
        self.assertEqual(len(self._events(agent, "canonical_direct.fallback")), 0)
        # Restart-safe: the persisted validator record re-establishes the
        # authorization (memory record + plan history agree).
        persisted = json.loads(
            sorted(
                (Path(agent.logger.task_dir) / "task_plan_history")
                .glob("plan.*.json")
            )[-1].read_text(encoding="utf-8")
        )["validatorReview"]
        self.assertEqual(persisted["status"], "canonical_direct")
        self.assertEqual(
            persisted["contractHash"], adoption["record"]["contractHash"]
        )
        self.assertEqual(
            persisted["originalTaskHash"],
            adoption["record"]["originalTaskHash"],
        )
        # And the spawn guard is therefore locked (overrided rejected).
        guard = _canonical_direct_spawn_guard(agent, {"task": "改写"})
        self.assertEqual(guard["status"], "canonical_plan_lock")

    async def test_fallback_task_mismatch(self):
        agent = self._agent(
            contract=_validated_contract(),
            task="另一个完全不同的任务",
        )
        self.assertIsNone(agent._maybe_adopt_canonical_direct_plan())
        fallbacks = self._events(agent, "canonical_direct.fallback")
        self.assertEqual(fallbacks[0]["payload"]["reason"], "task_mismatch")
        self.assertIsNone(agent.task_plan)

    async def test_fallback_resume_active(self):
        agent = self._agent(
            contract=_validated_contract(),
            resume=SimpleNamespace(instruction="继续", original_user_task=USER_TASK),
        )
        self.assertIsNone(agent._maybe_adopt_canonical_direct_plan())
        self.assertEqual(
            self._events(agent, "canonical_direct.fallback")[0]
            ["payload"]["reason"],
            "resume_active",
        )

    async def test_fallback_not_fresh_task(self):
        agent = self._agent(
            contract=_validated_contract(),
            task_plan=_normalized_plan(TRUSTED_PLAN),
        )
        self.assertIsNone(agent._maybe_adopt_canonical_direct_plan())
        self.assertEqual(
            self._events(agent, "canonical_direct.fallback")[0]
            ["payload"]["reason"],
            "not_fresh_task",
        )

    async def test_fallback_no_contract(self):
        agent = self._agent(contract=None)
        self.assertIsNone(agent._maybe_adopt_canonical_direct_plan())
        self.assertEqual(
            self._events(agent, "canonical_direct.fallback")[0]
            ["payload"]["reason"],
            "no_contract",
        )

    async def test_fallback_ineligible_multi_phase_contract(self):
        two_phase = copy.deepcopy(CONTRACT)
        second = copy.deepcopy(two_phase["plan"]["phases"][0])
        second["id"] = "p2"
        two_phase["plan"]["phases"].append(second)
        agent = self._agent(contract=_validated_contract(two_phase))
        self.assertIsNone(agent._maybe_adopt_canonical_direct_plan())
        payload = self._events(agent, "canonical_direct.fallback")[0]["payload"]
        self.assertEqual(payload["reason"], "ineligible")
        self.assertIn("multi_phase", payload["reasons"])

    async def test_dispatch_synthesizes_phase_id_only_spawn(self):
        agent = self._agent(contract=_validated_contract())
        adoption = agent._maybe_adopt_canonical_direct_plan()
        captured = {}

        async def fake_dispatch(tool_call):
            captured["tool_call"] = tool_call
            return {"status": "running", "workerId": "w-1"}, False

        messages = [{"role": "user", "content": "初始消息"}]
        status = await agent._dispatch_canonical_direct_phase(
            adoption, messages, fake_dispatch,
        )
        self.assertEqual(status, "running")
        # The synthetic spawn carries ONLY phase_id: every execution field
        # comes from the accepted contract plan verbatim.
        self.assertEqual(
            captured["tool_call"]["input"], {"phase_id": "p1"},
        )
        self.assertEqual(
            captured["tool_call"]["name"], "spawn_browser_agent",
        )
        # History mirrors a real Lead-issued spawn exactly.
        self.assertEqual(len(messages), 3)
        tool_use = messages[1]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["id"], captured["tool_call"]["id"])
        tool_result = messages[2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], tool_use["id"])
        self.assertIn("running", tool_result["content"])
        self.assertEqual(
            len(self._events(agent, "canonical_direct.dispatch")), 1,
        )
        self.assertEqual(
            len(self._events(agent, "canonical_direct.dispatch_failed")), 0,
        )

    async def test_dispatch_failure_records_event_and_status(self):
        agent = self._agent(contract=_validated_contract())
        adoption = agent._maybe_adopt_canonical_direct_plan()

        async def fake_dispatch(tool_call):
            return {
                "status": "spawn_infrastructure_exhausted",
                "error": "ABCPTransportError:unable to connect",
            }, False

        messages = [{"role": "user", "content": "初始消息"}]
        status = await agent._dispatch_canonical_direct_phase(
            adoption, messages, fake_dispatch,
        )
        self.assertEqual(status, "spawn_infrastructure_exhausted")
        failures = self._events(
            agent, "canonical_direct.dispatch_failed"
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(
            failures[0]["payload"]["spawnStatus"],
            "spawn_infrastructure_exhausted",
        )
        self.assertFalse(failures[0]["payload"]["shouldStop"])
        self.assertEqual(
            len(self._events(agent, "canonical_direct.dispatch")), 0,
        )
        # The receipt still entered the conversation - the Lead sees the
        # real failure instead of a fabricated success.
        self.assertEqual(len(messages), 3)
        self.assertIn("spawn_infrastructure_exhausted", messages[2]["content"][0]["content"])

    async def test_dispatch_records_should_stop_without_lying(self):
        agent = self._agent(contract=_validated_contract())
        adoption = agent._maybe_adopt_canonical_direct_plan()

        async def fake_dispatch(tool_call):
            return {"status": "failed"}, True

        status = await agent._dispatch_canonical_direct_phase(
            adoption, [ {"role": "user", "content": "初始消息"} ],
            fake_dispatch,
        )
        self.assertEqual(status, "failed")
        failures = self._events(
            agent, "canonical_direct.dispatch_failed"
        )
        self.assertTrue(failures[0]["payload"]["shouldStop"])

    def test_instruction_matches_dispatch_outcome(self):
        from agent_harness import _canonical_direct_instruction
        ok = _canonical_direct_instruction("running", "p1")
        self.assertIn("already been dispatched", ok)
        self.assertIn("Monitor the", ok)
        self.assertNotIn("decompose", ok)
        bad = _canonical_direct_instruction("spawn_infrastructure_exhausted", "p1")
        self.assertIn("did not start a worker", bad)
        self.assertIn("spawn_infrastructure_exhausted", bad)
        self.assertIn("phase_id ONLY", bad)
        self.assertNotIn("already been dispatched", bad)


class AdoptionConfigTests(unittest.TestCase):
    def test_adoption_flag_strict_boolean_default_off(self):
        from runtime_config import HarnessConfig
        self.assertFalse(HarnessConfig().canonical_direct_adoption_enabled)
        cfg = HarnessConfig.from_dict(
            {"canonical_direct_adoption_enabled": True}
        )
        self.assertTrue(cfg.canonical_direct_adoption_enabled)
        for bad in ("true", "false", 1, 0, None, []):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    HarnessConfig.from_dict({
                        "canonical_direct_adoption_enabled": bad,
                    })

    def test_skip_flag_still_rejected_when_adoption_exists(self):
        from runtime_config import HarnessConfig
        # The old skip flag keeps its not-ready rejection; direct adoption
        # is a separate control, not an unban of the emit-time skip.
        with self.assertRaises(ValueError):
            HarnessConfig.from_dict({
                "canonical_direct_skip_enabled": True,
                "canonical_direct_adoption_enabled": True,
            })

class AdoptionRunIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Full LeadAgent.run() wiring: adoption -> dispatch -> FIRST model call,
    with no plan_validator.* and no emit_task_plan before (or after) the
    dispatch, and an opening prompt that matches the dispatch outcome."""

    def _events(self, agent):
        path = Path(agent.logger.task_dir) / "run.jsonl"
        return [json.loads(line) for line in open(path)]

    @staticmethod
    def _async_noop():
        async def _noop(*args, **kwargs):
            return None
        return _noop

    async def test_run_orders_adoption_dispatch_before_first_model(self):
        import agent_harness as ah

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agent = LeadAgent.__new__(LeadAgent)
        agent.logger = RunLogger(tmp.name)
        agent.resume = None
        agent.task_plan = None
        agent.initial_task_plan = None
        agent.canonical_direct_contract = _validated_contract()
        agent._canonical_direct_normalized = True
        agent.last_plan_validator_record = None
        agent.canonical_active_receipt = None
        agent.plan_validator_provider = None
        agent.provider = SimpleNamespace(provider="p", model_id="m")
        agent.pinned_browser_context = None
        agent.strategy_bank = []
        agent.static_context_hash = "static-hash"
        agent._forced_compaction_reason = None
        agent.spawner = SimpleNamespace(
            shutdown=self._async_noop(),
        )
        agent.lifecycle = SimpleNamespace(
            agent_before_step=lambda ctx, meta: None,
        )
        agent._bootstrap_schema_cache = self._async_noop()
        agent._schema_cache_status = lambda: (None, set())
        agent._build_system_prompt = lambda: ""
        agent._observe_cache_pressure = lambda payload, **kw: None
        agent.runtime = SimpleNamespace(
            plan_validator=SimpleNamespace(enabled=True),
            agent_id="adoption-run-test",
            model=SimpleNamespace(provider="p", model_id="m"),
            harness=SimpleNamespace(
                canonical_direct_skip_enabled=False,
                canonical_direct_adoption_enabled=True,
                offload_threshold_bytes=1_000_000,
                tool_result_offload_threshold_bytes=10_000_000,
                max_observation_chars=100_000,
                max_browser_agent_instances=1,
                max_browser_agents=1,
                max_task_fleets=1,
                lead_max_steps=5,
                worker_max_steps=5,
                lead_model_timeout_step_retries=1,
                workflow_execution_enabled=False,
            ),
        )

        spawned_calls = []

        async def fake_dispatch(tool_call):
            if tool_call["name"] == "spawn_browser_agent":
                spawned_calls.append(tool_call)
                return {"status": "running", "workerId": "w-1"}, False
            if tool_call["name"] == "final_answer":
                return {
                    "status": "done",
                    "answer": "done",
                    "trigger": "lead_decided",
                    "completionReceipt": {},
                }, True
            raise AssertionError(
                f"unexpected tool call: {tool_call['name']}"
            )

        async def fake_generate(**kwargs):
            return (
                "",
                [{
                    "id": "t1",
                    "name": "final_answer",
                    "input": {"answer": "done"},
                }],
                "tool_calls",
                {},
            )

        originals = {
            name: getattr(ah, name)
            for name in (
                "build_lead_agent_tool_specs",
                "build_lead_tool_dispatcher",
                "compact_and_track_prefix_rebuild",
                "generate_response_surviving_moderation",
                "strategy_bank_index",
            )
        }

        def restore():
            for name, value in originals.items():
                setattr(ah, name, value)

        self.addCleanup(restore)
        ah.build_lead_agent_tool_specs = lambda **kw: []
        ah.build_lead_tool_dispatcher = lambda agent: fake_dispatch
        ah.compact_and_track_prefix_rebuild = (
            lambda *a, **kw: kw["messages"]
        )
        ah.generate_response_surviving_moderation = (
            lambda **kwargs: fake_generate(**kwargs)
        )
        ah.strategy_bank_index = lambda bank: "{}"

        answer = await agent.run(USER_TASK)

        self.assertEqual(answer, "done")
        events = self._events(agent)
        types = [e["type"] for e in events]
        # Ordering: adoption -> dispatch -> the FIRST lead model call.
        self.assertIn("canonical_direct.adoption", types)
        self.assertIn("canonical_direct.dispatch", types)
        self.assertIn("lead.model", types)
        self.assertLess(
            types.index("canonical_direct.adoption"),
            types.index("canonical_direct.dispatch"),
        )
        self.assertLess(
            types.index("canonical_direct.dispatch"),
            types.index("lead.model"),
        )
        # No reviewer event of any family, ever.
        self.assertEqual(
            [t for t in types if t.startswith("plan_validator")], [],
        )
        # No emit/replan/extend tool was ever dispatched.
        self.assertEqual(
            [c["name"] for c in spawned_calls], ["spawn_browser_agent"],
        )
        self.assertEqual(spawned_calls[0]["input"], {"phase_id": "p1"})
        # The dispatch actually failed-open nowhere: plan locked at accept.
        self.assertEqual(
            agent.last_plan_validator_record.get("status"),
            "canonical_direct",
        )


if __name__ == "__main__":
    unittest.main()
