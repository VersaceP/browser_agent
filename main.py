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

from agent_harness import (
    LeadAgent,
    ResumeContext,
    exception_payload,
    lead_agent_model_config,
    llm_rate_limit_terminal_result,
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
    load_task_manifest,
    load_task_plan_strict,
    load_task_state_strict,
    recover_legacy_user_task,
    reconcile_torn_plan_alias,
    release_run_lock,
    load_initial_task_plan_strict,
    write_task_manifest,
)
from harness.task_control import (
    TERMINAL_PHASE_STATUSES,
    prepare_resume_state,
    write_task_state,
)
from harness.planning.fleet_reference import extract_fleet_reference
from harness.tools.lead_tools import _lead_spawn_browser_agent, _lead_wait_browser_agents
from harness.tools.registry import ToolContext
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


def _available_skill_ids() -> List[str]:
    try:
        from harness.skill.registry import SkillRegistry
        return [s.skill_id for s in SkillRegistry.load().all()]
    except Exception:
        return []


def _available_suite_names() -> List[str]:
    try:
        from harness.skill.registry import SkillRegistry
        return sorted({s.suite for s in SkillRegistry.load().all() if s.suite})
    except Exception:
        return []


def _expand_skill_selection(name: str) -> "tuple[List[str], bool]":
    """把 /skill <name> 的 name 解析成 (skill_id 列表, 是否为 suite)。

    name 是 skill_id → ([name], False)；是 suite 名 → (成员 ids, True)；
    都不是 → ([], False)（未知）。展开逻辑复用 registry.expand_selection。"""
    try:
        from harness.skill.registry import SkillRegistry
        reg = SkillRegistry.load()
        ids = {s.skill_id for s in reg.all()}
        expanded = reg.expand_selection(name)
        if name in ids:
            return [name], False
        if name in _available_suite_names() and expanded:
            return expanded, True
        return [], False
    except Exception:
        return [], False


def _is_truthy_flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def _skill_display_line(skill: Any) -> str:
    """Skill id + trust markers for the /skill list: [draft] means the SKILL.md
    calibration checklist has not been signed off; [未试运行] means no live
    generality trial has passed; [质量门未过] means the last create/recheck
    left the skill blocked (see its .create_report.json)."""
    frontmatter = getattr(skill, "frontmatter", None) or {}
    marks: List[str] = []
    if getattr(skill, "is_hints_only", False):
        marks.append("hints")
    if _is_truthy_flag(frontmatter.get("draft")):
        marks.append("draft")
    if "tested" in frontmatter and not _is_truthy_flag(frontmatter.get("tested")):
        marks.append("未试运行")
    try:
        from harness.skill.registry import load_create_report
        status = str(load_create_report(getattr(skill, "directory", None)).get("status") or "")
        if status in ("draft_blocked", "revision_blocked", "recheck_failed"):
            marks.append("质量门未过")
    except Exception:
        pass
    try:
        from harness.skill.guidance import default_guidance_health
        if default_guidance_health().needs_review(skill.skill_id):
            marks.append("hints待复审")
    except Exception:
        pass
    return skill.skill_id + (f" [{','.join(marks)}]" if marks else "")


def _skill_display_lines() -> List[str]:
    try:
        from harness.skill.registry import SkillRegistry
        return [_skill_display_line(s) for s in SkillRegistry.load().all()]
    except Exception:
        return []


def _hint_matching_skills(task: str) -> None:
    """Passive hint (manual selection mode): if the task's URL host matches a
    known skill's domain, say so — never auto-engage."""
    try:
        match = re.search(r"https?://([^/\s\"'<>]+)", str(task or ""))
        if not match:
            return
        host = match.group(1).lower()
        host = host[4:] if host.startswith("www.") else host
        from harness.skill.registry import SkillRegistry, _domain_matches
        hits = [s for s in SkillRegistry.load().all() if _domain_matches(s.domain, host)]
        if hits:
            print(
                "提示: 检测到同域技能 "
                + ", ".join(_skill_display_line(s) for s in hits)
                + "（手动模式不会自动启用；如需使用请以 --skill <id> 重新运行，"
                "或交互模式先输 /skill <id>）",
                flush=True,
            )
    except Exception:
        pass


def _skill_create_tokens(line: str) -> List[str]:
    # IME 全角空格(U+3000)/NBSP 不在 shlex 的分隔符集里——路径后跟中文说明时
    # 会被粘成一个 token（07-06 事故），先归一成半角空格再切。
    line = line.replace("　", " ").replace("\xa0", " ")
    try:
        return shlex.split(line)
    except ValueError:
        return []


# 三个蒸馏/维护命令共享 /skill-create 前缀（识别需前缀匹配，勿用 ==）。
# -workflow 蒸 workflow skill（快路径），-guidance 蒸 hints 层，裸 /skill-create
# 保留给 --recheck/--retry 维护操作。
_SKILL_CREATE_COMMANDS = (
    "/skill-create-workflow", "/skill-create-guidance", "/skill-create",
)


def _skill_create_command_of(tokens: List[str]) -> str:
    return tokens[0] if tokens and tokens[0] in _SKILL_CREATE_COMMANDS else ""


def _is_skill_create_command(line: str) -> bool:
    return bool(_skill_create_command_of(_skill_create_tokens(line)))


def _extract_flag_value(tokens: List[str], flag: str) -> "tuple[Optional[str], List[str]]":
    """取 `--flag <value>` 的值并把这两个 token 从列表摘掉。

    返回 (value, tokens_without)。flag 未出现 → ("", tokens 原样)；flag 出现但
    缺值（末尾或后跟另一个 --flag）→ (None, tokens) 让调用方判用法错误。这样
    值（如 phase id p1_collection、含连字符的 skill/suite 名）不会漏进 positional。"""
    if flag not in tokens:
        return "", tokens
    i = tokens.index(flag)
    if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
        return None, tokens
    return tokens[i + 1], tokens[:i] + tokens[i + 2:]


# skill-id 必须是 slug（字母开头 + 字母数字-_）；中文任务说明之类的自由文本
# 绝不能被当成第二个位置参数吞进 skill_id。
_SKILL_ID_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# CJK 文字/标点/全角符号的起点 = 路径与粘连说明文字的切割边界
_CJK_BOUNDARY_RE = re.compile(r"[　-〿㐀-鿿豈-﫿！-～]")


def _existing_task_path(candidate: str) -> Optional[str]:
    """Resolve against cwd first, then the project root (main.py's dir), so a
    relative worktree/<id> works no matter where the CLI was launched from."""
    p = Path(candidate).expanduser()
    if p.exists():
        return str(p)
    if not p.is_absolute():
        rooted = Path(__file__).resolve().parent / candidate
        if rooted.exists():
            return str(rooted)
        if re.fullmatch(r"[0-9a-fA-F]{32}", candidate):
            task = Path(__file__).resolve().parent / "worktree" / candidate.lower()
            if task.is_dir():
                return str(task)
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


_SKILL_CREATE_USAGE = (
    "用法:\n"
    "  /skill-create-workflow <任务目录或trace.jsonl> [--skill <名称>] [--suite <名称>]"
    " [--phase <phaseId>] [--optimize|--new] [--no-test] [--no-judge] [--no-harden] [--verbose]\n"
    "      从任务蒸馏 workflow skill（快路径，happy-path 零 LLM）\n"
    "  /skill-create-guidance <任务目录或trace.jsonl> [--skill <名称>] [--suite <名称>]"
    " [--phase <phaseId>] [--optimize|--new] [--allow-unvalidated] [--no-judge] [--verbose]\n"
    "      从任务蒸馏 hints（页面知识）层：--skill 已存在则写进其 SKILL.md（双层），否则新建 hints-only\n"
    "  /skill-create --recheck <skill-id> [--no-test]\n"
    "      workflow 默认执行静态检查 + live canary 并写真实 health；--no-test 仅静态检查\n"
    "  /skill-create --retry <skill-id>     按生成记录重新蒸馏（原目录覆盖）\n"
    "提示: 含空格的路径请加引号；--suite 让多个 skill 组成技能组，"
    "跑任务时 /skill <suite名> 一次选中整组（各 phase 按四维路由到对应成员）\n"
    "退出码: 0=created/revision_candidate/hints_updated/复检通过 1=error 2=用法错误"
    " 3=needs_decision 4=质量门未过 5=aborted"
)

# Scripts/CI must be able to tell "skill ready" from "nothing usable was
# created": needs_decision is zero-write, *_blocked failed the dry-run gate,
# aborted is an explicit quit. Unknown statuses fail toward 1.
_SKILL_CREATE_EXIT_CODES = {
    "created": 0,
    "revision_candidate": 0,
    "hints_updated": 0,
    "error": 1,
    "needs_decision": 3,
    "draft_blocked": 4,
    "revision_blocked": 4,
    "aborted": 5,
}


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


def _build_objective_judge(config_path: Optional[str]):
    """LLM judge for /skill-create dedup: is the new task's objective the SAME
    business task as an existing same-domain skill's? Best-effort — any failure
    returns uncertain and the human decides."""

    def judge(objective: str, existing: Dict[str, Any]) -> Dict[str, str]:
        async def _call() -> Dict[str, str]:
            runtime = load_runtime_config(config_path or "config.json")
            provider = LLMFactory.create_provider(lead_agent_model_config(runtime))
            system_prompt = (
                "你是浏览器自动化技能库的守门人。判断【新任务目标】与【已有技能】是否是"
                "同一个业务任务（同一站点上抓取/操作同一类页面的同一类产出，字段命名差异、"
                "行数/排名范围差异不算业务不同）。只输出 JSON："
                '{"verdict": "same|different|uncertain", "reason": "简短中文理由"}'
            )
            payload = json.dumps(
                {"新任务目标": objective, "已有技能": existing},
                ensure_ascii=False,
            )
            text, _tool_calls, _stop, _usage = await provider.generate_response(
                system_prompt, [{"role": "user", "content": payload}], []
            )
            match = re.search(r"\{.*\}", text or "", re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
            return {"verdict": str(data.get("verdict") or "uncertain"),
                    "reason": str(data.get("reason") or "")}

        try:
            return _run_coro_blocking(_call())
        except Exception as exc:
            return {"verdict": "uncertain", "reason": f"LLM 判断失败: {exc}"}

    return judge


def _build_trial_runner(config_path: Optional[str]):
    """Live trial runner for /skill-create quality gate (panel required;
    degrades to attempted=False when unreachable)."""

    def run(workflow: Dict[str, Any], rows: List[Dict[str, str]]) -> Dict[str, Any]:
        async def _run() -> Dict[str, Any]:
            from harness.skill.create import trial_workflow_live
            runtime = load_runtime_config(config_path or "config.json")
            return await trial_workflow_live(
                workflow,
                rows,
                ws_config=runtime.browser,
                workflow_runtime=runtime,
            )

        try:
            return _run_coro_blocking(_run())
        except Exception as exc:
            return {"attempted": False, "runs": [], "error": str(exc)}

    return run


def _build_recheck_trial_runner(config_path: Optional[str]):
    """Full-contract live canary used by workflow ``--recheck``."""

    def run(skill: Any, source_context: Dict[str, Any]) -> Dict[str, Any]:
        async def _run() -> Dict[str, Any]:
            from harness.skill.create import recheck_skill_live
            runtime = load_runtime_config(config_path or "config.json")
            return await recheck_skill_live(
                skill,
                source_context,
                ws_config=runtime.browser,
                workflow_runtime=runtime,
            )

        try:
            return _run_coro_blocking(_run())
        except Exception as exc:
            return {
                "status": "inconclusive",
                "attempted": False,
                "reason": f"live recheck 启动失败: {exc}",
            }

    return run


def _confirm_skill_create(payload: Dict[str, Any]) -> str:
    """Interactive dedup decision: optimize the existing skill / create new / quit."""
    existing = payload.get("existing") or {}
    judgment = payload.get("judgment") or {}
    print(f"同域已有 skill `{existing.get('skill_id')}`：")
    print(f"  stage_hint 一致: {existing.get('stage_hint_match')}；"
          f"字段重叠(归一后): {', '.join(existing.get('field_overlap') or []) or '无'}")
    if existing.get("description"):
        print(f"  该 skill 目标: {existing['description'][:160]}")
    print(f"  新任务目标: {str(payload.get('objective') or '')[:160]}")
    print(f"  LLM 判断: {judgment.get('verdict')}"
          + (f" — {judgment.get('reason')}" if judgment.get("reason") else ""))
    prompt = ("[o]把 hints 写进该 skill（双层） / [n]确认业务不同,新建 hints-only / [q]放弃 > "
              if payload.get("mode") == "guidance"
              else "[o]基于已有 skill 优化 / [n]确认业务不同,新建 / [q]放弃 > ")
    while True:
        choice = input(prompt).strip().lower()
        if choice in ("o", "optimize"):
            return "optimize"
        if choice in ("n", "new"):
            return "new"
        if choice in ("q", "quit", ""):
            return "quit"


def _print_skill_create_report(report: Dict[str, Any], *, verbose: bool = False) -> int:
    """Print a create/retry report and map its status to the exit code. The
    distiller notes are developer detail — folded unless --verbose (they are
    always written into SKILL.md)."""
    for message in report.get("messages") or []:
        print(message)
    notes = report.get("notes") or []
    if notes and report.get("status") in (
        "created", "draft_blocked", "revision_candidate", "revision_blocked",
    ):
        if verbose:
            print("蒸馏器 notes:")
            for note in notes:
                print(f"  - {note}")
        else:
            print("（技术细节已写入 SKILL.md；加 --verbose 查看蒸馏器 notes）")
    return _SKILL_CREATE_EXIT_CODES.get(str(report.get("status") or ""), 1)


def _handle_skill_recheck(
    skill_id: str,
    *,
    config_path: Optional[str] = None,
    no_test: bool = False,
    skills_dir: Optional[str] = None,
    trial_runner: Optional[Any] = None,
    health: Any = None,
) -> int:
    """Recheck an existing skill.

    Workflow skills run a static gate followed by a live full-contract canary
    by default. ``--no-test`` is the explicit static-only escape hatch. Only a
    conclusive live outcome enters workflow health; infrastructure/challenge
    failures remain inconclusive and never poison the ledger.
    """
    try:
        from harness.skill import create as skill_create
        from harness.skill.registry import SKILLS_DIR_DEFAULT, SkillRegistry
        skill = SkillRegistry.load(skills_dir or SKILLS_DIR_DEFAULT).get(skill_id)
        if skill is None:
            print(f"未知技能 {skill_id!r}。可用: {', '.join(_available_skill_ids()) or '(无)'}")
            return 2
        if getattr(skill, "is_hints_only", False):
            # guidance skill 没有 workflow 契约可模拟；复检 = 确认 hints 小节
            # 存在 + 人工看过后清 needs_review（stale 上报的人工闭环终点）。
            from harness.skill.guidance import default_guidance_health, extract_hints_section
            ok = bool(extract_hints_section(skill.skill_md))
            if ok:
                default_guidance_health().mark_reviewed(skill_id)
                print(f"✅ guidance skill 复检通过: {skill_id}"
                      "（hints 小节存在；needs_review 标记已清）")
                print(f"下一步: 任务开始前输入 /skill {skill_id} 即可使用")
            else:
                print(f"⚠️ guidance skill {skill_id} 的 SKILL.md 没有 hints 小节"
                      "（## 页面知识）——补写后重跑本命令，"
                      f"或重新蒸馏: /skill-create --guidance --retry {skill_id}")
            if skill.directory is not None:
                skill_create.write_create_report(skill.directory, {
                    "status": "recheck_passed" if ok else "recheck_failed",
                    "mode": "guidance",
                    "cold_start_eligible": False,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
            return 0 if ok else 4
        source_context = skill_create.recheck_source_context(skill)
        sim = skill_create.simulate_persisted_contract(
            skill.skill_id,
            skill.workflow,
            skill.success_contract,
            skill.row_contract,
            expected_rows=source_context.get("expected_rows"),
        )
        failure_human = skill_create._humanize_failed_checks(sim["failed_checks"])
        now = datetime.now().isoformat(timespec="seconds")
        if not sim["ok"]:
            print(f"⚠️ 质量门复检未过: {skill_id}")
            for line in failure_human:
                print(f"  原因: {line}")
            print(f"  修复 skills/{skill_id}/workflow.json 或 fallback.yaml 后重跑本命令；"
                  f"或按生成记录重新蒸馏: /skill-create --retry {skill_id}")
            if skill.directory is not None:
                skill_create.write_create_report(skill.directory, {
                    "status": "recheck_failed",
                    "cold_start_eligible": False,
                    "updated_at": now,
                    "failed_checks": sim["failed_checks"],
                    "failure_human": failure_human,
                })
            return 4

        if no_test:
            print(f"⚠️ 静态检查通过，但未执行真实试运行: {skill_id}")
            print("不会生成 health 记录，也不会授予完整冷启动资格。")
            if skill.directory is not None:
                skill_create.write_create_report(skill.directory, {
                    "status": "recheck_static_passed",
                    "cold_start_eligible": False,
                    "updated_at": now,
                    "failed_checks": [],
                    "failure_human": [],
                })
            return 0

        runner = trial_runner or _build_recheck_trial_runner(config_path)
        try:
            live = runner(skill, source_context)
            if asyncio.iscoroutine(live):
                live = _run_coro_blocking(live)
        except Exception as exc:
            live = {"status": "inconclusive", "attempted": False, "reason": str(exc)}
        live = live if isinstance(live, dict) else {}
        live_status = str(live.get("status") or "inconclusive")
        if live_status not in {"passed", "failed", "inconclusive"}:
            live_status = "inconclusive"

        if health is None:
            from harness.skill.health import default_health
            health = default_health()
        if live_status == "passed":
            if hasattr(health, "reset"):
                # A successful recheck is the explicit recovery path for a
                # previously rot-disabled workflow. Reset first, then record
                # this real canary so totals still include the new success.
                health.reset(skill.skill_id)
            health.record(skill, True)
            skill_create.mark_skill_live_tested(skill)
            print(f"✅ 质量门复检通过（含 live canary）: {skill_id}")
            print(f"下一步: 输入 /skill {skill_id} 可直接使用；suite 路由将读取真实 health。")
            report_status = "recheck_passed"
            code = 0
            try:
                from harness.skill.guidance import default_guidance_health
                default_guidance_health().mark_reviewed(skill_id)
            except Exception:
                pass
        elif live_status == "failed":
            health.record(skill, False)
            print(f"⚠️ live canary 未通过: {skill_id}")
            if live.get("reason"):
                print(f"  原因: {live['reason']}")
            report_status = "recheck_failed"
            code = 4
        else:
            print(f"⚠️ live canary 无法得出结论: {skill_id}")
            print(f"  原因: {live.get('reason') or '浏览器/来源任务不可用'}")
            print("未写入成功或失败 health，请排除环境问题后重试。")
            report_status = "recheck_inconclusive"
            code = 4

        if skill.directory is not None:
            skill_create.write_create_report(skill.directory, {
                "status": report_status,
                # A conclusive live run already created health; no synthetic
                # cold-start priority is needed. Inconclusive remains inert.
                "cold_start_eligible": False,
                "updated_at": now,
                "failed_checks": list(live.get("failed_checks") or []),
                "failure_human": [str(live.get("reason") or "")] if live.get("reason") else [],
                "live_recheck": live,
            })
        return code
    except Exception as exc:  # CLI must never crash the prompt loop
        print(f"recheck 失败: {exc}")
        return 1


def _handle_skill_retry(
    skill_id: str,
    *,
    config_path: Optional[str] = None,
    no_test: bool = False,
    no_judge: bool = False,
    verbose: bool = False,
    skills_dir: Optional[str] = None,
    harden: bool = True,
) -> int:
    """/skill-create --retry <id>: regenerate a machine-generated skill in place
    from the source task recorded in its .create_report.json (human-triggered —
    generation failures are never retried automatically)."""
    try:
        from harness.skill.create import (
            create_guidance_skill_from_task,
            create_skill_from_task,
        )
        from harness.skill.registry import (
            SKILLS_DIR_DEFAULT,
            SkillRegistry,
            load_create_report,
        )
        root = Path(skills_dir) if skills_dir else Path(SKILLS_DIR_DEFAULT)
        report_data = load_create_report(root / skill_id)
        source = str(report_data.get("source_task") or "")
        if not source:
            print(f"{skill_id!r} 没有生成记录（.create_report.json），无法自动重试。")
            print("请提供原任务目录，并根据要重新蒸馏的层运行：")
            skill = SkillRegistry.load(root).get(skill_id)
            modes: List[str] = []
            if skill is None or skill.has_workflow:
                modes.append("workflow")
            if skill is None or skill.is_hints_only or bool(skill.hints):
                modes.append("guidance")
            for mode in modes:
                print(
                    f"  /skill-create-{mode} <任务目录> "
                    f"--skill {skill_id} --optimize"
                )
            return 2
        print(f"按生成记录重新蒸馏: 来源任务 {source}")
        if str(report_data.get("mode") or "") == "guidance":
            # hints_updated 的目标可能是手写 workflow skill —— 只重写 hints 小节
            # （overwrite=False 走 update 路径）；hints-only scaffold 才整目录覆盖。
            report = create_guidance_skill_from_task(
                source,
                skill_id=skill_id,
                skills_dir=root,
                phase_id=str(report_data.get("phase") or ""),
                overwrite=str(report_data.get("status") or "") == "created",
                objective_judge=None if no_judge else _build_objective_judge(config_path),
            )
        else:
            report = create_skill_from_task(
                source,
                skill_id=skill_id,
                skills_dir=root,
                phase_id=str(report_data.get("phase") or ""),
                overwrite=True,
                objective_judge=None if no_judge else _build_objective_judge(config_path),
                trial_runner=None if no_test else _build_trial_runner(config_path),
                run_trial=not no_test,
                harden=harden,
            )
    except Exception as exc:  # CLI must never crash the prompt loop
        print(f"retry 失败: {exc}")
        return 1
    return _print_skill_create_report(report, verbose=verbose)


def _handle_skill_create_command(line: str, *, config_path: Optional[str] = None) -> int:
    """Route the three /skill-create* commands.

    -workflow / -guidance distill a past task into a draft skill (dedup first,
    then quality gates); skill/suite names come from --skill/--suite flags (no
    longer positional — avoids the 07-06 "CJK note swallowed as skill_id" trap).
    Bare /skill-create keeps --recheck/--retry for maintaining an existing dir."""
    tokens = _skill_create_tokens(line)
    cmd = _skill_create_command_of(tokens)
    if not cmd:
        print(_SKILL_CREATE_USAGE)
        return 2
    optimize = "--optimize" in tokens or "--force" in tokens  # --force: legacy alias
    force_new = "--new" in tokens
    no_test = "--no-test" in tokens
    no_judge = "--no-judge" in tokens
    no_harden = "--no-harden" in tokens
    verbose = "--verbose" in tokens
    allow_unvalidated = "--allow-unvalidated" in tokens
    recheck = "--recheck" in tokens
    retry = "--retry" in tokens
    # 取值 flag（缺值 → None → 用法错误）
    skill_id, tokens = _extract_flag_value(tokens, "--skill")
    suite, tokens = _extract_flag_value(tokens, "--suite")
    phase_id, tokens = _extract_flag_value(tokens, "--phase")
    if skill_id is None or suite is None or phase_id is None:
        print(_SKILL_CREATE_USAGE)
        return 2
    positional = [t for t in tokens[1:] if not t.startswith("--")]

    # 裸 /skill-create：只做维护（recheck/retry），新建蒸馏引导到两个显式命令
    if cmd == "/skill-create":
        if recheck or retry:
            if (recheck and retry) or not positional:
                print(_SKILL_CREATE_USAGE)
                return 2
            target = skill_id or positional[0]  # --skill 或位置参数皆可
            if recheck:
                return _handle_skill_recheck(
                    target,
                    config_path=config_path,
                    no_test=no_test,
                )
            return _handle_skill_retry(target, config_path=config_path,
                                       no_test=no_test, no_judge=no_judge,
                                       verbose=verbose, harden=not no_harden)
        print("蒸馏新技能请用显式命令：")
        print("  /skill-create-workflow <任务目录> [--skill <名称>] [--suite <名称>] [--phase <phaseId>]")
        print("  /skill-create-guidance <任务目录> [--skill <名称>] [--suite <名称>] [--phase <phaseId>]")
        print(_SKILL_CREATE_USAGE)
        return 2

    if not positional or (optimize and force_new):
        print(_SKILL_CREATE_USAGE)
        print("示例: /skill-create-workflow worktree/5d69c57de8c0454893ea782940b97a1d"
              " --skill taaft-detail-extract --suite taaft-trending")
        return 2
    path, rest = _recover_task_path(positional)
    if rest:  # skill/suite 走 flag 了，剩下的 positional 都是被忽略的附加说明
        print("已忽略附加说明: " + " ".join(rest))
    decision = "optimize" if optimize else ("new" if force_new else "")
    try:
        if cmd == "/skill-create-guidance":
            from harness.skill.create import create_guidance_skill_from_task
            report = create_guidance_skill_from_task(
                path,
                skill_id=skill_id,
                suite=suite,
                phase_id=phase_id,
                decision=decision,
                confirm=_confirm_skill_create if sys.stdin.isatty() else None,
                objective_judge=None if no_judge else _build_objective_judge(config_path),
                allow_unvalidated=allow_unvalidated,
            )
        else:  # /skill-create-workflow
            from harness.skill.create import create_skill_from_task
            report = create_skill_from_task(
                path,
                skill_id=skill_id,
                suite=suite,
                phase_id=phase_id,
                decision=decision,
                confirm=_confirm_skill_create if sys.stdin.isatty() else None,
                objective_judge=None if no_judge else _build_objective_judge(config_path),
                trial_runner=None if no_test else _build_trial_runner(config_path),
                run_trial=not no_test,
                harden=not no_harden,
            )
    except Exception as exc:  # CLI must never crash the prompt loop
        print(f"skill-create 失败: {exc}")
        return 1
    return _print_skill_create_report(report, verbose=verbose)


def _handle_skill_command(line: str, args: argparse.Namespace) -> str:
    """Process a `/skill ...` line typed at the task prompt. Mutates args.skill
    and returns any inline task text after the id (empty -> caller re-prompts)."""
    tokens = line.split()
    arg = tokens[1] if len(tokens) > 1 else ""
    inline_task = " ".join(tokens[2:]).strip()
    ids = _available_skill_ids()
    suites = _available_suite_names()
    if not arg or arg in ("list", "ls", "?"):
        print("可用技能:", ", ".join(_skill_display_lines()) or "(无)")
        if suites:
            print("可用技能组(suite):", ", ".join(suites))
        if args.skill:
            print(f"当前已选: {args.skill}")
        print("用法: /skill <id|suite> 选取；/skill off 取消；/skill 列出；"
              "/skill-create-workflow|-guidance <任务目录> 从历史任务蒸馏新技能")
        return ""
    if arg in ("off", "none", "clear", "-"):
        args.skill = ""
        print("已取消技能强制。")
        return inline_task
    # /skill <name>：name 可以是单个 skill_id 或一个 suite 名（展开成成员集合）。
    # forced_skill_id 携逗号分隔集合串，spawn 时按 phase 四维路由到唯一成员。
    expanded, is_suite = _expand_skill_selection(arg)
    if not expanded:
        avail = ", ".join(ids) + (f"；技能组: {', '.join(suites)}" if suites else "")
        print(f"未知技能/技能组 {arg!r}。可用: {avail or '(无)'}")
        return ""
    args.skill = ",".join(expanded)
    if is_suite:
        print(f"已选技能组: {arg} → {', '.join(expanded)}"
              "（各阶段按四维路由到对应成员；不匹配的阶段自动回落）")
    else:
        print(f"已选技能: {arg}（本次运行强制使用；变量无法派生的阶段会自动回落）")
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
            f"[{selected_mode}] 请输入浏览器任务（/resume <任务目录> 恢复未完成的原任务；"
            "可先用 /skill <id|suite> 指定技能，/skill 列出，"
            "/skill-create-workflow|-guidance <任务目录> 蒸馏新技能）: "
            if selected_mode
            else "模式未选择> "
        )
        line = input(prompt).strip()
        # /skill-create* must route BEFORE /skill (shared prefix)
        if _is_skill_create_command(line):
            _handle_skill_create_command(line, config_path=getattr(args, "config", None))
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
        print("用法: /resume <worktree任务目录>（仅恢复原任务）")
        return None
    path, rest = _recover_task_path(tokens[1:])
    if rest:
        print("/resume 仅恢复原任务，不接受新指令。新目标请新建任务。")
        return None
    args.resume = path
    args.resume_instruction = ""
    return ""


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
    """Resolve an existing task directory without ever creating it."""

    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("缺少 worktree 任务目录")
    candidate = Path(raw).expanduser()
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates.append(Path(__file__).resolve().parent / candidate)
        if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
            candidates.append(Path(__file__).resolve().parent / "worktree" / raw.lower())
    for item in candidates:
        try:
            resolved = item.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if resolved.is_dir():
            return resolved
    raise ValueError(
        "worktree 目录不存在或已被删除；无法恢复，也不会自动重建空任务目录"
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


def _open_task_storage(logger, runtime) -> None:
    """Attach the configured backend and open this launch's run row.

    Must run after ``logger.run_id`` is assigned: the run id is a relational
    field on every event and a foreign key in the database, so it has to exist
    before the first write rather than being backfilled.
    """

    def _report_revision_conflict(detail: JsonDict) -> None:
        # Raised outside the failed transaction. With .run.lock holding other
        # processes off, this normally means two callbacks inside this process
        # raced - worth seeing, not worth failing over.
        logger.write("storage.revision_conflict", detail)

    def _report_verification(report: JsonDict) -> None:
        logger.write("storage.dual_verify", report)

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


def _print_browser_mode_summary(result: JsonDict, task_dir: str) -> None:
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
            for blocker in parsed.get("blockers") or []:
                print(f"阻塞: {blocker}", flush=True)
            for step_text in parsed.get("next_steps") or []:
                print(f"下一步: {step_text}", flush=True)
        elif isinstance(parsed, dict) and parsed.get("evidence"):
            for path in parsed.get("evidence") or []:
                print(f"产物: {path}", flush=True)
        else:
            print(answer, flush=True)
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
    if status in {"done", "completed", "validated_done"}:
        return
    # Resume reads task_plan.json. A run that died before a plan was accepted
    # has none, and pointing the user at resume there only produces a second,
    # different error (verified on run c7c931b7: ResumeStateError, "task plan
    # is missing").
    plan_path = Path(task_dir) / "task_plan.json" if task_dir else None
    if plan_path is not None and plan_path.exists():
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


def _browser_mode_failure(
    harness: LeadAgent,
    *,
    code: str,
    error: str,
    tool_was_executed: bool = False,
) -> JsonDict:
    """Single logged exit for every terminal browser-mode abort.

    Run 18daa415 ended on a structured early return that wrote no event at
    all, leaving run.jsonl with nothing to diagnose beyond an empty usage
    summary. Every failure exit from browser mode goes through here so a
    failed run always says why it failed in its own log.
    """
    _safe_logger_write(
        harness.logger,
        "direct_mode.failed",
        {
            "code": code,
            "error": error,
            "toolWasExecuted": tool_was_executed,
        },
    )
    return {
        "status": "failed",
        "error": error,
        "code": code,
        "tool_was_executed": tool_was_executed,
    }


async def _run_browser_mode(
    harness: LeadAgent,
    *,
    task: str,
    original_task: str,
    resume_context: Optional[ResumeContext],
) -> JsonDict:
    """Run the explicit browser entry without a Lead model turn.

    LeadAgent remains the shared execution host because the direct worker path
    uses its coordinator, lifecycle, assignment review and persistence methods.
    Dispatch acknowledges startup; this entry must collect the worker's actual
    result before the CLI can finish the run. It makes no Lead model turn and
    never automatically dispatches a continuation.
    """
    if resume_context is not None:
        if str(resume_context.instruction or "").strip():
            return _browser_mode_failure(
                harness,
                code="browser_mode_resume_instruction_unsupported",
                error="browser mode cannot apply a new instruction to a resumed plan",
            )
        plan = resume_context.current_plan
        phases = plan.get("phases") if isinstance(plan, dict) else None
        if (
            not isinstance(plan, dict)
            or plan.get("execution_mode") not in {"delegated", "direct_worker"}
            or not isinstance(phases, list)
            or len(phases) != 1
        ):
            return _browser_mode_failure(
                harness,
                code="browser_mode_resume_plan_unsupported",
                error="browser mode resume requires one accepted delegated phase",
            )
        harness.original_user_task = str(resume_context.original_user_task or task)
        harness.spawner.root_task = harness.original_user_task
        await harness._bootstrap_schema_cache()
        result = await _lead_spawn_browser_agent(ToolContext(
            agent=harness,
            tool_call={"name": "spawn_browser_agent", "id": "browser-resume"},
            tool_input={"phase_id": str(phases[0].get("id") or "")},
            step=0,
        ))
        if not isinstance(result, dict):
            return _browser_mode_failure(
                harness, code="browser_mode_resume_no_receipt",
                error="direct resume returned no receipt",
            )
        return await _wait_for_browser_mode_result(harness, result)

    fleet_reference = str(
        getattr(harness, "task_fleet_reference", "") or ""
    ).strip()
    if not fleet_reference:
        fleet_reference, fleet_error = extract_fleet_reference(original_task)
        if fleet_error:
            return _browser_mode_failure(
                harness,
                code="browser_mode_fleet_reference_invalid",
                error=fleet_error,
            )
        # Normal CLI construction sets this once before the Lead is created.
        # Keep direct programmatic callers on the same control-plane route.
        harness.task_fleet_reference = fleet_reference or ""

    harness.logger.write("direct_mode.assignment_started", {
        "fleetReferenceSource": "task_text" if fleet_reference else None,
    })
    harness.original_user_task = str(original_task or task)
    harness.spawner.root_task = harness.original_user_task
    await harness._bootstrap_schema_cache()
    result = await _submit_direct_plan(harness, original_task)
    return await _wait_for_browser_mode_result(harness, result)


async def _wait_for_browser_mode_result(
    harness: LeadAgent, spawned: JsonDict,
) -> JsonDict:
    """Collect exactly the dispatched worker, preserving its evidence and status.

    The spawn tool stays asynchronous for Lead orchestration. Waiting here has
    no polling or replay policy; cancellation is handled by the CLI owner while
    storage is still open.
    """
    if spawned.get("status") != "running":
        return spawned
    worker_id = spawned.get("workerId")
    if not isinstance(worker_id, str) or not worker_id.strip():
        return _browser_mode_failure(
            harness, code="browser_mode_worker_receipt_invalid",
            error="Worker startup returned running without a workerId.",
            tool_was_executed=True,
        )
    waited = await _lead_wait_browser_agents(ToolContext(
        agent=harness,
        tool_call={"name": "wait_browser_agents", "id": "browser-wait"},
        tool_input={"worker_ids": [worker_id], "mode": "all"},
        step=0,
    ))
    completed = waited.get("completed") if isinstance(waited, dict) else None
    matches = [item for item in (completed if isinstance(completed, list) else [])
               if isinstance(item, dict) and item.get("workerId") == worker_id]
    phase_id = spawned.get("phaseId")
    if (len(matches) != 1
            or worker_id in (waited.get("pending") or [])
            or not matches[0].get("status")
            or matches[0]["status"] == "running"
            or (phase_id and matches[0].get("phaseId") != phase_id)):
        return _browser_mode_failure(
            harness, code="browser_mode_worker_receipt_invalid",
            error=f"Wait returned no consistent terminal receipt for {worker_id}.",
            tool_was_executed=True,
        )
    result = dict(matches[0])
    worker_status = result["status"]
    # Preserve the former direct entry's contract requirement without treating
    # contract validation alone as proof of goal completion.
    if worker_status == "done" and result.get("validatedStatus") != "validated_done":
        result["status"] = "incomplete"
        result["reason"] = (
            "Worker reported done, but the assignment contract was not validated "
            f"(validatedStatus={result.get('validatedStatus') or 'missing'})."
        )
    result["directExecution"] = {
        "mode": "browser", "workerId": worker_id,
        "phaseId": phase_id, "workerStatus": worker_status,
    }
    for key in ("assignmentId", "assignmentAccepted", "assignmentReview", "budget"):
        if key in spawned:
            result[key] = spawned[key]
    for key in ("connectionRecovery", "operatorInputRecords", "scheduleSnapshot"):
        if key in waited:
            result[key] = waited[key]
    return result


def _browser_mode_terminal_error(result: JsonDict) -> Optional[JsonDict]:
    """A durable reason for a non-success receipt, without inventing a cause."""
    status = str(result.get("status") or "unknown")
    if status in {"done", "completed", "validated_done"}:
        return None
    error = result.get("error")
    detail = error.get("message") if isinstance(error, dict) else error
    failure = {
        "code": result.get("code") or "browser_mode_not_completed",
        "message": str(detail or result.get("reason")
                       or f"Browser execution ended with status={status}.")[:2000],
        "status": status,
        "workerId": result.get("workerId"),
        "validatedStatus": result.get("validatedStatus"),
    }
    review = result.get("review")
    if isinstance(review, dict):
        failure["review"] = {
            key: review[key] for key in ("status", "auditPath", "errors", "verdict")
            if key in review
        }
    if isinstance(result.get("errors"), list):
        failure["errors"] = result["errors"]
    return failure


async def _shutdown_browser_mode(harness: LeadAgent) -> bool:
    """Drain cancellation records before storage closes, even on another cancel."""
    shutdown = asyncio.create_task(harness.spawner.shutdown())
    cancelled = False
    while True:
        try:
            await asyncio.shield(shutdown)
            return cancelled
        except asyncio.CancelledError:
            if shutdown.done():
                # An internal shutdown cancellation is a cleanup failure, not
                # permission to quietly close storage with unfinished workers.
                shutdown.result()
                return True
            cancelled = True


async def _submit_direct_plan(harness: LeadAgent, original_task: str) -> JsonDict:
    assignment = {"task": str(original_task or "").strip()}
    result = await _lead_spawn_browser_agent(ToolContext(
        agent=harness,
        tool_call={"name": "spawn_browser_agent", "id": "browser-assignment"},
        tool_input={"assignment": assignment},
        step=0,
    ))
    return result if isinstance(result, dict) else _browser_mode_failure(
        harness,
        code="browser_mode_direct_pipeline_no_receipt",
        error="browser mode direct pipeline returned no receipt",
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
        sqlite_path=resolve_sqlite_path(
            runtime.harness.storage_sqlite_path, runtime.harness.worktree_dir
        ),
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
    if resume_requested and str(task or "").strip():
        print("/resume 仅恢复原任务，不接受 --task 或附加新指令；请新建任务。", flush=True)
        return CLI_INPUT_ERROR_EXIT_CODE
    if not task and not resume_requested:
        print("没有收到任务。")
        return 2
    if _is_skill_create_command(task):
        return _handle_skill_create_command(task, config_path=getattr(args, "config", None))
    # --skill / interactive /skill both land on args.skill; force it for this run
    # without editing config. A name may be a skill_id OR a suite; expand each
    # segment to member ids (idempotent — interactive /skill already comma-joins,
    # and skill_id→itself), dedup preserving order. forced_skill_id then carries
    # the collection string that apply_forced_skill routes per phase.
    forced_skill = str(getattr(args, "skill", "") or "").strip()
    if forced_skill:
        seen: set = set()
        final: List[str] = []
        for part in (p.strip() for p in forced_skill.split(",") if p.strip()):
            exp, _is_suite = _expand_skill_selection(part)
            for sid in (exp or [part]):
                if sid not in seen:
                    seen.add(sid)
                    final.append(sid)
        forced_skill = ",".join(final)
        runtime.harness.forced_skill_id = forced_skill
        print(f"技能强制: {forced_skill}", flush=True)
    elif not resume_requested:
        _hint_matching_skills(task)

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
                # The lock is acquired before reading the generation so a live
                # process cannot change plan/state between validation and use.
                run_lock = acquire_run_lock(task_dir)
                current_plan = load_task_plan_strict(task_dir)
                prior_state = load_task_state_strict(task_dir)
                manifest = load_task_manifest(task_dir)
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

                initial_plan, initial_plan_recovered = _load_initial_plan(
                    task_dir, current_plan,
                )
                runtime.harness.worktree_dir = str(task_dir.parent)
                logger = RunLogger(
                    str(task_dir.parent),
                    task_id=task_dir.name,
                    on_event=ConsoleProgressReporter(),
                )
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
            logger = RunLogger(
                runtime.harness.worktree_dir,
                on_event=ConsoleProgressReporter(),
            )
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
        print(f"运行日志: {logger.path}", flush=True)
        if resume_context is not None:
            print("按原计划恢复：先检查浏览器连接；连接不可用时停止，不调用模型或派发 worker。", flush=True)
        elif agent_mode == "browser":
            print("Browser 模式：先做轻量任务分类，再进入直达 worker。", flush=True)
        elif sys.stdin.isatty():
            print("开始生成执行计划；确认前不会启动 BrowserAgent。", flush=True)
        else:
            print("开始执行，关键进度会在这里显示。", flush=True)

        provider = LLMFactory.create_provider(lead_agent_model_config(runtime))
        harness = LeadAgent(
            provider,
            runtime,
            logger,
            resume=resume_context,
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
            # Human summary first; the machine receipt JSON follows below and
            # remains the parseable contract for embedding hosts.
            _print_browser_mode_summary(
                browser_result, str(logger.task_dir or ""),
            )
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
        if not _print_text(f"\n运行日志: {logger.path}"):
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
    if raw_argv and raw_argv[0] in _SKILL_CREATE_COMMANDS:
        line = " ".join(shlex.quote(part) for part in raw_argv)
        return _handle_skill_create_command(line)
    if raw_argv and raw_argv[0] == "/resume":
        if len(raw_argv) < 2:
            print("用法: /resume <worktree任务目录>（仅恢复原任务）")
            return 2
        path, rest = _recover_task_path(raw_argv[1:])
        raw_argv = ["--resume", path]
        if rest:
            print("/resume 仅恢复原任务，不接受新指令；请新建任务。")
            return CLI_INPUT_ERROR_EXIT_CODE
        argv = raw_argv

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if getattr(args, "list_skills", False):
        ids = _available_skill_ids()
        print("可用技能:", ", ".join(ids) or "(无)")
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
