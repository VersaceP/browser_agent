"""harness.skill.registry — discover skills/ and match a task to a skill.

A skill is a reusable task capsule (see skills/README.md): SKILL.md frontmatter
(identity + trigger dims) + workflow.json (frozen Workflow.execute steps) +
fallback.yaml (success contract + takeover). This module does the deterministic
pre-filter (§5.2) and manual lookup (§5.1) — no LLM. The actual run is in
harness.skill.workflow.

workflow.json is OPTIONAL (07-07 guidance layer): a directory with only
SKILL.md loads as a *hints-only* (guidance) skill — no fast path, its value is
the SKILL.md hints section injected into the slow-path worker context (see
harness.skill.guidance). A present-but-malformed workflow.json still rejects
the whole skill (fail loud, not silently hints-only).

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

from harness.skill.structured_output import validate_structured_output_workflow
from harness.task_types import normalize_task_type

SKILLS_DIR_DEFAULT = Path(__file__).resolve().parent.parent.parent / "skills"

# Per-skill generation report written by /skill-create (provenance, quality
# verdict, human-readable failures, next actions). Runtime state — gitignored
# like .skill_health.json. Readers: CLI markers, selected_skill_context (so the
# slow-path LLM knows a blocked draft's gaps), --recheck/--retry.
CREATE_REPORT_FILENAME = ".create_report.json"

ROW_CONTRACT_VARIABLE_TYPES = {
    "scalar",
    "string",
    "integer",
    "number",
    "boolean",
    "uri",
}


def load_create_report(directory: Any) -> Dict[str, Any]:
    if not directory:
        return {}
    try:
        path = Path(directory) / CREATE_REPORT_FILENAME
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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


def _truthy_flag(value: Any) -> bool:
    """Frontmatter flags arrive as bool from YAML or as strings from hand edits."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1", "on"}


def validate_row_contract(
    raw: Any,
    workflow: Optional[Dict[str, Any]] = None,
    success_contract: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str]]:
    """Validate the declarative contract used for batch row joins.

    The contract deliberately describes roles, not site-specific field names.
    A missing contract remains valid for legacy/single-run skills; a present but
    malformed contract is rejected so runtime enrichment never guesses.
    """
    if raw is None:
        return True, []
    if not isinstance(raw, dict):
        return False, ["row_contract must be an object"]
    failures: List[str] = []
    if raw.get("version") != 1:
        failures.append("row_contract.version must be 1")

    def _names(key: str, *, required: bool = False) -> List[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            if required or value is not None:
                failures.append(f"row_contract.{key} must be an array")
            return []
        names = [str(item).strip() for item in value]
        if any(not name for name in names):
            failures.append(f"row_contract.{key} cannot contain blank names")
        names = [name for name in names if name]
        if len(names) != len(set(names)):
            failures.append(f"row_contract.{key} cannot contain duplicates")
        if required and not names:
            failures.append(f"row_contract.{key} must not be empty")
        return names

    identities = _names("identity_variables", required=True)
    passthrough = _names("passthrough_variables", required=True)
    produced = _names("produced_fields", required=True)
    template = set((workflow or {}).get("variables") or {})
    unknown_identity = sorted(set(identities) - template)
    unknown_passthrough = sorted(set(passthrough) - template)
    if unknown_identity:
        failures.append(
            "row_contract.identity_variables are not workflow variables: "
            + ", ".join(unknown_identity)
        )
    if unknown_passthrough:
        failures.append(
            "row_contract.passthrough_variables are not workflow variables: "
            + ", ".join(unknown_passthrough)
        )
    if not set(identities).issubset(set(passthrough)):
        failures.append(
            "row_contract.identity_variables must also be passthrough_variables"
        )

    field_map = (success_contract or {}).get("variable_to_field")
    field_map = field_map if isinstance(field_map, dict) else {}

    def _extract_variables(steps: Any) -> Set[str]:
        values: Set[str] = set()
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            extract = step.get("extract")
            if isinstance(extract, dict):
                values.update(str(name) for name in extract)
            for branch in ("then", "else", "body"):
                values.update(_extract_variables(step.get(branch)))
        return values

    actual_produced = {
        str(field_map.get(variable) or variable)
        for variable in _extract_variables((workflow or {}).get("steps"))
    }
    undeclared_outputs = sorted(set(produced) - actual_produced)
    if undeclared_outputs:
        failures.append(
            "row_contract.produced_fields are not workflow extract outputs: "
            + ", ".join(undeclared_outputs)
        )

    variable_types = raw.get("variable_types")
    if not isinstance(variable_types, dict):
        failures.append("row_contract.variable_types must be an object")
    else:
        for name, kind in variable_types.items():
            variable = str(name).strip()
            normalized_kind = str(kind).strip().lower()
            if variable not in template:
                failures.append(
                    f"row_contract.variable_types names unknown variable: {variable}"
                )
            if normalized_kind not in ROW_CONTRACT_VARIABLE_TYPES:
                failures.append(
                    f"row_contract.variable_types.{variable} must be one of "
                    + ", ".join(sorted(ROW_CONTRACT_VARIABLE_TYPES))
                )
        missing_types = sorted(set(passthrough) - set(variable_types))
        if missing_types:
            failures.append(
                "row_contract.variable_types missing passthrough variables: "
                + ", ".join(missing_types)
            )
    return not failures, failures


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

    @property
    def row_contract(self) -> Dict[str, Any]:
        return dict(self.fallback.get("row_contract") or {})

    @property
    def structured_output(self) -> Dict[str, Any]:
        """Optional multi-row JSON-string output from ordinary Workflow.execute."""
        raw = self.workflow.get("structured_output")
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def has_workflow(self) -> bool:
        """本 skill 是否带 workflow（快路径）层。"""
        return bool(self.workflow.get("steps"))

    @property
    def is_hints_only(self) -> bool:
        """guidance skill：没有冻结 workflow，慢路径就是执行路径（快路径直接
        跳过、不碰引擎、不记任何账——见 harness.skill.guidance 模块说明）。"""
        return not self.has_workflow

    @property
    def hints(self) -> str:
        """SKILL.md 的 `## 页面知识（hints）` 小节（无则空串）。"""
        from harness.skill.guidance import extract_hints_section
        return extract_hints_section(self.skill_md)

    @property
    def suite(self) -> str:
        """技能组名（frontmatter `suite:`，无则空串）。suite 是"选择别名"：
        `/skill <suite名>` 展开成所有 suite 成员，各 phase 按四维路由到成员
        （见 SkillRegistry.expand_selection）。"""
        return str(self.frontmatter.get("suite") or "").strip()

    @property
    def is_draft(self) -> bool:
        """/skill-create scaffolds carry `draft: true` until calibrated. Drafts
        are excluded from auto matching (match/candidates/soft_candidates) but
        stay reachable via explicit get() — /skill <id> is an informed choice
        (the CLI list marks them [draft])."""
        return _truthy_flag(self.frontmatter.get("draft"))

    @property
    def is_tested(self) -> bool:
        """`tested` absent means the skill predates the live-trial gate — treat
        as tested; only an explicit falsy value marks it untried."""
        value = self.frontmatter.get("tested")
        return True if value is None else _truthy_flag(value)

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
        # Canonicalized comparison: productUrl vs detailUrl must not split a
        # match (07-05 incident); unknown tokens stay distinct (conservative).
        if self.fields and fields is not None and not canonical_fields(self.fields).issubset(
            canonical_fields(fields)
        ):
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
        if not skill_md.is_file():
            return None
        skill_md_text = skill_md.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(skill_md_text)
        # workflow.json 可缺席（hints-only guidance skill）；但存在即必须合法——
        # 坏 JSON 不能静默降级成 guidance，否则 workflow skill 的损坏被吞掉。
        workflow: Dict[str, Any] = {}
        if workflow_json.is_file():
            try:
                workflow = json.loads(workflow_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
                return None
            structured_ok, structured_failures = validate_structured_output_workflow(
                workflow.get("structured_output"), workflow,
            )
            if not structured_ok:
                print(
                    f"[skill_registry] WARNING: {workflow_json} has invalid "
                    f"structured_output ({'; '.join(structured_failures)}); skill rejected",
                    file=sys.stderr,
                )
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
        row_contract = fallback.get("row_contract") if fallback else None
        row_contract_ok, row_contract_failures = validate_row_contract(
            row_contract,
            workflow,
            fallback.get("success_contract") if isinstance(fallback, dict) else None,
        )
        if not row_contract_ok:
            print(
                f"[skill_registry] WARNING: {fb} has an invalid row_contract "
                f"({'; '.join(row_contract_failures)}); skill rejected",
                file=sys.stderr,
            )
            return None
        skill_id = str(frontmatter.get("name") or directory.name)
        return Skill(skill_id=skill_id, directory=directory,
                     frontmatter=frontmatter, workflow=workflow,
                     fallback=fallback, skill_md=skill_md_text)

    def all(self) -> List[Skill]:
        return list(self._skills)

    def get(self, skill_id: str) -> Optional[Skill]:
        """Manual selection (§5.1 /skill <id>)."""
        return self._by_id.get(skill_id)

    def suite_members(self, suite: str) -> List[Skill]:
        """一个技能组的所有成员（frontmatter suite 匹配），skill_id 升序稳定。"""
        s = str(suite or "").strip()
        if not s:
            return []
        return sorted((sk for sk in self._skills if sk.suite == s),
                      key=lambda sk: sk.skill_id)

    def expand_selection(self, name: str) -> List[str]:
        """把用户 `/skill <name>` 的 name 展开成 skill_id 列表。

        name 是某个 skill_id → 返回 [name]（单选，语义不变）；name 是一个
        suite 名（有成员且不与任何 skill_id 冲突）→ 返回该组全部成员 id。
        既是 skill_id 又是 suite 名时，skill_id 优先（显式点名的 id 最具体）。
        无匹配 → 返回 [name]（交给下游 get() 报未知，保持既有错误路径）。"""
        n = str(name or "").strip()
        if not n:
            return []
        if n in self._by_id:
            return [n]
        members = self.suite_members(n)
        if members:
            return [sk.skill_id for sk in members]
        return [n]

    def candidates(
        self,
        *,
        domain: str = "",
        task_type: str = "",
        stage_hint: str = "",
        fields: Optional[Set[str]] = None,
        include_drafts: bool = False,
    ) -> List[Skill]:
        return [
            s for s in self._skills
            if (include_drafts or not s.is_draft)
            and s.matches(domain=domain, task_type=task_type,
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
        include_drafts: bool = False,
    ) -> List[Tuple[Skill, int, List[str]]]:
        """Soft-recall skill candidates for LLM selection.

        This intentionally does not hard-filter on keywords. Every dimension only
        contributes score/reasons; the LeadAgent decides by reading SKILL.md and
        the executor later performs deterministic safety/variable checks.
        Drafts are excluded by default: an uncalibrated scaffold must not be
        offered to the Lead as if it were a proven recipe.
        """
        hits: List[Tuple[Skill, int, List[str]]] = []
        for skill in self._skills:
            if skill.is_draft and not include_drafts:
                continue
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
        over self.candidates(...)); no silent guessing. Drafts never auto-match
        (candidates() excludes them); explicit get() is the only draft entry.
        """
        hits = self.candidates(domain=domain, task_type=task_type,
                               stage_hint=stage_hint, fields=fields)
        return hits[0] if len(hits) == 1 else None


_TOKEN_RE = re.compile(r"[a-z0-9]+")

FIELD_ALIASES: Dict[str, Set[str]] = {
    "name": {"productname", "product", "tool", "toolname", "app", "appname"},
    "productname": {"name", "product", "tool", "toolname", "app", "appname"},
    "detailurl": {"url", "href", "link", "detail", "detailpage", "targeturl", "producturl"},
    "url": {"detailurl", "href", "link", "detailpage", "targeturl", "producturl"},
    "proscons": {"pros", "cons", "advantages", "disadvantages"},
    "pros": {"proscons", "advantages"},
    "cons": {"proscons", "disadvantages"},
    "qa": {"q", "a", "question", "questions", "answers", "faq"},
    "reviews": {"review", "rating", "ratings", "comments", "testimonials"},
}


def _norm_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


# ONE canonicalization for the whole skill machinery. The 07-05 duplicate-skill
# incident came from call sites comparing fields differently (literal subset in
# one place, alias-expanded overlap in another), so productUrl vs detailUrl
# split the registry into two competing skills for the same page anatomy.
# Groups are deliberately conservative: only high-confidence synonyms merge;
# an unknown token canonicalizes to itself (fails toward "not equal", which
# escalates to a human/Lead instead of silently merging distinct fields).
# NOTE: pageUrl is deliberately NOT in the detailurl group — in extraction rows
# it is provenance ("observed on this page"), not the row's target URL.
_CANONICAL_FIELD_GROUPS: Dict[str, Set[str]] = {
    "detailurl": {"url", "href", "link", "detailpage", "targeturl", "producturl", "itemurl"},
    "productname": {"name", "product", "toolname", "appname"},
    "proscons": {"pros", "cons", "prosandcons", "advantages", "disadvantages"},
    "qa": {"faq", "questions", "answers", "qanda", "questionsandanswers"},
    "reviews": {"review", "ratings", "comments", "testimonials"},
    "rank": {"ranking", "position"},
}

_CANONICAL_BY_ALIAS: Dict[str, str] = {}
for _canon, _aliases in _CANONICAL_FIELD_GROUPS.items():
    _CANONICAL_BY_ALIAS[_canon] = _canon
    for _alias in _aliases:
        _CANONICAL_BY_ALIAS[_alias] = _canon


def canonical_field(value: Any) -> str:
    """Normalize a field name to its canonical representative
    (productUrl/detailUrl/href → detailurl; faq → qa; unknown → itself)."""
    norm = _norm_token(value)
    return _CANONICAL_BY_ALIAS.get(norm, norm)


def canonical_fields(values: Any) -> Set[str]:
    return {canonical_field(v) for v in (values or []) if _norm_token(v)}


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
    "link", "targeturl", "producturl", "itemurl", "index", "position", "id",
    "row", "item",
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
        layer = "hints-only" if s.is_hints_only else f"steps={len(s.steps)}"
        print(f"  - {s.skill_id}: domain={s.domain!r} task_type={s.task_type!r} "
              f"stage_hint={s.stage_hint!r} fields={sorted(s.fields)} {layer}")
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
