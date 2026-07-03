"""
harness.spawner - Worker BrowserAgent spawning and lifecycle management.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

from abcp_client import ABCPClient, ABCPTransportError
from harness.constants import (
    WORKER_STATUS_CANCELLED,
    WORKER_STATUS_FAILED,
)
from harness.diagnostics import status_category
from harness.render_recovery import extract_page_id_from_values
from harness.config import RuntimeConfig
from harness.lifecycle import LifecycleContext, default_lifecycle_manager
from harness.model_config import browser_agent_model_config
from harness.schema_cache import global_schemas_dir
from harness.schema_loader import CapabilityBundle, load_capability_bundle
from harness.task_control import (
    build_attempt_digest,
    classification_for_worker_status,
    contract_hash_for_phase,
    mark_phase_result,
    mark_phase_running,
    phase_prior_artifact_paths,
    repeated_phase_attempt_guard,
    validate_worker_artifacts,
)
from harness.strategy_telemetry import append_strategy_attempt
from harness.tool_policy import ALWAYS_FORBIDDEN_ABCP_METHODS
from harness.templates import get_path
from harness.utils import (
    JsonDict,
    RunLogger,
    build_static_context_block,
    extract_offloaded_paths,
    make_browser_event_logger,
    optional_float,
    optional_int,
    safe_path_component,
    task_subdir,
    trim_large_strings,
)
from harness.worker_result import build_worker_result_levels
from llm import LLMFactory


BrowserAgentFactory = Callable[[Any, ABCPClient, RuntimeConfig, RunLogger], Any]


@dataclass
class BrowserAgentHandle:
    worker_id: str
    agent_id: str
    name: str
    task: str
    context: str
    result_contract: str
    phase_id: Optional[str]
    worker_contract: JsonDict
    async_task: Any
    slot_id: Optional[str] = None


@dataclass
class BrowserAgentSlot:
    slot_id: str
    agent_id: str
    client: Optional[ABCPClient] = None
    registration: JsonDict = field(default_factory=dict)
    status: str = "new"
    current_worker_id: Optional[str] = None
    last_worker_id: Optional[str] = None
    last_phase_id: Optional[str] = None
    last_task_type: str = ""
    last_contract_hash: str = ""
    last_result_summary: JsonDict = field(default_factory=dict)
    last_sync_at: float = 0.0
    fleet_ids: Set[str] = field(default_factory=set)
    page_registry: Dict[str, JsonDict] = field(default_factory=dict)
    page_quarantine: Dict[str, JsonDict] = field(default_factory=dict)
    origins: Set[str] = field(default_factory=set)
    sync_errors: List[str] = field(default_factory=list)
    idle_event_logger: Optional[Callable[[str, JsonDict], None]] = None


URL_RE = re.compile(r"https?://[^\s\"'<>]+")
SLOT_FULL_SYNC_TTL_SECONDS = 30.0


class BrowserAgentSpawner:
    """Creates isolated browser agents and manages their lifecycle."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        logger: RunLogger,
        browser_agent_factory: BrowserAgentFactory,
    ):
        self.runtime = runtime
        self.browser_agent_factory = browser_agent_factory
        self.logger = logger
        self._handles: Dict[str, BrowserAgentHandle] = {}
        self._slots: Dict[str, BrowserAgentSlot] = {}
        self._counter = 0
        self._slot_counter = 0
        self.static_context_block, self.static_context_hash = build_static_context_block(
            self.runtime.harness.context_file
        )
        self.lifecycle = default_lifecycle_manager()
        self._capability_bundle: Optional[CapabilityBundle] = None
        self._capability_bundle_lock = None

    async def spawn_browser_agent(
        self,
        task: str,
        context: str = "",
        name: Optional[str] = None,
        max_steps: Optional[int] = None,
        result_contract: str = "",
        phase_id: Optional[str] = None,
        worker_contract: Optional[JsonDict] = None,
        phase: Optional[JsonDict] = None,
        preferred_slot_id: Optional[str] = None,
        reuse_from_worker_id: Optional[str] = None,
    ) -> JsonDict:
        effective_contract = worker_contract or {}
        current_contract_hash = contract_hash_for_phase(
            phase,
            effective_contract,
            task=task,
            result_contract=result_contract,
        )
        repeated_guard = repeated_phase_attempt_guard(
            self.logger,
            phase_id=phase_id,
            contract_hash=current_contract_hash,
        )
        if repeated_guard is not None:
            self.logger.write("spawner.browser.repeated_guard", repeated_guard)
            return repeated_guard

        worker_id = self._next_id("browser")
        agent_name = name or worker_id
        try:
            slot = await self._acquire_slot(
                worker_id=worker_id,
                phase_id=phase_id,
                task=task,
                context=context,
                result_contract=result_contract,
                worker_contract=effective_contract,
                contract_hash=current_contract_hash,
                preferred_slot_id=preferred_slot_id,
                reuse_from_worker_id=reuse_from_worker_id,
            )
        except Exception as exc:
            result = {
                "status": "failed",
                "error": str(exc),
                "workerId": worker_id,
                "name": agent_name,
            }
            self.logger.write("spawner.slot.acquire_failed", result)
            return result
        if isinstance(slot, dict):
            return slot

        expose_reusable_pages = bool(
            str(preferred_slot_id or "").strip()
            or str(reuse_from_worker_id or "").strip()
        )
        async_task = asyncio.create_task(
            self._run_browser_worker(
                slot=slot,
                expose_reusable_pages=expose_reusable_pages,
                worker_id=worker_id,
                name=agent_name,
                task=task,
                context=context,
                max_steps=optional_int(max_steps),
                result_contract=result_contract,
                phase_id=phase_id,
                worker_contract=effective_contract,
                phase=phase or {},
            )
        )
        mark_phase_running(
            self.logger,
            phase_id=phase_id,
            worker_id=worker_id,
            worker_name=agent_name,
        )
        self._handles[worker_id] = BrowserAgentHandle(
            worker_id=worker_id,
            agent_id=slot.agent_id,
            name=agent_name,
            task=task,
            context=context,
            result_contract=result_contract,
            phase_id=phase_id,
            worker_contract=effective_contract,
            async_task=async_task,
            slot_id=slot.slot_id,
        )
        self.logger.write(
            "spawner.browser.spawn",
            {
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "slotReuse": bool(slot.last_worker_id),
                "pageReuseAllowed": expose_reusable_pages,
                "name": agent_name,
                "task": task,
                "resultContract": result_contract,
                "phaseId": phase_id,
                "workerContract": trim_large_strings(effective_contract, 2000),
                "contractHash": current_contract_hash,
            },
        )
        return {
            "status": "running",
            "workerId": worker_id,
            "agentId": slot.agent_id,
            "slotId": slot.slot_id,
            "name": agent_name,
            "phaseId": phase_id,
        }

    async def _acquire_slot(
        self,
        *,
        worker_id: str,
        phase_id: Optional[str],
        task: str,
        context: str,
        result_contract: str,
        worker_contract: JsonDict,
        contract_hash: str,
        preferred_slot_id: Optional[str],
        reuse_from_worker_id: Optional[str],
    ) -> Any:
        self._cleanup_retired_slots()
        max_slots = (
            optional_int(
                getattr(self.runtime.harness, "max_browser_agent_instances", None),
                0,
            )
            or optional_int(self.runtime.harness.max_browser_agents, 0)
            or 3
        )
        running_slots = [
            slot for slot in self._slots.values()
            if slot.status == "running"
        ]
        live_slots = [
            slot for slot in self._slots.values()
            if slot.status not in {"broken", "closed"}
        ]
        if len(running_slots) >= self.runtime.harness.max_browser_agents:
            return {
                "status": "rejected",
                "error": "Reached the max_browser_agents limit",
                "running": len(running_slots),
                "max_browser_agents": self.runtime.harness.max_browser_agents,
                "max_browser_agent_instances": max_slots,
                "limit_semantics": {
                    "max_browser_agents": "maximum concurrently running BrowserAgent workers",
                    "max_browser_agent_instances": "maximum live reusable BrowserAgent slots",
                },
                "slots": [self._slot_summary(slot) for slot in self._slots.values()],
                "next_instruction": (
                    "Do not create another worker now. Call wait_browser_agents"
                    " for one running worker to finish, then reuse an idle slot."
                ),
            }

        explicit_rejection = self._explicit_slot_rejection(
            preferred_slot_id=preferred_slot_id,
            reuse_from_worker_id=reuse_from_worker_id,
        )
        if explicit_rejection is not None:
            return explicit_rejection

        slot = self._select_idle_slot(
            phase_id=phase_id,
            task=task,
            context=context,
            result_contract=result_contract,
            worker_contract=worker_contract,
            preferred_slot_id=preferred_slot_id,
            reuse_from_worker_id=reuse_from_worker_id,
        )
        if slot is None:
            if len(live_slots) >= max_slots:
                return {
                    "status": "rejected",
                    "error": "No idle BrowserAgent slot available",
                    "running": len(running_slots),
                    "max_browser_agents": self.runtime.harness.max_browser_agents,
                    "max_browser_agent_instances": max_slots,
                    "limit_semantics": {
                        "max_browser_agents": "maximum concurrently running BrowserAgent workers",
                        "max_browser_agent_instances": "maximum live reusable BrowserAgent slots",
                    },
                    "slots": [self._slot_summary(item) for item in self._slots.values()],
                    "next_instruction": (
                        "The slot pool is full. Call wait_browser_agents, then"
                        " spawn the continuation with reuse_from_worker_id or"
                        " preferred_slot_id for the related idle slot."
                    ),
                }
            slot = await self._create_slot()

        slot.status = "running"
        slot.current_worker_id = worker_id
        slot.last_contract_hash = contract_hash
        return slot

    def _explicit_slot_rejection(
        self,
        *,
        preferred_slot_id: Optional[str],
        reuse_from_worker_id: Optional[str],
    ) -> Optional[JsonDict]:
        preferred = str(preferred_slot_id or "").strip()
        if preferred:
            slot = self._slots.get(preferred)
            if slot is None:
                return {
                    "status": "rejected",
                    "error": f"preferred_slot_id not found: {preferred}",
                    "slots": [self._slot_summary(item) for item in self._slots.values()],
                }
            if slot.status != "idle":
                return {
                    "status": "rejected",
                    "error": f"preferred_slot_id is not idle: {preferred}",
                    "slot": self._slot_summary(slot),
                    "next_instruction": (
                        "Call wait_browser_agents for the slot's running worker,"
                        " then retry with the same preferred_slot_id."
                    ),
                }
        reuse_worker = str(reuse_from_worker_id or "").strip()
        if reuse_worker:
            handle = self._handles.get(reuse_worker)
            if handle is None or not handle.slot_id:
                return {
                    "status": "rejected",
                    "error": f"reuse_from_worker_id not found: {reuse_worker}",
                    "slots": [self._slot_summary(item) for item in self._slots.values()],
                }
            slot = self._slots.get(handle.slot_id)
            if slot is None:
                return {
                    "status": "rejected",
                    "error": f"slot for reuse_from_worker_id not found: {reuse_worker}",
                    "workerId": reuse_worker,
                    "slots": [self._slot_summary(item) for item in self._slots.values()],
                }
            if slot.status != "idle":
                return {
                    "status": "rejected",
                    "error": f"slot for reuse_from_worker_id is not idle: {reuse_worker}",
                    "workerId": reuse_worker,
                    "slot": self._slot_summary(slot),
                    "next_instruction": (
                        "Wait for the related worker/slot to finish before"
                        " spawning this continuation."
                    ),
                }
        return None

    def _select_idle_slot(
        self,
        *,
        phase_id: Optional[str],
        task: str,
        context: str,
        result_contract: str,
        worker_contract: JsonDict,
        preferred_slot_id: Optional[str],
        reuse_from_worker_id: Optional[str],
    ) -> Optional[BrowserAgentSlot]:
        idle_slots = [
            slot for slot in self._slots.values()
            if slot.status == "idle"
        ]
        if not idle_slots:
            return None

        preferred = str(preferred_slot_id or "").strip()
        if preferred:
            slot = self._slots.get(preferred)
            if slot is not None and slot.status == "idle":
                return slot

        reuse_worker = str(reuse_from_worker_id or "").strip()
        if reuse_worker:
            handle = self._handles.get(reuse_worker)
            if handle is not None and handle.slot_id:
                slot = self._slots.get(handle.slot_id)
                if slot is not None and slot.status == "idle":
                    return slot

        task_origins = _origins_from_text(
            "\n".join([task, context, result_contract, json.dumps(worker_contract, default=str)])
        )
        scored = [
            (
                self._slot_relevance_score(
                    slot,
                    phase_id=phase_id,
                    task_origins=task_origins,
                    worker_contract=worker_contract,
                ),
                slot,
            )
            for slot in idle_slots
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1] if scored else None

    def _slot_relevance_score(
        self,
        slot: BrowserAgentSlot,
        *,
        phase_id: Optional[str],
        task_origins: Set[str],
        worker_contract: JsonDict,
    ) -> int:
        score = 0
        phase_text = str(phase_id or "")
        if phase_text and slot.last_phase_id == phase_text:
            score += 40
        elif phase_text and _phase_family(slot.last_phase_id) == _phase_family(phase_text):
            score += 24
        task_type = str(worker_contract.get("task_type") or "")
        if task_type and slot.last_task_type == task_type:
            score += 12
        overlap = task_origins.intersection(slot.origins)
        score += min(len(overlap), 3) * 8
        if slot.last_result_summary.get("validatedStatus") == "validated_done":
            score += 4
        if slot.last_result_summary.get("status") in {"done", "partial"}:
            score += 3
        return score

    async def _create_slot(self) -> BrowserAgentSlot:
        slot_id = self._next_slot_id()
        agent_id = f"{self.runtime.agent_id}-{slot_id}"
        event_logger = make_browser_event_logger(
            self.logger,
            self.runtime.harness.log_browser_payloads,
            prefix=f"{slot_id}.transport",
        )
        client = ABCPClient(self.runtime.browser, on_event=event_logger)
        await client.connect()
        slot = BrowserAgentSlot(
            slot_id=slot_id,
            agent_id=agent_id,
            client=client,
            status="idle",
            idle_event_logger=event_logger,
        )
        try:
            registration = await client.call("System.register", {"agentId": agent_id})
        except Exception:
            await client.close()
            raise
        slot.registration = registration
        self._update_slot_registry_from_value(slot, registration)
        self._slots[slot_id] = slot
        self.logger.write(
            "spawner.slot.created",
            self._slot_summary(slot),
        )
        return slot

    def _cleanup_retired_slots(self) -> None:
        retired = [
            slot_id
            for slot_id, slot in self._slots.items()
            if slot.status in {"broken", "closed"} and not slot.current_worker_id
        ]
        for slot_id in retired:
            slot = self._slots.pop(slot_id, None)
            if slot is not None:
                self.logger.write(
                    "spawner.slot.retired",
                    self._slot_summary(slot),
                )

    async def _prepare_slot_for_worker(
        self,
        slot: BrowserAgentSlot,
        worker_id: str,
        *,
        expose_reusable_pages: bool,
    ) -> JsonDict:
        if slot.client is None:
            raise ABCPTransportError(f"Slot {slot.slot_id} has no browser client")
        registration = await slot.client.call(
            "System.register",
            {"agentId": slot.agent_id},
        )
        slot.registration = registration
        self._update_slot_registry_from_value(slot, registration)
        if expose_reusable_pages or self._slot_sync_due(slot):
            await self._sync_slot_registry(slot, worker_id=worker_id)
        return registration

    def _slot_sync_due(self, slot: BrowserAgentSlot) -> bool:
        if slot.last_sync_at <= 0:
            return True
        if slot.sync_errors:
            return True
        return (time.monotonic() - slot.last_sync_at) >= SLOT_FULL_SYNC_TTL_SECONDS

    async def _sync_slot_registry(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
    ) -> None:
        if slot.client is None:
            return
        slot.sync_errors = []
        try:
            fleet_response = await slot.client.call("Fleet.list", {})
            self._update_slot_registry_from_value(slot, fleet_response)
        except Exception as exc:
            slot.sync_errors.append(f"Fleet.list: {str(exc)[:240]}")

        # Self-heal: drop registry keys that are obviously not real ids. A
        # leaked schema-dict repr (pre-as_id-guard bug, or any future leak) is
        # str({...}) and starts with '{'; calling Page.list/Page.getState on it
        # returns -32602 and just pollutes sync_errors. Real fleet/page ids are
        # UUIDs and never start with '{'.
        slot.fleet_ids = {
            fid for fid in slot.fleet_ids
            if isinstance(fid, str) and fid and not fid.startswith("{")
        }
        bogus_page_ids = [
            pid for pid in list(slot.page_registry)
            if not isinstance(pid, str) or pid.startswith("{")
        ]
        for pid in bogus_page_ids:
            slot.page_registry.pop(pid, None)
            slot.page_quarantine.pop(pid, None)

        for fleet_id in sorted(slot.fleet_ids)[:6]:
            try:
                pages_response = await slot.client.call(
                    "Page.list",
                    {"fleetId": fleet_id},
                )
                self._replace_fleet_pages_from_list(
                    slot,
                    fleet_id=fleet_id,
                    pages_response=pages_response,
                )
                self._update_slot_registry_from_value(slot, pages_response)
            except Exception as exc:
                slot.sync_errors.append(f"Page.list({fleet_id}): {str(exc)[:240]}")

        for page_id in sorted(slot.page_registry.keys())[:12]:
            try:
                state_response = await slot.client.call(
                    "Page.getState",
                    {
                        "pageId": page_id,
                        "purpose": (
                            "Synchronize reusable slot page state before assigning"
                            f" worker {worker_id}."
                        ),
                    },
                )
                state_data = (
                    state_response.get("data")
                    if isinstance(state_response, dict)
                    and isinstance(state_response.get("data"), dict)
                    else {}
                )
                self._update_slot_registry_from_value(
                    slot,
                    {"pageId": page_id, **state_data, "state": state_response},
                )
                if _state_response_indicates_paused(state_response):
                    self._mark_page_quarantined(
                        slot,
                        page_id,
                        reason="Page.getState reports the page is still paused.",
                        worker_id=worker_id,
                        status="paused",
                    )
                else:
                    self._clear_page_quarantine(
                        slot,
                        page_id,
                        reason="Page.getState confirmed the page is usable.",
                    )
                    self._mark_page_fresh(slot, page_id)
            except Exception as exc:
                error_text = str(exc)[:240]
                if _text_indicates_paused_error(error_text):
                    self._mark_page_quarantined(
                        slot,
                        page_id,
                        reason=error_text,
                        worker_id=worker_id,
                        status="paused",
                    )
                else:
                    page = dict(slot.page_registry.get(page_id) or {"pageId": page_id})
                    page["status"] = "stale"
                    page["lastStateError"] = error_text
                    slot.page_registry[page_id] = page
                slot.sync_errors.append(f"Page.getState({page_id}): {str(exc)[:240]}")
        slot.last_sync_at = time.monotonic()
        if slot.sync_errors:
            self.logger.write(
                "spawner.slot.sync_warning",
                {
                    "slotId": slot.slot_id,
                    "workerId": worker_id,
                    "errors": slot.sync_errors[-5:],
                },
            )

    def _render_slot_context(
        self,
        slot: BrowserAgentSlot,
        *,
        expose_reusable_pages: bool,
    ) -> str:
        payload = self._slot_context_summary(
            slot,
            expose_reusable_pages=expose_reusable_pages,
        )
        payload["reuseRules"] = [
            "Input.* actions are focus-affecting and must be serialized.",
            "After any Page.switchTo, Page.create, or Page.navigate, refresh DOM.getAXTree before targeting elements.",
        ]
        if expose_reusable_pages:
            payload["reuseRules"].extend([
                (
                    "This is an explicit continuation. Existing pageIds are"
                    " candidates only; verify with Page.getState/Page.switchTo"
                    " before acting."
                ),
                (
                    "Use an existing page only when it clearly belongs to this"
                    " continuation; otherwise create a fresh page."
                ),
            ])
        else:
            payload["reuseRules"].extend([
                (
                    "This assignment reuses only the browser connection. Do not"
                    " use pageIds left by previous workers."
                ),
                (
                    "Start browser work by creating or navigating a fresh page"
                    " for this task, then use Page.getState to record its current"
                    " pageId/fleetId/url/title/status."
                ),
            ])
        return (
            "<slot_context>\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n"
            "</slot_context>"
        )

    def _replace_fleet_pages_from_list(
        self,
        slot: BrowserAgentSlot,
        *,
        fleet_id: str,
        pages_response: Any,
    ) -> None:
        page_items = self._extract_page_items(pages_response)
        if page_items is None:
            return
        current_page_ids = {
            str(item.get("pageId") or item.get("page_id") or "")
            for item in page_items
            if isinstance(item, dict)
        }
        current_page_ids.discard("")
        removed_page_ids = {
            page_id
            for page_id, page in slot.page_registry.items()
            if (
                str(page.get("fleetId") or "") == fleet_id
                and page_id not in current_page_ids
            )
        }
        slot.page_registry = {
            page_id: page
            for page_id, page in slot.page_registry.items()
            if (
                str(page.get("fleetId") or "") != fleet_id
                or page_id in current_page_ids
            )
        }
        for page_id in removed_page_ids:
            slot.page_quarantine.pop(page_id, None)
        for item in page_items:
            if not isinstance(item, dict):
                continue
            page_id = str(item.get("pageId") or item.get("page_id") or "")
            if not page_id:
                continue
            page = dict(slot.page_registry.get(page_id) or {})
            self._clear_stale_fields(page)
            page.update(item)
            self._clear_stale_fields(page)
            page["pageId"] = page_id
            page["fleetId"] = str(page.get("fleetId") or fleet_id)
            self._apply_page_quarantine(slot, page_id, page)
            slot.page_registry[page_id] = page

    def _mark_page_fresh(self, slot: BrowserAgentSlot, page_id: str) -> None:
        page = slot.page_registry.get(page_id)
        if isinstance(page, dict):
            self._clear_stale_fields(page)

    def _clear_stale_fields(self, page: JsonDict) -> None:
        page.pop("lastStateError", None)
        if str(page.get("status") or "") == "stale":
            page.pop("status", None)

    def _mark_page_quarantined(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        *,
        reason: str,
        worker_id: str = "",
        phase_id: Optional[str] = None,
        status: str = "stale_pause_deadlock",
    ) -> None:
        page_id = str(page_id or "").strip()
        if not page_id:
            return
        quarantine = {
            "pageId": page_id,
            "status": status or "quarantined",
            "reason": str(reason or "")[:300],
            "workerId": str(worker_id or "")[:120],
            "phaseId": str(phase_id or "")[:120],
            "quarantinedAt": time.time(),
            "doNotUse": True,
        }
        slot.page_quarantine[page_id] = quarantine
        page = dict(slot.page_registry.get(page_id) or {"pageId": page_id})
        self._apply_page_quarantine(slot, page_id, page)
        slot.page_registry[page_id] = page
        self.logger.write(
            "spawner.slot.page_quarantined",
            {
                "slotId": slot.slot_id,
                "pageId": page_id,
                "reason": quarantine["reason"],
                "status": quarantine["status"],
                "workerId": quarantine["workerId"],
                "phaseId": quarantine["phaseId"],
            },
        )

    def _clear_page_quarantine(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        *,
        reason: str = "",
    ) -> None:
        page_id = str(page_id or "").strip()
        if not page_id or page_id not in slot.page_quarantine:
            return
        slot.page_quarantine.pop(page_id, None)
        page = slot.page_registry.get(page_id)
        if isinstance(page, dict):
            page.pop("quarantineReason", None)
            page.pop("quarantineStatus", None)
            page.pop("quarantinedAt", None)
            page.pop("doNotUse", None)
            if str(page.get("status") or "") == "quarantined":
                page.pop("status", None)
        self.logger.write(
            "spawner.slot.page_quarantine_cleared",
            {
                "slotId": slot.slot_id,
                "pageId": page_id,
                "reason": str(reason or "")[:300],
            },
        )

    def _apply_page_quarantine(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        page: JsonDict,
    ) -> None:
        quarantine = slot.page_quarantine.get(page_id)
        if not isinstance(quarantine, dict):
            return
        page["status"] = "quarantined"
        page["quarantineStatus"] = quarantine.get("status") or "quarantined"
        page["quarantineReason"] = quarantine.get("reason") or ""
        page["quarantinedAt"] = quarantine.get("quarantinedAt")
        page["doNotUse"] = True

    def _extract_page_items(self, value: Any) -> Optional[List[JsonDict]]:
        def normalize_list(items: Any) -> Optional[List[JsonDict]]:
            if not isinstance(items, list):
                return None
            pages = [item for item in items if isinstance(item, dict)]
            if not pages:
                return []
            if any(item.get("pageId") or item.get("page_id") for item in pages):
                return pages
            return None

        if isinstance(value, dict):
            for key in ("pages", "tabs"):
                pages = normalize_list(value.get(key))
                if pages is not None:
                    return pages
            data = value.get("data")
            if isinstance(data, dict):
                for key in ("pages", "tabs"):
                    pages = normalize_list(data.get(key))
                    if pages is not None:
                        return pages
            elif isinstance(data, list):
                pages = normalize_list(data)
                if pages is not None:
                    return pages
        return normalize_list(value)

    def _update_slot_after_worker(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        phase_id: Optional[str],
        worker_contract: JsonDict,
        result: JsonDict,
        trace: List[JsonDict],
    ) -> None:
        self._record_slot_result(
            slot,
            worker_id=worker_id,
            phase_id=phase_id,
            worker_contract=worker_contract,
            result=result,
            trace=trace,
        )
        self._mark_slot_idle(slot, worker_id=worker_id, result=result)

    def _record_slot_result(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        phase_id: Optional[str],
        worker_contract: JsonDict,
        result: JsonDict,
        trace: Optional[List[JsonDict]] = None,
    ) -> None:
        if trace is not None:
            self._update_slot_registry_from_trace(slot, trace)
        slot.last_phase_id = phase_id
        slot.last_task_type = str(worker_contract.get("task_type") or "")
        slot.last_result_summary = {
            "workerId": worker_id,
            "phaseId": phase_id,
            "status": result.get("status"),
            "statusCategory": result.get("statusCategory"),
            "validatedStatus": result.get("validatedStatus"),
            "artifactCount": len(result.get("artifacts") or []),
            "traceSummary": trim_large_strings(result.get("traceSummary") or {}, 2000),
        }
        self._quarantine_deadlock_page_from_result(
            slot,
            worker_id=worker_id,
            phase_id=phase_id,
            result=result,
        )

    def _quarantine_deadlock_page_from_result(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        phase_id: Optional[str],
        result: JsonDict,
    ) -> None:
        if str(result.get("status") or "") != "stale_pause_deadlock":
            return
        diagnostics = result.get("diagnostics")
        page_id = ""
        if isinstance(diagnostics, dict):
            page_id = str(diagnostics.get("last_pause_pageId") or "").strip()
        if not page_id:
            return
        self._mark_page_quarantined(
            slot,
            page_id,
            reason=(
                "Worker ended with stale_pause_deadlock; do not reuse this"
                " paused page unless a later Page.getState confirms it is usable."
            ),
            worker_id=worker_id,
            phase_id=phase_id,
            status="stale_pause_deadlock",
        )

    def _mark_slot_idle(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        result: JsonDict,
    ) -> None:
        slot.last_worker_id = worker_id
        slot.current_worker_id = None
        if slot.status not in {"broken", "closed"}:
            slot.status = "idle"
        if slot.client is not None and slot.idle_event_logger is not None:
            slot.client.on_event = slot.idle_event_logger
        self.logger.write(
            "spawner.slot.released",
            self._slot_summary(slot),
        )

    def _update_slot_registry_from_trace(
        self,
        slot: BrowserAgentSlot,
        trace: List[JsonDict],
    ) -> None:
        for item in trace or []:
            if not isinstance(item, dict) or item.get("type") != "browser_call":
                continue
            method = str(item.get("method") or "")
            params = item.get("params")
            if method == "Page.close" and isinstance(params, dict):
                page_id = str(params.get("pageId") or "")
                if page_id:
                    slot.page_registry.pop(page_id, None)
                    slot.page_quarantine.pop(page_id, None)
            if method == "Fleet.close" and isinstance(params, dict):
                fleet_id = str(params.get("fleetId") or "")
                if fleet_id:
                    slot.fleet_ids.discard(fleet_id)
                    removed_page_ids = [
                        page_id
                        for page_id, page in slot.page_registry.items()
                        if str(page.get("fleetId") or "") == fleet_id
                    ]
                    slot.page_registry = {
                        page_id: page
                        for page_id, page in slot.page_registry.items()
                        if str(page.get("fleetId") or "") != fleet_id
                    }
                    for page_id in removed_page_ids:
                        slot.page_quarantine.pop(page_id, None)
            self._update_slot_registry_from_value(slot, params)
            self._update_slot_registry_from_value(slot, item.get("result"))

    def _update_slot_registry_from_value(
        self,
        slot: BrowserAgentSlot,
        value: Any,
    ) -> None:
        def as_id(raw: Any) -> str:
            # Real fleet/page ids are string scalars (UUIDs). A dict/list value
            # here is a JSON-schema fragment echoed inside an attached
            # methodSchema.params (e.g. {"pageId": {"type":"string","pattern":...}}
            # from describeAction). str()-coercing it would poison page_registry
            # / fleet_ids with a bogus key that later makes Page.getState /
            # Page.list fail with -32602. Only accept real string ids.
            return raw if isinstance(raw, str) and raw else ""

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                fleet_id = as_id(item.get("fleetId") or item.get("fleet_id"))
                page_id = as_id(item.get("pageId") or item.get("page_id"))
                url = str(item.get("url") or item.get("currentUrl") or "")
                title = str(item.get("title") or "")
                status = str(item.get("status") or "")
                if fleet_id:
                    slot.fleet_ids.add(fleet_id)
                if page_id:
                    page = dict(slot.page_registry.get(page_id) or {})
                    page["pageId"] = page_id
                    if fleet_id:
                        page["fleetId"] = fleet_id
                    if url:
                        page["url"] = url
                        origin = _origin_from_url(url)
                        if origin:
                            page["origin"] = origin
                            slot.origins.add(origin)
                    if title:
                        page["title"] = title
                    if status:
                        page["status"] = status
                    self._apply_page_quarantine(slot, page_id, page)
                    slot.page_registry[page_id] = page
                # methodSchema echoes describeAction param specs (dict values for
                # pageId/fleetId/id/etc.); skip it so we never recurse into
                # schema fragments. The as_id guard already neutralizes them, but
                # this also avoids walking a large, meaningless schema subtree.
                for key, nested in item.items():
                    if key == "methodSchema":
                        continue
                    visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)

        visit(value)

    def _slot_summary(self, slot: BrowserAgentSlot) -> JsonDict:
        return {
            "slotId": slot.slot_id,
            "agentId": slot.agent_id,
            "status": slot.status,
            "currentWorkerId": slot.current_worker_id,
            "lastWorkerId": slot.last_worker_id,
            "lastPhaseId": slot.last_phase_id,
            "lastTaskType": slot.last_task_type,
            "fleetIds": sorted(slot.fleet_ids),
            "origins": sorted(slot.origins),
            "pages": [
                dict(page)
                for page in list(slot.page_registry.values())[:20]
            ],
            "quarantinedPages": [
                dict(page)
                for page in list(slot.page_quarantine.values())[:20]
            ],
            "syncErrors": slot.sync_errors[-5:],
            "lastResult": slot.last_result_summary,
        }

    def _slot_context_summary(
        self,
        slot: BrowserAgentSlot,
        *,
        expose_reusable_pages: bool,
    ) -> JsonDict:
        payload = {
            "slotId": slot.slot_id,
            "agentId": slot.agent_id,
            "status": slot.status,
            "lastWorkerId": slot.last_worker_id,
            "lastPhaseId": slot.last_phase_id,
            "lastTaskType": slot.last_task_type,
            "pageReuseMode": (
                "explicit_continuation" if expose_reusable_pages else "fresh_page_required"
            ),
            "existingPageCount": len(slot.page_registry),
            "quarantinedPageCount": len(slot.page_quarantine),
            "fleetCount": len(slot.fleet_ids),
            "originCount": len(slot.origins),
            "syncErrors": slot.sync_errors[-5:],
            "isolation": (
                "Only browser connection and page registry are reused. Worker"
                " AXTree snapshots, diagnostics, progress, artifacts, and challenge"
                " state are reset for this assignment."
            ),
        }
        if not expose_reusable_pages:
            return payload
        payload["fleetIds"] = sorted(slot.fleet_ids)
        payload["origins"] = sorted(slot.origins)
        payload["pages"] = [
            dict(page)
            for page in list(slot.page_registry.values())[:20]
            if not _page_hidden_from_reuse(slot, page)
        ]
        payload["stalePages"] = [
            {
                "pageId": page.get("pageId"),
                "fleetId": page.get("fleetId"),
                "url": page.get("url"),
                "lastStateError": page.get("lastStateError"),
            }
            for page in list(slot.page_registry.values())[:20]
            if str(page.get("status") or "") == "stale"
        ]
        payload["quarantinedPages"] = [
            {
                "pageId": quarantine.get("pageId"),
                "status": quarantine.get("status"),
                "reason": quarantine.get("reason"),
                "workerId": quarantine.get("workerId"),
                "phaseId": quarantine.get("phaseId"),
                "doNotUse": True,
            }
            for quarantine in list(slot.page_quarantine.values())[:20]
        ]
        return payload

    async def wait_browser_agents(
        self,
        worker_ids: Optional[List[str]] = None,
        mode: str = "all",
        timeout_seconds: Optional[float] = None,
    ) -> JsonDict:
        self._cleanup_retired_slots()
        handles = self._select_handles(worker_ids)
        if not handles:
            return {"status": "empty", "completed": [], "pending": []}

        tasks = [handle.async_task for handle in handles]
        return_when = (
            asyncio.FIRST_COMPLETED if mode == "first" else asyncio.ALL_COMPLETED
        )
        done, pending = await asyncio.wait(
            tasks,
            timeout=optional_float(timeout_seconds),
            return_when=return_when,
        )

        completed = [
            self._task_result(handle)
            for handle in handles
            if handle.async_task in done or handle.async_task.done()
        ]
        pending_ids = [
            handle.worker_id
            for handle in handles
            if handle.async_task in pending and not handle.async_task.done()
        ]
        self._cleanup_retired_slots()
        return {
            "status": "done" if not pending_ids else "partial",
            "completed": completed,
            "pending": pending_ids,
            "slots": [
                self._slot_summary(slot)
                for slot in self._slots.values()
            ],
        }

    def list_browser_agents(self) -> JsonDict:
        self._cleanup_retired_slots()
        agents = []
        for handle in self._handles.values():
            if handle.async_task.cancelled():
                status = "cancelled"
            elif handle.async_task.done():
                result = self._task_result(handle)
                status = result.get("status", "done")
            else:
                status = "running"
            agents.append({
                "workerId": handle.worker_id,
                "agentId": handle.agent_id,
                "slotId": handle.slot_id,
                "name": handle.name,
                "phaseId": handle.phase_id,
                "status": status,
                "task": handle.task,
            })
        return {
            "status": "done",
            "agents": agents,
            "slots": [
                self._slot_summary(slot)
                for slot in self._slots.values()
            ],
        }

    async def shutdown(self) -> None:
        pending = [
            handle.async_task for handle in self._handles.values()
            if not handle.async_task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for slot in list(self._slots.values()):
            slot.status = "closed"
            slot.current_worker_id = None
            if slot.client is not None:
                await slot.client.close()

    def _get_skill_registry(self):
        """Lazy-load the skill registry once per spawner."""
        registry = getattr(self, "_skill_registry", None)
        if registry is None:
            try:
                from harness.skill.registry import SkillRegistry
                registry = SkillRegistry.load()
            except Exception as exc:  # registry load must never break spawning
                self.logger.write("skill.registry.load_failed", {"error": str(exc)})
                registry = False  # sentinel: tried and failed
            self._skill_registry = registry
        return registry or None

    async def _try_skill_fast_path(
        self,
        harness: Any,
        *,
        worker_contract: JsonDict,
        phase: JsonDict,
        task: str,
        context: str,
        fleet_ids: List[str],
    ) -> Optional[str]:
        """Attempt a matching skill's fast path. Returns the worker answer if the
        skill handled the task, else None (caller runs the normal LLM loop).
        Any error falls back to the LLM loop — must never break the worker."""
        if not getattr(self.runtime.harness, "skill_fast_path_enabled", True):
            return None
        registry = self._get_skill_registry()
        if registry is None or not registry.all():
            return None
        try:
            from harness.skill.dispatch import maybe_run_skill_fast_path
            from harness.skill.health import default_health
            from harness.tools.browser_tools import _record_extraction
            outcome = await maybe_run_skill_fast_path(
                harness,
                registry=registry,
                worker_contract=worker_contract,
                phase=phase,
                task=task,
                context=context,
                fleet_ids=fleet_ids,
                record_extraction=_record_extraction,
                health=default_health(),
            )
        except Exception as exc:  # any failure → normal loop
            self.logger.write("skill.fast_path.error", {"error": str(exc)})
            return None
        if outcome and outcome.get("handled"):
            return outcome.get("answer")
        return None

    async def _maybe_autoheal_skill(
        self,
        harness: Any,
        *,
        fast_path_handled: bool,
        slow_path_succeeded: bool,
        worker_contract: JsonDict,
        phase: JsonDict,
        task: str,
        context: str,
        fleet_ids: List[str],
    ) -> None:
        """Close the self-heal loop: if the fast path fell back but the slow path
        succeeded for a degraded skill, distill the trace → candidate → canary →
        promote. Best-effort; any error is swallowed (never affects the worker)."""
        if fast_path_handled or not slow_path_succeeded:
            return
        if not getattr(self.runtime.harness, "skill_auto_heal_enabled", True):
            return
        registry = self._get_skill_registry()
        if registry is None or not registry.all():
            return
        try:
            from harness.skill.autoheal import maybe_autoheal_from_trace
            from harness.skill.dispatch import resolve_skill_and_variables
            from harness.skill.health import default_health

            skill, canary_variables = resolve_skill_and_variables(
                registry, worker_contract, phase=phase, task=task, context=context,
            )
            if skill is None:
                return
            await maybe_autoheal_from_trace(
                harness,
                skill=skill,
                health=default_health(),
                trace=getattr(harness, "trace", []) or [],
                canary_variables=canary_variables,
                fleet_id=next(iter(fleet_ids), "") if fleet_ids else "",
            )
        except Exception as exc:  # self-heal must never break the worker
            self.logger.write("skill.autoheal.error", {"error": str(exc)})

    async def _run_browser_worker(
        self,
        slot: BrowserAgentSlot,
        expose_reusable_pages: bool,
        worker_id: str,
        name: str,
        task: str,
        context: str,
        max_steps: Optional[int],
        result_contract: str,
        phase_id: Optional[str],
        worker_contract: JsonDict,
        phase: Optional[JsonDict],
    ) -> JsonDict:
        worker_runtime = replace(
            self.runtime,
            agent_id=slot.agent_id,
            harness=replace(
                self.runtime.harness,
                max_steps=max_steps or self.runtime.harness.worker_max_steps,
            ),
        )
        provider = LLMFactory.create_provider(
            browser_agent_model_config(worker_runtime.model)
        )
        event_logger = make_browser_event_logger(
            self.logger,
            worker_runtime.harness.log_browser_payloads,
            prefix=f"{worker_id}.transport",
        )

        try:
            if slot.client is None:
                raise ABCPTransportError(f"Slot {slot.slot_id} has no browser client")
            slot.client.on_event = event_logger
            registration = await self._prepare_slot_for_worker(
                slot,
                worker_id,
                expose_reusable_pages=expose_reusable_pages,
            )
            bundle = await self._capability_bundle_for_worker(
                slot.client,
                worker_runtime,
            )
            slot_context = self._render_slot_context(
                slot,
                expose_reusable_pages=expose_reusable_pages,
            )
            effective_context = context or "(none)"
            if slot_context:
                effective_context = f"{effective_context}\n\n{slot_context}".strip()
            try:
                from harness.skill.contract import selected_skill_context
                skill_context = selected_skill_context(
                    self._get_skill_registry(),
                    worker_contract or {},
                )
            except Exception as exc:
                self.logger.write("skill.context.error", {"error": str(exc)})
                skill_context = ""
            if skill_context:
                effective_context = f"{effective_context}\n\n{skill_context}".strip()
            worker_task = (
                f"BrowserAgent name: {name}\n"
                f"Independent context:\n{effective_context}\n\n"
                f"<worker_contract>\n"
                f"{json.dumps(worker_contract or {}, ensure_ascii=False, indent=2, default=str)}\n"
                f"</worker_contract>\n\n"
                f"Result contract:\n{result_contract or 'Return a structured JSON string containing outcome, data, evidence, next_steps.'}\n\n"
                f"Assigned task:\n{task}"
            )
            harness = self.browser_agent_factory(provider, slot.client, worker_runtime, self.logger)
            harness.worker_contract = worker_contract or {}
            harness.preloaded_registration = registration
            harness.preloaded_capability_bundle = bundle

            skill_answer = await self._try_skill_fast_path(
                harness,
                worker_contract=worker_contract or {},
                phase=phase or {},
                task=task,
                context=effective_context,
                fleet_ids=sorted(slot.fleet_ids),
            )
            if skill_answer is not None:
                answer = skill_answer
            else:
                answer = await harness.run(worker_task)
            trace_path = self._write_worker_trace(worker_id, harness.trace)
            trace_summary = self._summarize_worker_trace(harness.trace)
            challenge_tracker = getattr(harness, "challenge_tracker", None)
            if challenge_tracker is not None and hasattr(challenge_tracker, "suspected_pages"):
                trace_summary["suspectedChallengePages"] = challenge_tracker.suspected_pages()
            offloaded_files = trace_summary.pop("offloadedFiles", [])
            progress = getattr(harness, "progress", None)
            progress_snapshot = (
                progress.to_log_payload()
                if progress is not None
                else {}
            )
            artifact_validation = validate_worker_artifacts(
                contract=worker_contract,
                artifacts=harness.artifacts,
                attempt_artifacts=getattr(harness, "extraction_attempt_artifacts", []),
                prior_artifacts=phase_prior_artifact_paths(
                    self.logger,
                    phase_id=phase_id,
                    exclude_worker_id=worker_id,
                ),
                task_dir=self.logger.task_dir,
            )
            terminal_classification = classification_for_worker_status(
                harness.final_status
            )
            if terminal_classification is not None:
                artifact_validation["classification"] = terminal_classification
            elif artifact_validation.get("status") != "done":
                feedback_classification = _worker_feedback_classification(
                    harness.trace,
                    answer,
                )
                if feedback_classification is not None:
                    artifact_validation["classification"] = feedback_classification
            validated_status = (
                "validated_done"
                if artifact_validation.get("status") == "done"
                else "validation_failed"
                if artifact_validation.get("status") == "failed"
                else "not_validated"
            )
            # Self-heal loop: the fast path fell back (skill_answer is None) but the
            # slow path produced a validated result — distill its trace into a
            # candidate workflow and canary-promote it for the degraded skill.
            await self._maybe_autoheal_skill(
                harness,
                fast_path_handled=skill_answer is not None,
                slow_path_succeeded=validated_status == "validated_done",
                worker_contract=worker_contract or {},
                phase=phase or {},
                task=task,
                context=effective_context,
                fleet_ids=sorted(slot.fleet_ids),
            )
            diagnostics = getattr(harness, "diagnostics", None)
            result = {
                "status": harness.final_status,
                "statusCategory": status_category(harness.final_status),
                "validatedStatus": validated_status,
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "name": name,
                "phaseId": phase_id,
                "answer": answer,
                "artifacts": harness.artifacts,
                "extractionAttemptArtifacts": getattr(
                    harness,
                    "extraction_attempt_artifacts",
                    [],
                ),
                "artifactValidation": artifact_validation,
                "tracePath": trace_path,
                "traceSummary": trace_summary,
                "progressSnapshot": progress_snapshot,
                "progressInterventionCount": progress_snapshot.get(
                    "interventionCount",
                    0,
                ),
                "offloadedFiles": offloaded_files,
                "diagnostics": diagnostics.to_log_payload()
                if diagnostics is not None
                else {},
            }
            self._update_slot_after_worker(
                slot,
                worker_id=worker_id,
                phase_id=phase_id,
                worker_contract=worker_contract,
                result=result,
                trace=getattr(harness, "trace", []),
            )
        except asyncio.CancelledError:
            harness_obj = locals().get("harness")
            trace = (
                getattr(harness_obj, "trace", [])
                if harness_obj is not None
                else []
            )
            result = {
                "status": WORKER_STATUS_CANCELLED,
                "statusCategory": status_category(WORKER_STATUS_CANCELLED),
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "name": name,
                "phaseId": phase_id,
            }
            result = self._prepare_worker_result(
                result,
                worker_id=worker_id,
                agent_id=slot.agent_id,
                phase_id=phase_id,
            )
            self.logger.write(
                "spawner.browser.result",
                trim_large_strings(result, 8000),
            )
            self._record_slot_result(
                slot,
                worker_id=worker_id,
                phase_id=phase_id,
                worker_contract=worker_contract,
                result=result,
                trace=trace,
            )
            self._mark_slot_idle(slot, worker_id=worker_id, result=result)
            raise
        except Exception as exc:
            harness_obj = locals().get("harness")
            trace = (
                getattr(harness_obj, "trace", [])
                if harness_obj is not None
                else []
            )
            if harness_obj is not None:
                self._update_slot_registry_from_trace(
                    slot,
                    trace,
                )
            if isinstance(exc, ABCPTransportError):
                slot.status = "broken"
                slot.sync_errors.append(str(exc)[:500])
                if slot.client is not None:
                    await slot.client.close()
                    slot.client = None
            result = {
                "status": WORKER_STATUS_FAILED,
                "statusCategory": status_category(WORKER_STATUS_FAILED),
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "name": name,
                "phaseId": phase_id,
                "error": str(exc),
            }
            self._record_slot_result(
                slot,
                worker_id=worker_id,
                phase_id=phase_id,
                worker_contract=worker_contract,
                result=result,
                trace=None,
            )
            self._mark_slot_idle(slot, worker_id=worker_id, result=result)

        result = self._prepare_worker_result(
            result,
            worker_id=worker_id,
            agent_id=slot.agent_id,
            phase_id=phase_id,
        )
        attempt_digest = build_attempt_digest(
            result,
            phase=phase or {},
            worker_contract=worker_contract or {},
            task=task,
            result_contract=result_contract,
        )
        result["attemptDigest"] = attempt_digest
        phase_result_status = phase_result_status_for(result)
        mark_phase_result(
            self.logger,
            phase_id=phase_id,
            worker_id=worker_id,
            validation=result.get("artifactValidation"),
            result_status=phase_result_status,
            attempt_digest=attempt_digest,
        )
        append_strategy_attempt(
            logger=self.logger,
            worker_contract=worker_contract or {},
            result=result,
        )
        if slot.status == "running":
            self._mark_slot_idle(slot, worker_id=worker_id, result=result)
        self.logger.write("spawner.browser.result", trim_large_strings(result, 8000))
        return result

    def _prepare_worker_result(
        self,
        result: JsonDict,
        *,
        worker_id: str,
        agent_id: str,
        phase_id: Optional[str],
    ) -> JsonDict:
        result = self._attach_worker_result_levels(result)
        return self.lifecycle.worker_before_return(
            LifecycleContext(
                actor="browser_worker",
                metadata={
                    "worker_id": worker_id,
                    "agent_id": agent_id,
                    "phase_id": phase_id,
                },
            ),
            result,
        )

    async def _capability_bundle_for_worker(
        self,
        browser: ABCPClient,
        worker_runtime: RuntimeConfig,
    ) -> CapabilityBundle:
        if self._capability_bundle_lock is None:
            self._capability_bundle_lock = asyncio.Lock()
        async with self._capability_bundle_lock:
            if self._capability_bundle is not None:
                self.logger.write(
                    "schema.bundle.reused",
                    {
                        "capability_count": len(self._capability_bundle.capability_methods),
                        "schema_count": len(self._capability_bundle.method_schemas),
                    },
                )
                return _clone_capability_bundle(self._capability_bundle)
            bundle = await load_capability_bundle(
                browser,
                logger=self.logger,
                blocked_methods=ALWAYS_FORBIDDEN_ABCP_METHODS,
                schema_cache_dir=global_schemas_dir(worker_runtime.harness.worktree_dir),
            )
            self._capability_bundle = _clone_capability_bundle(bundle)
            return _clone_capability_bundle(bundle)

    def _attach_worker_result_levels(self, result: JsonDict) -> JsonDict:
        if result.get("resultLevels"):
            return result
        status = str(result.get("status") or "unknown")
        levels = build_worker_result_levels(
            status=status,
            status_category=str(result.get("statusCategory") or status_category(status)),
            validated_status=str(result.get("validatedStatus") or "not_validated"),
            worker_id=str(result.get("workerId") or ""),
            agent_id=str(result.get("agentId") or ""),
            name=str(result.get("name") or ""),
            phase_id=(
                str(result.get("phaseId"))
                if result.get("phaseId") is not None
                else None
            ),
            answer=str(result.get("answer") or ""),
            artifacts=_safe_str_list(result.get("artifacts")),
            extraction_attempt_artifacts=_safe_str_list(
                result.get("extractionAttemptArtifacts")
            ),
            artifact_validation=(
                result.get("artifactValidation")
                if isinstance(result.get("artifactValidation"), dict)
                else {}
            ),
            trace_path=str(result.get("tracePath") or ""),
            trace_summary=(
                result.get("traceSummary")
                if isinstance(result.get("traceSummary"), dict)
                else {}
            ),
            progress_snapshot=(
                result.get("progressSnapshot")
                if isinstance(result.get("progressSnapshot"), dict)
                else {}
            ),
            offloaded_files=_safe_str_list(result.get("offloadedFiles")),
            diagnostics=(
                result.get("diagnostics")
                if isinstance(result.get("diagnostics"), dict)
                else {}
            ),
            task_dir=getattr(self.logger, "task_dir", None),
        )
        result["resultLevels"] = levels
        result["workerResultProtocol"] = "L1/L2/L3"
        return result

    def _write_worker_trace(self, worker_id: str, trace: List[JsonDict]) -> str:
        traces_dir = task_subdir(self.logger, "traces")
        path = traces_dir / f"{safe_path_component(worker_id)}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for item in trace:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        return str(path.resolve())

    def _summarize_worker_trace(self, trace: List[JsonDict]) -> JsonDict:
        method_counts: Dict[str, int] = {}
        errors: List[str] = []
        page_ids: Set[str] = set()
        offloaded: List[str] = []
        progress_interventions: List[JsonDict] = []
        loop_nudges: List[JsonDict] = []
        page_stats_events: List[JsonDict] = []
        snapshot_diffs: List[JsonDict] = []
        tool_calls = 0
        max_step = 0
        for item in trace:
            if not isinstance(item, dict):
                continue
            max_step = max(max_step, optional_int(item.get("step"), 0) or 0)
            if item.get("type") == "browser_call":
                tool_calls += 1
                method = str(item.get("method") or "unknown")
                method_counts[method] = method_counts.get(method, 0) + 1
                result = item.get("result")
                offloaded.extend(extract_offloaded_paths(result))
                page_id = extract_page_id_from_values(item.get("params"), result)
                if page_id:
                    page_ids.add(page_id)
                if isinstance(result, dict):
                    if result.get("error"):
                        errors.append(str(result.get("error"))[:500])
                    response = result.get("response")
                    if isinstance(response, dict) and response.get("error"):
                        errors.append(str(response.get("error"))[:500])
            elif item.get("type") in {
                "tool_error",
                "browser_call_params_error",
                "browser_call_rejected",
            }:
                error = get_path(item, "result.error")
                if error:
                    errors.append(str(error)[:500])
            elif item.get("type") in {"progress_intervention", "progress_gate"}:
                result = item.get("result")
                if isinstance(result, dict):
                    progress_interventions.append({
                        "type": item.get("type"),
                        "reason": str(result.get("reason") or "")[:120],
                        "tool": str(result.get("tool") or result.get("method") or "")[:120],
                    })
            elif item.get("type") == "loop_nudge":
                result = item.get("result")
                if isinstance(result, dict):
                    loop_nudges.append({
                        "reason": str(result.get("reason") or "")[:120],
                        "action": str(result.get("action") or "")[:120],
                        "repeatCount": optional_int(result.get("repeatCount"), 0) or 0,
                        "pageStalledFor": optional_int(result.get("pageStalledFor"), 0) or 0,
                    })
            elif item.get("type") == "page_stats":
                result = item.get("result")
                if isinstance(result, dict):
                    page_stats_events.append({
                        "step": optional_int(item.get("step"), 0) or 0,
                        "pageId": str(result.get("pageId") or "")[:120],
                        "url": str(result.get("url") or "")[:240],
                        "title": str(result.get("title") or "")[:160],
                        "nodes": optional_int(result.get("nodes"), 0) or 0,
                        "actionable": optional_int(result.get("actionable"), 0) or 0,
                        "semanticItems": optional_int(result.get("semanticItems"), 0) or 0,
                        "links": optional_int(result.get("links"), 0) or 0,
                        "hint": str(result.get("hint") or "")[:240],
                    })
            elif item.get("type") == "snapshot_diff":
                result = item.get("result")
                if isinstance(result, dict):
                    snapshot_diffs.append({
                        "fromStep": optional_int(result.get("fromStep"), 0) or 0,
                        "toStep": optional_int(result.get("toStep"), 0) or 0,
                        "crossPageDiff": bool(result.get("crossPageDiff")),
                        "semanticAdded": optional_int(result.get("semanticAdded"), 0) or 0,
                        "semanticRemoved": optional_int(result.get("semanticRemoved"), 0) or 0,
                        "physicalAdded": optional_int(result.get("physicalAdded"), 0) or 0,
                        "physicalRemoved": optional_int(result.get("physicalRemoved"), 0) or 0,
                        "totalNodeDelta": optional_int(result.get("totalNodeDelta"), 0) or 0,
                        "semanticChanged": bool(result.get("semanticChanged")),
                        "physicalChanged": bool(result.get("physicalChanged")),
                    })
        progress_intervention_count = len(progress_interventions)
        loop_nudge_count = len(loop_nudges)
        stall_signal_count = progress_intervention_count + loop_nudge_count
        stall_replan = None
        if stall_signal_count >= 2:
            stall_replan = {
                "type": "stall_replan_recommended",
                "signalCount": stall_signal_count,
                "progressInterventionCount": progress_intervention_count,
                "loopNudgeCount": loop_nudge_count,
                "recentProgressInterventions": progress_interventions[-3:],
                "recentLoopNudges": loop_nudges[-3:],
                "next_instruction": (
                    "This worker emitted repeated stall signals inside one"
                    " attempt. If the phase is retried, revise the approach"
                    " instead of spawning another worker with the same path."
                ),
            }
        summary = {
            "steps": max_step,
            "traceEvents": len(trace),
            "toolCalls": tool_calls,
            "methods": method_counts,
            "pageIds": sorted(page_ids),
            "errors": errors[:10],
            "progressInterventions": progress_interventions[-5:],
            "progressInterventionCount": progress_intervention_count,
            "loopNudges": loop_nudges[-5:],
            "loopNudgeCount": loop_nudge_count,
            "latestPageStats": page_stats_events[-1] if page_stats_events else None,
            "pageStatsCount": len(page_stats_events),
            "snapshotDiffs": snapshot_diffs[-5:],
            "snapshotDiffCount": len(snapshot_diffs),
            "offloadedFiles": sorted(set(offloaded))[:100],
        }
        if stall_replan is not None:
            summary["stallReplanRecommended"] = stall_replan
        return summary

    def _select_handles(self, worker_ids: Optional[List[str]]) -> List[BrowserAgentHandle]:
        if not worker_ids:
            return list(self._handles.values())
        return [
            self._handles[worker_id]
            for worker_id in worker_ids
            if worker_id in self._handles
        ]

    def _task_result(self, handle: BrowserAgentHandle) -> JsonDict:
        try:
            return handle.async_task.result()
        except asyncio.CancelledError:
            return self._prepare_worker_result(
                {
                    "status": WORKER_STATUS_CANCELLED,
                    "statusCategory": status_category(WORKER_STATUS_CANCELLED),
                    "workerId": handle.worker_id,
                    "agentId": handle.agent_id,
                    "slotId": handle.slot_id,
                    "name": handle.name,
                    "phaseId": handle.phase_id,
                },
                worker_id=handle.worker_id,
                agent_id=handle.agent_id,
                phase_id=handle.phase_id,
            )
        except Exception as exc:
            return self._prepare_worker_result(
                {
                    "status": WORKER_STATUS_FAILED,
                    "statusCategory": status_category(WORKER_STATUS_FAILED),
                    "workerId": handle.worker_id,
                    "agentId": handle.agent_id,
                    "slotId": handle.slot_id,
                    "name": handle.name,
                    "phaseId": handle.phase_id,
                    "error": str(exc),
                },
                worker_id=handle.worker_id,
                agent_id=handle.agent_id,
                phase_id=handle.phase_id,
            )

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:03d}"

    def _next_slot_id(self) -> str:
        self._slot_counter += 1
        return f"slot-{self._slot_counter:03d}"


CHALLENGE_PHASE_STATUSES = frozenset({
    "blocked_by_challenge",
    "hitl_required",
    "hitl_timeout",
    "page_settled_after_hitl",
    "stale_pause_deadlock",
})


def phase_result_status_for(result: JsonDict) -> str:
    """Map a worker result to the status recorded against its phase.

    A challenge/HITL status normally freezes the phase ("do not retry without
    user action"). But when the worker recovered and its artifacts passed
    validation, the phase contract IS fulfilled — marking it as a challenge
    blocker would hide a validated success (observed in task 3b346d7e:
    browser-001 hit a stale-pause deadlock, recovered on a fresh page, and
    delivered the full validated phase-1 artifact).
    """
    worker_status = str(result.get("status") or "unknown")
    validation = result.get("artifactValidation")
    validation_done = (
        isinstance(validation, dict) and validation.get("status") == "done"
    )
    if worker_status in CHALLENGE_PHASE_STATUSES and not validation_done:
        return worker_status
    return str(result.get("validatedStatus") or result.get("status") or "unknown")


def _safe_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _origins_from_text(text: str) -> Set[str]:
    origins: Set[str] = set()
    for match in URL_RE.findall(str(text or "")):
        origin = _origin_from_url(match.rstrip(".,);]"))
        if origin:
            origins.add(origin)
    return origins


def _origin_from_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _phase_family(phase_id: Optional[str]) -> str:
    text = str(phase_id or "").strip()
    if not text:
        return ""
    # phase_2a, phase_2b, phase_2c should prefer the same reusable slot.
    return re.sub(r"(?<=\d)[a-z]$", "", text)


def _page_hidden_from_reuse(slot: BrowserAgentSlot, page: JsonDict) -> bool:
    page_id = str(page.get("pageId") or "").strip()
    if page_id and page_id in slot.page_quarantine:
        return True
    status = str(page.get("status") or "").strip().lower()
    if status in {"stale", "quarantined", "stale_pause_deadlock"}:
        return True
    return bool(page.get("doNotUse"))


def _text_indicates_paused_error(text: Any) -> bool:
    lowered = str(text or "").lower()
    return "err_page_paused" in lowered or "paused for human intervention" in lowered


def _state_response_indicates_paused(value: Any) -> bool:
    if _text_indicates_paused_error(value):
        return True
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict):
            if data.get("paused") is True:
                return True
            status = str(data.get("status") or "").strip().lower()
            if status == "paused":
                return True
            hitl = data.get("hitl")
            if isinstance(hitl, dict) and hitl.get("isPaused") is True:
                return True
        response = value.get("response")
        if isinstance(response, dict) and _state_response_indicates_paused(response):
            return True
    return False


def _worker_feedback_classification(
    trace: List[JsonDict],
    answer: str,
) -> Optional[JsonDict]:
    """Recover route-relevant classifications from worker feedback.

    Contract/tool-policy blockers are first surfaced as ordinary tool results
    inside the BrowserAgent loop. If the worker later finalizes cleanly or runs
    out of steps without a matching artifact, validation would otherwise report
    only data_missing. Preserve the more useful routing classification.
    """
    trace_classification = _classification_from_contract_violation(trace)
    if trace_classification is not None:
        return trace_classification
    browser_call_classification = _classification_from_browser_call(trace)
    if browser_call_classification is not None:
        return browser_call_classification
    return _classification_from_final_answer(answer)


def _classification_from_browser_call(
    trace: List[JsonDict],
) -> Optional[JsonDict]:
    for item in reversed(trace or []):
        if not isinstance(item, dict) or item.get("type") != "browser_call":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        classification = result.get("classification")
        if isinstance(classification, dict):
            category = str(classification.get("category") or "").strip()
            if category == "blocked_infrastructure":
                recovered = dict(classification)
                recovered.setdefault(
                    "hint",
                    "Browser infrastructure failed; rebuild page/fleet or reconnect the Browser Client before retrying.",
                )
                recovered["source"] = "browser_call.classification"
                return recovered
        error_classification = result.get("errorClassification")
        if not isinstance(error_classification, dict):
            continue
        error_type = str(error_classification.get("type") or "").strip()
        if error_type != "browser_unavailable_or_no_page":
            continue
        return {
            "category": "blocked_infrastructure",
            "type": error_type,
            "method": result.get("method") or "Page.create",
            "hint": (
                "Page.create failed with -32005 and no usable existing page was"
                " found; reconnect or rebuild the Browser Client before retrying."
            ),
            "source": "browser_call.errorClassification",
        }
    return None


def _classification_from_contract_violation(
    trace: List[JsonDict],
) -> Optional[JsonDict]:
    for item in reversed(trace or []):
        if not isinstance(item, dict) or item.get("type") != "contract_violation":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        classification = result.get("classification")
        if not isinstance(classification, dict):
            continue
        category = str(classification.get("category") or "").strip()
        if category != "blocked_cross_task_type_required":
            continue
        recovered = dict(classification)
        recovered.setdefault("hint", "LeadAgent should replan with a task_type that permits the required method.")
        recovered["source"] = "contract_violation"
        return recovered
    return None


def _classification_from_final_answer(answer: str) -> Optional[JsonDict]:
    try:
        payload = json.loads(str(answer or ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        return None
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        raw_classification = blocker.get("classification")
        if isinstance(raw_classification, dict):
            category = str(raw_classification.get("category") or "").strip()
            classification = dict(raw_classification)
        else:
            category = str(raw_classification or "").strip()
            classification = {"category": category}
        if category not in {
            "blocked_cross_task_type_required",
            "blocked_infrastructure",
        }:
            continue
        hint = (
            blocker.get("hint")
            or blocker.get("message")
            or blocker.get("reason")
            or (
                "Browser infrastructure failed; reconnect/rebuild the Browser"
                " Client before retrying."
                if category == "blocked_infrastructure"
                else "LeadAgent should replan with a task_type that permits the required method."
            )
        )
        classification.setdefault("hint", str(hint)[:500])
        if blocker.get("method"):
            classification.setdefault("method", blocker.get("method"))
        if blocker.get("task_type"):
            classification.setdefault("task_type", blocker.get("task_type"))
        classification["source"] = "final_answer.blockers"
        return classification
    return None


def _clone_capability_bundle(bundle: CapabilityBundle) -> CapabilityBundle:
    return CapabilityBundle(
        capabilities=list(bundle.capabilities),
        capability_methods=set(bundle.capability_methods),
        method_schemas=dict(bundle.method_schemas),
        methods_requiring_purpose=set(bundle.methods_requiring_purpose),
        purpose_hints=dict(bundle.purpose_hints),
        skills_doc=bundle.skills_doc,
    )
