"""
harness.strategy_bank - Strategy guidance for common browser tasks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional, Tuple

from harness.task_types import normalize_task_type
from harness.tool_policy import task_type_capability_covers
from harness.utils import JsonDict, trim_large_strings


STRATEGY_PROTOCOL = (
    "Strategy Bank entries are cross-site procedural guidance, not facts,"
    " permissions, contracts, validators, budgets, route state, or terminal"
    " authority. Verify applies_when against live evidence. Discard guidance"
    " that conflicts with Harness receipts, explicit worker/task contracts,"
    " validated Skill guidance, or runtime safety fences. Site-specific URLs,"
    " selectors, headings, and markers must come from live page evidence or a"
    " validated Skill, never from Strategy Bank."
)


def resolve_strategy_bank_path(raw_path: str) -> Path:
    path = Path(raw_path or "strategy_bank/strategy_bank.json").expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def load_strategy_bank(raw_path: str) -> JsonDict:
    path = resolve_strategy_bank_path(raw_path)
    if not path.exists():
        return {"version": 1, "strategies": [], "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "version": 1,
            "strategies": [],
            "path": str(path),
            "load_error": str(exc),
        }
    if not isinstance(data, dict):
        return {
            "version": 1,
            "strategies": [],
            "path": str(path),
            "load_error": "strategy bank root must be an object",
        }
    data.setdefault("version", 1)
    data.setdefault("strategies", [])
    data["path"] = str(path)
    return data


def compact_strategy_bank(bank: JsonDict, *, max_strategies: int = 8) -> JsonDict:
    strategies = bank.get("strategies") if isinstance(bank.get("strategies"), list) else []
    compact: List[JsonDict] = []
    for item in strategies[:max_strategies]:
        if not isinstance(item, dict):
            continue
        compact.append({
            "id": item.get("id"),
            "task_types": item.get("task_types") or [],
            "stage": item.get("stage"),
            "applies_to_stages": item.get("applies_to_stages") or [],
            "fallback": bool(item.get("fallback", False)),
            "cross_cutting": bool(item.get("cross_cutting", False)),
            "applies_when": item.get("applies_when") or [],
            "preferred_tools": item.get("preferred_tools") or [],
            "cautioned_tools": item.get("cautioned_tools") or [],
            "discouraged_tools": item.get("discouraged_tools") or [],
            "procedure": item.get("procedure") or [],
            "success_criteria": item.get("success_criteria") or [],
            "failure_signatures": item.get("failure_signatures") or [],
        })
    payload: JsonDict = {
        "version": bank.get("version", 1),
        "path": bank.get("path"),
        "strategies": compact,
    }
    if bank.get("load_error"):
        payload["load_error"] = bank.get("load_error")
    return trim_large_strings(payload, 2000)


def strategy_bank_index(bank: JsonDict, *, max_strategies: int = 50) -> JsonDict:
    """Small pull-oriented index; strategy bodies remain on disk."""
    strategies = bank.get("strategies") if isinstance(bank.get("strategies"), list) else []
    items: List[JsonDict] = []
    for item in strategies[:max_strategies]:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            applies = item.get("applies_when")
            if isinstance(applies, list) and applies:
                description = str(applies[0])[:240]
        items.append({
            "id": item.get("id"),
            "description": description,
            "task_types": item.get("task_types") or [],
            "stages": item.get("applies_to_stages") or [item.get("stage")],
        })
    return {
        "path": bank.get("path"),
        "strategies": items,
    }


def _task_type_matches(task_type: Optional[str], item: JsonDict) -> bool:
    # cross_cutting is an explicit declaration that the strategy applies across
    # task types. Previously it was consulted only after this filter, making the
    # declaration ineffective for every newly added task type.
    if bool(item.get("cross_cutting", False)):
        return True
    task_types = item.get("task_types") if isinstance(item.get("task_types"), list) else []
    if not task_type or not task_types:
        return True
    normalized = normalize_task_type(task_type)
    declared = {normalize_task_type(value) for value in task_types}
    if normalized in declared:
        return True
    # Strategy compatibility follows execution capability, not label equality: a
    # phase that can call everything a strategy's declared type can call can
    # also follow that strategy. Derived from the policy tables rather than
    # listed here, so moving a domain between task types cannot leave a stale
    # containment claim behind.
    return any(
        task_type_capability_covers(normalized, value) for value in declared
    )


def _stage_matches(stage_hint: str, item: JsonDict) -> bool:
    stages = item.get("applies_to_stages")
    if not isinstance(stages, list) or not stages:
        stage = str(item.get("stage") or "").strip()
        stages = [stage] if stage else []
    return stage_hint in {str(value).strip() for value in stages}


def _render_strategy(item: JsonDict) -> JsonDict:
    return {
        "id": item.get("id"),
        "stage": item.get("stage"),
        "applies_to_stages": item.get("applies_to_stages") or [],
        "fallback": bool(item.get("fallback", False)),
        "cross_cutting": bool(item.get("cross_cutting", False)),
        "applies_when": item.get("applies_when") or [],
        "preferred_tools": item.get("preferred_tools") or [],
        "cautioned_tools": item.get("cautioned_tools") or [],
        "discouraged_tools": item.get("discouraged_tools") or [],
        "procedure": item.get("procedure") or [],
        "success_criteria": item.get("success_criteria") or [],
        "failure_signatures": item.get("failure_signatures") or [],
    }


def select_strategies_for_phase(
    bank: JsonDict,
    *,
    task_type: Optional[str],
    phase: JsonDict,
    limit: int = 3,
) -> List[JsonDict]:
    strategies = bank.get("strategies") if isinstance(bank.get("strategies"), list) else []
    stage_hint = str(phase.get("stage_hint") or "").strip()
    selected: List[JsonDict] = []
    for item in strategies:
        if not isinstance(item, dict):
            continue
        if not _task_type_matches(task_type, item):
            continue
        rendered = _render_strategy(item)
        stage_match = bool(stage_hint and _stage_matches(stage_hint, item))
        if bool(item.get("fallback", False)):
            # Fallback/cross-cutting guidance is no longer guessed from prose.
            # It remains available through the pull-oriented index.
            continue
        if stage_match:
            selected.append(rendered)
    return selected[:limit]


def render_strategy_guidance(strategies: List[JsonDict]) -> str:
    if not strategies:
        return ""
    guidance: List[JsonDict] = []
    for item in strategies:
        rendered = dict(item)
        rendered.setdefault("usage_scope", "slow_path_guidance")
        rendered.setdefault(
            "precedence",
            (
                "Reusable skills and explicit worker_contract.skill_id/skill_variables "
                "take priority. Use strategy_bank entries only when no skill applies "
                "or as fallback/recovery context after a skill declines or fails."
            ),
        )
        guidance.append(rendered)
    return (
        "<strategy_bank_protocol>\n"
        + STRATEGY_PROTOCOL
        + "\n</strategy_bank_protocol>\n"
        + "<strategy_bank_guidance>\n"
        + json.dumps(guidance, ensure_ascii=False, indent=2, default=str)
        + "\n</strategy_bank_guidance>"
    )
