"""
harness.spawner.spawner_core - BrowserAgentSpawner - construction, spawn entry points and lifecycle.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from abcp_client import ABCPTransportError
from harness.fleet.auth import AuthFleetLedger
from harness.fleet.auth import normalize_auth_verification_contract
from harness.fleet.coordinator import FleetAssignment
from harness.fleet.coordinator import FleetCoordinator
from harness.fleet.coordinator import FleetRoutingError
from harness.fleet.coordinator import handle_records_from_value
from harness.fleet.coordinator import normalize_page_policy
from harness.fleet.coordinator import normalize_reuse_scope
from harness.fleet.runtime import FleetAuthBarrier
from harness.fleet.runtime import FleetClickGate
from harness.fleet.runtime import PageLeaseManager
from harness.observation.render_recovery import extract_page_id_from_values
from runtime_config import RuntimeConfig
from harness.lifecycle import default_lifecycle_manager
from harness.schema_loader import CapabilityBundle
from harness.task_control import cancel_phase_running_reservation
from harness.task_control import clear_spawn_acquisition_failures
from harness.task_control import contract_hash_for_phase
from harness.task_control import mark_phase_running
from harness.task_control import phase_pacing_remaining_seconds
from harness.task_control import phase_start_rejection
from harness.task_control import record_spawn_acquisition_failure
from harness.task_control import spawn_acquisition_fingerprint
from harness.task_control import spawn_acquisition_rejection
from harness.task_control import load_task_state
from harness.task_control import write_task_state
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import build_static_context_block
from harness.utils import optional_float
from harness.utils import optional_int
from harness.utils import trim_large_strings
from .spawner_classification import _page_hidden_from_reuse  # noqa: F401
from .spawner_helpers import BrowserAgentFactory, BrowserAgentHandle, BrowserAgentSlot, FleetReadinessError, PinnedBrowserContext, ResumeBrowserHint, _SessionStartLock  # noqa: F401
from .spawner_registry import SpawnerRegistryMixin  # noqa: F401
from .spawner_slots import SpawnerSlotsMixin  # noqa: F401
from .spawner_worker import SpawnerWorkerMixin  # noqa: F401

def _sp():
    import harness.spawner as sp

    return sp

class BrowserAgentSpawner(SpawnerSlotsMixin, SpawnerRegistryMixin, SpawnerWorkerMixin):

    """Creates isolated browser agents and manages their lifecycle."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        logger: RunLogger,
        browser_agent_factory: BrowserAgentFactory,
        pinned_browser_context: Any = None,
        resume_browser_hint: Any = None,
    ):
        self.runtime = runtime
        self.browser_agent_factory = browser_agent_factory
        self.logger = logger
        pinned_browser_context = PinnedBrowserContext.from_value(
            pinned_browser_context
        )
        if (
            pinned_browser_context is not None
            and not getattr(
                self.runtime.harness,
                "fleet_reuse_enabled",
                True,
            )
        ):
            raise ValueError(
                "pinned_browser_context requires"
                " runtime.harness.fleet_reuse_enabled=true"
            )
        self.pinned_browser_context = pinned_browser_context
        self.resume_browser_hint = ResumeBrowserHint.from_value(
            resume_browser_hint
        )
        self._handles: Dict[str, BrowserAgentHandle] = {}
        self._slots: Dict[str, BrowserAgentSlot] = {}
        self._counter = 0
        self._slot_counter = 0
        self.fleet_coordinator = FleetCoordinator()
        ledger_path = Path(
            str(getattr(
                self.runtime.harness,
                "auth_fleet_ledger_path",
                ".auth_fleet_ledger.json",
            ) or ".auth_fleet_ledger.json")
        )
        if not ledger_path.is_absolute():
            ledger_path = Path(self.runtime.harness.worktree_dir) / ledger_path
        self.auth_fleet_ledger = AuthFleetLedger(ledger_path)
        self.page_lease_manager = PageLeaseManager(
            wait_timeout_seconds=getattr(
                self.runtime.harness,
                "page_lease_wait_timeout_seconds",
                30.0,
            )
        )
        self.fleet_auth_barrier = FleetAuthBarrier(
            wait_timeout_seconds=getattr(
                self.runtime.harness,
                "fleet_auth_barrier_wait_seconds",
                120.0,
            )
        )
        if getattr(
            self.runtime.harness,
            "fleet_click_gate_enabled",
            True,
        ):
            self.fleet_click_gate = FleetClickGate(
                acquire_timeout_seconds=getattr(
                    self.runtime.harness,
                    "fleet_click_gate_acquire_timeout_seconds",
                    30.0,
                ),
                soft_settlement_seconds=getattr(
                    self.runtime.harness,
                    "fleet_click_gate_navigation_settlement_seconds",
                    0.75,
                ),
                non_link_settlement_seconds=getattr(
                    self.runtime.harness,
                    "fleet_click_gate_non_link_settlement_seconds",
                    0.10,
                ),
                submit_settlement_seconds=getattr(
                    self.runtime.harness,
                    "fleet_click_gate_submit_settlement_seconds",
                    2.5,
                ),
                late_guard_seconds=getattr(
                    self.runtime.harness,
                    "fleet_click_gate_late_guard_seconds",
                    5.0,
                ),
                popup_inventory_observation_enabled=getattr(
                    self.runtime.harness,
                    "fleet_click_gate_popup_inventory_observation_enabled",
                    True,
                ),
                workflow_hitl_late_guard_seconds=getattr(
                    self.runtime.harness,
                    "fleet_click_gate_workflow_hitl_late_guard_seconds",
                    15.0,
                ),
                logger=self.logger,
            )
        else:
            self.fleet_click_gate = None
            self.logger.write(
                "fleet_click_gate.disabled",
                {
                    "warning": (
                        "Process-local Fleet click serialization is disabled;"
                        " same-Fleet workers may dispatch concurrent clicks."
                    ),
                    "sameFleetMultiworkerEnabled": bool(getattr(
                        self.runtime.harness,
                        "same_fleet_multiworker_enabled",
                        False,
                    )),
                },
            )
        self.static_context_block, self.static_context_hash = build_static_context_block(
            self.runtime.harness.context_file
        )
        self.lifecycle = default_lifecycle_manager()
        self._capability_bundle: Optional[CapabilityBundle] = None
        self._capability_bundle_lock = None
        self._slot_pool_lock = None
        self._broken_slot_recovery_lock = None
        self._session_start_locks: Dict[str, _SessionStartLock] = {}
        self._notification_relays: Dict[tuple[str, str, str], Callable[[], None]] = {}
        # Concurrent phases assigned to one Fleet share one authoritative
        # readiness probe. Completed tasks are removed immediately: this is
        # single-flight coordination, not a stale readiness cache.
        self._fleet_readiness_tasks: Dict[
            tuple[str, str], "asyncio.Task[JsonDict]"
        ] = {}
        self._browser_context_fingerprints: Dict[str, str] = {}
        # Page inventory is slot-global, while resume state is task-local.
        # Record only pages this task actually addressed; sharing a Fleet does
        # not make every tab returned by Page.list part of this task.
        self._task_browser_page_ids: Dict[str, Set[str]] = {}

    def _resume_hint_for_worker(
        self,
        *,
        phase_id: str = "",
        worker_contract: JsonDict,
        session_key: str,
        fleet_reference: str,
        preferred_slot_id: Optional[str],
        reuse_from_worker_id: Optional[str],
    ) -> Optional[ResumeBrowserHint]:
        """Return the weak resume candidate only for an unconstrained worker."""

        hint = self.resume_browser_hint
        if hint is None:
            return None
        reason = ""
        if self.pinned_browser_context is not None:
            reason = "explicit_pin"
        elif hint.phase_id and hint.phase_id != str(phase_id or "").strip():
            reason = "different_phase"
        elif str(session_key or "").strip():
            reason = "session_key"
        elif str(fleet_reference or "").strip():
            reason = "explicit_fleet"
        elif worker_contract.get("needs_isolated_session") is True:
            reason = "needs_isolated_session"
        elif str(preferred_slot_id or "").strip():
            reason = "preferred_slot"
        elif str(reuse_from_worker_id or "").strip():
            reason = "reuse_from_worker"
        if not reason:
            return hint
        self.logger.write(
            "spawner.resume_browser_hint.ignored",
            {
                "reason": reason,
                "resumeBrowserHint": hint.to_dict(),
            },
        )
        return None

    @staticmethod
    def _browser_context_page_record(page: JsonDict, fleet_id: str) -> JsonDict:
        record: JsonDict = {
            "pageId": str(page.get("pageId") or ""),
            "fleetId": fleet_id,
        }
        for key in ("url", "title", "origin", "status"):
            value = page.get(key)
            if isinstance(value, (str, int, float, bool)) and value != "":
                record[key] = value
        return record

    def _persist_task_browser_context(
        self,
        slot: BrowserAgentSlot,
        assignment: FleetAssignment,
        *,
        phase_id: Optional[str] = None,
        primary_page_id: str = "",
        replace_pages: bool = False,
        removed_page_ids: Optional[Set[str]] = None,
    ) -> bool:
        """Persist only the FleetAssignment that this task actually received."""

        state_path = self.logger.task_dir / "task_state.json"
        if not state_path.exists():
            self.logger.write(
                "spawner.browser_context.persist_skipped",
                {
                    "reason": "task_state_missing",
                    "workerId": assignment.worker_id,
                    "fleetId": assignment.fleet_id,
                },
            )
            return False
        state = load_task_state(self.logger)
        if not state:
            self.logger.write(
                "spawner.browser_context.persist_skipped",
                {
                    "reason": "task_state_unreadable",
                    "workerId": assignment.worker_id,
                    "fleetId": assignment.fleet_id,
                },
            )
            return False

        browser_context = state.get("browser_context")
        browser_context = (
            dict(browser_context) if isinstance(browser_context, dict) else {}
        )
        fleets = browser_context.get("fleets")
        fleets = dict(fleets) if isinstance(fleets, dict) else {}
        fleet_id = assignment.fleet_id
        previous = fleets.get(fleet_id)
        previous = dict(previous) if isinstance(previous, dict) else {}
        touched_page_ids = self._task_browser_page_ids.setdefault(fleet_id, set())
        requested_primary_page_id = str(primary_page_id or "").strip()
        requested_primary = slot.page_registry.get(requested_primary_page_id)
        if (
            requested_primary_page_id
            and isinstance(requested_primary, dict)
            and str(requested_primary.get("fleetId") or "") == fleet_id
        ):
            touched_page_ids.add(requested_primary_page_id)

        pages_by_id: Dict[str, JsonDict] = {}
        for page in previous.get("pages") or []:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("pageId") or "").strip()
            if not page_id:
                continue
            current_page = slot.page_registry.get(page_id)
            if replace_pages and not (
                isinstance(current_page, dict)
                and str(current_page.get("fleetId") or "") == fleet_id
            ):
                continue
            pages_by_id[page_id] = dict(page)
        for page_id, page in slot.page_registry.items():
            if not isinstance(page, dict):
                continue
            if str(page.get("fleetId") or "").strip() != fleet_id:
                continue
            normalized_id = str(page.get("pageId") or page_id or "").strip()
            if not normalized_id or normalized_id not in touched_page_ids:
                continue
            normalized = dict(page)
            normalized["pageId"] = normalized_id
            pages_by_id[normalized_id] = self._browser_context_page_record(
                normalized, fleet_id
            )
        for page_id in removed_page_ids or set():
            normalized_id = str(page_id or "").strip()
            pages_by_id.pop(normalized_id, None)
            touched_page_ids.discard(normalized_id)

        now = time.time()
        pages = [pages_by_id[key] for key in sorted(pages_by_id)]
        fleets[fleet_id] = {
            **previous,
            "ownerSlotId": assignment.owner_slot_id or assignment.slot_id,
            "slotId": assignment.slot_id,
            "ownerAgentId": assignment.owner_agent_id,
            "sessionKey": assignment.session_key or None,
            "isIsolated": bool(assignment.is_isolated),
            "assignmentReason": assignment.assignment_reason,
            "pages": pages,
            "lastSeenAt": now,
        }
        browser_context["fleets"] = fleets

        candidate_page_id = requested_primary_page_id
        previous_primary = browser_context.get("last_primary")
        previous_primary = (
            dict(previous_primary)
            if isinstance(previous_primary, dict)
            else {}
        )
        if candidate_page_id not in pages_by_id:
            candidate_page_id = ""
        if (
            not candidate_page_id
            and str(previous_primary.get("fleetId") or "") == fleet_id
            and str(previous_primary.get("pageId") or "") in pages_by_id
        ):
            candidate_page_id = str(previous_primary.get("pageId") or "")
        browser_context["last_primary"] = {
            "fleetId": fleet_id,
            "pageId": candidate_page_id or None,
            "lastSeenAt": now,
        }
        resolved_phase_id = str(phase_id or "").strip()
        if not resolved_phase_id:
            handle = self._handles.get(assignment.worker_id)
            resolved_phase_id = str(
                handle.phase_id if handle is not None else ""
            ).strip()
        phase_primary: JsonDict = {}
        if resolved_phase_id:
            phase_primaries = browser_context.get("phase_primaries")
            phase_primaries = (
                dict(phase_primaries)
                if isinstance(phase_primaries, dict)
                else {}
            )
            previous_phase_primary = phase_primaries.get(resolved_phase_id)
            previous_phase_primary = (
                dict(previous_phase_primary)
                if isinstance(previous_phase_primary, dict)
                else {}
            )
            # A new phase must not inherit the task-wide last_primary merely
            # because it shares that Fleet. Only an explicitly observed page,
            # or this same phase's still-live prior page, is a phase candidate.
            phase_page_id = (
                requested_primary_page_id
                if requested_primary_page_id in pages_by_id
                else ""
            )
            if (
                not phase_page_id
                and str(previous_phase_primary.get("fleetId") or "") == fleet_id
                and str(previous_phase_primary.get("pageId") or "") in pages_by_id
            ):
                phase_page_id = str(previous_phase_primary.get("pageId") or "")
            phase_primary = {
                "fleetId": fleet_id,
                "pageId": phase_page_id or None,
                "lastSeenAt": now,
            }
            phase_primaries[resolved_phase_id] = phase_primary
            browser_context["phase_primaries"] = phase_primaries
        fingerprint_payload = {
            "fleetId": fleet_id,
            "ownerSlotId": assignment.owner_slot_id or assignment.slot_id,
            "slotId": assignment.slot_id,
            "ownerAgentId": assignment.owner_agent_id,
            "sessionKey": assignment.session_key or None,
            "isIsolated": bool(assignment.is_isolated),
            "assignmentReason": assignment.assignment_reason,
            "pages": pages,
            "primaryPageId": candidate_page_id or None,
            "phaseId": resolved_phase_id or None,
            "phasePrimary": {
                "fleetId": phase_primary.get("fleetId"),
                "pageId": phase_primary.get("pageId"),
            } if phase_primary else None,
        }
        fingerprint = json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint_key = f"{fleet_id}:{resolved_phase_id}"
        if self._browser_context_fingerprints.get(fingerprint_key) == fingerprint:
            return False
        state["browser_context"] = browser_context
        write_task_state(self.logger, state)
        self._browser_context_fingerprints[fingerprint_key] = fingerprint
        self.logger.write(
            "spawner.browser_context.persisted",
            {
                "workerId": assignment.worker_id,
                "fleetId": fleet_id,
                "phaseId": resolved_phase_id or None,
                "pageCount": len(pages),
                "primaryPageId": candidate_page_id or None,
                "replacePages": bool(replace_pages),
            },
        )
        return True

    def _observe_task_browser_call(
        self,
        slot: BrowserAgentSlot,
        assignment: FleetAssignment,
        method: str,
        params: Any,
        result: Any,
        *,
        phase_id: Optional[str] = None,
    ) -> None:
        """Checkpoint live page handles before a worker can be interrupted."""

        try:
            payload = params if isinstance(params, dict) else {}
            explicit_page_id = str(
                payload.get("pageId") or payload.get("page_id") or ""
            ).strip()
            result_page_ids = {
                str(item.get("pageId") or item.get("page_id") or "").strip()
                for item in handle_records_from_value(result)
                if isinstance(
                    item.get("pageId") or item.get("page_id"), str
                )
                and str(
                    item.get("pageId") or item.get("page_id") or ""
                ).strip()
            }
            result_page_id = str(
                extract_page_id_from_values(result) or ""
            ).strip()
            first_seen_page = any(
                page_id not in slot.page_registry
                for page_id in result_page_ids
            )
            task_page_ids = self._task_browser_page_ids.setdefault(
                assignment.fleet_id, set()
            )
            newly_touched_page = False
            addressed_page_ids = set()
            if explicit_page_id and method != "Page.close":
                addressed_page_ids.add(explicit_page_id)
            if method == "Page.create":
                addressed_page_ids.update(result_page_ids)
                if result_page_id:
                    addressed_page_ids.add(result_page_id)
            for page_id in addressed_page_ids:
                if page_id and page_id not in task_page_ids:
                    task_page_ids.add(page_id)
                    newly_touched_page = True
            checkpoint_methods = {
                "Page.create",
                "Page.list",
                "Page.switchTo",
                "Page.navigate",
                "Page.reload",
                "Page.go",
                "Page.close",
            }
            if (
                method not in checkpoint_methods
                and not first_seen_page
                and not newly_touched_page
            ):
                return
            if method == "Page.list":
                self._replace_fleet_pages_from_list(
                    slot,
                    fleet_id=assignment.fleet_id,
                    pages_response=result,
                )
            if method == "Page.close":
                closed_page_id = str(
                    payload.get("pageId") or payload.get("page_id") or ""
                ).strip()
                if closed_page_id:
                    task_page_ids.discard(closed_page_id)
                    slot.page_registry.pop(closed_page_id, None)
                    slot.page_quarantine.pop(closed_page_id, None)
            else:
                self._update_slot_registry_from_value(slot, payload)
                self._update_slot_registry_from_value(slot, result)

            removed_page_ids: Set[str] = set()
            if method == "Page.close" and explicit_page_id:
                removed_page_ids.add(explicit_page_id)
                primary_page_id = ""
            else:
                primary_page_id = explicit_page_id
                if method == "Page.create":
                    primary_page_id = result_page_id

            observed_page_ids: Set[str] = set()
            if method != "Page.close":
                observed_page_ids.update(result_page_ids)
            if explicit_page_id and method != "Page.close":
                observed_page_ids.add(explicit_page_id)
            for page_id in observed_page_ids:
                page = dict(slot.page_registry.get(page_id) or {})
                page["pageId"] = page_id
                page.setdefault("fleetId", assignment.fleet_id)
                slot.page_registry[page_id] = page

            self._persist_task_browser_context(
                slot,
                assignment,
                phase_id=phase_id,
                primary_page_id=primary_page_id,
                replace_pages=method == "Page.list",
                removed_page_ids=removed_page_ids,
            )
        except Exception as exc:
            # Context checkpointing is advisory. A successful browser action
            # must never be turned into a failed action by local persistence.
            self.logger.write(
                "spawner.browser_context.persist_failed",
                {
                    "workerId": assignment.worker_id,
                    "fleetId": assignment.fleet_id,
                    "method": method,
                    "error": str(exc)[:500],
                },
            )

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
        task_plan: Optional[JsonDict] = None,
        preferred_slot_id: Optional[str] = None,
        reuse_from_worker_id: Optional[str] = None,
        reuse_scope: Optional[str] = None,
        fleet_id: Optional[str] = None,
        session_key: Optional[str] = None,
        page_policy: Optional[str] = None,
    ) -> JsonDict:
        effective_contract = worker_contract or {}
        pinned = self.pinned_browser_context
        if pinned is not None and (
            effective_contract.get("needs_isolated_session")
            or fleet_id
            or effective_contract.get("fleet_id")
            or session_key
            or effective_contract.get("session_key")
        ):
            return {
                "status": "invalid_fleet_routing",
                "error": (
                    "pinned existing browser context cannot be combined with"
                    " fleet_id, session_key, or needs_isolated_session"
                ),
                "pinnedBrowserContext": pinned.to_dict(),
                "tool_was_executed": False,
            }
        if "auth_verification" in effective_contract:
            try:
                effective_contract["auth_verification"] = (
                    normalize_auth_verification_contract(
                        effective_contract.get("auth_verification")
                    )
                )
            except ValueError as exc:
                return {
                    "status": "invalid_fleet_routing",
                    "error": str(exc),
                    "tool_was_executed": False,
                }
        if (
            "needs_isolated_session" in effective_contract
            and not isinstance(effective_contract.get("needs_isolated_session"), bool)
        ):
            return {
                "status": "invalid_fleet_routing",
                "error": "needs_isolated_session must be a boolean",
                "tool_was_executed": False,
            }
        if session_key is not None and not isinstance(session_key, str):
            return {
                "status": "invalid_fleet_routing",
                "error": "session_key must be a string or null",
                "tool_was_executed": False,
            }
        if fleet_id is not None and not isinstance(fleet_id, str):
            return {
                "status": "invalid_fleet_routing",
                "error": "fleet_id must be a string or null",
                "tool_was_executed": False,
            }
        if (
            "fleet_id" in effective_contract
            and not isinstance(effective_contract.get("fleet_id"), str)
        ):
            return {
                "status": "invalid_fleet_routing",
                "error": "worker_contract.fleet_id must be a string",
                "tool_was_executed": False,
            }
        if (
            "session_key" in effective_contract
            and not isinstance(effective_contract.get("session_key"), str)
        ):
            return {
                "status": "invalid_fleet_routing",
                "error": "worker_contract.session_key must be a string",
                "tool_was_executed": False,
            }
        explicit_continuation = bool(
            str(preferred_slot_id or "").strip()
            or str(reuse_from_worker_id or "").strip()
        )
        requested_reuse_scope = str(
            reuse_scope or effective_contract.get("reuse_scope") or ""
        )
        requested_page_policy = str(
            page_policy or effective_contract.get("page_policy") or ""
        )
        resume_hint_may_select_page = not bool(
            requested_reuse_scope
            or requested_page_policy
            or explicit_continuation
        )
        if pinned is not None and pinned.page_id:
            requested_reuse_scope = "page"
            requested_page_policy = "existing"
        elif pinned is not None and not requested_reuse_scope:
            requested_reuse_scope = "fleet"
            requested_page_policy = requested_page_policy or "new"
        try:
            effective_reuse_scope = normalize_reuse_scope(
                requested_reuse_scope,
                explicit_continuation=explicit_continuation,
            )
            effective_page_policy = normalize_page_policy(
                requested_page_policy,
                reuse_scope=effective_reuse_scope,
            )
        except ValueError as exc:
            return {
                "status": "invalid_fleet_routing",
                "error": str(exc),
                "tool_was_executed": False,
            }
        effective_session_key = str(
            session_key or effective_contract.get("session_key") or ""
        ).strip()
        direct_fleet_reference = str(fleet_id or "").strip()
        contract_fleet_reference = str(
            effective_contract.get("fleet_id") or ""
        ).strip()
        if (
            direct_fleet_reference
            and contract_fleet_reference
            and direct_fleet_reference.lower()
            != contract_fleet_reference.lower()
        ):
            return {
                "status": "invalid_fleet_routing",
                "error": (
                    "spawn fleet_id and worker_contract.fleet_id must"
                    " reference the same existing Fleet"
                ),
                "tool_was_executed": False,
            }
        effective_fleet_reference = (
            direct_fleet_reference or contract_fleet_reference
        )
        if (
            effective_fleet_reference
            and not getattr(
                self.runtime.harness,
                "fleet_reuse_enabled",
                True,
            )
        ):
            return {
                "status": "invalid_fleet_routing",
                "error": (
                    "fleet_id requires"
                    " runtime.harness.fleet_reuse_enabled=true"
                ),
                "tool_was_executed": False,
            }
        if effective_fleet_reference and effective_session_key:
            return {
                "status": "invalid_fleet_routing",
                "error": "fleet_id and session_key are mutually exclusive",
                "tool_was_executed": False,
                "next_instruction": (
                    "Use fleet_id for an existing Fleet UUID/prefix. Use"
                    " session_key only to create or reuse a named harness"
                    " session whose Fleet does not yet have to exist."
                ),
            }
        if (
            effective_fleet_reference
            and effective_contract.get("needs_isolated_session") is True
        ):
            return {
                "status": "invalid_fleet_routing",
                "error": (
                    "fleet_id cannot be combined with"
                    " needs_isolated_session"
                ),
                "tool_was_executed": False,
            }
        if effective_fleet_reference and not requested_reuse_scope:
            effective_reuse_scope = "fleet"
            effective_page_policy = "new"
        start_rejection = phase_start_rejection(
            task_plan,
            self.logger,
            phase_id=phase_id,
            worker_contract=effective_contract,
        )
        if start_rejection is not None:
            self.logger.write("spawner.browser.start_rejected", start_rejection)
            return start_rejection
        phase_wait = phase_pacing_remaining_seconds(
            task_plan,
            self.logger,
            phase_id=phase_id,
            worker_contract=effective_contract,
        )
        if phase_wait > 0.0:
            wait_payload = {
                "phaseId": phase_id,
                "requestedIntervalSeconds": (
                    (effective_contract.get("pacing") or {}).get(
                        "phase_interval_seconds", 0.0
                    )
                    if isinstance(effective_contract.get("pacing"), dict)
                    else 0.0
                ),
                "actualWaitSeconds": phase_wait,
                "slotReserved": False,
            }
            self.logger.write("pacing.phase.wait_started", wait_payload)
            await asyncio.sleep(phase_wait)
            self.logger.write("pacing.phase.wait_completed", wait_payload)
            # Another spawn may have claimed or completed this phase while this
            # coroutine was waiting; re-run the gate before reserving a slot.
            start_rejection = phase_start_rejection(
                task_plan,
                self.logger,
                phase_id=phase_id,
                worker_contract=effective_contract,
            )
            if start_rejection is not None:
                self.logger.write("spawner.browser.start_rejected", start_rejection)
                return start_rejection
        # Retained as an observation/provenance key for acquisition and
        # attempt receipts; it no longer authorizes a repeated-phase lock.
        current_contract_hash = contract_hash_for_phase(
            phase,
            effective_contract,
            task=task,
            result_contract=result_contract,
        )
        acquisition_fingerprint = spawn_acquisition_fingerprint(
            phase,
            effective_contract,
            reuse_scope=effective_reuse_scope,
            page_policy=effective_page_policy,
            session_key=effective_session_key,
            fleet_id=effective_fleet_reference,
            preferred_slot_id=preferred_slot_id,
            reuse_from_worker_id=reuse_from_worker_id,
        )
        acquisition_rejection = spawn_acquisition_rejection(
            self.logger,
            acquisition_fingerprint=acquisition_fingerprint,
            phase_id=phase_id,
        )
        if acquisition_rejection is not None:
            self.logger.write(
                "spawner.slot.acquire_exhausted", acquisition_rejection
            )
            return acquisition_rejection

        isolation_declared = (
            isinstance(effective_contract, dict)
            and effective_contract.get("needs_isolated_session") is not None
        )
        effective_contract = self._apply_worker_session_isolation(
            effective_contract,
            phase_id=phase_id,
            session_key=effective_session_key,
            fleet_reference=effective_fleet_reference,
            reuse_from_worker_id=reuse_from_worker_id,
        )
        # Only the phase's own declaration is an identity boundary the task
        # fleet cap must fail closed on; deployment-default isolation is a
        # preference the cap may drop. Recording the provenance here keeps that
        # distinction race-free — the budget can fill between this point and
        # the fleet decision.
        isolation_auto_applied = bool(
            not isolation_declared
            and isinstance(effective_contract, dict)
            and effective_contract.get("needs_isolated_session") is True
        )
        resume_hint = self._resume_hint_for_worker(
            phase_id=str(phase_id or ""),
            worker_contract=effective_contract,
            session_key=effective_session_key,
            fleet_reference=effective_fleet_reference,
            preferred_slot_id=preferred_slot_id,
            reuse_from_worker_id=reuse_from_worker_id,
        )

        worker_id = self._next_id("browser")
        agent_name = name or worker_id
        mark_phase_running(
            self.logger,
            phase_id=phase_id,
            worker_id=worker_id,
            worker_name=agent_name,
        )
        expose_reusable_pages = effective_reuse_scope == "page"
        slot: Any = None
        registration: JsonDict = {}
        assignment: Optional[FleetAssignment] = None
        readiness_receipt: JsonDict = {}
        resume_page_inventory_refreshed = False
        try:
            fleet_group_key = self._fleet_group_key(
                session_key=effective_session_key,
                worker_id=worker_id,
                needs_isolated_session=bool(
                    effective_contract.get("needs_isolated_session", False)
                ),
            )
            # Slot reservation/registration remains concurrent. The narrower
            # fleet decision lock lives inside _assign_fleet_for_worker.
            start_guard_key = ""
            async with self._session_start_guard(start_guard_key):
                await self._recover_broken_slots()
                if self._slot_pool_lock is None:
                    self._slot_pool_lock = asyncio.Lock()
                async with self._slot_pool_lock:
                    self._validate_routing_intent(
                        session_key=effective_session_key,
                        preferred_slot_id=preferred_slot_id,
                        reuse_from_worker_id=reuse_from_worker_id,
                    )
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
                        session_key=effective_session_key,
                        fleet_id=(
                            effective_fleet_reference
                            or (resume_hint.fleet_id if resume_hint else "")
                        ),
                    )
                if not isinstance(slot, dict):
                    await self._initialize_reserved_slot(slot)
                    prepare_kwargs: JsonDict = {
                        # An explicit existing-Fleet reference must resolve
                        # against a fresh Fleet.list snapshot even when the
                        # ordinary slot inventory TTL has not expired.
                        "expose_reusable_pages": (
                            expose_reusable_pages
                            or bool(effective_fleet_reference)
                            or bool(resume_hint)
                        ),
                    }
                    if effective_fleet_reference:
                        prepare_kwargs["required_fleet_id"] = str(
                            effective_fleet_reference
                        )
                    registration = await self._prepare_slot_for_worker(
                        slot, worker_id, **prepare_kwargs
                    )
                    if (
                        resume_hint is not None
                        and resume_hint.page_id
                        and resume_hint.fleet_id in slot.fleet_ids
                    ):
                        try:
                            await self._sync_slot_registry(
                                slot,
                                worker_id=worker_id,
                                required_fleet_id=resume_hint.fleet_id,
                                include_page_details=True,
                            )
                            hinted_page = slot.page_registry.get(
                                resume_hint.page_id
                            )
                            if (
                                not isinstance(hinted_page, dict)
                                or str(hinted_page.get("fleetId") or "")
                                != resume_hint.fleet_id
                                or _page_hidden_from_reuse(slot, hinted_page)
                            ):
                                raise LookupError(
                                    "hinted page is not a live reusable page"
                                )
                            if slot.client is None:
                                raise ABCPTransportError(
                                    "resume page probe has no browser client"
                                )
                            state_response = await slot.client.call(
                                "Page.getState",
                                {
                                    "pageId": resume_hint.page_id,
                                    "purpose": (
                                        "Verify a best-effort resume page before"
                                        f" assigning worker {worker_id}."
                                    ),
                                },
                            )
                            self._update_slot_registry_from_value(
                                slot,
                                {
                                    "pageId": resume_hint.page_id,
                                    "fleetId": resume_hint.fleet_id,
                                    "state": state_response,
                                },
                            )
                            resume_page_inventory_refreshed = True
                        except Exception as exc:
                            self.logger.write(
                                "spawner.resume_browser_hint.page_probe_failed",
                                {
                                    "resumeBrowserHint": resume_hint.to_dict(),
                                    "error": str(exc)[:500],
                                },
                            )
                            resume_hint = ResumeBrowserHint(
                                fleet_id=resume_hint.fleet_id,
                                phase_id=resume_hint.phase_id,
                                source=resume_hint.source,
                            )
                    assignment = await self._assign_fleet_for_worker(
                        slot,
                        worker_id=worker_id,
                        worker_contract=effective_contract,
                        reuse_scope=effective_reuse_scope,
                        page_policy=effective_page_policy,
                        session_key=effective_session_key,
                        fleet_id=effective_fleet_reference,
                        reuse_from_worker_id=str(
                            reuse_from_worker_id or ""
                        ).strip(),
                        fleet_group_key=fleet_group_key,
                        isolation_auto_applied=isolation_auto_applied,
                        resume_browser_hint=resume_hint,
                        resume_hint_may_select_page=(
                            resume_hint_may_select_page
                        ),
                    )
                    self._ensure_notification_relay(slot, assignment)
                    readiness_receipt = await self._ensure_assigned_fleet_ready(
                        slot,
                        assignment,
                        worker_id=worker_id,
                    )
                    if assignment is not None and (
                        expose_reusable_pages
                        or bool(
                            self.pinned_browser_context
                            and self.pinned_browser_context.page_id
                        )
                    ):
                        await self._sync_assigned_fleet_pages(
                            slot,
                            assignment,
                            worker_id=worker_id,
                        )
                    if (
                        assignment is not None
                        and resume_hint is not None
                        and assignment.assignment_reason
                        == "resume_browser_hint"
                        and assignment.page_policy == "existing"
                    ):
                        hinted_page = slot.page_registry.get(
                            resume_hint.page_id
                        )
                        expose_reusable_pages = bool(
                            isinstance(hinted_page, dict)
                            and str(hinted_page.get("fleetId") or "")
                            == assignment.fleet_id
                        )
                    if assignment is not None:
                        self._persist_task_browser_context(
                            slot,
                            assignment,
                            phase_id=phase_id,
                            primary_page_id=(
                                resume_hint.page_id
                                if (
                                    resume_hint is not None
                                    and expose_reusable_pages
                                    and assignment.assignment_reason
                                    == "resume_browser_hint"
                                )
                                else self.pinned_browser_context.page_id
                                if (
                                    self.pinned_browser_context is not None
                                    and assignment.fleet_id
                                    == self.pinned_browser_context.fleet_id
                                )
                                else ""
                            ),
                            replace_pages=bool(
                                resume_page_inventory_refreshed
                                or expose_reusable_pages
                                or (
                                    self.pinned_browser_context
                                    and self.pinned_browser_context.page_id
                                )
                            ),
                        )
        except asyncio.CancelledError:
            cancel_phase_running_reservation(
                self.logger,
                phase_id=phase_id,
                worker_id=worker_id,
            )
            if isinstance(slot, BrowserAgentSlot):
                # Cancellation can interrupt an in-flight RPC. ABCP responses
                # are not guaranteed to echo request ids, so reusing this
                # connection could let a late startup response satisfy the next
                # worker's call. Retire it instead of returning it to idle.
                slot.status = "broken"
                self.fleet_coordinator.mark_slot_suspect(slot.slot_id)
                if slot.current_worker_id == worker_id:
                    slot.current_worker_id = None
                if slot.client is not None:
                    try:
                        await asyncio.shield(slot.client.close())
                    except (asyncio.CancelledError, Exception):
                        pass
                    slot.client = None
                self.logger.write(
                    "spawner.slot.start_cancelled",
                    self._slot_summary(slot),
                )
            raise
        except FleetRoutingError as exc:
            cancel_phase_running_reservation(
                self.logger,
                phase_id=phase_id,
                worker_id=worker_id,
            )
            if isinstance(slot, BrowserAgentSlot):
                self._release_slot_start_failure(slot, worker_id=worker_id)
            if exc.code == "session_fleet_lost" and effective_session_key:
                binding = self.fleet_coordinator.session_binding_details(
                    effective_session_key
                ) or {}
                try:
                    self._handle_auth_session_lost({
                        "sessionKey": effective_session_key,
                        "fleetId": str(
                            exc.details.get("lostFleetId")
                            or binding.get("fleetId")
                            or ""
                        ),
                        "sessionGeneration": int(
                            binding.get("generation") or 0
                        ),
                        "reason": str(exc),
                    })
                except Exception as release_exc:
                    self.logger.write(
                        "auth_fleet.session_release_conflict",
                        (
                            release_exc.to_dict()
                            if isinstance(release_exc, FleetRoutingError)
                            else {"error": str(release_exc)[:500]}
                        ),
                    )
            result = {
                **exc.to_dict(),
                "workerId": worker_id,
                "name": agent_name,
                "slotId": getattr(slot, "slot_id", None),
            }
            self.logger.write("spawner.fleet.assignment_rejected", result)
            return result
        except Exception as exc:
            cancel_phase_running_reservation(
                self.logger,
                phase_id=phase_id,
                worker_id=worker_id,
            )
            if isinstance(slot, BrowserAgentSlot):
                if isinstance(exc, FleetReadinessError):
                    # A Fleet restore timeout is an acquisition failure, not
                    # proof that its owner WebSocket is corrupt.
                    self._release_slot_start_failure(slot, worker_id=worker_id)
                elif isinstance(exc, ABCPTransportError):
                    slot.status = "broken"
                    self.fleet_coordinator.mark_slot_suspect(slot.slot_id)
                    slot.current_worker_id = None
                    if slot.client is not None:
                        await slot.client.close()
                        slot.client = None
                else:
                    self._release_slot_start_failure(slot, worker_id=worker_id)
            failure_receipt = record_spawn_acquisition_failure(
                self.logger,
                acquisition_fingerprint=acquisition_fingerprint,
                phase_id=phase_id,
                exc=exc,
            )
            result = {
                **failure_receipt,
                "status": "failed",
                "error": str(exc),
                "workerId": worker_id,
                "name": agent_name,
            }
            if failure_receipt.get("status") == "spawn_infrastructure_exhausted":
                result["status"] = "spawn_infrastructure_exhausted"
            self.logger.write("spawner.slot.acquire_failed", result)
            return result
        if isinstance(slot, dict):
            cancel_phase_running_reservation(
                self.logger,
                phase_id=phase_id,
                worker_id=worker_id,
            )
            return slot

        async_task = asyncio.create_task(
            self._run_browser_worker(
                slot=slot,
                registration=registration,
                assignment=assignment,
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
                readiness_receipt=readiness_receipt,
            )
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
        clear_spawn_acquisition_failures(
            self.logger,
            acquisition_fingerprint=acquisition_fingerprint,
        )
        self.logger.write(
            "spawner.browser.spawn",
            {
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "slotReuse": bool(slot.last_worker_id),
                "pageReuseAllowed": expose_reusable_pages,
                "reuseScope": effective_reuse_scope,
                "pagePolicy": effective_page_policy,
                "sessionKey": effective_session_key,
                "fleetReference": effective_fleet_reference,
                "fleetGroupKey": fleet_group_key,
                "fleetReadiness": readiness_receipt,
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
            "reuseScope": effective_reuse_scope,
            "pagePolicy": effective_page_policy,
            "sessionKey": effective_session_key,
            "fleetReference": effective_fleet_reference,
            "fleetGroupKey": fleet_group_key,
            "fleetAssignment": assignment.to_dict() if assignment else None,
            "fleetReadiness": readiness_receipt,
        }

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
            agent_summary = {
                "workerId": handle.worker_id,
                "agentId": handle.agent_id,
                "slotId": handle.slot_id,
                "name": handle.name,
                "phaseId": handle.phase_id,
                "status": status,
                "task": handle.task,
            }
            assignment = self.fleet_coordinator.assignment_for_worker(
                handle.worker_id
            )
            if assignment is not None:
                agent_summary["fleetAssignment"] = assignment.to_dict()
            agents.append(agent_summary)
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
        for unsubscribe in list(self._notification_relays.values()):
            try:
                unsubscribe()
            except Exception:
                pass
        self._notification_relays.clear()
        await self.fleet_auth_barrier.shutdown()
        for slot in list(self._slots.values()):
            slot.status = "closed"
            slot.current_worker_id = None
            if slot.client is not None:
                await slot.client.close()
