"""Runtime fleet assignment and lease metadata for BrowserAgent workers.

The Dispatcher remains authoritative for fleet inventory, ownership, process
lifecycle, and retention.  This coordinator deliberately keeps only the
orchestration state the harness needs to make deterministic routing decisions:
which fleet a worker was assigned, why it was assigned, which fleet ids that
worker may address, and which session keys are bound during this process.

It does not close fleets and it never treats its in-memory records as proof that
a Dispatcher fleet is still alive.  The spawner refreshes the records from
System.register/Fleet.list before assigning each worker.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Set


VALID_REUSE_SCOPES = frozenset({"connection", "fleet", "page"})
VALID_PAGE_POLICIES = frozenset({"new", "existing"})

ROUTING_ERROR_GUIDANCE = {
    "fleet_reference_invalid": (
        "Use an existing Fleet UUID or a hexadecimal UUID prefix of at least 8 characters."
    ),
    "fleet_reference_not_found": (
        "Refresh the authoritative Fleet inventory and choose an existing Fleet; do not create a replacement."
    ),
    "ambiguous_fleet_reference": (
        "Use a longer Fleet UUID prefix that identifies exactly one existing Fleet."
    ),
    "fleet_routing_conflict": (
        "Remove the conflicting selector and retry with one session/worker/slot route."
    ),
    "reuse_fleet_lost": (
        "Do not reuse that worker handle; request a fresh coordinator assignment."
    ),
    "reuse_session_conflict": (
        "Use the session_key already bound to that worker, or start a fresh named session."
    ),
    "session_isolation_conflict": (
        "Start a fresh session/fleet whose isolation contract matches the request."
    ),
    "session_binding_conflict": (
        "Keep the existing named-session binding unless trusted recovery explicitly releases it."
    ),
    "fleet_session_conflict": (
        "Do not attach this fleet to another session; request a fresh coordinator fleet."
    ),
    "released_fleet_conflict": (
        "Do not revive a released session fleet; create a fresh fleet through the coordinator."
    ),
    "task_fleet_limit_reached": (
        "Continue on a fleet the task already has (drop needs_isolated_session,"
        " or pass the exact session_key bound to it) or raise"
        " harness.max_task_fleets; waiting does not free a fleet, and a fresh"
        " one is never the answer."
    ),
}


_FLEET_UUID_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{8}[0-9a-fA-F-]*$")


def resolve_fleet_reference(
    fleet_reference: object,
    candidate_fleet_ids: Iterable[str],
) -> str:
    """Resolve one existing Fleet UUID from an exact id or unique prefix.

    The candidate set must come from the current authoritative Dispatcher
    inventory.  A model-provided value is only a selector: it never proves
    that the Fleet exists and it never authorizes a replacement Fleet.
    """

    reference = str(fleet_reference or "").strip()
    candidates = sorted({
        str(item or "").strip()
        for item in candidate_fleet_ids
        if str(item or "").strip()
    })
    if (
        len(reference) < 8
        or not _FLEET_UUID_PREFIX_RE.fullmatch(reference)
    ):
        raise FleetRoutingError(
            "fleet_reference_invalid",
            (
                "fleet_id must be an existing Fleet UUID or a hexadecimal"
                " UUID prefix of at least 8 characters"
            ),
            details={"fleetReference": reference},
        )

    lowered = reference.lower()
    exact = [item for item in candidates if item.lower() == lowered]
    matches = exact or [
        item for item in candidates if item.lower().startswith(lowered)
    ]
    if not matches:
        raise FleetRoutingError(
            "fleet_reference_not_found",
            f"fleet_id reference {reference!r} matched no active Fleet",
            details={"fleetReference": reference},
        )
    if len(matches) > 1:
        raise FleetRoutingError(
            "ambiguous_fleet_reference",
            (
                f"fleet_id reference {reference!r} matched multiple active"
                " Fleets"
            ),
            details={
                "fleetReference": reference,
                "matchingFleetIds": matches,
            },
        )
    return matches[0]


class FleetRoutingError(RuntimeError):
    """Structured, fail-closed fleet/session routing failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        next_instruction: str = "",
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)
        self.next_instruction = str(
            next_instruction or ROUTING_ERROR_GUIDANCE.get(self.code, "")
        )
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        result = {
            "status": self.code,
            "error": str(self),
            "retryable": self.retryable,
            "tool_was_executed": False,
            "reasonKind": self.code,
            **self.details,
        }
        if self.next_instruction:
            result["next_instruction"] = self.next_instruction
        return result


def handle_records_from_value(value: object) -> List[dict]:
    """Walk response dictionaries while excluding echoed method schemas."""

    records: List[dict] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            records.append(item)
            for key, nested in item.items():
                if key == "methodSchema":
                    continue
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return records


def fleet_ids_from_value(value: object) -> Set[str]:
    """Extract real fleetId string scalars from an ABCP response."""

    return {
        str(fleet_id).strip()
        for item in handle_records_from_value(value)
        if isinstance((fleet_id := item.get("fleetId") or item.get("fleet_id")), str)
        and str(fleet_id).strip()
    }


@dataclass
class FleetRecord:
    fleet_id: str
    slot_id: str
    owner_agent_id: str
    last_seen_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    origins: Set[str] = field(default_factory=set)
    status: str = "active"
    session_key: str = ""
    session_generation: int = 0
    is_isolated: bool = False
    retired_from_session: bool = False
    fleet_group_key: str = ""
    admitted: bool = True


@dataclass(frozen=True)
class FleetAssignment:
    worker_id: str
    slot_id: str
    owner_agent_id: str
    fleet_id: str
    assignment_reason: str
    reuse_scope: str = "connection"
    page_policy: str = "new"
    session_key: str = ""
    session_generation: int = 0
    allowed_fleet_ids: tuple[str, ...] = ()
    created_for_worker: bool = False
    is_isolated: bool = False
    owner_slot_id: str = ""
    fleet_group_key: str = ""
    delegated: bool = False

    def to_dict(self) -> dict:
        return {
            "workerId": self.worker_id,
            "slotId": self.slot_id,
            "ownerAgentId": self.owner_agent_id,
            "assignedFleetId": self.fleet_id,
            "assignmentReason": self.assignment_reason,
            "reuseScope": self.reuse_scope,
            "pagePolicy": self.page_policy,
            "sessionKey": self.session_key,
            "sessionGeneration": self.session_generation,
            "allowedFleetIds": list(self.allowed_fleet_ids),
            "createdForWorker": self.created_for_worker,
            "isIsolated": self.is_isolated,
            "ownerSlotId": self.owner_slot_id or self.slot_id,
            "fleetGroupKey": self.fleet_group_key,
            "delegated": self.delegated,
        }


class FleetCoordinator:
    """Own deterministic harness-side fleet assignment metadata."""

    def __init__(self) -> None:
        self._fleets: Dict[str, FleetRecord] = {}
        self._worker_assignments: Dict[str, FleetAssignment] = {}
        self._slot_defaults: Dict[str, str] = {}
        self._session_bindings: Dict[str, str] = {}
        self._session_generations: Dict[str, int] = {}
        self._fleet_groups: Dict[str, str] = {}

    def observe_slot(
        self,
        *,
        slot_id: str,
        owner_agent_id: str,
        fleet_ids: Iterable[str],
        origins_by_fleet: Optional[Mapping[str, Iterable[str]]] = None,
        admit_unbound: bool = True,
    ) -> None:
        """Refresh runtime records from a Dispatcher-authoritative snapshot."""

        now = time.time()
        observed = {
            str(fleet_id).strip()
            for fleet_id in fleet_ids
            if str(fleet_id).strip()
        }
        for fleet_id in observed:
            record = self._fleets.get(fleet_id)
            if record is None:
                record = FleetRecord(
                    fleet_id=fleet_id,
                    slot_id=slot_id,
                    owner_agent_id=owner_agent_id,
                    admitted=bool(admit_unbound),
                )
                self._fleets[fleet_id] = record
            # Inventory can be Agent-global: several live slot connections may
            # observe the same fleet. Once explicitly admitted/bound, a later
            # observer must not steal ownership merely by refreshing Fleet.list.
            if not record.admitted or record.slot_id == str(slot_id):
                record.slot_id = slot_id
                record.owner_agent_id = owner_agent_id
            record.last_seen_at = now
            record.status = (
                "released" if record.retired_from_session else "active"
            )
            record.origins = {
                str(origin).strip()
                for origin in (origins_by_fleet or {}).get(fleet_id, [])
                if str(origin).strip()
            }

        # Keep session-bound records as tombstones so a missing/archived fleet
        # cannot silently rebind to another cookie jar. Unbound records carry no
        # durable affinity value and can be dropped immediately.
        for fleet_id, record in list(self._fleets.items()):
            if record.slot_id != slot_id or fleet_id in observed:
                continue
            record.status = "missing"
            if not record.session_key:
                self._fleets.pop(fleet_id, None)
                for group_key, grouped_fleet in list(self._fleet_groups.items()):
                    if grouped_fleet == fleet_id:
                        self._fleet_groups.pop(group_key, None)

        # Do not retain a slot default that the latest owner snapshot no longer
        # reports.  This is the key stale-ledger guard for archived/GC'd fleets.
        default_fleet = self._slot_defaults.get(slot_id)
        if default_fleet and default_fleet not in observed:
            self._slot_defaults.pop(slot_id, None)

    def reconcile_missing_fleets(self, observed_fleet_ids: Iterable[str]) -> List[str]:
        """Retire records absent from a COMPLETE inventory read, whoever owns them.

        `observe_slot` deliberately retires only the records it owns: one slot's
        snapshot is no evidence about another slot's fleets. ABCP's Fleet.list is
        different — the dispatcher answers it from the whole fleets table
        (`SELECT * FROM fleets`, no agent or connection scoping) and the harness
        stores every returned row — so one successful read is authoritative for
        every fleet, including those another slot created. Pass the result of
        exactly such a read; never a partial or failed one.

        Named sessions stay as `missing` tombstones rather than disappearing, so
        a lost cookie jar keeps failing closed instead of silently rebinding.
        """

        observed = {
            str(fleet_id).strip()
            for fleet_id in observed_fleet_ids
            if str(fleet_id).strip()
        }
        retired: List[str] = []
        for fleet_id, record in list(self._fleets.items()):
            if fleet_id in observed:
                continue
            record.status = "missing"
            if record.session_key:
                continue
            retired.append(fleet_id)
            self._fleets.pop(fleet_id, None)
            self._slot_defaults = {
                slot_id: default_fleet
                for slot_id, default_fleet in self._slot_defaults.items()
                if default_fleet != fleet_id
            }
            for group_key, grouped_fleet in list(self._fleet_groups.items()):
                if grouped_fleet == fleet_id:
                    self._fleet_groups.pop(group_key, None)
        return sorted(retired)

    def owner_slot_for_fleet(
        self,
        fleet_id: str,
        *,
        admitted_only: bool = True,
    ) -> str:
        """Return the stable owner slot for one active fleet, if known."""

        record = self._fleets.get(str(fleet_id or "").strip())
        if record is None or record.status != "active":
            return ""
        if admitted_only and not record.admitted:
            return ""
        return str(record.slot_id or "")

    def restore_auth_binding(
        self,
        *,
        fleet_id: str,
        slot_id: str,
        owner_agent_id: str,
        session_key: str,
        session_generation: int = 1,
        is_isolated: bool = True,
        status: str = "active",
    ) -> None:
        """Restore persisted auth restrictions before generic admission."""

        fleet = str(fleet_id or "").strip()
        key = str(session_key or "").strip()
        if not fleet or not key:
            raise ValueError("auth binding restore requires fleet_id and session_key")
        existing = self._session_bindings.get(key, "")
        if existing and existing != fleet:
            raise FleetRoutingError(
                "session_binding_conflict",
                f"persisted session_key {key!r} maps to multiple fleets",
                details={
                    "sessionKey": key,
                    "boundFleetId": existing,
                    "restoredFleetId": fleet,
                },
            )
        record = self._fleets.get(fleet)
        if record is None:
            record = FleetRecord(
                fleet_id=fleet,
                slot_id=str(slot_id),
                owner_agent_id=str(owner_agent_id),
            )
            self._fleets[fleet] = record
        record.slot_id = str(slot_id)
        record.owner_agent_id = str(owner_agent_id)
        record.session_key = key
        record.session_generation = max(1, int(session_generation or 1))
        record.is_isolated = bool(is_isolated)
        record.admitted = True
        record.status = "active" if status == "active" else "missing"
        self._session_bindings[key] = fleet
        self._session_generations[key] = record.session_generation
        self._fleet_groups[f"session:{key}"] = fleet
        for default_slot, default_fleet in list(self._slot_defaults.items()):
            if default_fleet == fleet:
                self._slot_defaults.pop(default_slot, None)

    def preferred_slot_for_session(self, session_key: str) -> Optional[str]:
        key = str(session_key or "").strip()
        fleet_id = self._session_bindings.get(key)
        record = self._fleets.get(fleet_id or "")
        return record.slot_id if record is not None else None

    def fleet_for_session(self, session_key: str) -> Optional[str]:
        """Return the exact process-local fleet binding for a named session."""

        key = str(session_key or "").strip()
        return self._session_bindings.get(key) or None

    def session_binding_details(self, session_key: str) -> Optional[dict]:
        """Return a read-only routing view used by spawn intent validation."""

        fleet_id = self.fleet_for_session(session_key)
        if not fleet_id:
            return None
        record = self._fleets.get(fleet_id)
        return {
            "fleetId": fleet_id,
            "slotId": record.slot_id if record is not None else "",
            "status": record.status if record is not None else "missing",
            "generation": self._session_generations.get(
                str(session_key or "").strip(),
                record.session_generation if record is not None else 0,
            ),
        }

    def assignment_for_worker(self, worker_id: str) -> Optional[FleetAssignment]:
        return self._worker_assignments.get(str(worker_id or "").strip())

    def validate_routing_intent(
        self,
        *,
        session_key: str = "",
        preferred_slot_id: str = "",
        reuse_from_worker_id: str = "",
        reuse_source_known: bool = False,
        reuse_source_slot_id: str = "",
    ) -> None:
        """Own all session/fleet semantic checks for a spawn request.

        The spawner resolves physical handles and passes only their slot hint;
        it does not duplicate affinity rules or choose precedence silently.
        """

        key = str(session_key or "").strip()
        preferred = str(preferred_slot_id or "").strip()
        source_worker = str(reuse_from_worker_id or "").strip()
        source = self.assignment_for_worker(source_worker) if source_worker else None
        source_slot = str(
            (source.slot_id if source is not None else "")
            or reuse_source_slot_id
            or ""
        ).strip()
        binding = self.session_binding_details(key) if key else None
        binding_active = bool(binding and binding.get("status") == "active")

        if preferred and source_slot and preferred != source_slot:
            raise FleetRoutingError(
                "fleet_routing_conflict",
                "preferred_slot_id and reuse_from_worker_id resolve to different slots",
                details={
                    "preferredSlotId": preferred,
                    "reuseFromWorkerId": source_worker,
                    "reuseSlotId": source_slot,
                },
            )
        if source_worker and reuse_source_known and source is None:
            raise FleetRoutingError(
                "reuse_fleet_lost",
                f"reuse_from_worker_id {source_worker!r} has no fleet assignment",
                details={"reuseFromWorkerId": source_worker},
            )
        if source is not None:
            if key and source.session_key and source.session_key != key:
                raise FleetRoutingError(
                    "reuse_session_conflict",
                    "reuse_from_worker_id belongs to another named session",
                    details={
                        "reuseFromWorkerId": source_worker,
                        "requestedSessionKey": key,
                        "boundSessionKey": source.session_key,
                        "reuseFleetId": source.fleet_id,
                    },
                )
            if not key and source.session_key:
                raise FleetRoutingError(
                    "reuse_session_conflict",
                    "an unnamed continuation cannot attach to a named session fleet",
                    details={
                        "reuseFromWorkerId": source_worker,
                        "boundSessionKey": source.session_key,
                        "reuseFleetId": source.fleet_id,
                    },
                )
        if binding_active and preferred and binding.get("slotId") != preferred:
            raise FleetRoutingError(
                "fleet_routing_conflict",
                "session_key and preferred_slot_id resolve to different slots",
                details={
                    "sessionKey": key,
                    "sessionFleetId": binding.get("fleetId"),
                    "sessionSlotId": binding.get("slotId"),
                    "preferredSlotId": preferred,
                },
            )
        if (
            binding_active
            and source is not None
            and binding.get("fleetId") != source.fleet_id
        ):
            raise FleetRoutingError(
                "fleet_routing_conflict",
                "session_key and reuse_from_worker_id resolve to different fleets",
                details={
                    "sessionKey": key,
                    "sessionFleetId": binding.get("fleetId"),
                    "reuseFromWorkerId": source_worker,
                    "reuseFleetId": source.fleet_id,
                },
            )

    def mark_slot_suspect(self, slot_id: str) -> None:
        """Quarantine slot records while their owner connection is recovering."""

        for record in self._fleets.values():
            if record.slot_id == str(slot_id) and record.status == "active":
                record.status = "suspect"

    def slot_has_session_binding(self, slot_id: str) -> bool:
        return any(
            record.slot_id == str(slot_id) and bool(record.session_key)
            for record in self._fleets.values()
        )

    def release_session_binding(
        self,
        *,
        session_key: str,
        expected_fleet_id: str,
        expected_generation: int,
        reason: str,
    ) -> dict:
        """CAS-release a named session after trusted auth recovery authorizes it.

        Released fleets remain quarantined from generic reuse. The next use of
        the same key receives a new generation and must bootstrap a fresh fleet.
        This method is intentionally not exposed as a model/Lead tool.
        """

        key = str(session_key or "").strip()
        expected = str(expected_fleet_id or "").strip()
        why = str(reason or "").strip()
        if not key or not expected or not why:
            raise ValueError(
                "release_session_binding requires session_key, expected_fleet_id, and reason"
            )
        bound = self._session_bindings.get(key, "")
        generation = self._session_generations.get(key, 1)
        if bound != expected or generation != int(expected_generation):
            raise FleetRoutingError(
                "session_binding_conflict",
                "session binding changed before the authorized release",
                details={
                    "sessionKey": key,
                    "boundFleetId": bound,
                    "expectedFleetId": expected,
                    "sessionGeneration": generation,
                    "expectedGeneration": int(expected_generation),
                },
            )
        self._session_bindings.pop(key, None)
        self._fleet_groups.pop(f"session:{key}", None)
        next_generation = generation + 1
        self._session_generations[key] = next_generation
        record = self._fleets.get(bound)
        if record is not None:
            record.session_key = ""
            record.retired_from_session = True
            record.status = "released"
            for slot_id, default_fleet in list(self._slot_defaults.items()):
                if default_fleet == bound:
                    self._slot_defaults.pop(slot_id, None)
        return {
            "status": "released",
            "sessionKey": key,
            "releasedFleetId": bound,
            "releasedGeneration": generation,
            "nextGeneration": next_generation,
            "reason": why,
        }

    def choose_existing(
        self,
        *,
        worker_id: str,
        slot_id: str,
        owner_agent_id: str,
        candidate_fleet_ids: Iterable[str],
        reuse_scope: str,
        page_policy: str,
        session_key: str = "",
        reuse_from_worker_id: str = "",
        needs_isolated_session: bool = False,
        fleet_group_key: str = "",
        allow_cross_slot_delegate: bool = False,
    ) -> Optional[FleetAssignment]:
        candidates = {
            str(fleet_id).strip()
            for fleet_id in candidate_fleet_ids
            if str(fleet_id).strip()
        }
        selected = ""
        reason = ""
        selected_record: Optional[FleetRecord] = None
        key = str(session_key or "").strip()
        source_worker = str(reuse_from_worker_id or "").strip()
        source = (
            self._worker_assignments.get(source_worker)
            if source_worker
            else None
        )
        group_key = str(fleet_group_key or "").strip()
        self.validate_routing_intent(
            session_key=key,
            reuse_from_worker_id=source_worker,
            reuse_source_known=bool(source_worker),
        )
        if key:
            bound = self._session_bindings.get(key, "")
            if bound:
                record = self._fleets.get(bound)
                if (
                    record is None
                    or record.status != "active"
                    or (
                        bound not in candidates
                        and not allow_cross_slot_delegate
                    )
                ):
                    raise FleetRoutingError(
                        "session_fleet_lost",
                        f"session_key {key!r} is bound to unavailable fleet {bound!r}",
                        retryable=False,
                        next_instruction=(
                            "Do not retry this session binding. Treat its auth"
                            " state as stale and follow the auth-interrupt/login"
                            " recovery flow before continuing account actions."
                        ),
                        details={"sessionKey": key, "lostFleetId": bound},
                    )
                if needs_isolated_session and not record.is_isolated:
                    raise FleetRoutingError(
                        "session_isolation_conflict",
                        f"session_key {key!r} is already bound to a shared fleet",
                        details={"sessionKey": key, "assignedFleetId": bound},
                    )
                selected = bound
                selected_record = record
                reason = (
                    "isolated_session_reuse"
                    if record.is_isolated
                    else "session_key"
                )

        if not selected and source_worker:
            if source is None or (
                source.fleet_id not in candidates
                and not allow_cross_slot_delegate
            ):
                raise FleetRoutingError(
                    "reuse_fleet_lost",
                    f"reuse_from_worker_id {source_worker!r} has no available fleet",
                    details={"reuseFromWorkerId": source_worker},
                )
            record = self._fleets.get(source.fleet_id)
            if record is None or record.status != "active":
                raise FleetRoutingError(
                    "reuse_fleet_lost",
                    f"worker {source_worker!r} fleet is unavailable",
                    details={
                        "reuseFromWorkerId": source_worker,
                        "lostFleetId": source.fleet_id,
                    },
                )
            if needs_isolated_session and not record.is_isolated:
                raise FleetRoutingError(
                    "session_isolation_conflict",
                    "an isolated session cannot inherit a previously shared fleet",
                    details={"reuseFromWorkerId": source_worker},
                )
            selected = source.fleet_id
            selected_record = record
            reason = "reuse_from_worker"

        # A new named session always starts in a fresh fleet unless the caller
        # explicitly hands off an unbound prior worker fleet. An unnamed
        # isolated request likewise always creates a fresh fleet.
        if not selected and (key or needs_isolated_session):
            return None

        # A task/session fleet group is an explicit harness delegation. Unlike
        # Fleet.list discovery it is global within this coordinator process, so
        # an acting slot may address the owner's fleet without claiming
        # ownership or mutating the owner record.
        if not selected and group_key and allow_cross_slot_delegate:
            grouped_fleet = self._fleet_groups.get(group_key, "")
            grouped_record = self._fleets.get(grouped_fleet)
            if (
                grouped_fleet
                and grouped_record is not None
                and grouped_record.status == "active"
                and not grouped_record.session_key
                and not grouped_record.is_isolated
                and grouped_record.admitted
            ):
                selected = grouped_fleet
                selected_record = grouped_record
                reason = (
                    "fleet_group"
                    if grouped_record.slot_id == str(slot_id)
                    else "fleet_group_delegate"
                )

        eligible = {
            fleet_id
            for fleet_id in candidates
            if (
                (record := self._fleets.get(fleet_id)) is not None
                and record.status == "active"
                and not record.session_key
                and not record.is_isolated
                and record.admitted
            )
        }
        if not selected:
            default_fleet = self._slot_defaults.get(slot_id, "")
            if default_fleet in eligible:
                selected = default_fleet
                selected_record = self._fleets.get(selected)
                reason = "slot_default"

        if not selected:
            if not eligible:
                return None
            # Prefer the most recently used/seen known fleet.  Sorting by id is
            # only a deterministic final tie-breaker; no model-supplied id is
            # accepted into this candidate set.
            selected = max(
                eligible,
                key=lambda fleet_id: (
                    self._fleets.get(fleet_id).last_used_at
                    if self._fleets.get(fleet_id) is not None
                    else 0.0,
                    self._fleets.get(fleet_id).last_seen_at
                    if self._fleets.get(fleet_id) is not None
                    else 0.0,
                    fleet_id,
                ),
            )
            selected_record = self._fleets.get(selected)
            reason = "slot_healthy_fleet"

        # Phase 1/2 deliberately binds one worker to one fleet even when prior
        # pages are exposed.  A plural field is retained for the future formal
        # delegation contract, not to grant every fleet observed in the slot.
        allowed = {selected}
        return self.bind_assignment(
            worker_id=worker_id,
            slot_id=slot_id,
            owner_agent_id=(
                selected_record.owner_agent_id
                if selected_record is not None
                else owner_agent_id
            ),
            fleet_id=selected,
            assignment_reason=reason,
            reuse_scope=reuse_scope,
            page_policy=page_policy,
            session_key=key,
            allowed_fleet_ids=allowed,
            created_for_worker=False,
            is_isolated=bool(selected_record and selected_record.is_isolated),
            owner_slot_id=(
                selected_record.slot_id if selected_record is not None else slot_id
            ),
            fleet_group_key=group_key,
            delegated=bool(
                selected_record is not None
                and selected_record.slot_id != str(slot_id)
            ),
        )

    def task_fleet_ids(self) -> Set[str]:
        """Return the fleets this task actually occupies, for its fleet budget.

        Fleet.list inventory is Agent-global — one connection also reports
        fleets belonging to other tasks — so the budget is counted over fleets
        bound to THIS coordinator's workers. A fleet the owner inventory no
        longer reports (missing/released) stops consuming budget; a fleet whose
        slot is merely reconnecting ("suspect") still exists on the platform and
        keeps its share.
        """

        occupied: Set[str] = set()
        for assignment in self._worker_assignments.values():
            record = self._fleets.get(assignment.fleet_id)
            if record is not None and record.status in {"active", "suspect"}:
                occupied.add(assignment.fleet_id)
        return occupied

    def choose_under_cap(
        self,
        *,
        worker_id: str,
        slot_id: str,
        owner_agent_id: str,
        candidate_fleet_ids: Iterable[str],
        reuse_scope: str,
        page_policy: str,
        fleet_group_key: str = "",
        busy_fleet_ids: Iterable[str] = (),
    ) -> Optional[FleetAssignment]:
        """Reuse one of the task's own fleets once its fleet cap is full.

        This is the cap's degraded path, not ordinary routing: unlike
        `choose_existing` it may hand back a fleet that was created isolated,
        because an operator ceiling on live browser instances outranks the
        deployment's per-worker isolation preference. It still never touches a
        named-session cookie jar and never revives a released one — a caller
        that carries a real identity boundary must fail closed instead.

        `busy_fleet_ids` (fleets a running worker already holds) only ranks
        last, it never filters. Ordinary routing already lets two live workers
        share one fleet — `choose_existing` picks a slot's healthy fleet without
        asking who else is on it, and the click gate plus page leases are what
        keep that safe — so excluding them here would reject a worker for
        something allowed one fleet earlier.
        """

        candidates = {
            str(fleet_id).strip()
            for fleet_id in candidate_fleet_ids
            if str(fleet_id).strip()
        }
        busy = {
            str(fleet_id).strip()
            for fleet_id in busy_fleet_ids
            if str(fleet_id).strip()
        }
        reusable = [
            fleet_id
            for fleet_id in self.task_fleet_ids() & candidates
            if (
                (record := self._fleets.get(fleet_id)) is not None
                and record.status == "active"
                and record.admitted
                and not record.session_key
                and not record.retired_from_session
            )
        ]
        if not reusable:
            return None
        selected = max(
            reusable,
            key=lambda fleet_id: (
                fleet_id not in busy,
                not self._fleets[fleet_id].is_isolated,
                self._fleets[fleet_id].slot_id == str(slot_id),
                self._fleets[fleet_id].last_used_at,
                self._fleets[fleet_id].last_seen_at,
                fleet_id,
            ),
        )
        selected_record = self._fleets[selected]
        return self.bind_assignment(
            worker_id=worker_id,
            slot_id=slot_id,
            owner_agent_id=selected_record.owner_agent_id or owner_agent_id,
            fleet_id=selected,
            assignment_reason="task_fleet_cap_reuse",
            reuse_scope=reuse_scope,
            page_policy=page_policy,
            allowed_fleet_ids=[selected],
            created_for_worker=False,
            is_isolated=selected_record.is_isolated,
            owner_slot_id=selected_record.slot_id or slot_id,
            fleet_group_key=fleet_group_key,
            delegated=selected_record.slot_id != str(slot_id),
        )

    def bind_assignment(
        self,
        *,
        worker_id: str,
        slot_id: str,
        owner_agent_id: str,
        fleet_id: str,
        assignment_reason: str,
        reuse_scope: str,
        page_policy: str,
        session_key: str = "",
        allowed_fleet_ids: Optional[Iterable[str]] = None,
        created_for_worker: bool = False,
        is_isolated: bool = False,
        owner_slot_id: str = "",
        fleet_group_key: str = "",
        delegated: bool = False,
    ) -> FleetAssignment:
        fleet_id = str(fleet_id or "").strip()
        if not fleet_id:
            raise ValueError("fleet assignment requires a real fleetId")
        scope = normalize_reuse_scope(reuse_scope)
        policy = normalize_page_policy(page_policy, reuse_scope=scope)
        allowed = {
            str(item).strip()
            for item in (allowed_fleet_ids or [fleet_id])
            if str(item).strip()
        }
        allowed.add(fleet_id)
        key = str(session_key or "").strip()
        group_key = str(fleet_group_key or "").strip()
        record = self._fleets.get(fleet_id)
        record_is_new = record is None
        if record_is_new:
            record = FleetRecord(
                fleet_id=fleet_id,
                slot_id=str(slot_id),
                owner_agent_id=str(owner_agent_id),
            )
        if record.retired_from_session:
            raise FleetRoutingError(
                "released_fleet_conflict",
                f"fleet {fleet_id!r} was released from a prior named session",
                details={"assignedFleetId": fleet_id},
            )
        existing_binding = self._session_bindings.get(key, "") if key else ""
        if existing_binding and existing_binding != fleet_id:
            raise FleetRoutingError(
                "session_binding_conflict",
                f"session_key {key!r} is already bound to another fleet",
                details={
                    "sessionKey": key,
                    "boundFleetId": existing_binding,
                    "requestedFleetId": fleet_id,
                },
            )
        if record.session_key and record.session_key != key:
            raise FleetRoutingError(
                "fleet_session_conflict",
                f"fleet {fleet_id!r} is already bound to another session_key",
                details={
                    "requestedSessionKey": key,
                    "boundSessionKey": record.session_key,
                    "assignedFleetId": fleet_id,
                },
            )
        if not key and record.session_key:
            raise FleetRoutingError(
                "fleet_session_conflict",
                f"fleet {fleet_id!r} is reserved for a named session",
                details={
                    "boundSessionKey": record.session_key,
                    "assignedFleetId": fleet_id,
                },
            )
        effective_isolated = bool(record.is_isolated or is_isolated)
        if group_key:
            existing_group_fleet = self._fleet_groups.get(group_key, "")
            if existing_group_fleet and existing_group_fleet != fleet_id:
                existing_group_record = self._fleets.get(existing_group_fleet)
                if (
                    existing_group_record is not None
                    and existing_group_record.status == "active"
                ):
                    raise FleetRoutingError(
                        "fleet_routing_conflict",
                        f"fleet group {group_key!r} is already bound to another fleet",
                        details={
                            "fleetGroupKey": group_key,
                            "boundFleetId": existing_group_fleet,
                            "requestedFleetId": fleet_id,
                        },
                    )
        session_generation = 0
        if key:
            session_generation = self._session_generations.setdefault(key, 1)
        assignment = FleetAssignment(
            worker_id=str(worker_id),
            slot_id=str(slot_id),
            owner_agent_id=str(owner_agent_id),
            fleet_id=fleet_id,
            assignment_reason=str(assignment_reason or "explicit"),
            reuse_scope=scope,
            page_policy=policy,
            session_key=key,
            session_generation=session_generation,
            allowed_fleet_ids=tuple(sorted(allowed)),
            created_for_worker=bool(created_for_worker),
            is_isolated=effective_isolated,
            owner_slot_id=str(
                owner_slot_id or record.slot_id or slot_id
            ),
            fleet_group_key=group_key,
            delegated=bool(delegated),
        )
        if record_is_new:
            self._fleets[fleet_id] = record
        self._worker_assignments[assignment.worker_id] = assignment
        if key:
            self._session_bindings[key] = fleet_id
            record.session_key = key
            record.session_generation = session_generation
        record.is_isolated = effective_isolated
        record.admitted = True
        if key or effective_isolated:
            for default_slot, default_fleet in list(self._slot_defaults.items()):
                if default_fleet == fleet_id:
                    self._slot_defaults.pop(default_slot, None)
        else:
            self._slot_defaults[assignment.slot_id] = fleet_id
        if group_key:
            self._fleet_groups[group_key] = fleet_id
            record.fleet_group_key = group_key
        # A delegated worker has an acting slot/agent but does not become the
        # fleet owner. Ownership is kept on the socket that created/discovered
        # the fleet so notifications continue to have one stable destination.
        if not assignment.delegated:
            record.slot_id = assignment.owner_slot_id or assignment.slot_id
            record.owner_agent_id = assignment.owner_agent_id
        record.last_seen_at = time.time()
        record.last_used_at = record.last_seen_at
        record.status = "active"
        return assignment

    def retire_slot(self, slot_id: str) -> None:
        """Drop unbound metadata and tombstone named sessions for a dead slot."""

        self._slot_defaults.pop(str(slot_id), None)
        for fleet_id, record in list(self._fleets.items()):
            if record.slot_id != str(slot_id):
                continue
            record.status = (
                "released" if record.retired_from_session else "missing"
            )
            if not record.session_key:
                self._fleets.pop(fleet_id, None)
                for group_key, grouped_fleet in list(self._fleet_groups.items()):
                    if grouped_fleet == fleet_id:
                        self._fleet_groups.pop(group_key, None)

    def touch_worker(self, worker_id: str) -> None:
        assignment = self.assignment_for_worker(worker_id)
        if assignment is None:
            return
        record = self._fleets.get(assignment.fleet_id)
        if record is not None:
            record.last_used_at = time.time()

    def slot_snapshot(self, slot_id: str) -> List[dict]:
        return [
            {
                "fleetId": record.fleet_id,
                "ownerAgentId": record.owner_agent_id,
                "lastSeenAt": record.last_seen_at,
                "lastUsedAt": record.last_used_at,
                "origins": sorted(record.origins),
                "status": record.status,
                "isDefault": self._slot_defaults.get(slot_id) == record.fleet_id,
                "sessionKey": record.session_key,
                "sessionGeneration": record.session_generation,
                "isIsolated": record.is_isolated,
                "retiredFromSession": record.retired_from_session,
                "fleetGroupKey": record.fleet_group_key,
                "admitted": record.admitted,
            }
            for record in self._fleets.values()
            if record.slot_id == slot_id
        ]


def normalize_reuse_scope(value: str, *, explicit_continuation: bool = False) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "page" if explicit_continuation else "connection"
    if raw not in VALID_REUSE_SCOPES:
        raise ValueError(
            f"reuse_scope must be one of {sorted(VALID_REUSE_SCOPES)}; got {value!r}"
        )
    return raw


def normalize_page_policy(value: str, *, reuse_scope: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "existing" if reuse_scope == "page" else "new"
    if raw not in VALID_PAGE_POLICIES:
        raise ValueError(
            f"page_policy must be one of {sorted(VALID_PAGE_POLICIES)}; got {value!r}"
        )
    if reuse_scope != "page" and raw == "existing":
        raise ValueError("page_policy='existing' requires reuse_scope='page'")
    return raw
