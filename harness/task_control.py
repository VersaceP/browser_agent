"""
harness.task_control - Task plans, worker contracts, task state, and artifact validators.
"""

from __future__ import annotations

import json
import copy
import hashlib
import re
import time
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
)
from harness.content_completeness import (
    content_completeness_config_errors,
    normalize_content_completeness_config,
)
from harness.extraction_artifacts import field_name_from_spec, field_names_from_specs
from harness.artifact_evidence import (
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
from harness.auth_fleet import normalize_auth_verification_contract
from harness.fleet_coordinator import normalize_page_policy, normalize_reuse_scope
from harness.file_evidence import saved_paths_from_value
from harness.row_ledger import ROW_OUTCOMES, field_absence_accepted
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
    safe_path_component,
    trim_large_strings,
)


TASK_PLAN_FILE = "task_plan.json"
TASK_STATE_FILE = "task_state.json"
REPEAT_GUARD_REJECTION_LOCK_THRESHOLD = 3
SEMANTIC_TERMINAL_CLASSIFICATIONS = frozenset({
    "target_absent",
    "instruction_infeasible",
    "blocked_content_suppression",
})
TERMINAL_PHASE_STATUSES = frozenset({
    "validated_done",
    "phase_failed",
    "blocked_by_challenge",
    "hitl_required",
    "hitl_timeout",
    "page_settled_after_hitl",
    "stale_pause_deadlock",
    "target_absent",
    "instruction_infeasible",
    "blocked_content_suppression",
    "blocked_by_dependency",
    "session_fleet_lost",
})
RECOVERABLE_ROUTING_PHASE_STATUSES = frozenset({"fleet_assignment_lost"})
RETRYABLE_PHASE_FAILURE_STATUSES = frozenset({
    "validation_failed",
    "failed",
    "cancelled",
    "unknown",
    *RECOVERABLE_ROUTING_PHASE_STATUSES,
})
BLOCKING_DEPENDENCY_STATUSES = TERMINAL_PHASE_STATUSES - {"validated_done"}
# Statuses a replan resets to a clean slate. phase_failed is the Lead's
# explicit retry-via-replan path. blocked_by_dependency is DERIVED state:
# every blocking dependency status is itself terminal, so the only way the
# dependency recovers is a replan — preserving the stale marker across that
# replan would deadlock the dependent phase forever (next_pending_phase
# skips terminal statuses and never re-derives them). Reset it and let the
# new plan re-derive blocking from the (possibly fixed) dependency.
REPLAN_RESET_STATUSES = frozenset({"phase_failed", "blocked_by_dependency"})
# Cross-replan failure budget for one OBJECTIVE (see objective_fingerprint):
# per-phase max_attempts is escapable by replanning under a fresh phase id
# (attempts reset with the new id — the 2cb616 v1→v2→v3 loop), so failures
# are also accumulated per objective fingerprint, which survives replans.
# The budget counts ATTEMPTS, not phase ids: after 6 same-objective failures
# no new phase id gets more budget (how many ids that spans depends on when
# the Lead replans).
OBJECTIVE_MAX_ATTEMPTS = 6
# Slot/connection startup failures happen before a BrowserAgent exists, so
# they cannot legitimately consume either phase attempts or the business
# objective budget.  They still need a durable bound: otherwise a Lead can
# replan or respawn the same broken routing path until its own step budget is
# exhausted.  Two identical failures are enough to establish that the same
# acquisition path is not making progress; a changed routing contract gets a
# fresh budget.
SPAWN_ACQUISITION_MAX_FAILURES = 2
SPAWN_ACQUISITION_FLEET_COOLDOWN_SECONDS = 30
EXECUTION_ROLES = frozenset({
    "probe",
    "validation",
    "bulk",
    # Evidence-driven continuation for remaining homogeneous rows when the
    # preceding run did not produce a reusable fast-path candidate.  It keeps
    # the cohort/checkpoint fence without pretending confidence was validated.
    "continuation",
    "remediation",
})

AXTREE_ID_ANYWHERE_RE = re.compile(r"\b\d+:-?\d+:-?\d+\b")
VOLATILE_HANDLE_KEYS = {
    "pageId",
    "page_id",
    "fleetId",
    "fleet_id",
    "axTreeId",
    "axNodeId",
    "domNodeId",
    "nodeId",
    "selector",
    "sourceSelectorOrAxId",
}

VALID_STAGE_HINTS = {
    "collection",
    "detail_sections",
    "attribute_links",
    "form_interaction",
    "computed_relationship",
    "generic",
}

SENSITIVE_PROVENANCE_FIELD_MARKERS = {
    "rank",
    "order",
    "index",
    "position",
    "priority",
    "price",
    "score",
    "status",
    "count",
    "timestamp",
    "date",
    "quantity",
    "qty",
    "total",
    "rating",
    "stars",
    "views",
    "votes",
    "published_at",
    "publishedat",
    "created_at",
    "createdat",
    "updated_at",
    "updatedat",
    "version",
}

FILE_RECEIPT_ONLY_VALIDATOR_TYPES = FILE_VALIDATOR_TYPES - {"upload_confirmed"}

# This intentionally targets explicit persistence language, not every mention
# of a "download URL". Rendered page images are excluded because DOM.getImg is
# a native DOM export available to web_scrape; videos/PDFs/archives/arbitrary
# URL files require the Download domain and therefore file_download.
_NON_IMAGE_FILE_SAVE_RE = re.compile(
    r"(?:\b(?:download|save|export|persist|write)\b.{0,60}"
    r"\b(?:file|video|pdf|archive|zip|csv|media|asset|disk|folder|directory)\b"
    r"|\b(?:file|video|pdf|archive|zip|csv|media|asset)\b.{0,40}"
    r"\b(?:to disk|to (?:a )?(?:folder|directory)|locally|download|save|export)\b"
    r"|(?:下载|保存|落盘|写入|导出|落地).{0,30}(?:文件|视频|PDF|压缩包|归档|磁盘|目录|文件夹|本地|桌面)"
    r"|(?:文件|视频|PDF|压缩包|归档).{0,30}(?:下载|保存|存到|落盘|落地))",
    re.I | re.S,
)
_NON_IMAGE_ASSET_TOKEN_RE = re.compile(
    r"\b(?:video|pdf|archive|zip|csv|media|asset)\b|视频|PDF|压缩包|归档",
    re.I,
)

# High-frequency intuitive names models emit before learning the canonical
# enum (task 9d5655d3 burned two plan rejections on exactly these guesses).
# Normalized with a visible warning receipt (validator_type_alias) — never
# silently: the emit_task_plan schema enum is the primary fix, this is the
# fallback for gateway models that ignore input_schema.
VALIDATOR_TYPE_ALIASES = {
    "url_format": "url_pattern",
    "rank_range": "range",
    "value_range": "range",
    "no_duplicates": "unique",
    "unique_fields": "unique",
}

# Authentication walls and human-verification challenges are runtime
# interrupts for the worker that encounters them.  A plan may still contain a
# standalone diagnostic probe when that is the user's actual goal, but it must
# not serialize that probe into a second worker whose only job is to request
# HITL on the same gate.
_AUTH_PLAN_MARKERS = (
    "auth", "authentication", "login", "log in", "sign in", "signin",
    "sso", "oauth", "password", "captcha", "human verification",
    "verification code", "2fa", "mfa", "hitl", "登录", "登陆", "认证",
    "验证码", "扫码", "人机",
)
_AUTH_PROBE_MARKERS = (
    "probe", "detect", "identify", "assess", "check gate", "check login",
    "gate type", "gate evidence", "auth required", "login required",
    "门禁", "探测", "识别", "判断", "确认是否",
)
_AUTH_TRANSITION_MARKERS = (
    "hitl.requestpause", "requestpause", "request hitl", "request pause",
    "complete login", "complete authentication", "handle login",
    "perform login", "after login", "post-login", "verify login",
    "login status", "login_status", "人工登录", "请求 hitl", "完成登录",
    "处理登录", "登录后", "验证登录",
)
_HITL_INTERRUPT_MARKERS = (
    "hitl.requestpause", "requestpause", "request hitl", "request pause",
    "请求 hitl", "请求人工暂停", "暂停等待人工",
)
_AUTH_PROBE_FIELD_MARKERS = frozenset({
    "gatetype", "gateevidence", "authrequired", "authenticationrequired",
    "loginrequired", "requireslogin", "requiresauth", "authsurface",
    "loginsurface", "authevidence", "loginevidence",
    "nextphaserequireshitl",
})

# These are protocol/control fields, not user-requested business values.  The
# blocker-as-data guard deliberately exempts them so a diagnostic artifact may
# truthfully report a gate while ordinary fields cannot be padded with the same
# explanation.




def _auth_phase_kind(phase: JsonDict) -> str:
    """Classify only explicit auth-planning phases.

    Returns ``probe``, ``transition``, or ``""``.  Ordinary business phases
    intentionally classify as empty even though they may encounter an
    unpredictable login wall at runtime.
    """
    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    fields = field_names_from_specs(expected.get("fields") or [])
    required = field_names_from_specs(expected.get("required_fields") or [])
    normalized_fields = {
        _normalized_semantic_token(field) for field in [*fields, *required]
    }
    parts = [
        phase.get("id"),
        phase.get("objective"),
        phase.get("worker_task"),
        phase.get("stage_hint_reason"),
        phase.get("context"),
        *fields,
        *required,
    ]
    text = " ".join(str(item or "") for item in parts)
    if not any(
        contains_semantic_marker(text, marker) for marker in _AUTH_PLAN_MARKERS
    ):
        return ""
    if any(
        contains_affirmative_semantic_marker(text, marker)
        for marker in _AUTH_TRANSITION_MARKERS
    ):
        return "transition"
    if normalized_fields & _AUTH_PROBE_FIELD_MARKERS:
        return "probe"
    if any(
        contains_semantic_marker(text, marker) for marker in _AUTH_PROBE_MARKERS
    ):
        return "probe"
    return ""




def _allow_empty_fields(expected: JsonDict) -> Set[str]:
    out: Set[str] = set()
    for key in (
        "allow_empty", "optional_empty", "fields", "required_fields",
        "field_nonempty", "nonempty_fields",
    ):
        value = expected.get(key)
        if key in {"allow_empty", "optional_empty"} and isinstance(value, list):
            out.update(field_names_from_specs(value))
        if not isinstance(value, list):
            continue
        for spec in value:
            if not isinstance(spec, dict):
                continue
            if spec.get("allow_empty") is True or spec.get("optional_empty") is True:
                name = field_name_from_spec(spec)
                if name:
                    out.add(name)
    return out


def _nonempty_validator_fields(validators: List[JsonDict]) -> Set[str]:
    out: Set[str] = set()
    for validator in validators:
        if str(validator.get("type") or "") != "field_nonempty":
            continue
        out.update(field_names_from_specs(validator.get("fields") or []))
    return out


def _instruction_assigns_blocker_to_business_field(
    worker_task: str,
    business_fields: List[str],
) -> Optional[str]:
    clauses = re.split(r"(?<=[.!?。！？;；])|\n+", str(worker_task or ""))
    for clause in clauses:
        if not (
            _BLOCKER_TEMPLATE_SEARCH_RE.search(clause)
            or _PLACEHOLDER_LITERAL_RE.search(clause)
        ):
            continue
        for field in business_fields:
            escaped = re.escape(field)
            assignment = re.compile(
                rf"(?:\b(?:set|write|record|store|save|fill|populate|put|use)\b"
                rf"[^.!?。！？;；\n]{{0,48}}(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
                rf"|(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
                rf"[^.!?。！？;；\n]{{0,32}}(?:设为|写入|记录为|填入|保存为|=|\bto\b|\bas\b)"
                rf"|(?:将|把)[^.!?。！？;；\n]{{0,24}}(?<![a-z0-9_]){escaped}"
                rf"(?![a-z0-9_])[^.!?。！？;；\n]{{0,16}}(?:设为|写入|记录为|填入|保存为))",
                re.IGNORECASE,
            )
            if assignment.search(clause) and contains_affirmative_semantic_marker(
                clause, field,
            ):
                return field
    return None


def _reject_phase_execution_integrity(
    *,
    phase_id: str,
    objective: str,
    worker_task: str,
    stage_hint_reason: str,
    expected: JsonDict,
    validators: List[JsonDict],
    errors: List[str],
) -> None:
    conflicts = sorted(_allow_empty_fields(expected) & _nonempty_validator_fields(validators))
    if conflicts:
        errors.append(
            f"phase {phase_id}: fields cannot be both allow_empty and"
            f" field_nonempty: {conflicts}"
        )

    phase_view = {
        "id": phase_id,
        "objective": objective,
        "worker_task": worker_task,
        "stage_hint_reason": stage_hint_reason,
        "expected_artifact": expected,
    }
    hitl_mentioned = any(
        contains_semantic_marker(worker_task, marker)
        for marker in _HITL_INTERRUPT_MARKERS
    )
    hitl_affirmative = any(
        contains_affirmative_semantic_marker(worker_task, marker)
        for marker in _HITL_INTERRUPT_MARKERS
    )
    if hitl_mentioned and not hitl_affirmative and _auth_phase_kind(phase_view) != "probe":
        errors.append(
            f"phase {phase_id}: a business worker must not negate the runtime"
            " Hitl.requestPause SOP; authentication and human verification are"
            " runtime interrupts for the worker that encounters them"
        )

    assigned_field = _instruction_assigns_blocker_to_business_field(
        worker_task,
        _business_fields_from_expected(expected),
    )
    if assigned_field:
        errors.append(
            f"phase {phase_id}: worker_task assigns blocker/placeholder text to"
            f" business field {assigned_field!r}; record the blocker separately"
        )


def _validate_task_type_capability_match(
    *,
    phase_id: str,
    task_type: str,
    objective: str,
    worker_task: str,
    stage_hint_reason: str,
    validators: List[JsonDict],
    errors: List[str],
    warnings: List[JsonDict],
) -> None:
    """Hard-reject structured contradictions; warn on prose heuristics.

    Validators are mechanically decidable and may safely gate execution.
    Natural-language intent is not: DOM.getImg and record_extraction both write
    harness-managed files without requiring Download.*, while ordinary phrasing
    varies too much to classify without false positives/negatives. Prose can
    therefore request Lead review but never reject a plan.
    """
    validator_types = {
        str(item.get("type") or "").strip()
        for item in validators
        if isinstance(item, dict)
    }
    image_export = "image_exported" in validator_types
    if "download_completed" in validator_types and task_type != "file_download":
        errors.append(
            f"phase {phase_id}: validator download_completed requires task_type"
            f" 'file_download'; got {task_type!r}"
        )
    if (
        "file_integrity" in validator_types
        and not image_export
        and task_type != "file_download"
    ):
        errors.append(
            f"phase {phase_id}: validator file_integrity requires task_type"
            " 'file_download' unless it validates a DOM.getImg image_exported"
            f" receipt; got {task_type!r}"
        )
    upload_validators = validator_types & {"upload_selected", "upload_confirmed"}
    if upload_validators and task_type not in {"file_upload", "form_filling"}:
        errors.append(
            f"phase {phase_id}: validators {sorted(upload_validators)} require"
            " task_type 'file_upload' or 'form_filling'"
            f"; got {task_type!r}"
        )
    declared_work = " ".join((objective, worker_task, stage_hint_reason))
    lowered_work = declared_work.lower()
    harness_managed_write = (
        ("dom.getimg" in lowered_work or "record_extraction" in lowered_work)
        and "download." not in lowered_work
        and not _NON_IMAGE_ASSET_TOKEN_RE.search(declared_work)
    )
    if (
        _NON_IMAGE_FILE_SAVE_RE.search(declared_work)
        and task_type != "file_download"
        and not harness_managed_write
    ):
        warnings.append({
            "type": "task_type_file_intent_review",
            "phaseId": phase_id,
            "taskType": task_type,
            "message": (
                f"phase {phase_id}: prose may request a non-image file save,"
                f" but task_type is {task_type!r}. This is advisory because"
                " prose is not a mechanical capability contract. If the phase"
                " uses Download.*, re-emit it as file_download and add"
                " download_completed + file_integrity validators; DOM.getImg"
                " and record_extraction do not require file_download."
            ),
        })


def _reject_serial_auth_handoff(phases: List[JsonDict], errors: List[str]) -> None:
    """Reject the concrete probe-worker -> HITL-worker waste pattern.

    The guard is deliberately narrow: phases must be adjacent, the first must
    be an explicit auth diagnostic, and the second must serialize on it either
    implicitly or through depends_on.  A lone diagnostic probe remains valid.
    """
    for probe, transition in zip(phases, phases[1:]):
        if _auth_phase_kind(probe) != "probe":
            continue
        if _auth_phase_kind(transition) != "transition":
            continue
        dependencies = _phase_dependency_ids(transition)
        serialized_on_probe = (
            dependencies is None
            or str(probe.get("id") or "") in dependencies
        )
        if not serialized_on_probe:
            continue
        errors.append(
            "auth phase split is not allowed: diagnostic phase"
            f" {probe.get('id')!r} is followed by HITL/login phase"
            f" {transition.get('id')!r}. Authentication and human verification"
            " are runtime interrupts: merge detection, Hitl.requestPause, and"
            " post-resume verification into the worker performing the protected"
            " task. Keep a probe-only phase only when gate diagnosis itself is"
            " the final user objective."
        )


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _validated_task_type(
    raw: Any,
    *,
    errors: List[str],
    warnings: List[JsonDict],
    where: str,
) -> str:
    """Alias-normalize + membership-check ONE task_type field ('' when absent).
    Unknown values must error everywhere the field is accepted: the policy
    layer looks task_type up (TASK_TYPE_DISABLED_DOMAINS.get) and an unknown
    value silently disables NOTHING — a typo would grant a worker every method
    domain (review P2: worker_contract.task_type was never checked)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    canonical = normalize_task_type(text)
    if canonical != text:
        warnings.append({
            "type": "task_type_alias",
            "field": where,
            "input": text,
            "canonical": canonical,
            "message": (
                f"{where}: {text!r} is accepted as an alias; use canonical"
                f" task_type {canonical!r} in future plans."
            ),
        })
        text = canonical
    if text not in VALID_TASK_TYPES:
        errors.append(
            f"{where} must be one of {task_type_choices_for_error()}; got {text!r}"
        )
    return text


def _validate_pacing(value: Any, errors: List[str], *, where: str) -> JsonDict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{where} must be an object")
        return {}
    allowed = set(PACING_FIELDS)
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        errors.append(f"{where} contains unknown fields: {unknown}")
    for key in PACING_INTERVAL_FIELDS:
        if key not in value:
            continue
        try:
            number = float(value[key])
        except (TypeError, ValueError):
            errors.append(f"{where}.{key} must be a number")
            continue
        if number < 0 or number > MAX_PACING_INTERVAL_SECONDS:
            errors.append(
                f"{where}.{key} must be between 0 and"
                f" {MAX_PACING_INTERVAL_SECONDS:g}"
            )
    if "jitter_ratio" in value:
        try:
            jitter = float(value["jitter_ratio"])
        except (TypeError, ValueError):
            errors.append(f"{where}.jitter_ratio must be a number")
        else:
            if jitter < 0 or jitter > 1:
                errors.append(f"{where}.jitter_ratio must be between 0 and 1")
    normalized = normalized_pacing(value)
    # Preserve override semantics: omitted phase keys inherit the plan value;
    # materializing them as zero here would silently erase that inheritance.
    return {
        key: normalized[key]
        for key in allowed
        if key in value
    }


_ROW_SELECTION_LIMITS = {"probe": 1, "validation": 2}


def _adapt_cohort_row_selection(
    worker_contract: JsonDict, errors: List[str], *, phase_id: str,
) -> str:
    """Accept `cohort_source` + `row_selection` and express it as a batch.

    The two say what the older single `batch_source` conflated: which cohort
    this phase belongs to, and which of its rows this worker takes. A probe
    that owns one item can then still name its cohort, instead of having to
    pose as a batch so a checkpoint can be recorded at all. Rows are read from
    the validated artifact by index; the model never re-types row content.
    """
    cohort = worker_contract.get("cohort_source")
    selection = worker_contract.get("row_selection")
    if cohort is None and selection is None:
        return ""
    if not isinstance(cohort, dict):
        errors.append(
            f"phase {phase_id}: worker_contract.cohort_source must be an object"
            " naming artifact_name and identity_field"
        )
        return ""
    if not isinstance(selection, dict):
        errors.append(
            f"phase {phase_id}: worker_contract.row_selection must be an object"
            " with mode and source_indices"
        )
        return ""
    if worker_contract.get("batch_source") is not None:
        errors.append(
            f"phase {phase_id}: declare cohort_source or batch_source, not both"
        )
        return ""

    artifact_name = str(cohort.get("artifact_name") or "").strip()
    identity_field = str(cohort.get("identity_field") or "").strip()
    if not artifact_name or not identity_field:
        errors.append(
            f"phase {phase_id}: cohort_source requires artifact_name and"
            " identity_field"
        )
        return ""

    mode = str(selection.get("mode") or "").strip()
    if mode and mode not in EXECUTION_ROLES:
        errors.append(
            f"phase {phase_id}: row_selection.mode must be one of"
            f" {sorted(EXECUTION_ROLES)}; got {mode!r}"
        )
        return ""
    raw_indices = selection.get("source_indices")
    indices = [
        int(item) for item in raw_indices
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    ] if isinstance(raw_indices, list) else []
    if not indices:
        errors.append(
            f"phase {phase_id}: row_selection.source_indices must list at least"
            " one non-negative index into the cohort artifact"
        )
        return ""
    limit = _ROW_SELECTION_LIMITS.get(mode)
    if limit is not None and len(indices) > limit:
        errors.append(
            f"phase {phase_id}: row_selection.mode={mode} may select at most"
            f" {limit} row(s); a confidence stage that opens many pages is a"
            " bulk run wearing its name"
        )
        return ""

    worker_contract["batch_source"] = {
        "artifact_name": artifact_name,
        "identity_field": identity_field,
        "selector": {"indices": sorted(set(indices))},
    }
    cohort_selector = cohort.get("cohort_selector")
    if isinstance(cohort_selector, dict):
        worker_contract["batch_source"]["cohort_selector"] = cohort_selector
    if mode and not worker_contract.get("execution_role"):
        worker_contract["execution_role"] = mode
    return mode


def _normalize_batch_contract(
    worker_contract: JsonDict,
    errors: List[str],
    *,
    phase_id: str,
    user_task: str = "",
) -> str:
    """Validate the declared source of auto-materialized worker batch rows.

    Returns the role declared by `row_selection.mode`, if any, so the caller
    can raise it to the phase — where the plan-level role gates read it.
    """

    declared_role = _adapt_cohort_row_selection(
        worker_contract, errors, phase_id=phase_id,
    )
    raw_source = worker_contract.get("batch_source")
    if raw_source is not None and worker_contract.get("batch_rows") is not None:
        errors.append(
            f"phase {phase_id}: declare batch_source or batch_rows, not both;"
            " batch_source is required for artifact-derived planning"
        )
    if raw_source is not None:
        if not isinstance(raw_source, dict):
            errors.append(
                f"phase {phase_id}: worker_contract.batch_source must be an object"
            )
        else:
            artifact_name = str(raw_source.get("artifact_name") or "").strip()
            if not artifact_name:
                errors.append(
                    f"phase {phase_id}: worker_contract.batch_source.artifact_name"
                    " is required"
                )
            for selector_name in ("cohort_selector", "selector"):
                selector = raw_source.get(selector_name)
                if selector is not None and not isinstance(selector, dict):
                    errors.append(
                        f"phase {phase_id}: worker_contract.batch_source."
                        f"{selector_name} must be an object"
                    )
                    continue
                if not isinstance(selector, dict):
                    continue
                field = str(selector.get("field") or "").strip()
                values = selector.get("values")
                if values is not None:
                    if not field:
                        errors.append(
                            f"phase {phase_id}: batch_source.{selector_name}."
                            "field is required when values is present"
                        )
                    if not isinstance(values, list) or not values:
                        errors.append(
                            f"phase {phase_id}: batch_source.{selector_name}."
                            "values must be a non-empty array"
                        )
                if selector_name == "cohort_selector":
                    if not field or not isinstance(values, list) or not values:
                        errors.append(
                            f"phase {phase_id}: batch_source.cohort_selector"
                            " requires field plus a non-empty values array"
                        )
                    unexpected = sorted(
                        str(key)
                        for key in selector
                        if key not in {"field", "values"}
                    )
                    if unexpected:
                        errors.append(
                            f"phase {phase_id}: batch_source.cohort_selector"
                            f" does not accept {unexpected}"
                        )
                    continue
                for key in ("offset", "limit"):
                    if key not in selector:
                        continue
                    value = selector.get(key)
                    valid = isinstance(value, int) and not isinstance(value, bool)
                    valid = valid and (value >= 0 if key == "offset" else value > 0)
                    if not valid:
                        errors.append(
                            f"phase {phase_id}: batch_source.selector.{key} must"
                            f" be a {'non-negative' if key == 'offset' else 'positive'} integer"
                        )

    errors.extend(
        direct_batch_rows_provenance_errors(
            worker_contract,
            user_task=user_task,
            phase_id=phase_id,
        )
    )

    raw_policy = worker_contract.get("batch_policy")
    if raw_policy is not None:
        if not isinstance(raw_policy, dict):
            errors.append(
                f"phase {phase_id}: worker_contract.batch_policy must be an object"
            )
        else:
            max_rows = raw_policy.get("max_rows_per_phase")
            if max_rows is not None and (
                not isinstance(max_rows, int)
                or isinstance(max_rows, bool)
                or max_rows <= 0
            ):
                errors.append(
                    f"phase {phase_id}: batch_policy.max_rows_per_phase must be"
                    " a positive integer"
                )
            for key in ("row_independent", "requires_isolation_per_row"):
                if key in raw_policy and not isinstance(raw_policy.get(key), bool):
                    errors.append(
                        f"phase {phase_id}: batch_policy.{key} must be a boolean"
                    )
    return declared_role


def _canonical_identity_url(value: Any) -> str:
    text = str(value or "").strip().rstrip(".,;:!?)")
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    scheme = parts.scheme.casefold()
    if (scheme and scheme not in {"http", "https"}) or not parts.netloc:
        return ""
    host = parts.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    try:
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    except ValueError:
        query = parts.query
    return urlunsplit(("", host, parts.path.rstrip("/"), query, ""))


def canonical_identity_url(value: Any) -> str:
    """Public harness canonicalizer for control-plane page identity checks."""

    return _canonical_identity_url(value)


def _absolute_http_urls_from_value(value: Any) -> Set[str]:
    """Collect canonical absolute HTTP(S) URLs without assuming field names."""

    urls: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, str):
            return
        try:
            parts = urlsplit(item.strip())
        except ValueError:
            return
        if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
            return
        canonical = _canonical_identity_url(item)
        if canonical:
            urls.add(canonical)

    visit(value)
    return urls


def _identity_value_is_explicit_in_task(value: Any, user_task: str) -> bool:
    if value is None or isinstance(value, (dict, list)):
        return False
    text = str(value).strip()
    if not text:
        return False
    canonical_url = _canonical_identity_url(text)
    if canonical_url:
        return canonical_url in {
            _canonical_identity_url(item)
            for item in _SOURCE_URL_RE.findall(str(user_task or ""))
            if _canonical_identity_url(item)
        }
    return contains_semantic_marker(str(user_task or ""), text)


def direct_batch_rows_provenance_errors(
    worker_contract: Any,
    *,
    user_task: str,
    phase_id: str = "",
) -> List[str]:
    """Verify direct batch rows against the immutable original user task.

    ``batch_rows`` is the only row input that bypasses the validated artifact
    ledger.  A model assertion such as "these came from the user" is therefore
    insufficient: every row must expose at least one declared identity field
    whose value occurs in the original instruction. Browser-discovered rows
    must use ``batch_source`` instead.
    """
    if not isinstance(worker_contract, dict):
        return []
    rows = worker_contract.get("batch_rows")
    if not isinstance(rows, list) or not rows:
        return []
    prefix = f"phase {phase_id}: " if phase_id else ""
    provenance = worker_contract.get("batch_rows_provenance")
    if not isinstance(provenance, dict):
        return [
            prefix
            + "direct batch_rows requires batch_rows_provenance with"
            " source='user_instruction' and identity_fields"
        ]
    source = str(provenance.get("source") or "").strip()
    raw_fields = provenance.get("identity_fields")
    identity_fields = [
        str(field).strip()
        for field in raw_fields
        if str(field).strip()
    ] if isinstance(raw_fields, list) else []
    errors: List[str] = []
    if source != "user_instruction":
        errors.append(
            prefix
            + "direct batch_rows_provenance.source must be 'user_instruction';"
            " use batch_source for browser-discovered rows"
        )
    if not identity_fields:
        errors.append(
            prefix
            + "direct batch_rows_provenance.identity_fields must be a"
            " non-empty array"
        )
    if not str(user_task or "").strip():
        errors.append(
            prefix
            + "direct batch_rows cannot be verified without the immutable"
            " original user task"
        )
        return errors
    if errors:
        return errors
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(prefix + f"batch_rows[{index}] must be an object")
            continue
        if not any(
            field in row
            and _identity_value_is_explicit_in_task(row.get(field), user_task)
            for field in identity_fields
        ):
            errors.append(
                prefix
                + f"batch_rows[{index}] has no identity_fields value explicitly"
                " present in the original user task; use batch_source for"
                " browser-discovered identities"
            )
    return errors


def _effective_dependency_ids(phases: List[JsonDict], index: int) -> List[str]:
    declared = _phase_dependency_ids(phases[index])
    if declared is None:
        return [str(item.get("id") or "") for item in phases[:index]]
    return list(declared)


def _declared_batch_size(phase: JsonDict) -> Optional[int]:
    contract = phase.get("worker_contract")
    contract = contract if isinstance(contract, dict) else {}
    rows = contract.get("batch_rows")
    if isinstance(rows, list):
        return len([row for row in rows if isinstance(row, dict)])
    source = contract.get("batch_source")
    source = source if isinstance(source, dict) else {}
    selector = source.get("selector")
    selector = selector if isinstance(selector, dict) else {}
    indices = selector.get("indices")
    if isinstance(indices, list):
        # row_selection.source_indices, already adapted into the selector. A
        # probe's one-row limit must bind on this shape too, or the rule holds
        # only for the spelling it was written against.
        return len({
            int(item) for item in indices
            if isinstance(item, int) and not isinstance(item, bool)
        })
    values = selector.get("values")
    if isinstance(values, list):
        return len(values)
    limit = selector.get("limit")
    return limit if isinstance(limit, int) and not isinstance(limit, bool) else None


def _validate_execution_role_dependencies(
    phases: List[JsonDict], errors: List[str]
) -> None:
    for index, phase in enumerate(phases):
        role = str(phase.get("execution_role") or "")
        if not role:
            continue
        phase_id = str(phase.get("id") or "")
        contract = phase.get("worker_contract")
        contract = contract if isinstance(contract, dict) else {}
        source = contract.get("batch_source")
        explicit_rows = contract.get("batch_rows")
        has_explicit_rows = (
            isinstance(explicit_rows, list)
            and bool(explicit_rows)
            and all(isinstance(row, dict) for row in explicit_rows)
        )
        size = _declared_batch_size(phase)
        declared_deps = _phase_dependency_ids(phase)
        deps = _effective_dependency_ids(phases, index)
        checkpoint_id = str(
            contract.get("replan_checkpoint_id") or ""
        ).strip()

        if not isinstance(source, dict) and not has_explicit_rows:
            errors.append(
                f"phase {phase_id}: execution_role={role} requires"
                " exactly one row input: worker_contract.batch_source for"
                " artifact-derived rows, or batch_rows for targets explicitly"
                " supplied by the user"
            )
        if role in _ROW_SELECTION_LIMITS and size is None:
            # A confidence stage whose row count cannot be read off the contract
            # is a whole-cohort run with a confidence stage's name: the selector
            # says "everything the artifact holds". Require the count to be
            # stated so the one/two-row limit below is enforceable at all.
            errors.append(
                f"phase {phase_id}: execution_role={role} must state which rows"
                " it takes — use worker_contract.row_selection.source_indices"
                " (preferred) or a bounded batch_source selector; an unbounded"
                f" selector makes {role} a multi-page run under another name"
            )
        if role == "probe" and size is not None and size > 1:
            errors.append(
                f"phase {phase_id}: execution_role=probe may select at most 1 row"
            )
        elif role == "validation":
            if not checkpoint_id:
                errors.append(
                    f"phase {phase_id}: execution_role=validation is conditional"
                    " and requires worker_contract.replan_checkpoint_id from a"
                    " validated predecessor whose checkpoint requires validation;"
                    " do not pre-create it as a fixed ladder stage"
                )
            if checkpoint_id and not declared_deps:
                errors.append(
                    f"phase {phase_id}: execution_role=validation must explicitly"
                    " declare depends_on with the phase recorded by its replan"
                    " checkpoint; retain that validated predecessor in the"
                    " replacement plan"
                )
            if size is not None and size > 2:
                errors.append(
                    f"phase {phase_id}: execution_role=validation may select at most 2 rows"
                )
        elif role == "bulk":
            if not checkpoint_id:
                errors.append(
                    f"phase {phase_id}: execution_role=bulk is conditional and"
                    " requires worker_contract.replan_checkpoint_id from validated"
                    " confidence evidence; do not pre-create it as a fixed ladder"
                    " stage"
                )
            if checkpoint_id and not declared_deps:
                errors.append(
                    f"phase {phase_id}: execution_role=bulk must explicitly"
                    " declare depends_on with the phase recorded by its replan"
                    " checkpoint; retain that validated predecessor in the"
                    " replacement plan"
                )
            policy = contract.get("batch_policy")
            policy = policy if isinstance(policy, dict) else {}
            if policy.get("row_independent") is not True:
                errors.append(
                    f"phase {phase_id}: execution_role=bulk requires"
                    " batch_policy.row_independent=true"
                )
            if not isinstance(policy.get("max_rows_per_phase"), int):
                errors.append(
                    f"phase {phase_id}: execution_role=bulk requires an explicit"
                    " batch_policy.max_rows_per_phase"
                )
        elif role == "continuation":
            if not checkpoint_id:
                errors.append(
                    f"phase {phase_id}: execution_role=continuation requires an"
                    " active worker_contract.replan_checkpoint_id; it is emitted"
                    " only when the preceding checkpoint requires slow-path"
                    " continuation"
                )
            elif not declared_deps:
                errors.append(
                    f"phase {phase_id}: execution_role=continuation must explicitly"
                    " declare depends_on with the phase recorded by its replan"
                    " checkpoint; retain that validated predecessor in the"
                    " replacement plan"
                )
        elif role == "remediation":
            if checkpoint_id:
                errors.append(
                    f"phase {phase_id}: execution_role=remediation cannot bind an"
                    " active replan checkpoint; use execution_role=continuation"
                    " for failed or remaining rows inside an active cohort"
                )
            if not deps:
                errors.append(
                    f"phase {phase_id}: execution_role=remediation must depend_on"
                    " the phase that produced the explicit failed-row set"
                )
            selector = source.get("selector") if isinstance(source, dict) else None
            selector = selector if isinstance(selector, dict) else {}
            has_source_failed_set = (
                bool(str(selector.get("field") or "").strip())
                and isinstance(selector.get("values"), list)
                and bool(selector.get("values"))
            )
            if not has_source_failed_set and not has_explicit_rows:
                errors.append(
                    f"phase {phase_id}: execution_role=remediation requires an"
                    " explicit failed-row set via batch_source.selector.field +"
                    " values or batch_rows"
                )


def _singleton_range_feature(phase: JsonDict) -> Optional[Tuple[str, Any]]:
    validators = phase.get("validators")
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict) or validator.get("type") != "range":
            continue
        minimum = _fingerprint_num(validator.get("min"))
        maximum = _fingerprint_num(validator.get("max"))
        if minimum == maximum:
            return str(validator.get("field") or ""), minimum
    return None


def _singleton_cohort_key(
    phase: JsonDict,
    *,
    effective_dependencies: List[str],
) -> str:
    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    fields = sorted(field_names_from_specs(expected.get("fields") or []))
    contract = phase.get("worker_contract")
    contract = dict(contract) if isinstance(contract, dict) else {}
    for key in ("batch_rows", "batch_source", "batch_policy"):
        contract.pop(key, None)
    payload = {
        "taskType": str(phase.get("task_type") or ""),
        "stage": str(phase.get("stage_hint") or ""),
        "depends": effective_dependencies,
        "sources": _normalized_source_urls(
            phase.get("objective"),
            phase.get("worker_task"),
        ),
        "fields": fields,
        "contract": contract,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _reject_singleton_phase_fragmentation(
    phases: List[JsonDict],
    errors: List[str],
    warnings: List[JsonDict],
) -> None:
    """Detect rank-like one-row fanout without guessing how to merge roles."""

    cohorts: Dict[Tuple[str, str], List[Tuple[str, Any]]] = {}
    for index, phase in enumerate(phases):
        role = str(phase.get("execution_role") or "")
        # probe/validation/remediation are intentionally separate confidence
        # or repair stages. Missing-role phases and bulk singletons are the
        # only candidates for accidental one-worker-per-row fanout.
        if role not in {"", "bulk"}:
            continue
        contract = phase.get("worker_contract")
        contract = contract if isinstance(contract, dict) else {}
        policy = contract.get("batch_policy")
        policy = policy if isinstance(policy, dict) else {}
        if policy.get("requires_isolation_per_row") is True:
            continue
        feature = _singleton_range_feature(phase)
        if feature is None:
            continue
        range_field, value = feature
        key = (
            _singleton_cohort_key(
                phase,
                effective_dependencies=_effective_dependency_ids(phases, index),
            ),
            range_field,
        )
        cohorts.setdefault(key, []).append((str(phase.get("id") or ""), value))
    for (_, range_field), members in cohorts.items():
        distinct = {json.dumps(value, sort_keys=True, default=str) for _, value in members}
        if len(members) < 3 or len(distinct) < 3:
            continue
        phase_ids = [phase_id for phase_id, _ in members]
        errors.append(
            "fragmentation_candidate: homogeneous singleton phases"
            f" {phase_ids} split only by {range_field!r}. Declare a mechanically"
            " evidence-driven cohort structure and bind each role to validated"
            " rows via worker_contract.batch_source. Start with probe only when"
            " the path is unknown; use validation/bulk only after the checkpoint"
            " authorizes them, otherwise use continuation for the remaining slow"
            " path rows; or"
            " use batch_rows only for targets explicit in the user instruction."
            " If rows truly require separate identity/session boundaries, declare"
            " batch_policy.requires_isolation_per_row=true; otherwise do not"
            " create one worker phase per row."
        )
        warnings.append({
            "code": "fragmentation_candidate",
            "phaseIds": phase_ids,
            "rangeField": range_field,
        })


def validate_task_plan(
    raw_plan: Any,
    *,
    known_abcp_methods: Optional[AbstractSet[str]] = None,
    known_harness_tools: Optional[AbstractSet[str]] = None,
    user_task: str = "",
) -> Tuple[Optional[JsonDict], List[str]]:
    """Validate and normalize the v1 task plan.

    Scheduling is a dependency gate, not a scheduler: `depends_on` OMITTED ⇒
    the phase implicitly depends on all prior phases in plan order (strict
    serial, the conservative default); `depends_on=[]` ⇒ explicitly
    independent; `depends_on=[ids]` ⇒ exactly those. Phases whose dependencies
    are all validated_done can be spawned concurrently by the Lead. A phase
    whose dependency ended in a blocking terminal status is marked
    blocked_by_dependency instead of being spawned. `fanout_from` remains
    forward-compat only.
    """
    errors: List[str] = []
    if not isinstance(raw_plan, dict):
        return None, ["plan must be a JSON object"]

    warnings: List[JsonDict] = []
    plan_pacing = _validate_pacing(raw_plan.get("pacing"), errors, where="pacing")
    goal = str(raw_plan.get("goal") or "").strip()
    if not goal:
        errors.append("goal is required")

    task_type = str(raw_plan.get("task_type") or "").strip()
    if not task_type:
        errors.append(
            "task_type is required; use an explicit value such as web_scrape,"
            " form_filling, file_download, file_upload, web_search, or general"
        )
        task_type = "web_scrape"
    else:
        task_type = _validated_task_type(
            task_type, errors=errors, warnings=warnings, where="task_type",
        )

    raw_phases = raw_plan.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        errors.append("phases must be a non-empty array")
        raw_phases = []

    raw_checkpoint_ids = raw_plan.get("replan_checkpoint_ids")
    checkpoint_ids: List[str] = []
    if raw_checkpoint_ids is not None:
        if not isinstance(raw_checkpoint_ids, list):
            errors.append("replan_checkpoint_ids must be an array of strings")
        else:
            for item in raw_checkpoint_ids:
                if not isinstance(item, str):
                    errors.append(
                        "replan_checkpoint_ids must contain only non-empty strings"
                    )
                    continue
                checkpoint_id = str(item or "").strip()
                if not checkpoint_id:
                    errors.append(
                        "replan_checkpoint_ids must contain only non-empty strings"
                    )
                    continue
                if checkpoint_id not in checkpoint_ids:
                    checkpoint_ids.append(checkpoint_id)
    raw_legacy_checkpoint_id = raw_plan.get("replan_checkpoint_id")
    if (
        raw_legacy_checkpoint_id is not None
        and (
            not isinstance(raw_legacy_checkpoint_id, str)
            or not raw_legacy_checkpoint_id.strip()
        )
    ):
        errors.append("replan_checkpoint_id must be a non-empty string")

    phases: List[JsonDict] = []
    seen_ids = set()
    for index, raw_phase in enumerate(raw_phases):
        if not isinstance(raw_phase, dict):
            errors.append(f"phases[{index}] must be an object")
            continue
        phase_id = str(raw_phase.get("id") or f"phase_{index + 1}").strip()
        phase_id = safe_path_component(phase_id, fallback=f"phase_{index + 1}")
        if phase_id in seen_ids:
            errors.append(f"duplicate phase id: {phase_id}")
        seen_ids.add(phase_id)

        phase_type = str(raw_phase.get("type") or "browser_worker").strip()
        if phase_type != "browser_worker":
            errors.append(
                f"phase {phase_id}: v1 only supports type='browser_worker'"
            )

        objective = str(raw_phase.get("objective") or "").strip()
        worker_task = str(raw_phase.get("worker_task") or raw_phase.get("task") or "").strip()
        if not objective:
            errors.append(f"phase {phase_id}: objective is required")
        if not worker_task:
            errors.append(f"phase {phase_id}: worker_task is required")

        stage_hint = str(raw_phase.get("stage_hint") or "").strip()
        if not stage_hint:
            errors.append(f"phase {phase_id}: stage_hint is required")
            stage_hint = "generic"
        elif stage_hint not in VALID_STAGE_HINTS:
            errors.append(
                f"phase {phase_id}: stage_hint must be one of"
                f" {sorted(VALID_STAGE_HINTS)}; got {stage_hint!r}"
            )
        stage_hint_reason = str(raw_phase.get("stage_hint_reason") or "").strip()
        if len(stage_hint_reason) < 40:
            errors.append(
                f"phase {phase_id}: stage_hint_reason must explain the stage choice"
                " in at least 40 characters"
            )

        expected_artifact = raw_phase.get("expected_artifact") or {}
        if expected_artifact is not None and not isinstance(expected_artifact, dict):
            errors.append(f"phase {phase_id}: expected_artifact must be an object")
            expected_artifact = {}

        validators = raw_phase.get("validators") or []
        if validators is not None and not isinstance(validators, list):
            errors.append(f"phase {phase_id}: validators must be an array")
            validators = []
        expected_artifact = _normalize_expected_artifact_contract(
            expected_artifact if isinstance(expected_artifact, dict) else {},
            validators,
            errors,
            warnings,
            phase_id=phase_id,
        )
        validators = _normalize_validators(
            expected_artifact,
            validators,
            errors,
            phase_id=phase_id,
            warnings=warnings,
        )
        _reject_phase_execution_integrity(
            phase_id=phase_id,
            objective=objective,
            worker_task=worker_task,
            stage_hint_reason=stage_hint_reason,
            expected=expected_artifact,
            validators=validators,
            errors=errors,
        )

        worker_contract = raw_phase.get("worker_contract")
        if worker_contract is not None and not isinstance(worker_contract, dict):
            errors.append(f"phase {phase_id}: worker_contract must be an object")
            worker_contract = None
        if isinstance(worker_contract, dict):
            _validate_worker_contract_methods(
                worker_contract,
                errors,
                phase_id=phase_id,
                known_abcp_methods=known_abcp_methods,
                known_harness_tools=known_harness_tools,
                warnings=warnings,
            )
            if worker_contract.get("task_type") is not None:
                worker_contract["task_type"] = _validated_task_type(
                    worker_contract.get("task_type"),
                    errors=errors,
                    warnings=warnings,
                    where=f"phase {phase_id}: worker_contract.task_type",
                )
            if (
                "needs_isolated_session" in worker_contract
                and not isinstance(worker_contract.get("needs_isolated_session"), bool)
            ):
                errors.append(
                    f"phase {phase_id}: worker_contract.needs_isolated_session"
                    " must be a boolean"
                )
            raw_reuse_scope = worker_contract.get("reuse_scope")
            try:
                normalized_reuse_scope = normalize_reuse_scope(
                    str(raw_reuse_scope or "")
                )
                if raw_reuse_scope is not None:
                    if str(raw_reuse_scope).strip():
                        worker_contract["reuse_scope"] = normalized_reuse_scope
                    else:
                        # Empty means unspecified. Do not freeze it to
                        # connection here: spawn-time explicit continuation
                        # selectors must still be able to default to page.
                        worker_contract.pop("reuse_scope", None)
            except ValueError as exc:
                errors.append(f"phase {phase_id}: worker_contract.{exc}")
                normalized_reuse_scope = "connection"
            raw_page_policy = worker_contract.get("page_policy")
            try:
                normalized_page_policy = normalize_page_policy(
                    str(raw_page_policy or ""),
                    reuse_scope=normalized_reuse_scope,
                )
                if raw_page_policy is not None:
                    if str(raw_page_policy).strip():
                        worker_contract["page_policy"] = normalized_page_policy
                    else:
                        worker_contract.pop("page_policy", None)
            except ValueError as exc:
                errors.append(f"phase {phase_id}: worker_contract.{exc}")
            if (
                "session_key" in worker_contract
                and not isinstance(worker_contract.get("session_key"), str)
            ):
                errors.append(
                    f"phase {phase_id}: worker_contract.session_key must be a string"
                )
            if (
                "fleet_id" in worker_contract
                and not isinstance(worker_contract.get("fleet_id"), str)
            ):
                errors.append(
                    f"phase {phase_id}: worker_contract.fleet_id must be a string"
                )
            if (
                str(worker_contract.get("fleet_id") or "").strip()
                and str(worker_contract.get("session_key") or "").strip()
            ):
                errors.append(
                    f"phase {phase_id}: worker_contract.fleet_id and"
                    " session_key are mutually exclusive"
                )
            if (
                str(worker_contract.get("fleet_id") or "").strip()
                and worker_contract.get("needs_isolated_session") is True
            ):
                errors.append(
                    f"phase {phase_id}: worker_contract.fleet_id and"
                    " needs_isolated_session are mutually exclusive"
                )
            if "auth_verification" in worker_contract:
                try:
                    worker_contract["auth_verification"] = (
                        normalize_auth_verification_contract(
                            worker_contract.get("auth_verification")
                        )
                    )
                except ValueError as exc:
                    errors.append(f"phase {phase_id}: worker_contract.{exc}")
            if "content_completeness" in worker_contract:
                raw_completeness = worker_contract.get("content_completeness")
                if not isinstance(raw_completeness, dict):
                    errors.append(
                        f"phase {phase_id}: worker_contract.content_completeness"
                        " must be an object"
                    )
                else:
                    completeness_errors = content_completeness_config_errors(
                        raw_completeness
                    )
                    errors.extend(
                        f"phase {phase_id}: {message}"
                        for message in completeness_errors
                    )
                    if (
                        not completeness_errors
                        and not normalize_content_completeness_config(raw_completeness)
                    ):
                        errors.append(
                            f"phase {phase_id}: worker_contract.content_completeness"
                            " requires at least one valid expected_regions entry"
                        )
            declared_role = _normalize_batch_contract(
                worker_contract,
                errors,
                phase_id=phase_id,
                user_task=user_task,
            )
            # The plan-level role gates — the ladder dependency check, the
            # one-row-fanout detector, the checkpoint requiredNextRole match —
            # all read phase.execution_role. Leaving row_selection.mode inside
            # the contract would let a plan that uses only the newer cohort
            # shape slip past every one of them, which is worse than the
            # missing-role case those gates were written for.
            if declared_role:
                existing_role = str(raw_phase.get("execution_role") or "").strip()
                if not existing_role:
                    raw_phase["execution_role"] = declared_role
                elif existing_role != declared_role:
                    errors.append(
                        f"phase {phase_id}: execution_role={existing_role!r}"
                        f" contradicts row_selection.mode={declared_role!r};"
                        " declare the role once"
                    )
            if (
                "replan_checkpoint_id" in worker_contract
                and (
                    not isinstance(
                        worker_contract.get("replan_checkpoint_id"), str
                    )
                    or not str(
                        worker_contract.get("replan_checkpoint_id") or ""
                    ).strip()
                )
            ):
                errors.append(
                    f"phase {phase_id}: worker_contract.replan_checkpoint_id"
                    " must be a non-empty string"
                )

        execution_role = str(raw_phase.get("execution_role") or "").strip()
        if execution_role and execution_role not in EXECUTION_ROLES:
            errors.append(
                f"phase {phase_id}: execution_role must be one of"
                f" {sorted(EXECUTION_ROLES)}; got {execution_role!r}"
            )

        # phase_contract consumes phase.task_type (contract > phase > plan),
        # but normalization used to drop it silently — a per-phase override
        # the model emitted at the sanctioned granularity simply vanished
        # (review P2). Preserve it, validated.
        #
        # REQUIRED, not inherited: silent inheritance made the plan's single
        # task_type decide method access for every phase, so one classification
        # covering a whole multi-stage goal disabled domains a later phase
        # needed. Task b37bac2a planned "scrape listings AND export media" as
        # web_scrape, which disabled Download for the export phase; the worker
        # never saw the method and reported the videos as un-downloadable.
        # Making each phase state its own type puts the choice next to the
        # phase objective that justifies it.
        phase_task_type = _validated_task_type(
            raw_phase.get("task_type"),
            errors=errors,
            warnings=warnings,
            where=f"phase {phase_id}: task_type",
        )
        # Absence only — an unknown value already produced its own, more
        # specific error inside _validated_task_type (which also returns "").
        if not str(raw_phase.get("task_type") or "").strip():
            errors.append(
                f"phase {phase_id}: task_type is required and is NOT inherited"
                " from the plan; declare what this phase itself does"
                f" (one of {task_type_choices_for_error()}). A phase that saves"
                " a non-image file needs file_download, not web_scrape."
            )
        if isinstance(worker_contract, dict) and worker_contract.get("task_type"):
            contract_task_type = str(worker_contract.get("task_type") or "")
            if phase_task_type and contract_task_type != phase_task_type:
                errors.append(
                    f"phase {phase_id}: worker_contract.task_type cannot override"
                    f" phase.task_type ({contract_task_type!r} !="
                    f" {phase_task_type!r}); revise phase.task_type and re-emit"
                    " the plan instead"
                )
        if phase_task_type:
            _validate_task_type_capability_match(
                phase_id=phase_id,
                task_type=phase_task_type,
                objective=objective,
                worker_task=worker_task,
                stage_hint_reason=stage_hint_reason,
                validators=validators,
                errors=errors,
                warnings=warnings,
            )

        phases.append({
            "id": phase_id,
            "type": phase_type,
            "task_type": phase_task_type or None,
            "objective": objective,
            "worker_task": worker_task,
            "stage_hint": stage_hint,
            "stage_hint_reason": stage_hint_reason,
            "execution_role": execution_role or None,
            "context": str(raw_phase.get("context") or ""),
            "max_steps": raw_phase.get("max_steps"),
            # None (omitted) and [] (explicitly independent) mean DIFFERENT
            # schedules — `or []` used to collapse both into [], erasing the
            # planner's only syntax for parallel phases (task 2ed5a466).
            "depends_on": _normalized_depends_on(raw_phase.get("depends_on")),
            "pacing": (
                _validate_pacing(
                    raw_phase.get("pacing"), errors,
                    where=f"phase {phase_id}: pacing",
                )
                if raw_phase.get("pacing") is not None
                else None
            ),
            "fanout_from": raw_phase.get("fanout_from"),
            "join": raw_phase.get("join"),
            "expected_artifact": expected_artifact,
            "validators": validators,
            "validators_normalized": True,
            "worker_contract": worker_contract or {},
            "max_attempts": _positive_int(raw_phase.get("max_attempts"), default=3),
        })

    # depends_on must reference declared phase ids: an unknown id resolves to
    # dependency_not_ready (non-blocking) at schedule time, so the phase would
    # be skipped forever with no signal — reject the plan instead.
    for phase in phases:
        phase_id = str(phase.get("id"))
        for dep_id in _phase_dependency_ids(phase) or []:
            if dep_id == phase_id:
                errors.append(
                    f"phase {phase_id}: depends_on must not reference itself"
                )
            elif dep_id not in seen_ids:
                errors.append(
                    f"phase {phase_id}: depends_on references unknown phase"
                    f" id {dep_id!r}"
                )

    _validate_execution_role_dependencies(phases, errors)
    _reject_singleton_phase_fragmentation(phases, errors, warnings)
    _reject_serial_auth_handoff(phases, errors)

    normalized = {
        "version": "v1",
        "goal": goal,
        "task_type": task_type,
        "pacing": plan_pacing,
        "replan_checkpoint_id": (
            str(raw_plan.get("replan_checkpoint_id") or "").strip() or None
        ),
        "replan_checkpoint_ids": checkpoint_ids,
        "phases": phases,
    }
    if warnings:
        normalized["warnings"] = warnings
    if errors:
        return None, errors
    return normalized, []


def _validate_worker_contract_methods(
    worker_contract: JsonDict,
    errors: List[str],
    *,
    phase_id: str,
    known_abcp_methods: Optional[AbstractSet[str]],
    known_harness_tools: Optional[AbstractSet[str]],
    warnings: Optional[List[JsonDict]] = None,
) -> None:
    """allowed_methods (allow-list): an unknown name is a probable typo that
    would silently forbid the method the planner MEANT to allow — fail loud.
    forbidden_methods (deny-list): forbidding a method that does not exist is
    a no-op — rejecting the whole plan over it cost task 2ed5a466 a full plan
    round-trip on 'Download.save' (×4 phases). Unknown deny entries are
    DROPPED with a warning receipt instead; task_type policy already disables
    whole method domains worker-side, so the deny-list is only ever an extra."""
    harness_tools = known_harness_tools or set()
    for key in ("allowed_methods", "forbidden_methods"):
        raw_methods = worker_contract.get(key)
        if raw_methods is None:
            continue
        if not isinstance(raw_methods, list):
            errors.append(f"phase {phase_id}: worker_contract.{key} must be an array")
            continue
        tolerant = key == "forbidden_methods"
        kept: List[Any] = []

        def _unknown(method: str) -> None:
            if tolerant:
                if warnings is not None:
                    warnings.append({
                        "type": "unknown_forbidden_method_dropped",
                        "phase": phase_id,
                        "method": method,
                        "note": (
                            "Not a known method, so it forbids nothing —"
                            " dropped. task_type policy already disables whole"
                            " method domains worker-side; use canonical names"
                            " or Domain.* wildcards for extra restrictions."
                        ),
                    })
                return
            errors.append(
                f"phase {phase_id}: unknown method in worker_contract.{key}: {method!r}"
            )

        for raw_method in raw_methods:
            method = str(raw_method or "").strip()
            if not method:
                continue
            if "*" in method or method in harness_tools:
                kept.append(method)
                continue
            if known_abcp_methods is not None:
                if method not in known_abcp_methods:
                    _unknown(method)
                    continue
                kept.append(method)
                continue
            if "." not in method:
                if tolerant:
                    _unknown(method)
                else:
                    errors.append(
                        f"phase {phase_id}: unknown harness tool in worker_contract.{key}: {method!r}"
                    )
                continue
            # Dotted method with no schema cache to check against: keep it.
            kept.append(method)
        if tolerant:
            worker_contract[key] = kept


def _first_valid_task_type(*candidates: Any) -> str:
    """Return the first known candidate, else the restricted web_scrape type.

    Explicit ``general`` is valid, but missing/garbage must never become an
    implicit all-domain grant if a future internal caller bypasses validation.
    """
    for candidate in candidates:
        if candidate is None or str(candidate).strip() == "":
            continue
        canonical = normalize_task_type(candidate)
        if canonical in VALID_TASK_TYPES:
            return canonical
    return "web_scrape"


def _merged_expected_artifact(
    phase: JsonDict,
    worker_contract: JsonDict,
) -> JsonDict:
    """Build the one effective artifact contract used by every gate."""

    expected = dict(phase.get("expected_artifact") or {})
    override = worker_contract.get("expected_artifact")
    if isinstance(override, dict):
        expected.update(override)
    return expected


def phase_contract(
    phase: JsonDict,
    override: Optional[JsonDict] = None,
) -> JsonDict:
    contract: JsonDict = dict(phase.get("worker_contract") or {})
    if override:
        contract.update(override)

    expected_artifact = _merged_expected_artifact(phase, contract)

    validators = contract.get("validators")
    validators_from_contract = isinstance(validators, list)
    if not validators_from_contract:
        validators = list(phase.get("validators") or [])
    phase_id = str(contract.get("phase_id") or phase.get("id") or "")
    validator_errors: List[str] = []
    already_normalized = bool(
        contract.get("validators_normalized")
        if validators_from_contract
        else phase.get("validators_normalized")
    )
    if already_normalized:
        validators = [
            dict(validator) for validator in validators
            if isinstance(validator, dict)
        ]
    else:
        validators = _normalize_validators(
            expected_artifact,
            validators,
            validator_errors,
            phase_id=phase_id or "worker",
        )

    # phase.task_type is the sole authority for method policy. A spawn-time
    # worker_contract must not silently broaden/narrow the reviewed plan, and a
    # plan-level classification must not leak into a phase that omitted its own
    # type. New plans cannot reach this function without a valid phase type,
    # but legacy/internal callers fail closed to web_scrape rather than the
    # unrestricted general policy if they omit or corrupt it. An explicitly
    # reviewed phase.task_type="general" remains valid.
    resolved_task_type = resolve_task_type_fail_closed(phase.get("task_type"))
    file_only_contract = bool(validators) and all(
        str(item.get("type") or "") in FILE_RECEIPT_ONLY_VALIDATOR_TYPES
        for item in validators
        if isinstance(item, dict)
    )
    default_must_record = not (
        resolved_task_type in {"file_download", "file_upload"}
        and file_only_contract
        and not expected_artifact.get("fields")
        and not expected_artifact.get("required_fields")
    )
    payload: JsonDict = {
        "version": "v1",
        "phase_id": phase_id,
        "task_type": resolved_task_type,
        "stage_hint": str(contract.get("stage_hint") or phase.get("stage_hint") or "generic"),
        "execution_role": str(
            contract.get("execution_role") or phase.get("execution_role") or ""
        ),
        "stage_hint_reason": str(
            contract.get("stage_hint_reason")
            or phase.get("stage_hint_reason")
            or ""
        ),
        "objective": str(contract.get("objective") or phase.get("objective") or ""),
        # Passed through for downstream consumers that need the concrete task
        # phrasing (e.g. the VL reality check synthesizes its claim from the
        # contract and falls back to worker_task when objective is generic).
        "worker_task": str(
            contract.get("worker_task") or phase.get("worker_task") or ""
        ),
        "input_artifacts": contract.get("input_artifacts") or [],
        "expected_artifact": expected_artifact,
        "validators": validators,
        "validators_normalized": True,
        "allowed_methods": _string_list(contract.get("allowed_methods")),
        "forbidden_methods": _string_list(contract.get("forbidden_methods")),
        "max_surface_attempts": (
            contract.get("max_surface_attempts")
            if isinstance(contract.get("max_surface_attempts"), dict)
            else {}
        ),
        "must_record_extraction": bool(
            contract.get("must_record_extraction")
            if "must_record_extraction" in contract
            else default_must_record
        ),
        "stop_condition": str(
            contract.get("stop_condition")
            or "Record the required extraction artifact, then call final_answer."
        ),
        "pacing": normalized_pacing(
            contract.get("pacing")
            if isinstance(contract.get("pacing"), dict)
            else phase.get("pacing")
        ),
    }
    # Pass through skill-selection fields the LeadAgent set on the worker_contract.
    # phase_contract otherwise rebuilds a fixed-field payload, which silently
    # dropped these — so an explicit skill_id/skill_variables (select) or
    # skill_selection={"use_skill": false} (decline) never reached the dispatch
    # gate and spawn_browser_agent kept re-returning skill_selection_required
    # (an unbreakable loop for the Lead). Preserve them verbatim when present.
    for skill_key in (
        "skill_id", "skill_variables", "skill_rows", "batch_rows",
        # Must travel WITH batch_rows: the spawn gate rejects rows that arrive
        # without their provenance, so dropping this key alone turned a legal
        # plan into an unspawnable one. Observed live in task 1b219431, where
        # every phase carrying user-supplied URLs was refused with
        # invalid_batch_rows_provenance while the plan on disk declared it
        # correctly. Same trap the skill_id comment above describes.
        "batch_rows_provenance",
        "batch_source",
        "batch_policy", "replan_checkpoint_id", "skill_selection", "domain",
        "needs_isolated_session", "reuse_scope", "session_key", "page_policy",
        "fleet_id",
        "content_completeness",
    ):
        value = contract.get(skill_key)
        if value is not None:
            payload[skill_key] = value
    if validator_errors:
        payload["contract_warnings"] = validator_errors
    return payload


def write_task_plan(logger: RunLogger, plan: JsonDict) -> str:
    path = logger.task_dir / TASK_PLAN_FILE
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    logger.write("task_plan.accepted", {"path": str(path.resolve()), "phaseCount": len(plan.get("phases", []))})
    return str(path.resolve())


def write_versioned_task_plan(
    logger: RunLogger,
    plan: JsonDict,
    *,
    previous_plan: Optional[JsonDict],
    replan_reason: str,
    user_task: str,
    validator_review: Optional[JsonDict],
) -> Tuple[str, JsonDict]:
    """Persist an immutable accepted revision plus the latest-plan alias."""

    from harness.plan_validator import write_plan_version

    version = write_plan_version(
        logger,
        plan=plan,
        previous_plan=previous_plan,
        replan_reason=replan_reason,
        user_task=user_task,
        validator_review=validator_review,
    )
    path = write_task_plan(logger, plan)
    logger.write("task_plan.versioned", version)
    return path, version


def initialize_task_state(
    logger: RunLogger,
    plan: JsonDict,
    *,
    preserve_from: Optional[JsonDict] = None,
    replan_reason: str = "",
    plan_version: Optional[JsonDict] = None,
) -> JsonDict:
    previous_phases_raw = preserve_from.get("phases") if isinstance(preserve_from, dict) else None
    previous_phases: JsonDict = previous_phases_raw if isinstance(previous_phases_raw, dict) else {}
    raw_phases = plan.get("phases")
    plan_phases = raw_phases if isinstance(raw_phases, list) else []
    phases_state: JsonDict = {}
    replan_audit: Optional[JsonDict] = None
    if preserve_from is not None:
        replan_audit = {
            "at": utc_now_iso(),
            "reason": replan_reason,
            "preserved_phases": [],
            # Historic key name; holds every REPLAN_RESET_STATUSES reset
            # (phase_failed AND blocked_by_dependency — see each entry's
            # previousStatus). Kept as-is so old and new replan entries in
            # the same task_state stay grep-able under one key.
            "reset_phase_failed": [],
            "new_phases": [],
            "removed_phases": sorted(
                set(str(pid) for pid in previous_phases.keys())
                - set(str(phase.get("id")) for phase in plan_phases if isinstance(phase, dict))
            ),
        }

    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id"))
        previous = previous_phases.get(phase_id)
        if isinstance(previous, dict):
            previous_status = str(previous.get("status") or "")
            if previous_status in REPLAN_RESET_STATUSES:
                phases_state[phase_id] = _empty_phase_state()
                if replan_audit is not None:
                    replan_audit["reset_phase_failed"].append({
                        "phaseId": phase_id,
                        "previousStatus": previous_status,
                        "previousAttemptCount": len(previous.get("attempts") or []),
                    })
            else:
                preserved = copy.deepcopy(previous)
                if preserved.get("status") == "running":
                    preserved["status"] = "pending"
                    preserved["replan_reset_from"] = "running"
                _ensure_phase_state_defaults(preserved)
                phases_state[phase_id] = preserved
                if replan_audit is not None:
                    replan_audit["preserved_phases"].append({
                        "phaseId": phase_id,
                        "status": preserved.get("status"),
                        "attemptCount": len(preserved.get("attempts") or []),
                    })
        else:
            phases_state[phase_id] = _empty_phase_state()
            if replan_audit is not None:
                replan_audit["new_phases"].append(phase_id)

    state = {
        "version": "v1",
        "created_at": (
            preserve_from.get("created_at")
            if isinstance(preserve_from, dict) and preserve_from.get("created_at")
            else utc_now_iso()
        ),
        "updated_at": utc_now_iso(),
        "goal": plan.get("goal"),
        "current_phase": _first_active_phase_id(plan, phases_state),
        "phases": phases_state,
        "artifacts": list((preserve_from or {}).get("artifacts") or []),
        "completed_items": list((preserve_from or {}).get("completed_items") or []),
        "pending_items": list((preserve_from or {}).get("pending_items") or []),
        "failed_items": list((preserve_from or {}).get("failed_items") or []),
        "banned_strategies": list((preserve_from or {}).get("banned_strategies") or []),
        "quality": dict((preserve_from or {}).get("quality") or {}),
        # Survives replans BY DESIGN: this is the whole point of the
        # objective-level budget — a fresh phase id must not reset it.
        "objective_attempts": dict(
            (preserve_from or {}).get("objective_attempts") or {}
        ),
        # Startup failures predate a worker attempt and therefore live outside
        # the phase/objective ledgers. Preserve them across replans so changing
        # only a phase id cannot reopen the same broken acquisition route.
        "spawn_acquisition_failures": dict(
            (preserve_from or {}).get("spawn_acquisition_failures") or {}
        ),
        # Task-local Stage 6B-A evidence.  Candidates remain non-executable;
        # preserving them across replans lets the Lead acknowledge the exact
        # checkpoint instead of recreating guidance from prose.
        "batch_progress": dict(
            (preserve_from or {}).get("batch_progress") or {}
        ),
        "fast_path_generation": int(
            (preserve_from or {}).get("fast_path_generation") or 0
        ),
        "replan_checkpoints": copy.deepcopy(
            _replan_checkpoint_map(preserve_from or {})
        ),
    }
    if preserve_from is not None:
        state["replans"] = list((preserve_from or {}).get("replans") or [])
        state["replans"].append(replan_audit or {})
    if isinstance(plan_version, dict):
        state["plan_version"] = int(plan_version.get("planVersion") or 0)
        state["plan_hash"] = str(plan_version.get("planHash") or "")
        state["plan_history"] = list(
            (preserve_from or {}).get("plan_history") or []
        )
        state["plan_history"].append(copy.deepcopy(plan_version))
        if replan_audit is not None:
            replan_audit["planVersion"] = state["plan_version"]
            replan_audit["planHash"] = state["plan_hash"]
    write_task_state(logger, state)
    logger.write(
        "task_state.initialized",
        {
            "path": str(_state_path(logger).resolve()),
            "preserved": preserve_from is not None,
            "replanReason": replan_reason or None,
        },
    )
    return state


def load_task_state(logger: RunLogger) -> JsonDict:
    path = _state_path(logger)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def materialize_batch_rows_from_source(
    logger: RunLogger,
    *,
    phase: JsonDict,
    worker_contract: JsonDict,
) -> Optional[JsonDict]:
    """Bind batch_rows to a validated extraction artifact before spawning.

    The Lead declares selection intent; it never copies potentially large row
    payloads through model context.  Only paths already present in the task's
    validated artifact ledger are eligible.
    """

    source = worker_contract.get("batch_source")
    if not isinstance(source, dict):
        return None
    artifact_name = str(source.get("artifact_name") or "").strip()
    state = load_task_state(logger)
    ledger_paths = [
        str(path) for path in state.get("artifacts") or [] if str(path).strip()
    ]
    extraction_root = (logger.task_dir / "artifacts" / "extractions").resolve()
    matches: List[Tuple[Path, JsonDict, str]] = []
    for raw_path in ledger_paths:
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(extraction_root)
        except ValueError:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and str(payload.get("name") or "").strip() == artifact_name
        ):
            payload_blob = json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            matches.append((
                path,
                payload,
                hashlib.sha256(payload_blob.encode("utf-8")).hexdigest()[:16],
            ))
    if len(matches) != 1:
        return {
            "status": "batch_source_not_ready",
            "phaseId": str(phase.get("id") or ""),
            "artifactName": artifact_name,
            "matchCount": len(matches),
            "tool_was_executed": False,
            "next_instruction": (
                "Wait for the declared upstream artifact to become uniquely"
                " validated, or replan batch_source.artifact_name. Do not copy"
                " unvalidated rows into batch_rows manually."
            ),
        }
    path, payload, source_artifact_generation = matches[0]
    raw_rows = payload.get("rows")
    source_rows = (
        [dict(row) for row in raw_rows if isinstance(row, dict)]
        if isinstance(raw_rows, list)
        else []
    )
    cohort_selected = list(enumerate(source_rows))
    cohort_selector_missing: List[str] = []
    cohort_selector = _canonical_cohort_selector(
        source.get("cohort_selector")
    )
    cohort_field = str(cohort_selector.get("field") or "").strip()
    cohort_values = cohort_selector.get("values")
    if cohort_field and isinstance(cohort_values, list):
        cohort_wanted = {
            json.dumps(value, sort_keys=True, default=str)
            for value in cohort_values
        }
        cohort_selected = [
            (index, row) for index, row in cohort_selected
            if json.dumps(
                row.get(cohort_field), sort_keys=True, default=str
            ) in cohort_wanted
        ]
        matched = {
            json.dumps(
                row.get(cohort_field), sort_keys=True, default=str
            )
            for _, row in cohort_selected
        }
        cohort_selector_missing = sorted(cohort_wanted - matched)
    selected = list(cohort_selected)
    selector = source.get("selector")
    selector = selector if isinstance(selector, dict) else {}
    field = str(selector.get("field") or "").strip()
    values = selector.get("values")
    selector_missing: List[str] = []
    if field and isinstance(values, list):
        wanted = {json.dumps(value, sort_keys=True, default=str) for value in values}
        selected = [
            (index, row) for index, row in selected
            if json.dumps(row.get(field), sort_keys=True, default=str) in wanted
        ]
        matched = {
            json.dumps(row.get(field), sort_keys=True, default=str)
            for _, row in selected
        }
        selector_missing = sorted(wanted - matched)
    raw_indices = selector.get("indices")
    if isinstance(raw_indices, list):
        # row_selection.source_indices arrives here. Indices name positions in
        # the cohort artifact, so an index the artifact no longer has must fail
        # loudly: silently selecting fewer rows than the plan asked for is how
        # a slice quietly shrinks between generations.
        wanted_indices = [
            int(item) for item in raw_indices
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        ]
        available = {index for index, _ in selected}
        selected = [
            (index, row) for index, row in selected if index in set(wanted_indices)
        ]
        missing_indices = sorted(set(wanted_indices) - available)
        if missing_indices:
            selector_missing = [
                *selector_missing,
                *(f"index:{index}" for index in missing_indices),
            ]
    offset = selector.get("offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
        selected = selected[offset:]
    limit = selector.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        selected = selected[:limit]
    rows = [row for _, row in selected]
    selected_source_indices = [index for index, _ in selected]
    cohort_source_indices = [index for index, _ in cohort_selected]

    policy = worker_contract.get("batch_policy")
    policy = policy if isinstance(policy, dict) else {}
    raw_max_rows = policy.get("max_rows_per_phase")
    max_rows = (
        raw_max_rows
        if isinstance(raw_max_rows, int)
        and not isinstance(raw_max_rows, bool)
        and raw_max_rows > 0
        else 10
    )
    error = ""
    if cohort_selector_missing:
        error = (
            "cohort_selector values missing from validated artifact:"
            f" {cohort_selector_missing}"
        )
    elif selector_missing:
        error = (
            "selector values missing from cohort:"
            f" {selector_missing}"
        )
    elif not cohort_source_indices:
        error = "cohort_selector matched zero rows"
    elif not rows:
        error = "selector matched zero rows"
    elif len(rows) > max_rows:
        error = f"selected {len(rows)} rows, exceeding max_rows_per_phase={max_rows}"
    elif policy.get("requires_isolation_per_row") is True and len(rows) > 1:
        error = "multiple rows cross a declared per-row isolation boundary"
    role = str(phase.get("execution_role") or worker_contract.get("execution_role") or "")
    if role == "probe" and len(rows) > 1:
        error = "probe selected more than one row"
    elif role == "validation" and len(rows) > 2:
        error = "validation selected more than two rows"
    if error:
        return {
            "status": "invalid_batch_source_selection",
            "phaseId": str(phase.get("id") or ""),
            "artifactName": artifact_name,
            "error": error,
            "selectedRows": len(rows),
            "tool_was_executed": False,
            "next_instruction": (
                "Revise the batch_source selector or batch_policy. The harness"
                " will not silently split, truncate, or merge this batch."
            ),
        }

    worker_contract["batch_rows"] = rows
    worker_contract["_batch_source_receipt"] = {
        "artifactName": artifact_name,
        "artifactPath": str(path),
        "sourceArtifactGeneration": source_artifact_generation,
        "rowCount": len(rows),
        "sourceRowCount": len(source_rows),
        "cohortRowCount": len(cohort_source_indices),
        "cohortSourceIndices": cohort_source_indices,
        "selectedSourceIndices": selected_source_indices,
        "cohortSelector": cohort_selector,
        "selector": selector,
        "executionRole": role,
    }
    # The one receipt above answers two unrelated questions at once — who the
    # whole cohort is, and which rows THIS worker owns — and the checkpoint
    # reads it as if both were the same fact. That coupling is why a probe
    # cannot be a single item: it would have to pretend to be a batch to
    # produce a receipt at all. Split them; the checkpoint binds the cohort,
    # the slice records the assignment.
    identity_field = str(
        (source.get("identity_field") if isinstance(source, dict) else "")
        or field
        or cohort_field
    ).strip()
    worker_contract["_source_cohort_receipt"] = {
        "receiptType": "source_cohort.v1",
        "artifactName": artifact_name,
        "artifactPath": str(path),
        "artifactGeneration": source_artifact_generation,
        "identityField": identity_field,
        "cohortSourceIndices": cohort_source_indices,
        "cohortRowKeys": _row_keys_for_indices(
            source_rows, cohort_source_indices, identity_field,
        ),
        "sourceRowCount": len(source_rows),
        "cohortSelector": cohort_selector,
    }
    worker_contract["_execution_slice_receipt"] = {
        "receiptType": "execution_slice.v1",
        "role": role,
        "artifactPath": str(path),
        "artifactGeneration": source_artifact_generation,
        "selectedSourceIndices": selected_source_indices,
        "selectedRowKeys": _row_keys_for_indices(
            source_rows, selected_source_indices, identity_field,
        ),
        "selector": selector,
    }
    logger.write(
        "batch_source.materialized",
        {
            "phaseId": str(phase.get("id") or ""),
            **worker_contract["_batch_source_receipt"],
        },
    )
    return None


def _row_keys_for_indices(
    source_rows: List[JsonDict], indices: List[int], identity_field: str,
) -> List[str]:
    """Row keys for the given source indices, empty when there is no identity.

    Indices are positions in a file that a replan may replace; a key is what
    survives that. Both are recorded because neither alone is enough: indices
    without keys cannot be checked against a new generation, and keys without
    indices cannot be checked against the one that produced them.
    """
    if not identity_field:
        return []
    keys: List[str] = []
    for index in indices:
        if 0 <= index < len(source_rows):
            value = source_rows[index].get(identity_field)
            if value is not None and not isinstance(value, (dict, list, bool)):
                text = str(value).strip()
                if text:
                    keys.append(text)
    return keys


def _canonical_cohort_selector(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    field = str(value.get("field") or "").strip()
    values = value.get("values")
    if not field or not isinstance(values, list) or not values:
        return {}
    unique: Dict[str, Any] = {}
    for item in values:
        token = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        unique.setdefault(token, item)
    return {
        "field": field,
        "values": [unique[token] for token in sorted(unique)],
    }


def _cohort_selectors_provably_disjoint(left: Any, right: Any) -> bool:
    left_selector = _canonical_cohort_selector(left)
    right_selector = _canonical_cohort_selector(right)
    if not left_selector or not right_selector:
        return False
    if left_selector.get("field") != right_selector.get("field"):
        return False
    left_values = {
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        for value in left_selector.get("values") or []
    }
    right_values = {
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        for value in right_selector.get("values") or []
    }
    return bool(left_values and right_values and left_values.isdisjoint(right_values))


_FAST_PATH_QUANTITY_EXPECTED_KEYS = frozenset({
    "exact_rows", "min_rows", "max_rows", "count_range",
})


def _fast_path_selector_identity_fields(worker_contract: JsonDict) -> Set[str]:
    source = worker_contract.get("batch_source")
    source = source if isinstance(source, dict) else {}
    fields: Set[str] = set()
    for key in ("selector", "cohort_selector"):
        selector = source.get(key)
        selector = selector if isinstance(selector, dict) else {}
        field = str(selector.get("field") or "").strip()
        if field:
            fields.add(field)
    receipt = worker_contract.get("_batch_source_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    for key in ("selector", "cohortSelector"):
        selector = receipt.get(key)
        selector = selector if isinstance(selector, dict) else {}
        field = str(selector.get("field") or "").strip()
        if field:
            fields.add(field)
    return fields


def _fast_path_validator_is_slice(
    validator: JsonDict,
    *,
    selector_fields: AbstractSet[str],
) -> bool:
    """Classify phase-local validators without duplicating scope taxonomy.

    Aggregate evaluation scope and slice mutability are different concepts.
    Row-count validators always describe the selected batch.  Field-based
    aggregate/range validators are slice-local only when they target the
    declared batch identity; otherwise they remain business obligations.
    """

    validator_type = str(validator.get("type") or "").strip()
    if (
        VALIDATOR_SCOPE.get(validator_type) == "aggregate"
        and validator_type in {"min_rows", "max_rows", "exact_rows"}
    ):
        return True
    if validator_type not in {"range", "set_equals", "unique"}:
        return False
    fields = set(field_names_from_specs(validator.get("fields") or []))
    single = str(validator.get("field") or "").strip()
    if single:
        fields.add(single)
    return bool(fields) and fields.issubset(selector_fields)


def _fast_path_validator_obligations(validator: JsonDict) -> Set[str]:
    """Expand validators into monotonic obligations for checkpoint fencing."""

    validator_type = str(validator.get("type") or "").strip()
    if validator_type in {"required_fields", "field_nonempty"}:
        fields = set(field_names_from_specs(validator.get("fields") or []))
        single = str(validator.get("field") or "").strip()
        if single:
            fields.add(single)
        return {
            json.dumps(
                [validator_type, field],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for field in fields
        }
    return {_validator_semantic_signature(validator)}


def _canonical_fast_path_business_contract(
    phase: JsonDict,
    worker_contract: JsonDict,
) -> JsonDict:
    """Stable business schema shared by probe/validation/bulk/continuation.

    Stage hints, strategy choices, row selectors and per-stage row-count bounds
    are execution profile.  They may legitimately change after observing the
    probe.  Artifact identity/shape and non-slice validators are business
    contract and must remain stable, otherwise a checkpoint could certify a
    different output merely because it reads from the same source rows.
    """

    expected = copy.deepcopy(_merged_expected_artifact(phase, worker_contract))
    for key in _FAST_PATH_QUANTITY_EXPECTED_KEYS:
        expected.pop(key, None)

    fields = expected.get("fields")
    if isinstance(fields, list):
        expected["fields"] = sorted(
            (copy.deepcopy(item) for item in fields),
            key=lambda item: json.dumps(
                item, sort_keys=True, ensure_ascii=False, default=str,
            ),
        )
    required_fields = expected.get("required_fields")
    if isinstance(required_fields, list):
        expected["required_fields"] = sorted({
            str(item).strip() for item in required_fields if str(item).strip()
        })

    validators = worker_contract.get("validators")
    if not isinstance(validators, list):
        validators = phase.get("validators")
    selector_fields = _fast_path_selector_identity_fields(worker_contract)
    validator_tokens: Dict[str, JsonDict] = {}
    obligations: Set[str] = set()
    for item in validators if isinstance(validators, list) else []:
        if not isinstance(item, dict):
            continue
        if _fast_path_validator_is_slice(
            item,
            selector_fields=selector_fields,
        ):
            continue
        normalized = copy.deepcopy(item)
        token = _validator_semantic_signature(normalized)
        validator_tokens.setdefault(token, normalized)
        obligations.update(_fast_path_validator_obligations(normalized))

    return {
        "taskType": _first_valid_task_type(
            phase.get("task_type"),
            "web_scrape",
        ),
        "expectedArtifact": expected,
        "validators": [
            validator_tokens[token] for token in sorted(validator_tokens)
        ],
        "validatorObligations": sorted(obligations),
    }


def _fast_path_business_contract_signature(
    phase: JsonDict,
    worker_contract: JsonDict,
) -> str:
    payload = _canonical_fast_path_business_contract(phase, worker_contract)
    blob = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _business_contract_obligations(contract: JsonDict) -> Set[str]:
    raw = contract.get("validatorObligations")
    if isinstance(raw, list):
        return {str(item) for item in raw if str(item).strip()}
    obligations: Set[str] = set()
    for validator in contract.get("validators") or []:
        if isinstance(validator, dict):
            obligations.update(_fast_path_validator_obligations(validator))
    return obligations


def _fast_path_business_contract_fence_errors(
    expected: JsonDict,
    actual: JsonDict,
) -> List[str]:
    errors: List[str] = []
    if str(actual.get("taskType") or "") != str(expected.get("taskType") or ""):
        errors.append(
            "task_type changed across the checkpoint"
        )
    if actual.get("expectedArtifact") != expected.get("expectedArtifact"):
        errors.append(
            "merged expected_artifact changed across the checkpoint"
        )
    missing = sorted(
        _business_contract_obligations(expected)
        - _business_contract_obligations(actual)
    )
    if missing:
        errors.append(
            "non-slice validator obligations were removed or weakened: "
            + json.dumps(missing, ensure_ascii=False)
        )
    return errors


def _fast_path_cohort_key(
    phase: JsonDict,
    worker_contract: JsonDict,
    batch_receipt: JsonDict,
) -> str:
    payload = {
        "sourceArtifactPath": str(batch_receipt.get("artifactPath") or ""),
        "sourceArtifactGeneration": str(
            batch_receipt.get("sourceArtifactGeneration")
            or batch_receipt.get("artifactGeneration")
            or ""
        ),
        "cohortSourceIndices": sorted(
            int(item)
            for item in (batch_receipt.get("cohortSourceIndices") or [])
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        ),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _replan_checkpoint_map(state: JsonDict) -> Dict[str, JsonDict]:
    """Read the authoritative per-cohort map, migrating the legacy singleton."""

    checkpoints: Dict[str, JsonDict] = {}
    raw = state.get("replan_checkpoints")
    if isinstance(raw, dict):
        for raw_key, value in raw.items():
            if not isinstance(value, dict):
                continue
            cohort_key = str(value.get("cohortKey") or raw_key or "").strip()
            if cohort_key:
                checkpoints[cohort_key] = copy.deepcopy(value)
    legacy = state.get("replan_checkpoint")
    if isinstance(legacy, dict):
        cohort_key = str(legacy.get("cohortKey") or "").strip()
        if cohort_key and cohort_key not in checkpoints:
            checkpoints[cohort_key] = copy.deepcopy(legacy)
    # Checkpoints created before evidence-driven escalation forced every
    # successful probe into validation, even when assessment explicitly said
    # no reusable candidate existed.  Interpret both map and legacy receipts
    # conservatively as slow-path continuation so an existing task is not
    # permanently bound to false confidence semantics.
    for checkpoint in checkpoints.values():
        if (
            checkpoint.get("active")
            and checkpoint.get("completedRole") == "probe"
            and checkpoint.get("requiredNextRole") == "validation"
            and checkpoint.get("fastPathEligible") is False
        ):
            checkpoint["requiredNextRole"] = "continuation"
            checkpoint["next_instruction"] = (
                "This probe produced no reusable fast-path candidate. Continue"
                " the same cohort with execution_role=continuation and only the"
                " remaining source indices; do not claim validation or bulk"
                " confidence."
            )
    return checkpoints


def _required_next_execution_role(
    *,
    completed_role: str,
    remaining: List[int],
    fast_path_assessment: JsonDict,
) -> str:
    if not remaining:
        return ""
    candidate = str(fast_path_assessment.get("status") or "") == "candidate"
    if completed_role == "probe":
        return "validation" if candidate else "continuation"
    if completed_role == "validation":
        return "bulk" if candidate else "continuation"
    if completed_role == "bulk":
        return "bulk" if candidate else "continuation"
    if completed_role == "continuation":
        return "validation" if candidate else "continuation"
    return ""


def active_replan_checkpoints(state: JsonDict) -> List[JsonDict]:
    """Return every active checkpoint in deterministic cohort order."""

    return [
        checkpoint
        for _, checkpoint in sorted(_replan_checkpoint_map(state).items())
        if checkpoint.get("active")
    ]


def reconcile_replan_checkpoints(logger: RunLogger) -> JsonDict:
    """Mechanically retire checkpoints whose evidence can no longer advance.

    Retirement is not completion.  It removes an impossible continuation
    obligation while retaining the checkpoint and validated-row audit trail.
    A changed source snapshot must start a fresh probe; a missing source must
    be regenerated; an exhausted objective may only finish as incomplete.
    """

    state = load_task_state(logger)
    checkpoints = _replan_checkpoint_map(state)
    if not checkpoints:
        return state
    ledger_paths = {
        str(Path(item).expanduser().resolve())
        for item in (state.get("artifacts") or [])
        if str(item).strip()
    }
    objective_attempts = (
        state.get("objective_attempts")
        if isinstance(state.get("objective_attempts"), dict)
        else {}
    )
    changed = False
    for checkpoint in checkpoints.values():
        if not checkpoint.get("active"):
            continue
        reason = ""
        next_instruction = ""
        objective = str(
            checkpoint.get("nextObjectiveFingerprint")
            or checkpoint.get("objectiveFingerprint")
            or ""
        ).strip()
        objective_entry = objective_attempts.get(objective)
        objective_count = (
            int(objective_entry.get("count") or 0)
            if isinstance(objective_entry, dict)
            else 0
        )
        if objective and objective_count >= OBJECTIVE_MAX_ATTEMPTS:
            reason = "objective_exhausted"
            next_instruction = (
                "The same objective exhausted its mechanical retry budget."
                " Preserve validated rows and finish incomplete, or change the"
                " actual target rather than renaming the phase."
            )
        elif checkpoint.get("sourceLedgerBound") is True:
            source_path = str(checkpoint.get("sourceArtifactPath") or "").strip()
            resolved = str(Path(source_path).expanduser().resolve()) if source_path else ""
            if not resolved or resolved not in ledger_paths or not Path(resolved).is_file():
                reason = "source_artifact_missing"
                next_instruction = (
                    "The validated source artifact disappeared from the ledger."
                    " Re-run its upstream producer before creating a fresh probe."
                )
            else:
                try:
                    payload = json.loads(Path(resolved).read_text(encoding="utf-8"))
                    blob = json.dumps(
                        payload,
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )
                    current_generation = hashlib.sha256(
                        blob.encode("utf-8")
                    ).hexdigest()[:16]
                except (OSError, json.JSONDecodeError):
                    current_generation = ""
                expected_generation = str(
                    checkpoint.get("sourceArtifactGeneration") or ""
                ).strip()
                if not current_generation:
                    reason = "source_artifact_missing"
                    next_instruction = (
                        "The source artifact is unreadable. Re-run its upstream"
                        " producer before creating a fresh probe."
                    )
                elif expected_generation and current_generation != expected_generation:
                    reason = "source_generation_superseded"
                    next_instruction = (
                        "The source artifact changed after this checkpoint."
                        " Source indices are no longer authoritative; start a"
                        " fresh probe against the new validated generation."
                    )
        if not reason:
            continue
        checkpoint["active"] = False
        checkpoint["requiredNextRole"] = None
        checkpoint["terminalReason"] = reason
        checkpoint["invalidatedAt"] = utc_now_iso()
        checkpoint["next_instruction"] = next_instruction
        logger.write("fast_path.replan_checkpoint_invalidated", {
            "checkpointId": checkpoint.get("checkpointId"),
            "cohortKey": checkpoint.get("cohortKey"),
            "reason": reason,
            "validatedSourceIndices": checkpoint.get("validatedSourceIndices"),
            "remainingSourceIndices": checkpoint.get("remainingSourceIndices"),
        })
        changed = True
    if changed:
        state["replan_checkpoints"] = checkpoints
        state.pop("replan_checkpoint", None)
        write_task_state(logger, state)
    return state


def _checkpoint_receipts(
    contract: JsonDict,
) -> Tuple[Optional[JsonDict], Optional[JsonDict]]:
    """The cohort a checkpoint binds and the slice this worker completed.

    A probe owns exactly one item and must not have to pose as a batch to be
    recorded, so the cohort and the slice are separate receipts. Contracts
    materialized before the split carry only `_batch_source_receipt`; it
    answered both questions at once, so it can still be read as both.
    """
    cohort = contract.get("_source_cohort_receipt")
    execution = contract.get("_execution_slice_receipt")
    if isinstance(cohort, dict) and isinstance(execution, dict):
        return cohort, execution
    legacy = contract.get("_batch_source_receipt")
    if not isinstance(legacy, dict):
        return None, None
    return legacy, legacy


def record_replan_checkpoint(
    logger: RunLogger,
    *,
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict],
    worker_id: str,
    fast_path_assessment: Optional[JsonDict],
) -> Optional[JsonDict]:
    """Persist one evidence-driven cohort continuation checkpoint."""

    phase = phase if isinstance(phase, dict) else {}
    contract = worker_contract if isinstance(worker_contract, dict) else {}
    role = str(phase.get("execution_role") or contract.get("execution_role") or "")
    if role not in {"probe", "validation", "bulk", "continuation"}:
        return None
    cohort_receipt, slice_receipt = _checkpoint_receipts(contract)
    if cohort_receipt is None or slice_receipt is None:
        return None
    artifact_path = str(cohort_receipt.get("artifactPath") or "").strip()
    artifact_name = str(cohort_receipt.get("artifactName") or "").strip()
    selected = sorted({
        int(item)
        for item in (slice_receipt.get("selectedSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    })
    source_count = int(cohort_receipt.get("sourceRowCount") or 0)
    cohort_indices = sorted({
        int(item)
        for item in (cohort_receipt.get("cohortSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    })
    if not cohort_indices and source_count > 0:
        # Legacy receipt semantics treated the complete artifact as the cohort.
        cohort_indices = list(range(source_count))
    if (
        not artifact_path
        or not artifact_name
        or source_count <= 0
        or not cohort_indices
        or not selected
        or not set(selected).issubset(cohort_indices)
    ):
        return None

    state = load_task_state(logger)
    phase_id = str(phase.get("id") or contract.get("phase_id") or "")
    phase_state = _phase_state(state, phase_id)
    if (
        not isinstance(phase_state, dict)
        or str(phase_state.get("status") or "") != "validated_done"
    ):
        return None

    source_generation = str(
        cohort_receipt.get("sourceArtifactGeneration")
        or cohort_receipt.get("artifactGeneration")
        or ""
    ).strip()
    if not source_generation:
        # Compatibility for a contract materialized before Stage 6B-A.
        source_generation = hashlib.sha256(
            artifact_path.encode("utf-8")
        ).hexdigest()[:16]
    checkpoints = _replan_checkpoint_map(state)
    predecessor_id = str(
        contract.get("replan_checkpoint_id") or ""
    ).strip()
    predecessor = next(
        (
            item for item in checkpoints.values()
            if predecessor_id
            and str(item.get("checkpointId") or "") == predecessor_id
            and item.get("active")
        ),
        None,
    )
    if role != "probe" and predecessor is None:
        logger.write("fast_path.replan_checkpoint_predecessor_mismatch", {
            "checkpointId": predecessor_id or None,
            "phaseId": phase_id,
            "workerId": worker_id,
            "reason": "checkpoint_advancing_role_requires_active_predecessor",
        })
        return None
    computed_cohort_key = _fast_path_cohort_key(phase, contract, cohort_receipt)
    if predecessor is not None:
        if (
            str(predecessor.get("sourceArtifactPath") or "") != artifact_path
            or str(predecessor.get("sourceArtifactGeneration") or "")
            != source_generation
            or sorted(predecessor.get("cohortSourceIndices") or [])
            != cohort_indices
        ):
            logger.write("fast_path.replan_checkpoint_predecessor_mismatch", {
                "checkpointId": predecessor_id,
                "expectedSourceArtifactPath": predecessor.get(
                    "sourceArtifactPath"
                ),
                "actualSourceArtifactPath": artifact_path,
                "expectedSourceArtifactGeneration": predecessor.get(
                    "sourceArtifactGeneration"
                ),
                "actualSourceArtifactGeneration": source_generation,
                "expectedCohortSourceIndices": predecessor.get(
                    "cohortSourceIndices"
                ),
                "actualCohortSourceIndices": cohort_indices,
                "phaseId": phase_id,
                "workerId": worker_id,
            })
            return None
        # A validated successor advances the existing cohort in place.  New
        # phase ids and execution profiles never create a second progress
        # ledger for the same evidence chain.
        cohort_key = str(predecessor.get("cohortKey") or "").strip()
        if not cohort_key:
            return None
    else:
        cohort_key = computed_cohort_key
    if not str(phase.get("task_type") or "").strip():
        logger.write("fast_path.replan_checkpoint_contract_degraded", {
            "phaseId": phase_id,
            "workerId": worker_id,
            "reason": "task_type_missing_defaulted_to_web_scrape",
            "cohortKey": cohort_key,
        })
    progress = state.setdefault("batch_progress", {}).setdefault(
        cohort_key,
        {
            "sourceArtifactName": artifact_name,
            "sourceArtifactPath": artifact_path,
            "sourceArtifactGeneration": source_generation,
            "sourceRowCount": source_count,
            "cohortSourceIndices": cohort_indices,
            "validatedSourceIndices": [],
        },
    )
    if (
        str(progress.get("sourceArtifactPath") or "") != artifact_path
        or (
            str(progress.get("sourceArtifactGeneration") or source_generation)
            != source_generation
        )
        or int(progress.get("sourceRowCount") or 0) != source_count
        or sorted(progress.get("cohortSourceIndices") or []) != cohort_indices
    ):
        # Same cohort hash should make this impossible.  Fail closed instead of
        # merging progress from another artifact generation.
        return None
    completed = {
        int(item)
        for item in (progress.get("validatedSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    }
    if predecessor is not None:
        predecessor_completed = {
            int(item)
            for item in (predecessor.get("validatedSourceIndices") or [])
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        }
        predecessor_remaining = {
            int(item)
            for item in (predecessor.get("remainingSourceIndices") or [])
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        }
        if not predecessor_remaining:
            predecessor_remaining = set(cohort_indices) - predecessor_completed
        if (
            not set(selected).issubset(predecessor_remaining)
            or set(selected).intersection(predecessor_completed)
        ):
            logger.write("fast_path.replan_checkpoint_progress_mismatch", {
                "checkpointId": predecessor_id,
                "phaseId": phase_id,
                "workerId": worker_id,
                "selectedSourceIndices": selected,
                "validatedSourceIndices": sorted(predecessor_completed),
                "remainingSourceIndices": sorted(predecessor_remaining),
            })
            return None
        completed.update(predecessor_completed)
    completed.update(selected)
    progress["validatedSourceIndices"] = sorted(completed)
    remaining = [
        index for index in cohort_indices if index not in completed
    ]
    generation = int(state.get("fast_path_generation") or 0) + 1
    state["fast_path_generation"] = generation
    checkpoint_seed = (
        f"{getattr(logger, 'task_id', '')}|{cohort_key}|{generation}|"
        f"{phase_id}|{','.join(str(item) for item in completed)}"
    )
    checkpoint_id = hashlib.sha256(
        checkpoint_seed.encode("utf-8")
    ).hexdigest()[:20]
    assessment = (
        copy.deepcopy(fast_path_assessment)
        if isinstance(fast_path_assessment, dict)
        else {
            "status": "not_compilable",
            "executionPolicy": "not_executable_stage_6b_a",
            "reasons": ["assessment_unavailable"],
        }
    )
    required_next_role = _required_next_execution_role(
        completed_role=role,
        remaining=remaining,
        fast_path_assessment=assessment,
    )
    candidate = assessment.get("candidate")
    if isinstance(candidate, dict):
        candidate.update({
            "checkpointId": checkpoint_id,
            "cohortKey": cohort_key,
            "generation": generation,
            "sourceArtifactGeneration": source_generation,
            "validatedAgainstSourceIndices": selected,
        })
    checkpoint: JsonDict = {
        "version": "v1",
        "checkpointId": checkpoint_id,
        "active": bool(required_next_role),
        "phaseId": phase_id,
        "workerId": worker_id,
        # Audit-only lineage.  Enforcement deliberately keys off the
        # predecessor checkpoint's long-standing phaseId field so checkpoints
        # written before these optional fields existed remain usable.
        "predecessorCheckpointId": (
            str(predecessor.get("checkpointId") or "")
            if isinstance(predecessor, dict)
            else None
        ),
        "predecessorPhaseId": (
            str(predecessor.get("phaseId") or "")
            if isinstance(predecessor, dict)
            else None
        ),
        "lineageDepth": (
            (
                predecessor.get("lineageDepth")
                if isinstance(predecessor.get("lineageDepth"), int)
                and not isinstance(predecessor.get("lineageDepth"), bool)
                else 0
            ) + 1
            if isinstance(predecessor, dict)
            else 0
        ),
        "completedRole": role,
        "requiredNextRole": required_next_role or None,
        "cohortKey": cohort_key,
        "businessContract": _canonical_fast_path_business_contract(
            phase, contract,
        ),
        "businessContractSignature": (
            _fast_path_business_contract_signature(phase, contract)
        ),
        "executionProfile": {
            "stageHint": str(
                contract.get("stage_hint") or phase.get("stage_hint") or ""
            ),
            "strategyIds": sorted(
                str(item)
                for item in (contract.get("strategy_ids") or [])
                if str(item).strip()
            ),
            "completedRole": role,
        },
        "sourceArtifactName": artifact_name,
        "sourceArtifactPath": artifact_path,
        "sourceArtifactGeneration": source_generation,
        "sourceLedgerBound": artifact_path in {
            str(Path(item).expanduser().resolve())
            for item in (state.get("artifacts") or [])
            if str(item).strip()
        },
        "objectiveFingerprint": objective_fingerprint(phase, contract),
        "sourceRowCount": source_count,
        "cohortRowCount": len(cohort_indices),
        "cohortSourceIndices": cohort_indices,
        "cohortSelector": copy.deepcopy(cohort_receipt.get("cohortSelector") or {}),
        "validatedSourceIndices": sorted(completed),
        "remainingSourceIndices": remaining,
        "fastPathEligible": assessment.get("status") == "candidate",
        "fastPathAssessment": assessment,
        "next_instruction": (
            (
                f"Continue this cohort with execution_role={required_next_role}"
                " using the same business artifact contract and validated"
                " batch_source, but only the remaining source indices. stage_hint"
                " and strategy choice are execution profile and may change. Retain"
                f" validated predecessor phase {phase_id!r} in the replacement"
                " plan and list it in the next phase's depends_on. If emitting a"
                " replan, acknowledge this exact"
                f" checkpointId={checkpoint_id}. Active-cohort failed rows use"
                " execution_role=continuation, not remediation."
            )
            if required_next_role
            else "This batch_source cohort has no remaining rows."
        ),
    }
    checkpoints[cohort_key] = checkpoint
    state["replan_checkpoints"] = checkpoints
    state.pop("replan_checkpoint", None)
    write_task_state(logger, state)
    logger.write("fast_path.replan_checkpoint", checkpoint)
    return checkpoint


def replan_checkpoint_spawn_rejection(
    logger: RunLogger,
    *,
    phase: JsonDict,
    worker_contract: JsonDict,
) -> Optional[JsonDict]:
    """Reject same-source horizontal retries that ignore an active checkpoint."""

    state = reconcile_replan_checkpoints(logger)
    active = active_replan_checkpoints(state)
    if not active:
        return None
    receipt = worker_contract.get("_batch_source_receipt")
    if not isinstance(receipt, dict):
        return None
    cohort_key = _fast_path_cohort_key(phase, worker_contract, receipt)
    explicit_id = str(
        worker_contract.get("replan_checkpoint_id") or ""
    ).strip()
    receipt_indices = {
        int(item)
        for item in (receipt.get("cohortSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    }
    checkpoint: Optional[JsonDict] = None
    if explicit_id:
        checkpoint = next(
            (
                item for item in active
                if str(item.get("checkpointId") or "") == explicit_id
            ),
            None,
        )
        if checkpoint is None:
            return {
                "status": "replan_checkpoint_required",
                "phaseId": str(phase.get("id") or ""),
                "checkpointId": explicit_id,
                "errors": ["unknown or inactive replan checkpoint id"],
                "tool_was_executed": False,
                "next_instruction": (
                    "Bind this phase to one active checkpoint id returned by"
                    " the harness; do not invent or reuse an inactive id."
                ),
            }
    else:
        overlapping = [
            item for item in active
            if (
                str(item.get("sourceArtifactPath") or "")
                == str(receipt.get("artifactPath") or "")
                and receipt_indices.intersection({
                    int(index)
                    for index in (item.get("cohortSourceIndices") or [])
                    if isinstance(index, int) and not isinstance(index, bool)
                })
            )
        ]
        if overlapping:
            return {
                "status": "replan_checkpoint_required",
                "phaseId": str(phase.get("id") or ""),
                "candidateCheckpointIds": [
                    item.get("checkpointId") for item in overlapping
                ],
                "actualCohortKey": cohort_key,
                "errors": [
                    "batch_source rows overlap an active checkpoint but the"
                    " phase did not bind replan_checkpoint_id"
                ],
                "tool_was_executed": False,
                "next_instruction": (
                    "Bind the phase to the matching active checkpoint. A new"
                    " phase id or changed business contract does not create a"
                    " new cohort when source rows overlap."
                ),
            }
    if checkpoint is None:
        return None

    reasons: List[str] = []
    if (
        str(receipt.get("artifactPath") or "")
        != str(checkpoint.get("sourceArtifactPath") or "")
    ):
        reasons.append("batch_source artifact does not match the checkpoint")
    checkpoint_indices = {
        int(item)
        for item in (checkpoint.get("cohortSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    }
    if receipt_indices != checkpoint_indices:
        reasons.append("batch_source cohort rows do not match the checkpoint")
    expected_business = checkpoint.get("businessContract")
    expected_business = expected_business if isinstance(expected_business, dict) else {}
    actual_business = _canonical_fast_path_business_contract(
        phase, worker_contract,
    )
    if expected_business:
        reasons.extend(
            _fast_path_business_contract_fence_errors(
                expected_business,
                actual_business,
            )
        )
    else:
        logger.write(
            "fast_path.replan_checkpoint_business_contract_unavailable",
            {
                "checkpointId": checkpoint.get("checkpointId"),
                "cohortKey": checkpoint.get("cohortKey"),
                "phaseId": str(phase.get("id") or ""),
                "activeFences": [
                    "source_artifact_path",
                    "source_artifact_generation",
                    "cohort_source_indices",
                    "remaining_source_indices",
                    "execution_role",
                ],
            },
        )
    expected_business_signature = str(
        checkpoint.get("businessContractSignature") or ""
    ).strip()
    actual_business_signature = _fast_path_business_contract_signature(
        phase, worker_contract,
    )
    receipt_generation = str(
        receipt.get("sourceArtifactGeneration") or ""
    ).strip()
    checkpoint_generation = str(
        checkpoint.get("sourceArtifactGeneration") or ""
    ).strip()
    if (
        receipt_generation
        and checkpoint_generation
        and receipt_generation != checkpoint_generation
    ):
        reasons.append(
            "batch_source artifact generation changed after the checkpoint"
        )
    role = str(phase.get("execution_role") or worker_contract.get("execution_role") or "")
    required = str(checkpoint.get("requiredNextRole") or "")
    predecessor_phase_id = str(checkpoint.get("phaseId") or "").strip()
    declared_dependencies = _phase_dependency_ids(phase)
    selected = {
        int(item)
        for item in (receipt.get("selectedSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool)
    }
    completed = {
        int(item)
        for item in (checkpoint.get("validatedSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool)
    }
    remaining = {
        int(item)
        for item in (checkpoint.get("remainingSourceIndices") or [])
        if isinstance(item, int) and not isinstance(item, bool)
    }
    if role != required:
        reasons.append(f"execution_role must be {required!r}, got {role!r}")
    if role == "remediation":
        reasons.append(
            "active checkpoint rows must use execution_role='continuation';"
            " remediation is reserved for explicit failed-row sets outside an"
            " active cohort"
        )
    if (
        not predecessor_phase_id
        or declared_dependencies is None
        or predecessor_phase_id not in declared_dependencies
    ):
        reasons.append(
            "checkpoint-bound phase must explicitly depend_on validated"
            f" predecessor phase {predecessor_phase_id or '<missing>'!r}; retain"
            " that phase in the replacement plan"
        )
    if selected.intersection(completed):
        reasons.append("selection repeats already validated source rows")
    if not selected or not selected.issubset(remaining):
        reasons.append("selection must be a non-empty subset of remaining source rows")
    if not reasons:
        next_objective = objective_fingerprint(phase, worker_contract)
        if next_objective:
            checkpoints = _replan_checkpoint_map(state)
            checkpoint_key = str(checkpoint.get("cohortKey") or "").strip()
            stored = checkpoints.get(checkpoint_key)
            if isinstance(stored, dict):
                stored["nextObjectiveFingerprint"] = next_objective
                state["replan_checkpoints"] = checkpoints
                state.pop("replan_checkpoint", None)
                write_task_state(logger, state)
        return None
    return {
        "status": "replan_checkpoint_required",
        "phaseId": str(phase.get("id") or ""),
        "checkpointId": checkpoint.get("checkpointId"),
        "requiredNextRole": required,
        "expectedBusinessContractSignature": (
            expected_business_signature or None
        ),
        "actualBusinessContractSignature": actual_business_signature,
        "expectedBusinessContract": expected_business or None,
        "actualBusinessContract": actual_business,
        "expectedCohortKey": checkpoint.get("cohortKey"),
        "actualCohortKey": cohort_key,
        "expectedCohortSourceIndices": sorted(checkpoint_indices),
        "actualCohortSourceIndices": sorted(receipt_indices),
        "expectedCohortSelector": checkpoint.get("cohortSelector") or {},
        "actualCohortSelector": receipt.get("cohortSelector") or {},
        "remainingSourceIndices": sorted(remaining),
        "errors": reasons,
        "tool_was_executed": False,
        "next_instruction": checkpoint.get("next_instruction"),
    }


def replan_checkpoint_plan_errors(
    plan: JsonDict,
    previous_state: JsonDict,
) -> List[str]:
    """Require a replan to acknowledge and advance an active checkpoint."""

    plan_phases = [
        phase for phase in (plan.get("phases") or [])
        if isinstance(phase, dict)
    ]
    checkpoints = active_replan_checkpoints(previous_state)
    if not checkpoints:
        conditional_phases = []
        for phase in plan_phases:
            role = str(phase.get("execution_role") or "")
            contract = phase.get("worker_contract")
            contract = contract if isinstance(contract, dict) else {}
            if role in {"validation", "bulk", "continuation"} or str(
                contract.get("replan_checkpoint_id") or ""
            ).strip():
                conditional_phases.append(str(phase.get("id") or "<unnamed>"))
        if conditional_phases:
            return [
                "conditional execution phases require an active validated"
                " replan checkpoint; do not pre-create or invent checkpoint ids"
                f" for phases {conditional_phases}"
            ]
        return []
    errors: List[str] = []
    expected_ids = {
        str(checkpoint.get("checkpointId") or "")
        for checkpoint in checkpoints
        if str(checkpoint.get("checkpointId") or "")
    }
    supplied_ids = {
        str(item).strip()
        for item in (plan.get("replan_checkpoint_ids") or [])
        if str(item).strip()
    }
    legacy_id = str(plan.get("replan_checkpoint_id") or "").strip()
    if legacy_id:
        supplied_ids.add(legacy_id)
    if supplied_ids != expected_ids:
        missing = sorted(expected_ids - supplied_ids)
        unexpected = sorted(supplied_ids - expected_ids)
        errors.append(
            "replan must acknowledge the exact active checkpoint set;"
            f" missing={missing}, unexpected={unexpected}"
        )
        return errors

    previous_phase_states = (
        previous_state.get("phases")
        if isinstance(previous_state.get("phases"), dict)
        else {}
    )

    def _is_validated_history(phase: JsonDict) -> bool:
        phase_state = previous_phase_states.get(str(phase.get("id") or ""))
        return (
            isinstance(phase_state, dict)
            and str(phase_state.get("status") or "") == "validated_done"
        )

    phases_by_checkpoint: Dict[str, List[JsonDict]] = {}
    for phase in plan_phases:
        # Complete-replacement plans retain the original worker contract for
        # audit.  A validated conditional predecessor therefore still carries
        # the checkpoint id it consumed, which is now inactive.  It is history,
        # not a new binding to validate against the current active id set.
        if _is_validated_history(phase):
            continue
        contract = phase.get("worker_contract")
        contract = contract if isinstance(contract, dict) else {}
        checkpoint_id = str(
            contract.get("replan_checkpoint_id") or ""
        ).strip()
        if checkpoint_id:
            phases_by_checkpoint.setdefault(checkpoint_id, []).append(phase)

    matched_phase_objects: Set[int] = set()
    for checkpoint in checkpoints:
        checkpoint_id = str(checkpoint.get("checkpointId") or "")
        artifact_name = str(checkpoint.get("sourceArtifactName") or "")
        required_role = str(checkpoint.get("requiredNextRole") or "")
        matches = phases_by_checkpoint.get(checkpoint_id, [])
        if not matches and len(checkpoints) == 1:
            # Backward compatibility for the original single-checkpoint plan
            # shape, where only the top-level id was present.
            for phase in plan_phases:
                if _is_validated_history(phase):
                    continue
                contract = phase.get("worker_contract")
                contract = contract if isinstance(contract, dict) else {}
                source = contract.get("batch_source")
                if (
                    isinstance(source, dict)
                    and str(source.get("artifact_name") or "") == artifact_name
                    and str(phase.get("execution_role") or "") == required_role
                ):
                    matches.append(phase)
        if len(matches) != 1:
            errors.append(
                f"checkpoint {checkpoint_id!r} must bind exactly one phase"
                " via worker_contract.replan_checkpoint_id"
            )
            continue
        phase = matches[0]
        matched_phase_objects.add(id(phase))
        predecessor_phase_id = str(checkpoint.get("phaseId") or "").strip()
        retained_phase_ids = {
            str(item.get("id") or "").strip() for item in plan_phases
        }
        if not predecessor_phase_id or predecessor_phase_id not in retained_phase_ids:
            errors.append(
                f"checkpoint {checkpoint_id!r} requires retaining its validated"
                f" predecessor phase {predecessor_phase_id or '<missing>'!r} in"
                " the replacement plan and referencing it from depends_on"
            )
        else:
            declared_dependencies = _phase_dependency_ids(phase)
            if (
                declared_dependencies is None
                or predecessor_phase_id not in declared_dependencies
            ):
                errors.append(
                    f"checkpoint {checkpoint_id!r} requires phase"
                    f" {str(phase.get('id') or '<unnamed>')!r} to depend_on its"
                    f" validated predecessor phase {predecessor_phase_id!r}; keep"
                    " that phase in the plan and explicitly add it to depends_on"
                )
        contract = phase.get("worker_contract")
        contract = contract if isinstance(contract, dict) else {}
        source = contract.get("batch_source")
        source = source if isinstance(source, dict) else {}
        if str(phase.get("execution_role") or "") != required_role:
            errors.append(
                f"checkpoint {checkpoint_id!r} requires"
                f" execution_role={required_role!r}"
            )
        if str(source.get("artifact_name") or "") != artifact_name:
            errors.append(
                f"checkpoint {checkpoint_id!r} requires batch_source"
                f" artifact_name={artifact_name!r}"
            )
        expected_business = checkpoint.get("businessContract")
        expected_business = (
            expected_business if isinstance(expected_business, dict) else {}
        )
        if expected_business:
            # phase.task_type is the sole policy authority. Never reconstruct a
            # missing worker value from plan.task_type at this fence.
            actual_business = _canonical_fast_path_business_contract(
                phase,
                dict(contract),
            )
            for reason in _fast_path_business_contract_fence_errors(
                expected_business,
                actual_business,
            ):
                errors.append(f"checkpoint {checkpoint_id!r}: {reason}")
        expected_selector = _canonical_cohort_selector(
            checkpoint.get("cohortSelector")
        )
        actual_selector = _canonical_cohort_selector(
            source.get("cohort_selector")
        )
        if actual_selector != expected_selector:
            errors.append(
                f"checkpoint {checkpoint_id!r} requires the same"
                " batch_source.cohort_selector"
            )

    # A new probe may share an artifact with an active cohort only when the
    # declarations themselves prove disjointness.  Precise row-index overlap
    # is checked again after source materialization at the spawn boundary.
    for phase in plan_phases:
        if id(phase) in matched_phase_objects:
            continue
        phase_id = str(phase.get("id") or "")
        prior_phase_state = previous_phase_states.get(phase_id)
        prior_status = (
            str(prior_phase_state.get("status") or "")
            if isinstance(prior_phase_state, dict)
            else ""
        )
        # A complete-replacement replan intentionally retains validated
        # history so downstream depends_on edges remain auditable.  Such a
        # phase cannot run again and therefore is not a competing cohort.
        # Do not broaden this to every terminal status: phase_failed and
        # blocked_by_dependency are reset to pending by initialize_task_state.
        if prior_status == "validated_done":
            continue
        contract = phase.get("worker_contract")
        contract = contract if isinstance(contract, dict) else {}
        if str(contract.get("replan_checkpoint_id") or "").strip():
            continue
        source = contract.get("batch_source")
        source = source if isinstance(source, dict) else {}
        artifact_name = str(source.get("artifact_name") or "").strip()
        if not artifact_name:
            continue
        for checkpoint in checkpoints:
            if phase_id == str(checkpoint.get("phaseId") or ""):
                continue
            if artifact_name != str(checkpoint.get("sourceArtifactName") or ""):
                continue
            if _cohort_selectors_provably_disjoint(
                checkpoint.get("cohortSelector"),
                source.get("cohort_selector"),
            ):
                continue
            errors.append(
                f"phase {str(phase.get('id') or '<unnamed>')!r} may overlap"
                f" active checkpoint {str(checkpoint.get('checkpointId') or '')!r};"
                " bind that checkpoint or declare a provably disjoint"
                " batch_source.cohort_selector"
            )

    unknown_bindings = sorted(
        set(phases_by_checkpoint) - expected_ids
    )
    if unknown_bindings:
        errors.append(
            f"phases bind unknown or inactive checkpoints: {unknown_bindings}"
        )
    return errors


def _empty_phase_state() -> JsonDict:
    return {
        "status": "pending",
        "attempts": [],
        "validated_artifacts": [],
        "last_failure": None,
    }


def _ensure_phase_state_defaults(phase_state: JsonDict) -> None:
    phase_state.setdefault("status", "pending")
    phase_state.setdefault("attempts", [])
    phase_state.setdefault("validated_artifacts", [])
    phase_state.setdefault("last_failure", None)


def _first_active_phase_id(plan: JsonDict, phases_state: JsonDict) -> Optional[str]:
    for phase in plan.get("phases", []):
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        status = (
            phases_state.get(phase_id, {}).get("status")
            if isinstance(phases_state.get(phase_id), dict)
            else None
        )
        if status not in TERMINAL_PHASE_STATUSES:
            return phase_id
    return _first_phase_id(plan)


def write_task_state(logger: RunLogger, state: JsonDict) -> str:
    state["updated_at"] = utc_now_iso()
    path = _state_path(logger)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path.resolve())


def contract_hash_for_phase(
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict],
    *,
    task: str = "",
    result_contract: str = "",
) -> str:
    phase = phase if isinstance(phase, dict) else {}
    worker_contract = worker_contract if isinstance(worker_contract, dict) else {}
    payload = {
        "phaseId": str(phase.get("id") or worker_contract.get("phase_id") or ""),
        "taskType": str(phase.get("task_type") or "web_scrape"),
        "stageHint": str(
            worker_contract.get("stage_hint")
            or phase.get("stage_hint")
            or ""
        ),
        "objective": str(
            worker_contract.get("objective")
            or phase.get("objective")
            or ""
        ),
        "workerTask": str(task or phase.get("worker_task") or ""),
        "resultContract": str(result_contract or ""),
        "workerContract": worker_contract,
        "expectedArtifact": (
            worker_contract.get("expected_artifact")
            if isinstance(worker_contract.get("expected_artifact"), dict)
            else phase.get("expected_artifact")
        ),
        "validators": (
            worker_contract.get("validators")
            if isinstance(worker_contract.get("validators"), list)
            else phase.get("validators")
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


_SOURCE_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+")


def _normalized_source_urls(*texts: Any) -> List[str]:
    """Normalized source identities mentioned by a phase (host+path, scheme/
    www/query/trailing-slash and trailing sentence punctuation stripped).
    Bounded and sorted for stability.

    Caveat (by design): URLs are regex-extracted from natural-language
    worker_task/objective text, so this dimension is only as stable as the
    Lead's phrasing — it is an AUXILIARY discriminator (so "same range,
    different source" unlocks the budget). The primary objective key remains
    the numeric validators + artifact name."""
    urls: Set[str] = set()
    for text in texts:
        for raw in _SOURCE_URL_RE.findall(str(text or "")):
            # Regex capture over prose swallows sentence punctuation:
            # "... from https://x/trending/week/." must not mint a fresh
            # fingerprint via that trailing dot.
            parsed = urlparse(raw.rstrip(".,;:!?)"))
            host = str(parsed.netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if not host:
                continue
            path = str(parsed.path or "").rstrip("/")
            urls.add(f"{host}{path}")
    return sorted(urls)[:3]


def _fingerprint_num(value: Any) -> Any:
    """Numeric normalization so 40 and "40" fingerprint identically."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return int(number) if number == int(number) else number


def objective_fingerprint(
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict] = None,
) -> str:
    """Cross-replan identity of WHAT a phase is trying to obtain.

    Phase ids and artifact names drift across replans (2cb616:
    collect_trending_40_50 → _v2 → _v3, trending_week_40_50 →
    trending_week_products_40_50) while the actual objective — "rows with
    rank 40-50, exactly 11 of them, from theresanaiforthat.com/trending/week"
    — stays identical. The key combines the normalized source URLs with the
    numeric validator features (range bounds, expected row counts); the
    normalized artifact name is the fallback when a phase carries no numeric
    target. Changing any of these means genuinely changing the objective
    (different source, different range, different artifact), which is
    exactly when the accumulated budget should reset.

    When the Lead spawns with a worker_contract override, THAT is what the
    worker actually runs — its expected_artifact/validators/texts take
    precedence over the raw phase (same merge semantics as phase_contract),
    so the gate and the execution stay in sync.
    Returns "" (no fingerprint, never gated) when nothing usable exists.
    """
    if not isinstance(phase, dict):
        return ""
    contract = worker_contract if isinstance(worker_contract, dict) else {}
    expected = dict(
        phase.get("expected_artifact")
        if isinstance(phase.get("expected_artifact"), dict) else {}
    )
    contract_expected = contract.get("expected_artifact")
    if isinstance(contract_expected, dict):
        expected.update(contract_expected)
    validators = (
        contract.get("validators")
        if isinstance(contract.get("validators"), list)
        else phase.get("validators")
    )
    sources = _normalized_source_urls(
        contract.get("worker_task") or phase.get("worker_task"),
        contract.get("objective") or phase.get("objective"),
    )
    ranges: List[List[Any]] = []
    counts: List[List[Any]] = []
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        vtype = str(validator.get("type") or "")
        if vtype == "range":
            ranges.append([
                str(validator.get("field") or ""),
                _fingerprint_num(validator.get("min")),
                _fingerprint_num(validator.get("max")),
            ])
        elif vtype in {"exact_rows", "min_rows"}:
            for key in ("value", "count", "exact", "min"):
                value = validator.get(key)
                if value is None:
                    continue
                # Same tolerance as _run_validator's _positive_int: a
                # string "11" validates identically to 11, so it must
                # fingerprint identically too.
                normalized = _fingerprint_num(value)
                if isinstance(normalized, (int, float)) and normalized:
                    counts.append([vtype, int(normalized)])
                    break
    name = str(expected.get("name") or "").strip().lower()
    name = re.sub(r"[_-]v\d+$", "", name)
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    ranges.sort()
    counts.sort()
    if ranges:
        features: List[Any] = ["ranges", sources, ranges, counts]
    elif counts and name:
        # Counts alone are too weak (two detail phases may both expect 4
        # rows); anchor them with the name.
        features = ["named_counts", sources, name, counts]
    elif name:
        features = ["name", sources, name]
    else:
        return ""
    blob = json.dumps(features, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def spawn_acquisition_fingerprint(
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict],
    *,
    reuse_scope: str,
    page_policy: str,
    session_key: str,
    fleet_id: str = "",
    preferred_slot_id: Optional[str] = None,
    reuse_from_worker_id: Optional[str] = None,
) -> str:
    """Stable identity of one pre-worker slot/fleet acquisition path."""

    contract = worker_contract if isinstance(worker_contract, dict) else {}
    objective = objective_fingerprint(phase, contract)
    if not objective:
        objective = str((phase or {}).get("id") or "unscoped")
    payload = {
        "objective": objective,
        "reuseScope": str(reuse_scope or ""),
        "pagePolicy": str(page_policy or ""),
        "sessionKey": str(session_key or ""),
        "fleetReference": str(fleet_id or ""),
        "needsIsolatedSession": bool(contract.get("needs_isolated_session", False)),
        "preferredSlotId": str(preferred_slot_id or ""),
        "reuseFromWorkerId": str(reuse_from_worker_id or ""),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def spawn_acquisition_error_signature(exc: BaseException) -> str:
    """Normalize volatile ids/numbers while preserving the failure class."""

    message = " ".join(str(exc or "").strip().lower().split())
    message = re.sub(r"\b[0-9a-f]{16,}\b", "<id>", message)
    message = re.sub(r"\b\d{4,}\b", "<n>", message)
    return f"{type(exc).__name__}:{message}"[:500]


def spawn_acquisition_rejection(
    logger: RunLogger,
    *,
    acquisition_fingerprint: str,
    phase_id: Optional[str],
) -> Optional[JsonDict]:
    """Reject a route whose same startup error already exhausted its budget."""

    state = load_task_state(logger)
    ledger = state.get("spawn_acquisition_failures")
    ledger = ledger if isinstance(ledger, dict) else {}
    route_entry = ledger.get(acquisition_fingerprint)
    signatures = (
        route_entry.get("signatures") if isinstance(route_entry, dict) else {}
    )
    signatures = signatures if isinstance(signatures, dict) else {}
    for signature, raw_entry in signatures.items():
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        count = int(entry.get("count") or 0)
        retry_at = float(entry.get("retryAtEpoch") or 0.0)
        if count < SPAWN_ACQUISITION_MAX_FAILURES and retry_at > time.time():
            retry_after_ms = max(1, int((retry_at - time.time()) * 1000))
            return {
                "status": "spawn_acquisition_cooldown",
                "phaseId": str(phase_id or ""),
                "acquisitionFingerprint": acquisition_fingerprint,
                "errorSignature": str(signature),
                "failures": count,
                "maxFailures": SPAWN_ACQUISITION_MAX_FAILURES,
                "retryAfterMs": retry_after_ms,
                "tool_was_executed": False,
                "next_instruction": (
                    f"Wait {retry_after_ms} ms, then spawn the SAME phase id "
                    f"{str(phase_id or '')!r}. Do not rename or replan the phase "
                    "to bypass this Fleet acquisition cooldown."
                ),
            }
        if count >= SPAWN_ACQUISITION_MAX_FAILURES:
            return {
                "status": "spawn_infrastructure_exhausted",
                "phaseId": str(phase_id or ""),
                "acquisitionFingerprint": acquisition_fingerprint,
                "errorSignature": str(signature),
                "failures": count,
                "maxFailures": SPAWN_ACQUISITION_MAX_FAILURES,
                "tool_was_executed": False,
                "next_instruction": (
                    "Do not respawn or replan the same objective with the same"
                    " fleet/slot/session routing. This is a bounded startup"
                    " infrastructure failure, not evidence that the business"
                    " objective is infeasible. Change the routing contract or"
                    " report the infrastructure blocker."
                ),
            }
    return None


def record_spawn_acquisition_failure(
    logger: RunLogger,
    *,
    acquisition_fingerprint: str,
    phase_id: Optional[str],
    exc: BaseException,
) -> JsonDict:
    """Persist one startup failure and return its bounded diagnostic receipt."""

    state = load_task_state(logger)
    ledger = state.setdefault("spawn_acquisition_failures", {})
    if not isinstance(ledger, dict):
        ledger = {}
        state["spawn_acquisition_failures"] = ledger
    route_entry = ledger.setdefault(
        acquisition_fingerprint, {"phaseIds": [], "signatures": {}}
    )
    if not isinstance(route_entry, dict):
        route_entry = {"phaseIds": [], "signatures": {}}
        ledger[acquisition_fingerprint] = route_entry
    phase_ids = route_entry.setdefault("phaseIds", [])
    if isinstance(phase_ids, list) and phase_id and phase_id not in phase_ids:
        phase_ids.append(phase_id)
    signatures = route_entry.setdefault("signatures", {})
    if not isinstance(signatures, dict):
        signatures = {}
        route_entry["signatures"] = signatures
    signature = spawn_acquisition_error_signature(exc)
    entry = signatures.setdefault(signature, {"count": 0})
    if not isinstance(entry, dict):
        entry = {"count": 0}
        signatures[signature] = entry
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["lastError"] = str(exc)[:1000]
    entry["updated_at"] = utc_now_iso()
    is_fleet_timeout = "-32012" in str(exc) and "fleet open timeout" in str(exc).lower()
    requires_cooldown = bool(
        getattr(exc, "requires_spawn_acquisition_cooldown", False)
        or is_fleet_timeout
    )
    if requires_cooldown and int(entry["count"]) < SPAWN_ACQUISITION_MAX_FAILURES:
        entry["retryAtEpoch"] = time.time() + SPAWN_ACQUISITION_FLEET_COOLDOWN_SECONDS
    route_entry["updated_at"] = utc_now_iso()
    write_task_state(logger, state)
    count = int(entry["count"])
    return {
        "status": (
            "spawn_infrastructure_exhausted"
            if count >= SPAWN_ACQUISITION_MAX_FAILURES
            else "failed"
        ),
        "phaseId": str(phase_id or ""),
        "acquisitionFingerprint": acquisition_fingerprint,
        "errorSignature": signature,
        "failures": count,
        "maxFailures": SPAWN_ACQUISITION_MAX_FAILURES,
        "retryAfterMs": (
            SPAWN_ACQUISITION_FLEET_COOLDOWN_SECONDS * 1000
            if requires_cooldown and count < SPAWN_ACQUISITION_MAX_FAILURES
            else 0
        ),
        # This receipt is produced after the startup path actually threw. The
        # next pre-check is the first non-executed rejection.
        "tool_was_executed": True,
        "next_instruction": (
            "Do not retry this unchanged startup route; change the fleet/slot/"
            "session routing or report the infrastructure blocker. The business"
            " objective attempt budget was not consumed."
            if count >= SPAWN_ACQUISITION_MAX_FAILURES
            else (
                "One Fleet startup failure was recorded. Wait 30000 ms, then retry"
                f" the SAME phase id {str(phase_id or '')!r}; do not rename it."
                if requires_cooldown
                else "One startup infrastructure failure was recorded; one bounded retry remains."
            )
        ),
    }


def clear_spawn_acquisition_failures(
    logger: RunLogger,
    *,
    acquisition_fingerprint: str,
) -> None:
    """A successfully started worker proves this acquisition route recovered."""

    state = load_task_state(logger)
    ledger = state.get("spawn_acquisition_failures")
    if not isinstance(ledger, dict) or acquisition_fingerprint not in ledger:
        return
    ledger.pop(acquisition_fingerprint, None)
    write_task_state(logger, state)


def _record_objective_attempt(
    state: JsonDict,
    phase: Optional[JsonDict],
    phase_id: str,
    *,
    succeeded: bool,
    worker_contract: Optional[JsonDict] = None,
) -> None:
    fingerprint = objective_fingerprint(phase, worker_contract)
    if not fingerprint:
        return
    attempts = state.setdefault("objective_attempts", {})
    if succeeded:
        attempts.pop(fingerprint, None)
        return
    entry = attempts.get(fingerprint)
    if not isinstance(entry, dict):
        entry = {"count": 0, "phaseIds": []}
        attempts[fingerprint] = entry
    entry["count"] = int(entry.get("count") or 0) + 1
    phase_ids = entry.setdefault("phaseIds", [])
    if phase_id not in phase_ids:
        phase_ids.append(phase_id)
    entry["updated_at"] = utc_now_iso()


def build_attempt_digest(
    worker_result: JsonDict,
    *,
    phase: Optional[JsonDict],
    worker_contract: Optional[JsonDict],
    task: str = "",
    result_contract: str = "",
) -> JsonDict:
    artifact_validation = (
        worker_result.get("artifactValidation")
        if isinstance(worker_result.get("artifactValidation"), dict)
        else {}
    )
    classification = _classification_from_worker_result(worker_result)
    row_count = _attempt_row_count(worker_result, artifact_validation)
    artifact_paths = _attempt_artifact_paths(worker_result, artifact_validation)
    trace_path = str(worker_result.get("tracePath") or "")
    status = str(worker_result.get("status") or "unknown")
    status_category = str(worker_result.get("statusCategory") or "unknown")
    validated_status = str(worker_result.get("validatedStatus") or "")
    digest: JsonDict = {
        "status": status,
        "statusCategory": status_category,
        "validatedStatus": validated_status,
        "classification": classification,
        "rowCount": row_count,
        "artifactPaths": artifact_paths,
        "tracePath": trace_path,
        "blocker": _attempt_primary_blocker(worker_result),
        "failureSignature": failure_signature_from_result(worker_result),
        "contractHash": contract_hash_for_phase(
            phase,
            worker_contract,
            task=task,
            result_contract=result_contract,
        ),
    }
    return trim_large_strings(_strip_volatile_handles(digest), 4000)


def failure_signature_from_result(worker_result: JsonDict) -> List[Any]:
    classification = _classification_from_worker_result(worker_result)
    artifact_validation = (
        worker_result.get("artifactValidation")
        if isinstance(worker_result.get("artifactValidation"), dict)
        else {}
    )
    status = str(worker_result.get("status") or "")
    category = str(classification.get("category") or "").strip() if classification else ""
    if not category and status and status not in {"done", "partial"}:
        category = f"status:{status}"
    validation_failure_type = _primary_validation_failure_type(
        artifact_validation,
        classification,
    )
    hint_key = _classification_hint_key(classification)
    primary_blocker_method = (
        str(classification.get("method") or "").strip()
        if isinstance(classification, dict)
        else ""
    )
    progress_reason = _progress_intervention_reason(worker_result)
    return [
        category or None,
        validation_failure_type or None,
        hint_key or None,
        primary_blocker_method or None,
        progress_reason or None,
    ]


def repeated_phase_attempt_guard(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    contract_hash: str,
) -> Optional[JsonDict]:
    if not phase_id or not contract_hash:
        return None
    state = load_task_state(logger)
    phase_state = _phase_state(state, str(phase_id))
    if phase_state is None:
        return None
    block = should_block_repeated_phase_attempt(
        phase_state,
        contract_hash=contract_hash,
    )
    if block is None:
        if phase_state.pop("repeat_guard", None) is not None:
            write_task_state(logger, state)
        return None

    guard_key = json.dumps(
        {
            "contractHash": contract_hash,
            "signature": block.get("lockedSignature"),
        },
        sort_keys=True,
        default=str,
    )
    previous = phase_state.get("repeat_guard")
    previous_key = previous.get("key") if isinstance(previous, dict) else ""
    rejection_count = (
        int(previous.get("rejectionCount") or 0) + 1
        if isinstance(previous, dict) and previous_key == guard_key
        else 1
    )
    phase_state["repeat_guard"] = {
        "key": guard_key,
        "rejectionCount": rejection_count,
        "lockedSignature": block.get("lockedSignature"),
        "contractHash": contract_hash,
        "updated_at": utc_now_iso(),
    }
    write_task_state(logger, state)

    status = (
        "phase_locked_must_finalize"
        if rejection_count >= REPEAT_GUARD_REJECTION_LOCK_THRESHOLD
        else "phase_classification_repeated"
    )
    result = {
        "status": status,
        "phaseId": str(phase_id),
        "lockedSignature": block.get("lockedSignature"),
        "contractHash": contract_hash,
        "consecutiveSameSignatureCount": block.get("consecutiveSameSignatureCount"),
        "repeatSpawnRejectionCount": rejection_count,
        "recentDigests": block.get("recentDigests"),
        "tool_was_executed": False,
        "next_instruction": (
            "Emit a revised task_plan with replan_reason that changes objective,"
            " worker_task, worker_contract, expected_artifact, validators, or"
            " task_type; or call final_answer with the blocker."
            if status == "phase_classification_repeated" else
            "This phase has repeatedly hit the same failure signature under the"
            " same contract. Do not spawn another worker for this phase; call"
            " final_answer with the blocker or emit a substantially revised"
            " task_plan before continuing."
        ),
    }
    return _strip_volatile_handles(result)


def should_block_repeated_phase_attempt(
    phase_state: JsonDict,
    *,
    contract_hash: str,
) -> Optional[JsonDict]:
    attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
    if not isinstance(attempts, list):
        return None
    digests = [
        item.get("attemptDigest")
        for item in attempts
        if isinstance(item, dict) and isinstance(item.get("attemptDigest"), dict)
    ]
    if len(digests) < 2:
        return None
    recent = digests[-2:]
    if not all(_attempt_digest_is_failure(item) for item in recent):
        return None
    if not all(str(item.get("contractHash") or "") == contract_hash for item in recent):
        return None
    signatures = [item.get("failureSignature") for item in recent]
    if not all(isinstance(signature, list) and any(signature) for signature in signatures):
        return None
    if signatures[0] != signatures[1]:
        return None
    consecutive_count = 0
    for digest in reversed(digests):
        if (
            _attempt_digest_is_failure(digest)
            and str(digest.get("contractHash") or "") == contract_hash
            and digest.get("failureSignature") == signatures[0]
        ):
            consecutive_count += 1
            continue
        break
    return {
        "lockedSignature": signatures[0],
        "consecutiveSameSignatureCount": consecutive_count,
        "recentDigests": [_strip_volatile_handles(item) for item in recent],
    }


def _classification_from_worker_result(worker_result: JsonDict) -> JsonDict:
    artifact_validation = (
        worker_result.get("artifactValidation")
        if isinstance(worker_result.get("artifactValidation"), dict)
        else {}
    )
    classification = artifact_validation.get("classification")
    if isinstance(classification, dict):
        return dict(classification)
    result_levels = (
        worker_result.get("resultLevels")
        if isinstance(worker_result.get("resultLevels"), dict)
        else {}
    )
    l1 = result_levels.get("l1") if isinstance(result_levels.get("l1"), dict) else {}
    failure = l1.get("failureClassification")
    if isinstance(failure, dict):
        return dict(failure)
    if isinstance(failure, str) and failure.strip():
        return {"category": failure.strip(), "source": "resultLevels.l1"}
    return {}


def _attempt_row_count(worker_result: JsonDict, artifact_validation: JsonDict) -> int:
    try:
        return int(artifact_validation.get("rowCount") or 0)
    except (TypeError, ValueError):
        pass
    result_levels = (
        worker_result.get("resultLevels")
        if isinstance(worker_result.get("resultLevels"), dict)
        else {}
    )
    l2 = result_levels.get("l2") if isinstance(result_levels.get("l2"), dict) else {}
    data = l2.get("data") if isinstance(l2.get("data"), dict) else {}
    try:
        return int(data.get("totalExtractedRows") or 0)
    except (TypeError, ValueError):
        return 0


def _attempt_artifact_paths(
    worker_result: JsonDict,
    artifact_validation: JsonDict,
) -> List[str]:
    paths: List[str] = []
    for raw_list in (
        worker_result.get("artifacts"),
        artifact_validation.get("artifacts"),
        artifact_validation.get("allExtractionArtifacts"),
    ):
        if not isinstance(raw_list, list):
            continue
        for item in raw_list:
            path = str(item or "").strip()
            if path and path not in paths:
                paths.append(path)
    return paths[:20]


def _attempt_primary_blocker(worker_result: JsonDict) -> Optional[JsonDict]:
    result_levels = (
        worker_result.get("resultLevels")
        if isinstance(worker_result.get("resultLevels"), dict)
        else {}
    )
    l2 = result_levels.get("l2") if isinstance(result_levels.get("l2"), dict) else {}
    blockers = l2.get("blockers") if isinstance(l2.get("blockers"), list) else []
    for blocker in blockers:
        if isinstance(blocker, dict):
            return trim_large_strings(_strip_volatile_handles(blocker), 1000)
    artifact_validation = worker_result.get("artifactValidation")
    if isinstance(artifact_validation, dict) and artifact_validation.get("status") == "failed":
        return {
            "type": "artifact_validation_failed",
            "classification": _classification_from_worker_result(worker_result),
        }
    return None


def _primary_validation_failure_type(
    artifact_validation: JsonDict,
    classification: JsonDict,
) -> str:
    failure_types = classification.get("failureTypes") if isinstance(classification, dict) else None
    if isinstance(failure_types, list):
        values = sorted(str(item) for item in failure_types if str(item).strip())
        if values:
            return values[0]
    failures = artifact_validation.get("failures")
    if isinstance(failures, list):
        for failure in failures:
            if isinstance(failure, dict) and str(failure.get("type") or "").strip():
                return str(failure.get("type")).strip()
    return ""


def _classification_hint_key(classification: JsonDict) -> str:
    if not isinstance(classification, dict):
        return ""
    for key in (
        "expectedArtifactName",
        "workerStatus",
        "source",
        "task_type",
    ):
        value = str(classification.get(key) or "").strip()
        if value:
            return f"{key}={value[:120]}"
    return ""


def _progress_intervention_reason(worker_result: JsonDict) -> str:
    trace_summary = (
        worker_result.get("traceSummary")
        if isinstance(worker_result.get("traceSummary"), dict)
        else {}
    )
    interventions = trace_summary.get("progressInterventions")
    if isinstance(interventions, list):
        for item in reversed(interventions):
            if isinstance(item, dict):
                reason = str(item.get("reason") or "").strip()
                if reason:
                    return reason
    loop_nudges = trace_summary.get("loopNudges")
    if isinstance(loop_nudges, list):
        for item in reversed(loop_nudges):
            if isinstance(item, dict):
                reason = str(item.get("reason") or "").strip()
                action = str(item.get("action") or "").strip()
                if reason and action:
                    return f"loop_nudge:{action}:{reason}"
                if reason:
                    return f"loop_nudge:{reason}"
    return ""


def _attempt_digest_is_failure(digest: JsonDict) -> bool:
    if not isinstance(digest, dict):
        return False
    status = str(digest.get("status") or "")
    if status == "partial":
        return False
    validated_status = str(digest.get("validatedStatus") or "")
    if validated_status == "validation_failed":
        return True
    status_category = str(digest.get("statusCategory") or "")
    return status_category in {"recoverable", "fatal"}


def _strip_volatile_handles(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: JsonDict = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in VOLATILE_HANDLE_KEYS:
                continue
            cleaned[key_text] = _strip_volatile_handles(item)
        return cleaned
    if isinstance(value, list):
        return [
            _strip_volatile_handles(item)
            for item in value
            if not _is_volatile_string(item)
        ]
    if _is_volatile_string(value):
        return None
    return value


def _is_volatile_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if AXTREE_ID_ANYWHERE_RE.search(text):
        return True
    lowered = text.lower()
    return lowered.startswith("pageid=") or lowered.startswith("fleetid=")


def mark_phase_running(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
    worker_name: str,
) -> None:
    if not phase_id:
        return
    state = load_task_state(logger)
    phase_state = _phase_state(state, phase_id)
    if phase_state is None:
        return
    phase_state["status"] = "running"
    phase_state.setdefault("attempts", []).append({
        "workerId": worker_id,
        "name": worker_name,
        "started_at": utc_now_iso(),
        "status": "running",
    })
    state["current_phase"] = phase_id
    write_task_state(logger, state)


def cancel_phase_running_reservation(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
) -> None:
    if not phase_id:
        return
    state = load_task_state(logger)
    phase_state = _phase_state(state, phase_id)
    if phase_state is None:
        return
    attempts = phase_state.get("attempts")
    if isinstance(attempts, list):
        phase_state["attempts"] = [
            item for item in attempts
            if not (
                isinstance(item, dict)
                and str(item.get("workerId") or "") == str(worker_id)
                and str(item.get("status") or "") == "running"
            )
        ]
    if str(phase_state.get("status") or "") == "running":
        phase_state["status"] = "pending"
    write_task_state(logger, state)


def mark_phase_result(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_id: str,
    validation: Optional[JsonDict],
    result_status: str,
    attempt_digest: Optional[JsonDict] = None,
    phase: Optional[JsonDict] = None,
    worker_contract: Optional[JsonDict] = None,
) -> None:
    if not phase_id:
        return
    state = load_task_state(logger)
    phase_state = _phase_state(state, phase_id)
    if phase_state is None:
        return

    attempts = phase_state.setdefault("attempts", [])
    attempt = None
    for item in reversed(attempts):
        if item.get("workerId") == worker_id:
            attempt = item
            break
    if attempt is None:
        attempt = {"workerId": worker_id}
        attempts.append(attempt)
    attempt["finished_at"] = utc_now_iso()
    attempt["status"] = result_status
    if validation:
        attempt["validation"] = trim_large_strings(validation, 2000)
    if attempt_digest:
        attempt["attemptDigest"] = trim_large_strings(
            _strip_volatile_handles(attempt_digest),
            4000,
        )

    if result_status in RECOVERABLE_ROUTING_PHASE_STATUSES:
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "recoverable_routing_failure",
            "status": result_status,
            "message": (
                "The worker's unnamed fleet assignment was lost. Retry this"
                " phase with a fresh coordinator assignment; retries remain"
                " bounded by the phase max_attempts budget."
            ),
        }]
        phase_state["last_failure_classification"] = (
            validation.get("classification")
            if isinstance(validation, dict)
            and isinstance(validation.get("classification"), dict)
            else None
        )
        # This is an orchestration/inventory failure, not evidence that the
        # objective itself is infeasible. Preserve the reason and phase retry
        # budget, but do not consume the cross-replan objective budget.
        write_task_state(logger, state)
        return

    if result_status == "cancelled":
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "worker_cancelled",
            "status": result_status,
            "message": (
                "The worker was cancelled. A retry remains bounded by the"
                " phase max_attempts budget, but cancellation is not evidence"
                " that the objective itself is infeasible."
            ),
        }]
        phase_state["last_failure_classification"] = (
            validation.get("classification")
            if isinstance(validation, dict)
            and isinstance(validation.get("classification"), dict)
            else None
        )
        write_task_state(logger, state)
        return

    if result_status in {
        "blocked_by_challenge",
        "hitl_required",
        "hitl_timeout",
        "page_settled_after_hitl",
        "stale_pause_deadlock",
        "session_fleet_lost",
    }:
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "challenge_blocker",
            "status": result_status,
            "message": (
                "Worker reported a challenge/HITL/session blocker; do not"
                " retry the same browser/session binding without user action"
                " or a deliberate auth recovery pivot."
            ),
        }]
        write_task_state(logger, state)
        return

    if validation and validation.get("status") == "done":
        phase_state["status"] = "validated_done"
        artifacts = validation.get("artifacts") or []
        validated_artifacts = list(phase_state.get("validated_artifacts") or [])
        _append_unique(validated_artifacts, artifacts)
        phase_state["validated_artifacts"] = validated_artifacts
        phase_state["last_failure"] = None
        phase_state["last_failure_classification"] = None
        _append_unique(state.setdefault("artifacts", []), artifacts)
        _record_objective_attempt(
            state, phase, str(phase_id),
            succeeded=True, worker_contract=worker_contract,
        )
    else:
        classification = (
            validation.get("classification")
            if isinstance(validation, dict)
            and isinstance(validation.get("classification"), dict)
            else {}
        )
        semantic_category = str(classification.get("category") or "").strip()
        if semantic_category in SEMANTIC_TERMINAL_CLASSIFICATIONS:
            phase_state["status"] = semantic_category
            phase_state[f"{semantic_category}_at"] = utc_now_iso()
            phase_state["last_failure_classification"] = classification
            phase_state["last_failure"] = [{
                "type": semantic_category,
                "classification": classification,
                "message": (
                    classification.get("hint")
                    or f"Worker classified the phase as {semantic_category}."
                ),
            }]
            write_task_state(logger, state)
            return
        phase_state["status"] = "validation_failed" if validation else result_status
        phase_state["last_failure"] = (
            validation.get("failures") if isinstance(validation, dict) else None
        )
        phase_state["last_failure_classification"] = (
            validation.get("classification") if isinstance(validation, dict) else None
        )
        # Objective-level failure accounting survives replans (per-phase
        # attempts do not: a fresh phase id resets them). Challenge/HITL and
        # semantic-terminal outcomes returned earlier and are deliberately
        # not counted — they are not evidence the objective is unreachable.
        _record_objective_attempt(
            state, phase, str(phase_id),
            succeeded=False, worker_contract=worker_contract,
        )

    write_task_state(logger, state)


def phase_prior_artifact_paths(
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    exclude_worker_id: Optional[str] = None,
) -> List[str]:
    if not phase_id:
        return []
    state = load_task_state(logger)
    phase_state = _phase_state(state, str(phase_id))
    if phase_state is None:
        return []
    attempts = phase_state.get("attempts")
    if not isinstance(attempts, list):
        return []
    paths: List[Any] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        if exclude_worker_id and str(attempt.get("workerId") or "") == str(exclude_worker_id):
            continue
        validation = attempt.get("validation")
        if isinstance(validation, dict):
            for key in (
                "artifacts",
                "allExtractionArtifacts",
                "validExtractionArtifacts",
                "attemptExtractionArtifacts",
                "priorExtractionArtifacts",
            ):
                value = validation.get(key)
                if isinstance(value, list):
                    paths.extend(value)
        digest = attempt.get("attemptDigest")
        if isinstance(digest, dict) and isinstance(digest.get("artifactPaths"), list):
            paths.extend(digest.get("artifactPaths") or [])
    return [
        path for path in _unique_paths(paths)
        if "/artifacts/extractions/" in str(path)
    ]


def _normalized_depends_on(raw: Any) -> Optional[List[str]]:
    """Plan-normalization twin of _phase_dependency_ids: keep None (omitted →
    implicit serial) distinct from [] (explicitly independent); coerce a bare
    string to a one-element list; anything malformed degrades to None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return None


def _phase_dependency_ids(phase: JsonDict) -> Optional[List[str]]:
    """None ⇒ depends_on OMITTED (conservative implicit: all prior phases in
    plan order). [] ⇒ EXPLICITLY independent — startable immediately, in
    parallel with anything. Task 2ed5a466: the old falsy check collapsed [] into
    the implicit-serial default, leaving the planner no syntax at all to declare
    independence, and three logically-parallel detail phases ran serially."""
    raw = phase.get("depends_on")
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        values: List[Any] = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        return None  # malformed → conservative implicit ordering
    return [
        str(item).strip()
        for item in values
        if str(item).strip()
    ]


def phase_pacing_remaining_seconds(
    plan: Optional[JsonDict],
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_contract: Optional[JsonDict] = None,
    now: Optional[datetime] = None,
    random_value: Optional[float] = None,
) -> float:
    """Remaining dependency-to-start delay for a phase.

    Independent phases (depends_on=[]) have no dependency completion anchor and
    therefore never wait.  The caller invokes this only after the dependency
    gate reports ready and before reserving a BrowserAgent slot.
    """
    if not isinstance(plan, dict) or not phase_id:
        return 0.0
    plan_phases = [item for item in plan.get("phases", []) if isinstance(item, dict)]
    prior_ids: List[str] = []
    target: Optional[JsonDict] = None
    for phase in plan_phases:
        current_id = str(phase.get("id") or "")
        if current_id == str(phase_id):
            target = phase
            break
        prior_ids.append(current_id)
    if target is None:
        return 0.0
    dependencies = _phase_dependency_ids(target)
    if dependencies == []:
        return 0.0
    if dependencies is None:
        dependencies = prior_ids
    if not dependencies:
        return 0.0

    pacing = merge_pacing(
        plan.get("pacing"),
        target.get("pacing"),
        worker_contract.get("pacing") if isinstance(worker_contract, dict) else None,
    )
    interval = jittered_interval(
        pacing["phase_interval_seconds"],
        pacing["jitter_ratio"],
        random_value=random_value,
    )
    if interval <= 0.0:
        return 0.0

    state = load_task_state(logger)
    states = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    completed_at: List[datetime] = []
    for dependency_id in dependencies:
        dependency_state = states.get(dependency_id)
        if not isinstance(dependency_state, dict):
            return 0.0
        attempts = dependency_state.get("attempts")
        timestamps = [
            parse_utc_timestamp(item.get("finished_at"))
            for item in (attempts if isinstance(attempts, list) else [])
            if isinstance(item, dict) and _attempt_was_validated_done(item)
        ]
        timestamps = [item for item in timestamps if item is not None]
        if not timestamps:
            return 0.0
        completed_at.append(max(timestamps))
    anchor = max(completed_at)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (current.astimezone(timezone.utc) - anchor).total_seconds())
    return max(0.0, interval - elapsed)


def _attempt_was_validated_done(attempt: JsonDict) -> bool:
    """True only for the attempt that actually established dependency readiness."""
    if str(attempt.get("status") or "") == "validated_done":
        return True
    if str(attempt.get("validatedStatus") or "") == "validated_done":
        return True
    validation = attempt.get("validation")
    return bool(
        isinstance(validation, dict)
        and str(validation.get("status") or "") == "done"
    )


def _dependency_blocker(
    phase: JsonDict,
    phases: JsonDict,
    implicit_prior_phase_ids: List[str],
) -> Optional[JsonDict]:
    dependency_ids = _phase_dependency_ids(phase)
    if dependency_ids is None:
        dependency_ids = implicit_prior_phase_ids
    for dep_id in dependency_ids:
        dep_state = phases.get(dep_id)
        dep_status = (
            str(dep_state.get("status") or "")
            if isinstance(dep_state, dict)
            else ""
        )
        if dep_status == "validated_done":
            continue
        if dep_status in BLOCKING_DEPENDENCY_STATUSES:
            return {
                "type": "dependency_failed",
                "dependencyPhaseId": dep_id,
                "dependencyStatus": dep_status,
                "blocking": True,
                "message": (
                    f"Dependency phase {dep_id} ended with status"
                    f" {dep_status}; this phase cannot run without a revised"
                    " plan or replacement input artifact."
                ),
            }
        return {
            "type": "dependency_not_ready",
            "dependencyPhaseId": dep_id,
            "dependencyStatus": dep_status or "pending",
            "blocking": False,
            "message": (
                f"Dependency phase {dep_id} is not validated yet; wait for it"
                " before spawning this phase."
            ),
        }
    return None


def _mark_phase_blocked_by_dependency(
    phases: JsonDict,
    phase_id: str,
    blocker: JsonDict,
) -> None:
    phase_state = phases.get(phase_id)
    if not isinstance(phase_state, dict):
        return
    phase_state["status"] = "blocked_by_dependency"
    phase_state["last_failure"] = [blocker]
    phase_state["last_failure_classification"] = {
        "category": "blocked_by_dependency",
        "dependencyPhaseId": blocker.get("dependencyPhaseId"),
        "dependencyStatus": blocker.get("dependencyStatus"),
        "hint": blocker.get("message"),
    }


def phase_start_rejection(
    plan: Optional[JsonDict],
    logger: RunLogger,
    *,
    phase_id: Optional[str],
    worker_contract: Optional[JsonDict] = None,
) -> Optional[JsonDict]:
    if not plan or not phase_id:
        return None
    state = load_task_state(logger)
    phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    plan_phases = plan.get("phases") if isinstance(plan.get("phases"), list) else []
    prior_ids: List[str] = []
    target_phase: Optional[JsonDict] = None
    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        current_id = str(phase.get("id") or "")
        if current_id == str(phase_id):
            target_phase = phase
            break
        prior_ids.append(current_id)
    if target_phase is None:
        return None
    phase_state = phases.get(str(phase_id))
    status = (
        str(phase_state.get("status") or "")
        if isinstance(phase_state, dict)
        else ""
    )
    if status in TERMINAL_PHASE_STATUSES:
        return {
            "status": "phase_not_startable",
            "phaseId": str(phase_id),
            "phaseStatus": status,
            "tool_was_executed": False,
            "next_instruction": (
                "Do not spawn this phase. Emit a revised task_plan with a new"
                " phase id/objective or final_answer with the blocker."
            ),
        }
    if status == "running":
        return {
            "status": "phase_already_running",
            "phaseId": str(phase_id),
            "tool_was_executed": False,
            "next_instruction": (
                "A worker is already running for this phase. Wait for it instead"
                " of spawning another copy."
            ),
        }
    attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
    attempts_count = len(attempts) if isinstance(attempts, list) else 0
    max_attempts = _positive_int(target_phase.get("max_attempts"), default=3)
    if (
        attempts_count >= max_attempts
        and status in RETRYABLE_PHASE_FAILURE_STATUSES
    ):
        return {
            "status": "phase_exhausted",
            "phaseId": str(phase_id),
            "attempts": attempts_count,
            "max_attempts": max_attempts,
            "tool_was_executed": False,
            "next_instruction": (
                "This phase has reached max_attempts. Replan with a changed"
                " contract/objective or stop with final_answer."
            ),
        }
    fingerprint = objective_fingerprint(target_phase, worker_contract)
    if fingerprint:
        objective_attempts = (
            state.get("objective_attempts")
            if isinstance(state.get("objective_attempts"), dict)
            else {}
        )
        entry = objective_attempts.get(fingerprint)
        objective_count = (
            int(entry.get("count") or 0) if isinstance(entry, dict) else 0
        )
        if objective_count >= OBJECTIVE_MAX_ATTEMPTS:
            return {
                "status": "objective_exhausted",
                "phaseId": str(phase_id),
                "objectiveFingerprint": fingerprint,
                "objectiveAttempts": objective_count,
                "objectiveMaxAttempts": OBJECTIVE_MAX_ATTEMPTS,
                "priorPhaseIds": (
                    list(entry.get("phaseIds") or [])
                    if isinstance(entry, dict) else []
                ),
                "tool_was_executed": False,
                "next_instruction": (
                    "This OBJECTIVE (same target range/row count/artifact,"
                    " regardless of phase id) has already failed"
                    f" {objective_count} times across replans. Re-issuing it"
                    " under a fresh phase id is not allowed. Either genuinely"
                    " change the target (different source URL, different"
                    " range, different artifact), or final_answer reporting"
                    " target_absent/instruction_infeasible with the collected"
                    " evidence so the user can revise the instruction."
                ),
            }
    blocker = _dependency_blocker(target_phase, phases, prior_ids)
    if blocker is not None:
        if blocker.get("blocking"):
            _mark_phase_blocked_by_dependency(phases, str(phase_id), blocker)
            state["current_phase"] = _first_active_phase_id(plan, phases)
            write_task_state(logger, state)
            logger.write("task_phase.blocked_by_dependency", {
                "phaseId": str(phase_id),
                **blocker,
            })
        # Two very different situations share this branch: a dependency that
        # FAILED terminally (replan territory) vs one that simply has not
        # finished yet (wait territory). Now that the rejection payload
        # reaches the Lead verbatim, the wrong instruction would upgrade a
        # blind retry into a wrong replan — say the right thing per case.
        if blocker.get("blocking"):
            next_instruction = (
                "Do not spawn this phase: its dependency ended in a terminal"
                " failure and will not recover on its own. Emit a revised"
                " task_plan that fixes or replaces the dependency phase —"
                " a replan resets blocked_by_dependency and re-derives it"
                " from the new plan — or final_answer with the blocker."
            )
        else:
            next_instruction = (
                f"Dependency phase {blocker.get('dependencyPhaseId')} is"
                f" {blocker.get('dependencyStatus') or 'pending'}, not failed."
                " Do NOT replan and do not re-spawn in a loop: wait for it"
                " (wait_browser_agents if it is running), then spawn this"
                " phase once the dependency is validated_done."
            )
        return {
            "status": (
                "blocked_by_dependency"
                if blocker.get("blocking")
                else "dependency_not_ready"
            ),
            "phaseId": str(phase_id),
            "tool_was_executed": False,
            **blocker,
            "next_instruction": next_instruction,
        }
    return None


def mark_phase_exhausted_if_needed(
    plan: Optional[JsonDict],
    logger: RunLogger,
) -> List[JsonDict]:
    if not plan:
        return []
    state = load_task_state(logger)
    raw_phases_state = state.get("phases")
    phases: JsonDict = raw_phases_state if isinstance(raw_phases_state, dict) else {}
    raw_plan_phases = plan.get("phases")
    plan_phases = raw_plan_phases if isinstance(raw_plan_phases, list) else []
    exhausted: List[JsonDict] = []
    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        phase_state = phases.get(phase_id)
        if not isinstance(phase_state, dict):
            continue
        status = str(phase_state.get("status") or "")
        if status in TERMINAL_PHASE_STATUSES:
            continue
        attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
        attempts_count = len(attempts) if isinstance(attempts, list) else 0
        max_attempts = _positive_int(phase.get("max_attempts"), default=3)
        if (
            attempts_count < max_attempts
            or status not in RETRYABLE_PHASE_FAILURE_STATUSES
        ):
            continue
        classification = phase_state.get("last_failure_classification")
        if not isinstance(classification, dict) and isinstance(attempts, list) and attempts:
            validation = attempts[-1].get("validation")
            if isinstance(validation, dict):
                classification = validation.get("classification")
        payload = {
            "phaseId": phase_id,
            "status": "phase_failed",
            "previousStatus": status,
            "attempts": attempts_count,
            "max_attempts": max_attempts,
            "last_failure": phase_state.get("last_failure"),
            "classification": classification if isinstance(classification, dict) else None,
        }
        phase_state["status"] = "phase_failed"
        phase_state["exhausted_at"] = utc_now_iso()
        phase_state["max_attempts"] = max_attempts
        exhausted.append(payload)

    if exhausted:
        state["current_phase"] = _first_active_phase_id(plan, phases)
        write_task_state(logger, state)
        for payload in exhausted:
            logger.write("task_phase.exhausted", payload)
    return exhausted


def next_pending_phase(plan: Optional[JsonDict], logger: RunLogger) -> Optional[JsonDict]:
    if not plan:
        return None
    state = load_task_state(logger)
    raw_phases_state = state.get("phases")
    phases: JsonDict = raw_phases_state if isinstance(raw_phases_state, dict) else {}
    raw_plan_phases = plan.get("phases")
    plan_phases = raw_plan_phases if isinstance(raw_plan_phases, list) else []
    prior_ids: List[str] = []
    state_changed = False
    for phase in plan_phases:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        raw_phase_state = phases.get(phase_id)
        phase_state: JsonDict = raw_phase_state if isinstance(raw_phase_state, dict) else {}
        status = phase_state.get("status")
        if status in TERMINAL_PHASE_STATUSES:
            prior_ids.append(phase_id)
            continue
        if status == "running":
            prior_ids.append(phase_id)
            continue
        blocker = _dependency_blocker(phase, phases, prior_ids)
        if blocker is not None:
            if blocker.get("blocking"):
                _mark_phase_blocked_by_dependency(phases, phase_id, blocker)
                logger.write("task_phase.blocked_by_dependency", {
                    "phaseId": phase_id,
                    **blocker,
                })
                state_changed = True
            prior_ids.append(phase_id)
            continue
        attempts = phase_state.get("attempts") if isinstance(phase_state, dict) else []
        attempts_count = len(attempts) if isinstance(attempts, list) else 0
        max_attempts = _positive_int(phase.get("max_attempts"), default=3)
        if (
            attempts_count >= max_attempts
            and status in RETRYABLE_PHASE_FAILURE_STATUSES
        ):
            prior_ids.append(phase_id)
            continue
        if status not in {"validated_done"}:
            if state_changed:
                state["current_phase"] = phase_id
                write_task_state(logger, state)
            return phase
        prior_ids.append(phase_id)
    if state_changed:
        state["current_phase"] = _first_active_phase_id(plan, phases)
        write_task_state(logger, state)
    return None


def find_phase(plan: Optional[JsonDict], phase_id: Optional[str]) -> Optional[JsonDict]:
    if not plan or not phase_id:
        return None
    for phase in plan.get("phases", []):
        if isinstance(phase, dict) and str(phase.get("id")) == str(phase_id):
            return phase
    return None


def validate_worker_artifacts(
    *,
    contract: Optional[JsonDict],
    artifacts: List[str],
    task_dir: Path,
    attempt_artifacts: Optional[List[str]] = None,
    prior_artifacts: Optional[List[str]] = None,
    file_evidence: Optional[List[JsonDict]] = None,
    evidence_sink: Optional[Any] = None,
) -> JsonDict:
    """Validate a worker's artifacts against its contract.

    ``evidence_sink`` receives one structured payload describing the rows this
    call already loaded and merged, including why they may not be trustworthy.
    It exists so the shadow evaluator can reuse that work instead of re-reading
    every artifact, and it deliberately does not appear in the returned result:
    the rows can be large and that result is logged and partly surfaced to the
    model.
    """
    if not contract:
        return {"status": "skipped", "reason": "no worker_contract"}

    expected = contract.get("expected_artifact") if isinstance(contract, dict) else {}
    if not isinstance(expected, dict):
        expected = {}
    validators = contract.get("validators") if isinstance(contract, dict) else []
    if not isinstance(validators, list):
        validators = []
    if bool(contract.get("validators_normalized", False)):
        validators = [
            dict(validator) for validator in validators
            if isinstance(validator, dict)
        ]
    else:
        validators = _normalize_validators(
            expected,
            validators,
            [],
            phase_id=str(contract.get("phase_id") or "worker"),
        )

    row_validators = [
        validator for validator in validators
        if str(validator.get("type") or "") not in FILE_VALIDATOR_TYPES
    ]
    file_validators = [
        validator for validator in validators
        if str(validator.get("type") or "") in FILE_VALIDATOR_TYPES
    ]
    extraction_artifacts = [
        path for path in artifacts
        if "/artifacts/extractions/" in str(path)
    ]
    extraction_attempt_artifacts = [
        path for path in (attempt_artifacts or [])
        if "/artifacts/extractions/" in str(path)
    ]
    prior_extraction_artifacts = [
        path for path in (prior_artifacts or [])
        if "/artifacts/extractions/" in str(path)
    ]
    all_extraction_artifacts = _unique_paths([
        *extraction_artifacts,
        *extraction_attempt_artifacts,
        *prior_extraction_artifacts,
    ])
    failures: List[JsonDict] = []
    loaded = _load_extraction_artifacts(extraction_artifacts, task_dir)
    loaded_attempts = _load_extraction_artifacts(
        [
            path for path in extraction_attempt_artifacts
            if path not in extraction_artifacts
        ],
        task_dir,
    )
    loaded_prior = _load_extraction_artifacts(
        [
            path for path in prior_extraction_artifacts
            if path not in extraction_artifacts
            and path not in extraction_attempt_artifacts
        ],
        task_dir,
    )
    expected_name = str(expected.get("name") or "").strip()
    candidates = [
        item for item in loaded
        if not expected_name or item.get("payload", {}).get("name") == expected_name
    ]
    attempt_candidates = [
        item for item in loaded_attempts
        if not expected_name or item.get("payload", {}).get("name") == expected_name
    ]
    prior_candidates = [
        item for item in loaded_prior
        if not expected_name or item.get("payload", {}).get("name") == expected_name
    ]

    has_row_contract = bool(
        expected.get("name")
        or expected.get("fields")
        or expected.get("required_fields")
        or row_validators
    )
    must_record = bool(contract.get("must_record_extraction", has_row_contract))
    if must_record and not candidates and not attempt_candidates and not prior_candidates:
        failures.append({
            "type": "artifact_required",
            "message": (
                f"expected record_extraction artifact"
                + (f" named {expected_name!r}" if expected_name else "")
            ),
            "availableArtifacts": all_extraction_artifacts,
        })

    # Same-name artifact selection (fa86c5f6 fix): within the first non-empty
    # tier, order candidates best-first (no schemaWarnings > more rows >
    # recorded later) and pick the FIRST one that passes every validator; if
    # none passes, keep the heuristic-best and report ITS failures. The old
    # first-recorded pick validated a schema-flagged batch dump into a bogus
    # validation_failed while a clean complete artifact sat right next to it.
    def _order_best_first(items: List[JsonDict]) -> List[JsonDict]:
        def sort_key(pair):
            idx, item = pair
            payload = item.get("payload") or {}
            schema_warnings = payload.get("schemaWarnings")
            has_warnings = 1 if isinstance(schema_warnings, list) and schema_warnings else 0
            rows_list = payload.get("rows")
            n_rows = len(rows_list) if isinstance(rows_list, list) else 0
            return (has_warnings, -n_rows, -idx)
        return [item for _, item in sorted(enumerate(items), key=sort_key)]

    def _evaluate_candidate(
        item: Optional[JsonDict],
    ) -> Tuple[List[JsonDict], List[JsonDict]]:
        cand_failures: List[JsonDict] = []
        cand_rows: List[JsonDict] = []
        if item:
            payload = item.get("payload") or {}
            schema_warnings = payload.get("schemaWarnings")
            if isinstance(schema_warnings, list) and schema_warnings:
                cand_failures.append({
                    "type": "schema",
                    "message": "selected record_extraction artifact has schemaWarnings",
                    "path": item.get("path"),
                    "schemaWarnings": schema_warnings[:5],
                })
            raw_rows = payload.get("rows")
            if isinstance(raw_rows, list):
                cand_rows = [row for row in raw_rows if isinstance(row, dict)]
            else:
                cand_failures.append({
                    "type": "schema",
                    "message": "selected artifact has no rows array",
                    "path": item.get("path"),
                })
        for validator in row_validators:
            cand_failures.extend(_run_validator(validator, cand_rows))
        cand_failures.extend(detect_placeholder_rows(cand_rows))
        cand_failures.extend(detect_blocker_data_rows(cand_rows, expected))
        cand_failures.extend(detect_stub_rows(cand_rows, expected))
        return cand_failures, cand_rows

    if expected_name:
        tier = candidates or attempt_candidates or prior_candidates
    else:
        tier = loaded or loaded_attempts or loaded_prior
    ordered = _order_best_first(tier)
    selected = ordered[0] if ordered else None
    selected_failures, rows = _evaluate_candidate(selected)
    if selected_failures:
        for item in ordered[1:]:
            alt_failures, alt_rows = _evaluate_candidate(item)
            if not alt_failures:
                selected, selected_failures, rows = item, alt_failures, alt_rows
                break
    failures.extend(selected_failures)
    warnings = detect_near_stub_rows(rows, expected)

    cumulative = False
    cumulative_sources: List[str] = []
    merged_rows: List[JsonDict] = []
    merged_sources: List[str] = []
    merged_provenance: List[JsonDict] = []
    observer_merge_error = ""
    authoritative_merge_attempted = False
    if failures:
        # This is the pre-existing authoritative recovery path. Keep its error
        # semantics unchanged: a broken cumulative validator is a validation
        # failure, not an observer failure.
        authoritative_merge_attempted = True
        cumulative_rows, cumulative_sources, cumulative_failures, merged_provenance = (
            _validate_cumulative_artifacts(
                validators=row_validators,
                expected=expected,
                candidates=[
                    *prior_candidates,
                    *attempt_candidates,
                    *candidates,
                ],
            )
        )
        merged_rows, merged_sources = cumulative_rows, cumulative_sources
        if failures and cumulative_rows and not cumulative_failures:
            rows = cumulative_rows
            failures = []
            warnings = detect_near_stub_rows(rows, expected)
            cumulative = True

    file_failures: List[JsonDict] = []
    for validator in file_validators:
        file_failures.extend(_run_file_validator(
            validator,
            artifacts=artifacts,
            evidence=file_evidence or [],
            rows=rows,
        ))
    failures.extend(file_failures)

    if (
        evidence_sink is not None
        and failures
        and not authoritative_merge_attempted
    ):
        # Row validation passed, but a later file validator failed. Only this
        # narrow path can need a cumulative universe solely for the shadow
        # observer. A fully passing worker uses the selected candidate directly
        # and avoids an otherwise discarded O(cumulative rows) merge. Keep the
        # observer merge isolated: it may suppress its own worker-final verdict,
        # but it may never change authoritative validation.
        try:
            cumulative_rows, cumulative_sources, _observer_failures, merged_provenance = (
                _validate_cumulative_artifacts(
                    validators=row_validators,
                    expected=expected,
                    candidates=[
                        *prior_candidates,
                        *attempt_candidates,
                        *candidates,
                    ],
                )
            )
            merged_rows, merged_sources = cumulative_rows, cumulative_sources
        except Exception as exc:
            observer_merge_error = f"{type(exc).__name__}: {exc}"

    status = "done" if not failures else "failed"
    result_artifacts = cumulative_sources if cumulative else (
        [selected.get("path")] if selected else (
            _unique_paths(artifacts) if file_validators else []
        )
    )
    valid_extraction_artifacts = cumulative_sources if cumulative else (
        [selected.get("path")] if selected and not failures else []
    )
    if evidence_sink is not None:
        if observer_merge_error:
            try:
                evidence_sink({
                    "observerError": {
                        "stage": "worker_final_cumulative_merge",
                        "error": observer_merge_error,
                    },
                })
            except Exception:
                pass
            evidence_sink = None
    if evidence_sink is not None:
        # Hand over the identity-merged rows whenever a merge was possible, even
        # though an incomplete merge is not adopted as authoritative above. A
        # 10-row attempt on top of a 9-row prior attempt is exactly the trusted
        # partial the evidence ledger exists to see; passing only this attempt's
        # rows would hide it.
        #
        # When no merge was possible the rows come from the single selected
        # candidate, which may be a schema-warning artifact the authoritative
        # path rejected. Say so explicitly: the online path already refuses
        # those saves, and an observer that trusts them here would be judging a
        # different universe than the one it is supposed to shadow.
        # Mirror the authoritative choice rather than always merging. When this
        # attempt alone satisfied the contract, the merged universe would drag
        # in earlier failed attempts the authority deliberately ignored, and the
        # observer would report a would-block for a run that actually succeeded.
        # Only when the authority failed is the merged set the interesting one,
        # because that is where a trusted partial hides.
        # Three cases, and only the first may use the single selected candidate:
        #   passed without merging  -> selected rows, so a clean run is not
        #                              polluted by earlier failed attempts
        #   passed via the merge    -> merged rows, or provenance would credit
        #                              every merged row to the current artifact
        #   failed                  -> merged rows, because that is where a
        #                              trusted partial hides
        # Deciding this from `failures` alone was wrong in the second case (a
        # successful merge clears them); deciding it from `cumulative` alone was
        # wrong in the third (an incomplete merge never sets it).
        use_merged = bool(merged_rows) and bool(cumulative or failures)
        sink_rejection = ""
        selected_payload = selected.get("payload", {}) if selected else {}
        if use_merged:
            sink_rows = merged_rows
            sink_paths = merged_sources
            sink_row_paths = [
                str(item.get("path") or "") for item in merged_provenance
            ]
            sink_row_scopes = [item.get("scope") or {} for item in merged_provenance]
            sink_scope = (
                sink_row_scopes[0]
                if sink_row_scopes
                and all(item == sink_row_scopes[0] for item in sink_row_scopes)
                else {}
            )
        else:
            sink_rows = rows
            sink_paths = result_artifacts
            selected_path = str(selected.get("path") or "") if selected else ""
            sink_row_paths = [selected_path] * len(rows)
            scope = selected_payload.get("evidenceContext")
            sink_scope = dict(scope) if isinstance(scope, dict) else {}
            sink_row_scopes = [sink_scope] * len(rows)
            if selected_payload.get("schemaWarnings"):
                sink_rejection = "schema_warning_artifact"
        try:
            evidence_sink({
                "rows": sink_rows,
                "sourcePaths": sink_paths,
                "rowArtifactIds": sink_row_paths,
                # Per-row scope, not one summary value. The authoritative merge
                # does not bucket by scope, so a merged set can legitimately mix
                # page/auth generations; collapsing that into a single reported
                # scope relabels older rows as fresh.
                "rowScopes": sink_row_scopes,
                "evidenceContext": sink_scope,
                "filesRead": len(all_extraction_artifacts),
                "rejectedReason": sink_rejection,
                "cumulative": use_merged,
            })
        except Exception:  # an observer must never fail authoritative validation
            pass

    result = {
        "status": status,
        "phase_id": contract.get("phase_id"),
        "expectedArtifact": expected,
        "rowCount": len(rows),
        "artifacts": result_artifacts,
        "allExtractionArtifacts": all_extraction_artifacts,
        "validExtractionArtifacts": valid_extraction_artifacts,
        "attemptExtractionArtifacts": extraction_attempt_artifacts,
        "priorExtractionArtifacts": prior_extraction_artifacts,
        "fileArtifacts": _unique_paths([
            path for path in artifacts
            if "/artifacts/extractions/" not in str(path)
        ]),
        "fileEvidenceCount": len(file_evidence or []),
        "failures": failures,
    }
    if cumulative:
        result["cumulative"] = True
        result["sourceArtifactCount"] = len(cumulative_sources)
    if warnings:
        result["warnings"] = warnings
    if failures:
        result["classification"] = classify_artifact_validation_failures(
            failures,
            rows=rows,
            expected_artifact=expected,
        )
    return result






















def classify_artifact_validation_failures(
    failures: List[JsonDict],
    *,
    rows: Optional[List[JsonDict]] = None,
    expected_artifact: Optional[JsonDict] = None,
) -> JsonDict:
    failure_types = {
        str(item.get("type") or "")
        for item in failures
        if isinstance(item, dict)
    }
    if failure_types & {"data_placeholder", "data_stub"}:
        category = "data_placeholder"
        hint = "Observed rows look like placeholder or stub content; reveal/load the real content or report absence."
    elif "artifact_required" in failure_types:
        category = "data_missing"
        hint = "No matching record_extraction artifact was produced; collect and save the target rows."
    elif failure_types & {"schema", "required_fields", "field_provenance"}:
        category = "schema_mismatch"
        hint = "Rows exist but do not match the expected artifact schema; reshape from evidence before re-scraping."
    elif failure_types & {"min_rows", "max_rows", "exact_rows"}:
        category = "data_wrong_shape"
        hint = "The number of rows does not satisfy the expected shape; adjust range/materialization or scope."
    elif failure_types & {
        "unique",
        "url_pattern",
        "allowed_domain",
        "set_equals",
        "range",
        "field_pattern",
        "cross_field_contains",
        "action_outcome",
        "field_nonempty",
    }:
        category = "data_wrong_value"
        hint = "Rows were saved, but one or more values failed semantic validators."
    elif failure_types & FILE_VALIDATOR_TYPES:
        category = "file_validation_failed"
        hint = "The file action ran, but completion, selection, confirmation, or on-disk integrity evidence is insufficient."
    else:
        category = "data_wrong_value" if failures else "unknown"
        hint = "Validation failed; inspect failures and choose a different recovery path."
    return {
        "category": category,
        "hint": hint,
        "failureTypes": sorted(ft for ft in failure_types if ft),
        "rowCount": len(rows or []),
        "expectedArtifactName": (
            str((expected_artifact or {}).get("name") or "")
            if isinstance(expected_artifact, dict)
            else ""
        ),
    }


def classification_for_worker_status(status: str) -> Optional[JsonDict]:
    text = str(status or "")
    if text in {
        WORKER_STATUS_BLOCKED_BY_CHALLENGE,
        WORKER_STATUS_HITL_REQUIRED,
        WORKER_STATUS_HITL_WAITING,
        WORKER_STATUS_HITL_TIMEOUT,
        WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
    }:
        return {
            "category": "blocked_user_action_required",
            "hint": "Human action or challenge resolution is required before retrying this phase.",
            "workerStatus": text,
        }
    if text in {WORKER_STATUS_PAGE_CRASHED, WORKER_STATUS_API_CONTRACT_ERROR}:
        return {
            "category": "blocked_infrastructure",
            "hint": "Infrastructure or browser state failed; rebuild the page/fleet or switch platform path.",
            "workerStatus": text,
        }
    return None


def run_row_validator(validator: JsonDict, rows: List[JsonDict]) -> List[JsonDict]:
    """Public entry point to the validator engine.

    The shared evaluator in ``harness.artifact_evidence`` cannot import this
    module (task_control imports the evaluator's row-hygiene kernel), so the
    engine is injected instead. This alias keeps that injection from reaching
    into a private name.
    """
    return _run_validator(validator, rows)


def _run_validator(validator: JsonDict, rows: List[JsonDict]) -> List[JsonDict]:
    validator_type = str(validator.get("type") or "").strip()
    failures: List[JsonDict] = []

    if validator_type == "artifact_required":
        return []

    if validator_type == "required_fields":
        fields = _string_list(validator.get("fields"))
        for index, row in enumerate(rows):
            missing = [field for field in fields if field not in row]
            if missing:
                failures.append({
                    "type": validator_type,
                    "row": index,
                    "missing": missing,
                })
        return failures

    if validator_type == "field_nonempty":
        fields = _string_list(validator.get("fields"))
        if not fields:
            field = str(validator.get("field") or "").strip()
            fields = [field] if field else []
        for index, row in enumerate(rows):
            # A key the extractor never wrote and a key it wrote as empty are
            # different claims. The first records nothing about the field; the
            # second is an explicit "I looked and there is nothing here".
            # Collapsing them is how a field that was never fetched gets
            # reported as a confirmed absence — and an over-specified contract
            # (a product that genuinely has no detail images) becomes
            # indistinguishable from a worker that skipped the field.
            #
            # The verdict is deliberately unchanged: both still fail. Only the
            # failure's shape differs, so the worker is told which of the two
            # to fix, and repeated confirmed-empty on one field is visible as
            # evidence that the contract, not the run, is wrong.
            unverified = [field for field in fields if field not in row]
            confirmed_empty = [
                field for field in fields
                if field in row and _is_empty_value(row.get(field))
            ]
            # A field the contract declares emptiable, whose row carries a
            # complete absence proof, is a finding rather than a hole. Without
            # this branch a product that genuinely has no reviews can never
            # satisfy the contract, and the phase burns every attempt against a
            # page that will not change.
            allowance = validator.get("allow_empty_with_outcome")
            incomplete_proofs: List[JsonDict] = []
            if confirmed_empty and isinstance(allowance, dict):
                remaining: List[str] = []
                for field in confirmed_empty:
                    verdict = field_absence_accepted(
                        row, field, allowed_outcomes=allowance.get(field),
                    )
                    if verdict["accepted"]:
                        continue
                    remaining.append(field)
                    if verdict.get("absenceProof"):
                        incomplete_proofs.append({
                            "field": field,
                            "reason": verdict["reason"],
                            "absenceProof": verdict["absenceProof"],
                        })
                confirmed_empty = remaining
            if confirmed_empty or unverified:
                failure: JsonDict = {
                    "type": validator_type,
                    "row": index,
                    "empty": [*confirmed_empty, *unverified],
                }
                if confirmed_empty:
                    failure["confirmedEmpty"] = confirmed_empty
                if unverified:
                    failure["unverified"] = unverified
                if incomplete_proofs:
                    failure["absenceProofs"] = incomplete_proofs
                failures.append(failure)
        return failures

    if validator_type in {"min_rows", "max_rows", "exact_rows"}:
        raw_value = validator.get("value")
        if raw_value is None:
            if validator_type == "min_rows":
                raw_value = validator.get("min")
            elif validator_type == "max_rows":
                raw_value = validator.get("max")
            else:
                raw_value = (
                    validator.get("count")
                    or validator.get("exact")
                    or validator.get("rows")
                )
        value = _positive_int(raw_value, default=0)
        count = len(rows)
        ok = (
            count >= value if validator_type == "min_rows"
            else count <= value if validator_type == "max_rows"
            else count == value
        )
        if not ok:
            failures.append({
                "type": validator_type,
                "expected": value,
                "actual": count,
            })
        return failures

    if validator_type == "unique":
        # Plan-author validators are copied through normalization verbatim
        # and commonly use "fields": [...] (plural). Reading only "field"
        # made every row key "" — a bogus all-duplicates needs_fix.
        unique_fields: List[str] = []
        single = str(validator.get("field") or "").strip()
        if single:
            unique_fields = [single]
        else:
            raw_fields = validator.get("fields")
            if isinstance(raw_fields, list):
                unique_fields = [
                    str(item).strip() for item in raw_fields if str(item).strip()
                ]
        if not unique_fields:
            return failures
        seen: Dict[str, int] = {}
        duplicates: List[JsonDict] = []
        for index, row in enumerate(rows):
            value = "\x1f".join(
                str(row.get(name) or "") for name in unique_fields
            )
            if value in seen:
                duplicates.append({"row": index, "firstRow": seen[value], "value": value})
            else:
                seen[value] = index
        if duplicates:
            failures.append({
                "type": validator_type,
                "field": ", ".join(unique_fields),
                "duplicates": duplicates[:20],
            })
        return failures

    if validator_type == "url_pattern":
        field = str(validator.get("field") or "url").strip()
        pattern = str(validator.get("pattern") or r"^https?://").strip()
        regex = re.compile(pattern)
        bad = [
            {"row": index, "value": row.get(field)}
            for index, row in enumerate(rows)
            if not regex.search(str(row.get(field) or ""))
        ]
        if bad:
            failures.append({"type": validator_type, "field": field, "pattern": pattern, "bad": bad[:20]})
        return failures

    if validator_type == "field_pattern":
        field = str(validator.get("field") or "").strip()
        pattern = str(validator.get("pattern") or "").strip()
        regex = _compile_validator_regex(pattern, validator)
        bad = [
            {"row": index, "value": row.get(field)}
            for index, row in enumerate(rows)
            if not regex.search(str(row.get(field) or ""))
        ]
        if bad:
            failures.append({
                "type": validator_type,
                "field": field,
                "pattern": pattern,
                "bad": bad[:20],
            })
        return failures

    if validator_type == "cross_field_contains":
        field = str(validator.get("field") or "").strip()
        contains_field = str(
            validator.get("contains_field")
            or validator.get("expected_field")
            or ""
        ).strip()
        case_sensitive = bool(validator.get("case_sensitive", False))
        bad = []
        for index, row in enumerate(rows):
            haystack = str(row.get(field) or "")
            needle = str(row.get(contains_field) or "")
            if not needle:
                bad.append({
                    "row": index,
                    "field": contains_field,
                    "reason": "expected field empty",
                })
                continue
            left = haystack if case_sensitive else haystack.lower()
            right = needle if case_sensitive else needle.lower()
            if right not in left:
                bad.append({
                    "row": index,
                    "field": field,
                    "contains_field": contains_field,
                    "value": haystack[:200],
                    "expected": needle[:200],
                })
        if bad:
            failures.append({
                "type": validator_type,
                "field": field,
                "contains_field": contains_field,
                "bad": bad[:20],
            })
        return failures

    if validator_type == "action_outcome":
        failures.extend(_validate_action_outcome(validator, rows))
        return failures

    if validator_type == "field_provenance":
        failures.extend(_validate_field_provenance(validator, rows))
        return failures

    if validator_type == "allowed_domain":
        field = str(validator.get("field") or "url").strip()
        domains = set(_string_list(validator.get("domains")))
        bad = []
        for index, row in enumerate(rows):
            host = urlparse(str(row.get(field) or "")).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host not in domains:
                bad.append({"row": index, "host": host, "value": row.get(field)})
        if bad:
            failures.append({"type": validator_type, "field": field, "domains": sorted(domains), "bad": bad[:20]})
        return failures

    if validator_type == "set_equals":
        field = str(validator.get("field") or "").strip()
        expected = {str(item) for item in (validator.get("values") or [])}
        actual = {str(row.get(field)) for row in rows}
        if actual != expected:
            failures.append({
                "type": validator_type,
                "field": field,
                "expected": sorted(expected),
                "actual": sorted(actual),
            })
        return failures

    if validator_type == "range":
        field = str(validator.get("field") or "").strip()
        min_value = validator.get("min")
        max_value = validator.get("max")
        bad = []
        for index, row in enumerate(rows):
            raw_value = row.get(field)
            if raw_value is None:
                bad.append({"row": index, "value": raw_value, "reason": "not numeric"})
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                bad.append({"row": index, "value": raw_value, "reason": "not numeric"})
                continue
            if min_value is not None and value < float(min_value):
                bad.append({"row": index, "value": value, "reason": "below min"})
            if max_value is not None and value > float(max_value):
                bad.append({"row": index, "value": value, "reason": "above max"})
        if bad:
            failures.append({"type": validator_type, "field": field, "bad": bad[:20]})
        return failures

    return [{
        "type": "unknown_validator",
        "validator": validator_type,
        "message": f"validator type must be one of {sorted(VALIDATOR_TYPES)}",
    }]


def _run_file_validator(
    validator: JsonDict,
    *,
    artifacts: List[str],
    evidence: List[JsonDict],
    rows: List[JsonDict],
) -> List[JsonDict]:
    validator_type = str(validator.get("type") or "").strip()
    file_paths = _unique_paths([
        str(path) for path in artifacts
        if str(path).strip() and "/artifacts/extractions/" not in str(path)
    ])

    if validator_type == "download_completed":
        receipts = [
            item for item in evidence
            if isinstance(item, dict) and (
                str(item.get("method") or "") == "File.download"
                or str(item.get("method") or "").startswith("Download.")
            )
        ]
        completed = any(_file_receipt_completed(item) for item in receipts)
        if completed:
            return []
        return [{
            "type": validator_type,
            "message": "no successful completed download receipt was recorded",
            "receiptCount": len(receipts),
        }]

    if validator_type == "file_integrity":
        pattern = str(validator.get("path_pattern") or validator.get("pattern") or "").strip()
        extensions = {
            str(item).lower().lstrip(".")
            for item in (validator.get("extensions") or [])
            if str(item).strip()
        }
        raw_min_bytes = validator.get("min_bytes")
        min_bytes = max(0, int(1 if raw_min_bytes is None else raw_min_bytes))
        expected_sha256 = str(validator.get("sha256") or "").strip().lower()
        valid: List[str] = []
        bad: List[JsonDict] = []
        regex = re.compile(pattern) if pattern else None
        for raw_path in file_paths:
            path = Path(raw_path).expanduser()
            if regex is not None and regex.search(str(path)) is None:
                continue
            if extensions and path.suffix.lower().lstrip(".") not in extensions:
                continue
            try:
                if not path.is_file():
                    bad.append({"path": str(path), "reason": "not_a_file"})
                    continue
                size = path.stat().st_size
                if size < min_bytes:
                    bad.append({"path": str(path), "reason": "too_small", "byteSize": size})
                    continue
                if expected_sha256:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    if digest != expected_sha256:
                        bad.append({"path": str(path), "reason": "sha256_mismatch", "sha256": digest})
                        continue
                valid.append(str(path))
            except OSError as exc:
                bad.append({"path": str(path), "reason": "io_error", "error": str(exc)})
        min_files = max(1, int(validator.get("min_files") or 1))
        if len(valid) >= min_files:
            return []
        return [{
            "type": validator_type,
            "message": "downloaded/exported files did not satisfy integrity constraints",
            "requiredFiles": min_files,
            "validFiles": valid,
            "bad": bad[:20],
            "availableArtifacts": file_paths,
        }]

    if validator_type in {"upload_selected", "upload_confirmed"}:
        receipts = [
            item for item in evidence
            if isinstance(item, dict)
            and str(item.get("method") or "") == "File.handleChooser"
            and _file_receipt_succeeded(item)
        ]
        selected_count = max(
            (_selected_file_count(item.get("params")) for item in receipts),
            default=0,
        )
        min_files = max(1, int(validator.get("min_files") or 1))
        if not receipts or selected_count < min_files:
            return [{
                "type": validator_type,
                "message": "File.handleChooser did not confirm the required file selection",
                "requiredFiles": min_files,
                "selectedFiles": selected_count,
                "receiptCount": len(receipts),
            }]
        if validator_type == "upload_selected":
            return []
        field = str(validator.get("field") or "").strip()
        pattern = str(validator.get("pattern") or "").strip()
        if not field:
            return [{
                "type": validator_type,
                "message": "upload_confirmed requires a field containing post-upload page evidence",
            }]
        regex = re.compile(pattern) if pattern else None
        confirmed = any(
            not _is_empty_value(row.get(field))
            and (regex is None or regex.search(str(row.get(field) or "")) is not None)
            for row in rows
        )
        if confirmed:
            return []
        return [{
            "type": validator_type,
            "message": "file selection succeeded but post-upload page confirmation is missing",
            "field": field,
            "pattern": pattern,
        }]

    if validator_type == "image_exported":
        min_files = max(1, int(validator.get("min_files") or 1))
        # `svg` belongs here: DOM.getImg with imageFormat="auto" preserves a
        # safe self-contained inline SVG instead of rasterizing it, so a real
        # exported asset can legitimately arrive with that extension.
        image_exts = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "svg"}
        exported = [
            path for path in file_paths
            if Path(path).suffix.lower().lstrip(".") in image_exts
        ]
        receipts = [
            item for item in evidence
            if isinstance(item, dict)
            and str(item.get("method") or "") == "DOM.getImg"
            and _file_receipt_succeeded(item)
        ]
        if receipts and len(exported) >= min_files:
            return []
        return [{
            "type": validator_type,
            "message": "DOM.getImg did not produce the required image artifacts",
            "requiredFiles": min_files,
            "exportedFiles": exported,
            "receiptCount": len(receipts),
        }]

    return [{"type": "unknown_file_validator", "validator": validator_type}]


def _file_receipt_succeeded(receipt: JsonDict) -> bool:
    response = receipt.get("response")
    return isinstance(response, dict) and not response.get("error")


def _file_receipt_completed(receipt: JsonDict) -> bool:
    if not _file_receipt_succeeded(receipt):
        return False
    response = receipt.get("response")
    strings: List[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif key.lower() in {"status", "state"}:
            strings.append(str(value).strip().lower())

    walk(response)
    if any(value in {"completed", "complete", "done", "success", "succeeded"} for value in strings):
        return True
    return bool(_receipt_saved_paths(response))


def _receipt_saved_paths(value: Any) -> List[str]:
    # Compatibility wrapper retained for validator-level tests.
    return saved_paths_from_value(value)


def _selected_file_count(params: Any) -> int:
    if not isinstance(params, dict):
        return 0
    for key in ("files", "paths", "filePaths"):
        value = params.get(key)
        if isinstance(value, list):
            return len([item for item in value if str(item).strip()])
    for key in ("path", "filePath"):
        if isinstance(params.get(key), str) and str(params.get(key)).strip():
            return 1
    return 0


def _validate_action_outcome(
    validator: JsonDict,
    rows: List[JsonDict],
) -> List[JsonDict]:
    url_field = str(validator.get("url_field") or "url").strip()
    heading_field = str(validator.get("heading_field") or "heading").strip()
    expected_url_pattern = str(validator.get("expected_url_pattern") or "").strip()
    expected_heading_pattern = str(
        validator.get("expected_heading_pattern") or ""
    ).strip()
    heading_matches_field = str(
        validator.get("heading_matches_field") or ""
    ).strip()
    min_similarity = _float_value(validator.get("min_similarity"), default=0.8)

    url_regex = (
        _compile_validator_regex(expected_url_pattern, validator)
        if expected_url_pattern
        else None
    )
    heading_regex = (
        _compile_validator_regex(expected_heading_pattern, validator)
        if expected_heading_pattern
        else None
    )

    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        reasons: List[JsonDict] = []
        url_value = str(row.get(url_field) or "")
        heading_value = str(row.get(heading_field) or "")
        if url_regex and not url_regex.search(url_value):
            reasons.append({
                "reason": "url_pattern_mismatch",
                "field": url_field,
                "value": url_value[:300],
                "pattern": expected_url_pattern,
            })
        if heading_regex and not heading_regex.search(heading_value):
            reasons.append({
                "reason": "heading_pattern_mismatch",
                "field": heading_field,
                "value": heading_value[:300],
                "pattern": expected_heading_pattern,
            })
        if heading_matches_field:
            expected = str(row.get(heading_matches_field) or "")
            score = _similarity(heading_value, expected)
            if expected and score < min_similarity:
                reasons.append({
                    "reason": "heading_similarity_below_threshold",
                    "field": heading_field,
                    "matches_field": heading_matches_field,
                    "similarity": round(score, 3),
                    "min_similarity": min_similarity,
                    "value": heading_value[:300],
                    "expected": expected[:300],
                })
            elif not expected:
                reasons.append({
                    "reason": "expected_heading_field_empty",
                    "matches_field": heading_matches_field,
                })
        if reasons:
            bad.append({"row": index, "failures": reasons})

    if not bad:
        return []
    return [{"type": "action_outcome", "bad": bad[:20]}]


def _validate_field_provenance(
    validator: JsonDict,
    rows: List[JsonDict],
) -> List[JsonDict]:
    raw_fields = validator.get("fields") or validator.get("field_provenance")
    if isinstance(raw_fields, list):
        specs = {str(field): {} for field in raw_fields if str(field).strip()}
    elif isinstance(raw_fields, dict):
        specs = {
            str(field): spec if isinstance(spec, dict) else {}
            for field, spec in raw_fields.items()
            if str(field).strip()
        }
    else:
        field = str(validator.get("field") or "").strip()
        specs = {field: {}} if field else {}

    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        for field, spec in specs.items():
            evidence_field = str(
                spec.get("evidence_field")
                or validator.get("evidence_field")
                or f"{field}EvidenceText"
            ).strip()
            source_tool_field = str(
                spec.get("source_tool_field")
                or validator.get("source_tool_field")
                or "sourceTool"
            ).strip()
            selector_field = str(
                spec.get("selector_field")
                or validator.get("selector_field")
                or "sourceSelectorOrAxId"
            ).strip()
            evidence_aliases = _provenance_evidence_aliases(
                field,
                evidence_field,
                spec,
                validator,
            )
            missing = []
            if row.get(field) is None or str(row.get(field)).strip() == "":
                missing.append(field)
            if not evidence_field or not _row_has_nonempty_value(row, evidence_aliases):
                missing.append(evidence_field or "evidence_field")
            if bool(spec.get("require_source_tool", validator.get("require_source_tool", False))):
                if str(row.get(source_tool_field) or "").strip() == "":
                    missing.append(source_tool_field)
            if bool(spec.get("require_selector", validator.get("require_selector", False))):
                if str(row.get(selector_field) or "").strip() == "":
                    missing.append(selector_field)
            if missing:
                bad.append({"row": index, "field": field, "missing": missing})

    if not bad:
        return []
    return [{"type": "field_provenance", "bad": bad[:20]}]


def _provenance_evidence_aliases(
    field: str,
    evidence_field: str,
    spec: JsonDict,
    validator: JsonDict,
) -> List[str]:
    aliases: List[Any] = [
        evidence_field,
        f"{field}EvidenceText",
        f"{field}Evidence",
    ]
    raw_aliases = spec.get("evidence_aliases")
    if raw_aliases is None:
        raw_aliases = validator.get("evidence_aliases")
    if isinstance(raw_aliases, list):
        aliases.extend(raw_aliases)
    elif isinstance(raw_aliases, str):
        aliases.append(raw_aliases)
    aliases.extend(["evidence", "evidenceText"])
    return field_names_from_specs(aliases)


def _row_has_nonempty_value(row: JsonDict, fields: List[str]) -> bool:
    for field in fields:
        if str(row.get(field) or "").strip():
            return True
    return False


def _compile_validator_regex(pattern: str, validator: JsonDict) -> re.Pattern[str]:
    flags = re.I if bool(validator.get("case_insensitive", False)) else 0
    return re.compile(pattern or r"$^", flags)


def _similarity(left: str, right: str) -> float:
    left_norm = _norm_compare_text(left)
    right_norm = _norm_compare_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm in right_norm or right_norm in left_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _norm_compare_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_count_validator_value(validator: JsonDict) -> Optional[int]:
    validator_type = str(validator.get("type") or "").strip()
    raw_value = validator.get("value")
    if raw_value is None:
        if validator_type == "min_rows":
            raw_value = validator.get("min")
        elif validator_type == "max_rows":
            raw_value = validator.get("max")
        elif validator_type == "exact_rows":
            for key in ("count", "exact", "rows"):
                if validator.get(key) is not None:
                    raw_value = validator.get(key)
                    break
    value = _positive_int(raw_value, default=0)
    return value if value > 0 else None


def _normalize_expected_artifact_contract(
    expected_artifact: JsonDict,
    validators: List[Any],
    errors: List[str],
    warnings: List[JsonDict],
    *,
    phase_id: str,
) -> JsonDict:
    """Recover one canonical expected-artifact shape from equivalent inputs.

    Gateway models sometimes put a field list under an empty JSON key (task
    51496108) even though the same list is correctly present in a
    required_fields validator. The empty key has no semantics: drop it with a
    receipt, then backfill only from an unambiguous contract source.
    """
    expected = dict(expected_artifact)
    blank_values = []
    for key in list(expected):
        if str(key).strip():
            continue
        blank_values.append(expected.pop(key))
        warnings.append({
            "type": "empty_expected_artifact_key_dropped",
            "phase": phase_id,
            "message": (
                "expected_artifact contained an empty property name; it has no"
                " contract meaning and was dropped"
            ),
        })

    field_names = field_names_from_specs(expected.get("fields") or [])
    if not field_names:
        field_names = field_names_from_specs(expected.get("required_fields") or [])
    explicit_required: List[str] = []
    seen_required = set()
    explicit_exact_values: Set[int] = set()
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        validator_type = str(validator.get("type") or "").strip()
        validator_type = VALIDATOR_TYPE_ALIASES.get(validator_type, validator_type)
        if validator_type == "required_fields":
            for field in field_names_from_specs(validator.get("fields") or []):
                if field not in seen_required:
                    explicit_required.append(field)
                    seen_required.add(field)
        elif validator_type == "exact_rows":
            value = _row_count_validator_value({**validator, "type": validator_type})
            if value is not None:
                explicit_exact_values.add(value)

    if not field_names and explicit_required:
        expected["fields"] = explicit_required
        field_names = list(explicit_required)
        warnings.append({
            "type": "expected_artifact_fields_backfilled",
            "phase": phase_id,
            "source": "required_fields",
            "fields": explicit_required,
        })
    if blank_values and not field_names and any(value not in (None, "", [], {}) for value in blank_values):
        errors.append(
            f"phase {phase_id}: expected_artifact empty-key value could not be"
            " recovered from fields/required_fields"
        )

    if expected.get("exact_rows") is None:
        if len(explicit_exact_values) == 1:
            expected["exact_rows"] = next(iter(explicit_exact_values))
            warnings.append({
                "type": "expected_artifact_exact_rows_backfilled",
                "phase": phase_id,
                "source": "exact_rows validator",
                "value": expected["exact_rows"],
            })
        elif len(explicit_exact_values) > 1:
            errors.append(
                f"phase {phase_id}: conflicting exact_rows validators:"
                f" {sorted(explicit_exact_values)}"
            )
    return expected


def _canonical_validator_params(validator: JsonDict) -> JsonDict:
    normalized = dict(validator)
    validator_type = str(normalized.get("type") or "").strip()
    if validator_type in {"min_rows", "max_rows", "exact_rows"}:
        value = _row_count_validator_value(normalized)
        if value is not None:
            normalized["value"] = value
        for alias in ("count", "exact", "rows", "min", "max"):
            normalized.pop(alias, None)
    if validator_type in {"required_fields", "field_nonempty", "unique"}:
        fields = field_names_from_specs(normalized.get("fields") or [])
        single = str(normalized.get("field") or "").strip()
        if single and single not in fields:
            fields.append(single)
        if fields:
            normalized["fields"] = fields
            normalized.pop("field", None)
    return normalized


def _validator_semantic_signature(validator: JsonDict) -> str:
    validator_type = str(validator.get("type") or "")
    if validator_type in {"min_rows", "max_rows", "exact_rows"}:
        payload: Any = [validator_type, _row_count_validator_value(validator)]
    elif validator_type in {"required_fields", "field_nonempty", "unique"}:
        payload = [
            validator_type,
            sorted(set(field_names_from_specs(validator.get("fields") or []))),
        ]
    elif validator_type == "set_equals":
        payload = [
            validator_type,
            str(validator.get("field") or ""),
            sorted({str(value) for value in (validator.get("values") or [])}),
        ]
    else:
        payload = validator
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _dedupe_and_check_validators(
    validators: List[JsonDict],
    errors: List[str],
    *,
    phase_id: str,
    warnings: Optional[List[JsonDict]],
) -> List[JsonDict]:
    invalid_row_constraints = [
        str(validator.get("type") or "")
        for validator in validators
        if str(validator.get("type") or "") in {
            "min_rows", "max_rows", "exact_rows",
        }
        and _row_count_validator_value(validator) is None
    ]
    for validator_type in invalid_row_constraints:
        errors.append(
            f"phase {phase_id}: {validator_type} requires a positive integer value"
        )
    exact_values = {
        value for value in (
            _row_count_validator_value(validator)
            for validator in validators
            if str(validator.get("type") or "") == "exact_rows"
        )
        if value is not None
    }
    if len(exact_values) > 1:
        errors.append(
            f"phase {phase_id}: conflicting exact_rows constraints:"
            f" {sorted(exact_values)}"
        )

    out: List[JsonDict] = []
    seen: Dict[str, JsonDict] = {}
    for validator in validators:
        signature = _validator_semantic_signature(validator)
        kept = seen.get(signature)
        if kept is not None:
            # field_nonempty's semantic signature is type+fields, so a duplicate
            # may still carry an emptiable-field declaration the kept copy lacks
            # (expected_artifact and an explicit validator each derive one).
            # Dropping it wholesale would silently reinstate the strict reading
            # and pin the phase on a row that is legitimately empty.
            allowance = validator.get("allow_empty_with_outcome")
            if (
                str(validator.get("type") or "") == "field_nonempty"
                and isinstance(allowance, dict)
            ):
                merged = dict(kept.get("allow_empty_with_outcome") or {})
                merged.update(allowance)
                kept["allow_empty_with_outcome"] = merged
            if warnings is not None:
                warnings.append({
                    "type": "duplicate_validator_dropped",
                    "phase": phase_id,
                    "validatorType": str(validator.get("type") or ""),
                })
            continue
        seen[signature] = validator
        out.append(validator)
    return out


def _normalize_validators(
    expected_artifact: JsonDict,
    validators: List[Any],
    errors: List[str],
    *,
    phase_id: str,
    warnings: Optional[List[JsonDict]] = None,
) -> List[JsonDict]:
    normalized: List[JsonDict] = []
    if expected_artifact.get("min_rows") is not None:
        normalized.append({"type": "min_rows", "value": expected_artifact.get("min_rows")})
    if expected_artifact.get("max_rows") is not None:
        normalized.append({"type": "max_rows", "value": expected_artifact.get("max_rows")})
    has_exact_rows = expected_artifact.get("exact_rows") is not None
    if has_exact_rows:
        normalized.append({"type": "exact_rows", "value": expected_artifact.get("exact_rows")})
    count_range = expected_artifact.get("count_range")
    if isinstance(count_range, list) and len(count_range) >= 2:
        min_rows = _positive_int(count_range[0], default=0)
        max_rows = _positive_int(count_range[1], default=0)
        if not has_exact_rows:
            if min_rows > 0 and min_rows == max_rows:
                normalized.append({"type": "exact_rows", "value": min_rows})
            else:
                if min_rows > 0:
                    normalized.append({"type": "min_rows", "value": min_rows})
                if max_rows > 0:
                    normalized.append({"type": "max_rows", "value": max_rows})
    fields = expected_artifact.get("required_fields")
    if not isinstance(fields, list) or not fields:
        fields = expected_artifact.get("fields")
    field_names = field_names_from_specs(fields)
    if field_names:
        normalized.append({"type": "required_fields", "fields": field_names})
        nonempty_fields = _nonempty_fields_from_expected(expected_artifact, fields)
        if nonempty_fields:
            nonempty_validator: JsonDict = {
                "type": "field_nonempty", "fields": nonempty_fields,
            }
            allowance = _allow_empty_with_outcome_from_expected(
                expected_artifact, fields, nonempty_fields,
            )
            if allowance:
                nonempty_validator["allow_empty_with_outcome"] = allowance
            normalized.append(nonempty_validator)
        provenance_fields = _provenance_required_fields(expected_artifact, field_names)
        if provenance_fields and not _has_field_provenance_validator(validators, provenance_fields):
            normalized.append({
                "type": "field_provenance",
                "fields": _provenance_field_specs(provenance_fields),
                "require_source_tool": True,
                "require_selector": True,
            })

    for index, validator in enumerate(validators):
        if not isinstance(validator, dict):
            errors.append(f"phase {phase_id}: validators[{index}] must be an object")
            continue
        validator_type = str(validator.get("type") or "").strip()
        canonical_type = VALIDATOR_TYPE_ALIASES.get(validator_type, validator_type)
        if canonical_type != validator_type:
            if warnings is not None:
                warnings.append({
                    "type": "validator_type_alias",
                    "phase": phase_id,
                    "index": index,
                    "input": validator_type,
                    "canonical": canonical_type,
                    "message": (
                        f"validator type {validator_type!r} normalized to"
                        f" {canonical_type!r}; emit the canonical name next time"
                    ),
                })
            validator_type = canonical_type
        if validator_type not in VALIDATOR_TYPES:
            errors.append(
                f"phase {phase_id}: validators[{index}].type must be one of {sorted(VALIDATOR_TYPES)}"
            )
        normalized_validator = dict(validator)
        normalized_validator["type"] = validator_type
        if validator_type == "upload_confirmed":
            confirmation_field = str(normalized_validator.get("field") or "").strip()
            if not confirmation_field:
                errors.append(
                    f"phase {phase_id}: validators[{index}] upload_confirmed"
                    " requires field"
                )
            elif confirmation_field not in field_names:
                errors.append(
                    f"phase {phase_id}: upload_confirmed field"
                    f" {confirmation_field!r} must be declared in"
                    " expected_artifact.fields so the worker records page evidence"
                )
        if validator_type == "allowed_domain":
            # Accept the singular spelling a plan naturally writes, then refuse
            # a declaration that cannot pass. An empty allowlist is not "allow
            # anything" — it is "allow nothing": every row's host is outside
            # it, on every attempt, with nothing the worker can do. That is the
            # unsatisfiable-contract shape this whole series exists to stop,
            # and it happened live in task 3189c68b: two workers extracted
            # their pages completely and were failed anyway, because the plan
            # wrote `domain` while the check reads `domains`.
            singular = str(normalized_validator.pop("domain", "") or "").strip()
            domains = _string_list(normalized_validator.get("domains"))
            if singular and singular not in domains:
                domains = [*domains, singular]
            normalized_validator["domains"] = domains
            if not domains:
                errors.append(
                    f"phase {phase_id}: validators[{index}] allowed_domain"
                    " declares no domain, so no row can ever pass it; list the"
                    " allowed hosts in `domains`"
                )
            # The field defaults to "url", which most artifacts do not carry.
            # A validator pointed at a field the artifact never declares fails
            # every row for a reason the data cannot fix.
            domain_field = str(normalized_validator.get("field") or "url").strip()
            normalized_validator["field"] = domain_field
            if field_names and domain_field not in field_names:
                errors.append(
                    f"phase {phase_id}: allowed_domain field {domain_field!r}"
                    " is not declared in expected_artifact.fields; point it at"
                    " the field that holds the URL"
                )
        if validator_type == "field_provenance":
            normalized_validator["fields"] = _normalize_provenance_validator_fields(
                normalized_validator
            )
        normalized.append(_canonical_validator_params(normalized_validator))
    normalized = [_canonical_validator_params(item) for item in normalized]
    return _dedupe_and_check_validators(
        normalized,
        errors,
        phase_id=phase_id,
        warnings=warnings,
    )


def _allow_empty_with_outcome_from_expected(
    expected_artifact: JsonDict, fields: Any, nonempty_fields: List[str],
) -> JsonDict:
    """Which non-empty fields may still be empty when their absence is proven.

    Declared per field, either on the field spec or as a top-level map, and
    only meaningful for a field that is otherwise required non-empty. The plan
    must say so explicitly: guessing which fields a site sometimes omits is the
    kind of site knowledge the harness has no business inventing.
    """
    allowance: JsonDict = {}
    wanted = set(nonempty_fields)

    def _record(name: Any, raw: Any) -> None:
        field = str(name or "").strip()
        if not field or field not in wanted:
            return
        outcomes = [
            str(item).strip()
            for item in (raw if isinstance(raw, list) else [])
            if str(item or "").strip() in ROW_OUTCOMES
        ]
        if outcomes:
            allowance[field] = outcomes

    declared = expected_artifact.get("allow_empty_with_outcome")
    if isinstance(declared, dict):
        for name, raw in declared.items():
            _record(name, raw)
    if isinstance(fields, list):
        for spec in fields:
            if isinstance(spec, dict):
                _record(
                    field_name_from_spec(spec),
                    spec.get("allow_empty_with_outcome"),
                )
    return allowance


def _nonempty_fields_from_expected(expected_artifact: JsonDict, fields: Any) -> List[str]:
    explicit = expected_artifact.get("nonempty_fields")
    if explicit is None:
        explicit = expected_artifact.get("field_nonempty")
    out = field_names_from_specs(explicit if isinstance(explicit, list) else [])
    seen = set(out)
    if not isinstance(fields, list):
        return out
    scalar_types = {"str", "string", "text", "number", "integer", "int", "float", "url"}
    for spec in fields:
        if not isinstance(spec, dict):
            continue
        name = field_name_from_spec(spec)
        if not name or name in seen:
            continue
        if spec.get("allow_empty") is True or spec.get("optional_empty") is True:
            continue
        if spec.get("nonempty") is True or spec.get("required_nonempty") is True:
            out.append(name)
            seen.add(name)
            continue
        type_name = str(spec.get("type") or "").strip().lower()
        if type_name in scalar_types and spec.get("nullable") is not True:
            out.append(name)
            seen.add(name)
    return out


def _normalize_provenance_validator_fields(validator: JsonDict) -> JsonDict:
    raw_fields = validator.get("fields") or validator.get("field_provenance")
    if isinstance(raw_fields, dict):
        specs: JsonDict = {}
        for field, raw_spec in raw_fields.items():
            field_name = str(field or "").strip()
            if not field_name:
                continue
            spec = dict(raw_spec) if isinstance(raw_spec, dict) else {}
            merged = _default_provenance_field_spec(field_name)
            merged.update({key: value for key, value in spec.items() if value is not None})
            specs[field_name] = merged
        return specs
    fields = field_names_from_specs(raw_fields if isinstance(raw_fields, list) else [])
    if not fields:
        field = str(validator.get("field") or "").strip()
        fields = [field] if field else []
    return _provenance_field_specs(fields)


def _provenance_field_specs(fields: List[str]) -> JsonDict:
    specs: JsonDict = {}
    for field in field_names_from_specs(fields):
        specs[field] = _default_provenance_field_spec(field)
    return specs


def _default_provenance_field_spec(field: str) -> JsonDict:
    return {
        "evidence_field": f"{field}EvidenceText",
        "evidence_aliases": ["evidence", f"{field}Evidence"],
        "source_tool_field": "sourceTool",
        "selector_field": "sourceSelectorOrAxId",
        "require_source_tool": True,
        "require_selector": True,
    }


def _has_field_provenance_validator(
    validators: List[Any],
    fields: List[str],
) -> bool:
    wanted = set(field_names_from_specs(fields))
    if not wanted:
        return True
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "field_provenance":
            continue
        raw_fields = validator.get("fields") or validator.get("field_provenance")
        if isinstance(raw_fields, dict):
            present = {str(field).strip() for field in raw_fields.keys()}
        elif isinstance(raw_fields, list):
            present = set(field_names_from_specs(raw_fields))
        else:
            present = {str(validator.get("field") or "").strip()}
        if wanted.issubset({field for field in present if field}):
            return True
    return False


def _provenance_required_fields(
    expected_artifact: JsonDict,
    fields: List[Any],
) -> List[str]:
    override = expected_artifact.get("provenance_required")
    if isinstance(override, list):
        return field_names_from_specs(override)
    out: List[str] = []
    for field in fields:
        text = field_name_from_spec(field)
        if not text:
            continue
        lowered = text.lower()
        tokens = {token for token in re.split(r"[_\W]+", lowered) if token}
        if lowered in SENSITIVE_PROVENANCE_FIELD_MARKERS or tokens & SENSITIVE_PROVENANCE_FIELD_MARKERS:
            out.append(text)
    return out


def _validate_cumulative_artifacts(
    *,
    validators: List[JsonDict],
    expected: JsonDict,
    candidates: List[JsonDict],
) -> Tuple[List[JsonDict], List[str], List[JsonDict], List[JsonDict]]:
    rows_by_key: Dict[str, JsonDict] = {}
    row_sources: Dict[str, str] = {}
    row_scopes: Dict[str, JsonDict] = {}
    source_paths: List[str] = []
    schema_failures: List[JsonDict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        schema_warnings = payload.get("schemaWarnings")
        if isinstance(schema_warnings, list) and schema_warnings:
            schema_failures.append({
                "type": "schema",
                "message": "cumulative artifact has schemaWarnings",
                "path": path,
                "schemaWarnings": schema_warnings[:5],
            })
            continue
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list):
            schema_failures.append({
                "type": "schema",
                "message": "cumulative artifact has no rows array",
                "path": path,
            })
            continue
        if path:
            source_paths.append(path)
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            key = _cumulative_row_key(row, expected)
            existing = rows_by_key.get(key)
            if existing is None or _prefer_cumulative_row(
                row,
                existing,
                validators=validators,
                expected=expected,
            ):
                rows_by_key[key] = row
                row_sources[key] = path
                scope = payload.get("evidenceContext")
                row_scopes[key] = dict(scope) if isinstance(scope, dict) else {}

    source_paths = _unique_paths(source_paths)
    if len(source_paths) < 2:
        return [], source_paths, schema_failures, []

    ordered_keys = list(rows_by_key)
    rows = [rows_by_key[key] for key in ordered_keys]
    provenance = [
        {"path": row_sources.get(key, ""), "scope": row_scopes.get(key, {})}
        for key in ordered_keys
    ]
    failures: List[JsonDict] = []
    for validator in validators:
        failures.extend(_run_validator(validator, rows))
    failures.extend(detect_placeholder_rows(rows))
    failures.extend(detect_blocker_data_rows(rows, expected))
    failures.extend(detect_stub_rows(rows, expected))
    return rows, source_paths, [*schema_failures, *failures], provenance


def make_row_preference(
    *,
    validators: List[JsonDict],
    expected: JsonDict,
) -> Any:
    """Bind the authoritative row-quality reducer for an external consumer.

    The shadow ledger must resolve two rows sharing one identity exactly as the
    cumulative merge does. Last-write-wins there and quality-wins here would put
    a different row in each verdict and make the comparison meaningless.
    """

    def prefer(candidate: JsonDict, current: JsonDict) -> bool:
        return _prefer_cumulative_row(
            candidate, current, validators=validators, expected=expected,
        )

    return prefer


def _prefer_cumulative_row(
    candidate: JsonDict,
    current: JsonDict,
    *,
    validators: List[JsonDict],
    expected: JsonDict,
) -> bool:
    return _cumulative_row_quality(candidate, validators, expected) >= _cumulative_row_quality(
        current,
        validators,
        expected,
    )


def _cumulative_row_quality(
    row: JsonDict,
    validators: List[JsonDict],
    expected: JsonDict,
) -> tuple:
    row_failures: List[JsonDict] = []
    for validator in validators:
        validator_type = str(validator.get("type") or "").strip()
        if validator_type in {"min_rows", "max_rows", "exact_rows", "unique", "set_equals"}:
            continue
        row_failures.extend(_run_validator(validator, [row]))
    row_failures.extend(detect_placeholder_rows([row]))
    row_failures.extend(detect_blocker_data_rows([row], expected))
    row_failures.extend(detect_stub_rows([row], expected))

    expected_fields = field_names_from_specs(
        expected.get("required_fields") or expected.get("fields") or []
    )
    present = sum(1 for field in expected_fields if field in row)
    nonempty_expected = sum(
        1 for field in expected_fields
        if field in row and not _is_empty_value(row.get(field))
    )
    evidence = sum(
        1 for key, value in row.items()
        if str(key).endswith("EvidenceText") and str(value or "").strip()
    )
    source = sum(
        1 for key in ("sourceTool", "sourceSelectorOrAxId", "pageUrl")
        if str(row.get(key) or "").strip()
    )
    nonempty_total = sum(1 for value in row.values() if not _is_empty_value(value))
    return (
        -len(row_failures),
        present,
        evidence,
        source,
        nonempty_expected,
        nonempty_total,
    )


def _load_extraction_artifacts(paths: List[str], task_dir: Path) -> List[JsonDict]:
    loaded: List[JsonDict] = []
    root = task_dir.resolve(strict=False)
    for raw_path in paths:
        try:
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            loaded.append({"path": str(resolved), "payload": payload})
    return loaded


def _state_path(logger: RunLogger) -> Path:
    return logger.task_dir / TASK_STATE_FILE


def _first_phase_id(plan: JsonDict) -> Optional[str]:
    for phase in plan.get("phases", []):
        if isinstance(phase, dict):
            return str(phase.get("id") or "")
    return None


def _phase_state(state: JsonDict, phase_id: str) -> Optional[JsonDict]:
    phases = state.get("phases")
    if not isinstance(phases, dict):
        return None
    phase_state = phases.get(str(phase_id))
    return phase_state if isinstance(phase_state, dict) else None


def _string_list(value: Any) -> List[str]:
    return field_names_from_specs(value)


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _unique_paths(values: List[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _append_unique(target: List[Any], values: List[Any]) -> None:
    seen = {str(item) for item in target}
    for value in values:
        key = str(value)
        if key not in seen:
            target.append(value)
            seen.add(key)
