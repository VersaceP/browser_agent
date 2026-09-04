"""
harness.spawner.spawner_core - BrowserAgentSpawner - construction, spawn entry points and lifecycle.
"""

import asyncio
import json
import time
from dataclasses import replace
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
from harness.observation.browser_call import extract_page_id_from_values
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
from .spawner_helpers import BrowserAgentFactory, BrowserAgentHandle, BrowserAgentSlot, FleetReadinessError, PinnedBrowserContext, ResumeBrowserHint, TaskSessionBinding, _SessionStartLock  # noqa: F401
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
        # A HITL resume may prove continuity only inside this task.  Keep that
        # routing constraint separate from the durable auth-fleet ledger,
        # whose entries require an explicit predeclared auth contract.
        self._task_session_binding = self._load_task_session_binding()
        # A generic human-resolved challenge is not authentication proof. Keep
        # its Fleet/Page identity in memory only until the owning worker ends;
        # it becomes an exact Page continuation solely when that worker leaves
        # an unfinished page-local form. Completed work discards it.
        self._pending_task_session_candidates: Dict[str, TaskSessionBinding] = {}
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
            self.runtime.harness.context_file,
            project_context_files=getattr(
                self.runtime.harness, "project_context_files", None,
            ),
            append_system_prompt=getattr(
                self.runtime.harness, "append_system_prompt", None,
            ),
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
        # Only the first successful Fleet acquisition in one top-level task
        # may consult historical task memory. The lock remains held through
        # readiness so concurrent first workers cannot both claim that role.
        self._first_fleet_acquisition_lock = None
        self._first_fleet_acquisition_committed = False
        self._preverified_fleet_readiness: Dict[str, JsonDict] = {}
        self._browser_context_fingerprints: Dict[str, str] = {}
        # Page inventory is slot-global, while resume state is task-local.
        # Record only pages this task actually addressed; sharing a Fleet does
        # not make every tab returned by Page.list part of this task.
        self._task_browser_page_ids: Dict[str, Set[str]] = {}
        # LeadAgent sets this to the original user objective before the first
        # spawn. Direct spawner tests/callers fall back to the worker task.
        self.root_task = ""

    def _task_session_binding_expired(
        self,
        binding: TaskSessionBinding,
        *,
        now_ms: Optional[int] = None,
    ) -> bool:
        ttl_seconds = max(
            0.0,
            float(getattr(
                self.runtime.harness,
                "task_session_binding_ttl_seconds",
                86400.0,
            )),
        )
        anchor_ms = binding.last_verified_at_ms or binding.created_at_ms
        if ttl_seconds <= 0.0 or anchor_ms <= 0:
            return False
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return current_ms - anchor_ms > int(ttl_seconds * 1000)

    def _archive_expired_task_session_binding(
        self,
        *,
        state: JsonDict,
        browser: JsonDict,
        binding: TaskSessionBinding,
    ) -> None:
        expired = replace(
            binding,
            state="stale",
            reason="task_session_binding_ttl_expired",
        )
        bindings = browser.get("task_session_bindings")
        bindings = dict(bindings) if isinstance(bindings, dict) else {}
        bindings[binding.phase_id] = expired.to_dict()
        browser["task_session_bindings"] = bindings
        current = TaskSessionBinding.from_value(
            browser.get("task_session_binding"),
        )
        if current is not None and (
            current.fleet_id,
            current.page_id,
            current.session_generation,
        ) == (
            binding.fleet_id,
            binding.page_id,
            binding.session_generation,
        ):
            browser.pop("task_session_binding", None)
        state["browser_context"] = browser
        write_task_state(self.logger, state)
        self.logger.write("spawner.task_session_binding.expired", {
            "binding": binding.to_dict(),
            "ttlSeconds": float(getattr(
                self.runtime.harness,
                "task_session_binding_ttl_seconds",
                86400.0,
            )),
        })

    def _load_task_session_binding(self) -> Optional[TaskSessionBinding]:
        """Load the one browser-continuity constraint valid for this task.

        A verified HITL resume is evidence that the task's active browser page
        owns a login.  It is deliberately stronger than a phase-local resume
        hint: later phases must not silently acquire a clean Fleet and lose
        that state.  This remains task-scoped and is never read by the
        cross-task auth ledger.
        """
        try:
            state = load_task_state(self.logger)
        except Exception as exc:  # pragma: no cover - diagnostic-only startup
            self.logger.write(
                "spawner.task_session_binding.load_failed",
                {"error": str(exc)[:300]},
            )
            return None
        browser = state.get("browser_context") if isinstance(state, dict) else {}
        browser = browser if isinstance(browser, dict) else {}
        binding = TaskSessionBinding.from_value(
            browser.get("task_session_binding"),
        )
        if binding is not None:
            if self._task_session_binding_expired(binding):
                self._archive_expired_task_session_binding(
                    state=state,
                    browser=browser,
                    binding=binding,
                )
                return None
            # Persisted identity is a candidate after process reconstruction,
            # not proof that the Fleet/Page is still live. Explicit-fleet
            # inventory and (for page scope) page probing reactivate it during
            # spawn.
            return (
                replace(binding, state="needs_reverification")
                if binding.state == "active"
                else binding
            )
        # Compatibility for the short-lived phase-indexed representation used
        # while this feature was introduced.  It had no ordering metadata, so
        # use it only when it names exactly one unambiguous Fleet/Page pair.
        raw_bindings = browser.get("task_session_bindings")
        raw_bindings = raw_bindings if isinstance(raw_bindings, dict) else {}
        candidates: Dict[tuple[str, str], TaskSessionBinding] = {}
        for phase_id, raw in raw_bindings.items():
            binding = TaskSessionBinding.from_value(raw, phase_id=str(phase_id))
            # A host reset removes the singular active pointer but retains the
            # indexed stale record for audit. Never resurrect that tombstone as
            # the old compatibility representation on process reconstruction.
            if binding is not None and self._task_session_binding_expired(binding):
                self._archive_expired_task_session_binding(
                    state=state,
                    browser=browser,
                    binding=binding,
                )
                continue
            if binding is not None and binding.state != "stale":
                candidates[(binding.fleet_id, binding.page_id)] = binding
        if len(candidates) == 1:
            binding = next(iter(candidates.values()))
            return (
                replace(binding, state="needs_reverification")
                if binding.state == "active"
                else binding
            )
        if candidates:
            self.logger.write(
                "spawner.task_session_binding.load_ambiguous",
                {"count": len(candidates)},
            )
        return None

    def _task_session_binding_for_phase(
        self, phase_id: Optional[str],
    ) -> Optional[TaskSessionBinding]:
        # phase_id is intentionally accepted for call-site clarity. Fleet
        # continuity is task-wide; exact Page continuity is represented by the
        # binding's own scope and is reserved for unfinished page-local work.
        del phase_id
        return self._task_session_binding

    def _persist_task_session_binding(
        self,
        binding: TaskSessionBinding,
        *,
        event: str,
    ) -> Optional[TaskSessionBinding]:
        state = load_task_state(self.logger)
        browser = state.get("browser_context")
        browser = dict(browser) if isinstance(browser, dict) else {}
        bindings = browser.get("task_session_bindings")
        bindings = dict(bindings) if isinstance(bindings, dict) else {}
        previous = TaskSessionBinding.from_value(
            bindings.get(binding.phase_id), phase_id=binding.phase_id,
        )
        bindings[binding.phase_id] = binding.to_dict()
        browser["task_session_bindings"] = bindings
        browser["task_session_binding"] = binding.to_dict()
        state["browser_context"] = browser
        write_task_state(self.logger, state)
        self._task_session_binding = binding
        self.logger.write(event, {
            "binding": binding.to_dict(),
            "superseded": bool(previous is not None and previous != binding),
        })
        return previous

    def _mark_task_session_binding_stale(self, reason: str) -> JsonDict:
        binding = self._task_session_binding
        if binding is None:
            return {"updated": False, "reason": "not_found"}
        stale = replace(
            binding,
            state="stale",
            reason=str(reason or "task session binding became stale").strip(),
        )
        self._persist_task_session_binding(
            stale, event="spawner.task_session_binding.stale",
        )
        return {"updated": True, "binding": stale.to_dict()}

    def _missing_task_continuation_page_error(
        self,
        binding: TaskSessionBinding,
        *,
        fleet_id: str,
        sync_receipt: JsonDict,
    ) -> FleetRoutingError:
        """Classify a missing exact page only from this round's Page.list proof."""

        page_list_succeeded = fleet_id in set(
            sync_receipt.get("pageListSucceededFleetIds") or []
        )
        if not page_list_succeeded:
            return FleetRoutingError(
                "task_session_page_temporarily_unavailable",
                (
                    "The task's exact continuation page was not found, but"
                    " the bound Fleet's Page.list did not complete in this"
                    " synchronization round. This is not proof that the page"
                    " was closed."
                ),
                retryable=True,
                next_instruction=(
                    "Retry after browser transport recovery without resetting"
                    " the binding or creating a replacement page."
                ),
                details={
                    "taskSessionBinding": binding.to_dict(),
                    "registrySync": sync_receipt,
                },
            )
        stale = self._mark_task_session_binding_stale(
            "page_continuation_lost"
        )
        return FleetRoutingError(
            "page_continuation_lost",
            (
                "The task's exact continuation page is absent from a"
                " successful authoritative Page.list of its bound Fleet. Any"
                " unsaved page-local form state is lost."
            ),
            retryable=False,
            next_instruction=(
                "Report that the original unsaved page continuation cannot be"
                " recovered. Do not claim that a newly created page resumes"
                " it; an operator may reset the stale binding only to"
                " deliberately restart."
            ),
            details={
                "lossKind": "exact_page_missing",
                "pinSource": "task_page_continuation",
                "taskSessionBinding": binding.to_dict(),
                "staleTransition": stale,
                "registrySync": sync_receipt,
            },
        )

    def _verify_task_session_binding(self) -> Optional[TaskSessionBinding]:
        binding = self._task_session_binding
        if binding is None:
            return None
        verified = replace(
            binding,
            state="active",
            reason="",
            last_verified_at_ms=int(time.time() * 1000),
        )
        self._persist_task_session_binding(
            verified, event="spawner.task_session_binding.verified",
        )
        return verified

    async def reset_task_session_binding(
        self,
        *,
        expected_fleet_id: str,
        expected_page_id: str = "",
        expected_generation: int = 0,
        reason: str,
    ) -> JsonDict:
        """Host/operator-only CAS reset after authoritative continuity loss.

        This is intentionally not a Lead/Browser tool. A model cannot discard
        an active authenticated context to escape a busy or temporarily broken
        page. The host may reset only a binding already marked stale and must
        echo the identity from the failure receipt.
        """

        binding = self._task_session_binding
        why = str(reason or "").strip()
        if binding is None:
            return {"released": False, "reason": "not_found"}
        if binding.state != "stale":
            raise FleetRoutingError(
                "task_session_binding_conflict",
                "only a stale task session binding can be reset",
                details={"taskSessionBinding": binding.to_dict()},
            )
        if (
            binding.fleet_id != str(expected_fleet_id or "").strip()
            or (
                str(expected_page_id or "").strip()
                and binding.page_id != str(expected_page_id or "").strip()
            )
            or (
                int(expected_generation or 0) > 0
                and binding.session_generation != int(expected_generation)
            )
            or not why
        ):
            raise FleetRoutingError(
                "task_session_binding_conflict",
                "task session binding changed before operator reset",
                details={"taskSessionBinding": binding.to_dict()},
            )
        active_workers = [
            handle.worker_id
            for handle in self._handles.values()
            if not handle.async_task.done()
            and (
                (assignment := self.fleet_coordinator.assignment_for_worker(
                    handle.worker_id
                )) is not None
                and assignment.fleet_id == binding.fleet_id
            )
        ]
        if active_workers:
            raise FleetRoutingError(
                "task_session_reset_busy",
                "cannot reset task continuity while a worker uses its Fleet",
                retryable=True,
                details={"workerIds": active_workers},
            )
        state = load_task_state(self.logger)
        browser = state.get("browser_context")
        browser = dict(browser) if isinstance(browser, dict) else {}
        bindings = browser.get("task_session_bindings")
        bindings = dict(bindings) if isinstance(bindings, dict) else {}
        bindings[binding.phase_id] = replace(
            binding, reason=f"operator reset: {why}",
        ).to_dict()
        browser["task_session_bindings"] = bindings
        browser.pop("task_session_binding", None)
        state["browser_context"] = browser
        write_task_state(self.logger, state)
        self._task_session_binding = None
        receipt = {
            "released": True,
            "binding": binding.to_dict(),
            "reason": why,
        }
        self.logger.write("spawner.task_session_binding.operator_reset", receipt)
        return receipt

    def _record_task_session_binding(
        self,
        assignment: Optional[FleetAssignment],
        payload: JsonDict,
    ) -> JsonDict:
        """Persist a task-only Fleet/Page constraint after a resumed HITL wait."""

        phase_id = str(payload.get("phaseId") or "").strip()
        page_id = str(payload.get("pageId") or "").strip()
        if assignment is None or not phase_id or not page_id:
            return {"recorded": False, "reason": "binding_identity_required"}
        now_ms = int(time.time() * 1000)
        binding_scope = str(payload.get("bindingScope") or "fleet").strip().lower()
        if binding_scope not in {"fleet", "page"}:
            return {"recorded": False, "reason": "invalid_binding_scope"}
        binding = TaskSessionBinding(
            phase_id=phase_id,
            fleet_id=assignment.fleet_id,
            page_id=page_id,
            session_generation=max(
                0,
                int(payload.get("sessionGeneration") or assignment.session_generation or 0),
            ),
            source="hitl_resume",
            binding_scope=binding_scope,
            state="active",
            reason=str(payload.get("reason") or "hitl_resume_continuity").strip(),
            created_at_ms=now_ms,
            last_verified_at_ms=now_ms,
            auth_verified=bool(payload.get("authVerified", False)),
        )
        if not binding.auth_verified:
            # Do not turn every CAPTCHA/HITL resume into a task-wide Fleet
            # routing constraint. The candidate is consumed only by the
            # unfinished-form transition below and is never reconstructed
            # across processes on its own.
            self._pending_task_session_candidates[assignment.worker_id] = binding
            receipt = {
                "recorded": False,
                "candidateRecorded": True,
                "reason": "positive_auth_evidence_required",
                "candidate": binding.to_dict(),
            }
            self.logger.write(
                "spawner.task_session_binding.candidate_recorded", receipt,
            )
            return receipt
        self._pending_task_session_candidates.pop(assignment.worker_id, None)
        try:
            previous = self._persist_task_session_binding(
                binding, event="spawner.task_session_binding.recorded",
            )
        except Exception as exc:  # local persistence must not re-close HITL
            receipt = {
                "recorded": False,
                "reason": "task_session_binding_write_failed",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            }
            self.logger.write("spawner.task_session_binding.write_failed", receipt)
            return receipt
        receipt = {
            "recorded": True,
            "binding": binding.to_dict(),
            "superseded": bool(
                previous is not None and previous != binding
            ),
        }
        return receipt

    def _update_task_session_binding_after_worker(
        self,
        *,
        phase: JsonDict,
        result: JsonDict,
    ) -> Optional[JsonDict]:
        """Promote only unfinished page-local work to exact Page continuity."""

        binding = self._task_session_binding
        worker_id = str(result.get("workerId") or "").strip()
        candidate = self._pending_task_session_candidates.pop(worker_id, None)
        if binding is not None and binding.state == "stale":
            return None
        task_type = str((phase or {}).get("task_type") or "").strip()
        stage_hint = str((phase or {}).get("stage_hint") or "").strip()
        if task_type != "form_filling" or stage_hint != "form_interaction":
            return None
        validation = result.get("artifactValidation")
        validation_done = bool(
            isinstance(validation, dict) and validation.get("status") == "done"
        )
        worker_done = str(result.get("status") or "") == "done"
        if worker_done and validation_done:
            # A generic HITL candidate has no lifecycle beyond its worker when
            # the page-local contract is already complete.
            if binding is None or not binding.requires_exact_page:
                return None
            demoted = replace(
                binding,
                binding_scope="fleet",
                reason="page_local_form_contract_completed",
                last_verified_at_ms=int(time.time() * 1000),
            )
            self._persist_task_session_binding(
                demoted, event="spawner.task_session_binding.demoted_to_fleet",
            )
            return {"transition": "page_to_fleet", "binding": demoted.to_dict()}

        binding = binding or candidate
        if binding is None:
            return None
        trace_summary = result.get("traceSummary")
        trace_summary = trace_summary if isinstance(trace_summary, dict) else {}
        latest_stats = trace_summary.get("latestPageStats")
        latest_stats = latest_stats if isinstance(latest_stats, dict) else {}
        page_ids = [
            str(item).strip()
            for item in (trace_summary.get("pageIds") or [])
            if str(item).strip()
        ]
        # Never move an existing exact-page continuation to a popup merely
        # because that popup was the last observed page. For a fresh HITL
        # candidate, prefer the resumed page when it is present in this
        # worker's trace; only then fall back to the latest observed page.
        page_id = (
            binding.page_id
            if binding.requires_exact_page
            else binding.page_id
            if binding.page_id and binding.page_id in page_ids
            else str(latest_stats.get("pageId") or "").strip()
        )
        if not page_id:
            if len(page_ids) == 1:
                page_id = page_ids[0]
        if not page_id:
            page_id = binding.page_id
        if not page_id:
            return None
        promoted = replace(
            binding,
            phase_id=str(result.get("phaseId") or binding.phase_id),
            page_id=page_id,
            binding_scope="page",
            state="active",
            reason="unsaved_form_continuation",
            last_verified_at_ms=int(time.time() * 1000),
        )
        if promoted == binding:
            return None
        self._persist_task_session_binding(
            promoted, event="spawner.task_session_binding.promoted_to_page",
        )
        return {"transition": "fleet_to_page", "binding": promoted.to_dict()}

    async def _begin_first_fleet_acquisition(self) -> bool:
        """Claim the task's first-acquisition gate, if still uncommitted."""

        if self._first_fleet_acquisition_lock is None:
            self._first_fleet_acquisition_lock = asyncio.Lock()
        await self._first_fleet_acquisition_lock.acquire()
        if self._first_fleet_acquisition_committed:
            self._first_fleet_acquisition_lock.release()
            return False
        return True

    def _finish_first_fleet_acquisition(
        self,
        claimed: bool,
        *,
        committed: bool,
    ) -> None:
        if not claimed:
            return
        if committed:
            self._first_fleet_acquisition_committed = True
        lock = self._first_fleet_acquisition_lock
        if lock is None:
            raise RuntimeError("first Fleet acquisition lock was not initialized")
        # ``claimed`` is the ownership token returned only after this task
        # acquired the lock. asyncio.Lock.locked() does not identify an owner
        # and could otherwise release a different task's acquisition.
        lock.release()

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
        task_session_binding = (
            None
            if pinned is not None
            else self._task_session_binding_for_phase(phase_id)
        )
        if (
            task_session_binding is not None
            and task_session_binding.state == "stale"
        ):
            return {
                "status": "task_session_recovery_required",
                "error": (
                    "The task's browser continuity binding is authoritatively"
                    " stale and must be reset by the host before a fresh"
                    " authentication flow can start."
                ),
                "retryable": False,
                "taskSessionBinding": task_session_binding.to_dict(),
                "tool_was_executed": False,
                "next_instruction": (
                    "Do not create or select another Fleet. Ask the operator to"
                    " call the host-only reset_task_session_binding API with"
                    " the exact Fleet/Page/generation in this receipt, then"
                    " restart authentication."
                ),
            }
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
        if task_session_binding is not None:
            # HITL establishes task-local Fleet continuity. Exact Page
            # continuity is stronger and is promoted only for unfinished
            # page-local work such as an unsaved form checkpoint.
            requested_reuse_scope = (
                "page" if task_session_binding.requires_exact_page else "fleet"
            )
            requested_page_policy = (
                "existing" if task_session_binding.requires_exact_page else "new"
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
        if task_session_binding is not None:
            if (
                effective_fleet_reference
                and effective_fleet_reference.lower()
                != task_session_binding.fleet_id.lower()
            ):
                return {
                    "status": "task_session_binding_violation",
                    "error": (
                        "This task has an active browser-continuity binding on"
                        f" Fleet {task_session_binding.fleet_id}; a different"
                        " fleet cannot replace it."
                    ),
                    "taskSessionBinding": task_session_binding.to_dict(),
                    "tool_was_executed": False,
                }
            if effective_session_key:
                return {
                    "status": "task_session_binding_violation",
                    "error": (
                        "A task-local browser-continuity binding is active;"
                        " omit session_key so the exact bound Fleet/Page can"
                        " continue."
                    ),
                    "taskSessionBinding": task_session_binding.to_dict(),
                    "tool_was_executed": False,
                }
            if effective_contract.get("needs_isolated_session") is True:
                return {
                    "status": "task_session_binding_violation",
                    "error": (
                        "needs_isolated_session would create a new identity"
                        " while this phase requires the bound session."
                    ),
                    "taskSessionBinding": task_session_binding.to_dict(),
                    "tool_was_executed": False,
                }
            effective_fleet_reference = task_session_binding.fleet_id
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
        hard_isolation_requested = bool(
            isolation_declared
            and effective_contract.get("needs_isolated_session") is True
        )
        automatic_task_reuse_allowed = not bool(
            pinned is not None
            or self.resume_browser_hint is not None
            or effective_fleet_reference
            or effective_session_key
            or str(preferred_slot_id or "").strip()
            or str(reuse_from_worker_id or "").strip()
            or requested_reuse_scope
            or requested_page_policy
            or hard_isolation_requested
        )
        effective_contract = {
            **effective_contract,
            # Harness-private memory policy; _prompt_worker_contract strips it.
            "_fleet_memory_auto_reuse_eligible": not bool(
                effective_session_key
                or hard_isolation_requested
                or pinned is not None
            ),
        }
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
        first_acquisition_claimed = False
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
                    registration, inventory_sync = await self._prepare_slot_for_worker(
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
                    first_acquisition_claimed = (
                        await self._begin_first_fleet_acquisition()
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
                        root_task=str(self.root_task or task or "").strip(),
                        automatic_task_reuse_allowed=(
                            automatic_task_reuse_allowed
                            and first_acquisition_claimed
                        ),
                        inventory_sync=inventory_sync,
                    )
                    preverified = self._preverified_fleet_readiness.pop(
                        worker_id, None
                    )
                    preverified_matches = bool(
                        isinstance(preverified, dict)
                        and assignment is not None
                        and preverified.get("fleetId") == assignment.fleet_id
                    )
                    if preverified_matches:
                        # Similar-task admission commits only after its own
                        # readiness probe, so the task's first acquisition is
                        # already successful before any later page/relay work.
                        self._finish_first_fleet_acquisition(
                            first_acquisition_claimed,
                            committed=True,
                        )
                        first_acquisition_claimed = False
                    self._ensure_notification_relay(slot, assignment)
                    if preverified_matches:
                        readiness_receipt = preverified
                    else:
                        readiness_receipt = await self._ensure_assigned_fleet_ready(
                            slot,
                            assignment,
                            worker_id=worker_id,
                        )
                        self._finish_first_fleet_acquisition(
                            first_acquisition_claimed,
                            committed=assignment is not None,
                        )
                        first_acquisition_claimed = False
                    assigned_page_sync: JsonDict = {}
                    if assignment is not None and (
                        expose_reusable_pages
                        or bool(
                            self.pinned_browser_context
                            and self.pinned_browser_context.page_id
                        )
                        or bool(
                            task_session_binding
                            and task_session_binding.requires_exact_page
                        )
                    ):
                        assigned_page_sync = await self._sync_assigned_fleet_pages(
                            slot,
                            assignment,
                            worker_id=worker_id,
                        )
                    if assignment is not None and task_session_binding is not None:
                        if (
                            task_session_binding.session_generation > 0
                            and assignment.session_generation > 0
                            and task_session_binding.session_generation
                            != assignment.session_generation
                        ):
                            stale = self._mark_task_session_binding_stale(
                                "session_generation_mismatch"
                            )
                            raise FleetRoutingError(
                                "task_session_generation_mismatch",
                                (
                                    "The task continuity Fleet belongs to a"
                                    " different session generation."
                                ),
                                retryable=False,
                                next_instruction=(
                                    "Use the host-only task session reset with"
                                    " this stale receipt, then authenticate"
                                    " again; do not silently reuse the new"
                                    " generation."
                                ),
                                details=stale,
                            )
                    if (
                        assignment is not None
                        and task_session_binding is not None
                        and task_session_binding.requires_exact_page
                    ):
                        bound_page = slot.page_registry.get(
                            task_session_binding.page_id
                        )
                        if (
                            not isinstance(bound_page, dict)
                            or str(bound_page.get("fleetId") or "")
                            != assignment.fleet_id
                        ):
                            raise self._missing_task_continuation_page_error(
                                task_session_binding,
                                fleet_id=assignment.fleet_id,
                                sync_receipt=assigned_page_sync,
                            )
                        if _page_hidden_from_reuse(slot, bound_page):
                            raise FleetRoutingError(
                                "task_session_page_temporarily_unavailable",
                                (
                                    "The exact continuation page is present but"
                                    " currently quarantined or otherwise not"
                                    " reusable. This is not proof that it was"
                                    " closed."
                                ),
                                retryable=True,
                                next_instruction=(
                                    "Wait for page recovery/reverification; do"
                                    " not reset the binding or create a"
                                    " replacement page from this receipt."
                                ),
                                details={
                                    "taskSessionBinding": (
                                        task_session_binding.to_dict()
                                    ),
                                },
                            )
                    if assignment is not None and task_session_binding is not None:
                        task_session_binding = (
                            self._verify_task_session_binding()
                            or task_session_binding
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
                        primary_bound_page_id = (
                            task_session_binding.page_id
                            if (
                                task_session_binding is not None
                                and task_session_binding.requires_exact_page
                            )
                            else resume_hint.page_id
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
                        )
                        self._persist_task_browser_context(
                            slot,
                            assignment,
                            phase_id=phase_id,
                            primary_page_id=primary_bound_page_id,
                            replace_pages=bool(
                                resume_page_inventory_refreshed
                                or expose_reusable_pages
                                or task_session_binding is not None
                                or (
                                    self.pinned_browser_context
                                    and self.pinned_browser_context.page_id
                                )
                            ),
                        )
        except asyncio.CancelledError:
            self._preverified_fleet_readiness.pop(worker_id, None)
            self._finish_first_fleet_acquisition(
                first_acquisition_claimed,
                committed=False,
            )
            first_acquisition_claimed = False
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
            self._preverified_fleet_readiness.pop(worker_id, None)
            self._finish_first_fleet_acquisition(
                first_acquisition_claimed,
                committed=False,
            )
            first_acquisition_claimed = False
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
            self._preverified_fleet_readiness.pop(worker_id, None)
            self._finish_first_fleet_acquisition(
                first_acquisition_claimed,
                committed=False,
            )
            first_acquisition_claimed = False
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
        finally:
            # The explicit success/error branches decide whether an acquisition
            # committed and clear this token. This is the BaseException safety
            # net: no uncommon control-flow escape may strand every later spawn.
            self._finish_first_fleet_acquisition(
                first_acquisition_claimed,
                committed=False,
            )
            first_acquisition_claimed = False
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
                task_session_binding=task_session_binding,
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
        reported_reuse_scope = (
            assignment.reuse_scope if assignment else effective_reuse_scope
        )
        reported_page_policy = (
            assignment.page_policy if assignment else effective_page_policy
        )
        self.logger.write(
            "spawner.browser.spawn",
            {
                "workerId": worker_id,
                "agentId": slot.agent_id,
                "slotId": slot.slot_id,
                "slotReuse": bool(slot.last_worker_id),
                "pageReuseAllowed": expose_reusable_pages,
                "reuseScope": reported_reuse_scope,
                "pagePolicy": reported_page_policy,
                "sessionKey": effective_session_key,
                "fleetReference": effective_fleet_reference,
                "fleetGroupKey": fleet_group_key,
                "taskSessionBinding": (
                    task_session_binding.to_dict()
                    if task_session_binding is not None else None
                ),
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
            "reuseScope": reported_reuse_scope,
            "pagePolicy": reported_page_policy,
            "sessionKey": effective_session_key,
            "fleetReference": effective_fleet_reference,
            "fleetGroupKey": fleet_group_key,
            "taskSessionBinding": (
                task_session_binding.to_dict()
                if task_session_binding is not None else None
            ),
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
