"""Durable human decisions, separate from file grants and business completion."""
from __future__ import annotations

import uuid

from harness.planning.context import retain_operator_inputs
from harness.skill_builder.session import _result


def record_answer(owner, text, *, source="skill_terminal"):
    owner.state = owner.store.load()
    pending = owner.state.get("pendingInput")
    if not pending:
        raise ValueError("No pending business question")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Empty input supplies no decision")
    item = {"inputId": pending["inputId"], "source": source,
        "question": pending["question"], "text": text,
        "blockedSteps": pending.get("blockedSteps", [])}
    owner.state.setdefault("operatorInputs", []).append(item)
    owner.state.pop("pendingInput", None)
    owner.state["status"] = "active"
    ref = owner.store.record(owner.state, "skill.user_input", item)
    # The independent reviewer validates against the same attributed inputs.
    retain_operator_inputs(owner.logger, owner.state["operatorInputs"])
    owner.finished = None
    return {"status": "answered", "receiptRef": ref, **item,
        "next": "Interpret this answer against the original goal; update affected bindings/plan, reconcile prior effects, then continue remaining work. It grants no file or code permissions."}


def pause_for_input(owner):
    owner.state = owner.store.load()
    owner.state["status"] = "awaiting_user"
    owner.store.save(owner.state)
    pending = owner.state["pendingInput"]
    owner.finished = {"status": "needs_user", "taskId": owner.logger.task_id,
        "summary": pending["question"], "pendingInput": pending,
        "execution": owner.projection(), "next": [f"/resume {owner.logger.task_id} <answer>"]}
    owner.finished["packageRef"] = owner.export_package()
    return _result(owner.finished, terminate=True)


async def request_input(owner, question, *, reason="", evidence_refs=None, blocked_steps=None):
    from harness.runtime.hitl_input import read_terminal_input, GuidanceWake
    if not question.strip():
        raise ValueError("Ask a nonempty business question")
    owner.state = owner.store.load()
    pending = owner.state.get("pendingInput")
    if pending and pending["question"] != question:
        raise ValueError("Resolve the persisted question before replacing it")
    if not pending:
        refs, steps = list(evidence_refs or []), list(blocked_steps or [])
        if refs:
            owner._check_evidence(refs)
        if set(steps) - owner.state.get("steps", {}).keys():
            raise ValueError("blocked_steps contains an unknown step ID")
        pending = {"inputId": uuid.uuid4().hex, "question": question, "reason": reason,
            "evidenceRefs": refs, "blockedSteps": steps, "revision": owner.state.get("revision")}
        owner.state.update(pendingInput=pending, status="awaiting_user")
        ref = owner.store.record(owner.state, "skill.user_input.requested", pending)
        pending["receiptRef"] = ref
        owner.store.save(owner.state)
    # Persist before awaiting: cancellation or EOF cannot lose the question.
    answer = await read_terminal_input(
        f"[{owner.mode.title()} @{owner.logger.task_id} · 需要你的决定] {pending['question']}\n{pending.get('reason', '')}",
        question_id=pending["inputId"], guidance_interruptible=True)
    if isinstance(answer, GuidanceWake):
        return _result({"status": "user_input_pending", "pendingInput": pending,
            "next": "Interpret the new guidance. Re-ask this question or withdraw it with withdraw_skill_input if its business decision is no longer needed. No answer or permission was supplied."})
    if answer is None or not answer.strip():
        return pause_for_input(owner)
    return _result(record_answer(owner, answer))


def withdraw_input(owner, input_id, reason):
    owner.state = owner.store.load()
    pending = owner.state.get("pendingInput")
    if not pending or pending["inputId"] != input_id or not reason.strip():
        raise ValueError("Withdraw the exact pending business question with a reason")
    owner.state.pop("pendingInput")
    owner.state["status"] = "active"
    ref = owner.store.record(owner.state, "skill.user_input.withdrawn", {"question": pending, "reason": reason})
    return {"status": "withdrawn", "receiptRef": ref, "next": "No answer or permission was granted."}
