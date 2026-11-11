"""Bounded, read-only evidence collection for a published Workflow run.

This delegate uses the shared agent loop, but never receives the Browser action
tools. Standalone Browser and author exploration keep their existing toolsets.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses

from harness.workflow_mode.evidence import evidence_locations, evidence_reference, pointer
from harness.workflow_mode.platform import OBSERVE_METHODS
from harness.workflow_mode.tools import A, I, O, S, compact, receipt_tool, result, schema, tool

READ_METHODS = OBSERVE_METHODS - {"Page.list", "Fleet.list"}


def observer_tools(session):
    async def describe(args):
        if args["method"] not in READ_METHODS:
            raise PermissionError("Only observation schemas are available to this delegate")
        return await session._describe(args)

    async def finish(args):
        args = copy.deepcopy(args)
        # Validate ownership and addresses, not whether a fact proves the goal.
        for fact in args["facts"]:
            if not fact["evidenceRefs"]:
                raise ValueError("A fact needs evidence; put unobserved claims in unresolved")
            canonical_refs = []
            for value in fact["evidenceRefs"]:
                ref, path = evidence_reference(value, known_refs=session.known_refs())
                if ref not in session.known_refs():
                    raise ValueError("Evidence must belong to this observation task")
                pointer(session.store.read_ref(ref), path)
                canonical_refs.append({"ref": ref, "pointer": path})
            fact["evidenceRefs"] = canonical_refs
        session._check_stop()
        session.finished = {"status": "done", "taskId": session.logger.task_id, **args}
        session.state.update(status="done", observation=session.finished)
        session.save()
        return result({"status": "done", "factCount": len(args["facts"]),
                       "unresolved": args["unresolved"]}, terminate=True)

    reference = {"anyOf": [S, schema({"ref": S, "pointer": S}, ("ref",))]}
    return [
        tool("observe_page", "Read a specific missing fact on the fixed bound page. No actions.",
             schema({"method": {"type": "string", "enum": sorted(READ_METHODS)},
                     "params": O, "purpose": S}, ("method", "purpose")), session._observe_page),
        tool("describe_action", "Read the schema of an allowed observation method.",
             schema({"method": S}, ("method",)), describe),
        receipt_tool(session),
        tool("read_artifact", "Read an artifact returned by this delegate's page observation.",
             schema({"path": S, "offset": I, "max_chars": I}, ("path",)), session._read_artifact),
        tool("finish_observation", "Return observed facts with coverage and exact evidence. "
             "Report unavailable evidence in unresolved; do not complete business work.",
             schema({"summary": S, "facts": A(schema({
                 "fact": S, "coverage": S, "evidenceRefs": A(reference)},
                 ("fact", "coverage", "evidenceRefs"))), "unresolved": A()},
                    ("summary", "facts", "unresolved")), finish),
    ]


async def delegate_observation(parent, args):
    from harness.runtime.resume_state import write_task_manifest
    from harness.workflow_mode.session import WorkflowSession

    parent._check_stop()
    binding = parent.state.get("binding") or {}
    if not binding.get("pageId") or not binding.get("fleetId"):
        raise ValueError("Bind the page before delegating observation")
    if parent.host is None:
        raise ValueError("Browser delegation is unavailable in this embedding")
    refs = args.get("evidence_refs") or []
    if not refs or not args["goal"].strip() or not args["reason"].strip():
        raise ValueError("Supply the missing fact, decision impact and existing evidence_refs")
    context = []
    for value in refs:
        ref, path = evidence_reference(value, known_refs=parent.known_refs())
        if ref not in parent.known_refs():
            raise ValueError("Delegation evidence must belong to this task")
        context.append({"source": {"ref": ref, "pointer": path},
                        "value": pointer(parent.store.read_ref(ref), path)})

    runtime = dataclasses.replace(parent.runtime, harness=copy.copy(parent.runtime.harness))
    logger = parent.host._create_task_logger(runtime)
    logger.run_id = parent.host._new_run_id(resumed=False)
    parent.host._open_task_storage(logger, runtime)
    runtime.harness.runs_dir = str(logger.task_dir)
    child = WorkflowSession(runtime, logger, profile="observer", host=parent.host,
                            quiet=parent.quiet, batch_id=parent.logger.task_id)
    child._observation_parent = parent
    question = f"Missing fact and stopping condition: {args['goal']}\nDecision impact: {args['reason']}"
    write_task_manifest(logger, original_user_task=question, task_id=logger.task_id,
        agent_id=runtime.agent_id, startup_args={"agent_mode": "workflow",
        "workflowProfile": "observer", "parentWorkflowTask": parent.logger.task_id})
    child.initialize(question)
    child.state.update(binding=dict(binding), authorizedFleets=[binding["fleetId"]])
    seed = child.store.receipt(child.state, "workflow.observation.context", {"evidence": context})
    child.state["goal"] += f"\nExisting evidence: {seed} (pointer /evidence)."
    child.save()
    outcome = {"status": "failed", "summary": "Observation did not finish"}
    task = None
    try:
        task = asyncio.create_task(child.run(child.state["goal"]))
        while not task.done():
            parent._check_stop()
            await asyncio.wait({task}, timeout=0.25)
        outcome = await task
    except asyncio.CancelledError:
        outcome = {"status": "cancelled", "summary": "Parent task cancelled observation"}
        raise
    except Exception as exc:
        outcome = {"status": "failed", "summary": str(exc), "errorType": type(exc).__name__}
        # Preserve evidence below, then honour a stop instead of starting another turn.
        if parent._stop_flag():
            outcome["status"] = "stopped"
            parent._check_stop()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            try:
                await child.close()
            except Exception as exc:
                outcome = {**outcome, "status": "failed",
                           "cleanupError": f"{type(exc).__name__}: {exc}"}
            observations = [{"sourceRef": item["ref"], "event": item["event"],
                             "value": child.store.read_ref(item["ref"])}
                            for item in child.state.get("receipts", [])]
            child.store.write("artifacts/observation-result.json", outcome, "workflow_observation_result")
            logger.write_usage_summary()
        finally:
            parent.host._close_task_storage(
                logger, status="completed" if outcome.get("status") == "done" else "interrupted")
        record = {"browserTaskId": logger.task_id, "mode": "observation",
                  "goal": args["goal"], "reason": args["reason"], "binding": dict(binding),
                  "status": outcome.get("status"), "result": outcome, "observations": observations,
                  "cost": outcome.get('cost') or child._cost_snapshot()}
        # Copy raw evidence into the parent ownership boundary; child-only refs
        # remain traceable via sourceRef instead of becoming dangling citations.
        # Keep the existing event/parent linkage so evaluation still includes
        # delegate turns and tokens; mode distinguishes observation from author work.
        ref = parent.store.receipt(parent.reload(), "workflow.browser.delegation", record)
        parent.state.setdefault("delegations", []).append({
            "browserTaskId": logger.task_id, "mode": "observation",
            "status": outcome.get("status"), "receiptRef": ref})
        parent.save()
    # Give the parent directly usable citations, not child-owned receipt IDs.
    view = copy.deepcopy(outcome)
    indexes = {item["sourceRef"]: index for index, item in enumerate(observations)}
    for fact in view.get("facts", []):
        fact["evidenceRefs"] = [
            {"ref": ref, "pointer": f"/observations/{indexes[source]}/value{path}"}
            for source, path in (evidence_reference(value) for value in fact["evidenceRefs"])]
    return result({"browserTaskId": logger.task_id, "mode": "observation",
                   "receiptRef": ref, "result": compact(view, 6000),
                   "evidenceLocations": evidence_locations(ref, record, {
                       "result": "/result", "facts": "/result/facts",
                       "unresolved": "/result/unresolved", "observations": "/observations"}),
                   "next": "Audit these facts. Re-observe only a remaining gap, conflict or changed state. "
                           "Package defects go to /workflow-edit; no business fallback."},
                  error=outcome.get("status") != "done")
