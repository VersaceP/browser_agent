"""Measure the region-reality model before it is allowed to matter.

`vl.reality_check_evidence_mode` ships at "advisory" and the only honest way to
raise it is to run the model against captures whose answer is already known and
count how often it is right. This module is that measurement: a labelled
manifest in, a per-class score and a pass/fail gate out.

Precision is scored per class rather than in aggregate because the classes are
not interchangeable. Being wrong toward "keep looking" costs steps; being wrong
toward "there is nothing here" costs the answer. The severity table below names
every confusion that falls on the expensive side, and the gate refuses on a
single one of them no matter how good the averages look.

Pure scoring lives in `score_predictions` and has no I/O, so it is unit-tested
directly. `run_manifest` is the thin live wrapper that calls the configured VL
over the manifest's screenshots.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness.utils import JsonDict
from harness.vl.capture_geometry import (
    CLASS_AUTH_OVERLAY,
    CLASS_CONTENT_PRESENT,
    CLASS_EXPLICIT_EMPTY,
    CLASS_REGION_NOT_IN_CAPTURE,
    CLASS_UNCERTAIN,
    REGION_CLASSES,
)

# Per-class precision required before a class may carry weight. Reading them:
# when the model says this, how often is it right? The two "stop working"
# classes are held to a stricter bar than the one that only ever adds work.
PRECISION_THRESHOLDS: Dict[str, float] = {
    CLASS_AUTH_OVERLAY: 0.98,
    CLASS_EXPLICIT_EMPTY: 0.98,
    CLASS_CONTENT_PRESENT: 0.95,
}

# Minimum labelled cases per class before its precision is believed at all. A
# 1-for-1 class scores 1.00 and means nothing.
MIN_SUPPORT_PER_CLASS = 8

# Classes the fixture must actually contain, which is not the same set as the
# classes held to a precision bar. `region_not_in_capture` has no threshold —
# predicting it only ever costs steps — but a fixture with no cases of it can
# never surface the one confusion that matters most,
# `region_not_in_capture -> explicit_empty_state`. Without these cases the
# fixture is measuring a model that was never shown the hard question.
REQUIRED_LABEL_SUPPORT = (
    CLASS_CONTENT_PRESENT,
    CLASS_EXPLICIT_EMPTY,
    CLASS_AUTH_OVERLAY,
    CLASS_REGION_NOT_IN_CAPTURE,
)

# (truth, predicted) pairs that are expensive rather than merely wrong. Each
# entry names the harm so a failing run reads as a diagnosis, not a number.
SEVERE_CONFUSIONS: Dict[Tuple[str, str], str] = {
    (CLASS_CONTENT_PRESENT, CLASS_EXPLICIT_EMPTY):
        "declares_absence_over_real_content",
    (CLASS_CONTENT_PRESENT, CLASS_AUTH_OVERLAY):
        "invents_a_blocker_over_readable_content",
    (CLASS_REGION_NOT_IN_CAPTURE, CLASS_EXPLICIT_EMPTY):
        "reports_what_it_cannot_see_as_empty",
    (CLASS_REGION_NOT_IN_CAPTURE, CLASS_CONTENT_PRESENT):
        "reports_content_absent_from_the_capture",
    (CLASS_AUTH_OVERLAY, CLASS_EXPLICIT_EMPTY):
        "turns_a_removable_overlay_into_permanent_absence",
}


def _norm_class(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in REGION_CLASSES else CLASS_UNCERTAIN


def load_manifest(path: str) -> JsonDict:
    """Read a labelled fixture manifest and reject malformed cases loudly.

    A case with no ground-truth class is a labelling mistake, and silently
    skipping it would inflate every score computed afterwards.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"manifest {path} has no cases")
    problems: List[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            problems.append(f"case[{index}] is not an object")
            continue
        if not str(case.get("caseId") or "").strip():
            problems.append(f"case[{index}] has no caseId")
        truth = case.get("groundTruth")
        truth_class = str((truth or {}).get("class") or "") if isinstance(truth, dict) else ""
        if truth_class not in REGION_CLASSES:
            problems.append(
                f"case[{index}] groundTruth.class must be one of {list(REGION_CLASSES)}"
            )
    if problems:
        raise ValueError("; ".join(problems))
    return data


def score_predictions(cases: Any, predictions: Any) -> JsonDict:
    """Per-class precision/recall plus the severe-confusion list.

    A case with no prediction counts as missing, never as correct — an eval
    that quietly drops the cases the model failed to answer is measuring the
    wrong population.
    """
    case_list = [case for case in (cases or []) if isinstance(case, dict)]
    preds = predictions if isinstance(predictions, dict) else {}

    per_class: Dict[str, Dict[str, int]] = {
        name: {"truePositive": 0, "falsePositive": 0, "support": 0}
        for name in REGION_CLASSES
    }
    confusions: Dict[str, int] = {}
    severe: List[JsonDict] = []
    missing: List[str] = []
    count_errors: List[JsonDict] = []

    for case in case_list:
        case_id = str(case.get("caseId") or "")
        truth = case.get("groundTruth") if isinstance(case.get("groundTruth"), dict) else {}
        truth_class = _norm_class(truth.get("class"))
        per_class[truth_class]["support"] += 1

        prediction = preds.get(case_id)
        if not isinstance(prediction, dict):
            missing.append(case_id)
            continue
        predicted_class = _norm_class(
            prediction.get("classification") or prediction.get("class")
        )
        if predicted_class == truth_class:
            per_class[truth_class]["truePositive"] += 1
        else:
            per_class[predicted_class]["falsePositive"] += 1
            key = f"{truth_class}->{predicted_class}"
            confusions[key] = confusions.get(key, 0) + 1
            harm = SEVERE_CONFUSIONS.get((truth_class, predicted_class))
            if harm:
                severe.append({
                    "caseId": case_id,
                    "truth": truth_class,
                    "predicted": predicted_class,
                    "harm": harm,
                })

        # Item counts are scored separately and never gate the mode: a model
        # that finds the region and reads 4 of 5 reviews is still right about
        # the thing the harness asks it — whether there is content at all.
        expected_count = truth.get("itemCount")
        actual_count = prediction.get("itemCount")
        if (
            truth_class == CLASS_CONTENT_PRESENT
            and isinstance(expected_count, int)
            and isinstance(actual_count, int)
            and expected_count != actual_count
        ):
            count_errors.append({
                "caseId": case_id,
                "expected": expected_count,
                "actual": actual_count,
            })

    metrics: Dict[str, JsonDict] = {}
    for name, counts in per_class.items():
        predicted_total = counts["truePositive"] + counts["falsePositive"]
        metrics[name] = {
            "support": counts["support"],
            "predicted": predicted_total,
            "truePositive": counts["truePositive"],
            "falsePositive": counts["falsePositive"],
            "precision": (
                counts["truePositive"] / predicted_total if predicted_total else None
            ),
            "recall": (
                counts["truePositive"] / counts["support"]
                if counts["support"] else None
            ),
        }
    return {
        "cases": len(case_list),
        "scored": len(case_list) - len(missing),
        "missingPredictions": missing,
        "perClass": metrics,
        "confusions": dict(sorted(confusions.items())),
        "severeConfusions": severe,
        "itemCountErrors": count_errors,
    }


def evaluate_gate(
    score: Any,
    *,
    thresholds: Optional[Dict[str, float]] = None,
    min_support: int = MIN_SUPPORT_PER_CLASS,
) -> JsonDict:
    """Whether this score licenses raising `reality_check_evidence_mode`.

    Fails closed on three separate grounds, and reports all of them rather than
    the first: too few labelled cases to believe a class, precision under its
    threshold, and any severe confusion at all.
    """
    limits = thresholds if isinstance(thresholds, dict) else PRECISION_THRESHOLDS
    metrics = (score or {}).get("perClass") or {}
    failures: List[JsonDict] = []

    # Coverage and precision are separate obligations. A class can owe labelled
    # cases without owing a precision bar (region_not_in_capture), and the two
    # were conflated: iterating only the threshold table let a fixture with
    # zero region_not_in_capture cases license corroborating mode.
    for name in REQUIRED_LABEL_SUPPORT:
        support = int((metrics.get(name) or {}).get("support") or 0)
        if support < min_support:
            failures.append({
                "class": name,
                "reason": "insufficient_labelled_support",
                "support": support,
                "required": min_support,
            })

    covered = {failure.get("class") for failure in failures}
    for name, required in limits.items():
        if name in covered:
            continue
        entry = metrics.get(name) or {}
        precision = entry.get("precision")
        if precision is None:
            failures.append({
                "class": name,
                "reason": "class_never_predicted",
                "support": int(entry.get("support") or 0),
            })
            continue
        if precision < required:
            failures.append({
                "class": name,
                "reason": "precision_below_threshold",
                "precision": precision,
                "required": required,
            })

    severe = list((score or {}).get("severeConfusions") or [])
    if severe:
        failures.append({
            "reason": "severe_confusions_present",
            "count": len(severe),
            "examples": severe[:5],
        })
    missing = list((score or {}).get("missingPredictions") or [])
    if missing:
        failures.append({
            "reason": "cases_without_predictions",
            "count": len(missing),
            "caseIds": missing[:10],
        })

    return {
        "eligibleForCorroborating": not failures,
        "failures": failures,
        "recommendedEvidenceMode": "corroborating" if not failures else "advisory",
    }


def _case_question(case: JsonDict) -> str:
    """Rebuild the exact claim the runtime would have asked for this case.

    Reusing the production builder is the point: an eval that asks a kinder
    question than the harness does measures a prompt nobody runs.
    """
    from harness.vl.reality_check import build_row_scoped_claim

    claim = str(case.get("claim") or "").strip()
    if claim:
        return claim
    region = case.get("region") if isinstance(case.get("region"), dict) else {}
    hint = str(region.get("description") or "").strip()
    if not hint and region.get("selector"):
        hint = f"the page section matching {region['selector']}"
    return build_row_scoped_claim(
        worker_contract=case.get("workerContract"),
        row_key=str(case.get("rowKey") or case.get("caseId") or ""),
        fields=case.get("fields"),
        region_hint=hint,
    )


async def run_manifest(
    manifest_path: str, *, config: Any = None, config_path: str = "config.json",
) -> JsonDict:
    """Run the configured VL over a manifest and score it."""
    from runtime_config import load_runtime_config
    from harness.vl.core import visual_verify_image

    data = load_manifest(manifest_path)
    vl_config = config
    if vl_config is None:
        vl_config = load_runtime_config(config_path, warn=False).harness.vl
    base = Path(manifest_path).resolve().parent

    predictions: Dict[str, JsonDict] = {}
    for case in data["cases"]:
        case_id = str(case.get("caseId") or "")
        raw_path = str(case.get("screenshotPath") or "")
        image_path = Path(raw_path)
        if not image_path.is_absolute():
            image_path = base / raw_path
        verdict = await visual_verify_image(
            config=vl_config,
            image_path=str(image_path),
            expected={},
            mode="region_reality",
            question=_case_question(case),
        )
        if isinstance(verdict, dict) and verdict.get("status") == "done":
            predictions[case_id] = verdict
    score = score_predictions(data["cases"], predictions)
    return {
        "manifest": manifest_path,
        "model": getattr(vl_config, "model_id", ""),
        "score": score,
        "gate": evaluate_gate(score),
        "predictions": predictions,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score the region-reality VL against a labelled manifest",
    )
    parser.add_argument("manifest", help="path to the labelled fixture manifest")
    parser.add_argument(
        "--config", default="config.json",
        help="runtime config supplying the vl section (default: config.json)",
    )
    parser.add_argument(
        "--out", default="", help="write the full report JSON to this path",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(run_manifest(args.manifest, config_path=args.config))
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    summary = {"score": report["score"], "gate": report["gate"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["gate"]["eligibleForCorroborating"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
