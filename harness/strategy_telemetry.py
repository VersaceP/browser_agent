"""
harness.strategy_telemetry - Append-only strategy attempt telemetry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from harness.task_control import utc_now_iso
from harness.utils import JsonDict


ROOT_STRATEGY_ATTEMPTS_FILE = "strategy_attempts.jsonl"


def append_strategy_attempt(
    *,
    logger: Any,
    worker_contract: JsonDict,
    result: JsonDict,
) -> JsonDict:
    artifact_validation = (
        result.get("artifactValidation")
        if isinstance(result.get("artifactValidation"), dict)
        else {}
    )
    classification = (
        artifact_validation.get("classification")
        if isinstance(artifact_validation.get("classification"), dict)
        else None
    )
    payload: JsonDict = {
        "ts": utc_now_iso(),
        "taskId": getattr(logger, "task_id", ""),
        "phaseId": result.get("phaseId"),
        "workerId": result.get("workerId"),
        "strategy_ids": _strategy_ids(worker_contract),
        "status": result.get("status"),
        "statusCategory": result.get("statusCategory"),
        "validatedStatus": result.get("validatedStatus"),
        "failureClassification": (
            classification.get("category") if isinstance(classification, dict) else None
        ),
        "rowCount": artifact_validation.get("rowCount"),
        "artifactCount": len(result.get("artifacts") or []),
    }
    paths = _unique_paths([
        Path.cwd() / ROOT_STRATEGY_ATTEMPTS_FILE,
    ])
    task_dir = getattr(logger, "task_dir", None)
    if task_dir:
        paths = _unique_paths(paths + [Path(task_dir) / ROOT_STRATEGY_ATTEMPTS_FILE])

    written: List[str] = []
    line = json.dumps(payload, ensure_ascii=False, default=str)
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            written.append(str(path.resolve()))
        except OSError as exc:
            if logger is not None and hasattr(logger, "write"):
                logger.write(
                    "strategy_attempts.write_failed",
                    {"path": str(path), "error": str(exc)},
                )
    if logger is not None and hasattr(logger, "write"):
        logger.write(
            "strategy_attempts.appended",
            {"paths": written, "payload": payload},
        )
    return {"status": "done" if written else "failed", "paths": written, "payload": payload}


def _strategy_ids(worker_contract: JsonDict) -> List[str]:
    raw = worker_contract.get("strategy_ids")
    if raw is None:
        raw = worker_contract.get("_strategy_ids")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def _unique_paths(paths: List[Path]) -> List[Path]:
    seen = set()
    unique: List[Path] = []
    for path in paths:
        try:
            key = str(path.resolve(strict=False))
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique
