import asyncio
import json
import tempfile
import unittest

from abcp_client import ABCPClientConfig
from harness.config import HarnessConfig, RuntimeConfig
from harness.spawner import BrowserAgentSlot, BrowserAgentSpawner
from harness.utils import RunLogger
from llm.config import ModelConfig


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


if __name__ == "__main__":
    unittest.main()
