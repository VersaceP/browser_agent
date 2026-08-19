"""Canonical direct contract: the trusted-source single-phase fast path.

FINAL ARCHITECTURE (decided; shadowed now, adopted after the shadow audit):

    load + validate the trusted contract (operator-configured, versioned)
      -> verify contract.original_task == the run's original_user_task
      -> the harness ADOPTS contract.plan DIRECTLY as the accepted plan
      -> the Lead starts at spawn; no emit_task_plan round, no Plan
         Validator call, and never the contradiction of asking an untrusted
         Lead to reproduce a trusted plan verbatim.

Until adoption is enabled, two NON-BLOCKING shadows run on the normal flow:

    canonical_direct.contract_shadow  could this contract have been adopted
                                      directly for this run? (task binding +
                                      plan validity under the same schema
                                      parameters the candidate sees)
    canonical_direct.spawn_shadow     at spawn, which execution/routing
                                      fields the Lead passed that the trusted
                                      plan does not carry

Contract format v2 (incompatible with the v1 field-set prototype: the plan
is now embedded whole):

    original_task        immutable user task text; MUST equal the run's
                         original_user_task
    plan                 a COMPLETE raw v1 plan, normalized with the SAME
                         validate_task_plan() parameters as candidates
    side_effect_policy   strict schema {"declared_effects": [...]} from a
                         fixed vocabulary; empty objects and unknown fields
                         rejected

Validation is two-stage on purpose: validate_contract_shape() runs at
LeadAgent init (before the schema cache exists); normalize_contract() runs
at review time with the exact known-method set the candidate gets, so
method allowlists can never be judged under two different rule sets.

This module is pure code: no LLM, no IO, no state mutation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, List, Optional, Tuple

JsonDict = dict

ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"
UNREPLAYABLE = "unreplayable"

_CONTRACT_VERSION = "v2"

# Fixed vocabulary for side_effect_policy.declared_effects. Runtime gates
# (spawn guard, future action gates) key off these exact strings; free text
# here would be audit-decor, not policy.
ALLOWED_SIDE_EFFECTS = frozenset({
    "navigation",
    "form_submission",
    "file_upload",
    "file_download",
    "authentication",
    "payment",
    "data_deletion",
})


def _stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def contract_identity_hash(contract: JsonDict) -> str:
    """Stable identity of a VALIDATED contract for receipts/events."""
    public = {
        key: value for key, value in contract.items()
        if not key.startswith("_")
    }
    return hashlib.sha256(_stable_json(public).encode("utf-8")).hexdigest()


def task_identity_hash(original_user_task: str) -> str:
    return hashlib.sha256(
        str(original_user_task or "").encode("utf-8")
    ).hexdigest()


def validate_contract_shape(value: Any) -> Tuple[Optional[JsonDict], List[str]]:
    """Stage 1 (LeadAgent init, pre-schema-cache): structure only.

    Checks version, field presence/types, side-effect schema, unknown
    fields, and that plan is a JSON object - everything that does NOT need
    the schema cache. The embedded plan is NOT normalized here; that happens
    in normalize_contract() with the same method/tool parameters candidates
    are judged under.
    """
    if not isinstance(value, dict):
        return None, ["contract must be a JSON object"]
    errors: List[str] = []
    if str(value.get("version") or "") != _CONTRACT_VERSION:
        errors.append(f"contract.version must be {_CONTRACT_VERSION!r}")

    original_task = str(value.get("original_task") or "").strip()
    if not original_task:
        errors.append("contract.original_task is required")

    if not isinstance(value.get("plan"), dict):
        errors.append("contract.plan must be a complete v1 plan object")

    policy = value.get("side_effect_policy")
    if not isinstance(policy, dict) or not policy:
        errors.append(
            "contract.side_effect_policy must be a non-empty object with a"
            " declared_effects list"
        )
    else:
        extra = sorted(str(k) for k in policy if str(k) != "declared_effects")
        if extra:
            errors.append(
                f"contract.side_effect_policy has unknown fields: {extra}"
            )
        effects = policy.get("declared_effects")
        if not isinstance(effects, list) or not effects:
            errors.append(
                "contract.side_effect_policy.declared_effects must be a"
                " non-empty list"
            )
        else:
            unknown = sorted(
                str(e) for e in effects if str(e) not in ALLOWED_SIDE_EFFECTS
            )
            if unknown:
                errors.append(
                    "contract.side_effect_policy.declared_effects contains"
                    f" unknown effects: {unknown}; vocabulary:"
                    f" {sorted(ALLOWED_SIDE_EFFECTS)}"
                )

    unknown_top = sorted(
        str(k) for k in value
        if str(k) not in {
            "version", "original_task", "plan", "side_effect_policy",
        }
    )
    if unknown_top:
        errors.append(f"contract has unknown fields: {unknown_top}")

    if errors:
        return None, errors
    return {
        "version": _CONTRACT_VERSION,
        "original_task": original_task,
        "plan": value.get("plan"),
        "side_effect_policy": policy,
    }, []


def normalize_contract(
    shape_validated: JsonDict,
    *,
    known_abcp_methods: Optional[Any] = None,
    known_harness_tools: Optional[Any] = None,
    validate_plan=None,
) -> Tuple[Optional[JsonDict], List[str]]:
    """Stage 2 (review time, schema cache available): normalize the plan
    with the EXACT parameters candidates are validated under, so a method
    allowlist can never be judged under two different rule sets."""
    if validate_plan is None:
        from harness.task_control.plan_validation import validate_task_plan
        validate_plan = validate_task_plan
    normalized_plan, plan_errors = validate_plan(
        shape_validated["plan"],
        known_abcp_methods=known_abcp_methods,
        known_harness_tools=known_harness_tools,
        user_task=shape_validated["original_task"],
    )
    if normalized_plan is None:
        return None, [f"contract.plan invalid: {e}" for e in plan_errors]
    contract = dict(shape_validated)
    contract["_normalized_plan"] = normalized_plan
    return contract, []


def validate_canonical_direct_contract(
    value: Any,
    *,
    known_abcp_methods: Optional[Any] = None,
    known_harness_tools: Optional[Any] = None,
    validate_plan=None,
) -> Tuple[Optional[JsonDict], List[str]]:
    """Convenience wrapper: shape + normalize in one call (tests, tools)."""
    shaped, errors = validate_contract_shape(value)
    if shaped is None:
        return None, errors
    return normalize_contract(
        shaped,
        known_abcp_methods=known_abcp_methods,
        known_harness_tools=known_harness_tools,
        validate_plan=validate_plan,
    )


def classify_canonical_direct_eligibility(
    plan: Any,
    *,
    contract: Optional[JsonDict],
    original_user_task: str,
    runtime_state: Optional[JsonDict] = None,
) -> JsonDict:
    """Pure-code eligibility for the canonical direct fast path.

    runtime_state (all optional, all mechanical):
      resume_active / has_accepted_plan / pending_hitl / pending_challenge
    """
    reasons: List[str] = []

    if not isinstance(plan, dict) or not isinstance(plan.get("phases"), list):
        return {"eligibility": UNREPLAYABLE, "reasons": ["plan_shape"]}

    phases = plan["phases"]

    # --- structural, contract-independent ---------------------------------
    if len(phases) != 1:
        reasons.append("multi_phase")
    if plan.get("replan_checkpoint_ids") or plan.get("replan_checkpoint_id"):
        reasons.append("replan_checkpoints")
    if plan.get("pacing"):
        reasons.append("pacing_not_allowed")

    phase = phases[0] if phases and isinstance(phases[0], dict) else {}
    if not phase:
        if len(phases) == 1:
            return {"eligibility": UNREPLAYABLE, "reasons": ["plan_shape"]}
    if phase.get("depends_on"):
        reasons.append("dependencies")
    if phase.get("fanout_from") or phase.get("join"):
        reasons.append("fanout_join")
    worker_contract = phase.get("worker_contract")
    worker_contract = worker_contract if isinstance(worker_contract, dict) else {}
    for key in (
        "cohort_source", "row_selection", "batch_source", "batch_rows",
        "batch_policy", "batch_rows_provenance", "replan_checkpoint_id",
    ):
        if key in worker_contract:
            reasons.append("batch_cohort")
            break
    if phase.get("pacing"):
        reasons.append("phase_pacing_not_allowed")
    if str(phase.get("context") or "").strip():
        reasons.append("context_not_allowed")

    # --- runtime state -----------------------------------------------------
    state = runtime_state if isinstance(runtime_state, dict) else {}
    if state.get("resume_active"):
        reasons.append("resume")
    if state.get("has_accepted_plan"):
        reasons.append("replan_not_initial_plan")
    if state.get("pending_hitl"):
        reasons.append("pending_hitl")
    if state.get("pending_challenge"):
        reasons.append("pending_challenge")

    # --- contract presence ---------------------------------------------------
    if contract is None:
        reasons.append("no_canonical_contract")
        return {"eligibility": INELIGIBLE, "reasons": reasons}
    if "_normalized_plan" not in contract:
        reasons.append("invalid_contract")
        return {"eligibility": INELIGIBLE, "reasons": reasons}

    # --- binding to THIS run's user task ------------------------------------
    # The contract path is global config; without this check a contract left
    # over from a previous task could grant eligibility to a plan matching
    # the OLD task while a NEW task runs.
    if str(original_user_task or "").strip() != str(
        contract.get("original_task") or ""
    ):
        reasons.append("contract_task_mismatch")

    # --- the ONE equivalence: normalized candidate == normalized trusted plan.
    # Covers every field - goal, objective, worker_task, worker_contract
    # values (session_key!), allowed/forbidden methods, stage hints, pacing,
    # max_steps, validators. A single mismatch is a single reason code; no
    # field blacklist to drift.
    candidate_cmp = {
        key: value for key, value in plan.items() if key != "warnings"
    }
    contract_cmp = {
        key: value
        for key, value in contract["_normalized_plan"].items()
        if key != "warnings"
    }
    if _stable_json(candidate_cmp) != _stable_json(contract_cmp):
        reasons.append("plan_not_contract_plan")

    if reasons:
        return {"eligibility": INELIGIBLE, "reasons": reasons}
    return {"eligibility": ELIGIBLE, "reasons": []}
