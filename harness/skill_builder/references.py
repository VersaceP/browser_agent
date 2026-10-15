"""Resolve Skill identities before choosing an execution or draft version."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.skill_builder.catalog import SkillCatalog


class AmbiguousSkillReference(ValueError):
    def __init__(self, reference: str, matches: list[dict[str, Any]]):
        self.matches = matches
        lines = [f"Multiple Skills match {reference!r}; enter an exact Skill name or ID:"]
        for row in matches:
            lines.append(f"  Name: {row['name']} | Skill ID: {row['skill_id']}")
        super().__init__("\n".join(lines))


class AmbiguousSkillDraft(ValueError):
    def __init__(self, reference: str, matches: list[dict[str, Any]]):
        self.matches = matches
        lines = [f"Multiple Builder drafts match {reference!r}; select a draft:"]
        for row in matches:
            metadata = row.get("metadata") or {}
            lines.append(f"  Task ID: {row['builder_task_id']} | Version: "
                         f"{metadata.get('version') or 'unversioned'} | Status: {row['status']}")
        super().__init__("\n".join(lines))


def draft_skill_id(row: dict[str, Any]) -> str:
    return str(row.get("published_skill_id") or row.get("source_skill_id")
               or (row.get("metadata") or {}).get("name") or "")


class SkillReferences:
    def __init__(self, catalog: SkillCatalog, worktree: Path):
        self.catalog = catalog
        self.worktree = worktree
        self._drafts: list[dict[str, Any]] | None = None

    def builder_id(self, reference: str) -> dict[str, Any] | None:
        # Task IDs are always exact, including internal references from saved runs.
        row = self.catalog.connection.execute(
            """SELECT s.* FROM skill_builder_sessions s JOIN tasks t
               ON t.task_id=s.builder_task_id WHERE s.builder_task_id=?
               AND s.status!='discarded' AND t.is_deleted=0 AND t.purge_status='none'
               AND s.work_rel_path NOT LIKE 'coding/workflows/%'""",
            (reference.removeprefix("@"),),
        ).fetchone()
        return dict(row) if row else None

    def matching_skills(self, reference: str, *, include_drafts: bool = False) -> list[dict[str, Any]]:
        """Exact ID, then case-insensitive exact name, then name containment.

        IDs never accept prefixes. Draft versions are grouped by Skill identity;
        selecting a name does not implicitly select the most recent draft.
        """
        groups: dict[str, dict[str, Any]] = {}
        for row in self.catalog.list():
            name = str(json.loads(row.get("metadata_json") or "{}").get("name") or row["skill_id"])
            groups[row["skill_id"]] = {"skill_id": row["skill_id"], "name": name,
                                       "names": {name}, "published": row}
        if include_drafts:
            for row in self.drafts():
                skill_id = draft_skill_id(row)
                if not skill_id:
                    continue
                name = str((row.get("metadata") or {}).get("name") or skill_id)
                group = groups.setdefault(skill_id, {"skill_id": skill_id, "name": name,
                                                     "names": set(), "published": None})
                group["names"].add(name)
        if not reference or reference.startswith("@"):
            return []
        if reference in groups:
            return [groups[reference]]
        needle = reference.casefold()
        ordered = [groups[key] for key in sorted(groups)]
        exact = [row for row in ordered if any(needle == name.casefold() for name in row["names"])]
        if exact:
            return exact
        return [row for row in ordered if any(needle in name.casefold() for name in row["names"])]

    def identity(self, reference: str, *, include_drafts: bool = False) -> dict[str, Any]:
        matches = self.matching_skills(reference, include_drafts=include_drafts)
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise AmbiguousSkillReference(reference, matches)
        raise ValueError(f"Unknown Skill {reference!r}. Use /skill to list Skills.")

    def drafts(self, reference: str | None = None) -> list[dict[str, Any]]:
        if self._drafts is None:
            self._drafts = self.catalog.list_drafts(self.worktree)
        if reference is None:
            return self._drafts
        builder = self.builder_id(reference)
        if builder:
            return [row for row in self._drafts if row["builder_task_id"] == builder["builder_task_id"]]
        if reference.startswith("@"):
            return []
        matches = self.matching_skills(reference, include_drafts=True)
        if len(matches) > 1:
            raise AmbiguousSkillReference(reference, matches)
        identities = {row["skill_id"] for row in matches}
        return [row for row in self._drafts if draft_skill_id(row) in identities]

    def builder(self, reference: str) -> dict[str, Any]:
        candidates = self.drafts(reference)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise AmbiguousSkillDraft(reference, candidates)
        raise ValueError(f"No Builder draft matches {reference!r}. Use /skill to list Skills and drafts.")

    def published(self, reference: str) -> dict[str, Any]:
        # Keep exact historical Builder references readable by internal tools.
        builder = self.builder_id(reference)
        if builder and (reference.startswith("@") or not self.matching_skills(reference)):
            target = builder.get("published_skill_id") or builder.get("source_skill_id")
            for row in self.catalog.list():
                if row["skill_id"] == target:
                    return row
        matches = self.matching_skills(reference)
        if len(matches) == 1:
            return matches[0]["published"]
        if matches:
            raise AmbiguousSkillReference(reference, matches)
        if self.drafts(reference):
            raise ValueError(f"Skill {reference!r} is not published. Use /skill to view its drafts.")
        raise ValueError(f"Unknown Skill {reference!r}. Use /skill to list Skills.")

    def scope(self, reference: str, requested: str | None = None) -> str:
        """Retained for internal file viewers; CLI mutations choose explicit versions."""
        if requested:
            if requested not in {"draft", "published"}:
                raise ValueError("Scope must be draft or published.")
            return requested
        if self.builder_id(reference):
            if not reference.startswith("@") and self.matching_skills(reference):
                raise ValueError(f"Ambiguous reference {reference!r}; select a draft or published version.")
            return "draft"
        identity = self.identity(reference, include_drafts=True)
        return "published" if identity["published"] else "draft"
