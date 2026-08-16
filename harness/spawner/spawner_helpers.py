"""
harness.spawner.spawner_helpers - Module-level helpers, dataclasses and client wrappers.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from abcp_client import ABCPClient
from abcp_client import ABCPTransportError
from harness.constants import WORKER_STATUS_DONE
from harness.fleet.runtime import PageLeasedBrowserClient
from runtime_config import RuntimeConfig
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import optional_int

def _sp():
    import harness.spawner as sp

    return sp

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
