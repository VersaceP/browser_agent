"""Versioned findings for Browser review; semantic decisions belong to the LLM."""
from __future__ import annotations

import copy
import hashlib
import uuid


ACTIVE = {"open", "needs_user"}
STATUSES = ACTIVE | {"resolved", "disclosure", "merged"}


def issue_state(review: dict) -> dict:
    saved = review.get("issueState")
    if isinstance(saved, dict) and saved.get("version") == 2:
        return copy.deepcopy(saved)
    # Migrate old findings once, retaining their old IDs. Later text updates do
    # not recalculate identity. No semantic deduplication during migration.
    items = {}
    for text in review.get("issues", []):
        if isinstance(text, str) and text.strip():
            key = "issue:" + hashlib.sha256(text.encode()).hexdigest()[:20]
            items[key] = {"issueId": key, "text": text, "status": "open",
                          "reason": "Migrated unresolved finding", "evidenceIds": []}
    for key, text in (review.get("pendingIssues") or {}).items():
        if isinstance(key, str) and isinstance(text, str) and text.strip():
            items.setdefault(key, {"issueId": key, "text": text, "status": "open",
                                   "reason": "Recovered interrupted finding", "evidenceIds": []})
    return {"version": 2, "revision": 0, "items": items}


def active_issues(state: dict) -> list[dict]:
    return [item for item in state["items"].values() if item["status"] in ACTIVE]


def stage_findings(state: dict, raw: dict, evidence_ids: set[str]) -> dict:
    """Keep identified findings even if the verdict needs protocol repair.

    clientId is a model-provided retry key, never the stable issue identity.
    Existing client IDs are not allowed to rewrite or reopen a prior finding.
    """
    result = copy.deepcopy(state)
    creations = {item.get("clientId") for item in result["items"].values()}
    additions = list(raw.get("newIssues", [])) if isinstance(raw.get("newIssues"), list) else []
    # A continued pre-upgrade conversation can still submit the retired format.
    # Reject that protocol in apply_issue_delta, but do not let repair erase its
    # findings. This is migration of explicit text, never semantic deduplication.
    for text in raw.get("issues", []) if isinstance(raw.get("issues"), list) else []:
        if isinstance(text, str) and text.strip():
            old_id = "issue:" + hashlib.sha256(text.encode()).hexdigest()[:20]
            if old_id not in result["items"]:
                additions.append({"clientId": "legacy-submission:" + old_id, "text": text,
                                  "reason": "Retained while repairing legacy submission format",
                                  "evidenceIds": raw.get("evidenceIds", [])})
    for item in additions:
        if not isinstance(item, dict):
            continue
        key, text = item.get("clientId"), item.get("text")
        if not isinstance(key, str) or not key.strip() or not isinstance(text, str) or not text.strip():
            continue
        if key in creations:
            continue
        issue_id = "issue:" + uuid.uuid4().hex[:20]
        citations = item.get("evidenceIds", [])
        result["items"][issue_id] = {
            "issueId": issue_id, "clientId": key, "text": text, "status": "open",
            "reason": str(item.get("reason") or "Finding retained pending valid verdict"),
            "evidenceIds": [ref for ref in citations if isinstance(ref, str) and ref in evidence_ids]
                           if isinstance(citations, list) else [],
        }
        creations.add(key)
    if result != state:
        result["revision"] += 1
    return result


def apply_issue_delta(state: dict, raw: dict, evidence_ids: set[str]) -> tuple[dict, list[str]]:
    errors = []
    if type(raw.get("issueRevision")) is not int or raw["issueRevision"] != state["revision"]:
        errors.append("issueRevision does not match the supplied revision")
    if raw.get("issues") or raw.get("issueDispositions"):
        errors.append("Use newIssues and issueUpdates; legacy issues/issueDispositions are not writable")
    additions, updates = raw.get("newIssues", []), raw.get("issueUpdates", [])
    if not isinstance(additions, list) or not isinstance(updates, list):
        return state, errors + ["newIssues and issueUpdates must be arrays"]
    seen_clients = set()
    existing_clients = {item.get("clientId"): item for item in state["items"].values()
                        if item.get("clientId")}
    for item in additions:
        if not isinstance(item, dict):
            errors.append("newIssues entries must be objects")
            continue
        key = item.get("clientId")
        if not isinstance(key, str) or not key.strip() or key in seen_clients:
            errors.append("newIssues clientId must be nonempty and unique")
        else:
            seen_clients.add(key)
            old = existing_clients.get(key)
            if old and old["text"] != item.get("text"):
                errors.append("Existing clientId has different text; update its issueId instead")
        if not isinstance(item.get("text"), str) or not item["text"].strip():
            errors.append("newIssues text must be nonempty")
        _validate_reason(item, evidence_ids, errors)
    proposed = stage_findings(state, raw, evidence_ids)
    seen_ids = set()
    for item in updates:
        if not isinstance(item, dict):
            errors.append("issueUpdates entries must be objects")
            continue
        key = item.get("issueId")
        if not isinstance(key, str) or key not in state["items"] or key in seen_ids:
            errors.append("issueUpdates must name distinct existing issue IDs")
            continue
        seen_ids.add(key)
        if not isinstance(item.get("status"), str) or item["status"] not in STATUSES:
            errors.append("issueUpdates status is invalid")
            continue
        if "text" in item and (not isinstance(item["text"], str) or not item["text"].strip()):
            errors.append("issueUpdates text must be nonempty")
        _validate_reason(item, evidence_ids, errors)
        target = item.get("mergeInto")
        if item.get("status") == "merged":
            if not isinstance(target, str) or target == key or target not in state["items"]:
                errors.append("merged issue must name another existing issue as mergeInto")
        elif target:
            errors.append("mergeInto requires merged status")
        updated = {**proposed["items"][key], **{field: item[field] for field in
                   ("text", "status", "reason", "evidenceIds") if field in item}}
        updated.pop("mergeInto", None)
        if item.get("status") == "merged":
            updated["mergeInto"] = target
        proposed["items"][key] = updated
    for item in proposed["items"].values():
        if item["status"] == "merged":
            visited = {item["issueId"]}
            current = item
            while current.get("status") == "merged":
                target = current.get("mergeInto")
                if not isinstance(target, str) or target in visited or target not in proposed["items"]:
                    errors.append("issue merge graph has a cycle or missing target")
                    break
                visited.add(target)
                current = proposed["items"][target]
    remaining = active_issues(proposed)
    if raw.get("verdict") == "complete" and remaining:
        errors.append("complete contradicts unresolved issue state")
    if any(item["status"] == "needs_user" for item in remaining) and raw.get("verdict") != "needs_user":
        errors.append("needs_user issue requires a needs_user verdict")
    if errors:
        return state, errors
    proposed["revision"] = state["revision"] + (proposed["items"] != state["items"])
    return proposed, []


def _validate_reason(item: dict, evidence_ids: set[str], errors: list[str]) -> None:
    if not isinstance(item.get("reason"), str) or not item["reason"].strip():
        errors.append("Issue changes require a nonempty reason")
    refs = item.get("evidenceIds")
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or ref not in evidence_ids for ref in refs)):
        errors.append("Issue changes must cite available evidenceIds")
