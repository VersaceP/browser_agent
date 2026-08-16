"""
harness.tools.browser_tools - BrowserAgent tool schemas and dispatch factory.

This package is the namespace hub: submodules implement the
tools and route cross-module calls through this package (see _bt() in
each submodule) so attribute patching keeps working.
"""

import asyncio
import base64
import copy
import hashlib
import re
import sys
import time
import uuid
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import json
from pathlib import Path
from urllib.parse import urlparse

from abcp_client import ABCPTransportError
from harness.observation.challenge_detector import (
    HIGH_CONFIDENCE_CHALLENGE_KEYWORDS,
    ChallengeTracker,
    detect_structural_challenge,
    detect_structural_challenge_from_lines,
    extract_page_id,
    is_lingering_loading_title,
)
from harness.observation.content_completeness import (
    ContentCompletenessTracker,
    content_completeness_observation_facts,
)
from harness.diagnostics.error_classification import attach_error_classification
from harness.evidence.extraction_artifacts import (
    field_names_from_specs,
    save_extraction_artifact,
    validate_extraction_rows,
)
from harness.evidence.artifact_evidence import detect_blocker_data_rows
from harness.results.call_outcome import (
    action_runtime_info,
    auto_hitl_is_actionable,
    classify_call_outcome,
    evaluate_grant,
    page_state_evidence_ok,
    replay_forbidden,
)
from harness.fleet.runtime import FleetClickGateTimeout
from harness.hitl import wait_for_hitl_resume
from harness.lifecycle import LifecycleContext, lifecycle_for
from harness.local_fs import local_fs_read, local_fs_search
from harness.observation.overlay_actions import (
    compute_backdrop_point,
    backdrop_point_is_safe,
    find_close_control,
    is_sensitive_method,
    is_sensitive_target,
    normalized_point_to_css,
    visible_layers_occluded,
    vl_dismiss_target_is_safe,
)
from harness.observation.overlay_detector import (
    detect_overlay_from_result,
    title_looks_like_auth_page,
)
from harness.observation.semantic_index import discover_selector_candidates
from harness.observation.page_lifecycle import (
    AUTOMATION_UNAVAILABLE_FAILURE,
    PageLifecycleTracker,
)
from harness.observation.event_observer import unwrap_notification
from harness.observation.verifiers import (
    build_collection_oracle,
    build_read_only_oracle,
    collect_rows,
    probe_occluder,
    probe_viewport_metrics,
    SemanticLocator,
    verify_field_value,
    verify_overlay_gone,
)
from harness.offload import offload_large_tool_result
from harness.progress import NO_ARTIFACT_DIAGNOSTIC_TOOLS, extraction_artifact_count
from harness.pacing import wait_between_rows
from harness.observation.render_recovery import build_render_recovery_runner
from harness.screenshot_policy import normalize_screenshot_output_params
from harness.runtime_evaluation import (
    MAIN_WORLD_REQUIRED_PREFIX,
    RuntimeEvaluationService,
    runtime_last_resort_evidence,
)
from harness.task_control import (
    phase_prior_artifact_paths,
    validate_worker_artifacts,
)
from harness.task_types import resolve_task_type_fail_closed
from harness.tool_policy import (
    disabled_reason_for_method,
    hidden_harness_tools_for_task_type,
    mask_params,
)
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.parsers import (
    attach_method_schema,
    ensure_required_purpose,
    parse_browser_call_params,
    parse_direct_capability_params,
)
from harness.tools.registry import ToolContext, ToolRegistry
from harness.storage.base import normalize_external_path
from harness.utils import (
    JsonDict,
    exception_payload,
    optional_int,
    storage_for_logger,
    trim_large_strings,
)
from harness.workflow_runtime import (
    workflow_execution_disabled_result,
    workflow_execution_enabled,
)
from .schemas import EVAL_JS_REASON_KINDS, _browser_input_schemas
from .axtree_state import (
    AXTREE_INVALIDATING_METHODS,
    _apply_recovered_target,
    AXTREE_ID_RE,
    _axtree_ids_from_params,
    _axtree_ids_from_value,
    _axtree_lines_from_value,
    _axtree_nodes_from_lines,
    _axtree_seen_ids,
    _axtree_seen_signature,
    _browser_side_rematch_mode,
    _check_stale_axtree_target,
    _invalidate_axtree_snapshot,
    _observe_axtree_state_after,
    _precompute_axtree_snapshot,
    _record_axtree_history,
)
from harness.vl import visual_verify_image
from harness.workflow_policy import validate_workflow_params

from .composites.dismiss_overlay import (
    DISMISS_OVERLAY_MAX_ATTEMPTS,
    DISMISS_OVERLAY_MAX_DURATION_MS,
    _dismiss_overlay,
    _maybe_retry_original_action,
    _vl_overlay_arbiter,
)
from .composites.collect_items import (
    COLLECT_ITEMS_DEFAULT_FIELDS,
    COLLECT_ITEMS_HARVEST_LIMIT,
    COLLECT_ITEMS_MAX_DURATION_MS,
    COLLECT_ITEMS_MAX_ROUNDS,
    COLLECT_ITEMS_MAX_WINDOWS,
    COLLECT_ITEMS_SETTLE_MS,
    COLLECT_ITEMS_STABILITY_THRESHOLD,
    _collect_dedup_key,
    _collect_interrupt_result,
    _collect_items,
    _collect_items_materialize,
    _collect_overlay_recovery,
    _collect_overlay_stop_reason,
)
from .composites.fill_field_verified import (
    FILL_FIELD_STOPWORDS,
    _axtree_node_name,
    _fill_field_action,
    _fill_field_keywords,
    _fill_field_verified,
)

from .auto_intercept import (  # noqa: F401
    AUTO_INTERCEPT_MAX_PER_PAGE,
    _auto_intercept_mode,
    _blocked_target_id,
    _maybe_auto_intercept_overlay,
    _record_microloop_telemetry,
)
from .bindings import (  # noqa: F401
    _REPERCEPTION_ALLOWED_METHODS,
    _apply_fleet_binding,
    _check_page_binding,
    _claim_fleet_auth_barrier_for_hitl,
    _claim_ownerless_fleet_auth_barrier_for_page_create,
    _filter_page_list_response,
    _fleet_auth_barrier_after_call,
    _fleet_auth_barrier_before_call,
    _fleet_reuse_enabled,
    _observe_page_binding_after,
    _page_is_claimable,
    _quarantine_workflow_result_after_auth_change,
    _relinquish_fleet_auth_resolver_after_failed_pause,
    _relinquish_fleet_auth_resolver_after_failed_recovery_page_create,
    _shown_page_inventory_rows,
    _workflow_auth_started_generation,
)
from .capability import (  # noqa: F401
    _TRUSTED_COLLECTION_RUNTIME_TOKEN,
    _execute_browser_capability_tool,
    _find_in_axtree,
    _invoke_browser_method,
)
from .dispatch import (  # noqa: F401
    BROWSER_TOOLS,
    BrowserToolDispatcher,
    SCREENSHOT_ALLOWED_PURPOSE_RE,
    SCREENSHOT_MISUSE_RE,
    _NAVIGATION_CONTEXT_KINDS,
    _allowed_tool_hint,
    _browser_call,
    _browser_collect_items,
    _browser_dismiss_overlay,
    _browser_execute_browser_workflow,
    _browser_execute_selected_skill,
    _browser_fill_field_verified,
    _browser_final_answer,
    _browser_find_in_axtree,
    _browser_input_schemas_cached,
    _browser_local_fs_read,
    _browser_local_fs_search,
    _browser_navigate_verified,
    _browser_record_extraction,
    _browser_schema_for,
    _browser_visual_verify,
    _capability_methods_key,
    _contains_truncated_receipt,
    _execute_browser_tool_impl,
    _lifecycle_page_id,
    _page_lifecycle_after_action,
    _page_lifecycle_before_action,
    _page_lifecycle_guard_before,
    _prepare_navigation_context,
    _prepare_runtime_evaluation,
    _record_selected_skill_tool_trace,
    build_browser_agent_tool_specs,
    build_browser_tool_dispatcher,
    execute_browser_tool,
)
from .downloads import (  # noqa: F401
    DOWNLOAD_TIMEOUT_RECONCILIATION_DELAY_SECONDS,
    _classify_download_reconciliation,
    _download_operation_key,
    _download_receipt_store,
    _download_records,
    _download_resource_registration_store,
    _download_start_timed_out,
    _reconcile_download_start_timeout,
    _refresh_active_download_response,
    _register_download_resource,
    _remember_download_record,
    _remember_unverified_download_timeout,
    _reusable_download_response,
)
from .hitl import (  # noqa: F401
    _adjudicate_and_maybe_hitl,
    _autosolve_cleared,
    _autosolve_cleared_result,
    _capture_hitl_pause_snapshot,
    _clear_challenge_state_after_recovery,
    _compact_vl_for_wait,
    _count_hitl_pause_round,
    _enrich_pause_with_wait,
    _ensure_hitl_request_reason,
    _hitl_admission,
    _hitl_pause_rounds,
    _hitl_pause_snapshot,
    _hitl_pause_succeeded,
    _hitl_resumed_suggested_prompt,
    _make_hitl_challenge_verifier,
    _maybe_auto_hitl_for_challenge,
    _maybe_autosolve_before_hitl,
    _maybe_autosolve_before_model_pause,
    _model_pause_challenge_evidence,
    _normalize_post_hitl_confirmation,
    _post_hitl_raw_browser_call,
    _post_hitl_recovery_loop,
    _post_hitl_recovery_vl_check,
    _post_hitl_repause_guard_ms,
    _post_hitl_structural_challenge_check,
    _prompt_post_hitl_confirmation,
    _reason_with_autosolve,
    _record_post_hitl_repause_guard,
    _refresh_and_wait_for_post_hitl_retry,
    _refuse_hitl,
    _release_fleet_auth_after_hitl_refusal,
    _repause_for_structural_challenge,
    _request_hitl_for_challenge,
    _result_has_paused_error,
    _verify_and_open_fleet_auth_barrier,
)
from .navigate import (  # noqa: F401
    NAVIGATE_VERIFIED_AX_REFRESH_MAX_ATTEMPTS,
    NAVIGATE_VERIFIED_DEFAULT_STATE_CHECKS,
    NAVIGATE_VERIFIED_MAX_STATE_CHECKS,
    NAVIGATE_VERIFIED_STATE_RECHECK_SECONDS,
    _NAVIGATION_FAILED_STATUSES,
    _NAVIGATION_IN_FLIGHT_STATUSES,
    _SELECT_FAILURE_GUIDANCE,
    _URL_DEFAULT_PORTS,
    _apply_select_failure_guidance,
    _auto_hitl_is_actionable,
    _cancel_waiter,
    _challenge_score,
    _clear_navigation_challenge_state,
    _content_completeness_upstream_blocker,
    _ensure_content_completeness_tracker,
    _fresh_page_settlement_task,
    _hitl_digest,
    _invoke_result_failed,
    _loop_interrupt_from_result,
    _loop_interrupt_summary,
    _make_url_matcher,
    _navigate_challenge_blocked_result,
    _navigate_dispatch_failure_result,
    _navigate_hitl_result,
    _navigate_pattern_invalid_result,
    _navigate_verified,
    _navigate_verified_impl,
    _navigation_state_snapshot,
    _nested_response_error,
    _normalize_url_for_equivalence,
    _notify_navigation_success,
    _observe_content_completeness_after,
    _observe_navigation_progress_after,
    _page_challenge_summary,
    _page_inventory_is_discoverable,
    _possible_double_escape,
    _read_page_state_once,
    _refresh_axtree_after_verified_navigation,
    _result_has_auto_hitl,
    _result_page_ids_for_inventory,
    _settle_page_inventory_signal,
    _strip_challenge_fields,
    _transport_error_metadata,
)
from .page_create import (  # noqa: F401
    FLEET_LOSS_ERROR_CODES,
    _assigned_fleet_lost_result,
    _attach_navigation_check,
    _attach_runtime_strategy_hints,
    _fleet_loss_signal,
    _is_page_create_32005_failure,
    _looks_like_challenge_title,
    _page_create_error_text,
    _page_create_infrastructure_classification,
    _page_create_probe_call,
    _page_create_terminal_answer,
    _page_state_is_usable,
    _pages_from_value,
    _raw_response_data,
    _recover_page_create_32005,
    _response_data,
    _urls_same_destination,
)
from .progress_obs import (  # noqa: F401
    _PROGRESS_OBSERVATION_IDENTITY_KEYS,
    _annotate_axtree_offload,
    _check_cross_task_memory_scope,
    _check_worker_contract,
    _gate_subject_tool,
    _is_own_artifact_read,
    _method_pattern_matches,
    _observe_progress_after,
    _observe_progress_before,
    _observe_unrecorded_extraction_before,
    _progress_observation_is_new,
    _record_extraction_persisted,
)
from .record_extraction import (  # noqa: F401
    PLACEHOLDER_URL_RE,
    PLACEHOLDER_VALUE_RE,
    _is_advisory_record_failure,
    _merge_repair_patch_rows,
    _record_extraction,
    _record_extraction_content_warnings,
    _record_extraction_persist,
    _record_extraction_schema_warnings,
    _repair_field_requires_nonempty,
    _repair_resolution_has_source_evidence,
    _repair_value_is_empty,
    _repair_visual_checks_enabled,
    _row_reports_placeholder,
    _validate_recorded_extraction,
)
from .runtime_eval import (  # noqa: F401
    _attach_runtime_json_value,
    _build_runtime_json_expression,
    _invoke_trusted_collection_template,
    _rows_from_eval_value,
    _runtime_any_json_payload,
    _runtime_attempt_receipt,
    _runtime_evaluation_error_text,
    _runtime_execution_metadata,
    _runtime_main_fallback_signaled,
    _runtime_response_world_metadata_supplied,
    _runtime_response_world_verified,
)
from .validation import (  # noqa: F401
    DOM_GET_IMG_MAX_TARGETS,
    _SCROLL_MODE_INSTRUCTION,
    _annotate_dom_batch_response,
    _attach_normalized_handles,
    _check_id_param_format,
    _check_nested_id_format,
    _check_screenshot_misuse,
    _check_scroll_param_requirements,
    _check_select_param_requirements,
    _check_target_param_requirements,
    _default_semantic_tree_shadow_dom,
    _non_empty_param,
    _non_negative_numeric_param,
    _normalize_dom_get_img_output,
    _normalize_screenshot_output,
)
from .visual import (  # noqa: F401
    REALITY_CHECK_CAPTURE_FAILURE_LIMIT,
    _arbiter_error_text,
    _arbiter_next_instruction,
    _layers_from_result,
    _log_dismiss_overlay,
    _maybe_reality_check,
    _maybe_vl_arbitrate,
    _normalized_repair_page,
    _page_reality_check_instruction,
    _promote_visual_locate,
    _reality_check_instruction,
    _reality_check_region,
    _reality_check_summary,
    _record_repair_visual_evidence,
    _region_hint_text,
    _repair_identity_text,
    _repair_page_binding,
    _repair_visual_target_signature,
    _result_occlusion_blocked,
    _screenshot_saved_path,
    _scroll_region_into_view,
    _validated_repair_visual_targets,
    _verify_repair_visual_page,
    _viewport_from_layers,
    _visual_verify,
)
