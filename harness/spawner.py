"""
harness.spawner - Worker BrowserAgent spawning and lifecycle management.
"""

import asyncio
import json
import os
import re
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

from abcp_client import ABCPClient, ABCPTransportError
from harness.constants import (
    COLLECTION_CONTRACT_REPLAN_REQUIRED,
    WORKER_STATUS_CANCELLED,
    WORKER_STATUS_DONE,
    WORKER_STATUS_FAILED,
)
from harness.call_outcome import classify_call_outcome, evaluate_grant
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
    resolve_fleet_reference,
)
from harness.fleet_runtime import (
    FleetAuthBarrier,
    FleetClickGate,
    PageLeaseManager,
    PageLeasedBrowserClient,
)
from harness.render_recovery import extract_page_id_from_values
from harness.extraction_artifacts import field_names_from_specs
from harness.fast_path import assess_fast_path_candidate
from harness.row_ledger import (
    identity_fields_from_contract,
    row_identity,
    derive_row_facts,
    derive_row_ledger,
)
from runtime_config import RuntimeConfig
from harness.lifecycle import LifecycleContext, default_lifecycle_manager
from harness.model_config import browser_agent_model_config
from harness.observation.event_observer import unwrap_notification
from harness.schema_cache import global_schemas_dir
from harness.schema_loader import CapabilityBundle, load_capability_bundle
from harness.task_control import (
    build_attempt_digest,
    cancel_phase_running_reservation,
    classification_for_worker_status,
    clear_spawn_acquisition_failures,
    contract_hash_for_phase,
    mark_phase_result,
    mark_phase_running,
    phase_prior_artifact_paths,
    phase_pacing_remaining_seconds,
    phase_start_rejection,
    record_replan_checkpoint,
    record_spawn_acquisition_failure,
    spawn_acquisition_fingerprint,
    spawn_acquisition_rejection,
    validate_worker_artifacts,
    load_task_state,
    write_task_state,
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
    storage_for_logger,
    task_subdir,
    trim_large_strings,
)
from harness.worker_result import (
    build_worker_handoff_projection,
    build_worker_result_levels,
)
from harness.workflow_runtime import workflow_execution_enabled
from llm import LLMFactory


BrowserAgentFactory = Callable[[Any, ABCPClient, RuntimeConfig, RunLogger], Any]


class FleetReadinessError(ABCPTransportError):
    """Assigned Fleet did not become usable before worker construction."""

    # Readiness has already spent its bounded status probes. Re-entering the
    # same acquisition path immediately only repeats Fleet startup/restore
    # pressure, so reuse the existing acquisition ledger's cooldown. Keep the
    # duration authoritative in task_control rather than duplicating it here.
    requires_spawn_acquisition_cooldown = True

    def __init__(self, message: str, *, fleet_id: str, owner_slot_id: str):
        super().__init__(message, rpc_method="Fleet.status")
        self.fleet_id = str(fleet_id)
        self.owner_slot_id = str(owner_slot_id)


def _is_fleet_open_timeout(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return "-32012" in text and "fleet open timeout" in text


def _fresh_click_settlement_class(
    agent: Any,
    method: str,
    params: JsonDict,
) -> str:
    """Classify only a current canonical AX target; never read model purpose.

    Unknown selectors/coordinates and stale snapshots keep the conservative
    settlement window. A current non-link role may use the short window, while
    links retain the full popup allowance.
    """

    if agent is None or method != "Input.click":
        return "conservative"
    page_id = str(params.get("pageId") or "").strip()
    target_id = str(params.get("id") or "").strip()
    if (
        not page_id
        or not target_id
        or bool(getattr(agent, "axtree_invalidated", True))
        or str(getattr(agent, "axtree_page_id", "") or "") != page_id
    ):
        return "conservative"
    current_ids = set(getattr(agent, "axtree_ids", set()) or set())
    if target_id not in current_ids:
        return "conservative"
    nodes = list(getattr(agent, "axtree_nodes", []) or [])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or "") != target_id:
            continue
        role = str(node.get("role") or "").strip().lower()
        if not role:
            return "conservative"
        return "fresh_link" if role == "link" else "fresh_non_link"
    return "conservative"


async def _verified_workflow_hitl_settlement(
    agent: Any,
    page_id: str,
) -> JsonDict:
    """Run the existing verified barrier-open path after opaque Workflow HITL."""

    if agent is None:
        return {
            "enabled": True,
            "opened": False,
            "reason": "browser_agent_unavailable",
        }
    # Local import avoids making spawner/browser_tools initialization cyclic.
    from harness.tools.browser_tools import (
        _verify_and_open_fleet_auth_barrier,
    )

    return await _verify_and_open_fleet_auth_barrier(
        agent,
        str(page_id or ""),
        0,
    )


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


@dataclass(frozen=True)
class PinnedBrowserContext:
    """Trusted task-level routing target supplied outside the Lead plan."""

    fleet_id: str
    page_id: str = ""
    source: str = "api"

    @classmethod
    def from_value(cls, value: Any) -> Optional["PinnedBrowserContext"]:
        if value in (None, {}, ""):
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("pinned_browser_context must be an object")
        fleet_id = str(
            value.get("fleet_id") or value.get("fleetId") or ""
        ).strip()
        page_id = str(
            value.get("page_id") or value.get("pageId") or ""
        ).strip()
        source = str(value.get("source") or "api").strip() or "api"
        if not fleet_id:
            raise ValueError("pinned_browser_context.fleet_id is required")
        for label, raw in (("fleet_id", fleet_id), ("page_id", page_id)):
            if not raw:
                continue
            try:
                uuid.UUID(raw)
            except (ValueError, AttributeError) as exc:
                raise ValueError(
                    f"pinned_browser_context.{label} must be a UUID"
                ) from exc
        return cls(fleet_id=fleet_id, page_id=page_id, source=source)

    def to_dict(self) -> JsonDict:
        return {
            "fleetId": self.fleet_id,
            "pageId": self.page_id or None,
            "source": self.source,
            "mode": "existing_only",
        }


@dataclass(frozen=True)
class ResumeBrowserHint:
    """Best-effort browser target recovered from this task's prior state.

    Unlike :class:`PinnedBrowserContext`, this is not a routing constraint. A
    missing or conflicting hint is ignored, and ordinary assignment continues.
    """

    fleet_id: str
    page_id: str = ""
    phase_id: str = ""
    source: str = "task_state"

    @classmethod
    def from_value(cls, value: Any) -> Optional["ResumeBrowserHint"]:
        if value in (None, {}, ""):
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("resume_browser_hint must be an object")
        fleet_id = str(
            value.get("fleet_id") or value.get("fleetId") or ""
        ).strip()
        page_id = str(
            value.get("page_id") or value.get("pageId") or ""
        ).strip()
        phase_id = str(
            value.get("phase_id") or value.get("phaseId") or ""
        ).strip()
        source = str(value.get("source") or "task_state").strip() or "task_state"
        if not fleet_id:
            raise ValueError("resume_browser_hint.fleet_id is required")
        for label, raw in (("fleet_id", fleet_id), ("page_id", page_id)):
            if not raw:
                continue
            try:
                uuid.UUID(raw)
            except (ValueError, AttributeError) as exc:
                raise ValueError(
                    f"resume_browser_hint.{label} must be a UUID"
                ) from exc
        return cls(
            fleet_id=fleet_id,
            page_id=page_id,
            phase_id=phase_id,
            source=source,
        )

    def to_dict(self) -> JsonDict:
        return {
            "fleetId": self.fleet_id,
            "pageId": self.page_id or None,
            "phaseId": self.phase_id or None,
            "source": self.source,
            "mode": "best_effort",
        }


class _TaskContextTrackingBrowserClient(PageLeasedBrowserClient):
    """Observe successful browser calls before control returns to the worker."""

    def __init__(self, *args: Any, after_call: Optional[Callable[..., None]] = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._after_call = after_call

    async def call(self, method: str, params: Any = None) -> Any:
        result = await super().call(method, params)
        if self._after_call is not None:
            self._after_call(method, params, result)
        return result


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
            await self._sync_slot_registry(
                slot,
                worker_id=worker_id,
                include_page_details=False,
            )
            self._observe_slot_fleets(slot)
            if not any(
                str(error).startswith("Fleet.list")
                for error in slot.sync_errors
            ):
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
            await self._sync_slot_registry(
                slot,
                worker_id=worker_id,
                required_fleet_id=required_fleet_id,
                include_page_details=False,
            )
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
        fleet_id: str = "",
        fleet_group_key: str = "",
        isolation_auto_applied: bool = False,
        resume_browser_hint: Optional[ResumeBrowserHint] = None,
        resume_hint_may_select_page: bool = True,
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
                allowed_fleet_ids=[resolved_fleet_id],
                created_for_worker=False,
                owner_slot_id=stable_owner_slot_id,
                fleet_group_key=fleet_group_key,
                delegated=stable_owner_slot_id != slot.slot_id,
            )
        assignment: Optional[FleetAssignment] = None
        if resume_browser_hint is not None:
            hint_fleet_id = resume_browser_hint.fleet_id
            inventory_failed = any(
                str(error).startswith("Fleet.list")
                for error in slot.sync_errors
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
    def _fleet_status_ready(response: Any, fleet_id: str) -> bool:
        """Treat a successful Fleet.status response as authoritative readiness.

        Current ABCP returns data.status="active". Keeping the accepted set
        narrow catches a future explicit transitional state, while accepting a
        response without status preserves compatibility with older clients and
        test doubles: the status RPC itself could only complete after opening
        the Fleet.
        """

        explicit_statuses: List[str] = []
        root_data = response.get("data") if isinstance(response, dict) else None
        if isinstance(root_data, dict):
            root_fleet_id = str(
                root_data.get("fleetId") or root_data.get("fleet_id") or ""
            ).strip()
            root_status = str(root_data.get("status") or "").strip().lower()
            if root_status and (not root_fleet_id or root_fleet_id == fleet_id):
                explicit_statuses.append(root_status)
        for item in handle_records_from_value(response):
            item_fleet_id = str(
                item.get("fleetId") or item.get("fleet_id") or ""
            ).strip()
            status = str(item.get("status") or "").strip().lower()
            # Ignore nested page/task status fields. Only the record carrying
            # this Fleet's identity may certify its lifecycle state.
            if status and item_fleet_id == fleet_id:
                explicit_statuses.append(status)
        if not explicit_statuses:
            return True
        return any(
            status in {"active", "ready", "running", "idle"}
            for status in explicit_statuses
        )

    @staticmethod
    def _fleet_ready_notification(message: Any, fleet_id: str) -> bool:
        event = unwrap_notification(message)
        if event is None or str(event.get("event") or "") != "Fleet.ready":
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
                lambda message: self._fleet_ready_notification(message, fleet_id)
            )

            async def wait_for_ready_event() -> Optional[JsonDict]:
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

            event_waiter = asyncio.create_task(wait_for_ready_event())
        self.logger.write("spawner.fleet.readiness_started", {
            "fleetId": fleet_id,
            "ownerSlotId": owner_slot.slot_id,
            "workerId": worker_id,
            "timeoutSeconds": timeout,
        })
        initial_error = ""
        initial_status = ""
        try:
            try:
                response = await client.call("Fleet.status", {"fleetId": fleet_id})
                if self._fleet_status_ready(response, fleet_id):
                    receipt = {
                        "fleetId": fleet_id,
                        "ownerSlotId": owner_slot.slot_id,
                        "status": "ready",
                        "verifiedBy": "status",
                        "elapsedMs": int((time.monotonic() - started) * 1000),
                    }
                    self.logger.write("spawner.fleet.readiness_ready", receipt)
                    return receipt
                initial_status = "transitional"
            except Exception as exc:
                initial_error = str(exc)[:500]

            event = None
            if event_waiter is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining > 0:
                    # Never spend the entire remaining budget waiting for an
                    # event. ABCP emits Fleet.ready for process startup, but a
                    # later session-restore completion has no corresponding
                    # control event. Reserve at least half of this window for
                    # one terminal Fleet.status retry.
                    event_wait_seconds = min(5.0, remaining / 2.0)
                    try:
                        event = await asyncio.wait_for(
                            asyncio.shield(event_waiter),
                            timeout=event_wait_seconds,
                        )
                    except asyncio.TimeoutError:
                        event = None
            # A Fleet.ready signal must be confirmed, but its absence does not
            # prove session restore is still pending. Probe exactly once more
            # even when the soft signal budget was consumed by the first RPC.
            # Never loop or cancel an already-dispatched WebSocket RPC: the
            # actual wall-clock duration may therefore exceed `timeout`.
            try:
                response = await client.call(
                    "Fleet.status", {"fleetId": fleet_id}
                )
                if self._fleet_status_ready(response, fleet_id):
                    receipt = {
                        "fleetId": fleet_id,
                        "ownerSlotId": owner_slot.slot_id,
                        "status": "ready",
                        "verifiedBy": (
                            "event_then_status"
                            if event is not None
                            else "status_retry"
                        ),
                        "elapsedMs": int(
                            (time.monotonic() - started) * 1000
                        ),
                    }
                    self.logger.write(
                        "spawner.fleet.readiness_ready", receipt
                    )
                    return receipt
                initial_status = "transitional_after_retry"
            except Exception as exc:
                initial_error = str(exc)[:500]

            detail = initial_error or initial_status or "Fleet.ready was not observed"
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
    ) -> JsonDict:
        if assignment is None or not getattr(
            self.runtime.harness,
            "fleet_readiness_barrier_enabled",
            True,
        ):
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
    ) -> None:
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
        await self._sync_slot_registry(
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
                raise FleetRoutingError(
                    "pinned_page_unavailable",
                    (
                        f"pinned page {pinned.page_id!r} was not found in"
                        f" fleet {pinned.fleet_id!r} after readiness"
                    ),
                    retryable=False,
                    next_instruction=(
                        "Do not create or navigate a replacement page. Ask the"
                        " user to reopen the pinned page."
                    ),
                    details={"pinnedBrowserContext": pinned.to_dict()},
                )

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
        required_fleet_id: str = "",
        include_page_details: bool = True,
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

        fleet_timeout_counts: Dict[str, int] = {}
        unavailable_fleets: Set[str] = set()

        def note_fleet_timeout(fleet_id: str, exc: BaseException) -> None:
            if not _is_fleet_open_timeout(exc):
                return
            count = fleet_timeout_counts.get(fleet_id, 0) + 1
            fleet_timeout_counts[fleet_id] = count
            if count >= 2:
                unavailable_fleets.add(fleet_id)

        required = str(required_fleet_id or "").strip()
        fleet_ids_to_scan = (
            [required]
            if include_page_details and required and required in slot.fleet_ids
            else sorted(slot.fleet_ids)[:6]
            if include_page_details
            else []
        )
        for fleet_id in fleet_ids_to_scan:
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
                note_fleet_timeout(fleet_id, exc)
                slot.sync_errors.append(f"Page.list({fleet_id}): {str(exc)[:240]}")

        eligible_page_ids = [
            page_id
            for page_id in sorted(slot.page_registry.keys())
            if not required
            or str(
                (slot.page_registry.get(page_id) or {}).get("fleetId") or ""
            ).strip() == required
        ]
        # The scan is capped, and the cap used to be applied to an id-sorted
        # list. A slot holding more than the cap could therefore starve its
        # quarantined pages of the very Page.getState that decides whether they
        # are usable again or due for retirement, leaving them stuck forever on
        # a technicality of id ordering. Quarantined pages are also the ones
        # whose state we know the least about, so they go first: expired ones
        # (a retirement decision is pending) ahead of the rest.
        def _scan_rank(page_id: str) -> int:
            if page_id not in slot.page_quarantine:
                return 2
            return 0 if self._quarantine_expired(slot, page_id) else 1

        def _last_seen(page_id: str) -> float:
            # Within a rank, least-recently-rechecked first. Every scanned page
            # that is not retired gets re-marked, which refreshes
            # lastQuarantinedAt and sends it to the back — so consecutive syncs
            # rotate through the quarantine set instead of replaying the same
            # id-sorted prefix. Without this, more quarantined pages than the
            # cap would leave the tail permanently unexamined whenever the head
            # keeps failing to close.
            entry = slot.page_quarantine.get(page_id)
            if not isinstance(entry, dict):
                return 0.0
            stamp = entry.get("lastQuarantinedAt") or entry.get("quarantinedAt")
            return float(stamp) if isinstance(stamp, (int, float)) else 0.0

        page_ids_to_scan = sorted(
            eligible_page_ids,
            key=lambda pid: (_scan_rank(pid), _last_seen(pid), pid),
        )[:12] if include_page_details else []
        for page_id in page_ids_to_scan:
            page_fleet_id = str(
                (slot.page_registry.get(page_id) or {}).get("fleetId") or ""
            ).strip()
            if page_fleet_id and page_fleet_id in unavailable_fleets:
                continue
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
                    # This getState IS the TTL re-check: the sync already asks
                    # the authoritative question every pass, so retirement
                    # needs no extra probe — only the verdict plus the age of
                    # the quarantine.
                    retired = False
                    if self._quarantine_expired(slot, page_id):
                        retired = await self._retire_expired_quarantined_page(
                            slot,
                            page_id,
                            reason="Page.getState still reports paused after the quarantine TTL.",
                            verdict="still_paused",
                        )
                    if not retired:
                        self._mark_page_quarantined(
                            slot,
                            page_id,
                            reason="Page.getState reports the page is still paused.",
                            worker_id=worker_id,
                            status="paused",
                            recheck_verdict=True,
                        )
                else:
                    self._clear_page_quarantine(
                        slot,
                        page_id,
                        reason="Page.getState confirmed the page is usable.",
                    )
                    self._mark_page_fresh(slot, page_id)
            except Exception as exc:
                if page_fleet_id:
                    note_fleet_timeout(page_fleet_id, exc)
                error_text = str(exc)[:240]
                quarantine = slot.page_quarantine.get(page_id)
                was_quarantined = isinstance(quarantine, dict)
                # Two independent questions. The paused-error text decides
                # whether a page enters quarantine in the first place; whether
                # a page is ALREADY quarantined decides whether this pass is a
                # failed re-check. Gating the re-check on the error text too
                # would mean a quarantined page whose Page.getState keeps
                # timing out (a plain transport error, no "paused" anywhere in
                # it) never accrues a failure and so never reaches retirement —
                # the exact indefinite quarantine the TTL exists to end.
                if was_quarantined or _text_indicates_paused_error(error_text):
                    # The re-check produced no verdict — only a bounded
                    # tolerance for verdict-less passes before an already
                    # expired quarantine is retired anyway.
                    retired = False
                    if was_quarantined and self._quarantine_expired(slot, page_id):
                        failures = (optional_int(
                            quarantine.get("recheckFailures"), 0
                        ) or 0) + 1
                        quarantine["recheckFailures"] = failures
                        max_failures = int(getattr(
                            self.runtime.harness,
                            "page_quarantine_recheck_max_failures",
                            2,
                        ))
                        if failures > max_failures:
                            retired = await self._retire_expired_quarantined_page(
                                slot,
                                page_id,
                                reason=error_text,
                                verdict="recheck_failed",
                            )
                    if not retired:
                        self._mark_page_quarantined(
                            slot,
                            page_id,
                            reason=error_text,
                            worker_id=worker_id,
                            # A failed re-check is not evidence about WHY the
                            # page was quarantined, so it must not relabel it.
                            status=str(
                                quarantine.get("status") or "paused"
                            ) if was_quarantined else "paused",
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
                    "fleetTimeoutCounts": fleet_timeout_counts,
                    "unavailableFleetIds": sorted(unavailable_fleets),
                },
            )
        if required and required in unavailable_fleets:
            raise ABCPTransportError(
                f"-32012 Fleet open timeout for required fleet {required}; "
                "retry the same phase id after the acquisition cooldown"
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
                "A live worker holds a persistent page lease. Another worker's"
                " call to that page is rejected with page_busy; different pages"
                " in the assigned fleet may run concurrently."
                if getattr(
                    self.runtime.harness,
                    "same_fleet_multiworker_enabled",
                    False,
                )
                else "Do not issue concurrent calls targeting the same page."
            ),
            "During login/CAPTCHA resolution the fleet-wide auth barrier pauses every non-resolver worker.",
            "After any Page.switchTo, Page.create, Page.navigate, Page.reload, or Page.go, refresh Page.getState and DOM.getAXTree before targeting elements.",
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
                    "This assignment reuses only the browser connection. Begin"
                    " on a fresh page; Page.list may also reveal a result tab"
                    " opened by your action. A same-fleet row is usable only"
                    " when claimable=true and quarantined=false."
                ),
                (
                    "Use Page.create for a fresh task page. If an action opens a"
                    " new tab, call Page.list and address its pageId on first use;"
                    " the harness atomically claims it."
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
        pinned_page_id = (
            self.pinned_browser_context.page_id
            if self.pinned_browser_context is not None
            else ""
        )
        resume_page_id = (
            self.resume_browser_hint.page_id
            if (
                self.resume_browser_hint is not None
                and assignment.assignment_reason == "resume_browser_hint"
                and assignment.page_policy == "existing"
            )
            else ""
        )
        return {
            str(page_id): str(page.get("fleetId") or "")
            for page_id, page in slot.page_registry.items()
            if (
                str(page_id).strip()
                and str(page.get("fleetId") or "") in allowed_fleets
                and (not pinned_page_id or str(page_id) == pinned_page_id)
                and (not resume_page_id or str(page_id) == resume_page_id)
                and not _page_hidden_from_reuse(slot, page)
                and not self.page_lease_manager.owner_for(str(page_id))
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
        recheck_verdict: bool = False,
    ) -> None:
        page_id = str(page_id or "").strip()
        if not page_id:
            return
        now = time.time()
        existing = slot.page_quarantine.get(page_id)
        existing = existing if isinstance(existing, dict) else {}
        # `quarantinedAt` is the FIRST time this page was quarantined, and it
        # deliberately survives re-marking. The registry sync re-runs this call
        # on every pass for as long as Page.getState keeps reporting `paused`,
        # so refreshing the timestamp here would hold the TTL permanently in
        # the future and the retirement path below could never fire — exactly
        # the leak it exists to close. `lastQuarantinedAt` carries the
        # per-observation time for anyone who needs it.
        first_at = existing.get("quarantinedAt")
        quarantine = {
            "pageId": page_id,
            "status": status or "quarantined",
            "reason": str(reason or "")[:300],
            "workerId": str(worker_id or "")[:120],
            "phaseId": str(phase_id or "")[:120],
            "quarantinedAt": (
                float(first_at) if isinstance(first_at, (int, float)) else now
            ),
            "lastQuarantinedAt": now,
            # A pass that produced an authoritative verdict clears the
            # verdict-less streak: the tolerance exists for re-checks that
            # could not answer, not for answers we did not like.
            "recheckFailures": 0 if recheck_verdict else (
                optional_int(existing.get("recheckFailures"), 0) or 0
            ),
            "doNotUse": True,
        }
        slot.page_quarantine[page_id] = quarantine
        self.page_lease_manager.quarantine_page(page_id)
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

    def _quarantine_expired(self, slot: BrowserAgentSlot, page_id: str) -> bool:
        """Has this page been quarantined longer than the configured TTL?"""
        ttl = float(
            getattr(self.runtime.harness, "page_quarantine_ttl_seconds", 300.0)
            or 0.0
        )
        if ttl <= 0.0:
            return False
        quarantine = slot.page_quarantine.get(page_id)
        if not isinstance(quarantine, dict):
            return False
        first_at = quarantine.get("quarantinedAt")
        if not isinstance(first_at, (int, float)):
            return False
        return (time.time() - float(first_at)) > ttl

    async def _retire_expired_quarantined_page(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        *,
        reason: str,
        verdict: str,
    ) -> bool:
        """Close a page whose quarantine outlived the TTL. True if retired.

        Only reached after a re-check already said the page is still unusable,
        so this is not a timer guessing that the challenge expired — closing is
        the same remedy `_close_deadlocked_page` applies when the platform will
        not clear a pause flag: give up on this page so a fresh one can be
        created, rather than leaving it open and assignable to nobody.

        Clearing the quarantine entry is safe precisely BECAUSE the page is
        gone: the entry exists to keep workers off a live-but-unusable page.
        If the close fails the page still exists, so the quarantine stays.
        """
        closed = False
        close_error = ""
        client = getattr(slot, "client", None)
        if client is not None:
            try:
                close_response = await client.call("Page.close", {
                    "pageId": page_id,
                    "purpose": (
                        "Retire a page whose quarantine outlived its TTL so the"
                        " fleet can reclaim the slot."
                    ),
                })
                # "Did not raise" is NOT "closed", and neither is "the call
                # succeeded". ABCPClient only raises on a JSON-RPC
                # {error:{...}} envelope, so a domain-level failure arrives as
                # an ordinary response with a negative observation; and a
                # generically-successful envelope still says nothing about THIS
                # page — `classify_call_outcome` alone accepts an observation-
                # only failure, and even a receipt announcing that some OTHER
                # page was closed. Discharging inventory needs the registered
                # Page.close evidence (data.closed is True AND data.pageId
                # matches), which is exactly what the grant layer encodes.
                # Getting this wrong is fail-open: the quarantine would be
                # cleared on a page that is still open and still unusable,
                # strictly worse than the leak this method exists to close.
                decision = evaluate_grant(
                    kind="inventory_discharge_page_close",
                    method="Page.close",
                    result={"response": close_response},
                    page_id=page_id,
                )
                closed = decision.allowed
                if not closed:
                    close_error = str(
                        decision.reason or "close not acknowledged"
                    )[:240]
            except Exception as exc:  # noqa: BLE001 - retirement is best effort
                close_error = str(exc)[:240]
        self.logger.write("spawner.slot.page_quarantine_retired", {
            "slotId": slot.slot_id,
            "pageId": page_id,
            "verdict": verdict,
            "reason": str(reason or "")[:300],
            "closed": closed,
            "error": close_error or None,
            "ttlSeconds": float(
                getattr(self.runtime.harness, "page_quarantine_ttl_seconds", 300.0)
            ),
        })
        if not closed:
            return False
        slot.page_registry.pop(page_id, None)
        self._clear_page_quarantine(
            slot,
            page_id,
            reason="Page retired after its quarantine outlived the TTL.",
        )
        return True

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
        self.page_lease_manager.clear_page_quarantine(page_id)
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
        self._mark_slot_idle(slot, worker_id=worker_id)

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
        assignment = self.fleet_coordinator.assignment_for_worker(worker_id)
        if assignment is not None:
            try:
                task_pages = self._task_browser_page_ids.setdefault(
                    assignment.fleet_id, set()
                )
                for item in trace or []:
                    if not isinstance(item, dict) or item.get("type") != "browser_call":
                        continue
                    method = str(item.get("method") or "")
                    page_id = str(extract_page_id_from_values(
                        item.get("params"), item.get("result")
                    ) or "").strip()
                    if not page_id:
                        continue
                    if method == "Page.close":
                        task_pages.discard(page_id)
                        continue
                    if method == "Page.list":
                        continue
                    page = slot.page_registry.get(page_id)
                    if (
                        isinstance(page, dict)
                        and str(page.get("fleetId") or "")
                        == assignment.fleet_id
                    ):
                        task_pages.add(page_id)
                self._persist_task_browser_context(
                    slot,
                    assignment,
                    phase_id=phase_id,
                )
            except Exception as exc:
                self.logger.write(
                    "spawner.browser_context.persist_failed",
                    {
                        "workerId": worker_id,
                        "fleetId": assignment.fleet_id,
                        "stage": "worker_result",
                        "error": str(exc)[:500],
                    },
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
                and (
                    self.pinned_browser_context is None
                    or not self.pinned_browser_context.page_id
                    or str(page.get("pageId") or "")
                    == self.pinned_browser_context.page_id
                )
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
        if not workflow_execution_enabled(self.runtime):
            return None
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

    def _record_row_ledger(
        self,
        harness: Any,
        *,
        trace_summary: JsonDict,
        worker_contract: JsonDict,
        phase: JsonDict,
        validation: JsonDict,
        worker_id: str,
        phase_id: str,
    ) -> List[JsonDict]:
        """Persist what happened to each assigned row, per field, from receipts.

        The Lead may narrate a run; it may not decide why a row came back
        empty. Without this record one row's login modal explains three rows'
        missing data and nothing in the system disagrees.
        """
        try:
            expected = phase.get("expected_artifact")
            expected = expected if isinstance(expected, dict) else {}
            identity_fields = _cohort_identity_fields(worker_contract, phase)
            fields = field_names_from_specs(
                expected.get("required_fields") or expected.get("fields") or []
            )
            if not fields:
                return []
            rows = _validated_rows_for_ledger(validation)
            row_keys = [
                key for key in (
                    row_identity(row, identity_fields) for row in rows
                ) if key
            ]
            # The budget that ran out is the WORKER's, not the global default:
            # a phase may override max_steps, and reading the default instead
            # means a worker that stopped at its own 15-step cap is compared
            # against 40, reports budgetExhausted=False, and every row it never
            # opened loses the one cause that explains it. That substitution is
            # the whole defect this ledger was built to prevent.
            worker_harness = getattr(getattr(harness, "runtime", None), "harness", None)
            max_steps = optional_int(
                getattr(worker_harness, "max_steps", None),
                0,
            ) or int(self.runtime.harness.worker_max_steps or 0)
            steps = int(trace_summary.get("steps") or 0)
            ledger = derive_row_ledger(
                rows,
                fields=fields,
                identity_fields=identity_fields,
                allow_empty_with_outcome=_allowance_from_validators(
                    phase.get("validators")
                ),
                row_facts=derive_row_facts(
                    getattr(harness, "trace", []) or [],
                    row_keys=row_keys,
                    budget_exhausted=bool(max_steps and steps >= max_steps),
                ),
            )
        except Exception as exc:  # a ledger defect must never fail a worker
            self.logger.write("row_ledger.error", {
                "workerId": worker_id, "phaseId": phase_id, "error": str(exc),
            })
            return []
        if ledger:
            self.logger.write("row_ledger.recorded", {
                "workerId": worker_id,
                "phaseId": phase_id,
                "rows": ledger,
            })
        return ledger

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
        if not workflow_execution_enabled(self.runtime):
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
        readiness_receipt: Optional[JsonDict] = None,
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
            browser_agent_model_config(worker_runtime.model, worker_runtime.worker)
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
            if readiness_receipt:
                slot_context = (
                    f"{slot_context}\n\n<fleet_readiness>\n"
                    f"{json.dumps(readiness_receipt, ensure_ascii=False, indent=2)}\n"
                    "</fleet_readiness>"
                )
            effective_context = context or "(none)"
            if slot_context:
                effective_context = f"{effective_context}\n\n{slot_context}".strip()
            try:
                from harness.skill.contract import selected_skill_context
                skill_context = selected_skill_context(
                    self._get_skill_registry(),
                    worker_contract or {},
                    workflow_enabled=workflow_execution_enabled(worker_runtime),
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
            worker_browser = _TaskContextTrackingBrowserClient(
                slot.client,
                self.page_lease_manager,
                fleet_owner_client=owner_client,
                fleet_click_gate=self.fleet_click_gate,
                fleet_auth_barrier=(
                    self.fleet_auth_barrier
                    if getattr(
                        self.runtime.harness,
                        "fleet_auth_barrier_enabled",
                        False,
                    )
                    else None
                ),
                assigned_fleet_id=assignment.fleet_id if assignment else "",
                registered_agent_id=slot.agent_id,
                worker_id=worker_id,
                after_call=(
                    (
                        lambda method, params, result: (
                            self._observe_task_browser_call(
                                slot,
                                assignment,
                                method,
                                params,
                                result,
                                phase_id=phase_id,
                            )
                        )
                    )
                    if assignment is not None
                    else None
                ),
            )
            worker_logger = self.logger.bind_context(
                workerId=worker_id,
                slotId=slot.slot_id,
                agentId=slot.agent_id,
                phaseId=str(phase_id or ""),
            )
            harness = self.browser_agent_factory(
                provider,
                worker_browser,
                worker_runtime,
                worker_logger,
            )
            try:
                harness_ref = weakref.ref(harness)
            except TypeError:
                worker_browser.set_click_settlement_classifier(
                    lambda method, params, current=harness: (
                        _fresh_click_settlement_class(
                            current, method, params
                        )
                    )
                )
                worker_browser.set_workflow_hitl_settlement_handler(
                    lambda page_id, current=harness: (
                        _verified_workflow_hitl_settlement(
                            current,
                            page_id,
                        )
                    )
                )
            else:
                worker_browser.set_click_settlement_classifier(
                    lambda method, params, ref=harness_ref: (
                        _fresh_click_settlement_class(ref(), method, params)
                    )
                )
                worker_browser.set_workflow_hitl_settlement_handler(
                    lambda page_id, ref=harness_ref: (
                        _verified_workflow_hitl_settlement(
                            ref(),
                            page_id,
                        )
                    )
                )
            harness.worker_contract = worker_contract or {}
            batch_rows = (
                harness.worker_contract.get("batch_rows")
                if isinstance(harness.worker_contract, dict)
                else None
            )
            progress = getattr(harness, "progress", None)
            if progress is not None and hasattr(
                progress, "configure_history_navigation_credits"
            ):
                progress.configure_history_navigation_credits(
                    len(batch_rows) if isinstance(batch_rows, list) else 0
                )
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
            self.page_lease_manager.seed_worker_pages(worker_id, page_bindings)
            harness.fleet_page_fleet_ids = {}
            harness.pinned_browser_context = (
                self.pinned_browser_context.to_dict()
                if self.pinned_browser_context is not None
                else {}
            )
            harness.pinned_page_id = (
                self.pinned_browser_context.page_id
                if self.pinned_browser_context is not None
                else ""
            )
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
            harness.slot_id = slot.slot_id
            harness.phase_id = phase_id
            harness.page_lease_manager = self.page_lease_manager
            harness.fleet_click_gate = self.fleet_click_gate
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
            completeness_tracker = getattr(
                harness, "content_completeness_tracker", None
            )
            if completeness_tracker is not None and hasattr(
                completeness_tracker, "summaries"
            ):
                trace_summary["contentCompletenessPages"] = (
                    completeness_tracker.summaries()
                )
            # "The worker never did X" and "this phase cannot do X" are
            # different facts, and only one of them is in the trace. In task
            # a608b5e7 a worker spent its whole budget on comments without
            # calling DOM.getImg once; the Lead read the absence as a
            # capability limit and replanned the image work into a
            # file_download phase, away from the page that had the images.
            advertised = getattr(harness, "capability_methods", None)
            if isinstance(advertised, (set, frozenset)):
                called = set(trace_summary.get("methods") or {})
                trace_summary["advertisedMethodsNeverCalled"] = sorted(
                    str(method) for method in advertised if method not in called
                )
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
                logger=self.logger,
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
                    counterevidence = feedback_classification.get(
                        "counterevidence"
                    )
                    if isinstance(counterevidence, dict):
                        self.logger.write(
                            "semantic_terminal.counterevidence",
                            {
                                "workerId": worker_id,
                                "phaseId": phase_id,
                                "category": feedback_classification.get(
                                    "category"
                                ),
                                **counterevidence,
                            },
                        )
            # Completeness observations remain model-visible evidence, but do
            # not override the artifact contract mechanically.
            contract_validation = json.loads(json.dumps(artifact_validation))
            content_completeness_validation: JsonDict = {
                "status": "observed",
                "classification": None,
            }
            validated_status = (
                "validated_done"
                if artifact_validation.get("status") == "done"
                else "validation_failed"
                if artifact_validation.get("status") == "failed"
                else "not_validated"
            )
            fast_path_assessment = assess_fast_path_candidate(
                trace=getattr(harness, "trace", []) or [],
                trace_summary=trace_summary,
                worker_contract=worker_contract or {},
                phase=phase or {},
                validation=artifact_validation,
            )
            row_ledger = self._record_row_ledger(
                harness,
                trace_summary=trace_summary,
                worker_contract=worker_contract or {},
                phase=phase or {},
                validation=artifact_validation,
                worker_id=worker_id,
                phase_id=phase_id,
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
            captcha_receipts = list(
                getattr(harness, "captcha_autosolve_receipts", []) or []
            )
            vl_cleared_statuses = {
                "solved", "cleared", "not_a_challenge", "already_cleared",
            }
            hitl_request_count = sum(
                1 for item in (getattr(harness, "trace", []) or [])
                if isinstance(item, dict)
                and item.get("type") == "browser_call"
                and item.get("method") == "Hitl.requestPause"
            )
            challenge_receipt = {
                "observed": bool(
                    trace_summary.get("suspectedChallengePages")
                    or captcha_receipts
                    or hitl_request_count
                ),
                "observedCount": max(
                    len(trace_summary.get("suspectedChallengePages") or []),
                    len(captcha_receipts),
                    int(bool(hitl_request_count)),
                ),
                "vlSolveAttempts": sum(
                    len(item.get("attempts") or [])
                    for item in captcha_receipts if isinstance(item, dict)
                ),
                "vlSolvedCount": sum(
                    1 for item in captcha_receipts
                    if isinstance(item, dict)
                    and str(item.get("status") or "") in vl_cleared_statuses
                ),
                "hitlRequests": hitl_request_count,
                "hitlResumes": int(bool(
                    getattr(diagnostics, "hitl_resumed_observed", False)
                )),
            }
            challenge_receipt["unresolved"] = bool(
                challenge_receipt["observed"]
                and not challenge_receipt["vlSolvedCount"]
                and not challenge_receipt["hitlResumes"]
            )
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
                "contractValidation": contract_validation,
                "contentCompletenessValidation": content_completeness_validation,
                "finalArtifactValidation": artifact_validation,
                "tracePath": trace_path,
                "traceSummary": trace_summary,
                "progressSnapshot": progress_snapshot,
                "progressObservationCount": progress_snapshot.get(
                    "observationCount",
                    0,
                ),
                "offloadedFiles": offloaded_files,
                "diagnostics": diagnostics.to_log_payload()
                if diagnostics is not None
                else {},
                "fastPathAssessment": fast_path_assessment,
                "downloadOperationReceipts": list(
                    getattr(harness, "download_operation_receipts", {}).values()
                ),
                "challengeReceipt": challenge_receipt,
                # Per-row outcome and cause, derived from receipts. The Lead
                # reads this instead of inferring one explanation for every row
                # from the worker's prose.
                "rowLedger": row_ledger,
            }
            receipt_candidate = fast_path_assessment.get("candidate")
            if isinstance(receipt_candidate, dict):
                result["fastPathReceiptCandidate"] = receipt_candidate
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
            self._mark_slot_idle(slot, worker_id=worker_id)
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
            self._mark_slot_idle(slot, worker_id=worker_id)

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
        handoff = build_worker_handoff_projection(
            result,
            original_goal=str((phase or {}).get("objective") or ""),
        )
        if isinstance(handoff, dict):
            state_before_result = load_task_state(self.logger)
            phase_state = (
                (state_before_result.get("phases") or {}).get(str(phase_id or ""))
                if isinstance(state_before_result.get("phases"), dict)
                else None
            )
            prior_attempts = (
                phase_state.get("attempts")
                if isinstance(phase_state, dict)
                and isinstance(phase_state.get("attempts"), list)
                else []
            )
            prior_rows = [
                int((item.get("attemptDigest") or {}).get("rowCount") or 0)
                for item in prior_attempts
                if isinstance(item, dict)
                and item.get("workerId") != worker_id
                and isinstance(item.get("attemptDigest"), dict)
            ]
            current_rows = int(attempt_digest.get("rowCount") or 0)
            receipts = handoff.setdefault("rawReceipts", {})
            receipts["attemptCount"] = len(prior_attempts)
            receipts["previousRowCount"] = prior_rows[-1] if prior_rows else None
            receipts["rowCountDelta"] = (
                current_rows - prior_rows[-1] if prior_rows else current_rows
            )
            handoff.setdefault("evidencePaths", {})[
                "strategyAttempts"
            ] = str(self.logger.task_dir / "strategy_attempts.jsonl")
            attempt_digest["handoff"] = handoff
        result["attemptDigest"] = attempt_digest
        mark_phase_result(
            self.logger,
            phase_id=phase_id,
            worker_id=worker_id,
            validation=result.get("artifactValidation"),
            # Lifecycle truth must use the worker's raw outcome.  The derived
            # validatedStatus is a separate artifact dimension and must never
            # turn a partial worker into a completed phase.
            result_status=str(result.get("status") or "unknown"),
            attempt_digest=attempt_digest,
            phase=phase,
            worker_contract=worker_contract,
        )
        checkpoint = record_replan_checkpoint(
            self.logger,
            phase=phase,
            worker_contract=worker_contract,
            worker_id=worker_id,
            fast_path_assessment=(
                result.get("fastPathAssessment")
                if isinstance(result.get("fastPathAssessment"), dict)
                else None
            ),
        )
        if isinstance(checkpoint, dict):
            result["replanCheckpoint"] = checkpoint
            checkpoint_assessment = checkpoint.get("fastPathAssessment")
            if isinstance(checkpoint_assessment, dict):
                result["fastPathAssessment"] = checkpoint_assessment
                receipt_candidate = checkpoint_assessment.get("candidate")
                if isinstance(receipt_candidate, dict):
                    result["fastPathReceiptCandidate"] = receipt_candidate
                else:
                    result.pop("fastPathReceiptCandidate", None)
        append_strategy_attempt(
            logger=self.logger,
            worker_contract=worker_contract or {},
            result=result,
        )
        if slot.status == "running":
            self._mark_slot_idle(slot, worker_id=worker_id)
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
            row_ledger=(
                result.get("rowLedger")
                if isinstance(result.get("rowLedger"), list)
                else None
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
            logger=self.logger,
        )
        result["resultLevels"] = levels
        result["workerResultProtocol"] = "L1/L2/L3"
        return result

    def _write_worker_trace(self, worker_id: str, trace: List[JsonDict]) -> str:
        """Persist one worker's trace and return where a reader can find it.

        This used to truncate the file, which silently destroyed the earlier
        trace whenever a resumed run reissued the same worker id - the ids come
        from a per-run counter, so browser-001 recurs on every resume. Writes
        now append, and the database backend scopes them by run so the two
        attempts stay separable rather than one overwriting the other.
        """

        safe_worker = safe_path_component(worker_id)
        storage, task_id = storage_for_logger(self.logger)
        storage.append_worker_trace(
            task_id=task_id,
            run_id=str(getattr(self.logger, "run_id", "") or ""),
            worker_id=safe_worker,
            entries=trace,
        )
        return str((task_subdir(self.logger, "traces") / f"{safe_worker}.jsonl").resolve())

    def _summarize_worker_trace(self, trace: List[JsonDict]) -> JsonDict:
        method_counts: Dict[str, int] = {}
        errors: List[str] = []
        page_ids: Set[str] = set()
        offloaded: List[str] = []
        progress_observations: List[JsonDict] = []
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
            elif item.get("type") == "progress_observation":
                result = item.get("result")
                if isinstance(result, dict):
                    progress_observations.append({
                        "source": str(result.get("source") or "")[:120],
                        "reasonObserved": str(
                            result.get("reasonObserved") or ""
                        )[:120],
                        "tool": str(result.get("tool") or "")[:120],
                        **{
                            key: result[key]
                            for key in (
                                "turnsSinceArtifactProgress",
                                "toolCalls",
                                "localFsWithoutExtraction",
                                "localFsStreak",
                                "diagnosticUses",
                                "diagnosticLimit",
                            )
                            if key in result
                        },
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
        loop_nudge_count = len(loop_nudges)
        summary = {
            "steps": max_step,
            "traceEvents": len(trace),
            "toolCalls": tool_calls,
            "methods": method_counts,
            "pageIds": sorted(page_ids),
            "errors": errors[:10],
            "progressObservations": progress_observations[-5:],
            "progressObservationCount": len(progress_observations),
            "loopNudges": loop_nudges[-5:],
            "loopNudgeCount": loop_nudge_count,
            "latestPageStats": page_stats_events[-1] if page_stats_events else None,
            "pageStatsCount": len(page_stats_events),
            "snapshotDiffs": snapshot_diffs[-5:],
            "snapshotDiffCount": len(snapshot_diffs),
            "offloadedFiles": sorted(set(offloaded))[:100],
        }
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
    collection_contract = _classification_from_collection_contract_preflight(trace)
    if collection_contract is not None:
        return collection_contract
    browser_call_classification = _classification_from_browser_call(trace)
    if browser_call_classification is not None:
        return browser_call_classification
    return _classification_from_final_answer(
        answer,
        persisted_artifacts=persisted_artifacts,
        traversal=_page_traversal_evidence(trace),
    )


def _classification_from_collection_contract_preflight(
    trace: List[JsonDict],
) -> Optional[JsonDict]:
    """Recover immutable-plan defects mechanically from collect_items trace."""
    for item in reversed(trace or []):
        if not isinstance(item, dict) or item.get("type") != "collect_items":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        classification = result.get("classification")
        if not isinstance(classification, dict):
            continue
        if str(classification.get("category") or "").strip() != (
            COLLECTION_CONTRACT_REPLAN_REQUIRED
        ):
            continue
        recovered = dict(classification)
        recovered.setdefault(
            "hint",
            "LeadAgent must replan expected_artifact with a nested array field.",
        )
        recovered["source"] = "collect_items.contractPreflight"
        return recovered
    return None


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
                " found."
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
        recovered["source"] = "contract_violation"
        return recovered
    return None


# Counterevidence for semantic-terminal blockers reads two things the claim
# cannot vouch for itself: whether the blocker carries reason text, and whether
# its evidenceArtifacts name a savedPath the harness itself recorded via
# record_extraction this run. The ledger is the source — never the filesystem
# and never the model's claim — so a fabricated path is visible as one. None of
# it changes the claim; it travels beside it.


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


def _cohort_identity_fields(
    worker_contract: JsonDict, phase: Optional[JsonDict] = None,
) -> List[str]:
    """The field(s) that name a row, from whichever shape this contract uses.

    Delegates to the single resolver in harness.row_ledger: this lookup had
    already been re-implemented three times with a different set of shapes
    each time, and a missed shape is silent — no identity, every rowKey None,
    per-row attribution quietly gone.
    """
    return identity_fields_from_contract(worker_contract, phase)


def _validated_rows_for_ledger(validation: JsonDict) -> List[JsonDict]:
    paths = validation.get("validExtractionArtifacts")
    if not isinstance(paths, list) or not paths:
        paths = validation.get("allExtractionArtifacts")
    rows: List[JsonDict] = []
    for raw_path in paths if isinstance(paths, list) else []:
        try:
            payload = json.loads(Path(str(raw_path)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        for row in (payload.get("rows") if isinstance(payload, dict) else None) or []:
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _allowance_from_validators(validators: Any) -> JsonDict:
    """Merge every emptiable-field declaration on the phase.

    Dedup only merges validators that share a semantic signature (type+fields),
    so two field_nonempty validators covering different fields both survive to
    here. Returning the first one dropped the second's allowance and pinned the
    phase on a field it had explicitly declared emptiable. Outcomes for a field
    named twice are unioned rather than overwritten.
    """
    merged: JsonDict = {}
    for validator in validators if isinstance(validators, list) else []:
        if (
            not isinstance(validator, dict)
            or str(validator.get("type") or "") != "field_nonempty"
            or not isinstance(validator.get("allow_empty_with_outcome"), dict)
        ):
            continue
        for field, outcomes in validator["allow_empty_with_outcome"].items():
            values = outcomes if isinstance(outcomes, list) else [outcomes]
            existing = merged.setdefault(str(field), [])
            for outcome in values:
                text = str(outcome or "").strip()
                if text and text not in existing:
                    existing.append(text)
    return merged


def _scroll_receipt_data(result: JsonDict) -> Optional[JsonDict]:
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    return data if isinstance(data, dict) else None


def _scroll_was_state_probe(result: JsonDict) -> bool:
    """True when the receipt says no wheel input was dispatched at all.

    `amount: 0` reads the scroll state without moving anything, so such a call
    is neither a traversal nor a failed traversal — counting it either way
    corrupts the ledger that guards `target_absent`.
    """
    data = _scroll_receipt_data(result)
    if data is None:
        return False
    return str(data.get("completedReason") or "") == "amount-zero"


def _axis_magnitude(value: Any) -> Optional[float]:
    """Largest absolute axis component of a `{x, y}` delta, or None."""
    if not isinstance(value, dict):
        return None
    magnitude: Optional[float] = None
    for axis in ("x", "y"):
        raw = value.get(axis)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        magnitude = max(magnitude or 0.0, abs(float(raw)))
    return magnitude


def _scroll_delta_applied(result: JsonDict) -> Optional[float]:
    """Pixels an `Input.scroll` actually moved, or None when unreported.

    None and 0 must stay distinct: None means this platform build ships no
    scroll receipt, while 0 is a positive report that the page did not move.

    Two receipt shapes are accepted on purpose. Current builds return
    `AbcpScrollActionResult` with a `totalDelta {x, y}` plus per-surface
    `layers[].delta`; older builds returned a scalar `deltaApplied`. Reading
    only one of them silently degrades `scrollEffectEvidence` to "unavailable"
    on the other build, which is exactly how a wheel event that travelled zero
    pixels gets to look like a real traversal.
    """
    data = _scroll_receipt_data(result)
    if data is None:
        return None

    total = _axis_magnitude(data.get("totalDelta"))
    if total is not None:
        return total

    layers = data.get("layers")
    if isinstance(layers, list):
        magnitudes = [
            magnitude
            for layer in layers
            if isinstance(layer, dict)
            and (magnitude := _axis_magnitude(layer.get("delta"))) is not None
        ]
        if magnitudes:
            return max(magnitudes)

    if "deltaApplied" not in data:
        return None
    raw = data.get("deltaApplied")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return abs(float(raw))


def _page_traversal_evidence(trace: Optional[List[JsonDict]]) -> JsonDict:
    """Did this worker ever move past the first screenful?

    A `target_absent` report can be perfectly self-consistent and still be
    wrong: the upstream batch says `[]`, the worker enumerates what it can see,
    a screenshot confirms the target is not there, confidence is high — and the
    viewport never moved, so every one of those observations describes the same
    screenful. No judge can catch that, because nothing in the report is false;
    the missing piece is a mechanical fact about what WE did, which only our
    own ledger holds.

    Two signals count, both decidable from the trace:
      * an `Input.scroll` that moved the page — we asked, and the page obeyed
      * a collection exhaustion proof — we enumerated a container to its end

    "Moved the page" needs the platform's scroll receipt, because a wheel event
    dispatched into a page that ignores it still returns a success envelope: a
    worker can scroll three times, travel zero pixels, and look fully traversed.
    When `deltaApplied` is present it decides the question; builds that do not
    report it leave `scrollEffectEvidence="unavailable"` and keep the older,
    weaker reading, so this gate never fabricates evidence in either direction.

    The two branches below verify success DIFFERENTLY on purpose, and the
    asymmetry is load-bearing rather than an oversight. `classify_call_outcome`
    reads the native ABCP envelope (`result.response` + executionId/observation
    /data) and is fail-closed: absent that envelope it reports FAILED. A
    composite such as collect_items returns a FLAT result with no `response`
    key at all, so routing the exhaustion branch through the same classifier
    would score every real proof as a failure and silently zero out traversal
    evidence for every collection-based claim. The composite instead proves
    itself: its failure paths return `{"status": "failed", ...}` and never
    construct an exhaustionEvidence block, so a non-empty `kind` is already
    positive evidence that the enumeration ran.
    """
    scrolls = 0
    scrolls_without_effect = 0
    scroll_effect_evidence = "unavailable"
    exhaustion_proofs = 0
    for item in trace or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        if str(item.get("method") or "") == "Input.scroll":
            if not classify_call_outcome(result).succeeded:
                continue
            if _scroll_was_state_probe(result):
                # A zero-amount state read dispatched no input; it is neither
                # traversal nor a failure to traverse.
                continue
            delta = _scroll_delta_applied(result)
            if delta is None:
                # No scroll receipt in this ABCP build. The success envelope
                # proves only that the wheel event was dispatched, so keep
                # counting it and record that the effect is unproven rather
                # than inventing evidence either way.
                scrolls += 1
                continue
            scroll_effect_evidence = "receipt"
            if delta > 0:
                scrolls += 1
            else:
                scrolls_without_effect += 1
            continue
        evidence = result.get("exhaustionEvidence")
        if isinstance(evidence, dict) and str(evidence.get("kind") or "").strip():
            exhaustion_proofs += 1
    return {
        "scrolls": scrolls,
        "scrollsWithoutEffect": scrolls_without_effect,
        "scrollEffectEvidence": scroll_effect_evidence,
        "exhaustionProofs": exhaustion_proofs,
        "traversed": bool(scrolls or exhaustion_proofs),
    }


def _semantic_terminal_counterevidence(
    category: str,
    blocker: JsonDict,
    classification: JsonDict,
    persisted_artifacts: Optional[List[str]],
    traversal: Optional[JsonDict] = None,
) -> Optional[JsonDict]:
    """Return what our own receipts say against a semantic-terminal claim.

    The worker's category is its own to declare and is never rewritten here:
    whether the evidence suffices to prove absence is a semantic reading, and
    the model that gathered the evidence is the one to make it. What this
    returns is the other half of the record. A `target_absent` report can be
    perfectly self-consistent and still be wrong — the upstream batch says
    `[]`, the worker enumerates what it can see, a screenshot agrees, and the
    viewport never moved, so every observation describes the same screenful.
    Nothing in the report is false, so no reader can catch it from the report
    alone; only our ledger holds the missing fact.

    Returns None when the ledger has nothing to add.
    """
    reason_text = str(
        blocker.get("reason")
        or classification.get("reason")
        or blocker.get("message")
        or blocker.get("detail")
        or ""
    ).strip()
    facts: JsonDict = {
        "observation": "semantic_terminal_counterevidence",
        "claimedCategory": category,
        "reasonTextLength": len(reason_text),
        "note": (
            "Attributed facts from this run's own receipts, not a verdict on"
            " the claim."
        ),
    }
    if not reason_text:
        return {**facts, "findings": ["blocker carries no reason text"]}
    if (
        category == "target_absent"
        and isinstance(traversal, dict)
        and not traversal.get("traversed")
    ):
        # Checked ahead of the artifact ledger on purpose: a persisted artifact
        # proves we saved what we saw, never that we looked past the fold.
        try:
            ignored_scrolls = int(traversal.get("scrollsWithoutEffect") or 0)
        except (TypeError, ValueError):
            ignored_scrolls = 0
        finding = (
            f"{ignored_scrolls} scroll(s) were dispatched and the page did not"
            " move; no collection was enumerated to exhaustion"
            if ignored_scrolls
            # Distinguishable from the above only with a scroll receipt, and
            # the difference matters: one says the page refused, the other
            # says we never asked.
            else (
                "the page was never scrolled and no collection was enumerated"
                " to exhaustion"
            )
        )
        return {
            **facts,
            "findings": [finding],
            "pageTraversal": dict(traversal),
        }
    evidence_paths = _blocker_evidence_paths(blocker, classification)
    ledger = {
        os.path.normpath(str(path).strip())
        for path in (persisted_artifacts or [])
        if str(path).strip()
    }
    matched = [
        path for path in evidence_paths if os.path.normpath(path) in ledger
    ]
    if matched:
        if _only_visual_check_evidence(matched):
            # The artifact is real and ledger-bound, and still proves nothing:
            # it holds a model's reading of a screenshot. Task 5324506f turned
            # one page's overlay into "the site requires login for reviews"
            # on exactly this evidence.
            return {
                **facts,
                "findings": [
                    "every cited artifact is a visual reality check, which"
                    " records a model's reading of one screenshot rather than"
                    " a measurement"
                ],
                "citedEvidenceArtifacts": list(evidence_paths),
                "ledgerMatchedArtifacts": list(matched),
            }
        return None
    if category == "instruction_infeasible":
        # Infeasibility often has nothing extractable to persist — the site can
        # lack the requested concept entirely — which makes a missing artifact
        # unremarkable rather than damning. That is a reading of the claim, and
        # it used to be made here by measuring the reason text against a
        # 40-character floor: 40 characters "stood on its own" and 39 did not.
        # The length is a fact and travels as one; what it is worth is not this
        # function's call.
        return {
            **facts,
            "findings": [
                "no evidenceArtifacts entry matches a record_extraction"
                " savedPath from this run"
            ],
            "citedEvidenceArtifacts": list(evidence_paths),
        }
    return {
        **facts,
        "findings": [
            "no evidenceArtifacts listed"
            if not evidence_paths
            else (
                "no evidenceArtifacts entry matches a record_extraction"
                " savedPath from this run"
            )
        ],
        "citedEvidenceArtifacts": list(evidence_paths),
    }


def _only_visual_check_evidence(paths: List[str]) -> bool:
    """True when every cited artifact is a visual reality check and nothing else.

    Deliberately does NOT read the row's own `evidenceGrade`. That field is
    written into an artifact, and artifacts are written by record_extraction,
    which a worker can call with any rows it likes — so trusting the grade let
    a worker mint `{"kind": "vl_reality_check", "evidenceGrade":
    "corroborating"}` and walk past this gate. The grade is not needed anyway:
    `evidence_grade` returns mayTerminate=False in every mode, so a visual
    verdict is never sufficient on its own regardless of how well the model
    scores. Precision buys corroboration, not authority to end work.

    Fails OPEN on an unreadable or unrecognized artifact: this function exists
    to catch a specific, self-labelled artifact kind, and a file we cannot
    parse must not become a reason to reject a blocker that may be perfectly
    well evidenced.
    """
    if not paths:
        return False
    for raw_path in paths:
        try:
            payload = json.loads(Path(str(raw_path)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return False
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return False
        for row in rows:
            if not isinstance(row, dict):
                return False
            if str(row.get("kind") or "") != "vl_reality_check":
                return False
    return True


def _classification_from_final_answer(
    answer: str,
    persisted_artifacts: Optional[List[str]] = None,
    traversal: Optional[JsonDict] = None,
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
            COLLECTION_CONTRACT_REPLAN_REQUIRED,
            "target_absent",
            "instruction_infeasible",
            "route_sensitive_content_suppression",
            "blocked_content_suppression",
        }:
            continue
        if category in {"target_absent", "instruction_infeasible"}:
            counterevidence = _semantic_terminal_counterevidence(
                category,
                blocker,
                classification,
                persisted_artifacts,
                traversal=traversal,
            )
            if counterevidence is not None:
                if isinstance(traversal, dict):
                    classification["pageTraversal"] = dict(traversal)
                # The claim stays the worker's own. Our receipts are appended
                # beside it so the Lead reads both and decides; rewriting the
                # category here would be this harness judging a page's
                # behaviour, which is the model's call to make.
                classification["counterevidence"] = counterevidence
                findings = "; ".join(
                    str(item) for item in counterevidence.get("findings") or []
                )
                classification.setdefault("hint", (
                    f"Worker claimed {category}. Counterevidence from this"
                    f" run's own receipts: {findings}."
                )[:500])
                classification["source"] = "final_answer.blockers"
                return classification
        # The worker's own words are its claim and travel verbatim. Where it
        # gave none, this used to substitute a per-category directive
        # ("LeadAgent should stop retrying…", "…should replan…"); that is the
        # harness picking the next move from a category name, with none of the
        # run's evidence in front of it. The category and the receipts beside
        # it are the record; reading them is the Lead's job.
        hint = (
            blocker.get("hint")
            or blocker.get("message")
            or blocker.get("reason")
        )
        if hint:
            classification.setdefault("hint", str(hint)[:500])
        if blocker.get("method"):
            classification.setdefault("method", blocker.get("method"))
        if blocker.get("task_type"):
            classification.setdefault("task_type", blocker.get("task_type"))
        if blocker.get("field"):
            classification.setdefault("field", blocker.get("field"))
        if isinstance(blocker.get("expectedShape"), dict):
            classification.setdefault("expectedShape", blocker.get("expectedShape"))
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
