"""
harness.task_control.state_utils - Small shared state helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import List
from typing import Optional
from harness.evidence.extraction_artifacts import field_names_from_specs
from harness.utils import JsonDict
from harness.utils import RunLogger

def _tc():
    import harness.task_control as tc

    return tc

def _state_path(logger: RunLogger) -> Path:
    return logger.task_dir / _tc().TASK_STATE_FILE

def _first_phase_id(plan: JsonDict) -> Optional[str]:
    for phase in plan.get("phases", []):
        if isinstance(phase, dict):
            return str(phase.get("id") or "")
    return None

def _phase_state(state: JsonDict, phase_id: str) -> Optional[JsonDict]:
    phases = state.get("phases")
    if not isinstance(phases, dict):
        return None
    phase_state = phases.get(str(phase_id))
    return phase_state if isinstance(phase_state, dict) else None

def _string_list(value: Any) -> List[str]:
    return field_names_from_specs(value)

def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False

def _unique_paths(values: List[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out

def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default

def _append_unique(target: List[Any], values: List[Any]) -> None:
    seen = {str(item) for item in target}
    for value in values:
        key = str(value)
        if key not in seen:
            target.append(value)
            seen.add(key)
