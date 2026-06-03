"""
harness.strategy_bank - Read-only strategy guidance for common browser tasks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

from harness.utils import JsonDict, trim_large_strings


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
            "applies_when": item.get("applies_when") or [],
            "preferred_tools": item.get("preferred_tools") or [],
            "avoid_tools": item.get("avoid_tools") or [],
            "procedure": item.get("procedure") or [],
            "success_criteria": item.get("success_criteria") or [],
        })
    payload: JsonDict = {
        "version": bank.get("version", 1),
        "path": bank.get("path"),
        "strategies": compact,
    }
    if bank.get("load_error"):
        payload["load_error"] = bank.get("load_error")
    return trim_large_strings(payload, 2000)


def _matches_any(text: str, values: Any) -> bool:
    if not isinstance(values, list):
        return False
    lowered = text.lower()
    return any(str(value or "").lower() in lowered for value in values)


def select_strategies_for_phase(
    bank: JsonDict,
    *,
    task_type: Optional[str],
    phase: JsonDict,
    limit: int = 3,
) -> List[JsonDict]:
    strategies = bank.get("strategies") if isinstance(bank.get("strategies"), list) else []
    phase_text = " ".join(
        str(phase.get(key) or "")
        for key in ("id", "objective", "worker_task", "type")
    )
    selected: List[JsonDict] = []
    for item in strategies:
        if not isinstance(item, dict):
            continue
        task_types = item.get("task_types") if isinstance(item.get("task_types"), list) else []
        if task_type and task_types and task_type not in task_types:
            continue
        stage = str(item.get("stage") or "")
        stage_match = bool(stage and stage.lower() in phase_text.lower())
        keyword_match = _matches_any(phase_text, item.get("phase_keywords"))
        if not stage_match and not keyword_match:
            if selected:
                continue
        selected.append({
            "id": item.get("id"),
            "stage": item.get("stage"),
            "preferred_tools": item.get("preferred_tools") or [],
            "avoid_tools": item.get("avoid_tools") or [],
            "procedure": item.get("procedure") or [],
            "success_criteria": item.get("success_criteria") or [],
            "failure_signatures": item.get("failure_signatures") or [],
        })
        if len(selected) >= limit:
            break
    return selected


def render_strategy_guidance(strategies: List[JsonDict]) -> str:
    if not strategies:
        return ""
    return (
        "<strategy_bank_guidance>\n"
        + json.dumps(strategies, ensure_ascii=False, indent=2, default=str)
        + "\n</strategy_bank_guidance>"
    )
