"""Live author guidance and revision facts, not business-strategy verdicts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.capabilities.schema_cache import global_schemas_dir
from harness.capabilities.schema_loader import CapabilityBundle, build_capability_digest
from harness.runtime.model_support import _webcross_behavioral_guide
from harness.tools.tool_policy import disabled_reason_for_method
from harness.workflow.script_runtime import DEFAULT_ACTIONS, HOST_OWNED_PAGE_ACTIONS


def _package_method(method: str) -> bool:
    return (method not in HOST_OWNED_PAGE_ACTIONS and not disabled_reason_for_method(method)
            and (method.startswith(("Page.", "DOM.", "Input."))
                 or method in DEFAULT_ACTIONS or method == "Runtime.evaluate"))


def snapshot(platform: Any) -> dict[str, Any]:
    """Only use schemas tied to this connection's live catalog revision."""
    revision = platform.catalog.get("catalogRevision")
    methods = {method for method in platform.capability_methods if _package_method(method)}
    bundle = CapabilityBundle(capabilities=[row for row in platform.capabilities
        if row.get("method") in methods], capability_methods=methods)
    schemas_dir = global_schemas_dir(platform.runtime.harness.worktree_dir)
    for method in sorted(methods):
        try:
            schema = json.loads((schemas_dir / f"{method}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if revision and isinstance(schema, dict) and schema.get("catalogRevision") == revision:
            bundle.method_schemas[method] = schema
    behavioral = _webcross_behavioral_guide(platform.agent_guide,
        workflow_artifact_reader="platform.readText(result.artifact)")
    behavioral = behavioral.replace("through `browser_call`", "through `observe_page`")
    behavioral = behavioral.replace("read_harness_guide id=browser.select-recovery",
        "describe_action for DOM.inspectSelect and Input.select")
    return {**platform.catalog, "methods": sorted(methods),
            "guideStatus": "available" if behavioral else "unavailable_or_unrecognized",
            "guideContentHash": hashlib.sha256(platform.agent_guide.encode()).hexdigest()
                                if platform.agent_guide else None,
            "schemasMatched": len(bundle.method_schemas), "capabilityCount": len(methods),
            "schemaBootstrap": platform.schema_bootstrap,
            "behavioralGuide": behavioral, "capabilityDigest": build_capability_digest(bundle)}


def identity(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in ("catalogRevision", "guideRevision", "guideContentHash")}


def revision_changes(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    if not before:
        return {"status": "no_previous_snapshot", "changed": []}
    keys = ("catalogRevision", "guideRevision", "guideContentHash")
    return {"status": "compared", "changed": [key for key in keys
            if before.get(key) is not None and after.get(key) is not None
            and before[key] != after[key]],
            "unknown": [key for key in keys if before.get(key) is None or after.get(key) is None],
            "before": identity(before), "after": identity(after),
            "removedMethods": sorted(set(before.get("methods", [])) - set(after.get("methods", [])))}


def system_block(value: dict[str, Any]) -> str:
    revisions = json.dumps(identity(value), ensure_ascii=False)
    return ("\n\n<webcross_author_guidance>\nLive connection identity: " + revisions
        + "\nThese are the same WebCross behavioral source and capability digest used by Browser. "
          "Connection and event delivery belong to the Harness. Use observe_page for live page reads, "
          "describe_action for current contracts, and platform.actions inside the package. "
          "The capability summary is lossy and does not grant permissions; the scope wrapper remains authoritative. "
          "Only schemas matching the live catalog contribute parameter hints. "
          "A missing guide or schema is unknown, not permission to substitute remembered contracts.\n"
        + AUTHOR_REPAIR_GUIDE + "\n"
        + "Guide status: " + str(value.get("guideStatus")) + "\n"
        + str(value.get("behavioralGuide") or "") + "\n"
        + str(value.get("capabilityDigest") or "") + "\n</webcross_author_guidance>")


def strategy_facts(inventory: dict[str, Any] | None, *, current: dict[str, Any],
                   source_hashes: dict[str, str], limit: int = 8) -> dict[str, Any]:
    """Compare explicit strategy evidence only; don't infer actions from source."""
    rows = (inventory or {}).get("controls", [])
    recorded, changed, missing = 0, [], 0
    for row in rows:
        strategy = (row.get("interaction") or {}).get("strategy")
        if not strategy:
            missing += 1
            continue
        recorded += 1
        facts = {}
        for key in ("catalogRevision", "guideRevision"):
            if strategy.get(key) and current.get(key) and strategy[key] != current[key]:
                facts[key] = {"recorded": strategy[key], "current": current[key]}
        unavailable = sorted(set(strategy.get("methods", [])) - set(current.get("methods", [])))
        if unavailable and current.get("catalogRevision"):
            facts["methodsNotAdvertised"] = unavailable
        source_changes = [name for name, digest in strategy.get("sourceHashes", {}).items()
                          if source_hashes.get(name) != digest]
        if source_changes:
            facts["changedSourceFiles"] = source_changes
        if facts:
            changed.append({"controlId": row["id"], "facts": facts})
    return {"path": "coverage.json", "recordedStrategies": recorded, "withoutStrategy": missing,
            "differenceCount": len(changed), "differences": changed[:limit],
            "truncated": len(changed) > limit,
            "interpretation": "Session-entry facts, not automatic invalidation or proof of applicability. "
                "Read relevant interaction.strategy and evidence; decide whether a changed contract, "
                "source or current page requires a small experiment. Absence of records is unknown. "
                "Do not rerun every alternative or repeat Browser delegation by default."}


AUTHOR_REPAIR_GUIDE = """For Workflow repairs, preserve the public Action error fields and the exact
execution/call evidence; a caught Action failure is not erased by a successful
outer Workflow. Distinguish a confirmed submission, no success receipt, and a
verified absent effect. A missing reply does not prove no effect. Start success
confirmation waits only from an acknowledged operation; on a failed observation,
return its error instead of blindly polling or repeating the trigger. Reconcile
uncertain running work before dependent actions or another launch.
Use failed fields and observed candidates as facts for the semantic decision;
do not blindly repeat the whole input or declare a field permanently absent.
For automatic reapplication after dependent writes, retain evidence of values
applied and later lost; otherwise report candidates for a semantic recovery decision.
For reviewed short page reads, use the live native async result when supported;
long work needs an explicit deadline, cancellation/lifetime and result contract.
Do not use a detached script or a different world to bypass platform timeouts.
Keep original draft identity and publication baseline. Continue the selected
draft; resolve real branch changes before publishing. Verify affected success,
caught failure, uncaught failure and unknown-effect paths locally, and separately
measure net time, all four token classes, model turns and platform I/O. Fewer
calls or smaller messages alone do not prove end-to-end improvement.
"""
