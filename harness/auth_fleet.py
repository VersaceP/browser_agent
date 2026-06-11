"""
harness.auth_fleet - Stable Memory scope names for reusable authenticated fleets.

This module intentionally does not store credentials. It only centralizes the
names and shape used when agents record that a verified browser fleet/session
can be reused for the same origin/account label.
"""

from __future__ import annotations

from typing import Optional

from harness.utils import JsonDict


AUTH_FLEET_MEMORY_SCOPE = "auth_fleet_sessions_v1"
AUTH_FLEET_MEMORY_KIND = "auth_fleet_session_index"


def build_auth_fleet_entry(
    *,
    origin: str,
    account_label: str,
    fleet_id: str,
    page_id: str = "",
    verified_at: str = "",
    stale: bool = False,
) -> JsonDict:
    return {
        "kind": AUTH_FLEET_MEMORY_KIND,
        "origin": str(origin or ""),
        "accountLabel": str(account_label or ""),
        "fleetId": str(fleet_id or ""),
        "pageId": str(page_id or ""),
        "verifiedAt": str(verified_at or ""),
        "stale": bool(stale),
    }


def auth_fleet_memory_guidance() -> JsonDict:
    return {
        "scope": AUTH_FLEET_MEMORY_SCOPE,
        "kind": AUTH_FLEET_MEMORY_KIND,
        "allowedFields": [
            "origin",
            "accountLabel",
            "fleetId",
            "pageId",
            "verifiedAt",
            "stale",
        ],
        "forbidden": [
            "plaintext passwords",
            "private tokens",
            "cookies",
            "localStorage values",
            "one-time codes",
        ],
    }


def is_auth_fleet_entry(value: Optional[JsonDict]) -> bool:
    return isinstance(value, dict) and value.get("kind") == AUTH_FLEET_MEMORY_KIND
