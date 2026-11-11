"""Host-owned Workshop persistence authority, separate from execution grants.

Neither a model-authored worker contract nor a manifest can create this grant.
No current Browser entry issues publishing authority. A future explicit author
publisher can supply an exact record scope; run/completion always starts read-only.
Absent grants on resume are read-only, never reconstructed from conversation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


WRITE_METHODS = frozenset({"Workflow.upsert", "Workflow.remove"})


@dataclass(frozen=True)
class WorkshopAccess:
    role: str = "run"
    writable_ids: frozenset[str] = frozenset()
    create: bool = False

    def __post_init__(self):
        if (self.role not in {"run", "author"} or type(self.create) is not bool
                or not isinstance(self.writable_ids, frozenset)
                or any(not isinstance(item, str) or not item for item in self.writable_ids)):
            raise ValueError("Workshop authority requires a role, boolean create grant and immutable record ID set")


READ_ONLY = WorkshopAccess()


def access_for(owner: Any) -> WorkshopAccess:
    config = getattr(getattr(owner, "runtime", None), "harness", None)
    grant = getattr(config, "_workshop_access", None)
    return grant if isinstance(grant, WorkshopAccess) else READ_ONLY


def write_denial(owner: Any, method: str, params: dict | None = None) -> dict | None:
    if method not in WRITE_METHODS:
        return None
    grant = access_for(owner)
    if grant.role == "author":
        if params is None:
            permitted = bool(grant.writable_ids or (method == "Workflow.upsert" and grant.create))
        else:
            identity = params.get("workshopId")
            permitted = (identity in grant.writable_ids if isinstance(identity, str) else
                         method == "Workflow.upsert" and identity is None and grant.create)
        if permitted:
            return None
    return {"status": "permission_denied", "method": method, "tool_was_executed": False,
            "error": "Workshop persistence requires host-issued author authority for this record.",
            "next_instruction": "Complete the authorized task with existing workflows and Browser operations. "
                "Record package defects; modifying or publishing a package belongs to an explicitly authorized author flow."}
