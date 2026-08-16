"""
harness.fleet.auth - Stable Memory scope names for reusable authenticated fleets.

This module intentionally does not store credentials. It only centralizes the
names and shape used when agents record that a verified browser fleet/session
can be reused for the same origin/account label.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from harness.utils import JsonDict


AUTH_FLEET_MEMORY_SCOPE = "auth_fleet_sessions_v1"
AUTH_FLEET_MEMORY_KIND = "auth_fleet_session_index"

_AUTH_AXTREE_LINE_RE = re.compile(
    r'^(?:\d+\s+)?\s*\[(?P<id>\d+:-?\d+:-?\d+)\]\s+'
    r'(?P<role>[^\s"]+)(?:\s+"(?P<name>.*?)")?(?P<rest>.*)$'
)
_AUTH_AXTREE_REJECTED_FLAGS = frozenset({"hidden", "blocked"})
_AUTH_MARKER_ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def canonical_origin(value: str) -> str:
    """Return the canonical HTTP(S) origin used by ledger and routing."""

    parsed = urlparse(str(value or "").strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = host if port is None or default_port else f"{host}:{port}"
    return f"{scheme}://{authority}"


def normalize_auth_verification_contract(value: object) -> JsonDict:
    """Validate the pre-HITL proof required for durable auth reuse.

    A successful HITL wait only clears the current fleet barrier.  Persisting a
    reusable authenticated session additionally requires both a protected URL
    match and a stable authenticated UI marker declared before the worker runs.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("auth_verification must be an object")

    def string_list(key: str) -> list[str]:
        raw = value.get(key)
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"auth_verification.{key} must be a non-empty array")
        result = []
        for item in raw:
            text = str(item or "").strip()
            if not text:
                raise ValueError(
                    f"auth_verification.{key} entries must be non-empty strings"
                )
            result.append(text)
        return result

    prefixes = string_list("protected_url_prefixes")
    raw_markers = value.get("authenticated_markers")
    if not isinstance(raw_markers, list) or not raw_markers:
        raise ValueError(
            "auth_verification.authenticated_markers must be a non-empty array"
        )
    markers: list[JsonDict] = []
    for raw_marker in raw_markers:
        if not isinstance(raw_marker, dict):
            raise ValueError(
                "auth_verification.authenticated_markers entries must be objects"
                " with role and name"
            )
        unknown = set(raw_marker) - {"role", "name", "match"}
        if unknown:
            raise ValueError(
                "auth_verification.authenticated_markers entries contain unknown"
                f" fields: {', '.join(sorted(unknown))}"
            )
        role = str(raw_marker.get("role") or "").strip().casefold()
        name = str(raw_marker.get("name") or "").strip()
        match = str(raw_marker.get("match") or "exact").strip().casefold()
        if not _AUTH_MARKER_ROLE_RE.fullmatch(role):
            raise ValueError(
                "auth_verification.authenticated_markers.role must be an AX role"
            )
        if len(name) < 3:
            raise ValueError(
                "auth_verification.authenticated_markers.name must be at least"
                " 3 characters"
            )
        if match != "exact":
            raise ValueError(
                "auth_verification.authenticated_markers.match must be exact"
            )
        markers.append({"role": role, "name": name, "match": "exact"})
    for prefix in prefixes:
        if not canonical_origin(prefix):
            raise ValueError(
                "auth_verification.protected_url_prefixes must contain HTTP(S) URLs"
            )
    return {
        "protected_url_prefixes": prefixes,
        "authenticated_markers": markers,
    }


def _protected_url_matches(url: str, prefix: str) -> bool:
    """Match a declared protected URL without host or path-prefix confusion."""

    if not url or canonical_origin(url) != canonical_origin(prefix):
        return False
    candidate = urlparse(url)
    declared = urlparse(prefix)
    candidate_path = candidate.path or "/"
    declared_path = declared.path or "/"
    if declared_path == "/":
        path_matches = True
    elif candidate_path == declared_path:
        path_matches = True
    elif declared_path.endswith("/"):
        path_matches = candidate_path.startswith(declared_path)
    else:
        path_matches = candidate_path.startswith(declared_path + "/")
    if not path_matches:
        return False
    return not declared.query or candidate.query == declared.query


def _normalized_accessible_name(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _visible_auth_nodes(tree_text: str) -> list[JsonDict]:
    nodes: list[JsonDict] = []
    for line in str(tree_text or "").splitlines():
        match = _AUTH_AXTREE_LINE_RE.match(line)
        if not match:
            continue
        rest = str(match.group("rest") or "")
        flags = set(rest.replace("#", " ").split())
        if flags & _AUTH_AXTREE_REJECTED_FLAGS:
            continue
        nodes.append({
            "role": str(match.group("role") or "").casefold(),
            "name": str(match.group("name") or ""),
        })
    return nodes


def verify_protected_auth_target(evidence: JsonDict) -> JsonDict:
    """Evaluate protected-target reachability without trusting worker claims."""

    try:
        contract = normalize_auth_verification_contract(
            evidence.get("verificationContract")
        )
    except ValueError as exc:
        return {"verified": False, "reason": str(exc)}
    if not contract:
        return {"verified": False, "reason": "auth_verification_contract_required"}

    url = str(evidence.get("url") or "").strip()
    matched_prefix = next(
        (
            prefix
            for prefix in contract["protected_url_prefixes"]
            if _protected_url_matches(url, prefix)
        ),
        "",
    )
    if not matched_prefix:
        return {"verified": False, "reason": "protected_url_not_reached"}

    nodes = _visible_auth_nodes(str(evidence.get("axTreeText") or ""))
    matched_marker = next(
        (
            marker
            for marker in contract["authenticated_markers"]
            if any(
                node["role"] == marker["role"]
                and _normalized_accessible_name(node["name"])
                == _normalized_accessible_name(marker["name"])
                for node in nodes
            )
        ),
        None,
    )
    if not matched_marker:
        return {"verified": False, "reason": "authenticated_marker_not_observed"}
    return {
        "verified": True,
        "matchedUrlPrefix": matched_prefix,
        "matchedAuthenticatedMarker": matched_marker,
    }


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


class AuthFleetLedger:
    """Harness-owned persistent index of evidence-verified auth fleets.

    The file stores handles and non-secret evidence metadata only. Credentials,
    cookies, storage values and one-time codes are never accepted.
    """

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, JsonDict] = {}
        self._load()

    def _load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            value = {}
        rows = value.get("entries") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("sessionKey") or "").strip()
            fleet_id = str(row.get("fleetId") or "").strip()
            if key and fleet_id:
                self._entries[key] = dict(row)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "entries": sorted(
                self._entries.values(),
                key=lambda item: str(item.get("sessionKey") or ""),
            ),
        }
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)

    def entries(self) -> List[JsonDict]:
        return [dict(item) for item in self._entries.values()]

    def entries_for_inventory(
        self,
        *,
        owner_agent_id: str,
        fleet_ids: Iterable[str],
    ) -> List[JsonDict]:
        owner = str(owner_agent_id or "").strip()
        inventory = {str(item or "").strip() for item in fleet_ids}
        return [
            dict(item)
            for item in self._entries.values()
            if not item.get("stale")
            and str(item.get("fleetId") or "") in inventory
            and (
                not str(item.get("ownerAgentId") or "").strip()
                or str(item.get("ownerAgentId") or "").strip() == owner
            )
        ]

    def record_verified(self, evidence: JsonDict) -> JsonDict:
        session_key = str(evidence.get("sessionKey") or "").strip()
        fleet_id = str(evidence.get("fleetId") or "").strip()
        page_id = str(evidence.get("pageId") or "").strip()
        url = str(evidence.get("url") or "").strip()
        proof = evidence.get("evidence")
        origin = canonical_origin(url)
        target_proof = verify_protected_auth_target(evidence)
        trusted = (
            isinstance(proof, dict)
            and proof.get("pageStateObserved") is True
            and proof.get("axTreeObserved") is True
            and proof.get("hitlPaused") is False
            and target_proof.get("verified") is True
        )
        if not session_key:
            return {"recorded": False, "reason": "session_key_required"}
        if not fleet_id or not page_id or not origin or not trusted:
            return {
                "recorded": False,
                "reason": (
                    str(target_proof.get("reason") or "")
                    if not target_proof.get("verified")
                    else "insufficient_trusted_evidence"
                ),
            }
        row: JsonDict = {
            "kind": AUTH_FLEET_MEMORY_KIND,
            "sessionKey": session_key,
            "fleetId": fleet_id,
            "pageId": page_id,
            "origin": origin,
            # No model assertion is accepted as account identity evidence.
            "accountLabel": None,
            "ownerAgentId": str(evidence.get("ownerAgentId") or "").strip(),
            "sessionGeneration": int(evidence.get("sessionGeneration") or 1),
            "isIsolated": True,
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
            "evidenceSummary": {
                "pageStateObserved": True,
                "axTreeObserved": True,
                "hitlPaused": False,
                "url": url,
                "title": str(evidence.get("title") or "")[:300],
                "matchedUrlPrefix": target_proof.get("matchedUrlPrefix"),
                "matchedAuthenticatedMarker": target_proof.get(
                    "matchedAuthenticatedMarker"
                ),
            },
            "stale": False,
        }
        self._entries[session_key] = row
        self._save()
        return {"recorded": True, "entry": dict(row)}

    def mark_stale(
        self,
        session_key: str,
        *,
        reason: str,
        fleet_id: str = "",
        expected_generation: int = 0,
    ) -> JsonDict:
        key = str(session_key or "").strip()
        row = self._entries.get(key)
        if row is None:
            return {"updated": False, "reason": "not_found"}
        expected = str(fleet_id or "").strip()
        if expected and str(row.get("fleetId") or "") != expected:
            return {"updated": False, "reason": "fleet_mismatch"}
        expected_gen = int(expected_generation or 0)
        if expected_gen and int(row.get("sessionGeneration") or 0) != expected_gen:
            return {"updated": False, "reason": "generation_mismatch"}
        row["stale"] = True
        row["staleReason"] = str(reason or "")[:500]
        row["staleAt"] = datetime.now(timezone.utc).isoformat()
        self._save()
        return {"updated": True, "entry": dict(row)}
