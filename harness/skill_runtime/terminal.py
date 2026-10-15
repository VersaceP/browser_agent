"""Review every model-proposed ending; business disposition remains semantic."""
from __future__ import annotations

from harness.planning.context import retain_operator_inputs
from harness.skill_builder.catalog import inspect_skill
from harness.skill_builder.session import _result
from harness.skill_runtime.decisions import pause_for_input, request_input
from harness.runtime.steering import current_steering


def validate_terminal_decision(decision, verdict):
    """Protocol/consistency only; do not infer business intent from prose."""
    if not isinstance(verdict, str) or verdict not in {"needs_work", "needs_user", "insufficient_evidence", "complete"}:
        raise ValueError("terminalDecision requires a valid final review verdict")
    if (not isinstance(decision, dict) or not isinstance(decision.get("action"), str)
            or decision["action"] not in {"continue", "ask_user", "stop"}):
        raise ValueError("terminalDecision requires action=continue/ask_user/stop")
    if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
        raise ValueError("terminalDecision requires an evidence-based reason")
    if decision.keys() - {"action", "reason", "question"}:
        raise ValueError("Unknown terminalDecision fields")
    action = decision["action"]
    if verdict == "needs_user" and action != "ask_user":
        raise ValueError("needs_user requires ask_user")
    if action == "ask_user" and (verdict != "needs_user" or
            not isinstance(decision.get("question"), str) or not decision["question"].strip()):
        raise ValueError("ask_user requires needs_user and a nonempty question")
    if verdict == "complete" and action != "stop":
        raise ValueError("complete contradicts a pending continuation/decision")
    if action != "ask_user" and decision.get("question"):
        raise ValueError("A pending question requires ask_user")
    return decision


async def finish(owner, args):
    manager = current_steering()
    proposal_revision = manager.revision if manager else None
    if manager and not manager.can_finish():
        return _result({"status": "user_input_pending", "next": "Interpret the newly received input before proposing an ending."})
    status = args["status"]
    if status not in {"done", "incomplete"}:
        raise ValueError("Use done or incomplete")
    if status == "done" and args.get("remaining_work"):
        raise ValueError("Done contradicts declared remaining work")
    owner.state = owner.store.load()
    if owner.state.get("pendingInput"):
        return pause_for_input(owner)
    retain_operator_inputs(owner.logger, owner.state.get("operatorInputs", []))
    inputs = manager.latest_inputs(owner.state["goal"]) if manager else owner.state.get("operatorInputs", [])
    context = {"reviewScope": "whole_delivery", "mode": owner.mode, "evidence": owner.program_evidence(),
        "terminalProposal": {"status": status, "remainingWork": args.get("remaining_work", []),
            "knownLimitations": args.get("known_limitations", [])},
        "executionFacts": {key: owner.state.get(key) for key in
            ("revision", "developmentSource", "steps", "browserBinding")},
        "objective": ("Assess the original business goal and actual delivery against user answers and raw effects. "
            "Separately assess program validation: exact source, contracts, input variations, actual paths and "
            "the author's independent-page assessments. Planned Browser stages are valid mixed orchestration; "
            "Browser rescue does not verify unexecuted code. Business completion may coexist with honestly "
            "disclosed program limits. Decide whether each limit prevents the requested delivery or warrants "
            "further validation; do not demand zero technical defects or every optional branch. "
            "remainingWork is unfinished business; knownLimitations is program/verification scope, not proof "
            "of completion. Do not accept omitted required work disguised as a limit. "
            "Continue recoverable required work, ask the human for missing business decisions, or stop with "
            "an evidenced outcome. No fixed budget or no-progress count decides this. "
            "Check original receipts rather than treating successful execution as business completion. "
            "Missing observations do not prove unavailable choices. Do not invent new requirements.")}
    if manager:
        from harness.runtime.workflow_steering import owned_workflows
        context["inFlightWorkflows"] = owned_workflows(manager)
        context["objective"] += " Inspect any paused/uncertain in-flight executions and their effects; do not assume they finished or vanished when the local await returned."
    result = await owner._delegate({"kind": "review", "goal": owner.state["goal"],
        "operatorInputs": inputs, "summary": args["summary"],
        "programContext": context}, "Review this ending proposal against original requirements and version-bound execution facts.")
    review = (result.get("answer") or {}).get("review", {}) if isinstance(result.get("answer"), dict) else {}
    owner.state = owner.store.load()
    if manager and not manager.can_finish(proposal_revision):
        return _result({"status": "user_input_pending", "review": result,
            "next": "The proposal predates new user guidance. Preserve this historical review and reconsider the current goal."})
    if review.get("status") != "reviewed":
        return _result({"status": "needs_recovery", "review": result,
            "next": "The independent review is unavailable or invalid; inspect its evidence before proposing an ending again."})
    try:
        decision = validate_terminal_decision(review.get("terminalDecision"), review.get("verdict"))
    except ValueError as exc:
        return _result({"status": "needs_recovery", "review": result, "reason": str(exc)}, error=True)
    ref = owner.store.record(owner.state, "skill.terminal.review", {"proposal": args, "review": review,
        "reviewReceiptRef": result.get("receiptRef"), "activeSourceHash": inspect_skill(owner.source_root())["hash"]})
    if decision["action"] == "ask_user":
        return await request_input(owner, decision["question"], reason=decision["reason"], evidence_refs=[ref])
    if decision["action"] == "continue" or (status == "done" and review.get("verdict") != "complete"):
        return _result({"status": "needs_recovery", "review": result, "receiptRef": ref})
    if status == "incomplete" and review.get("verdict") == "complete":
        return _result({"status": "needs_recovery", "review": result, "receiptRef": ref,
            "next": "Review supports completion. Reconcile the summary/remaining work and propose the matching status."})
    if manager and not manager.commit(proposal_revision):
        return _result({"status": "user_input_pending"})
    owner.finished = {"status": status, "summary": args["summary"],
        "remainingWork": args.get("remaining_work", []),
        "knownLimitations": args.get("known_limitations", []), "taskId": owner.logger.task_id,
        "skillId": owner.state["skillId"], "skillHash": owner.state["skillHash"],
        "activeSourceHash": inspect_skill(owner.source_root())["hash"],
        "programEvidence": owner.program_evidence(), "reviewReceiptRef": ref}
    owner.state["status"] = status
    owner.state["delivery"] = {key: owner.finished[key] for key in
        ("status", "summary", "remainingWork", "knownLimitations", "reviewReceiptRef")}
    owner.store.save(owner.state)
    owner.finished["packageRef"] = owner.export_package()
    return _result(owner.finished, terminate=True)
