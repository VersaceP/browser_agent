"""Readable terminal projections; full transcripts and receipts stay in storage."""
from __future__ import annotations

import json
import difflib


def readable_diff(path, before, after):
    """Show embedded JS as source lines while retaining other package fields."""
    def diff(name, left, right):
        lines = list(difflib.unified_diff(left.splitlines(), right.splitlines(),
            fromfile="before/" + name, tofile="after/" + name, lineterm=""))
        return "\n".join(lines) + "\n" if lines else ""
    try:
        left, right = json.loads(before or "{}"), json.loads(after or "{}")
    except ValueError:
        return diff(path, before, after)
    if isinstance(left, dict) and isinstance(right, dict) and ("files" in left or "files" in right):
        files_left, files_right = left.get("files", {}), right.get("files", {})
        if isinstance(files_left, dict) and isinstance(files_right, dict) and all(
                isinstance(v, str) for v in [*files_left.values(), *files_right.values()]):
            metadata = diff(path, json.dumps({k: v for k, v in left.items() if k != "files"}, ensure_ascii=False, indent=2) + "\n",
                json.dumps({k: v for k, v in right.items() if k != "files"}, ensure_ascii=False, indent=2) + "\n")
            return metadata + "".join(diff(path + "::" + name, files_left.get(name, ""), files_right.get(name, ""))
                for name in sorted(set(files_left) | set(files_right)))
    return diff(path, json.dumps(left, ensure_ascii=False, indent=2) + "\n", json.dumps(right, ensure_ascii=False, indent=2) + "\n")


def block(label, title, fields=()):
    from harness.runtime.hitl_input import notify_terminal_output
    notify_terminal_output()
    print(f"\n[{label}] {title}", flush=True)
    for key, value in fields:
        if value is not None and value != "":
            print(f"  {key}: {value}", flush=True)


def message(label, text):
    block(label, "说明")
    for line in str(text).strip().splitlines():
        print(f"  {line}", flush=True)


def tool_result(label, name, result, *, error=False):
    try:
        value = json.loads(result.text)
    except (ValueError, AttributeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    from harness.runtime.hitl_input import notify_terminal_output
    notify_terminal_output()
    # Independent trials nest the same consumer receipt one level deeper.
    nested = (value.get("result") or {}).get("execution") if isinstance(value.get("result"), dict) else None
    if isinstance(nested, dict):
        value = {**value, **nested}
    status = "失败" if error else value.get("status") or "返回"
    details = []
    for key in ("path", "analysisTaskId", "trialTaskId", "pageId", "executionId", "stepId"):
        if value.get(key):
            details.append(f"{key}={value[key]}")
    source = value.get("activeSourceHash") or value.get("sourceHash") or (value.get("skill") or {}).get("hash")
    if source:
        details.append(f"版本={source[:12]}")
    execution = (value.get("answer") or {}).get("executed") if isinstance(value.get("answer"), dict) else None
    answer = value.get("answer") if isinstance(value.get("answer"), dict) else {}
    script = answer.get("execution") or {}
    if isinstance(script, dict) and script.get("status"):
        details.append("脚本=" + script["status"])
        if script.get("failedErrorCode"):
            details.append("错误=" + script["failedErrorCode"])
    elif answer.get("status") in {"rejected", "unavailable", "page_binding_rejected"}:
        details.append("执行=" + answer["status"])
        if answer.get("reason"):
            details.append(answer["reason"])
    if value.get("executorId"):
        details.append("执行器=" + value["executorId"])
    if isinstance(execution, list):
        details.append(f"执行阶段={len(execution)}")
        for step in execution:
            facts = (step.get("output") or {}).get("execution") or {}
            if facts.get("status") and facts["status"] != "succeeded":
                code = facts.get("failedActionCode") or facts.get("failedErrorCode")
                details.append(f"阶段={step.get('stepId')}；脚本={facts['status']}" + (f"；错误={code}" if code else ""))
    reason = value.get("reason") or value.get("message")
    if error and reason:
        details.append(str(reason)[:500])
    print(f"[{label}] {name} → {status}" + (" | " + "；".join(details) if details else ""), flush=True)


def result(label, value):
    if not isinstance(value, dict):
        message(label, str(value))
        return
    info = value.get("skill") or {}
    block(label, f"结果 · {value.get('status', '返回')}", [
        ("任务", "@" + str(value.get("taskId") or value.get("builderTaskId")))
            if value.get("taskId") or value.get("builderTaskId") else ("任务", None),
        ("Skill", (info.get("metadata") or {}).get("name") or value.get("skillId")),
        ("版本", info.get("version")),
        ("源码版本", info.get("hash") or value.get("activeSourceHash") or value.get("skillHash")),
        ("目录", value.get("workDir")),
        ("模型停止原因", (value.get("modelStop") or {}).get("rawStopReason") or
                          (value.get("modelStop") or {}).get("stopReason")),
        ("模型错误", (value.get("modelStop") or {}).get("error")),
    ])
    if value.get("summary"):
        print("\n" + str(value["summary"]).strip(), flush=True)
    for title, key in (("尚需处理", "remainingWork"), ("程序及验证限制", "knownLimitations"), ("下一步", "next")):
        items = value.get(key) or []
        if isinstance(items, str):
            items = [items]
        if items:
            print(f"\n{title}:", flush=True)
            for item in items:
                print(f"  - {item}", flush=True)
    evidence = value.get("programEvidence") or value.get("validation")
    if isinstance(evidence, dict):
        coverage = (evidence.get("stageValidation") or {}).get("goalCoverage") or evidence.get("goalCoverage")
        if isinstance(coverage, dict):
            print("\n目标覆盖声明（不等于实际验证；发布由用户决定）:", flush=True)
            if coverage.get("status") != "declared":
                print("  - 尚未声明完整目标与阶段的对应关系，不能据现有阶段推断全流程可复用。", flush=True)
            for item in coverage.get("requirements", []):
                print(f"  - {item.get('requirement', '(invalid requirement)')}: {item.get('coverage', 'unknown')}"
                      + ("；源码或阶段已变化，需重新核对" if item.get("versionStatus") == "stale" else ""), flush=True)
        stages = (evidence.get("stageValidation") or {}).get("stages", [])
        if stages:
            print("\n程序阶段验证（当前源码与合同，页面及来源见回执）:", flush=True)
            for stage in stages:
                assessment = stage.get("assessment") or {}
                print(f"  - {stage['id']}: {assessment.get('verdict', 'unassessed')}"
                      + f"；验证路径={assessment.get('executionScope', 'unspecified')}"
                      + (f" — {assessment['reason']}" if assessment.get('reason') else ""), flush=True)
        paths = evidence.get("workflows", [])
        if paths:
            print("\n程序执行证据（业务是否达成由审查判断）:", flush=True)
            for item in paths:
                print(f"  - {item['path']}: 当前版本回执 {len(item.get('currentExecutions', []))}；"
                      f"历史其他版本回执 {item.get('otherVersionExecutions', 0)}", flush=True)
    if value.get("error") or value.get("cleanupError"):
        print("\n错误: " + json.dumps(value.get("error") or value.get("cleanupError"), ensure_ascii=False), flush=True)


def tool_start(label, name, *, task_id, args):
    params, stage = args.get("params"), args.get("stage")
    page = args.get("page_id") or (params.get("pageId") if isinstance(params, dict) else None)
    stage = (stage.get("id") if isinstance(stage, dict) else stage) or args.get("step_id")
    block(label, "开始 · " + name, [("任务", "@" + task_id), ("页面", page), ("阶段", stage)])


async def progress_events(events, *, label, task_id, quiet=False):
    import asyncio
    from harness._vendor.tau_agent.events import ToolExecutionStartEvent, ToolExecutionEndEvent
    active = {}
    async def heartbeat():
        while True:
            await asyncio.sleep(60)
            for name, started in list(active.values()):
                if name in {"request_skill_input", "request_builder_input"}:
                    continue  # The terminal broker redisplays the exact question.
                block(label, "仍在等待工具返回", [("任务", "@" + task_id),
                    ("工具", name), ("已等待", f"{asyncio.get_running_loop().time() - started:.0f}s")])
    pulse = asyncio.create_task(heartbeat()) if not quiet else None
    try:
        async for event in events:
            if isinstance(event, ToolExecutionStartEvent):
                active[event.tool_call_id] = (event.tool_name, asyncio.get_running_loop().time())
                if not quiet:
                    tool_start(label, event.tool_name, task_id=task_id, args=event.args)
            elif isinstance(event, ToolExecutionEndEvent):
                active.pop(event.tool_call_id, None)
            yield event
    finally:
        if pulse:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
