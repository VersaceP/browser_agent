"""harness.skill.create — /skill-create: distill a past task run into a skill.

CLI entry (main.py `/skill-create <task_dir|trace.jsonl> [skill-id] [--optimize|--new]`).
Goal: reduce duplicate skills AT THE SOURCE. Flow:

  1. pick the cleanest VALIDATED worker trace and distill it (P3 distiller);
  2. dedup BEFORE creating: same-domain skills are candidates; an injected LLM
     judge compares the new task's objective against the top-N candidates
     (_DEDUP_JUDGE_LIMIT, ordered non-draft → stage match → field overlap) ONE
     BY ONE (same / different / uncertain). The first same/uncertain → the USER
     decides (optimize that skill vs create new vs quit); creation requires
     every examined candidate to be confidently "different" (or no candidates)
     — judging only top-1 would let it shadow the real duplicate at rank 2;
  3. quality gates on the distilled workflow:
       - dry-run persisted-contract simulation (no browser): distilled variables
         → build_extraction_row → check_persisted_contract. Fails → the draft is
         written but status=draft_blocked with the reasons (this catches
         rwContText≠reviews / missing passthrough vars before anything runs);
       - live trial + GENERALITY: run the workflow on up to 3 DIFFERENT
         instances harvested from the task's own extraction artifacts (not just
         the page the trace came from), same warm tab, success_contract checked
         per run. Results land in SKILL.md (frontmatter `tested`, body detail).
     The optimize path runs the same dry-run simulation against the EXISTING
     skill's success_contract — a failing candidate is still written (inert)
     but reported as revision_blocked, and the live trial is skipped.
  4. conservative auto-hardening (harden_draft_workflow, --no-harden to skip):
     navigate onError:continue + a Page.getState page-binding step — read-only
     defenses only; challenge gating / Escape stay on the human checklist.

Skills are engaged at runtime ONLY by explicit user choice
(skill_selection_mode=manual), so a draft can never steal execution — the gates
here are about not wasting the user's trust when they DO pick it.

Everything returns a report dict; printing/interaction is the CLI's job.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import yaml

from harness.skill.autoheal import _load_distiller
from harness.skill.dispatch import build_extraction_row
from harness.skill.heal import _is_structurally_valid, write_candidate
from harness.skill.registry import (
    CREATE_REPORT_FILENAME,
    Skill,
    SkillRegistry,
    _domain_matches,
    canonical_field,
    canonical_fields,
    load_create_report,
    validate_row_contract,
)
from harness.skill.structured_output import validate_structured_output_workflow
from harness.skill.workflow import (
    check_persisted_contract,
    check_success_contract,
    run_skill_workflow,
)

SKILLS_DIR_DEFAULT = Path(__file__).resolve().parent.parent.parent / "skills"


# ---------------------------------------------------------------------------
# task-dir loading
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _resolve_task_layout(task_path: Path) -> Dict[str, Any]:
    """Accept a task worktree dir OR a single trace .jsonl file."""
    if task_path.is_file() and task_path.suffix == ".jsonl":
        base = task_path.parent.parent if task_path.parent.name == "traces" else task_path.parent
        return {"base": base, "traces": [task_path]}
    traces_dir = task_path / "traces"
    traces = sorted(traces_dir.glob("*.jsonl")) if traces_dir.is_dir() else []
    return {"base": task_path, "traces": traces}


def _load_task_plan(base: Path) -> Dict[str, Any]:
    plan_path = base / "task_plan.json"
    if not plan_path.is_file():
        return {}
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        return plan if isinstance(plan, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _validated_workers(base: Path) -> Dict[str, Dict[str, str]]:
    """workerId -> {phaseId, validatedStatus} from run.jsonl spawner results."""
    out: Dict[str, Dict[str, str]] = {}
    for entry in _read_jsonl(base / "run.jsonl"):
        if entry.get("type") != "spawner.browser.result":
            continue
        payload = entry.get("payload") or {}
        worker_id = str(payload.get("workerId") or "")
        if worker_id:
            out[worker_id] = {
                "phaseId": str(payload.get("phaseId") or ""),
                "validatedStatus": str(payload.get("validatedStatus") or ""),
            }
    return out


def _first_navigate_host(events: List[Dict[str, Any]]) -> str:
    for ev in events:
        if ev.get("type") != "browser_call":
            continue
        if ev.get("method") not in ("Page.navigate", "Page.create"):
            continue
        url = str((ev.get("params") or {}).get("url") or "")
        m = re.match(r"https?://([^/?#]+)", url)
        if m:
            host = m.group(1).lower()
            return host[4:] if host.startswith("www.") else host
    return ""


def _successful_model_tool_runs(
    events: List[Dict[str, Any]], tool_name: str,
) -> List[tuple[Dict[str, Any], Dict[str, Any]]]:
    """Pair a model tool input with its subsequent successful trace result.

    A model merely proposing a composite is not execution evidence.  Keep only
    calls followed by a same-type event whose result says ``status=done``;
    later distillation can additionally check tool-specific row/selector facts.
    """
    out: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    pending: Optional[Dict[str, Any]] = None
    for event in events:
        if event.get("type") == "model":
            for call in event.get("tool_calls") or []:
                if not isinstance(call, dict) or str(call.get("name") or "") != tool_name:
                    continue
                value = call.get("input")
                pending = value if isinstance(value, dict) else None
            continue
        if event.get("type") != tool_name or pending is None:
            continue
        result = event.get("result")
        result = result if isinstance(result, dict) else {}
        if str(result.get("status") or "") == "done":
            out.append((pending, result))
        pending = None
    return out


def _phase_url_pattern(phase: Dict[str, Any], field: str) -> str:
    wanted = canonical_field(field)
    for validator in phase.get("validators") or []:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") not in {"url_pattern", "field_pattern"}:
            continue
        if canonical_field(str(validator.get("field") or "")) != wanted:
            continue
        return str(validator.get("pattern") or "").strip()
    return ""


def _build_collection_structured_workflow(
    events: List[Dict[str, Any]],
    phase: Dict[str, Any],
    *,
    domain: str,
    description: str,
) -> Optional[Dict[str, Any]]:
    """Distill validated collect_items evidence into an ordinary Workflow.

    The workflow accumulates filtered candidates in a page global while it
    scrolls, then JSON.stringify's them into one scalar workflow variable. The
    harness later assigns DOM-order ranks and slices the current phase window.
    No historical product URL or rank is frozen into the recipe.
    """
    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    exact_rows = _expected_rows_of_phase(phase)
    fields = [str(field) for field in expected.get("fields") or [] if str(field)]
    rank_field = next(
        (field for field in fields if canonical_field(field) == canonical_field("rank")),
        "",
    )
    name_output_field = next(
        (field for field in fields
         if canonical_field(field) == canonical_field("productName")),
        "",
    )
    url_output_field = next(
        (field for field in fields
         if canonical_field(field) == canonical_field("productUrl")),
        "",
    )
    if not exact_rows or not rank_field or not name_output_field or not url_output_field:
        return None
    rank_window = _rank_window_from_phase(phase, rank_field)
    if rank_window is None or rank_window[1] - rank_window[0] + 1 != exact_rows:
        return None

    collect_runs = _successful_model_tool_runs(events, "collect_items")
    record_runs = _successful_model_tool_runs(events, "record_extraction")
    if not collect_runs or not record_runs:
        return None
    artifact_name = str(expected.get("name") or "")
    final_pair = next((
        (item, result) for item, result in reversed(record_runs)
        if isinstance(item.get("rows"), list)
        and len(item["rows"]) == exact_rows
        and int(result.get("rowCount") or 0) == exact_rows
        and (not artifact_name or str(item.get("name") or "") == artifact_name)
        and (not artifact_name or str(result.get("name") or "") == artifact_name)
        and isinstance(result.get("artifactValidation"), dict)
        and str(result["artifactValidation"].get("status") or "") == "done"
    ), None)
    if final_pair is None:
        return None
    final, _final_result = final_pair
    final_rows = [row for row in final.get("rows") or [] if isinstance(row, dict)]
    if len(final_rows) != exact_rows:
        return None

    try:
        final_ranks = [int(row.get(rank_field)) for row in final_rows]
    except (TypeError, ValueError):
        return None
    if final_ranks != list(range(rank_window[0], rank_window[1] + 1)):
        return None

    collect_pair = next((
        (item, result) for item, result in reversed(collect_runs)
        if int(result.get("rowCount") or 0) >= rank_window[1]
        and (
            not str(result.get("selector") or "")
            or str(result.get("selector") or "") == str(item.get("selector") or "")
        )
        and (
            not str(result.get("mode") or "")
            or str(result.get("mode") or "") == str(item.get("mode") or result.get("mode") or "")
        )
    ), None)
    if collect_pair is None:
        return None
    collect, _collect_result = collect_pair
    selector = str(collect.get("selector") or "").strip()
    raw_fields = collect.get("fields")
    raw_fields = raw_fields if isinstance(raw_fields, dict) else {}
    name_field = next((
        str(name) for name, spec in raw_fields.items()
        if str(spec) in {"text", "textContent", "imgAlt"}
        and canonical_field(str(name)) == canonical_field("productName")
    ), "")
    url_field = next((
        str(name) for name, spec in raw_fields.items()
        if str(spec) == "href"
        and canonical_field(str(name)) in {
            canonical_field("productUrl"), canonical_field("detailUrl")
        }
    ), "")
    if not selector or not name_field or not url_field:
        return None
    if any(not str(row.get(name_field) or row.get("productName") or "").strip()
           for row in final_rows):
        return None
    final_urls = [
        str(row.get(url_field) or row.get("productUrl") or row.get("detailUrl") or "").strip()
        for row in final_rows
    ]
    if any(not value.startswith("http") for value in final_urls):
        return None
    query_must_be_empty = all("?" not in value and "#" not in value for value in final_urls)
    exact_ai_path = all(
        re.match(r"^https?://[^/?#]+/ai/[^/?#]+/?$", value) is not None
        for value in final_urls
    )
    url_pattern = _phase_url_pattern(phase, url_field)
    target_url = _first_phase_url(phase)
    if not target_url:
        return None

    selector_js = json.dumps(selector)
    domain_js = json.dumps(domain)
    pattern_js = json.dumps(url_pattern)
    name_output_js = json.dumps(name_output_field)
    url_output_js = json.dumps(url_output_field)
    query_guard = "if (u.search || u.hash) continue;" if query_must_be_empty else ""
    path_guard = (
        "if (!/^\\/ai\\/[^/]+\\/?$/.test(u.pathname)) continue;"
        if exact_ai_path else ""
    )
    init_expression = (
        "window.__abcpSkillCollection={seen:{},rows:[]};"
        "return {ready:'yes'};"
    )
    probe_expression = rf"""
const selector={selector_js};
const expectedHost={domain_js};
const pattern={pattern_js};
const state=window.__abcpSkillCollection||{{seen:{{}},rows:[]}};
for (const anchor of Array.from(document.querySelectorAll(selector))) {{
  let u; try {{ u=new URL(anchor.href||anchor.getAttribute('href')||'', location.href); }} catch (_) {{ continue; }}
  if (expectedHost && u.hostname.replace(/^www\./,'') !== expectedHost.replace(/^www\./,'')) continue;
  if (pattern) {{ try {{ if (!(new RegExp(pattern)).test(u.href)) continue; }} catch (_) {{ continue; }} }}
  {query_guard}
  {path_guard}
  const productUrl=u.origin+u.pathname;
  const productName=String(anchor.textContent||'').replace(/\s+/g,' ').trim();
  if (!productName || state.seen[productUrl]) continue;
  const row={{}}; row[{name_output_js}]=productName; row[{url_output_js}]=productUrl;
  state.seen[productUrl]=true; state.rows.push(row);
}}
window.__abcpSkillCollection=state;
return {{candidateCount:state.rows.length}};
""".strip()
    finalize_expression = """
const state=window.__abcpSkillCollection||{rows:[]};
return {structuredRowsJson:JSON.stringify({rows:state.rows}),candidateCount:state.rows.length};
""".strip()
    try:
        max_iterations = int(collect.get("maxRounds") or 25)
    except (TypeError, ValueError):
        max_iterations = 25
    max_iterations = max(1, min(50, max_iterations))
    structured_fields = list(dict.fromkeys(
        [rank_field, name_output_field, url_output_field]
    ))
    return {
        "description": description,
        "variables": {
            "targetUrl": target_url,
            "minRank": rank_window[0],
            "maxRank": rank_window[1],
            "expectedRows": exact_rows,
        },
        "errorConfig": {"onError": "stop", "maxRetries": 1},
        "structured_output": {
            "version": 1,
            "transport": "json_variable",
            "variable": "structuredRowsJson",
            "fields": structured_fields,
            "rank": {"field": rank_field, "source": "dom_order", "base": 1},
            "window": {"source": "phase_validator", "field": rank_field, "inclusive": True},
            "runtime_variables": {
                "target_url": "targetUrl",
                "rank_min": "minRank",
                "rank_max": "maxRank",
                "expected_rows": "expectedRows",
            },
            "source_selector": (
                f"{selector} filtered to unique query-free URLs matching {url_pattern or domain}"
            ),
        },
        "steps": [
            {"action": "Page.navigate", "params": {"url": "$vars.targetUrl"},
             "onError": "continue",
             "purpose": (
                 "Start navigation to the ranked collection page; heavy pages may "
                 "time out at the call boundary, so Page.loaded settles below."
             )},
            {"type": "listen", "event": "Page.loaded", "timeout": 15000,
             "onTimeout": "continue"},
            {"action": "Input.press", "params": {"key": "Escape"},
             "onError": "continue",
             "purpose": "Dismiss the observed non-auth overlay without submitting anything."},
            {"action": "Page.getState", "extract": {
                 "pageUrl": "url", "pageTitle": "title", "pageStatus": "status",
             },
             "purpose": "Bind structured collection output to the actual page."},
            {"action": "Runtime.evaluate", "params": {
                 "expression": init_expression, "returnByValue": True,
             },
             "extract": {"collectionInitReady": "ready"},
             "purpose": "Initialize the page-local unique collection accumulator."},
            {"action": "Runtime.evaluate", "params": {
                 "expression": probe_expression, "returnByValue": True,
             },
             "extract": {"collectionCandidateCount": "candidateCount"},
             "purpose": "Harvest currently materialized clean product links in DOM order."},
            {"type": "loop", "maxIterations": max_iterations,
             "condition": {"path": "$vars.collectionCandidateCount", "operator": "lt",
                           "value": "$vars.maxRank"},
             "body": [
                 {"action": "Input.scroll", "params": {"direction": "down", "amount": 1200},
                  "purpose": "Materialize more ranked collection items."},
                 {"action": "Runtime.evaluate", "params": {
                      "expression": probe_expression, "returnByValue": True,
                  },
                  "extract": {"collectionCandidateCount": "candidateCount"},
                  "purpose": "Merge newly materialized clean product links into the accumulator."},
             ]},
            {"action": "Runtime.evaluate", "params": {
                 "expression": finalize_expression, "returnByValue": True,
             },
             "extract": {"structuredRowsJson": "structuredRowsJson",
                         "collectionCandidateCount": "candidateCount"},
             "purpose": "Serialize accumulated candidates into a scalar JSON workflow variable."},
            {"action": "Page.getState", "extract": {
                 "pageUrl": "url", "pageTitle": "title", "pageStatus": "status",
             },
             "purpose": "Reconfirm page identity after producing structured output."},
        ],
    }


# ---------------------------------------------------------------------------
# distillation + trace selection
# ---------------------------------------------------------------------------

def _distill_candidates(
    traces: List[Path],
    validated: Dict[str, Dict[str, str]],
    *,
    phase_id: str = "",
) -> List[Dict[str, Any]]:
    distiller = _load_distiller()
    candidates: List[Dict[str, Any]] = []
    for path in traces:
        events = _read_jsonl(path)
        if not events:
            continue
        steps, notes, persist, variables = distiller.distill(events)
        if not steps:
            continue
        meta = validated.get(path.stem, {})
        candidates.append({
            "trace": path,
            "steps": steps,
            "notes": notes,
            "persist": persist,
            "variables": variables,
            "domain": _first_navigate_host(events),
            "phase_id": meta.get("phaseId", ""),
            "validated": meta.get("validatedStatus", "") == "validated_done",
            "has_extract": any(isinstance(s, dict) and s.get("extract") for s in steps),
        })
    if phase_id:
        candidates = [c for c in candidates if c["phase_id"] == phase_id]
    # cleanest first: validated phase, extract-bearing, then fewest steps
    candidates.sort(key=lambda c: (not c["validated"], not c["has_extract"], len(c["steps"])))
    return candidates


def _phase_of(plan: Dict[str, Any], phase_id: str) -> Dict[str, Any]:
    for phase in plan.get("phases") or []:
        if isinstance(phase, dict) and str(phase.get("id") or "") == phase_id:
            return phase
    return {}


def _phase_ids(plan: Dict[str, Any]) -> List[str]:
    """Return task-plan phase IDs in declared order (IDs, not list indexes)."""
    return [
        str(phase.get("id")).strip()
        for phase in (plan.get("phases") or [])
        if isinstance(phase, dict) and str(phase.get("id") or "").strip()
    ]


def _expected_rows_of_phase(phase: Dict[str, Any]) -> Optional[int]:
    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    value = expected.get("exact_rows")
    if isinstance(value, int) and value > 0:
        return value
    for validator in phase.get("validators") or []:
        if not isinstance(validator, dict) or validator.get("type") != "exact_rows":
            continue
        value = validator.get("value")
        if isinstance(value, int) and value > 0:
            return value
    return None


def recheck_source_context(skill: Skill) -> Dict[str, Any]:
    """Resolve the generation report back to its source task/phase.

    A live recheck must validate the original phase contract, not merely the
    skill's usually-one-row fallback. Missing/stale provenance is returned
    explicitly so the caller can classify the live check as inconclusive.
    """
    report = load_create_report(getattr(skill, "directory", None))
    source_text = str(report.get("source_task") or "").strip()
    source = Path(source_text).expanduser() if source_text else Path()
    if source_text and not source.is_absolute():
        rooted = SKILLS_DIR_DEFAULT.parent / source
        if rooted.exists():
            source = rooted
    plan = _load_task_plan(source) if source_text and source.is_dir() else {}
    phase_id = str(report.get("phase") or "").strip()
    phase = _phase_of(plan, phase_id) if phase_id else {}
    return {
        "source_task": str(source) if source_text else "",
        "source_exists": bool(source_text and source.exists()),
        "phase_id": phase_id,
        "phase": phase,
        "expected_rows": _expected_rows_of_phase(phase),
        "task_type": str(plan.get("task_type") or phase.get("task_type") or ""),
        "goal": str(plan.get("goal") or ""),
    }


def _validate_requested_phase(
    plan: Dict[str, Any],
    phase_id: str,
) -> tuple[str, List[str]]:
    """Validate a CLI phase against current task_plan.json declarations."""
    normalized = str(phase_id or "").strip()
    if not normalized:
        return "", []
    available_phase_ids = _phase_ids(plan)
    if normalized in available_phase_ids:
        return normalized, []
    available = ", ".join(available_phase_ids) or "（无）"
    return normalized, [
        f"任务计划中不存在 phase id `{normalized}`。可选 phase id: {available}",
        "`--phase` 使用 task_plan.json 的 phases[].id，不是数组序号。",
    ]


# ---------------------------------------------------------------------------
# dedup evidence + objective judgment
# ---------------------------------------------------------------------------

# How many same-domain candidates the LLM objective judge examines per create
# (ordered non-draft → stage match → field overlap). Caps judge cost; the sort
# puts the likeliest duplicates first, so 3 covers realistic per-domain libraries.
_DEDUP_JUDGE_LIMIT = 3


def _dedup_evidence(
    skill: Skill,
    *,
    task_type: str,
    stage_hint: str,
    fields: List[str],
) -> Dict[str, Any]:
    """Deterministic evidence for 'does this existing skill cover the task?'.
    Field comparison is CANONICAL (productUrl == detailUrl), never literal."""
    overlap = sorted(canonical_fields(skill.fields) & canonical_fields(fields))
    return {
        "skill_id": skill.skill_id,
        "domain": skill.domain,
        "stage_hint_match": bool(stage_hint and skill.stage_hint == stage_hint),
        "task_type_match": bool(task_type and skill.task_type == task_type),
        "field_overlap": overlap,
        "is_draft": skill.is_draft,
        "description": str(skill.description or "").strip(),
    }


def _judge_objective(
    objective_judge: Optional[Callable[..., Dict[str, Any]]],
    objective: str,
    evidence: Dict[str, Any],
) -> Dict[str, str]:
    """Run the injected LLM judge; anything unclear/failing → uncertain (the
    human is the final arbiter, the LLM only supplies evidence)."""
    if objective_judge is None:
        return {"verdict": "uncertain", "reason": "未配置 LLM 目标判断器"}
    try:
        raw = objective_judge(objective, evidence) or {}
        verdict = str(raw.get("verdict") or "").strip().lower()
        if verdict not in ("same", "different", "uncertain"):
            verdict = "uncertain"
        return {"verdict": verdict, "reason": str(raw.get("reason") or "").strip()}
    except Exception as exc:
        return {"verdict": "uncertain", "reason": f"LLM 判断失败: {exc}"}


# ---------------------------------------------------------------------------
# scaffolding
# ---------------------------------------------------------------------------

def _slugify_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug or "auto-skill"


def _new_skill_command(
    mode: str,
    task_path: Path,
    *,
    skill_id: str,
    suite: str = "",
    phase_id: str = "",
) -> str:
    """Render a complete, copyable command for resolving a name collision."""
    command = (
        f"/skill-create-{mode} "
        f"{json.dumps(str(task_path), ensure_ascii=False)} --skill {skill_id}"
    )
    if suite:
        command += f" --suite {suite}"
    if phase_id:
        command += f" --phase {phase_id}"
    return command + " --new"


def _next_available_skill_id(skill_id: str, skills_dir: Path) -> str:
    """Return a deterministic sibling ID whose directory does not exist."""
    suffix = 2
    while (skills_dir / f"{skill_id}-{suffix}").exists():
        suffix += 1
    return f"{skill_id}-{suffix}"


def _canonical_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _persist_stem(variable: str) -> str:
    stem = str(variable or "")
    for suffix in ("Text", "Href", "Url", "Value"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _infer_variable_to_field(persist: List[str], fields: List[str]) -> Dict[str, str]:
    """Map distilled extract vars like specsText/priceBoxText to phase fields.

    The harness persists variable names verbatim unless fallback.yaml declares
    variable_to_field. Without this map, an auto-generated skill whose workflow
    fills specsText would fail its own fields_required: [specs] contract.
    Canonical comparison first (prosAndConsText → prosCons), then containment.
    """
    field_by_canonical: Dict[str, str] = {}
    for f in fields:
        field_by_canonical.setdefault(canonical_field(f), f)
        field_by_canonical.setdefault(_canonical_name(f), f)
    out: Dict[str, str] = {}
    for var in persist:
        stem = _persist_stem(var)
        target = field_by_canonical.get(canonical_field(stem)) or field_by_canonical.get(
            _canonical_name(stem)
        )
        if target is None:
            var_key = _canonical_name(stem)
            matches = sorted({
                field
                for key, field in field_by_canonical.items()
                if key and var_key and (key in var_key or var_key in key)
            })
            if len(matches) == 1:
                target = matches[0]
        if target and target != var:
            out[var] = target
    return out


def _draft_contract(
    persist: List[str],
    fields: List[str],
    *,
    mapping: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """The draft's success_contract as a dict — the SAME object drives both the
    fallback.yaml render and the dry-run simulation, so what we validate is
    exactly what we ship. `mapping` (from calibrate_draft_workflow) supersedes
    the bare name-based inference."""
    contract: Dict[str, Any] = {
        "workflow_no_error": True,
        "observation_prefix": "Workflow execution completed:",
        "variables_required": [],
        "variables_any_nonempty": list(persist),
        "persisted_rows_at_least": 1,
        "fields_required": list(fields),
        "fields_nonempty": list(fields),
    }
    mapping = mapping if mapping is not None else _infer_variable_to_field(persist, fields)
    if mapping:
        contract["variable_to_field"] = mapping
    return contract


def _stub_skill(skill_id: str, workflow: Dict[str, Any], contract: Dict[str, Any]) -> Skill:
    return Skill(skill_id=skill_id, directory=None,
                 frontmatter={"name": skill_id}, workflow=workflow,
                 fallback={"success_contract": contract})


def _walk_workflow_steps(steps: Any):
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        yield step
        for branch in ("then", "else", "body"):
            yield from _walk_workflow_steps(step.get(branch))


def _workflow_extract_vars(workflow: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for step in _walk_workflow_steps(workflow.get("steps")):
        extract = step.get("extract")
        if isinstance(extract, dict):
            out.extend(str(k) for k in extract)
    return out


def _step_purpose_by_var(workflow: Dict[str, Any]) -> Dict[str, str]:
    """extract variable → the purpose text of the step that fills it."""
    out: Dict[str, str] = {}
    for step in _walk_workflow_steps(workflow.get("steps")):
        if isinstance(step.get("extract"), dict):
            for var in step["extract"]:
                out[str(var)] = str(step.get("purpose") or "")
    return out


def _artifact_row_fields(base: Path) -> Set[str]:
    """Canonical field names that actually appear non-empty in the task's own
    extraction artifact rows — the EVIDENCE required before declaring a field a
    row-input variable. Without this gate, passthrough completion would let any
    missing field \"pass\" as an empty input and hollow out the dry-run gate."""
    found: Set[str] = set()
    extractions = Path(base) / "artifacts" / "extractions"
    for path in sorted(extractions.glob("*.json")) if extractions.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in data.get("rows") or []:
            if isinstance(row, dict):
                for key, value in row.items():
                    if isinstance(value, (str, int, float)) and str(value).strip():
                        found.add(canonical_field(key))
    return found


def _artifact_scalar_examples(base: Path) -> Dict[str, List[Any]]:
    """Observed scalar values keyed by canonical field for row-contract typing.

    Types are hints for equality normalization, never extraction instructions.
    Ambiguous/mixed observations deliberately collapse to ``scalar`` below.
    """
    found: Dict[str, List[Any]] = {}
    extractions = Path(base) / "artifacts" / "extractions"
    for path in sorted(extractions.glob("*.json")) if extractions.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in data.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if isinstance(value, (str, int, float, bool)) and str(value).strip():
                    bucket = found.setdefault(canonical_field(key), [])
                    if len(bucket) < 20:
                        bucket.append(value)
    return found


def _row_variable_type(values: List[Any], *, is_url: bool = False) -> str:
    if is_url:
        return "uri"
    if not values:
        return "scalar"
    if all(isinstance(value, bool) for value in values):
        return "boolean"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return "integer"
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in values
    ):
        return "number"
    if all(isinstance(value, str) for value in values):
        return "string"
    return "scalar"


def _draft_row_contract(
    workflow: Dict[str, Any],
    persist: List[str],
    fields: List[str],
    *,
    mapping: Optional[Dict[str, str]] = None,
    scalar_examples: Optional[Dict[str, List[Any]]] = None,
) -> Dict[str, Any]:
    """Declare the roles needed for deterministic batch joins.

    A workflow without a variable-bound navigation target is not automatically
    declared batch-capable. For a batch-capable workflow, every role is derived
    from the frozen recipe and expected artifact; no site/field vocabulary is
    embedded in the harness.
    """
    url_var = _workflow_url_var(workflow)
    template = dict(workflow.get("variables") or {})
    if not url_var or url_var not in template:
        return {}
    mapping = mapping or {}
    expected = set(fields)
    passthrough = [
        variable
        for variable in template
        if mapping.get(variable, variable) in expected or variable == url_var
    ]
    if url_var not in passthrough:
        passthrough.append(url_var)
    produced = list(dict.fromkeys(mapping.get(var, var) for var in persist))
    examples = scalar_examples or {}
    variable_types = {
        variable: _row_variable_type(
            examples.get(canonical_field(mapping.get(variable, variable)), []),
            is_url=variable == url_var,
        )
        for variable in passthrough
    }
    return {
        "version": 1,
        "identity_variables": [url_var],
        "passthrough_variables": passthrough,
        "produced_fields": produced,
        "variable_types": variable_types,
    }


def calibrate_draft_workflow(
    workflow: Dict[str, Any],
    persist: List[str],
    fields: List[str],
    *,
    row_field_evidence: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Auto-calibration: derive the contract plumbing the machine can prove,
    so the dry-run gate only blocks on genuine unknowns. Three layers on top of
    the name-based inference, every action recorded, ambiguity never guessed:

      ① purpose inference — an unmapped extract var whose step purpose names
        exactly ONE still-uncovered field maps to it (rwContText with purpose
        \"Extract Reviews section text\" → reviews);
      ② canonical bridge — an uncovered field synonymous with an existing
        variable gets a variable_to_field rename (detailUrl → productUrl);
      ③ passthrough completion — a field still without a source is declared a
        row-input variable ONLY when the task's own artifact rows prove it
        exists (rank/productName in the upstream collection artifact); it
        appears in no step, so it never gates execution.

    Mutates workflow[\"variables\"] (layer ③). Returns
    {\"mapping\", \"added_variables\", \"notes\"(人话，进 SKILL.md/报告)}."""
    mapping = _infer_variable_to_field(persist, fields)
    notes: List[str] = []
    evidence = row_field_evidence or set()

    def covered(field: str) -> bool:
        pool = set(persist) | set((workflow.get("variables") or {}).keys())
        return any(mapping.get(v, v) == field for v in pool)

    purposes = _step_purpose_by_var(workflow)
    for var in persist:
        if var in mapping:
            continue
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*", purposes.get(var, ""))
        hits = sorted({
            f for f in fields
            if not covered(f)
            and any(canonical_field(t) == canonical_field(f) for t in tokens)
        })
        if len(hits) == 1:
            mapping[var] = hits[0]
            notes.append(
                f"{var} → {hits[0]}（依据步骤描述 “{purposes.get(var, '')}”）")

    for f in fields:
        if covered(f):
            continue
        synonyms = sorted({
            v for v in set(persist) | set((workflow.get("variables") or {}).keys())
            if v not in mapping and v != f and canonical_field(v) == canonical_field(f)
        })
        if len(synonyms) == 1:
            mapping[synonyms[0]] = f
            notes.append(f"{synonyms[0]} → {f}（同义字段桥接）")

    added: List[str] = []
    variables = workflow.setdefault("variables", {})
    for f in fields:
        if covered(f):
            continue
        if canonical_field(f) in evidence and f not in variables:
            variables[f] = ""
            added.append(f)
    if added:
        notes.append(
            "补行级输入变量 " + ", ".join(added)
            + "（任务 artifact 行中实际存在；运行时由每行任务数据填充）")
    return {"mapping": mapping, "added_variables": added, "notes": notes}


_PAGE_BINDING_STEP = {
    "action": "Page.getState",
    "params": {},
    "extract": {"pageUrl": "url", "pageTitle": "title"},
    "onError": "continue",
    "purpose": (
        "Bind the extraction to THIS row's page: the harness compares pageUrl "
        "against the navigated URL variable and rejects the row on mismatch "
        "(same-tab batch protection; auto-hardened by /skill-create)."
    ),
}


def harden_draft_workflow(workflow: Dict[str, Any]) -> List[str]:
    """Conservative auto-hardening (07-06 user decision: read-only defenses
    only). Two injections, both proven in taaft-detail-extract v2:

      ① every Page.navigate gets onError:continue — a heavy page makes the
        navigate CALL return -32001 while the page actually loads; the
        distilled `listen Page.loaded` settles it (an explicit onError set by
        the distiller/human is left untouched);
      ③ one native Page.getState extract {pageUrl, pageTitle} before the
        content reads — declaring the binding activates the harness's
        page_binding_mismatch fail-closed protection (wrong-page rows are
        never persisted in same-tab batches).

    Challenge gating and Escape overlay dismiss are deliberately NOT injected
    (human checklist items). Idempotent: existing onError / an existing deep
    pageUrl extract suppress the respective injection. Never touches
    workflow["variables"] (pageUrl/pageTitle are run artifacts, not inputs).
    Returns 人话 notes for SKILL.md / .create_report.json."""
    from harness.skill.dispatch import _iter_steps_deep

    notes: List[str] = []
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        return notes

    nav_hardened = 0
    for step in _iter_steps_deep(steps):
        if step.get("action") == "Page.navigate" and "onError" not in step:
            step["onError"] = "continue"
            nav_hardened += 1
    if nav_hardened:
        notes.append(
            f"navigate 容错: {nav_hardened} 个 Page.navigate 补 onError:continue"
            "（重页面 -32001 超时但实际加载成功，由 listen Page.loaded 兜底）")

    has_binding = any(
        isinstance(step.get("extract"), dict) and "pageUrl" in step["extract"]
        for step in _iter_steps_deep(steps)
    )
    if not has_binding:
        insert_at = None
        for i, step in enumerate(steps):
            if isinstance(step, dict) and step.get("action") == "Page.navigate":
                insert_at = i + 1
                while insert_at < len(steps) and (
                    isinstance(steps[insert_at], dict)
                    and steps[insert_at].get("type") == "listen"
                ):
                    insert_at += 1
                break
        if insert_at is None:
            for i, step in enumerate(steps):
                if isinstance(step, dict) and isinstance(step.get("extract"), dict):
                    insert_at = i
                    break
        if insert_at is not None:
            steps.insert(insert_at, dict(_PAGE_BINDING_STEP))
            notes.append(
                "页面绑定: 注入 Page.getState extract {pageUrl, pageTitle}"
                "（激活 harness 批量防串页 fail-closed——错页行不落盘）")
    return notes


def _humanize_failed_checks(failed_checks: List[str]) -> List[str]:
    """检查项代号 → 人话（fields_required:rank 之类直接打给用户只会造成困惑）。"""
    missing: List[str] = []
    empty: List[str] = []
    others: List[str] = []
    for check in failed_checks or []:
        kind, _, arg = str(check).partition(":")
        if kind == "fields_required":
            missing.append(arg)
        elif kind == "fields_nonempty":
            empty.append(arg)
        elif kind == "persisted_rows_at_least":
            others.append("模拟未能产出任何落盘数据行")
        elif kind == "variables_any_nonempty":
            others.append("工作流的提取变量全为空（extract 没有取到内容）")
        elif kind == "row_contract":
            others.append(f"批量行契约无效: {arg}")
        else:
            others.append(f"检查项未过: {check}")
    out: List[str] = []
    if missing:
        out.append(
            "契约要求每行数据包含字段 " + "、".join(dict.fromkeys(missing))
            + "，但工作流无法产出它们（没有对应的变量或映射，自动校准也没找到可靠依据）")
    leftover = [f for f in dict.fromkeys(empty) if f not in missing]
    if leftover:
        out.append("字段 " + "、".join(leftover) + " 会落盘为空值")
    out.extend(others)
    return out


def write_create_report(directory: Path, updates: Dict[str, Any]) -> None:
    """Merge-write the per-skill generation report (.create_report.json) —
    runtime state (gitignored, like .skill_health.json): the durable failure
    record the LLM context and --recheck/--retry read. Best-effort: the create
    flow must never fail because of bookkeeping."""
    try:
        directory = Path(directory)
        current = load_create_report(directory)
        current.update(updates)
        (directory / CREATE_REPORT_FILENAME).write_text(
            json.dumps(current, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8")
    except Exception:
        pass


def mark_skill_live_tested(skill: Skill) -> None:
    """Promote frontmatter quality flags after a full live recheck passes."""
    if skill.directory is None:
        return
    path = Path(skill.directory) / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return
        end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if end is None:
            return
        for key, value in (("draft", "false"), ("tested", "true")):
            found = False
            for index in range(1, end):
                if re.match(rf"^{re.escape(key)}\s*:", lines[index]):
                    lines[index] = f"{key}: {value}"
                    found = True
                    break
            if not found:
                lines.insert(end, f"{key}: {value}")
                end += 1
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    except (OSError, ValueError):
        return


def simulate_persisted_contract(
    skill_id: str,
    workflow: Dict[str, Any],
    contract: Dict[str, Any],
    row_contract: Optional[Dict[str, Any]] = None,
    *,
    expected_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """Dry-run gate (no browser): pretend every input/extract variable got a
    non-empty value, build the row exactly like the fast path would, and check
    the draft's own persisted contract. Catches variable/field mismatches
    (rwContText vs reviews) and missing passthrough vars (rank/productName)
    before anything ever runs."""
    structured = workflow.get("structured_output")
    structured_ok, structured_failures = validate_structured_output_workflow(
        structured, workflow,
    )
    if structured is not None:
        fields = (
            [str(field) for field in structured.get("fields") or []]
            if isinstance(structured, dict) else []
        )
        row = {field: f"<{field}>" for field in fields}
        rank = structured.get("rank") if isinstance(structured, dict) else None
        if isinstance(rank, dict) and str(rank.get("field") or ""):
            row[str(rank["field"])] = int(rank.get("base", 1) or 1)
        simulated_count = expected_rows or max(
            1, int(contract.get("persisted_rows_at_least", 1) or 1)
        )
        stub = _stub_skill(skill_id, workflow, contract)
        verdict = check_persisted_contract(
            stub, row, None,
            row_count=simulated_count,
            expected_rows=expected_rows,
        )
        failed_checks = list(structured_failures) + list(verdict["failed_checks"])
        return {
            "ok": bool(structured_ok and verdict["ok"]),
            "failed_checks": failed_checks,
            "simulated_row_fields": sorted(row.keys()),
            "simulated_row_count": simulated_count,
        }

    template = workflow.get("variables") or {}
    sim_vars: Dict[str, Any] = {
        k: (v if str(v or "").strip() else f"<{k}>") for k, v in template.items()
    }
    for var in _workflow_extract_vars(workflow):
        sim_vars.setdefault(var, f"<{var}>")
    stub = _stub_skill(skill_id, workflow, contract)
    row = build_extraction_row(stub, {"variables": sim_vars})
    verdict = check_persisted_contract(
        stub,
        row,
        None,
        expected_rows=expected_rows,
    )
    row_ok, row_failures = validate_row_contract(
        row_contract if row_contract else None,
        workflow,
        contract,
    )
    failed_checks = list(verdict["failed_checks"])
    failed_checks.extend(f"row_contract:{failure}" for failure in row_failures)
    return {
        "ok": bool(verdict["ok"] and row_ok),
        "failed_checks": failed_checks,
        "simulated_row_fields": sorted(row.keys()),
    }


# ---------------------------------------------------------------------------
# generality trial (live)
# ---------------------------------------------------------------------------

def _workflow_url_var(workflow: Dict[str, Any]) -> str:
    for step in _walk_workflow_steps(workflow.get("steps")):
        if step.get("action") == "Page.navigate":
            url = str(((step.get("params") or {}).get("url")) or "").strip()
            m = re.match(r"^\$vars\.([A-Za-z0-9_]+)$", url)
            if m:
                return m.group(1)
    return ""


def collect_instance_rows(
    base: Path,
    workflow: Dict[str, Any],
    *,
    limit: int = 3,
    prefer_not: str = "",
) -> List[Dict[str, str]]:
    """Harvest up to `limit` DISTINCT instance variable-sets from the task's own
    extraction artifacts — the generality test runs the workflow on pages the
    trace never touched. Row keys are matched canonically (productUrl fills a
    detailUrl variable); a row's pageUrl is provenance, never the target."""
    url_var = _workflow_url_var(workflow)
    if not url_var:
        return []
    template = workflow.get("variables") or {}
    target_canon = canonical_field(url_var)
    out: List[Dict[str, str]] = []
    seen = set()
    preferred_later: List[Dict[str, str]] = []
    extractions = base / "artifacts" / "extractions"
    for path in sorted(extractions.glob("*.json")) if extractions.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in data.get("rows") or []:
            if not isinstance(row, dict):
                continue
            url = ""
            for key, value in row.items():
                if not isinstance(value, str) or not value.startswith("http"):
                    continue
                if _canonical_name(key) == "pageurl":
                    continue  # provenance of the observation, not the row target
                if canonical_field(key) == target_canon:
                    url = value.strip()
                    break
            if not url:
                continue
            dedupe_key = url.rstrip("/")
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            variables: Dict[str, str] = {url_var: url}
            for tkey in template:
                if tkey == url_var:
                    continue
                for key, value in row.items():
                    if canonical_field(key) == canonical_field(tkey) and isinstance(
                        value, (str, int, float)
                    ):
                        variables[tkey] = str(value)
                        break
            bucket = preferred_later if prefer_not and dedupe_key == prefer_not.rstrip("/") else out
            bucket.append(variables)
    combined = out + preferred_later  # pages the trace never touched first
    return combined[:limit]


async def trial_workflow_live(
    workflow: Dict[str, Any],
    rows: List[Dict[str, str]],
    *,
    ws_config: Any = None,
    client: Any = None,
    agent_id: str = "skill-create-trial",
) -> Dict[str, Any]:
    """Run the draft workflow live for each instance row on ONE warm tab.
    Returns {"attempted": bool, "runs": [{"variables", "result"}], "error"?}."""
    from harness.skill.workflow import run_skill_workflow

    stub = _stub_skill("skill-create-trial", workflow, {})
    own_client = False
    if client is None:
        try:
            from abcp_client import ABCPClient
            from runtime_config import ABCPClientConfig
            client = ABCPClient(ws_config or ABCPClientConfig())
            await client.connect()
            own_client = True
        except Exception as exc:
            return {"attempted": False, "runs": [], "error": f"面板连接失败: {exc}"}
    runs: List[Dict[str, Any]] = []
    try:
        if own_client:
            await client.call("System.register", {"agentId": agent_id})
        fl = await client.call("Fleet.create", {})
        fleet_id = ((fl or {}).get("data") or {}).get("fleetId") or ""
        pg = await client.call("Page.create", {"fleetId": fleet_id, "url": "about:blank"})
        page_id = ((pg or {}).get("data") or {}).get("pageId") or ""
        for variables in rows:
            result = await run_skill_workflow(
                client, stub, page_id=page_id, fleet_id=fleet_id, variables=dict(variables),
            )
            runs.append({"variables": dict(variables), "result": result})
        return {"attempted": True, "runs": runs}
    except Exception as exc:
        return {"attempted": bool(runs), "runs": runs, "error": str(exc)}
    finally:
        if own_client:
            try:
                await client.close()
            except Exception:
                pass


def _evaluate_trial(
    skill_id: str,
    workflow: Dict[str, Any],
    contract: Dict[str, Any],
    trial: Dict[str, Any],
) -> Dict[str, Any]:
    """Per-run success_contract verdicts → tested flag + summary lines."""
    stub = _stub_skill(skill_id, workflow, contract)
    url_var = _workflow_url_var(workflow)
    runs = trial.get("runs") or []
    results: List[Dict[str, Any]] = []
    for run in runs:
        result = run.get("result") or {}
        verdict = check_success_contract(stub, result)
        results.append({
            "url": str((run.get("variables") or {}).get(url_var) or ""),
            "succeeded": bool(result.get("succeeded")),
            "contract_ok": bool(verdict["ok"]),
            "failed_checks": verdict["failed_checks"],
            "failedStepPath": result.get("failedStepPath"),
        })
    tested = bool(trial.get("attempted")) and bool(results) and all(
        r["succeeded"] and r["contract_ok"] for r in results
    )
    return {
        "attempted": bool(trial.get("attempted")),
        "tested": tested,
        "results": results,
        "error": trial.get("error"),
    }


def _rank_window_from_phase(phase: Dict[str, Any], field: str) -> Optional[tuple[int, int]]:
    wanted = canonical_field(field)
    for validator in phase.get("validators") or []:
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


def _first_phase_url(phase: Dict[str, Any]) -> str:
    text = "\n".join(
        str(phase.get(key) or "")
        for key in ("worker_task", "objective", "context")
    )
    match = re.search(r"https?://[^\s\]\[()<>\"']+", text)
    return match.group(0).rstrip(".,;:") if match else ""


def _structured_trial_variables(skill: Skill, phase: Dict[str, Any]) -> Dict[str, Any]:
    config = skill.structured_output
    runtime = config.get("runtime_variables")
    runtime = runtime if isinstance(runtime, dict) else {}
    rank = config.get("rank")
    rank = rank if isinstance(rank, dict) else {}
    window = _rank_window_from_phase(phase, str(rank.get("field") or "rank"))
    values = {
        str(runtime.get("target_url") or "targetUrl"): _first_phase_url(phase),
        str(runtime.get("rank_min") or "minRank"): window[0] if window else None,
        str(runtime.get("rank_max") or "maxRank"): window[1] if window else None,
        str(runtime.get("expected_rows") or "expectedRows"): _expected_rows_of_phase(phase),
    }
    return {
        key: value for key, value in values.items()
        if key in skill.variable_template and value not in (None, "")
    }


def _structured_rows_contract_failures(
    rows: List[Dict[str, Any]], phase: Dict[str, Any],
) -> List[str]:
    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    fields = [str(field) for field in expected.get("fields") or [] if str(field)]
    failures: List[str] = []
    exact = _expected_rows_of_phase(phase)
    if exact is not None and len(rows) != exact:
        failures.append(f"exact_rows:{exact}(got {len(rows)})")
    for index, row in enumerate(rows):
        for field in fields:
            if field not in row or row.get(field) in (None, ""):
                failures.append(f"rows[{index}].{field}:missing_or_empty")
    # Reuse the same row-level range/url-pattern semantics as dispatch.
    try:
        from harness.skill.dispatch import _row_passes_validators
        for index, row in enumerate(rows):
            if not _row_passes_validators(row, phase.get("validators")):
                failures.append(f"rows[{index}]:phase_validator_failed")
    except Exception:
        failures.append("phase_validator_check_unavailable")
    for validator in phase.get("validators") or []:
        if not isinstance(validator, dict) or validator.get("type") != "unique":
            continue
        for field in validator.get("fields") or []:
            values = [str(row.get(str(field)) or "") for row in rows]
            if len(values) != len(set(values)):
                failures.append(f"unique:{field}")
    return failures


def _enrich_structured_recheck_rows(
    skill: Skill,
    rows: List[Dict[str, Any]],
    run_result: Dict[str, Any],
    variables: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Mirror the generic provenance that dispatch adds before validators.

    The structured JSON intentionally carries only business fields.  Rank
    provenance is harness metadata derived from the declared selector and the
    same DOM-order rule, so the live recheck must validate the enriched rows,
    not the pre-enrichment payload.
    """
    config = skill.structured_output
    rank = config.get("rank")
    rank = rank if isinstance(rank, dict) else {}
    rank_field = str(rank.get("field") or "rank")
    selector = str(config.get("source_selector") or "Workflow structured output")
    result_variables = run_result.get("variables")
    result_variables = result_variables if isinstance(result_variables, dict) else {}
    page_url = str(
        result_variables.get("pageUrl")
        or variables.get("targetUrl")
        or ""
    )
    enriched_rows: List[Dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        rank_value = row.get(rank_field)
        row.setdefault("pageUrl", page_url)
        row.setdefault("sourceTool", f"Workflow.execute(skill:{skill.skill_id})")
        row.setdefault("sourceSelectorOrAxId", selector)
        if rank_value not in (None, ""):
            row.setdefault(
                "rankEvidenceText",
                f"Rank {rank_value} is derived from the unique filtered DOM order "
                f"of {selector} on {page_url}.",
            )
        enriched_rows.append(row)
    return enriched_rows


def _validate_structured_recheck_artifact(
    rows: List[Dict[str, Any]], phase: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the authoritative phase artifact validator without keeping a file."""
    import tempfile

    from harness.task_control import validate_worker_artifacts

    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    name = str(expected.get("name") or "skill_recheck")
    with tempfile.TemporaryDirectory(prefix="abcp-skill-recheck-") as temp:
        task_dir = Path(temp)
        artifact_dir = task_dir / "artifacts" / "extractions"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"{_slugify_id(name) or 'skill-recheck'}.json"
        artifact_path.write_text(
            json.dumps({
                "name": name,
                "description": "Ephemeral live skill recheck artifact",
                "rowCount": len(rows),
                "rows": rows,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        contract = dict(phase)
        contract.setdefault("phase_id", str(phase.get("id") or "skill_recheck"))
        return validate_worker_artifacts(
            contract=contract,
            artifacts=[],
            attempt_artifacts=[str(artifact_path)],
            prior_artifacts=[],
            task_dir=task_dir,
        )


async def recheck_skill_live(
    skill: Skill,
    source_context: Dict[str, Any],
    *,
    ws_config: Any = None,
    client: Any = None,
    agent_id: str = "skill-recheck-live",
) -> Dict[str, Any]:
    """Run a real canary against the generation source phase.

    The function never writes health; the CLI owns that policy after classifying
    this result as passed, failed, or inconclusive.
    """
    phase = source_context.get("phase")
    phase = phase if isinstance(phase, dict) else {}
    if not source_context.get("source_exists") or not phase:
        return {
            "status": "inconclusive", "attempted": False,
            "reason": "生成记录中的来源任务或 phase 不可用",
        }
    own_client = False
    if client is None:
        try:
            from abcp_client import ABCPClient
            from runtime_config import ABCPClientConfig
            client = ABCPClient(ws_config or ABCPClientConfig())
            await client.connect()
            own_client = True
        except Exception as exc:
            return {
                "status": "inconclusive", "attempted": False,
                "reason": f"面板连接失败: {exc}",
            }
    try:
        if own_client:
            await client.call("System.register", {"agentId": agent_id})
        if skill.structured_output:
            from harness.skill.structured_output import structured_output_rows
            variables = _structured_trial_variables(skill, phase)
            if not variables or any(
                not str(variables.get(key) or "").strip()
                for key in skill.variable_template
                if f"$vars.{key}" in json.dumps(skill.steps, ensure_ascii=False)
            ):
                return {
                    "status": "failed", "attempted": False,
                    "reason": "无法从来源 phase 派生 structured workflow 运行变量",
                }
            fl = await client.call("Fleet.create", {})
            fleet_id = ((fl or {}).get("data") or {}).get("fleetId") or ""
            pg = await client.call("Page.create", {"fleetId": fleet_id, "url": "about:blank"})
            page_id = ((pg or {}).get("data") or {}).get("pageId") or ""
            result = await run_skill_workflow(
                client, skill, page_id=page_id, fleet_id=fleet_id,
                variables=variables,
            )
            result_variables = result.get("variables")
            result_variables = (
                result_variables if isinstance(result_variables, dict) else {}
            )
            try:
                from harness.skill.dispatch import workflow_challenge_signal
                challenge = workflow_challenge_signal(skill, result, variables)
            except Exception:
                challenge = None
            if challenge is not None:
                return {
                    "status": "inconclusive", "attempted": True,
                    "reason": (
                        "live page is a challenge/HITL surface: "
                        + str(challenge.get("title") or challenge.get("url") or "")
                    ),
                }
            page_status = str(result_variables.get("pageStatus") or "").lower()
            if page_status in {"loading", "navigating", "startedloading"}:
                return {
                    "status": "inconclusive", "attempted": True,
                    "reason": f"page remained unsettled after workflow: {page_status}",
                }
            verdict = check_success_contract(skill, result)
            error_text = " ".join(str(result.get(key) or "") for key in (
                "failedError", "failedPurpose", "exc",
            )).lower()
            if not result.get("succeeded") or not verdict["ok"]:
                environmental = any(marker in error_text for marker in (
                    "cloudflare", "captcha", "challenge", "hitl", "websocket",
                    "connection", "timed out", "timeout",
                ))
                return {
                    "status": "inconclusive" if environmental else "failed",
                    "attempted": True,
                    "reason": error_text or "; ".join(verdict["failed_checks"]),
                    "failed_checks": verdict["failed_checks"],
                }
            try:
                from harness.skill.dispatch import page_binding_mismatch
                binding = page_binding_mismatch(skill, result, variables)
            except Exception as exc:
                return {
                    "status": "inconclusive", "attempted": True,
                    "reason": f"page binding validator unavailable: {exc}",
                }
            if binding is not None:
                return {
                    "status": "failed", "attempted": True,
                    "reason": str(binding.get("reason") or "wrong_page"),
                    "failed_checks": [
                        "page_binding:"
                        + str(binding.get("reason") or "wrong_page")
                    ],
                    "page_binding": binding,
                }
            rank = skill.structured_output.get("rank") or {}
            window = _rank_window_from_phase(phase, str(rank.get("field") or "rank"))
            rows, parse_failures = structured_output_rows(
                skill, result, rank_window=window,
            )
            rows = _enrich_structured_recheck_rows(
                skill, rows, result, variables,
            )
            failures = list(parse_failures)
            failures.extend(_structured_rows_contract_failures(rows, phase))
            artifact_validation: Dict[str, Any] = {}
            if not failures:
                try:
                    artifact_validation = _validate_structured_recheck_artifact(
                        rows, phase,
                    )
                except Exception as exc:
                    return {
                        "status": "inconclusive", "attempted": True,
                        "reason": f"phase artifact validator unavailable: {exc}",
                    }
                if str(artifact_validation.get("status") or "") != "done":
                    failures.append(
                        "artifact_validation:"
                        + str(artifact_validation.get("status") or "unknown")
                    )
            expected_rows = _expected_rows_of_phase(phase)
            for row in rows:
                persisted = check_persisted_contract(
                    skill,
                    row,
                    {"status": "done" if not failures else "needs_fix"},
                    row_count=len(rows),
                    expected_rows=expected_rows,
                )
                failures.extend(persisted["failed_checks"])
            return {
                "status": "passed" if not failures else "failed",
                "attempted": True,
                "reason": "" if not failures else "; ".join(failures),
                "failed_checks": failures,
                "row_count": len(rows),
                "artifact_validation": artifact_validation,
            }

        base = Path(str(source_context.get("source_task") or ""))
        rows = collect_instance_rows(base, skill.workflow, limit=1)
        if not rows:
            return {
                "status": "inconclusive", "attempted": False,
                "reason": "来源 artifacts 中没有可用的试运行实例",
            }
        trial = await trial_workflow_live(
            skill.workflow, rows, client=client, ws_config=ws_config,
            agent_id=agent_id,
        )
        runs = [run for run in (trial.get("runs") or []) if isinstance(run, dict)]
        if not trial.get("attempted") or not runs:
            return {
                "status": "inconclusive", "attempted": False,
                "reason": str(trial.get("error") or "live trial unavailable"),
            }
        if trial.get("error"):
            return {
                "status": "inconclusive", "attempted": True,
                "reason": str(trial.get("error")),
            }
        failures: List[str] = []
        run_summaries: List[Dict[str, Any]] = []
        from harness.skill.dispatch import (
            page_binding_mismatch,
            workflow_challenge_signal,
        )
        for index, run in enumerate(runs):
            result = run.get("result")
            result = result if isinstance(result, dict) else {}
            run_variables = run.get("variables")
            run_variables = run_variables if isinstance(run_variables, dict) else {}
            challenge = workflow_challenge_signal(skill, result, run_variables)
            if challenge is not None:
                return {
                    "status": "inconclusive", "attempted": True,
                    "reason": (
                        "live page is a challenge/HITL surface: "
                        + str(challenge.get("title") or challenge.get("url") or "")
                    ),
                }
            error_text = " ".join(str(result.get(key) or "") for key in (
                "failedError", "failedPurpose", "exc", "observation",
            ))
            if not result.get("succeeded") and any(
                marker in error_text.lower() for marker in (
                    "cloudflare", "captcha", "challenge", "hitl", "websocket",
                    "connection", "timed out", "timeout", "-32001",
                )
            ):
                return {
                    "status": "inconclusive", "attempted": True,
                    "reason": error_text or "live infrastructure failure",
                }
            verdict = check_success_contract(skill, result)
            if not result.get("succeeded") or not verdict["ok"]:
                failures.extend(
                    f"runs[{index}].{item}" for item in verdict["failed_checks"]
                )
                if not result.get("succeeded"):
                    failures.append(f"runs[{index}].workflow_failed")
                continue
            binding = page_binding_mismatch(skill, result, run_variables)
            if binding is not None:
                failures.append(
                    f"runs[{index}].page_binding:"
                    + str(binding.get("reason") or "wrong_page")
                )
                continue
            row = build_extraction_row(
                skill, result, input_variables=run_variables,
            )
            persisted = check_persisted_contract(skill, row, row_count=1)
            failures.extend(
                f"runs[{index}].{item}" for item in persisted["failed_checks"]
            )
            run_summaries.append({
                "url": str(run_variables.get(_workflow_url_var(skill.workflow)) or ""),
                "succeeded": True,
                "contract_ok": bool(verdict["ok"] and persisted["ok"]),
            })
        return {
            "status": "passed" if not failures else "failed",
            "attempted": True,
            "reason": "" if not failures else "; ".join(failures),
            "failed_checks": failures,
            "runs": run_summaries,
        }
    except Exception as exc:
        text = str(exc)
        environmental = any(marker in text.lower() for marker in (
            "cloudflare", "captcha", "challenge", "hitl", "websocket",
            "connection", "timed out", "timeout",
        ))
        return {
            "status": "inconclusive" if environmental else "failed",
            "attempted": True,
            "reason": text,
        }
    finally:
        if own_client:
            try:
                await client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _suite_frontmatter(suite: str) -> str:
    """完整一行 `suite: <名>\\n`（含尾换行），空则空串——用 `{...}draft: true`
    的方式插值，让空 suite 不留空行。技能组（suite）是"选择别名"：多个
    per-stage skill 共享 suite 名，用户 /skill <suite名> 一次选中整组。"""
    s = str(suite or "").strip()
    return f"suite: {s}\n" if s else ""


def _render_skill_md(
    *,
    skill_id: str,
    description: str,
    domain: str,
    task_type: str,
    stage_hint: str,
    fields: List[str],
    provenance: Dict[str, Any],
    notes: List[str],
    tested: bool,
    quality_lines: List[str],
    suite: str = "",
) -> str:
    note_lines = "\n".join(f"- {n}" for n in notes) or "- (无)"
    quality_block = "\n".join(f"- {q}" for q in quality_lines) or "- (未运行任何质量门)"
    fields_line = ", ".join(fields)
    suite_fm = _suite_frontmatter(suite)
    return f"""---
name: {skill_id}
description: |
  {description}
  Triggers on: domain={domain or '<host>'}, task_type={task_type},
  stage_hint={stage_hint or '<stage_hint>'}, artifact fields ⊇ {{{fields_line}}}.
version: 1
domain: {domain or '<host>'}
task_type: {task_type}
stage_hint: {stage_hint}
fields: [{fields_line}]
allow_auto_captcha: false
{suite_fm}draft: true
generated_by: skill-create
source_task: {provenance.get('task', '')}
source_trace: {provenance.get('trace', '')}
tested: {'true' if tested else 'false'}
---

## 状态：AUTO-GENERATED DRAFT（/skill-create，{provenance.get('date', '')}）

由 `/skill-create` 从任务 `{provenance.get('task', '')}` 的成功 trace `{provenance.get('trace', '')}`
（phase `{provenance.get('phase', '') or '未知'}`）自动蒸馏。skill 仅在用户显式选择
（`/skill {skill_id}` / `--skill {skill_id}`）时启用（skill_selection_mode=manual）。

## 质量门结果
{quality_block}

## 校准清单（上线前逐项确认）
- [ ] workflow.json 里所有 `__TODO_LOCATE__` / selector 的耐久性（蒸馏器 notes 见下）。
- [ ] `variables` 是否齐全（输入变量 + rank/productName 类透传变量）。
- [ ] fallback.yaml `variables_any_nonempty` / `fields_required` 与 phase 契约对齐；
      复核自动推断的 `variable_to_field`。
- [ ] 挑战边界（未自动注入，人工决策）：若目标站有 Cloudflare/CAPTCHA，参照
      taaft-detail-extract v2 补 title-only challengeFlag 检测 + `listen Hitl.resumed`
      门控（challengeFlag 不可声明进 variables，否则被当必填）。
- [ ] 遮罩处理（未自动注入，人工决策）：登录/订阅弹窗常见的站点，在内容读取前补
      `Input.press` Escape（onError:continue；表单类任务勿加）。
- [ ] 页面绑定：通常已由自动加固注入 `Page.getState` extract {{pageUrl: "url"}}——
      确认存在即可（--no-harden 生成的需手工补；声明后 harness 对缺 pageUrl 的运行
      fail-closed 判 page_binding_unknown）。
- [ ] 泛用性：若下方试运行未覆盖 ≥2 个不同实例，补测后把 frontmatter `tested` 改 true。
- [ ] 全部确认后移除 frontmatter `draft: true`。

## 蒸馏器 notes
{note_lines}

## 运行指令
1. 取运行期 pageId / fleetId（来自最近 Page.getState / Page.list）；复用已打开的暖 tab。
2. 取运行期 variables（见 workflow.json `variables`；来自上游 collection 行或任务输入）。
3. 调 harness `execute_selected_skill({{pageId, fleetId, variables, rows: []}})`；
   runner 从 registry 读取当前所选 skill 的冻结 workflow，禁止把 steps 复制进 prompt/browser_call。
4. 持久化在 workflow 之外：读 runner 返回的结构化行 → harness `record_extraction`
   （artifact 名由快路径按 `expected_artifact.name` 决定）。
5. 按 fallback.yaml `success_contract` 判定；不成立 → 兜底接管（Page.getState +
   DOM.getAXTree 重感知后继续）。
"""


def _render_fallback_yaml(
    contract: Dict[str, Any],
    row_contract: Optional[Dict[str, Any]] = None,
) -> str:
    def _list(values: Any) -> str:
        items = [str(v) for v in (values or [])]
        return "[" + ", ".join(items) + "]"

    mapping = contract.get("variable_to_field") or {}
    mapping_line = ""
    if mapping:
        body = ", ".join(f"{k}: {v}" for k, v in mapping.items())
        mapping_line = f"  variable_to_field: {{ {body} }}\n"
    row_contract_block = ""
    if row_contract:
        row_contract_block = yaml.safe_dump(
            {"row_contract": row_contract},
            allow_unicode=True,
            sort_keys=False,
        ).rstrip() + "\n\n"
    return f"""# AUTO-GENERATED DRAFT（/skill-create）——结构化成功判据 + 接管策略。字段说明见 skills/README.md §5。

{row_contract_block}success_contract:
  workflow_no_error: true
  observation_prefix: "Workflow execution completed:"
  variables_required: []
  variables_any_nonempty: {_list(contract.get('variables_any_nonempty'))}
{mapping_line}  # 落盘由 harness 在 workflow 返回后做；以下声明落盘后的期望：
  persisted_rows_at_least: 1
  fields_required: {_list(contract.get('fields_required'))}
  fields_nonempty: {_list(contract.get('fields_nonempty'))}

takeover:
  on_call_error:
    recover_via: Workflow.getStatus(runId)
    read: [status.failedStepPath, status.error, status.variables, "status.results[-1].step"]
    reobserve: [Page.getState, DOM.getAXTree]
    semantic_anchor: status.results[-1].step.purpose
  on_contract_unmet:
    from_step: len(results)
    reason: postcondition_unmet
    recover_with: [DOM.getAXTree, DOM.getText]
    then: self_heal_workflow_v2

hitl_boundary:
  detect: [Hitl.resumed]
  action: listen_then_pause

maintenance:
  max_revision_per_failure_class: 3
  disable_after_consecutive_failures: 3
  canary_ttl_hours: 24
  auto_disable_on_challenge: false
"""


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def create_skill_from_task(
    task_path: str | Path,
    *,
    skill_id: str = "",
    suite: str = "",
    phase_id: str = "",
    skills_dir: str | Path = SKILLS_DIR_DEFAULT,
    health: Any = None,
    force: bool = False,
    decision: str = "",
    confirm: Optional[Callable[[Dict[str, Any]], str]] = None,
    objective_judge: Optional[Callable[..., Dict[str, Any]]] = None,
    trial_runner: Optional[Callable[[Dict[str, Any], List[Dict[str, str]]], Dict[str, Any]]] = None,
    run_trial: bool = True,
    overwrite: bool = False,
    harden: bool = True,
) -> Dict[str, Any]:
    """Distill a past task into a new draft skill or a revision candidate.

    Dedup decisions belong to the human: the objective judge examines the top-N
    same-domain candidates one by one, and on the first same/uncertain verdict
    `decision` ("optimize"|"new") or the `confirm` callback picks the path;
    without either the report comes back as status=needs_decision. Scaffolding
    proceeds only when every examined candidate is confidently different.
    `force` is a legacy alias for decision="optimize". `overwrite=True` (only
    honored together with an explicit skill_id — the --retry path) regenerates
    an existing machine-generated skill dir in place and skips dedup entirely
    (source-level dedup already ran when the dir was first created).

    Returns a report dict: status ∈ created | draft_blocked | revision_candidate
    | revision_blocked | needs_decision | aborted | error, plus human-facing
    `messages`."""
    task_path = Path(task_path).expanduser()
    skills_dir = Path(skills_dir)
    decision = str(decision or ("optimize" if force else "")).strip().lower()
    # suite 是"选择别名"，必须是单 token slug（/skill <name> 按空白切、只读第一
    # 个 token；带空格的 suite 永远选不中）。slugify 兜住引号多词输入。
    suite = _slugify_id(suite) if str(suite or "").strip() else ""
    if not task_path.exists() and not task_path.is_absolute():
        # relative worktree/<id> must work no matter where the CLI was launched
        rooted = SKILLS_DIR_DEFAULT.parent / task_path
        if rooted.exists():
            task_path = rooted
    if not task_path.exists():
        return {"status": "error",
                "messages": [f"路径不存在: {task_path}"
                             "（含空格的路径请加引号；相对路径会同时按当前目录和项目根目录解析）"]}

    layout = _resolve_task_layout(task_path)
    base: Path = layout["base"]
    traces: List[Path] = layout["traces"]
    if not traces:
        return {"status": "error", "messages": [f"没有找到 trace（{base}/traces/*.jsonl）"]}

    plan = _load_task_plan(base)
    phase_id, phase_errors = _validate_requested_phase(plan, phase_id)
    if phase_errors:
        return {"status": "error", "messages": phase_errors}
    validated = _validated_workers(base)
    candidates = _distill_candidates(traces, validated, phase_id=phase_id)
    if not candidates:
        return {
            "status": "error",
            "messages": [
                (f"phase `{phase_id}` 没有可归属且可蒸馏的 trace"
                 "（需通过 run.jsonl 的 workerId/phaseId 关联到该 phase）。"
                 if phase_id else "所有 trace 都蒸馏不出可用步骤")
            ],
        }
    best = candidates[0]

    phase = _phase_of(plan, best["phase_id"])
    task_type = str(plan.get("task_type") or "general")
    stage_hint = str(phase.get("stage_hint") or "")
    expected_raw = phase.get("expected_artifact")
    expected = expected_raw if isinstance(expected_raw, dict) else {}
    fields = [str(f) for f in (expected.get("fields") or []) if str(f)]
    domain = best["domain"]
    objective = str(phase.get("objective") or plan.get("goal") or "Automate the recorded browser task")

    workflow_description = (
        f"AUTO-GENERATED by /skill-create from task {base.name}, trace {best['trace'].name}"
        f" (phase {best['phase_id'] or 'unknown'}). Calibrate before trusting."
    )
    workflow = {
        "description": (
            workflow_description
        ),
        "variables": best["variables"] or {"detailUrl": ""},
        "errorConfig": {"onError": "stop", "maxRetries": 1},
        "steps": best["steps"],
    }
    if stage_hint == "collection" and (_expected_rows_of_phase(phase) or 0) > 1:
        collection_workflow = _build_collection_structured_workflow(
            _read_jsonl(best["trace"]),
            phase,
            domain=domain,
            description=workflow_description,
        )
        if collection_workflow is not None:
            workflow = collection_workflow
    if not _is_structurally_valid(workflow):
        return {"status": "error",
                "messages": ["蒸馏结果未过结构校验（steps 缺 action），不落盘"],
                "notes": best["notes"]}

    # ------------------------------------------------------------------
    # dedup BEFORE creating: same-domain candidates + LLM objective judgment
    # + explicit human decision (source-level duplicate prevention)
    # ------------------------------------------------------------------
    registry = SkillRegistry.load(skills_dir)
    requested_skill_id = _slugify_id(skill_id) if skill_id else ""
    overwrite = bool(overwrite and requested_skill_id)  # --retry 专用，必须点名
    explicit_existing = (None if overwrite else
                         (registry.get(requested_skill_id) if requested_skill_id else None))
    same_domain = ([] if overwrite else
                   [s for s in registry.all() if domain and _domain_matches(s.domain, domain)])
    # Non-draft first: a calibrated skill is the reference to optimize against;
    # a draft only surfaces as "已有草稿" when nothing better covers the domain.
    evidence_list = sorted(
        (_dedup_evidence(s, task_type=task_type, stage_hint=stage_hint, fields=fields)
         for s in same_domain),
        key=lambda e: (e["is_draft"], not e["stage_hint_match"], -len(e["field_overlap"])),
    )
    judgments: List[Dict[str, Any]] = []  # every examined candidate, in order
    judgment: Optional[Dict[str, str]] = None
    existing: Optional[Skill] = None
    evidence: Optional[Dict[str, Any]] = None
    covered = False
    if explicit_existing is not None:
        # the user named the reference skill — no scanning, it IS the reference
        existing = explicit_existing
        evidence = next(
            (e for e in evidence_list if e["skill_id"] == existing.skill_id),
            _dedup_evidence(existing, task_type=task_type, stage_hint=stage_hint, fields=fields),
        )
        judgment = _judge_objective(objective_judge, objective, evidence)
        judgments.append({"skill_id": existing.skill_id, **judgment})
        covered = True
    else:
        # Judge the top-N candidates ONE BY ONE (order: non-draft → stage match
        # → field overlap): top-1 alone can shadow the real duplicate at rank 2
        # when it happens to be a genuinely different same-domain skill. The
        # first same/uncertain becomes the human-decision reference; creation
        # requires EVERY examined candidate to be confidently different. N caps
        # LLM-judge cost on large same-domain libraries.
        by_id = {s.skill_id: s for s in same_domain}
        for cand_evidence in evidence_list[:_DEDUP_JUDGE_LIMIT]:
            cand_judgment = _judge_objective(objective_judge, objective, cand_evidence)
            judgments.append({"skill_id": cand_evidence["skill_id"], **cand_judgment})
            if cand_judgment["verdict"] in ("same", "uncertain"):
                existing = by_id[cand_evidence["skill_id"]]
                evidence = cand_evidence
                judgment = cand_judgment
                covered = True
                break
        if not covered and judgments:
            judgment = judgments[0]  # 全部 different——记录在案，取 top-1 判词

    if existing is not None:
        # 两条设置 existing 的路径都同时设了 evidence/judgment；这里做防御性收窄
        # （也让类型检查不再对 Optional 报警）
        if evidence is None:
            evidence = _dedup_evidence(existing, task_type=task_type,
                                       stage_hint=stage_hint, fields=fields)
        if judgment is None:
            judgment = {"verdict": "uncertain", "reason": ""}
        if covered:
            chosen = decision
            if not chosen and confirm is not None:
                try:
                    chosen = str(confirm({
                        "existing": evidence,
                        "objective": objective,
                        "judgment": judgment,
                    }) or "").strip().lower()
                except Exception:
                    chosen = ""
            if chosen in ("quit", "q", "abort"):
                return {"status": "aborted", "skill_id": existing.skill_id,
                        "judgment": judgment, "judgments": judgments,
                        "messages": ["已放弃：未生成任何文件。"]}
            if chosen in ("optimize", "o", "revise"):
                # Same dry-run gate as new drafts, but against the EXISTING
                # skill's success_contract: a distilled workflow whose variables
                # don't map to the contract would produce a candidate that can
                # never pass canary — write it anyway (inert, human-inspectable)
                # but say so as revision_blocked, and skip the live trial.
                revision_sim = simulate_persisted_contract(
                    existing.skill_id,
                    workflow,
                    existing.success_contract,
                    existing.row_contract,
                )
                candidate_path = write_candidate(existing, workflow)
                trial_summary = None
                if revision_sim["ok"] and run_trial and trial_runner is not None:
                    rows = collect_instance_rows(base, workflow, limit=3)
                    if rows:
                        trial_summary = _evaluate_trial(
                            existing.skill_id, workflow, existing.success_contract,
                            trial_runner(workflow, rows),
                        )
                status = "revision_candidate" if revision_sim["ok"] else "revision_blocked"
                messages = [
                    f"已基于现有 skill `{existing.skill_id}` 写修订候选: {candidate_path}"
                    + ("" if revision_sim["ok"] else "（revision_blocked）"),
                ]
                if not revision_sim["ok"]:
                    messages.append(
                        "❌ dry-run 契约模拟未过（对现有 skill 的 success_contract）: "
                        + "; ".join(revision_sim["failed_checks"])
                        + " —— 修复候选后再 canary/promote。"
                    )
                messages.append(
                    "候选不会自动生效：经 canary 验证（skill.heal.canary_validate/self_heal）"
                    "或人工确认后 promote。"
                )
                return {
                    "status": status,
                    "skill_id": existing.skill_id,
                    "candidate_path": str(candidate_path),
                    "trace": str(best["trace"]),
                    "notes": best["notes"],
                    "judgment": judgment,
                    "judgments": judgments,
                    "simulation": revision_sim,
                    "trial": trial_summary,
                    "messages": messages + _trial_messages(trial_summary),
                }
            if chosen in ("new", "n", "create"):
                pass  # fall through to scaffold — user确认业务不同
            else:
                return {
                    "status": "needs_decision",
                    "skill_id": existing.skill_id,
                    "judgment": judgment,
                    "judgments": judgments,
                    "evidence": evidence,
                    "messages": [
                        f"同域已有{'草稿 skill（未校准）' if evidence.get('is_draft') else ' skill'}"
                        f" `{existing.skill_id}`"
                        f"（stage_hint {'一致' if evidence['stage_hint_match'] else '不同'}；"
                        f"字段重叠(归一后): {', '.join(evidence['field_overlap']) or '无'}）。",
                        f"LLM 目标判断: {judgment['verdict']}"
                        + (f" — {judgment['reason']}" if judgment['reason'] else ""),
                        "请决定: 重跑加 --optimize（基于该 skill 写修订候选"
                        + ("，完善该草稿" if evidence.get("is_draft") else "")
                        + "）或 --new（确认业务不同，新建）。",
                    ],
                }
        # 到这里 = covered 且用户选了 "new" → 新建；未 covered（top-N 全部
        # different / 同域无候选）直接走 scaffold，全部判断记录在案

    # ------------------------------------------------------------------
    # scaffold a new draft skill (quality gates first)
    # ------------------------------------------------------------------
    new_id = requested_skill_id if requested_skill_id else _slugify_id(
        f"{domain}-{stage_hint or task_type}")
    target = skills_dir / new_id
    if target.exists() and not overwrite:
        suggested_id = _next_available_skill_id(new_id, skills_dir)
        create_command = _new_skill_command(
            "workflow", base, skill_id=suggested_id,
            suite=suite, phase_id=phase_id,
        )
        return {
            "status": "error",
            "messages": [
                f"skill 名称冲突：`{new_id}` 已存在，不能用这个名称新建 skill。",
                f"新建 skill 请换一个名称后重试: {create_command}",
                f"重新蒸馏现有 skill: /skill-create --retry {new_id}",
            ],
        }

    is_structured = isinstance(workflow.get("structured_output"), dict)
    persist_variables = (
        [str(workflow["structured_output"].get("variable") or "structuredRowsJson")]
        if is_structured else best["persist"]
    )
    calibration = (
        {"mapping": {}, "added_variables": [], "notes": [
            "collection trace → 标准 Workflow.execute structured JSON 多行输出",
            "rank 范围由运行期 phase validator 注入，未冻结来源任务的 41-50",
        ]}
        if is_structured else calibrate_draft_workflow(
            workflow, persist_variables, fields,
            row_field_evidence=_artifact_row_fields(base),
        )
    )
    hardening = harden_draft_workflow(workflow) if harden else []
    contract = _draft_contract(
        persist_variables, fields, mapping=calibration["mapping"]
    )
    if is_structured:
        contract["variables_required"] = [
            persist_variables[0], "collectionCandidateCount", "pageUrl", "pageStatus",
        ]
    row_contract = ({} if is_structured else _draft_row_contract(
        workflow,
        persist_variables,
        fields,
        mapping=calibration["mapping"],
        scalar_examples=_artifact_scalar_examples(base),
    ))
    simulation = simulate_persisted_contract(
        new_id, workflow, contract, row_contract,
        expected_rows=_expected_rows_of_phase(phase),
    )
    quality_lines = [f"自动校准: {note}" for note in calibration["notes"]]
    quality_lines.extend(f"自动加固: {note}" for note in hardening)
    if not harden:
        quality_lines.append("自动加固: 跳过（--no-harden）")
    quality_lines.append(
        "dry-run 契约模拟: " + ("✅ 通过" if simulation["ok"] else
                                "❌ 未过 — " + "; ".join(simulation["failed_checks"])))
    if row_contract:
        quality_lines.append(
            "批量行契约: identity="
            + ", ".join(row_contract["identity_variables"])
            + "; passthrough="
            + ", ".join(row_contract["passthrough_variables"])
            + "; produced="
            + ", ".join(row_contract["produced_fields"])
        )

    trial_summary: Optional[Dict[str, Any]] = None
    tested = False
    if is_structured and simulation["ok"]:
        quality_lines.append(
            "试运行: collection structured workflow 必须用 /skill-create --recheck <id> "
            "执行来源 phase 的完整 live canary"
        )
    elif simulation["ok"] and run_trial and trial_runner is not None:
        rows = collect_instance_rows(
            base, workflow, limit=3,
            prefer_not=_trace_url_of(best),
        )
        if rows:
            trial_summary = _evaluate_trial(new_id, workflow, contract, trial_runner(workflow, rows))
            tested = trial_summary["tested"]
            quality_lines.extend(_trial_messages(trial_summary))
        else:
            quality_lines.append("试运行: 跳过（任务 artifacts 中未找到可用实例行）")
    elif simulation["ok"]:
        quality_lines.append("试运行: 未执行（未提供 trial_runner 或 --no-test）")

    provenance = {
        "task": base.name,
        "trace": best["trace"].name,
        "phase": best["phase_id"],
        "date": _dt.date.today().isoformat(),
    }
    if judgments:
        quality_lines.append(
            "去重判断: " + "; ".join(
                f"{j['skill_id']}→{j['verdict']}"
                + (f"（{j['reason']}）" if j.get("reason") else "")
                for j in judgments
            )
        )
    target.mkdir(parents=True, exist_ok=True)
    (target / "workflow.json").write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "SKILL.md").write_text(
        _render_skill_md(
            skill_id=new_id, description=objective, domain=domain,
            task_type=task_type, stage_hint=stage_hint, fields=fields,
            provenance=provenance, notes=best["notes"],
            tested=tested, quality_lines=quality_lines, suite=suite,
        ), encoding="utf-8")
    (target / "fallback.yaml").write_text(
        _render_fallback_yaml(contract, row_contract), encoding="utf-8",
    )

    status = "created" if simulation["ok"] else "draft_blocked"
    failure_human = _humanize_failed_checks(simulation["failed_checks"])
    source_line = (f"来源: 任务 {base.name} / trace {best['trace'].name}"
                   + (f" / phase {best['phase_id']}" if best['phase_id'] else ""))
    # 结论先行、说人话、给下一步——检查项代号只进报告文件不打给用户（07-06 反馈）
    if simulation["ok"]:
        messages = [f"✅ skill 已生成并通过静态质量门: {new_id}"]
        if calibration["notes"]:
            messages.append(
                f"自动校准 {len(calibration['notes'])} 项（依据已记入 SKILL.md，可复核）:")
            messages.extend(f"  - {note}" for note in calibration["notes"])
        if hardening:
            messages.append(f"自动加固 {len(hardening)} 项:")
            messages.extend(f"  - {note}" for note in hardening)
        messages.append(f"位置: {target}")
        messages.append(source_line)
        messages.extend(_trial_messages(trial_summary))
        if is_structured:
            messages.append(
                f"下一步: /skill-create --recheck {new_id}（默认执行 live canary；"
                "通过后写入 health 并解除 draft）"
            )
            next_actions = [
                f"/skill-create --recheck {new_id} 执行 live canary",
                f"通过后用 /skill {new_id} 启用",
            ]
        else:
            messages.append(
                f"下一步: 任务开始前输入 /skill {new_id} 即可使用"
                "（manual 模式仅显式选择才启用）")
            next_actions = [f"/skill {new_id} 启用（仅显式选择才生效）",
                            "（可选）复核 SKILL.md 校准清单"]
    else:
        messages = ["⚠️ 草稿已生成但未通过质量门，目前不可用（draft_blocked）",
                    f"位置: {target}", source_line, "原因:"]
        messages.extend(f"  - {line}" for line in failure_human)
        messages.append("你需要:")
        messages.append(
            f"  1. 打开 skills/{new_id}/workflow.json，在 \"variables\" 中补声明缺失的"
            "行级输入变量；或在 fallback.yaml 的 variable_to_field 中补"
            "“提取变量→字段”映射")
        messages.append(
            f"  2. 改完验证: /skill-create --recheck {new_id}；"
            f"或按生成记录重新蒸馏: /skill-create --retry {new_id}")
        messages.extend(_trial_messages(trial_summary))
        next_actions = [
            f"编辑 skills/{new_id}/workflow.json 或 fallback.yaml 补齐缺失字段来源",
            f"/skill-create --recheck {new_id} 复检质量门",
            f"/skill-create --retry {new_id} 按生成记录重新蒸馏",
        ]
    write_create_report(target, {
        "status": status,
        "skill_id": new_id,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        # Static/dry-run quality eligibility only. Runtime health remains empty
        # until a suite-routed fast path actually succeeds or fails.
        "cold_start_eligible": bool(simulation["ok"]),
        "source_task": str(base),
        "source_trace": best["trace"].name,
        "phase": best["phase_id"],
        "auto_calibrations": calibration["notes"],
        "auto_hardening": hardening,
        "row_contract": row_contract,
        "failed_checks": simulation["failed_checks"],
        "failure_human": failure_human,
        "next_actions": next_actions,
    })
    return {
        "status": status,
        "skill_id": new_id,
        "path": str(target),
        "trace": str(best["trace"]),
        "notes": best["notes"],
        "judgment": judgment,
        "judgments": judgments,
        "simulation": simulation,
        "row_contract": row_contract,
        "calibration": calibration,
        "trial": trial_summary,
        "tested": tested,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# guidance（hints 层）蒸馏 — /skill-create --guidance
# ---------------------------------------------------------------------------

def _guidance_candidates(
    traces: List[Path],
    validated: Dict[str, Dict[str, str]],
    *,
    phase_id: str = "",
) -> List[Dict[str, Any]]:
    """挑知识最丰富的成功探索 trace（与工作流蒸馏"最少步数优先"相反：
    hints 的原料是探索本身，调用越多、看到的页面行为越全）。"""
    out: List[Dict[str, Any]] = []
    for path in traces:
        events = _read_jsonl(path)
        calls = sum(1 for e in events
                    if isinstance(e, dict) and e.get("type") == "browser_call")
        if not calls:
            continue
        meta = validated.get(path.stem, {})
        out.append({
            "trace": path, "events": events, "calls": calls,
            "phase_id": meta.get("phaseId", ""),
            "validated": meta.get("validatedStatus", "") == "validated_done",
        })
    if phase_id:
        out = [c for c in out if c["phase_id"] == phase_id]
    out.sort(key=lambda c: (not c["validated"], -c["calls"]))
    return out


def _render_guidance_skill_md(
    *,
    skill_id: str,
    description: str,
    domain: str,
    task_type: str,
    stage_hint: str,
    fields: List[str],
    provenance: Dict[str, Any],
    hints_section: str,
    suite: str = "",
) -> str:
    fields_line = ", ".join(fields)
    suite_fm = _suite_frontmatter(suite)
    return f"""---
name: {skill_id}
description: |
  {description}
  Triggers on: domain={domain or '<host>'}, task_type={task_type},
  stage_hint={stage_hint or '<stage_hint>'}, artifact fields ⊇ {{{fields_line}}}.
version: 1
domain: {domain or '<host>'}
task_type: {task_type}
stage_hint: {stage_hint}
fields: [{fields_line}]
allow_auto_captcha: false
{suite_fm}draft: true
generated_by: skill-create-guidance
source_task: {provenance.get('task', '')}
source_trace: {provenance.get('trace', '')}
tested: false
---

## 状态：GUIDANCE SKILL（hints-only，/skill-create --guidance，{provenance.get('date', '')}）

本 skill **没有 workflow 快路径**（目录里刻意没有 workflow.json）：worker 仍自己
执行任务，skill 的价值是下方 hints 小节——由 harness 连同探针协议注进 worker
上下文，省去重复探索。hints 是**待验证假设**：agent 会先验证锚点探针，失配即
整段弃用转自由探索并上报 `guidance_stale`。

由任务 `{provenance.get('task', '')}` 的 trace `{provenance.get('trace', '')}`
（phase `{provenance.get('phase', '') or '未知'}`）蒸馏。仅在用户显式选择
（`/skill {skill_id}`）时启用（skill_selection_mode=manual）。

{hints_section}

## 校准清单（上线前逐项确认）
- [ ] 锚点探针选择器的耐久性（它失配会让整段 hints 被弃用——选最稳的那个）。
- [ ] 删掉对 agent 没有增量价值的行（hints 要 quirk 密度，不要全）。
- [ ] 负知识是否仍然成立（页面改版后"走不通的路"可能已通）。
- [ ] 若 skills/.guidance_health.json 标了 needs_review：复核后
      `/skill-create --recheck {skill_id}` 清标记。
- [ ] 全部确认后移除 frontmatter `draft: true`。
"""


def create_guidance_skill_from_task(
    task_path: str | Path,
    *,
    skill_id: str = "",
    suite: str = "",
    skills_dir: str | Path = SKILLS_DIR_DEFAULT,
    phase_id: str = "",
    decision: str = "",
    confirm: Optional[Callable[[Dict[str, Any]], str]] = None,
    objective_judge: Optional[Callable[..., Dict[str, Any]]] = None,
    overwrite: bool = False,
    allow_unvalidated: bool = False,
) -> Dict[str, Any]:
    """/skill-create --guidance：从历史任务蒸馏 hints（页面知识）层。

    与 workflow 蒸馏是同一产物的另一层（"层不是类"）：
      - 目标 skill 已存在（显式点名，或去重判 same 后用户选 optimize）→ 把
        hints 小节写进该 skill 的 SKILL.md（status=hints_updated）——workflow
        skill 由此获得双层；
      - 无匹配 → scaffold 一个 hints-only 目录（无 workflow.json，
        status=created）。
    质量门 = 知识非空（连 URL/选择器都蒸不出 → error 不落盘）。"""
    task_path = Path(task_path).expanduser()
    skills_dir = Path(skills_dir)
    decision = str(decision or "").strip().lower()
    # suite 必须是单 token slug（见 create_skill_from_task 同名注释）。
    suite = _slugify_id(suite) if str(suite or "").strip() else ""
    if not task_path.exists() and not task_path.is_absolute():
        rooted = SKILLS_DIR_DEFAULT.parent / task_path
        if rooted.exists():
            task_path = rooted
    if not task_path.exists():
        return {"status": "error",
                "messages": [f"路径不存在: {task_path}"
                             "（含空格的路径请加引号；相对路径会同时按当前目录和项目根目录解析）"]}

    layout = _resolve_task_layout(task_path)
    base: Path = layout["base"]
    traces: List[Path] = layout["traces"]
    if not traces:
        return {"status": "error", "messages": [f"没有找到 trace（{base}/traces/*.jsonl）"]}

    plan = _load_task_plan(base)
    phase_id, phase_errors = _validate_requested_phase(plan, phase_id)
    if phase_errors:
        return {"status": "error", "messages": phase_errors}
    validated = _validated_workers(base)
    candidates = _guidance_candidates(traces, validated, phase_id=phase_id)
    if not candidates:
        return {
            "status": "error",
            "messages": [
                (f"phase `{phase_id}` 没有可归属的可用 trace"
                 "（需通过 run.jsonl 的 workerId/phaseId 关联到该 phase）。"
                 if phase_id else "没有可用 trace")
            ],
        }
    best = candidates[0]  # _guidance_candidates 已按 validated 优先排序
    # validated 强制（次要项）：从失败/未验证任务蒸出的页面知识可能误导，默认
    # 只从 validated trace 蒸。全部非 validated 时报错并给逃生口，除非显式放行。
    from_unvalidated = not best["validated"]
    if from_unvalidated and not allow_unvalidated:
        return {"status": "error",
                "messages": [
                    f"最佳候选 trace {best['trace'].name} 未通过验证"
                    "（validatedStatus≠validated_done）——从未验证任务蒸馏的页面知识"
                    "可能误导。",
                    "如确认要用: 重跑加 --allow-unvalidated（产物会标注待复审）。",
                ]}

    from harness.skill.guidance import (
        default_guidance_health,
        distill_guidance_from_trace,
        knowledge_is_empty,
        render_hints_markdown,
        replace_hints_section,
    )
    knowledge = distill_guidance_from_trace(best["events"])
    if knowledge_is_empty(knowledge):
        return {"status": "error",
                "messages": [f"trace {best['trace'].name} 蒸不出页面知识"
                             "（无 URL/选择器/负知识/遮罩记录），未生成任何文件"]}

    phase = _phase_of(plan, best["phase_id"])
    task_type = str(plan.get("task_type") or "general")
    stage_hint = str(phase.get("stage_hint") or "")
    expected_raw = phase.get("expected_artifact")
    expected = expected_raw if isinstance(expected_raw, dict) else {}
    fields = [str(f) for f in (expected.get("fields") or []) if str(f)]
    domain = _first_navigate_host(best["events"])
    objective = str(phase.get("objective") or plan.get("goal")
                    or "Guidance for the recorded browser task")
    provenance = {
        "task": base.name,
        "trace": best["trace"].name,
        "phase": best["phase_id"],
        "date": _dt.date.today().isoformat(),
    }
    hints_section = render_hints_markdown(
        knowledge,
        provenance=f"{base.name}/{best['trace'].name}"
                   + (f", phase {best['phase_id']}" if best["phase_id"] else ""),
    )

    registry = SkillRegistry.load(skills_dir)
    requested_skill_id = _slugify_id(skill_id) if skill_id else ""

    def _update_hints(target_skill: Skill) -> Dict[str, Any]:
        if target_skill.directory is None:
            return {"status": "error",
                    "messages": [f"skill `{target_skill.skill_id}` 没有目录，无法写 hints"]}
        md_path = target_skill.directory / "SKILL.md"
        new_text = replace_hints_section(target_skill.skill_md, hints_section)
        # 传了 suite 且该 skill 尚无 suite → 补写进 frontmatter（把已有 skill
        # 纳入技能组；已有 suite 的不覆盖，避免误改用户分组）
        if suite and not str(target_skill.frontmatter.get("suite") or "").strip():
            from harness.skill.guidance import set_frontmatter_suite
            new_text = set_frontmatter_suite(new_text, suite)
        md_path.write_text(new_text, encoding="utf-8")
        # 新蒸馏的 hints 覆盖旧知识 → 疑似腐烂标记一并清零（人工触发的重蒸馏
        # 就是 needs_review 的解决动作）
        default_guidance_health().mark_reviewed(target_skill.skill_id)
        write_create_report(target_skill.directory, {
            "status": "hints_updated",
            "mode": "guidance",
            "skill_id": target_skill.skill_id,
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "source_task": str(base),
            "source_trace": best["trace"].name,
            "phase": best["phase_id"],
        })
        layer = ("hints-only" if target_skill.is_hints_only
                 else "workflow+hints 双层")
        suite_msg = ([f"已纳入技能组 suite=`{suite}`"]
                     if suite and not str(target_skill.frontmatter.get("suite") or "").strip()
                     else [])
        return {
            "status": "hints_updated",
            "skill_id": target_skill.skill_id,
            "path": str(target_skill.directory),
            "trace": str(best["trace"]),
            "knowledge": knowledge,
            "messages": [
                f"✅ hints 已写入现有 skill `{target_skill.skill_id}`（现为 {layer}）",
                *suite_msg,
                f"位置: {md_path}",
                f"来源: 任务 {base.name} / trace {best['trace'].name}"
                + (f" / phase {best['phase_id']}" if best["phase_id"] else ""),
                "下一步: 复核 SKILL.md 的 hints 小节（quirk 密度、锚点探针耐久性）",
            ],
        }

    explicit_existing = registry.get(requested_skill_id) if requested_skill_id else None
    if explicit_existing is not None and not overwrite:
        return _update_hints(explicit_existing)

    # 去重（与 workflow 蒸馏同一裁决机制）：同域候选逐判，首个 same/uncertain
    # 交人决策——optimize = 把 hints 叠进该 skill（双层），new = 新建 hints-only
    if not overwrite:
        same_domain = [s for s in registry.all()
                       if domain and _domain_matches(s.domain, domain)]
        evidence_list = sorted(
            (_dedup_evidence(s, task_type=task_type, stage_hint=stage_hint, fields=fields)
             for s in same_domain),
            key=lambda e: (e["is_draft"], not e["stage_hint_match"], -len(e["field_overlap"])),
        )
        by_id = {s.skill_id: s for s in same_domain}
        judgments: List[Dict[str, Any]] = []
        for cand_evidence in evidence_list[:_DEDUP_JUDGE_LIMIT]:
            cand_judgment = _judge_objective(objective_judge, objective, cand_evidence)
            judgments.append({"skill_id": cand_evidence["skill_id"], **cand_judgment})
            if cand_judgment["verdict"] not in ("same", "uncertain"):
                continue
            existing = by_id[cand_evidence["skill_id"]]
            chosen = decision
            if not chosen and confirm is not None:
                try:
                    chosen = str(confirm({
                        "existing": cand_evidence,
                        "objective": objective,
                        "judgment": cand_judgment,
                        "mode": "guidance",
                    }) or "").strip().lower()
                except Exception:
                    chosen = ""
            if chosen in ("quit", "q", "abort"):
                return {"status": "aborted", "skill_id": existing.skill_id,
                        "judgment": cand_judgment, "judgments": judgments,
                        "messages": ["已放弃：未生成任何文件。"]}
            if chosen in ("optimize", "o", "revise"):
                report = _update_hints(existing)
                report["judgments"] = judgments
                return report
            if chosen in ("new", "n", "create"):
                break  # 用户确认业务不同 → 新建 hints-only
            return {
                "status": "needs_decision",
                "skill_id": existing.skill_id,
                "judgment": cand_judgment,
                "judgments": judgments,
                "evidence": cand_evidence,
                "messages": [
                    f"同域已有 skill `{existing.skill_id}`"
                    f"（stage_hint {'一致' if cand_evidence['stage_hint_match'] else '不同'}；"
                    f"字段重叠(归一后): {', '.join(cand_evidence['field_overlap']) or '无'}）。",
                    f"LLM 目标判断: {cand_judgment['verdict']}"
                    + (f" — {cand_judgment['reason']}" if cand_judgment['reason'] else ""),
                    "请决定: 重跑加 --optimize（把 hints 写进该 skill，成为"
                    " workflow+hints 双层）或 --new（确认业务不同，新建 hints-only skill）。",
                ],
            }

    # scaffold 新 hints-only skill（无 workflow.json）
    new_id = requested_skill_id or _slugify_id(f"{domain}-{stage_hint or task_type}")
    target = skills_dir / new_id
    if target.exists() and not overwrite:
        suggested_id = _next_available_skill_id(new_id, skills_dir)
        create_command = _new_skill_command(
            "guidance", base, skill_id=suggested_id,
            suite=suite, phase_id=phase_id,
        )
        return {
            "status": "error",
            "messages": [
                f"skill 名称冲突：`{new_id}` 已存在，不能用这个名称新建 skill。",
                f"新建 skill 请换一个名称后重试: {create_command}",
                f"重新蒸馏现有 skill: /skill-create --retry {new_id}",
            ],
        }
    target.mkdir(parents=True, exist_ok=True)
    section = hints_section
    if from_unvalidated:
        section = ("> ⚠️ 本 hints 蒸馏自**未验证 trace**（源任务未通过验证），"
                   "上线前务必逐条复核页面知识是否可信。\n\n") + hints_section
    (target / "SKILL.md").write_text(
        _render_guidance_skill_md(
            skill_id=new_id, description=objective, domain=domain,
            task_type=task_type, stage_hint=stage_hint, fields=fields,
            provenance=provenance, hints_section=section, suite=suite,
        ), encoding="utf-8")
    default_guidance_health().mark_reviewed(new_id)
    write_create_report(target, {
        "status": "created",
        "mode": "guidance",
        "skill_id": new_id,
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_task": str(base),
        "source_trace": best["trace"].name,
        "phase": best["phase_id"],
    })
    return {
        "status": "created",
        "skill_id": new_id,
        "path": str(target),
        "trace": str(best["trace"]),
        "knowledge": knowledge,
        "messages": [
            f"✅ guidance skill（hints-only）已生成: {new_id}",
            f"位置: {target}（刻意没有 workflow.json——慢路径就是执行路径）",
            f"来源: 任务 {base.name} / trace {best['trace'].name}"
            + (f" / phase {best['phase_id']}" if best["phase_id"] else ""),
            "下一步: 复核 SKILL.md 的 hints 小节与校准清单；"
            f"任务开始前输入 /skill {new_id} 即可使用（manual 模式仅显式选择才启用）",
        ],
    }


def _trace_url_of(best: Dict[str, Any]) -> str:
    for note in best.get("notes") or []:
        m = re.search(r"trace url: (https?://\S+)\)?", str(note))
        if m:
            return m.group(1).rstrip(")")
    return ""


def _trial_messages(trial_summary: Optional[Dict[str, Any]]) -> List[str]:
    if not trial_summary:
        return []
    if not trial_summary.get("attempted"):
        return ["试运行: 未能执行"
                + (f"（{trial_summary.get('error')}）" if trial_summary.get("error") else "")]
    lines = []
    for r in trial_summary.get("results") or []:
        ok = r["succeeded"] and r["contract_ok"]
        lines.append(
            ("✅" if ok else "❌") + f" 试运行 {r['url'] or '(未知URL)'}: "
            + ("succeeded+contract 通过" if ok else
               f"失败（succeeded={r['succeeded']}, failed_checks={r['failed_checks']}, "
               f"failedStepPath={r['failedStepPath']}）")
        )
    verdict = "✅ 泛用性试运行全部通过" if trial_summary.get("tested") else "⚠️ 泛用性试运行未全部通过（tested=false）"
    return [verdict] + lines
