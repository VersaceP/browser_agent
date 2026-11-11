"""Parse explicit Fleet references from the original user request."""

from __future__ import annotations

import re
from typing import Optional, Tuple


_FLEET_REF_RE = re.compile(
    r"@(?:fleet[-_:])?([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)"
    r"(?![0-9a-f-])",
    re.IGNORECASE,
)
_FLEET_SIGIL_RE = re.compile(r"@(?:fleet[-_:]?\S*|[0-9a-f][0-9a-f-]{3,})", re.IGNORECASE)
FLEET_REFERENCE_SYNTAX = (
    "Fleet references must use @ immediately followed by a Fleet ID, with no space, for example "
    "@2677c96a-7a2b-4119-bec8-2e56cf93a5cd (the 8-character prefix @2677c96a and the label form "
    "@fleet-2677c96a-... are also accepted)."
)


def extract_fleet_reference(task: str) -> Tuple[Optional[str], Optional[str]]:
    """Return one explicit ``@<fleet-id>`` reference or a syntax error."""
    text = str(task or "")
    matches = [match.group(1).lower() for match in _FLEET_REF_RE.finditer(text)]
    unique = list(dict.fromkeys(matches))
    # A prefix and the full UUID it starts name the same Fleet.
    unique = [m for m in unique if not any(o != m and o.startswith(m) for o in unique)]
    if len(unique) > 1:
        return None, "The task contains multiple different Fleet references; select one Fleet."
    for match in _FLEET_SIGIL_RE.finditer(text):
        # Workflow advice can cite a generated Harness task (32 hex chars)
        # alongside an explicit Fleet UUID/prefix. It is not a second Fleet.
        # Without a valid Fleet reference, preserve the existing syntax error.
        if unique and re.fullmatch(r"@[0-9a-f]{32}", match.group(0), re.IGNORECASE):
            continue
        if not _FLEET_REF_RE.fullmatch(match.group(0)):
            return None, (
                f"{match.group(0)} is not a valid Fleet ID (use an 8-character hexadecimal prefix or a full"
                f" UUID).{FLEET_REFERENCE_SYNTAX}"
            )
    if unique:
        return unique[0], None
    return None, None
