"""Deterministic cross-task Fleet reuse based on harness-owned Fleet memory.

The Dispatcher owns Fleet inventory and memory persistence.  This module only
interprets the small ``abcp-harness-fleet-memory/v1`` envelope written by the
harness and ranks conservative reuse candidates.  It deliberately does not
read arbitrary/foreign Fleet memory as instructions.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, List, Optional
from urllib.parse import urlsplit


FLEET_MEMORY_SCHEMA = "abcp-harness-fleet-memory/v1"
FLEET_REUSE_POLICY_VERSION = 2
DEFAULT_RUNNING_STALE_SECONDS = 24.0 * 60.0 * 60.0
MAX_ACTIVE_RUNNING_MEMORY_RECORDS = 32
RUNNING_OVERFLOW_TASK_ID = "__fleet_running_overflow__"
RUNNING_OVERFLOW_WORKER_ID = "__fleet_running_overflow__"

_LEGACY_ASSIGNED_TASK = "Assigned task:\n"
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_NON_SEMANTIC_RE = re.compile(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+")
_NUMBER_RE = re.compile(r"(?<![a-z0-9])\d+(?:\.\d+)*(?![a-z0-9])")
_URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
_REUSABLE_FLEET_STATUSES = frozenset({"active", "prepared"})


@dataclass(frozen=True)
class TaskReuseMatch:
    fleet_id: str
    task_id: str
    prior_task: str
    score: float
    fleet_updated_at: float = 0.0
    task_updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "fleetId": self.fleet_id,
            "taskId": self.task_id,
            "score": round(self.score, 6),
            "fleetUpdatedAt": self.fleet_updated_at,
            "taskUpdatedAt": self.task_updated_at,
        }


def parse_fleet_memory(value: Any) -> dict:
    """Parse one Memory.get/System.register memory value without trusting it."""

    data = (
        value.get("data")
        if isinstance(value, dict) and isinstance(value.get("data"), dict)
        else value
    )
    if data is None:
        return {"envelope": {}, "revision": None, "foreign": False}
    context = data if isinstance(data, str) else (
        data.get("context") if isinstance(data, dict) else None
    )
    revision = data.get("revision") if isinstance(data, dict) else None
    if not context:
        return {"envelope": {}, "revision": revision, "foreign": False}
    try:
        envelope = json.loads(context) if isinstance(context, str) else context
    except (TypeError, ValueError, json.JSONDecodeError):
        envelope = None
    recognized = (
        isinstance(envelope, dict)
        and envelope.get("schema") == FLEET_MEMORY_SCHEMA
    )
    return {
        "envelope": envelope if recognized else {},
        "revision": revision,
        "foreign": not recognized,
    }


def task_text_from_memory_entry(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    task = str(entry.get("rootTask") or entry.get("task") or "").strip()
    # Older harness versions stored the entire BrowserAgent prompt. Recover the
    # assigned-task tail when it survived the historical 2,000-char limit.
    if _LEGACY_ASSIGNED_TASK in task:
        task = task.rsplit(_LEGACY_ASSIGNED_TASK, 1)[-1].strip()
    return task


def normalize_task_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = _NON_SEMANTIC_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _char_bigrams(text: str) -> set[str]:
    compact = text.replace(" ", "")
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return (2.0 * len(left & right)) / (len(left) + len(right))


def task_similarity(left: Any, right: Any) -> float:
    """Return a stable lexical similarity score suitable for routing.

    This is intentionally conservative and dependency-free.  Reuse only
    chooses browser context; it never treats a prior task result as current
    evidence or authorizes a side effect.
    """

    normalized_left = normalize_task_text(left)
    normalized_right = normalize_task_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    if min(len(normalized_left), len(normalized_right)) < 6:
        return 0.0

    left_bigrams = _char_bigrams(normalized_left)
    right_bigrams = _char_bigrams(normalized_right)
    bigram_dice = _dice(left_bigrams, right_bigrams)
    sequence = SequenceMatcher(
        None, normalized_left, normalized_right, autojunk=False
    ).ratio()
    left_words = set(_WORD_RE.findall(normalized_left))
    right_words = set(_WORD_RE.findall(normalized_right))
    containment = (
        len(left_words & right_words) / min(len(left_words), len(right_words))
        if left_words and right_words
        else 0.0
    )
    return (0.55 * bigram_dice) + (0.35 * sequence) + (0.10 * containment)


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _explicit_origins(value: Any) -> set[str]:
    origins: set[str] = set()
    for raw_url in _URL_RE.findall(str(value or "")):
        try:
            parsed = urlsplit(raw_url.rstrip(".,;:!?，。；：！？"))
            port = parsed.port
        except ValueError:
            continue
        hostname = str(parsed.hostname or "").lower().strip(".")
        if not hostname:
            continue
        default_port = (parsed.scheme.lower() == "http" and port == 80) or (
            parsed.scheme.lower() == "https" and port == 443
        )
        origins.add(
            f"{parsed.scheme.lower()}://{hostname}"
            + (f":{port}" if port and not default_port else "")
        )
    return origins


def _number_tokens(value: Any) -> set[str]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return set(_NUMBER_RE.findall(normalized))


def tasks_structurally_compatible(left: Any, right: Any) -> bool:
    """Fail closed on target conflicts before lexical ranking.

    The generic layer deliberately has no site/action vocabulary. Automatic
    reuse therefore accepts only normalized equality or a high-overlap
    contiguous extension, then rejects explicit URL-origin or numeric-token
    conflicts. A paraphrase that cannot be proved compatible simply creates a
    fresh Fleet.
    """

    normalized_left = normalize_task_text(left)
    normalized_right = normalize_task_text(right)
    if not normalized_left or not normalized_right:
        return False
    left_origins = _explicit_origins(left)
    right_origins = _explicit_origins(right)
    if left_origins and right_origins and left_origins.isdisjoint(right_origins):
        return False
    left_numbers = _number_tokens(left)
    right_numbers = _number_tokens(right)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False
    if normalized_left == normalized_right:
        return True
    compact_left = normalized_left.replace(" ", "")
    compact_right = normalized_right.replace(" ", "")
    shorter, longer = sorted((compact_left, compact_right), key=len)
    return bool(
        shorter
        and shorter in longer
        and len(shorter) / max(1, len(longer)) >= 0.60
    )


def running_memory_record_blocks_reuse(
    entry: Any,
    *,
    now: Optional[float] = None,
    running_stale_seconds: float = DEFAULT_RUNNING_STALE_SECONDS,
) -> bool:
    """Return whether one running worker lease must keep a Fleet closed."""

    if (
        not isinstance(entry, dict)
        or str(entry.get("reuseStatus") or "").lower() != "running"
    ):
        return False
    reference_time = time.time() if now is None else _safe_float(now)
    stale_seconds = max(0.0, _safe_float(running_stale_seconds))
    updated_at = _safe_float(entry.get("updatedAt"))
    # Missing/future timestamps cannot prove that the writer is gone. A
    # non-positive TTL explicitly disables expiry and keeps fail-closed
    # behaviour for every running record.
    if stale_seconds <= 0.0 or updated_at <= 0.0 or updated_at > reference_time:
        return True
    return (reference_time - updated_at) < stale_seconds


def compact_running_memory_records(
    entries: Iterable[Any],
    *,
    now: Optional[float] = None,
    running_stale_seconds: float = DEFAULT_RUNNING_STALE_SECONDS,
    limit: int = MAX_ACTIVE_RUNNING_MEMORY_RECORDS,
) -> List[dict]:
    """Bound live worker leases without forgetting that overflow is active.

    The synthetic overflow entry intentionally looks like an ordinary running
    record. Older readers therefore remain fail-closed even though they do not
    understand the compaction convention. A finite timestamp represents the
    newest omitted lease and expires under the normal TTL rule; timestamp zero
    represents non-expiring or untrustworthy omitted leases.
    """

    reference_time = time.time() if now is None else _safe_float(now)
    stale_seconds = max(0.0, _safe_float(running_stale_seconds))
    bounded_limit = max(1, int(limit or 0))
    finite: List[tuple[float, int, dict]] = []
    overflow_updated_at = 0.0
    permanent_overflow = False

    for index, entry in enumerate(entries):
        if (
            not isinstance(entry, dict)
            or str(entry.get("reuseStatus") or "").strip().lower()
            != "running"
        ):
            continue
        updated_at = _safe_float(entry.get("updatedAt"))
        is_overflow = (
            str(entry.get("taskId") or "").strip()
            == RUNNING_OVERFLOW_TASK_ID
            and str(entry.get("workerId") or "").strip()
            == RUNNING_OVERFLOW_WORKER_ID
        )

        if stale_seconds <= 0.0:
            if is_overflow or updated_at <= 0.0:
                permanent_overflow = True
            else:
                finite.append((updated_at, index, entry))
            continue
        if updated_at <= 0.0 or updated_at > reference_time:
            # Missing, invalid, and future timestamps are permanent fences.
            permanent_overflow = True
            continue
        if (reference_time - updated_at) >= stale_seconds:
            continue
        if is_overflow:
            overflow_updated_at = max(overflow_updated_at, updated_at)
        else:
            finite.append((updated_at, index, entry))

    finite.sort(key=lambda item: (item[0], item[1]))
    omitted = finite[:-bounded_limit]
    kept = finite[-bounded_limit:]
    if omitted:
        if stale_seconds <= 0.0:
            permanent_overflow = True
        else:
            overflow_updated_at = max(
                overflow_updated_at,
                max(item[0] for item in omitted),
            )

    compacted = [dict(item[2]) for item in kept]
    if permanent_overflow or overflow_updated_at > 0.0:
        compacted.append({
            "taskId": RUNNING_OVERFLOW_TASK_ID,
            "workerId": RUNNING_OVERFLOW_WORKER_ID,
            "agentId": "",
            "updatedAt": 0.0 if permanent_overflow else overflow_updated_at,
            "reuseStatus": "running",
            "autoReuseEligible": True,
        })
    return compacted


def fleet_memory_auto_reuse_allowed(
    envelope: Any,
    *,
    now: Optional[float] = None,
    running_stale_seconds: float = DEFAULT_RUNNING_STALE_SECONDS,
) -> bool:
    """Return whether a recognized envelope carries a trusted Fleet policy."""

    if not isinstance(envelope, dict):
        return False
    policy = envelope.get("reusePolicy")
    if not isinstance(policy, dict):
        return False
    try:
        version = int(policy.get("version") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if version < FLEET_REUSE_POLICY_VERSION or policy.get("blocked") is not False:
        return False
    tasks = envelope.get("tasks")
    if not isinstance(tasks, list):
        return False
    # Belt-and-suspenders compatibility with records written while policy was
    # task-scoped: one explicit identity veto closes the whole Fleet.
    return not any(
        isinstance(entry, dict)
        and (
            entry.get("autoReuseEligible") is False
            or running_memory_record_blocks_reuse(
                entry,
                now=now,
                running_stale_seconds=running_stale_seconds,
            )
        )
        for entry in tasks
    )


def find_similar_task_fleet(
    registration: Any,
    current_task: str,
    *,
    candidate_fleet_ids: Iterable[str] = (),
    current_task_id: str = "",
    threshold: float = 0.78,
    running_stale_seconds: float = DEFAULT_RUNNING_STALE_SECONDS,
) -> Optional[TaskReuseMatch]:
    """Find the best recognized, reuse-eligible Fleet memory entry."""

    data = registration.get("data") if isinstance(registration, dict) else None
    fleets = data.get("fleets") if isinstance(data, dict) else None
    if not isinstance(fleets, list):
        return None
    candidates = {
        str(fleet_id or "").strip()
        for fleet_id in candidate_fleet_ids
        if str(fleet_id or "").strip()
    }
    # An empty authoritative candidate set means there is nothing this slot
    # may address. It must never widen into every Fleet in registration.
    if not candidates:
        return None
    current_id = str(current_task_id or "").strip()
    minimum = min(1.0, max(0.0, _safe_float(threshold)))
    matches: List[TaskReuseMatch] = []
    for fleet in fleets:
        if not isinstance(fleet, dict):
            continue
        fleet_id = str(fleet.get("fleetId") or "").strip()
        status = str(fleet.get("status") or "").strip().lower()
        if (
            not fleet_id
            or fleet_id not in candidates
            or status not in _REUSABLE_FLEET_STATUSES
        ):
            continue
        parsed = parse_fleet_memory(fleet.get("memory"))
        if parsed["foreign"]:
            continue
        envelope = parsed["envelope"]
        if not fleet_memory_auto_reuse_allowed(
            envelope,
            running_stale_seconds=running_stale_seconds,
        ):
            continue
        tasks = envelope.get("tasks")
        if not isinstance(tasks, list):
            continue
        for entry in tasks:
            if not isinstance(entry, dict):
                continue
            task_id = str(entry.get("taskId") or "").strip()
            if (
                not task_id
                or entry.get("autoReuseEligible") is not True
                or str(entry.get("reuseStatus") or "").lower() != "completed"
                or (current_id and task_id == current_id)
            ):
                continue
            prior_task = task_text_from_memory_entry(entry)
            if not prior_task:
                continue
            if not tasks_structurally_compatible(current_task, prior_task):
                continue
            score = task_similarity(current_task, prior_task)
            if score < minimum:
                continue
            matches.append(TaskReuseMatch(
                fleet_id=fleet_id,
                task_id=task_id,
                prior_task=prior_task,
                score=score,
                fleet_updated_at=_safe_float(fleet.get("updatedAt")),
                task_updated_at=_safe_float(entry.get("updatedAt")),
            ))
    if not matches:
        return None
    return max(matches, key=lambda item: (
        item.score,
        item.task_updated_at,
        item.fleet_updated_at,
        item.fleet_id,
        item.task_id,
    ))
