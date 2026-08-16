"""
harness.tools.browser_tools.progress_obs - Progress accountant observations and worker contract checks.
"""

import re
from typing import Any
from typing import Optional
from harness.observation.page_lifecycle import PageLifecycleTracker
from harness.progress import extraction_artifact_count
from harness.task_types import resolve_task_type_fail_closed
from harness.tool_policy import disabled_reason_for_method
from harness.utils import JsonDict
from harness.utils import optional_int

def _bt():
    import harness.tools.browser_tools as bt

    return bt

def _record_extraction_persisted(result: JsonDict) -> bool:
    return bool(isinstance(result, dict) and str(result.get("savedPath") or "").strip())

def _gate_subject_tool(next_tool: str, tool_input: JsonDict) -> str:
    if str(next_tool or "") == "browser_call":
        method = str(tool_input.get("method") or "").strip()
        if method:
            return method
    return str(next_tool or "").strip()

_PROGRESS_OBSERVATION_IDENTITY_KEYS = (
    "source",
    "reasonObserved",
    "tool",
    "pageId",
    "diagnosticScope",
)

def _progress_observation_is_new(
    agent: Any, progress: Any, fact: JsonDict,
) -> bool:
    """True when this observation has not been put to the model yet.

    Task a608b5e7 emitted 102 progress observations covering 33 distinct
    situations: roughly two in three were the same fact restated, each one
    carried on a fresh ~83k-token context replay.

    The memory is scoped to a stretch without artifact progress. When an
    extraction lands the slate clears, so a stall that returns after real
    progress is reported again — it genuinely is new information then.
    """
    identity = tuple(
        str(fact.get(key) or "") for key in _PROGRESS_OBSERVATION_IDENTITY_KEYS
    )
    artifact_mark = int(getattr(progress, "extraction_artifact_count", 0) or 0)
    seen = getattr(agent, "_progress_observation_identities", None)
    if (
        not isinstance(seen, set)
        or getattr(agent, "_progress_observation_artifact_mark", None)
        != artifact_mark
    ):
        seen = set()
        try:
            agent._progress_observation_identities = seen
            agent._progress_observation_artifact_mark = artifact_mark
        except Exception:
            return True
    if identity in seen:
        return False
    seen.add(identity)
    return True

def _annotate_axtree_offload(response: Any, snapshot: Optional[JsonDict]) -> None:
    """Say that an offloaded AXTree is also queryable in memory, and until when.

    The file and the live indexed snapshot hold the same tree, but only while
    the epoch stands. Task a608b5e7 answered every AXTree question by searching
    the file — 100 `local_fs_search` and 108 `local_fs_read` turns — with
    `find_in_axtree` sitting on the same data in memory the whole time.

    The file is not redundant: once `Input.*` or a navigation bumps the epoch
    it is the only record of that tree, which is why this points at the live
    tool rather than replacing the path. No epoch number is quoted here because
    it is not settled until after this response is built; `find_in_axtree`
    answers `needs_fresh_axtree` on its own when the tree has moved on.
    """
    if not snapshot or not isinstance(response, dict):
        return
    data = response.get("data")
    if not isinstance(data, dict):
        return
    blob = data.get("lines")
    if not isinstance(blob, dict) or not blob.get("_offloaded"):
        return
    blob["liveQuery"] = {
        "tool": "find_in_axtree",
        "holdsWhile": "this page's AXTree epoch is unchanged",
        "note": (
            "The same tree is indexed in memory: query it with find_in_axtree"
            " instead of searching this file. After an Input.* action or a"
            " navigation bumps the epoch, find_in_axtree reports"
            " needs_fresh_axtree and this file becomes the only record of"
            " this tree."
        ),
    }

def _observe_unrecorded_extraction_before(
    agent: Any,
    next_tool: str,
    tool_input: JsonDict,
    step: Optional[int] = None,
) -> None:
    """Expose pending Runtime rows as facts without deciding the next action."""
    if next_tool == "record_extraction":
        return
    pending = getattr(agent, "pending_unrecorded_extraction", None)
    if not isinstance(pending, dict):
        return
    observations = optional_int(pending.get("observations"), 0) or 0
    pending["observations"] = observations + 1
    agent.pending_unrecorded_extraction = pending
    fact: JsonDict = {
        "source": "unrecorded_runtime_rows",
        "reasonObserved": "structured_rows_not_persisted",
        "rowCount": pending.get("rowCount"),
        "rowSource": pending.get("source"),
        "observedAtStep": pending.get("step"),
        "nextTool": _gate_subject_tool(next_tool, tool_input),
        "observationCount": pending["observations"],
    }
    queued = getattr(agent, "_pending_progress_observations", None)
    if not isinstance(queued, list):
        queued = []
        agent._pending_progress_observations = queued
    queued.append(fact)
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("progress.unrecorded_rows_observed", fact)
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({
            "type": "progress_observation",
            "step": step,
            "result": fact,
        })

def _check_cross_task_memory_scope(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Block Memory.get/save against another task's scope.

    Task-scope memories carry a previous task's objective/steps; reading
    them contaminates the current worker's premise (2cb616: "scroll to
    rank 50, extract 11 rows" restored as established knowledge), and
    writing them corrupts the other task's record. Registration already
    strips foreign entries; this guard closes the direct-query path.
    Non-task scopes (auth fleet, fleet ids) are untouched.
    """
    if method not in {"Memory.get", "Memory.save"}:
        return None
    scope = str((params or {}).get("scope") or "").strip()
    if not scope:
        return None
    parts = scope.split(":")
    if len(parts) < 3 or parts[-1] != "task":
        return None
    scope_task_id = parts[-2]
    # Only gate scopes whose middle segment looks like a harness task id
    # (long hex) — custom scopes keep working.
    if not re.fullmatch(r"[0-9a-f]{16,}", scope_task_id):
        return None
    task_dir = getattr(getattr(agent, "logger", None), "task_dir", None)
    current_task_id = str(getattr(task_dir, "name", "") or "")
    if not current_task_id or scope_task_id == current_task_id:
        return None
    return {
        "status": "rejected",
        "method": method,
        "error": (
            f"{method} targets another task's memory scope: {scope}"
        ),
        "tool_was_executed": False,
        "next_instruction": (
            "Memory from other tasks is historical context, not instructions"
            " for the current task. Use your own task scope"
            f" (…:{current_task_id}:task) and derive the objective from the"
            " user_task and worker contract only."
        ),
    }

def _check_worker_contract(agent: Any, method_or_tool: str) -> Optional[JsonDict]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict) or not contract:
        contract = {}

    forbidden = {
        str(item).strip()
        for item in contract.get("forbidden_methods", [])
        if str(item).strip()
    }
    if any(_method_pattern_matches(pattern, method_or_tool) for pattern in forbidden):
        return {
            "status": "contract_violation",
            "method": method_or_tool,
            "error": f"{method_or_tool} is forbidden by worker_contract",
            "next_instruction": "Choose an allowed method or finalize with a blocker.",
        }

    resolved_contract_task_type = resolve_task_type_fail_closed(
        contract.get("task_type")
    )
    disabled_reason = ""
    if "." in str(method_or_tool):
        disabled_reason = disabled_reason_for_method(
            method_or_tool,
            resolved_contract_task_type,
        )
    if disabled_reason:
        return {
            "status": "contract_violation",
            "method": method_or_tool,
            "error": disabled_reason,
            "task_type": resolved_contract_task_type,
            "classification": {
                "category": "blocked_cross_task_type_required",
                "hint": (
                    "This phase needs a method outside its task_type policy."
                ),
                "method": method_or_tool,
                "task_type": resolved_contract_task_type,
            },
            "next_instruction": (
                "Use a method allowed by the task_type policy, or finalize with"
                " a blocker if this task really requires the disabled domain."
                " In final_answer, report blocked_cross_task_type_required so"
                " LeadAgent can emit a new phase with the appropriate task_type."
            ),
        }

    max_attempts = contract.get("max_surface_attempts")
    if isinstance(max_attempts, dict):
        limit = optional_int(max_attempts.get(method_or_tool))
        if limit is not None and limit >= 0:
            attempts = getattr(agent, "surface_attempts", None)
            if not isinstance(attempts, dict):
                attempts = {}
                agent.surface_attempts = attempts
            current = optional_int(attempts.get(method_or_tool), 0) or 0
            if current >= limit:
                return {
                    "status": "contract_violation",
                    "method": method_or_tool,
                    "error": (
                        f"{method_or_tool} exceeded max_surface_attempts={limit}"
                    ),
                    "next_instruction": (
                        "Switch strategy, record the blocker, or call final_answer."
                    ),
                }
            attempts[method_or_tool] = current + 1

    return None

def _method_pattern_matches(pattern: str, method: str) -> bool:
    if pattern == method:
        return True
    if pattern.endswith(".*"):
        return method.startswith(pattern[:-1])
    return False

def _is_own_artifact_read(agent: Any, tool_name: str, path_hint: Any) -> bool:
    """True only for local_fs_read of a file THIS RUN persisted via
    record_extraction (exact path match against the attempt ledger). Reading
    one's own needs_fix artifact to figure out what to fix is analysis of the
    ledger, not offload spinning — task 9d5655d3 got gated mid-self-diagnosis."""
    if tool_name != "local_fs_read":
        return False
    path = str(path_hint or "")
    if not path or "/artifacts/extractions/" not in path:
        return False
    attempts = {
        str(item)
        for item in (getattr(agent, "extraction_attempt_artifacts", None) or [])
    }
    return path in attempts

def _observe_progress_before(
    agent: Any,
    tool_name: str,
    tool_input: Optional[JsonDict] = None,
    step: Optional[int] = None,
    *,
    charge_diagnostic: bool = True,
) -> Optional[JsonDict]:
    """Record the accountant's arithmetic observation about this call.

    The accountant emits facts, not verdicts: nothing here decides whether the
    call runs, and the caller dispatches it either way.
    """
    progress = getattr(agent, "progress", None)
    if progress is None:
        return None
    limit = optional_int(
        getattr(agent.runtime.harness, "progress_local_fs_without_extraction_limit", 5),
        5,
    ) or 5
    raw_no_artifact_limit = optional_int(
        getattr(agent.runtime.harness, "progress_no_artifact_limit", 8),
        8,
    )
    no_artifact_limit = (
        raw_no_artifact_limit
        if raw_no_artifact_limit is not None
        else 8
    )
    contract = getattr(agent, "worker_contract", {}) or {}
    requires_artifact = bool(
        contract.get("must_record_extraction")
        or contract.get("expected_artifact")
        or contract.get("validators")
    )
    page_id = str((tool_input or {}).get("pageId") or "")
    mandatory_recovery_generation: Optional[int] = None
    lifecycle = getattr(agent, "page_lifecycle", None)
    if isinstance(lifecycle, PageLifecycleTracker) and page_id:
        lifecycle_state = lifecycle.state(page_id)
        if lifecycle_state is not None and (
            (
                tool_name == "Page.getState"
                and lifecycle_state.requires_state_resync
            )
            or (
                tool_name == "DOM.getAXTree"
                and not lifecycle_state.requires_state_resync
                and lifecycle_state.requires_ax_refresh
            )
        ):
            mandatory_recovery_generation = lifecycle_state.generation
    result = progress.observe_before(
        tool_name=tool_name,
        artifact_count=extraction_artifact_count(getattr(agent, "artifacts", [])),
        local_fs_limit=limit,
        no_artifact_limit=no_artifact_limit,
        requires_artifact=requires_artifact,
        own_artifact_read=_is_own_artifact_read(
            agent, tool_name, (tool_input or {}).get("path"),
        ),
        step=step,
        page_id=page_id,
        charge_heavy_diagnostic=charge_diagnostic,
        mandatory_recovery_generation=mandatory_recovery_generation,
    )
    mandatory_allowance = getattr(
        progress, "last_mandatory_recovery_allowance", None
    )
    if isinstance(mandatory_allowance, dict):
        agent.logger.write(
            "progress.mandatory_recovery_credit_used",
            dict(mandatory_allowance),
        )
    allowance = (
        progress.consume_diagnostic_allowance()
        if hasattr(progress, "consume_diagnostic_allowance") else None
    )
    if isinstance(allowance, dict) and tool_name == "DOM.getSemanticTree":
        agent.logger.write("semantic_tree.diagnostic_bypass", allowance)
    if result is None:
        return None
    # Saves that carried schemaWarnings were persisted but deliberately NOT
    # credited to the artifact ledger ("trust the ledger, not the claim").
    # Surface them beside the observation so neither the model nor a human
    # reading the log mistakes "uncredited save" for "never extracted
    # anything" — task 9d5655d3's diagnosis stalled on that ambiguity.
    attempted = [
        str(path)
        for path in (getattr(agent, "extraction_attempt_artifacts", None) or [])
    ]
    credited = {
        str(path) for path in (getattr(agent, "artifacts", None) or [])
    }
    uncredited = [path for path in attempted if path not in credited]
    if uncredited:
        result["uncreditedArtifacts"] = {
            "count": len(uncredited),
            "paths": uncredited[-3:],
            "note": (
                "saved with schema warnings, so not counted as extraction"
                " progress; fix the row keys/values and re-record"
            ),
        }
    if _progress_observation_is_new(agent, progress, result):
        pending = getattr(agent, "_pending_progress_observations", None)
        if not isinstance(pending, list):
            pending = []
            agent._pending_progress_observations = pending
        pending.append(result)
    else:
        # Restating a fact the model already has does not make it truer, and
        # every restatement rides a full context replay. The counts still go
        # to the trace and handoff below, where reading them costs nothing.
        result["repeatOfReportedObservation"] = True
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("progress.observed", result)
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({
            "type": "progress_observation",
            "step": step,
            "result": result,
        })
    return result

def _observe_progress_after(agent: Any, tool_name: str, result: Optional[JsonDict] = None) -> None:
    progress = getattr(agent, "progress", None)
    if progress is None:
        return
    result_path = (
        result.get("path") if isinstance(result, dict) else None
    )
    progress.after_tool(
        tool_name=tool_name,
        artifact_count=extraction_artifact_count(getattr(agent, "artifacts", [])),
        result=result,
        own_artifact_read=_is_own_artifact_read(agent, tool_name, result_path),
    )
    repair_merge = (
        result.get("repairMerge") if isinstance(result, dict) else None
    )
    applied_repairs = (
        repair_merge.get("applied")
        if isinstance(repair_merge, dict) else None
    )
    if applied_repairs and hasattr(progress, "notify_repair_progress"):
        repair_progress = progress.notify_repair_progress(applied_repairs)
        if repair_progress.get("newFieldCount"):
            agent.logger.write("progress.repair_advanced", repair_progress)
    if (
        tool_name == "navigate_verified"
        and isinstance(result, dict)
        and result.get("status") == "done"
        and hasattr(progress, "notify_navigation_success")
    ):
        progress.notify_navigation_success(str(result.get("pageId") or ""))
    agent.logger.write("progress.snapshot", progress.to_log_payload())
