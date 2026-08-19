#!/usr/bin/env python3
"""Replay canonical-direct classification over ALL emitted plan candidates.

v3 (from external review):
- loads each run's REAL original_user_task from task_manifest.json; the v2
  "" placeholder made mechanical validation a different exercise than the
  historical task actually faced
- adds the 791e DIFFERENTIAL fixture: build a trusted contract from the
  reviewer-APPROVED plan + the manifest task, then classify the genuinely
  rejected first candidate against it - the assertion that matters is
  plan_not_contract_plan, not the trivially-true no_canonical_contract
- the production-mode "zero false positives" (contract=None) is reported as
  what it is: a structural distribution stat, NOT eligibility precision -
  precision is proven by unit fixtures and this differential.

Exit code 1 on any false positive.
"""
import glob
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness.task_control.canonical_direct import (  # noqa: E402
    validate_canonical_direct_contract,
    classify_canonical_direct_eligibility,
)
from harness.task_control.plan_validation import validate_task_plan  # noqa: E402
from harness.planning.validator import plan_candidate_hash  # noqa: E402

RUN_791E = "791e1b47bab144fe99b780b7177499e6"


def approved_candidate_hashes(run_file):
    """candidateHashes of reviewer-APPROVED candidates, correlated by hash
    rather than positional order (robust across retries/errors)."""
    hashes = set()
    for line in open(run_file):
        if '"plan_validator.approved"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        h = (d.get("payload") or {}).get("candidateHash")
        if h:
            hashes.add(str(h))
    return hashes


def run_user_task(run_dir):
    manifest = run_dir / "task_manifest.json"
    if not manifest.exists():
        return ""
    try:
        return str(json.load(open(manifest)).get("original_user_task") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def emitted_candidates(run_file):
    out = []
    for line in open(run_file):
        if '"lead.model"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "lead.model":
            continue
        for call in (d.get("payload") or {}).get("tool_calls") or []:
            if (
                isinstance(call, dict)
                and str(call.get("name")) == "emit_task_plan"
            ):
                out.append((call.get("input") or {}).get("plan"))
    return out


def main():
    totals = Counter()
    reason_hist = Counter()
    false_positives = []
    candidate_791e = None
    approved_791e_plan = None

    for run_file in sorted(glob.glob(str(ROOT / "worktree/*/run.jsonl"))):
        run = Path(run_file).parent
        run_id = run.name
        user_task = run_user_task(run)
        approved_hashes = (
            approved_candidate_hashes(run_file)
            if run_id == RUN_791E else set()
        )
        for index, raw in enumerate(emitted_candidates(run_file)):
            totals["emitted"] += 1
            if run_id == RUN_791E and index == 0:
                candidate_791e = raw
            normalized, errors = validate_task_plan(raw, user_task=user_task)
            if normalized is not None and approved_hashes:
                replan_reason = (
                    str(raw.get("replan_reason") or "").strip()
                    if isinstance(raw, dict) else ""
                )
                if plan_candidate_hash(normalized, replan_reason) in approved_hashes:
                    approved_791e_plan = raw
            if normalized is None:
                totals["mechanically_invalid_today"] += 1
                reason_hist["mechanical_invalid_current_rules"] += 1
                continue
            result = classify_canonical_direct_eligibility(
                normalized,
                contract=None,
                original_user_task=user_task,
                runtime_state={"has_accepted_plan": index > 0},
            )
            totals[f"classified_{result['eligibility']}"] += 1
            for reason in result["reasons"]:
                reason_hist[reason] += 1
            if result["eligibility"] == "eligible":
                false_positives.append((run_id, index, result["reasons"]))

    print(f"emitted candidates         : {totals['emitted']}")
    print(f"mechanically invalid today : {totals['mechanically_invalid_today']}")
    print(f"classified eligible        : {totals['classified_eligible']}"
          "  <- must stay 0 with contract=None (structural stat only)")
    print(f"classified ineligible      : {totals['classified_ineligible']}")
    print("reason histogram:")
    for reason, count in reason_hist.most_common():
        print(f"  {count:>4}  {reason}")
    if false_positives:
        print("\nFALSE POSITIVES:")
        for run_id, index, reasons in false_positives:
            print(f"  {run_id} candidate#{index}: {reasons}")
        sys.exit(1)
    print("false positives: NONE (structural distribution only - eligibility"
          " precision is proven by the differential below + unit fixtures)")

    # --- 791e differential fixture ------------------------------------------
    if candidate_791e is None or approved_791e_plan is None:
        print(f"\nFAIL: {RUN_791E} candidates incomplete")
        sys.exit(1)
    run_dir = ROOT / "worktree" / RUN_791E
    user_task = run_user_task(run_dir)
    normalized_approved, _ = validate_task_plan(
        approved_791e_plan, user_task=user_task,
    )
    if normalized_approved is None:
        print("\nFAIL: approved 791e plan fails current validation; cannot"
              " build the differential contract")
        sys.exit(1)
    contract, contract_errors = validate_canonical_direct_contract({
        "version": "v2",
        "original_task": user_task,
        "plan": approved_791e_plan,
        "side_effect_policy": {"declared_effects": ["navigation",
                                                    "form_submission"]},
    })
    if contract is None:
        print(f"\nFAIL: differential contract invalid: {contract_errors[:3]}")
        sys.exit(1)
    normalized_rejected, _ = validate_task_plan(
        candidate_791e, user_task=user_task,
    )
    if normalized_rejected is None:
        print("\n791e first candidate: mechanically invalid under current"
              " rules -> ineligible by L1; differential not applicable")
        sys.exit(0)
    result = classify_canonical_direct_eligibility(
        normalized_rejected,
        contract=contract,
        original_user_task=user_task,
    )
    print(f"\n791e DIFFERENTIAL (contract built from the APPROVED plan):")
    print(f"  first (rejected) candidate: {result['eligibility']}"
          f" reasons={result['reasons']}")
    if "plan_not_contract_plan" not in result["reasons"]:
        print("FAIL: differential must flag plan_not_contract_plan")
        sys.exit(1)
    print("  plan_not_contract_plan: OK - the rejected candidate provably"
          " differs from the trusted plan")


if __name__ == "__main__":
    main()
