"""Row- and phase-level pacing primitives."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional


JsonDict = Dict[str, Any]
SleepFn = Callable[[float], Awaitable[Any]]
PACING_INTERVAL_FIELDS = ("row_interval_seconds", "phase_interval_seconds")
PACING_FIELDS = (*PACING_INTERVAL_FIELDS, "jitter_ratio")
MAX_PACING_INTERVAL_SECONDS = 86400.0
DEFAULT_PACING: JsonDict = {
    "row_interval_seconds": 0.0,
    "phase_interval_seconds": 0.0,
    "jitter_ratio": 0.0,
}


def normalized_pacing(value: Any) -> JsonDict:
    source = value if isinstance(value, dict) else {}
    result = dict(DEFAULT_PACING)
    for key in PACING_INTERVAL_FIELDS:
        try:
            result[key] = max(
                0.0,
                min(float(source.get(key) or 0.0), MAX_PACING_INTERVAL_SECONDS),
            )
        except (TypeError, ValueError):
            result[key] = 0.0
    try:
        result["jitter_ratio"] = max(
            0.0, min(float(source.get("jitter_ratio") or 0.0), 1.0)
        )
    except (TypeError, ValueError):
        result["jitter_ratio"] = 0.0
    return result


def merge_pacing(*values: Any) -> JsonDict:
    merged = dict(DEFAULT_PACING)
    for value in values:
        if isinstance(value, dict):
            for key in DEFAULT_PACING:
                if key in value:
                    merged[key] = value[key]
    return normalized_pacing(merged)


def jittered_interval(
    interval_seconds: Any,
    jitter_ratio: Any,
    *,
    random_value: Optional[float] = None,
) -> float:
    try:
        interval = max(0.0, float(interval_seconds or 0.0))
        jitter = max(0.0, min(float(jitter_ratio or 0.0), 1.0))
    except (TypeError, ValueError):
        return 0.0
    if interval == 0.0 or jitter == 0.0:
        return interval
    sample = random.random() if random_value is None else max(0.0, min(float(random_value), 1.0))
    return max(0.0, interval * (1.0 + jitter * (sample * 2.0 - 1.0)))


async def wait_between_rows(
    agent: Any,
    worker_contract: Any,
    *,
    completed_index: int,
    total_rows: int,
    sleep: SleepFn = asyncio.sleep,
    random_value: Optional[float] = None,
    source: str,
) -> float:
    if completed_index >= total_rows - 1:
        return 0.0
    pacing = normalized_pacing(
        worker_contract.get("pacing") if isinstance(worker_contract, dict) else {}
    )
    delay = jittered_interval(
        pacing["row_interval_seconds"], pacing["jitter_ratio"],
        random_value=random_value,
    )
    if delay <= 0.0:
        return 0.0
    logger = getattr(agent, "logger", None)
    payload = {
        "source": source,
        "completedRowIndex": completed_index,
        "nextRowIndex": completed_index + 1,
        "totalRows": total_rows,
        "requestedIntervalSeconds": pacing["row_interval_seconds"],
        "jitterRatio": pacing["jitter_ratio"],
        "actualWaitSeconds": delay,
    }
    if logger is not None:
        logger.write("pacing.row.wait_started", payload)
    await sleep(delay)
    if logger is not None:
        logger.write("pacing.row.wait_completed", payload)
    return delay


def parse_utc_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
