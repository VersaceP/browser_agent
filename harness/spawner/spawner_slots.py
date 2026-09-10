"""
harness.spawner.spawner_slots - SpawnerSlotsMixin - session isolation, slot acquisition, fleet assignment, auth session.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from typing import AsyncIterator
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from abcp_client import ABCPTransportError
from harness.fleet.coordinator import FleetAssignment
from harness.fleet.coordinator import FleetRoutingError
from harness.fleet.coordinator import handle_records_from_value
from harness.fleet.coordinator import resolve_fleet_reference
from harness.fleet.task_reuse import find_similar_task_fleet
from harness.observation.event_observer import unwrap_notification
from harness.utils import JsonDict
from harness.utils import make_browser_event_logger
from harness.utils import optional_float
from harness.utils import optional_int
from .spawner_classification import _origin_from_url, _origins_from_text, _phase_family  # noqa: F401
from .spawner_helpers import BrowserAgentSlot, FleetReadinessError, ResumeBrowserHint, _SessionStartLock  # noqa: F401

def _sp():
    import harness.spawner as sp

    return sp

class SpawnerSlotsMixin:

    def _apply_worker_session_isolation(
        self,
        worker_contract: JsonDict,
        *,
        phase_id: str,
        session_key: str,
        fleet_reference: str,
        reuse_from_worker_id: str,
    ) -> JsonDict:
        """Give each worker its own Fleet when the deployment asks for it.

        `same_fleet_multiworker_enabled=False` is NOT enough on its own: it only
        drops the cross-slot task group. Two workers landing on one slot still
        converge on that slot's fleet through the `slot_default` / eligible
        fallback in `FleetCoordinator.choose_existing`, which is how
        browser-005 would have inherited browser-002's cookie jar in task
        48b4d7d7. Real per-worker isolation is `needs_isolated_session`.

        Anything that explicitly asks to SHARE wins: a named `session_key`, an
        explicit `fleet_id`, a hand-off from another worker, or an explicit
        `needs_isolated_session` in the contract. Those are the login flows —
        one Fleet carries one logged-in identity, so they cannot be split.
        """
        if not isinstance(worker_contract, dict):
            return worker_contract
        if worker_contract.get("needs_isolated_session") is not None:
            return worker_contract
        if not getattr(
            self.runtime.harness, "worker_session_isolation_enabled", False
        ):
            return worker_contract
        shared_intent = (
            str(session_key or "").strip()
            or str(fleet_reference or "").strip()
            or str(reuse_from_worker_id or "").strip()
            or self.pinned_browser_context is not None
            # A task-owned resume hint is an explicit request to continue the
            # prior cookie/storage partition when it still exists.  Deployment
            # default isolation is only a preference and must not manufacture
            # a fresh Fleet before the hint is probed.  A phase that explicitly
            # declared needs_isolated_session returned above and still wins.
            or (
                getattr(self, "resume_browser_hint", None) is not None
                and (
                    not self.resume_browser_hint.phase_id
                    or self.resume_browser_hint.phase_id
                    == str(phase_id or "").strip()
                )
            )
        )
        if shared_intent:
            return worker_contract
        if self._task_fleet_budget_exhausted():
            # Isolation is a preference here, not a declared identity boundary,
            # and honoring it would need a fleet the task no longer has budget
            # for. Leave the contract generic so ordinary reuse can serve it.
            self.logger.write("spawner.fleet.worker_isolation_skipped", {
                "phaseId": phase_id,
                "reason": "task_fleet_limit_reached",
                "maxTaskFleets": self._task_fleet_limit(),
                "taskFleetIds": sorted(
                    self.fleet_coordinator.task_fleet_ids()
                ),
            })
            return worker_contract
        isolated = dict(worker_contract)
        isolated["needs_isolated_session"] = True
        self.logger.write("spawner.fleet.worker_isolation_applied", {
            "phaseId": phase_id,
            "reason": "worker_session_isolation_enabled",
        })
        return isolated

    def _task_fleet_limit(self) -> int:
        """Configured ceiling on distinct Fleets for this task (0 = unlimited)."""

        return max(
            0,
            optional_int(getattr(self.runtime.harness, "max_task_fleets", 0), 0)
            or 0,
        )

    def _task_fleet_budget_exhausted(self) -> bool:
        limit = self._task_fleet_limit()
        return bool(limit) and len(
            self.fleet_coordinator.task_fleet_ids()
        ) >= limit

    def _busy_task_fleet_ids(self, worker_id: str) -> Set[str]:
        """Fleets a currently running worker is holding.

        The cap's reuse path ranks these last so a spare fleet absorbs the
        worker first. It does not refuse them: ordinary routing already places
        two live workers in one fleet, so refusing here would reject a worker
        for something allowed one fleet earlier. The requesting worker's own
        slot is already marked with its id and is not a conflict with itself.
        """

        busy: Set[str] = set()
        for slot in self._slots.values():
            holder = str(slot.current_worker_id or "").strip()
            if not holder or holder == str(worker_id):
                continue
            assignment = self.fleet_coordinator.assignment_for_worker(holder)
            if assignment is not None:
                busy.add(assignment.fleet_id)
        return busy

    async def _assign_within_task_fleet_cap(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        reuse_scope: str,
        page_policy: str,
        session_key: str,
        needs_isolated_session: bool,
        isolation_auto_applied: bool,
        fleet_group_key: str,
    ) -> Optional[FleetAssignment]:
        """Gate the one place a task creates a Fleet against its fleet budget.

        Returning None means the budget still has room and the caller may create.
        The harness never closes a Fleet, so an uncapped task keeps every fleet
        it ever opened for as long as the platform reports it.

        An identity boundary is never merged into another cookie jar: a new
        named session, or an isolation flag the phase itself declared, fails
        closed with a retryable receipt. Isolation the deployment applied by
        default carries no identity, so it degrades to reuse instead.

        A fleet a running worker already holds is ranked last but still
        reusable: ordinary routing puts two live workers in one fleet, so the
        cap must not be stricter than the rule it degrades from.

        Nothing here is decided on a stale view. `slot.fleet_ids` is a
        30-second cache, so a fleet another slot created moments ago can be
        missing from it, and a fleet the platform already dropped can still be
        in it. Before the cap refuses anything it re-reads the authoritative
        Fleet.list once — the same rule an explicit fleet_id reference follows —
        and re-decides, which is also what lets a vanished fleet hand its budget
        back. It still never binds a fleet the acting connection has not seen.
        """

        limit = self._task_fleet_limit()
        if not limit:
            return None
        if len(self.fleet_coordinator.task_fleet_ids()) < limit:
            return None

        identity_boundary = bool(session_key) or (
            needs_isolated_session and not isolation_auto_applied
        )

        def select() -> Optional[FleetAssignment]:
            if identity_boundary:
                return None
            return self.fleet_coordinator.choose_under_cap(
                worker_id=worker_id,
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                candidate_fleet_ids=slot.fleet_ids,
                reuse_scope=reuse_scope,
                page_policy=page_policy,
                fleet_group_key=fleet_group_key,
                busy_fleet_ids=self._busy_task_fleet_ids(worker_id),
            )

        def receipt_for(occupied: Set[str]) -> JsonDict:
            return {
                "workerId": worker_id,
                "slotId": slot.slot_id,
                "maxTaskFleets": limit,
                "taskFleetIds": sorted(occupied),
                "sessionKey": session_key,
                "needsIsolatedSession": bool(needs_isolated_session),
                "isolationAutoApplied": bool(isolation_auto_applied),
                "busyFleetIds": sorted(self._busy_task_fleet_ids(worker_id)),
            }

        assignment = select()
        if assignment is None:
            # The cached inventory says "refuse". Confirm that against the
            # authoritative view before acting on it.
            inventory_sync = await self._sync_slot_registry(
                slot,
                worker_id=worker_id,
                include_page_details=False,
            )
            self._observe_slot_fleets(slot)
            if bool(inventory_sync.get("fleetListSucceeded")):
                # Fleet.list answered, so this connection now holds a complete
                # inventory. `_observe_slot_fleets` can only retire this slot's
                # own records; a fleet another slot created and the platform has
                # since dropped would otherwise hold task budget forever.
                retired = self.fleet_coordinator.reconcile_missing_fleets(
                    slot.fleet_ids
                )
                if retired:
                    self.logger.write("spawner.fleet.inventory_retired", {
                        "workerId": worker_id,
                        "slotId": slot.slot_id,
                        "retiredFleetIds": retired,
                    })
            occupied = self.fleet_coordinator.task_fleet_ids()
            if len(occupied) < limit:
                self.logger.write(
                    "spawner.fleet.cap_released",
                    receipt_for(occupied),
                )
                return None
            assignment = select()

        occupied = self.fleet_coordinator.task_fleet_ids()
        if assignment is None:
            receipt = receipt_for(occupied)
            self.logger.write("spawner.fleet.cap_blocked", receipt)
            if identity_boundary:
                raise FleetRoutingError(
                    "task_fleet_limit_reached",
                    (
                        f"the task already occupies {len(occupied)} of"
                        f" {limit} allowed fleets and this worker asks for a"
                        " separate session identity"
                    ),
                    retryable=True,
                    next_instruction=(
                        "Waiting does not clear this: the harness does not"
                        " close fleets, so a finished worker keeps its fleet."
                        " Continue on a fleet the task already has — drop"
                        " needs_isolated_session, or pass the exact session_key"
                        " already bound to it — or raise harness.max_task_fleets."
                    ),
                    details=receipt,
                )
            raise FleetRoutingError(
                "task_fleet_limit_reached",
                (
                    f"the task already occupies {len(occupied)} of"
                    f" {limit} allowed fleets and none of them may be lent to a"
                    " generic worker (each one is bound to, or released from, a"
                    " named session)"
                ),
                retryable=True,
                next_instruction=(
                    "Waiting does not release a named session; its fleet stays"
                    " bound after the worker ends. Continue that session with"
                    " its exact session_key, release the binding through the"
                    " auth-recovery flow, or raise harness.max_task_fleets."
                ),
                details=receipt,
            )
        self.logger.write(
            "spawner.fleet.cap_reuse",
            {**receipt_for(occupied), "assignedFleetId": assignment.fleet_id},
        )
        return assignment

    def _fleet_group_key(
        self,
        *,
        session_key: str,
        worker_id: str,
        needs_isolated_session: bool,
        fleet_reference: str = "",
    ) -> str:
        """Which workers should be held to ONE fleet.

        A group exists so several workers can share a coordinator-owned fleet
        with relayed notifications. Workers the caller routed to DIFFERENT
        fleets are the opposite of that, so an explicit reference has to be part
        of the key: without it the task-wide group bound itself to whichever
        fleet spawned first and refused every later spawn naming the other one
        as `fleet_routing_conflict`. Task 9d490dc3 lost four of its seven
        products that way, on an instruction that named two fleets.

        The reference is used verbatim rather than resolved to a UUID, because
        resolution needs a slot this decision runs before. Two spellings of one
        fleet therefore land in two groups — that costs those workers the
        multiworker relay and nothing else, whereas collapsing distinct fleets
        into one key would resurrect the conflict this fixes. The failure mode
        has to be a lost optimisation, never a refused spawn.
        """
        if not getattr(
            self.runtime.harness, "same_fleet_multiworker_enabled", False
        ):
            return ""
        key = str(session_key or "").strip()
        if key:
            return f"session:{key}"
        if needs_isolated_session:
            return f"isolated:{worker_id}"
        reference = str(fleet_reference or "").strip().lower()
        if reference:
            return f"task:{self.logger.task_id}:fleet:{reference}"
        return f"task:{self.logger.task_id}"

    @asynccontextmanager
    async def _session_start_guard(self, session_key: str) -> AsyncIterator[None]:
        """Serialize startup only for workers sharing one named session.

        Dictionary access is synchronous on the single asyncio event loop. The
        reference count includes waiters, so the keyed lock can be removed
        without racing a task that has already selected it.
        """

        key = str(session_key or "").strip()
        if not key:
            yield
            return
        entry = self._session_start_locks.get(key)
        if entry is None:
            entry = _SessionStartLock()
            self._session_start_locks[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if (
                entry.users <= 0
                and self._session_start_locks.get(key) is entry
            ):
                self._session_start_locks.pop(key, None)

    def _validate_routing_intent(
        self,
        *,
        session_key: Optional[str],
        preferred_slot_id: Optional[str],
        reuse_from_worker_id: Optional[str],
    ) -> None:
        """Fail closed when explicit routing selectors disagree.

        A caller may provide redundant selectors for a continuation, but they
        must resolve to the same current slot/fleet. Silent precedence would
        make the lead believe an explicit handoff occurred when it did not.
        """

        source_worker = str(reuse_from_worker_id or "").strip()
        source_handle = self._handles.get(source_worker) if source_worker else None
        if not getattr(self.runtime.harness, "fleet_reuse_enabled", True):
            return
        self.fleet_coordinator.validate_routing_intent(
            session_key=str(session_key or "").strip(),
            preferred_slot_id=str(preferred_slot_id or "").strip(),
            reuse_from_worker_id=source_worker,
            reuse_source_known=source_handle is not None,
            reuse_source_slot_id=str(
                source_handle.slot_id if source_handle is not None else ""
            ).strip(),
        )

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
        session_key: str = "",
        fleet_id: str = "",
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
            if slot.status in {"starting", "running"} or slot.current_worker_id
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
            capacity={
                # Three separate facts, because one number cannot answer "can
                # another worker start now?". An earlier revision sent only
                # `max_slots - live_slots`, which is capacity NEVER CREATED: in
                # the ordinary mid-run state of six live slots with five idle
                # it reads zero and tells the Lead to wait while five slots sit
                # free. Reusing an idle slot and creating a new one are both
                # routes to parallelism, and max_browser_agents caps both.
                "idleSlots": sum(
                    1 for slot in self._slots.values() if slot.status == "idle"
                ),
                "uncreatedSlotCapacity": max(0, max_slots - len(live_slots)),
                "concurrencyHeadroom": max(
                    0,
                    int(self.runtime.harness.max_browser_agents or 0)
                    - len(running_slots),
                ),
            },
        )
        if explicit_rejection is not None:
            return explicit_rejection

        pinned = self.pinned_browser_context
        if pinned is not None and pinned.page_id:
            owner_slot_id = self.fleet_coordinator.owner_slot_for_fleet(
                pinned.fleet_id
            )
            owner_slot = self._slots.get(owner_slot_id) if owner_slot_id else None
            matching_slots = [
                slot
                for slot in live_slots
                if (
                    pinned.fleet_id in slot.fleet_ids
                    and pinned.page_id in slot.page_registry
                )
            ]
            idle_matches = [
                item
                for item in matching_slots
                if item.status == "idle" and not item.current_worker_id
            ]
            pinned_slot = owner_slot or (
                None
                if idle_matches
                else (
                    sorted(matching_slots, key=lambda item: item.slot_id)[0]
                    if matching_slots
                    else None
                )
            )
            if (
                pinned_slot is not None
                and (
                    pinned_slot.status != "idle"
                    or pinned_slot.current_worker_id
                )
            ):
                return {
                    "status": "pinned_browser_context_busy",
                    "error": (
                        f"pinned page {pinned.page_id!r} is attached to busy"
                        f" slot {pinned_slot.slot_id!r}"
                    ),
                    "pinnedBrowserContext": pinned.to_dict(),
                    "slot": self._slot_summary(pinned_slot),
                    "tool_was_executed": False,
                    "next_instruction": (
                        "Wait for the worker using the pinned page to finish;"
                        " do not create or select another fleet/page."
                    ),
                }

        task_binding = getattr(self, "_task_session_binding", None)
        if (
            pinned is None
            and task_binding is not None
            and str(getattr(task_binding, "state", "") or "") != "stale"
            and (
                getattr(task_binding, "requires_exact_page", False)
                or not getattr(
                    self.runtime.harness,
                    "same_fleet_multiworker_enabled",
                    False,
                )
            )
        ):
            owner_slot_id = self.fleet_coordinator.owner_slot_for_fleet(
                task_binding.fleet_id
            )
            owner_slot = self._slots.get(owner_slot_id) if owner_slot_id else None
            matching_slots = [
                item for item in live_slots
                if (
                    task_binding.fleet_id in item.fleet_ids
                    and (
                        not getattr(task_binding, "requires_exact_page", False)
                        or task_binding.page_id in item.page_registry
                    )
                )
            ]
            task_slot = owner_slot or (
                sorted(matching_slots, key=lambda item: item.slot_id)[0]
                if matching_slots else None
            )
            if (
                task_slot is not None
                and (
                    task_slot.status != "idle"
                    or bool(task_slot.current_worker_id)
                )
            ):
                return {
                    "status": (
                        "task_session_context_busy"
                        if getattr(task_binding, "requires_exact_page", False)
                        else "task_session_fleet_busy"
                    ),
                    "error": (
                        (
                            f"task continuation page {task_binding.page_id!r}"
                            if getattr(task_binding, "requires_exact_page", False)
                            else f"task continuity Fleet {task_binding.fleet_id!r}"
                        )
                        + f" is attached to busy slot {task_slot.slot_id!r}"
                    ),
                    "retryable": True,
                    "pinSource": (
                        "task_page_continuation"
                        if getattr(task_binding, "requires_exact_page", False)
                        else "task_auth"
                    ),
                    "taskSessionBinding": task_binding.to_dict(),
                    "slot": self._slot_summary(task_slot),
                    "tool_was_executed": False,
                    "next_instruction": (
                        "Wait for the worker using this bound task context."
                        " Do not start another worker or choose a replacement"
                        " Fleet/Page."
                    ),
                }

        session_slot_id = self.fleet_coordinator.preferred_slot_for_session(
            session_key
        )
        if session_slot_id:
            session_slot = self._slots.get(session_slot_id)
            if session_slot is None:
                session_slot_id = None
            elif session_slot.status == "broken":
                reset_threshold = max(
                    1,
                    optional_int(
                        getattr(
                            self.runtime.harness,
                            "fleet_slot_manual_reset_after_failures",
                            3,
                        ),
                        3,
                    ) or 3,
                )
                manual_reset_required = (
                    session_slot.recovery_failure_cycles >= reset_threshold
                )
                binding = self.fleet_coordinator.session_binding_details(
                    session_key
                ) or {}
                return {
                    "status": (
                        "session_manual_reset_required"
                        if manual_reset_required
                        else "session_transport_unavailable"
                    ),
                    "error": (
                        f"session_key {session_key!r} owner connection could not"
                        f" be restored for slot {session_slot_id}"
                    ),
                    "retryable": not manual_reset_required,
                    "slot": self._slot_summary(session_slot),
                    "sessionKey": session_key,
                    "fleetId": binding.get("fleetId"),
                    "sessionGeneration": binding.get("generation"),
                    "recoveryFailureCycles": (
                        session_slot.recovery_failure_cycles
                    ),
                    "next_instruction": (
                        (
                            "An operator must either restore the original browser"
                            " transport or call the host-only reset_auth_session"
                            " API with this fleet and generation. Do not let the"
                            " model release or silently rebind the session."
                        )
                        if manual_reset_required
                        else (
                            "Retry later after browser transport recovers. Do not"
                            " create or bind a different fleet for this session;"
                            " transport failure is not proof that its fleet is lost."
                        )
                    ),
                }
            elif (
                session_slot.status != "idle"
                and not getattr(
                    self.runtime.harness,
                    "same_fleet_multiworker_enabled",
                    False,
                )
            ):
                return {
                    "status": "session_slot_busy",
                    "error": (
                        f"session_key {session_key!r} is bound to busy slot"
                        f" {session_slot_id}"
                    ),
                    "slot": self._slot_summary(session_slot),
                    "next_instruction": (
                        "Wait for the worker using this session slot; do not"
                        " create or select a different fleet for the same session."
                    ),
                }

        slot = self._select_idle_slot(
            phase_id=phase_id,
            task=task,
            context=context,
            result_contract=result_contract,
            worker_contract=worker_contract,
            preferred_slot_id=preferred_slot_id,
            reuse_from_worker_id=reuse_from_worker_id,
            session_key=session_key,
            fleet_id=fleet_id,
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
            slot = self._reserve_new_slot()

        slot.status = "running" if slot.client is not None else "starting"
        slot.current_worker_id = worker_id
        slot.last_contract_hash = contract_hash
        return slot

    def _explicit_slot_rejection(
        self,
        *,
        preferred_slot_id: Optional[str],
        reuse_from_worker_id: Optional[str],
        capacity: Optional[JsonDict] = None,
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
                # Two different situations wear this same error, and telling
                # the caller to wait is right for only one of them. In task
                # fae5a7b6 the Lead sent two spawns in one turn -- the correct
                # fan-out shape -- and the second was a SIBLING that merely
                # carried reuse_from_worker_id out of habit. It was refused
                # with "wait", the Lead obeyed, and the remaining six phases
                # ran strictly one at a time through a single slot while five
                # slots in the pool were never created at all.
                facts = capacity if isinstance(capacity, dict) else {}
                idle_slots = max(0, int(facts.get("idleSlots") or 0))
                uncreated = max(0, int(facts.get("uncreatedSlotCapacity") or 0))
                concurrency = max(0, int(facts.get("concurrencyHeadroom") or 0))
                startable = min(idle_slots + uncreated, concurrency)
                rejection: JsonDict = {
                    "status": "rejected",
                    "error": f"slot for reuse_from_worker_id is not idle: {reuse_worker}",
                    "workerId": reuse_worker,
                    "slot": self._slot_summary(slot),
                    "idleSlots": idle_slots,
                    "uncreatedSlotCapacity": uncreated,
                    "concurrencyHeadroom": concurrency,
                    "startableNow": startable,
                }
                if startable > 0:
                    where = (
                        f"{idle_slots} idle slot(s) and room for {uncreated}"
                        " more"
                        if idle_slots and uncreated else
                        f"{idle_slots} idle slot(s)" if idle_slots else
                        f"room for {uncreated} more slot(s)"
                    )
                    rejection["next_instruction"] = (
                        f"Slot {slot.slot_id} is busy, but the pool has {where}"
                        f" and can run {startable} more worker(s) right now."
                        " reuse_from_worker_id asks to CONTINUE inside"
                        f" {reuse_worker}'s own page, and one page serves one"
                        " worker at a time -- that pin is what serialises this"
                        " spawn, not the pool. If this phase is a parallel"
                        " SIBLING (a different entity of the same stage) it"
                        " does not need that page: drop reuse_from_worker_id"
                        " and page_policy=existing, keep the same fleet_id,"
                        " and re-send it now together with every other pending"
                        " sibling. Wait only if this spawn must genuinely"
                        f" resume {reuse_worker}'s unfinished page."
                    )
                else:
                    rejection["next_instruction"] = (
                        "Wait for the related worker/slot to finish before"
                        " spawning this continuation."
                    )
                return rejection
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
        session_key: str,
        fleet_id: str = "",
    ) -> Optional[BrowserAgentSlot]:
        idle_slots = [
            slot for slot in self._slots.values()
            if slot.status == "idle"
        ]
        if not idle_slots:
            return None

        # Prefer the stable owner (or an idle observer) when the Lead supplied
        # an existing Fleet UUID/prefix.  Final uniqueness/existence proof is
        # intentionally deferred until the selected slot has refreshed its
        # authoritative Fleet.list inventory.
        fleet_reference = str(fleet_id or "").strip().lower()
        if fleet_reference:
            known_ids = {
                known_fleet
                for candidate_slot in self._slots.values()
                for known_fleet in candidate_slot.fleet_ids
                if str(known_fleet).lower().startswith(fleet_reference)
            }
            if len(known_ids) == 1:
                resolved = next(iter(known_ids))
                owner_slot_id = self.fleet_coordinator.owner_slot_for_fleet(
                    resolved
                )
                owner_slot = (
                    self._slots.get(owner_slot_id) if owner_slot_id else None
                )
                if owner_slot in idle_slots:
                    return owner_slot
                matching_slots = [
                    item for item in idle_slots
                    if resolved in item.fleet_ids
                ]
                if matching_slots:
                    return sorted(
                        matching_slots, key=lambda item: item.slot_id
                    )[0]

        pinned = self.pinned_browser_context
        if pinned is not None:
            owner_slot_id = self.fleet_coordinator.owner_slot_for_fleet(
                pinned.fleet_id
            )
            owner_slot = self._slots.get(owner_slot_id) if owner_slot_id else None
            if owner_slot is not None and owner_slot.status == "idle":
                if not pinned.page_id or pinned.page_id in owner_slot.page_registry:
                    return owner_slot
            pinned_matches = [
                slot
                for slot in idle_slots
                if (
                    pinned.fleet_id in slot.fleet_ids
                    and (
                        not pinned.page_id
                        or pinned.page_id in slot.page_registry
                    )
                )
            ]
            if pinned_matches:
                return sorted(pinned_matches, key=lambda item: item.slot_id)[0]

        session_slot_id = self.fleet_coordinator.preferred_slot_for_session(
            session_key
        )
        if session_slot_id:
            session_slot = self._slots.get(session_slot_id)
            if session_slot is not None and session_slot.status == "idle":
                return session_slot

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

    def _reserve_new_slot(self) -> BrowserAgentSlot:
        slot_id = self._next_slot_id()
        agent_id = f"{self.runtime.agent_id}-{slot_id}"
        slot = BrowserAgentSlot(
            slot_id=slot_id,
            agent_id=agent_id,
            status="starting",
        )
        self._slots[slot_id] = slot
        self.logger.write(
            "spawner.slot.reserved",
            self._slot_summary(slot),
        )
        return slot

    async def _initialize_reserved_slot(self, slot: BrowserAgentSlot) -> None:
        """Connect and fully initialize a slot before Fleet work begins.

        Newer ABCP Dispatchers require the connection sequence
        ``System.register -> System.getCapabilities`` before any business
        Action such as Fleet.list/create/status.  A worker used to defer the
        capability load until after its Fleet had been assigned; that is too
        late for the connection contract and can make the Dispatcher close the
        socket without a close frame.
        """

        if slot.client is not None:
            if slot.status == "starting":
                slot.status = "running" if slot.current_worker_id else "idle"
            return
        event_logger = make_browser_event_logger(
            self.logger,
            self.runtime.harness.log_browser_payloads,
            prefix=f"{slot.slot_id}.transport",
        )
        client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)
        slot.client = client
        slot.idle_event_logger = event_logger
        bootstrap_started = time.monotonic()
        timings: JsonDict = {}
        outcome = "failed"
        try:
            stage_started = time.monotonic()
            await client.connect()
            timings["connectMs"] = int(
                (time.monotonic() - stage_started) * 1000
            )
            stage_started = time.monotonic()
            registration = await client.call(
                "System.register",
                {"agentId": slot.agent_id},
            )
            timings["registerMs"] = int(
                (time.monotonic() - stage_started) * 1000
            )
            stage_started = time.monotonic()
            caps_response = await client.call(
                "System.getCapabilities",
                {"guide": "content"},
            )
            timings["getCapabilitiesMs"] = int(
                (time.monotonic() - stage_started) * 1000
            )
            stage_started = time.monotonic()
            await self._capability_bundle_for_worker(
                client,
                self.runtime,
                caps_response=caps_response,
            )
            timings["schemaBundleMs"] = int(
                (time.monotonic() - stage_started) * 1000
            )
            outcome = "ready"
        except Exception:
            try:
                await client.close()
            finally:
                slot.client = None
                slot.status = "broken"
            raise
        finally:
            self.logger.write(
                "spawner.slot.bootstrap_timing",
                {
                    "slotId": slot.slot_id,
                    "agentId": slot.agent_id,
                    **timings,
                    "elapsedMs": int(
                        (time.monotonic() - bootstrap_started) * 1000
                    ),
                    "outcome": outcome,
                },
            )
        slot.registration = registration
        slot.protocol_initialized = True
        self._replace_slot_fleets_from_response(slot, registration)
        self._update_slot_registry_from_value(slot, registration)
        slot.status = "running" if slot.current_worker_id else "idle"
        self.logger.write(
            "spawner.slot.created",
            self._slot_summary(slot),
        )

    async def _recover_broken_slots(self) -> None:
        """Reconnect quarantined slots with their original owner agentId.

        Only the transport and owner inventory are retried. We deliberately do
        not replay the browser RPC that failed because a mutating call may have
        reached the Dispatcher before the response was lost.
        """

        if not any(
            slot.status == "broken"
            and not slot.current_worker_id
            and self.fleet_coordinator.slot_has_session_binding(slot.slot_id)
            for slot in self._slots.values()
        ):
            return
        if self._broken_slot_recovery_lock is None:
            self._broken_slot_recovery_lock = asyncio.Lock()
        async with self._broken_slot_recovery_lock:
            broken = [
                slot for slot in list(self._slots.values())
                if slot.status == "broken"
                and not slot.current_worker_id
                and self.fleet_coordinator.slot_has_session_binding(slot.slot_id)
            ]
            for slot in broken:
                if self._slots.get(slot.slot_id) is not slot:
                    continue
                recovered = await self._recover_broken_slot(slot)
                if recovered:
                    continue
                slot.recovery_failure_cycles += 1
                if not slot.recovery_unavailable_since:
                    slot.recovery_unavailable_since = time.time()
                self.logger.write(
                    "spawner.slot.recovery_deferred",
                    self._slot_summary(slot),
                )

    async def _recover_broken_slot(self, slot: BrowserAgentSlot) -> bool:
        attempts = max(
            1,
            optional_int(
                getattr(
                    self.runtime.harness,
                    "fleet_slot_reconnect_attempts",
                    2,
                ),
                2,
            ) or 2,
        )
        backoff = max(
            0.0,
            optional_float(
                getattr(
                    self.runtime.harness,
                    "fleet_slot_reconnect_backoff_seconds",
                    0.25,
                ),
                0.25,
            ) or 0.0,
        )
        event_logger = slot.idle_event_logger or make_browser_event_logger(
            self.logger,
            self.runtime.harness.log_browser_payloads,
            prefix=f"{slot.slot_id}.transport",
        )
        for attempt in range(1, attempts + 1):
            client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)
            try:
                await client.connect()
                registration = await client.call(
                    "System.register",
                    {"agentId": slot.agent_id},
                )
                caps_response = await client.call(
                    "System.getCapabilities",
                    {"guide": "content"},
                )
                await self._capability_bundle_for_worker(
                    client,
                    self.runtime,
                    caps_response=caps_response,
                )
            except Exception as exc:
                slot.sync_errors.append(
                    f"reconnect {attempt}/{attempts}: {str(exc)[:300]}"
                )
                try:
                    await client.close()
                except Exception:
                    pass
                self.logger.write(
                    "spawner.slot.recovery_failed",
                    {
                        **self._slot_summary(slot),
                        "attempt": attempt,
                        "maxAttempts": attempts,
                        "error": str(exc)[:500],
                    },
                )
                if attempt < attempts and backoff > 0:
                    await asyncio.sleep(backoff * attempt)
                continue

            slot.client = client
            slot.idle_event_logger = event_logger
            slot.registration = registration
            slot.protocol_initialized = True
            self._replace_slot_fleets_from_response(slot, registration)
            self._update_slot_registry_from_value(slot, registration)
            self._observe_slot_fleets(slot)
            slot.status = "idle"
            slot.recovery_failure_cycles = 0
            slot.recovery_unavailable_since = 0.0
            self.logger.write(
                "spawner.slot.recovered",
                {
                    **self._slot_summary(slot),
                    "attempt": attempt,
                    "reusedAgentId": True,
                },
            )
            return True
        return False

    def _cleanup_retired_slots(self) -> None:
        retired = [
            slot_id
            for slot_id, slot in self._slots.items()
            if (
                slot.status == "closed"
                or (
                    slot.status == "broken"
                    and not self.fleet_coordinator.slot_has_session_binding(slot_id)
                )
            )
            and not slot.current_worker_id
        ]
        for slot_id in retired:
            slot = self._slots.pop(slot_id, None)
            if slot is not None:
                self._remove_notification_relays_for_slot(slot_id)
                self.fleet_coordinator.retire_slot(slot_id)
                for fleet_id in slot.fleet_ids:
                    self.fleet_auth_barrier.discard_inactive(fleet_id)
                self.logger.write(
                    "spawner.slot.retired",
                    self._slot_summary(slot),
                )

    def _remove_notification_relays_for_slot(self, slot_id: str) -> None:
        for key, unsubscribe in list(self._notification_relays.items()):
            owner_slot_id, acting_slot_id, _fleet_id = key
            if slot_id not in {owner_slot_id, acting_slot_id}:
                continue
            try:
                unsubscribe()
            finally:
                self._notification_relays.pop(key, None)

    def _remove_notification_relay_for_assignment(
        self,
        assignment: Optional[FleetAssignment],
    ) -> None:
        if assignment is None or not assignment.delegated:
            return
        key = (
            assignment.owner_slot_id,
            assignment.slot_id,
            assignment.fleet_id,
        )
        unsubscribe = self._notification_relays.pop(key, None)
        if unsubscribe is not None:
            unsubscribe()

    def _release_slot_start_failure(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
    ) -> None:
        self._release_slot_to_pool(
            slot,
            worker_id=worker_id,
            event="spawner.slot.start_released",
            remember_worker=False,
        )

    def _release_slot_to_pool(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        event: str,
        remember_worker: bool,
    ) -> None:
        """Single transition for returning a healthy slot to the idle pool."""

        self.page_lease_manager.release_worker(worker_id)
        if remember_worker:
            slot.last_worker_id = worker_id
        if slot.current_worker_id == worker_id:
            slot.current_worker_id = None
        if slot.status not in {"broken", "closed"}:
            slot.status = "idle"
        if slot.client is not None and slot.idle_event_logger is not None:
            slot.client.on_event = slot.idle_event_logger
        self.logger.write(
            event,
            self._slot_summary(slot),
        )

    async def _prepare_slot_for_worker(
        self,
        slot: BrowserAgentSlot,
        worker_id: str,
        *,
        expose_reusable_pages: bool,
        required_fleet_id: str = "",
    ) -> Tuple[JsonDict, JsonDict]:
        if slot.client is None:
            raise ABCPTransportError(f"Slot {slot.slot_id} has no browser client")
        # Normal slots were registered and capability-initialized when the
        # connection was created/recovered. Re-registering them here can reset
        # the Dispatcher-side initialization sequence immediately before Fleet
        # actions, so use that authoritative receipt instead. The fallback is
        # retained solely for externally-adopted legacy slots that predate this
        # invariant (including resume/test injection paths).
        if slot.protocol_initialized:
            registration = slot.registration
        else:
            registration = await slot.client.call(
                "System.register",
                {"agentId": slot.agent_id},
            )
        slot.registration = registration
        self._replace_slot_fleets_from_response(slot, registration)
        self._update_slot_registry_from_value(slot, registration)
        inventory_sync: JsonDict = {
            "fleetListSucceeded": False,
            "pageListAttemptedFleetIds": [],
            "pageListSucceededFleetIds": [],
        }
        if expose_reusable_pages or self._slot_sync_due(slot):
            inventory_sync = await self._sync_slot_registry(
                slot,
                worker_id=worker_id,
                required_fleet_id=required_fleet_id,
                include_page_details=False,
            )
        self._observe_slot_fleets(slot)
        return registration, inventory_sync

    def _observe_slot_fleets(self, slot: BrowserAgentSlot) -> None:
        """Refresh non-authoritative routing metadata from the slot snapshot."""

        origins_by_fleet: Dict[str, Set[str]] = {}
        for page in slot.page_registry.values():
            fleet_id = str(page.get("fleetId") or "").strip()
            origin = str(
                page.get("origin") or _origin_from_url(page.get("url") or "")
            ).strip()
            if fleet_id and origin:
                origins_by_fleet.setdefault(fleet_id, set()).add(origin)
        self.fleet_coordinator.observe_slot(
            slot_id=slot.slot_id,
            owner_agent_id=slot.agent_id,
            fleet_ids=slot.fleet_ids,
            origins_by_fleet=origins_by_fleet,
            # Reclaimed inventory is quarantined until a persisted auth entry
            # restores its restrictions. Fleets created/bound in this process
            # retain their admitted bit across later observations.
            admit_unbound=False,
        )
        self._reconcile_auth_ledger_for_slot(slot)

    def _reconcile_auth_ledger_for_slot(self, slot: BrowserAgentSlot) -> None:
        for entry in self.auth_fleet_ledger.entries():
            if entry.get("stale"):
                continue
            fleet_id = str(entry.get("fleetId") or "").strip()
            session_key = str(entry.get("sessionKey") or "").strip()
            owner_agent_id = str(entry.get("ownerAgentId") or "").strip()
            if not fleet_id or not session_key:
                continue
            if owner_agent_id:
                if owner_agent_id != slot.agent_id:
                    continue
            elif fleet_id not in slot.fleet_ids:
                continue
            status = "active" if fleet_id in slot.fleet_ids else "missing"
            self.fleet_coordinator.restore_auth_binding(
                fleet_id=fleet_id,
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                session_key=session_key,
                session_generation=int(
                    entry.get("sessionGeneration") or 1
                ),
                is_isolated=True,
                status=status,
            )
            self.logger.write(
                "auth_fleet.reconciled",
                {
                    "sessionKey": session_key,
                    "fleetId": fleet_id,
                    "slotId": slot.slot_id,
                    "status": status,
                },
            )

    async def _assign_fleet_for_worker(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        worker_contract: JsonDict,
        reuse_scope: str,
        page_policy: str,
        session_key: str,
        reuse_from_worker_id: str,
        fleet_id: str = "",
        fleet_group_key: str = "",
        isolation_auto_applied: bool = False,
        resume_browser_hint: Optional[ResumeBrowserHint] = None,
        resume_hint_may_select_page: bool = True,
        root_task: str = "",
        automatic_task_reuse_allowed: bool = False,
        inventory_sync: Optional[JsonDict],
    ) -> Optional[FleetAssignment]:
        lock_key = str(
            fleet_group_key
            or (f"fleet:{fleet_id.lower()}" if fleet_id else "")
            or (f"session:{session_key}" if session_key else "")
        ).strip()
        if self._task_fleet_limit():
            # The per-task fleet budget is one shared counter, so the narrower
            # group/session/fleet keys are not enough: two undirected spawns
            # would both read "under the cap" and both create. One key for every
            # fleet decision is strictly stronger serialization than the keys it
            # replaces, and the readiness barrier still runs outside this guard.
            lock_key = "task_fleet_budget"
        async with self._session_start_guard(
            f"assignment:{lock_key}" if lock_key else ""
        ):
            return await self._assign_fleet_for_worker_locked(
                slot,
                worker_id=worker_id,
                worker_contract=worker_contract,
                reuse_scope=reuse_scope,
                page_policy=page_policy,
                session_key=session_key,
                fleet_id=fleet_id,
                reuse_from_worker_id=reuse_from_worker_id,
                fleet_group_key=fleet_group_key,
                isolation_auto_applied=isolation_auto_applied,
                resume_browser_hint=resume_browser_hint,
                resume_hint_may_select_page=resume_hint_may_select_page,
                root_task=root_task,
                automatic_task_reuse_allowed=automatic_task_reuse_allowed,
                inventory_sync=inventory_sync,
            )

    async def _assign_fleet_for_worker_locked(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        worker_contract: JsonDict,
        reuse_scope: str,
        page_policy: str,
        session_key: str,
        reuse_from_worker_id: str,
        fleet_id: str = "",
        fleet_group_key: str = "",
        isolation_auto_applied: bool = False,
        resume_browser_hint: Optional[ResumeBrowserHint] = None,
        resume_hint_may_select_page: bool = True,
        root_task: str = "",
        automatic_task_reuse_allowed: bool = False,
        inventory_sync: Optional[JsonDict],
    ) -> Optional[FleetAssignment]:
        """Select or create the one fleet the worker is allowed to address.

        Fleet creation is harness-controlled.  The worker model never gets to
        rely on Dispatcher fleetless auto-selection or invent a fleet id.
        """

        if not getattr(self.runtime.harness, "fleet_reuse_enabled", True):
            return None
        if slot.client is None:
            raise ABCPTransportError(f"Slot {slot.slot_id} has no browser client")

        self._observe_slot_fleets(slot)
        needs_isolated_session = bool(
            worker_contract.get("needs_isolated_session", False)
        )
        pinned = self.pinned_browser_context
        if pinned is not None:
            if pinned.fleet_id not in slot.fleet_ids:
                if (
                    inventory_sync is not None
                    and not inventory_sync.get("fleetListSucceeded")
                ):
                    raise FleetRoutingError(
                        "fleet_inventory_temporarily_unavailable",
                        "Fleet.list did not answer while resolving the pinned Fleet.",
                        retryable=True,
                        next_instruction=(
                            "Retry with the same pinned context after Fleet.list"
                            " succeeds; do not create a replacement Fleet."
                        ),
                        details={
                            "pinnedBrowserContext": pinned.to_dict(),
                            "registrySync": dict(inventory_sync),
                        },
                    )
                raise FleetRoutingError(
                    "pinned_fleet_unavailable",
                    (
                        f"pinned fleet {pinned.fleet_id!r} was not returned by"
                        f" the authoritative inventory for slot {slot.slot_id!r}"
                    ),
                    retryable=False,
                    next_instruction=(
                        "Do not create a replacement fleet. Ask the user to"
                        " reopen or reselect the pinned browser instance."
                    ),
                    details={"pinnedBrowserContext": pinned.to_dict()},
                )
            if pinned.page_id and pinned.page_id in slot.page_registry:
                page = slot.page_registry.get(pinned.page_id)
                if (
                    not isinstance(page, dict)
                    or str(page.get("fleetId") or "") != pinned.fleet_id
                ):
                    raise FleetRoutingError(
                        "pinned_page_unavailable",
                        (
                            f"pinned page {pinned.page_id!r} was not found in"
                            f" fleet {pinned.fleet_id!r}"
                        ),
                        retryable=False,
                        next_instruction=(
                            "Do not create or navigate a replacement page. Ask"
                            " the user to reopen the pinned page."
                        ),
                        details={"pinnedBrowserContext": pinned.to_dict()},
                    )
            stable_owner_slot_id = (
                self.fleet_coordinator.owner_slot_for_fleet(pinned.fleet_id)
                or slot.slot_id
            )
            return self.fleet_coordinator.bind_assignment(
                worker_id=worker_id,
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                fleet_id=pinned.fleet_id,
                assignment_reason="user_pinned_existing_fleet",
                reuse_scope=reuse_scope,
                page_policy=page_policy,
                allowed_fleet_ids=[pinned.fleet_id],
                created_for_worker=False,
                owner_slot_id=stable_owner_slot_id,
                fleet_group_key=fleet_group_key,
                delegated=stable_owner_slot_id != slot.slot_id,
            )
        if fleet_id:
            # The public wrapper always passes the receipt argument explicitly.
            # ``None`` remains a deliberate legacy/test value, but a future
            # direct caller of this locked implementation cannot silently omit
            # the evidence that makes the inventory guards fail closed.
            if (
                inventory_sync is not None
                and not bool(inventory_sync.get("fleetListSucceeded"))
            ):
                raise FleetRoutingError(
                    "fleet_inventory_temporarily_unavailable",
                    (
                        "the authoritative Fleet.list inventory was not"
                        " available while resolving the explicit fleet"
                        " reference"
                    ),
                    retryable=True,
                    next_instruction=(
                        "Retry after Fleet.list succeeds. Keep the same"
                        " fleet/session reference and do not create a"
                        " replacement Fleet."
                    ),
                    details={
                        "fleetReference": fleet_id,
                        "registrySync": dict(inventory_sync or {}),
                    },
                )
            resolved_fleet_id = resolve_fleet_reference(
                fleet_id,
                slot.fleet_ids,
            )
            source_worker = str(reuse_from_worker_id or "").strip()
            if source_worker:
                source_assignment = self.fleet_coordinator.assignment_for_worker(
                    source_worker
                )
                if (
                    source_assignment is None
                    or source_assignment.fleet_id != resolved_fleet_id
                ):
                    raise FleetRoutingError(
                        "fleet_routing_conflict",
                        (
                            "fleet_id and reuse_from_worker_id resolve to"
                            " different Fleets"
                        ),
                        details={
                            "fleetReference": fleet_id,
                            "resolvedFleetId": resolved_fleet_id,
                            "reuseFromWorkerId": source_worker,
                            "reuseFleetId": (
                                source_assignment.fleet_id
                                if source_assignment is not None else None
                            ),
                        },
                    )
            stable_owner_slot_id = (
                self.fleet_coordinator.owner_slot_for_fleet(resolved_fleet_id)
                or slot.slot_id
            )
            return self.fleet_coordinator.bind_assignment(
                worker_id=worker_id,
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                fleet_id=resolved_fleet_id,
                assignment_reason="explicit_fleet_reference",
                reuse_scope=reuse_scope,
                page_policy=page_policy,
                # Forward the key, or a named fleet cannot be re-entered by
                # reference. bind_assignment refuses a fleet whose record holds
                # a session_key the request does not name, so omitting it here
                # made every continuation of a named session fail as
                # `fleet_session_conflict` -- the last leg of the 69cab1c4 ring,
                # and the one no routing-level test could see because it lives
                # past slot registration. A caller may only reach this path
                # with both values when a task session binding supplied them,
                # since fleet_id and session_key are otherwise exclusive.
                session_key=session_key,
                allowed_fleet_ids=[resolved_fleet_id],
                created_for_worker=False,
                owner_slot_id=stable_owner_slot_id,
                fleet_group_key=fleet_group_key,
                delegated=stable_owner_slot_id != slot.slot_id,
            )
        assignment: Optional[FleetAssignment] = None
        if resume_browser_hint is not None:
            hint_fleet_id = resume_browser_hint.fleet_id
            inventory_failed = bool(
                inventory_sync is not None
                and not inventory_sync.get("fleetListSucceeded")
            )
            if hint_fleet_id in slot.fleet_ids and not inventory_failed:
                hinted_page = slot.page_registry.get(
                    resume_browser_hint.page_id
                )
                page_is_live = bool(
                    resume_browser_hint.page_id
                    and isinstance(hinted_page, dict)
                    and str(hinted_page.get("fleetId") or "")
                    == hint_fleet_id
                )
                hint_scope = (
                    "page"
                    if page_is_live and resume_hint_may_select_page
                    else reuse_scope
                )
                hint_policy = (
                    "existing"
                    if page_is_live and resume_hint_may_select_page
                    else page_policy
                )
                hint_owner_slot_id = (
                    self.fleet_coordinator.owner_slot_for_fleet(
                        hint_fleet_id,
                        admitted_only=False,
                    )
                    or slot.slot_id
                )
                try:
                    assignment = self.fleet_coordinator.bind_assignment(
                        worker_id=worker_id,
                        slot_id=slot.slot_id,
                        owner_agent_id=slot.agent_id,
                        fleet_id=hint_fleet_id,
                        assignment_reason="resume_browser_hint",
                        reuse_scope=hint_scope,
                        page_policy=hint_policy,
                        allowed_fleet_ids=[hint_fleet_id],
                        created_for_worker=False,
                        owner_slot_id=hint_owner_slot_id,
                        fleet_group_key=fleet_group_key,
                        delegated=hint_owner_slot_id != slot.slot_id,
                    )
                except FleetRoutingError as exc:
                    self.logger.write(
                        "spawner.resume_browser_hint.ignored",
                        {
                            "reason": exc.code,
                            "resumeBrowserHint": resume_browser_hint.to_dict(),
                        },
                    )
                    assignment = None
                else:
                    self.logger.write(
                        "spawner.resume_browser_hint.used",
                        {
                            "workerId": worker_id,
                            "slotId": slot.slot_id,
                            "fleetId": hint_fleet_id,
                            "pageId": (
                                resume_browser_hint.page_id
                                if page_is_live
                                and resume_hint_may_select_page
                                else None
                            ),
                            "pageRecovered": bool(
                                page_is_live and resume_hint_may_select_page
                            ),
                        },
                    )
            else:
                self.logger.write(
                    "spawner.resume_browser_hint.ignored",
                    {
                        "reason": (
                            "fleet_inventory_unavailable"
                            if inventory_failed
                            else "fleet_not_found"
                        ),
                        "resumeBrowserHint": resume_browser_hint.to_dict(),
                    },
                )
        if (
            assignment is None
            and automatic_task_reuse_allowed
            and getattr(
                self.runtime.harness,
                "similar_task_fleet_reuse_enabled",
                True,
            )
            and str(root_task or "").strip()
        ):
            match = find_similar_task_fleet(
                slot.registration,
                str(root_task).strip(),
                candidate_fleet_ids=slot.fleet_ids,
                current_task_id=Path(str(
                    getattr(self.logger, "task_dir", "") or ""
                )).name,
                threshold=getattr(
                    self.runtime.harness,
                    "similar_task_reuse_threshold",
                    0.78,
                ),
                running_stale_seconds=getattr(
                    self.runtime.harness,
                    "similar_task_running_stale_seconds",
                    86400.0,
                ),
            )
            if match is None:
                self.logger.write("spawner.similar_task_reuse.miss", {
                    "workerId": worker_id,
                    "slotId": slot.slot_id,
                    "threshold": float(getattr(
                        self.runtime.harness,
                        "similar_task_reuse_threshold",
                        0.78,
                    )),
                })
            else:
                owner_slot_id = (
                    self.fleet_coordinator.owner_slot_for_fleet(
                        match.fleet_id,
                        admitted_only=False,
                    )
                    or slot.slot_id
                )
                provisional = FleetAssignment(
                    worker_id=worker_id,
                    slot_id=slot.slot_id,
                    owner_agent_id=slot.agent_id,
                    fleet_id=match.fleet_id,
                    assignment_reason="similar_task_fleet_reuse",
                    reuse_scope="fleet",
                    page_policy="new",
                    allowed_fleet_ids=(match.fleet_id,),
                    created_for_worker=False,
                    is_isolated=needs_isolated_session,
                    owner_slot_id=owner_slot_id,
                    fleet_group_key=fleet_group_key,
                    delegated=owner_slot_id != slot.slot_id,
                )
                try:
                    readiness = await self._ensure_assigned_fleet_ready(
                        slot,
                        provisional,
                        worker_id=worker_id,
                        force=True,
                    )
                    assignment = self.fleet_coordinator.admit_similar_task_fleet(
                        worker_id=worker_id,
                        slot_id=slot.slot_id,
                        owner_agent_id=slot.agent_id,
                        fleet_id=match.fleet_id,
                        observed_fleet_ids=slot.fleet_ids,
                        is_isolated=needs_isolated_session,
                        owner_slot_id=owner_slot_id,
                        fleet_group_key=fleet_group_key,
                        delegated=owner_slot_id != slot.slot_id,
                    )
                except FleetReadinessError as exc:
                    # The candidate was never committed, so ordinary routing
                    # can safely create/reuse without coordinator rollback.
                    self.logger.write(
                        "spawner.similar_task_reuse.readiness_fallback",
                        {
                            **match.to_dict(),
                            "workerId": worker_id,
                            "slotId": slot.slot_id,
                            "error": str(exc)[:500],
                        },
                    )
                    assignment = None
                except FleetRoutingError as exc:
                    self.logger.write(
                        "spawner.similar_task_reuse.rejected",
                        {
                            **match.to_dict(),
                            "workerId": worker_id,
                            "slotId": slot.slot_id,
                            "reason": exc.code,
                        },
                    )
                    assignment = None
                else:
                    self._preverified_fleet_readiness[worker_id] = readiness
                    self.logger.write("spawner.similar_task_reuse.matched", {
                        **match.to_dict(),
                        "workerId": worker_id,
                        "slotId": slot.slot_id,
                    })
        if assignment is None:
            assignment = self.fleet_coordinator.choose_existing(
                worker_id=worker_id,
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                candidate_fleet_ids=slot.fleet_ids,
                reuse_scope=reuse_scope,
                page_policy=page_policy,
                session_key=session_key,
                reuse_from_worker_id=reuse_from_worker_id,
                needs_isolated_session=needs_isolated_session,
                fleet_group_key=fleet_group_key,
                allow_cross_slot_delegate=bool(
                    getattr(
                        self.runtime.harness,
                        "same_fleet_multiworker_enabled",
                        False,
                    )
                ),
            )
        if assignment is None:
            assignment = await self._assign_within_task_fleet_cap(
                slot,
                worker_id=worker_id,
                reuse_scope=reuse_scope,
                page_policy=page_policy,
                session_key=session_key,
                needs_isolated_session=needs_isolated_session,
                isolation_auto_applied=isolation_auto_applied,
                fleet_group_key=fleet_group_key,
            )

        if assignment is None:
            before = set(slot.fleet_ids)
            response = await slot.client.call("Fleet.create", {})
            self._update_slot_registry_from_value(slot, response)
            created_ids = sorted(slot.fleet_ids.difference(before))
            if not created_ids:
                # A successful response is expected to carry fleetId.  Refresh
                # once from the authoritative owner view before failing closed.
                await self._sync_slot_registry(
                    slot,
                    worker_id=worker_id,
                    include_page_details=False,
                )
                created_ids = sorted(slot.fleet_ids.difference(before))
            if not created_ids:
                raise ABCPTransportError(
                    "Fleet.create succeeded without a discoverable fleetId; "
                    "refusing fleetless Page.create fallback"
                )
            fleet_id = created_ids[-1]
            assignment = self.fleet_coordinator.bind_assignment(
                worker_id=worker_id,
                slot_id=slot.slot_id,
                owner_agent_id=slot.agent_id,
                fleet_id=fleet_id,
                assignment_reason=(
                    "isolated_session"
                    if needs_isolated_session
                    else "session_bootstrap"
                    if session_key
                    else "slot_bootstrap"
                ),
                reuse_scope=reuse_scope,
                page_policy=page_policy,
                session_key=session_key,
                allowed_fleet_ids=[fleet_id],
                created_for_worker=True,
                is_isolated=needs_isolated_session,
                owner_slot_id=slot.slot_id,
                fleet_group_key=fleet_group_key,
                delegated=False,
            )

        if assignment.delegated:
            owner_slot = self._slots.get(assignment.owner_slot_id)
            if (
                owner_slot is None
                or owner_slot.client is None
                or owner_slot.status in {"broken", "closed"}
            ):
                raise FleetRoutingError(
                    "fleet_owner_unavailable",
                    "the owner connection for the delegated fleet is unavailable",
                    retryable=True,
                    next_instruction=(
                        "Wait for the fleet owner slot to reconnect; do not create"
                        " another fleet for this task/session."
                    ),
                    details={
                        "assignedFleetId": assignment.fleet_id,
                        "ownerSlotId": assignment.owner_slot_id,
                    },
                )

        self.logger.write("spawner.fleet.assigned", assignment.to_dict())
        return assignment

    @staticmethod
    def _fleet_readiness_notification(message: Any, fleet_id: str) -> bool:
        event = unwrap_notification(message)
        if event is None or str(event.get("event") or "") not in {
            "Fleet.ready",
            "Fleet.stopped",
        }:
            return False
        payload = event.get("payload")
        return bool(
            isinstance(payload, dict)
            and str(payload.get("fleetId") or "").strip() == fleet_id
        )

    async def _probe_fleet_readiness(
        self,
        owner_slot: BrowserAgentSlot,
        *,
        fleet_id: str,
        worker_id: str,
    ) -> JsonDict:
        if owner_slot.client is None:
            raise FleetReadinessError(
                "Fleet readiness owner connection is unavailable",
                fleet_id=fleet_id,
                owner_slot_id=owner_slot.slot_id,
            )
        client = owner_slot.client
        timeout = max(
            0.01,
            float(getattr(
                self.runtime.harness,
                "fleet_readiness_wait_seconds",
                45.0,
            )),
        )
        started = time.monotonic()
        deadline = started + timeout
        event_waiter: Optional["asyncio.Task[Optional[JsonDict]]"] = None
        wait_for_notification = getattr(client, "wait_for_notification", None)
        if callable(wait_for_notification):
            predicate = (
                lambda message: self._fleet_readiness_notification(
                    message, fleet_id
                )
            )

            async def wait_for_readiness_event() -> Optional[JsonDict]:
                try:
                    return await wait_for_notification(
                        predicate,
                        timeout=timeout,
                        replay_window_seconds=5.0,
                    )
                except TypeError:
                    # Compatibility with minimal ABCP test doubles and older
                    # clients lacking replay-window keyword support.
                    return await wait_for_notification(predicate, timeout)

            event_waiter = asyncio.create_task(wait_for_readiness_event())
        self.logger.write("spawner.fleet.readiness_started", {
            "fleetId": fleet_id,
            "ownerSlotId": owner_slot.slot_id,
            "workerId": worker_id,
            "timeoutSeconds": timeout,
        })
        page_list_error = ""

        async def verify_with_page_list() -> Optional[JsonDict]:
            """Use one target-scoped, read-only browser RPC as live proof.

            Fleet.list is persisted inventory and can include a prepared Fleet
            with idle-looking page records, so its success is not a readiness
            certificate. Page.list must reach the selected Fleet's live page
            owner. A successful empty result is also valid: a ready Fleet need
            not have an existing page.
            """

            nonlocal page_list_error
            try:
                response = await client.call(
                    "Page.list", {"fleetId": fleet_id}
                )
                page_items = self._extract_page_items(response)
                if page_items is None:
                    page_list_error = (
                        "Page.list returned no authoritative page collection"
                    )
                    return None
                foreign_fleet_ids = sorted({
                    str(item.get("fleetId") or item.get("fleet_id") or "").strip()
                    for item in page_items
                    if (
                        isinstance(item, dict)
                        and str(
                            item.get("fleetId") or item.get("fleet_id") or ""
                        ).strip()
                        not in {"", fleet_id}
                    )
                })
                if foreign_fleet_ids:
                    page_list_error = (
                        "Page.list returned pages for a different Fleet: "
                        + ", ".join(foreign_fleet_ids[:3])
                    )
                    return None
                self._replace_fleet_pages_from_list(
                    owner_slot,
                    fleet_id=fleet_id,
                    pages_response=response,
                )
                self._update_slot_registry_from_value(owner_slot, response)
                page_list_error = ""
                return response
            except ABCPTransportError as exc:
                # Page.list is a real socket RPC now. A dead reader or send
                # path proves the owner slot is unusable; preserve the typed
                # transport failure so spawn_browser_agent marks it broken
                # instead of spending the Fleet-readiness retry budget.
                if bool(getattr(exc, "connection_fatal", False)):
                    raise
                page_list_error = str(exc)[:500]
                return None
            except Exception as exc:
                page_list_error = str(exc)[:500]
                return None

        def ready_receipt(verified_by: str) -> JsonDict:
            receipt = {
                "fleetId": fleet_id,
                "ownerSlotId": owner_slot.slot_id,
                "status": "ready",
                "verifiedBy": verified_by,
                "elapsedMs": int((time.monotonic() - started) * 1000),
            }
            self.logger.write("spawner.fleet.readiness_ready", receipt)
            return receipt

        try:
            if await verify_with_page_list() is not None:
                return ready_receipt("page_list")

            event = None
            event_name = ""
            if event_waiter is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining > 0:
                    # Fleet.ready is a wake-up hint, while Fleet.stopped is a
                    # terminal failure. Keep the wait short because session
                    # restore may complete without either event; a ready hint
                    # is always followed by one terminal live probe.
                    event_wait_seconds = min(5.0, remaining / 2.0)
                    try:
                        event = await asyncio.wait_for(
                            asyncio.shield(event_waiter),
                            timeout=event_wait_seconds,
                        )
                    except asyncio.TimeoutError:
                        event = None

            if event is not None:
                notification = unwrap_notification(event)
                if notification is not None:
                    event_name = str(notification.get("event") or "")
            if event_name == "Fleet.stopped":
                failure = {
                    "fleetId": fleet_id,
                    "ownerSlotId": owner_slot.slot_id,
                    "workerId": worker_id,
                    "elapsedMs": int((time.monotonic() - started) * 1000),
                    "error": "Fleet.stopped was observed during readiness wait",
                }
                self.logger.write("spawner.fleet.readiness_failed", failure)
                raise FleetReadinessError(
                    (
                        f"Fleet {fleet_id} stopped before worker startup; "
                        "no readiness retry was attempted"
                    ),
                    fleet_id=fleet_id,
                    owner_slot_id=owner_slot.slot_id,
                )

            # Restore completion can be eventless. Retry exactly once even if
            # no Fleet.ready notification arrived. Do not cancel an in-flight
            # ABCP RPC merely to enforce the soft readiness budget; ABCPClient
            # retains its own bounded call timeout and serializes the socket.
            if await verify_with_page_list() is not None:
                return ready_receipt(
                    "event_then_page_list"
                    if event is not None else "page_list_retry"
                )

            detail = (
                "target Page.list did not verify Fleet readiness"
                + (f": {page_list_error}" if page_list_error else "")
                + (
                    "; no Fleet.ready or Fleet.stopped event was observed"
                    if event is None else
                    "; Fleet.ready was observed but live verification still failed"
                )
                + "; Fleet.status cannot substitute because it only reports"
                " Fleet directory and Client-process state, not whether the"
                " assigned page is usable"
            )
            failure = {
                "fleetId": fleet_id,
                "ownerSlotId": owner_slot.slot_id,
                "workerId": worker_id,
                "elapsedMs": int((time.monotonic() - started) * 1000),
                "error": detail,
            }
            self.logger.write("spawner.fleet.readiness_failed", failure)
            raise FleetReadinessError(
                (
                    f"Fleet {fleet_id} did not become ready before worker startup:"
                    f" {detail}"
                ),
                fleet_id=fleet_id,
                owner_slot_id=owner_slot.slot_id,
            )
        finally:
            if event_waiter is not None and not event_waiter.done():
                event_waiter.cancel()
                try:
                    await event_waiter
                except (asyncio.CancelledError, Exception):
                    pass

    async def _ensure_assigned_fleet_ready(
        self,
        acting_slot: BrowserAgentSlot,
        assignment: Optional[FleetAssignment],
        *,
        worker_id: str,
        force: bool = False,
    ) -> JsonDict:
        barrier_enabled = bool(getattr(
            self.runtime.harness,
            "fleet_readiness_barrier_enabled",
            True,
        ))
        if assignment is None or (not force and not barrier_enabled):
            return {
                "status": "not_applicable" if assignment is None else "disabled"
            }
        owner_slot_id = assignment.owner_slot_id or acting_slot.slot_id
        owner_slot = self._slots.get(owner_slot_id)
        if owner_slot is None and owner_slot_id == acting_slot.slot_id:
            owner_slot = acting_slot
        if owner_slot is None or owner_slot.client is None:
            raise FleetReadinessError(
                "Fleet readiness owner slot is unavailable",
                fleet_id=assignment.fleet_id,
                owner_slot_id=owner_slot_id,
            )
        key = (owner_slot_id, assignment.fleet_id)
        task = self._fleet_readiness_tasks.get(key)
        shared = task is not None and not task.done()
        if not shared:
            task = asyncio.create_task(self._probe_fleet_readiness(
                owner_slot,
                fleet_id=assignment.fleet_id,
                worker_id=worker_id,
            ))
            self._fleet_readiness_tasks[key] = task

            def discard(completed: "asyncio.Task[JsonDict]") -> None:
                if self._fleet_readiness_tasks.get(key) is completed:
                    self._fleet_readiness_tasks.pop(key, None)

            task.add_done_callback(discard)
        receipt = dict(await asyncio.shield(task))
        receipt["sharedProbe"] = shared
        return receipt

    async def _sync_assigned_fleet_pages(
        self,
        acting_slot: BrowserAgentSlot,
        assignment: FleetAssignment,
        *,
        worker_id: str,
    ) -> JsonDict:
        """Inspect pages only after the selected Fleet is ready.

        Inventory discovery before assignment remains Fleet.list-only. This
        targeted pass prevents an unrelated restoring Fleet from generating a
        storm of Page.list/Page.getState calls during another worker's spawn.
        """

        owner_slot_id = assignment.owner_slot_id or acting_slot.slot_id
        owner_slot = self._slots.get(owner_slot_id)
        if owner_slot is None and owner_slot_id == acting_slot.slot_id:
            owner_slot = acting_slot
        if owner_slot is None:
            raise FleetReadinessError(
                "Fleet page inventory owner slot is unavailable",
                fleet_id=assignment.fleet_id,
                owner_slot_id=owner_slot_id,
            )
        sync_receipt = await self._sync_slot_registry(
            owner_slot,
            worker_id=worker_id,
            required_fleet_id=assignment.fleet_id,
            include_page_details=True,
        )
        if owner_slot is not acting_slot:
            for page_id, page in owner_slot.page_registry.items():
                if str(page.get("fleetId") or "") == assignment.fleet_id:
                    acting_slot.page_registry[page_id] = dict(page)
        pinned = self.pinned_browser_context
        if pinned is not None and pinned.page_id:
            page = acting_slot.page_registry.get(pinned.page_id)
            if (
                not isinstance(page, dict)
                or str(page.get("fleetId") or "") != pinned.fleet_id
            ):
                page_list_succeeded = pinned.fleet_id in set(
                    sync_receipt.get("pageListSucceededFleetIds") or []
                )
                raise FleetRoutingError(
                    "pinned_page_unavailable",
                    (
                        f"pinned page {pinned.page_id!r} was not found in"
                        f" fleet {pinned.fleet_id!r} after readiness"
                    ),
                    retryable=not page_list_succeeded,
                    next_instruction=(
                        (
                            "The bound Fleet's Page.list did not complete, so"
                            " this is not proof that the pinned page closed."
                            " Retry after transport recovery without changing"
                            " the pinned Fleet/Page."
                        )
                        if not page_list_succeeded else
                        "Do not create or navigate a replacement page. Ask the"
                        " user to reopen the pinned page."
                    ),
                    details={
                        "pinnedBrowserContext": pinned.to_dict(),
                        "inventoryAuthoritative": page_list_succeeded,
                        "registrySync": sync_receipt,
                    },
                )
        return sync_receipt

    def _ensure_notification_relay(
        self,
        acting_slot: BrowserAgentSlot,
        assignment: Optional[FleetAssignment],
    ) -> None:
        """Relay owner-socket notifications to a delegated acting socket.

        Legacy/resource-associated events may still reach only the registered
        owner, while newer pending-target and Fleet-fallback paths can also
        deliver directly to the acting Agent. The relay therefore filters by
        Fleet and shares stable-event deduplication with direct delivery.
        """

        if assignment is None or not assignment.delegated:
            return
        owner_slot = self._slots.get(assignment.owner_slot_id)
        if (
            owner_slot is None
            or owner_slot.client is None
            or acting_slot.client is None
        ):
            return
        key = (owner_slot.slot_id, acting_slot.slot_id, assignment.fleet_id)
        if key in self._notification_relays:
            return
        target_hub = getattr(acting_slot.client, "notifications", None)
        publish = getattr(target_hub, "publish", None)
        publish_once = getattr(target_hub, "publish_once", None)
        subscribe = getattr(owner_slot.client, "subscribe_notifications", None)
        if not callable(publish) or not callable(subscribe):
            return

        def relay(message: JsonDict) -> None:
            fleet_id = self._notification_fleet_id(message)
            if fleet_id != assignment.fleet_id:
                return
            relayed_message = dict(message)
            relayed_message["deliveryProvenance"] = {
                "kind": "owner_relay",
                "ownerRegisteredAgentId": owner_slot.agent_id,
                "actingRegisteredAgentId": acting_slot.agent_id,
                "fleetId": assignment.fleet_id,
                "authoritativeForCausality": False,
            }
            if callable(publish_once):
                publish_once(relayed_message)
            else:
                publish(relayed_message)

        self._notification_relays[key] = subscribe(relay)
        self.logger.write(
            "spawner.fleet.notification_relay_attached",
            {
                "fleetId": assignment.fleet_id,
                "ownerSlotId": owner_slot.slot_id,
                "actingSlotId": acting_slot.slot_id,
            },
        )

    @staticmethod
    def _notification_fleet_id(message: JsonDict) -> str:
        candidates: List[JsonDict] = []
        if isinstance(message, dict):
            candidates.append(message)
            params = message.get("params")
            if isinstance(params, dict):
                candidates.append(params)
                data = params.get("data")
                if isinstance(data, dict):
                    candidates.append(data)
            data = message.get("data")
            if isinstance(data, dict):
                candidates.append(data)
        for candidate in candidates:
            fleet_id = str(
                candidate.get("fleetId") or candidate.get("fleet_id") or ""
            ).strip()
            if fleet_id:
                return fleet_id
        return ""

    def _record_verified_auth_session(
        self,
        assignment: FleetAssignment,
        payload: JsonDict,
    ) -> JsonDict:
        evidence = {
            **dict(payload or {}),
            "fleetId": assignment.fleet_id,
            "sessionKey": assignment.session_key,
            "sessionGeneration": assignment.session_generation,
            "ownerAgentId": assignment.owner_agent_id,
        }
        receipt = self.auth_fleet_ledger.record_verified(evidence)
        if receipt.get("recorded"):
            self.fleet_coordinator.restore_auth_binding(
                fleet_id=assignment.fleet_id,
                slot_id=assignment.owner_slot_id or assignment.slot_id,
                owner_agent_id=assignment.owner_agent_id,
                session_key=assignment.session_key,
                session_generation=assignment.session_generation,
                is_isolated=True,
                status="active",
            )
        self.logger.write(
            "auth_fleet.verified_record",
            {
                "workerId": assignment.worker_id,
                "sessionKey": assignment.session_key,
                "fleetId": assignment.fleet_id,
                **receipt,
            },
        )
        return receipt

    def _handle_auth_session_lost(self, payload: JsonDict) -> None:
        session_key = str(payload.get("sessionKey") or "").strip()
        fleet_id = str(payload.get("fleetId") or "").strip()
        generation = int(payload.get("sessionGeneration") or 0)
        reason = str(payload.get("reason") or "authoritative fleet loss")
        if not session_key or not fleet_id or generation <= 0:
            return
        stale = self.auth_fleet_ledger.mark_stale(
            session_key,
            fleet_id=fleet_id,
            expected_generation=generation,
            reason=reason,
        )
        released = self.fleet_coordinator.release_session_binding(
            session_key=session_key,
            expected_fleet_id=fleet_id,
            expected_generation=generation,
            reason="authoritative fleet loss requires fresh authentication",
        )
        self.logger.write(
            "auth_fleet.session_released",
            {**released, "ledger": stale},
        )

    async def reset_auth_session(
        self,
        *,
        session_key: str,
        expected_fleet_id: str,
        expected_generation: int,
        reason: str,
    ) -> JsonDict:
        """Host/operator-only CAS reset for an unrecoverable named session.

        This is intentionally not registered as a LeadAgent or BrowserAgent
        tool. Transport failure alone never calls it automatically: an operator
        must explicitly accept losing the old cookie jar and provide the fleet
        and generation shown in ``session_manual_reset_required``.
        """

        key = str(session_key or "").strip()
        fleet_id = str(expected_fleet_id or "").strip()
        why = str(reason or "").strip()
        details = self.fleet_coordinator.session_binding_details(key)
        if not key or not fleet_id or int(expected_generation or 0) <= 0 or not why:
            raise ValueError(
                "reset_auth_session requires session_key, expected_fleet_id, "
                "expected_generation, and reason"
            )
        if not details:
            raise FleetRoutingError(
                "session_binding_conflict",
                f"session_key {key!r} has no active binding",
            )
        if (
            str(details.get("fleetId") or "") != fleet_id
            or int(details.get("generation") or 0) != int(expected_generation)
        ):
            raise FleetRoutingError(
                "session_binding_conflict",
                "session binding changed before operator reset",
                details={
                    "sessionKey": key,
                    "boundFleetId": details.get("fleetId"),
                    "expectedFleetId": fleet_id,
                    "sessionGeneration": details.get("generation"),
                    "expectedGeneration": int(expected_generation),
                },
            )
        slot = self._slots.get(str(details.get("slotId") or ""))
        active_workers = [
            handle.worker_id
            for handle in self._handles.values()
            if not handle.async_task.done()
            and (
                (assignment := self.fleet_coordinator.assignment_for_worker(
                    handle.worker_id
                ))
                is not None
                and assignment.fleet_id == fleet_id
            )
        ]
        if (slot is not None and slot.current_worker_id) or active_workers:
            raise FleetRoutingError(
                "session_reset_busy",
                "cannot reset a named session while a worker is using its fleet",
                retryable=True,
                details={
                    "sessionKey": key,
                    "slotId": slot.slot_id if slot is not None else "",
                    "workerIds": active_workers or [slot.current_worker_id],
                },
            )

        stale = self.auth_fleet_ledger.mark_stale(
            key,
            fleet_id=fleet_id,
            expected_generation=int(expected_generation),
            reason=f"operator reset: {why}",
        )
        if not stale.get("updated") and stale.get("reason") not in {"not_found"}:
            raise FleetRoutingError(
                "session_binding_conflict",
                "persistent auth ledger changed before operator reset",
                details={"sessionKey": key, "ledger": stale},
            )
        released = self.fleet_coordinator.release_session_binding(
            session_key=key,
            expected_fleet_id=fleet_id,
            expected_generation=int(expected_generation),
            reason=f"operator reset: {why}",
        )
        barrier = await self.fleet_auth_barrier.discard_fleet(
            fleet_id,
            force=True,
        )
        self._cleanup_retired_slots()
        receipt = {**released, "ledger": stale, "authBarrier": barrier}
        self.logger.write("auth_fleet.operator_reset", receipt)
        return receipt
