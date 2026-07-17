import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from runtime_config import ABCPClientConfig
from runtime_config import HarnessConfig, RuntimeConfig

from harness.auth_fleet import (
    normalize_auth_verification_contract,
    verify_protected_auth_target,
)
from harness.fleet_coordinator import FleetCoordinator, FleetRoutingError
from harness.constants import (
    LEAD_FLEET_ROUTING_DECISION_CODES,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FLEET_ASSIGNMENT_LOST,
    WORKER_STATUS_SESSION_FLEET_LOST,
    WORKER_STATUS_UNKNOWN,
)
from harness.diagnostics import WorkerDiagnostics, classify_terminal_status
from harness.skill.heal import canary_validate
from harness.spawner import (
    BrowserAgentSlot,
    BrowserAgentSpawner,
    _effective_worker_status,
    _finalize_skill_execution_metadata,
    _prompt_worker_contract,
    _skill_execution_metadata,
    _unresolved_repair_visual_evidence,
    phase_result_status_for,
)
from harness.tools.browser_tools import (
    _assigned_fleet_lost_result,
    _apply_fleet_binding,
    _check_page_binding,
    _filter_page_list_response,
    _observe_page_binding_after,
    _recover_page_create_32005,
)
from harness.task_control import (
    initialize_task_state,
    load_task_state,
    mark_phase_running,
)
from harness.utils import RunLogger
from runtime_config import ModelConfig



def _make_spawner(testcase: unittest.TestCase) -> BrowserAgentSpawner:
    temp = tempfile.TemporaryDirectory()
    testcase.addCleanup(temp.cleanup)
    runtime = RuntimeConfig(
        agent_id="test-agent",
        model=ModelConfig(provider="test", model_id="test-model"),
        browser=ABCPClientConfig(),
        harness=HarnessConfig(
            max_browser_agent_instances=3,
            max_browser_agents=3,
            worktree_dir=temp.name,
        ),
    )
    logger = RunLogger(temp.name, task_id="slot-tests")
    return BrowserAgentSpawner(runtime, logger, lambda *args: None)


def _slot_context_payload(context: str) -> dict:
    body = context.split("<slot_context>", 1)[1].split("</slot_context>", 1)[0]
    return json.loads(body)


class BrowserAgentSlotTests(unittest.TestCase):
    def test_auth_verification_uses_origin_and_path_boundaries(self) -> None:
        contract = {
            "protected_url_prefixes": ["https://bank.com/account"],
            "authenticated_markers": [
                {"role": "button", "name": "Sign out"}
            ],
        }
        tree = '0 [1:2:3] button "Sign out" # @0,0,100,30'
        for hostile_url in (
            "https://bank.com.evil.test/account",
            "https://bank.com/accounting",
            "https://bank.com/Account",
        ):
            receipt = verify_protected_auth_target({
                "verificationContract": contract,
                "url": hostile_url,
                "axTreeText": tree,
            })
            self.assertFalse(receipt["verified"], hostile_url)
            self.assertEqual(receipt["reason"], "protected_url_not_reached")

        accepted = verify_protected_auth_target({
            "verificationContract": contract,
            "url": "https://BANK.com:443/account/settings?tab=security",
            "axTreeText": tree,
        })
        self.assertTrue(accepted["verified"])

    def test_auth_verification_requires_exact_visible_ax_node(self) -> None:
        contract = {
            "protected_url_prefixes": ["https://example.com/account"],
            "authenticated_markers": [
                {"role": "button", "name": "Sign out", "match": "exact"}
            ],
        }
        base = {
            "verificationContract": contract,
            "url": "https://example.com/account",
        }
        for tree in (
            '0 [1:2:3] heading "Sign out"',
            '0 [1:2:3] button "Sign out now" #',
            '0 [1:2:3] button "Sign out" hidden #',
            '0 [1:2:3] button "Sign out" blocked #',
            "ordinary page text containing Sign out",
        ):
            receipt = verify_protected_auth_target({**base, "axTreeText": tree})
            self.assertFalse(receipt["verified"], tree)
            self.assertEqual(
                receipt["reason"], "authenticated_marker_not_observed"
            )

        accepted = verify_protected_auth_target({
            **base,
            "axTreeText": '0 [1:2:3] BUTTON "  Sign   out  " #',
        })
        self.assertTrue(accepted["verified"])

    def test_auth_verification_rejects_legacy_string_markers(self) -> None:
        with self.assertRaisesRegex(ValueError, "entries must be objects"):
            normalize_auth_verification_contract({
                "protected_url_prefixes": ["https://example.com/account"],
                "authenticated_markers": ["Sign out"],
            })

    def test_lead_prompt_covers_declared_fleet_routing_codes(self) -> None:
        from agent_harness import LeadAgent

        agent = object.__new__(LeadAgent)
        agent.strategy_bank = {}
        agent.static_context_block = ""
        prompt = agent._build_system_prompt()
        for code in LEAD_FLEET_ROUTING_DECISION_CODES:
            self.assertIn(code, prompt)
        self.assertIn("reasonKind=fleet_auth_resolver_required", prompt)

    def test_new_session_bootstraps_fresh_and_lost_binding_is_retained(self) -> None:
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-a", "fleet-b"],
        )
        first = coordinator.choose_existing(
            worker_id="browser-001",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-a"],
            reuse_scope="fleet",
            page_policy="new",
            session_key="account:primary",
        )
        self.assertIsNone(first)
        first = coordinator.bind_assignment(
            worker_id="browser-001",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-a",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="account:primary",
        )
        self.assertEqual(first.fleet_id, "fleet-a")
        self.assertEqual(
            coordinator.preferred_slot_for_session("account:primary"),
            "slot-001",
        )

        second = coordinator.choose_existing(
            worker_id="browser-002",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-a", "fleet-b"],
            reuse_scope="fleet",
            page_policy="new",
            session_key="account:primary",
        )
        self.assertEqual(second.fleet_id, "fleet-a")
        self.assertEqual(second.assignment_reason, "session_key")
        self.assertEqual(second.allowed_fleet_ids, ("fleet-a",))

        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-b"],
        )
        self.assertEqual(
            coordinator.preferred_slot_for_session("account:primary"),
            "slot-001",
        )
        with self.assertRaises(FleetRoutingError) as caught:
            coordinator.choose_existing(
                worker_id="browser-003",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                candidate_fleet_ids=["fleet-b"],
                reuse_scope="fleet",
                page_policy="new",
                session_key="account:primary",
            )
        self.assertEqual(caught.exception.code, "session_fleet_lost")
        self.assertFalse(caught.exception.retryable)

    def test_isolated_and_named_fleets_never_become_generic_default(self) -> None:
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-shared", "fleet-isolated", "fleet-named"],
        )
        coordinator.bind_assignment(
            worker_id="shared",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-shared",
            assignment_reason="slot_default",
            reuse_scope="connection",
            page_policy="new",
        )
        coordinator.bind_assignment(
            worker_id="isolated",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-isolated",
            assignment_reason="isolated_session",
            reuse_scope="connection",
            page_policy="new",
            is_isolated=True,
        )
        coordinator.bind_assignment(
            worker_id="named",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-named",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )

        generic = coordinator.choose_existing(
            worker_id="generic",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-shared", "fleet-isolated", "fleet-named"],
            reuse_scope="connection",
            page_policy="new",
        )
        self.assertEqual(generic.fleet_id, "fleet-shared")
        routing = {item["fleetId"]: item for item in coordinator.slot_snapshot("slot-001")}
        self.assertTrue(routing["fleet-shared"]["isDefault"])
        self.assertFalse(routing["fleet-isolated"]["isDefault"])
        self.assertTrue(routing["fleet-isolated"]["isIsolated"])
        self.assertFalse(routing["fleet-named"]["isDefault"])

    def test_distinct_session_keys_cannot_collapse_or_rebind(self) -> None:
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-a", "fleet-b"],
        )
        coordinator.bind_assignment(
            worker_id="worker-a",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-a",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        self.assertIsNone(coordinator.choose_existing(
            worker_id="worker-b",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-a", "fleet-b"],
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:B",
        ))
        coordinator.bind_assignment(
            worker_id="worker-b",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-b",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:B",
        )
        with self.assertRaises(FleetRoutingError) as caught:
            coordinator.bind_assignment(
                worker_id="worker-b2",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id="fleet-a",
                assignment_reason="explicit",
                reuse_scope="fleet",
                page_policy="new",
                session_key="shop:B",
            )
        self.assertIn(caught.exception.code, {
            "session_binding_conflict", "fleet_session_conflict"
        })

    def test_session_release_is_generation_guarded_and_old_fleet_stays_quarantined(self) -> None:
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-auth"],
        )
        first = coordinator.bind_assignment(
            worker_id="worker-auth",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-auth",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        self.assertEqual(first.session_generation, 1)

        with self.assertRaises(FleetRoutingError):
            coordinator.release_session_binding(
                session_key="shop:A",
                expected_fleet_id="fleet-auth",
                expected_generation=2,
                reason="stale auth evidence",
            )

        released = coordinator.release_session_binding(
            session_key="shop:A",
            expected_fleet_id="fleet-auth",
            expected_generation=1,
            reason="verified re-authentication required",
        )
        self.assertEqual(released["nextGeneration"], 2)
        self.assertIsNone(coordinator.session_binding_details("shop:A"))

        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-auth", "fleet-new"],
        )
        self.assertIsNone(coordinator.choose_existing(
            worker_id="worker-new",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-auth", "fleet-new"],
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        ))
        second = coordinator.bind_assignment(
            worker_id="worker-new",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-new",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        self.assertEqual(second.session_generation, 2)
        old = {
            item["fleetId"]: item
            for item in coordinator.slot_snapshot("slot-001")
        }["fleet-auth"]
        self.assertEqual(old["status"], "released")
        self.assertTrue(old["retiredFromSession"])

    def test_routing_errors_always_include_structured_guidance(self) -> None:
        error = FleetRoutingError(
            "reuse_session_conflict",
            "worker belongs to another named session",
        ).to_dict()
        self.assertEqual(error["reasonKind"], "reuse_session_conflict")
        self.assertTrue(error["next_instruction"])

    def test_model_cannot_self_report_infrastructure_routing_failure(self) -> None:
        diagnostics = WorkerDiagnostics()
        for status in (
            WORKER_STATUS_SESSION_FLEET_LOST,
            WORKER_STATUS_FLEET_ASSIGNMENT_LOST,
        ):
            classified, _reason = classify_terminal_status(
                diagnostics=diagnostics,
                model_reported_status=status,
                reached_step_cap=False,
            )
            self.assertEqual(classified, WORKER_STATUS_UNKNOWN)

    def test_terminal_browser_result_is_hard_routing_evidence(self) -> None:
        diagnostics = WorkerDiagnostics()
        diagnostics.observe_browser_call(
            "Page.create",
            {"fleetId": "fleet-auth"},
            {
                "status": WORKER_STATUS_SESSION_FLEET_LOST,
                "terminal": True,
                "error": "Fleet fleet-auth is archived",
            },
        )
        classified, _reason = classify_terminal_status(
            diagnostics=diagnostics,
            model_reported_status=WORKER_STATUS_DONE,
            reached_step_cap=False,
        )
        self.assertEqual(classified, WORKER_STATUS_SESSION_FLEET_LOST)

    def test_session_and_explicit_handoff_must_resolve_to_same_fleet(self) -> None:
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-session", "fleet-other"],
        )
        coordinator.bind_assignment(
            worker_id="worker-session",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-session",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        coordinator.bind_assignment(
            worker_id="worker-other",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-other",
            assignment_reason="explicit",
            reuse_scope="page",
            page_policy="existing",
        )

        with self.assertRaises(FleetRoutingError) as caught:
            coordinator.choose_existing(
                worker_id="worker-conflict",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                candidate_fleet_ids=["fleet-session", "fleet-other"],
                reuse_scope="page",
                page_policy="existing",
                session_key="shop:A",
                reuse_from_worker_id="worker-other",
            )
        self.assertEqual(caught.exception.code, "fleet_routing_conflict")

        consistent = coordinator.choose_existing(
            worker_id="worker-consistent",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-session", "fleet-other"],
            reuse_scope="page",
            page_policy="existing",
            session_key="shop:A",
            reuse_from_worker_id="worker-session",
        )
        self.assertEqual(consistent.fleet_id, "fleet-session")

    def test_named_isolated_session_reuses_exact_fleet(self) -> None:
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-isolated"],
        )
        coordinator.bind_assignment(
            worker_id="first",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-isolated",
            assignment_reason="isolated_session",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
            is_isolated=True,
        )
        reused = coordinator.choose_existing(
            worker_id="second",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-isolated"],
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
            needs_isolated_session=True,
        )
        self.assertEqual(reused.fleet_id, "fleet-isolated")
        self.assertEqual(reused.assignment_reason, "isolated_session_reuse")
        self.assertTrue(reused.is_isolated)

    def test_fleet_binding_injects_and_rejects_out_of_assignment_calls(self) -> None:
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            assigned_fleet_id="fleet-a",
            allowed_fleet_ids={"fleet-a"},
            allowed_page_ids={"page-a"},
            page_fleet_ids={"page-a": "fleet-a"},
            page_reuse_allowed=False,
            fleet_assignment_reason="slot_default",
        )
        params = {"url": "https://example.com"}
        rejection, receipt = _apply_fleet_binding(agent, "Page.create", params)
        self.assertIsNone(rejection)
        self.assertEqual(params["fleetId"], "fleet-a")
        self.assertTrue(receipt["fleetInjected"])

        rejection, _ = _apply_fleet_binding(
            agent, "Page.create", {"fleetId": "fabricated"}
        )
        self.assertEqual(rejection["status"], "fleet_binding_violation")

        rejection, _ = _apply_fleet_binding(agent, "Fleet.create", {})
        self.assertEqual(rejection["status"], "fleet_create_coordinator_owned")
        rejection, _ = _apply_fleet_binding(
            agent, "Fleet.close", {"fleetId": "fleet-a"}
        )
        self.assertEqual(rejection["status"], "fleet_close_coordinator_owned")
        rejection, receipt = _apply_fleet_binding(
            agent, "DOM.getAXTree", {"pageId": "page-a"}
        )
        self.assertIsNone(rejection)
        self.assertEqual(receipt, {})
        self.assertIsNone(
            _check_page_binding(agent, "DOM.getAXTree", {"pageId": "page-a"})
        )
        rejection = _check_page_binding(
            agent, "DOM.getAXTree", {"pageId": "page-b"}
        )
        self.assertEqual(rejection["status"], "page_binding_violation")

        list_params = {}
        rejection, receipt = _apply_fleet_binding(agent, "Page.list", list_params)
        self.assertIsNone(rejection)
        self.assertEqual(list_params["fleetId"], "fleet-a")
        self.assertTrue(receipt["fleetInjected"])
        rejection = _check_page_binding(agent, "Page.list", list_params)
        self.assertEqual(rejection["status"], "page_reuse_not_allowed")
        agent.page_reuse_allowed = True
        self.assertIsNone(_check_page_binding(agent, "Page.list", list_params))

        _observe_page_binding_after(
            agent,
            "Page.create",
            {"fleetId": "fleet-a"},
            {
                "method": "Page.create",
                "response": {
                    "data": {"pageId": "page-new", "fleetId": "fleet-a"}
                },
            },
        )
        self.assertIn("page-new", agent.allowed_page_ids)
        self.assertEqual(agent.page_fleet_ids["page-new"], "fleet-a")
        self.assertIsNone(
            _check_page_binding(agent, "Input.click", {"pageId": "page-new"})
        )
        _observe_page_binding_after(
            agent,
            "Page.close",
            {"pageId": "page-new"},
            {"method": "Page.close", "response": {"data": {"ok": True}}},
        )
        self.assertNotIn("page-new", agent.allowed_page_ids)
        rejection = _check_page_binding(
            agent, "Input.click", {"pageId": "page-new"}
        )
        self.assertEqual(rejection["status"], "page_binding_violation")

    def test_page_list_refresh_cannot_expand_page_delegation(self) -> None:
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            assigned_fleet_id="fleet-a",
            allowed_fleet_ids={"fleet-a"},
            allowed_page_ids={"page-allowed"},
            page_fleet_ids={"page-allowed": "fleet-a"},
        )
        response, receipt = _filter_page_list_response(
            agent,
            {
                "data": {
                    "pages": [
                        {"pageId": "page-allowed", "fleetId": "fleet-a"},
                        {"pageId": "page-hidden", "fleetId": "fleet-a"},
                    ]
                }
            },
        )
        self.assertEqual(
            [page["pageId"] for page in response["data"]["pages"]],
            ["page-allowed"],
        )
        self.assertEqual(receipt["visiblePageCount"], 1)
        self.assertEqual(receipt["hiddenPageCount"], 1)

        _observe_page_binding_after(
            agent,
            "Page.list",
            {"fleetId": "fleet-a"},
            {
                "method": "Page.list",
                "response": {
                    "data": {
                        "pages": [
                            {"pageId": "page-allowed", "fleetId": "fleet-a"},
                            {"pageId": "page-hidden", "fleetId": "fleet-a"},
                        ]
                    }
                },
            },
        )
        self.assertEqual(agent.allowed_page_ids, {"page-allowed"})
        self.assertNotIn("page-hidden", agent.page_fleet_ids)

    def test_page_create_recovery_cannot_escape_assigned_fleet(self) -> None:
        class FakeBrowser:
            def __init__(self):
                self.page_list_fleets = []

            async def call(self, method, params):
                if method == "Fleet.list":
                    return {
                        "data": {
                            "fleets": [
                                {"fleetId": "fleet-a"},
                                {"fleetId": "fleet-b"},
                            ]
                        }
                    }
                if method == "Page.list":
                    fleet_id = params["fleetId"]
                    self.page_list_fleets.append(fleet_id)
                    return {
                        "data": {
                            "pages": [{
                                "pageId": f"page-{fleet_id[-1]}",
                                "fleetId": fleet_id,
                            }]
                        }
                    }
                if method == "Page.getState":
                    suffix = str(params["pageId"])[-1]
                    return {
                        "data": {
                            "pageId": params["pageId"],
                            "fleetId": f"fleet-{suffix}",
                            "status": "idle",
                            "url": "https://example.com",
                        }
                    }
                raise AssertionError(method)

        browser = FakeBrowser()
        agent = SimpleNamespace(
            browser=browser,
            render_recovery_runner=None,
            assigned_fleet_id="fleet-a",
            preloaded_registration={
                "data": {
                    "pages": [{
                        "pageId": "page-b-old",
                        "fleetId": "fleet-b",
                    }]
                }
            },
        )
        recovered, should_stop = asyncio.run(_recover_page_create_32005(
            agent,
            {"fleetId": "fleet-a", "url": "https://example.com"},
            {"error": "Page.create failed -32005"},
        ))

        self.assertFalse(should_stop)
        self.assertEqual(browser.page_list_fleets, ["fleet-a"])
        self.assertEqual(recovered["response"]["data"]["fleetId"], "fleet-a")
        self.assertEqual(recovered["response"]["data"]["pageId"], "page-a")

    def test_archived_assigned_fleet_returns_terminal_session_signal(self) -> None:
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            assigned_fleet_id="fleet-a",
            fleet_session_key="shop:A",
        )
        result = _assigned_fleet_lost_result(
            agent,
            "Page.create",
            {"fleetId": "fleet-a"},
            {"error": "Fleet fleet-a is archived; reset it from User before reuse"},
        )
        self.assertEqual(result["status"], "session_fleet_lost")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["errorClassification"]["suggested_action"] == "retry")

        alternate = _assigned_fleet_lost_result(
            agent,
            "Page.create",
            {"fleetId": "fleet-a"},
            {"error": "Fleet fleet-a has been archived and cannot be reused"},
        )
        self.assertEqual(alternate["status"], "session_fleet_lost")

        structured = _assigned_fleet_lost_result(
            agent,
            "Page.create",
            {"fleetId": "fleet-a"},
            {
                "response": {
                    "error": {
                        "errorCode": "FLEET_ARCHIVED",
                        "message": "unavailable",
                    }
                }
            },
        )
        self.assertEqual(structured["status"], "session_fleet_lost")
        self.assertEqual(structured["fleetLossSignal"], "FLEET_ARCHIVED")

    def test_canary_without_page_or_assigned_fleet_fails_closed(self) -> None:
        class NoCallBrowser:
            async def call(self, method, params):
                raise AssertionError(f"unexpected browser call: {method}")

        result = asyncio.run(canary_validate(
            SimpleNamespace(),
            {"steps": [{"type": "action", "action": "Page.navigate"}]},
            browser=NoCallBrowser(),
            canary_variables={},
        ))
        self.assertEqual(result["reason"], "fleet_assignment_required")
        self.assertFalse(result["tool_was_executed"])

    def test_archived_generic_fleet_preserves_lost_status_then_reassigns(self) -> None:
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            assigned_fleet_id="fleet-old",
            fleet_session_key="",
        )
        lost = _assigned_fleet_lost_result(
            agent,
            "Page.create",
            {"fleetId": "fleet-old"},
            {"error": "Fleet fleet-old is archived"},
        )
        self.assertEqual(lost["status"], "fleet_assignment_lost")
        self.assertEqual(
            phase_result_status_for({
                "status": "fleet_assignment_lost",
                "validatedStatus": "not_validated",
                "artifactValidation": {"status": "not_validated"},
            }),
            "fleet_assignment_lost",
        )

        class FakeClient:
            def __init__(self):
                self.on_event = None

            async def call(self, method, params):
                if method == "System.register":
                    return {"data": {"fleets": []}}
                if method == "Fleet.create":
                    return {"data": {"fleetId": "fleet-new"}}
                raise AssertionError(method)

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            client=FakeClient(),
            status="running",
            current_worker_id="browser-retry",
            last_sync_at=10**12,
            fleet_ids={"fleet-old"},
        )
        spawner.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="browser-old",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-old",
            assignment_reason="slot_default",
            reuse_scope="connection",
            page_policy="new",
        )
        asyncio.run(spawner._prepare_slot_for_worker(
            slot,
            "browser-retry",
            expose_reusable_pages=False,
        ))
        reassigned = asyncio.run(spawner._assign_fleet_for_worker(
            slot,
            worker_id="browser-retry",
            worker_contract={},
            reuse_scope="connection",
            page_policy="new",
            session_key="",
            reuse_from_worker_id="",
        ))
        self.assertEqual(reassigned.fleet_id, "fleet-new")
        self.assertTrue(reassigned.created_for_worker)

    def test_disabled_fleet_reuse_keeps_legacy_page_create_recovery(self) -> None:
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=False)
            ),
            assigned_fleet_id="fleet-old",
            fleet_session_key="",
        )
        result = _assigned_fleet_lost_result(
            agent,
            "Page.create",
            {"fleetId": "fleet-old"},
            {"error": "Fleet fleet-old is archived"},
        )
        self.assertIsNone(result)

    def test_spawner_creates_one_bootstrap_fleet_then_reuses_it(self) -> None:
        class FakeClient:
            def __init__(self):
                self.create_calls = 0

            async def call(self, method, params):
                if method == "Fleet.create":
                    self.create_calls += 1
                    return {"data": {"fleetId": "fleet-created"}}
                raise AssertionError(method)

        spawner = _make_spawner(self)
        client = FakeClient()
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            client=client,
        )
        first = asyncio.run(spawner._assign_fleet_for_worker(
            slot,
            worker_id="browser-001",
            worker_contract={},
            reuse_scope="connection",
            page_policy="new",
            session_key="",
            reuse_from_worker_id="",
        ))
        second = asyncio.run(spawner._assign_fleet_for_worker(
            slot,
            worker_id="browser-002",
            worker_contract={},
            reuse_scope="connection",
            page_policy="new",
            session_key="",
            reuse_from_worker_id="",
        ))

        self.assertEqual(client.create_calls, 1)
        self.assertEqual(first.fleet_id, "fleet-created")
        self.assertEqual(second.fleet_id, "fleet-created")
        self.assertEqual(second.assignment_reason, "slot_default")

    def test_prompt_contract_redacts_only_top_level_internal_fields(self) -> None:
        internal = {
            "skill_id": "collection",
            "_skill_route_source": "suite_routed",
            "_repair_manifest": {"_nested_state": "kept-with-parent"},
            "expected_artifact": {"fields": ["rank"]},
        }
        exposed = _prompt_worker_contract(internal)
        self.assertEqual(exposed, {
            "skill_id": "collection",
            "expected_artifact": {"fields": ["rank"]},
        })
        self.assertIn("_skill_route_source", internal)  # source contract untouched

    def test_fast_path_terminal_status_is_done_not_constructor_running(self) -> None:
        self.assertEqual(_effective_worker_status("running", "skill answer"), "done")
        self.assertEqual(_effective_worker_status("incomplete", None), "incomplete")

    def test_execution_metadata_distinguishes_fast_repair_and_slow(self) -> None:
        self.assertEqual(
            _skill_execution_metadata({"handled": True, "completedRows": 4}),
            {"executionMode": "skill_fast_path", "fastPathRows": 4, "repairRows": 0},
        )
        repair = _skill_execution_metadata({
            "handled": False,
            "completedRows": 4,
            "repair_manifest": {"repairs": [{}, {}]},
        })
        self.assertEqual(repair["executionMode"], "skill_repair")
        self.assertEqual(repair["fastPathRows"], 4)
        self.assertEqual(repair["repairRows"], 2)
        self.assertEqual(
            _skill_execution_metadata(None)["executionMode"],
            "browser_slow_path",
        )

    def test_repair_visual_pending_is_machine_enforced_at_spawner_boundary(self) -> None:
        harness = SimpleNamespace(
            worker_contract={
                "_repair_manifest": {
                    "visualEvidencePending": [
                        {
                            "identity": {"field": "url", "value": "https://example.com/a"},
                            "field": "availability",
                            "outcome": "confirmed_absent",
                            "signature": "sig-a",
                        },
                        {
                            "identity": {"field": "url", "value": "https://example.com/b"},
                            "field": "availability",
                            "outcome": "confirmed_absent",
                            "signature": "sig-b",
                        },
                    ],
                },
            },
            reality_check_count=0,
            vl_check_count=0,
            vl_force_check_count=0,
        )
        self.assertEqual(len(_unresolved_repair_visual_evidence(harness)), 2)
        harness.vl_check_count = 99
        # Unrelated/overlay VL counters no longer satisfy target evidence.
        self.assertEqual(len(_unresolved_repair_visual_evidence(harness)), 2)
        harness.worker_contract["_repair_manifest"]["visualEvidenceSatisfied"] = {
            "sig-a": {"screenshotPath": "/tmp/a.png"},
        }
        unresolved = _unresolved_repair_visual_evidence(harness)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["signature"], "sig-b")

    def test_repair_fallback_reports_actual_full_slow_path(self) -> None:
        metadata = {
            "executionMode": "skill_repair",
            "fastPathRows": 3,
            "repairRows": 1,
        }
        harness = SimpleNamespace(worker_contract={
            "_repair_manifest": {
                "disabledReason": "baseline_unreadable",
                "visualEvidencePending": [{
                    "identity": {"field": "url", "value": "https://example.com/a"},
                    "field": "availability",
                    "signature": "stale-a",
                }],
            },
        })

        result = _finalize_skill_execution_metadata(metadata, harness)

        self.assertEqual(result["executionMode"], "browser_slow_path")
        self.assertTrue(result["skillRepairFallback"])
        self.assertEqual(result["repairFallbackReason"], "baseline_unreadable")
        self.assertEqual(result["fastPathRows"], 3)
        self.assertEqual(_unresolved_repair_visual_evidence(harness), [])

    def test_selected_workflow_tool_is_reported_without_claiming_zero_llm(self) -> None:
        harness = SimpleNamespace(
            worker_contract={},
            trace=[
                {"type": "model"},
                {"type": "execute_selected_skill", "result": {"completedRows": 2}},
            ],
        )
        result = _finalize_skill_execution_metadata(
            _skill_execution_metadata(None), harness,
        )
        self.assertEqual(result["executionMode"], "browser_slow_path")
        self.assertTrue(result["skillAssistedSlowPath"])
        self.assertEqual(result["selectedSkillWorkflowCalls"], 1)

    def test_fresh_page_context_hides_previous_page_details(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        slot.fleet_ids.add("fleet-1")
        slot.origins.add("https://example.com")
        slot.page_registry["page-1"] = {
            "pageId": "page-1",
            "fleetId": "fleet-1",
            "url": "https://example.com/private/orders?user=alice",
            "title": "订单详情 - 张三",
            "status": "navigated",
        }

        context = spawner._render_slot_context(slot, expose_reusable_pages=False)
        payload = _slot_context_payload(context)

        self.assertEqual(payload["pageReuseMode"], "fresh_page_required")
        self.assertEqual(payload["existingPageCount"], 1)
        self.assertEqual(payload["fleetCount"], 1)
        self.assertEqual(payload["originCount"], 1)
        self.assertNotIn("fleetIds", payload)
        self.assertNotIn("origins", payload)
        self.assertNotIn("pages", payload)
        self.assertNotIn("fleet-1", context)
        self.assertNotIn("https://example.com", context)
        self.assertNotIn("https://example.com/private", context)
        self.assertNotIn("订单详情", context)

    def test_assigned_context_exposes_only_coordinator_binding(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        slot.fleet_ids.update({"fleet-assigned", "fleet-unrelated"})
        assignment = spawner.fleet_coordinator.bind_assignment(
            worker_id="browser-001",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-assigned",
            assignment_reason="slot_default",
            reuse_scope="connection",
            page_policy="new",
        )

        context = spawner._render_slot_context(
            slot,
            expose_reusable_pages=False,
            assignment=assignment,
        )
        payload = _slot_context_payload(context)

        self.assertEqual(payload["assignedFleetId"], "fleet-assigned")
        self.assertEqual(payload["allowedFleetIds"], ["fleet-assigned"])
        self.assertEqual(payload["pageReuseMode"], "fresh_page_same_fleet")
        self.assertNotIn("fleet-unrelated", context)

    def test_explicit_continuation_context_exposes_page_candidates(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        slot.page_registry["page-1"] = {
            "pageId": "page-1",
            "fleetId": "fleet-1",
            "url": "https://example.com/product/1",
            "title": "Product 1",
            "status": "navigated",
        }

        context = spawner._render_slot_context(slot, expose_reusable_pages=True)
        payload = _slot_context_payload(context)

        self.assertEqual(payload["pageReuseMode"], "explicit_continuation")
        self.assertEqual(payload["pages"][0]["pageId"], "page-1")
        self.assertIn("https://example.com/product/1", context)

    def test_page_continuation_exposes_only_assigned_fleet_and_hides_newtab(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        slot.fleet_ids.update({"fleet-a", "fleet-b"})
        slot.page_registry = {
            "page-a": {
                "pageId": "page-a",
                "fleetId": "fleet-a",
                "url": "https://example.com/account",
            },
            "page-b": {
                "pageId": "page-b",
                "fleetId": "fleet-b",
                "url": "https://other.example/private",
            },
            "page-newtab": {
                "pageId": "page-newtab",
                "fleetId": "fleet-a",
                "url": "http://browser.internal/newtab.html?source=dispatcher#ready",
            },
            "page-blank": {
                "pageId": "page-blank",
                "fleetId": "fleet-a",
                "url": "about:blank#initial",
            },
            "page-chrome": {
                "pageId": "page-chrome",
                "fleetId": "fleet-a",
                "url": "chrome://newtab/?source=browser",
            },
        }
        assignment = spawner.fleet_coordinator.bind_assignment(
            worker_id="browser-001",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-a",
            assignment_reason="reuse_from_worker",
            reuse_scope="page",
            page_policy="existing",
        )

        context = spawner._render_slot_context(
            slot,
            expose_reusable_pages=True,
            assignment=assignment,
        )
        payload = _slot_context_payload(context)

        self.assertEqual(payload["fleetIds"], ["fleet-a"])
        self.assertEqual([page["pageId"] for page in payload["pages"]], ["page-a"])
        self.assertNotIn("other.example", context)
        self.assertNotIn("page-newtab", context)
        self.assertNotIn("page-blank", context)
        self.assertNotIn("page-chrome", context)

        self.assertEqual(
            spawner._page_bindings_for_worker(
                slot,
                assignment=assignment,
                expose_reusable_pages=False,
            ),
            {},
        )
        self.assertEqual(
            spawner._page_bindings_for_worker(
                slot,
                assignment=assignment,
                expose_reusable_pages=True,
            ),
            {"page-a": "fleet-a"},
        )

    def test_isolated_worker_does_not_steal_prior_page_continuation_default(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        slot.fleet_ids.update({"fleet-prior", "fleet-isolated"})
        slot.page_registry = {
            "page-prior": {
                "pageId": "page-prior",
                "fleetId": "fleet-prior",
                "url": "https://example.com/prior",
            },
            "page-isolated": {
                "pageId": "page-isolated",
                "fleetId": "fleet-isolated",
                "url": "https://example.com/private",
            },
        }
        spawner.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-prior",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-prior",
            assignment_reason="slot_default",
            reuse_scope="connection",
            page_policy="new",
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-isolated",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-isolated",
            assignment_reason="isolated_session",
            reuse_scope="connection",
            page_policy="new",
            is_isolated=True,
        )
        continuation = spawner.fleet_coordinator.choose_existing(
            worker_id="worker-continuation",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            candidate_fleet_ids=slot.fleet_ids,
            reuse_scope="page",
            page_policy="existing",
            reuse_from_worker_id="worker-prior",
        )

        context = spawner._render_slot_context(
            slot,
            expose_reusable_pages=True,
            assignment=continuation,
        )
        payload = _slot_context_payload(context)
        self.assertEqual(payload["assignedFleetId"], "fleet-prior")
        self.assertEqual([page["pageId"] for page in payload["pages"]], ["page-prior"])
        self.assertNotIn("page-isolated", context)

    def test_busy_preferred_slot_is_rejected(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            status="running",
            current_worker_id="browser-001",
        )
        spawner._slots[slot.slot_id] = slot

        result = spawner._explicit_slot_rejection(
            preferred_slot_id="slot-001",
            reuse_from_worker_id=None,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("not idle", result["error"])

    def test_unknown_reuse_from_worker_id_is_rejected(self) -> None:
        spawner = _make_spawner(self)

        result = spawner._explicit_slot_rejection(
            preferred_slot_id=None,
            reuse_from_worker_id="browser-missing",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("not found", result["error"])

    def test_spawn_intent_rejects_conflicting_session_and_slot_selectors(self) -> None:
        spawner = _make_spawner(self)
        for slot_id in ("slot-001", "slot-002"):
            spawner._slots[slot_id] = BrowserAgentSlot(
                slot_id=slot_id,
                agent_id=f"agent-{slot_id}",
                status="idle",
            )
        spawner.fleet_coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_ids=["fleet-session"],
        )
        spawner.fleet_coordinator.observe_slot(
            slot_id="slot-002",
            owner_agent_id="agent-slot-002",
            fleet_ids=["fleet-other"],
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-session",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-session",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-other",
            slot_id="slot-002",
            owner_agent_id="agent-slot-002",
            fleet_id="fleet-other",
            assignment_reason="slot_bootstrap",
            reuse_scope="connection",
            page_policy="new",
        )
        spawner._handles["worker-session"] = SimpleNamespace(slot_id="slot-001")
        spawner._handles["worker-other"] = SimpleNamespace(slot_id="slot-002")

        with self.assertRaises(FleetRoutingError) as caught:
            spawner._validate_routing_intent(
                session_key="shop:A",
                preferred_slot_id=None,
                reuse_from_worker_id="worker-other",
            )
        self.assertEqual(caught.exception.code, "fleet_routing_conflict")

        with self.assertRaises(FleetRoutingError) as caught:
            spawner._validate_routing_intent(
                session_key="shop:A",
                preferred_slot_id="slot-002",
                reuse_from_worker_id=None,
            )
        self.assertEqual(caught.exception.code, "fleet_routing_conflict")

        spawner._validate_routing_intent(
            session_key="shop:A",
            preferred_slot_id="slot-001",
            reuse_from_worker_id="worker-session",
        )

    async def _acquire_slot_for_test(self, spawner: BrowserAgentSpawner) -> dict:
        return await spawner._acquire_slot(
            worker_id="browser-new",
            phase_id="phase1",
            task="Collect data from https://example.com",
            context="",
            result_contract="Return JSON.",
            worker_contract={"task_type": "web_scrape"},
            contract_hash="hash",
            preferred_slot_id=None,
            reuse_from_worker_id=None,
        )

    def test_running_cap_rejects_with_limit_semantics(self) -> None:
        spawner = _make_spawner(self)
        for index in range(3):
            slot_id = f"slot-{index + 1:03d}"
            spawner._slots[slot_id] = BrowserAgentSlot(
                slot_id=slot_id,
                agent_id=f"agent-{slot_id}",
                status="running",
                current_worker_id=f"browser-{index + 1:03d}",
            )

        result = asyncio.run(self._acquire_slot_for_test(spawner))

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "Reached the max_browser_agents limit")
        self.assertIn("max_browser_agents", result["limit_semantics"])
        self.assertIn("max_browser_agent_instances", result["limit_semantics"])

    def test_spawn_reserves_phase_during_slot_acquire_and_cancels_on_rejection(self) -> None:
        spawner = _make_spawner(self)
        plan = {
            "version": "v1",
            "goal": "Collect one row",
            "task_type": "web_scrape",
            "phases": [{
                "id": "p1",
                "type": "browser_worker",
                "objective": "Collect one row",
                "worker_task": "Collect one row.",
                "stage_hint": "collection",
                "stage_hint_reason": "Collect a single structured row from the page.",
                "expected_artifact": {"name": "rows", "exact_rows": 1},
                "validators": [],
                "validators_normalized": True,
                "worker_contract": {},
                "max_attempts": 3,
            }],
        }
        initialize_task_state(spawner.logger, plan)
        seen_status = {}

        async def fake_acquire(**kwargs):
            state = load_task_state(spawner.logger)
            seen_status["status"] = state["phases"]["p1"]["status"]
            seen_status["attempts"] = len(state["phases"]["p1"]["attempts"])
            return {"status": "rejected", "error": "no slot"}

        spawner._acquire_slot = fake_acquire  # type: ignore[method-assign]

        result = asyncio.run(spawner.spawn_browser_agent(
            task="Collect one row.",
            phase_id="p1",
            worker_contract={},
            phase=plan["phases"][0],
            task_plan=plan,
        ))

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(seen_status, {"status": "running", "attempts": 1})
        state = load_task_state(spawner.logger)
        self.assertEqual(state["phases"]["p1"]["status"], "pending")
        self.assertEqual(state["phases"]["p1"]["attempts"], [])

    def test_live_slot_cap_rejects_when_no_idle_slot_available(self) -> None:
        spawner = _make_spawner(self)
        spawner.runtime.harness.max_browser_agents = 4
        for index in range(3):
            slot_id = f"slot-{index + 1:03d}"
            spawner._slots[slot_id] = BrowserAgentSlot(
                slot_id=slot_id,
                agent_id=f"agent-{slot_id}",
                status="running",
                current_worker_id=f"browser-{index + 1:03d}",
            )

        result = asyncio.run(self._acquire_slot_for_test(spawner))

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "No idle BrowserAgent slot available")
        self.assertEqual(result["max_browser_agent_instances"], 3)
        self.assertIn("max_browser_agent_instances", result["limit_semantics"])

    def test_cleanup_retired_slots_removes_broken_idle_slot(self) -> None:
        spawner = _make_spawner(self)
        spawner._slots["slot-001"] = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            status="broken",
        )

        spawner._cleanup_retired_slots()

        self.assertNotIn("slot-001", spawner._slots)

    def test_broken_named_session_reconnects_with_same_agent_before_tombstone(self) -> None:
        class ReconnectedClient:
            registered_agent_ids = []

            def __init__(self, *_args, **_kwargs):
                self.on_event = _kwargs.get("on_event")

            async def connect(self):
                return None

            async def call(self, method, params):
                self.assert_method = method
                self.registered_agent_ids.append(params["agentId"])
                return {
                    "data": {
                        "fleets": [{"fleetId": "fleet-auth", "status": "active"}]
                    }
                }

            async def close(self):
                return None

        spawner = _make_spawner(self)
        spawner.runtime.harness.fleet_slot_reconnect_backoff_seconds = 0
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            status="broken",
            fleet_ids={"fleet-auth"},
        )
        spawner._slots[slot.slot_id] = slot
        spawner.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-auth",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-auth",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        spawner.fleet_coordinator.mark_slot_suspect(slot.slot_id)

        with patch("harness.spawner.ABCPClient", ReconnectedClient):
            asyncio.run(spawner._recover_broken_slots())

        self.assertEqual(slot.status, "idle")
        self.assertIsNotNone(slot.client)
        self.assertEqual(ReconnectedClient.registered_agent_ids, [slot.agent_id])
        self.assertEqual(
            spawner.fleet_coordinator.session_binding_details("shop:A")["status"],
            "active",
        )

    def test_exhausted_named_session_reconnect_stays_suspect_not_lost(self) -> None:
        class FailingReconnectClient:
            attempts = 0

            def __init__(self, *_args, **_kwargs):
                pass

            async def connect(self):
                self.__class__.attempts += 1
                raise OSError("temporary network unavailable")

            async def close(self):
                return None

        spawner = _make_spawner(self)
        spawner.runtime.harness.fleet_slot_reconnect_attempts = 2
        spawner.runtime.harness.fleet_slot_reconnect_backoff_seconds = 0
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            status="broken",
            fleet_ids={"fleet-auth"},
        )
        spawner._slots[slot.slot_id] = slot
        spawner.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-auth",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-auth",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        spawner.fleet_coordinator.mark_slot_suspect(slot.slot_id)

        with patch("harness.spawner.ABCPClient", FailingReconnectClient):
            asyncio.run(spawner._recover_broken_slots())

        self.assertEqual(FailingReconnectClient.attempts, 2)
        self.assertIn(slot.slot_id, spawner._slots)
        details = spawner.fleet_coordinator.session_binding_details("shop:A")
        self.assertEqual(details["status"], "suspect")

        rejection = asyncio.run(spawner._acquire_slot(
            worker_id="worker-next",
            phase_id=None,
            task="continue authenticated work",
            context="",
            result_contract="",
            worker_contract={},
            contract_hash="hash",
            preferred_slot_id=None,
            reuse_from_worker_id=None,
            session_key="shop:A",
        ))
        self.assertEqual(rejection["status"], "session_transport_unavailable")
        self.assertTrue(rejection["retryable"])

    def test_repeated_session_transport_failure_requires_operator_reset(self) -> None:
        spawner = _make_spawner(self)
        spawner.runtime.harness.fleet_slot_manual_reset_after_failures = 2
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            status="broken",
            fleet_ids={"fleet-auth"},
            recovery_failure_cycles=2,
        )
        spawner._slots[slot.slot_id] = slot
        spawner.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
        )
        assignment = spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-auth",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-auth",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )

        rejection = asyncio.run(spawner._acquire_slot(
            worker_id="worker-next",
            phase_id=None,
            task="continue authenticated work",
            context="",
            result_contract="",
            worker_contract={},
            contract_hash="hash",
            preferred_slot_id=None,
            reuse_from_worker_id=None,
            session_key="shop:A",
        ))
        self.assertEqual(rejection["status"], "session_manual_reset_required")
        self.assertFalse(rejection["retryable"])
        self.assertEqual(rejection["fleetId"], assignment.fleet_id)
        self.assertEqual(
            rejection["sessionGeneration"], assignment.session_generation
        )

    def test_operator_reset_cas_releases_broken_session(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            status="broken",
            fleet_ids={"fleet-auth"},
        )
        spawner._slots[slot.slot_id] = slot
        spawner.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
        )
        assignment = spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-auth",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-auth",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        asyncio.run(spawner.fleet_auth_barrier.claim(
            "fleet-auth", "resolver", "login"
        ))

        receipt = asyncio.run(spawner.reset_auth_session(
            session_key="shop:A",
            expected_fleet_id="fleet-auth",
            expected_generation=assignment.session_generation,
            reason="operator accepted a fresh login",
        ))
        self.assertEqual(receipt["status"], "released")
        self.assertTrue(receipt["authBarrier"]["discarded"])
        self.assertIsNone(
            spawner.fleet_coordinator.fleet_for_session("shop:A")
        )
        self.assertNotIn(slot.slot_id, spawner._slots)

    def test_authoritative_reconnect_inventory_can_confirm_session_fleet_lost(self) -> None:
        class EmptyInventoryClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def connect(self):
                return None

            async def call(self, method, params):
                return {"data": {"fleets": []}}

            async def close(self):
                return None

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            status="broken",
            fleet_ids={"fleet-auth"},
        )
        spawner._slots[slot.slot_id] = slot
        spawner.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
        )
        spawner.fleet_coordinator.bind_assignment(
            worker_id="worker-auth",
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_id="fleet-auth",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
        )
        spawner.fleet_coordinator.mark_slot_suspect(slot.slot_id)

        with patch("harness.spawner.ABCPClient", EmptyInventoryClient):
            asyncio.run(spawner._recover_broken_slots())

        self.assertEqual(slot.status, "idle")
        self.assertEqual(
            spawner.fleet_coordinator.session_binding_details("shop:A")["status"],
            "missing",
        )
        with self.assertRaises(FleetRoutingError) as caught:
            spawner.fleet_coordinator.choose_existing(
                worker_id="worker-next",
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                candidate_fleet_ids=slot.fleet_ids,
                reuse_scope="fleet",
                page_policy="new",
                session_key="shop:A",
            )
        self.assertEqual(caught.exception.code, "session_fleet_lost")

    def test_page_list_restores_stale_page(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        slot.page_registry["page-1"] = {
            "pageId": "page-1",
            "fleetId": "fleet-1",
            "status": "stale",
            "lastStateError": "detached",
        }

        spawner._replace_fleet_pages_from_list(
            slot,
            fleet_id="fleet-1",
            pages_response={
                "data": {
                    "pages": [
                        {
                            "pageId": "page-1",
                            "url": "https://example.com/product/1",
                        }
                    ]
                }
            },
        )

        page = slot.page_registry["page-1"]
        self.assertNotEqual(page.get("status"), "stale")
        self.assertNotIn("lastStateError", page)

    def test_deadlock_result_quarantines_page_candidates(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        slot.page_registry["page-dead"] = {
            "pageId": "page-dead",
            "fleetId": "fleet-1",
            "url": "https://example.com/challenge",
            "title": "Just a moment...",
            "status": "navigated",
        }

        spawner._record_slot_result(
            slot,
            worker_id="browser-003",
            phase_id="phase2",
            worker_contract={"task_type": "web_scrape"},
            result={
                "status": "stale_pause_deadlock",
                "statusCategory": "recoverable",
                "diagnostics": {"last_pause_pageId": "page-dead"},
            },
            trace=[],
        )

        context = spawner._render_slot_context(slot, expose_reusable_pages=True)
        payload = _slot_context_payload(context)

        self.assertEqual(slot.page_registry["page-dead"]["status"], "quarantined")
        self.assertTrue(slot.page_registry["page-dead"]["doNotUse"])
        self.assertEqual(payload["pages"], [])
        self.assertEqual(payload["quarantinedPageCount"], 1)
        self.assertEqual(payload["quarantinedPages"][0]["pageId"], "page-dead")
        self.assertTrue(payload["quarantinedPages"][0]["doNotUse"])

    def test_page_list_does_not_restore_quarantined_page(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        spawner._mark_page_quarantined(
            slot,
            "page-1",
            reason="stale pause deadlock",
            worker_id="browser-003",
            phase_id="phase2",
        )

        spawner._replace_fleet_pages_from_list(
            slot,
            fleet_id="fleet-1",
            pages_response={
                "data": {
                    "pages": [
                        {
                            "pageId": "page-1",
                            "fleetId": "fleet-1",
                            "url": "https://example.com/product/1",
                            "status": "navigated",
                        }
                    ]
                }
            },
        )

        context = spawner._render_slot_context(slot, expose_reusable_pages=True)
        payload = _slot_context_payload(context)

        self.assertIn("page-1", slot.page_quarantine)
        self.assertEqual(slot.page_registry["page-1"]["status"], "quarantined")
        self.assertEqual(payload["pages"], [])
        self.assertEqual(payload["quarantinedPages"][0]["pageId"], "page-1")

    def test_usable_state_sync_clears_page_quarantine(self) -> None:
        class FakeClient:
            async def call(self, method, params):
                if method == "Fleet.list":
                    return {"data": {"fleets": [{"fleetId": "fleet-1"}]}}
                if method == "Page.list":
                    return {
                        "data": {
                            "pages": [
                                {
                                    "pageId": "page-1",
                                    "fleetId": "fleet-1",
                                    "url": "https://example.com/product/1",
                                }
                            ]
                        }
                    }
                if method == "Page.getState":
                    return {
                        "data": {
                            "pageId": "page-1",
                            "fleetId": "fleet-1",
                            "url": "https://example.com/product/1",
                            "status": "idle",
                        }
                    }
                raise AssertionError(method)

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            client=FakeClient(),
        )
        slot.fleet_ids.add("fleet-1")
        spawner._mark_page_quarantined(
            slot,
            "page-1",
            reason="stale pause deadlock",
            worker_id="browser-003",
            phase_id="phase2",
        )

        asyncio.run(spawner._sync_slot_registry(slot, worker_id="browser-004"))

        self.assertNotIn("page-1", slot.page_quarantine)
        self.assertNotEqual(slot.page_registry["page-1"].get("status"), "quarantined")
        context = spawner._render_slot_context(slot, expose_reusable_pages=True)
        payload = _slot_context_payload(context)
        self.assertEqual(payload["pages"][0]["pageId"], "page-1")

    def test_cancelled_result_updates_slot_summary(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")

        spawner._record_slot_result(
            slot,
            worker_id="browser-001",
            phase_id="phase2",
            worker_contract={"task_type": "web_scrape"},
            result={"status": "cancelled", "statusCategory": "cancelled"},
            trace=[],
        )

        self.assertEqual(slot.last_phase_id, "phase2")
        self.assertEqual(slot.last_task_type, "web_scrape")
        self.assertEqual(slot.last_result_summary["status"], "cancelled")

    def test_method_schema_echo_does_not_poison_page_registry(self) -> None:
        # Regression: a browser_call trace result carries an attached
        # methodSchema whose params.pageId is the describeAction SCHEMA DICT
        # (not a real id). The legacy str()-coercion registered str({...}) as a
        # pageId key, and Page.getState on it later returned -32602.
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-001", agent_id="agent-slot-001")
        real_page_id = "14f29e2b-e625-4f72-a6ad-db012cc1c79b"
        spawner._update_slot_registry_from_value(slot, {
            "method": "DOM.getAttribute",
            "params": {"pageId": real_page_id},
            "result": {
                "methodSchema": {
                    "method": "DOM.getAttribute",
                    "params": {
                        "pageId": {
                            "type": "string",
                            "format": "uuid",
                            "pattern": "^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$",
                            "description": "Page ID containing the target element.",
                            "required": True,
                        },
                        "id": {"type": "string", "pattern": "^\\d+:\\d+:\\d+$"},
                    },
                }
            },
        })
        # The real page id (a string) must register; no schema-dict repr key
        # (which starts with '{') may leak into the registry or fleet set.
        self.assertIn(real_page_id, slot.page_registry)
        self.assertEqual(len(slot.page_registry), 1)
        self.assertFalse(any(k.startswith("{") for k in slot.page_registry))
        self.assertFalse(any(fid.startswith("{") for fid in slot.fleet_ids))

    def test_sync_replaces_fleet_inventory_and_self_heals_bogus_ids(self) -> None:
        # Defense-in-depth: even if a bogus (schema-dict repr) id already sits
        # in the registry, _sync_slot_registry must drop it instead of calling
        # Page.list / Page.getState on it (which return -32602).
        seen_state_ids: list = []

        class FakeClient:
            async def call(self, method, params):
                if method == "Fleet.list":
                    return {"data": {"fleets": []}}
                if method == "Page.list":
                    return {"data": {"pages": []}}
                if method == "Page.getState":
                    seen_state_ids.append(params.get("pageId"))
                    return {"data": {"pageId": params.get("pageId"), "status": "idle"}}
                raise AssertionError(method)

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001", agent_id="agent-slot-001", client=FakeClient()
        )
        slot.fleet_ids = {"fleet-real", "{'type': 'string', 'description': 'bad fleet'}"}
        slot.page_registry = {
            "page-real": {"pageId": "page-real"},
            "{'type': 'string', 'description': 'bad page'}": {
                "pageId": "{'type': 'string', 'description': 'bad page'}",
            },
        }

        asyncio.run(spawner._sync_slot_registry(slot, worker_id="browser-004"))

        # Fleet.list is the authoritative owner inventory; an empty response
        # removes even syntactically valid stale fleet ids.
        self.assertNotIn("fleet-real", slot.fleet_ids)
        self.assertNotIn("{'type': 'string', 'description': 'bad fleet'}", slot.fleet_ids)
        self.assertIn("page-real", slot.page_registry)
        self.assertNotIn("{'type': 'string', 'description': 'bad page'}", slot.page_registry)
        self.assertTrue(all(not (i and str(i).startswith("{")) for i in seen_state_ids))

    def test_register_response_replaces_fleet_inventory_without_ttl_delay(self) -> None:
        class FakeClient:
            async def call(self, method, params):
                if method == "System.register":
                    return {"data": {"fleets": [{"fleetId": "fleet-current"}]}}
                raise AssertionError(method)

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            client=FakeClient(),
            last_sync_at=10**12,
        )
        slot.fleet_ids = {"fleet-archived"}
        slot.page_registry["page-old"] = {
            "pageId": "page-old",
            "fleetId": "fleet-archived",
        }

        asyncio.run(spawner._prepare_slot_for_worker(
            slot,
            "browser-001",
            expose_reusable_pages=False,
        ))

        self.assertEqual(slot.fleet_ids, {"fleet-current"})
        self.assertNotIn("page-old", slot.page_registry)

    def test_concurrent_first_session_spawn_creates_one_fleet(self) -> None:
        async def scenario() -> None:
            class FakeClient:
                def __init__(self, fleet_id):
                    self.fleet_id = fleet_id
                    self.fleets = set()
                    self.create_calls = 0
                    self.on_event = None

                async def call(self, method, params):
                    if method == "System.register":
                        return {
                            "data": {
                                "fleets": [
                                    {"fleetId": fleet_id}
                                    for fleet_id in sorted(self.fleets)
                                ]
                            }
                        }
                    if method == "Fleet.create":
                        self.create_calls += 1
                        await asyncio.sleep(0)
                        self.fleets.add(self.fleet_id)
                        return {"data": {"fleetId": self.fleet_id}}
                    raise AssertionError(method)

                async def close(self):
                    return None

            spawner = _make_spawner(self)
            spawner.runtime.harness.same_fleet_multiworker_enabled = True
            clients = [FakeClient("fleet-1"), FakeClient("fleet-2")]
            for index, client in enumerate(clients, 1):
                slot = BrowserAgentSlot(
                    slot_id=f"slot-{index:03d}",
                    agent_id=f"agent-slot-{index:03d}",
                    client=client,
                    status="idle",
                    last_sync_at=10**12,
                )
                spawner._slots[slot.slot_id] = slot

            release = asyncio.Event()

            async def hold_worker(**kwargs):
                await release.wait()
                result = {
                    "status": "done",
                    "workerId": kwargs["worker_id"],
                    "slotId": kwargs["slot"].slot_id,
                }
                spawner._mark_slot_idle(
                    kwargs["slot"],
                    worker_id=kwargs["worker_id"],
                    result=result,
                )
                return result

            spawner._run_browser_worker = hold_worker
            first, second = await asyncio.gather(
                spawner.spawn_browser_agent(
                    task="first",
                    worker_contract={},
                    reuse_scope="fleet",
                    session_key="shop:A",
                ),
                spawner.spawn_browser_agent(
                    task="second",
                    worker_contract={},
                    reuse_scope="fleet",
                    session_key="shop:A",
                ),
            )
            self.assertEqual(first["status"], "running")
            self.assertEqual(second["status"], "running")
            self.assertEqual(
                first["fleetAssignment"]["assignedFleetId"],
                second["fleetAssignment"]["assignedFleetId"],
            )
            self.assertTrue(
                first["fleetAssignment"]["delegated"]
                or second["fleetAssignment"]["delegated"]
            )
            self.assertEqual(sum(client.create_calls for client in clients), 1)
            release.set()
            await asyncio.gather(*(
                handle.async_task for handle in spawner._handles.values()
            ))

        asyncio.run(scenario())

    def test_parallel_generic_workers_share_task_fleet_across_slots(self) -> None:
        async def scenario() -> None:
            class FakeClient:
                def __init__(self, fleet_id):
                    self.fleet_id = fleet_id
                    self.fleets = set()
                    self.create_calls = 0
                    self.on_event = None

                async def call(self, method, params):
                    if method == "System.register":
                        return {
                            "data": {
                                "fleets": [
                                    {"fleetId": fleet_id}
                                    for fleet_id in sorted(self.fleets)
                                ]
                            }
                        }
                    if method == "Fleet.create":
                        self.create_calls += 1
                        self.fleets.add(self.fleet_id)
                        return {"data": {"fleetId": self.fleet_id}}
                    raise AssertionError(method)

            spawner = _make_spawner(self)
            spawner.runtime.harness.same_fleet_multiworker_enabled = True
            clients = [FakeClient("fleet-1"), FakeClient("fleet-2")]
            for index, client in enumerate(clients, 1):
                slot = BrowserAgentSlot(
                    slot_id=f"slot-{index:03d}",
                    agent_id=f"agent-slot-{index:03d}",
                    client=client,
                    status="idle",
                    last_sync_at=10**12,
                )
                spawner._slots[slot.slot_id] = slot

            release = asyncio.Event()

            async def hold_worker(**kwargs):
                await release.wait()
                result = {
                    "status": "done",
                    "workerId": kwargs["worker_id"],
                    "slotId": kwargs["slot"].slot_id,
                }
                spawner._mark_slot_idle(
                    kwargs["slot"],
                    worker_id=kwargs["worker_id"],
                    result=result,
                )
                return result

            spawner._run_browser_worker = hold_worker
            first, second = await asyncio.gather(
                spawner.spawn_browser_agent(task="parallel phase one"),
                spawner.spawn_browser_agent(task="parallel phase two"),
            )
            self.assertEqual(first["status"], "running")
            self.assertEqual(second["status"], "running")
            self.assertEqual(
                first["fleetAssignment"]["assignedFleetId"],
                second["fleetAssignment"]["assignedFleetId"],
            )
            self.assertEqual(sum(client.create_calls for client in clients), 1)
            self.assertTrue(
                first["fleetAssignment"]["delegated"]
                or second["fleetAssignment"]["delegated"]
            )
            release.set()
            await asyncio.gather(*(
                handle.async_task for handle in spawner._handles.values()
            ))

        asyncio.run(scenario())

    def test_distinct_sessions_prepare_in_parallel(self) -> None:
        async def scenario() -> None:
            class FakeClient:
                def __init__(self, fleet_id):
                    self.fleet_id = fleet_id
                    self.fleets = set()
                    self.on_event = None

                async def call(self, method, params):
                    if method == "System.register":
                        return {
                            "data": {
                                "fleets": [
                                    {"fleetId": fleet_id}
                                    for fleet_id in sorted(self.fleets)
                                ]
                            }
                        }
                    if method == "Fleet.create":
                        self.fleets.add(self.fleet_id)
                        return {"data": {"fleetId": self.fleet_id}}
                    raise AssertionError(method)

                async def close(self):
                    return None

            spawner = _make_spawner(self)
            for index in (1, 2):
                slot = BrowserAgentSlot(
                    slot_id=f"slot-{index:03d}",
                    agent_id=f"agent-slot-{index:03d}",
                    client=FakeClient(f"fleet-{index}"),
                    status="idle",
                    last_sync_at=10**12,
                )
                spawner._slots[slot.slot_id] = slot

            original_prepare = spawner._prepare_slot_for_worker
            prepare_release = asyncio.Event()
            both_preparing = asyncio.Event()
            entered = 0

            async def gated_prepare(slot, worker_id, *, expose_reusable_pages):
                nonlocal entered
                entered += 1
                if entered == 2:
                    both_preparing.set()
                await prepare_release.wait()
                return await original_prepare(
                    slot,
                    worker_id,
                    expose_reusable_pages=expose_reusable_pages,
                )

            worker_release = asyncio.Event()

            async def hold_worker(**kwargs):
                await worker_release.wait()
                return {"status": "done", "workerId": kwargs["worker_id"]}

            spawner._prepare_slot_for_worker = gated_prepare
            spawner._run_browser_worker = hold_worker
            first_task = asyncio.create_task(spawner.spawn_browser_agent(
                task="session A",
                worker_contract={},
                reuse_scope="fleet",
                session_key="shop:A",
            ))
            second_task = asyncio.create_task(spawner.spawn_browser_agent(
                task="session B",
                worker_contract={},
                reuse_scope="fleet",
                session_key="shop:B",
            ))

            await asyncio.wait_for(both_preparing.wait(), timeout=1.0)
            prepare_release.set()
            first, second = await asyncio.gather(first_task, second_task)
            self.assertEqual(first["status"], "running")
            self.assertEqual(second["status"], "running")
            self.assertNotEqual(
                first["fleetAssignment"]["assignedFleetId"],
                second["fleetAssignment"]["assignedFleetId"],
            )
            worker_release.set()
            await asyncio.gather(*(
                handle.async_task for handle in spawner._handles.values()
            ))

        asyncio.run(scenario())

    def test_concurrent_slot_reservations_respect_instance_cap(self) -> None:
        async def scenario() -> None:
            class FakeClient:
                def __init__(self, fleet_id):
                    self.fleet_id = fleet_id
                    self.fleets = set()
                    self.on_event = None

                async def call(self, method, params):
                    if method == "System.register":
                        return {
                            "data": {
                                "fleets": [
                                    {"fleetId": fleet_id}
                                    for fleet_id in sorted(self.fleets)
                                ]
                            }
                        }
                    if method == "Fleet.create":
                        self.fleets.add(self.fleet_id)
                        return {"data": {"fleetId": self.fleet_id}}
                    raise AssertionError(method)

            spawner = _make_spawner(self)
            initialization_release = asyncio.Event()
            three_reserved = asyncio.Event()
            initialization_count = 0

            async def gated_initialize(slot):
                nonlocal initialization_count
                initialization_count += 1
                if initialization_count == 3:
                    three_reserved.set()
                await initialization_release.wait()
                slot.client = FakeClient(f"fleet-{slot.slot_id}")
                slot.status = "running"
                slot.last_sync_at = 10**12

            async def finish_worker(**kwargs):
                return {"status": "done", "workerId": kwargs["worker_id"]}

            spawner._initialize_reserved_slot = gated_initialize
            spawner._run_browser_worker = finish_worker
            starts = [
                asyncio.create_task(spawner.spawn_browser_agent(
                    task=f"independent {index}",
                    worker_contract={},
                ))
                for index in range(5)
            ]
            await asyncio.wait_for(three_reserved.wait(), timeout=1.0)
            self.assertEqual(len(spawner._slots), 3)
            initialization_release.set()
            results = await asyncio.gather(*starts)
            self.assertEqual(
                sum(result["status"] == "running" for result in results),
                3,
            )
            self.assertEqual(
                sum(result["status"] == "rejected" for result in results),
                2,
            )
            await asyncio.gather(*(
                handle.async_task for handle in spawner._handles.values()
            ))

        asyncio.run(scenario())

    def test_prepare_failure_releases_reserved_slot(self) -> None:
        class FailingClient:
            on_event = None

            async def call(self, method, params):
                raise RuntimeError("register failed")

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="agent-slot-001",
            client=FailingClient(),
            status="idle",
        )
        spawner._slots[slot.slot_id] = slot

        result = asyncio.run(spawner.spawn_browser_agent(
            task="will fail during preparation",
            worker_contract={},
        ))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(slot.status, "idle")
        self.assertIsNone(slot.current_worker_id)

    def test_cancelled_spawn_retires_invisible_starting_slot(self) -> None:
        async def scenario() -> None:
            spawner = _make_spawner(self)
            entered = asyncio.Event()
            never = asyncio.Event()

            async def hanging_initialize(slot):
                entered.set()
                await never.wait()

            spawner._initialize_reserved_slot = hanging_initialize
            start = asyncio.create_task(spawner.spawn_browser_agent(
                task="cancel during startup",
                worker_contract={},
            ))
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            start.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await start

            self.assertEqual(len(spawner._handles), 0)
            self.assertEqual(len(spawner._slots), 1)
            slot = next(iter(spawner._slots.values()))
            self.assertEqual(slot.status, "broken")
            self.assertIsNone(slot.current_worker_id)
            spawner._cleanup_retired_slots()
            self.assertEqual(spawner._slots, {})

        asyncio.run(scenario())

    def test_cancelled_running_worker_releases_slot_and_phase(self) -> None:
        async def scenario() -> None:
            spawner = _make_spawner(self)
            entered = asyncio.Event()
            never = asyncio.Event()

            class FakeClient:
                on_event = None

            class FakeHarness:
                def __init__(self):
                    self.trace = [{"type": "worker_started"}]

                async def run(self, _task):
                    entered.set()
                    await never.wait()

            fake_harness = FakeHarness()
            spawner.browser_agent_factory = lambda *_args: fake_harness

            async def capability_bundle(*_args, **_kwargs):
                return SimpleNamespace()

            async def no_fast_path(*_args, **_kwargs):
                return None

            spawner._capability_bundle_for_worker = capability_bundle
            spawner._try_skill_fast_path = no_fast_path

            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=FakeClient(),
                status="running",
                current_worker_id="browser-001",
            )
            spawner._slots[slot.slot_id] = slot
            phase = {"id": "p1", "max_attempts": 3}
            initialize_task_state(
                spawner.logger,
                {"goal": "test cancellation", "phases": [phase]},
            )
            mark_phase_running(
                spawner.logger,
                phase_id="p1",
                worker_id="browser-001",
                worker_name="browser-001",
            )

            with patch(
                "harness.spawner.LLMFactory.create_provider",
                return_value=object(),
            ):
                worker = asyncio.create_task(spawner._run_browser_worker(
                    slot=slot,
                    registration={},
                    assignment=None,
                    expose_reusable_pages=False,
                    worker_id="browser-001",
                    name="browser-001",
                    task="wait until cancelled",
                    context="",
                    max_steps=None,
                    result_contract="",
                    phase_id="p1",
                    worker_contract={},
                    phase=phase,
                ))
                await asyncio.wait_for(entered.wait(), timeout=1.0)
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

            self.assertEqual(slot.status, "idle")
            self.assertIsNone(slot.current_worker_id)
            self.assertEqual(slot.last_result_summary["status"], "cancelled")
            phase_state = load_task_state(spawner.logger)["phases"]["p1"]
            self.assertEqual(phase_state["status"], "cancelled")
            self.assertEqual(phase_state["attempts"][-1]["status"], "cancelled")
            self.assertEqual(
                load_task_state(spawner.logger).get("objective_attempts"),
                {},
            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
