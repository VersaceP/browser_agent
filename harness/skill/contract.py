"""harness.skill.contract - skill selection/enrichment for worker contracts.

LeadAgent has the phase rows, detail URLs, and validators, so skill selection
belongs at spawn time. The worker-level dispatch still owns execution and
fallback: explicit skill_id wins, selected variables are validated, and failed
fast paths fall through to the BrowserAgent slow path.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# Minimum soft-recall score required to interrupt the Lead with a selection
# request. Paired with skill_selection_signal (structural gate) so neither a
# bare domain match (+8) nor keyword-only overlap can trigger on its own.
_MIN_SELECTION_SCORE = 4


def apply_forced_skill(
    worker_contract: Dict[str, Any],
    *,
    registry: Any,
    forced_skill_id: str,
    logger: Any = None,
) -> bool:
    """Human/operator override: stamp a forced skill_id onto the worker_contract.

    Returns True if a valid forced skill was applied. A forced skill wins over the
    LeadAgent's auto-match, explicit selection, AND decline (it is a deliberate
    operator directive), so any prior skill_selection is cleared. An unknown
    forced id is ignored (logged), leaving normal selection intact. Downstream the
    stamped skill_id makes build_skill_selection_request short-circuit and
    select_skill return this skill; variable derivation / success contract still
    gate the fast path, so a phase the skill does not fit falls back safely.
    """
    forced = str(forced_skill_id or "").strip()
    if not forced or registry is None or not isinstance(worker_contract, dict):
        return False
    try:
        skill = registry.get(forced)
    except Exception:  # pragma: no cover - registry lookup must never break spawn
        skill = None
    if skill is None:
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.forced.unknown", {"skill_id": forced})
            except Exception:  # pragma: no cover
                pass
        return False
    worker_contract["skill_id"] = forced
    worker_contract.pop("skill_selection", None)  # operator force overrides decline
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write("skill.forced", {"skill_id": forced})
        except Exception:  # pragma: no cover
            pass
    return True


def skill_selection_declined(worker_contract: Dict[str, Any]) -> bool:
    selection = worker_contract.get("skill_selection")
    if not isinstance(selection, dict):
        return False
    if selection.get("use_skill") is False:
        return True
    status = str(selection.get("status") or "").strip().lower()
    return status in {"declined", "skip", "skipped", "none"}


def _selection_skill_id(worker_contract: Dict[str, Any]) -> str:
    skill_id = str(worker_contract.get("skill_id") or "").strip()
    if skill_id:
        return skill_id
    selection = worker_contract.get("skill_selection")
    if isinstance(selection, dict) and selection.get("use_skill") is not False:
        return str(selection.get("skill_id") or "").strip()
    return ""


def enrich_worker_contract_with_skill(
    worker_contract: Dict[str, Any],
    *,
    registry: Any,
    phase: Optional[Dict[str, Any]] = None,
    task: str = "",
    context: str = "",
    logger: Any = None,
) -> Optional[Dict[str, Any]]:
    """Stamp skill_id (+ skill_variables) onto worker_contract when a skill matches.

    Returns enrichment info or None. Mutates worker_contract in place. This keeps
    the old deterministic route for explicit/accepted skills and no-candidate
    cases, while respecting an explicit Lead decision to decline skill use.
    """
    if registry is None or not isinstance(worker_contract, dict):
        return None
    selected_skill_id = _selection_skill_id(worker_contract)
    if selected_skill_id and not worker_contract.get("skill_id"):
        worker_contract["skill_id"] = selected_skill_id
    if skill_selection_declined(worker_contract):
        return None
    if worker_contract.get("skill_id") and isinstance(worker_contract.get("skill_variables"), dict):
        return None
    try:
        from harness.skill.dispatch import (
            _find_url,
            derive_variables,
            required_filled,
            select_skill,
        )
    except Exception:  # pragma: no cover
        return None

    try:
        target_url = _find_url("", [worker_contract, phase, task, context]) or ""
        skill = select_skill(registry, worker_contract, target_url=target_url)
        if skill is None:
            return None

        info: Dict[str, Any] = {"skill": skill.skill_id}
        if not worker_contract.get("skill_id"):
            worker_contract["skill_id"] = skill.skill_id
            info["set_skill_id"] = True

        if not isinstance(worker_contract.get("skill_variables"), dict):
            variables = derive_variables(skill, worker_contract, phase or {}, task, context)
            if required_filled(skill, variables):
                worker_contract["skill_variables"] = variables
                info["set_skill_variables"] = sorted(variables.keys())
            else:
                info["skill_variables_unfilled"] = True

        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.contract.enriched", info)
            except Exception:  # pragma: no cover
                pass
        return info
    except Exception as exc:  # never break spawning
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.contract.enrich_error", {"error": str(exc)})
            except Exception:  # pragma: no cover
                pass
        return None


def build_skill_selection_request(
    worker_contract: Dict[str, Any],
    *,
    registry: Any,
    phase: Optional[Dict[str, Any]] = None,
    task: str = "",
    context: str = "",
    logger: Any = None,
    max_candidates: int = 3,
) -> Optional[Dict[str, Any]]:
    """Return a LeadAgent-facing skill selection request, or None.

    Candidate recall is soft: keywords/field aliases/domain/stage rank
    candidates, but only the LeadAgent's follow-up call chooses or declines.
    """
    if registry is None or not isinstance(worker_contract, dict):
        return None
    if _selection_skill_id(worker_contract) or skill_selection_declined(worker_contract):
        return None
    try:
        from harness.skill.dispatch import (
            _contract_fields,
            _find_url,
            derive_variables,
            required_filled,
        )
    except Exception:  # pragma: no cover
        return None

    try:
        fields = _contract_fields(worker_contract)
        target_url = _find_url("", [worker_contract, phase, task, context]) or ""
        domain = _host_of_url(target_url) or str(worker_contract.get("domain") or "")
        text = " ".join([
            str(task or ""),
            str(context or ""),
            json.dumps(phase or {}, ensure_ascii=False, default=str),
            json.dumps(worker_contract or {}, ensure_ascii=False, default=str),
        ])
        stage_hint = str(worker_contract.get("stage_hint") or "")
        candidates = registry.soft_candidates(
            domain=domain,
            task_type=str(worker_contract.get("task_type") or ""),
            stage_hint=stage_hint,
            fields=fields,
            text=text,
            limit=max_candidates,
        )
        # Gate before interrupting the Lead: pure keyword overlap (common tokens
        # vs a SKILL.md body) or a bare domain match would otherwise raise a
        # skill_selection_required round-trip on nearly every browser phase, and
        # surface single-detail skills over unrelated collection phases. Require
        # a structural applicability signal (exact stage_hint or a non-generic
        # output-field overlap) plus a minimum total score.
        try:
            from harness.skill.registry import skill_selection_signal
            candidates = [
                (skill, score, reasons)
                for (skill, score, reasons) in candidates
                if score >= _MIN_SELECTION_SCORE
                and skill_selection_signal(
                    skill, domain=domain, stage_hint=stage_hint, fields=fields
                )
            ]
        except Exception:  # pragma: no cover - never block spawning on gate import
            pass
        if not candidates:
            return None

        payload_candidates: List[Dict[str, Any]] = []
        for skill, score, reasons in candidates:
            variables = derive_variables(skill, worker_contract, phase or {}, task, context)
            payload_candidates.append(_skill_candidate_payload(
                skill,
                score=score,
                reasons=reasons,
                variables=variables,
                required_filled=required_filled(skill, variables),
            ))

        request = {
            "status": "skill_selection_required",
            "reason": (
                "Reusable skill candidates were found. Read each candidate's"
                " skillMarkdown before deciding whether to use one."
            ),
            "phaseId": str((phase or {}).get("id") or worker_contract.get("phase_id") or ""),
            "targetUrl": target_url,
            "candidates": payload_candidates,
            "next_instruction": (
                "Read the candidate skillMarkdown. If a skill is suitable, call"
                " spawn_browser_agent again with worker_contract.skill_id set to"
                " the chosen skill_id and worker_contract.skill_variables filled"
                " for the target row/page. If none is suitable, call"
                " spawn_browser_agent again with worker_contract.skill_selection="
                "{\"use_skill\": false, \"reason\": \"...\","
                " \"considered_skill_ids\": [...]}. For a batch of detail URLs,"
                " do not force a single-detail skill over the whole batch;"
                " split/replan per row or explicitly decline."
            ),
        }
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write(
                    "skill.selection.required",
                    {
                        "phaseId": request["phaseId"],
                        "candidateIds": [c["skill_id"] for c in payload_candidates],
                    },
                )
            except Exception:  # pragma: no cover
                pass
        return request
    except Exception as exc:
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.selection.error", {"error": str(exc)})
            except Exception:  # pragma: no cover
                pass
        return None


def selected_skill_context(
    registry: Any,
    worker_contract: Dict[str, Any],
    *,
    max_markdown_chars: int = 6000,
) -> str:
    """Context block for BrowserAgent fallback slow path.

    This is task context, not system prompt. If the fast path falls back, the
    worker can still follow the selected skill's recipe.
    """
    if registry is None or not isinstance(worker_contract, dict):
        return ""
    skill_id = _selection_skill_id(worker_contract)
    if not skill_id or skill_selection_declined(worker_contract):
        return ""
    try:
        skill = registry.get(skill_id)
    except Exception:
        skill = None
    if skill is None:
        return ""

    variables = worker_contract.get("skill_variables")
    variables = variables if isinstance(variables, dict) else {}
    block = {
        "skill_id": skill.skill_id,
        "sourcePath": str(skill.directory / "SKILL.md") if skill.directory else "",
        "frontmatter": {
            "domain": skill.domain,
            "task_type": skill.task_type,
            "stage_hint": skill.stage_hint,
            "fields": sorted(skill.fields),
            "description": skill.description,
            "allow_auto_captcha": bool(skill.frontmatter.get("allow_auto_captcha", False)),
        },
        "skill_variables": variables,
        "workflowVariables": skill.variable_template,
        "fallback_guidance": (
            "Use this selected skill recipe only if the skill fast path did not"
            " already complete. Do not invent variables; derive missing values"
            " from worker input, page evidence, or record_extraction artifacts."
        ),
    }
    markdown = str(skill.skill_md or "")
    if len(markdown) > max_markdown_chars:
        markdown = markdown[:max_markdown_chars] + "\n...[truncated]"
    return (
        "<selected_skill>\n"
        f"{json.dumps(block, ensure_ascii=False, indent=2, default=str)}\n\n"
        "<skill_markdown>\n"
        f"{markdown}\n"
        "</skill_markdown>\n"
        "</selected_skill>"
    )


def _skill_candidate_payload(
    skill: Any,
    *,
    score: int,
    reasons: List[str],
    variables: Dict[str, Any],
    required_filled: bool,
) -> Dict[str, Any]:
    steps = []
    for step in list(getattr(skill, "steps", []) or [])[:8]:
        if not isinstance(step, dict):
            continue
        steps.append({
            "action": step.get("action"),
            "purpose": step.get("purpose"),
            "extract": step.get("extract"),
        })
    markdown = str(getattr(skill, "skill_md", "") or "")
    return {
        "skill_id": skill.skill_id,
        "score": score,
        "reasons": reasons,
        "sourcePath": str(skill.directory / "SKILL.md") if skill.directory else "",
        "frontmatter": {
            "domain": skill.domain,
            "task_type": skill.task_type,
            "stage_hint": skill.stage_hint,
            "fields": sorted(skill.fields),
            "description": skill.description,
            "allow_auto_captcha": bool(skill.frontmatter.get("allow_auto_captcha", False)),
        },
        "workflowVariables": skill.variable_template,
        "derivedVariables": variables,
        "requiredVariablesFilled": required_filled,
        "workflowStepSummary": steps,
        "skillMarkdown": markdown[:12000] + ("\n...[truncated]" if len(markdown) > 12000 else ""),
    }


def _host_of_url(url: str) -> str:
    text = str(url or "")
    if "://" not in text:
        return ""
    host = text.split("://", 1)[1].split("/", 1)[0]
    return host.lower().lstrip("www.")
