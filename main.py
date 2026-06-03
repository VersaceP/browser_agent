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
        if event_type == "task_state.initialized":
            return f"[TaskState] 已初始化: {payload.get('path')}"
        if event_type == "progress.intervention":
            return (
                f"[Progress] 干预 {payload.get('tool') or '?'}: "
                f"{payload.get('reason') or 'no progress'}"
            )
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
        if tool_summaries:
            return f"[{role}] 模型返回，准备调用: {'; '.join(tool_summaries)}"
        text = str(payload.get("text") or "").strip().replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        return f"[{role}] 模型返回: {text or '(无文本)'}"

    def _format_tool_call(self, tool_call: Dict[str, Any]) -> str:
        name = str(tool_call.get("name") or "unknown")
        raw_input = tool_call.get("input") or {}
        if name != "browser_call" or not isinstance(raw_input, dict):
            return name

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
        if status == "failed":
            return f"[BrowserAgent] {worker_id} 失败: {payload.get('error') or 'unknown error'}"
        validated = payload.get("validatedStatus")
        if validated:
            return f"[BrowserAgent] {worker_id} 完成，验收状态: {validated}"
        if status == "done":
            return f"[BrowserAgent] {worker_id} 完成。"
        return f"[BrowserAgent] {worker_id} 状态: {status}"


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


def read_task(args: argparse.Namespace) -> str:
    if args.task_option:
        return args.task_option
    if args.task:
        return args.task
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return input("请输入浏览器任务: ").strip()


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

    logger = RunLogger(
        runtime.harness.worktree_dir,
        on_event=ConsoleProgressReporter(),
    )
    _LAST_LOGGER = logger
    runtime.harness.runs_dir = str(logger.task_dir)
    runtime.harness.artifacts_dir = str(logger.artifacts_dir)
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _CANCELLED_LOGGED

    parser = build_arg_parser()
    args = parser.parse_args(argv)
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
