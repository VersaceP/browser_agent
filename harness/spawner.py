"""
harness.spawner - Worker BrowserAgent spawning and lifecycle management.
"""

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

from abcp_client import ABCPClient, ABCPTransportError
from harness.constants import (
    WORKER_STATUS_CANCELLED,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
)
from harness.auth_fleet import (
    AuthFleetLedger,
    canonical_origin,
    normalize_auth_verification_contract,
)
from harness.diagnostics import status_category
from harness.fleet_coordinator import (
    FleetAssignment,
    FleetCoordinator,
    FleetRoutingError,
    handle_records_from_value,
    normalize_page_policy,
    normalize_reuse_scope,
)
from harness.fleet_runtime import (
    FleetAuthBarrier,
    PageLeaseManager,
    PageLeasedBrowserClient,
)
from harness.render_recovery import extract_page_id_from_values
from runtime_config import RuntimeConfig
from harness.lifecycle import LifecycleContext, default_lifecycle_manager
from harness.model_config import browser_agent_model_config
from harness.schema_cache import global_schemas_dir
from harness.schema_loader import CapabilityBundle, load_capability_bundle
from harness.task_control import (
    build_attempt_digest,
    cancel_phase_running_reservation,
    classification_for_worker_status,
    contract_hash_for_phase,
    mark_phase_result,
    mark_phase_running,
    phase_prior_artifact_paths,
    phase_pacing_remaining_seconds,
    phase_start_rejection,
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


def _prompt_worker_contract(worker_contract: Any) -> JsonDict:
    """Return the contract view exposed to the worker LLM.

    Top-level underscore-prefixed fields are harness-private provenance/state.
    Keep them on ``harness.worker_contract`` while excluding them from prompt
    text so implementation details cannot influence the worker's decisions.
    """
    if not isinstance(worker_contract, dict):
        return {}
    return {
        key: value for key, value in worker_contract.items()
        if not str(key).startswith("_")
    }


def _skill_execution_metadata(skill_outcome: Any) -> JsonDict:
    if not isinstance(skill_outcome, dict):
        return {
            "executionMode": "browser_slow_path",
            "fastPathRows": 0,
            "repairRows": 0,
        }
    completed_rows = optional_int(skill_outcome.get("completedRows"), 0) or 0
    if skill_outcome.get("handled"):
        mode = "skill_fast_path"
        repair_rows = 0
    elif isinstance(skill_outcome.get("repair_manifest"), dict):
        mode = "skill_repair"
        repairs = skill_outcome["repair_manifest"].get("repairs")
        repair_rows = len(repairs) if isinstance(repairs, list) else 0
    else:
        mode = "browser_slow_path"
        repair_rows = 0
    return {
        "executionMode": mode,
        "fastPathRows": max(0, completed_rows),
        "repairRows": repair_rows,
    }


def _effective_worker_status(current_status: str, skill_answer: Any) -> str:
    # A handled fast path deliberately skips BrowserAgent.run(), whose terminal
    # transition normally changes the constructor default from running -> done.
    # Validation remains a separate dimension in validatedStatus.
    return WORKER_STATUS_DONE if skill_answer is not None else current_status


def _finalize_skill_execution_metadata(
    metadata: JsonDict,
    harness: Any,
) -> JsonDict:
    """Repair mode can disable itself during record_extraction when its trusted
    baseline becomes unreadable/inconsistent. Re-derive telemetry after the LLM
    run so reports describe the actual full slow-path replacement, while keeping
    fastPathRows as useful history.
    """
    out = dict(metadata)
    contract = getattr(harness, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest") if isinstance(contract, dict) else None
    )
    disabled_reason = (
        str(manifest.get("disabledReason") or "").strip()
        if isinstance(manifest, dict) else ""
    )
    if disabled_reason:
        out["executionMode"] = "browser_slow_path"
        out["skillRepairFallback"] = True
        out["repairFallbackReason"] = disabled_reason
    trace = getattr(harness, "trace", None)
    selected_workflow_calls = sum(
        1
        for item in (trace if isinstance(trace, list) else [])
        if isinstance(item, dict) and item.get("type") == "execute_selected_skill"
    )
    if selected_workflow_calls:
        # Keep executionMode honest: the BrowserAgent LLM still orchestrated
        # this path, so it is not the zero-LLM fast path. This companion field
        # proves the frozen registry recipe ran instead of being reconstructed.
        out["selectedSkillWorkflowCalls"] = selected_workflow_calls
        out["skillAssistedSlowPath"] = True
    return out


def _unresolved_repair_visual_evidence(harness: Any) -> List[JsonDict]:
    contract = getattr(harness, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest") if isinstance(contract, dict) else None
    )
    if isinstance(manifest, dict) and manifest.get("disabledReason"):
        # Full slow-path replacement abandoned the baseline repair contract;
        # visual obligations tied to that baseline no longer govern completion.
        return []
    pending = (
        manifest.get("visualEvidencePending")
        if isinstance(manifest, dict) else None
    )
    if not isinstance(pending, list) or not pending:
        return []
    satisfied = (
        manifest.get("visualEvidenceSatisfied")
        if isinstance(manifest, dict) else None
    )
    satisfied_signatures = set(satisfied) if isinstance(satisfied, dict) else set()
    return [
        dict(item) for item in pending
        if isinstance(item, dict)
        and (
            not str(item.get("signature") or "")
            or str(item.get("signature")) not in satisfied_signatures
        )
    ]


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
    recovery_failure_cycles: int = 0
    recovery_unavailable_since: float = 0.0
    idle_event_logger: Optional[Callable[[str, JsonDict], None]] = None


@dataclass
class _SessionStartLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


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
        self.page_lease_manager = PageLeaseManager()
        self.fleet_auth_barrier = FleetAuthBarrier(
            wait_timeout_seconds=getattr(
                self.runtime.harness,
                "fleet_auth_barrier_wait_seconds",
                120.0,
            )
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
        session_key: Optional[str] = None,
        page_policy: Optional[str] = None,
    ) -> JsonDict:
        effective_contract = worker_contract or {}
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
        try:
            effective_reuse_scope = normalize_reuse_scope(
                str(reuse_scope or effective_contract.get("reuse_scope") or ""),
                explicit_continuation=explicit_continuation,
            )
            effective_page_policy = normalize_page_policy(
                str(page_policy or effective_contract.get("page_policy") or ""),
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
                    )
                if not isinstance(slot, dict):
                    await self._initialize_reserved_slot(slot)
                    registration = await self._prepare_slot_for_worker(
                        slot,
                        worker_id,
                        expose_reusable_pages=expose_reusable_pages,
                    )
                    assignment = await self._assign_fleet_for_worker(
                        slot,
                        worker_id=worker_id,
                        worker_contract=effective_contract,
                        reuse_scope=effective_reuse_scope,
                        page_policy=effective_page_policy,
                        session_key=effective_session_key,
                        reuse_from_worker_id=str(
                            reuse_from_worker_id or ""
                        ).strip(),
                        fleet_group_key=fleet_group_key,
                    )
                    self._ensure_notification_relay(slot, assignment)
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
                if isinstance(exc, ABCPTransportError):
                    slot.status = "broken"
                    self.fleet_coordinator.mark_slot_suspect(slot.slot_id)
                    slot.current_worker_id = None
                    if slot.client is not None:
                        await slot.client.close()
                        slot.client = None
                else:
                    self._release_slot_start_failure(slot, worker_id=worker_id)
            result = {
                "status": "failed",
                "error": str(exc),
                "workerId": worker_id,
                "name": agent_name,
            }
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
                "fleetGroupKey": fleet_group_key,
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
            "fleetGroupKey": fleet_group_key,
            "fleetAssignment": assignment.to_dict() if assignment else None,
        }

    def _fleet_group_key(
        self,
        *,
        session_key: str,
        worker_id: str,
        needs_isolated_session: bool,
    ) -> str:
        if not getattr(
            self.runtime.harness, "same_fleet_multiworker_enabled", False
        ):
            return ""
        key = str(session_key or "").strip()
        if key:
            return f"session:{key}"
        if needs_isolated_session:
            return f"isolated:{worker_id}"
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
        )
        if explicit_rejection is not None:
            return explicit_rejection

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
        session_key: str,
    ) -> Optional[BrowserAgentSlot]:
        idle_slots = [
            slot for slot in self._slots.values()
            if slot.status == "idle"
        ]
        if not idle_slots:
            return None

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
        """Connect a reserved slot without holding the global slot-pool lock."""

        if slot.client is not None:
            if slot.status == "starting":
                slot.status = "running" if slot.current_worker_id else "idle"
            return
        event_logger = make_browser_event_logger(
            self.logger,
            self.runtime.harness.log_browser_payloads,
            prefix=f"{slot.slot_id}.transport",
        )
        client = ABCPClient(self.runtime.browser, on_event=event_logger)
        slot.client = client
        slot.idle_event_logger = event_logger
        try:
            await client.connect()
            registration = await client.call(
                "System.register",
                {"agentId": slot.agent_id},
            )
        except Exception:
            try:
                await client.close()
            finally:
                slot.client = None
                slot.status = "broken"
            raise
        slot.registration = registration
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
            client = ABCPClient(self.runtime.browser, on_event=event_logger)
            try:
                await client.connect()
                registration = await client.call(
                    "System.register",
                    {"agentId": slot.agent_id},
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
    ) -> JsonDict:
        if slot.client is None:
            raise ABCPTransportError(f"Slot {slot.slot_id} has no browser client")
        registration = await slot.client.call(
            "System.register",
            {"agentId": slot.agent_id},
        )
        slot.registration = registration
        self._replace_slot_fleets_from_response(slot, registration)
        self._update_slot_registry_from_value(slot, registration)
        if expose_reusable_pages or self._slot_sync_due(slot):
            await self._sync_slot_registry(slot, worker_id=worker_id)
        self._observe_slot_fleets(slot)
        return registration

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
        fleet_group_key: str = "",
    ) -> Optional[FleetAssignment]:
        lock_key = str(
            fleet_group_key or (f"session:{session_key}" if session_key else "")
        ).strip()
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
                reuse_from_worker_id=reuse_from_worker_id,
                fleet_group_key=fleet_group_key,
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
        fleet_group_key: str = "",
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
            before = set(slot.fleet_ids)
            response = await slot.client.call("Fleet.create", {})
            self._update_slot_registry_from_value(slot, response)
            created_ids = sorted(slot.fleet_ids.difference(before))
            if not created_ids:
                # A successful response is expected to carry fleetId.  Refresh
                # once from the authoritative owner view before failing closed.
                await self._sync_slot_registry(slot, worker_id=worker_id)
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

    def _ensure_notification_relay(
        self,
        acting_slot: BrowserAgentSlot,
        assignment: Optional[FleetAssignment],
    ) -> None:
        """Relay owner-socket notifications to a delegated acting socket.

        Dispatcher currently delivers fleet events only to the registered
        owner agent.  The relay preserves that single owner connection while
        allowing waiters/observers attached to a delegated BrowserAgent to see
        the same notification stream.
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
        subscribe = getattr(owner_slot.client, "subscribe_notifications", None)
        if not callable(publish) or not callable(subscribe):
            return

        def relay(message: JsonDict) -> None:
            publish(message)

        self._notification_relays[key] = subscribe(relay)
        self.logger.write(
            "spawner.fleet.notification_relay_attached",
            {
                "fleetId": assignment.fleet_id,
                "ownerSlotId": owner_slot.slot_id,
                "actingSlotId": acting_slot.slot_id,
            },
        )

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
            self._replace_slot_fleets_from_response(slot, fleet_response)
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
        assignment: Optional[FleetAssignment] = None,
    ) -> str:
        payload = self._slot_context_summary(
            slot,
            expose_reusable_pages=expose_reusable_pages,
            assignment=assignment,
        )
        payload["reuseRules"] = [
            (
                "Calls targeting the same page are serialized by the harness;"
                " different delegated pages in the assigned fleet may run concurrently."
                if getattr(
                    self.runtime.harness,
                    "same_fleet_multiworker_enabled",
                    False,
                )
                else "Calls targeting the same page must be serialized."
            ),
            "During login/CAPTCHA resolution the fleet-wide auth barrier pauses every non-resolver worker.",
            "After any Page.switchTo, Page.create, or Page.navigate, refresh DOM.getAXTree before targeting elements.",
        ]
        if assignment is not None:
            payload["reuseRules"].extend([
                (
                    "Every Page.create must explicitly use assignedFleetId="
                    f"{assignment.fleet_id}. Fresh page does not mean fresh fleet."
                ),
                (
                    "Do not call Fleet.create/Fleet.close and do not fabricate"
                    " or substitute another fleetId; fleet routing and lifecycle"
                    " are coordinator/Dispatcher-owned."
                ),
            ])
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

    def _page_bindings_for_worker(
        self,
        slot: BrowserAgentSlot,
        *,
        assignment: Optional[FleetAssignment],
        expose_reusable_pages: bool,
    ) -> Dict[str, str]:
        """Return existing page handles explicitly delegated to this worker."""

        if assignment is None or not expose_reusable_pages:
            return {}
        allowed_fleets = set(assignment.allowed_fleet_ids)
        return {
            str(page_id): str(page.get("fleetId") or "")
            for page_id, page in slot.page_registry.items()
            if (
                str(page_id).strip()
                and str(page.get("fleetId") or "") in allowed_fleets
                and not _page_hidden_from_reuse(slot, page)
            )
        }

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
        return self._extract_collection_items(
            value,
            collection_keys=("pages", "tabs"),
            id_keys=("pageId", "page_id"),
        )

    def _extract_fleet_items(self, value: Any) -> Optional[List[JsonDict]]:
        return self._extract_collection_items(
            value,
            collection_keys=("fleets",),
            id_keys=("fleetId", "fleet_id"),
        )

    def _extract_collection_items(
        self,
        value: Any,
        *,
        collection_keys: tuple[str, ...],
        id_keys: tuple[str, ...],
    ) -> Optional[List[JsonDict]]:
        def normalize_list(items: Any) -> Optional[List[JsonDict]]:
            if not isinstance(items, list):
                return None
            records = [item for item in items if isinstance(item, dict)]
            if not records:
                return []
            if any(any(item.get(key) for key in id_keys) for item in records):
                return records
            return None

        if isinstance(value, dict):
            for key in collection_keys:
                records = normalize_list(value.get(key))
                if records is not None:
                    return records
            data = value.get("data")
            if isinstance(data, dict):
                for key in collection_keys:
                    records = normalize_list(data.get(key))
                    if records is not None:
                        return records
            elif isinstance(data, list):
                records = normalize_list(data)
                if records is not None:
                    return records
        return normalize_list(value)

    def _replace_slot_fleets_from_response(
        self,
        slot: BrowserAgentSlot,
        response: Any,
    ) -> bool:
        """Converge slot inventory when a response carries an owner fleet list."""

        fleet_items = self._extract_fleet_items(response)
        if fleet_items is None:
            return False
        observed_fleet_ids = {
            str(item.get("fleetId") or item.get("fleet_id") or "").strip()
            for item in fleet_items
            if isinstance(item, dict)
        }
        observed_fleet_ids.discard("")
        removed_fleet_ids = slot.fleet_ids.difference(observed_fleet_ids)
        slot.fleet_ids = observed_fleet_ids
        if removed_fleet_ids:
            removed_page_ids = {
                page_id
                for page_id, page in slot.page_registry.items()
                if str(page.get("fleetId") or "") in removed_fleet_ids
            }
            for page_id in removed_page_ids:
                slot.page_registry.pop(page_id, None)
                slot.page_quarantine.pop(page_id, None)
        return True

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
        self._observe_slot_fleets(slot)
        self.fleet_coordinator.touch_worker(worker_id)

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
        self._release_slot_to_pool(
            slot,
            worker_id=worker_id,
            event="spawner.slot.released",
            remember_worker=True,
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

        for item in handle_records_from_value(value):
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
            "fleetRouting": self.fleet_coordinator.slot_snapshot(slot.slot_id),
        }

    def _slot_context_summary(
        self,
        slot: BrowserAgentSlot,
        *,
        expose_reusable_pages: bool,
        assignment: Optional[FleetAssignment] = None,
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
        if assignment is not None:
            payload["fleetAssignment"] = assignment.to_dict()
            payload["assignedFleetId"] = assignment.fleet_id
            payload["allowedFleetIds"] = list(assignment.allowed_fleet_ids)
            payload["pageReuseMode"] = (
                "explicit_page_continuation"
                if expose_reusable_pages
                else "fresh_page_same_fleet"
            )
        if not expose_reusable_pages:
            return payload
        allowed_fleet_ids = (
            set(assignment.allowed_fleet_ids)
            if assignment is not None
            else {
                *slot.fleet_ids,
                *{
                    str(page.get("fleetId") or "")
                    for page in slot.page_registry.values()
                    if str(page.get("fleetId") or "")
                },
            }
        )
        payload["fleetIds"] = sorted(allowed_fleet_ids)
        payload["pages"] = [
            dict(page)
            for page in list(slot.page_registry.values())[:20]
            if (
                str(page.get("fleetId") or "") in allowed_fleet_ids
                and not _page_hidden_from_reuse(slot, page)
            )
        ]
        payload["origins"] = (
            sorted({
                str(page.get("origin") or _origin_from_url(page.get("url") or ""))
                for page in payload["pages"]
                if str(page.get("origin") or _origin_from_url(page.get("url") or ""))
            })
            if assignment is not None
            else sorted(slot.origins)
        )
        payload["stalePages"] = [
            {
                "pageId": page.get("pageId"),
                "fleetId": page.get("fleetId"),
                "url": page.get("url"),
                "lastStateError": page.get("lastStateError"),
            }
            for page in list(slot.page_registry.values())[:20]
            if (
                str(page.get("fleetId") or "") in allowed_fleet_ids
                and str(page.get("status") or "") == "stale"
            )
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
            if str(
                (slot.page_registry.get(str(quarantine.get("pageId") or "")) or {}).get(
                    "fleetId"
                )
                or ""
            ) in allowed_fleet_ids
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
    ) -> Optional[JsonDict]:
        """Attempt a matching skill's fast path. Returns the dispatch outcome:
        {"handled": True, "answer": ...} when the skill completed the task,
        {"handled": False, "handoff_note": ...} when a batch run stopped mid-way
        (completed rows persisted; the note tells the slow path what remains),
        or None (caller runs the normal LLM loop with the original task).
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
        return outcome

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
        try:
            # 07-07: a directly forced skill takes health OUT of the loop —
            # dispatch stopped recording, and health-driven autoheal must not
            # fire either. A suite route is different: its four-dimensional
            # phase match is exact, so it remains eligible for health/autoheal.
            from harness.skill.contract import is_suite_routed
            from harness.skill.dispatch import _is_explicit_selection
            suite_routed = is_suite_routed(worker_contract or {})
            # An exact suite route is health-managed and may autoheal after a
            # degraded workflow falls back successfully. A direct force remains
            # outside both health accounting and health-driven autoheal.
            if not suite_routed and _is_explicit_selection(worker_contract or {}):
                return
        except Exception:  # pragma: no cover - guard must never break the worker
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
                mode=str(getattr(self.runtime.harness, "skill_selection_mode", "manual") or "manual"),
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

    def _record_guidance_signal(
        self,
        *,
        worker_contract: JsonDict,
        fast_path_handled: bool,
        validated_ok: bool,
        steps: int,
        answer: str,
    ) -> None:
        """Guidance（hints）层的防腐弱信号：结局 + 步数 + answer 里的
        guidance_stale 上报 → skills/.guidance_health.json（独立软通道，只标
        needs_review 供人工复审，永不禁用）。record_guidance_outcome 只接受
        suite_routed；直接强制单个 guidance 不记。Best-effort，绝不影响结果。"""
        if not getattr(self.runtime.harness, "skill_guidance_signal_enabled", True):
            return
        try:
            from harness.skill.guidance import record_guidance_outcome
            record_guidance_outcome(
                self._get_skill_registry(),
                worker_contract,
                validated_ok=validated_ok,
                fast_path_handled=fast_path_handled,
                steps=steps,
                answer=answer,
                logger=self.logger,
            )
        except Exception as exc:  # weak signal must never break the worker
            self.logger.write("skill.guidance.signal_error", {"error": str(exc)})

    async def _run_browser_worker(
        self,
        slot: BrowserAgentSlot,
        registration: JsonDict,
        assignment: Optional[FleetAssignment],
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

        harness = None
        try:
            if slot.client is None:
                raise ABCPTransportError(f"Slot {slot.slot_id} has no browser client")
            slot.client.on_event = event_logger
            bundle = await self._capability_bundle_for_worker(
                slot.client,
                worker_runtime,
            )
            slot_context = self._render_slot_context(
                slot,
                expose_reusable_pages=expose_reusable_pages,
                assignment=assignment,
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
            prompt_worker_contract = _prompt_worker_contract(worker_contract)
            worker_task = (
                f"BrowserAgent name: {name}\n"
                f"Independent context:\n{effective_context}\n\n"
                f"<worker_contract>\n"
                f"{json.dumps(prompt_worker_contract, ensure_ascii=False, indent=2, default=str)}\n"
                f"</worker_contract>\n\n"
                f"Result contract:\n{result_contract or 'Return a structured JSON string containing outcome, data, evidence, next_steps.'}\n\n"
                f"Assigned task:\n{task}"
            )
            owner_client = None
            if assignment is not None and assignment.delegated:
                owner_slot = self._slots.get(assignment.owner_slot_id)
                owner_client = owner_slot.client if owner_slot is not None else None
            worker_browser = PageLeasedBrowserClient(
                slot.client,
                self.page_lease_manager,
                fleet_owner_client=owner_client,
            )
            harness = self.browser_agent_factory(
                provider,
                worker_browser,
                worker_runtime,
                self.logger,
            )
            harness.worker_contract = worker_contract or {}
            harness.preloaded_registration = registration
            harness.preloaded_capability_bundle = bundle
            harness.assigned_fleet_id = assignment.fleet_id if assignment else ""
            harness.allowed_fleet_ids = set(
                assignment.allowed_fleet_ids if assignment else ()
            )
            page_bindings = self._page_bindings_for_worker(
                slot,
                assignment=assignment,
                expose_reusable_pages=expose_reusable_pages,
            )
            harness.allowed_page_ids = set(page_bindings)
            harness.page_fleet_ids = dict(page_bindings)
            harness.page_reuse_allowed = bool(expose_reusable_pages)
            harness.fleet_assignment_reason = (
                assignment.assignment_reason if assignment else ""
            )
            harness.fleet_session_key = assignment.session_key if assignment else ""
            harness.fleet_session_generation = (
                assignment.session_generation if assignment else 0
            )
            harness.fleet_is_isolated = bool(
                assignment.is_isolated if assignment else False
            )
            harness.worker_id = worker_id
            harness.page_lease_manager = self.page_lease_manager
            harness.fleet_auth_barrier = (
                self.fleet_auth_barrier
                if getattr(
                    self.runtime.harness,
                    "fleet_auth_barrier_enabled",
                    False,
                )
                else None
            )
            harness.fleet_barrier_generation = (
                self.fleet_auth_barrier.generation(assignment.fleet_id)
                if assignment is not None
                else 0
            )
            harness.fleet_reperception_pending = False
            harness.fleet_reperception_generation = (
                harness.fleet_barrier_generation
            )
            harness.fleet_reperception_state_seen = False
            harness.fleet_reperception_tree_seen = False
            harness.auth_session_verified_handler = (
                (lambda payload: self._record_verified_auth_session(
                    assignment, payload
                ))
                if assignment is not None and assignment.session_key
                else None
            )
            harness.auth_session_lost_handler = (
                self._handle_auth_session_lost
                if assignment is not None and assignment.session_key
                else None
            )

            skill_outcome = await self._try_skill_fast_path(
                harness,
                worker_contract=worker_contract or {},
                phase=phase or {},
                task=task,
                context=effective_context,
                fleet_ids=([assignment.fleet_id] if assignment else sorted(slot.fleet_ids)),
            )
            skill_answer = (
                skill_outcome.get("answer")
                if skill_outcome and skill_outcome.get("handled")
                else None
            )
            execution_metadata = _skill_execution_metadata(skill_outcome)
            if skill_answer is not None:
                answer = skill_answer
                harness.final_status = _effective_worker_status(
                    harness.final_status, skill_answer,
                )
            else:
                # A batch fast path that stopped mid-way hands its progress to the
                # slow path: completed rows are already persisted, the note says
                # which rows remain and how to merge into ONE final artifact.
                handoff_note = str((skill_outcome or {}).get("handoff_note") or "")
                if handoff_note:
                    repair_manifest = (skill_outcome or {}).get("repair_manifest")
                    if isinstance(repair_manifest, dict):
                        harness.worker_contract = {
                            **(harness.worker_contract or {}),
                            "_repair_manifest": dict(repair_manifest),
                        }
                    worker_task = (
                        f"{worker_task}\n\nSKILL FAST-PATH BATCH HANDOFF:\n{handoff_note}"
                    )
                answer = await harness.run(worker_task)
            execution_metadata = _finalize_skill_execution_metadata(
                execution_metadata, harness,
            )
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
                file_evidence=getattr(harness, "file_action_evidence", []),
                task_dir=self.logger.task_dir,
            )
            unresolved_visual = _unresolved_repair_visual_evidence(harness)
            if unresolved_visual:
                artifact_validation["status"] = "failed"
                failures = artifact_validation.get("failures")
                if not isinstance(failures, list):
                    failures = []
                    artifact_validation["failures"] = failures
                failures.append({
                    "type": "repair_absence_visual_evidence",
                    "message": (
                        "repair marked fields confirmed_absent but completed no"
                        " visual_verify before worker termination"
                    ),
                    "pending": unresolved_visual,
                })
            terminal_classification = classification_for_worker_status(
                harness.final_status
            )
            if terminal_classification is not None:
                artifact_validation["classification"] = terminal_classification
            elif artifact_validation.get("status") != "done":
                feedback_classification = _worker_feedback_classification(
                    harness.trace,
                    answer,
                    persisted_artifacts=[
                        *list(getattr(harness, "artifacts", []) or []),
                        *list(
                            getattr(harness, "extraction_attempt_artifacts", [])
                            or []
                        ),
                    ],
                )
                if feedback_classification is not None:
                    artifact_validation["classification"] = feedback_classification
                    if feedback_classification.get("evidenceGate"):
                        self.logger.write("semantic_terminal.evidence_gate", {
                            "workerId": worker_id,
                            "phaseId": phase_id,
                            "claimedCategory": feedback_classification.get(
                                "claimedCategory"
                            ),
                            "category": feedback_classification.get("category"),
                            "evidenceGate": feedback_classification.get(
                                "evidenceGate"
                            ),
                        })
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
                fleet_ids=([assignment.fleet_id] if assignment else sorted(slot.fleet_ids)),
            )
            self._record_guidance_signal(
                worker_contract=worker_contract or {},
                fast_path_handled=skill_answer is not None,
                validated_ok=validated_status == "validated_done",
                steps=int(trace_summary.get("toolCalls") or 0),
                answer=str(answer or ""),
            )
            diagnostics = getattr(harness, "diagnostics", None)
            result = {
                "status": harness.final_status,
                "statusCategory": status_category(harness.final_status),
                "validatedStatus": validated_status,
                **execution_metadata,
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
            if assignment is not None:
                result["fleetAssignment"] = assignment.to_dict()
            self._update_slot_after_worker(
                slot,
                worker_id=worker_id,
                phase_id=phase_id,
                worker_contract=worker_contract,
                result=result,
                trace=getattr(harness, "trace", []),
            )
        except asyncio.CancelledError:
            harness_obj = harness
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
            if isinstance(assignment, FleetAssignment):
                result["fleetAssignment"] = assignment.to_dict()
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
            self._remove_notification_relay_for_assignment(assignment)
            await self.fleet_auth_barrier.abandon_worker(worker_id)
            mark_phase_result(
                self.logger,
                phase_id=phase_id,
                worker_id=worker_id,
                validation=None,
                result_status=WORKER_STATUS_CANCELLED,
                phase=phase,
                worker_contract=worker_contract,
            )
            raise
        except Exception as exc:
            harness_obj = harness
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
                self.fleet_coordinator.mark_slot_suspect(slot.slot_id)
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

        self._remove_notification_relay_for_assignment(assignment)
        await self.fleet_auth_barrier.abandon_worker(worker_id)
        if isinstance(assignment, FleetAssignment):
            result.setdefault("fleetAssignment", assignment.to_dict())
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
            phase=phase,
            worker_contract=worker_contract,
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
    "session_fleet_lost",
    "fleet_assignment_lost",
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
    return canonical_origin(url)


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
    url = str(page.get("url") or "").strip().lower()
    url = url.split("#", 1)[0].split("?", 1)[0]
    if (
        url == "about:blank"
        or url.startswith("chrome://")
        or url.endswith("/newtab.html")
        or url.endswith("://newtab.html")
    ):
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
    persisted_artifacts: Optional[List[str]] = None,
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
    return _classification_from_final_answer(
        answer,
        persisted_artifacts=persisted_artifacts,
    )


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


# Evidence gate (A + B3) for semantic-terminal blockers. A: the blocker must
# carry reason text. B3: evidenceArtifacts must name at least one savedPath
# the harness itself recorded via record_extraction this run — we trust the
# harness ledger, never the filesystem or the model's claim, so a fabricated
# path can not mint a terminal verdict. Failure costs are asymmetric: a false
# terminal silently blocks dependents and tells the user their instruction is
# wrong, while a false downgrade just spends another attempt — so the gate
# fails closed toward "not terminal".
_SEMANTIC_TERMINAL_MIN_REASON_CHARS = 40


def _blocker_evidence_paths(
    blocker: JsonDict,
    classification: JsonDict,
) -> List[str]:
    raw = blocker.get("evidenceArtifacts")
    if not isinstance(raw, list):
        raw = classification.get("evidenceArtifacts")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _semantic_terminal_evidence_failure(
    category: str,
    blocker: JsonDict,
    classification: JsonDict,
    persisted_artifacts: Optional[List[str]],
) -> Optional[str]:
    """Return None when the semantic-terminal claim may go terminal, else a
    short human-readable reason why it must be downgraded to retryable."""
    reason_text = str(
        blocker.get("reason")
        or classification.get("reason")
        or blocker.get("message")
        or blocker.get("detail")
        or ""
    ).strip()
    if not reason_text:
        return "blocker carries no reason text"
    evidence_paths = _blocker_evidence_paths(blocker, classification)
    ledger = {
        os.path.normpath(str(path).strip())
        for path in (persisted_artifacts or [])
        if str(path).strip()
    }
    if any(os.path.normpath(path) in ledger for path in evidence_paths):
        return None
    if category == "instruction_infeasible":
        # Infeasibility often has nothing extractable to persist (the site
        # lacks the requested concept entirely), so a substantive reason is
        # acceptable evidence on its own.
        if len(reason_text) >= _SEMANTIC_TERMINAL_MIN_REASON_CHARS:
            return None
        return (
            "no evidenceArtifacts entry matches a record_extraction savedPath"
            " from this run, and the reason text is too thin to stand alone"
        )
    if not evidence_paths:
        return "no evidenceArtifacts listed"
    return (
        "no evidenceArtifacts entry matches a record_extraction savedPath"
        " from this run"
    )


def _classification_from_final_answer(
    answer: str,
    persisted_artifacts: Optional[List[str]] = None,
) -> Optional[JsonDict]:
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
            # Models phrase the blocker several ways; accept a top-level
            # "category" key too so a semantic-terminal report is not
            # silently dropped back into the retry loop.
            category = str(
                raw_classification
                or blocker.get("category")
                or blocker.get("type")
                or ""
            ).strip()
            classification = {"category": category}
        if category not in {
            "blocked_cross_task_type_required",
            "blocked_infrastructure",
            "target_absent",
            "instruction_infeasible",
        }:
            continue
        if category in {"target_absent", "instruction_infeasible"}:
            gate_failure = _semantic_terminal_evidence_failure(
                category,
                blocker,
                classification,
                persisted_artifacts,
            )
            if gate_failure is not None:
                # Downgrade, never drop silently: keep the claim visible so
                # the Lead/next worker can persist evidence and re-declare,
                # but do not let it mint a terminal phase status.
                classification["category"] = f"{category}_unverified"
                classification["claimedCategory"] = category
                classification["evidenceGate"] = gate_failure
                classification.setdefault("hint", (
                    f"Worker claimed {category} but the evidence gate failed:"
                    f" {gate_failure}. Persist the observed evidence via"
                    " record_extraction and re-declare with its savedPath in"
                    " evidenceArtifacts, or keep working the phase."
                )[:500])
                classification["source"] = "final_answer.blockers"
                return classification
        hint = (
            blocker.get("hint")
            or blocker.get("message")
            or blocker.get("reason")
            or (
                "Browser infrastructure failed; reconnect/rebuild the Browser"
                " Client before retrying."
                if category == "blocked_infrastructure"
                else (
                    "LeadAgent should stop retrying the same target and ask the"
                    " user to revise the range/source."
                    if category in {"target_absent", "instruction_infeasible"}
                    else "LeadAgent should replan with a task_type that permits the required method."
                )
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
