"""harness.skill.registry — discover skills/ and match a task to a skill.

A skill is a reusable task capsule (see skills/README.md): SKILL.md frontmatter
(identity + trigger dims) + workflow.json (frozen Workflow.execute steps) +
fallback.yaml (success contract + takeover). This module does the deterministic
pre-filter (§5.2) and manual lookup (§5.1) — no LLM. The actual run is in
harness.skill.workflow.

Match dims (SKILL.md frontmatter vs task descriptor):
  domain      exact or wildcard (*.example.com) vs the task's target host
  task_type   exact
  stage_hint  exact
  fields      SKILL.md fields must be a subset of the task's expected fields

Usage:
    reg = SkillRegistry.load()
    skill = reg.match(domain="theresanaiforthat.com", task_type="web_scrape",
                      stage_hint="detail_sections", fields={"reviews","prosCons","qa"})
    skill = reg.get("taaft-detail-extract")        # manual /skill <id>
    python -m harness.skill.registry                # list + self-check
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import yaml

from harness.task_types import normalize_task_type

SKILLS_DIR_DEFAULT = Path(__file__).resolve().parent.parent.parent / "skills"


def _parse_frontmatter(md_text: str) -> Dict[str, Any]:
    """Extract the YAML frontmatter block delimited by leading '---' lines."""
    lines = md_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _domain_matches(skill_domain: str, task_domain: str) -> bool:
    skill_domain = (skill_domain or "").strip().lower()
    task_domain = (task_domain or "").strip().lower()
    if not skill_domain:
        return True  # skill declares no domain → matches any
    if not task_domain:
        return False
    if skill_domain.startswith("*."):
        suffix = skill_domain[1:]  # ".example.com"
        return task_domain == skill_domain[2:] or task_domain.endswith(suffix)
    return task_domain == skill_domain


@dataclass
class Skill:
    skill_id: str
    directory: Optional[Path]
    frontmatter: Dict[str, Any]
    workflow: Dict[str, Any]
    fallback: Dict[str, Any] = field(default_factory=dict)
    skill_md: str = ""

    @property
    def domain(self) -> str:
        return str(self.frontmatter.get("domain") or "")

    @property
    def task_type(self) -> str:
        return str(self.frontmatter.get("task_type") or "")

    @property
    def stage_hint(self) -> str:
        return str(self.frontmatter.get("stage_hint") or "")

    @property
    def fields(self) -> Set[str]:
        return {str(f) for f in (self.frontmatter.get("fields") or [])}

    @property
    def description(self) -> str:
        return str(self.frontmatter.get("description") or "")

    @property
    def steps(self) -> List[Dict[str, Any]]:
        return list(self.workflow.get("steps") or [])

    @property
    def error_config(self) -> Dict[str, Any]:
        return dict(self.workflow.get("errorConfig") or {"onError": "stop"})

    @property
    def variable_template(self) -> Dict[str, Any]:
        return dict(self.workflow.get("variables") or {})

    @property
    def success_contract(self) -> Dict[str, Any]:
        return dict(self.fallback.get("success_contract") or {})

    def matches(
        self,
        *,
        domain: str = "",
        task_type: str = "",
        stage_hint: str = "",
        fields: Optional[Set[str]] = None,
    ) -> bool:
        if not _domain_matches(self.domain, domain):
            return False
        if (
            self.task_type
            and task_type
            and normalize_task_type(self.task_type) != normalize_task_type(task_type)
        ):
            return False
        if self.stage_hint and stage_hint and self.stage_hint != stage_hint:
            return False
        if self.fields and fields is not None and not self.fields.issubset(fields):
            return False
        return True


class SkillRegistry:
    def __init__(self, skills: Sequence[Skill]):
        self._skills: List[Skill] = list(skills)
        self._by_id: Dict[str, Skill] = {s.skill_id: s for s in skills}

    @classmethod
    def load(cls, skills_dir: Path | str = SKILLS_DIR_DEFAULT) -> "SkillRegistry":
        root = Path(skills_dir)
        skills: List[Skill] = []
        if not root.is_dir():
            return cls(skills)
        for child in sorted(root.iterdir()):
            # skip files and tooling dirs (_template, _tools, ...)
            if not child.is_dir() or child.name.startswith("_") or child.name.startswith("."):
                continue
            skill = cls._load_one(child)
            if skill is not None:
                skills.append(skill)
        return cls(skills)

    @staticmethod
    def _load_one(directory: Path) -> Optional[Skill]:
        skill_md = directory / "SKILL.md"
        workflow_json = directory / "workflow.json"
        if not skill_md.is_file() or not workflow_json.is_file():
            return None
        skill_md_text = skill_md.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(skill_md_text)
        try:
            workflow = json.loads(workflow_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
            return None
        fallback: Dict[str, Any] = {}
        fb = directory / "fallback.yaml"
        if fb.is_file():
            try:
                loaded = yaml.safe_load(fb.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    fallback = loaded
            except yaml.YAMLError as exc:
                # Don't crash, but make it loud: a malformed fallback.yaml means
                # the success_contract silently disappears (no gating).
                print(f"[skill_registry] WARNING: {fb} is malformed YAML "
                      f"(success_contract gating disabled): {exc}", file=sys.stderr)
                fallback = {}
        skill_id = str(frontmatter.get("name") or directory.name)
        return Skill(skill_id=skill_id, directory=directory,
                     frontmatter=frontmatter, workflow=workflow,
                     fallback=fallback, skill_md=skill_md_text)

    def all(self) -> List[Skill]:
        return list(self._skills)

    def get(self, skill_id: str) -> Optional[Skill]:
        """Manual selection (§5.1 /skill <id>)."""
        return self._by_id.get(skill_id)

    def candidates(
        self,
        *,
        domain: str = "",
        task_type: str = "",
        stage_hint: str = "",
        fields: Optional[Set[str]] = None,
    ) -> List[Skill]:
        return [
            s for s in self._skills
            if s.matches(domain=domain, task_type=task_type,
                         stage_hint=stage_hint, fields=fields)
        ]

    def soft_candidates(
        self,
        *,
        domain: str = "",
        task_type: str = "",
        stage_hint: str = "",
        fields: Optional[Set[str]] = None,
        text: str = "",
        limit: int = 5,
    ) -> List[Tuple[Skill, int, List[str]]]:
        """Soft-recall skill candidates for LLM selection.

        This intentionally does not hard-filter on keywords. Every dimension only
        contributes score/reasons; the LeadAgent decides by reading SKILL.md and
        the executor later performs deterministic safety/variable checks.
        """
        hits: List[Tuple[Skill, int, List[str]]] = []
        for skill in self._skills:
            score, reasons = _soft_skill_score(
                skill,
                domain=domain,
                task_type=task_type,
                stage_hint=stage_hint,
                fields=fields or set(),
                text=text,
            )
            if score > 0:
                hits.append((skill, score, reasons))
        hits.sort(key=lambda item: (-item[1], item[0].skill_id))
        return hits[: max(1, int(limit or 5))]

    def match(
        self,
        *,
        domain: str = "",
        task_type: str = "",
        stage_hint: str = "",
        fields: Optional[Set[str]] = None,
    ) -> Optional[Skill]:
        """Deterministic pre-filter (§5.2): return the unique hit, else None.

        Multiple candidates → None here (caller does a single LLM disambiguation
        over self.candidates(...)); no silent guessing.
        """
        hits = self.candidates(domain=domain, task_type=task_type,
                               stage_hint=stage_hint, fields=fields)
        return hits[0] if len(hits) == 1 else None


_TOKEN_RE = re.compile(r"[a-z0-9]+")

FIELD_ALIASES: Dict[str, Set[str]] = {
    "name": {"productname", "product", "tool", "toolname", "app", "appname"},
    "productname": {"name", "product", "tool", "toolname", "app", "appname"},
    "detailurl": {"url", "href", "link", "detail", "detailpage", "targeturl"},
    "url": {"detailurl", "href", "link", "detailpage", "targeturl"},
    "proscons": {"pros", "cons", "advantages", "disadvantages"},
    "pros": {"proscons", "advantages"},
    "cons": {"proscons", "disadvantages"},
    "qa": {"q", "a", "question", "questions", "answers", "faq"},
    "reviews": {"review", "rating", "ratings", "comments", "testimonials"},
}


def _norm_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _tokenize(value: Any) -> Set[str]:
    return set(_TOKEN_RE.findall(str(value or "").lower()))


def _expanded_field_tokens(fields: Set[str]) -> Set[str]:
    out: Set[str] = set()
    for field in fields:
        norm = _norm_token(field)
        if not norm:
            continue
        out.add(norm)
        out.update(FIELD_ALIASES.get(norm, set()))
    return out


# Output fields too generic to indicate a skill's recipe actually fits a phase:
# almost every scrape names a rank/title/url/name, so overlap on these alone is
# not evidence. A skill must share a *distinctive* output field (reviews,
# prosCons, qa, ...) or an exact stage_hint to justify interrupting the Lead.
GENERIC_OUTPUT_FIELD_TOKENS: Set[str] = {
    "rank", "name", "productname", "product", "tool", "toolname", "app",
    "appname", "title", "url", "detailurl", "detail", "detailpage", "href",
    "link", "targeturl", "index", "position", "id", "row", "item",
}


def skill_selection_signal(
    skill: Skill,
    *,
    domain: str = "",
    stage_hint: str = "",
    fields: Optional[Set[str]] = None,
) -> bool:
    """True if there is a *structural* reason to surface `skill` to the Lead.

    Soft recall (keyword/description overlap, bare domain match) is deliberately
    loose for ranking, but too loose to interrupt the Lead with a selection
    request: nearly any scrape shares common tokens with some SKILL.md, and one
    domain can host both collection and detail phases. Require an exact
    stage_hint match or a non-generic output-field overlap (e.g.
    reviews/prosCons/qa, not rank/name/detailUrl).

    Domain is a hard veto: a skill bound to a specific domain (e.g.
    theresanaiforthat.com) is inapplicable on a different host (e.g.
    detail.1688.com) no matter how well stage_hint/keywords line up. Surfacing it
    there only traps the Lead in a skill_selection_required loop. Generic skills
    (no declared domain) match any host; if the target host is unknown, a
    domain-bound skill is conservatively vetoed.
    """
    if not _domain_matches(skill.domain, domain):
        return False
    if stage_hint and skill.stage_hint and stage_hint == skill.stage_hint:
        return True
    task_fields = _expanded_field_tokens(fields or set()) - GENERIC_OUTPUT_FIELD_TOKENS
    skill_fields = _expanded_field_tokens(skill.fields) - GENERIC_OUTPUT_FIELD_TOKENS
    return bool(task_fields & skill_fields)


def _soft_skill_score(
    skill: Skill,
    *,
    domain: str,
    task_type: str,
    stage_hint: str,
    fields: Set[str],
    text: str,
) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    if domain and skill.domain:
        if _domain_matches(skill.domain, domain):
            score += 8
            reasons.append(f"domain matches {skill.domain}")
        elif domain.endswith("." + skill.domain.lower().lstrip("*.")):
            score += 4
            reasons.append(f"domain is related to {skill.domain}")

    if (
        task_type
        and skill.task_type
        and normalize_task_type(task_type) == normalize_task_type(skill.task_type)
    ):
        score += 3
        reasons.append(f"task_type matches {normalize_task_type(task_type)}")

    if stage_hint and skill.stage_hint and stage_hint == skill.stage_hint:
        score += 4
        reasons.append(f"stage_hint matches {stage_hint}")

    task_fields = _expanded_field_tokens(fields)
    skill_fields = _expanded_field_tokens(skill.fields)
    field_overlap = sorted(task_fields & skill_fields)
    if field_overlap:
        score += min(6, len(field_overlap) * 2)
        reasons.append("field/alias overlap: " + ", ".join(field_overlap[:8]))

    haystack = " ".join([
        skill.skill_id,
        skill.domain,
        skill.task_type,
        skill.stage_hint,
        skill.description,
        " ".join(skill.fields),
        skill.skill_md[:5000],
    ])
    skill_tokens = _tokenize(haystack)
    query_tokens = _tokenize(text)
    query_tokens.update(_tokenize(" ".join(fields)))
    keyword_overlap = sorted(
        token for token in (query_tokens & skill_tokens)
        if len(token) >= 3
    )
    if keyword_overlap:
        score += min(5, len(keyword_overlap))
        reasons.append("keyword overlap: " + ", ".join(keyword_overlap[:10]))

    return score, reasons


def _self_check() -> int:
    reg = SkillRegistry.load()
    skills = reg.all()
    print(f"loaded {len(skills)} skill(s) from {SKILLS_DIR_DEFAULT}")
    for s in skills:
        print(f"  - {s.skill_id}: domain={s.domain!r} task_type={s.task_type!r} "
              f"stage_hint={s.stage_hint!r} fields={sorted(s.fields)} steps={len(s.steps)}")
    # demonstrate a deterministic match against a TAAFT-like task descriptor
    hit = reg.match(domain="theresanaiforthat.com", task_type="web_scrape",
                    stage_hint="detail_sections",
                    fields={"rank", "productName", "detailUrl", "reviews", "prosCons", "qa"})
    print(f"\nmatch(TAAFT detail task) -> {hit.skill_id if hit else None}")
    miss = reg.match(domain="example.com", task_type="web_scrape", stage_hint="collection")
    print(f"match(unrelated task)    -> {miss.skill_id if miss else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
