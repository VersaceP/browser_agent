"""
harness.task_control - Task plans, worker contracts, task state, and artifact validators.
"""

from __future__ import annotations

import json
import copy
import hashlib
import re
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from harness.constants import (
    WORKER_STATUS_API_CONTRACT_ERROR,
    WORKER_STATUS_BLOCKED_BY_CHALLENGE,
    WORKER_STATUS_HITL_REQUIRED,
    WORKER_STATUS_HITL_TIMEOUT,
    WORKER_STATUS_HITL_WAITING,
    WORKER_STATUS_PAGE_CRASHED,
    WORKER_STATUS_PAGE_SETTLED_AFTER_HITL,
)
from harness.extraction_artifacts import field_name_from_spec, field_names_from_specs
from harness.task_types import (
    VALID_TASK_TYPES,
    normalize_task_type,
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
    "blocked_by_dependency",
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

VALIDATOR_TYPES = {
    "artifact_required",
    "required_fields",
    "field_nonempty",
    "min_rows",
    "max_rows",
    "exact_rows",
    "unique",
    "url_pattern",
    "allowed_domain",
    "set_equals",
    "range",
    "field_pattern",
    "cross_field_contains",
    "action_outcome",
    "field_provenance",
}

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
_AUTH_PROBE_FIELD_MARKERS = frozenset({
    "gatetype", "gateevidence", "authrequired", "authenticationrequired",
    "loginrequired", "requireslogin", "requiresauth", "authsurface",
    "loginsurface", "authevidence", "loginevidence",
    "nextphaserequireshitl",
})


def _normalized_semantic_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


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


def validate_task_plan(
    raw_plan: Any,
    *,
    known_abcp_methods: Optional[AbstractSet[str]] = None,
    known_harness_tools: Optional[AbstractSet[str]] = None,
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
    goal = str(raw_plan.get("goal") or "").strip()
    if not goal:
        errors.append("goal is required")

    task_type = str(raw_plan.get("task_type") or "").strip()
    if not task_type:
        errors.append(
            "task_type is required; use an explicit value such as web_scrape,"
            " form_filling, file_download, file_upload, web_search, or general"
        )
        task_type = "general"
    else:
        task_type = _validated_task_type(
            task_type, errors=errors, warnings=warnings, where="task_type",
        )

    raw_phases = raw_plan.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        errors.append("phases must be a non-empty array")
        raw_phases = []

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

        # phase_contract consumes phase.task_type (contract > phase > plan),
        # but normalization used to drop it silently — a per-phase override
        # the model emitted at the sanctioned granularity simply vanished
        # (review P2). Preserve it, validated.
        phase_task_type = _validated_task_type(
            raw_phase.get("task_type"),
            errors=errors,
            warnings=warnings,
            where=f"phase {phase_id}: task_type",
        )

        phases.append({
            "id": phase_id,
            "type": phase_type,
            "task_type": phase_task_type or None,
            "objective": objective,
            "worker_task": worker_task,
            "stage_hint": stage_hint,
            "stage_hint_reason": stage_hint_reason,
            "context": str(raw_phase.get("context") or ""),
            "max_steps": raw_phase.get("max_steps"),
            # None (omitted) and [] (explicitly independent) mean DIFFERENT
            # schedules — `or []` used to collapse both into [], erasing the
            # planner's only syntax for parallel phases (task 2ed5a466).
            "depends_on": _normalized_depends_on(raw_phase.get("depends_on")),
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

    _reject_serial_auth_handoff(phases, errors)

    normalized = {
        "version": "v1",
        "goal": goal,
        "task_type": task_type,
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
    """First candidate that normalizes to a KNOWN task_type ('general' when
    none does). The policy layer is a dict lookup that fail-opens on unknown
    values — an unknown task_type disables NOTHING — so garbage at a
    higher-precedence level must fall through to the validated level below
    it instead of reaching the policy (review: a spawn override typo
    'scraping' re-enabled Download.* on a web_scrape phase; phase_contract
    has no error channel, so it degrades instead of rejecting — the spawn
    tool boundary rejects loud)."""
    for candidate in candidates:
        if candidate is None or str(candidate).strip() == "":
            continue
        canonical = normalize_task_type(candidate)
        if canonical in VALID_TASK_TYPES:
            return canonical
    return "general"


def phase_contract(
    phase: JsonDict,
    override: Optional[JsonDict] = None,
    *,
    default_task_type: str = "general",
) -> JsonDict:
    contract: JsonDict = dict(phase.get("worker_contract") or {})
    if override:
        contract.update(override)

    expected_artifact = dict(phase.get("expected_artifact") or {})
    if contract.get("expected_artifact"):
        merged = dict(expected_artifact)
        merged.update(contract.get("expected_artifact") or {})
        expected_artifact = merged

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

    payload: JsonDict = {
        "version": "v1",
        "phase_id": phase_id,
        "task_type": _first_valid_task_type(
            contract.get("task_type"),
            phase.get("task_type"),
            default_task_type,
        ),
        "stage_hint": str(contract.get("stage_hint") or phase.get("stage_hint") or "generic"),
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
            else True
        ),
        "stop_condition": str(
            contract.get("stop_condition")
            or "Record the required extraction artifact, then call final_answer."
        ),
    }
    # Pass through skill-selection fields the LeadAgent set on the worker_contract.
    # phase_contract otherwise rebuilds a fixed-field payload, which silently
    # dropped these — so an explicit skill_id/skill_variables (select) or
    # skill_selection={"use_skill": false} (decline) never reached the dispatch
    # gate and spawn_browser_agent kept re-returning skill_selection_required
    # (an unbreakable loop for the Lead). Preserve them verbatim when present.
    for skill_key in ("skill_id", "skill_variables", "skill_rows", "skill_selection", "domain"):
        value = contract.get(skill_key)
        if value is not None:
            payload[skill_key] = value
    if validator_errors:
        payload["contract_warnings"] = validator_errors
    return payload


def write_task_plan(logger: RunLogger, plan: JsonDict) -> str:
    path = logger.task_dir / TASK_PLAN_FILE
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.write("task_plan.accepted", {"path": str(path.resolve()), "phaseCount": len(plan.get("phases", []))})
    return str(path.resolve())


def initialize_task_state(
    logger: RunLogger,
    plan: JsonDict,
    *,
    preserve_from: Optional[JsonDict] = None,
    replan_reason: str = "",
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
    }
    if preserve_from is not None:
        state["replans"] = list((preserve_from or {}).get("replans") or [])
        state["replans"].append(replan_audit or {})
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
        "taskType": str(
            worker_contract.get("task_type")
            or phase.get("task_type")
            or "general"
        ),
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

    if result_status in {
        "blocked_by_challenge",
        "hitl_required",
        "hitl_timeout",
        "page_settled_after_hitl",
        "stale_pause_deadlock",
    }:
        phase_state["status"] = result_status
        phase_state["last_failure"] = [{
            "type": "challenge_blocker",
            "status": result_status,
            "message": (
                "Worker reported a challenge/HITL blocker or stale pause"
                " deadlock; do not retry this phase with the same browser"
                " strategy without user action or a deliberate pivot."
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
    if attempts_count >= max_attempts and status in {
        "validation_failed",
        "failed",
        "cancelled",
        "unknown",
    }:
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
        if attempts_count < max_attempts or status not in {
            "validation_failed",
            "failed",
            "cancelled",
            "unknown",
        }:
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
        if attempts_count >= max_attempts and status in {
            "validation_failed",
            "failed",
            "cancelled",
            "unknown",
        }:
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
) -> JsonDict:
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

    must_record = bool(contract.get("must_record_extraction", True))
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
        for validator in validators:
            cand_failures.extend(_run_validator(validator, cand_rows))
        cand_failures.extend(_detect_placeholder_rows(cand_rows))
        cand_failures.extend(_detect_stub_rows(cand_rows, expected))
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
    warnings = _detect_near_stub_rows(rows, expected)

    cumulative = False
    cumulative_sources: List[str] = []
    if failures:
        cumulative_rows, cumulative_sources, cumulative_failures = (
            _validate_cumulative_artifacts(
                validators=validators,
                expected=expected,
                candidates=[
                    *prior_candidates,
                    *attempt_candidates,
                    *candidates,
                ],
            )
        )
        if cumulative_rows and not cumulative_failures:
            rows = cumulative_rows
            failures = []
            warnings = _detect_near_stub_rows(rows, expected)
            cumulative = True

    status = "done" if not failures else "failed"
    result_artifacts = cumulative_sources if cumulative else (
        [selected.get("path")] if selected else []
    )
    valid_extraction_artifacts = cumulative_sources if cumulative else (
        [selected.get("path")] if selected and not failures else []
    )
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


PLACEHOLDER_TEXT_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"^\s*loading(?:\.\.\.)?\s*$",
        r"^\s*(?:<\s*)?placeholder(?:\s*>)?\s*$",
        r"\bplaceholder\b",
        r"^\s*be the first to\b.*$",
        r"^\s*sign in to (?:view|continue|see)\b.*$",
        r"^\s*ask a question\s*$",
        r"^\s*no data(?: available)?\s*$",
        r"^\s*no reviews? yet\s*$",
        r"^\s*no comments? yet\s*$",
        r"^\s*coming soon\s*$",
        r"^\s*nothing (?:here|found)\s*$",
        # Failure-as-data: a worker writing WHY it could not get the value (EN/zh)
        # instead of the value itself. Anchored bare-absence values, plus
        # iframe/main-DOM meta-commentary that never appears in real field data.
        r"^\s*(?:n/?a|none|null|nil|unknown|unavailable|not (?:found|available|provided|specified|shown|displayed|captured|obtained|extracted))\s*\.?\s*$",
        r"(?:located in|present in|inside|within)\s+(?:an?\s+)?iframe",
        r"位于\s*iframe|iframe\s*(?:中|内|里)|嵌(?:套|入)在?\s*iframe",
        r"主\s*dom\s*(?:未|中未|没有|不包含)|not (?:directly )?(?:in|present in|contained in) the (?:main )?dom",
        r"^\s*(?:未(?:获取|提供|找到|明确|展示|提取|包含|显示|抓取|呈现)|无法(?:获取|提取|访问|抓取|读取)|暂无(?:数据|内容|信息)?|无(?:数据|内容|此信息|相关信息)|未知|不适用|页面(?:未|没有)(?:明确|直接|展示))",
    )
)


def _detect_placeholder_rows(rows: List[JsonDict]) -> List[JsonDict]:
    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        if _row_self_reports_placeholder(row):
            bad.append({
                "type": "data_placeholder",
                "row": index,
                "reason": "row_self_reported_placeholder",
            })
            continue
        matched_fields: List[JsonDict] = []
        for field, value in row.items():
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > 120:
                continue
            pattern = _placeholder_pattern(text)
            if pattern:
                matched_fields.append({
                    "field": str(field),
                    "value": text[:120],
                    "pattern": pattern,
                })
        if matched_fields:
            bad.append({
                "type": "data_placeholder",
                "row": index,
                "fields": matched_fields[:5],
            })
    return bad[:20]


def _detect_stub_rows(rows: List[JsonDict], expected_artifact: JsonDict) -> List[JsonDict]:
    array_fields = _expected_array_fields(expected_artifact)
    if not array_fields:
        return []
    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        present = [field for field in array_fields if field in row]
        if not present:
            continue
        empty = [
            field for field in present
            if isinstance(row.get(field), list) and len(row.get(field) or []) == 0
        ]
        if len(empty) < max(2, len(present)):
            continue
        if _row_has_blocker_explanation(row):
            continue
        bad.append({
            "type": "data_stub",
            "row": index,
            "emptyArrayFields": empty,
            "reason": "detail-like row has empty arrays without blocker or absence note",
        })
    return bad[:20]


def _detect_near_stub_rows(rows: List[JsonDict], expected_artifact: JsonDict) -> List[JsonDict]:
    array_fields = _expected_array_fields(expected_artifact)
    if len(array_fields) < 3:
        return []
    warnings: List[JsonDict] = []
    for index, row in enumerate(rows):
        present = [field for field in array_fields if field in row]
        if len(present) < 3:
            continue
        empty = [
            field for field in present
            if isinstance(row.get(field), list) and len(row.get(field) or []) == 0
        ]
        if len(empty) < max(2, len(present) - 1):
            continue
        if len(empty) >= len(present):
            continue
        if _row_has_blocker_explanation(row):
            continue
        non_empty = [
            field for field in present
            if field not in empty
        ]
        warnings.append({
            "type": "near_stub_row",
            "row": index,
            "emptyArrayFields": empty,
            "nonEmptyArrayFields": non_empty,
            "reason": "most detail array fields are empty; verify this is real page absence, not padding",
        })
    return warnings[:20]


def _expected_array_fields(expected_artifact: JsonDict) -> List[str]:
    raw_fields = expected_artifact.get("fields")
    if not isinstance(raw_fields, list):
        return []
    out: List[str] = []
    for field in raw_fields:
        if not isinstance(field, dict):
            continue
        name = field_name_from_spec(field)
        field_type = str(field.get("type") or "").strip().lower()
        if name and field_type == "array":
            out.append(name)
    return out


def _row_has_blocker_explanation(row: JsonDict) -> bool:
    for key in (
        "_note",
        "note",
        "notes",
        "blocker",
        "blockers",
        "status",
        "reason",
        "absence_reason",
        "missing_reason",
    ):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
    absence_markers = (
        "absent",
        "empty",
        "missing",
        "no ",
        "none",
        "not visible",
        "not shown",
        "unavailable",
        "未显示",
        "没有",
        "无",
    )
    for key, value in row.items():
        if not str(key).endswith("EvidenceText"):
            continue
        text = str(value or "").strip().lower()
        if text and any(marker in text for marker in absence_markers):
            return True
    return False


def _row_self_reports_placeholder(row: JsonDict) -> bool:
    for key in (
        "placeholderDetected",
        "placeholder_detected",
        "isPlaceholder",
        "is_placeholder",
        "dataPlaceholder",
        "data_placeholder",
    ):
        if row.get(key) is True:
            return True
        value = row.get(key)
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
            return True
    return False


def _placeholder_pattern(text: str) -> str:
    for pattern in PLACEHOLDER_TEXT_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return ""


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
            empty = [
                field for field in fields
                if _is_empty_value(row.get(field))
            ]
            if empty:
                failures.append({"type": validator_type, "row": index, "empty": empty})
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
    seen = set()
    for validator in validators:
        signature = _validator_semantic_signature(validator)
        if signature in seen:
            if warnings is not None:
                warnings.append({
                    "type": "duplicate_validator_dropped",
                    "phase": phase_id,
                    "validatorType": str(validator.get("type") or ""),
                })
            continue
        seen.add(signature)
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
            normalized.append({"type": "field_nonempty", "fields": nonempty_fields})
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
) -> Tuple[List[JsonDict], List[str], List[JsonDict]]:
    rows_by_key: Dict[str, JsonDict] = {}
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

    source_paths = _unique_paths(source_paths)
    if len(source_paths) < 2:
        return [], source_paths, schema_failures

    rows = list(rows_by_key.values())
    failures: List[JsonDict] = []
    for validator in validators:
        failures.extend(_run_validator(validator, rows))
    failures.extend(_detect_placeholder_rows(rows))
    failures.extend(_detect_stub_rows(rows, expected))
    return rows, source_paths, [*schema_failures, *failures]


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
    row_failures.extend(_detect_placeholder_rows([row]))
    row_failures.extend(_detect_stub_rows([row], expected))

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


def _cumulative_row_key(row: JsonDict, expected: JsonDict) -> str:
    fields = field_names_from_specs(
        expected.get("required_fields") or expected.get("fields") or []
    )
    # url-class keys first: a rank/position only identifies a row within one
    # source page, so re-scrapes from a different surface could collide on
    # rank while pointing at different products.
    priority_fields = [
        "url",
        "href",
        "detailUrl",
        "detail_url",
        "productUrl",
        "product_url",
        "rank",
        "position",
        "name",
        "title",
    ]
    for field in [*priority_fields, *fields]:
        value = row.get(field)
        text = str(value or "").strip()
        if text:
            return f"{field}:{text.lower()}"
    canonical = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return "rowhash:" + hashlib.sha256(
        canonical.encode("utf-8", errors="replace")
    ).hexdigest()[:16]


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
