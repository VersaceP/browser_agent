"""
main.py - CLI entrypoint for ABCP Agent Harness.
"""

import argparse
import asyncio
import json
import re
import shlex
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import readline  # noqa: F401  启用 input() 的行编辑（退格/方向键）
except ImportError:
    pass

from harness.runtime.resume_context import ResumeContext
from harness.runtime.model_support import llm_rate_limit_terminal_result
from harness.utils import exception_payload
from harness.agents.browser.task import (
    BrowserTaskRunner, _run_browser_mode, _browser_mode_terminal_error,
    _shutdown_browser_mode, _submit_direct_plan, _wait_for_browser_mode_result,
    _browser_mode_failure, _submit_browser_resume_amendment,
    _unbound_browser_resume_inputs, _bind_browser_resume_inputs,
)
from harness.storage import create_storage_from_config
from harness.storage.base import StorageError
from harness.storage.factory import resolve_sqlite_path
from harness.storage.virtual_fs import db_authoritative_for
from harness.utils import JsonDict, RunLogger
from harness.version import HARNESS_VERSION
from harness.runtime.resume_state import (
    ResumeStateError,
    RunLock,
    RunLockError,
    acquire_run_lock,
    configure_resume_storage,
    database_has_task,
    resume_worktree_dir,
    load_task_manifest,
    load_task_plan_strict,
    load_task_state_strict,
    promote_legacy_task_to_database,
    recover_legacy_user_task,
    reconcile_torn_plan_alias,
    release_run_lock,
    load_initial_task_plan_strict,
    write_task_manifest,
)
from harness.task_control import (
    TERMINAL_PHASE_STATUSES,
    load_task_state,
    prepare_resume_state,
    write_task_state,
)
from harness.planning.fleet_reference import extract_fleet_reference
from llm import (
    LLMConnectionError,
    LLMEmptyResponseError,
    LLMFactory,
    LLMProviderProtocolError,
    LLMRateLimitError,
    LLMRequestTimeoutError,
)
from llm.base import connection_failure_reason
from runtime_config import RuntimeConfig, load_runtime_config


_LAST_LOGGER: Optional[RunLogger] = None
_CANCELLED_LOGGED = False
LLM_TEMPORARY_FAILURE_EXIT_CODE = 75
CLI_ERROR_EXIT_CODE = 1
CLI_INPUT_ERROR_EXIT_CODE = 2
CLI_IO_FAILURE_EXIT_CODE = 74
CLI_CANCELLED_EXIT_CODE = 130

_INTERACTIVE_AGENT_MODES = {
    "/browser": "browser",
}
_AGENT_MODE_LABELS = {
    "browser": "direct BrowserAgent execution",
    "workflow": "Create, run and repair reusable Workshop packages",
}


def _safe_logger_write(
    logger: Optional[RunLogger],
    event_type: str,
    payload: JsonDict,
) -> bool:
    """Best-effort event write that can never replace the primary failure."""
    if logger is None:
        return False
    try:
        logger.write(event_type, payload)
        return True
    except Exception:
        return False


def _exception_http_status(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _cli_failure_result(
    *,
    code: str,
    message: str,
    error_type: str,
    retryable: bool,
    status: str = "failed",
    details: Optional[JsonDict] = None,
) -> JsonDict:
    """Stable failure envelope for CLI and in-process panel callers."""
    rendered = str(message or error_type or code).strip()[:2000]
    error: JsonDict = {
        "code": code,
        "type": error_type,
        "message": rendered,
        "retryable": retryable,
    }
    if details:
        error["details"] = details
    return {
        "status": status,
        "blockers": [{"type": code, "detail": rendered}],
        "error": error,
    }


def _classify_cli_exception(
    exc: BaseException,
    *,
    phase: str,
) -> "tuple[JsonDict, int]":
    """Map an exception to a non-throwing host result and process exit code."""
    if isinstance(exc, LLMRateLimitError):
        return llm_rate_limit_terminal_result(exc), LLM_TEMPORARY_FAILURE_EXIT_CODE

    llm_kinds = (
        (LLMRequestTimeoutError, "llm_timeout", "LLM request timed out."),
        (LLMConnectionError, "llm_connection_error", "LLM connection failed."),
        (
            LLMProviderProtocolError,
            "llm_provider_protocol_error",
            "LLM provider returned an unusable protocol response.",
        ),
        (
            LLMEmptyResponseError,
            "llm_empty_response",
            "LLM provider repeatedly returned an empty response.",
        ),
    )
    for error_class, code, fallback_message in llm_kinds:
        if isinstance(exc, error_class):
            return _cli_failure_result(
                code=code,
                message=str(exc) or fallback_message,
                error_type=type(exc).__name__,
                retryable=True,
                status="incomplete",
            ), LLM_TEMPORARY_FAILURE_EXIT_CODE

    raw_connection_reason = connection_failure_reason(exc)
    if isinstance(exc, asyncio.TimeoutError) or raw_connection_reason:
        code = (
            "provider_timeout"
            if isinstance(exc, asyncio.TimeoutError)
            else "provider_connection_error"
        )
        return _cli_failure_result(
            code=code,
            message=str(exc) or code,
            error_type=type(exc).__name__,
            retryable=True,
            status="incomplete",
            details=(
                {"reason": raw_connection_reason}
                if raw_connection_reason
                else None
            ),
        ), LLM_TEMPORARY_FAILURE_EXIT_CODE

    http_status = _exception_http_status(exc)
    if http_status is not None:
        if http_status in {408, 409, 425, 429} or http_status >= 500:
            return _cli_failure_result(
                code="provider_temporary_error",
                message=str(exc),
                error_type=type(exc).__name__,
                retryable=True,
                status="incomplete",
                details={"statusCode": http_status},
            ), LLM_TEMPORARY_FAILURE_EXIT_CODE
        if http_status in {401, 403}:
            return _cli_failure_result(
                code="provider_auth_error",
                message=str(exc),
                error_type=type(exc).__name__,
                retryable=False,
                details={"statusCode": http_status},
            ), CLI_INPUT_ERROR_EXIT_CODE
        return _cli_failure_result(
            code="provider_request_error",
            message=str(exc),
            error_type=type(exc).__name__,
            retryable=False,
            details={"statusCode": http_status},
        ), CLI_ERROR_EXIT_CODE

    if phase == "startup":
        return _cli_failure_result(
            code="startup_error",
            message=str(exc),
            error_type=type(exc).__name__,
            retryable=False,
        ), CLI_INPUT_ERROR_EXIT_CODE
    if isinstance(exc, (StorageError, OSError)):
        return _cli_failure_result(
            code="io_or_storage_error",
            message=str(exc),
            error_type=type(exc).__name__,
            retryable=True,
        ), CLI_IO_FAILURE_EXIT_CODE
    return _cli_failure_result(
        code="internal_error",
        message=str(exc),
        error_type=type(exc).__name__,
        retryable=False,
    ), CLI_ERROR_EXIT_CODE


def _cancelled_cli_result(reason: str) -> JsonDict:
    return _cli_failure_result(
        code="cancelled",
        message=reason or "Task execution was cancelled.",
        error_type="CancelledError",
        retryable=True,
        status="cancelled",
    )


def _print_json_result(payload: JsonDict, *, stream: Any = None) -> bool:
    try:
        print(
            json.dumps(payload, ensure_ascii=False, default=str),
            file=stream or sys.stdout,
            flush=True,
        )
        return True
    except (BrokenPipeError, OSError, UnicodeError):
        return False


def _print_text(value: Any, *, stream: Any = None) -> bool:
    """Best-effort text output for hosts that may close stdout early."""
    try:
        print(value, file=stream or sys.stdout, flush=True)
        return True
    except (BrokenPipeError, OSError, UnicodeError):
        return False


class ConsoleProgressReporter:
    def __init__(self) -> None:
        # transport.response payloads don't carry the method name; remember
        # the last requested method per actor so we can silence the
        # bootstrap describeAction storm (one request + one response per
        # method, ~68 each) and replace it with a single schema.bundle.loaded
        # summary line.
        self._last_method_by_actor: Dict[str, str] = {}

    def __call__(self, event_type: str, payload: Dict[str, Any]) -> None:
        message = self._format(event_type, payload)
        if message:
            print(message, flush=True)
            from harness.runtime.hitl_input import notify_terminal_output
            notify_terminal_output()

    def _format(self, event_type: str, payload: Dict[str, Any]) -> Optional[str]:
        if event_type == "browser.task_review.start":
            return f"[BrowserReview] Starting {payload.get('phase')} review at browser step {payload.get('step')}"
        if event_type == "browser.task_review.model":
            return f"[BrowserReview {payload.get('reviewId')}] Round {payload.get('round')}: requesting model..."
        if event_type == "browser.task_review.tool":
            result = payload.get("result") or {}
            method = (payload.get("input") or {}).get("method")
            detail = result.get("reason") or result.get("error") or ""
            if payload.get("tool") in {"review_search_text", "review_list_files"} \
                    and result.get("status") == "done":
                detail = (f"{result.get('count', 0)} records"
                          f", next offset {result.get('nextLineOffset') or result.get('nextOffset')}"
                          if result.get("truncated") else f"{result.get('count', 0)} records")
            return (f"[BrowserReview {payload.get('reviewId')}] {payload.get('tool')}"
                    f"{(' ' + str(method)) if method else ''}: {result.get('status')}"
                    f" {self._short_text(detail, 160)}").rstrip()
        if event_type == "browser.task_review.verdict":
            errors = (payload.get("validation") or {}).get("errors")
            if errors:
                return f"[BrowserReview] Review verdict requires protocol repair: {self._short_text('; '.join(errors), 350)}"
            return None  # the completion event prints the verdict once
        if event_type == "browser.task_review":
            return (f"[BrowserReview] {payload.get('verdict') or payload.get('status')}"
                    f", {payload.get('modelCalls', '?')} model calls, {payload.get('durationMs', '?')}ms"
                    f", {len(payload.get('issues') or [])} issues: "
                    f"{self._short_text(payload.get('reason'), 240)}")
        if event_type == "browser.task_review.skipped":
            return f"[BrowserReview] Not started: {payload.get('reason')}"
        if event_type == "llm.usage" and payload.get("source") == "browser_task_reviewer":
            return (f"[LLM {payload.get('source')}] cache_read={payload.get('cache_read', 0)} "
                    f"cache_creation={payload.get('cache_creation', 0)} "
                    f"uncached_input={payload.get('uncached_input', 0)} output={payload.get('output', 0)}")
        if event_type == "lifecycle.compaction.start":
            return (
                "[Compaction] Started: "
                f"reason={payload.get('reason')} "
                f"tokens≈{payload.get('estimatedTokensBefore')}"
            )
        if event_type == "lifecycle.compaction.end":
            status = payload.get("status") or "completed"
            if status == "completed":
                fallback = (
                    "(fallback summary)"
                    if payload.get("summaryMode") == "mechanical_fallback"
                    else ""
                )
                return (
                    "[Compaction] Completed: "
                    f"tokens≈{payload.get('estimatedTokensAfter')} "
                    f"checkpoint={payload.get('checkpointRef') or '-'}{fallback}"
                )
            return f"[Compaction] {status}: {self._short_text(payload.get('error'), 160)}"
        if event_type == "agent.step.start":
            return (
                f"[{self._browser_actor_label(payload)}] "
                f"Step {payload.get('step')}: requesting model..."
            )
        if event_type in {"agent.model"}:
            return self._format_model_event(event_type, payload)
        if event_type == "agent.truncated_response" and payload.get("kind") == "truncated":
            streak = payload.get("streak")
            limit = payload.get("limit")
            ending = isinstance(streak, int) and isinstance(limit, int) and streak >= limit
            action = "terminate this worker and return an incomplete result" if ending else "request the model again using the current recovery policy"
            return (
                f"[{self._browser_actor_label(payload)}] Consecutive errors {streak}/{limit} (this attempt: output truncated); "
                f"{action}"
            )
        if event_type == "tool_result.offloaded":
            return self._format_offloaded_tool_result(payload)
        if event_type == "spawner.browser.spawn":
            return (
                f"[BrowserAgent] Started {payload.get('workerId')} "
                f"({payload.get('name') or 'unnamed'})"
            )
        if event_type == "spawner.browser.result":
            return self._format_browser_result(payload)
        if event_type == "task_plan.accepted":
            return (
                f"[TaskPlan] Accepted {payload.get('phaseCount') or 0} phases: "
                f"{payload.get('path')}"
            )
        if event_type == "task_plan.rejected":
            return self._format_task_plan_rejected(payload)
        if event_type == "task_state.initialized":
            return f"[TaskState] Initialized: {payload.get('path')}"
        if event_type == "spawner.resume_browser_hint.used":
            page = payload.get("pageId")
            suffix = f", page {page}" if page else ""
            return (
                "[Resume] Original browser Fleet is available and reused: "
                f"{payload.get('fleetId')}{suffix}. "
            )
        if event_type == "spawner.resume_browser_hint.page_probe_failed":
            return (
                "[Resume] Original page is unavailable; attempting to preserve the Fleet session "
                "and restart this phase on a new page."
            )
        if event_type == "spawner.resume_browser_hint.ignored":
            reason = str(payload.get("reason") or "unavailable")
            return (
                "[Resume] Original browser context was not restored"
                f"({reason}); restarting the phase with normal browser allocation; "
                "sign in again if needed."
            )
        if event_type == "progress.observed":
            return (
                f"[Progress] Observed {payload.get('tool') or '?'}: "
                f"{payload.get('reasonObserved') or 'no progress'}"
            )
        if event_type == "agent.step_cap.reminder":
            return (
                f"[{self._browser_actor_label(payload)}] "
                f"Approaching step limit: step={payload.get('step')} "
                f"remaining={payload.get('remaining')}"
            )
        if event_type == "browser.call.params_error":
            return (
                f"[BrowserCall] Invalid parameters for {payload.get('method') or '?'}: "
                f"{self._short_text(payload.get('error'), 140)}"
            )
        if event_type == "spawner.slot.sync_warning":
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            first = self._short_text(errors[0] if errors else payload.get("error"), 140)
            suffix = f" (+{len(errors) - 1})" if len(errors) > 1 else ""
            return (
                f"[Slot] Synchronization warning {payload.get('workerId') or ''}: "
                f"{first}{suffix}"
            )
        if event_type == "tool.record_extraction":
            return self._format_record_extraction(payload)
        if event_type == "vl.visual_verify":
            verdict = payload.get("verdict") or payload.get("status") or "unknown"
            confidence = payload.get("confidence")
            if confidence is None:
                return f"[VL] Visual verification: {verdict}"
            return f"[VL] Visual verification: {verdict} (confidence={confidence})"
        if event_type == "schema.bundle.loaded":
            count = payload.get("schema_count") or 0
            req = payload.get("requires_purpose_count") or 0
            guide_chars = payload.get("agent_guide_chars") or 0
            return (
                f"[Schema] Loaded {count} method schemas "
                f"({req} require purpose); agent guide: {guide_chars} characters."
            )
        if event_type == "loop_guard.warn":
            tool = payload.get("tool") or "?"
            streak = payload.get("streak") or 1
            return (
                f"[LoopGuard] Blocked repeated {tool} (attempt {streak}; not executed); "
                "asking the model to change its approach or stop."
            )
        if event_type == "loop_guard.force_stop":
            tool = payload.get("tool") or "?"
            streak = payload.get("streak") or 1
            return (
                f"[LoopGuard] Stopped after {streak} consecutive repeated calls to {tool}. "
                "Worker ended with extraction_inconclusive."
            )
        if event_type.endswith(".transport.recovery.started"):
            return f"[Browser connection] Dispatcher disconnected; reconnecting for up to {payload.get('timeoutSeconds', 60):g}s."
        if event_type.endswith(".transport.recovery.attempt"):
            return f"[Browser connection] Reconnect attempt {payload.get('attempt')}; {payload.get('remainingSeconds')}s remaining."
        if event_type.endswith(".transport.recovery.finished"):
            return f"[Browser connection] {payload.get('status')}; {payload.get('elapsedMs', 0)}ms; business actions replayed: 0."
        if event_type.endswith(".transport.request"):
            actor = event_type.split(".transport.", 1)[0]
            method = str(payload.get("method") or "unknown")
            self._last_method_by_actor[actor] = method
            if method == "System.describeAction":
                # Suppressed; see schema.bundle.loaded summary.
                return None
            return f"[{actor}] Browser call: {method}"
        if event_type.endswith(".transport.response"):
            actor = event_type.split(".transport.", 1)[0]
            if self._last_method_by_actor.get(actor) == "System.describeAction":
                return None
            if payload.get("error"):
                return f"[{actor}] Browser response: error ({self._format_error(payload)})"
            return f"[{actor}] Browser response: ok"
        if event_type in {"agent.model_timeout"}:
            attempts = len(
                payload.get("attempts") or payload.get("timeoutAttempts") or []
            )
            seconds = payload.get("timeoutSeconds")
            will_retry = payload.get("willRetry")
            tail = (
                "Compact context and retry this step." if will_retry
                else "Retries exhausted; this step has stopped." if will_retry is False
                else "Treating this step as an empty turn; recovery is handled by the repetition guard."
            )
            return (
                f"[LLM] Model idle timeout after {seconds}s without new data; "
                f"all {attempts} attempts, including the initial request, failed; {tail}"
            )
        if event_type in {"agent.model_connection_error"}:
            # The provider already retried silently; by the time this fires the
            # connection is repeatedly dropping, which the user should see
            # rather than discover as a traceback.
            attempts = len(payload.get("attempts") or payload.get("connectionAttempts") or [])
            will_retry = payload.get("willRetry")
            tail = (
                "Compact context and retry this step." if will_retry
                else "Retries exhausted; this step has stopped." if will_retry is False
                else "Treating this step as an empty turn; recovery is handled by the repetition guard."
            )
            return (
                f"[LLM] Model connection interrupted({payload.get('reason') or 'connection error'}); "
                f"all {attempts} attempts, including the initial request, failed; {tail}"
            )
        if event_type in {"run.error", "agent.error"}:
            error = payload.get("error") or payload.get("errorType") or "unknown error"
            return f"[Error] {error}"
        if event_type in {"agent.final"}:
            return "[Finished] Agent returned its final result."
        return None

    def _format_model_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
    ) -> str:
        role = self._browser_actor_label(payload)
        tool_calls = payload.get("tool_calls") or []
        tool_summaries = [
            self._format_tool_call(item)
            for item in tool_calls
            if isinstance(item, dict)
        ]
        text = self._short_text(payload.get("text"), 100)
        stop_reason = payload.get("stop_reason")
        telemetry = payload.get("outputTelemetry")
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        output_tokens = telemetry.get("providerOutputTokens")
        token_note = f", {output_tokens} output tokens" if output_tokens is not None else ""
        if stop_reason == "max_tokens":
            if tool_summaries:
                return (
                    f"[{role}] Model output reached max_tokens{token_note}; "
                    f"Response truncated; tool calls returned: {'; '.join(tool_summaries)}"
                )
            has_thinking = (
                bool(telemetry.get("thinkingBlockCount"))
                or bool(telemetry.get("thinkingChars"))
                or "thinking" in (telemetry.get("prefixBlockTypes") or [])
                or "redacted_thinking" in (telemetry.get("prefixBlockTypes") or [])
            )
            content_note = (
                "Only reasoning returned; no text or tool calls"
                if has_thinking and not text else "No tool calls returned"
            )
            return (
                f"[{role}] Model output reached max_tokens{token_note}; "
                f"{content_note}; no tools were executed in this step"
            )
        if tool_summaries:
            if text:
                return (
                    f"[{role}] Model response: {text}; "
                    f"Tool calls: {'; '.join(tool_summaries)}"
                )
            return f"[{role}] Model returned tool calls: {'; '.join(tool_summaries)}"
        if not text:
            reason_note = f", stop_reason={stop_reason}" if stop_reason else ""
            return f"[{role}] Model returned no text or tool calls{reason_note}"
        return f"[{role}] Model response: {text}"

    @staticmethod
    def _browser_actor_label(payload: Dict[str, Any]) -> str:
        worker_id = str(payload.get("workerId") or "").strip()
        slot_id = str(payload.get("slotId") or "").strip()
        agent_id = str(payload.get("agentId") or "").strip()
        identity = worker_id or agent_id or "unknown"
        if slot_id and slot_id != identity:
            return f"BrowserAgent {identity} / slot {slot_id}"
        return f"BrowserAgent {identity}"

    def _format_tool_call(self, tool_call: Dict[str, Any]) -> str:
        name = str(tool_call.get("name") or "unknown")
        raw_input = tool_call.get("input") or {}
        if not isinstance(raw_input, dict):
            return name
        if name != "browser_call":
            return self._format_harness_tool_call(name, raw_input)

        method = str(raw_input.get("method") or "unknown")
        details = []

        reason = self._short_text(raw_input.get("reason"), 60)
        if reason:
            details.append(f"reason={reason}")

        if "params" in raw_input:
            params_summary = self._format_params_summary(raw_input.get("params"))
            details.append(f"params={params_summary}")

        if not details:
            return f"{name} -> {method}"
        return f"{name} -> {method} ({'; '.join(details)})"

    def _format_harness_tool_call(self, name: str, raw_input: Dict[str, Any]) -> str:
        if name == "local_fs_read":
            return (
                f"{name} (file={self._short_path(raw_input.get('path'))}; "
                f"offset={raw_input.get('line_offset', 0)}; "
                f"limit={raw_input.get('line_limit', 200)})"
            )
        if name == "local_fs_search":
            return (
                f"{name} (glob={self._short_path(raw_input.get('glob') or '**/*')}; "
                f"pattern={self._short_text(raw_input.get('pattern'), 70) or '-'})"
            )
        if name == "record_extraction":
            rows = raw_input.get("rows")
            row_count = len(rows) if isinstance(rows, list) else "?"
            return f"{name} (name={raw_input.get('name') or '-'}; rows={row_count})"
        return name

    def _format_params_summary(self, params: Any) -> str:
        if isinstance(params, dict):
            if not params:
                return "{}"
            keys = [str(key) for key in params.keys()]
            shown = keys[:6]
            suffix = f",+{len(keys) - len(shown)}" if len(keys) > len(shown) else ""
            return "{" + ",".join(shown) + suffix + "}"
        if params is None:
            return "null"
        return f"<{type(params).__name__}>"

    def _short_text(self, value: Any, max_len: int) -> str:
        text = " ".join(str(value or "").strip().split())
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _short_path(self, value: Any, max_len: int = 90) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        if "/worktree/" in text:
            text = "worktree/" + text.split("/worktree/", 1)[1]
        else:
            for marker in (
                "tool_results/",
                "artifacts/",
                "observations/",
                "traces/",
                "contexts/",
            ):
                if marker in text:
                    text = marker + text.rsplit(marker, 1)[1]
                    break
        return self._short_text(text, max_len)

    def _format_error(self, payload: Dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or error.get("data") or "unknown error"
            return f"{code} {message}" if code is not None else str(message)
        return str(error or "unknown error")

    def _format_browser_result(self, payload: Dict[str, Any]) -> str:
        worker_id = payload.get("workerId") or "unknown"
        status = payload.get("status") or "unknown"
        validated = payload.get("validatedStatus") or "not_validated"
        phase = payload.get("phaseId") or "-"
        result_levels = payload.get("resultLevels") if isinstance(payload.get("resultLevels"), dict) else {}
        l1 = result_levels.get("l1") if isinstance(result_levels.get("l1"), dict) else {}
        l2 = result_levels.get("l2") if isinstance(result_levels.get("l2"), dict) else {}
        data = l2.get("data") if isinstance(l2.get("data"), dict) else {}
        artifact_validation = (
            payload.get("artifactValidation")
            if isinstance(payload.get("artifactValidation"), dict)
            else {}
        )
        row_count = artifact_validation.get("rowCount")
        if row_count is None:
            row_count = data.get("totalExtractedRows")
        artifact_count = l1.get("extractionArtifactCount") or l1.get("artifactCount")
        trace_summary = (
            payload.get("traceSummary")
            if isinstance(payload.get("traceSummary"), dict)
            else {}
        )
        errors = trace_summary.get("errors") if isinstance(trace_summary.get("errors"), list) else []
        error_count = l1.get("errorCount")
        if error_count is None:
            error_count = len(errors)
        blockers = l2.get("blockers") if isinstance(l2.get("blockers"), list) else []
        parts = [
            f"status={status}",
            f"validated={validated}",
            f"phase={phase}",
        ]
        if row_count is not None:
            parts.append(f"rows={row_count}")
        if artifact_count is not None:
            parts.append(f"artifacts={artifact_count}")
        if error_count:
            parts.append(f"errors={error_count}")
        if blockers:
            parts.append(f"blockers={len(blockers)}")
        if status == "failed":
            parts.append(f"error={self._short_text(payload.get('error'), 120) or 'unknown error'}")
        if status == "failed" or validated == "validation_failed":
            classification = artifact_validation.get("classification")
            if isinstance(classification, dict):
                category = classification.get("category")
                if category:
                    parts.append(f"classification={category}")
                failure_types = classification.get("failureTypes")
                if isinstance(failure_types, list) and failure_types:
                    shown = ",".join(str(item) for item in failure_types[:4])
                    if len(failure_types) > 4:
                        shown += f",+{len(failure_types) - 4}"
                    parts.append(f"failureTypes={shown}")
        return f"[BrowserAgent] {worker_id} finished: {', '.join(parts)}"

    def _format_task_plan_rejected(self, payload: Dict[str, Any]) -> str:
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        first = self._short_text(errors[0] if errors else payload.get("error"), 140)
        suffix = f" (+{len(errors) - 1})" if len(errors) > 1 else ""
        return f"[TaskPlan] Rejected: {first or 'invalid plan'}{suffix}"

    def _format_record_extraction(self, payload: Dict[str, Any]) -> str:
        name = payload.get("name") or "-"
        row_count = payload.get("rowCount")
        path = self._short_path(payload.get("savedPath"))
        warnings = payload.get("schemaWarnings")
        warning_count = len(warnings) if isinstance(warnings, list) else 0
        suffix = f", warnings={warning_count}" if warning_count else ""
        return f"[Artifact] {name} wrote {row_count if row_count is not None else '?'} rows: {path}{suffix}"


    def _format_offloaded_tool_result(self, payload: Dict[str, Any]) -> str:
        tool = payload.get("tool") or "tool"
        size = (
            payload["byteSize"]
            if "byteSize" in payload
            else payload["originalBytes"]
            if "originalBytes" in payload
            else "?"
        )
        return (
            f"[ToolResult] {tool} Result offloaded: {size} bytes -> "
            f"{self._short_path(payload.get('relativePath') or payload.get('savedPath'))}"
        )


_CJK_BOUNDARY_RE = re.compile(r"[　-〿㐀-鿿豈-﫿！-～]")


def _existing_task_path(candidate: str) -> Optional[str]:
    """Recognize physical task directories and DB-only task paths."""
    path = Path(candidate).expanduser()
    if path.is_dir():
        return str(path)
    if not path.is_absolute():
        rooted = Path(__file__).resolve().parent / path
        if rooted.is_dir():
            return str(rooted)
    try:
        return str(_resolve_resume_directory(candidate))
    except ValueError:
        return None


def _recover_task_path(positional: List[str]) -> "tuple[str, List[str]]":
    """Reassemble the task path from positional tokens.

    Two real-world input shapes break naive positional[0]: an unquoted path
    with spaces arrives as several tokens, and a trailing natural-language
    note can be glued to the last one (CJK needs no space before it). Try
    longest-first prefix joins; per candidate the untrimmed form goes first so
    genuine CJK directory names still win over the boundary trim. Returns
    (path, leftover tokens); falls back to the literal first token."""
    for k in range(len(positional), 0, -1):
        joined = " ".join(positional[:k])
        resolved = _existing_task_path(joined)
        if resolved is not None:
            return resolved, positional[k:]
        m = _CJK_BOUNDARY_RE.search(joined)
        if m and m.start() > 0:
            resolved = _existing_task_path(joined[:m.start()].rstrip())
            if resolved is not None:
                return resolved, [joined[m.start():]] + positional[k:]
    return positional[0], positional[1:]


def _split_command(line: str) -> "tuple[str, str]":
    """First shell token and the untouched remainder of a command line."""
    lexer = shlex.shlex(line.strip(), posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    command = lexer.get_token()
    return command or "", line.strip()[lexer.instream.tell():].lstrip()


_WORKFLOW_COMMANDS = frozenset({
    "/workflow", "/workflow-create", "/workflow-edit", "/workflow-drafts",
    "/workflow-publish", "/workflow-unpublish", "/workflow-rename", "/workflow-delete", "/workflow-stop",
})


def _is_workflow_command(line: str) -> bool:
    """Syntax only; Browser tasks never import the Workflow package."""
    first = str(line or "").strip().split(maxsplit=1)
    return bool(first) and first[0].lower() in _WORKFLOW_COMMANDS


def _handle_agent_mode_command(line: str, args: argparse.Namespace) -> bool:
    """Handle /browser at the interactive prompt.

    The choice is scoped to this invocation by mutating the parsed arguments
    only. It never rewrites config.json. Workflow commands are not a mode
    switch: each /workflow* line is a complete command.
    """
    first = line.strip().split(maxsplit=1)
    if not first or first[0].lower() not in _INTERACTIVE_AGENT_MODES:
        return False
    if len(first) != 1:
        print("Usage: /browser, then submit the task.")
        return True
    agent_mode = _INTERACTIVE_AGENT_MODES[first[0].lower()]
    args.agent_mode = agent_mode
    print(
        f"Selected {agent_mode}: {_AGENT_MODE_LABELS[agent_mode]}. "
        "Enter a task, a /workflow command, or /resume."
    )
    return True


def read_task(args: argparse.Namespace) -> str:
    if args.task_option:
        return args.task_option
    if args.task:
        return args.task
    if str(getattr(args, "resume", "") or "").strip():
        return str(getattr(args, "resume_instruction", "") or "").strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    configured_mode = str(
        getattr(args, "configured_agent_mode", "browser") or "browser"
    ).strip().lower()
    selected_mode = str(getattr(args, "agent_mode", "") or "").strip().lower()
    if selected_mode not in _AGENT_MODE_LABELS:
        selected_mode = ""
    if not selected_mode:
        print(
            "Choose /browser (direct BrowserAgent execution), or use a Workflow command: "
            "/workflow (list), /workflow <name> [task], /workflow-create <request>, "
            "/workflow-edit, /workflow-drafts, /workflow-publish, /workflow-unpublish, /workflow-rename, "
            "/workflow-delete, /workflow-stop."
        )
        print(f"Configured default: {configured_mode}.")
    while True:
        prompt = f"[{selected_mode}]> " if selected_mode else "Choose mode> "
        line = input(prompt).strip()
        if _is_workflow_command(line):
            return line
        if _handle_agent_mode_command(line, args):
            selected_mode = str(getattr(args, "agent_mode", "") or "").strip().lower()
            continue
        if line == "/resume" or line.startswith("/resume "):
            resumed = _handle_resume_command(line, args)
            if resumed is not None:
                return resumed
            continue
        if not selected_mode:
            print("Choose /browser or enter a /workflow command before submitting a task.")
            continue
        return line


def _handle_resume_command(
    line: str,
    args: argparse.Namespace,
) -> Optional[str]:
    """Parse an interactive phase-level resume request without touching disk."""

    try:
        command, tail = _split_command(line)
        reference, instruction = _split_command(tail)
        if command == "/resume" and re.fullmatch(r"@?[0-9a-fA-F]{32}", reference):
            args.resume = reference
            args.resume_instruction = instruction
            return instruction
        tokens = shlex.split(line)
    except ValueError as exc:
        print(f"Invalid /resume arguments: {exc}")
        return None
    if len(tokens) < 2:
        print("Usage: /resume <task-id> [instruction]")
        return None
    path, rest = _recover_task_path(tokens[1:])
    args.resume = path
    args.resume_instruction = " ".join(rest).strip()
    return args.resume_instruction


def _validate_resume_mode(manifest: Optional[JsonDict], plan: JsonDict, selected: str) -> str:
    """Check before reconciliation writes. Old tasks fall back to plan shape."""
    startup = (manifest or {}).get("startup_args") or {}
    from harness.workflow_mode.run_policy import completion_resume_error
    retired = completion_resume_error(manifest)
    if retired:
        raise ResumeStateError(retired)
    recorded = startup.get("agent_mode") if isinstance(startup, dict) else None
    original = str(recorded or "").strip().lower()
    execution_mode = str(plan.get("execution_mode") or "").strip()
    if original and original not in _AGENT_MODE_LABELS:
        raise ResumeStateError(
            f"Saved task mode '{original}' is no longer supported; this task cannot be resumed. "
            "Its saved records remain available. Start a new task in a supported mode."
        )
    if execution_mode and execution_mode not in {"direct_worker", "delegated"}:
        raise ResumeStateError(
            f"Saved plan execution mode '{execution_mode}' is no longer supported; "
            "this task cannot be resumed. Its saved records remain available."
        )
    if not original:
        if plan.get("execution_mode") == "direct_worker":
            original = "browser"
        else:
            raise ResumeStateError(
                "This legacy task has no recorded execution mode; it cannot be "
                "safely identified as a Browser task. Its saved records remain available."
            )
    if selected != original:
        raise ResumeStateError(
            f"This task uses /{original}; switch to /{original} before resuming from /{selected}."
        )
    return original


def _resolve_resume_directory(raw_path: str) -> Path:
    """Resolve a full task ID (optionally @-prefixed) or a legacy task path."""

    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("Missing task ID")
    identifier = raw.removeprefix("@")
    if re.fullmatch(r"[0-9a-fA-F]{32}", identifier):
        root = resume_worktree_dir() or Path(__file__).resolve().parent / "worktree"
        candidates = [root / identifier.lower()]
    else:
        candidate = Path(raw).expanduser()
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.append(Path(__file__).resolve().parent / candidate)
    for item in candidates:
        try:
            resolved = item.resolve(strict=False)
        except OSError:
            continue
        if resolved.is_dir():
            return resolved
        if not resolved.exists() and database_has_task(resolved):
            return resolved
    raise ValueError(
        "Task is absent from the database and its worktree directory is missing or deleted; "
        "cannot resume or recreate an empty task directory"
    )


def _load_initial_plan(
    task_dir: Path,
    current_plan: Dict[str, Any],
) -> "tuple[Dict[str, Any], bool]":
    """Recover the first accepted plan, from files or the database.

    Falling back to the current plan silently weakens resume: the immutable
    contract a replan is checked against becomes whatever the plan happens to
    be now. Reading it file-only meant every db-mode task took that fallback.
    """

    try:
        return load_initial_task_plan_strict(task_dir), True
    except ResumeStateError:
        return dict(current_plan), False


def _resume_browser_hint(
    state: Dict[str, Any],
    *,
    preferred_phase_ids: Sequence[str] = (),
) -> Dict[str, str]:
    browser = state.get("browser_context")
    browser = browser if isinstance(browser, dict) else {}
    phase_primaries = browser.get("phase_primaries")
    phase_primaries = (
        phase_primaries if isinstance(phase_primaries, dict) else {}
    )
    phase_order = [str(item or "").strip() for item in preferred_phase_ids]
    current_phase = str(state.get("current_phase") or "").strip()
    if current_phase and current_phase not in phase_order:
        phase_order.append(current_phase)
    candidates = []
    for phase_id in phase_order:
        primary = phase_primaries.get(phase_id)
        if isinstance(primary, dict):
            candidates.append((primary, f"resume_phase:{phase_id}", phase_id))
    primary = browser.get("last_primary")
    if isinstance(primary, dict):
        candidates.append((primary, "resume_task_state", current_phase))

    for primary, source, phase_id in candidates:
        fleet_id = str(
            primary.get("fleetId") or primary.get("fleet_id") or ""
        ).strip()
        page_id = str(
            primary.get("pageId") or primary.get("page_id") or ""
        ).strip()
        if not fleet_id:
            continue
        try:
            uuid.UUID(fleet_id)
            if page_id:
                uuid.UUID(page_id)
        except (ValueError, AttributeError):
            # This is a weak historical hint.  Skip malformed hand-edited
            # candidates and keep looking; never convert it into a hard pin.
            continue
        return {
            "fleet_id": fleet_id,
            "page_id": page_id,
            "phase_id": phase_id,
            "source": source,
        }
    return {}


def _new_run_id(*, resumed: bool) -> str:
    prefix = "resume" if resumed else "run"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}"


def _create_task_logger(runtime, *, task_id: Optional[str] = None) -> RunLogger:
    """Select storage before the logger decides whether it needs directories."""

    storage = create_storage_from_config(
        runtime.harness,
        worktree_dir=runtime.harness.worktree_dir,
        on_revision_conflict=lambda detail: logger.write("storage.revision_conflict", detail),
        on_verify=lambda report: logger.write("storage.dual_verify", report),
    )
    try:
        logger = RunLogger(
            runtime.harness.worktree_dir,
            task_id=task_id,
            on_event=ConsoleProgressReporter(),
            storage=storage,
        )
    except Exception:
        storage.close()
        raise
    return logger


def _open_task_storage(logger, runtime) -> None:
    """Attach the configured backend and open this launch's run row.

    Must run after ``logger.run_id`` is assigned: the run id is a relational
    field on every event and a foreign key in the database, so it has to exist
    before the first write rather than being backfilled.
    """

    def _report_revision_conflict(detail: JsonDict) -> None:
        # Raised outside the failed transaction. With the run lock holding other
        # processes off, this normally means two callbacks inside this process
        # raced - worth seeing, not worth failing over.
        logger.write("storage.revision_conflict", detail)

    def _report_verification(report: JsonDict) -> None:
        logger.write("storage.dual_verify", report)

    if logger.storage_attached:
        storage = logger.storage
    else:
        storage = create_storage_from_config(
            runtime.harness,
            worktree_dir=runtime.harness.worktree_dir,
            on_revision_conflict=_report_revision_conflict,
            on_verify=_report_verification,
        )
        logger.attach_storage(storage)
    try:
        storage.create_task(
            task_id=logger.task_id,
            harness_version=HARNESS_VERSION,
        )
        storage.start_run(
            task_id=logger.task_id,
            harness_version=HARNESS_VERSION,
            run_id=logger.run_id,
        )
    except Exception:
        # start_run can fail after the backend has opened file/db handles.
        # Release them here because run_cli has no successfully-started run to
        # finish in its normal cleanup path yet.
        try:
            storage.close()
        except Exception:
            pass
        raise


def _run_log_location(logger, runtime) -> str:
    if db_authoritative_for(logger):
        database = resolve_sqlite_path(
            runtime.harness.storage_sqlite_path, runtime.harness.worktree_dir)
        return f"SQLite {database}(task {logger.task_id})"
    return str(logger.path)


def _close_task_storage(
    logger, *, status: str, error: Optional[JsonDict] = None,
) -> List[str]:
    """Close the run row and, in dual mode, report whether the backends agree.

    Verification runs before the handles are released. Failures are returned
    to the process boundary so a host never mistakes incomplete persistence
    for a successful task, while the original task result remains available.
    """

    if not getattr(logger, "storage_attached", False):
        return []
    storage = logger.storage
    errors: List[str] = []
    try:
        storage.finish_run(
            task_id=logger.task_id, run_id=logger.run_id, status=status, error=error,
        )
        verify = getattr(storage, "verify", None)
        if callable(verify):
            verify(task_id=logger.task_id, run_id=logger.run_id)
    except Exception as exc:  # noqa: BLE001 - report after preserving result
        detail = f"finish/verify failed: {type(exc).__name__}: {exc}"
        errors.append(detail)
        _safe_logger_write(logger, "storage.close_failed", {"error": detail})
    finally:
        try:
            storage.close()
        except Exception as exc:  # noqa: BLE001 - surfaced as cleanup failure
            errors.append(f"close failed: {type(exc).__name__}: {exc}")
    return errors


def _artifact_row_count(path: Path) -> Optional[int]:
    """Small, bounded summary for the Browser resume bootstrap."""

    try:
        suffix = path.suffix.lower()
        if suffix in {".jsonl", ".ndjson"}:
            with path.open("r", encoding="utf-8") as stream:
                return sum(1 for line in stream if line.strip())
        if suffix == ".csv":
            with path.open("r", encoding="utf-8") as stream:
                lines = sum(1 for line in stream if line.strip())
            return max(0, lines - 1)
        if suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return len(payload)
            if isinstance(payload, dict):
                for key in ("rows", "items", "records", "results", "data"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        return len(value)
                return 1
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


_RESUME_PROJECTION_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_RESUME_PROJECTION_MAX_ROWS_PER_ARTIFACT = 500


def _resolve_resume_artifact_path(task_dir: Path, raw_path: Any) -> Optional[Path]:
    """Resolve one task-owned artifact without following a foreign pointer.

    Resume state is durable input, not trusted instruction.  In particular,
    an interrupted attempt can contain a temporary screenshot under /tmp or a
    manually edited path.  ResumeProjection only exposes extraction artifacts
    that still live below the task directory.
    """

    text = str(raw_path or "").strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = task_dir / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(task_dir.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _resume_artifact_rows(path: Path) -> "tuple[List[Dict[str, Any]], Optional[str]]":
    """Read a small structured-artifact projection, never values for prompt.

    The caller uses the rows only to derive stable ``controlKey`` coverage.
    Values remain in the artifact and must be read again by the continuation
    worker when it has a concrete need for them.  A size cap keeps a damaged or
    unexpectedly large artifact from making `/resume` slow.
    """

    try:
        if path.stat().st_size > _RESUME_PROJECTION_MAX_ARTIFACT_BYTES:
            return [], "artifact_too_large"
        suffix = path.suffix.lower()
        raw_rows: Any = []
        if suffix in {".jsonl", ".ndjson"}:
            rows: List[Any] = []
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    rows.append(json.loads(line))
                    if len(rows) >= _RESUME_PROJECTION_MAX_ROWS_PER_ARTIFACT:
                        break
            raw_rows = rows
        elif suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                raw_rows = payload
            elif isinstance(payload, dict):
                raw_rows = next(
                    (
                        payload[key]
                        for key in ("rows", "items", "records", "results", "data")
                        if isinstance(payload.get(key), list)
                    ),
                    [],
                )
        else:
            return [], "unsupported_artifact_format"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], "artifact_unreadable"

    if not isinstance(raw_rows, list):
        return [], "artifact_rows_not_array"
    return [
        row for row in raw_rows[:_RESUME_PROJECTION_MAX_ROWS_PER_ARTIFACT]
        if isinstance(row, dict)
    ], None


def _resume_required_controls(phase: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return canonical, display-safe control requirements from a phase."""

    expected = phase.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    raw_controls = expected.get("requiredControls")
    if raw_controls is None:
        raw_controls = expected.get("required_controls")
    if not isinstance(raw_controls, list):
        return []

    controls: List[Dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        control_key = str(
            raw.get("controlKey") or raw.get("control_key") or ""
        ).strip()
        if not control_key or control_key in seen:
            continue
        seen.add(control_key)
        item = {"controlKey": control_key}
        for key in ("label", "section"):
            value = str(raw.get(key) or "").strip()
            if value:
                item[key] = value
        controls.append(item)
    return controls


def _resume_artifact_references(
    task_dir: Path,
    raw_state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Collect credible and partial extraction pointers without mixing rows.

    ``validated_artifacts`` is the phase-level credited source.  Attempt
    validation artifacts are also useful to avoid redoing a filled form, but
    must remain explicitly uncredited unless that attempt itself completed.
    Screenshots and broad attempt digests are deliberately excluded.
    """

    references: Dict[str, Dict[str, Any]] = {}

    def add_paths(
        raw_paths: Any,
        *,
        credit_status: str,
        source: str,
        attempt_status: str = "",
    ) -> None:
        if not isinstance(raw_paths, list):
            return
        for raw_path in raw_paths:
            artifact_path = _resolve_resume_artifact_path(task_dir, raw_path)
            if artifact_path is None:
                continue
            key = str(artifact_path)
            current = references.get(key)
            if current is None:
                current = {
                    "path": key,
                    "rowCount": _artifact_row_count(artifact_path),
                    "creditStatus": credit_status,
                    "sources": [source],
                }
                if attempt_status:
                    current["attemptStatus"] = attempt_status
                references[key] = current
            else:
                sources = current.get("sources")
                if isinstance(sources, list) and source not in sources:
                    sources.append(source)
                # A phase-level validated path outranks any partial-attempt
                # pointer to the same immutable artifact.
                if credit_status == "credited":
                    current["creditStatus"] = "credited"
                    current.pop("attemptStatus", None)

    add_paths(
        raw_state.get("validated_artifacts"),
        credit_status="credited",
        source="phase_validated_artifacts",
    )
    attempts = raw_state.get("attempts")
    for attempt in attempts if isinstance(attempts, list) else []:
        if not isinstance(attempt, dict):
            continue
        validation = attempt.get("validation")
        validation = validation if isinstance(validation, dict) else {}
        attempt_status = str(attempt.get("status") or "").strip().lower()
        validation_done = str(validation.get("status") or "").strip().lower() == "done"
        credited = attempt_status in {"done", "validated_done"} and validation_done
        for field in ("artifacts", "validExtractionArtifacts"):
            add_paths(
                validation.get(field),
                credit_status="credited" if credited else "uncredited_partial",
                source=f"attempt_validation.{field}",
                attempt_status=attempt_status or "unknown",
            )

    return list(references.values())


def _resume_browser_context_projection(state: Dict[str, Any]) -> Dict[str, Any]:
    """Expose useful browser continuity facts without leaking reusable ids.

    Fleet/page UUIDs are coordinator-owned and do not belong in an LLM's plan.
    The prompt only needs to know that the next worker is task-pinned and what
    the last task-owned page looked like when it was recorded.
    """

    browser = state.get("browser_context")
    browser = browser if isinstance(browser, dict) else {}
    projection: Dict[str, Any] = {"taskSessionContinuity": "not_required"}
    binding = browser.get("task_session_binding")
    binding = binding if isinstance(binding, dict) else {}
    if binding:
        projection["taskSessionContinuity"] = "required"
        phase_id = str(binding.get("phaseId") or binding.get("phase_id") or "").strip()
        if phase_id:
            projection["bindingPhaseId"] = phase_id
        source = str(binding.get("source") or "").strip()
        if source:
            projection["bindingSource"] = source

    primary = browser.get("last_primary")
    primary = primary if isinstance(primary, dict) else {}
    fleet_id = str(primary.get("fleetId") or primary.get("fleet_id") or "").strip()
    page_id = str(primary.get("pageId") or primary.get("page_id") or "").strip()
    if not fleet_id or not page_id:
        return projection
    fleets = browser.get("fleets")
    fleet = fleets.get(fleet_id) if isinstance(fleets, dict) else None
    pages = fleet.get("pages") if isinstance(fleet, dict) else None
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            continue
        candidate_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        if candidate_id != page_id:
            continue
        recorded = {
            key: str(page.get(key) or "").strip()
            for key in ("url", "title", "status")
            if str(page.get(key) or "").strip()
        }
        if recorded:
            projection["lastRecordedPage"] = recorded
        break
    return projection


def _resume_projection(
    task_dir: Path,
    plan: Dict[str, Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a read-only recovery map from durable state and artifacts.

    It intentionally does not change phase status, copy filled values into
    task state, or convert partial work into completion.  The runtime gets stable
    control identities and artifact pointers; a continuation must re-perceive
    the live page and re-read any value it needs from the cited artifact.
    """

    phase_states = state.get("phases")
    phase_states = phase_states if isinstance(phase_states, dict) else {}
    phases: List[Dict[str, Any]] = []
    for phase in plan.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "").strip()
        if not phase_id:
            continue
        raw_state = phase_states.get(phase_id)
        raw_state = raw_state if isinstance(raw_state, dict) else {}
        references = _resume_artifact_references(task_dir, raw_state)
        required_controls = _resume_required_controls(phase)
        required_keys = {item["controlKey"] for item in required_controls}
        credited_keys: set[str] = set()
        partial_keys: set[str] = set()
        legacy_control_labels: set[str] = set()
        for reference in references:
            path = Path(str(reference["path"]))
            rows, issue = _resume_artifact_rows(path)
            if issue:
                reference["projectionRead"] = issue
                continue
            observed = {
                str(row.get("controlKey") or "").strip()
                for row in rows
                if str(row.get("controlKey") or "").strip()
            }
            if observed:
                reference["controlKeys"] = sorted(observed)
            if not required_controls:
                legacy_labels = {
                    str(row.get("controlLabel") or row.get("label") or "").strip()
                    for row in rows
                    if str(row.get("controlLabel") or row.get("label") or "").strip()
                }
                if legacy_labels:
                    reference["legacyControlLabels"] = sorted(legacy_labels)
                    legacy_control_labels.update(legacy_labels)
            if reference.get("creditStatus") == "credited":
                credited_keys.update(observed)
            else:
                partial_keys.update(observed)

        item: Dict[str, Any] = {
            "phaseId": phase_id,
            "phaseStatus": str(raw_state.get("status") or "pending"),
            "artifactRefs": references,
        }
        if required_controls:
            observed_keys = credited_keys | partial_keys
            item.update({
                "requiredControls": required_controls,
                "creditedControlKeys": sorted(credited_keys & required_keys),
                "partialObservedControlKeys": sorted(
                    (partial_keys - credited_keys) & required_keys
                ),
                "notYetObservedControls": [
                    control for control in required_controls
                    if control["controlKey"] not in observed_keys
                ],
            })
            unexpected = observed_keys - required_keys
            if unexpected:
                item["unexpectedObservedControlKeys"] = sorted(unexpected)
        elif references:
            item["controlCoverage"] = "not_declared_by_legacy_contract"
            if legacy_control_labels:
                item["legacyObservedControlLabels"] = sorted(
                    legacy_control_labels
                )
        phases.append(item)

    return {
        "version": "v1",
        "source": "recomputed_from_resume_state_and_task_owned_artifacts",
        "valueHandling": "read_values_from_artifact_on_demand; values_are_not_copied_into_resume_state",
        "browserContext": _resume_browser_context_projection(state),
        "phases": phases,
    }


def _pending_human_interventions(
    run_jsonl: Path,
    phase_ids: List[str],
    *,
    tail_bytes: int = 524_288,
) -> List[Dict[str, Any]]:
    """Latest pending human decision per phase, from the run log tail.

    Resume reopens HITL-terminal phases, and the operator deserves to see
    WHICH decision is pending (browser challenge vs local-file authorization,
    and the exact path) before the workers start asking again.  Only the last
    ``tail_bytes`` are scanned: these receipts are written at worker exit, so
    they live at the end of the log.
    """
    wanted = [str(item) for item in phase_ids if str(item)]
    if not wanted or not run_jsonl.is_file():
        return []
    try:
        size = run_jsonl.stat().st_size
        with run_jsonl.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()  # drop the partial first line
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    found: Dict[str, Dict[str, Any]] = {}
    seen: set = set()
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or str(event.get("type") or "") != "spawner.browser.result":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        phase_id = str(payload.get("phaseId") or "")
        if phase_id not in wanted or phase_id in seen:
            continue
        seen.add(phase_id)
        if (
            str(payload.get("status") or "") in {
                "hitl_required", "hitl_waiting", "hitl_timeout",
                "blocked_by_challenge",
            }
        ):
            found[phase_id] = {
                "phaseId": phase_id,
                "status": str(payload.get("status") or ""),
                "message": str(payload.get("answer") or "")[:400],
                "source": "historical_worker_claim_unverified",
                "requiresLiveVerification": True,
            }
        if len(seen) == len(set(wanted)):
            break
    return [found[phase_id] for phase_id in wanted if phase_id in found]


def _operator_input_receipts(
    run_jsonl: Path,
    *,
    storage: Any = None,
    task_id: str = "",
    max_inline: int = 32,
) -> Dict[str, Any]:
    """Project durable human text into resume context without inferring consent.

    The full ordered record remains in the run log. The bounded prompt copy
    lets the runtime see recent answers, their request identity and classification;
    older inputs remain available at the source path for a concrete question.
    """
    result: Dict[str, Any] = {
        "source": str(run_jsonl), "total": 0, "recent": [], "truncated": False,
    }
    records: Dict[str, Dict[str, Any]] = {}

    def events():
        if storage is not None and task_id:
            after_id = 0
            while True:
                page = storage.read_events(
                    task_id=task_id, after_event_id=after_id, limit=1000,
                )
                if not page:
                    return
                for row in page:
                    kind = row.get("event_type")
                    if kind not in {
                        "task_plan.approval_input_received",
                        "task_plan.approval_input_classified",
                        "hitl.feedback_received",
                    }:
                        continue
                    payload = row.get("payload_json")
                    resource_id = row.get("payload_resource_id")
                    if payload is None and resource_id:
                        from harness.storage.sqlite_store import build_resource_uri
                        resource = storage.read_resource(
                            current_task_id=task_id,
                            resource_uri=build_resource_uri(task_id, str(resource_id)),
                        )
                        payload = (resource or {}).get("content_json")
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            payload = None
                    yield {
                        "type": kind,
                        "payload": payload,
                        "ts": row.get("event_time"),
                        "eventUid": row.get("event_uid"),
                    }
                after_id = int(page[-1]["event_id"])
        elif run_jsonl.is_file():
            with run_jsonl.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if ("task_plan.approval_input_" not in line
                            and "hitl.feedback_received" not in line):
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    try:
        for event in events():
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type") or "")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "task_plan.approval_input_received":
                input_id = str(payload.get("inputId") or event.get("eventUid") or "")
                records[input_id] = {
                    "kind": "plan_approval", "inputId": input_id,
                    "candidateHash": payload.get("candidateHash"),
                    "text": payload.get("text"),
                    "receivedAt": payload.get("receivedAt") or event.get("ts"),
                    "decision": payload.get("decision"),
                }
            elif kind == "task_plan.approval_input_classified":
                input_id = str(payload.get("inputId") or "")
                if input_id in records:
                    records[input_id].update({
                        "decision": payload.get("decision"),
                        "errorCode": payload.get("errorCode"),
                    })
            elif kind == "hitl.feedback_received":
                input_id = str(payload.get("feedbackId") or event.get("eventUid") or "")
                records[input_id] = {
                    "kind": "hitl_feedback", "inputId": input_id,
                    "pageId": payload.get("pageId"),
                    "pauseId": payload.get("pauseId"),
                    "assistanceKind": payload.get("assistanceKind"),
                    "requestPurpose": payload.get("requestPurpose"),
                    "text": payload.get("text"),
                    "receivedAt": event.get("ts"),
                }
    except Exception as exc:
        result["readError"] = type(exc).__name__
    result["total"] = len(records)
    result["recent"] = list(records.values())[-max_inline:]
    result["truncated"] = len(records) > max_inline
    return result


def _resume_phase_summary(
    task_dir: Path,
    plan: Dict[str, Any],
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    phase_states = state.get("phases")
    phase_states = phase_states if isinstance(phase_states, dict) else {}
    summary: List[Dict[str, Any]] = []
    for phase in plan.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        phase_id = str(phase.get("id") or "")
        raw_state = phase_states.get(phase_id)
        raw_state = raw_state if isinstance(raw_state, dict) else {}
        artifacts: List[Dict[str, Any]] = []
        for raw_path in raw_state.get("validated_artifacts") or []:
            artifact_path = Path(str(raw_path or "")).expanduser()
            if not artifact_path.is_absolute():
                artifact_path = task_dir / artifact_path
            resolved = artifact_path.resolve(strict=False)
            artifacts.append({
                "path": str(resolved),
                "rows": _artifact_row_count(resolved) if resolved.is_file() else None,
            })
        item: Dict[str, Any] = {
            "phaseId": phase_id,
            "status": str(raw_state.get("status") or "pending"),
            "validatedArtifacts": artifacts,
        }
        if raw_state.get("resume_reset_reason"):
            item["resumeResetReason"] = raw_state.get("resume_reset_reason")
        if raw_state.get("resume_reset_from"):
            item["resumeResetFrom"] = raw_state.get("resume_reset_from")
        summary.append(item)
    return summary


def _interrupted_phase_ids(report: Dict[str, Any]) -> List[str]:
    phase_ids = {
        str(item.get("phaseId") or "")
        for item in report.get("interruptedAttempts") or []
        if isinstance(item, dict) and str(item.get("phaseId") or "")
    }
    phase_ids.update(
        str(item)
        for key in ("resetRunningPhases", "hitlReactivatedPhases")
        for item in report.get(key) or []
        if str(item)
    )
    return sorted(phase_ids)


def _confirm_interrupted_replay(
    report: Dict[str, Any],
    *,
    explicitly_allowed: bool,
) -> bool:
    phase_ids = _interrupted_phase_ids(report)
    if not phase_ids or explicitly_allowed:
        return True
    joined = ", ".join(phase_ids)
    warning = (
        f"Phases [{joined}] were interrupted or stopped for human input in the previous run. "
        "Worker steps/messages are not restored; these phases will restart from the beginning. "
        "Previous operations may already have external effects."
    )
    print(warning, flush=True)
    if not sys.stdin.isatty():
        print(
            "Replay is denied in non-interactive mode; after reviewing, pass "
            "--resume-retry-interrupted. ",
            flush=True,
        )
        return False
    answer = input("Allow these phases to restart from the beginning? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


_BROWSER_MODE_STATUS_LABELS = {
    "done": "completed",
    "completed": "completed",
    "validated_done": "completed (validated)",
    "partial": "partially completed",
    "incomplete": "incomplete",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _print_browser_mode_summary(result: JsonDict, task_dir: str, *, logger=None) -> None:
    """Human summary printed before the machine receipt of browser mode.

    The browser-mode CLI result is a machine receipt (an embedding host parses
    it); a human watching the terminal used to see only that JSON with the
    actual answer text buried inside string escapes (run a686e03f). Conclusion
    first, then the answer text, then the concrete next step. Everything
    printed is taken mechanically from the receipt — no new claims.
    """
    if not isinstance(result, dict):
        return
    status = str(result.get("status") or "").strip().lower()
    label = _BROWSER_MODE_STATUS_LABELS.get(status, f"status {status or 'unknown'}")
    worker_status = ""
    direct_exec = result.get("directExecution")
    if isinstance(direct_exec, dict):
        worker_status = str(direct_exec.get("workerStatus") or "").strip()
    header = f"Browser task result: {label}"
    if worker_status and worker_status not in {status, "done"}:
        header += f"(worker terminal status: {worker_status})"
    print(f"\n══ {header} ══", flush=True)
    answer = str(result.get("answer") or "").strip()
    if answer:
        try:
            parsed = json.loads(answer)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and (parsed.get("blockers") or parsed.get("next_steps")):
            # The synthesized fallback receipt: unwrap its human fields
            # instead of printing one escaped JSON line.
            for key, label in (("blockers", "Blockers"), ("next_steps", "Next steps")):
                items = parsed.get(key) or []
                for item in items[:4]:
                    if isinstance(item, dict):
                        item = item.get("detail") or item.get("reason") or item.get("message") or item.get("type") or "See full receipt"
                    print(f"{label}: {str(item)[:400]}", flush=True)
                if len(items) > 4:
                    print(f"{len(items) - 4} more items in the full receipt.", flush=True)
        elif isinstance(parsed, dict) and parsed.get("evidence"):
            for path in parsed.get("evidence") or []:
                if isinstance(path, str) and (path.startswith(("/", "https://", "http://"))):
                    print(f"Artifact: {path}", flush=True)
        elif isinstance(parsed, dict):
            if parsed.get("summary"):
                print(str(parsed["summary"]), flush=True)
        else:
            print(answer, flush=True)
    delivery = result.get("artifactDelivery")
    if isinstance(delivery, dict) and delivery.get("status") == "failed":
        print(
            "Artifact export failed: some extracted paths cannot be opened directly; "
            "data remains in the task database."
            f" Reason: {delivery.get('error')}",
            flush=True,
        )
    error = result.get("error")
    reason = error.get("message") if isinstance(error, dict) else error
    reason = str(reason or result.get("reason") or "").strip()
    recovery = result.get("connectionRecovery")
    if not reason and isinstance(recovery, dict) and recovery.get("status") == "blocked":
        reason = str(recovery.get("reasonCode") or recovery.get("reason") or "Transport recovery probe failed")
        if recovery.get("probeStage"):
            reason += f" (probe stage: {recovery['probeStage']})"
    if reason:
        # A worker may return an answer alongside a failure. Show both instead
        # of hiding the failure reason inside the following machine receipt.
        print(f"Reason: {reason}", flush=True)
        for item in result.get("errors") or []:
            print(f"  · {item}", flush=True)
        review = result.get("review")
        verdict = review.get("verdict") if isinstance(review, dict) else None
        if isinstance(verdict, dict):
            for finding in verdict.get("findings") or []:
                if isinstance(finding, dict) and finding.get("blocking"):
                    print(f"  · {finding.get('reason') or finding}", flush=True)
        if isinstance(review, dict) and review.get("auditPath"):
                print(f"Review audit: {review['auditPath']}", flush=True)
    if result.get("receiptPath"):
        print(f"Full receipt: {result['receiptPath']}", flush=True)
    if status in {"done", "completed", "validated_done"}:
        return
    # Resume reads task_plan.json. A run that died before a plan was accepted
    # has none, and pointing the user at resume there only produces a second,
    # different error (verified on run c7c931b7: ResumeStateError, "task plan
    # is missing").
    plan_path = Path(task_dir) / "task_plan.json" if task_dir else None
    from harness.utils import task_file_exists
    plan_exists = (task_file_exists(logger, "task_plan.json") if logger is not None
                   else plan_path is not None and plan_path.is_file())
    if plan_exists:
        print(
            "\nContinue: python main.py --config config.json"
            f"\nThen enter /browser, followed by /resume {getattr(logger, 'task_id', '') or Path(task_dir).name}",
            flush=True,
        )
    else:
        print(
            "\nNo saved plan is available for /resume."
            "\nRevise the task using the reason above, then start a new run.",
            flush=True,
        )


async def _run_cli_impl(args: argparse.Namespace) -> int:
    global _CANCELLED_LOGGED, _LAST_LOGGER

    _CANCELLED_LOGGED = False
    _LAST_LOGGER = None
    runtime = load_runtime_config(args.config)
    configured_mode = str(getattr(runtime.harness, "agent_mode", "browser") or "browser").strip().lower()
    if configured_mode not in _AGENT_MODE_LABELS:
        print("config.json harness.agent_mode must be browser or workflow.")
        return CLI_INPUT_ERROR_EXIT_CODE
    args.configured_agent_mode = configured_mode
    # Resume helpers run before a logger exists; hand them the configuration
    # that was actually parsed rather than letting them re-guess it.
    configure_resume_storage(
        backend=runtime.harness.storage_backend,
        worktree_dir=runtime.harness.worktree_dir,
        sqlite_path=runtime.harness.storage_sqlite_path,
    )
    if args.agent_id:
        runtime.agent_id = args.agent_id
    if args.max_steps is not None:
        print("--max-steps is deprecated and ignored.")
    task = read_task(args)
    resume_requested = bool(str(getattr(args, "resume", "") or "").strip())
    if not resume_requested and _is_workflow_command(task):
        from harness.workflow_mode.cli import run_command
        return await run_command(runtime, args, task, host=sys.modules[__name__])
    requested_mode = str(getattr(args, "agent_mode", "") or "").strip().lower()
    if requested_mode:
        if requested_mode not in _AGENT_MODE_LABELS:
            print("Interactive mode must be /browser or a /workflow command.")
            return CLI_INPUT_ERROR_EXIT_CODE
        runtime.harness.agent_mode = requested_mode
    agent_mode = str(getattr(runtime.harness, "agent_mode", "browser") or "browser").strip().lower()
    if agent_mode == "workflow" and not resume_requested:
        print("Workflow tasks start with a /workflow command; plain tasks run in /browser.")
        return CLI_INPUT_ERROR_EXIT_CODE
    if not task and not resume_requested:
        print("No task received.")
        return 2

    if resume_requested:
        directory = _resolve_resume_directory(args.resume)
        runtime.harness.worktree_dir = str(directory.parent)
        configure_resume_storage(backend=runtime.harness.storage_backend,
                                 worktree_dir=runtime.harness.worktree_dir,
                                 sqlite_path=runtime.harness.storage_sqlite_path)
        resume_manifest = load_task_manifest(directory)
        from harness.workflow_mode.run_policy import completion_resume_error
        retired = completion_resume_error(resume_manifest)
        if retired:
            print(retired)
            return CLI_INPUT_ERROR_EXIT_CODE
        recorded_mode = str(
            ((resume_manifest or {}).get("startup_args") or {}).get("agent_mode") or ""
        ).strip().lower()
        if recorded_mode and recorded_mode not in _AGENT_MODE_LABELS:
            print(
                f"Saved task mode '{recorded_mode}' is no longer supported; this task cannot be resumed. "
                "Its saved records remain available. Start a new task in a supported mode."
            )
            return CLI_INPUT_ERROR_EXIT_CODE
        if recorded_mode in _AGENT_MODE_LABELS:
            args.agent_mode = recorded_mode
            agent_mode = runtime.harness.agent_mode = recorded_mode
    if agent_mode == "workflow":
        from harness.workflow_mode.cli import resume_task
        return await resume_task(runtime, args, task, host=sys.modules[__name__])

    logger: Optional[RunLogger] = None
    harness: Optional[BrowserTaskRunner] = None
    run_lock: Optional[RunLock] = None
    resume_context: Optional[ResumeContext] = None
    task_for_agent = task
    run_started = False
    run_status = "interrupted"
    run_error: Optional[JsonDict] = None
    answer = ""
    exit_code = 0
    cleanup_errors: List[str] = []
    try:
        if resume_requested:
            try:
                task_dir = _resolve_resume_directory(args.resume)
                runtime.harness.worktree_dir = str(task_dir.parent)
                configure_resume_storage(
                    backend=runtime.harness.storage_backend,
                    worktree_dir=runtime.harness.worktree_dir,
                    sqlite_path=runtime.harness.storage_sqlite_path,
                )
                # The lock is acquired before reading the generation so a live
                # process cannot change plan/state between validation and use.
                run_lock = acquire_run_lock(task_dir)
                promote_legacy_task_to_database(task_dir)
                current_plan = load_task_plan_strict(task_dir)
                prior_state = load_task_state_strict(task_dir)
                manifest = load_task_manifest(task_dir)
                if manifest is not None:
                    runtime.harness.worktree_dir = str(task_dir.parent)
                _validate_resume_mode(manifest, current_plan, agent_mode)
                current_plan, plan_alias_recovery = reconcile_torn_plan_alias(
                    task_dir,
                    current_plan=current_plan,
                    state=prior_state,
                )

                manifest = load_task_manifest(task_dir)
                if manifest is not None:
                    original_user_task = str(
                        manifest.get("original_user_task") or ""
                    ).strip()
                    if not original_user_task:
                        raise ResumeStateError(
                            "task_manifest.json is missing original_user_task"
                        )
                else:
                    original_user_task = recover_legacy_user_task(task_dir)
                    if not original_user_task and sys.stdin.isatty():
                        print(
                            "Legacy worktree: the original task could not be recovered.",
                            flush=True,
                        )
                        original_user_task = input(
                            "Restate the complete original task: "
                        ).strip()
                    if not original_user_task:
                        raise ResumeStateError(
                            "Legacy worktree has no manifest and its historical context "
                            "cannot recover the original task; restate it in interactive mode"
                        )

                if str(task or "").strip():
                    original_fleet, original_fleet_error = extract_fleet_reference(
                        original_user_task)
                    amendment_fleet, amendment_fleet_error = extract_fleet_reference(task)
                    if original_fleet_error or amendment_fleet_error:
                        raise ResumeStateError(original_fleet_error or amendment_fleet_error)
                    if amendment_fleet and not (
                        original_fleet and (
                            original_fleet.startswith(amendment_fleet)
                            or amendment_fleet.startswith(original_fleet)
                        )
                    ):
                        raise ResumeStateError(
                            "Additional instructions cannot rebind a resumed task to another @Fleet; "
                            "use the original Fleet or create a new task"
                        )

                initial_plan, initial_plan_recovered = _load_initial_plan(
                    task_dir, current_plan,
                )
                runtime.harness.worktree_dir = str(task_dir.parent)
                logger = _create_task_logger(runtime, task_id=task_dir.name)
                logger.run_id = _new_run_id(resumed=True)
                logger.context_run_id = logger.run_id
                logger.resumed_from = str(task_dir.resolve())
                _open_task_storage(logger, runtime)
                run_started = True

                report = prepare_resume_state(
                    logger,
                    old_plan=current_plan,
                    instruction=task,
                    persist=False,
                )
                if not _confirm_interrupted_replay(
                    report,
                    explicitly_allowed=bool(
                        getattr(args, "resume_retry_interrupted", False)
                    ),
                ):
                    run_status = "cancelled"
                    return 2

                reconciled_state = report["state"]
                write_task_state(logger, reconciled_state)
                if str(task or "").strip():
                    from harness.planning.context import retain_operator_inputs
                    input_record = {
                        "inputId": f"resume:{logger.run_id}",
                        "source": "resume_cli", "taskId": logger.task_id,
                        "runId": logger.run_id, "text": task,
                        "receivedAt": datetime.now(timezone.utc).isoformat(),
                    }
                    retain_operator_inputs(logger, [input_record])
                    logger.write("resume.instruction_received", input_record)
                if manifest is None:
                    write_task_manifest(
                        logger,
                        original_user_task=original_user_task,
                        task_id=logger.task_id,
                        config_path=args.config,
                        agent_id=runtime.agent_id,
                    )

                fleet_reuse_enabled = bool(
                    getattr(runtime.harness, "fleet_reuse_enabled", True)
                )
                browser_hint = (
                    _resume_browser_hint(
                        reconciled_state,
                        preferred_phase_ids=_interrupted_phase_ids(report),
                    )
                    if fleet_reuse_enabled else {}
                )
                prompt_report = {
                    key: value
                    for key, value in report.items()
                    if key not in {"state", "instruction"}
                }
                prompt_report["phaseStates"] = _resume_phase_summary(
                    task_dir, current_plan, reconciled_state,
                )
                resume_projection = _resume_projection(
                    task_dir, current_plan, reconciled_state,
                )
                prompt_report["resumeProjection"] = resume_projection
                prompt_report["pendingHumanInterventions"] = (
                    _pending_human_interventions(
                        task_dir / "run.jsonl",
                        [
                            str(item)
                            for item in report.get("hitlReactivatedPhases") or []
                            if str(item)
                        ],
                    )
                )
                prompt_report["operatorInputReceipts"] = (
                    _operator_input_receipts(
                        task_dir / "run.jsonl",
                        storage=logger.storage if db_authoritative_for(logger) else None,
                        task_id=logger.task_id,
                    )
                )
                from harness.planning.context import user_context
                prompt_report["operatorInputs"] = user_context(
                    logger, original_user_task)["operatorInputs"]
                prompt_report["browserRecovery"] = {
                    "candidateRecorded": bool(browser_hint),
                    "status": (
                        "pending_live_probe_on_next_worker"
                        if browser_hint
                        else "disabled_by_config"
                        if not fleet_reuse_enabled
                        else "no_task_owned_candidate"
                    ),
                    "fallback": "ordinary_assignment_and_phase_replay",
                }
                if plan_alias_recovery is not None:
                    prompt_report["planAliasRecovery"] = plan_alias_recovery
                resume_context = ResumeContext(
                    original_user_task=original_user_task,
                    current_plan=current_plan,
                    initial_plan=initial_plan,
                    initial_plan_recovered=initial_plan_recovered,
                    instruction=task,
                    report=prompt_report,
                    run_id=logger.run_id,
                    browser_hint=browser_hint,
                    task_dir=str(task_dir),
                )
                task_for_agent = original_user_task
                runtime.harness.runs_dir = str(task_dir)
                logger.write(
                    "resume.started",
                    {
                        "runId": logger.run_id,
                        "taskDir": str(task_dir),
                        "instruction": task or None,
                        "resetPhases": report.get("resetPhases") or [],
                        "interruptedPhases": _interrupted_phase_ids(report),
                        "browserCandidateRecorded": bool(browser_hint),
                        "initialPlanRecovered": initial_plan_recovered,
                        "planAliasRecovery": plan_alias_recovery,
                    },
                )
                projection_phases = resume_projection.get("phases")
                projection_phases = (
                    projection_phases
                    if isinstance(projection_phases, list) else []
                )
                logger.write(
                    "resume.projection_built",
                    {
                        "phaseCount": len(projection_phases),
                        "artifactRefCount": sum(
                            len(item.get("artifactRefs") or [])
                            for item in projection_phases
                            if isinstance(item, dict)
                        ),
                        "partialArtifactRefCount": sum(
                            1
                            for item in projection_phases
                            if isinstance(item, dict)
                            for reference in item.get("artifactRefs") or []
                            if isinstance(reference, dict)
                            and reference.get("creditStatus")
                            == "uncredited_partial"
                        ),
                        "taskSessionContinuity": (
                            resume_projection.get("browserContext") or {}
                        ).get("taskSessionContinuity"),
                    },
                )
            except (ResumeStateError, RunLockError, ValueError, OSError) as exc:
                run_status = "failed"
                print(f"Cannot resume task: {exc}", flush=True)
                return 2
        else:
            logger = _create_task_logger(runtime)
            logger.run_id = _new_run_id(resumed=False)
            run_lock = acquire_run_lock(logger.task_dir)
            _open_task_storage(logger, runtime)
            run_started = True
            write_task_manifest(
                logger,
                original_user_task=task,
                task_id=logger.task_id,
                config_path=args.config,
                agent_id=runtime.agent_id,
                startup_args={
                    "max_steps": getattr(args, "max_steps", None),
                    "agent_mode": agent_mode,
                },
            )
            runtime.harness.runs_dir = str(logger.task_dir)

        if logger is None:  # defensive; both setup branches assign it
            print("Cannot initialize task logging.", flush=True)
            return 2
        _LAST_LOGGER = logger
        route_task = (
            str(resume_context.original_user_task or "")
            if resume_context is not None
            else task
        )
        task_fleet_reference, fleet_reference_error = extract_fleet_reference(
            route_task
        )
        if fleet_reference_error:
            logger.write("task.fleet_reference.rejected", {
                "error": fleet_reference_error,
                "source": "original_user_task",
            })
            print(f"Invalid Fleet reference: {fleet_reference_error}", flush=True)
            return CLI_INPUT_ERROR_EXIT_CODE
        if task_fleet_reference and not bool(
            getattr(runtime.harness, "fleet_reuse_enabled", True)
        ):
            message = (
                "The task references @Fleet but harness.fleet_reuse_enabled=false; "
                "cannot bind an existing Fleet."
            )
            logger.write("task.fleet_reference.rejected", {
                "fleetReference": task_fleet_reference,
                "error": message,
                "source": "original_user_task",
            })
            print(message, flush=True)
            return CLI_INPUT_ERROR_EXIT_CODE
        if task_fleet_reference:
            logger.write("task.fleet_reference.bound", {
                "fleetReference": task_fleet_reference,
                "source": "original_user_task",
            })
        if resume_context is not None:
            phase_states = resume_context.report.get("phaseStates") or []
            kept = sum(
                1 for item in phase_states
                if isinstance(item, dict)
                and item.get("status") == "validated_done"
            )
            pending_ids = [
                str(item.get("phaseId") or "")
                for item in phase_states
                if isinstance(item, dict)
                and item.get("status") not in TERMINAL_PHASE_STATUSES
                and str(item.get("phaseId") or "")
            ]
            reset = resume_context.report.get("resetPhases") or []
            hitl_reactivated = [
                str(item)
                for item in resume_context.report.get("hitlReactivatedPhases") or []
                if str(item)
            ]
            print(f"Task resumed: {logger.task_id}", flush=True)
            print(
                f"Resume summary: retained {kept} validated phases; "
                f"{len(pending_ids)} phases to continue/retry: "
                f"{', '.join(pending_ids) or '(none)'}; "
                f"{len(reset)} phases reset after interruption or invalid artifacts: "
                f"{', '.join(map(str, reset)) or '(none)'}",
                flush=True,
            )
            if hitl_reactivated:
                print(
                    f"Reactivated {len(hitl_reactivated)} human-interrupted "
                    f"(HITL/local file authorization) phases: "
                    f"{', '.join(hitl_reactivated)}; the relevant operations will request "
                    "approval again in the terminal.",
                    flush=True,
                )
                interventions = resume_context.report.get(
                    "pendingHumanInterventions"
                ) or []
                for item in interventions:
                    if isinstance(item, dict) and item.get("message"):
                        print(
                            f"  · {item.get('phaseId')}: {item.get('message')}",
                            flush=True,
                        )
            browser_status = resume_context.report.get("browserRecovery") or {}
            if browser_status.get("candidateRecorded"):
                print(
                    "Browser recovery: historical Fleet/Page candidates found (availability not yet verified); "
                    "probe and restore the session once connected. "
                    "This record does not confirm WebCross Dispatcher availability.",
                    flush=True,
                )
            elif browser_status.get("status") == "disabled_by_config":
                print(
                    "Browser recovery: fleet_reuse_enabled=false; "
                    "using a new browser context instead of restoring the original Fleet.",
                    flush=True,
                )
            else:
                print(
                    "Browser recovery: no task-specific candidate found; allocating a browser; "
                    "sign in again if needed.",
                    flush=True,
                )
        else:
            print(f"Task created: {logger.task_id}", flush=True)
        print(f"Mode: {agent_mode}", flush=True)
        print(f"Task directory: {logger.task_dir}", flush=True)
        print(f"Run log: {_run_log_location(logger, runtime)}", flush=True)
        if resume_context is not None:
            label = "Continue with additional instructions" if resume_context.instruction else "Resume the saved plan"
            print(f"{label}: check the browser connection before model calls or worker dispatch.", flush=True)
        print("Browser mode: start BrowserAgent directly and maintain the task checklist during execution.", flush=True)

        harness = BrowserTaskRunner(
            runtime, logger, resume=resume_context,
            task_fleet_reference=task_fleet_reference or "",
        )
        browser_result = await _run_browser_mode(
            harness,
            task=task_for_agent,
            original_task=task,
            resume_context=resume_context,
        )
        # Browser receipts still use savedPath for extraction evidence.
        # In DB mode those paths are virtual during execution; export only
        # the final receipt's extraction artifacts so a user can open the
        # paths it presents without mirroring task logs to the worktree.
        from harness.evidence.extraction_artifacts import (
            export_extraction_artifacts_for_delivery,
        )
        try:
            delivered = export_extraction_artifacts_for_delivery(
                logger, browser_result.get("artifacts") or [])
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            browser_result["artifactDelivery"] = {
                "status": "failed", "error": detail,
            }
            logger.write("artifact.delivery_failed", {"error": detail})
        else:
            if delivered:
                browser_result["artifactDelivery"] = {
                    "status": "done", "paths": delivered,
                }
                logger.write("artifact.delivery_exported", {
                    "count": len(delivered), "paths": delivered,
                })
        result_resource = logger.storage.save_resource(
            task_id=logger.task_id, run_id=str(logger.run_id or ""),
            resource_type="browser_task_result", logical_path="artifacts/browser-result.json",
            media_type="application/json", content=json.dumps(browser_result, ensure_ascii=False, default=str),
        )
        output_mode = getattr(args, "output", "auto")
        human_output = output_mode == "text" or output_mode == "auto" and sys.stdout.isatty()
        if human_output or output_mode == "auto":
            _print_browser_mode_summary(
                {**browser_result, "receiptPath": result_resource.get("saved_path") or
                    f"Database task {logger.task_id} / artifacts/browser-result.json"},
                str(logger.task_dir or ""),
                logger=logger,
            )
        if human_output:
            answer = ""
        else:
            answer = json.dumps(browser_result, ensure_ascii=False, default=str)
        result_status = str(browser_result.get("status") or "failed").lower()
        run_error = _browser_mode_terminal_error(browser_result)
        run_status = (
            "completed"
            if result_status in {"done", "completed", "validated_done"}
            else "cancelled" if result_status == "cancelled"
            else "failed"
        )
        if run_status == "cancelled":
            exit_code = CLI_CANCELLED_EXIT_CODE
        elif run_status != "completed":
            exit_code = CLI_ERROR_EXIT_CODE
        logger.write("direct_mode.final", {
            "status": result_status,
            "workerId": browser_result.get("workerId"),
            "phaseId": browser_result.get("phaseId"),
            "directExecution": browser_result.get("directExecution"),
            "validatedStatus": browser_result.get("validatedStatus"),
            "error": run_error,
        })
    except asyncio.CancelledError as exc:
        run_status = "cancelled"
        exit_code = CLI_CANCELLED_EXIT_CODE
        _CANCELLED_LOGGED = True
        answer = json.dumps(
            _cancelled_cli_result(str(exc) or "Task execution was cancelled."),
            ensure_ascii=False,
        )
        run_error = json.loads(answer)["error"]
        _safe_logger_write(
            logger,
            "run.cancelled",
            exception_payload(exc, mode=agent_mode, task=task_for_agent),
        )
    except Exception as exc:
        run_status = "failed"
        failure, exit_code = _classify_cli_exception(exc, phase="run")
        run_error = failure.get("error") or failure
        answer = json.dumps(failure, ensure_ascii=False, default=str)
        event_type = (
            "run.rate_limited"
            if isinstance(exc, LLMRateLimitError)
            else "run.error"
        )
        _safe_logger_write(
            logger,
            event_type,
            {
                **exception_payload(exc, mode=agent_mode, task=task_for_agent),
                "terminal": failure.get("error"),
            },
        )
    finally:
        # Join all workers before closing the audit store.
        if agent_mode == "browser" and harness is not None:
            try:
                cancelled = await _shutdown_browser_mode(harness)
                if cancelled and exit_code == 0:
                    run_status = "cancelled"
                    exit_code = CLI_CANCELLED_EXIT_CODE
                    failure = _cancelled_cli_result("Cancelled during browser cleanup.")
                    run_error = failure["error"]
                    answer = json.dumps(failure, ensure_ascii=False)
                    _safe_logger_write(logger, "run.cancelled", run_error)
            except (Exception, asyncio.CancelledError) as exc:
                cleanup_errors.append(f"browser shutdown failed: {type(exc).__name__}: {exc}")
        if logger is not None and run_started:
            try:
                logger.write_usage_summary()
            except Exception as exc:  # noqa: BLE001 - preserve primary result
                cleanup_errors.append(
                    f"usage summary failed: {type(exc).__name__}: {exc}"
                )
            if cleanup_errors:
                _safe_logger_write(logger, "run.cleanup_failed", {"errors": cleanup_errors})
                if exit_code == 0:
                    run_status = "failed"
                    exit_code = CLI_IO_FAILURE_EXIT_CODE
                if run_error is None:
                    run_error = {"code": "cleanup_error", "message": "; ".join(cleanup_errors)}
            _safe_logger_write(logger, "run.finished", {
                "status": run_status, "exitCode": exit_code, "error": run_error,
            })
            cleanup_errors.extend(
                _close_task_storage(logger, status=run_status, error=run_error)
            )
        elif logger is not None and logger.storage_attached:
            # Storage is selected before lock acquisition now; release handles
            # even when startup fails before a run row can be opened.
            try:
                logger.storage.close()
            except Exception as exc:
                cleanup_errors.append(f"storage close failed: {type(exc).__name__}: {exc}")
        if run_lock is not None:
            try:
                released = release_run_lock(run_lock)
            except Exception as exc:  # noqa: BLE001 - report at boundary
                cleanup_errors.append(
                    f"run lock release failed: {type(exc).__name__}: {exc}"
                )
            else:
                if not released:
                    cleanup_errors.append("run lock release failed")

    if cleanup_errors:
        if exit_code == 0:
            exit_code = CLI_IO_FAILURE_EXIT_CODE
        _print_json_result(
            _cli_failure_result(
                code="cleanup_error",
                message="; ".join(cleanup_errors),
                error_type="CleanupError",
                retryable=True,
                status="incomplete",
            ),
            stream=sys.stderr,
        )

    if answer:
        if not _print_text(answer):
            return CLI_IO_FAILURE_EXIT_CODE
    if logger is not None:
        if not _print_text(f"\nTask ID: {logger.task_id}"):
            return CLI_IO_FAILURE_EXIT_CODE
        if not _print_text(f"\nTask directory: {logger.task_dir}"):
            return CLI_IO_FAILURE_EXIT_CODE
        if not _print_text(f"\nRun log: {_run_log_location(logger, runtime)}"):
            return CLI_IO_FAILURE_EXIT_CODE
    return exit_code


async def run_cli(args: argparse.Namespace) -> int:
    """Never let bootstrap failures escape into an embedding host."""
    try:
        return await _run_cli_impl(args)
    except asyncio.CancelledError as exc:
        _print_json_result(
            _cancelled_cli_result(str(exc) or "Task execution was cancelled."),
        )
        return CLI_CANCELLED_EXIT_CODE
    except Exception as exc:  # bootstrap/config/input boundary
        failure, exit_code = _classify_cli_exception(exc, phase="startup")
        _safe_logger_write(
            _LAST_LOGGER,
            "run.startup_failed",
            {
                **exception_payload(exc, mode=str(getattr(args, "agent_mode", "") or "browser")),
                "terminal": failure.get("error"),
            },
        )
        _print_json_result(failure)
        return exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ABCP Browser Agent Harness")
    parser.add_argument("--output", choices=("auto", "text", "json"), default="auto",
                        help="Browser result format: auto prints a terminal summary or JSON when piped; full receipts are always saved.")
    parser.add_argument("task", nargs="?", help="Task for the browser agent")
    parser.add_argument("--task", dest="task_option", help="Task for the browser agent")
    parser.add_argument("--config", default="config.json", help="Configuration file path")
    parser.add_argument("--agent-id", help="Override config.json browser.agent_id")
    parser.add_argument("--max-steps", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--resume",
        default="",
        metavar="TASK_ID",
        help="Resume a saved task by its full task ID (optional @ prefix); legacy task paths are also accepted",
    )
    parser.add_argument(
        "--resume-retry-interrupted",
        action="store_true",
        help="Allow interrupted phases to restart in non-interactive mode",
    )
    return parser


def _main_impl(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and _is_workflow_command(raw_argv[0]):
        # A Workflow command is one complete line; argparse must not split it.
        config_args = []
        if "--config" in raw_argv:
            index = raw_argv.index("--config")
            if index + 1 >= len(raw_argv):
                print("--config requires a configuration file path")
                return CLI_INPUT_ERROR_EXIT_CODE
            config_args = raw_argv[index:index + 2]
            del raw_argv[index:index + 2]
        output_args = []
        if "--output" in raw_argv:
            index = raw_argv.index("--output")
            if index + 1 < len(raw_argv):
                output_args = raw_argv[index:index + 2]
                del raw_argv[index:index + 2]
        # Quote only arguments that the shell had to keep together.
        line = " ".join([raw_argv[0], *(shlex.quote(part) if any(c.isspace() for c in part) else part
                                        for part in raw_argv[1:])])
        argv = raw_argv = [*config_args, *output_args, "--task", line]
    if raw_argv and raw_argv[0] == "/resume":
        config_args = []
        if "--config" in raw_argv:
            index = raw_argv.index("--config")
            if index + 1 >= len(raw_argv):
                print("--config requires a configuration file path")
                return CLI_INPUT_ERROR_EXIT_CODE
            config_args = raw_argv[index:index + 2]
            del raw_argv[index:index + 2]
        if len(raw_argv) < 2:
            print("Usage: /resume <task-id> [instruction]")
            return 2
        if re.fullmatch(r"@?[0-9a-fA-F]{32}", raw_argv[1]):
            path, rest = raw_argv[1], raw_argv[2:]
        else:
            path, rest = _recover_task_path(raw_argv[1:])
        raw_argv = [*config_args, "--resume", path]
        if rest:
            raw_argv.extend(["--task", " ".join(rest)])
        argv = raw_argv

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "main() cannot run inside an active event loop; await run_cli(args)"
        )
    return asyncio.run(run_cli(args))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Top-level containment boundary for CLI and panel process adapters."""
    global _CANCELLED_LOGGED

    try:
        return _main_impl(argv)
    except KeyboardInterrupt:
        if _LAST_LOGGER is not None and not _CANCELLED_LOGGED:
            _safe_logger_write(
                _LAST_LOGGER,
                "run.cancelled",
                {"reason": "KeyboardInterrupt"},
            )
            _CANCELLED_LOGGED = True
        _print_json_result(_cancelled_cli_result("KeyboardInterrupt"))
        return CLI_CANCELLED_EXIT_CODE
    except asyncio.CancelledError as exc:
        _print_json_result(
            _cancelled_cli_result(str(exc) or "Task execution was cancelled.")
        )
        return CLI_CANCELLED_EXIT_CODE
    except Exception as exc:  # noqa: BLE001 - last non-SystemExit boundary
        failure, exit_code = _classify_cli_exception(exc, phase="main")
        _safe_logger_write(
            _LAST_LOGGER,
            "run.fatal",
            {
                **exception_payload(exc, mode="cli"),
                "terminal": failure.get("error"),
            },
        )
        _print_json_result(failure, stream=sys.stderr)
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
