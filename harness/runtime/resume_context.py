"""Persisted orchestration context shared by task entry points."""
from __future__ import annotations

from dataclasses import dataclass, field
from harness.utils import JsonDict

@dataclass
class ResumeContext:
    """Durable task state injected into a fresh task runner process.

    Resume deliberately restores orchestration at phase granularity.  It does
    not pretend that a worker coroutine or a model conversation survived the
    previous process.
    """

    original_user_task: str
    current_plan: JsonDict
    initial_plan: JsonDict
    initial_plan_recovered: bool = True
    instruction: str = ""
    report: JsonDict = field(default_factory=dict)
    run_id: str = ""
    browser_hint: JsonDict = field(default_factory=dict)
    task_dir: str = ""

    def prompt_payload(self) -> JsonDict:
        return {
            "taskDir": self.task_dir,
            "runId": self.run_id,
            "instruction": self.instruction or None,
            "initialPlanRecovered": self.initial_plan_recovered,
            **dict(self.report or {}),
        }
