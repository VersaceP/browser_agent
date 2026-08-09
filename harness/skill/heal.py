"""harness.skill.heal — propose a skill workflow revision, canary-validate, promote.

When the BrowserAgent slow path (or a coding agent) figures out a corrected
workflow for a rotted skill, this:
  1. writes the candidate as workflow.v<next>.json (current stays live),
  2. runs it once as a canary (run_skill_workflow + check_success_contract),
  3. on pass: archives the live workflow.json → workflow.v<cur>.json, promotes the
     candidate to workflow.json, clears the skill's failure state (health.reset),
     and appends to .heal_history.json;
     on fail: keeps the live version, leaves the candidate for inspection.

Auto-generating the candidate is out of scope — the corrected steps come from the
agent fallback. This is the durable versioning + canary infrastructure.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.skill.registry import Skill
from harness.skill.workflow import (
    check_success_contract,
    run_skill_workflow,
)
from harness.workflow_auth_fence import (
    workflow_auth_fence_after,
    workflow_auth_fence_before,
)
from harness.workflow_runtime import (
    workflow_execution_disabled_result,
    workflow_execution_enabled,
)


def _is_structurally_valid(workflow: Dict[str, Any]) -> bool:
    steps = workflow.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    pending = list(steps)
    while pending:
        s = pending.pop()
        if not isinstance(s, dict):
            return False
        if (s.get("type") or "action") == "action" and not s.get("action"):
            return False
        if str(s.get("action") or "") == "Runtime.evaluate":
            return False
        for key in ("then", "else", "body"):
            nested = s.get(key)
            if isinstance(nested, list):
                pending.extend(nested)
    return True


def _next_version(directory: Path) -> int:
    existing = [int(p.stem.split("v")[-1]) for p in directory.glob("workflow.v*.json")
                if p.stem.split("v")[-1].isdigit()]
    return (max(existing) + 1) if existing else 2


def write_candidate(skill: Skill, new_workflow: Dict[str, Any]) -> Path:
    """Write the candidate as workflow.v<next>.json; returns its path."""
    if skill.directory is None:
        raise ValueError("skill has no directory")
    if not _is_structurally_valid(new_workflow):
        raise ValueError("candidate workflow is invalid or contains forbidden Runtime.evaluate")
    version = _next_version(skill.directory)
    path = skill.directory / f"workflow.v{version}.json"
    path.write_text(json.dumps(new_workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _candidate_skill(skill: Skill, new_workflow: Dict[str, Any]) -> Skill:
    return Skill(skill_id=skill.skill_id, directory=skill.directory,
                 frontmatter=skill.frontmatter, workflow=new_workflow, fallback=skill.fallback)


def _workflow_runtime_context(*, agent: Any, workflow_runtime: Any) -> Any:
    """Prefer the explicit authorization context; agent is compatibility fallback."""

    return workflow_runtime if workflow_runtime is not None else agent


async def canary_validate(
    skill: Skill,
    new_workflow: Dict[str, Any],
    *,
    browser: Any,
    canary_variables: Dict[str, Any],
    page_id: Optional[str] = None,
    fleet_id: Optional[str] = None,
    agent: Any = None,
    workflow_runtime: Any = None,
) -> Dict[str, Any]:
    """Run the candidate once and check its success_contract. No promotion."""
    if not _is_structurally_valid(new_workflow):
        return {"ok": False, "reason": "structurally_invalid"}
    runtime_context = _workflow_runtime_context(
        agent=agent,
        workflow_runtime=workflow_runtime,
    )
    if not workflow_execution_enabled(runtime_context):
        disabled = workflow_execution_disabled_result(
            source="autoheal_canary",
        )
        return {
            **disabled,
            "ok": False,
            "reason": "workflow_runtime_disabled",
        }
    if not page_id:
        if not fleet_id:
            return {
                "ok": False,
                "reason": "fleet_assignment_required",
                "tool_was_executed": False,
            }
        pg = await browser.call("Page.create", {"fleetId": fleet_id, "url": "about:blank"})
        page_id = ((pg or {}).get("data") or {}).get("pageId") or ""
    candidate = _candidate_skill(skill, new_workflow)
    fence = None
    if agent is not None:
        fence = await workflow_auth_fence_before(
            agent,
            fleet_id=str(fleet_id or ""),
            run_id=f"heal-canary-{skill.skill_id}",
            source="autoheal_canary",
        )
        if not fence.get("allowed"):
            return {
                "ok": False,
                "reason": "workflow_auth_fenced",
                "authFence": fence,
            }
    run_result = await run_skill_workflow(
        browser, candidate, run_id=f"canary-{skill.skill_id}-{uuid.uuid4().hex[:6]}",
        page_id=page_id, fleet_id=fleet_id, variables=canary_variables,
        workflow_runtime=runtime_context,
    )
    if agent is not None and isinstance(fence, dict):
        postflight = await workflow_auth_fence_after(
            agent,
            fleet_id=str(fleet_id or ""),
            run_id=str(run_result.get("runId") or "heal-canary"),
            started_generation=int(fence.get("generation") or 0),
            source="autoheal_canary",
        )
        if not postflight.get("valid"):
            return {
                "ok": False,
                "reason": "workflow_auth_fenced",
                "authFence": postflight,
            }
    verdict = check_success_contract(candidate, run_result)
    ok = bool(run_result.get("succeeded") and verdict["ok"])
    return {"ok": ok, "run_result": run_result, "verdict": verdict}


def promote(skill: Skill, candidate_path: Path) -> Dict[str, Any]:
    """Archive the live workflow.json and replace it with the candidate."""
    if skill.directory is None:
        raise ValueError("skill has no directory")
    live = skill.directory / "workflow.json"
    archived = None
    if live.is_file():
        version = _next_version(skill.directory)  # archive under the next free slot
        archived = skill.directory / f"workflow.v{version}.json"
        # avoid clobbering the candidate's own filename
        if archived == candidate_path:
            archived = skill.directory / f"workflow.v{version + 1}.json"
        archived.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
    live.write_text(candidate_path.read_text(encoding="utf-8"), encoding="utf-8")
    record = {
        "skill": skill.skill_id,
        "promoted_from": str(candidate_path.name),
        "archived_live_to": str(archived.name) if archived else None,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _append_history(skill.directory, record)
    return record


def _append_history(directory: Path, record: Dict[str, Any]) -> None:
    path = directory / ".heal_history.json"
    history: List[Dict[str, Any]] = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = loaded
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(record)
    try:
        path.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


async def self_heal(
    skill: Skill,
    new_workflow: Dict[str, Any],
    *,
    browser: Any,
    canary_variables: Dict[str, Any],
    health: Any = None,
    page_id: Optional[str] = None,
    fleet_id: Optional[str] = None,
    agent: Any = None,
    workflow_runtime: Any = None,
) -> Dict[str, Any]:
    """Full loop: write candidate → canary → promote (or reject). Returns a report."""
    runtime_context = _workflow_runtime_context(
        agent=agent,
        workflow_runtime=workflow_runtime,
    )
    if not workflow_execution_enabled(runtime_context):
        disabled = workflow_execution_disabled_result(
            source="self_heal",
        )
        return {
            **disabled,
            "promoted": False,
            "reason": "workflow_runtime_disabled",
        }
    try:
        candidate_path = write_candidate(skill, new_workflow)
    except ValueError as exc:
        return {"promoted": False, "reason": str(exc)}

    canary = await canary_validate(
        skill, new_workflow, browser=browser, canary_variables=canary_variables,
        page_id=page_id, fleet_id=fleet_id, agent=agent,
        workflow_runtime=runtime_context,
    )
    if not canary["ok"]:
        return {"promoted": False, "reason": "canary_failed",
                "candidate": str(candidate_path.name), "canary": canary}

    record = promote(skill, candidate_path)
    if health is not None:
        health.reset(skill.skill_id)
    return {"promoted": True, "record": record, "candidate": str(candidate_path.name)}
