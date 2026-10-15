"""Shared source-bound validation ledger for Workflow and standalone Skill authoring.

Adapters supply their workspace, evidence permissions and state store. No model
or business verdict is introduced here; authors assess raw execution evidence.
"""
from __future__ import annotations
import json
from harness.skill_builder.catalog import inspect_skill
from harness.skill_runtime.plan import digest, read_source
from harness.storage.sqlite_store import parse_resource_uri
from harness.skill_builder.runtime import owned_trials


def validate_test_page(data, fleet_id, excluded_pages=()):
    page = data.get("pageId") if isinstance(data, dict) else None
    if not isinstance(page, str) or not page or data.get("fleetId") != fleet_id or page in excluded_pages:
        raise ValueError("Page creation returned an inconsistent page/Fleet identity")
    return page


def stages(root):
    path = root / "orchestration.json"
    if not path.exists():
        return {"stages": [], "planIssue": "No orchestration authored yet"}
    try:
        plan = read_source(root, "orchestration.json")
        rows = []
        for step in plan["steps"]:
            row = {"id": step["id"], "kind": step["kind"], "contractHash": digest(step)}
            if step["kind"] == "workflow":
                row.update(path=step["path"], sourceHash=digest(read_source(root, step["path"])))
            rows.append(row)
        return {"stages": rows}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"stages": [], "planIssue": str(exc)}


def evidence(owner, *, trials=None):
    state = owner.store.load()
    root = owner._foreground.work_dir if owner._foreground else owner.source_root()
    facts = stages(root)
    assessments = state.get("validationAssessments", [])
    for row in facts["stages"]:
        row["assessment"] = next((a for a in reversed(assessments)
            if a["stageId"] == row["id"] and a["contractHash"] == row["contractHash"]
            and a.get("sourceHash") == row.get("sourceHash")), None)
    return {**facts, "trials": state.get("validationTrials", {}) if trials is None else trials,
        "testPages": state.get("workflowTestPages", {}),
        "interpretation": "Version identity is mechanical; assessments are the author's semantic judgments. "
            "Independent-page checkpoints never complete business-page work. Browser effects do not verify unexecuted code."}


def read_receipt(owner, *, reference, offset=0, max_chars=16000):
    task_id, resource_id = parse_resource_uri(reference)
    builder = owner.foreground()
    if task_id not in {owner.logger.task_id, builder.logger.task_id, *owned_trials(builder),
                       *getattr(owner, "allowed_evidence_tasks", set())}:
        raise PermissionError(f"Receipt is outside this task and its owned trials: {reference}")
    row = owner.logger.storage.read_resource(current_task_id=task_id, resource_uri=reference)
    if not row or not row.get("content_available", True):
        raise ValueError(f"Evidence is unavailable: {reference}")
    content = row.get("content_json") or row.get("content_text")
    if content is None:
        raise ValueError(f"Use the image observation's indexed receipt for evidence: {reference}")
    imported = reference
    if task_id != owner.logger.task_id:
        imported = owner.store.write(f"workflow/validation-evidence/{resource_id}.json",
            {"sourceReference": reference, "sha256": row.get("sha256"), "content": content}, "workflow_validation_evidence")
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    offset = max(0, offset); limit = max(1, min(max_chars, 30000))
    return {"reference": reference, "ownerEvidenceRef": imported, "content": text[offset:offset+limit],
        "nextOffset": offset+limit, "hasMore": offset+limit < len(text)}


def record_assessment(owner, *, stage_id, verdict, reason, evidence_refs):
    if verdict not in {"verified", "needs_work", "blocked", "not_applicable"} or not reason.strip() or not evidence_refs:
        raise ValueError("Assessment needs a verdict, explanation and owned evidence")
    row = next((s for s in evidence(owner)["stages"] if s["id"] == stage_id), None)
    if row is None:
        raise ValueError(f"Stage is not in the current source: {stage_id}")
    refs = [read_receipt(owner, reference=ref, max_chars=1)["ownerEvidenceRef"] for ref in evidence_refs]
    state = owner.store.load()
    assessment = {k: v for k, v in row.items() if k not in {"assessment", "id"}}
    assessment.update(stageId=stage_id, verdict=verdict, reason=reason, evidenceRefs=refs,
                      assessedPackageHash=inspect_skill(owner.foreground().work_dir)["hash"])
    state.setdefault("validationAssessments", []).append(assessment)
    ref = owner.store.record(state, "workflow.validation.assessed", assessment)
    return {"receiptRef": ref, "validation": evidence(owner)}
