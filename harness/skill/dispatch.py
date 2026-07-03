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
from typing import Any, Dict, List, Optional, Sequence

from harness.skill.pause import HitlOnsetMonitor, classify_run_for_hitl
from harness.skill.registry import Skill, SkillRegistry
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
) -> Optional[Skill]:
    """Explicit skill_id wins; else deterministic match on contract dims."""
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


def build_extraction_row(skill: Skill, run_result: Dict[str, Any]) -> Dict[str, Any]:
    """variables → one record_extraction row. Field names come from the skill's
    declared variable_to_field map (else the variable name verbatim — no implicit
    *Text stripping); provenance is the skill's own extract action."""
    variables = run_result.get("variables") or {}
    field_map = _field_map(skill)
    row: Dict[str, Any] = {}
    for k, v in variables.items():
        if k in ("pageId", "fleetId"):
            continue
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


def resolve_skill_and_variables(
    registry: SkillRegistry,
    worker_contract: Optional[Dict[str, Any]],
    *,
    phase: Optional[Dict[str, Any]] = None,
    task: str = "",
    context: str = "",
) -> tuple[Optional[Skill], Dict[str, Any]]:
    """Deterministically resolve which skill a task maps to + its derived
    variables, using the exact same logic as the fast path. Used by the
    self-heal hook to know which skill to heal and with what canary variables
    (no state threaded from the fast-path attempt)."""
    worker_contract = worker_contract or {}
    target_url = _find_url("", [worker_contract, phase, task, context]) or ""
    skill = select_skill(registry, worker_contract, target_url=target_url)
    if skill is None:
        return None, {}
    variables = derive_variables(skill, worker_contract, phase, task, context)
    return skill, variables


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
    fleet_id = next(iter(fleet_ids), "") if fleet_ids else ""
    if not fleet_id:
        fl = await agent.browser.call("Fleet.create", {})
        fleet_id = ((fl or {}).get("data") or {}).get("fleetId") or ""
    pg = await agent.browser.call("Page.create", {"fleetId": fleet_id, "url": "about:blank"})
    page_id = ((pg or {}).get("data") or {}).get("pageId") or ""
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
    skill = select_skill(registry, worker_contract, target_url=target_url)
    if skill is None:
        return None

    # P5: a rotted skill (too many consecutive failures) is disabled → slow path.
    if health is not None and health.is_disabled(skill):
        _log(agent, "skill.fast_path.disabled",
             {"skill": skill.skill_id, "health": health.entry(skill.skill_id)})
        return None

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
    run_result, observed_signal = await _run_skill_with_optional_control(
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

    row = build_extraction_row(skill, run_result)
    artifact: Dict[str, Any] = {}
    if record_extraction is not None:
        try:
            artifact = record_extraction(agent, {
                "name": f"{skill.skill_id}-extraction",
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
    persisted = check_persisted_contract(skill, row, artifact)
    if not persisted["ok"]:
        if health is not None:
            health.record(skill, False)
        _log(agent, "skill.fast_path.persisted_contract_unmet", {
            "skill": skill.skill_id, "runId": run_id,
            "failed_checks": persisted["failed_checks"],
            "artifactStatus": artifact.get("status"),
        })
        return None  # incomplete row / failed artifact validation → slow path

    if health is not None:
        health.record(skill, True)
    _log(agent, "skill.fast_path.completed",
         {"skill": skill.skill_id, "runId": run_id, "row_fields": sorted(row.keys())})
    return {"handled": True, "answer": _completed_answer(skill, run_result, artifact),
            "skill": skill.skill_id, "runId": run_id}


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
