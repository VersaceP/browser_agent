"""
harness.task_control.plan_validation - Task plan validation, acceptance and state initialization.
"""

from __future__ import annotations

import json
import copy
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import AbstractSet
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from harness.observation.content_completeness import content_completeness_config_errors
from harness.observation.content_completeness import normalize_content_completeness_config
from harness.evidence.extraction_artifacts import field_name_from_spec
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.evidence.artifact_evidence import FILE_VALIDATOR_TYPES
from harness.evidence.artifact_evidence import _BLOCKER_TEMPLATE_SEARCH_RE
from harness.evidence.artifact_evidence import _PLACEHOLDER_LITERAL_RE
from harness.evidence.artifact_evidence import _business_fields_from_expected
from harness.evidence.artifact_evidence import _normalized_semantic_token
from harness.fleet.auth import normalize_auth_verification_contract
from harness.fleet.coordinator import normalize_page_policy
from harness.fleet.coordinator import normalize_reuse_scope
from harness.storage.base import SNAPSHOT_KEY_CURRENT_PLAN
from harness.storage.base import SNAPSHOT_KEY_TASK_STATE
from harness.pacing import MAX_PACING_INTERVAL_SECONDS
from harness.pacing import PACING_FIELDS
from harness.pacing import PACING_INTERVAL_FIELDS
from harness.pacing import normalized_pacing
from harness.task_types import VALID_TASK_TYPES
from harness.task_types import normalize_task_type
from harness.task_types import resolve_task_type_fail_closed
from harness.task_types import task_type_choices_for_error
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import contains_affirmative_semantic_marker
from harness.utils import contains_semantic_marker
from harness.utils import safe_path_component
from harness.utils import storage_for_logger

def _tc():
    import harness.task_control as tc

    return tc

TASK_PLAN_FILE = "task_plan.json"

TASK_STATE_FILE = "task_state.json"

_TASK_STATE_WRITE_LOCK = threading.RLock()

class _PathOnlyLogger:
    """Minimal stand-in for callers that only have a task directory.

    Resolves to the file backend, which is the correct answer when there is no
    logger to inherit a database connection from.
    """

    def __init__(self, task_dir: Path) -> None:
        self.task_dir = task_dir
        self.task_id = task_dir.name

class _TaskStateSnapshot(dict):
    """A dict carrying its read baseline for optimistic three-way writes."""

    def __init__(self, value: Optional[JsonDict] = None) -> None:
        super().__init__(value or {})
        self._task_state_base = copy.deepcopy(dict(self))
        self._task_state_replace = False
        # Revision the value was read at; 0 means "no row/file yet", which the
        # backend treats as an insert rather than a lost compare-and-swap.
        self._task_state_revision = 0

SEMANTIC_TERMINAL_CLASSIFICATIONS = frozenset({
})

TERMINAL_PHASE_STATUSES = frozenset({
    "validated_done",
    "phase_failed",
    "blocked_by_challenge",
    "hitl_required",
    "hitl_timeout",
    "page_settled_after_hitl",
    "stale_pause_deadlock",
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

REPLAN_RESET_STATUSES = frozenset({"phase_failed", "blocked_by_dependency"})

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

VALIDATOR_TYPE_ALIASES = {
    "url_format": "url_pattern",
    "rank_range": "range",
    "value_range": "range",
    "no_duplicates": "unique",
    "unique_fields": "unique",
}

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
        dependencies = _tc()._phase_dependency_ids(transition)
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
            for item in _tc()._SOURCE_URL_RE.findall(str(user_task or ""))
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
    declared = _tc()._phase_dependency_ids(phases[index])
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
        declared_deps = _tc()._phase_dependency_ids(phase)
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
        minimum = _tc()._fingerprint_num(validator.get("min"))
        maximum = _tc()._fingerprint_num(validator.get("max"))
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
        "sources": _tc()._normalized_source_urls(
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
    if task_type:
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
        worker_task = str(
            raw_phase.get("worker_task")
            or raw_phase.get("task")
            or objective
        ).strip()
        if not objective:
            errors.append(f"phase {phase_id}: objective is required")

        stage_hint = str(raw_phase.get("stage_hint") or "generic").strip()
        if stage_hint not in VALID_STAGE_HINTS:
            errors.append(
                f"phase {phase_id}: stage_hint must be one of"
                f" {sorted(VALID_STAGE_HINTS)}; got {stage_hint!r}"
            )
        stage_hint_reason = str(raw_phase.get("stage_hint_reason") or "").strip()

        expected_artifact = raw_phase.get("expected_artifact") or {}
        if expected_artifact is not None and not isinstance(expected_artifact, dict):
            errors.append(f"phase {phase_id}: expected_artifact must be an object")
            expected_artifact = {}

        validators = raw_phase.get("validators") or []
        if validators is not None and not isinstance(validators, list):
            errors.append(f"phase {phase_id}: validators must be an array")
            validators = []
        expected_artifact = _tc()._normalize_expected_artifact_contract(
            expected_artifact if isinstance(expected_artifact, dict) else {},
            validators,
            errors,
            warnings,
            phase_id=phase_id,
        )
        validators = _tc()._normalize_validators(
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
            "depends_on": _tc()._normalized_depends_on(raw_phase.get("depends_on")),
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
            # Optional, explicitly-declared resource budget.  The harness must
            # not invent a default phase retry wall.
            "max_attempts": (
                _tc()._positive_int(raw_phase.get("max_attempts"), default=1)
                if raw_phase.get("max_attempts") is not None
                else None
            ),
        })

    # depends_on must reference declared phase ids: an unknown id resolves to
    # dependency_not_ready (non-blocking) at schedule time, so the phase would
    # be skipped forever with no signal — reject the plan instead.
    for phase in phases:
        phase_id = str(phase.get("id"))
        for dep_id in _tc()._phase_dependency_ids(phase) or []:
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

    if not task_type:
        phase_task_types = {
            str(phase.get("task_type") or "") for phase in phases
            if str(phase.get("task_type") or "")
        }
        task_type = (
            next(iter(phase_task_types))
            if len(phase_task_types) == 1
            else "general"
        )

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
        validators = _tc()._normalize_validators(
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
        "allowed_methods": _tc()._string_list(contract.get("allowed_methods")),
        "forbidden_methods": _tc()._string_list(contract.get("forbidden_methods")),
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
    """Publish the current plan.

    Accepting a plan always rebuilds the whole document, so this is a replace
    rather than a merge: three-way merging here would resurrect phases that a
    replan deliberately removed.
    """

    path = logger.task_dir / TASK_PLAN_FILE
    storage, task_id = storage_for_logger(logger)
    storage.save_snapshot(
        task_id=task_id,
        snapshot_key=SNAPSHOT_KEY_CURRENT_PLAN,
        base=None,
        proposed=plan,
        updated_run_id=str(getattr(logger, "run_id", "") or ""),
        replace=True,
    )
    logger.write("task_plan.accepted", {"path": str(path.resolve()), "phaseCount": len(plan.get("phases", []))})
    return str(path.resolve())

def accept_task_plan(
    logger: RunLogger,
    plan: JsonDict,
    *,
    previous_plan: Optional[JsonDict],
    replan_reason: str,
    user_task: str,
    validator_review: Optional[JsonDict],
    preserve_from: Optional[JsonDict] = None,
    extension_decision: Optional[JsonDict] = None,
) -> Tuple[str, JsonDict, JsonDict]:
    """Publish one plan generation: version record, alias and reset state.

    These three were written as three independent transactions, so a crash
    between them left a plan and a state disagreeing about which phases exist.
    The state is computed first and handed to the backend, which commits the
    whole generation at once where it can.
    """

    from harness.planning.validator import build_plan_version_record

    record = build_plan_version_record(
        plan=plan,
        previous_plan=previous_plan,
        replan_reason=replan_reason,
        user_task=user_task,
        validator_review=validator_review,
    )
    if extension_decision is not None and isinstance(preserve_from, dict):
        # Recorded before the state is built so it lands inside the same
        # generation; the version number it cites is stamped at commit time.
        audit_resumes = preserve_from.get("resumes")
        if (
            isinstance(audit_resumes, list)
            and audit_resumes
            and isinstance(audit_resumes[-1], dict)
        ):
            decisions = audit_resumes[-1].setdefault("extensionDecisions", [])
            if isinstance(decisions, list):
                decisions.append(dict(extension_decision))
    state = initialize_task_state(
        logger,
        plan,
        preserve_from=preserve_from,
        replan_reason=replan_reason,
        plan_version=record,
        persist=False,
    )
    storage, task_id = storage_for_logger(logger)
    stored, persisted = storage.commit_accepted_plan(
        task_id=task_id,
        run_id=str(getattr(logger, "run_id", "") or ""),
        plan_record=record,
        current_plan=plan,
        task_state=dict(state),
        # The listing summary is refreshed on every state write; accepting a
        # plan writes state through the aggregate instead, so it is refreshed
        # here - inside the same transaction, because a summary quoting a plan
        # version that rolled back would be worse than a stale one.
        summarize=_tc().task_state_summary,
    )
    state.clear()
    state.update(copy.deepcopy(persisted))
    if isinstance(state, _TaskStateSnapshot):
        state._task_state_base = copy.deepcopy(persisted)
        state._task_state_replace = False
    path = str((logger.task_dir / TASK_PLAN_FILE).resolve())
    version_summary = {
        "planVersion": stored["planVersion"],
        "path": str(stored.get("path") or ""),
        "planHash": stored["planHash"],
        "previousVersion": stored["previousVersion"],
        "replanReason": stored["replanReason"],
        "diffCount": len(stored.get("diff") or []),
    }
    logger.write("task_plan.accepted", {
        "path": path, "phaseCount": len(plan.get("phases", [])),
    })
    logger.write("task_plan.versioned", version_summary)
    return path, version_summary, state

def initialize_task_state(
    logger: RunLogger,
    plan: JsonDict,
    *,
    preserve_from: Optional[JsonDict] = None,
    replan_reason: str = "",
    plan_version: Optional[JsonDict] = None,
    persist: bool = True,
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
                phases_state[phase_id] = _tc()._empty_phase_state()
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
                _tc()._ensure_phase_state_defaults(preserved)
                phases_state[phase_id] = preserved
                if replan_audit is not None:
                    replan_audit["preserved_phases"].append({
                        "phaseId": phase_id,
                        "status": preserved.get("status"),
                        "attemptCount": len(preserved.get("attempts") or []),
                    })
        else:
            phases_state[phase_id] = _tc()._empty_phase_state()
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
        "current_phase": _tc()._first_active_phase_id(plan, phases_state),
        "phases": phases_state,
        "artifacts": list((preserve_from or {}).get("artifacts") or []),
        "completed_items": list((preserve_from or {}).get("completed_items") or []),
        "pending_items": list((preserve_from or {}).get("pending_items") or []),
        "failed_items": list((preserve_from or {}).get("failed_items") or []),
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
            _tc()._replan_checkpoint_map(preserve_from or {})
        ),
        # Resume/runtime audit ledgers are task-scoped and must survive the
        # state reconstruction performed by every accepted replan.
        "resumes": copy.deepcopy((preserve_from or {}).get("resumes") or []),
        "browser_context": copy.deepcopy(
            (preserve_from or {}).get("browser_context") or {}
        ),
        "completion_receipts": copy.deepcopy(
            (preserve_from or {}).get("completion_receipts") or {}
        ),
        "artifact_digests": copy.deepcopy(
            (preserve_from or {}).get("artifact_digests") or {}
        ),
        "artifact_supersessions": copy.deepcopy(
            (preserve_from or {}).get("artifact_supersessions") or []
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
    # A plan acceptance deliberately reconstructs the complete state shape.
    # It must not three-way merge removed phases back from an older snapshot.
    if persist:
        _tc().write_task_state(logger, state, replace=True)
    logger.write(
        "task_state.initialized",
        {
            "path": str(_tc()._state_path(logger).resolve()),
            "preserved": preserve_from is not None,
            "replanReason": replan_reason or None,
        },
    )
    return state

def load_task_state(logger: RunLogger) -> JsonDict:
    """Read current state, remembering the baseline it was read at.

    "Never written" and "written empty" stay indistinguishable here, exactly
    as when this read a file directly: every caller downstream is written
    against that assumption.
    """

    storage, task_id = storage_for_logger(logger)
    with _TASK_STATE_WRITE_LOCK:
        value, revision = storage.load_snapshot(
            task_id=task_id,
            snapshot_key=SNAPSHOT_KEY_TASK_STATE,
        )
        snapshot = _TaskStateSnapshot(value if isinstance(value, dict) else {})
        snapshot._task_state_revision = int(revision or 0)
        return snapshot
