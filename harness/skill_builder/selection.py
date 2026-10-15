"""Read the one Skill version explicitly selected for a task."""

from __future__ import annotations

from typing import Any



def selected_skill_context(runtime: Any, *, task_id: str | None = None) -> str:
    config = runtime.harness
    skill_id = str(getattr(config, "forced_skill_id", "") or "")
    content_hash = str(getattr(config, "forced_skill_hash", "") or "")
    if not skill_id or not content_hash:
        return ""
    from harness.skill_builder.resources import selected_snapshot
    catalog, snapshot = selected_snapshot(runtime, task_id=task_id)
    try:
        guide = (snapshot / "SKILL.md").read_text(encoding="utf-8")
        from harness.skill_builder.resources import workflow_entries
        import json
        workflows = workflow_entries(snapshot)
        resources = sorted(p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*")
                           if p.is_file() and p.relative_to(snapshot).parts[0] not in {"trials", "workflows"})
        execution_note = (
            "When useful and authorized, call execute_published_skill_workflow "
            "with a listed relative path, the assigned pageId/fleetId, and "
            "explicit business variables (sent as input on JS/TS deployments). Read "
            "the source package and input instructions; legacy steps require rebuilding. "
            "Interpret the receipt against the user's task; "
            "a failed call may have side effects."
            if bool(getattr(config, "workflow_execution_enabled", False))
            else "Workflow execution is disabled for this run. Use this Skill's "
                 "instructions as guidance and operate with available browser tools."
        )
        return (
            f"<selected_skill name={skill_id!r} hash={content_hash!r}>\n"
            f"{guide}\n"
            f"Formal Workflow entries: {json.dumps(workflows, ensure_ascii=False)}\n"
            f"Supporting text/parameter resources: {resources}. Development probes are not execution entries. {execution_note}\n"
            "</selected_skill>"
        )
    finally:
        catalog.close()
