"""
main.py - CLI entrypoint for ABCP Agent Harness.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import readline  # noqa: F401  启用 input() 的行编辑（退格/方向键）
except ImportError:
    pass

from abcp_client import ABCPClient, ABCPClientConfig
from agent_harness import (
    BrowserAgent,
    HarnessConfig,
    LeadAgent,
    RuntimeConfig,
    VLConfig,
    browser_agent_model_config,
    exception_payload,
    lead_agent_model_config,
)
from harness.utils import RunLogger, make_browser_event_logger
from llm import LLMFactory, ModelConfig


_LAST_LOGGER: Optional[RunLogger] = None
_CANCELLED_LOGGED = False


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
        if event_type == "lead.step.start":
            return f"[LeadAgent] 第 {payload.get('step')} 步：请求模型..."
        if event_type == "agent.step.start":
            return f"[BrowserAgent] 第 {payload.get('step')} 步：请求模型..."
        if event_type in {"lead.model", "agent.model"}:
            return self._format_model_event(event_type, payload)
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
        if event_type == "progress.intervention":
            return (
                f"[Progress] 干预 {payload.get('tool') or '?'}: "
                f"{payload.get('reason') or 'no progress'}"
            )
        if event_type == "lead.step_cap.reminder":
            return (
                f"[LeadAgent] 接近步数上限: step={payload.get('step')} "
                f"remaining={payload.get('remaining')}"
            )
        if event_type == "agent.step_cap.reminder":
            return (
                f"[BrowserAgent] 接近步数上限: step={payload.get('step')} "
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
            skills = payload.get("skills_doc_chars") or 0
            return (
                f"[Schema] 加载 {count} 条 method schema "
                f"({req} 个需要 purpose)；skillsDoc {skills} 字符。"
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
        role = "LeadAgent" if event_type == "lead.model" else "BrowserAgent"
        tool_calls = payload.get("tool_calls") or []
        tool_summaries = [
            self._format_tool_call(item)
            for item in tool_calls
            if isinstance(item, dict)
        ]
        text = self._short_text(payload.get("text"), 100)
        if tool_summaries:
            if text:
                return (
                    f"[{role}] 模型返回: {text}；"
                    f"准备调用: {'; '.join(tool_summaries)}"
                )
            return f"[{role}] 模型返回，准备调用: {'; '.join(tool_summaries)}"
        return f"[{role}] 模型返回: {text or '(无文本)'}"

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


def load_runtime_config(config_path: str) -> RuntimeConfig:
    path = Path(config_path)
    raw = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))

    model = ModelConfig.load_from_file(config_path)
    browser_raw = raw.get("browser", {})
    harness_raw = raw.get("harness", {})

    harness = HarnessConfig.from_dict(harness_raw)
    # VL is configured only at the top level of config.json:
    # {"vl": {"enabled": true, "provider": "openai", ...}}
    harness.vl = VLConfig.from_dict(raw.get("vl", {}))

    return RuntimeConfig(
        agent_id=browser_raw.get("agent_id") or raw.get("agent_id", "abcp-agent"),
        model=model,
        browser=ABCPClientConfig.from_dict(browser_raw),
        harness=harness,
    )


def _available_skill_ids() -> List[str]:
    try:
        from harness.skill.registry import SkillRegistry
        return [s.skill_id for s in SkillRegistry.load().all()]
    except Exception:
        return []


def _handle_skill_command(line: str, args: argparse.Namespace) -> str:
    """Process a `/skill ...` line typed at the task prompt. Mutates args.skill
    and returns any inline task text after the id (empty -> caller re-prompts)."""
    tokens = line.split()
    arg = tokens[1] if len(tokens) > 1 else ""
    inline_task = " ".join(tokens[2:]).strip()
    ids = _available_skill_ids()
    if not arg or arg in ("list", "ls", "?"):
        print("可用技能:", ", ".join(ids) or "(无)")
        if args.skill:
            print(f"当前已选: {args.skill}")
        print("用法: /skill <id> 选取；/skill off 取消；/skill 列出")
        return ""
    if arg in ("off", "none", "clear", "-"):
        args.skill = ""
        print("已取消技能强制。")
        return inline_task
    if ids and arg not in ids:
        print(f"未知技能 {arg!r}。可用: {', '.join(ids) or '(无)'}")
        return ""
    args.skill = arg
    print(f"已选技能: {arg}（本次运行强制使用；变量无法派生的阶段会自动回落）")
    return inline_task


def read_task(args: argparse.Namespace) -> str:
    if args.task_option:
        return args.task_option
    if args.task:
        return args.task
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    while True:
        line = input("请输入浏览器任务（可先用 /skill <id> 指定技能，/skill 列出）: ").strip()
        if line.startswith("/skill"):
            inline_task = _handle_skill_command(line, args)
            if inline_task:
                return inline_task
            continue
        return line


async def run_cli(args: argparse.Namespace) -> int:
    global _CANCELLED_LOGGED, _LAST_LOGGER

    runtime = load_runtime_config(args.config)
    if args.agent_id:
        runtime.agent_id = args.agent_id
    if args.max_steps:
        runtime.harness.max_steps = args.max_steps
        runtime.harness.worker_max_steps = args.max_steps
    mode = args.mode or runtime.harness.mode

    task = read_task(args)
    if not task:
        print("没有收到任务。")
        return 2

    # --skill / interactive /skill both land on args.skill; force it for this run
    # without editing config. Unknown ids are validated + ignored downstream.
    forced_skill = str(getattr(args, "skill", "") or "").strip()
    if forced_skill:
        runtime.harness.forced_skill_id = forced_skill
        print(f"技能强制: {forced_skill}", flush=True)

    logger = RunLogger(
        runtime.harness.worktree_dir,
        on_event=ConsoleProgressReporter(),
    )
    _LAST_LOGGER = logger
    runtime.harness.runs_dir = str(logger.task_dir)
    print(f"任务已创建: {logger.task_id}", flush=True)
    print(f"模式: {mode}", flush=True)
    print(f"任务目录: {logger.task_dir}", flush=True)
    print(f"运行日志: {logger.path}", flush=True)
    print("开始执行，关键进度会在这里显示。", flush=True)

    artifacts: List[str] = []
    try:
        if mode == "single":
            provider = LLMFactory.create_provider(
                browser_agent_model_config(runtime.model)
            )
            event_logger = make_browser_event_logger(
                logger,
                runtime.harness.log_browser_payloads,
            )
            async with ABCPClient(runtime.browser, on_event=event_logger) as browser:
                harness = BrowserAgent(provider, browser, runtime, logger)
                answer = await harness.run(task)
                artifacts = harness.artifacts
        elif mode == "lead":
            provider = LLMFactory.create_provider(
                lead_agent_model_config(runtime.model)
            )
            harness = LeadAgent(provider, runtime, logger)
            answer = await harness.run(task)
        else:
            print(f"未知 mode: {mode}")
            logger.write("run.error", {"error": f"未知 mode: {mode}"})
            return 2
    except asyncio.CancelledError as exc:
        _CANCELLED_LOGGED = True
        logger.write(
            "run.cancelled",
            exception_payload(exc, mode=mode, task=task),
        )
        raise
    except Exception as exc:
        logger.write(
            "run.error",
            exception_payload(exc, mode=mode, task=task),
        )
        raise
    finally:
        logger.write_usage_summary()

    print(answer)
    print(f"\n任务ID: {logger.task_id}")
    print(f"\n任务目录: {logger.task_dir}")
    print(f"\n运行日志: {logger.path}")
    if artifacts:
        print("Artifacts:")
        for artifact in artifacts:
            print(f"- {artifact}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ABCP Browser Agent Harness")
    parser.add_argument("task", nargs="?", help="要交给浏览器 agent 完成的任务")
    parser.add_argument("--task", dest="task_option", help="要交给浏览器 agent 完成的任务")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--agent-id", help="覆盖 config.json 中的 browser.agent_id")
    parser.add_argument("--max-steps", type=int, help="覆盖最大 agent 编排步数")
    parser.add_argument(
        "--mode",
        choices=["lead", "single"],
        help="lead=多 agent 编排；single=旧版单 browser agent",
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _CANCELLED_LOGGED

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if getattr(args, "list_skills", False):
        ids = _available_skill_ids()
        print("可用技能:", ", ".join(ids) or "(无)")
        return 0
    try:
        return asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        if _LAST_LOGGER is not None and not _CANCELLED_LOGGED:
            _LAST_LOGGER.write("run.cancelled", {"reason": "KeyboardInterrupt"})
            _CANCELLED_LOGGED = True
        print("已停止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
