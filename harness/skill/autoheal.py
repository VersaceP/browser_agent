"""harness.skill.autoheal — close the rotted-skill self-heal loop.

When the skill fast path falls back (a frozen workflow failed) AND the BrowserAgent
slow path then SUCCEEDS for that same task, the successful trace is a working route
the frozen skill couldn't take. This module distills that trace into a *candidate*
workflow and runs skill_heal (write candidate → canary → promote):

    rotted skill → slow-path success → auto-distilled candidate → canary-gated promote

Everything here is best-effort and gated:
  - only fires for a skill that is *degraded* (health shows recent failures), so a
    healthy skill that merely fell back for unrelated reasons is never churned;
  - the candidate must canary-pass its own success_contract before promotion, so a
    bad auto-distilled draft never replaces the live workflow;
  - any exception is swallowed and reported — healing never affects the worker result.

The candidate is produced by reusing the P3 distiller (skills/_tools/distill_trace.py)
on the in-memory trace, which has the same event schema as the JSONL it normally reads.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from harness.skill.distiller import load_distiller
from harness.skill.heal import self_heal
from harness.skill.registry import Skill
from harness.workflow_policy import harden_navigation_lifecycle

# Backward-compatible name used by skill/create.py and existing tests.
_load_distiller = load_distiller


def distill_trace_to_workflow(
    trace: List[Dict[str, Any]],
    base_skill: Skill,
) -> Optional[Dict[str, Any]]:
    """Distill a successful slow-path trace into a candidate workflow dict, seeded
    with the live skill's variable template + errorConfig. Returns None if no usable
    steps were recovered."""
    distiller = _load_distiller()
    steps, _notes, _persist, variables = distiller.distill(list(trace or []))
    if not steps:
        return None
    steps = harden_navigation_lifecycle(steps)
    # Prefer the live skill's variable template (preserves passthrough vars like
    # rank/productName); fall back to what the distiller inferred.
    template = dict(base_skill.variable_template) or (variables or {"detailUrl": ""})
    return {
        "description": f"Auto-distilled candidate for {base_skill.skill_id} from a successful slow-path run.",
        "variables": template,
        "errorConfig": base_skill.error_config or {"onError": "stop", "maxRetries": 1},
        "steps": steps,
    }


def is_degraded(skill: Skill, health: Any) -> bool:
    """A skill worth healing: explicitly disabled OR at least one recent failure."""
    if health is None:
        return False
    if health.is_disabled(skill):
        return True
    entry = health.entry(skill.skill_id)
    return int(entry.get("consecutive_failures", 0) or 0) >= 1


async def maybe_autoheal_from_trace(
    agent: Any,
    *,
    skill: Skill,
    health: Any,
    trace: List[Dict[str, Any]],
    canary_variables: Dict[str, Any],
    page_id: Optional[str] = None,
    fleet_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Distill the trace into a candidate and run the canary-gated self-heal.

    Caller guarantees: the fast path fell back, the slow path succeeded, and the
    skill matched this task. We additionally require the skill to be degraded
    (is_degraded) so healthy skills are never churned. Returns the self_heal
    report ({"promoted": bool, ...}) or None when nothing was attempted."""
    if skill is None or skill.directory is None:
        return None
    if getattr(skill, "is_hints_only", False):
        # guidance skill 没有 workflow 可修；蒸一个出来会静默改变它的性质
        # （hints 按设计保持建议性）。想升格成 workflow 走 /skill-create。
        _log(agent, "skill.autoheal.skipped", {"skill": skill.skill_id, "reason": "hints_only"})
        return None
    if not is_degraded(skill, health):
        _log(agent, "skill.autoheal.skipped", {"skill": skill.skill_id, "reason": "not_degraded"})
        return None
    try:
        candidate = distill_trace_to_workflow(trace, skill)
    except Exception as exc:  # distillation must never break the worker
        _log(agent, "skill.autoheal.distill_error", {"skill": skill.skill_id, "error": str(exc)})
        return None
    if candidate is None:
        _log(agent, "skill.autoheal.no_candidate", {"skill": skill.skill_id})
        return None

    _log(agent, "skill.autoheal.attempt", {
        "skill": skill.skill_id,
        "candidate_steps": len(candidate.get("steps") or []),
        "canary_variables": {k: v for k, v in canary_variables.items() if k not in ("pageId", "fleetId")},
    })
    try:
        report = await self_heal(
            skill, candidate,
            browser=agent.browser,
            canary_variables=canary_variables,
            health=health,
            page_id=page_id, fleet_id=fleet_id,
        )
    except Exception as exc:  # canary/promote must never break the worker
        _log(agent, "skill.autoheal.error", {"skill": skill.skill_id, "error": str(exc)})
        return None

    _log(agent, "skill.autoheal.result", {
        "skill": skill.skill_id,
        "promoted": report.get("promoted"),
        "reason": report.get("reason"),
        "candidate": report.get("candidate"),
    })
    return report


def _log(agent: Any, event: str, payload: Dict[str, Any]) -> None:
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:  # pragma: no cover
            pass
