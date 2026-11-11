"""Resolve a saved record once, then run its scoped immutable source snapshot."""
from __future__ import annotations

import copy
from typing import Any

from harness.workflow.script_runtime import source_hash
from harness.tools.argument_pipeline import validate_schema


def snapshot_from_response(response: dict, workshop_id: str, inline_schema: dict) -> tuple[dict, dict]:
    if not isinstance(response, dict) or response.get("error"):
        raise ValueError("Workflow.list lookup failed or returned an invalid envelope")
    data = response.get("data", response)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Workflow.list did not return a complete record lookup")
    items = data["items"]
    if len(items) != 1 or not isinstance(items[0], dict) or items[0].get("workshopId") != workshop_id:
        raise ValueError("Workflow.list exact lookup must return exactly the requested Workshop identity")
    item = items[0]
    revision, manifest = item.get("revision"), item.get("manifest")
    if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
            or not isinstance(manifest, dict) or manifest.get("kind") != "webcross-workshop"):
        raise ValueError("Saved Workshop revision or complete manifest is unavailable")
    document = copy.deepcopy({key: value for key, value in manifest.items()
                              if key not in {"kind", "availability"}})
    issues = validate_schema({"workflow": document}, inline_schema)
    if issues:
        raise ValueError("Saved Workshop source is incompatible with the inline execution contract: " + str(issues))
    return document, {"workshopId": workshop_id, "revision": revision,
                      "manifestHash": source_hash(manifest), "sourceHash": source_hash(document),
                      "execution": "scoped_inline_snapshot", "enabled": item.get("enabled")}


async def prepare_saved_execution(agent: Any, params: dict) -> tuple[dict, dict]:
    """Host lookup keeps full source out of the model loop and saves it before execution."""
    from harness.workflow.workflow_schema_source import workflow_contract
    from harness.tools.browser_tools.progress_obs import _check_worker_contract
    from harness.observation.browser_call import build_browser_call_runner
    from harness.workflow.workflow_definitions import save_workflow_definition
    from harness.utils import storage_for_logger
    contract = workflow_contract()
    if "source" not in contract.execute_forms or "saved" not in contract.execute_forms:
        raise ValueError("The current catalog does not support saved Workflow snapshot execution")
    if "Workflow.list" not in agent.capability_methods:
        raise ValueError("Workflow.list is unavailable; saved source cannot be resolved safely")
    denial = _check_worker_contract(agent, "Workflow.list")
    if denial:
        raise PermissionError(denial.get("error", "Workflow.list is not permitted"))
    runner = getattr(agent, "browser_call_runner", None) or build_browser_call_runner(
        browser=agent.browser, logger=agent.logger, capability_methods=agent.capability_methods)
    response = await runner.call("Workflow.list", {"workshopId": params["workshopId"]})
    storage, task_id = storage_for_logger(agent.logger)
    lookup = storage.save_resource(task_id=task_id, run_id=str(getattr(agent.logger, "run_id", "") or ""),
        resource_type="workshop_lookup", logical_path=f"workshop_lookups/{params['workshopId']}.json",
        content=response, media_type="application/json")
    lookup_ref = lookup.get("saved_path")
    agent.logger.write("workflow.workshop.lookup", {"workshopId": params["workshopId"], "receiptRef": lookup_ref})
    try:
        document, provenance = snapshot_from_response(response, params["workshopId"], contract.execute_forms["source"])
    except ValueError as exc:
        exc.lookup_receipt = lookup_ref
        raise
    business_input = copy.deepcopy(params.get("input", {}))
    definition = save_workflow_definition(agent.logger, {"workflow": document, "input": business_input})
    evidence = {**provenance, "lookupReceiptRef": lookup_ref, "workflowDefinition": definition}
    agent.logger.write("workflow.workshop.snapshot", evidence)
    return {"workflow": document, "input": business_input,
            "binding": copy.deepcopy(params.get("binding", {}))}, evidence
