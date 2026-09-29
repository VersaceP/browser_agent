"""Parse explicit Fleet references from the original user request."""

from __future__ import annotations

import re
from typing import Optional, Tuple


_FLEET_REF_RE = re.compile(
    r"@([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)"
    r"(?![0-9a-f-])",
    re.IGNORECASE,
)
_FLEET_SIGIL_RE = re.compile(r"@[0-9a-f][0-9a-f-]{3,}", re.IGNORECASE)
FLEET_REFERENCE_SYNTAX = (
    "Fleet 引用只认一种写法：@ 紧跟 Fleet id，中间不要空格，例如 "
    "@2677c96a-7a2b-4119-bec8-2e56cf93a5cd（只写前 8 位 @2677c96a 也可以）。"
)


def extract_fleet_reference(task: str) -> Tuple[Optional[str], Optional[str]]:
    """Return one explicit ``@<fleet-id>`` reference or a syntax error."""
    text = str(task or "")
    matches = [match.group(1).lower() for match in _FLEET_REF_RE.finditer(text)]
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        return None, "任务里出现了多个不同的 Fleet 引用，无法判断该用哪一个。"
    for match in _FLEET_SIGIL_RE.finditer(text):
        if not _FLEET_REF_RE.fullmatch(match.group(0)):
            return None, (
                f"{match.group(0)} 不是合法的 Fleet id（要 8 位十六进制前缀或完整"
                f" UUID）。{FLEET_REFERENCE_SYNTAX}"
            )
    if unique:
        return unique[0], None
    return None, None
