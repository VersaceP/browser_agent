"""harness.skill.dispatch — try a matching skill's fast path before the LLM loop.

Called from spawner._run_browser_worker right before harness.run(). If a skill
matches the worker_contract AND its required variables are derivable, run the
skill's frozen workflow (fast path, no page-level LLM); on success persist via
record_extraction and return a completed worker answer so the LLM loop is
skipped. On no-match / undrivable vars / failure / contract-unmet, return None
so the normal BrowserAgent loop runs (slow path).

Gated by config.harness.skill.fast_path_enabled. Fires only when a skill matches,
so it is naturally scoped and reversible.

The `agent` only needs: .browser (ABCPClient-like .call), .logger, .runtime,
.artifacts — so this is unit-testable with a mock agent.
"""
from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.skill.pause import HitlOnsetMonitor, classify_run_for_hitl
from harness.skill.registry import Skill, SkillRegistry, canonical_field
from harness.skill.structured_output import structured_output_rows
from harness.skill.workflow import (
    check_persisted_contract,
    check_success_contract,
    run_skill_workflow,
)

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", str(url or ""))
    return (m.group(1) if m else "").lower().lstrip("www.")


def _find_url(domain: str, sources: Sequence[Any]) -> Optional[str]:
    """Find the first URL whose host matches the skill domain in any source."""
    dom = (domain or "").lower().lstrip("*.").lstrip("www.")
    for src in sources:
        if src is None:
            continue
        text = src if isinstance(src, str) else json.dumps(src, ensure_ascii=False, default=str)
        for url in _URL_RE.findall(text):
            host = _host_of(url)
            if not dom or host == dom or host.endswith("." + dom):
                return url
    return None


def _contract_fields(worker_contract: Dict[str, Any]) -> set:
    expected = worker_contract.get("expected_artifact")
    fields = expected.get("fields") if isinstance(expected, dict) else None
    if not isinstance(fields, list):
        fields = worker_contract.get("fields")
    return {str(f) for f in fields} if isinstance(fields, list) else set()


def select_skill(
    registry: SkillRegistry,
    worker_contract: Dict[str, Any],
    *,
    target_url: str = "",
    mode: str = "manual",
) -> Optional[Skill]:
    """Explicit skill_id always wins; auto-match only in mode="auto".

    mode="manual" (default, 2026-07-06 user decision): a skill engages ONLY via
    an explicit choice (forced `/skill <id>` → worker_contract.skill_id, or a
    Lead-set selection) — no registry auto-match, so an uncalibrated draft can
    never steal execution from the slow path."""
    try:
        from harness.skill.contract import skill_selection_declined
        if skill_selection_declined(worker_contract):
            return None
    except Exception:  # pragma: no cover
        pass
    skill_id = str(worker_contract.get("skill_id") or "").strip()
    if not skill_id:
        selection = worker_contract.get("skill_selection")
        if isinstance(selection, dict) and selection.get("use_skill") is not False:
            skill_id = str(selection.get("skill_id") or "").strip()
    if skill_id:
        return registry.get(skill_id)  # explicit /skill <id> — human opted in
    if mode != "auto":
        return None
    task_type = str(worker_contract.get("task_type") or "")
    stage_hint = str(worker_contract.get("stage_hint") or "")
    fields = _contract_fields(worker_contract) or None
    # Guard against a DOMAIN-ONLY auto-match: with no task dimension, a same-domain
    # but unrelated task would mis-fire the fast path — opening/navigating a page,
    # possibly tripping a challenge, burning a slot, AND recording a health failure
    # on the inevitable contract-unmet that could rot/disable a perfectly good skill
    # (doc §5.2: deterministic prefilter needs a task_plan; bare domain ⇒ slow path).
    if not (task_type or stage_hint or fields):
        return None
    domain = _host_of(target_url) or str(worker_contract.get("domain") or "")
    return registry.match(
        domain=domain, task_type=task_type, stage_hint=stage_hint, fields=fields,
    )


def _expected_fields_of(worker_contract: Optional[Dict[str, Any]]) -> List[str]:
    expected = (worker_contract or {}).get("expected_artifact")
    fields = expected.get("fields") if isinstance(expected, dict) else None
    return [str(f) for f in fields if str(f)] if isinstance(fields, list) else []


def _align_row_fields_to_expected(
    row: Dict[str, Any], expected_fields: Sequence[str],
) -> Dict[str, Any]:
    """Rename row keys to the PLAN's expected field names when canonically
    equivalent (productUrl↔detailUrl). Plan-side naming drifts task to task and
    record_extraction's schema check compares LITERALLY — in task fa86c5f6 a
    batch of 11 real rows was discarded as needs_fix only because the skill
    persisted productUrl while that day's plan said detailUrl. Conservative:
    rename only when the expected field is missing from the row, exactly ONE
    row key is canonically equal, and that key is not itself an expected field."""
    if not expected_fields or not isinstance(row, dict):
        return row
    expected = [str(f) for f in expected_fields if str(f)]
    expected_set = set(expected)
    for field in expected:
        if field in row:
            continue
        matches = [
            k for k in row
            if k not in expected_set and canonical_field(k) == canonical_field(field)
        ]
        if len(matches) == 1:
            row[field] = row.pop(matches[0])
    return row


def _is_explicit_selection(worker_contract: Dict[str, Any]) -> bool:
    """True when the skill engagement is a human/Lead EXPLICIT choice (forced
    /skill → worker_contract.skill_id, or a skill_selection.skill_id) — the
    same signal select_skill's explicit branch keys on. Direct explicit runs
    bypass health entirely. A suite_routed skill is phase-matched rather than a
    partial-fit force, so it deliberately returns False and keeps health outcome
    accounting enabled."""
    if not isinstance(worker_contract, dict):
        return False
    from harness.skill.contract import is_suite_routed
    if is_suite_routed(worker_contract):
        return False
    if str(worker_contract.get("skill_id") or "").strip():
        return True
    selection = worker_contract.get("skill_selection")
    if isinstance(selection, dict) and selection.get("use_skill") is not False:
        return bool(str(selection.get("skill_id") or "").strip())
    return False


def _url_variable(skill: Skill) -> Optional[str]:
    """The variable a skill actually navigates to (the `$vars.<x>` in its
    Page.navigate step), so the URL-derivation fallback isn't pinned to hardcoded
    names like detailUrl/targetUrl. None when the nav URL isn't a bare $vars ref."""
    for step in skill.steps:
        if isinstance(step, dict) and step.get("action") == "Page.navigate":
            url = str(((step.get("params") or {}).get("url")) or "").strip()
            m = re.match(r"^\$vars\.([A-Za-z0-9_]+)$", url)
            if m:
                return m.group(1)
    return None


def derive_variables(
    skill: Skill,
    worker_contract: Dict[str, Any],
    phase: Optional[Dict[str, Any]],
    task: str,
    context: str,
) -> Dict[str, Any]:
    template = skill.variable_template
    explicit = worker_contract.get("skill_variables")
    explicit = explicit if isinstance(explicit, dict) else {}
    sources: List[Dict[str, Any]] = [
        explicit,
        worker_contract,
        worker_contract.get("expected_artifact") if isinstance(worker_contract.get("expected_artifact"), dict) else {},
        phase or {},
    ]
    url_var = _url_variable(skill)
    out: Dict[str, Any] = {}
    for key, default in template.items():
        val: Any = None
        for src in sources:
            if isinstance(src, dict) and src.get(key) not in (None, ""):
                val = src.get(key)
                break
        if val is None and (key == url_var or key.lower() in ("detailurl", "url", "targeturl")):
            val = _find_url(skill.domain, [explicit.get(key), worker_contract, phase, task, context])
        out[key] = val if val is not None else default
    return out


def _referenced_vars(skill: Skill) -> set:
    """Template vars actually used by the steps (as $vars.<key>) — these are the
    inputs the workflow needs to run. Others (e.g. rank/productName) are output
    passthrough and need not be derivable for the fast path to fire."""
    try:
        blob = json.dumps(skill.steps, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return set()
    return {k for k in skill.variable_template if f"$vars.{k}" in blob}


def required_filled(skill: Skill, variables: Dict[str, Any]) -> bool:
    """Every var referenced by the steps must be non-empty after derivation."""
    for key in _referenced_vars(skill):
        if not str(variables.get(key) or "").strip():
            return False
    return True


def _field_map(skill: Skill) -> Dict[str, str]:
    """Optional variable→field rename a skill declares in
    fallback.yaml success_contract.variable_to_field. Empty ⇒ the variable name IS
    the field name (no harness-side *Text convention baked in)."""
    mapping = skill.success_contract.get("variable_to_field")
    return {str(k): str(v) for k, v in mapping.items()} if isinstance(mapping, dict) else {}


def _extract_provenance(skill: Skill) -> str:
    """The action a skill reads its content with (its last extract-bearing step),
    used as the row's sourceSelectorOrAxId instead of assuming Runtime.evaluate."""
    action = ""
    for step in skill.steps:
        if isinstance(step, dict) and step.get("extract") and step.get("action"):
            action = str(step.get("action"))
    return action or "Workflow.execute"


def build_extraction_row(
    skill: Skill,
    run_result: Dict[str, Any],
    input_variables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """variables → one record_extraction row. Field names come from the skill's
    declared variable_to_field map (else the variable name verbatim — no implicit
    *Text stripping); provenance is the skill's own extract action.

    input_variables (the values the fast path RAN with) merge UNDER the engine
    output: output wins, except an empty echoed value never overwrites a
    non-empty input. This removes the dependency on the engine echoing input
    variables back — an observed but unpromised behavior."""
    merged: Dict[str, Any] = {
        k: v
        for k, v in (input_variables or {}).items()
        if k not in ("pageId", "fleetId")
    }
    for k, v in (run_result.get("variables") or {}).items():
        if k in ("pageId", "fleetId"):
            continue
        if (
            (v is None or (isinstance(v, str) and not v.strip()))
            and str(merged.get(k) or "").strip()
        ):
            continue
        merged[k] = v
    field_map = _field_map(skill)
    row: Dict[str, Any] = {}
    for k, v in merged.items():
        row[field_map.get(k, k)] = v
    row.setdefault("sourceTool", f"Workflow.execute(skill:{skill.skill_id})")
    row.setdefault("sourceSelectorOrAxId", _extract_provenance(skill))
    return row


def _completed_answer(skill: Skill, run_result: Dict[str, Any], artifact: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "outcome": "completed_via_skill",
            "skill": skill.skill_id,
            "runId": run_result.get("runId"),
            "data": {k: v for k, v in (run_result.get("variables") or {}).items()
                     if k not in ("pageId", "fleetId")},
            "evidence": [f"skill {skill.skill_id} workflow completed; success_contract satisfied"],
            "artifact": artifact.get("savedPath") or artifact.get("relativePath"),
            "next_steps": "none; skill fast path persisted the row",
        },
        ensure_ascii=False,
    )


def _artifact_name(skill: Skill, worker_contract: Dict[str, Any]) -> str:
    """The phase's expected artifact name when declared, else a skill-derived
    fallback. validate_worker_artifacts filters artifacts BY NAME (task_control
    artifact_required is a blocking failure), so persisting under a skill-derived
    name in a named phase would make record_extraction return needs_fix and the
    fast path veto itself on its own success."""
    expected = worker_contract.get("expected_artifact") if isinstance(worker_contract, dict) else None
    name = str(expected.get("name") or "").strip() if isinstance(expected, dict) else ""
    return name or f"{skill.skill_id}-extraction"


def _normalized_page(url: str) -> tuple:
    """(host, path) with scheme/query/fragment/trailing-slash/www stripped —
    query must be ignored because a cleared Cloudflare challenge can leave a
    residual __cf_chl_rt_tk param on the real page URL."""
    m = re.match(r"https?://([^/?#]+)([^?#]*)", str(url or "").strip())
    if not m:
        return ("", str(url or "").strip().rstrip("/"))
    host = m.group(1).lower()
    if host.startswith("www."):
        host = host[4:]
    path = (m.group(2) or "/").rstrip("/") or "/"
    return (host, path)


def _iter_steps_deep(steps: Any) -> Any:
    """Walk steps depth-first, descending into if.then/else AND loop.body.
    (control.py's _iter_steps covers then/else only; a binding declaration
    inside a loop body must still count here, or the fail-closed gate would
    silently fail OPEN for that skill.)"""
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        yield step
        for branch in ("then", "else", "body"):
            yield from _iter_steps_deep(step.get(branch))


def _declares_page_binding(skill: Skill) -> bool:
    """True when the workflow itself promises a pageUrl extraction (the v2
    Page.getState binding step), anywhere in the step tree. Only such skills
    are held to fail-closed binding below — a skill without the step cannot
    be blamed for not producing pageUrl."""
    for step in _iter_steps_deep(skill.steps):
        extract = step.get("extract")
        if isinstance(extract, dict) and "pageUrl" in extract:
            return True
    return False


def page_binding_mismatch(
    skill: Skill,
    run_result: Dict[str, Any],
    variables: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    """Prove the extracted content came from THIS run's page. The v2 recipe
    extracts pageUrl via Page.getState; with a soft-failing navigate
    (onError:continue) a same-tab batch would otherwise silently read the
    PREVIOUS row's sections and the any-nonempty contract could not tell.

    FAIL-CLOSED for skills that declare the binding step: pageUrl missing from
    the run (Page.getState itself soft-failed) means provenance is UNKNOWN —
    reason "page_binding_unknown", never a pass. Skills without a declared
    binding are not enforced. Returns {"expected", "actual", "reason"} on
    mismatch/unknown, None when satisfied or not applicable."""
    url_var = _url_variable(skill)
    if not url_var:
        return None
    expected = str(variables.get(url_var) or "").strip()
    if not expected:
        return None
    actual = str((run_result.get("variables") or {}).get("pageUrl") or "").strip()
    if not actual:
        if _declares_page_binding(skill):
            return {"expected": expected, "actual": "", "reason": "page_binding_unknown"}
        return None
    if _normalized_page(expected) == _normalized_page(actual):
        return None
    return {"expected": expected, "actual": actual, "reason": "wrong_page"}


def workflow_challenge_signal(
    skill: Skill,
    run_result: Dict[str, Any],
    variables: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    """Detect a final challenge surface without misreading residual CF query.

    TAAFT may keep ``__cf_chl_*`` in the query after clearance, so URL markers
    count only when the normalized host/path also differs from the intended
    navigation target. A challenge title is independently conclusive.
    """
    result_variables = run_result.get("variables")
    result_variables = result_variables if isinstance(result_variables, dict) else {}
    title = str(result_variables.get("pageTitle") or "")
    actual_url = str(result_variables.get("pageUrl") or "")
    try:
        from harness.hitl import _is_challenge_url
        from harness.skill.pause import _title_is_challenge
        if _title_is_challenge(title):
            return {"kind": "challenge_title", "title": title, "url": actual_url}
        url_var = _url_variable(skill)
        expected_url = str(variables.get(url_var) or "") if url_var else ""
        url_is_distinct_challenge = (
            _is_challenge_url(actual_url)
            and (
                not expected_url
                or _normalized_page(actual_url) != _normalized_page(expected_url)
            )
        )
        if url_is_distinct_challenge:
            return {
                "kind": "challenge_url",
                "title": title,
                "url": actual_url,
            }
    except Exception:
        return None
    return None


def skill_rows(worker_contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The batch input a Lead attaches for a multi-row detail phase: one variable
    dict per row (e.g. {rank, productName, detailUrl}). Non-dict entries are
    dropped rather than failing the whole batch shape."""
    rows = worker_contract.get("skill_rows")
    if not isinstance(rows, list):
        return []
    return [{str(k): v for k, v in row.items()} for row in rows if isinstance(row, dict)]


def _phase_expected_rows(worker_contract: Dict[str, Any]) -> Optional[int]:
    """The phase's own row-count demand (exact_rows first, else min_rows) from
    worker_contract validators. None when the phase declares no count."""
    validators = worker_contract.get("validators") if isinstance(worker_contract, dict) else None
    if not isinstance(validators, list):
        return None
    exact: Optional[int] = None
    minimum: Optional[int] = None
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        vtype = str(validator.get("type") or "")
        if vtype not in ("exact_rows", "min_rows"):
            continue
        raw = validator.get("value")
        if raw is None:
            raw = (
                validator.get("count")
                or validator.get("exact")
                or validator.get("rows")
                or validator.get("min")
            )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if vtype == "exact_rows":
            exact = value
        else:
            minimum = max(minimum or 0, value)
    return exact if exact is not None else minimum


def _phase_rank_window(
    worker_contract: Dict[str, Any], field: str,
) -> Optional[Tuple[int, int]]:
    """Inclusive numeric range for a structured-output rank field."""
    validators = worker_contract.get("validators")
    if not isinstance(validators, list):
        return None
    wanted = canonical_field(field)
    for validator in validators:
        if not isinstance(validator, dict) or validator.get("type") != "range":
            continue
        if canonical_field(str(validator.get("field") or "")) != wanted:
            continue
        try:
            minimum = int(validator.get("min"))
            maximum = int(validator.get("max"))
        except (TypeError, ValueError):
            return None
        return (minimum, maximum) if minimum <= maximum else None
    return None


def _structured_runtime_variables(
    skill: Skill,
    worker_contract: Dict[str, Any],
    phase: Optional[Dict[str, Any]],
    task: str,
    context: str,
) -> Dict[str, Any]:
    """Fill declared scalar inputs used by a standard multi-row workflow."""
    config = skill.structured_output
    runtime_names = config.get("runtime_variables")
    runtime_names = runtime_names if isinstance(runtime_names, dict) else {}
    rank = config.get("rank")
    rank = rank if isinstance(rank, dict) else {}
    rank_field = str(rank.get("field") or "rank")
    window = _phase_rank_window(worker_contract, rank_field)
    target_url = _find_url(skill.domain, [worker_contract, phase, task, context])
    expected_rows = _phase_expected_rows(worker_contract)
    values = {
        str(runtime_names.get("target_url") or "targetUrl"): target_url,
        str(runtime_names.get("rank_min") or "minRank"): window[0] if window else None,
        str(runtime_names.get("rank_max") or "maxRank"): window[1] if window else None,
        str(runtime_names.get("expected_rows") or "expectedRows"): expected_rows,
    }
    template = skill.variable_template
    return {
        key: value for key, value in values.items()
        if key in template and value not in (None, "")
    }


def _row_field_value(row: Dict[str, Any], field: str) -> Any:
    """Row value for a PLAN field name, bridging canonical synonyms
    (detailUrl↔productUrl) — validators speak plan names, auto-built rows
    speak skill variable names."""
    if field in row:
        return row.get(field)
    canon = canonical_field(field)
    for key, value in row.items():
        if canonical_field(key) == canon:
            return value
    return None


def _row_passes_validators(row: Dict[str, Any], validators: Any) -> bool:
    """Apply the phase's ROW-level validators (range / field_pattern /
    url_pattern / allowed_domain) to one candidate row. Set-level validators
    (exact_rows/unique/required_fields/...) are judged on the whole set by the
    caller. A row missing the referenced field fails closed — membership in
    this phase's slice cannot be verified, so it must not be included."""
    if not isinstance(validators, list):
        return True
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        vtype = str(validator.get("type") or "")
        field = str(validator.get("field") or "").strip()
        if vtype == "range" and field:
            raw = _row_field_value(row, field)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return False
            minimum = validator.get("min")
            maximum = validator.get("max")
            if minimum is not None and value < float(minimum):
                return False
            if maximum is not None and value > float(maximum):
                return False
        elif vtype in ("field_pattern", "url_pattern") and field:
            pattern = str(validator.get("pattern") or "").strip()
            if not pattern:
                continue
            raw = str(_row_field_value(row, field) or "")
            try:
                if not re.search(pattern, raw):
                    return False
            except re.error:
                continue
        elif vtype == "allowed_domain":
            field_name = field or "detailUrl"
            domain = str(
                validator.get("domain") or validator.get("value") or ""
            ).strip().lower().lstrip("*.")
            if not domain:
                continue
            host = _host_of(str(_row_field_value(row, field_name) or ""))
            if not (host == domain or host.endswith("." + domain)):
                return False
        elif vtype == "set_equals" and field:
            # At phase validation this asserts equality of the whole observed
            # set. During upstream row selection its row-local counterpart is
            # membership: retain exactly the explicitly named non-contiguous
            # values, then exact_rows/set_equals validate the final batch.
            expected = {
                str(item).strip()
                for item in (validator.get("values") or [])
                if str(item).strip()
            }
            if expected and str(_row_field_value(row, field) or "").strip() not in expected:
                return False
    return True


def _provenance_evidence_requirements(validators: Any) -> Dict[str, str]:
    """field → evidence-field name demanded by the phase's field_provenance
    validators (mirrors _validate_field_provenance's spec parsing: fields as
    list, dict-of-specs, or single `field`; evidence field defaults to
    `<field>EvidenceText`)."""
    out: Dict[str, str] = {}
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "field_provenance":
            continue
        raw = validator.get("fields") or validator.get("field_provenance")
        if isinstance(raw, list):
            specs: Dict[str, Dict[str, Any]] = {
                str(f): {} for f in raw if str(f).strip()
            }
        elif isinstance(raw, dict):
            specs = {
                str(f): (s if isinstance(s, dict) else {})
                for f, s in raw.items()
                if str(f).strip()
            }
        else:
            field = str(validator.get("field") or "").strip()
            specs = {field: {}} if field else {}
        for field, spec in specs.items():
            evidence = str(
                spec.get("evidence_field")
                or validator.get("evidence_field")
                or f"{field}EvidenceText"
            ).strip()
            if evidence:
                out[field] = evidence
    return out


def _unsatisfiable_evidence_fields(
    skill: Skill,
    worker_contract: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> List[str]:
    """Evidence fields the phase's field_provenance validators demand that the
    fast path can NEVER mint: not a skill output (variable template /
    variable_to_field), and not already present on every input row (inherited
    provenance from build_skill_rows_from_artifacts or a Lead-filled skill_rows).
    A worker on the slow path cites its sources as it extracts; a frozen
    workflow cannot — running it anyway just burns a browser pass before the
    inevitable phase-validation failure (task 2ed5a466 p3, three attempts)."""
    requirements = _provenance_evidence_requirements(
        worker_contract.get("validators") if isinstance(worker_contract, dict) else None
    )
    if not requirements:
        return []
    if not rows:
        # Single-run path: a Lead-filled skill_variables dict IS the one input
        # row — evidence carried there satisfies the demand exactly like a
        # skill_rows entry would (review P3: judging producibility only would
        # wrongly skip a legitimate single-row fast path).
        explicit = (
            worker_contract.get("skill_variables")
            if isinstance(worker_contract, dict) else None
        )
        if isinstance(explicit, dict) and explicit:
            rows = [explicit]
    producible = set(skill.variable_template.keys())
    producible.update(_field_map(skill).values())
    producible_canon = {canonical_field(str(k)) for k in producible}
    missing: List[str] = []
    for _field, evidence_field in requirements.items():
        if canonical_field(evidence_field) in producible_canon:
            continue
        if rows and all(
            str(_row_field_value(row, evidence_field) or "").strip()
            for row in rows
        ):
            continue
        missing.append(evidence_field)
    return sorted(set(missing))


def _normalized_row_contract_value(skill: Skill, variable: str, value: Any) -> str:
    """Type-aware equality for declared row inputs.

    Runtime joins do not infer field semantics from names. The skill creator
    declares each passthrough variable's comparison type; ``scalar`` provides a
    conservative numeric bridge for common JSON int/string drift.
    """
    if value is None:
        return ""
    kind = str(
        (skill.row_contract.get("variable_types") or {}).get(variable) or "string"
    ).strip().lower()
    if kind == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value).strip().lower()
        return text if text in {"true", "false"} else str(value).strip()
    text = str(value).strip()
    numeric = kind in {"integer", "number"} or (
        kind == "scalar"
        and bool(re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text))
    )
    if numeric:
        try:
            number = Decimal(text)
            if not number.is_finite():
                return text
            if kind == "integer" and number != number.to_integral_value():
                return text
            normalized = format(number.normalize(), "f")
            if "." in normalized:
                normalized = normalized.rstrip("0").rstrip(".")
            return normalized or "0"
        except (InvalidOperation, ValueError):
            return text
    return text


def _row_identity_key(
    skill: Skill,
    row: Dict[str, Any],
) -> Optional[Tuple[str, ...]]:
    identities = skill.row_contract.get("identity_variables") or []
    if not isinstance(identities, list) or not identities:
        return None
    values: List[str] = []
    for variable in identities:
        name = str(variable)
        value = _row_field_value(row, name)
        normalized = _normalized_row_contract_value(skill, name, value)
        if not normalized:
            return None
        values.append(normalized)
    return tuple(values)


def _core_projection(
    skill: Skill,
    candidate_rows: List[Dict[str, Any]],
) -> List[str]:
    """Unordered projection of declared workflow inputs, excluding evidence."""
    template_vars = list(skill.variable_template.keys())
    projected = [
        {
            variable: _normalized_row_contract_value(
                skill, variable, row.get(variable),
            )
            for variable in template_vars
            if variable in row and row.get(variable) is not None
        }
        for row in candidate_rows
    ]
    return sorted(
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        for item in projected
    )


def _build_skill_rows_from_artifacts_with_info(
    agent: Any,
    skill: Skill,
    worker_contract: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Auto-build skill_rows for a multi-row phase from UPSTREAM validated
    artifacts, so a batch phase gets its batch input even when the Lead forgot
    to attach skill_rows (task 9d5655d3: the prompt instructed it, the lead
    didn't comply — prompt-only contracts are unreliable, so mechanize it).

    Deterministic and guess-free:
    - rows come only from task_state phases that finished validated_done, or
      the task_state global artifact ledger (which is credited only after a
      phase validates and survives replans);
    - artifact rows map to skill variable names via canonical_field, and a row
      must fill the skill-declared identity variables (legacy skills fall back
      to their step-referenced URL variable);
    - THIS phase's own row validators (range etc.) select the slice;
    - the filtered set must meet the phase row-count demand (== exact_rows, or
      >= min_rows);
    - two upstream artifacts producing DIFFERENT qualifying sets ⇒ ambiguous ⇒
      return [] (never guess between candidates).
    """
    expected = _phase_expected_rows(worker_contract)
    if not expected or expected <= 1:
        return [], {"reason": "phase_not_batch"}
    try:
        from harness.task_control import load_task_state
        state = load_task_state(agent.logger)
    except Exception:
        return [], {"reason": "task_state_unavailable"}
    phases = state.get("phases") if isinstance(state, dict) else None
    if not isinstance(phases, dict):
        return [], {"reason": "phase_ledger_unavailable"}
    current_phase = str(worker_contract.get("phase_id") or "")
    validators = worker_contract.get("validators")
    evidence_requirements = _provenance_evidence_requirements(validators)
    template_vars = list(skill.variable_template.keys())
    url_var = _url_variable(skill)
    identity_vars = [
        str(value)
        for value in (skill.row_contract.get("identity_variables") or [])
        if str(value).strip()
    ]
    if not identity_vars and url_var:
        identity_vars = [url_var]
    has_exact = any(
        isinstance(v, dict) and str(v.get("type") or "") == "exact_rows"
        for v in (validators if isinstance(validators, list) else [])
    )

    artifact_paths: List[str] = []
    for phase_id, phase_state in phases.items():
        if not isinstance(phase_state, dict) or str(phase_id) == current_phase:
            continue
        if str(phase_state.get("status") or "") != "validated_done":
            continue
        # Validated-quality sources only, but across ALL attempts plus the
        # durable phase-level ledger — reading just attempts[-1] broke the
        # moment a later failed attempt was appended after validated_done
        # (review finding: the auto-build then skipped exactly the case it
        # exists to save). Never the allExtraction/attempt keys: those can
        # carry needs_fix saves.
        candidates: List[str] = []
        validated = phase_state.get("validated_artifacts")
        if isinstance(validated, list):
            candidates.extend(validated)
        attempts = phase_state.get("attempts")
        for attempt in attempts if isinstance(attempts, list) else []:
            if not isinstance(attempt, dict):
                continue
            validation = attempt.get("validation")
            if not isinstance(validation, dict):
                continue
            # validExtractionArtifacts is validated-by-construction (emptied
            # on failure at the source). validation.artifacts is only the
            # SELECTED artifact and is populated on failed validations too —
            # trust it solely on a "done" validation, or a failed attempt's
            # needs_fix artifact would pollute the candidates / manufacture
            # ambiguity (review finding).
            value = validation.get("validExtractionArtifacts")
            if isinstance(value, list):
                candidates.extend(value)
            if str(validation.get("status") or "") == "done":
                value = validation.get("artifacts")
                if isinstance(value, list):
                    candidates.extend(value)
        for path in candidates:
            if str(path) and str(path) not in artifact_paths:
                artifact_paths.append(str(path))

    phase_artifact_paths = set(artifact_paths)

    # Replan replaces the active phase map, but task_state.artifacts is the
    # durable validated ledger and intentionally survives. Without this source,
    # a remediation replan loses its collection inputs and falls back with
    # batch_requires_skill_rows even though the validated artifact is on disk.
    global_artifacts = state.get("artifacts")
    for path in global_artifacts if isinstance(global_artifacts, list) else []:
        text = str(path or "").strip()
        if text and text not in artifact_paths:
            artifact_paths.append(text)

    phase_qualifying: List[List[Dict[str, Any]]] = []
    phase_sources: List[str] = []
    global_qualifying: List[List[Dict[str, Any]]] = []
    global_sources: List[str] = []
    for path in artifact_paths:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        raw_rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(raw_rows, list):
            continue
        mapped: List[Dict[str, Any]] = []
        seen_identities: set = set()
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                continue
            candidate: Dict[str, Any] = {}
            for var in template_vars:
                value = _row_field_value(raw_row, var)
                if value is not None and str(value).strip() != "":
                    candidate[var] = value
            # Inherited provenance: when THIS phase's validators demand
            # evidence for a passthrough field (e.g. rank), carry the upstream
            # row's own VALIDATED evidence along — the value's proof lives one
            # hop upstream, and re-proving it on a detail page is impossible
            # for a frozen workflow (task 2ed5a466 p3: 3 attempts burned on
            # missing rankEvidenceText).
            for _field, evidence_field in evidence_requirements.items():
                value = _row_field_value(raw_row, evidence_field)
                if value is not None and str(value).strip() != "":
                    candidate.setdefault(evidence_field, value)
            if identity_vars:
                if skill.row_contract:
                    identity = _row_identity_key(skill, candidate)
                else:
                    values = tuple(
                        str(candidate.get(variable) or "").strip()
                        for variable in identity_vars
                    )
                    identity = values if all(values) else None
                if identity is None:
                    continue
                if identity in seen_identities:
                    continue
                seen_identities.add(identity)
            if not _row_passes_validators(candidate, validators):
                continue
            mapped.append(candidate)
        count_ok = (
            len(mapped) == expected if has_exact else len(mapped) >= expected
        )
        if mapped and count_ok:
            if path in phase_artifact_paths:
                phase_qualifying.append(mapped)
                phase_sources.append(path)
            else:
                global_qualifying.append(mapped)
                global_sources.append(path)

    # Active validated phases are the closest, plan-scoped upstream contract.
    # Consult the cross-replan global ledger only when that tier has no usable
    # source; unrelated historical artifacts must not manufacture ambiguity.
    qualifying = phase_qualifying or global_qualifying
    qualifying_sources = phase_sources if phase_qualifying else global_sources

    if not qualifying:
        return [], {"reason": "no_qualifying_validated_source"}
    first = qualifying[0]

    first_projection = _core_projection(skill, first)
    for other in qualifying[1:]:
        if _core_projection(skill, other) != first_projection:
            _log(agent, "skill.fast_path.auto_rows_ambiguous", {
                "skill": skill.skill_id,
                "sources": qualifying_sources,
                "expectedRows": expected,
            })
            return [], {
                "reason": "ambiguous_validated_sources",
                "sources": qualifying_sources,
            }
    # Evidence the upstream row did not carry verbatim: attest the inheritance
    # itself. A citation of the validated source artifact is the exact form the
    # slow path mints for passthrough fields (accepted by field_provenance in
    # task 2ed5a466 p2), keyed by the row's URL variable for join audit.
    # AFTER the ambiguity check: the citation embeds the source basename, and
    # injecting it earlier would make identical row sets from two artifacts
    # read as different (manufactured ambiguity).
    if evidence_requirements:
        source_name = qualifying_sources[0].rsplit("/", 1)[-1]
        for row_out in first:
            for field, evidence_field in evidence_requirements.items():
                if str(_row_field_value(row_out, evidence_field) or "").strip():
                    continue
                value = _row_field_value(row_out, field)
                if value is None or str(value).strip() == "":
                    continue  # nothing to attest; the preflight gate decides
                identity_parts = [
                    f"{variable}={row_out.get(variable)}"
                    for variable in identity_vars
                    if str(row_out.get(variable) or "").strip()
                ]
                join = "; " + ", ".join(identity_parts) if identity_parts else ""
                row_out[evidence_field] = (
                    f"{field} {value} inherited from validated upstream "
                    f"artifact {source_name}{join}"
                )
    return first, {
        "reason": "validated_source",
        "source": qualifying_sources[0],
        "sources": qualifying_sources,
        "sourceTier": "phase" if phase_qualifying else "global",
    }


def build_skill_rows_from_artifacts(
    agent: Any,
    skill: Skill,
    worker_contract: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows, _info = _build_skill_rows_from_artifacts_with_info(
        agent, skill, worker_contract,
    )
    return rows


def enrich_explicit_skill_rows_from_artifacts(
    agent: Any,
    skill: Skill,
    worker_contract: Dict[str, Any],
    explicit_rows: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fill only declared passthrough/provenance gaps in explicit skill rows.

    The candidate set is built by the exact same validator-filtered source
    selector as auto_rows. Enrichment is allowed only when the normalized,
    unordered identity sets are equal and every explicit non-empty passthrough
    value agrees with the validated source. Rows are never added/dropped and
    non-empty Lead values are never overwritten.
    """
    row_contract = skill.row_contract
    identities = row_contract.get("identity_variables")
    passthrough = row_contract.get("passthrough_variables")
    if not isinstance(identities, list) or not identities:
        info = {"reason": "row_contract_identity_unavailable"}
        _log(agent, "skill.fast_path.rows_enrichment_skipped", {
            "skill": skill.skill_id, **info,
        })
        return explicit_rows, info
    if not isinstance(passthrough, list):
        info = {"reason": "row_contract_passthrough_unavailable"}
        _log(agent, "skill.fast_path.rows_enrichment_skipped", {
            "skill": skill.skill_id, **info,
        })
        return explicit_rows, info

    candidates, source_info = _build_skill_rows_from_artifacts_with_info(
        agent, skill, worker_contract,
    )
    if not candidates:
        info = {"reason": source_info.get("reason") or "validated_source_unavailable"}
        _log(agent, "skill.fast_path.rows_enrichment_skipped", {
            "skill": skill.skill_id,
            "rowCount": len(explicit_rows),
            **info,
        })
        return explicit_rows, info

    def _index(rows: List[Dict[str, Any]]) -> Optional[Dict[Tuple[str, ...], Dict[str, Any]]]:
        indexed: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for row in rows:
            key = _row_identity_key(skill, row)
            if key is None or key in indexed:
                return None
            indexed[key] = row
        return indexed

    explicit_by_id = _index(explicit_rows)
    candidates_by_id = _index(candidates)
    if (
        explicit_by_id is None
        or candidates_by_id is None
        or set(explicit_by_id) != set(candidates_by_id)
    ):
        info = {
            "reason": "identity_set_mismatch",
            "explicitRows": len(explicit_rows),
            "candidateRows": len(candidates),
        }
        _log(agent, "skill.fast_path.rows_enrichment_rejected", {
            "skill": skill.skill_id, **info,
        })
        return explicit_rows, info

    for identity, explicit in explicit_by_id.items():
        candidate = candidates_by_id[identity]
        for variable in passthrough:
            name = str(variable)
            explicit_value = _row_field_value(explicit, name)
            if explicit_value is None or not str(explicit_value).strip():
                continue
            candidate_value = _row_field_value(candidate, name)
            if candidate_value is None or not str(candidate_value).strip():
                info = {
                    "reason": "explicit_value_unverifiable",
                    "variable": name,
                }
                _log(agent, "skill.fast_path.rows_enrichment_rejected", {
                    "skill": skill.skill_id, **info,
                })
                return explicit_rows, info
            if _normalized_row_contract_value(
                skill, name, explicit_value,
            ) != _normalized_row_contract_value(skill, name, candidate_value):
                info = {
                    "reason": "explicit_value_conflict",
                    "variable": name,
                }
                _log(agent, "skill.fast_path.rows_enrichment_rejected", {
                    "skill": skill.skill_id, **info,
                })
                return explicit_rows, info

    evidence_fields = set(
        _provenance_evidence_requirements(
            worker_contract.get("validators")
        ).values()
    )
    fillable = {str(item) for item in passthrough if str(item).strip()}
    fillable.update(evidence_fields)
    enriched_rows: List[Dict[str, Any]] = []
    filled_fields: set = set()
    for explicit in explicit_rows:
        identity = _row_identity_key(skill, explicit)
        candidate = candidates_by_id[identity]
        enriched = dict(explicit)
        for field in fillable:
            exact_existing = enriched.get(field)
            if exact_existing is not None and str(exact_existing).strip():
                continue
            existing = _row_field_value(enriched, field)
            if existing is not None and str(existing).strip():
                # The explicit row used a canonically equivalent plan field.
                # Preserve it and add the exact workflow-variable spelling so
                # the frozen recipe can consume the value without alias logic.
                enriched[field] = existing
                filled_fields.add(field)
                continue
            value = _row_field_value(candidate, field)
            if value is None or not str(value).strip():
                continue
            enriched[field] = value
            filled_fields.add(field)
        enriched_rows.append(enriched)

    info = {
        "reason": "validated_rows_equivalent",
        "rowCount": len(enriched_rows),
        "filledFields": sorted(filled_fields),
        "source": source_info.get("source"),
        "sourceTier": source_info.get("sourceTier"),
    }
    _log(agent, "skill.fast_path.rows_enriched", {
        "skill": skill.skill_id, **info,
    })
    return enriched_rows, info


_TRANSIENT_WORKFLOW_ERROR_RE = re.compile(
    r"("
    r"-32001|timed? ?out|timeout|transport|websocket|"
    r"connection[_ ](closed|reset|refused|aborted)|"
    r"err_network|err_timed_out|econnreset|socket hang ?up"
    r")",
    re.I,
)


def _is_transient_workflow_failure(run_result: Dict[str, Any]) -> bool:
    """Engine-level failure whose error text reads as infrastructure-transient
    (timeout / dropped connection). Contract-unmet and structural failures are
    deterministic — retrying replays the same result and only burns a real
    browser run, so they never match here."""
    if run_result.get("succeeded"):
        return False
    text = " ".join(
        str(run_result.get(key) or "")
        for key in ("failedError", "exc", "observation")
    )
    return bool(_TRANSIENT_WORKFLOW_ERROR_RE.search(text))


async def _run_with_transient_retry(
    agent: Any,
    skill: Skill,
    *,
    run_id: str,
    page_id: str,
    fleet_id: str,
    variables: Dict[str, Any],
    event_prefix: str = "skill.fast_path",
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """One bounded retry, ONLY for transient engine failures on the same warm
    tab. HITL/challenge interruptions and deterministic failures pass through
    untouched — those belong to the hand-off/fallback ladders."""
    agent._selected_skill_workflow_attempted = True
    run_result, observed_signal = await _run_skill_with_optional_control(
        agent, skill, run_id=run_id, page_id=page_id, fleet_id=fleet_id,
        variables=variables,
    )
    if classify_run_for_hitl(run_result, observed_signal) is not None:
        return run_result, observed_signal
    if not _is_transient_workflow_failure(run_result):
        return run_result, observed_signal
    _log(agent, f"{event_prefix}.transient_retry", {
        "skill": skill.skill_id,
        "runId": run_id,
        "failedError": run_result.get("failedError"),
        "exc": run_result.get("exc"),
    })
    return await _run_skill_with_optional_control(
        agent, skill, run_id=f"{run_id}-t2", page_id=page_id,
        fleet_id=fleet_id, variables=variables,
    )


def _derive_row_variables(
    skill: Skill,
    worker_contract: Dict[str, Any],
    row: Dict[str, Any],
) -> Dict[str, Any]:
    """Per-row derivation: base skill_variables overlaid with the row (row wins),
    template defaults applied. Deliberately does NOT run the task/contract URL
    fallback the single-run path uses — in a batch, a same-domain URL fished out
    of the task text or another row would be a DIFFERENT row's page, so a row
    missing its URL variable must stay unfilled and hand off instead."""
    base = worker_contract.get("skill_variables") if isinstance(worker_contract, dict) else None
    merged = dict(base) if isinstance(base, dict) else {}
    merged.update(row)
    return derive_variables(skill, {"skill_variables": merged}, None, "", "")


def _batch_handoff_note(
    skill: Skill,
    rows: List[Dict[str, Any]],
    completed_count: int,
    *,
    reason: str,
    partial_artifact: Dict[str, Any],
    failed_row: Optional[Dict[str, Any]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> str:
    """Slow-path continuation contract (mirrors Lead rule 11: remainingItems +
    existingArtifactPath + trusted rows). The slow path must extract ONLY the
    remaining rows and record ONE final artifact containing ALL rows so phase
    validators like exact_rows still see a single complete artifact."""
    remaining = rows[completed_count:]
    lines = [
        f"Skill '{skill.skill_id}' fast path completed {completed_count} of {len(rows)} rows, then stopped: {reason}.",
    ]
    if failed_row:
        lines.append(f"Failing row: {json.dumps(failed_row, ensure_ascii=False, default=str)}")
    if details:
        lines.append(f"Failure details: {json.dumps(details, ensure_ascii=False, default=str)}")
    saved = (partial_artifact or {}).get("savedPath") or (partial_artifact or {}).get("relativePath")
    if saved:
        lines.append(
            f"The {completed_count} completed rows are trusted and persisted at: {saved} — "
            "read them back with local_fs_read; do NOT re-scrape them."
        )
    lines.append(
        "Remaining rows to extract: "
        + json.dumps(remaining, ensure_ascii=False, default=str)
    )
    lines.append(
        "Extract ONLY the remaining rows, then record ONE final extraction artifact "
        f"containing ALL {len(rows)} rows (merge the trusted completed rows back in), "
        "so phase validators see a single complete artifact."
    )
    return "\n".join(lines)


def _persist_partial_rows(
    agent: Any,
    skill: Skill,
    built_rows: List[Dict[str, Any]],
    record_extraction: Any,
    *,
    artifact_name: str,
) -> Dict[str, Any]:
    """Durably persist rows completed before a mid-batch handoff (best-effort).
    Named '<expected>-partial', NOT the expected name itself: it is a handoff
    buffer for the slow path to merge from — the slow path records the single
    final artifact under the expected name."""
    if not built_rows or record_extraction is None:
        return {}
    try:
        return record_extraction(agent, {
            "name": f"{artifact_name}-partial",
            "rows": built_rows,
            "description": (
                f"Partial batch persisted by skill fast path before slow-path handoff: "
                f"{skill.skill_id} ({len(built_rows)} rows)"
            ),
        }) or {}
    except Exception as exc:  # pragma: no cover - persistence best-effort
        _log(agent, "skill.fast_path.partial_persist_error",
             {"skill": skill.skill_id, "error": str(exc)})
    return {}


def _repair_row_identity(
    row: Dict[str, Any],
    all_rows: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Choose one stable, unique key the slow path can echo in a patch row."""
    priority = (
        "detailUrl", "detail_url", "productUrl", "product_url", "url", "href",
        "rank", "position", "productName", "name", "title",
    )
    for field in priority:
        value = row.get(field)
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        matches = sum(
            1 for candidate in all_rows
            if (
                str(candidate.get(field)).strip()
                if candidate.get(field) is not None else ""
            ) == text
        )
        if matches == 1:
            return {"field": field, "value": value}
    return None


def _repair_page_binding(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Select an internal URL binding for later target-specific VL evidence."""
    for field in (
        "pageUrl", "page_url", "detailUrl", "detail_url",
        "productUrl", "product_url", "url", "href",
    ):
        value = str(row.get(field) or "").strip()
        if re.match(r"^https?://[^\s]+$", value, flags=re.IGNORECASE):
            return {"field": field, "url": value}
    return None


def _repair_manifest_from_artifact(
    *,
    skill: Skill,
    rows: List[Dict[str, Any]],
    artifact: Dict[str, Any],
    artifact_name: str,
) -> Optional[Dict[str, Any]]:
    """Describe a safe field-level repair, or None when partial trust is unsafe.

    Only row-local missing/empty/provenance failures qualify. Any placeholder,
    wrong-shape, value, uniqueness, or other semantic failure keeps the old full
    slow-path fallback: those failures can invalidate fields that look present.
    """
    saved_path = str(
        artifact.get("savedPath") or artifact.get("relativePath") or ""
    ).strip()
    if not saved_path or not rows:
        return None

    defects: Dict[int, set[str]] = {}

    def add_fields(raw_index: Any, raw_fields: Any) -> bool:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(rows):
            return False
        values = raw_fields if isinstance(raw_fields, list) else []
        fields = {str(item).strip() for item in values if str(item).strip()}
        if not fields:
            return False
        defects.setdefault(index, set()).update(fields)
        return True

    schema_warnings = artifact.get("schemaWarnings")
    for warning in schema_warnings if isinstance(schema_warnings, list) else []:
        if not isinstance(warning, dict):
            return None
        if str(warning.get("type") or "") != "expected_fields_missing":
            return None
        if not add_fields(warning.get("row"), warning.get("missing")):
            return None

    validation = (
        artifact.get("artifactValidation")
        if isinstance(artifact.get("artifactValidation"), dict)
        else {}
    )
    failures = validation.get("failures")
    for failure in failures if isinstance(failures, list) else []:
        if not isinstance(failure, dict):
            return None
        failure_type = str(failure.get("type") or "")
        if failure_type == "required_fields":
            if not add_fields(failure.get("row"), failure.get("missing")):
                return None
        elif failure_type == "field_nonempty":
            if not add_fields(failure.get("row"), failure.get("empty")):
                return None
        elif failure_type == "field_provenance":
            bad = failure.get("bad")
            if not isinstance(bad, list) or not bad:
                return None
            for item in bad:
                if not isinstance(item, dict) or not add_fields(
                    item.get("row"), item.get("missing"),
                ):
                    return None
        elif failure_type == "schema" and defects:
            # validate_worker_artifacts reports the same schemaWarnings again as
            # a schema failure. The concrete row/field defects above are the
            # repair authority; an unrelated schema failure is not repairable.
            warning_copy = failure.get("schemaWarnings")
            if not isinstance(warning_copy, list):
                return None
        else:
            return None

    if not defects:
        return None

    repairs: List[Dict[str, Any]] = []
    for index in sorted(defects):
        identity = _repair_row_identity(rows[index], rows)
        if identity is None:
            return None
        repair_fields = sorted(defects[index])
        repair = {
            "rowIndex": index,
            "identity": identity,
            "fields": repair_fields,
            "trustedFields": sorted(
                field for field in rows[index].keys()
                if field not in defects[index]
            ),
        }
        page_binding = _repair_page_binding(rows[index])
        if page_binding is not None:
            repair["pageBinding"] = page_binding
        repairs.append(repair)

    return {
        "version": "repair_manifest.v1",
        "skill": skill.skill_id,
        "artifactName": artifact_name,
        "baselineArtifact": saved_path,
        "workingArtifact": saved_path,
        "rowCount": len(rows),
        "repairs": repairs,
    }


def _repair_handoff_note(manifest: Dict[str, Any]) -> str:
    repairs = manifest.get("repairs") or []
    targets = [
        {
            "identity": item.get("identity"),
            "fields": item.get("fields"),
            "pageBinding": item.get("pageBinding"),
        }
        for item in repairs if isinstance(item, dict)
    ]
    return "\n".join([
        (
            "The skill fast path completed every row, but phase validation found"
            " localized field defects. Preserve the valid fast-path data."
        ),
        f"Trusted baseline artifact: {manifest.get('baselineArtifact')}",
        "Repair targets (process serially): "
        + json.dumps(targets, ensure_ascii=False, default=str),
        (
            "Do NOT re-scrape or rewrite other rows/fields. Visit only the target"
            " rows and obtain only the listed fields. Keep the current page/fleet"
            " open until record_extraction reports that the repair and its evidence"
            " are complete."
        ),
        (
            "Call record_extraction with the expected artifact name and PATCH rows:"
            " each row needs its listed identity field plus repaired fields and"
            " their evidence fields. The harness deterministically merges patches"
            " into the trusted baseline before running the full phase validators."
        ),
        (
            "For each EMPTY repaired value, also pass repair_resolutions with the"
            " same identity + field. Use outcome=observed_empty only when the source"
            " explicitly exposes a legal empty value; use outcome=confirmed_absent"
            " when expected browser content does not exist; unresolved is not a"
            " completed repair. Non-empty values default to value_found. For"
            " confirmed_absent, run visual_verify on the relevant live page BEFORE"
            " final_answer when visual checks are enabled, passing repair_targets"
            " with this manifest's exact identity and field names. Page.screenshot"
            " only saves pixels, and visual checks without matching repair_targets"
            " do not count as repair evidence."
        ),
    ])


async def _run_batch_fast_path(
    agent: Any,
    skill: Skill,
    rows: List[Dict[str, Any]],
    *,
    worker_contract: Dict[str, Any],
    fleet_ids: Optional[Sequence[str]],
    record_extraction: Any,
    health: Any,
) -> Optional[Dict[str, Any]]:
    """Iterate skill_rows on ONE page (same-tab renavigation keeps e.g. Cloudflare
    clearance warm): each row is a full single-run — workflow + success contract +
    visual contract. All rows pass → one artifact with all rows, LLM loop skipped.
    Any row stopping (HITL / failure / underivable variables) → persist the rows
    already completed and return {"handled": False, "handoff_note": ...} so the
    slow path continues from the failing row instead of redoing the batch.

    Health accounting is per BATCH RUN, not per row (one False on the first
    stopping row, one True when all rows pass) so a single bad page cannot burn
    a skill's disable budget in one worker. HITL and underivable-variable stops
    record nothing — neither is skill rot."""
    page_id, fleet_id = await _ensure_page(agent, fleet_ids)
    batch_id = uuid.uuid4().hex[:8]
    artifact_name = _artifact_name(skill, worker_contract)
    built_rows: List[Dict[str, Any]] = []

    def _handoff(reason: str, index: int, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        partial = _persist_partial_rows(
            agent, skill, built_rows, record_extraction, artifact_name=artifact_name)
        note = _batch_handoff_note(
            skill, rows, index, reason=reason, partial_artifact=partial,
            failed_row=rows[index] if index < len(rows) else None, details=details,
        )
        _log(agent, "skill.fast_path.batch_handoff", {
            "skill": skill.skill_id, "reason": reason, "rowIndex": index,
            "completedRows": len(built_rows), "totalRows": len(rows),
            "partialArtifact": partial.get("savedPath") or partial.get("relativePath"),
        })
        return {"handled": False, "handoff_note": note, "skill": skill.skill_id,
                "completedRows": len(built_rows), "totalRows": len(rows)}

    for index, row in enumerate(rows):
        variables = _derive_row_variables(skill, worker_contract, row)
        if not required_filled(skill, variables):
            # Malformed input row (e.g. missing detailUrl) — a data bug, not skill
            # rot: no health record, hand the remainder to the slow path.
            return _handoff("required_variables_unfilled", index, {"variables": variables})

        run_id = f"skill-{skill.skill_id}-{batch_id}-row{index}"
        run_result, observed_signal = await _run_with_transient_retry(
            agent, skill, run_id=run_id, page_id=page_id, fleet_id=fleet_id, variables=variables,
        )

        hitl = classify_run_for_hitl(run_result, observed_signal)
        if hitl is not None:
            # HITL is NOT a skill failure (same as the single-run path): no health
            # record; the paused page lives in the slot fleet for the slow path.
            outcome = _handoff("hitl_required", index, {
                "signal": hitl, "pageId": page_id,
                "failedStepPath": run_result.get("failedStepPath"),
            })
            outcome["hitl"] = hitl
            return outcome

        verdict = check_success_contract(skill, run_result)
        if not (run_result.get("succeeded") and verdict["ok"]):
            if health is not None:
                health.record(skill, False)
            return _handoff("row_failed", index, {
                "succeeded": run_result.get("succeeded"),
                "failed_checks": verdict["failed_checks"],
                "failedStepPath": run_result.get("failedStepPath"),
                "failedPurpose": run_result.get("failedPurpose"),
            })

        mismatch = page_binding_mismatch(skill, run_result, variables)
        if mismatch is not None:
            # wrong_page: a soft-failed navigate left the tab on another page —
            # the sections just read belong to a DIFFERENT row. page_binding_unknown:
            # Page.getState itself soft-failed, provenance unprovable. Either way,
            # never persist these sections for this row.
            if health is not None:
                health.record(skill, False)
            return _handoff(str(mismatch.get("reason") or "wrong_page"), index, mismatch)

        visual = await _evaluate_visual_contract(agent, skill, page_id)
        if not visual["ok"]:
            if health is not None:
                health.record(skill, False)
            return _handoff("visual_contract_violated", index, {
                "failed_checks": visual.get("failed_checks"), "verdict": visual.get("verdict"),
            })

        built_rows.append(_align_row_fields_to_expected(
            build_extraction_row(skill, run_result, input_variables={
                # Non-template row keys (inherited provenance evidence from
                # build_skill_rows_from_artifacts, or Lead-attached extras)
                # ride along into the persisted row — variable derivation is
                # template-scoped and would silently drop them. Derived
                # template variables win on overlap.
                **{k: v for k, v in row.items() if k not in variables},
                **variables,
            }),
            _expected_fields_of(worker_contract),
        ))
        _log(agent, "skill.fast_path.row_completed", {
            "skill": skill.skill_id, "batchId": batch_id,
            "rowIndex": index, "runId": run_id,
            "rowFields": sorted(built_rows[-1].keys()),
        })

    artifact: Dict[str, Any] = {}
    if record_extraction is not None:
        try:
            artifact = record_extraction(agent, {
                "name": artifact_name,
                "rows": built_rows,
                "description": (
                    f"Persisted by skill fast path (batch of {len(built_rows)}): {skill.skill_id}"
                ),
            }) or {}
        except Exception as exc:  # pragma: no cover - persistence best-effort
            _log(agent, "skill.fast_path.persist_error", {"skill": skill.skill_id, "error": str(exc)})
            return None

    expected_rows = _phase_expected_rows(worker_contract)
    for row_built in built_rows:
        persisted = check_persisted_contract(
            skill, row_built, artifact,
            row_count=len(built_rows), expected_rows=expected_rows,
        )
        if not persisted["ok"]:
            if health is not None:
                health.record(skill, False)
            _log(agent, "skill.fast_path.persisted_contract_unmet", {
                "skill": skill.skill_id, "batchId": batch_id,
                "failed_checks": persisted["failed_checks"],
                "artifactStatus": artifact.get("status"),
            })
            repair_manifest = _repair_manifest_from_artifact(
                skill=skill,
                rows=built_rows,
                artifact=artifact,
                artifact_name=artifact_name,
            )
            if repair_manifest is not None:
                _log(agent, "skill.fast_path.repair_handoff", {
                    "skill": skill.skill_id,
                    "batchId": batch_id,
                    "baselineArtifact": repair_manifest["baselineArtifact"],
                    "repairRows": len(repair_manifest["repairs"]),
                    "repairFields": sum(
                        len(item.get("fields") or [])
                        for item in repair_manifest["repairs"]
                    ),
                })
                return {
                    "handled": False,
                    "handoff_note": _repair_handoff_note(repair_manifest),
                    "repair_manifest": repair_manifest,
                    "skill": skill.skill_id,
                    "completedRows": len(built_rows),
                    "totalRows": len(built_rows),
                }
            return None  # non-local/unsafe validation failure → full slow path

    if health is not None:
        health.record(skill, True)
    _log(agent, "skill.fast_path.batch_completed", {
        "skill": skill.skill_id, "batchId": batch_id,
        "rows": len(built_rows), "row_fields": sorted(built_rows[0].keys()) if built_rows else [],
    })
    answer = json.dumps(
        {
            "outcome": "completed_via_skill",
            "skill": skill.skill_id,
            "batchId": batch_id,
            "rows": len(built_rows),
            "data": {"rowCount": len(built_rows)},
            "evidence": [
                f"skill {skill.skill_id} batch fast path: {len(built_rows)}/{len(rows)} rows "
                "completed; success_contract satisfied per row"
            ],
            "artifact": artifact.get("savedPath") or artifact.get("relativePath"),
            "next_steps": "none; skill fast path persisted all rows",
        },
        ensure_ascii=False,
    )
    return {
        "handled": True,
        "answer": answer,
        "skill": skill.skill_id,
        "batchId": batch_id,
        "completedRows": len(built_rows),
        "totalRows": len(rows),
    }


async def _run_structured_output_fast_path(
    agent: Any,
    skill: Skill,
    *,
    worker_contract: Dict[str, Any],
    phase: Optional[Dict[str, Any]],
    task: str,
    context: str,
    fleet_ids: Optional[Sequence[str]],
    record_extraction: Any,
    health: Any,
) -> Optional[Dict[str, Any]]:
    """Run one ordinary Workflow.execute that returns a JSON-string row set."""
    variables = derive_variables(skill, worker_contract, phase, task, context)
    variables.update(
        _structured_runtime_variables(skill, worker_contract, phase, task, context)
    )
    if not required_filled(skill, variables):
        _log(agent, "skill.fast_path.skipped", {
            "skill": skill.skill_id,
            "reason": "required_variables_unfilled",
            "variables": {
                key: value for key, value in variables.items()
                if "expression" not in key.lower()
            },
        })
        return None

    config = skill.structured_output
    rank = config.get("rank")
    rank = rank if isinstance(rank, dict) else {}
    rank_field = str(rank.get("field") or "rank")
    rank_window = _phase_rank_window(worker_contract, rank_field)
    if isinstance(config.get("window"), dict) and rank_window is None:
        _log(agent, "skill.fast_path.structured_output_unavailable", {
            "skill": skill.skill_id,
            "reason": "rank_window_missing",
            "field": rank_field,
        })
        return None

    page_id, fleet_id = await _ensure_page(agent, fleet_ids)
    run_id = f"skill-{skill.skill_id}-{uuid.uuid4().hex[:8]}"
    run_result, observed_signal = await _run_with_transient_retry(
        agent,
        skill,
        run_id=run_id,
        page_id=page_id,
        fleet_id=fleet_id,
        variables=variables,
    )
    hitl = classify_run_for_hitl(run_result, observed_signal)
    if hitl is not None:
        _log(agent, "skill.fast_path.hitl_required", {
            "skill": skill.skill_id,
            "runId": run_id,
            "pageId": page_id,
            "signal": hitl,
        })
        return None

    result_variables = run_result.get("variables")
    result_variables = result_variables if isinstance(result_variables, dict) else {}
    challenge = workflow_challenge_signal(skill, run_result, variables)
    if challenge is not None:
        # A challenge page can still execute the frozen JS and return an empty
        # JSON array. That is an environment/HITL outcome, not evidence that the
        # skill's selector or contract is rotten, so never debit suite health.
        _log(agent, "skill.fast_path.hitl_required", {
            "skill": skill.skill_id,
            "runId": run_id,
            "pageId": page_id,
            "signal": challenge,
        })
        return None
    page_status = str(result_variables.get("pageStatus") or "").lower()
    if page_status in {"loading", "navigating", "startedloading"}:
        _log(agent, "skill.fast_path.page_unsettled", {
            "skill": skill.skill_id,
            "runId": run_id,
            "pageId": page_id,
            "pageStatus": page_status,
        })
        return None

    verdict = check_success_contract(skill, run_result)
    if not (run_result.get("succeeded") and verdict["ok"]):
        if health is not None:
            health.record(skill, False)
        _log(agent, "skill.fast_path.fell_back", {
            "skill": skill.skill_id,
            "runId": run_id,
            "failed_checks": verdict["failed_checks"],
            "failedStepPath": run_result.get("failedStepPath"),
            "failedPurpose": run_result.get("failedPurpose"),
        })
        return None

    mismatch = page_binding_mismatch(skill, run_result, variables)
    if mismatch is not None:
        if health is not None:
            health.record(skill, False)
        _log(agent, f"skill.fast_path.{mismatch.get('reason') or 'wrong_page'}", {
            "skill": skill.skill_id,
            "runId": run_id,
            **mismatch,
        })
        return None

    rows, failures = structured_output_rows(
        skill,
        run_result,
        rank_window=rank_window,
    )
    selector = str(config.get("source_selector") or "Workflow structured output")
    page_url = str(
        (run_result.get("variables") or {}).get("pageUrl")
        or variables.get("targetUrl")
        or ""
    )
    expected_fields = _expected_fields_of(worker_contract)
    built_rows: List[Dict[str, Any]] = []
    if not failures:
        for row in rows:
            enriched = dict(row)
            rank_value = _row_field_value(enriched, rank_field)
            enriched.setdefault("pageUrl", page_url)
            enriched.setdefault("sourceTool", f"Workflow.execute(skill:{skill.skill_id})")
            enriched.setdefault("sourceSelectorOrAxId", selector)
            if rank_value not in (None, ""):
                enriched.setdefault(
                    "rankEvidenceText",
                    f"Rank {rank_value} is derived from the unique filtered DOM order "
                    f"of {selector} on {page_url}.",
                )
            built_rows.append(
                _align_row_fields_to_expected(enriched, expected_fields)
            )

    validators = worker_contract.get("validators")
    if not failures and any(
        not _row_passes_validators(row, validators) for row in built_rows
    ):
        failures.append("structured_output row failed phase row validators")
    expected_rows = _phase_expected_rows(worker_contract)
    if expected_rows is not None and len(built_rows) != expected_rows:
        failures.append(
            f"phase_rows:{expected_rows}(got {len(built_rows)})"
        )
    if failures:
        if health is not None:
            health.record(skill, False)
        _log(agent, "skill.fast_path.structured_output_unmet", {
            "skill": skill.skill_id,
            "runId": run_id,
            "failed_checks": failures,
            "rowCount": len(built_rows),
        })
        return None

    artifact: Dict[str, Any] = {}
    if record_extraction is not None:
        try:
            artifact = record_extraction(agent, {
                "name": _artifact_name(skill, worker_contract),
                "rows": built_rows,
                "description": (
                    f"Persisted by structured-output skill fast path: {skill.skill_id}"
                ),
            }) or {}
        except Exception as exc:  # pragma: no cover
            _log(agent, "skill.fast_path.persist_error", {
                "skill": skill.skill_id, "error": str(exc),
            })
            return None

    for row in built_rows:
        persisted = check_persisted_contract(
            skill,
            row,
            artifact,
            row_count=len(built_rows),
            expected_rows=expected_rows,
        )
        if not persisted["ok"]:
            if health is not None:
                health.record(skill, False)
            _log(agent, "skill.fast_path.persisted_contract_unmet", {
                "skill": skill.skill_id,
                "runId": run_id,
                "failed_checks": persisted["failed_checks"],
                "artifactStatus": artifact.get("status"),
            })
            return None

    if health is not None:
        health.record(skill, True)
    _log(agent, "skill.fast_path.structured_output_completed", {
        "skill": skill.skill_id,
        "runId": run_id,
        "rows": len(built_rows),
        "row_fields": sorted(built_rows[0]) if built_rows else [],
    })
    answer = json.dumps({
        "outcome": "completed_via_skill",
        "skill": skill.skill_id,
        "runId": run_id,
        "rows": len(built_rows),
        "data": {"rowCount": len(built_rows)},
        "evidence": [
            f"skill {skill.skill_id} returned and persisted {len(built_rows)} structured rows"
        ],
        "artifact": artifact.get("savedPath") or artifact.get("relativePath"),
        "next_steps": "none; structured-output fast path satisfied the phase contract",
    }, ensure_ascii=False)
    return {
        "handled": True,
        "answer": answer,
        "skill": skill.skill_id,
        "runId": run_id,
        "completedRows": len(built_rows),
        "totalRows": len(built_rows),
    }


def resolve_skill_and_variables(
    registry: SkillRegistry,
    worker_contract: Optional[Dict[str, Any]],
    *,
    phase: Optional[Dict[str, Any]] = None,
    task: str = "",
    context: str = "",
    mode: str = "manual",
) -> tuple[Optional[Skill], Dict[str, Any]]:
    """Deterministically resolve which skill a task maps to + its derived
    variables, using the exact same logic as the fast path. Used by the
    self-heal hook to know which skill to heal and with what canary variables
    (no state threaded from the fast-path attempt)."""
    worker_contract = worker_contract or {}
    target_url = _find_url("", [worker_contract, phase, task, context]) or ""
    skill = select_skill(registry, worker_contract, target_url=target_url, mode=mode)
    if skill is None:
        return None, {}
    variables = derive_variables(skill, worker_contract, phase, task, context)
    return skill, variables


def _selection_mode(agent: Any) -> str:
    harness_cfg = getattr(getattr(agent, "runtime", None), "harness", None)
    mode = str(getattr(harness_cfg, "skill_selection_mode", "manual") or "manual")
    return mode if mode in ("manual", "auto") else "manual"


def _active_control_enabled(agent: Any) -> bool:
    harness_cfg = getattr(getattr(agent, "runtime", None), "harness", None)
    return bool(getattr(harness_cfg, "skill_workflow_active_control_enabled", False))


def _should_vl_solve(vl_config: Any, skill: Any = None) -> bool:
    """Role C (auto-solve a CAPTCHA) needs its OWN opt-in beyond vl.enabled — acting
    on a challenge is the most consequential VL action (ToS/legal weight).

    Two AND-gates, both default-deny: (1) global vl.enabled && captcha_solve_enabled;
    (2) the matched skill's frontmatter `allow_auto_captcha: true` (doc §13.8 — a
    skill may forbid auto-solving its challenges; e.g. TAAFT declares false). A skill
    that does not opt in falls to the human path even when the global gate is open."""
    if not (
        vl_config is not None
        and getattr(vl_config, "enabled", False)
        and getattr(vl_config, "captcha_solve_enabled", False)
    ):
        return False
    if skill is not None:
        frontmatter = getattr(skill, "frontmatter", None) or {}
        if not bool(frontmatter.get("allow_auto_captcha", False)):
            return False
    return True


async def _run_skill_with_optional_control(
    agent: Any,
    skill: Skill,
    *,
    run_id: str,
    page_id: str,
    fleet_id: str,
    variables: Dict[str, Any],
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Run the skill workflow. If active control is enabled and a 2nd connection
    opens, drive pause/resolve/resume over it (observed_signal is None — pauses are
    handled actively). Otherwise (or on ANY control failure) fall back to the
    observe-only path. Returns (run_result, observed_signal)."""
    if _active_control_enabled(agent):
        try:
            import functools

            from harness.skill.control import (
                ControlChannel,
                _default_screenshot,
                make_challenge_poller,
                resolve_via_hitl,
                resolve_via_vl_then_hitl,
                run_workflow_with_control,
            )

            logger = getattr(agent, "logger", None)
            control = ControlChannel.from_browser(agent.browser, logger=logger)
            if control is not None and await control.try_open():
                harness_cfg = getattr(getattr(agent, "runtime", None), "harness", None)
                common = dict(
                    timeout_seconds=float(getattr(harness_cfg, "hitl_wait_timeout_seconds", 1200.0) or 1200.0),
                    poll_interval_seconds=float(getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0) or 2.0),
                    logger=logger,
                )
                vl_config = getattr(harness_cfg, "vl", None)
                if _should_vl_solve(vl_config, skill):
                    # VL-first: try to auto-solve a visual CAPTCHA, human fall-back.
                    resolver = functools.partial(
                        resolve_via_vl_then_hitl,
                        vl_config=vl_config, screenshot_fn=_default_screenshot,
                        max_retries=int(getattr(vl_config, "captcha_solve_max_retries", 2) or 2),
                        **common,
                    )
                else:
                    resolver = functools.partial(resolve_via_hitl, **common)
                # Per-skill IN-PAGE poll: the primary is blocked in execute, so an
                # in-page widget firing no navigation is invisible to the notification
                # leg. The poll mirrors the skill's own challengeFlag JS on the 2nd
                # connection (None when the skill declares no challenge boundary).
                poll_challenge_fn = make_challenge_poller(skill)
                try:
                    run_result = await run_workflow_with_control(
                        primary=agent.browser, control=control, skill=skill,
                        run_id=run_id, page_id=page_id, fleet_id=fleet_id,
                        variables=variables, on_pause=resolver,
                        poll_challenge_fn=poll_challenge_fn, logger=logger,
                    )
                    return run_result, None
                finally:
                    await control.close()
        except Exception as exc:  # any control failure → degrade to observe-only
            _log(agent, "skill.control.degraded", {"skill": skill.skill_id, "error": str(exc)})

    monitor = HitlOnsetMonitor(agent.browser, page_id).start()
    try:
        run_result = await run_skill_workflow(
            agent.browser, skill, run_id=run_id,
            page_id=page_id, fleet_id=fleet_id, variables=variables,
        )
    finally:
        observed_signal = monitor.stop()
    return run_result, observed_signal


async def _ensure_page(agent: Any, fleet_ids: Optional[Sequence[str]]) -> tuple[str, str]:
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if not fleet_id:
        fleet_id = next(iter(fleet_ids), "") if fleet_ids else ""
    if not fleet_id:
        harness_config = getattr(getattr(agent, "runtime", None), "harness", None)
        if bool(getattr(harness_config, "fleet_reuse_enabled", False)):
            raise RuntimeError(
                "fleet_assignment_required: fast path refuses fleetless Page.create"
            )
        # Legacy-only fallback for deployments that explicitly disable the new
        # coordinator.  Reuse-enabled workers are assigned before dispatch.
        fl = await agent.browser.call("Fleet.create", {})
        fleet_id = ((fl or {}).get("data") or {}).get("fleetId") or ""
    pg = await agent.browser.call("Page.create", {"fleetId": fleet_id, "url": "about:blank"})
    page_id = ((pg or {}).get("data") or {}).get("pageId") or ""
    if page_id:
        allowed_pages = getattr(agent, "allowed_page_ids", None)
        if not isinstance(allowed_pages, set):
            allowed_pages = set()
            agent.allowed_page_ids = allowed_pages
        allowed_pages.add(str(page_id))
        page_fleets = getattr(agent, "page_fleet_ids", None)
        if not isinstance(page_fleets, dict):
            page_fleets = {}
            agent.page_fleet_ids = page_fleets
        page_fleets[str(page_id)] = str(fleet_id)
    return page_id, fleet_id


async def maybe_run_skill_fast_path(
    agent: Any,
    *,
    registry: Optional[SkillRegistry],
    worker_contract: Optional[Dict[str, Any]],
    phase: Optional[Dict[str, Any]] = None,
    task: str = "",
    context: str = "",
    fleet_ids: Optional[Sequence[str]] = None,
    record_extraction: Any = None,
    health: Any = None,
) -> Optional[Dict[str, Any]]:
    """Return {"handled": True, "answer": <json str>, ...} if the skill fast path
    completed the task; otherwise None (caller runs the normal LLM loop)."""
    if registry is None:
        return None
    worker_contract = worker_contract or {}

    target_url = _find_url("", [worker_contract, phase, task, context]) or ""
    skill = select_skill(
        registry, worker_contract, target_url=target_url, mode=_selection_mode(agent),
    )
    if skill is None:
        return None

    if getattr(skill, "is_hints_only", False):
        _log(agent, "skill.fast_path.hints_only", {
            "skill": skill.skill_id,
            "reason": "guidance skill (no workflow); slow path runs with injected hints",
        })
        return None

    # 07-07 user decision: an EXPLICIT (human/Lead-forced) skill takes health
    # out of the loop entirely — it neither vetoes the run (a human's choice
    # outranks bookkeeping) nor records outcomes (a forced skill legitimately
    # runs on phases it only partially fits, and the LLM slow path judges for
    # itself; task fa86c5f6 got a good skill auto-disabled by 3 such bogus
    # failures). health keeps its P5 semantics for auto-mode selection only.
    if _is_explicit_selection(worker_contract):
        health = None

    # P5: a rotted skill (too many consecutive failures) is disabled → slow path.
    if health is not None and health.is_disabled(skill):
        _log(agent, "skill.fast_path.disabled",
             {"skill": skill.skill_id, "health": health.entry(skill.skill_id)})
        return None

    # A standard Workflow.execute may return a JSON string containing multiple
    # rows. It runs once and owns its own collection/materialization steps, so
    # the ordinary detail-batch `skill_rows` preflight does not apply.
    if skill.structured_output:
        return await _run_structured_output_fast_path(
            agent,
            skill,
            worker_contract=worker_contract,
            phase=phase,
            task=task,
            context=context,
            fleet_ids=fleet_ids,
            record_extraction=record_extraction,
            health=health,
        )

    # Batch mode: a Lead-attached skill_rows list runs the workflow once per row
    # on one page (no LLM loop) instead of forcing a single-detail skill over a
    # batch phase or falling back entirely.
    rows = skill_rows(worker_contract)
    expected_rows = _phase_expected_rows(worker_contract)
    if rows and expected_rows and expected_rows > 1:
        enriched_rows, enrichment = enrich_explicit_skill_rows_from_artifacts(
            agent, skill, worker_contract, rows,
        )
        if enrichment.get("reason") == "validated_rows_equivalent":
            rows = enriched_rows
            worker_contract["skill_rows"] = rows

    # Pre-flight for multi-row phases without skill_rows: a single workflow run
    # can never satisfy exact_rows>1, so running it would only burn a real
    # browser pass before the inevitable fallback (task 9d5655d3 p2). First try
    # to auto-build the batch input from upstream validated artifacts; if that
    # is not possible, skip the fast path outright.
    if not rows and expected_rows and expected_rows > 1:
        rows = build_skill_rows_from_artifacts(agent, skill, worker_contract)
        if rows:
            worker_contract["skill_rows"] = rows
            _log(agent, "skill.fast_path.auto_rows", {
                "skill": skill.skill_id,
                "rowCount": len(rows),
                "expectedRows": expected_rows,
            })
        else:
            _log(agent, "skill.fast_path.skipped", {
                "skill": skill.skill_id,
                "reason": "batch_requires_skill_rows",
                "expectedRows": expected_rows,
            })
            return None

    # Validator preflight: evidence fields the phase demands that neither the
    # skill's outputs nor the input rows can supply will fail phase validation
    # no matter how cleanly the workflow runs — skip before touching a page.
    # (Auto-built rows above already carry inherited/synthesized evidence when
    # the upstream artifact allows it, so this fires only on the truly
    # unsatisfiable remainder.)
    unmet_evidence = _unsatisfiable_evidence_fields(skill, worker_contract, rows)
    if unmet_evidence:
        _log(agent, "skill.fast_path.skipped", {
            "skill": skill.skill_id,
            "reason": "validators_unsatisfiable",
            "missing_evidence_fields": unmet_evidence,
            "expectedRows": expected_rows,
        })
        return None

    if rows:
        return await _run_batch_fast_path(
            agent, skill, rows,
            worker_contract=worker_contract, fleet_ids=fleet_ids,
            record_extraction=record_extraction, health=health,
        )

    variables = derive_variables(skill, worker_contract, phase, task, context)
    if not required_filled(skill, variables):
        _log(agent, "skill.fast_path.skipped",
             {"skill": skill.skill_id, "reason": "required_variables_unfilled", "variables": variables})
        return None

    page_id, fleet_id = await _ensure_page(agent, fleet_ids)
    run_id = f"skill-{skill.skill_id}-{uuid.uuid4().hex[:8]}"
    # P4: when active control is enabled, drive pause→resolve→resume over a 2nd
    # connection; otherwise observe-only. Either way an unresolved pause classifies
    # as hitl_required below and hands off.
    run_result, observed_signal = await _run_with_transient_retry(
        agent, skill, run_id=run_id, page_id=page_id, fleet_id=fleet_id, variables=variables,
    )

    # P4 hand-off: a HITL/challenge interruption is NOT a skill failure. Hand the
    # live (paused) page — it lives in the worker's slot fleet — to the BrowserAgent
    # slow path, which owns the proven VL+human HITL machinery. Do not record a
    # health failure and do not feed self-heal.
    hitl = classify_run_for_hitl(run_result, observed_signal)
    if hitl is not None:
        _log(agent, "skill.fast_path.hitl_required", {
            "skill": skill.skill_id, "runId": run_id, "pageId": page_id, "signal": hitl,
            "failedStepPath": run_result.get("failedStepPath"),
        })
        return None  # slow path takes over the paused page

    verdict = check_success_contract(skill, run_result)
    if not (run_result.get("succeeded") and verdict["ok"]):
        if health is not None:
            health.record(skill, False)
        _log(agent, "skill.fast_path.fell_back", {
            "skill": skill.skill_id, "runId": run_id,
            "succeeded": run_result.get("succeeded"),
            "failed_checks": verdict["failed_checks"],
            "failedStepPath": run_result.get("failedStepPath"),
            "failedPurpose": run_result.get("failedPurpose"),
        })
        return None  # slow path takes over

    mismatch = page_binding_mismatch(skill, run_result, variables)
    if mismatch is not None:
        # wrong_page: the tab is elsewhere; page_binding_unknown: Page.getState
        # soft-failed so provenance is unprovable. Never persist either way.
        # Event name carries the reason so event-name statistics don't lump the
        # two failure classes together.
        if health is not None:
            health.record(skill, False)
        _log(agent, f"skill.fast_path.{mismatch.get('reason') or 'wrong_page'}",
             {"skill": skill.skill_id, "runId": run_id, **mismatch})
        return None  # slow path takes over

    # Role B: the variable contract passed, but visual_checks (if declared) must
    # also hold. VL judges the screenshot; only a definitive `violated` vetoes (VL
    # is L4 — uncertainty / infra failure never sinks a structurally-successful run).
    visual = await _evaluate_visual_contract(agent, skill, page_id)
    if not visual["ok"]:
        if health is not None:
            health.record(skill, False)
        _log(agent, "skill.fast_path.visual_contract_violated", {
            "skill": skill.skill_id, "runId": run_id,
            "failed_checks": visual.get("failed_checks"), "verdict": visual.get("verdict"),
        })
        return None  # visible end state contradicts success → slow path

    explicit_variables = worker_contract.get("skill_variables")
    explicit_variables = (
        explicit_variables if isinstance(explicit_variables, dict) else {}
    )
    row = _align_row_fields_to_expected(
        build_extraction_row(skill, run_result, input_variables={
            # Same passthrough as the batch path: non-template keys in the
            # Lead-filled skill_variables (e.g. rankEvidenceText) must reach
            # the persisted row — derivation is template-scoped and would
            # silently drop them. Derived template variables win on overlap.
            **{k: v for k, v in explicit_variables.items() if k not in variables},
            **variables,
        }),
        _expected_fields_of(worker_contract),
    )
    artifact: Dict[str, Any] = {}
    if record_extraction is not None:
        try:
            artifact = record_extraction(agent, {
                "name": _artifact_name(skill, worker_contract),
                "rows": [row],
                "description": f"Persisted by skill fast path: {skill.skill_id}",
            }) or {}
        except Exception as exc:  # pragma: no cover - persistence best-effort
            _log(agent, "skill.fast_path.persist_error", {"skill": skill.skill_id, "error": str(exc)})
            return None

    # Persistence-side contract: the workflow variable contract passing is NOT
    # enough — the persisted row must satisfy fields_required/fields_nonempty and
    # record_extraction's artifact validation must not be needs_fix. A skill that
    # "ran clean" but produced an incomplete/invalid row hands off to the slow path.
    persisted = check_persisted_contract(
        skill, row, artifact, expected_rows=_phase_expected_rows(worker_contract),
    )
    if not persisted["ok"]:
        if health is not None:
            health.record(skill, False)
        _log(agent, "skill.fast_path.persisted_contract_unmet", {
            "skill": skill.skill_id, "runId": run_id,
            "failed_checks": persisted["failed_checks"],
            "artifactStatus": artifact.get("status"),
        })
        repair_manifest = _repair_manifest_from_artifact(
            skill=skill,
            rows=[row],
            artifact=artifact,
            artifact_name=_artifact_name(skill, worker_contract),
        )
        if repair_manifest is not None:
            _log(agent, "skill.fast_path.repair_handoff", {
                "skill": skill.skill_id,
                "runId": run_id,
                "baselineArtifact": repair_manifest["baselineArtifact"],
                "repairRows": 1,
                "repairFields": sum(
                    len(item.get("fields") or [])
                    for item in repair_manifest["repairs"]
                ),
            })
            return {
                "handled": False,
                "handoff_note": _repair_handoff_note(repair_manifest),
                "repair_manifest": repair_manifest,
                "skill": skill.skill_id,
                "completedRows": 1,
                "totalRows": 1,
            }
        return None  # non-local/unsafe validation failure → full slow path

    if health is not None:
        health.record(skill, True)
    _log(agent, "skill.fast_path.completed",
         {"skill": skill.skill_id, "runId": run_id, "row_fields": sorted(row.keys())})
    return {
        "handled": True,
        "answer": _completed_answer(skill, run_result, artifact),
        "skill": skill.skill_id,
        "runId": run_id,
        "completedRows": 1,
        "totalRows": 1,
    }


async def _evaluate_visual_contract(agent: Any, skill: Skill, page_id: str) -> Dict[str, Any]:
    """Role B visual success-check (fail-safe: any error ⇒ ok:True, never veto)."""
    try:
        from harness.skill.visual_contract import evaluate_visual_contract
        vl_config = getattr(getattr(getattr(agent, "runtime", None), "harness", None), "vl", None)
        return await evaluate_visual_contract(
            agent.browser, skill, page_id,
            vl_config=vl_config, logger=getattr(agent, "logger", None),
        )
    except Exception as exc:  # contract verify must never break a passing run
        _log(agent, "skill.visual_contract.error", {"skill": getattr(skill, "skill_id", "?"), "error": str(exc)})
        return {"applicable": False, "ok": True, "error": str(exc)}


def _log(agent: Any, event: str, payload: Dict[str, Any]) -> None:
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:  # pragma: no cover
            pass
    # Mirror into the worker's trace list: a fast-path-only worker never runs
    # the LLM loop, so without this its traces/<worker>.jsonl is a 0-byte file
    # (task 2ed5a466 browser-003) and operators read "did nothing" where a
    # whole batch actually ran. The skill event stream IS its execution trace.
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        try:
            trace.append({
                "type": "skill_fast_path",
                "event": event,
                "payload": payload,
            })
        except Exception:  # pragma: no cover
            pass
