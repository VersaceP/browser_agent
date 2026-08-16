"""
harness.task_control - Task plan/state control hub.

This package is the namespace hub: submodules implement the logic and
route cross-module calls through this package (see _tc() in each
submodule) so imports and attribute patches keep working.
"""

from __future__ import annotations



import json
import copy
import csv
import hashlib
import os
import re
import tempfile
import threading
import time
from collections import Counter
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from harness.constants import (
    WORKER_STATUS_API_CONTRACT_ERROR,
    WORKER_STATUS_BLOCKED_BY_CHALLENGE,
    WORKER_STATUS_HITL_REQUIRED,
    WORKER_STATUS_HITL_TIMEOUT,
    WORKER_STATUS_HITL_WAITING,
    WORKER_STATUS_PAGE_CRASHED,
    WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
    WORKER_STATUS_DONE,
    WORKER_STATUS_PARTIAL,
)
from harness.observation.content_completeness import (
    content_completeness_config_errors,
    normalize_content_completeness_config,
)
from harness.evidence.extraction_artifacts import field_name_from_spec, field_names_from_specs
from harness.evidence.artifact_evidence import (
    FILE_VALIDATOR_TYPES,
    VALIDATOR_SCOPE,
    VALIDATOR_TYPES,
    _BLOCKER_TEMPLATE_SEARCH_RE,
    _PLACEHOLDER_LITERAL_RE,
    _business_fields_from_expected,
    cumulative_row_key as _cumulative_row_key,
    _normalized_semantic_token,
    detect_blocker_data_rows,
    detect_near_stub_rows,
    detect_placeholder_rows,
    detect_stub_rows,
)
from harness.fleet.auth import normalize_auth_verification_contract
from harness.fleet.coordinator import normalize_page_policy, normalize_reuse_scope
from harness.evidence.file_evidence import saved_paths_from_value
# Import the leaf module rather than the package: harness.storage.__init__
# pulls in the SQLite factory, which file mode must never need to load.
from harness.storage.base import (
    SNAPSHOT_KEY_CURRENT_PLAN,
    SNAPSHOT_KEY_TASK_STATE,
)
from harness.results.row_ledger import ROW_OUTCOMES, field_absence_accepted
from harness.pacing import (
    MAX_PACING_INTERVAL_SECONDS,
    PACING_FIELDS,
    PACING_INTERVAL_FIELDS,
    jittered_interval,
    merge_pacing,
    normalized_pacing,
    parse_utc_timestamp,
)
from harness.task_types import (
    VALID_TASK_TYPES,
    normalize_task_type,
    resolve_task_type_fail_closed,
    task_type_choices_for_error,
)
from harness.utils import (
    JsonDict,
    RunLogger,
    contains_affirmative_semantic_marker,
    contains_semantic_marker,
    load_task_json,
    read_task_file_text,
    safe_path_component,
    storage_for_logger,
    task_file_exists,
    trim_large_strings,
)

from .artifact_validation import (  # noqa: F401
    _empty_array_observations,
    _tc,
    classification_for_worker_status,
    classify_artifact_validation_failures,
    validate_worker_artifacts,
)
from .cohorts import (  # noqa: F401
    _FAST_PATH_QUANTITY_EXPECTED_KEYS,
    _business_contract_obligations,
    _canonical_cohort_selector,
    _canonical_fast_path_business_contract,
    _cohort_selectors_provably_disjoint,
    _fast_path_business_contract_fence_errors,
    _fast_path_business_contract_signature,
    _fast_path_cohort_key,
    _fast_path_selector_identity_fields,
    _fast_path_validator_is_slice,
    _fast_path_validator_obligations,
    _row_keys_for_indices,
    _tc,
    materialize_batch_rows_from_source,
)
from .fingerprints import (  # noqa: F401
    _SOURCE_URL_RE,
    _attempt_artifact_paths,
    _attempt_digest_is_failure,
    _attempt_primary_blocker,
    _attempt_row_count,
    _canonical_resume_contract_value,
    _classification_from_worker_result,
    _classification_hint_key,
    _fingerprint_num,
    _is_volatile_string,
    _normalized_evidence_source_urls,
    _normalized_source_urls,
    _primary_evidence_source_url,
    _primary_validation_failure_type,
    _record_objective_attempt,
    _stall_signal_reason,
    _strip_volatile_handles,
    _tc,
    build_attempt_digest,
    clear_spawn_acquisition_failures,
    evidence_contract_fingerprint,
    execution_contract_fingerprint,
    failure_signature_from_result,
    objective_fingerprint,
    record_spawn_acquisition_failure,
    spawn_acquisition_error_signature,
    spawn_acquisition_fingerprint,
    spawn_acquisition_rejection,
)
from .phase_lifecycle import (  # noqa: F401
    _artifact_recorded_digest,
    _artifact_sha256,
    _attempt_was_validated_done,
    _count_budgeted_phase_attempts,
    _dependency_blocker,
    _legacy_artifact_syntax_error,
    _legacy_extraction_validation_error,
    _mark_phase_blocked_by_dependency,
    _normalized_depends_on,
    _phase_dependency_ids,
    _resume_artifact_integrity_error,
    _resume_artifact_is_readable,
    _resume_artifact_path,
    _resume_downstream_map,
    _resume_plan_phase_map,
    _tc,
    cancel_phase_running_reservation,
    find_phase,
    mark_phase_exhausted_if_needed,
    mark_phase_result,
    mark_phase_running,
    next_pending_phase,
    phase_pacing_remaining_seconds,
    phase_prior_artifact_paths,
    phase_start_rejection,
    prepare_resume_state,
)
from .plan_validation import (  # noqa: F401
    AXTREE_ID_ANYWHERE_RE,
    BLOCKING_DEPENDENCY_STATUSES,
    EXECUTION_ROLES,
    FILE_RECEIPT_ONLY_VALIDATOR_TYPES,
    RECOVERABLE_ROUTING_PHASE_STATUSES,
    REPLAN_RESET_STATUSES,
    RETRYABLE_PHASE_FAILURE_STATUSES,
    SEMANTIC_TERMINAL_CLASSIFICATIONS,
    SENSITIVE_PROVENANCE_FIELD_MARKERS,
    SPAWN_ACQUISITION_FLEET_COOLDOWN_SECONDS,
    SPAWN_ACQUISITION_MAX_FAILURES,
    TASK_PLAN_FILE,
    TASK_STATE_FILE,
    TERMINAL_PHASE_STATUSES,
    VALIDATOR_TYPE_ALIASES,
    VALID_STAGE_HINTS,
    VOLATILE_HANDLE_KEYS,
    _AUTH_PLAN_MARKERS,
    _AUTH_PROBE_FIELD_MARKERS,
    _AUTH_PROBE_MARKERS,
    _AUTH_TRANSITION_MARKERS,
    _HITL_INTERRUPT_MARKERS,
    _NON_IMAGE_ASSET_TOKEN_RE,
    _NON_IMAGE_FILE_SAVE_RE,
    _PathOnlyLogger,
    _ROW_SELECTION_LIMITS,
    _TASK_STATE_WRITE_LOCK,
    _TaskStateSnapshot,
    _absolute_http_urls_from_value,
    _adapt_cohort_row_selection,
    _allow_empty_fields,
    _auth_phase_kind,
    _canonical_identity_url,
    _declared_batch_size,
    _effective_dependency_ids,
    _first_valid_task_type,
    _identity_value_is_explicit_in_task,
    _instruction_assigns_blocker_to_business_field,
    _merged_expected_artifact,
    _nonempty_validator_fields,
    _normalize_batch_contract,
    _reject_phase_execution_integrity,
    _reject_serial_auth_handoff,
    _reject_singleton_phase_fragmentation,
    _singleton_cohort_key,
    _singleton_range_feature,
    _tc,
    _validate_execution_role_dependencies,
    _validate_pacing,
    _validate_task_type_capability_match,
    _validate_worker_contract_methods,
    _validated_task_type,
    accept_task_plan,
    canonical_identity_url,
    direct_batch_rows_provenance_errors,
    initialize_task_state,
    load_task_state,
    phase_contract,
    utc_now_iso,
    validate_task_plan,
    write_task_plan,
)
from .replan import (  # noqa: F401
    _checkpoint_receipts,
    _replan_checkpoint_map,
    _required_next_execution_role,
    _tc,
    active_replan_checkpoints,
    reconcile_replan_checkpoints,
    record_replan_checkpoint,
    replan_checkpoint_plan_errors,
    replan_checkpoint_spawn_rejection,
)
from .state_store import (  # noqa: F401
    _TASK_STATE_MISSING,
    _atomic_replace_task_state,
    _empty_phase_state,
    _ensure_phase_state_defaults,
    _first_active_phase_id,
    _merge_state_lists,
    _read_task_state_for_merge,
    _state_value_token,
    _tc,
    _three_way_merge_task_state,
    contract_hash_for_phase,
    task_state_summary,
    write_task_state,
)
from .state_utils import (  # noqa: F401
    _append_unique,
    _first_phase_id,
    _is_empty_value,
    _phase_state,
    _positive_int,
    _state_path,
    _string_list,
    _tc,
    _unique_paths,
)
from .validators import (  # noqa: F401
    _allow_empty_with_outcome_from_expected,
    _canonical_validator_params,
    _compile_validator_regex,
    _cumulative_row_quality,
    _dedupe_and_check_validators,
    _default_provenance_field_spec,
    _file_receipt_completed,
    _file_receipt_succeeded,
    _float_value,
    _has_field_provenance_validator,
    _load_extraction_artifacts,
    _nonempty_fields_from_expected,
    _norm_compare_text,
    _normalize_expected_artifact_contract,
    _normalize_provenance_validator_fields,
    _normalize_validators,
    _prefer_cumulative_row,
    _provenance_evidence_aliases,
    _provenance_field_specs,
    _provenance_required_fields,
    _receipt_saved_paths,
    _row_count_validator_value,
    _row_has_nonempty_value,
    _run_file_validator,
    _run_validator,
    _selected_file_count,
    _similarity,
    _tc,
    _validate_action_outcome,
    _validate_cumulative_artifacts,
    _validate_field_provenance,
    _validator_semantic_signature,
    make_row_preference,
    run_row_validator,
)
