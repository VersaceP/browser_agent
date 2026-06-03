"""
harness.templates - Lightweight template rendering for plan variables.
"""

import json
import re
from typing import Any

from harness.utils import JsonDict


_TEMPLATE_RE = re.compile(r"\{([^{}]+)\}")


def get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if 0 <= idx < len(current) else None
        else:
            return None
    return current


def render_templates(value: Any, variables: JsonDict) -> Any:
    if isinstance(value, str):
        def replace_match(match: re.Match) -> str:
            resolved = get_path(variables, match.group(1))
            if resolved is None:
                return match.group(0)
            if isinstance(resolved, (dict, list)):
                return json.dumps(resolved, ensure_ascii=False)
            return str(resolved)

        return _TEMPLATE_RE.sub(replace_match, value)
    if isinstance(value, list):
        return [render_templates(item, variables) for item in value]
    if isinstance(value, dict):
        return {
            key: render_templates(item, variables)
            for key, item in value.items()
        }
    return value
