"""
harness.task_control.replan - Replan checkpoint recording and reconciliation.
"""

from __future__ import annotations

import json
import copy
import hashlib
from pathlib import Path
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import load_task_json
from harness.utils import task_file_exists

def _tc():
    import harness.task_control as tc

    return tc

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

    state = _tc().load_task_state(logger)
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
        if checkpoint.get("sourceLedgerBound") is True:
            source_path = str(checkpoint.get("sourceArtifactPath") or "").strip()
            resolved = str(Path(source_path).expanduser().resolve()) if source_path else ""
            if (
                not resolved
                or resolved not in ledger_paths
                or not task_file_exists(logger, resolved)
            ):
                reason = "source_artifact_missing"
                next_instruction = (
                    "The validated source artifact disappeared from the ledger."
                    " Re-run its upstream producer before creating a fresh probe."
                )
            else:
                try:
                    payload = load_task_json(logger, resolved) or {}
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
        checkpoint["invalidatedAt"] = _tc().utc_now_iso()
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
        _tc().write_task_state(logger, state)
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

    state = _tc().load_task_state(logger)
    phase_id = str(phase.get("id") or contract.get("phase_id") or "")
    phase_state = _tc()._phase_state(state, phase_id)
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
    computed_cohort_key = _tc()._fast_path_cohort_key(phase, contract, cohort_receipt)
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
        "businessContract": _tc()._canonical_fast_path_business_contract(
            phase, contract,
        ),
        "businessContractSignature": (
            _tc()._fast_path_business_contract_signature(phase, contract)
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
        "objectiveFingerprint": _tc().objective_fingerprint(phase, contract),
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
    _tc().write_task_state(logger, state)
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
    cohort_key = _tc()._fast_path_cohort_key(phase, worker_contract, receipt)
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
    actual_business = _tc()._canonical_fast_path_business_contract(
        phase, worker_contract,
    )
    if expected_business:
        reasons.extend(
            _tc()._fast_path_business_contract_fence_errors(
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
    actual_business_signature = _tc()._fast_path_business_contract_signature(
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
    declared_dependencies = _tc()._phase_dependency_ids(phase)
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
        next_objective = _tc().objective_fingerprint(phase, worker_contract)
        if next_objective:
            checkpoints = _replan_checkpoint_map(state)
            checkpoint_key = str(checkpoint.get("cohortKey") or "").strip()
            stored = checkpoints.get(checkpoint_key)
            if isinstance(stored, dict):
                stored["nextObjectiveFingerprint"] = next_objective
                state["replan_checkpoints"] = checkpoints
                state.pop("replan_checkpoint", None)
                _tc().write_task_state(logger, state)
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
            declared_dependencies = _tc()._phase_dependency_ids(phase)
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
            actual_business = _tc()._canonical_fast_path_business_contract(
                phase,
                dict(contract),
            )
            for reason in _tc()._fast_path_business_contract_fence_errors(
                expected_business,
                actual_business,
            ):
                errors.append(f"checkpoint {checkpoint_id!r}: {reason}")
        expected_selector = _tc()._canonical_cohort_selector(
            checkpoint.get("cohortSelector")
        )
        actual_selector = _tc()._canonical_cohort_selector(
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
            if _tc()._cohort_selectors_provably_disjoint(
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
