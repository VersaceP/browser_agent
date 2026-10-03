"""BrowserAgent state and role-specific coordination."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from abcp_client import ABCPClient
from harness.fleet.task_reuse import DEFAULT_RUNNING_STALE_SECONDS, FLEET_MEMORY_SCHEMA, FLEET_REUSE_POLICY_VERSION, compact_running_memory_records, parse_fleet_memory, task_text_from_memory_entry
from harness.context.compaction import estimate_image_tokens, estimate_prompt_tokens
from runtime_config import RuntimeConfig
from harness.observation.challenge_detector import ChallengeTracker
from harness.observation.content_completeness import ContentCompletenessTracker
from harness.constants import WORKER_STATUS_CONTEXT_LIMIT, WORKER_STATUS_RUNNING
from harness.diagnostics import WorkerDiagnostics, status_category
from harness.runtime.lifecycle import LifecycleContext, default_lifecycle_manager
from harness.runtime.model_config import browser_agent_model_config
from harness.observation.event_observer import BrowserEventObserver
from harness.observation.page_inventory import PageInventorySignal
from harness.observation.page_lifecycle import PageLifecycleTracker
from harness.observation.loop_nudge import ActionLoopNudge
from harness.context.offload import offload_large_response_fields, strip_image_payload
from harness.observation.page_fingerprint import PageObservationTracker
from harness.observation.progress import ProgressAccountant
from harness.capabilities.schema_loader import CapabilityBundle, load_capability_bundle
from harness.capabilities.schema_cache import global_schemas_dir
from harness.tools.tool_policy import filter_capability_methods
from harness.tools.browser_tools import AXTREE_INVALIDATING_METHODS, _invoke_result_failed
from harness.workflow.workflow_runtime import workflow_execution_enabled
from harness.tools.tool_policy import ALWAYS_FORBIDDEN_ABCP_METHODS as _BLOCKED_CAPABILITIES
from harness.utils import JsonDict, RunLogger, build_static_context_block, exception_payload, optional_int, strip_llm_hidden_fields, trim_large_strings
from llm import BaseLLMProvider
from harness.runtime.model_support import (
    CachePressureState,
    _EXTENSION_HANDOFF_HINT,
    _lifecycle_recorder_for,
    _saved_paths_from_value,
    _tool_result_image_accounting,
    update_cache_pressure_state,
)

class BrowserAgent:
    def __init__(
        self,
        provider: BaseLLMProvider,
        browser: ABCPClient,
        runtime: RuntimeConfig,
        logger: RunLogger,
    ):
        self.provider = provider
        self.browser = browser
        self.runtime = runtime
        self.effective_model_config = browser_agent_model_config(runtime)
        self.logger = logger
        self.capabilities: List[JsonDict] = []
        self.capability_methods: Set[str] = set()
        self.method_schemas: Dict[str, JsonDict] = {}
        self.methods_requiring_purpose: Set[str] = set()
        self.purpose_hints: Dict[str, str] = {}
        self.agent_guide: str = ""
        self.catalog_revision: str = ""
        self.guide_revision: str = ""
        self.artifacts: List[str] = []
        self.file_action_evidence: List[JsonDict] = []
        self.file_manifests: List[JsonDict] = []
        self.extraction_attempt_artifacts: List[str] = []
        self.trace: List[JsonDict] = []
        self.final_status = WORKER_STATUS_RUNNING
        self.diagnostics = WorkerDiagnostics()
        self.progress = ProgressAccountant()
        self.loop_nudge = ActionLoopNudge()
        self.page_observer = PageObservationTracker()
        self.challenge_tracker = ChallengeTracker()
        self.content_completeness_tracker = ContentCompletenessTracker()
        self.hitl_structural_challenges: Dict[str, JsonDict] = {}
        self.hitl_no_repause_until: float = 0.0
        self.lifecycle = default_lifecycle_manager()
        # Typed lifecycle events. Built from the logger this agent was handed,
        # so a worker's events carry the worker identity the spawner bound,
        # not whatever the payload happened to mention.
        self.lifecycle_events = _lifecycle_recorder_for(
            runtime, logger, self.trace, actor_type="browser",
        )
        self.preloaded_capability_bundle: Optional[CapabilityBundle] = None
        self.preloaded_registration: Optional[JsonDict] = None
        # Spawner-owned observability identity. These fields are injected
        # before run() and must accompany every persisted `agent.*` event so
        # concurrent workers cannot be confused by their local step numbers.
        self.worker_id = ""
        self.slot_id = ""
        self.phase_id = ""
        self.assigned_fleet_id = ""
        self.allowed_fleet_ids: Set[str] = set()
        self.allowed_page_ids: Set[str] = set()
        self.page_fleet_ids: Dict[str, str] = {}
        self.page_reuse_allowed = False
        # Trusted task-level routing input.  Unlike an ordinary page reuse
        # delegation, a pinned page must not be replaced or closed by the
        # worker model.
        self.pinned_browser_context: JsonDict = {}
        self.pinned_page_id = ""
        self.fleet_assignment_reason = ""
        self.fleet_session_key = ""
        self.fleet_is_isolated = False
        self.axtree_epoch = 0
        self.axtree_ids: Set[str] = set()
        self.axtree_page_id = ""
        self.axtree_invalidated = True
        # Monotonic serial bumped only when BrowserEventObserver applies a fresh
        # full snapshot from DOM.axTreeUpdated. _invoke_browser_method samples it
        # before runner.call so post-action pessimistic invalidation can detect a
        # same-page event that landed mid-call and avoid clobbering it (race fix).
        self.axtree_event_serial = 0
        # Page of the most recently applied DOM.axTreeUpdated; suppression is
        # gated on this matching the page held before the call (page scope).
        self.axtree_event_page_id = ""
        self.browser_call_runner = None
        self.page_lifecycle = PageLifecycleTracker()
        self.page_inventory_signal = PageInventorySignal()
        self.event_observer = BrowserEventObserver(self)
        self.recent_tool_signatures: List[str] = []
        self._cache_pressure = CachePressureState()
        self._forced_compaction_reason: Optional[str] = None
        self.standalone_browser_mode = (
            self.runtime.harness.agent_mode == "browser"
        )
        from harness.agents.browser.control import StandaloneTaskControl
        self.task_control = StandaloneTaskControl(self) if self.standalone_browser_mode else None
        self.compaction_continuity_factory = (
            self.task_control.compaction_continuity if self.task_control else None)
        self.base_max_steps = (
            0 if self.standalone_browser_mode
            else max(0, int(self.runtime.harness.max_steps or 0))
        )
        self.effective_max_steps = self.base_max_steps
        self._step_extension_granted_steps = 0
        self._step_extension_locked = False
        self._recent_tool_outcomes: List[JsonDict] = []
        self._current_step = 0
        self.static_context_block, self.static_context_hash = build_static_context_block(
            self.runtime.harness.context_file,
            project_context_files=getattr(
                self.runtime.harness, "project_context_files", None,
            ),
            append_system_prompt=getattr(
                self.runtime.harness, "append_system_prompt", None,
            ),
        )

    def _agent_event_payload(
        self,
        payload: Optional[JsonDict] = None,
    ) -> JsonDict:
        return {
            **dict(payload or {}),
            "workerId": str(self.worker_id or ""),
            "slotId": str(self.slot_id or ""),
            "agentId": str(self.runtime.agent_id or ""),
            "phaseId": str(self.phase_id or ""),
        }

    def _write_agent_event(
        self,
        event_type: str,
        payload: Optional[JsonDict] = None,
    ) -> None:
        self.logger.write(
            event_type,
            self._agent_event_payload(payload),
        )

    async def _review_standalone_task(self, *, phase, final_answer="", question=""):
        return await self.task_control.review(
            phase=phase, final_answer=final_answer, question=question)

    def _fit_image_to_context_window(
        self,
        image_block: JsonDict,
        receipt: JsonDict,
        *,
        system_prompt: str,
        messages: List[JsonDict],
        tools: List[JsonDict],
    ) -> Tuple[Optional[JsonDict], JsonDict]:
        """Attach a screenshot only if the next request can carry it.

        Compaction never shrinks a pending image, so an image the transport
        counts past the window can only produce a request the provider must
        reject after uploading and counting it.
        """
        accounting = _tool_result_image_accounting(self)
        image_tokens = estimate_image_tokens(image_block, accounting=accounting)
        request_tokens = image_tokens + estimate_prompt_tokens(
            system_prompt, messages, tools,
            tool_result_image_accounting=accounting,
        )
        window = max(1, int(self.runtime.harness.model_context_window_tokens))
        if request_tokens <= window:
            return image_block, receipt
        return None, {
            "attached": False,
            "reason": "image_exceeds_context_window",
            "mediaType": receipt.get("mediaType"),
            "rawBytes": receipt.get("rawBytes"),
            "encodedBytes": receipt.get("encodedBytes"),
            "imageAccounting": accounting,
            "estimatedImageTokens": image_tokens,
            "estimatedRequestTokens": request_tokens,
            "contextWindowTokens": window,
        }

    async def run(self, task: str) -> str:
        from harness.agents.browser.loop import run_browser_agent
        return await run_browser_agent(self, task)

    async def _bootstrap_browser(self, task: str = "") -> JsonDict:
        registration = self.preloaded_registration
        if registration is None:
            registration = await self.browser.call("System.register", {})
        fleet_assignment = {
            "status": "preassigned" if self.assigned_fleet_id else "missing",
            "assignedFleetId": self.assigned_fleet_id,
            "allowedFleetIds": sorted(self.allowed_fleet_ids),
            "assignmentReason": self.fleet_assignment_reason,
            "sessionKey": self.fleet_session_key,
            "isIsolated": self.fleet_is_isolated,
        }
        bundle = self.preloaded_capability_bundle
        preloaded = bundle is not None
        if bundle is None:
            bundle = await load_capability_bundle(
                self.browser,
                logger=self.logger,
                blocked_methods=_BLOCKED_CAPABILITIES,
                schema_cache_dir=global_schemas_dir(self.runtime.harness.worktree_dir),
            )

        self.capabilities = list(bundle.capabilities)
        self.capability_methods = set(bundle.capability_methods)
        self.method_schemas = dict(bundle.method_schemas)
        self.methods_requiring_purpose = set(bundle.methods_requiring_purpose)
        self.purpose_hints = dict(bundle.purpose_hints)
        self.agent_guide = bundle.agent_guide
        self.catalog_revision = bundle.catalog_revision
        self.guide_revision = bundle.guide_revision
        memory_auto_reuse_eligible = getattr(
            self, "task_memory_auto_reuse_eligible", None
        )
        if not self.assigned_fleet_id:
            # Standalone BrowserAgent tests/callers have no Fleet memory to
            # authorize. Use an explicit fail-closed value and let the memory
            # helper return its ordinary "no assigned fleet" skip receipt.
            memory_auto_reuse_eligible = False
        memory_bootstrap = await self._ensure_task_memory(
            str(getattr(self, "task_memory_root_task", "") or task),
            registration=registration,
            auto_reuse_eligible=memory_auto_reuse_eligible,
            reuse_status="running",
        )
        if (
            self.fleet_assignment_reason == "similar_task_fleet_reuse"
            and memory_auto_reuse_eligible is True
            and memory_bootstrap.get("status") != "saved"
        ):
            # A historical Fleet still contains the completed record that made
            # it match. Do not begin browser work unless this worker has first
            # replaced that reusable state with a visible running lease.
            raise RuntimeError(
                "similar-task Fleet reuse could not establish its running "
                "memory lease: "
                + str(
                    memory_bootstrap.get("reason")
                    or memory_bootstrap.get("error")
                    or memory_bootstrap.get("status")
                )
            )

        vl_cfg = self.runtime.harness.vl
        bootstrap = {
            "registration": self._trim_for_log(
                self._sanitize_registration_memory(
                    registration,
                    current_task_scope=self._task_memory_scope(),
                )
            ),
            "capability_count": len(self.capabilities),
            "schema_count": len(self.method_schemas),
            "requires_purpose_count": len(self.methods_requiring_purpose),
            "agent_guide_chars": len(self.agent_guide),
            "fleetAssignment": fleet_assignment,
            "memory": memory_bootstrap,
            "preloaded_capability_bundle": preloaded,
            "vl": {
                "enabled": bool(getattr(vl_cfg, "enabled", False)),
                "provider": str(getattr(vl_cfg, "provider", "") or ""),
                "model_id": str(getattr(vl_cfg, "model_id", "") or ""),
            },
        }
        self.logger.write("browser.bootstrap", bootstrap)
        return bootstrap

    async def _ensure_task_memory(
        self,
        task: str = "",
        *,
        registration: Any = None,
        auto_reuse_eligible: bool,
        reuse_status: str = "running",
    ) -> JsonDict:
        """Initialize ABCP Memory with task context when Memory.save/get exist.

        Memory is used for agent task context only. It is not page state, and it
        must not hold secrets or extracted page data.
        """
        if not isinstance(auto_reuse_eligible, bool):
            raise TypeError("auto_reuse_eligible must be an explicit boolean")
        methods = set(getattr(self, "capability_methods", set()) or set())
        if not {"Memory.get", "Memory.save"}.issubset(methods):
            return {"status": "skipped", "reason": "Memory.get/save unavailable"}
        save_schema = self.method_schemas.get("Memory.save") or {}
        schema_params = save_schema.get("params") if isinstance(save_schema, dict) else {}
        if not isinstance(schema_params, dict) or "fleetId" not in schema_params:
            result = {
                "status": "skipped",
                "reason": "connected Memory.save contract does not advertise fleetId",
            }
            self.logger.write("memory.bootstrap.unsupported_contract", result)
            return result
        fleet_id = str(self.assigned_fleet_id or "").strip()
        if not fleet_id:
            result = {"status": "skipped", "reason": "no assigned fleetId"}
            self.logger.write("memory.bootstrap.skipped", result)
            return result

        found, memory = self._registration_fleet_memory(registration, fleet_id)
        if not found:
            try:
                memory = await self.browser.call("Memory.get", {"fleetId": fleet_id})
            except Exception as exc:
                result = exception_payload(exc, fleetId=fleet_id)
                result["status"] = "failed"
                self.logger.write("memory.bootstrap.get_failed", result)
                return result

        for save_attempt in range(2):
            parsed = self._parse_fleet_memory(memory)
            if parsed["foreign"]:
                result = {
                    "status": "skipped",
                    "fleetId": fleet_id,
                    "reason": "foreign nonempty Fleet memory was not overwritten",
                }
                self.logger.write("memory.bootstrap.foreign_context", result)
                return result
            envelope = self._merge_task_memory_envelope(
                parsed["envelope"],
                task,
                auto_reuse_eligible=auto_reuse_eligible,
                reuse_status=reuse_status,
            )
            params: JsonDict = {
                "fleetId": fleet_id,
                "context": json.dumps(envelope, ensure_ascii=False),
            }
            if parsed["revision"] is not None:
                params["expectedRevision"] = parsed["revision"]
            try:
                saved = await self.browser.call("Memory.save", params)
                result = {
                    "status": "saved",
                    "fleetId": fleet_id,
                    "conflictRetry": bool(save_attempt),
                    "response": self._trim_for_log(saved),
                }
                self.logger.write("memory.bootstrap", result)
                return result
            except Exception as exc:
                if save_attempt == 0 and self._memory_revision_conflict(exc):
                    try:
                        memory = await self.browser.call("Memory.get", {"fleetId": fleet_id})
                        continue
                    except Exception as reread_exc:
                        exc = reread_exc
                result = exception_payload(exc, fleetId=fleet_id)
                result["status"] = "failed"
                result["conflictRetry"] = bool(save_attempt)
                event = (
                    "memory.bootstrap.conflict"
                    if self._memory_revision_conflict(exc)
                    else "memory.bootstrap.failed"
                )
                self.logger.write(event, result)
                return result
        return {"status": "failed", "fleetId": fleet_id}

    def _task_memory_heartbeat_interval_seconds(self) -> float:
        try:
            stale_seconds = float(getattr(
                self.runtime.harness,
                "similar_task_running_stale_seconds",
                DEFAULT_RUNNING_STALE_SECONDS,
            ))
        except (TypeError, ValueError, OverflowError):
            stale_seconds = DEFAULT_RUNNING_STALE_SECONDS
        if stale_seconds <= 0.0:
            return 0.0
        base_interval = min(300.0, stale_seconds / 3.0)
        identity = f"{self.runtime.agent_id}:{self.worker_id}".encode("utf-8")
        jitter_bucket = int.from_bytes(
            hashlib.sha256(identity).digest()[:2], "big"
        ) / 65535.0
        # Stable per-worker jitter avoids synchronized optimistic-write
        # conflicts while always remaining below one third of the lease TTL.
        return max(0.05, base_interval * (0.75 + (0.20 * jitter_bucket)))

    def _start_task_memory_heartbeat(
        self,
        bootstrap: Any,
        *,
        task: str,
    ) -> Optional[asyncio.Task]:
        memory = bootstrap.get("memory") if isinstance(bootstrap, dict) else None
        if (
            not isinstance(memory, dict)
            or memory.get("status") != "saved"
            or getattr(self, "task_memory_auto_reuse_eligible", None) is not True
            or self._task_memory_heartbeat_interval_seconds() <= 0.0
        ):
            return None
        return asyncio.create_task(
            self._task_memory_heartbeat_loop(task),
            name=f"fleet-memory-heartbeat:{self.worker_id or self.runtime.agent_id}",
        )

    async def _task_memory_heartbeat_loop(self, task: str) -> None:
        interval = self._task_memory_heartbeat_interval_seconds()
        if interval <= 0.0:
            return
        try:
            stale_seconds = float(getattr(
                self.runtime.harness,
                "similar_task_running_stale_seconds",
                DEFAULT_RUNNING_STALE_SECONDS,
            ))
        except (TypeError, ValueError, OverflowError):
            stale_seconds = DEFAULT_RUNNING_STALE_SECONDS
        write_timeout = max(1.0, min(30.0, stale_seconds / 3.0))
        while True:
            await asyncio.sleep(interval)
            try:
                receipt = await asyncio.wait_for(
                    self._ensure_task_memory(
                        task,
                        auto_reuse_eligible=True,
                        reuse_status="running",
                    ),
                    timeout=write_timeout,
                )
                self._write_agent_event("memory.heartbeat", {
                    "status": str(receipt.get("status") or "unknown"),
                    "fleetId": str(receipt.get("fleetId") or ""),
                    "intervalSeconds": round(interval, 3),
                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The existing lease remains authoritative until its TTL. A
                # heartbeat failure is observable but never masks worker work.
                self._write_agent_event(
                    "memory.heartbeat.failed",
                    exception_payload(exc, intervalSeconds=round(interval, 3)),
                )

    async def _stop_task_memory_heartbeat(
        self,
        heartbeat: Optional[asyncio.Task],
    ) -> None:
        if heartbeat is None:
            return
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._write_agent_event(
                "memory.heartbeat.stop_failed",
                exception_payload(exc),
            )

    @staticmethod
    def _registration_fleet_memory(registration: Any, fleet_id: str) -> Tuple[bool, Any]:
        data = registration.get("data") if isinstance(registration, dict) else None
        fleets = data.get("fleets") if isinstance(data, dict) else None
        for fleet in fleets if isinstance(fleets, list) else []:
            if isinstance(fleet, dict) and str(fleet.get("fleetId") or "") == fleet_id:
                return "memory" in fleet, fleet.get("memory")
        return False, None

    @staticmethod
    def _parse_fleet_memory(value: Any) -> JsonDict:
        return parse_fleet_memory(value)

    def _merge_task_memory_envelope(
        self,
        existing: Any,
        task: str,
        *,
        auto_reuse_eligible: bool,
        reuse_status: str = "running",
    ) -> JsonDict:
        if not isinstance(auto_reuse_eligible, bool):
            raise TypeError("auto_reuse_eligible must be an explicit boolean")
        envelope = dict(existing) if isinstance(existing, dict) else {}
        tasks = list(envelope.get("tasks") or []) if isinstance(envelope.get("tasks"), list) else []
        now = time.time()
        prior_policy = (
            envelope.get("reusePolicy")
            if isinstance(envelope.get("reusePolicy"), dict)
            else None
        )
        try:
            prior_policy_version = int(
                (prior_policy or {}).get("version") or 0
            )
        except (TypeError, ValueError, OverflowError):
            prior_policy_version = 0
        trusted_policy = bool(
            prior_policy
            and prior_policy_version >= FLEET_REUSE_POLICY_VERSION
        )
        # Existing task history without the Fleet-level policy may contain an
        # old session/isolated use. Never promote that unknown identity merely
        # because a later generic worker touched the Fleet.
        blocked = bool(
            not auto_reuse_eligible
            or (tasks and not trusted_policy)
            or (trusted_policy and prior_policy.get("blocked") is not False)
            or any(
                isinstance(item, dict)
                and item.get("autoReuseEligible") is False
                for item in tasks
            )
        )
        envelope["reusePolicy"] = {
            "version": FLEET_REUSE_POLICY_VERSION,
            "blocked": blocked,
            "updatedAt": now,
        }
        task_id = getattr(getattr(self, "logger", None), "task_dir", Path("")).name
        worker_id = str(getattr(self, "worker_id", "") or "").strip()
        tasks = [
            item for item in tasks
            if (
                isinstance(item, dict)
                and not (
                    item.get("taskId") == task_id
                    and str(item.get("workerId") or "").strip() == worker_id
                )
            )
        ]
        normalized_status = (
            str(reuse_status or "running").strip().lower() or "running"
        )
        record = {
            "taskId": task_id,
            "workerId": worker_id,
            "agentId": self.runtime.agent_id,
            "updatedAt": now,
            "reuseStatus": normalized_status,
            "autoReuseEligible": auto_reuse_eligible,
        }
        # Active-worker leases need identity and freshness only. The task text
        # becomes reusable history only once that worker reaches a terminal
        # state, so parallel workers cannot expose an in-flight Fleet.
        if normalized_status != "running":
            record["rootTask"] = str(task or "")[:2000]
        tasks.append(record)
        envelope["schema"] = FLEET_MEMORY_SCHEMA
        # This value is injected into the live BrowserAgent prompt directly;
        # persisting a second, unread copy in Fleet memory only grows payloads.
        envelope.pop("memoryContext", None)

        running_stale_seconds = getattr(
            self.runtime.harness,
            "similar_task_running_stale_seconds",
            DEFAULT_RUNNING_STALE_SECONDS,
        )
        running_records: List[JsonDict] = []
        terminal_by_task: Dict[str, Tuple[int, JsonDict]] = {}
        for index, item in enumerate(tasks):
            if not isinstance(item, dict):
                continue
            status = str(item.get("reuseStatus") or "").strip().lower()
            if status == "running":
                running_records.append(item)
                continue

            # Worker identity remains useful for diagnosis, but completed task
            # history is one reusable summary per task. Prefer a completed
            # outcome over other terminal outcomes, then the newest record.
            history_key = str(item.get("taskId") or "").strip()
            if not history_key:
                # An identity-free terminal record cannot be safely matched or
                # excluded as the current task. It is transition debris, not a
                # reusable history candidate.
                continue
            previous = terminal_by_task.get(history_key)
            previous_item = previous[1] if previous is not None else None
            is_completed = status == "completed"
            previous_completed = bool(
                previous_item
                and str(previous_item.get("reuseStatus") or "").lower()
                == "completed"
            )
            if (
                previous is None
                or (is_completed and not previous_completed)
                or (is_completed == previous_completed)
            ):
                terminal_by_task[history_key] = (index, item)

        terminal_history: List[JsonDict] = []
        for _, item in sorted(terminal_by_task.values(), key=lambda pair: pair[0]):
            summary: JsonDict = {
                "taskId": str(item.get("taskId") or ""),
                "workerId": str(item.get("workerId") or ""),
                "agentId": str(item.get("agentId") or ""),
                "updatedAt": item.get("updatedAt"),
                "reuseStatus": str(item.get("reuseStatus") or "").strip().lower(),
                "autoReuseEligible": item.get("autoReuseEligible"),
            }
            root_task = task_text_from_memory_entry(item)[:2000]
            if root_task:
                summary["rootTask"] = root_task
            terminal_history.append(summary)

        active_running = [{
            "taskId": str(item.get("taskId") or ""),
            "workerId": str(item.get("workerId") or ""),
            "agentId": str(item.get("agentId") or ""),
            "updatedAt": item.get("updatedAt"),
            "reuseStatus": "running",
            "autoReuseEligible": item.get("autoReuseEligible"),
        } for item in compact_running_memory_records(
            running_records,
            now=now,
            running_stale_seconds=running_stale_seconds,
        )]

        # The cap applies only to reusable terminal history. Every active
        # worker lease is retained outside it, so a terminal write can neither
        # evict another live worker nor be silently discarded by live workers.
        envelope["tasks"] = terminal_history[-12:] + active_running
        return envelope

    @staticmethod
    def _memory_revision_conflict(exc: BaseException) -> bool:
        text = str(exc or "").lower()
        return "revision" in text and any(token in text for token in ("conflict", "mismatch", "expected"))

    def _task_memory_scope(self) -> str:
        """Legacy identifier used only to redact stale registration payloads."""
        task_id = getattr(getattr(self, "logger", None), "task_dir", Path("")).name
        return f"{self.runtime.agent_id}:{task_id}:task"

    def _sanitize_registration_memory(
        self,
        registration: Any,
        *,
        current_task_scope: str,
    ) -> Any:
        if not isinstance(registration, dict):
            return registration
        cleaned = json.loads(json.dumps(registration, ensure_ascii=False, default=str))
        data = cleaned.get("data")
        if not isinstance(data, dict):
            return cleaned
        # Redact old registration payloads defensively, but never issue the old
        # scope-shaped RPC contract from bootstrap.
        memories = data.get("memories")
        if isinstance(memories, list):
            current_parts = str(current_task_scope or "").split(":")
            current_task_id = current_parts[-2] if len(current_parts) >= 3 else ""
            kept: List[JsonDict] = []
            removed = 0
            for item in memories:
                scope = str(item.get("scope") or "") if isinstance(item, dict) else ""
                parts = scope.split(":")
                foreign_task = bool(
                    len(parts) >= 3
                    and parts[-1] == "task"
                    and re.fullmatch(r"[0-9a-f]{16,}", parts[-2] or "")
                    and parts[-2] != current_task_id
                )
                if foreign_task:
                    removed += 1
                    continue
                kept.append(item)
            data["memories"] = kept
            if removed:
                data["removedForeignTaskMemories"] = {
                    "count": removed,
                    "reason": "removed stale task-scoped registration memory",
                }
        # Current ABCP exposes one Fleet-global memory record per fleet.  Never
        # place its task text into a new worker's model context; the bootstrap
        # code above consumes it mechanically.
        fleets = data.get("fleets")
        if isinstance(fleets, list):
            for fleet in fleets:
                if not isinstance(fleet, dict) or fleet.get("memory") is None:
                    continue
                raw = fleet.get("memory")
                revision = raw.get("revision") if isinstance(raw, dict) else None
                fleet["memory"] = {"present": True, "revision": revision}
        return cleaned

    def _build_dynamic_context(self, bootstrap: JsonDict) -> str:
        payload = {
            "bootstrap": bootstrap,
            "memory_context": self.runtime.harness.memory_context,
        }
        payload = self.lifecycle.session_context_build(
            LifecycleContext(actor="browser_agent"),
            payload,
        )
        return json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    def _build_system_prompt(self) -> str:
        from harness.agents.browser.prompt import build_system_prompt
        return build_system_prompt(self)

    def _visible_capability_methods(self) -> Set[str]:
        visible = filter_capability_methods(self.capability_methods)
        from harness.workflow.workflow_runtime import workflow_execution_enabled
        if not workflow_execution_enabled(self):
            visible.discard("Workflow.execute")
        return visible

    def _capture_artifacts(self, method: str, response: Any) -> Any:
        if not isinstance(response, dict):
            return response
        captured = strip_image_payload(
            logger=self.logger,
            method=method,
            response=response,
            artifacts=self.artifacts,
            prefix=self.runtime.agent_id,
        )
        return captured

    def _capture_file_action(
        self,
        method: str,
        params: JsonDict,
        response: Any,
    ) -> None:
        if method == "Workflow.execute" and isinstance(response, dict):
            data = response.get("data")
            results = data.get("results") if isinstance(data, dict) else None
            if isinstance(results, list):
                # Workflow child actions are the operations that produced the
                # files. Preserve their individual method, params, result and
                # step identity instead of crediting the opaque outer call or
                # scanning its whole envelope for unrelated historical paths.
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("status") or "").lower() not in {
                        "success", "succeeded", "done", "completed",
                    }:
                        continue
                    step = item.get("step")
                    if not isinstance(step, dict) or step.get("type") != "action":
                        continue
                    action = str(step.get("action") or "").strip()
                    if not (
                        action == "DOM.getImg"
                        or action == "File.download"
                        or action == "File.handleChooser"
                        or action.startswith("Download.")
                    ):
                        continue
                    child_params = step.get("params")
                    child_params = child_params if isinstance(child_params, dict) else {}
                    child_result = item.get("result")
                    before = len(self.file_action_evidence)
                    BrowserAgent._capture_file_action(
                        self, action, child_params, child_result,
                    )
                    if action.startswith("Download."):
                        from harness.tools.browser_tools.downloads import (
                            remember_workflow_download_result,
                        )
                        remember_workflow_download_result(
                            self,
                            child_result,
                            action=action,
                            workflow_id=str(data.get("workflowId") or ""),
                            step_path=str(item.get("stepPath") or ""),
                        )
                    if len(self.file_action_evidence) > before:
                        self.file_action_evidence[-1]["workflowStepPath"] = str(
                            item.get("stepPath") or ""
                        )
                        self.file_action_evidence[-1]["workflowId"] = str(
                            data.get("workflowId") or ""
                        )
            return
        file_method = (
            method == "DOM.getImg"
            or method == "File.download"
            or method == "File.handleChooser"
            or method.startswith("Download.")
        )
        if not file_method:
            return
        if method.startswith("Download."):
            from harness.tools.browser_tools.downloads import sync_download_artifacts
            sync_download_artifacts(self)
        captured_paths = _saved_paths_from_value(response)
        if method in {"Download.list", "Download.control"}:
            captured_paths = [path for path in captured_paths if path in self.artifacts]
        for saved_path in captured_paths:
            # An inventory observation is not a new delivery. Refresh only
            # paths already attributed to this worker, retaining the full list
            # below as diagnostic evidence.
            if method in {"Download.list", "Download.control"} and saved_path not in self.artifacts:
                continue
            if saved_path not in self.artifacts:
                self.artifacts.append(saved_path)
            # Register the file the platform wrote. The harness never holds
            # these bytes - Download.start hands the path to ABCP, which does
            # the writing - so only a reference plus an integrity snapshot can
            # be recorded, and a later read reports drift rather than claiming
            # the content is immutable.
            self._register_external_file(method, saved_path)
        self.file_action_evidence.append({
            "method": method,
            "params": trim_large_strings(dict(params or {}), max_chars=2000),
            "response": trim_large_strings(response, max_chars=4000),
        })
        # Evidence is a diagnostic/validator ledger, not an unbounded trace.
        # Retain a generous recent window while preventing long download or
        # image-export batches from growing worker memory without limit.
        if len(self.file_action_evidence) > 200:
            del self.file_action_evidence[:-200]

    def _register_external_file(self, method: str, saved_path: str) -> None:
        """Record a platform-written file as an external resource."""

        logger = getattr(self, "logger", None)
        if logger is None or getattr(logger, "task_dir", None) is None:
            return
        from harness.utils import storage_for_logger

        try:
            storage, task_id = storage_for_logger(logger)
            task_root = Path(logger.task_dir).resolve(strict=False)
            resolved = Path(saved_path).expanduser().resolve(strict=False)
            try:
                logical_path = str(resolved.relative_to(task_root))
            except ValueError:
                # Outside the worktree: still worth a record, but it must be
                # marked so a purge never deletes a file it does not own.
                logical_path = resolved.name
            storage.save_resource(
                task_id=task_id,
                run_id=str(getattr(logger, "run_id", "") or ""),
                resource_type="download" if method.startswith("Download.") else "file_evidence",
                logical_path=logical_path,
                external_path=str(resolved),
                media_type="application/octet-stream",
                metadata={"method": method},
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail a call
            try:
                logger.write(
                    "storage.external_file_unregistered",
                    {"method": method, "savedPath": saved_path, "error": str(exc)},
                )
            except Exception:
                pass

    def _offload_response(
        self,
        method: str,
        params: JsonDict,
        response: Any,
        step: int,
    ) -> Any:
        return offload_large_response_fields(
            logger=self.logger,
            method=method,
            params=params,
            response=response,
            step=step,
            prefix=self.runtime.agent_id,
            threshold_bytes=self.runtime.harness.offload_threshold_bytes,
        )

    def _to_model_json(self, value: Any) -> str:
        return json.dumps(
            self._clean_for_model(value),
            ensure_ascii=False,
            default=str,
        )

    def _clean_for_model(self, value: Any) -> Any:
        return trim_large_strings(
            strip_llm_hidden_fields(value),
            max_chars=self.runtime.harness.max_observation_chars,
        )

    def _trim_for_model(self, value: Any) -> Any:
        return trim_large_strings(
            value,
            max_chars=self.runtime.harness.max_observation_chars,
        )

    def _trim_for_log(self, value: Any) -> Any:
        return trim_large_strings(value, max_chars=8000)

    def _consume_reality_check_blocks(self) -> List[JsonDict]:
        """Deliver any reality check that finished since the last turn.

        The check itself runs in the background (see
        `harness.tools.browser_tools.visual._reality_check_inference`): its
        verdict is advisory, so making the worker sit through a VL inference —
        4,527.8s of critical path across 164 runs, 60% of it producing no
        verdict at all — bought nothing. It arrives here instead, on the first
        turn after it completes, in its own text block so the tool receipts
        around it stay untouched.
        """
        pending = getattr(self, "pending_reality_check", None)
        if not pending:
            return []
        self.pending_reality_check = []
        blocks: List[JsonDict] = []
        for payload in pending:
            if not isinstance(payload, dict):
                continue
            reality = payload.get("realityCheck")
            instruction = str(payload.get("next_instruction") or "").strip()
            body = json.dumps(reality, ensure_ascii=False, default=str)
            text = f"<reality_check>\n{body}\n</reality_check>"
            if instruction:
                text += f"\n{instruction}"
            blocks.append({"type": "text", "text": text})
            self._write_agent_event("vl.reality_check.delivered", {
                "verdict": (reality or {}).get("verdict")
                if isinstance(reality, dict) else None,
                "hasInstruction": bool(instruction),
            })
        return blocks

    def _step_cap_reminder_block(
        self, *, current_step: int, max_steps: int,
    ) -> Optional[JsonDict]:
        """Append a transient reminder to the next user message, not system."""
        next_step = current_step + 1
        if next_step > max_steps:
            return None
        # Inclusive count: when step 48 has completed in a 50-step run, steps
        # 49 and 50 are both still available.
        remaining = max_steps - current_step
        if remaining > 2:
            return None
        if remaining <= 0:
            remaining = 1  # we are at the last step
        reminder = (
            "[HARNESS-CHECKPOINT-REMINDER]\n"
            "This reminder applies to the immediately following assistant turn only.\n"
            f"currentStep={next_step}\n"
            f"maxSteps={max_steps}\n"
            f"remainingSteps={remaining}\n"
            "These are arithmetic budget facts only. Choose the next action"
            " from the original goal and current evidence."
        )
        harness_config = getattr(getattr(self, "runtime", None), "harness", None)
        cap = 0
        extension_state = "disabled"
        if bool(getattr(
            harness_config, "browser_agent_step_extension_enabled", False,
        )):
            cap = int(getattr(
                harness_config, "browser_agent_max_extension_steps", 0,
            ) or 0)
            if self._step_extension_granted_steps:
                extension_state = "granted"
                reminder += (
                    " The one permitted extension has already been granted;"
                    " no further extension is available."
                )
            elif self._step_extension_locked:
                extension_state = "locked"
                reminder += (
                    " No extension is available in this run. Now "
                    + _EXTENSION_HANDOFF_HINT
                )
            else:
                extension_state = "available"
                # The cap was never stated, so every estimate was authored
                # blind: 7 of 21 historical grants asked for 20-50 steps
                # against a cap of 15 and not one of them finished. Naming the
                # number is only half of it — an honest over-cap estimate has
                # to have somewhere to go, hence the handoff instruction.
                reminder += (
                    " One bounded extension is available in this run, of at"
                    f" most {cap} steps (hard limit"
                    f" {self.base_max_steps + cap}). estimated_steps counts"
                    " model turns, not individual actions — one turn may carry"
                    " several tool calls. Estimate truthfully: if finishing"
                    f" this phase needs more than {cap} turns, do NOT request"
                    " an extension. Such a request is denied and no further"
                    " request is accepted in this run. In that case, "
                    + _EXTENSION_HANDOFF_HINT
                    + " If the remaining work does fit, request the extension"
                    " instead of handing off early."
                )
        self._write_agent_event(
            "agent.step_cap.reminder",
            {
                "step": next_step,
                "max_steps": max_steps,
                "remaining": remaining,
                "injected_after_step": current_step,
                "placement": "user_message_text_block",
                # Whether the model had the cap in front of it when it authored
                # an estimate is the whole question this change turns on, so it
                # has to be readable from the event rather than reconstructed.
                "extensionState": extension_state,
                "extensionCap": cap,
            },
        )
        return {"type": "text", "text": reminder}

    def _observe_cache_pressure(
        self, usage_payload: JsonDict, *, step: int, max_steps: int,
    ) -> None:
        self._cache_pressure, reason = update_cache_pressure_state(
            self._cache_pressure,
            usage_payload=usage_payload,
            config=self.runtime.harness,
            step=step,
            max_steps=max_steps,
        )
        if reason:
            self._forced_compaction_reason = reason
            self.logger.write(
                "context.compaction_requested",
                {
                    "actor": "browser_agent",
                    "step": step + 1,
                    "reason": reason,
                    "triggerStep": step,
                },
            )

    def _observe_tool_result(self, tool_call: JsonDict, result: Any) -> None:
        """Feed browser_call results into diagnostics for status classification."""
        if not isinstance(result, dict):
            return
        self._recent_tool_outcomes.append({
            "step": int(getattr(self, "_current_step", 0) or 0),
            "tool": str(tool_call.get("name") or ""),
            "failed": bool(_invoke_result_failed(result)),
        })
        if len(self._recent_tool_outcomes) > 20:
            self._recent_tool_outcomes = self._recent_tool_outcomes[-20:]
        name = tool_call.get("name")
        method = result.get("method") or ""
        # Direct-capability tools (when ABCP method is wired as a top-level tool)
        # land here with name == method; treat them the same as a browser_call.
        if name == "browser_call" or method:
            if not method:
                return
            params = result.get("params") or {}
            self.diagnostics.observe_browser_call(str(method), params, result)

    def request_step_extension(
        self, tool_input: JsonDict, *, step: int,
    ) -> JsonDict:
        """Evaluate one model-authored request under harness-owned hard guards."""
        estimated_steps = optional_int(tool_input.get("estimated_steps"), 0) or 0
        remaining_actions = [
            str(item).strip()
            for item in (tool_input.get("remaining_actions") or [])
            if str(item).strip()
        ]
        configured_max = int(
            self.runtime.harness.browser_agent_max_extension_steps or 0
        )
        requested_payload = {
            "step": step,
            "estimatedSteps": estimated_steps,
            "remainingActionCount": len(remaining_actions),
            "baseMaxSteps": self.base_max_steps,
            "currentMaxSteps": self.effective_max_steps,
            "configuredMaxExtensionSteps": configured_max,
        }
        self._write_agent_event(
            "agent.step_extension.requested", requested_payload,
        )

        denial_reasons: List[str] = []
        # A run that already refused an over-cap estimate stays refused. Without
        # this, denying an honest "I need 40" only teaches the model to come
        # back at 49 with a compliant 15 it cannot meet either.
        if self._step_extension_locked:
            denial_reasons.append("extension_locked")
        if not bool(self.runtime.harness.browser_agent_step_extension_enabled):
            denial_reasons.append("feature_disabled")
        if self._step_extension_granted_steps:
            denial_reasons.append("extension_already_granted")
        # The request is useful only at the handoff boundary. An early grant
        # turns the hard cap into an invisible larger default and defeats the
        # A/B comparison this feature exists to measure.
        if step < max(1, self.base_max_steps - 2):
            denial_reasons.append("request_too_early")
        if estimated_steps < 1:
            denial_reasons.append("invalid_estimate")
        # Granting a truncated slice against an estimate the cap cannot cover
        # never once finished the phase: across 21 historical grants, the 7
        # whose estimate exceeded the cap produced zero `done` (3 exhausted,
        # 4 partial) while burning 100 extension steps. Refusing sends the
        # worker to a clean handoff with its remaining budget instead.
        # No `configured_max > 0` guard: a cap of 0 means no extension is
        # allowed, so every estimate exceeds it. Guarding here would grant the
        # estimate in full precisely when the configuration forbids one.
        if estimated_steps > configured_max:
            denial_reasons.append("estimate_exceeds_cap")
        if not remaining_actions:
            denial_reasons.append("remaining_actions_required")

        recent_window_start = max(1, step - 4)
        recent_loop_nudge = any(
            isinstance(item, dict)
            and item.get("type") == "loop_nudge"
            and int(item.get("step") or 0) >= recent_window_start
            for item in self.trace
        )
        risk_observations = ["recent_loop_nudge"] if recent_loop_nudge else []
        recent_outcomes = [
            item for item in self._recent_tool_outcomes
            if int(item.get("step") or 0) >= recent_window_start
            and item.get("tool") != "request_step_extension"
        ]
        if (
            len(recent_outcomes) >= 2
            and all(bool(item.get("failed")) for item in recent_outcomes[-2:])
        ):
            denial_reasons.append("consecutive_tool_failures")
        if self.diagnostics.hitl_unresolved():
            denial_reasons.append("hitl_unresolved")
        if self.diagnostics.routing_failure_status:
            denial_reasons.append("routing_failure")

        if denial_reasons:
            # An over-cap estimate locks the run unless the request was merely
            # early: before the window opens the estimate describes work the
            # worker may well finish on its own by the time it matters, so
            # refusing it then must not spend the run's one shot. Loop nudges
            # are reported separately as observations; they are not guards.
            # An already-granted run has no channel left to close, so locking
            # it would only blur what `extensionLocked` means: without this,
            # any over-cap request after a grant sets the flag too and the
            # count of runs actually closed by an honest estimate reads high.
            if (
                "estimate_exceeds_cap" in denial_reasons
                and "request_too_early" not in denial_reasons
                and not self._step_extension_granted_steps
            ):
                self._step_extension_locked = True
            result = {
                "status": "denied",
                "reasons": denial_reasons,
                "step": step,
                "baseMaxSteps": self.base_max_steps,
                "effectiveMaxSteps": self.effective_max_steps,
                "extensionLocked": self._step_extension_locked,
                "riskObservations": risk_observations,
                "next_instruction": (
                    "No extension is available in this run. Now "
                    + _EXTENSION_HANDOFF_HINT
                    + " Do not start new business actions."
                    if self._step_extension_locked else
                    # Worded off the lock's actual predicate — request_too_early
                    # being present, not being the sole reason. "Only because"
                    # reads false in exactly the combination the exemption
                    # exists for (too early AND over cap), sending a literal
                    # reader to the fallback clause and never asking again.
                    "If request_too_early is among the reasons above, the run"
                    " is still open: you may request once more when the window"
                    " opens, but only with an estimate that fits the"
                    " configured limit, since an over-limit estimate is"
                    " refused outright and closes this run to any further"
                    " request. Otherwise finish within the current budget or"
                    " provide the best truthful terminal status/blocker."
                ),
            }
            self._write_agent_event(
                "agent.step_extension.denied",
                {
                    **requested_payload,
                    "reasons": denial_reasons,
                    "extensionLocked": self._step_extension_locked,
                },
            )
            return result

        # `estimate_exceeds_cap` already refused everything the cap cannot
        # cover, so the estimate is grantable in full and no residual work is
        # left to hand off from a grant.
        granted_steps = estimated_steps
        self._step_extension_granted_steps = granted_steps
        self.effective_max_steps = self.base_max_steps + granted_steps
        result = {
            "status": "granted",
            "requestedSteps": estimated_steps,
            "grantedSteps": granted_steps,
            "step": step,
            "baseMaxSteps": self.base_max_steps,
            "effectiveMaxSteps": self.effective_max_steps,
            "hardLimit": self.base_max_steps + configured_max,
            "riskObservations": risk_observations,
            "remainingActionCount": len(remaining_actions),
            "next_instruction": (
                "Execute only the bounded remaining checklist, then call"
                " final_answer. No further extension is available."
            ),
        }
        self._write_agent_event(
            "agent.step_extension.granted",
            {**requested_payload, **result},
        )
        return result

    def _has_extraction_artifact(self) -> bool:
        """True iff this worker wrote at least one extraction artifact via
        record_extraction. Used by classifier to decide between
        extraction_inconclusive and step_budget_exhausted: if the worker did
        manage to land structured rows somewhere, "extraction inconclusive"
        is the wrong story even if recent JS calls were noisy."""
        for path in self.artifacts:
            if "/artifacts/extractions/" in str(path).replace("\\", "/"):
                return True
        return False

    def _compose_step_cap_message(self, final_status: str) -> str:
        from harness.constants import (
            WORKER_STATUS_CONTEXT_LIMIT,
            WORKER_STATUS_EXTRACTION_INCONCLUSIVE,
            WORKER_STATUS_HITL_TIMEOUT,
            WORKER_STATUS_HITL_WAITING,
            WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
            WORKER_STATUS_PAGE_CRASHED,
            WORKER_STATUS_API_CONTRACT_ERROR,
        )
        hints = {
            WORKER_STATUS_CONTEXT_LIMIT: "Model token limit hit; trim the prompt or split the task for follow-up runs.",
            WORKER_STATUS_HITL_WAITING: "A human-pause was requested but the harness did not enter wait (should disappear once PR #4 lands).",
            WORKER_STATUS_HITL_TIMEOUT: "Human intervention was requested and the wait window elapsed without a resume signal.",
            WORKER_STATUS_PAGE_SETTLED_AFTER_HITL: "The page got past the challenge, but ABCP still reports it paused; platform auto-recovery has not released the control channel.",
            WORKER_STATUS_API_CONTRACT_ERROR: (
                "Repeated ABCP contract errors (method not found / routing / etc.); "
                "do not retry the same API path in the short term."
            ),
            WORKER_STATUS_PAGE_CRASHED: "The page lost its render context repeatedly within the window — rebuild the fleet/page before retrying.",
            WORKER_STATUS_EXTRACTION_INCONCLUSIVE: (
                "Extraction kept failing (JS/AXTree returning null/empty/timeout, etc.); switch probing strategy."
            ),
        }
        suffix = hints.get(final_status, "Reached the maximum orchestration step count without an explicit completion.")
        parts = [f"{suffix} See run log: {self.logger.path}"]
        progress = self._compose_step_cap_progress()
        if progress:
            parts.append(progress)
        return "\n".join(parts)

    def _compose_step_cap_progress(self) -> str:
        """State this worker reached, for whoever picks the phase up next.

        A worker cut off at the step cap never writes a final answer, so the
        handoff used to be a hint plus a log path. The Lead then reconstructed
        the story from the raw worker trace instead: in a608 that was five reads
        of a 230KB trace file, 29% of the Lead's entire context. Everything
        below is already in hand here and costs no extra call.
        """
        lines: List[str] = []
        urls = getattr(self, "page_urls", None)
        if isinstance(urls, dict) and urls:
            page_id = str(getattr(self, "axtree_page_id", "") or "")
            url = urls.get(page_id) or list(urls.values())[-1]
            if url:
                lines.append(f"- Page is now at: {url}")
        artifacts = [
            str(path) for path in (self.artifacts or [])
            if "/artifacts/extractions/" in str(path).replace("\\", "/")
        ]
        if artifacts:
            lines.append(
                "- Extraction artifacts written: " + ", ".join(artifacts[-3:])
            )
        succeeded: List[str] = []
        last_failure = ""
        for item in (self.trace or []):
            if not isinstance(item, dict) or item.get("type") != "browser_call":
                continue
            method = str(item.get("method") or "")
            # Allowlist, not a denylist. Enumerating read-only methods to skip
            # let Page.screenshot / Download.list / History.list read as state
            # changes; the invalidating set is the harness's existing answer to
            # "did this touch the page". Runtime.evaluate is carved back out:
            # model-authored evaluates are read-only by contract, so listing one
            # as a state change would misreport what this worker actually did.
            if (
                method not in AXTREE_INVALIDATING_METHODS
                or method == "Runtime.evaluate"
            ):
                continue
            # Control-plane pauses are not page mutations. The pause → human →
            # resume window does change the page, but that cycle is reported
            # through the challenge/HITL receipts and the resume checkpoint;
            # listing requestPause here as a "state-changing action" told the
            # next worker the pause itself mutated something (run a686e03f).
            if method.startswith("Hitl."):
                continue
            # Same predicate the batch guard uses. A hand-rolled check on
            # result.error misses the cases that actually matter here: browser
            # action errors land in response.data.error (top-level error is only
            # set on transport failures), and stale_element_reference /
            # tool_was_executed=False carry no error object at all. Those would
            # be listed to the Lead as actions that succeeded.
            result = item.get("result")
            failed = _invoke_result_failed(result)
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            target = str(
                params.get("id") or params.get("selector") or params.get("url") or ""
            )[:60]
            entry = f"{method}({target})" if target else method
            if failed:
                last_failure = entry
            else:
                succeeded.append(entry)
        if succeeded:
            lines.append(
                "- State-changing actions that succeeded, in order: "
                + " -> ".join(succeeded[-8:])
            )
        if last_failure:
            lines.append(f"- Last action that failed: {last_failure}")
        if not lines:
            return ""
        return (
            "Progress handoff (read this instead of the raw trace):\n"
            + "\n".join(lines)
        )

    def _write_agent_final(
        self,
        *,
        final_status: str,
        final_answer: str,
        model_reported_status: Optional[str],
        override_reason: Optional[str],
        reached_step_cap: bool,
    ) -> None:
        payload: JsonDict = {
            "status": final_status,
            "statusCategory": status_category(final_status),
            "answer": final_answer,
            "artifacts": self.artifacts,
            "reachedStepCap": reached_step_cap,
            "diagnostics": self.diagnostics.to_log_payload(),
        }
        continuation = getattr(self, "continuation_decision", None)
        if isinstance(continuation, dict):
            payload["continuation"] = continuation
        if self._step_extension_granted_steps:
            payload["stepExtension"] = {
                "baseMaxSteps": self.base_max_steps,
                "effectiveMaxSteps": self.effective_max_steps,
                "grantedSteps": self._step_extension_granted_steps,
            }
        if model_reported_status and model_reported_status != final_status:
            payload["modelReportedStatus"] = model_reported_status
        if override_reason:
            payload["statusOverrideReason"] = override_reason
        self._write_agent_event("agent.final", payload)
