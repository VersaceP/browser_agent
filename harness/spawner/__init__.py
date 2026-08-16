"""
harness.spawner - Worker BrowserAgent spawning and lifecycle management.

This package is the namespace hub: submodules implement the spawner and
route package-attribute-sensitive calls through it (see _sp()).
"""

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
from harness.results.call_outcome import classify_call_outcome, evaluate_grant
from harness.fleet.auth import (
    AuthFleetLedger,
    canonical_origin,
    normalize_auth_verification_contract,
)
from harness.diagnostics import status_category
from harness.fleet.coordinator import (
    FleetAssignment,
    FleetCoordinator,
    FleetRoutingError,
    handle_records_from_value,
    normalize_page_policy,
    normalize_reuse_scope,
    resolve_fleet_reference,
)
from harness.fleet.runtime import (
    FleetAuthBarrier,
    FleetClickGate,
    PageLeaseManager,
    PageLeasedBrowserClient,
)
from harness.observation.render_recovery import extract_page_id_from_values
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.fast_path import assess_fast_path_candidate
from harness.results.row_ledger import (
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
from harness.results.worker_result import (
    build_worker_handoff_projection,
    build_worker_result_levels,
)
from harness.workflow_runtime import workflow_execution_enabled
from llm import LLMFactory

from .spawner_classification import (  # noqa: F401
    CHALLENGE_PHASE_STATUSES,
    _allowance_from_validators,
    _axis_magnitude,
    _blocker_evidence_paths,
    _classification_from_browser_call,
    _classification_from_collection_contract_preflight,
    _classification_from_contract_violation,
    _classification_from_final_answer,
    _clone_capability_bundle,
    _cohort_identity_fields,
    _only_visual_check_evidence,
    _origin_from_url,
    _origins_from_text,
    _page_hidden_from_reuse,
    _page_traversal_evidence,
    _phase_family,
    _safe_str_list,
    _scroll_delta_applied,
    _scroll_receipt_data,
    _scroll_was_state_probe,
    _semantic_terminal_counterevidence,
    _state_response_indicates_paused,
    _text_indicates_paused_error,
    _validated_rows_for_ledger,
    _worker_feedback_classification,
    phase_result_status_for,
)
from .spawner_core import (  # noqa: F401
    BrowserAgentSpawner,
)
from .spawner_helpers import (  # noqa: F401
    BrowserAgentFactory,
    BrowserAgentHandle,
    BrowserAgentSlot,
    FleetReadinessError,
    PinnedBrowserContext,
    ResumeBrowserHint,
    SLOT_FULL_SYNC_TTL_SECONDS,
    URL_RE,
    _SessionStartLock,
    _TaskContextTrackingBrowserClient,
    _effective_worker_status,
    _finalize_skill_execution_metadata,
    _fresh_click_settlement_class,
    _is_fleet_open_timeout,
    _prompt_worker_contract,
    _skill_execution_metadata,
    _unresolved_repair_visual_evidence,
    _verified_workflow_hitl_settlement,
)
from .spawner_registry import (  # noqa: F401
    SpawnerRegistryMixin,
)
from .spawner_slots import (  # noqa: F401
    SpawnerSlotsMixin,
)
from .spawner_worker import (  # noqa: F401
    SpawnerWorkerMixin,
)
