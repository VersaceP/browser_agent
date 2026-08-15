"""
harness.strategy_telemetry - Append-only strategy attempt telemetry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

from harness.task_control import utc_now_iso
from harness.utils import JsonDict, storage_for_logger


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

    # What a file backend would write. It is not a claim that anything was
    # written: the receipt below reports only what this call can actually
    # observe. Before the storage layer this function appended path by path and
    # listed the ones that succeeded; afterwards it listed the same paths
    # unconditionally, which reads as a write confirmation in db mode where no
    # such file exists, and in file mode too - FileStore swallows a per-path
    # OSError and continues. `append_strategy_attempt` returns None, so the
    # only fact available here is whether it raised.
    targets = [str(path) for path in paths]

    recorded = False
    backend = ""
    try:
        storage, task_id = storage_for_logger(logger)
        backend = type(storage).__name__
        storage.append_strategy_attempt(
            task_id=task_id,
            run_id=str(getattr(logger, "run_id", "") or ""),
            payload=payload,
        )
        recorded = True
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a phase
        if logger is not None and hasattr(logger, "write"):
            logger.write(
                "strategy_attempts.write_failed",
                {"fileTargets": targets, "backend": backend, "error": str(exc)},
            )
    if logger is not None and hasattr(logger, "write"):
        logger.write(
            "strategy_attempts.appended",
            {
                "accepted": recorded,
                "backend": backend,
                "fileTargets": targets,
                "payload": payload,
            },
        )
    return {
        "status": "done" if recorded else "failed",
        "accepted": recorded,
        "backend": backend,
        "fileTargets": targets,
        "payload": payload,
    }


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
