import argparse
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from abcp_client import ABCPTransportError
from runtime_config import ABCPClientConfig
from runtime_config import HarnessConfig, RuntimeConfig

from harness.auth_fleet import (
    normalize_auth_verification_contract,
    verify_protected_auth_target,
)
from harness.fleet_coordinator import (
    FleetAssignment,
    FleetCoordinator,
    FleetRoutingError,
    resolve_fleet_reference,
)
from harness.fleet_runtime import PageLeaseManager, PageLeasedBrowserClient
from harness.observation.page_inventory import PageInventorySignal
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
    FleetReadinessError,
    PinnedBrowserContext,
    _effective_worker_status,
    _finalize_skill_execution_metadata,
    _fresh_click_settlement_class,
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
    _settle_page_inventory_signal,
)
from harness.task_control import (
    active_replan_checkpoints,
    initialize_task_state,
    load_task_state,
    mark_phase_result,
    mark_phase_running,
    materialize_batch_rows_from_source,
    record_replan_checkpoint,
    record_spawn_acquisition_failure,
    reconcile_replan_checkpoints,
    replan_checkpoint_plan_errors,
    replan_checkpoint_spawn_rejection,
    spawn_acquisition_rejection,
    validate_task_plan,
    write_task_state,
)
from harness.fast_path import (
    assess_fast_path_candidate,
    trace_params_for_fast_path,
)
from harness.utils import RunLogger
from main import _validated_pinned_browser_context, build_arg_parser
from runtime_config import ModelConfig
from agent_harness import BrowserAgent
from abcp_client import NotificationHub



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
            # Legacy slot tests use minimal clients that intentionally expose
            # only the RPCs under test. Readiness-specific cases enable this.
            fleet_readiness_barrier_enabled=False,
            worktree_dir=temp.name,
        ),
    )
    logger = RunLogger(temp.name, task_id="slot-tests")
    return BrowserAgentSpawner(runtime, logger, lambda *args: None)


def _slot_context_payload(context: str) -> dict:
    body = context.split("<slot_context>", 1)[1].split("</slot_context>", 1)[0]
    return json.loads(body)


class BrowserAgentSlotTests(unittest.TestCase):
    def test_existing_fleet_reference_resolves_exact_or_unique_prefix(self) -> None:
        target = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"
        other = "961e0e6c-b405-45ce-a68d-3796871a3133"
        inventory = {target, other}

        self.assertEqual(
            resolve_fleet_reference(target.upper(), inventory),
            target,
        )
        self.assertEqual(
            resolve_fleet_reference("48f0864a", inventory),
            target,
        )

    def test_existing_fleet_reference_fails_closed_on_invalid_missing_or_ambiguous(self) -> None:
        inventory = {
            "48f0864a-79fb-4ef6-acb2-732e5e1e1818",
            "48f0864a-a6b5-4f37-9a1a-c2d209db392f",
        }
        cases = (
            ("48f0", "fleet_reference_invalid"),
            ("not-a-uuid", "fleet_reference_invalid"),
            ("--------", "fleet_reference_invalid"),
            ("961e0e6c", "fleet_reference_not_found"),
            ("48f0864a", "ambiguous_fleet_reference"),
        )
        for reference, expected_code in cases:
            with self.subTest(reference=reference):
                with self.assertRaises(FleetRoutingError) as caught:
                    resolve_fleet_reference(reference, inventory)
                self.assertEqual(caught.exception.code, expected_code)

    def test_existing_fleet_prefix_is_admitted_without_create(self) -> None:
        fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

        class NoCreateClient:
            async def call(self, method, params):
                raise AssertionError(f"unexpected browser call: {method}")

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="test-agent-slot-001",
            client=NoCreateClient(),
            fleet_ids={fleet_id},
        )
        assignment = asyncio.run(spawner._assign_fleet_for_worker(
            slot,
            worker_id="browser-001",
            worker_contract={},
            reuse_scope="fleet",
            page_policy="new",
            session_key="",
            reuse_from_worker_id="",
            fleet_id="48f0864a",
        ))

        self.assertEqual(assignment.fleet_id, fleet_id)
        self.assertFalse(assignment.created_for_worker)
        self.assertEqual(
            assignment.assignment_reason,
            "explicit_fleet_reference",
        )

    def test_existing_fleet_reference_failure_never_creates_replacement(self) -> None:
        class NoCreateClient:
            async def call(self, method, params):
                raise AssertionError(f"unexpected browser call: {method}")

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="test-agent-slot-001",
            client=NoCreateClient(),
            fleet_ids={"961e0e6c-b405-45ce-a68d-3796871a3133"},
        )
        with self.assertRaises(FleetRoutingError) as caught:
            asyncio.run(spawner._assign_fleet_for_worker(
                slot,
                worker_id="browser-001",
                worker_contract={},
                reuse_scope="fleet",
                page_policy="new",
                session_key="",
                reuse_from_worker_id="",
                fleet_id="48f0864a",
            ))

        self.assertEqual(caught.exception.code, "fleet_reference_not_found")

    def test_existing_fleet_reference_conflicts_fail_before_slot_acquisition(self) -> None:
        spawner = _make_spawner(self)
        with patch.object(
            spawner,
            "_acquire_slot",
            new_callable=AsyncMock,
        ) as acquire:
            session_conflict = asyncio.run(spawner.spawn_browser_agent(
                "Collect rows",
                fleet_id="48f0864a",
                session_key="shop:account-a",
            ))
            isolation_conflict = asyncio.run(spawner.spawn_browser_agent(
                "Collect rows",
                fleet_id="48f0864a",
                worker_contract={"needs_isolated_session": True},
            ))

        self.assertEqual(session_conflict["status"], "invalid_fleet_routing")
        self.assertEqual(isolation_conflict["status"], "invalid_fleet_routing")
        acquire.assert_not_awaited()

    def test_existing_fleet_reference_requires_reuse_runtime(self) -> None:
        spawner = _make_spawner(self)
        spawner.runtime.harness.fleet_reuse_enabled = False
        with patch.object(
            spawner,
            "_acquire_slot",
            new_callable=AsyncMock,
        ) as acquire:
            result = asyncio.run(spawner.spawn_browser_agent(
                "Collect rows",
                fleet_id="48f0864a",
            ))

        self.assertEqual(result["status"], "invalid_fleet_routing")
        self.assertIn("fleet_reuse_enabled=true", result["error"])
        acquire.assert_not_awaited()

    def test_existing_fleet_reference_forces_fresh_inventory_sync(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class FakeClient:
                def __init__(self):
                    self.calls = []
                    self.on_event = None

                async def call(self, method, params):
                    self.calls.append(method)
                    if method == "System.register":
                        return {"data": {"fleets": []}}
                    if method == "Fleet.list":
                        return {"data": {"fleets": [{"fleetId": fleet_id}]}}
                    if method == "Page.list":
                        return {"data": {"pages": []}}
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            client = FakeClient()
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=client,
                status="idle",
                # A normal spawn would trust this fresh TTL and skip
                # Fleet.list. Explicit fleet_id must override that cache.
                last_sync_at=10**12,
            )
            spawner._slots[slot.slot_id] = slot

            async def finish_worker(**kwargs):
                result = {
                    "status": "done",
                    "workerId": kwargs["worker_id"],
                    "slotId": kwargs["slot"].slot_id,
                }
                spawner._mark_slot_idle(
                    kwargs["slot"],
                    worker_id=kwargs["worker_id"],
                )
                return result

            spawner._run_browser_worker = finish_worker
            started = await spawner.spawn_browser_agent(
                "Collect rows",
                fleet_id="48f0864a",
            )
            await spawner._handles[started["workerId"]].async_task

            self.assertEqual(started["status"], "running")
            self.assertEqual(
                started["fleetAssignment"]["assignedFleetId"],
                fleet_id,
            )
            self.assertIn("Fleet.list", client.calls)
            self.assertNotIn("Fleet.create", client.calls)

        asyncio.run(scenario())

    def test_plan_rejects_existing_fleet_conflicts(self) -> None:
        for conflicting_field, conflicting_value in (
            ("session_key", "shop:account-a"),
            ("needs_isolated_session", True),
        ):
            with self.subTest(conflicting_field=conflicting_field):
                plan = {
                    "goal": "Collect rows",
                    "task_type": "web_scrape",
                    "phases": [{
                        "id": "phase-1",
                        "type": "browser_worker",
                        "objective": "Collect rows",
                        "worker_task": "Collect rows.",
                        "stage_hint": "collection",
                        "stage_hint_reason": "Collect structured rows.",
                        "expected_artifact": {"fields": ["url"]},
                        "validators": [],
                        "worker_contract": {
                            "fleet_id": "48f0864a",
                            conflicting_field: conflicting_value,
                        },
                    }],
                }

                normalized, errors = validate_task_plan(plan)

                self.assertIsNone(normalized)
                self.assertTrue(
                    any(
                        "worker_contract.fleet_id" in error
                        and conflicting_field in error
                        for error in errors
                    ),
                    errors,
                )

    def test_click_settlement_class_uses_only_fresh_exact_ax_target(self) -> None:
        agent = SimpleNamespace(
            axtree_invalidated=False,
            axtree_page_id="page",
            axtree_ids={"1:2:3", "1:2:4"},
            axtree_nodes=[
                {"id": "1:2:3", "role": "link", "name": "Detail"},
                {"id": "1:2:4", "role": "button", "name": "Reveal"},
            ],
        )
        self.assertEqual(
            _fresh_click_settlement_class(
                agent,
                "Input.click",
                {"pageId": "page", "id": "1:2:3", "purpose": "ignore me"},
            ),
            "fresh_link",
        )
        self.assertEqual(
            _fresh_click_settlement_class(
                agent,
                "Input.click",
                {"pageId": "page", "id": "1:2:4"},
            ),
            "fresh_non_link",
        )
        agent.axtree_invalidated = True
        self.assertEqual(
            _fresh_click_settlement_class(
                agent,
                "Input.click",
                {"pageId": "page", "id": "1:2:4"},
            ),
            "conservative",
        )
        agent.axtree_invalidated = False
        self.assertEqual(
            _fresh_click_settlement_class(
                agent,
                "Input.click",
                {"pageId": "other", "id": "1:2:4"},
            ),
            "conservative",
        )
        self.assertEqual(
            _fresh_click_settlement_class(
                agent,
                "Input.click",
                {"pageId": "page", "selector": ".reveal"},
            ),
            "conservative",
        )
        self.assertEqual(
            _fresh_click_settlement_class(
                agent,
                "Input.click",
                {"pageId": "page", "x": 20, "y": 30},
            ),
            "conservative",
        )

    def test_fleet_click_gate_escape_switch_is_noisy_and_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            events = []
            runtime = RuntimeConfig(
                agent_id="test-agent",
                model=ModelConfig(provider="test", model_id="test-model"),
                browser=ABCPClientConfig(),
                harness=HarnessConfig(
                    worktree_dir=temp,
                    fleet_click_gate_enabled=False,
                ),
            )
            logger = SimpleNamespace(
                write=lambda event, payload: events.append(
                    (event, payload)
                )
            )
            spawner = BrowserAgentSpawner(
                runtime,
                logger,
                lambda *args: None,
            )
            self.assertIsNone(spawner.fleet_click_gate)
            self.assertEqual(
                [event for event, _payload in events],
                ["fleet_click_gate.disabled"],
            )

    def test_spawner_wires_popup_inventory_observation(self) -> None:
        # Opener agreement is reported as an observation, never attribution:
        # on by default so a worker learns its page set moved, still
        # configurable off for a deployment that wants silence.
        for configured, expected in ((None, True), (True, True), (False, False)):
            with self.subTest(configured=configured):
                with tempfile.TemporaryDirectory() as temp:
                    overrides = {}
                    if configured is not None:
                        overrides[
                            "fleet_click_gate_popup_inventory_observation_enabled"
                        ] = configured
                    runtime = RuntimeConfig(
                        agent_id="test-agent",
                        model=ModelConfig(provider="test", model_id="test-model"),
                        browser=ABCPClientConfig(),
                        harness=HarnessConfig(worktree_dir=temp, **overrides),
                    )
                    spawner = BrowserAgentSpawner(
                        runtime,
                        SimpleNamespace(write=lambda *_args: None),
                        lambda *args: None,
                    )
                    self.assertEqual(
                        spawner.fleet_click_gate.popup_inventory_observation_enabled,
                        expected,
                    )

    def test_delegated_notification_relay_filters_fleet_and_deduplicates(self) -> None:
        class FakeClient:
            def __init__(self):
                self.notifications = NotificationHub()

            def subscribe_notifications(self, callback):
                return self.notifications.subscribe(callback)

        spawner = _make_spawner(self)
        owner = BrowserAgentSlot(
            slot_id="slot-owner",
            agent_id="run:slot-owner:01",
            client=FakeClient(),
        )
        acting = BrowserAgentSlot(
            slot_id="slot-acting",
            agent_id="run:slot-acting:02",
            client=FakeClient(),
        )
        spawner._slots = {
            owner.slot_id: owner,
            acting.slot_id: acting,
        }
        assignment = FleetAssignment(
            worker_id="browser-001",
            slot_id=acting.slot_id,
            owner_agent_id=owner.agent_id,
            fleet_id="fleet-1",
            assignment_reason="delegated",
            owner_slot_id=owner.slot_id,
            delegated=True,
        )
        seen = []
        acting.client.notifications.subscribe(seen.append)
        spawner._ensure_notification_relay(acting, assignment)

        event = {
            "method": "System.notification",
            "params": {
                "type": "event",
                "data": {
                    "eventId": "open-1",
                    "cursor": 17,
                    "event": "Page.open",
                    "fleetId": "fleet-1",
                },
            },
        }
        other_fleet = {
            "method": "System.notification",
            "params": {
                "type": "event",
                "data": {
                    "eventId": "open-2",
                    "cursor": 18,
                    "event": "Page.open",
                    "fleetId": "fleet-2",
                },
            },
        }

        owner.client.notifications.publish(event)
        acting.client.notifications.publish_once(event)
        owner.client.notifications.publish(other_fleet)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["params"]["data"]["eventId"], "open-1")
        self.assertEqual(
            seen[0]["deliveryProvenance"]["kind"],
            "owner_relay",
        )
        self.assertFalse(
            seen[0]["deliveryProvenance"]["authoritativeForCausality"]
        )

    def test_pinned_context_requires_uuid_and_page_requires_fleet(self) -> None:
        fleet_id = "961e0e6c-b405-45ce-a68d-3796871a3133"
        page_id = "0442b698-85c8-4c8d-811c-04bf0a9948f1"
        context = PinnedBrowserContext.from_value({
            "fleet_id": fleet_id,
            "page_id": page_id,
            "source": "cli",
        })
        self.assertEqual(context.fleet_id, fleet_id)
        self.assertEqual(context.page_id, page_id)
        with self.assertRaisesRegex(ValueError, "fleet_id must be a UUID"):
            PinnedBrowserContext.from_value({"fleet_id": "not-a-fleet"})

    def test_pinned_existing_fleet_is_admitted_without_create(self) -> None:
        fleet_id = "961e0e6c-b405-45ce-a68d-3796871a3133"
        page_id = "0442b698-85c8-4c8d-811c-04bf0a9948f1"

        class NoCreateClient:
            async def call(self, method, params):
                raise AssertionError(f"unexpected browser call: {method}")

        spawner = _make_spawner(self)
        spawner.pinned_browser_context = PinnedBrowserContext(
            fleet_id=fleet_id,
            page_id=page_id,
            source="test",
        )
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="test-agent-slot-001",
            client=NoCreateClient(),
            fleet_ids={fleet_id},
            page_registry={
                page_id: {"pageId": page_id, "fleetId": fleet_id}
            },
        )
        assignment = asyncio.run(spawner._assign_fleet_for_worker(
            slot,
            worker_id="browser-001",
            worker_contract={},
            reuse_scope="page",
            page_policy="existing",
            session_key="",
            reuse_from_worker_id="",
        ))
        self.assertEqual(assignment.fleet_id, fleet_id)
        self.assertFalse(assignment.created_for_worker)
        self.assertEqual(
            assignment.assignment_reason,
            "user_pinned_existing_fleet",
        )
        self.assertEqual(
            spawner._page_bindings_for_worker(
                slot,
                assignment=assignment,
                expose_reusable_pages=True,
            ),
            {page_id: fleet_id},
        )

    def test_missing_pinned_fleet_fails_without_create(self) -> None:
        fleet_id = "961e0e6c-b405-45ce-a68d-3796871a3133"

        class NoCreateClient:
            async def call(self, method, params):
                raise AssertionError(f"unexpected browser call: {method}")

        spawner = _make_spawner(self)
        spawner.pinned_browser_context = PinnedBrowserContext(
            fleet_id=fleet_id,
            source="test",
        )
        slot = BrowserAgentSlot(
            slot_id="slot-001",
            agent_id="test-agent-slot-001",
            client=NoCreateClient(),
        )
        with self.assertRaises(FleetRoutingError) as caught:
            asyncio.run(spawner._assign_fleet_for_worker(
                slot,
                worker_id="browser-001",
                worker_contract={},
                reuse_scope="fleet",
                page_policy="new",
                session_key="",
                reuse_from_worker_id="",
            ))
        self.assertEqual(caught.exception.code, "pinned_fleet_unavailable")
        self.assertFalse(caught.exception.retryable)

    def test_admitted_fleet_owner_is_not_stolen_by_inventory_observer(self) -> None:
        fleet_id = "961e0e6c-b405-45ce-a68d-3796871a3133"
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-owner",
            owner_agent_id="agent-owner",
            fleet_ids=[fleet_id],
            admit_unbound=True,
        )
        coordinator.observe_slot(
            slot_id="slot-observer",
            owner_agent_id="agent-observer",
            fleet_ids=[fleet_id],
            admit_unbound=False,
        )
        self.assertEqual(
            coordinator.owner_slot_for_fleet(fleet_id),
            "slot-owner",
        )

    def test_pinned_page_uses_owner_slot_and_waits_when_busy(self) -> None:
        fleet_id = "961e0e6c-b405-45ce-a68d-3796871a3133"
        page_id = "0442b698-85c8-4c8d-811c-04bf0a9948f1"
        spawner = _make_spawner(self)
        spawner.pinned_browser_context = PinnedBrowserContext(
            fleet_id=fleet_id,
            page_id=page_id,
            source="test",
        )
        owner = BrowserAgentSlot(
            slot_id="slot-owner",
            agent_id="agent-owner",
            status="running",
            current_worker_id="browser-owner",
            fleet_ids={fleet_id},
            page_registry={
                page_id: {"pageId": page_id, "fleetId": fleet_id}
            },
        )
        observer = BrowserAgentSlot(
            slot_id="slot-observer",
            agent_id="agent-observer",
            status="idle",
            fleet_ids={fleet_id},
            page_registry={
                page_id: {"pageId": page_id, "fleetId": fleet_id}
            },
        )
        spawner._slots = {
            owner.slot_id: owner,
            observer.slot_id: observer,
        }
        spawner.fleet_coordinator.observe_slot(
            slot_id=owner.slot_id,
            owner_agent_id=owner.agent_id,
            fleet_ids=[fleet_id],
            admit_unbound=True,
        )
        rejection = asyncio.run(spawner._acquire_slot(
            worker_id="browser-new",
            phase_id="pinned",
            task="reuse pinned page",
            context="",
            result_contract="",
            worker_contract={},
            contract_hash="hash",
            preferred_slot_id=None,
            reuse_from_worker_id=None,
        ))
        self.assertEqual(
            rejection["status"],
            "pinned_browser_context_busy",
        )

    def test_cli_exposes_pinned_fleet_and_page(self) -> None:
        fleet_id = "961e0e6c-b405-45ce-a68d-3796871a3133"
        page_id = "0442b698-85c8-4c8d-811c-04bf0a9948f1"
        args = build_arg_parser().parse_args([
            "--fleet-id",
            fleet_id,
            "--page-id",
            page_id,
            "--task",
            "reuse the existing page",
        ])
        self.assertEqual(
            _validated_pinned_browser_context(args),
            {
                "fleet_id": fleet_id,
                "page_id": page_id,
                "source": "cli",
            },
        )

    def test_cli_page_id_requires_fleet_id(self) -> None:
        args = argparse.Namespace(
            fleet_id="",
            page_id="0442b698-85c8-4c8d-811c-04bf0a9948f1",
        )
        with self.assertRaisesRegex(ValueError, "requires --fleet-id"):
            _validated_pinned_browser_context(args)

    def test_cli_invalid_uuid_is_rejected(self) -> None:
        args = argparse.Namespace(fleet_id="not-a-uuid", page_id="")
        with self.assertRaisesRegex(ValueError, "--fleet-id must be a UUID"):
            _validated_pinned_browser_context(args)

    def test_cli_rejects_pinned_context_when_fleet_reuse_is_disabled(
        self,
    ) -> None:
        args = argparse.Namespace(
            fleet_id="961e0e6c-b405-45ce-a68d-3796871a3133",
            page_id="0442b698-85c8-4c8d-811c-04bf0a9948f1",
        )
        with self.assertRaisesRegex(
            ValueError,
            "fleet_reuse_enabled=true",
        ):
            _validated_pinned_browser_context(
                args,
                fleet_reuse_enabled=False,
            )

    def test_spawner_rejects_pin_when_fleet_reuse_is_disabled(
        self,
    ) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = RuntimeConfig(
            agent_id="test-agent",
            model=ModelConfig(provider="test", model_id="test-model"),
            browser=ABCPClientConfig(),
            harness=HarnessConfig(
                fleet_reuse_enabled=False,
                worktree_dir=temp.name,
            ),
        )
        logger = RunLogger(temp.name, task_id="pinned-config-test")

        # Legacy non-reuse mode remains valid when no pinned context exists.
        spawner = BrowserAgentSpawner(runtime, logger, lambda *args: None)
        self.assertIsNone(spawner.pinned_browser_context)

        with self.assertRaisesRegex(
            ValueError,
            "fleet_reuse_enabled=true",
        ):
            BrowserAgentSpawner(
                runtime,
                logger,
                lambda *args: None,
                pinned_browser_context={
                    "fleet_id": (
                        "961e0e6c-b405-45ce-a68d-3796871a3133"
                    ),
                    "page_id": (
                        "0442b698-85c8-4c8d-811c-04bf0a9948f1"
                    ),
                },
            )

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
        self.assertIn("collection_contract_replan_required", prompt)
        self.assertIn("worker cannot repair its own immutable contract", prompt)
        self.assertIn(
            "HARD REQUIREMENT: retain the checkpoint's validated predecessor",
            prompt,
        )
        self.assertIn(
            "A continuation that newly proves a reusable candidate upgrades",
            prompt,
        )
        self.assertIn("remediation cannot bind a checkpoint", prompt)

    def test_lead_prompt_declares_nested_collection_contract_shape(self) -> None:
        from agent_harness import LeadAgent

        agent = object.__new__(LeadAgent)
        agent.strategy_bank = {}
        agent.static_context_block = ""
        prompt = agent._build_system_prompt()

        self.assertIn(
            '{"name":"reviews","type":"array","items":{"required":',
            prompt,
        )
        self.assertIn(
            "Do not describe nested item fields as top-level artifact fields",
            prompt,
        )

    def test_lead_prompt_prefers_live_batched_dom_get_img_in_page_phase(self) -> None:
        from agent_harness import LeadAgent
        from harness.tool_policy import filter_capability_methods_for_task_type

        agent = object.__new__(LeadAgent)
        agent.strategy_bank = {}
        agent.static_context_block = ""
        prompt = agent._build_system_prompt()

        self.assertIn(
            "DOM.getImg is present in the live capability set exposed for that"
            " phase's task_type",
            prompt,
        )
        self.assertIn(
            "keep the export in the page-owning phase and instruct one batched"
            " DOM.getImg call before leaving the page",
            prompt,
        )
        self.assertIn(
            "Do not mechanically split that image export into an image-URL"
            " artifact followed by a separate Download.start phase",
            prompt,
        )
        self.assertIn("image_exported and file_integrity", prompt)
        self.assertEqual(
            filter_capability_methods_for_task_type(
                {"DOM.getImg", "Download.start"},
                "web_scrape",
            ),
            {"DOM.getImg"},
        )

    def test_browser_prompt_routes_materialization_to_composite_tools(self) -> None:
        from agent_harness import BrowserAgent

        agent = object.__new__(BrowserAgent)
        agent.capability_methods = set()
        agent.capabilities = []
        agent.method_schemas = {}
        agent.methods_requiring_purpose = set()
        agent.purpose_hints = {}
        agent.skills_doc = ""
        agent.worker_contract = {"task_type": "web_scrape"}
        agent.static_context_block = ""

        prompt = agent._build_system_prompt()

        self.assertIn(
            "DOM.getAXTree to enumerate stable canonical ids",
            prompt,
        )
        self.assertIn("one native batched DOM.getAttribute", prompt)
        self.assertIn("call dismiss_overlay once", prompt)
        self.assertNotIn(
            "refresh DOM.getSemanticTree after each bounded container scroll",
            prompt,
        )

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

    def test_task_fleet_budget_counts_only_this_task_fleets(self) -> None:
        coordinator = FleetCoordinator()
        coordinator.observe_slot(
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            # Fleet.list inventory is Agent-global: fleet-foreign belongs to
            # another task on the same connection and must not spend this
            # task's budget or become a reuse candidate.
            fleet_ids=["fleet-generic", "fleet-named", "fleet-foreign"],
            admit_unbound=False,
        )
        coordinator.bind_assignment(
            worker_id="browser-001",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-generic",
            assignment_reason="isolated_session",
            reuse_scope="connection",
            page_policy="new",
            created_for_worker=True,
            is_isolated=True,
        )
        coordinator.bind_assignment(
            worker_id="browser-002",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-named",
            assignment_reason="session_bootstrap",
            reuse_scope="fleet",
            page_policy="new",
            session_key="shop:A",
            created_for_worker=True,
        )

        self.assertEqual(
            coordinator.task_fleet_ids(),
            {"fleet-generic", "fleet-named"},
        )

        capped = coordinator.choose_under_cap(
            worker_id="browser-003",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=[
                "fleet-generic",
                "fleet-named",
                "fleet-foreign",
            ],
            reuse_scope="connection",
            page_policy="new",
        )
        # Isolation yields to the ceiling; a named cookie jar never does.
        self.assertEqual(capped.fleet_id, "fleet-generic")
        self.assertEqual(capped.assignment_reason, "task_fleet_cap_reuse")
        self.assertFalse(capped.created_for_worker)

        # A busy fleet only ranks last; with nothing else reusable it is still
        # handed back rather than refused.
        self.assertEqual(
            coordinator.choose_under_cap(
                worker_id="browser-004",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                candidate_fleet_ids=[
                    "fleet-generic",
                    "fleet-named",
                    "fleet-foreign",
                ],
                reuse_scope="connection",
                page_policy="new",
                busy_fleet_ids=["fleet-generic"],
            ).fleet_id,
            "fleet-generic",
        )

        # Nothing of this task's own is reusable: fleet-foreign belongs to
        # another task and a named cookie jar is never lent out.
        self.assertIsNone(coordinator.choose_under_cap(
            worker_id="browser-005",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            candidate_fleet_ids=["fleet-named", "fleet-foreign"],
            reuse_scope="connection",
            page_policy="new",
        ))

    def test_task_fleet_cap_reuses_instead_of_creating_another_fleet(self) -> None:
        async def scenario() -> None:
            created: List[str] = []

            class CreatingClient:
                async def call(self, method, params):
                    if method != "Fleet.create":
                        raise AssertionError(
                            f"unexpected browser call: {method}"
                        )
                    fleet_id = f"fleet-{len(created) + 1}"
                    created.append(fleet_id)
                    return {"data": {"fleetId": fleet_id}}

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 2
            spawner.runtime.harness.worker_session_isolation_enabled = True
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=CreatingClient(),
            )
            spawner._slots[slot.slot_id] = slot

            assignments = []
            for index in (1, 2, 3):
                contract = spawner._apply_worker_session_isolation(
                    {},
                    phase_id=f"phase-{index}",
                    session_key="",
                    fleet_reference="",
                    reuse_from_worker_id="",
                )
                assignments.append(await spawner._assign_fleet_for_worker(
                    slot,
                    worker_id=f"browser-00{index}",
                    worker_contract=contract,
                    reuse_scope="connection",
                    page_policy="new",
                    session_key="",
                    reuse_from_worker_id="",
                    isolation_auto_applied=bool(
                        contract.get("needs_isolated_session")
                    ),
                ))

            # Three sequential workers, two fleets: the third one reuses rather
            # than opening a browser instance nobody will ever close.
            self.assertEqual(created, ["fleet-1", "fleet-2"])
            self.assertEqual(
                [item.assignment_reason for item in assignments],
                ["isolated_session", "isolated_session", "task_fleet_cap_reuse"],
            )
            self.assertIn(assignments[2].fleet_id, created)
            self.assertFalse(assignments[2].created_for_worker)
            self.assertEqual(
                spawner.fleet_coordinator.task_fleet_ids(),
                set(created),
            )

        asyncio.run(scenario())

    def test_concurrent_spawns_cannot_overshoot_the_task_fleet_cap(self) -> None:
        """The budget is one shared counter, so its decision must serialize."""

        async def scenario() -> None:
            created: List[str] = []

            class SlowCreatingClient:
                async def call(self, method, params):
                    if method != "Fleet.create":
                        raise AssertionError(
                            f"unexpected browser call: {method}"
                        )
                    # Yield inside the RPC: without one lock over the whole
                    # decision every racer would read "under the cap" here.
                    await asyncio.sleep(0.01)
                    fleet_id = f"fleet-{len(created) + 1}"
                    created.append(fleet_id)
                    return {"data": {"fleetId": fleet_id}}

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 2
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=SlowCreatingClient(),
            )
            spawner._slots[slot.slot_id] = slot

            assignments = await asyncio.gather(*[
                spawner._assign_fleet_for_worker(
                    slot,
                    worker_id=f"browser-00{index}",
                    worker_contract={"needs_isolated_session": True},
                    reuse_scope="connection",
                    page_policy="new",
                    session_key="",
                    reuse_from_worker_id="",
                    # Deployment-default isolation, so the loser degrades to
                    # reuse instead of failing on an identity boundary.
                    isolation_auto_applied=True,
                )
                for index in (1, 2, 3)
            ])

            self.assertEqual(created, ["fleet-1", "fleet-2"])
            self.assertEqual(
                sorted(
                    item.assignment_reason for item in assignments
                ),
                ["isolated_session", "isolated_session", "task_fleet_cap_reuse"],
            )

        asyncio.run(scenario())

    def test_full_cap_prefers_an_idle_fleet_but_still_reuses_a_busy_one(self) -> None:
        """The cap must not be stricter than the routing it degrades from."""

        async def scenario() -> None:
            class NoCreateClient:
                async def call(self, method, params):
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 2
            holder = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=NoCreateClient(),
                fleet_ids={"fleet-busy", "fleet-idle"},
                status="running",
                current_worker_id="browser-001",
            )
            spawner._slots[holder.slot_id] = holder
            for worker_id, fleet_id in (
                ("browser-001", "fleet-busy"),
                ("browser-002", "fleet-idle"),
            ):
                spawner.fleet_coordinator.bind_assignment(
                    worker_id=worker_id,
                    slot_id="slot-001",
                    owner_agent_id="agent-slot-001",
                    fleet_id=fleet_id,
                    assignment_reason="isolated_session",
                    reuse_scope="connection",
                    page_policy="new",
                    created_for_worker=True,
                    is_isolated=True,
                )

            async def assign_generic_worker(worker_id: str):
                return await spawner._assign_fleet_for_worker(
                    holder,
                    worker_id=worker_id,
                    worker_contract={},
                    reuse_scope="connection",
                    page_policy="new",
                    session_key="",
                    reuse_from_worker_id="",
                )

            # browser-001 is still running on fleet-busy, so the spare fleet
            # absorbs the newcomer instead of doubling up.
            idle_first = await assign_generic_worker("browser-003")
            self.assertEqual(idle_first.fleet_id, "fleet-idle")
            self.assertEqual(
                idle_first.assignment_reason,
                "task_fleet_cap_reuse",
            )

            # With every task fleet held by a live worker the cap still hands
            # one back: ordinary routing already shares a fleet between live
            # workers, so rejecting here would be stricter than the rule this
            # path degrades from.
            holder.current_worker_id = "browser-003"
            spawner._slots["slot-002"] = BrowserAgentSlot(
                slot_id="slot-002",
                agent_id="agent-slot-002",
                client=NoCreateClient(),
                fleet_ids={"fleet-busy", "fleet-idle"},
                status="running",
                current_worker_id="browser-001",
            )
            all_busy = await assign_generic_worker("browser-004")
            self.assertIn(all_busy.fleet_id, {"fleet-busy", "fleet-idle"})
            self.assertEqual(
                all_busy.assignment_reason,
                "task_fleet_cap_reuse",
            )

        asyncio.run(scenario())

    def test_cap_refresh_finds_a_task_fleet_another_slot_created(self) -> None:
        """slot.fleet_ids is a 30s cache; refuse only against a fresh read."""

        async def scenario() -> None:
            class InventoryClient:
                def __init__(self) -> None:
                    self.calls: List[str] = []

                async def call(self, method, params):
                    self.calls.append(method)
                    if method == "Fleet.list":
                        return {
                            "data": {"fleets": [{"fleetId": "fleet-1"}]}
                        }
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            owner = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=InventoryClient(),
                fleet_ids={"fleet-1"},
                status="running",
                current_worker_id="browser-001",
            )
            spawner._slots[owner.slot_id] = owner
            spawner.fleet_coordinator.bind_assignment(
                worker_id="browser-001",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id="fleet-1",
                assignment_reason="slot_bootstrap",
                reuse_scope="connection",
                page_policy="new",
                created_for_worker=True,
            )
            acting = BrowserAgentSlot(
                slot_id="slot-002",
                agent_id="agent-slot-002",
                client=InventoryClient(),
                # Synced before slot-001 created the fleet, and the TTL has not
                # expired — the task fleet is simply not in this cache yet.
                fleet_ids=set(),
                last_sync_at=10**12,
            )
            spawner._slots[acting.slot_id] = acting

            assignment = await spawner._assign_fleet_for_worker(
                acting,
                worker_id="browser-002",
                worker_contract={},
                reuse_scope="connection",
                page_policy="new",
                session_key="",
                reuse_from_worker_id="",
            )

            self.assertEqual(assignment.fleet_id, "fleet-1")
            self.assertEqual(
                assignment.assignment_reason,
                "task_fleet_cap_reuse",
            )
            # Reused across slots through the owner, not re-owned by slot-002.
            self.assertTrue(assignment.delegated)
            self.assertEqual(assignment.owner_slot_id, "slot-001")
            self.assertIn("Fleet.list", acting.client.calls)
            self.assertNotIn("Fleet.create", acting.client.calls)

        asyncio.run(scenario())

    def test_cap_refresh_releases_a_fleet_another_slot_owned(self) -> None:
        """One Fleet.list is a full table read, so absence retires any owner."""

        async def scenario() -> None:
            created: List[str] = []

            class InventoryClient:
                async def call(self, method, params):
                    if method == "Fleet.list":
                        # fleet-1 is gone platform-wide, not just from slot-002.
                        return {"data": {"fleets": []}}
                    if method == "Fleet.create":
                        created.append("fleet-2")
                        return {"data": {"fleetId": "fleet-2"}}
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            owner = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=InventoryClient(),
                fleet_ids={"fleet-1"},
            )
            spawner._slots[owner.slot_id] = owner
            spawner.fleet_coordinator.bind_assignment(
                worker_id="browser-001",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id="fleet-1",
                assignment_reason="slot_bootstrap",
                reuse_scope="connection",
                page_policy="new",
                created_for_worker=True,
            )
            acting = BrowserAgentSlot(
                slot_id="slot-002",
                agent_id="agent-slot-002",
                client=InventoryClient(),
                fleet_ids=set(),
                last_sync_at=10**12,
            )
            spawner._slots[acting.slot_id] = acting
            self.assertTrue(spawner._task_fleet_budget_exhausted())

            assignment = await spawner._assign_fleet_for_worker(
                acting,
                worker_id="browser-002",
                worker_contract={},
                reuse_scope="connection",
                page_policy="new",
                session_key="",
                reuse_from_worker_id="",
            )

            # slot-002 owns none of slot-001's records, but its authoritative
            # read still proves the fleet is gone, so the budget comes back.
            self.assertEqual(created, ["fleet-2"])
            self.assertEqual(assignment.fleet_id, "fleet-2")
            self.assertTrue(assignment.created_for_worker)
            self.assertEqual(
                spawner.fleet_coordinator.task_fleet_ids(),
                {"fleet-2"},
            )

        asyncio.run(scenario())

    def test_lost_inventory_read_never_retires_another_slots_fleet(self) -> None:
        """A failed Fleet.list is not evidence that anything disappeared."""

        async def scenario() -> None:
            class BrokenListClient:
                async def call(self, method, params):
                    if method == "Fleet.list":
                        raise ABCPTransportError("connection reset")
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            owner = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=BrokenListClient(),
                fleet_ids={"fleet-1"},
            )
            spawner._slots[owner.slot_id] = owner
            spawner.fleet_coordinator.bind_assignment(
                worker_id="browser-001",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id="fleet-1",
                assignment_reason="slot_bootstrap",
                reuse_scope="connection",
                page_policy="new",
                created_for_worker=True,
            )
            acting = BrowserAgentSlot(
                slot_id="slot-002",
                agent_id="agent-slot-002",
                client=BrokenListClient(),
                fleet_ids=set(),
                last_sync_at=10**12,
            )
            spawner._slots[acting.slot_id] = acting

            with self.assertRaises(FleetRoutingError) as caught:
                await spawner._assign_fleet_for_worker(
                    acting,
                    worker_id="browser-002",
                    worker_contract={},
                    reuse_scope="connection",
                    page_policy="new",
                    session_key="",
                    reuse_from_worker_id="",
                )

            # Fail closed instead of inventing budget from a broken read. The
            # recorded sync error is what the retirement guard reads; had it
            # retired fleet-1 anyway, this spawn would have gone on to call
            # Fleet.create on the same broken client.
            self.assertEqual(
                caught.exception.code,
                "task_fleet_limit_reached",
            )
            self.assertTrue(any(
                str(error).startswith("Fleet.list")
                for error in acting.sync_errors
            ))
            self.assertEqual(
                spawner.fleet_coordinator.task_fleet_ids(),
                {"fleet-1"},
            )

        asyncio.run(scenario())

    def test_full_cap_fails_closed_when_every_task_fleet_is_a_named_session(self) -> None:
        async def scenario() -> None:
            class NoCreateClient:
                async def call(self, method, params):
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=NoCreateClient(),
                fleet_ids={"fleet-named"},
            )
            spawner._slots[slot.slot_id] = slot
            spawner.fleet_coordinator.bind_assignment(
                worker_id="browser-001",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id="fleet-named",
                assignment_reason="session_bootstrap",
                reuse_scope="fleet",
                page_policy="new",
                session_key="shop:A",
                created_for_worker=True,
            )

            with self.assertRaises(FleetRoutingError) as caught:
                await spawner._assign_fleet_for_worker(
                    slot,
                    worker_id="browser-002",
                    worker_contract={},
                    reuse_scope="connection",
                    page_policy="new",
                    session_key="",
                    reuse_from_worker_id="",
                )

            # A logged-in cookie jar is the one thing the cap will not lend to
            # a generic worker, so there is nothing left to reuse.
            self.assertEqual(
                caught.exception.code,
                "task_fleet_limit_reached",
            )
            self.assertTrue(caught.exception.retryable)
            self.assertFalse(caught.exception.details["needsIsolatedSession"])

        asyncio.run(scenario())

    def test_a_fleet_lost_from_owner_inventory_releases_its_budget(self) -> None:
        async def scenario() -> None:
            created: List[str] = []

            class CreatingClient:
                async def call(self, method, params):
                    if method == "Fleet.list":
                        # The authoritative view confirms the fleet is gone.
                        return {"data": {"fleets": []}}
                    if method != "Fleet.create":
                        raise AssertionError(
                            f"unexpected browser call: {method}"
                        )
                    fleet_id = f"fleet-{len(created) + 2}"
                    created.append(fleet_id)
                    return {"data": {"fleetId": fleet_id}}

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=CreatingClient(),
                fleet_ids={"fleet-1"},
            )
            spawner._slots[slot.slot_id] = slot
            spawner.fleet_coordinator.bind_assignment(
                worker_id="browser-001",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id="fleet-1",
                assignment_reason="slot_bootstrap",
                reuse_scope="connection",
                page_policy="new",
                created_for_worker=True,
            )
            self.assertTrue(spawner._task_fleet_budget_exhausted())

            # The authoritative owner inventory stops reporting the fleet: it
            # is gone, so it cannot keep spending the task's budget.
            slot.fleet_ids = set()

            assignment = await spawner._assign_fleet_for_worker(
                slot,
                worker_id="browser-002",
                worker_contract={},
                reuse_scope="connection",
                page_policy="new",
                session_key="",
                reuse_from_worker_id="",
            )

            self.assertEqual(created, ["fleet-2"])
            self.assertEqual(assignment.fleet_id, "fleet-2")
            self.assertTrue(assignment.created_for_worker)
            self.assertEqual(
                spawner.fleet_coordinator.task_fleet_ids(),
                {"fleet-2"},
            )

        asyncio.run(scenario())

    def test_zero_task_fleet_cap_keeps_the_previous_behavior(self) -> None:
        async def scenario() -> None:
            created: List[str] = []

            class CreatingClient:
                async def call(self, method, params):
                    if method != "Fleet.create":
                        raise AssertionError(
                            f"unexpected browser call: {method}"
                        )
                    fleet_id = f"fleet-{len(created) + 1}"
                    created.append(fleet_id)
                    return {"data": {"fleetId": fleet_id}}

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 0
            spawner.runtime.harness.worker_session_isolation_enabled = True
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=CreatingClient(),
            )
            spawner._slots[slot.slot_id] = slot

            for index in (1, 2, 3):
                contract = spawner._apply_worker_session_isolation(
                    {},
                    phase_id=f"phase-{index}",
                    session_key="",
                    fleet_reference="",
                    reuse_from_worker_id="",
                )
                self.assertTrue(contract["needs_isolated_session"])
                await spawner._assign_fleet_for_worker(
                    slot,
                    worker_id=f"browser-00{index}",
                    worker_contract=contract,
                    reuse_scope="connection",
                    page_policy="new",
                    session_key="",
                    reuse_from_worker_id="",
                    isolation_auto_applied=True,
                )

            self.assertEqual(created, ["fleet-1", "fleet-2", "fleet-3"])

        asyncio.run(scenario())

    def test_spawned_workers_stop_creating_fleets_at_the_cap(self) -> None:
        """End-to-end spawn wiring: two spawns, one browser instance."""

        async def scenario() -> None:
            created: List[str] = []

            class FakeClient:
                def __init__(self) -> None:
                    self.calls: List[str] = []
                    self.on_event = None

                async def call(self, method, params):
                    self.calls.append(method)
                    if method == "System.register":
                        return {
                            "data": {
                                "fleets": [
                                    {"fleetId": item} for item in created
                                ]
                            }
                        }
                    if method == "Fleet.list":
                        return {
                            "data": {
                                "fleets": [
                                    {"fleetId": item} for item in created
                                ]
                            }
                        }
                    if method == "Page.list":
                        return {"data": {"pages": []}}
                    if method == "Fleet.create":
                        fleet_id = f"fleet-{len(created) + 1}"
                        created.append(fleet_id)
                        return {"data": {"fleetId": fleet_id}}
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            spawner.runtime.harness.worker_session_isolation_enabled = True
            client = FakeClient()
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=client,
                status="idle",
            )
            spawner._slots[slot.slot_id] = slot

            async def finish_worker(**kwargs):
                spawner._mark_slot_idle(
                    kwargs["slot"],
                    worker_id=kwargs["worker_id"],
                )
                return {"status": "done", "workerId": kwargs["worker_id"]}

            spawner._run_browser_worker = finish_worker

            spawns = []
            for index in (1, 2):
                started = await spawner.spawn_browser_agent(
                    f"Collect page {index}",
                )
                await spawner._handles[started["workerId"]].async_task
                spawns.append(started)

            self.assertEqual(created, ["fleet-1"])
            self.assertEqual(client.calls.count("Fleet.create"), 1)
            self.assertEqual(
                spawns[0]["fleetAssignment"]["assignmentReason"],
                "isolated_session",
            )
            self.assertEqual(
                spawns[1]["fleetAssignment"]["assignmentReason"],
                "task_fleet_cap_reuse",
            )
            self.assertEqual(
                spawns[1]["fleetAssignment"]["assignedFleetId"],
                "fleet-1",
            )

        asyncio.run(scenario())

    def test_task_fleet_cap_never_blocks_an_explicit_fleet_reference(self) -> None:
        async def scenario() -> None:
            first = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"
            second = "961e0e6c-b405-45ce-a68d-3796871a3133"

            class NoCreateClient:
                async def call(self, method, params):
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=NoCreateClient(),
                fleet_ids={first, second},
            )
            spawner._slots[slot.slot_id] = slot
            spawner.fleet_coordinator.bind_assignment(
                worker_id="browser-001",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id=first,
                assignment_reason="slot_bootstrap",
                reuse_scope="connection",
                page_policy="new",
                created_for_worker=True,
            )

            assignment = await spawner._assign_fleet_for_worker(
                slot,
                worker_id="browser-002",
                worker_contract={},
                reuse_scope="fleet",
                page_policy="new",
                session_key="",
                reuse_from_worker_id="",
                fleet_id="961e0e6c",
            )

            # An explicitly selected fleet is honored past the ceiling — it
            # adopts an existing browser instead of opening one — and it does
            # spend budget, so nothing fresh can follow it.
            self.assertEqual(assignment.fleet_id, second)
            self.assertEqual(
                assignment.assignment_reason,
                "explicit_fleet_reference",
            )
            self.assertEqual(
                spawner.fleet_coordinator.task_fleet_ids(),
                {first, second},
            )

        asyncio.run(scenario())

    def test_task_fleet_cap_fails_closed_on_a_declared_identity_boundary(self) -> None:
        async def scenario() -> None:
            class NoCreateClient:
                async def call(self, method, params):
                    raise AssertionError(f"unexpected browser call: {method}")

            spawner = _make_spawner(self)
            spawner.runtime.harness.max_task_fleets = 1
            slot = BrowserAgentSlot(
                slot_id="slot-001",
                agent_id="agent-slot-001",
                client=NoCreateClient(),
                fleet_ids={"fleet-1"},
            )
            spawner._slots[slot.slot_id] = slot
            spawner.fleet_coordinator.bind_assignment(
                worker_id="browser-001",
                slot_id="slot-001",
                owner_agent_id="agent-slot-001",
                fleet_id="fleet-1",
                assignment_reason="slot_bootstrap",
                reuse_scope="connection",
                page_policy="new",
                created_for_worker=True,
            )

            for worker_id, kwargs in (
                ("browser-002", {"session_key": "shop:A"}),
                (
                    "browser-003",
                    {
                        "worker_contract": {"needs_isolated_session": True},
                        "isolation_auto_applied": False,
                    },
                ),
            ):
                with self.assertRaises(FleetRoutingError) as caught:
                    await spawner._assign_fleet_for_worker(
                        slot,
                        worker_id=worker_id,
                        worker_contract=kwargs.pop("worker_contract", {}),
                        reuse_scope="connection",
                        page_policy="new",
                        session_key=kwargs.pop("session_key", ""),
                        reuse_from_worker_id="",
                        **kwargs,
                    )
                # A login identity is never merged into a used cookie jar just
                # to respect the ceiling; the Lead is told to wait instead.
                self.assertEqual(
                    caught.exception.code,
                    "task_fleet_limit_reached",
                )
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(caught.exception.details["maxTaskFleets"], 1)
                self.assertIn(
                    "max_task_fleets",
                    caught.exception.next_instruction,
                )

        asyncio.run(scenario())

    def test_task_fleet_cap_stops_deployment_default_isolation(self) -> None:
        spawner = _make_spawner(self)
        spawner.runtime.harness.max_task_fleets = 1
        spawner.runtime.harness.worker_session_isolation_enabled = True

        under_budget = spawner._apply_worker_session_isolation(
            {},
            phase_id="phase-1",
            session_key="",
            fleet_reference="",
            reuse_from_worker_id="",
        )
        self.assertTrue(under_budget["needs_isolated_session"])

        spawner.fleet_coordinator.bind_assignment(
            worker_id="browser-001",
            slot_id="slot-001",
            owner_agent_id="agent-slot-001",
            fleet_id="fleet-1",
            assignment_reason="isolated_session",
            reuse_scope="connection",
            page_policy="new",
            created_for_worker=True,
            is_isolated=True,
        )

        at_budget = spawner._apply_worker_session_isolation(
            {},
            phase_id="phase-2",
            session_key="",
            fleet_reference="",
            reuse_from_worker_id="",
        )
        # The deployment preference is dropped, not converted into a rejection:
        # a generic worker can still be served by an existing fleet.
        self.assertNotIn("needs_isolated_session", at_budget)

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
        # Page.list is observable for every assignment: hiding the Fleet made a
        # worker unable to tell "my action did nothing" from "my result opened
        # in a tab I may not look at". Delegation is enforced per pageId below,
        # not by blinding the listing.
        self.assertIsNone(_check_page_binding(agent, "Page.list", list_params))
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

    def test_pinned_page_cannot_be_replaced_or_closed(self) -> None:
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            assigned_fleet_id="fleet-a",
            allowed_fleet_ids={"fleet-a"},
            allowed_page_ids={"page-pinned"},
            page_fleet_ids={"page-pinned": "fleet-a"},
            fleet_assignment_reason="user_pinned_existing_fleet",
            pinned_page_id="page-pinned",
        )

        rejection, _ = _apply_fleet_binding(
            agent,
            "Page.create",
            {"fleetId": "fleet-a", "url": "https://example.com"},
        )
        self.assertEqual(
            rejection["status"],
            "pinned_browser_context_violation",
        )
        self.assertFalse(rejection["tool_was_executed"])

        rejection, _ = _apply_fleet_binding(
            agent,
            "Page.close",
            {"pageId": "page-pinned"},
        )
        self.assertEqual(
            rejection["status"],
            "pinned_browser_context_violation",
        )

        rejection, _ = _apply_fleet_binding(
            agent,
            "Page.navigate",
            {"pageId": "page-pinned", "url": "https://example.com"},
        )
        self.assertIsNone(rejection)

    def test_pinned_fleet_without_page_allows_page_create(self) -> None:
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            assigned_fleet_id="fleet-a",
            allowed_fleet_ids={"fleet-a"},
            fleet_assignment_reason="user_pinned_existing_fleet",
            pinned_page_id="",
        )
        params = {"url": "https://example.com"}
        rejection, receipt = _apply_fleet_binding(
            agent,
            "Page.create",
            params,
        )
        self.assertIsNone(rejection)
        self.assertEqual(params["fleetId"], "fleet-a")
        self.assertTrue(receipt["fleetInjected"])

    def test_unheld_fleet_page_is_claimed_on_first_use(self) -> None:
        """The 1688 failure, end to end.

        A search submitted with Enter renders in a tab the worker never
        created. No ABCP signal says which action caused which tab, so the
        worker is allowed to drive any page in its own Fleet that no other
        worker holds — discovered through Page.list, claimed on first touch.
        """

        leases = PageLeaseManager()
        leases.seed_worker_pages(
            "worker-other", {"page-other-worker": "fleet-a"}
        )
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            logger=SimpleNamespace(write=lambda *_args, **_kwargs: None),
            assigned_fleet_id="fleet-a",
            allowed_fleet_ids={"fleet-a"},
            allowed_page_ids={"page-own"},
            page_fleet_ids={"page-own": "fleet-a"},
            page_lease_manager=leases,
            worker_id="worker-a",
        )
        response, receipt = _filter_page_list_response(
            agent,
            {"data": {"pages": [
                {"pageId": "page-own", "fleetId": "fleet-a"},
                {"pageId": "page-results", "fleetId": "fleet-a"},
                {"pageId": "page-other-worker", "fleetId": "fleet-a"},
            ]}},
        )
        self.assertEqual(
            {
                row["pageId"]: (row["delegated"], row["claimable"])
                for row in response["data"]["pages"]
            },
            {
                "page-own": (True, False),
                "page-results": (False, True),
                "page-other-worker": (False, False),
            },
        )
        self.assertEqual(receipt["claimablePageCount"], 1)
        self.assertEqual(receipt["heldByOtherWorkerCount"], 1)

        # Addressing the free page is enough to take it over...
        self.assertIsNone(
            _check_page_binding(agent, "Page.getState", {"pageId": "page-results"})
        )
        self.assertNotIn("page-results", agent.allowed_page_ids)

        class FakeClient:
            async def call(self, method, params=None):
                return {"data": {"pageId": params.get("pageId")}}

        browser = PageLeasedBrowserClient(
            FakeClient(),
            leases,
            assigned_fleet_id="fleet-a",
            worker_id="worker-a",
        )
        result = asyncio.run(browser.call(
            "Page.getState", {"pageId": "page-results"}
        ))
        self.assertEqual(leases.owner_for("page-results"), "worker-a")
        _observe_page_binding_after(
            agent,
            "Page.getState",
            {"pageId": "page-results"},
            {"method": "Page.getState", "response": result},
        )
        self.assertIn("page-results", agent.allowed_page_ids)
        # ...while another worker's page stays rejected however often it is tried.
        rejection = _check_page_binding(
            agent, "Page.getState", {"pageId": "page-other-worker"}
        )
        self.assertEqual(rejection["status"], "page_busy")
        self.assertNotIn("page-other-worker", agent.allowed_page_ids)

    def test_unseen_or_foreign_fleet_page_is_not_claimable(self) -> None:
        """Claiming requires having seen the page in this Fleet's listing, so a
        guessed or cross-Fleet pageId cannot be taken over."""

        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            logger=SimpleNamespace(write=lambda *_args, **_kwargs: None),
            assigned_fleet_id="fleet-a",
            allowed_fleet_ids={"fleet-a"},
            allowed_page_ids={"page-own"},
            page_fleet_ids={"page-own": "fleet-a"},
        )
        guessed = _check_page_binding(agent, "Page.getState", {"pageId": "page-guess"})
        self.assertEqual(guessed["status"], "page_binding_violation")

        _filter_page_list_response(
            agent,
            {"data": {"pages": [{"pageId": "page-elsewhere", "fleetId": "fleet-z"}]}},
        )
        cross = _check_page_binding(
            agent, "Page.getState", {"pageId": "page-elsewhere"}
        )
        self.assertEqual(cross["status"], "page_binding_violation")

    def test_page_list_reports_delegation_and_claimability(self) -> None:
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
        # Both same-Fleet rows stay visible; the flags say what may be driven.
        self.assertEqual(
            [page["pageId"] for page in response["data"]["pages"]],
            ["page-allowed", "page-hidden"],
        )
        self.assertEqual(
            {
                page["pageId"]: (page["delegated"], page["claimable"])
                for page in response["data"]["pages"]
            },
            {"page-allowed": (True, False), "page-hidden": (False, True)},
        )
        self.assertEqual(receipt["delegatedPageCount"], 1)
        self.assertEqual(receipt["claimablePageCount"], 1)
        self.assertEqual(receipt["heldByOtherWorkerCount"], 0)
        self.assertEqual(receipt["hiddenPageCount"], 0)

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

    def test_disabled_fleet_reuse_page_list_still_discharge_shown_rows(self) -> None:
        signal = PageInventorySignal()
        signal.observe_opened("fleet-a", "page-popup")
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=False)
            ),
            assigned_fleet_id="fleet-a",
            page_inventory_signal=signal,
            page_lease_manager=None,
            worker_id="worker-a",
        )
        raw_response = {
            "observation": "Retrieved one page.",
            "data": [
                {"pageId": "page-popup", "fleetId": "fleet-a"},
            ],
        }

        response, private_receipt = _filter_page_list_response(
            agent, raw_response
        )
        shown = private_receipt.pop("_shownInventoryPages", None)

        self.assertIs(response, raw_response)
        self.assertEqual(
            shown,
            [{"pageId": "page-popup", "fleetId": "fleet-a"}],
        )
        self.assertEqual(private_receipt, {})

        result = _settle_page_inventory_signal(
            agent,
            "Page.list",
            {},
            {"response": response},
            page_list_shown=shown,
        )
        self.assertEqual(signal.pending_for("fleet-a"), set())
        self.assertNotIn("_shownInventoryPages", repr(result))

    def test_page_list_fails_closed_for_missing_fleet_and_quarantine(self) -> None:
        leases = PageLeaseManager()
        leases.quarantine_page("page-paused")
        agent = SimpleNamespace(
            runtime=SimpleNamespace(
                harness=SimpleNamespace(fleet_reuse_enabled=True)
            ),
            assigned_fleet_id="fleet-a",
            allowed_fleet_ids={"fleet-a"},
            allowed_page_ids=set(),
            page_fleet_ids={},
            page_lease_manager=leases,
            worker_id="worker-a",
        )
        response, receipt = _filter_page_list_response(
            agent,
            {"data": {"pages": [
                {"pageId": "page-missing-fleet"},
                {"fleetId": "fleet-a"},
                {"pageId": "page-foreign", "fleetId": "fleet-b"},
                {"pageId": "page-paused", "fleetId": "fleet-a"},
                {"pageId": "page-free", "fleetId": "fleet-a"},
            ]}},
        )
        self.assertEqual(
            [row["pageId"] for row in response["data"]["pages"]],
            ["page-paused", "page-free"],
        )
        paused, free = response["data"]["pages"]
        self.assertTrue(paused["quarantined"])
        self.assertFalse(paused["claimable"])
        self.assertFalse(free["quarantined"])
        self.assertTrue(free["claimable"])
        self.assertEqual(receipt["hiddenPageCount"], 3)
        rejection = _check_page_binding(
            agent, "Page.getState", {"pageId": "page-paused"}
        )
        self.assertEqual(rejection["status"], "page_quarantined")
        self.assertEqual(leases.fleet_for("page-missing-fleet"), "")
        self.assertEqual(leases.fleet_for("page-foreign"), "")

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
            workflow_runtime=SimpleNamespace(
                harness=SimpleNamespace(workflow_execution_enabled=True),
            ),
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

    def test_spawn_acquisition_failure_budget_survives_cancelled_attempts(self) -> None:
        spawner = _make_spawner(self)
        phase = {
            "id": "p1",
            "type": "browser_worker",
            "objective": "Collect one row",
            "worker_task": "Collect one row from https://example.test/items.",
            "stage_hint": "collection",
            "stage_hint_reason": "Collect one repeated structured row from the page.",
            "expected_artifact": {"name": "rows", "exact_rows": 1},
            "validators": [],
            "validators_normalized": True,
            "worker_contract": {},
            "max_attempts": 3,
        }
        plan = {
            "version": "v1", "goal": "Collect one row",
            "task_type": "web_scrape", "phases": [phase],
        }
        initialize_task_state(spawner.logger, plan)
        calls = {"count": 0}

        async def failing_acquire(**kwargs):
            calls["count"] += 1
            raise ValueError("Invalid IPv6 URL")

        spawner._acquire_slot = failing_acquire  # type: ignore[method-assign]

        first = asyncio.run(spawner.spawn_browser_agent(
            task=phase["worker_task"], phase_id="p1", worker_contract={},
            phase=phase, task_plan=plan,
        ))
        second = asyncio.run(spawner.spawn_browser_agent(
            task=phase["worker_task"], phase_id="p1", worker_contract={},
            phase=phase, task_plan=plan,
        ))
        # A fresh phase id must not reopen the identical objective+routing
        # acquisition path.
        preserved = load_task_state(spawner.logger)
        replanned_phase = dict(phase)
        replanned_phase["id"] = "p2"
        replanned_plan = {**plan, "phases": [replanned_phase]}
        initialize_task_state(
            spawner.logger, replanned_plan, preserve_from=preserved,
            replan_reason="retry same startup path under a fresh phase id",
        )
        third = asyncio.run(spawner.spawn_browser_agent(
            task=phase["worker_task"], phase_id="p2", worker_contract={},
            phase=replanned_phase, task_plan=replanned_plan,
        ))

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "spawn_infrastructure_exhausted")
        self.assertEqual(third["status"], "spawn_infrastructure_exhausted")
        self.assertTrue(second["tool_was_executed"])
        self.assertFalse(third["tool_was_executed"])
        self.assertEqual(calls["count"], 2)
        state = load_task_state(spawner.logger)
        self.assertEqual(state["phases"]["p2"]["attempts"], [])
        self.assertEqual(state["objective_attempts"], {})

    def test_fleet_readiness_failure_reuses_same_phase_acquisition_cooldown(self) -> None:
        spawner = _make_spawner(self)
        fingerprint = "fleet-readiness-route"
        error = FleetReadinessError(
            "Fleet did not become ready: -32011 Fleet open failed",
            fleet_id="48f0864a-79fb-4ef6-acb2-732e5e1e1818",
            owner_slot_id="slot-001",
        )

        receipt = record_spawn_acquisition_failure(
            spawner.logger,
            acquisition_fingerprint=fingerprint,
            phase_id="p1",
            exc=error,
        )
        rejection = spawn_acquisition_rejection(
            spawner.logger,
            acquisition_fingerprint=fingerprint,
            phase_id="p1",
        )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["retryAfterMs"], 30_000)
        self.assertIn("SAME phase id 'p1'", receipt["next_instruction"])
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection["status"], "spawn_acquisition_cooldown")
        self.assertGreater(rejection["retryAfterMs"], 0)
        self.assertLessEqual(rejection["retryAfterMs"], 30_000)
        self.assertFalse(rejection["tool_was_executed"])
        self.assertIn("SAME phase id 'p1'", rejection["next_instruction"])

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


    def test_requarantining_preserves_the_original_timestamp(self) -> None:
        """The TTL measures age, so re-marking must not restart the clock.

        The leak this closes: `_sync_slot_registry` re-quarantines a still-
        paused page on every pass, so a `quarantinedAt` refreshed per mark
        would sit permanently in the future and no TTL could ever elapse.
        """
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-ttl", agent_id="agent-ttl")
        slot.page_registry["page-1"] = {"pageId": "page-1", "status": "navigated"}

        spawner._mark_page_quarantined(
            slot, "page-1", reason="first", worker_id="w1", status="paused",
        )
        first_at = slot.page_quarantine["page-1"]["quarantinedAt"]

        time.sleep(0.01)
        spawner._mark_page_quarantined(
            slot, "page-1", reason="second", worker_id="w1", status="paused",
        )
        entry = slot.page_quarantine["page-1"]

        self.assertEqual(entry["quarantinedAt"], first_at)
        self.assertGreater(entry["lastQuarantinedAt"], first_at)
        self.assertEqual(entry["reason"], "second")

    def test_quarantine_expiry_is_measured_from_the_first_mark(self) -> None:
        spawner = _make_spawner(self)
        spawner.runtime.harness.page_quarantine_ttl_seconds = 60.0
        slot = BrowserAgentSlot(slot_id="slot-ttl2", agent_id="agent-ttl2")
        slot.page_registry["page-1"] = {"pageId": "page-1", "status": "navigated"}
        spawner._mark_page_quarantined(
            slot, "page-1", reason="paused", worker_id="w1", status="paused",
        )

        self.assertFalse(spawner._quarantine_expired(slot, "page-1"))

        slot.page_quarantine["page-1"]["quarantinedAt"] = time.time() - 61.0
        self.assertTrue(spawner._quarantine_expired(slot, "page-1"))

        # Re-marking an already-expired quarantine keeps it expired.
        spawner._mark_page_quarantined(
            slot, "page-1", reason="still paused", worker_id="w1", status="paused",
        )
        self.assertTrue(spawner._quarantine_expired(slot, "page-1"))

    def test_zero_ttl_restores_indefinite_quarantine(self) -> None:
        spawner = _make_spawner(self)
        spawner.runtime.harness.page_quarantine_ttl_seconds = 0.0
        slot = BrowserAgentSlot(slot_id="slot-ttl3", agent_id="agent-ttl3")
        spawner._mark_page_quarantined(
            slot, "page-1", reason="paused", worker_id="w1", status="paused",
        )
        slot.page_quarantine["page-1"]["quarantinedAt"] = time.time() - 86400.0

        self.assertFalse(spawner._quarantine_expired(slot, "page-1"))

    def test_retirement_closes_the_page_and_frees_the_entry(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-ret", agent_id="agent-ret")
        slot.page_registry["page-1"] = {"pageId": "page-1", "status": "navigated"}
        spawner._mark_page_quarantined(
            slot, "page-1", reason="paused", worker_id="w1", status="paused",
        )
        calls: list = []

        class _Client:
            async def call(self, method, params):
                calls.append((method, params.get("pageId")))
                # Registered Page.close evidence: a generically-successful
                # envelope is not enough, the receipt must name THIS page.
                return {
                    "executionId": "e-1",
                    "observation": "closed",
                    "data": {"closed": True, "pageId": "page-1"},
                }

        slot.client = _Client()

        retired = asyncio.run(spawner._retire_expired_quarantined_page(
            slot, "page-1", reason="still paused", verdict="still_paused",
        ))

        self.assertTrue(retired)
        self.assertEqual(calls, [("Page.close", "page-1")])
        self.assertNotIn("page-1", slot.page_quarantine)
        self.assertNotIn("page-1", slot.page_registry)

    def test_close_that_fails_without_raising_keeps_the_quarantine(self) -> None:
        """ABCPClient only raises on JSON-RPC {error:{...}} envelopes.

        A domain-level failure returns as an ordinary response with a negative
        observation, so "did not raise" cannot be read as "closed". Treating it
        as success would clear the quarantine on a page that is still open and
        still unusable — fail-open, and worse than the leak being closed here.
        """
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-ret3", agent_id="agent-ret3")
        slot.page_registry["page-1"] = {"pageId": "page-1", "status": "navigated"}
        spawner._mark_page_quarantined(
            slot, "page-1", reason="paused", worker_id="w1", status="paused",
        )

        class _Client:
            async def call(self, method, params):
                # No exception, no top-level `error`: exactly the implicit
                # error envelope shape ABCPClient passes through.
                return {
                    "observation": "close failed: the page is paused",
                    "success": False,
                    "data": {},
                }

        slot.client = _Client()

        retired = asyncio.run(spawner._retire_expired_quarantined_page(
            slot, "page-1", reason="still paused", verdict="still_paused",
        ))

        self.assertFalse(retired)
        self.assertIn("page-1", slot.page_quarantine)
        self.assertTrue(slot.page_quarantine["page-1"]["doNotUse"])
        self.assertIn("page-1", slot.page_registry)

    def test_close_responses_without_registered_evidence_keep_the_quarantine(
        self,
    ) -> None:
        """Call-level success is not page-level proof.

        Each of these returns a perfectly ordinary, non-raising, generically
        successful envelope. None of them proves THIS page closed, so none may
        discharge the quarantine — including the last one, which cheerfully
        reports that a completely different page was closed.
        """
        shapes = {
            "empty envelope": {"data": {}},
            "observation-only failure": {
                "observation": "close failed: page remains paused",
                "data": {},
            },
            "ack without page id": {
                "executionId": "e-1",
                "observation": "closed",
                "data": {"closed": True},
            },
            "closed flag missing": {
                "executionId": "e-1",
                "observation": "closed",
                "data": {"pageId": "page-1"},
            },
            "a different page closed": {
                "executionId": "e-1",
                "observation": "closed",
                "data": {"closed": True, "pageId": "page-9"},
            },
        }
        for name, response in shapes.items():
            with self.subTest(shape=name):
                spawner = _make_spawner(self)
                slot = BrowserAgentSlot(
                    slot_id="slot-ev", agent_id="agent-ev",
                )
                slot.page_registry["page-1"] = {
                    "pageId": "page-1", "status": "navigated",
                }
                spawner._mark_page_quarantined(
                    slot, "page-1", reason="paused", worker_id="w1",
                    status="paused",
                )

                class _Client:
                    async def call(self, method, params, _r=response):
                        return _r

                slot.client = _Client()

                retired = asyncio.run(
                    spawner._retire_expired_quarantined_page(
                        slot, "page-1", reason="still paused",
                        verdict="still_paused",
                    )
                )

                self.assertFalse(retired)
                self.assertIn("page-1", slot.page_quarantine)
                self.assertIn("page-1", slot.page_registry)

    def test_failed_close_keeps_the_quarantine(self) -> None:
        """A page that is still open must stay off the assignable pool.

        Dropping the entry here would be worse than the leak: the page would
        become assignable again while still unusable.
        """
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-ret2", agent_id="agent-ret2")
        slot.page_registry["page-1"] = {"pageId": "page-1", "status": "navigated"}
        spawner._mark_page_quarantined(
            slot, "page-1", reason="paused", worker_id="w1", status="paused",
        )

        class _Client:
            async def call(self, method, params):
                raise RuntimeError("page close rejected")

        slot.client = _Client()

        retired = asyncio.run(spawner._retire_expired_quarantined_page(
            slot, "page-1", reason="still paused", verdict="still_paused",
        ))

        self.assertFalse(retired)
        self.assertIn("page-1", slot.page_quarantine)
        self.assertTrue(slot.page_quarantine["page-1"]["doNotUse"])

    def test_authoritative_verdict_clears_the_recheck_failure_streak(self) -> None:
        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(slot_id="slot-rf", agent_id="agent-rf")
        spawner._mark_page_quarantined(
            slot, "page-1", reason="paused", worker_id="w1", status="paused",
        )
        slot.page_quarantine["page-1"]["recheckFailures"] = 2

        # A verdict-less pass inherits the streak...
        spawner._mark_page_quarantined(
            slot, "page-1", reason="probe raised", worker_id="w1", status="paused",
        )
        self.assertEqual(slot.page_quarantine["page-1"]["recheckFailures"], 2)

        # ...but a pass that actually answered resets it.
        spawner._mark_page_quarantined(
            slot, "page-1", reason="still paused", worker_id="w1",
            status="paused", recheck_verdict=True,
        )
        self.assertEqual(slot.page_quarantine["page-1"]["recheckFailures"], 0)


    def test_transport_error_on_recheck_also_ages_out_a_quarantine(self) -> None:
        """A verdict-less re-check must count, whatever shape the error took.

        The paused-error text decides whether a page ENTERS quarantine; it must
        not also decide whether a failed re-check counts. Otherwise a page whose
        Page.getState keeps timing out — a plain transport error with no
        "paused" anywhere in it — never accrues a failure and stays quarantined
        forever, which is the leak the TTL exists to end.
        """
        class FakeClient:
            async def call(self, method, params=None):
                if method == "Fleet.list":
                    return {"data": {"fleets": [{"fleetId": "fleet-1"}]}}
                if method == "Page.list":
                    return {"data": {"pages": [
                        {"pageId": "page-1", "fleetId": "fleet-1"},
                    ]}}
                if method == "Page.getState":
                    raise ABCPTransportError("Call to Page.getState timed out (30s)")
                if method == "Page.close":
                    return {
                        "executionId": "e-1",
                        "observation": "closed",
                        "data": {"closed": True, "pageId": params.get("pageId")},
                    }
                raise AssertionError(method)

        spawner = _make_spawner(self)
        spawner.runtime.harness.page_quarantine_recheck_max_failures = 1
        slot = BrowserAgentSlot(
            slot_id="slot-tx", agent_id="agent-tx", client=FakeClient(),
        )
        slot.fleet_ids.add("fleet-1")
        spawner._mark_page_quarantined(
            slot, "page-1", reason="paused", worker_id="w1", status="paused",
        )
        # Backdate past the TTL so retirement is on the table.
        slot.page_quarantine["page-1"]["quarantinedAt"] = time.time() - 10_000.0

        # First verdict-less pass: counted, but under the tolerance.
        asyncio.run(spawner._sync_slot_registry(slot, worker_id="w2"))
        self.assertIn("page-1", slot.page_quarantine)
        self.assertEqual(slot.page_quarantine["page-1"]["recheckFailures"], 1)

        # Second pass exceeds it and the page is retired.
        asyncio.run(spawner._sync_slot_registry(slot, worker_id="w2"))
        self.assertNotIn("page-1", slot.page_quarantine)

    def test_quarantined_pages_are_scanned_before_the_cap_applies(self) -> None:
        """The scan cap must not starve quarantined pages of their re-check.

        The cap used to be applied to an id-sorted list, so on a slot with more
        pages than the cap a quarantined page sorting late by id would never be
        asked about and could never age out.
        """
        scanned: list = []

        class FakeClient:
            async def call(self, method, params=None):
                if method == "Fleet.list":
                    return {"data": {"fleets": [{"fleetId": "fleet-1"}]}}
                if method == "Page.list":
                    return {"data": {"pages": [
                        {"pageId": f"page-{i:02d}", "fleetId": "fleet-1"}
                        for i in range(30)
                    ]}}
                if method == "Page.getState":
                    scanned.append(params.get("pageId"))
                    return {"data": {"pageId": params.get("pageId"), "status": "idle"}}
                raise AssertionError(method)

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-cap", agent_id="agent-cap", client=FakeClient(),
        )
        slot.fleet_ids.add("fleet-1")
        for i in range(30):
            slot.page_registry[f"page-{i:02d}"] = {
                "pageId": f"page-{i:02d}", "fleetId": "fleet-1",
            }
        # Sorts last by id, so the old id-ordered cap would never reach it.
        spawner._mark_page_quarantined(
            slot, "page-29", reason="paused", worker_id="w1", status="paused",
        )

        asyncio.run(spawner._sync_slot_registry(slot, worker_id="w2"))

        self.assertIn("page-29", scanned)
        self.assertEqual(scanned[0], "page-29")


    def test_more_quarantined_pages_than_the_cap_rotate_across_syncs(self) -> None:
        """The tail of an oversized quarantine set must not starve.

        Ranking quarantined pages ahead of ordinary ones is not enough on its
        own: with more quarantined pages than the scan cap, a fixed id order
        would replay the same prefix every sync and the pages sorting last
        would never be re-checked at all. Least-recently-rechecked ordering
        turns the cap into a rotation instead.
        """
        scanned: list = []

        class FakeClient:
            async def call(self, method, params=None):
                if method == "Fleet.list":
                    return {"data": {"fleets": [{"fleetId": "fleet-1"}]}}
                if method == "Page.list":
                    return {"data": {"pages": [
                        {"pageId": f"page-{i:02d}", "fleetId": "fleet-1"}
                        for i in range(20)
                    ]}}
                if method == "Page.getState":
                    scanned.append(params.get("pageId"))
                    # Still paused: nothing gets cleared, so the quarantine set
                    # stays at 20 and the cap keeps biting on every pass.
                    raise ABCPTransportError("ERR_PAGE_PAUSED: page is paused")
                raise AssertionError(method)

        spawner = _make_spawner(self)
        # Retirement off, so this test isolates scan fairness only.
        spawner.runtime.harness.page_quarantine_ttl_seconds = 0.0
        slot = BrowserAgentSlot(
            slot_id="slot-rot", agent_id="agent-rot", client=FakeClient(),
        )
        slot.fleet_ids.add("fleet-1")
        for i in range(20):
            page_id = f"page-{i:02d}"
            slot.page_registry[page_id] = {
                "pageId": page_id, "fleetId": "fleet-1",
            }
            spawner._mark_page_quarantined(
                slot, page_id, reason="paused", worker_id="w1", status="paused",
            )
            # Distinct re-check times so the ordering is deterministic.
            slot.page_quarantine[page_id]["lastQuarantinedAt"] = 1000.0 + i

        asyncio.run(spawner._sync_slot_registry(slot, worker_id="w2"))
        first_pass = list(scanned)
        self.assertEqual(len(first_pass), 12)
        # Oldest re-check times go first, regardless of id ordering.
        self.assertEqual(first_pass, [f"page-{i:02d}" for i in range(12)])

        scanned.clear()
        asyncio.run(spawner._sync_slot_registry(slot, worker_id="w2"))
        second_pass = list(scanned)

        # The 8 pages the cap excluded last time are now the least recently
        # rechecked, so they lead this pass.
        self.assertEqual(second_pass[:8], [f"page-{i:02d}" for i in range(12, 20)])
        self.assertEqual(
            set(first_pass) | set(second_pass),
            {f"page-{i:02d}" for i in range(20)},
            "two passes must cover every quarantined page",
        )

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

    def test_sync_short_circuits_repeated_required_fleet_open_timeouts(self) -> None:
        state_calls = []

        class FakeClient:
            async def call(self, method, params):
                if method == "Fleet.list":
                    return {"data": {"fleets": [{"fleetId": "fleet-bad"}]}}
                if method == "Page.list":
                    return {"data": {"pages": [
                        {"pageId": f"page-{i}", "fleetId": "fleet-bad"}
                        for i in range(4)
                    ]}}
                if method == "Page.getState":
                    state_calls.append(params["pageId"])
                    raise ABCPTransportError("-32012 Fleet open timeout")
                raise AssertionError(method)

        spawner = _make_spawner(self)
        slot = BrowserAgentSlot(
            slot_id="slot-001", agent_id="agent-slot-001", client=FakeClient()
        )

        with self.assertRaisesRegex(ABCPTransportError, "same phase id"):
            asyncio.run(spawner._sync_slot_registry(
                slot,
                worker_id="browser-004",
                required_fleet_id="fleet-bad",
            ))

        self.assertEqual(len(state_calls), 2)

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
            spawner.page_lease_manager.seed_worker_pages(
                "browser-001", {"page-held": "fleet-a"}
            )
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
                self.assertEqual(fake_harness.worker_id, "browser-001")
                self.assertEqual(fake_harness.slot_id, "slot-001")
                self.assertEqual(fake_harness.phase_id, "p1")
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

            self.assertEqual(slot.status, "idle")
            self.assertIsNone(slot.current_worker_id)
            self.assertEqual(
                spawner.page_lease_manager.owner_for("page-held"), ""
            )
            self.assertEqual(slot.last_result_summary["status"], "cancelled")
            phase_state = load_task_state(spawner.logger)["phases"]["p1"]
            self.assertEqual(phase_state["status"], "cancelled")
            self.assertEqual(phase_state["attempts"][-1]["status"], "cancelled")
            self.assertEqual(
                load_task_state(spawner.logger).get("objective_attempts"),
                {},
            )

        asyncio.run(scenario())


class FastPathCheckpointTests(unittest.TestCase):
    def test_conditional_roles_cannot_invent_checkpoint_in_initial_plan(self) -> None:
        errors = replan_checkpoint_plan_errors(
            {
                "phases": [{
                    "id": "precreated-validation",
                    "execution_role": "validation",
                    "worker_contract": {
                        "replan_checkpoint_id": "invented-before-probe",
                    },
                }],
            },
            {},
        )
        self.assertTrue(any(
            "require an active validated replan checkpoint" in error
            for error in errors
        ))

    def test_trace_params_are_stable_and_non_executable(self) -> None:
        params = trace_params_for_fast_path("collect_items", {
            "pageId": "page-volatile",
            "selector": ".record",
            "fields": {"text": ".body", "empty": ""},
            "regionId": "declared-region",
            "collectionField": "records",
            "recordName": "details",
            "baseRowRef": "/tmp/row.json",
            "containerId": "node-123",
            "x": 42,
            "y": 84,
        })
        self.assertEqual(params["selector"], ".record")
        self.assertEqual(params["fields"], {"text": ".body"})
        self.assertEqual(params["regionId"], "declared-region")
        self.assertEqual(
            params["baseRowBinding"],
            "validated_ref_required",
        )
        for forbidden in (
            "pageId",
            "baseRowRef",
            "containerId",
            "x",
            "y",
        ):
            self.assertNotIn(forbidden, params)

    def test_candidate_requires_validated_complete_collection_trace(self) -> None:
        contract = {
            "strategy_ids": ["declared-strategy"],
            "content_completeness": {
                "expected_regions": [{
                    "id": "declared-region",
                    "fields": ["records"],
                    "markers": ["declared-ready-marker"],
                }],
            },
        }
        trace = [{
            "type": "collect_items",
            "params": {
                "pageId": "volatile-page",
                "selector": ".record",
                "fields": {"text": ".body"},
                "regionId": "declared-region",
                "collectionField": "records",
                "recordName": "details",
                "baseRowRef": {
                    "savedPath": "/tmp/volatile.json",
                    "rowIndex": 7,
                },
            },
            "result": {
                "status": "done",
                "collectionState": "target_reached",
                "rowCount": 20,
                "recordExtraction": {"status": "done"},
            },
        }]
        assessment = assess_fast_path_candidate(
            trace=trace,
            trace_summary={},
            worker_contract=contract,
            phase={"execution_role": "probe"},
            validation={"status": "done"},
        )
        self.assertEqual(assessment["status"], "candidate")
        candidate = assessment["candidate"]
        self.assertEqual(
            candidate["executionPolicy"],
            "not_executable_stage_6b_a",
        )
        self.assertEqual(
            candidate["detailReadyMarkers"],
            ["declared-ready-marker"],
        )
        self.assertNotIn("pageId", candidate["collectionParams"])
        self.assertNotIn("baseRowRef", candidate["collectionParams"])
        self.assertEqual(
            candidate["collectionParams"]["baseRowBinding"],
            "validated_ref_required",
        )

        for role in ("validation", "bulk", "continuation"):
            role_assessment = assess_fast_path_candidate(
                trace=trace,
                trace_summary={},
                worker_contract=contract,
                phase={"execution_role": role},
                validation={"status": "done"},
            )
            self.assertEqual(role_assessment["status"], "candidate", role)
        remediation_assessment = assess_fast_path_candidate(
            trace=trace,
            trace_summary={},
            worker_contract=contract,
            phase={"execution_role": "remediation"},
            validation={"status": "done"},
        )
        self.assertEqual(remediation_assessment["status"], "not_compilable")

        for result, validation in (
            (
                {
                    "status": "done",
                    "collectionState": "materialization_stalled",
                    "rowCount": 8,
                },
                {"status": "done"},
            ),
            (
                {
                    "status": "done",
                    "collectionState": "target_reached",
                    "rowCount": 20,
                    "recordExtraction": {"status": "needs_fix"},
                },
                {"status": "done"},
            ),
            (
                {
                    "status": "done",
                    "collectionState": "target_reached",
                    "rowCount": 20,
                    "recordExtraction": {
                        "status": "done",
                        "validationPending": [{"type": "exact_rows"}],
                    },
                },
                {"status": "done"},
            ),
            (
                {
                    "status": "done",
                    "collectionState": "target_reached",
                    "rowCount": 20,
                },
                {"status": "failed"},
            ),
        ):
            rejected_trace = [dict(trace[0], result=result)]
            rejected = assess_fast_path_candidate(
                trace=rejected_trace,
                trace_summary={},
                worker_contract=contract,
                phase={"execution_role": "probe"},
                validation=validation,
            )
            self.assertEqual(rejected["status"], "not_compilable")

        historical = assess_fast_path_candidate(
            trace=[{"type": "collect_items", "result": trace[0]["result"]}],
            trace_summary={},
            worker_contract=contract,
            phase={"execution_role": "probe"},
            validation={"status": "done"},
        )
        self.assertEqual(historical["status"], "not_compilable")

    def test_checkpoint_originates_from_the_split_cohort_and_slice_receipts(self) -> None:
        """A probe owns one item; the cohort it belongs to is a separate fact.

        While both lived in one receipt, a probe could only be recorded by
        pretending to be a batch — which is why `execution_role` stayed unset
        in task 5324506f and the ladder never started.
        """
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="cohort-slice-checkpoint")
            phase = {
                "id": "probe",
                "execution_role": "probe",
                "stage_hint": "detail_sections",
            }
            initialize_task_state(
                logger, {"goal": "checkpoint", "phases": [phase]},
            )
            mark_phase_result(
                logger,
                phase_id="probe",
                worker_id="browser-probe",
                validation={"status": "done", "artifacts": []},
                result_status="validated_done",
                phase=phase,
                worker_contract={},
            )
            artifact_path = f"{temp}/artifacts/extractions/listing.json"
            contract = {
                "task_type": "web_scrape",
                "expected_artifact": {"fields": ["detailUrl"]},
                "validators": [
                    {"type": "required_fields", "fields": ["detailUrl"]},
                ],
                "_source_cohort_receipt": {
                    "receiptType": "source_cohort.v1",
                    "artifactName": "listing",
                    "artifactPath": artifact_path,
                    "artifactGeneration": "generation-a",
                    "identityField": "detailUrl",
                    "cohortSourceIndices": [0, 1, 2, 3],
                    "cohortRowKeys": ["u0", "u1", "u2", "u3"],
                    "sourceRowCount": 4,
                    "cohortSelector": {},
                },
                "_execution_slice_receipt": {
                    "receiptType": "execution_slice.v1",
                    "role": "probe",
                    "artifactPath": artifact_path,
                    "selectedSourceIndices": [0],
                    "selectedRowKeys": ["u0"],
                    "selector": {"indices": [0]},
                },
            }

            checkpoint = record_replan_checkpoint(
                logger,
                phase=phase,
                worker_contract=contract,
                worker_id="browser-probe",
                fast_path_assessment={"status": "not_compilable"},
            )

            self.assertIsNotNone(checkpoint)
            # The cohort is bound whole even though the slice was one row.
            self.assertEqual(checkpoint["cohortSourceIndices"], [0, 1, 2, 3])
            self.assertEqual(checkpoint["validatedSourceIndices"], [0])
            self.assertEqual(checkpoint["remainingSourceIndices"], [1, 2, 3])
            # No reusable candidate: the remaining rows continue on the slow
            # path rather than being authorized as a bulk run.
            self.assertEqual(checkpoint["requiredNextRole"], "continuation")

    def test_checkpoint_advances_roles_and_fences_source_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="fast-path-checkpoint")
            phase = {
                "id": "probe",
                "execution_role": "probe",
                "stage_hint": "computed_relationship",
            }
            initialize_task_state(
                logger,
                {
                    "goal": "checkpoint",
                    "phases": [phase],
                },
            )
            mark_phase_result(
                logger,
                phase_id="probe",
                worker_id="browser-probe",
                validation={"status": "done", "artifacts": []},
                result_status="validated_done",
                phase=phase,
                worker_contract={},
            )
            contract = {
                "task_type": "web_scrape",
                "strategy_ids": ["declared-strategy"],
                "expected_artifact": {
                    "fields": ["rank", "records"],
                },
                "validators": [{
                    "type": "required_fields",
                    "fields": ["rank", "records"],
                }, {
                    "type": "exact_rows",
                    "value": 1,
                }, {
                    "type": "range",
                    "field": "rank",
                    "min": 6,
                    "max": 6,
                }],
                "_batch_source_receipt": {
                    "artifactName": "listing",
                    "artifactPath": f"{temp}/artifacts/extractions/listing.json",
                    "sourceArtifactGeneration": "generation-a",
                    "sourceRowCount": 4,
                    "cohortSourceIndices": [0, 1, 2, 3],
                    "selectedSourceIndices": [0],
                    "selector": {"field": "rank", "values": [6]},
                },
            }
            assessment = {
                "status": "candidate",
                "executionPolicy": "not_executable_stage_6b_a",
                "candidate": {
                    "status": "candidate",
                    "executionPolicy": "not_executable_stage_6b_a",
                },
            }
            checkpoint = record_replan_checkpoint(
                logger,
                phase=phase,
                worker_contract=contract,
                worker_id="browser-probe",
                fast_path_assessment=assessment,
            )
            self.assertIsNotNone(checkpoint)
            self.assertTrue(checkpoint["active"])
            self.assertEqual(checkpoint["requiredNextRole"], "validation")
            self.assertEqual(checkpoint["validatedSourceIndices"], [0])
            self.assertEqual(checkpoint["remainingSourceIndices"], [1, 2, 3])
            self.assertEqual(
                checkpoint["fastPathAssessment"]["candidate"]["checkpointId"],
                checkpoint["checkpointId"],
            )

            valid_contract = {
                "task_type": "web_scrape",
                "replan_checkpoint_id": checkpoint["checkpointId"],
                "strategy_ids": ["declared-strategy"],
                "expected_artifact": {
                    "fields": ["rank", "records"],
                },
                "validators": [{
                    "type": "required_fields",
                    "fields": ["rank", "records"],
                }, {
                    "type": "exact_rows",
                    "value": 2,
                }, {
                    "type": "range",
                    "field": "rank",
                    "min": 7,
                    "max": 8,
                }],
                "stage_hint": "detail_sections",
                "_batch_source_receipt": {
                    "artifactPath": contract["_batch_source_receipt"]["artifactPath"],
                    "sourceArtifactGeneration": "generation-a",
                    "sourceRowCount": 4,
                    "cohortSourceIndices": [0, 1, 2, 3],
                    "selectedSourceIndices": [1, 2],
                    "selector": {"field": "rank", "values": [7, 8]},
                },
            }
            self.assertIsNone(replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "validation",
                    "execution_role": "validation",
                    "stage_hint": "detail_sections",
                    "depends_on": ["probe"],
                },
                worker_contract=valid_contract,
            ))
            stored_checkpoint = next(iter(
                load_task_state(logger)["replan_checkpoints"].values()
            ))
            self.assertTrue(stored_checkpoint.get("nextObjectiveFingerprint"))
            unbound = copy.deepcopy(valid_contract)
            unbound.pop("replan_checkpoint_id", None)
            overlap_rejection = replan_checkpoint_spawn_rejection(
                logger,
                phase={"id": "unbound", "execution_role": "validation"},
                worker_contract=unbound,
            )
            self.assertIn("overlap", " ".join(overlap_rejection["errors"]))
            changed_business_contract = copy.deepcopy(valid_contract)
            changed_business_contract["replan_checkpoint_id"] = checkpoint[
                "checkpointId"
            ]
            changed_business_contract["expected_artifact"] = {
                "fields": ["rank", "records", "unapprovedExtraField"],
            }
            changed_business_contract["_batch_source_receipt"] = {
                **valid_contract["_batch_source_receipt"],
                "selectedSourceIndices": [1],
            }
            changed_business = replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "validation-changed-contract",
                    "execution_role": "validation",
                    "stage_hint": "detail_sections",
                    "depends_on": ["probe"],
                },
                worker_contract=changed_business_contract,
            )
            self.assertIn(
                "merged expected_artifact changed",
                " ".join(changed_business["errors"]),
            )
            wrong_role = replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "bulk", "execution_role": "bulk",
                    "depends_on": ["probe"],
                },
                worker_contract=valid_contract,
            )
            self.assertIn("execution_role", " ".join(wrong_role["errors"]))

            repeated_contract = {
                "task_type": "web_scrape",
                "replan_checkpoint_id": checkpoint["checkpointId"],
                "strategy_ids": ["declared-strategy"],
                "expected_artifact": {
                    "fields": ["rank", "records"],
                },
                "_batch_source_receipt": {
                    "artifactPath": contract["_batch_source_receipt"]["artifactPath"],
                    "sourceArtifactGeneration": "generation-a",
                    "sourceRowCount": 4,
                    "cohortSourceIndices": [0, 1, 2, 3],
                    "selectedSourceIndices": [0],
                },
            }
            repeated = replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "validation", "execution_role": "validation",
                    "depends_on": ["probe"],
                },
                worker_contract=repeated_contract,
            )
            self.assertIn("already validated", " ".join(repeated["errors"]))

            stale_contract = {
                "task_type": "web_scrape",
                "replan_checkpoint_id": checkpoint["checkpointId"],
                "strategy_ids": ["declared-strategy"],
                "expected_artifact": {
                    "fields": ["rank", "records"],
                },
                "_batch_source_receipt": {
                    "artifactPath": contract["_batch_source_receipt"]["artifactPath"],
                    "sourceArtifactGeneration": "generation-b",
                    "sourceRowCount": 4,
                    "cohortSourceIndices": [0, 1, 2, 3],
                    "selectedSourceIndices": [1],
                },
            }
            stale = replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "validation", "execution_role": "validation",
                    "depends_on": ["probe"],
                },
                worker_contract=stale_contract,
            )
            self.assertIn("generation changed", " ".join(stale["errors"]))

            narrowed_contract = copy.deepcopy(valid_contract)
            narrowed_contract["_batch_source_receipt"] = {
                **valid_contract["_batch_source_receipt"],
                "cohortSourceIndices": [0, 1],
                "selectedSourceIndices": [1],
            }
            narrowed = replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "validation-narrowed",
                    "execution_role": "validation",
                    "depends_on": ["probe"],
                },
                worker_contract=narrowed_contract,
            )
            self.assertIn("cohort rows do not match", " ".join(narrowed["errors"]))
            self.assertEqual(narrowed["expectedCohortSourceIndices"], [0, 1, 2, 3])
            self.assertEqual(narrowed["actualCohortSourceIndices"], [0, 1])
            self.assertEqual(narrowed["expectedCohortSelector"], {})

            checkpoint_id = checkpoint["checkpointId"]
            previous_state = load_task_state(logger)
            self.assertTrue(replan_checkpoint_plan_errors(
                {
                    "phases": [],
                },
                previous_state,
            ))
            self.assertTrue(replan_checkpoint_plan_errors(
                {
                    "replan_checkpoint_id": "wrong",
                    "phases": [],
                },
                previous_state,
            ))
            self.assertEqual(
                replan_checkpoint_plan_errors(
                    {
                        "replan_checkpoint_id": checkpoint_id,
                        "phases": [{
                            "id": "probe",
                            "execution_role": "probe",
                        }, {
                            "id": "validation",
                            "execution_role": "validation",
                            "depends_on": ["probe"],
                            "expected_artifact": {
                                "fields": ["rank", "records"],
                            },
                            "validators": [{
                                "type": "required_fields",
                                "fields": ["rank", "records"],
                            }, {
                                "type": "exact_rows",
                                "value": 3,
                            }],
                            "worker_contract": {
                                "task_type": "web_scrape",
                                "batch_source": {
                                    "artifact_name": "listing",
                                },
                            },
                        }],
                    },
                    previous_state,
                ),
                [],
            )

    def test_parallel_cohort_checkpoints_cannot_overwrite_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="parallel-checkpoints")
            phases = [
                {"id": "probe-a", "execution_role": "probe"},
                {"id": "probe-b", "execution_role": "probe"},
            ]
            initialize_task_state(
                logger,
                {"goal": "parallel", "phases": phases},
            )

            checkpoints = []
            contracts = []
            for suffix, phase in zip(("a", "b"), phases):
                worker_id = f"browser-{suffix}"
                mark_phase_result(
                    logger,
                    phase_id=phase["id"],
                    worker_id=worker_id,
                    validation={"status": "done", "artifacts": []},
                    result_status="validated_done",
                    phase=phase,
                    worker_contract={},
                )
                contract = {
                    "task_type": "web_scrape",
                    "strategy_ids": [f"strategy-{suffix}"],
                    "expected_artifact": {"fields": ["rank", "records"]},
                    "_batch_source_receipt": {
                        "artifactName": f"listing-{suffix}",
                        "artifactPath": (
                            f"{temp}/artifacts/extractions/listing-{suffix}.json"
                        ),
                        "sourceArtifactGeneration": f"generation-{suffix}",
                        "sourceRowCount": 3,
                        "cohortSourceIndices": [0, 1, 2],
                        "selectedSourceIndices": [0],
                        "cohortSelector": {},
                    },
                }
                checkpoint = record_replan_checkpoint(
                    logger,
                    phase=phase,
                    worker_contract=contract,
                    worker_id=worker_id,
                    fast_path_assessment={"status": "not_compilable"},
                )
                self.assertIsNotNone(checkpoint)
                checkpoints.append(checkpoint)
                contracts.append(contract)

            state = load_task_state(logger)
            self.assertEqual(len(state["replan_checkpoints"]), 2)
            self.assertEqual(
                {
                    item["checkpointId"]
                    for item in state["replan_checkpoints"].values()
                },
                {item["checkpointId"] for item in checkpoints},
            )

            incomplete_plan = {
                "replan_checkpoint_ids": [
                    item["checkpointId"] for item in checkpoints
                ],
                "phases": [{
                    "id": "continuation-a",
                    "execution_role": "continuation",
                    "worker_contract": {
                        "replan_checkpoint_id": checkpoints[0]["checkpointId"],
                        "batch_source": {"artifact_name": "listing-a"},
                    },
                }],
            }
            errors = replan_checkpoint_plan_errors(incomplete_plan, state)
            self.assertTrue(any(
                checkpoints[1]["checkpointId"] in error for error in errors
            ))

            complete_plan = {
                "replan_checkpoint_ids": [
                    item["checkpointId"] for item in checkpoints
                ],
                "phases": (
                    [
                        {
                            "id": f"probe-{suffix}",
                            "execution_role": "probe",
                        }
                        for suffix in ("a", "b")
                    ] + [
                    {
                        "id": f"continuation-{suffix}",
                        "execution_role": "continuation",
                        "depends_on": [f"probe-{suffix}"],
                        "expected_artifact": {
                            "fields": ["rank", "records"],
                        },
                        "validators": [],
                        "worker_contract": {
                            "task_type": "web_scrape",
                            "replan_checkpoint_id": checkpoint["checkpointId"],
                            "batch_source": {
                                "artifact_name": f"listing-{suffix}",
                            },
                        },
                    }
                    for suffix, checkpoint in zip(("a", "b"), checkpoints)
                    ]
                ),
            }
            self.assertEqual(
                replan_checkpoint_plan_errors(complete_plan, state),
                [],
            )

            guarded_contract = dict(contracts[0])
            guarded_contract["replan_checkpoint_id"] = checkpoints[0][
                "checkpointId"
            ]
            guarded_contract["_batch_source_receipt"] = {
                **contracts[0]["_batch_source_receipt"],
                "selectedSourceIndices": [1],
            }
            wrong_role = replan_checkpoint_spawn_rejection(
                logger,
                phase={"id": "bulk-a", "execution_role": "bulk"},
                worker_contract=guarded_contract,
            )
            self.assertIsNotNone(wrong_role)
            self.assertIn("execution_role", " ".join(wrong_role["errors"]))

    def test_cohort_selector_limits_checkpoint_remaining_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="cohort-selector")
            phase = {"id": "probe", "execution_role": "probe"}
            initialize_task_state(
                logger,
                {"goal": "subset", "phases": [phase]},
            )
            extraction_dir = (
                Path(logger.task_dir) / "artifacts" / "extractions"
            )
            extraction_dir.mkdir(parents=True, exist_ok=True)
            artifact_path = extraction_dir / "listing.json"
            artifact_path.write_text(
                json.dumps({
                    "name": "listing",
                    "rows": [
                        {"rank": rank, "url": f"https://example.test/{rank}"}
                        for rank in range(1, 7)
                    ],
                }),
                encoding="utf-8",
            )
            state = load_task_state(logger)
            state["artifacts"] = [str(artifact_path.resolve())]
            write_task_state(logger, state)

            contract = {
                "task_type": "web_scrape",
                "expected_artifact": {"fields": ["rank", "records"]},
                "batch_source": {
                    "artifact_name": "listing",
                    "cohort_selector": {
                        "field": "rank",
                        "values": [2, 4, 6],
                    },
                    "selector": {"offset": 0, "limit": 1},
                },
            }
            self.assertIsNone(materialize_batch_rows_from_source(
                logger,
                phase=phase,
                worker_contract=contract,
            ))
            receipt = contract["_batch_source_receipt"]
            self.assertEqual(receipt["sourceRowCount"], 6)
            self.assertEqual(receipt["cohortSourceIndices"], [1, 3, 5])
            self.assertEqual(receipt["selectedSourceIndices"], [1])
            self.assertEqual(
                [row["rank"] for row in contract["batch_rows"]],
                [2],
            )
            missing_target = {
                "batch_source": {
                    "artifact_name": "listing",
                    "cohort_selector": {
                        "field": "rank",
                        "values": [2, 999],
                    },
                    "selector": {"limit": 1},
                },
            }
            rejection = materialize_batch_rows_from_source(
                logger,
                phase=phase,
                worker_contract=missing_target,
            )
            self.assertEqual(
                rejection["status"],
                "invalid_batch_source_selection",
            )
            self.assertIn("missing", rejection["error"])

            mark_phase_result(
                logger,
                phase_id="probe",
                worker_id="browser-probe",
                validation={"status": "done", "artifacts": []},
                result_status="validated_done",
                phase=phase,
                worker_contract=contract,
            )
            checkpoint = record_replan_checkpoint(
                logger,
                phase=phase,
                worker_contract=contract,
                worker_id="browser-probe",
                fast_path_assessment={"status": "not_compilable"},
            )
            self.assertEqual(checkpoint["cohortSourceIndices"], [1, 3, 5])
            self.assertEqual(checkpoint["remainingSourceIndices"], [3, 5])
            self.assertNotIn(0, checkpoint["remainingSourceIndices"])
            self.assertNotIn(2, checkpoint["remainingSourceIndices"])

    def test_checkpoint_validator_order_and_monotonic_strengthening(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="checkpoint-validator-semantics")
            phase = {
                "id": "probe",
                "execution_role": "probe",
                "expected_artifact": {"name": "details"},
            }
            initialize_task_state(logger, {"goal": "semantic", "phases": [phase]})
            mark_phase_result(
                logger,
                phase_id="probe",
                worker_id="browser-probe",
                validation={"status": "done", "artifacts": []},
                result_status="validated_done",
                phase=phase,
                worker_contract={},
            )
            receipt = {
                "artifactName": "listing",
                "artifactPath": f"{temp}/artifacts/extractions/listing.json",
                "sourceArtifactGeneration": "generation-a",
                "sourceRowCount": 3,
                "cohortSourceIndices": [0, 1, 2],
                "selectedSourceIndices": [0],
                "selector": {"field": "rank", "values": [1]},
            }
            baseline = {
                "task_type": "web_scrape",
                "expected_artifact": {"fields": ["rank", "title"]},
                "validators": [{
                    "type": "required_fields",
                    "fields": ["title", "rank"],
                }, {
                    "type": "range", "field": "rank", "min": 1, "max": 1,
                }],
                "_batch_source_receipt": receipt,
            }
            checkpoint = record_replan_checkpoint(
                logger,
                phase=phase,
                worker_contract=baseline,
                worker_id="browser-probe",
                fast_path_assessment={"status": "candidate", "candidate": {}},
            )
            strengthened = {
                "task_type": "web_scrape",
                "replan_checkpoint_id": checkpoint["checkpointId"],
                "expected_artifact": {"fields": ["rank", "title"]},
                "validators": [{
                    "type": "required_fields",
                    "fields": ["rank", "title", "url"],
                }, {
                    "type": "unique", "fields": ["url"],
                }, {
                    "type": "range", "field": "rank", "min": 2, "max": 3,
                }],
                "_batch_source_receipt": {
                    **receipt,
                    "selectedSourceIndices": [1, 2],
                    "selector": {"field": "rank", "values": [2, 3]},
                },
            }
            self.assertIsNone(replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "validation",
                    "execution_role": "validation",
                    "expected_artifact": {"name": "details"},
                    "depends_on": ["probe"],
                },
                worker_contract=strengthened,
            ))
            weakened = copy.deepcopy(strengthened)
            weakened["validators"] = [{
                "type": "required_fields",
                "fields": ["rank"],
            }]
            rejection = replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "validation-weakened",
                    "execution_role": "validation",
                    "expected_artifact": {"name": "details"},
                    "depends_on": ["probe"],
                },
                worker_contract=weakened,
            )
            self.assertIn(
                "removed or weakened",
                " ".join(rejection["errors"]),
            )

    def test_successor_checkpoint_inherits_cohort_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="checkpoint-inheritance")
            phases = [
                {"id": "probe", "execution_role": "probe"},
                {"id": "validation", "execution_role": "validation"},
            ]
            initialize_task_state(logger, {"goal": "inherit", "phases": phases})
            receipt = {
                "artifactName": "listing",
                "artifactPath": f"{temp}/artifacts/extractions/listing.json",
                "sourceArtifactGeneration": "generation-a",
                "sourceRowCount": 4,
                "cohortSourceIndices": [0, 1, 2, 3],
                "selectedSourceIndices": [0],
            }
            contract = {
                "task_type": "web_scrape",
                "expected_artifact": {"fields": ["rank"]},
                "_batch_source_receipt": receipt,
            }
            mark_phase_result(
                logger, phase_id="probe", worker_id="browser-probe",
                validation={"status": "done", "artifacts": []},
                result_status="validated_done", phase=phases[0],
                worker_contract=contract,
            )
            first = record_replan_checkpoint(
                logger, phase=phases[0], worker_contract=contract,
                worker_id="browser-probe",
                fast_path_assessment={"status": "candidate", "candidate": {}},
            )
            successor_contract = {
                **contract,
                "replan_checkpoint_id": first["checkpointId"],
                "_batch_source_receipt": {
                    **receipt,
                    "selectedSourceIndices": [1, 2],
                },
            }
            mark_phase_result(
                logger, phase_id="validation", worker_id="browser-validation",
                validation={"status": "done", "artifacts": []},
                result_status="validated_done", phase=phases[1],
                worker_contract=successor_contract,
            )
            mismatched_predecessor = copy.deepcopy(successor_contract)
            mismatched_predecessor["_batch_source_receipt"] = {
                **successor_contract["_batch_source_receipt"],
                "sourceArtifactGeneration": "generation-b",
            }
            self.assertIsNone(record_replan_checkpoint(
                logger, phase=phases[1], worker_contract=mismatched_predecessor,
                worker_id="browser-validation",
                fast_path_assessment={"status": "candidate", "candidate": {}},
            ))
            self.assertIn(
                "fast_path.replan_checkpoint_predecessor_mismatch",
                logger.path.read_text(encoding="utf-8"),
            )
            second = record_replan_checkpoint(
                logger, phase=phases[1], worker_contract=successor_contract,
                worker_id="browser-validation",
                fast_path_assessment={"status": "candidate", "candidate": {}},
            )
            self.assertEqual(second["cohortKey"], first["cohortKey"])
            self.assertEqual(second["validatedSourceIndices"], [0, 1, 2])
            self.assertEqual(len(load_task_state(logger)["replan_checkpoints"]), 1)

    def test_checkpoint_lineage_upgrades_downgrades_and_strictly_shrinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="checkpoint-lineage")
            phases = [
                {"id": "probe", "execution_role": "probe"},
                {
                    "id": "continue-1", "execution_role": "continuation",
                    "depends_on": ["probe"],
                },
                {
                    "id": "validation", "execution_role": "validation",
                    "depends_on": ["continue-1"],
                },
                {
                    "id": "bulk", "execution_role": "bulk",
                    "depends_on": ["validation"],
                },
                {
                    "id": "continue-2", "execution_role": "continuation",
                    "depends_on": ["bulk"],
                },
            ]
            initialize_task_state(logger, {"goal": "lineage", "phases": phases})
            artifact_path = f"{temp}/artifacts/extractions/listing.json"
            base_receipt = {
                "artifactName": "listing",
                "artifactPath": artifact_path,
                "sourceArtifactGeneration": "generation-a",
                "sourceRowCount": 7,
                "cohortSourceIndices": list(range(7)),
                "cohortSelector": {},
            }
            contracts_by_phase = {}

            def advance(
                phase: dict,
                *,
                selected: list[int],
                predecessor,
                candidate: bool,
            ) -> dict:
                contract = {
                    "task_type": "web_scrape",
                    "expected_artifact": {"fields": ["rank"]},
                    "validators": [{
                        "type": "required_fields", "fields": ["rank"],
                    }],
                    "_batch_source_receipt": {
                        **base_receipt,
                        "selectedSourceIndices": selected,
                    },
                }
                if predecessor is not None:
                    contract["replan_checkpoint_id"] = predecessor[
                        "checkpointId"
                    ]
                contracts_by_phase[phase["id"]] = copy.deepcopy(contract)
                mark_phase_result(
                    logger,
                    phase_id=phase["id"],
                    worker_id=f"browser-{phase['id']}",
                    validation={"status": "done", "artifacts": []},
                    result_status="validated_done",
                    phase=phase,
                    worker_contract=contract,
                )
                checkpoint = record_replan_checkpoint(
                    logger,
                    phase=phase,
                    worker_contract=contract,
                    worker_id=f"browser-{phase['id']}",
                    fast_path_assessment=(
                        {"status": "candidate", "candidate": {}}
                        if candidate
                        else {"status": "not_compilable"}
                    ),
                )
                self.assertIsNotNone(checkpoint)
                return checkpoint

            def retained(phase: dict) -> dict:
                contract = copy.deepcopy(contracts_by_phase[phase["id"]])
                contract.pop("_batch_source_receipt", None)
                contract["batch_source"] = {"artifact_name": "listing"}
                return {**phase, "worker_contract": contract}

            probe = advance(
                phases[0], selected=[0], predecessor=None, candidate=False,
            )
            with self.subTest(stage="probe_to_continuation"):
                self.assertEqual(probe["requiredNextRole"], "continuation")
            continuation = advance(
                phases[1], selected=[1], predecessor=probe, candidate=True,
            )
            with self.subTest(stage="continuation_to_validation"):
                self.assertEqual(continuation["requiredNextRole"], "validation")
                self.assertEqual(
                    continuation["predecessorCheckpointId"],
                    probe["checkpointId"],
                )
                self.assertEqual(continuation["predecessorPhaseId"], "probe")
                self.assertEqual(continuation["lineageDepth"], 1)

            validation_contract = {
                "task_type": "web_scrape",
                "replan_checkpoint_id": continuation["checkpointId"],
                "expected_artifact": {"fields": ["rank"]},
                "validators": [{
                    "type": "required_fields", "fields": ["rank"],
                }],
                "batch_source": {"artifact_name": "listing"},
                "_batch_source_receipt": {
                    **base_receipt,
                    "selectedSourceIndices": [2, 3],
                },
            }
            validation_plan_phase = {
                **phases[2],
                "worker_contract": {
                    key: value for key, value in validation_contract.items()
                    if key != "_batch_source_receipt"
                },
            }
            validation_plan = {
                "replan_checkpoint_ids": [continuation["checkpointId"]],
                "phases": [
                    retained(phases[0]), retained(phases[1]),
                    validation_plan_phase,
                ],
            }
            state = load_task_state(logger)
            self.assertEqual(
                replan_checkpoint_plan_errors(validation_plan, state), [],
            )
            self.assertIsNone(replan_checkpoint_spawn_rejection(
                logger,
                phase=phases[2],
                worker_contract=validation_contract,
            ))

            omitted_dependency_phase = copy.deepcopy(validation_plan_phase)
            omitted_dependency_phase.pop("depends_on", None)
            with self.subTest(stage="explicit_dependency_required_by_both_gates"):
                omitted_plan_errors = replan_checkpoint_plan_errors(
                    {
                        **validation_plan,
                        "phases": [
                            retained(phases[0]), retained(phases[1]),
                            omitted_dependency_phase,
                        ],
                    },
                    state,
                )
                self.assertTrue(any(
                    "explicitly add it to depends_on" in error
                    for error in omitted_plan_errors
                ))
                omitted_spawn = replan_checkpoint_spawn_rejection(
                    logger,
                    phase={
                        key: value for key, value in phases[2].items()
                        if key != "depends_on"
                    },
                    worker_contract=validation_contract,
                )
                self.assertIn(
                    "must explicitly depend_on validated predecessor phase",
                    " ".join(omitted_spawn["errors"]),
                )

            wrong_lineage_phase = {
                **validation_plan_phase,
                "depends_on": ["probe"],
            }
            wrong_plan_errors = replan_checkpoint_plan_errors(
                {
                    **validation_plan,
                    "phases": [
                        retained(phases[0]), retained(phases[1]),
                        wrong_lineage_phase,
                    ],
                },
                state,
            )
            with self.subTest(stage="wrong_lineage_rejected_by_both_gates"):
                self.assertTrue(any(
                    "validated predecessor phase 'continue-1'" in error
                    for error in wrong_plan_errors
                ))
                wrong_spawn = replan_checkpoint_spawn_rejection(
                    logger,
                    phase={**phases[2], "depends_on": ["probe"]},
                    worker_contract=validation_contract,
                )
                self.assertIn(
                    "validated predecessor phase 'continue-1'",
                    " ".join(wrong_spawn["errors"]),
                )

            remediation_phase = {
                "id": "repair", "execution_role": "remediation",
                "depends_on": ["continue-1"],
            }
            remediation_contract = {
                **validation_contract,
                "batch_source": {
                    "artifact_name": "listing",
                    "selector": {"field": "rank", "values": [3]},
                },
                "_batch_source_receipt": {
                    **base_receipt,
                    "selectedSourceIndices": [3],
                },
            }
            remediation_errors = replan_checkpoint_plan_errors(
                {
                    **validation_plan,
                    "phases": [retained(phases[0]), retained(phases[1]), {
                        **remediation_phase,
                        "worker_contract": {
                            key: value for key, value in remediation_contract.items()
                            if key != "_batch_source_receipt"
                        },
                    }],
                },
                state,
            )
            with self.subTest(stage="active_cohort_remediation_rejected"):
                self.assertTrue(any(
                    "requires execution_role='validation'" in error
                    for error in remediation_errors
                ))
                remediation_spawn = replan_checkpoint_spawn_rejection(
                    logger,
                    phase=remediation_phase,
                    worker_contract=remediation_contract,
                )
                self.assertIn("use execution_role='continuation'", " ".join(
                    remediation_spawn["errors"]
                ))

            validation = advance(
                phases[2], selected=[2, 3], predecessor=continuation,
                candidate=True,
            )
            with self.subTest(stage="validation_to_bulk"):
                self.assertEqual(validation["requiredNextRole"], "bulk")
            bulk = advance(
                phases[3], selected=[4], predecessor=validation,
                candidate=False,
            )
            with self.subTest(stage="bulk_to_continuation"):
                self.assertEqual(bulk["requiredNextRole"], "continuation")
                self.assertEqual(bulk["lineageDepth"], 3)

            snapshots = [probe, continuation, validation, bulk]
            for index, (previous, current) in enumerate(
                zip(snapshots, snapshots[1:]), start=1,
            ):
                with self.subTest(stage="monotonic_progress", hop=index):
                    self.assertTrue(
                        set(previous["validatedSourceIndices"]).issubset(
                            current["validatedSourceIndices"]
                        )
                    )
                    self.assertGreater(
                        len(current["validatedSourceIndices"]),
                        len(previous["validatedSourceIndices"]),
                    )
                    self.assertLess(
                        len(current["remainingSourceIndices"]),
                        len(previous["remainingSourceIndices"]),
                    )
                    self.assertFalse(
                        set(current["validatedSourceIndices"]).intersection(
                            current["remainingSourceIndices"]
                        )
                    )

            continuation_contract = {
                **validation_contract,
                "replan_checkpoint_id": bulk["checkpointId"],
                "_batch_source_receipt": {
                    **base_receipt,
                    "selectedSourceIndices": [5],
                },
            }
            continuation_plan_phase = {
                **phases[4],
                "worker_contract": {
                    key: value for key, value in continuation_contract.items()
                    if key != "_batch_source_receipt"
                },
            }
            bulk_state = load_task_state(logger)
            self.assertEqual(replan_checkpoint_plan_errors(
                {
                    "replan_checkpoint_ids": [bulk["checkpointId"]],
                    "phases": [
                        *(retained(phase) for phase in phases[:4]),
                        continuation_plan_phase,
                    ],
                },
                bulk_state,
            ), [])
            self.assertIsNone(replan_checkpoint_spawn_rejection(
                logger,
                phase=phases[4],
                worker_contract=continuation_contract,
            ))

    def test_legacy_checkpoint_emits_degraded_business_fence_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="legacy-business-fence")
            initialize_task_state(logger, {"goal": "legacy", "phases": []})
            state = load_task_state(logger)
            state["replan_checkpoints"] = {"legacy-cohort": {
                "checkpointId": "legacy-id",
                "cohortKey": "legacy-cohort",
                "active": True,
                "phaseId": "legacy-probe",
                "sourceArtifactName": "listing",
                "requiredNextRole": "continuation",
                "sourceArtifactPath": f"{temp}/listing.json",
                "sourceArtifactGeneration": "generation-a",
                "cohortSourceIndices": [0, 1],
                "validatedSourceIndices": [0],
                "remainingSourceIndices": [1],
            }}
            state["phases"] = {
                "legacy-probe": {"status": "validated_done"},
            }
            write_task_state(logger, state)
            contract = {
                "task_type": "web_scrape",
                "replan_checkpoint_id": "legacy-id",
                "expected_artifact": {"fields": ["rank"]},
                "_batch_source_receipt": {
                    "artifactPath": f"{temp}/listing.json",
                    "sourceArtifactGeneration": "generation-a",
                    "sourceRowCount": 2,
                    "cohortSourceIndices": [0, 1],
                    "selectedSourceIndices": [1],
                },
            }
            self.assertIsNone(replan_checkpoint_spawn_rejection(
                logger,
                phase={
                    "id": "continue", "execution_role": "continuation",
                    "depends_on": ["legacy-probe"],
                },
                worker_contract=contract,
            ))
            self.assertIn(
                "fast_path.replan_checkpoint_business_contract_unavailable",
                logger.path.read_text(encoding="utf-8"),
            )
            legacy_plan = {
                "replan_checkpoint_ids": ["legacy-id"],
                "phases": [{
                    "id": "legacy-probe", "execution_role": "probe",
                }, {
                    "id": "continue", "execution_role": "continuation",
                    "depends_on": ["legacy-probe"],
                    "worker_contract": {
                        "task_type": "web_scrape",
                        "replan_checkpoint_id": "legacy-id",
                        "expected_artifact": {"fields": ["rank"]},
                        "batch_source": {"artifact_name": "listing"},
                    },
                }],
            }
            self.assertEqual(
                replan_checkpoint_plan_errors(legacy_plan, load_task_state(logger)),
                [],
            )

    def test_source_generation_change_retires_checkpoint_for_fresh_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="checkpoint-generation-exit")
            phase = {"id": "probe", "execution_role": "probe"}
            initialize_task_state(logger, {"goal": "generation", "phases": [phase]})
            extraction_dir = Path(logger.task_dir) / "artifacts" / "extractions"
            extraction_dir.mkdir(parents=True, exist_ok=True)
            artifact_path = extraction_dir / "listing.json"
            artifact_path.write_text(
                json.dumps({"name": "listing", "rows": [{"rank": 1}, {"rank": 2}]}),
                encoding="utf-8",
            )
            state = load_task_state(logger)
            state["artifacts"] = [str(artifact_path.resolve())]
            write_task_state(logger, state)
            contract = {
                "task_type": "web_scrape",
                "expected_artifact": {"fields": ["rank"]},
                "batch_source": {
                    "artifact_name": "listing",
                    "selector": {"limit": 1},
                },
            }
            self.assertIsNone(materialize_batch_rows_from_source(
                logger, phase=phase, worker_contract=contract,
            ))
            mark_phase_result(
                logger, phase_id="probe", worker_id="browser-probe",
                validation={"status": "done", "artifacts": []},
                result_status="validated_done", phase=phase,
                worker_contract=contract,
            )
            checkpoint = record_replan_checkpoint(
                logger, phase=phase, worker_contract=contract,
                worker_id="browser-probe",
                fast_path_assessment={"status": "candidate", "candidate": {}},
            )
            self.assertTrue(checkpoint["sourceLedgerBound"])
            artifact_path.write_text(
                json.dumps({"name": "listing", "rows": [{"rank": 9}]}),
                encoding="utf-8",
            )
            reconciled = reconcile_replan_checkpoints(logger)
            retired = reconciled["replan_checkpoints"][checkpoint["cohortKey"]]
            self.assertFalse(retired["active"])
            self.assertEqual(retired["terminalReason"], "source_generation_superseded")
            self.assertEqual(active_replan_checkpoints(reconciled), [])

    def test_same_artifact_disjoint_cohorts_remain_legal(self) -> None:
        checkpoint = {
            "checkpointId": "checkpoint-a",
            "cohortKey": "cohort-a",
            "active": True,
            "phaseId": "probe-a",
            "requiredNextRole": "continuation",
            "sourceArtifactName": "listing",
            "cohortSelector": {"field": "rank", "values": [1, 2]},
        }
        state = {
            "replan_checkpoints": {"cohort-a": checkpoint},
            "phases": {"probe-a": {"status": "validated_done"}},
        }
        retained_probe = {"id": "probe-a", "execution_role": "probe"}
        continuation = {
            "id": "continue-a",
            "execution_role": "continuation",
            "depends_on": ["probe-a"],
            "worker_contract": {
                "replan_checkpoint_id": "checkpoint-a",
                "batch_source": {
                    "artifact_name": "listing",
                    "cohort_selector": {"field": "rank", "values": [1, 2]},
                },
            },
        }
        disjoint_probe = {
            "id": "probe-b",
            "execution_role": "probe",
            "worker_contract": {
                "batch_source": {
                    "artifact_name": "listing",
                    "cohort_selector": {"field": "rank", "values": [3, 4]},
                },
            },
        }
        self.assertEqual(replan_checkpoint_plan_errors({
            "replan_checkpoint_ids": ["checkpoint-a"],
            "phases": [retained_probe, continuation, disjoint_probe],
        }, state), [])
        overlapping_probe = copy.deepcopy(disjoint_probe)
        overlapping_probe["worker_contract"]["batch_source"][
            "cohort_selector"
        ]["values"] = [2, 3]
        errors = replan_checkpoint_plan_errors({
            "replan_checkpoint_ids": ["checkpoint-a"],
            "phases": [retained_probe, continuation, overlapping_probe],
        }, state)
        self.assertTrue(any("may overlap" in error for error in errors))

    def test_validated_probe_history_can_remain_in_checkpoint_replan(self) -> None:
        checkpoint = {
            "checkpointId": "probe-checkpoint",
            "cohortKey": "probe-cohort",
            "active": True,
            "phaseId": "probe",
            "requiredNextRole": "validation",
            "sourceArtifactName": "listing",
            "cohortSelector": {"field": "rank", "values": [6, 7]},
        }
        state = {
            "replan_checkpoints": {"probe-cohort": checkpoint},
            "phases": {
                "probe": {"status": "validated_done"},
                "older-history": {"status": "validated_done"},
            },
        }
        common_source = {
            "artifact_name": "listing",
            "cohort_selector": {"field": "rank", "values": [6, 7]},
        }
        retained_probe = {
            "id": "probe",
            "execution_role": "probe",
            "worker_contract": {
                "batch_source": {
                    **common_source,
                    "selector": {"field": "rank", "values": [6]},
                },
            },
        }
        retained_history = {
            "id": "older-history",
            "worker_contract": {"batch_source": dict(common_source)},
        }
        validation = {
            "id": "validation",
            "execution_role": "validation",
            "depends_on": ["probe"],
            "worker_contract": {
                "replan_checkpoint_id": "probe-checkpoint",
                "batch_source": {
                    **common_source,
                    "selector": {"field": "rank", "values": [7]},
                },
            },
        }
        plan = {
            "replan_checkpoint_ids": ["probe-checkpoint"],
            "phases": [retained_probe, retained_history, validation],
        }
        self.assertEqual(replan_checkpoint_plan_errors(plan, state), [])

        missing_predecessor_errors = replan_checkpoint_plan_errors({
            "replan_checkpoint_ids": ["probe-checkpoint"],
            "phases": [validation],
        }, state)
        self.assertTrue(any(
            "requires retaining its validated predecessor phase 'probe'" in error
            and "referencing it from depends_on" in error
            for error in missing_predecessor_errors
        ))

        # Removing the retained predecessor is not a supported workaround:
        # the structural role dependency remains authoritative.
        structurally_invalid = {
            "goal": "validate without predecessor",
            "task_type": "web_scrape",
            "phases": [{
                "id": "validation",
                "type": "browser_worker",
                "objective": "Validate the remaining source row",
                "worker_task": "Validate the remaining source row.",
                "stage_hint": "detail_sections",
                "stage_hint_reason": (
                    "Validate the extraction path against the remaining"
                    " homogeneous source row after a successful probe."
                ),
                "execution_role": "validation",
                "depends_on": ["probe"],
                "expected_artifact": {"fields": ["rank"]},
                "validators": [{
                    "type": "required_fields", "fields": ["rank"],
                }],
                "worker_contract": validation["worker_contract"],
            }],
        }
        normalized, structural_errors = validate_task_plan(structurally_invalid)
        self.assertIsNone(normalized)
        self.assertTrue(any(
            "depends_on references unknown phase id 'probe'" in error
            for error in structural_errors
        ))

    def test_replan_resettable_failed_phase_cannot_bypass_overlap_gate(self) -> None:
        checkpoint = {
            "checkpointId": "checkpoint-a",
            "cohortKey": "cohort-a",
            "active": True,
            "phaseId": "validated-probe",
            "requiredNextRole": "continuation",
            "sourceArtifactName": "listing",
            "cohortSelector": {"field": "rank", "values": [1, 2]},
        }
        state = {
            "replan_checkpoints": {"cohort-a": checkpoint},
            "phases": {"failed-retry": {"status": "phase_failed"}},
        }
        plan = {
            "replan_checkpoint_ids": ["checkpoint-a"],
            "phases": [{
                "id": "continue-a",
                "execution_role": "continuation",
                "worker_contract": {
                    "replan_checkpoint_id": "checkpoint-a",
                    "batch_source": {
                        "artifact_name": "listing",
                        "cohort_selector": {"field": "rank", "values": [1, 2]},
                    },
                },
            }, {
                "id": "failed-retry",
                "execution_role": "probe",
                "worker_contract": {
                    "batch_source": {
                        "artifact_name": "listing",
                        "cohort_selector": {"field": "rank", "values": [1, 2]},
                    },
                },
            }],
        }
        errors = replan_checkpoint_plan_errors(plan, state)
        self.assertTrue(any("failed-retry" in error and "may overlap" in error for error in errors))

    def test_missing_source_and_exhausted_objective_retire_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="checkpoint-mechanical-exits")
            initialize_task_state(logger, {"goal": "exit", "phases": []})
            state = load_task_state(logger)
            state["objective_attempts"] = {"objective-a": {"count": 6}}
            state["replan_checkpoints"] = {
                "missing": {
                    "checkpointId": "missing-id",
                    "cohortKey": "missing",
                    "active": True,
                    "requiredNextRole": "continuation",
                    "sourceLedgerBound": True,
                    "sourceArtifactPath": f"{temp}/missing.json",
                    "validatedSourceIndices": [0],
                    "remainingSourceIndices": [1],
                },
                "exhausted": {
                    "checkpointId": "exhausted-id",
                    "cohortKey": "exhausted",
                    "active": True,
                    "requiredNextRole": "continuation",
                    "sourceLedgerBound": False,
                    "objectiveFingerprint": "objective-a",
                    "validatedSourceIndices": [0],
                    "remainingSourceIndices": [1],
                },
            }
            write_task_state(logger, state)
            reconciled = reconcile_replan_checkpoints(logger)
            self.assertEqual(
                reconciled["replan_checkpoints"]["missing"]["terminalReason"],
                "source_artifact_missing",
            )
            self.assertEqual(
                reconciled["replan_checkpoints"]["exhausted"]["terminalReason"],
                "objective_exhausted",
            )
            self.assertEqual(active_replan_checkpoints(reconciled), [])

    def test_legacy_single_checkpoint_migrates_to_cohort_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = RunLogger(temp, task_id="legacy-checkpoint")
            legacy = {
                "checkpointId": "legacy-id",
                "cohortKey": "legacy-cohort",
                "active": True,
                "requiredNextRole": "validation",
            }
            initialize_task_state(
                logger,
                {"goal": "legacy", "phases": [{"id": "next"}]},
                preserve_from={
                    "replan_checkpoint": legacy,
                    "phases": {},
                },
                replan_reason="migrate",
            )
            state = load_task_state(logger)
            self.assertNotIn("replan_checkpoint", state)
            self.assertEqual(
                state["replan_checkpoints"]["legacy-cohort"]["checkpointId"],
                "legacy-id",
            )

    def test_fleet_readiness_accepts_authoritative_active_status(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class Client:
                def __init__(self):
                    self.calls = []

                async def call(self, method, params):
                    self.calls.append((method, params))
                    return {"data": {"fleetId": fleet_id, "status": "active"}}

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            spawner._slots[slot.slot_id] = slot
            assignment = FleetAssignment(
                worker_id="browser-001",
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                fleet_id=fleet_id,
                assignment_reason="test",
                owner_slot_id=slot.slot_id,
            )
            receipt = await spawner._ensure_assigned_fleet_ready(
                slot, assignment, worker_id="browser-001"
            )
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["verifiedBy"], "status")
            self.assertEqual(slot.client.calls[0][0], "Fleet.status")

        asyncio.run(scenario())

    def test_fleet_ready_event_is_confirmed_by_second_status_rpc(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class Client:
                def __init__(self):
                    self.status_calls = 0

                async def call(self, method, params):
                    self.status_calls += 1
                    if self.status_calls == 1:
                        raise ABCPTransportError(
                            "Fleet open timeout", rpc_code=-32012,
                            rpc_method="Fleet.status",
                        )
                    return {"data": {"fleetId": fleet_id, "status": "active"}}

                async def wait_for_notification(self, predicate, timeout, **kwargs):
                    message = {
                        "method": "System.notification",
                        "params": {"data": {
                            "event": "Fleet.ready",
                            "payload": {"fleetId": fleet_id},
                        }},
                    }
                    self.assert_predicate = predicate(message)
                    return message

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            spawner._slots[slot.slot_id] = slot
            assignment = FleetAssignment(
                worker_id="browser-001", slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id, fleet_id=fleet_id,
                assignment_reason="test", owner_slot_id=slot.slot_id,
            )
            receipt = await spawner._ensure_assigned_fleet_ready(
                slot, assignment, worker_id="browser-001"
            )
            self.assertEqual(receipt["verifiedBy"], "event_then_status")
            self.assertEqual(slot.client.status_calls, 2)
            self.assertTrue(slot.client.assert_predicate)

        asyncio.run(scenario())

    def test_session_restore_without_ready_event_gets_reserved_status_retry(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class Client:
                def __init__(self):
                    self.status_calls = 0

                async def call(self, method, params):
                    self.status_calls += 1
                    if self.status_calls == 1:
                        # Scaled replay of a 33s Fleet.status inside the 45s
                        # readiness budget.
                        await asyncio.sleep(0.08)
                        raise ABCPTransportError(
                            "session restore did not complete in time",
                            rpc_code=-32012,
                            rpc_method="Fleet.status",
                        )
                    return {"data": {"fleetId": fleet_id, "status": "active"}}

                async def wait_for_notification(self, predicate, timeout, **kwargs):
                    await asyncio.sleep(timeout)
                    return None

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            spawner.runtime.harness.fleet_readiness_wait_seconds = 0.12
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            spawner._slots[slot.slot_id] = slot
            assignment = FleetAssignment(
                worker_id="browser-001", slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id, fleet_id=fleet_id,
                assignment_reason="test", owner_slot_id=slot.slot_id,
            )
            receipt = await spawner._ensure_assigned_fleet_ready(
                slot, assignment, worker_id="browser-001"
            )
            self.assertEqual(receipt["verifiedBy"], "status_retry")
            self.assertEqual(slot.client.status_calls, 2)
            self.assertLess(receipt["elapsedMs"], 120)

        asyncio.run(scenario())

    def test_fleet_readiness_stops_after_one_terminal_status_retry(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class Client:
                def __init__(self):
                    self.status_calls = 0

                async def call(self, method, params):
                    self.status_calls += 1
                    raise ABCPTransportError(
                        "session restore did not complete in time",
                        rpc_code=-32012,
                        rpc_method="Fleet.status",
                    )

                async def wait_for_notification(self, predicate, timeout, **kwargs):
                    return None

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            spawner._slots[slot.slot_id] = slot
            assignment = FleetAssignment(
                worker_id="browser-001", slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id, fleet_id=fleet_id,
                assignment_reason="test", owner_slot_id=slot.slot_id,
            )
            with self.assertRaises(ABCPTransportError):
                await spawner._ensure_assigned_fleet_ready(
                    slot, assignment, worker_id="browser-001"
                )
            self.assertEqual(slot.client.status_calls, 2)

        asyncio.run(scenario())

    def test_status_retry_runs_even_when_initial_rpc_exhausts_soft_budget(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class Client:
                def __init__(self):
                    self.status_calls = 0

                async def call(self, method, params):
                    self.status_calls += 1
                    if self.status_calls == 1:
                        await asyncio.sleep(0.04)
                        raise ABCPTransportError(
                            "session restore did not complete in time",
                            rpc_code=-32012,
                            rpc_method="Fleet.status",
                        )
                    return {"data": {"fleetId": fleet_id, "status": "active"}}

                async def wait_for_notification(self, predicate, timeout, **kwargs):
                    await asyncio.sleep(timeout)
                    return None

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            spawner.runtime.harness.fleet_readiness_wait_seconds = 0.03
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            spawner._slots[slot.slot_id] = slot
            assignment = FleetAssignment(
                worker_id="browser-001", slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id, fleet_id=fleet_id,
                assignment_reason="test", owner_slot_id=slot.slot_id,
            )
            receipt = await spawner._ensure_assigned_fleet_ready(
                slot, assignment, worker_id="browser-001"
            )
            self.assertEqual(receipt["verifiedBy"], "status_retry")
            self.assertEqual(slot.client.status_calls, 2)
            self.assertGreaterEqual(receipt["elapsedMs"], 30)

        asyncio.run(scenario())

    def test_failed_event_confirmation_does_not_add_third_status_rpc(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class Client:
                def __init__(self):
                    self.status_calls = 0

                async def call(self, method, params):
                    self.status_calls += 1
                    raise ABCPTransportError(
                        "session restore did not complete in time",
                        rpc_code=-32012,
                        rpc_method="Fleet.status",
                    )

                async def wait_for_notification(self, predicate, timeout, **kwargs):
                    return {
                        "method": "System.notification",
                        "params": {"data": {
                            "event": "Fleet.ready",
                            "payload": {"fleetId": fleet_id},
                        }},
                    }

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            spawner._slots[slot.slot_id] = slot
            assignment = FleetAssignment(
                worker_id="browser-001", slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id, fleet_id=fleet_id,
                assignment_reason="test", owner_slot_id=slot.slot_id,
            )
            with self.assertRaises(ABCPTransportError):
                await spawner._ensure_assigned_fleet_ready(
                    slot, assignment, worker_id="browser-001"
                )
            self.assertEqual(slot.client.status_calls, 2)

        asyncio.run(scenario())

    def test_concurrent_fleet_readiness_uses_single_flight(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"
            release = asyncio.Event()

            class Client:
                def __init__(self):
                    self.status_calls = 0

                async def call(self, method, params):
                    self.status_calls += 1
                    await release.wait()
                    return {"data": {"fleetId": fleet_id, "status": "active"}}

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            spawner._slots[slot.slot_id] = slot

            def assignment(worker_id):
                return FleetAssignment(
                    worker_id=worker_id, slot_id=slot.slot_id,
                    owner_agent_id=slot.agent_id, fleet_id=fleet_id,
                    assignment_reason="test", owner_slot_id=slot.slot_id,
                )

            first = asyncio.create_task(spawner._ensure_assigned_fleet_ready(
                slot, assignment("browser-001"), worker_id="browser-001"
            ))
            await asyncio.sleep(0)
            second = asyncio.create_task(spawner._ensure_assigned_fleet_ready(
                slot, assignment("browser-002"), worker_id="browser-002"
            ))
            await asyncio.sleep(0)
            self.assertEqual(slot.client.status_calls, 1)
            release.set()
            receipts = await asyncio.gather(first, second)
            self.assertEqual(slot.client.status_calls, 1)
            self.assertEqual(
                sorted(receipt["sharedProbe"] for receipt in receipts),
                [False, True],
            )

        asyncio.run(scenario())

    def test_targeted_registry_sync_does_not_probe_unrelated_fleet_pages(self) -> None:
        async def scenario() -> None:
            target = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"
            unrelated = "961e0e6c-b405-45ce-a68d-3796871a3133"

            class Client:
                def __init__(self):
                    self.page_list_fleets = []

                async def call(self, method, params):
                    if method == "Fleet.list":
                        return {"data": [
                            {"fleetId": target}, {"fleetId": unrelated},
                        ]}
                    if method == "Page.list":
                        self.page_list_fleets.append(params["fleetId"])
                        return {"data": []}
                    raise AssertionError(method)

            spawner = _make_spawner(self)
            slot = BrowserAgentSlot("slot-001", "agent-001", client=Client())
            await spawner._sync_slot_registry(
                slot,
                worker_id="browser-001",
                required_fleet_id=target,
                include_page_details=True,
            )
            self.assertEqual(slot.client.page_list_fleets, [target])

        asyncio.run(scenario())

    def test_spawn_constructs_no_worker_before_assigned_fleet_is_ready(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"
            status_entered = asyncio.Event()
            status_release = asyncio.Event()

            class Client:
                def __init__(self):
                    self.on_event = None

                async def call(self, method, params):
                    if method == "System.register":
                        return {"data": {"fleets": [{"fleetId": fleet_id}]}}
                    if method == "Fleet.list":
                        return {"data": [{"fleetId": fleet_id}]}
                    if method == "Fleet.status":
                        status_entered.set()
                        await status_release.wait()
                        return {
                            "data": {"fleetId": fleet_id, "status": "active"}
                        }
                    raise AssertionError(method)

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            slot = BrowserAgentSlot(
                "slot-001", "agent-001", client=Client(), status="idle",
                last_sync_at=10**12,
            )
            spawner._slots[slot.slot_id] = slot

            async def finish_worker(**kwargs):
                return {"status": "done", "workerId": kwargs["worker_id"]}

            spawner._run_browser_worker = finish_worker
            spawn_task = asyncio.create_task(spawner.spawn_browser_agent(
                "collect", fleet_id=fleet_id
            ))
            await asyncio.wait_for(status_entered.wait(), timeout=1.0)
            self.assertEqual(spawner._handles, {})
            status_release.set()
            result = await spawn_task
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["fleetReadiness"]["status"], "ready")
            await spawner._handles[result["workerId"]].async_task

        asyncio.run(scenario())

    def test_delegated_readiness_uses_owner_slot_client(self) -> None:
        async def scenario() -> None:
            fleet_id = "48f0864a-79fb-4ef6-acb2-732e5e1e1818"

            class ActingClient:
                async def call(self, method, params):
                    raise AssertionError("acting client must not probe owner Fleet")

            class OwnerClient:
                def __init__(self):
                    self.calls = 0

                async def call(self, method, params):
                    self.calls += 1
                    return {"data": {"fleetId": fleet_id, "status": "active"}}

            spawner = _make_spawner(self)
            spawner.runtime.harness.fleet_readiness_barrier_enabled = True
            acting = BrowserAgentSlot(
                "slot-002", "agent-002", client=ActingClient()
            )
            owner = BrowserAgentSlot(
                "slot-001", "agent-001", client=OwnerClient()
            )
            spawner._slots = {acting.slot_id: acting, owner.slot_id: owner}
            assignment = FleetAssignment(
                worker_id="browser-002", slot_id=acting.slot_id,
                owner_agent_id=owner.agent_id, fleet_id=fleet_id,
                assignment_reason="delegated-test", owner_slot_id=owner.slot_id,
                delegated=True,
            )
            receipt = await spawner._ensure_assigned_fleet_ready(
                acting, assignment, worker_id="browser-002"
            )
            self.assertEqual(receipt["ownerSlotId"], owner.slot_id)
            self.assertEqual(owner.client.calls, 1)

        asyncio.run(scenario())




if __name__ == "__main__":
    unittest.main()
