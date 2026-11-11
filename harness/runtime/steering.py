"""Task-owned user input; delivery does not authorize browser side effects."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import uuid
from datetime import datetime, timezone

from harness.planning.context import user_context
from harness.task_control import load_task_state, write_task_state

_current = contextvars.ContextVar("task_steering", default=None)

STEERING_GUIDE = """Human guidance can arrive while you work. Its original text is
attributed user input appended to the original goal. Interpret it and preserve
unaffected requirements and recorded effects. A status question calls for a short
answer followed by continued work. A correction may require re-binding/replanning,
bounded Browser work or another user decision. Guidance itself grants no file or
page permission. Delegated workers keep their assignment and report scope changes
to the owner; reviewers remain read-only. not_executed/user_input_pending means
the old decision was not dispatched; it says nothing about earlier actions.
For a paused Workflow inspect its identity, state and effects before choosing
Workflow.resume or Workflow.cancel and a new bounded invocation. Never replay an
uncertain effect. HITL answers use /answer <id>; a new guide is not an answer.
"""


def current_steering():
    return _current.get()


def dispatch_boundary(agent, revision, method):
    """Recheck after authorization/lease waits, before an RPC is sent."""
    manager = current_steering()
    if manager is None or method in {"Workflow.pause", "Workflow.resume", "Workflow.cancel",
                                    "Workflow.getStatus", "Hitl.resolvePause"}:
        return None
    role = getattr(agent, "steering_role", None)
    expected = getattr(agent, "_steering_operation_revision", revision)
    if manager.paused or manager.revision != expected or (role and role.pending):
        return {"status": "not_executed", "tool_was_executed": False,
                "reason": "user_input_pending", "method": method}
    return None


class SteeringRole:
    def __init__(self, owner, name, *, primary=False):
        self.owner, self.name = owner, name
        saved = owner.saved_roles.get(name, {})
        self.delivered = self.acknowledged = int(saved.get("acknowledgedThrough", 0))
        self.model_revision = self.delivered
        self.active = True
        self.listeners = []
        owner.roles[name] = self
        if primary:
            owner.primary = self

    @property
    def pending(self):
        return self.owner.revision != self.model_revision or self.owner.paused

    def prepare(self, messages, *, typed=False):
        batch = [r for r in self.owner.records if r["sequence"] > self.delivered]
        if batch:
            # Queue delivery may already be in Tau's conversation.
            context = str(messages)
            missing = [r for r in batch if r["inputId"] not in context]
            if missing:
                text = self.owner.render(missing)
                if typed:
                    from harness._vendor.tau_agent.messages import UserMessage
                    messages.append(UserMessage(content=text))
                else:
                    messages.append({"role": "user", "content": text})
            self.delivered = batch[-1]["sequence"]
            self.owner.save_role(self)
            self.owner.show(f"#{batch[0]['sequence']}–#{self.delivered} · 已送达 {self.name}")
        self.model_revision = self.owner.revision
        return messages

    def acknowledge(self):
        if self.model_revision > self.acknowledged:
            self.acknowledged = self.model_revision
            self.owner.save_role(self)
            self.owner.show(f"#{self.acknowledged} · 已回应 {self.name}")

    def close(self):
        self.active = False
        self.listeners.clear()


class TaskSteering:
    def __init__(self, logger, *, quiet=False):
        self.logger, self.quiet = logger, quiet
        state = load_task_state(logger)
        saved = state.get("user_steering", {})
        inputs = state.get("operator_inputs", {})
        self.records = sorted([dict(r) for r in inputs.values()
            if r.get("source") in {"terminal_steering", "terminal_control"}], key=lambda r: r["sequence"])
        self.revision = max([r["sequence"] for r in self.records], default=0)
        self.saved_roles = saved.get("roles", {})
        self.roles = {}
        self.primary = None
        self.paused = bool(saved.get("paused", False))
        self.closed = False
        self.changed = asyncio.Event()
        self.runnable = asyncio.Event()
        if not self.paused:
            self.runnable.set()
        self.control_tasks = set()
        self.workflow_controls = set()
        self.workflow_tasks = {}
        self.workflow_runners = {}
        # Command name -> callable(line); registered by the running session.
        self.control_handlers = {}

    def show(self, text):
        if not self.quiet:
            print(f"\n[用户引导 · @{self.logger.task_id}] {text}", flush=True)
            from harness.runtime.hitl_input import notify_terminal_output
            notify_terminal_output()

    def register(self, name, *, primary=False):
        prior = self.roles.get(name)
        if prior and prior.active:
            return prior
        return SteeringRole(self, name, primary=primary)

    def save_role(self, role):
        state = load_task_state(self.logger)
        data = state.setdefault("user_steering", {})
        progress = {
            "deliveredThrough": role.delivered, "acknowledgedThrough": role.acknowledged}
        data.setdefault("roles", {})[role.name] = progress
        write_task_state(self.logger, state)
        self.saved_roles[role.name] = progress

    def accept(self, text, *, control=False):
        if self.closed:
            self.show(f"任务已结束，未插入。请用 /resume {self.logger.task_id} <补充要求>")
            return False
        text = text.strip()
        if not text:
            self.show("未接收：消息不能为空。")
            return False
        sequence = self.revision + 1
        item = {"inputId": uuid.uuid4().hex, "taskId": self.logger.task_id,
            "sequence": sequence, "receivedAt": datetime.now(timezone.utc).isoformat(),
            "source": "terminal_control" if control else "terminal_steering", "text": text}
        state = load_task_state(self.logger)
        state.setdefault("operator_inputs", {})[item["inputId"]] = item
        state.setdefault("operator_input_order", []).append(item["inputId"])
        paused = text == "/pause" if control else self.paused
        if control and text == "/continue": paused = False
        state.setdefault("user_steering", {}).update(inputRevision=sequence, paused=paused, terminal=False)
        write_task_state(self.logger, state)  # Before receipt acknowledgement.
        self.records.append(item)
        self.revision, self.paused = sequence, paused
        if paused: self.runnable.clear()
        else: self.runnable.set()
        self.changed.set()
        self.changed = asyncio.Event()
        self.show(f"#{sequence} · 已接收：{text[:180]}")
        try:
            self.logger.write("user.steering.received", {"inputId": item["inputId"], "sequence": sequence})
        except Exception as exc:
            self.show(f"#{sequence} 已持久化，附加事件写入失败：{type(exc).__name__}")
        for role in list(self.roles.values()):
            if role.active:
                for listener in list(role.listeners): listener(item)
        if text != "/continue":
            for callback in list(self.workflow_controls): callback()
        return True

    @staticmethod
    def render(records):
        return "[真实用户运行中引导；按顺序结合原始目标判断]\n" + json.dumps(records, ensure_ascii=False) + "\n" + STEERING_GUIDE

    def latest_inputs(self, original_goal):
        return user_context(self.logger, original_goal)["operatorInputs"]

    def can_finish(self, expected=None):
        return (not self.paused and (expected is None or expected == self.revision)
                and (self.primary is None or self.primary.acknowledged >= self.revision))

    def commit(self, expected=None):
        if not self.can_finish(expected): return False
        state = load_task_state(self.logger)
        state.setdefault("user_steering", {}).update(terminal=True, finalizingRevision=self.revision)
        write_task_state(self.logger, state)
        self.closed = True
        return True

    async def wait_runnable(self):
        await self.runnable.wait()

    def status(self):
        from harness.runtime.workflow_steering import owned_workflows
        self.show(json.dumps({"inputRevision": self.revision, "paused": self.paused,
            "messages": [{"sequence": r["sequence"], "text": r["text"][:180]} for r in self.records[-10:]],
            "roles": {r.name: {"active": r.active, "deliveredThrough": r.delivered,
                "acknowledgedThrough": r.acknowledged} for r in self.roles.values()},
            "workflows": owned_workflows(self)}, ensure_ascii=False))


def steerable_task(fn):
    """One terminal owner across nested task-owned workers and reviews."""
    @functools.wraps(fn)
    async def wrapped(self, *args, **kwargs):
        existing = current_steering()
        if existing is not None:
            return await fn(self, *args, **kwargs)
        manager = TaskSteering(self.logger, quiet=getattr(self, "quiet", False))
        token = _current.set(manager)
        from harness.runtime.hitl_input import attach_task_input, detach_task_input
        broker = attach_task_input(manager)
        try:
            return await fn(self, *args, **kwargs)
        finally:
            manager.closed = True
            detach_task_input(broker, manager)
            try:
                if manager.workflow_tasks:
                    from harness.runtime.workflow_steering import shutdown_workflows
                    await shutdown_workflows(manager)
            finally:
                if manager.control_tasks:
                    tasks = list(manager.control_tasks)
                    for task in tasks: task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                _current.reset(token)
    return wrapped
