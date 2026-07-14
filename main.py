"""
main.py - CLI entrypoint for ABCP Agent Harness.
"""

import argparse
import asyncio
import json
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import readline  # noqa: F401  启用 input() 的行编辑（退格/方向键）
except ImportError:
    pass

from abcp_client import ABCPClient
from agent_harness import (
    BrowserAgent,
    LeadAgent,
    browser_agent_model_config,
    exception_payload,
    lead_agent_model_config,
)
from harness.utils import RunLogger, make_browser_event_logger
from llm import LLMFactory
from runtime_config import RuntimeConfig, load_runtime_config


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
            provider = LLMFactory.create_provider(lead_agent_model_config(runtime.model))
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
            return await trial_workflow_live(workflow, rows, ws_config=runtime.browser)

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


def read_task(args: argparse.Namespace) -> str:
    if args.task_option:
        return args.task_option
    if args.task:
        return args.task
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    while True:
        line = input("请输入浏览器任务（可先用 /skill <id|suite> 指定技能，/skill 列出，"
                     "/skill-create-workflow|-guidance <任务目录> 蒸馏新技能）: ").strip()
        # /skill-create* must route BEFORE /skill (shared prefix)
        if _is_skill_create_command(line):
            _handle_skill_create_command(line, config_path=getattr(args, "config", None))
            continue
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
    else:
        _hint_matching_skills(task)

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

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in _SKILL_CREATE_COMMANDS:
        line = " ".join(shlex.quote(part) for part in raw_argv)
        return _handle_skill_create_command(line)

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
