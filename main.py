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

from harness.agents.lead.agent import LeadAgent
from harness.runtime.resume_context import ResumeContext
from harness.runtime.model_config import lead_agent_model_config
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
    "/lead": "lead",
}
_AGENT_MODE_LABELS = {
    "browser": "直达单个 BrowserAgent",
    "lead": "计划、并发与汇总",
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

    def _format(self, event_type: str, payload: Dict[str, Any]) -> Optional[str]:
        if event_type == "browser.task_review.start":
            return f"[BrowserReview] 开始 {payload.get('phase')} 复核，Browser 步骤 {payload.get('step')}"
        if event_type == "browser.task_review.model":
            return f"[BrowserReview {payload.get('reviewId')}] 第 {payload.get('round')} 轮：请求模型..."
        if event_type == "browser.task_review.tool":
            result = payload.get("result") or {}
            method = (payload.get("input") or {}).get("method")
            detail = result.get("reason") or result.get("error") or ""
            if payload.get("tool") in {"review_search_text", "review_list_files"} \
                    and result.get("status") == "done":
                detail = (f"{result.get('count', 0)} 条"
                          f"，续查 {result.get('nextLineOffset') or result.get('nextOffset')}"
                          if result.get("truncated") else f"{result.get('count', 0)} 条")
            return (f"[BrowserReview {payload.get('reviewId')}] {payload.get('tool')}"
                    f"{(' ' + str(method)) if method else ''}: {result.get('status')}"
                    f" {self._short_text(detail, 160)}").rstrip()
        if event_type == "browser.task_review.verdict":
            errors = (payload.get("validation") or {}).get("errors")
            if errors:
                return f"[BrowserReview] 判决协议需修正: {self._short_text('; '.join(errors), 350)}"
            return None  # the completion event prints the verdict once
        if event_type == "browser.task_review":
            return (f"[BrowserReview] {payload.get('verdict') or payload.get('status')}"
                    f"，模型 {payload.get('modelCalls', '?')} 轮，耗时 {payload.get('durationMs', '?')}ms"
                    f"，问题 {len(payload.get('issues') or [])} 项："
                    f"{self._short_text(payload.get('reason'), 240)}")
        if event_type == "browser.task_review.skipped":
            return f"[BrowserReview] 未启动：{payload.get('reason')}"
        if event_type == "assignment_review.start":
            return f"[PlanValidator] 开始{'协议修正' if payload.get('repair') else '复核'} {payload.get('assignmentId')}"
        if event_type == "assignment_review.call":
            return f"[PlanValidator] 模型调用结束，耗时 {payload.get('durationMs')}ms"
        if event_type == "assignment_review.result":
            return (f"[PlanValidator] {payload.get('status')}，记录：{payload.get('auditPath')}"
                    f" {self._short_text(payload.get('errors') or '', 240)}").rstrip()
        if event_type in {"assignment_review.cache_hit", "assignment_review.service_unavailable"}:
            return f"[PlanValidator] {event_type.rsplit('.', 1)[-1]}: {payload.get('assignmentId') or payload.get('candidateHash')}"
        if event_type == "llm.usage" and payload.get("source") in {
            "browser_task_reviewer", "plan_validator", "plan_validator_repair",
        }:
            return (f"[LLM {payload.get('source')}] cache_read={payload.get('cache_read', 0)} "
                    f"cache_creation={payload.get('cache_creation', 0)} "
                    f"uncached_input={payload.get('uncached_input', 0)} output={payload.get('output', 0)}")
        if event_type == "lifecycle.compaction.start":
            return (
                "[Compaction] 开始: "
                f"reason={payload.get('reason')} "
                f"tokens≈{payload.get('estimatedTokensBefore')}"
            )
        if event_type == "lifecycle.compaction.end":
            status = payload.get("status") or "completed"
            if status == "completed":
                fallback = (
                    "（机械降级摘要）"
                    if payload.get("summaryMode") == "mechanical_fallback"
                    else ""
                )
                return (
                    "[Compaction] 完成: "
                    f"tokens≈{payload.get('estimatedTokensAfter')} "
                    f"checkpoint={payload.get('checkpointRef') or '-'}{fallback}"
                )
            return f"[Compaction] {status}: {self._short_text(payload.get('error'), 160)}"
        if event_type == "lead.step.start":
            return f"[LeadAgent] 第 {payload.get('step')} 步：请求模型..."
        if event_type == "agent.step.start":
            return (
                f"[{self._browser_actor_label(payload)}] "
                f"第 {payload.get('step')} 步：请求模型..."
            )
        if event_type in {"lead.model", "agent.model"}:
            return self._format_model_event(event_type, payload)
        if event_type == "agent.truncated_response" and payload.get("kind") == "truncated":
            streak = payload.get("streak")
            limit = payload.get("limit")
            ending = isinstance(streak, int) and isinstance(limit, int) and streak >= limit
            action = "终止本 Worker 并返回未完成状态" if ending else "将按现有恢复策略再次请求模型"
            return (
                f"[{self._browser_actor_label(payload)}] 连续异常 {streak}/{limit}（本次：输出截断）；"
                f"{action}"
            )
        if event_type == "lead.tool.result":
            return self._format_lead_tool_result(payload)
        if event_type == "tool_result.offloaded":
            return self._format_offloaded_tool_result(payload)
        if event_type == "spawner.browser.spawn":
            return (
                f"[BrowserAgent] 启动 {payload.get('workerId')} "
                f"({payload.get('name') or 'unnamed'})"
            )
        if event_type == "spawner.browser.result":
            return self._format_browser_result(payload)
        if event_type == "task_plan.accepted":
            return (
                f"[TaskPlan] 已接受 {payload.get('phaseCount') or 0} 个 phase: "
                f"{payload.get('path')}"
            )
        if event_type == "task_plan.rejected":
            return self._format_task_plan_rejected(payload)
        if event_type == "task_state.initialized":
            return f"[TaskState] 已初始化: {payload.get('path')}"
        if event_type == "spawner.resume_browser_hint.used":
            page = payload.get("pageId")
            suffix = f"，页面 {page}" if page else ""
            return (
                "[Resume] 原浏览器 Fleet 仍可用，已继续复用 "
                f"{payload.get('fleetId')}{suffix}。"
            )
        if event_type == "spawner.resume_browser_hint.page_probe_failed":
            return (
                "[Resume] 原页面已不可用；将尝试保留 Fleet 登录态，"
                "并从新页面重跑当前 phase。"
            )
        if event_type == "spawner.resume_browser_hint.ignored":
            reason = str(payload.get("reason") or "unavailable")
            return (
                "[Resume] 原浏览器上下文未恢复"
                f"（{reason}）；将按普通分配从 phase 开头重跑，"
                "如需登录将重新获取。"
            )
        if event_type == "progress.observed":
            return (
                f"[Progress] 观察 {payload.get('tool') or '?'}: "
                f"{payload.get('reasonObserved') or 'no progress'}"
            )
        if event_type == "lead.step_cap.reminder":
            return (
                f"[LeadAgent] 接近步数上限: step={payload.get('step')} "
                f"remaining={payload.get('remaining')}"
            )
        if event_type == "agent.step_cap.reminder":
            return (
                f"[{self._browser_actor_label(payload)}] "
                f"接近步数上限: step={payload.get('step')} "
                f"remaining={payload.get('remaining')}"
            )
        if event_type == "browser.call.params_error":
            return (
                f"[BrowserCall] 参数错误 {payload.get('method') or '?'}: "
                f"{self._short_text(payload.get('error'), 140)}"
            )
        if event_type == "spawner.slot.sync_warning":
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            first = self._short_text(errors[0] if errors else payload.get("error"), 140)
            suffix = f" (+{len(errors) - 1})" if len(errors) > 1 else ""
            return (
                f"[Slot] 同步警告 {payload.get('workerId') or ''}: "
                f"{first}{suffix}"
            )
        if event_type == "tool.record_extraction":
            return self._format_record_extraction(payload)
        if event_type == "vl.visual_verify":
            verdict = payload.get("verdict") or payload.get("status") or "unknown"
            confidence = payload.get("confidence")
            if confidence is None:
                return f"[VL] 视觉验收: {verdict}"
            return f"[VL] 视觉验收: {verdict} (confidence={confidence})"
        if event_type == "schema.bundle.loaded":
            count = payload.get("schema_count") or 0
            req = payload.get("requires_purpose_count") or 0
            guide_chars = payload.get("agent_guide_chars") or 0
            return (
                f"[Schema] 加载 {count} 条 method schema "
                f"({req} 个需要 purpose)；Agent 指南 {guide_chars} 字符。"
            )
        if event_type == "loop_guard.warn":
            tool = payload.get("tool") or "?"
            streak = payload.get("streak") or 1
            return (
                f"[LoopGuard] 拦截重复 {tool}（第 {streak} 次，已不再执行）；"
                "提示模型切换策略或终止。"
            )
        if event_type == "loop_guard.force_stop":
            tool = payload.get("tool") or "?"
            streak = payload.get("streak") or 1
            return (
                f"[LoopGuard] 强制停止：{tool} 连续重复 {streak} 次。"
                "Worker 已以 extraction_inconclusive 终止。"
            )
        if event_type.endswith(".transport.request"):
            actor = event_type.split(".transport.", 1)[0]
            method = str(payload.get("method") or "unknown")
            self._last_method_by_actor[actor] = method
            if method == "System.describeAction":
                # Suppressed; see schema.bundle.loaded summary.
                return None
            return f"[{actor}] 调用浏览器方法: {method}"
        if event_type.endswith(".transport.response"):
            actor = event_type.split(".transport.", 1)[0]
            if self._last_method_by_actor.get(actor) == "System.describeAction":
                return None
            if payload.get("error"):
                return f"[{actor}] 浏览器响应: error ({self._format_error(payload)})"
            return f"[{actor}] 浏览器响应: ok"
        if event_type in {"lead.model_timeout", "agent.model_timeout"}:
            attempts = len(
                payload.get("attempts") or payload.get("timeoutAttempts") or []
            )
            seconds = payload.get("timeoutSeconds")
            will_retry = payload.get("willRetry")
            tail = (
                "压缩上下文后重试本步。" if will_retry
                else "重试次数已用尽，本步终止。" if will_retry is False
                else "本步按空回合处理，由重复守卫接管恢复。"
            )
            return (
                f"[LLM] 模型服务无响应（连续 {seconds}s 未收到新数据，触发空闲超时），"
                f"连初次请求共尝试 {attempts} 次仍失败；{tail}"
            )
        if event_type in {"lead.model_connection_error", "agent.model_connection_error"}:
            # The provider already retried silently; by the time this fires the
            # connection is repeatedly dropping, which the user should see
            # rather than discover as a traceback.
            attempts = len(payload.get("attempts") or payload.get("connectionAttempts") or [])
            will_retry = payload.get("willRetry")
            tail = (
                "压缩上下文后重试本步。" if will_retry
                else "重试次数已用尽，本步终止。" if will_retry is False
                else "本步按空回合处理，由重复守卫接管恢复。"
            )
            return (
                f"[LLM] 与模型服务的连接中断（{payload.get('reason') or 'connection error'}），"
                f"连初次请求共尝试 {attempts} 次仍失败；{tail}"
            )
        if event_type in {"run.error", "lead.error", "agent.error"}:
            error = payload.get("error") or payload.get("errorType") or "unknown error"
            return f"[错误] {error}"
        if event_type in {"lead.final", "agent.final"}:
            return "[完成] Agent 已生成最终结果。"
        return None

    def _format_model_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
    ) -> str:
        role = (
            "LeadAgent"
            if event_type == "lead.model"
            else self._browser_actor_label(payload)
        )
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
        token_note = f"，输出 {output_tokens} tokens" if output_tokens is not None else ""
        if stop_reason == "max_tokens":
            if tool_summaries:
                return (
                    f"[{role}] 模型输出达到上限（max_tokens）{token_note}；"
                    f"响应被截断，已返回工具调用: {'; '.join(tool_summaries)}"
                )
            has_thinking = (
                bool(telemetry.get("thinkingBlockCount"))
                or bool(telemetry.get("thinkingChars"))
                or "thinking" in (telemetry.get("prefixBlockTypes") or [])
                or "redacted_thinking" in (telemetry.get("prefixBlockTypes") or [])
            )
            content_note = (
                "仅返回推理内容，无正文、无工具调用"
                if has_thinking and not text else "未产生工具调用"
            )
            return (
                f"[{role}] 模型输出达到上限（max_tokens）{token_note}；"
                f"{content_note}，本步没有执行工具"
            )
        if tool_summaries:
            if text:
                return (
                    f"[{role}] 模型返回: {text}；"
                    f"准备调用: {'; '.join(tool_summaries)}"
                )
            return f"[{role}] 模型返回，准备调用: {'; '.join(tool_summaries)}"
        if not text:
            reason_note = f"，stop_reason={stop_reason}" if stop_reason else ""
            return f"[{role}] 模型未返回正文或工具调用{reason_note}"
        return f"[{role}] 模型返回: {text}"

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
        if name == "spawn_browser_agent":
            details = [
                f"name={raw_input.get('name') or 'unnamed'}",
                f"phase={raw_input.get('phase_id') or '-'}",
            ]
            if raw_input.get("max_steps") is not None:
                details.append(f"max_steps={raw_input.get('max_steps')}")
            if raw_input.get("reuse_from_worker_id"):
                details.append(f"reuse={raw_input.get('reuse_from_worker_id')}")
            return f"{name} ({'; '.join(details)})"
        if name == "wait_browser_agents":
            worker_ids = raw_input.get("worker_ids")
            if isinstance(worker_ids, list):
                workers = ",".join(str(item) for item in worker_ids[:4])
                if len(worker_ids) > 4:
                    workers += f",+{len(worker_ids) - 4}"
            else:
                workers = "all"
            return (
                f"{name} (workers={workers}; mode={raw_input.get('mode') or 'all'}; "
                f"timeout={raw_input.get('timeout_seconds') or '-'})"
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
        return f"[BrowserAgent] {worker_id} 结束: {', '.join(parts)}"

    def _format_task_plan_rejected(self, payload: Dict[str, Any]) -> str:
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        first = self._short_text(errors[0] if errors else payload.get("error"), 140)
        suffix = f" (+{len(errors) - 1})" if len(errors) > 1 else ""
        return f"[TaskPlan] 拒绝: {first or 'invalid plan'}{suffix}"

    def _format_record_extraction(self, payload: Dict[str, Any]) -> str:
        name = payload.get("name") or "-"
        row_count = payload.get("rowCount")
        path = self._short_path(payload.get("savedPath"))
        warnings = payload.get("schemaWarnings")
        warning_count = len(warnings) if isinstance(warnings, list) else 0
        suffix = f", warnings={warning_count}" if warning_count else ""
        return f"[Artifact] {name} 写入 {row_count if row_count is not None else '?'} 行: {path}{suffix}"

    def _format_lead_tool_result(self, payload: Dict[str, Any]) -> str:
        tool = payload.get("tool") or "unknown"
        parts = []
        for key in ("status", "count", "truncated"):
            if key in payload:
                parts.append(f"{key}={payload.get(key)}")
        if payload.get("expr"):
            parts.append(f"expr={self._short_text(payload.get('expr'), 70)}")
        path = payload.get("relativePath") or payload.get("savedPath") or payload.get("path")
        if path:
            parts.append(f"file={self._short_path(path)}")
        if payload.get("completedCount") is not None:
            parts.append(f"completed={payload.get('completedCount')}")
        if payload.get("pendingCount") is not None:
            parts.append(f"pending={payload.get('pendingCount')}")
        worker_statuses = payload.get("workerStatuses")
        if isinstance(worker_statuses, list) and worker_statuses:
            rendered = []
            for item in worker_statuses[:4]:
                if not isinstance(item, dict):
                    continue
                rendered.append(
                    f"{item.get('workerId') or '?'}:{item.get('status') or '?'}"
                    f"/{item.get('validatedStatus') or '-'}"
                )
            if len(worker_statuses) > 4:
                rendered.append(f"+{len(worker_statuses) - 4}")
            if rendered:
                parts.append("workers=" + ",".join(rendered))
        if payload.get("error"):
            parts.append(f"error={self._short_text(payload.get('error'), 120)}")
        issues = payload.get("issues")
        if isinstance(issues, list):
            issues = [item for item in issues if isinstance(item, dict)]
            rendered = [
                f"{self._short_text(item.get('path') or 'input', 140)}: "
                f"{self._short_text(item.get('message') or item.get('keyword'), 120)}"
                for item in issues[:3]
            ]
            remaining = int(payload.get("issueCount") or len(issues)) - len(rendered)
            if remaining > 0:
                rendered.append(f"另有 {remaining} 项（见日志 issues）")
            if rendered:
                parts.append("issues=" + "; ".join(rendered))
        return f"[LeadAgent] 工具结果 {tool}: {', '.join(parts) if parts else 'ok'}"

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
            f"[ToolResult] {tool} 结果已 offload: {size} bytes -> "
            f"{self._short_path(payload.get('relativePath') or payload.get('savedPath'))}"
        )


def _skill_catalog_for_runtime(runtime: Any):
    from harness.skill_builder.catalog import SkillCatalog
    from harness.storage.factory import resolve_sqlite_path
    return SkillCatalog(
        Path(__file__).resolve().parent / "skills",
        resolve_sqlite_path(runtime.harness.storage_sqlite_path,
                            runtime.harness.worktree_dir),
    )


def _selected_skill_version(runtime: Any, skill_id: str) -> str:
    from harness.skill_builder.catalog import inspect_skill
    catalog = _skill_catalog_for_runtime(runtime)
    try:
        catalog.sync_existing()
        row = catalog.get(skill_id)
        if row is None or row["deleted"]:
            raise ValueError(f"未知 Skill: {skill_id}")
        content_hash = str(row["current_hash"])
        try:
            current_hash = inspect_skill(catalog.root / skill_id)["hash"]
        except Exception as exc:
            raise ValueError(f"Skill {skill_id} 的正式文件不可读取: {exc}") from exc
        if current_hash != content_hash:
            raise ValueError(f"Skill {skill_id} 的正式文件与索引不一致")
        if catalog.version_path(skill_id, content_hash) is None:
            raise ValueError(f"Skill {skill_id} 的版本快照不可用")
        return content_hash
    finally:
        catalog.close()


def _available_skill_lines(config_path: str = "config.json") -> List[str]:
    try:
        from runtime_config import load_runtime_config
        catalog = _skill_catalog_for_runtime(load_runtime_config(config_path))
        try:
            catalog.sync_existing()
            return [_skill_listing(item) for item in catalog.list()]
        finally:
            catalog.close()
    except Exception:
        return []


def _skill_listing(row: Dict[str, Any]) -> str:
    metadata = json.loads(row.get("metadata_json") or "{}")
    description = str(metadata.get("description") or "").strip().replace("\n", " ")
    suffix = f" — {description[:120]}" if description else ""
    return (f"{row['skill_id']} @{row['current_hash']}"
            f" (version {row['current_version']}){suffix}")


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


def _run_coro_blocking(coro: Any) -> Any:
    """Run a coroutine from sync CLI code, even when an event loop is already
    running (read_task's input() executes inside run_cli's loop): fall back to a
    dedicated thread with its own loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _handle_skill_builder_command(line: str, *, config_path: str = "config.json") -> int:
    if sys.version_info < (3, 12):
        print("Skill Builder 需要 Python 3.12+；当前解释器: " + sys.version.split()[0],
              flush=True)
        return 2
    from harness.skill_builder.commands import execute
    try:
        return execute(line, config_path=config_path, run_blocking=_run_coro_blocking)
    except Exception as exc:
        print(f"Skill Builder 无法完成: {type(exc).__name__}: {exc}", flush=True)
        return 1


def _handle_skill_command(line: str, args: argparse.Namespace) -> str:
    """Process a `/skill ...` line typed at the task prompt. Mutates args.skill
    and returns any inline task text after the id (empty -> caller re-prompts)."""
    tokens = line.split()
    arg = tokens[1] if len(tokens) > 1 else ""
    inline_task = " ".join(tokens[2:]).strip()
    from runtime_config import load_runtime_config
    runtime = load_runtime_config(getattr(args, "config", "config.json"))
    catalog = _skill_catalog_for_runtime(runtime)
    try:
        catalog.sync_existing()
        rows = catalog.list()
    finally:
        catalog.close()
    ids = [row["skill_id"] for row in rows]
    if not arg or arg in ("list", "ls", "?"):
        print("可用技能:", "\n".join(_skill_listing(row) for row in rows) or "(无)")
        if args.skill:
            print(f"当前已选: {args.skill}")
        print("用法: /skill <确切Skill名> 选取；/skill off 取消；"
              "/skill-create @<task_id> 需求；/<某个Skill名> @<task_id> 修复建议")
        return ""
    if arg in ("off", "none", "clear", "-"):
        args.skill = ""
        print("已取消技能强制。")
        return inline_task
    if arg not in ids:
        print(f"未知 Skill {arg!r}。可用: {', '.join(ids) or '(无)'}")
        return ""
    args.skill = arg
    print(f"已选 Skill: {arg}（执行时绑定当前版本）")
    return inline_task


def _handle_agent_mode_command(line: str, args: argparse.Namespace) -> bool:
    """Handle a mode choice at the interactive task prompt.

    The choice is intentionally scoped to this invocation by mutating the
    parsed arguments only.  It never rewrites config.json, and it is accepted
    only before a task is submitted from ``read_task``.
    """
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        print(f"模式命令参数错误: {exc}")
        return line.lstrip().startswith(("/browser", "/lead"))
    if not tokens or tokens[0].lower() not in _INTERACTIVE_AGENT_MODES:
        return False
    if len(tokens) != 1:
        print("用法: /browser 或 /lead；模式命令不能携带任务文本。")
        return True
    agent_mode = _INTERACTIVE_AGENT_MODES[tokens[0].lower()]
    args.agent_mode = agent_mode
    print(
        f"已选择 {agent_mode}：{_AGENT_MODE_LABELS[agent_mode]}。"
        "请继续输入任务；可用 /skill 或 /resume。"
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
        getattr(args, "configured_agent_mode", "lead") or "lead"
    ).strip().lower()
    selected_mode = str(getattr(args, "agent_mode", "") or "").strip().lower()
    if selected_mode not in _AGENT_MODE_LABELS:
        selected_mode = ""
    if not selected_mode:
        print(
            "请选择编排模式：/browser（直达单个 BrowserAgent）或 "
            "/lead（计划、并发与汇总）。"
        )
        print(f"当前配置默认：{configured_mode}；请选择后再输入任务。")
    while True:
        prompt = (
            f"[{selected_mode}] 请输入浏览器任务（/resume <任务目录> [补充指令]；"
            "可先用 /skill <确切名称> 指定技能，/skill 列出；"
            "/skill-create @<task_id> 创建，/<确切Skill名> @<task_id> 修复）: "
            if selected_mode
            else "模式未选择> "
        )
        line = input(prompt).strip()
        from harness.skill_builder.commands import recognizes as is_builder_command
        if is_builder_command(line, config_path=getattr(args, "config", "config.json")):
            _handle_skill_builder_command(line, config_path=getattr(args, "config", "config.json"))
            continue
        if _handle_agent_mode_command(line, args):
            selected_mode = str(getattr(args, "agent_mode", "") or "").strip().lower()
            continue
        if not selected_mode:
            print("请先输入 /browser 或 /lead 选择本次编排模式。")
            continue
        if line.startswith("/skill"):
            inline_task = _handle_skill_command(line, args)
            if inline_task:
                return inline_task
            continue
        if line == "/resume" or line.startswith("/resume "):
            resumed = _handle_resume_command(line, args)
            if resumed is not None:
                return resumed
            continue
        return line


def _print_task_plan_review(plan: Dict[str, Any], candidate_hash: str) -> None:
    from harness.planning.context import approval_view
    view = approval_view(plan)
    print("\n[Assignment] 委派等待确认", flush=True)
    print(f"候选版本: {candidate_hash[:12]}", flush=True)
    print(json.dumps(view, ensure_ascii=False, indent=2), flush=True)
    print("确认/确定执行；修改意见交给 Lead；详情查看完整记录；取消停止。", flush=True)


async def _terminal_plan_approval(
    plan: Dict[str, Any], candidate_hash: str,
    *, runtime: Optional[RuntimeConfig] = None, logger: Any = None,
) -> Dict[str, Any]:
    _print_task_plan_review(plan, candidate_hash)
    input_records: List[Dict[str, Any]] = []

    def record_input(answer: str, decision: str, error: str = "") -> Dict[str, Any]:
        record = {
            "inputId": uuid.uuid4().hex,
            "candidateHash": candidate_hash,
            "text": answer,
            "decision": decision,
            "error": error,
            "receivedAt": datetime.now(timezone.utc).isoformat(),
        }
        input_records.append(record)
        if logger is not None:
            logger.write("task_plan.approval_input_received", record)
        return record

    def unresolved_feedback() -> str:
        return "\n".join(
            item["text"] for item in input_records
            if item["decision"] in {"clarify", "revision"}
        )

    def unresolved_records() -> List[Dict[str, Any]]:
        return [item for item in input_records
                if item["decision"] in {"clarify", "revision"}]

    def has_unclassified_feedback() -> bool:
        return any(item.get("error") or item.get("errorCode")
                   for item in unresolved_records())

    while True:
        answer = (await asyncio.to_thread(input, "计划审阅> ")).strip()
        normalized = answer.lower()
        # ``确定`` is the normal affirmative answer in the terminal UI.  It
        # must have exactly the same meaning as ``确认``; treating it as free
        # form feedback makes a user-approved candidate go back through the
        # Lead and be submitted a second time.
        if normalized in {"确认", "确定", "同意", "执行", "y", "yes"}:
            record_input(answer, "approved")
            feedback = unresolved_feedback()
            if feedback and has_unclassified_feedback():
                # An unclassified response may change the candidate. A
                # classifier error cannot authorize discarding that input.
                return {"decision": "revision", "feedback": feedback,
                        "inputRecords": input_records}
            return ({"decision": "approved", "inputRecords": input_records}
                    if feedback else {"decision": "approved"})
        if normalized in {"取消", "停止", "n", "no", "cancel"}:
            record_input(answer, "cancelled")
            return {"decision": "cancelled"}
        if normalized in {"详情", "detail", "details", "json"}:
            record_input(answer, "details")
            print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
            continue
        if answer:
            prior_unresolved = bool(unresolved_records())
            record = record_input(answer, "received")
            if runtime is None:
                record["decision"] = "clarify"
                record["error"] = "classifier_unavailable"
                if logger is not None:
                    logger.write("task_plan.approval_input_classified", record)
                print("无法进行语义确认，请输入明确的确认、取消或详情命令。", flush=True)
                continue
            from harness.planning.approval_intent import classify_approval_intent
            outcome = await classify_approval_intent(
                answer, plan, candidate_hash, runtime, logger, prior_inputs=input_records[:-1],
            )
            record["decision"] = str(outcome.get("decision") or "clarify")
            record["error"] = str(outcome.get("error") or "")
            record["errorCode"] = str(outcome.get("errorCode") or "")
            if logger is not None:
                logger.write("task_plan.approval_input_classified", record)
            if outcome["decision"] == "details":
                print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
                continue
            if outcome["decision"] == "clarify":
                return {"decision": "revision", "feedback": unresolved_feedback(),
                        "inputRecords": input_records}
            if outcome["decision"] == "revision":
                outcome["feedback"] = unresolved_feedback()
                outcome["inputRecords"] = input_records
            elif (outcome["decision"] == "approved" and unresolved_feedback()
                  and has_unclassified_feedback()):
                return {"decision": "revision",
                        "feedback": unresolved_feedback(),
                        "inputRecords": input_records}
            elif outcome["decision"] == "approved" and prior_unresolved:
                outcome["inputRecords"] = input_records
            return outcome
        print("空输入不会确认计划。请输入“确认”、修改意见、“详情”或“取消”。", flush=True)


def _handle_resume_command(
    line: str,
    args: argparse.Namespace,
) -> Optional[str]:
    """Parse an interactive phase-level resume request without touching disk."""

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        print(f"/resume 参数错误: {exc}")
        return None
    if len(tokens) < 2:
        print("用法: /resume <worktree任务目录> [补充指令]")
        return None
    path, rest = _recover_task_path(tokens[1:])
    args.resume = path
    args.resume_instruction = " ".join(rest).strip()
    return args.resume_instruction


def _validate_resume_mode(manifest: Optional[JsonDict], plan: JsonDict, selected: str) -> str:
    """Check before reconciliation writes. Old tasks fall back to plan shape."""
    startup = (manifest or {}).get("startup_args") or {}
    recorded = startup.get("agent_mode") if isinstance(startup, dict) else None
    original = str(recorded or "").strip().lower()
    if original not in {"browser", "lead"}:
        original = "browser" if plan.get("execution_mode") == "direct_worker" else "lead"
    if selected != original:
        raise ResumeStateError(
            f"该任务使用 /{original} 模式；请切换到 /{original} 后恢复，不能在 /{selected} 中恢复。"
        )
    return original


def _resolve_resume_directory(raw_path: str) -> Path:
    """Resolve a registered DB task or existing directory without creating it."""

    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("缺少 worktree 任务目录")
    candidate = Path(raw).expanduser()
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates.append(Path(__file__).resolve().parent / candidate)
        if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
            root = resume_worktree_dir() or Path(__file__).resolve().parent / "worktree"
            candidates.insert(0, root / raw.lower())
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
        "任务在数据库中不存在，且 worktree 目录不存在或已被删除；"
        "无法恢复，也不会自动重建空任务目录"
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
        return f"SQLite {database}（任务 {logger.task_id}）"
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
    """Small, bounded summary for the Lead resume bootstrap."""

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
    task state, or convert partial work into completion.  The Lead gets stable
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
    lets Lead see recent answers, their request identity and classification;
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
        f"phase [{joined}] 上次运行中断，或因等待人工处理而结束。"
        "Worker 的 step/messages 不会恢复；继续将从 phase 开头重跑，"
        "而上次操作可能已产生外部副作用。"
    )
    print(warning, flush=True)
    if not sys.stdin.isatty():
        print(
            "非交互模式默认拒绝重放；确认后请加 "
            "--resume-retry-interrupted。",
            flush=True,
        )
        return False
    answer = input("是否允许完整重跑这些 phase？ [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


_BROWSER_MODE_STATUS_LABELS = {
    "done": "任务完成",
    "completed": "任务完成",
    "validated_done": "任务完成（已验证）",
    "partial": "部分完成",
    "incomplete": "未完成",
    "failed": "失败",
    "cancelled": "已取消",
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
    label = _BROWSER_MODE_STATUS_LABELS.get(status, f"状态 {status or 'unknown'}")
    worker_status = ""
    direct_exec = result.get("directExecution")
    if isinstance(direct_exec, dict):
        worker_status = str(direct_exec.get("workerStatus") or "").strip()
    header = f"Browser 模式任务结果: {label}"
    if worker_status and worker_status not in {status, "done"}:
        header += f"（worker 终态: {worker_status}）"
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
            for key, label in (("blockers", "阻塞"), ("next_steps", "下一步")):
                items = parsed.get(key) or []
                for item in items[:4]:
                    if isinstance(item, dict):
                        item = item.get("detail") or item.get("reason") or item.get("message") or item.get("type") or "见完整回执"
                    print(f"{label}: {str(item)[:400]}", flush=True)
                if len(items) > 4:
                    print(f"其余 {len(items) - 4} 项见完整回执。", flush=True)
        elif isinstance(parsed, dict) and parsed.get("evidence"):
            for path in parsed.get("evidence") or []:
                if isinstance(path, str) and (path.startswith(("/", "https://", "http://"))):
                    print(f"产物: {path}", flush=True)
        elif isinstance(parsed, dict):
            if parsed.get("summary"):
                print(str(parsed["summary"]), flush=True)
        else:
            print(answer, flush=True)
    delivery = result.get("artifactDelivery")
    if isinstance(delivery, dict) and delivery.get("status") == "failed":
        print(
            "产物导出失败：有提取文件路径无法直接打开；"
            "数据仍保存在任务数据库中。"
            f" 原因：{delivery.get('error')}",
            flush=True,
        )
    error = result.get("error")
    reason = error.get("message") if isinstance(error, dict) else error
    reason = str(reason or result.get("reason") or "").strip()
    if reason:
        # A worker may return an answer alongside a failure. Show both instead
        # of hiding the failure reason inside the following machine receipt.
        print(f"原因: {reason}", flush=True)
        for item in result.get("errors") or []:
            print(f"  · {item}", flush=True)
        review = result.get("review")
        verdict = review.get("verdict") if isinstance(review, dict) else None
        if isinstance(verdict, dict):
            for finding in verdict.get("findings") or []:
                if isinstance(finding, dict) and finding.get("blocking"):
                    print(f"  · {finding.get('reason') or finding}", flush=True)
        if isinstance(review, dict) and review.get("auditPath"):
                print(f"审核记录: {review['auditPath']}", flush=True)
    if result.get("receiptPath"):
        print(f"完整回执: {result['receiptPath']}", flush=True)
    if status in {"done", "completed", "validated_done"}:
        return
    # Resume reads task_plan.json. A run that died before a plan was accepted
    # has none, and pointing the user at resume there only produces a second,
    # different error (verified on run c7c931b7: ResumeStateError, "task plan
    # is missing").
    plan_path = Path(task_dir) / "task_plan.json" if task_dir else None
    from harness.utils import task_file_exists
    plan_exists = (task_file_exists(logger, str(plan_path)) if logger is not None and plan_path is not None
                   else plan_path is not None and plan_path.is_file())
    if plan_exists:
        print(
            "\n继续: python main.py --config config.json"
            f"\n然后输入 /browser，再输入 /resume {task_dir}",
            flush=True,
        )
    else:
        print(
            "\n这次在计划生成阶段就结束了，没有可恢复的计划，/resume 用不了。"
            "\n按上面的原因改一下任务描述再跑一次即可。",
            flush=True,
        )


async def _run_cli_impl(args: argparse.Namespace) -> int:
    global _CANCELLED_LOGGED, _LAST_LOGGER

    _CANCELLED_LOGGED = False
    _LAST_LOGGER = None
    runtime = load_runtime_config(args.config)
    configured_mode = str(getattr(runtime.harness, "agent_mode", "lead") or "lead").strip().lower()
    if configured_mode not in _AGENT_MODE_LABELS:
        print("config.json 中 harness.agent_mode 必须是 lead 或 browser。")
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
    if args.max_steps:
        runtime.harness.max_steps = args.max_steps
        runtime.harness.worker_max_steps = args.max_steps
    task = read_task(args)
    requested_mode = str(getattr(args, "agent_mode", "") or "").strip().lower()
    if requested_mode:
        if requested_mode not in _AGENT_MODE_LABELS:
            print("交互模式必须是 /lead 或 /browser。")
            return CLI_INPUT_ERROR_EXIT_CODE
        runtime.harness.agent_mode = requested_mode
    agent_mode = str(getattr(runtime.harness, "agent_mode", "lead") or "lead").strip().lower()
    resume_requested = bool(str(getattr(args, "resume", "") or "").strip())
    if not task and not resume_requested:
        print("没有收到任务。")
        return 2
    from harness.skill_builder.commands import recognizes as is_builder_command
    if is_builder_command(task, config_path=args.config):
        return _handle_skill_builder_command(task, config_path=args.config)
    # Explicit selection binds one immutable Skill version for this task.
    forced_skill = str(getattr(args, "skill", "") or "").strip()
    if forced_skill and not resume_requested:
        try:
            forced_hash = _selected_skill_version(runtime, forced_skill)
        except ValueError as exc:
            print(str(exc), flush=True)
            return CLI_INPUT_ERROR_EXIT_CODE
        runtime.harness.forced_skill_id = forced_skill
        runtime.harness.forced_skill_hash = forced_hash
        print(f"已选 Skill: {forced_skill}@{forced_hash[:12]}", flush=True)

    logger: Optional[RunLogger] = None
    harness: Optional[LeadAgent] = None
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
                    saved_skill = str(manifest.get("forced_skill_id") or "")
                    saved_hash = str((manifest.get("startup_args") or {}).get("forced_skill_hash") or "")
                    if forced_skill and forced_skill != saved_skill:
                        raise ResumeStateError("恢复任务不能改选 Skill")
                    if saved_skill:
                        if not saved_hash:
                            raise ResumeStateError("旧任务未绑定 Skill 版本，无法安全恢复")
                        catalog = _skill_catalog_for_runtime(runtime)
                        try:
                            if catalog.version_path(saved_skill, saved_hash) is None:
                                raise ResumeStateError("恢复任务的 Skill 版本快照不可用")
                        finally:
                            catalog.close()
                        runtime.harness.forced_skill_id = saved_skill
                        runtime.harness.forced_skill_hash = saved_hash
                        forced_skill = saved_skill
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
                            "task_manifest.json 缺少 original_user_task"
                        )
                else:
                    original_user_task = recover_legacy_user_task(task_dir)
                    if not original_user_task and sys.stdin.isatty():
                        print(
                            "这是旧版 worktree，未找到可恢复的原始用户任务。",
                            flush=True,
                        )
                        original_user_task = input(
                            "请完整重述原始任务（不是本次新指令）: "
                        ).strip()
                    if not original_user_task:
                        raise ResumeStateError(
                            "旧 worktree 无 manifest，也无法从历史 context "
                            "恢复原始任务；请在交互模式下重述原始任务"
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
                            "补充指令不能将已恢复任务改绑到其他 @Fleet；"
                            "请沿用原任务的 Fleet 或创建新任务"
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
                        forced_skill_id=forced_skill or None,
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
                print(f"无法恢复任务: {exc}", flush=True)
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
                forced_skill_id=forced_skill or None,
                agent_id=runtime.agent_id,
                startup_args={
                    "max_steps": getattr(args, "max_steps", None),
                    "agent_mode": agent_mode,
                    "forced_skill_hash": str(getattr(runtime.harness, "forced_skill_hash", "") or ""),
                },
            )
            runtime.harness.runs_dir = str(logger.task_dir)

        if logger is None:  # defensive; both setup branches assign it
            print("无法初始化任务日志。", flush=True)
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
            print(f"Fleet 引用错误: {fleet_reference_error}", flush=True)
            return CLI_INPUT_ERROR_EXIT_CODE
        if task_fleet_reference and not bool(
            getattr(runtime.harness, "fleet_reuse_enabled", True)
        ):
            message = (
                "任务使用了 @Fleet 引用，但 harness.fleet_reuse_enabled=false，"
                "无法绑定已有 Fleet。"
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
            print(f"任务已恢复: {logger.task_id}", flush=True)
            print(
                f"恢复摘要: 保留 {kept} 个已验证 phase；"
                f"待继续/重试 {len(pending_ids)} 个: "
                f"{', '.join(pending_ids) or '(无)'}；"
                f"其中因中断/产物失效重置 {len(reset)} 个: "
                f"{', '.join(map(str, reset)) or '(无)'}",
                flush=True,
            )
            if hitl_reactivated:
                print(
                    f"已重新开放 {len(hitl_reactivated)} 个人工中断"
                    f"（HITL/本地文件授权）phase: "
                    f"{', '.join(hitl_reactivated)}；执行到相关操作时会在终端"
                    "重新请求确认。",
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
                    "浏览器恢复: 已找到历史 Fleet/Page 候选（尚未验证在线）；"
                    "连接就绪后按会话约束探活和恢复。"
                    "此记录不代表 WebCross Dispatcher 当前可用。",
                    flush=True,
                )
            elif browser_status.get("status") == "disabled_by_config":
                print(
                    "浏览器恢复: fleet_reuse_enabled=false，"
                    "不会尝试恢复原 Fleet，将从新浏览器上下文执行。",
                    flush=True,
                )
            else:
                print(
                    "浏览器恢复: 无本任务专属候选，将重新分配；"
                    "需要时会重新登录。",
                    flush=True,
                )
        else:
            print(f"任务已创建: {logger.task_id}", flush=True)
        print(f"模式: {agent_mode}", flush=True)
        print(f"任务目录: {logger.task_dir}", flush=True)
        print(f"运行日志: {_run_log_location(logger, runtime)}", flush=True)
        if resume_context is not None:
            label = "带补充指令继续" if resume_context.instruction else "按原计划恢复"
            print(f"{label}：先检查浏览器连接；连接不可用时停止，不调用模型或派发 worker。", flush=True)
        elif agent_mode == "browser":
            print("Browser 模式：直接启动 BrowserAgent，执行中维护任务清单。", flush=True)
        elif sys.stdin.isatty():
            print("开始生成执行计划；确认前不会启动 BrowserAgent。", flush=True)
        else:
            print("开始执行，关键进度会在这里显示。", flush=True)

        if agent_mode == "browser":
            harness = BrowserTaskRunner(
                runtime, logger, resume=resume_context,
                task_fleet_reference=task_fleet_reference or "",
            )
        else:
            harness = LeadAgent(
                LLMFactory.create_provider(lead_agent_model_config(runtime)),
                runtime, logger, resume=resume_context,
                task_fleet_reference=task_fleet_reference or "",
                plan_approval_handler=(
                    (lambda plan, candidate_hash: _terminal_plan_approval(
                        plan, candidate_hash, runtime=runtime, logger=logger
                    )) if sys.stdin.isatty() else None
                ),
            )
        if agent_mode == "browser":
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
                        f"数据库任务 {logger.task_id} / artifacts/browser-result.json"},
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
        else:
            answer = await harness.run(task_for_agent)
        terminal_error = getattr(harness, "terminal_error", None)
        if agent_mode == "browser":
            # The browser entry owns the worker receipt and status;
            # LeadAgent.final_status is intentionally untouched because no
            # LeadAgent.run() turn was made.
            pass
        elif isinstance(terminal_error, dict):
            run_error = terminal_error
            run_status = "failed"
            exit_code = LLM_TEMPORARY_FAILURE_EXIT_CODE
            _safe_logger_write(logger, "run.rate_limited", terminal_error)
        else:
            lead_status = str(
                getattr(harness, "final_status", "") or ""
            ).strip().lower()
            if lead_status and lead_status not in {"done", "completed"}:
                run_status = "failed"
                exit_code = CLI_ERROR_EXIT_CODE
                lead_trigger = str(
                    getattr(harness, "final_trigger", "") or "unknown"
                )
                failure = _cli_failure_result(
                    code="task_not_completed",
                    message=(
                        f"LeadAgent ended with status={lead_status} "
                        f"(trigger={lead_trigger})."
                    ),
                    error_type="LeadTerminalOutcome",
                    retryable=lead_status in {"blocked", "incomplete"},
                    status=(
                        lead_status
                        if lead_status in {"blocked", "failed", "incomplete"}
                        else "failed"
                    ),
                    details={"trigger": lead_trigger},
                )
                failure["answer"] = answer
                run_error = failure["error"]
                answer = json.dumps(failure, ensure_ascii=False)
                _safe_logger_write(
                    logger,
                    "run.incomplete",
                    {
                        "status": lead_status,
                        "trigger": lead_trigger,
                    },
                )
            else:
                run_status = "completed"
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
        # LeadAgent.run owns its spawner cleanup. Browser mode skips that method,
        # so the CLI must explicitly join workers before closing the audit store.
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
        if not _print_text(f"\n任务ID: {logger.task_id}"):
            return CLI_IO_FAILURE_EXIT_CODE
        if not _print_text(f"\n任务目录: {logger.task_dir}"):
            return CLI_IO_FAILURE_EXIT_CODE
        if not _print_text(f"\n运行日志: {_run_log_location(logger, runtime)}"):
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
                **exception_payload(exc, mode="lead"),
                "terminal": failure.get("error"),
            },
        )
        _print_json_result(failure)
        return exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ABCP Browser Agent Harness")
    parser.add_argument("--output", choices=("auto", "text", "json"), default="auto",
                        help="Browser 结果格式：auto 在终端显示简报，管道保留 JSON；完整回执始终保存。")
    parser.add_argument("task", nargs="?", help="要交给浏览器 agent 完成的任务")
    parser.add_argument("--task", dest="task_option", help="要交给浏览器 agent 完成的任务")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--agent-id", help="覆盖 config.json 中的 browser.agent_id")
    parser.add_argument("--max-steps", type=int, help="覆盖最大 agent 编排步数")
    parser.add_argument(
        "--resume",
        default="",
        metavar="WORKTREE",
        help="从已有 worktree 目录按 phase 粒度恢复任务",
    )
    parser.add_argument(
        "--resume-retry-interrupted",
        action="store_true",
        help="非交互模式下明确允许完整重跑上次被中断的 phase",
    )
    parser.add_argument(
        "--skill",
        dest="skill",
        default="",
        help="强制本次运行使用的技能 id（等价于 harness.forced_skill_id，无需改 config）",
    )
    parser.add_argument(
        "--list-skills",
        action="store_true",
        help="列出可用技能 id 后退出",
    )
    return parser


def _main_impl(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    from harness.skill_builder.commands import recognizes as is_builder_command
    if raw_argv and is_builder_command(" ".join(shlex.quote(part) for part in raw_argv)):
        line = " ".join(shlex.quote(part) for part in raw_argv)
        return _handle_skill_builder_command(line)
    if raw_argv and raw_argv[0] == "/resume":
        if len(raw_argv) < 2:
            print("用法: /resume <worktree任务目录> [补充指令]")
            return 2
        path, rest = _recover_task_path(raw_argv[1:])
        raw_argv = ["--resume", path]
        if rest:
            raw_argv.extend(["--task", " ".join(rest)])
        argv = raw_argv

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if getattr(args, "list_skills", False):
        print("可用技能:", "\n".join(_available_skill_lines(args.config)) or "(无)")
        return 0
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
                **exception_payload(exc, mode="lead"),
                "terminal": failure.get("error"),
            },
        )
        _print_json_result(failure, stream=sys.stderr)
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
