"""
execution_loop.py — 核心驱动大循环（纯函数异步生成器）+ 执行结果语义解读

V4 核心设计：执行引擎是无状态的纯函数生成器。
所有 Agent 共享同一个 execute_turn()，行为差异完全由 AgentDefinition 驱动。

执行流程（每个 Turn）：
1. 压缩检查 → 是否触发第二级上下文压缩
2. Prompt 组装 → build_system_prompt + 历史消息
3. LLM 调用 → 获取文本回复和工具调用列表
4. Hook: pre_tool_execute → 安全校验
5. 工具执行 → ToolRegistry.dispatch()
6. Hook: post_tool_execute → 日志记录
7. 结果写回 → Context.append_message()
8. 判断结束 → stop_reason != "tool_use" 则退出

使用 AsyncGenerator 的 yield 机制向调用方实时报告事件。

本模块还提供一组纯函数辅助器（属于"执行层语义解读"，不依赖 Spawner 状态）：
- evaluate_completion(): 根据 progress_board 判定 complete / partial / failed
- extract_pending_goals(): 提取未完成的子目标列表
这些函数由 AgentSpawner._drive_execution 在 execute_turn 结束后调用，用于组装 summary。
"""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional

from core.agent_definition import AgentDefinition
from core.teammate_context import TeammateContext
from core.hook_registry import HookRegistry, HookEvent, HookAction
from core.context_compactor import ContextCompactor
from core.prompt_builder import build_system_prompt
from toolkits.tool_registry import ToolRegistry
from core.llm_provider import BaseLLMProvider


# ══════════════════════════════════════════════════════════════════════
#  完成度判定（execution 层语义；AgentSpawner 在 execute_turn 结束后调用）
# ══════════════════════════════════════════════════════════════════════

def evaluate_completion(
    agent_type: str,
    has_fatal_error: bool,
    progress_board: Dict[str, Any],
) -> str:
    """
    判定一次 execute_turn 结束后的完成度。

    返回值：
    - "complete" — 全部子目标完成（或非 lead agent 且无致命错误）
    - "partial"  — 部分子目标完成 / 仍有未完成项；session 应保留以便用户继续
    - "failed"   — 完全失败（致命错误且无任何完成进度，或非 lead agent + 致命错误）

    判定规则：
    - 非 lead agent：没有进度板，沿用二元判定（has_fatal_error → failed，否则 complete）
    - lead agent：
        * progress_board 未初始化 → 二元判定
        * 全部 goals 是 completed → "complete"
        * 没有任何 goal completed 且 has_fatal_error → "failed"
        * 其他情况（部分完成 / 无致命错误但仍有 pending）→ "partial"
    """
    if agent_type != "lead":
        return "failed" if has_fatal_error else "complete"

    goals = (progress_board or {}).get("goals", {}) or {}
    if not goals:
        return "failed" if has_fatal_error else "complete"

    statuses = [g.get("status", "pending") for g in goals.values()]
    all_done = all(s == "completed" for s in statuses)
    any_done = any(s == "completed" for s in statuses)

    if all_done:
        return "complete"
    if has_fatal_error and not any_done:
        return "failed"
    return "partial"


def extract_pending_goals(progress_board: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    提取未完成的子目标列表，给 UI 层 / chat 续作时提示用户后续应该补做什么。
    """
    goals = (progress_board or {}).get("goals", {}) or {}
    return [
        {
            "id": gid,
            "description": g.get("description", gid),
            "status": g.get("status", "pending"),
            "current": g.get("current", 0),
            "target": g.get("target"),
        }
        for gid, g in goals.items()
        if g.get("status") != "completed"
    ]


async def execute_turn(
    context: TeammateContext,
    tool_registry: ToolRegistry,
    hook_registry: HookRegistry,
    llm_provider: BaseLLMProvider,
    agent_def: AgentDefinition,
    compactor: ContextCompactor,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    核心驱动大循环 — 纯函数异步生成器。

    这是 V4 架构的心脏。它不持有任何状态，所有输入通过参数传入，
    所有输出通过 yield 事件流式传递给调用方。

    Browser Agent 的技能注入由 AgentSpawner.spawn() 在创建第一条 user 消息前完成，
    本函数不再负责该逻辑。

    :param context: 会话状态容器（纯数据）
    :param tool_registry: 全局工具注册表
    :param hook_registry: 生命周期 Hook 注册表
    :param llm_provider: LLM 提供方（BaseLLMProvider 实例）
    :param agent_def: Agent 人格定义（决定行为差异）
    :param compactor: 上下文压缩器

    :yields: 事件字典，格式为 {"event": str, ...附加数据}
    """
    # ── 0. 触发 SESSION_START Hook ──
    session_payload = {
        "agent_type": agent_def.agent_type,
        "session_id": context.session_id,
        "task": context.task,
        "env_vars": context.env_vars,
        "context": context,
    }
    session_result = await hook_registry.emit(HookEvent.SESSION_START, session_payload)
    if session_result.action == HookAction.BLOCK:
        yield {"event": "session_blocked", "reason": session_result.reason}
        return

    yield {"event": "turn_started", "agent": agent_def.agent_type, "task": context.task}

    # ── 1. 组装 System Prompt ──
    system_prompt = build_system_prompt(agent_def)

    # ── 1.5 注入进度板状态 ──
    progress_summary = context.progress_summary()
    if progress_summary:
        system_prompt += f"\n\n{progress_summary}"

    # ── 2. 获取可用工具 Schema ──
    available_tools = tool_registry.get_schemas(agent_def)

    turns = 0
    consecutive_errors = 0      # 连续 LLM 错误计数
    MAX_CONSECUTIVE_ERRORS = 3  # 连续错误阈值
    idle_turns = 0              # 连续无有效产出的轮次
    MAX_IDLE_TURNS = 8          # 早停阈值：连续N轮无产出则自动终止（从5提高到8，给浏览器操作更多容错）
    # 有产出的工具（本轮只要调用了其中任意一个，就不算 idle）：
    # - 所有浏览器操作都是有效产出（navigate/click/screenshot/extract_text/scroll/fill_form/run_js）
    # - 文件操作中的写入是产出（read_file/list_files 仅是读取，不算产出）
    # - 代码执行是产出
    # - Agent 编排操作是产出（spawn_agent/submit_plan/init_progress/update_progress）
    # 不算产出的：wait_user（纯等待）、read_file（纯读取）、list_files（纯列举）
    NON_PRODUCTIVE_TOOLS = {"wait_user", "read_file", "list_files"}
    while turns < agent_def.max_turns:
        turns += 1

        yield {"event": "turn_loop", "turn": turns, "max_turns": agent_def.max_turns}

        # ── 3.1 压缩检查 ──
        if compactor.should_compact(context):
            # 触发 PRE_COMPACT Hook
            compact_payload = {"context": context, "turn": turns}
            compact_result = await hook_registry.emit(HookEvent.PRE_COMPACT, compact_payload)
            
            if compact_result.action != HookAction.BLOCK:
                async def _summarize_fn(msgs):
                    summary_prompt = "请作为一位严谨的助手，提炼提供的对话摘要，保留关键事实、路径、API响应、已达成结论并保留重要选择器或错误代码。必须非常精简。"
                    msg_text = ""
                    for m in msgs:
                        role = m.get("role", "unknown")
                        # 仅保留截断版本以防止摘要本身Token过长
                        content_str = str(m.get("content", ""))[:2000]
                        msg_text += f"[{role}]: {content_str}...\n"
                    
                    try:
                        res_text, _, _ = await llm_provider.generate_response(
                            system_prompt=summary_prompt,
                            messages=[{"role": "user", "content": "需要摘要的历史对话记录:\n" + msg_text}],
                            tools=[]
                        )
                        return res_text
                    except Exception as e:
                        print(f"[ContextCompactor] ⚠️ LLM 摘要调用失败: {e}")
                        return None  # 返回 None 让 compactor 降级为规则压缩
                    
                compressed = await compactor.compact_if_needed(context, llm_summarize_fn=_summarize_fn)
                if compressed:
                    yield {"event": "context_compacted", "turn": turns}
                    # 触发 POST_COMPACT Hook（压缩完成通知）
                    await hook_registry.emit(HookEvent.POST_COMPACT, {
                        "context": context,
                        "turn": turns,
                    })

        # ── 3.2 LLM 调用 ──
        try:
            response_text, tool_calls, stop_reason = await llm_provider.generate_response(
                system_prompt=system_prompt,
                messages=context.get_messages(),
                tools=available_tools if available_tools else [],
            )
            consecutive_errors = 0  # LLM 调用成功，重置连续错误计数
        except Exception as e:
            # 超时类异常 str(e) 为空字符串，单独识别以保证错误信息和 backoff 都能正常工作
            is_timeout = isinstance(e, (asyncio.TimeoutError, TimeoutError))
            if is_timeout:
                error_msg = "LLM API 调用超时（无响应）"
            else:
                error_msg = str(e) or type(e).__name__
            error_str = error_msg.lower()

            # ── 不可恢复错误识别 ──
            # 优先匹配 Anthropic/OpenAI SDK 抛出的结构化错误类型（高置信度，不易和正常文本歧义）
            # 注意："tool call result does not follow" 不属于不可恢复 —— 由消息修复器处理
            unrecoverable_error_types = (
                "authentication_error",
                "permission_error",
                "not_found_error",
                "invalid_request_error",
                "billing_hard_limit_reached",
                "insufficient_quota",
                "model_not_found",
                "account_disabled",
                "account_deactivated",
                "subscription_expired",
                "rate_limit_error",
            )
            # 兜底的具体短语（直接由网关/上游返回的原始错误文本）
            unrecoverable_phrases = (
                "invalid api key",
                "incorrect api key",
                "invalid x-api-key",
                "x-api-key header",
                "api key not valid",
                "token plan not support",
            )
            is_unrecoverable = (
                any(t in error_str for t in unrecoverable_error_types)
                or any(p in error_str for p in unrecoverable_phrases)
            )
            yield {
                "event": "llm_error",
                "error": error_msg,
                "turn": turns,
                "fatal": is_unrecoverable,
            }
            if is_unrecoverable:
                break

            # ── 连续错误熔断：防止非致命但持续的错误无限循环 ──
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                yield {
                    "event": "llm_error",
                    "error": f"连续 {consecutive_errors} 次 LLM 错误，自动终止",
                    "turn": turns,
                    "fatal": True,
                }
                break

            # ── 安全注入错误提示：确保不破坏 user/assistant 交替结构 ──
            error_hint = f"[系统提示] LLM 调用出错: {error_msg}。请调整策略重试。"
            if context.session_messages and context.session_messages[-1]["role"] == "user":
                # 最后一条已是 user 消息 → 合并到已有消息中，避免连续 user
                last = context.session_messages[-1]
                if isinstance(last["content"], str):
                    last["content"] += f"\n{error_hint}"
                elif isinstance(last["content"], list):
                    last["content"].append({"type": "text", "text": error_hint})
            else:
                context.append_message("user", error_hint)

            # ── 对于 529 (overloaded)、500 错误和超时使用更长的等待时间 ──
            if is_timeout or "529" in error_str or "overloaded" in error_str or "500" in error_str:
                backoff_time = min(2 ** consecutive_errors, 30)  # 最多等待 30 秒
                yield {"event": "llm_backoff", "wait_seconds": backoff_time, "consecutive_errors": consecutive_errors}
                await asyncio.sleep(backoff_time)
            else:
                await asyncio.sleep(1)
            continue

        # ── 3.3 组装 assistant 消息写回上下文 ──
        assistant_blocks = []
        if response_text:
            assistant_blocks.append({"type": "text", "text": response_text})
        for tc in tool_calls:
            assistant_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })

        if assistant_blocks:
            context.append_message("assistant", assistant_blocks)

        # ── 3.4 如果没有工具调用，Agent 认为任务完成 ──
        if stop_reason != "tool_use" or not tool_calls:
            yield {
                "event": "agent_response",
                "text": response_text,
                "turn": turns,
            }
            break

        # ── 3.5 处理工具调用 ──
        turn_has_productive_tool = False  # 追踪本轮是否有产出工具调用
        for tc in tool_calls:
            tool_name = tc["name"]
            tool_input = tc["input"]
            tool_id = tc["id"]

            yield {
                "event": "tool_call",
                "tool": tool_name,
                "input": tool_input,
                "turn": turns,
            }

            # ── Hook: PRE_TOOL_EXECUTE ──
            pre_payload = {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "agent_type": agent_def.agent_type,
                "session_id": context.session_id,
                "worktree_path": context.worktree_path,
            }
            pre_result = await hook_registry.emit(HookEvent.PRE_TOOL_EXECUTE, pre_payload)

            if pre_result.action == HookAction.BLOCK:
                # 安全拦截，将拒绝原因作为工具结果返回
                tool_result_str = f"[安全拦截] {pre_result.reason}"
                yield {"event": "tool_blocked", "tool": tool_name, "reason": pre_result.reason}
            else:
                # 如果 Hook 修改了参数，使用修改后的
                if pre_result.action == HookAction.MODIFY and pre_result.updated_payload:
                    tool_input = pre_result.updated_payload.get("tool_input", tool_input)

                # ── 处理 WorkTree 边界 ──
                from pathlib import Path
                effective_worktree = str(Path(context.worktree_path).parent) if agent_def.agent_type == "lead" else context.worktree_path

                # ── 执行工具 ──
                tool_result_str = await tool_registry.dispatch(
                    tool_name=tool_name,
                    params=tool_input,
                    agent_def=agent_def,
                    worktree_path=effective_worktree,
                    session_id=context.session_id,
                    context=context,
                )

            yield {
                "event": "tool_result",
                "tool": tool_name,
                "result_preview": tool_result_str[:200],
                "turn": turns,
            }

            # ── Hook: POST_TOOL_EXECUTE ──
            post_payload = {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_result": tool_result_str,
                "agent_type": agent_def.agent_type,
                "session_id": context.session_id,
                "turn": turns,
            }
            post_result = await hook_registry.emit(HookEvent.POST_TOOL_EXECUTE, post_payload)

            # 立即将工具结果写回上下文（每个工具结果单独一条 user 消息）
            context.append_message("user", [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": tool_result_str,
            }])

            # ── 延迟注入：Hook 产生的额外消息在 tool_result 之后注入 ──
            # 这确保 tool_result 紧跟 assistant 的 tool_use，不破坏 Anthropic 消息交替约束
            inject_content = (post_result.updated_payload or {}).get("inject_after_tool_result")
            if inject_content:
                context.append_message("user", inject_content)

            # ── 早停检测：标记本轮是否有产出工具 ──
            # 使用黑名单逻辑：不在 NON_PRODUCTIVE_TOOLS 中的工具都算有产出
            if tool_name not in NON_PRODUCTIVE_TOOLS:
                turn_has_productive_tool = True

            # AUTO_COMPACT：RepetitionCompactor 已将压缩摘要放入 updated_payload，
            # 由上方延迟注入逻辑在 tool_result 之后写入为独立 user 消息。
            # 新策略：
            #   1) pop 掉独立 append 的摘要 user 消息（避免摘要重复出现两次）
            #   2) 把摘要内容就地回填到刚 append 的 tool_result.content 中
            #      —— 既保留 tool_use ↔ tool_result 配对，又起到压缩失败信息的效果
            #   3) 不再 break：继续处理本轮剩余 tool_call，避免它们的 tool_use 也变成孤儿
            if post_result.action == HookAction.AUTO_COMPACT:
                yield {"event": "auto_compact", "tool": tool_name, "reason": post_result.reason, "turn": turns}
                if inject_content:
                    # (1) 回收独立 append 的摘要消息
                    if context.session_messages and context.session_messages[-1]["role"] == "user":
                        last = context.session_messages[-1]
                        last_content = last.get("content", "")
                        if isinstance(last_content, str) and last_content.startswith("[系统自动压缩]"):
                            context.session_messages.pop()
                    # (2) 回填到 tool_result.content
                    if context.session_messages and context.session_messages[-1]["role"] == "user":
                        last = context.session_messages[-1]
                        last_content = last.get("content")
                        if isinstance(last_content, list):
                            for block in last_content:
                                if isinstance(block, dict) and block.get("type") == "tool_result":
                                    block["content"] = inject_content
                                    break

        # ── 早停检测：连续无产出则自动终止 ──
        if turn_has_productive_tool:
            idle_turns = 0
        else:
            idle_turns += 1
            if idle_turns >= MAX_IDLE_TURNS:
                yield {
                    "event": "early_stop",
                    "reason": f"连续 {idle_turns} 轮仅调用了非产出工具({NON_PRODUCTIVE_TOOLS})，自动终止",
                    "turns_used": turns,
                }
                break

    # ── 4. Turn 结束，触发 PRE_TURN_COMPLETE Hook ──
    # 提取最终回复文本
    final_text = ""
    if context.session_messages:
        last_msg = context.session_messages[-1]
        if last_msg.get("role") == "assistant":
            content = last_msg.get("content", "")
            if isinstance(content, str):
                final_text = content
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                final_text = "\n".join(texts)

    complete_payload = {
        "agent_type": agent_def.agent_type,
        "session_id": context.session_id,
        "final_text": final_text,
        "turns_used": turns,
        "context": context,
    }
    complete_result = await hook_registry.emit(HookEvent.PRE_TURN_COMPLETE, complete_payload)

    if complete_result.action == HookAction.BLOCK:
        # Verification 拒绝，注入错误信息继续循环（但此处循环已结束）
        yield {
            "event": "verification_rejected",
            "reason": complete_result.reason,
            "turns_used": turns,
        }
    else:
        yield {
            "event": "turn_completed",
            "result": final_text,
            "turns_used": turns,
            "agent": agent_def.agent_type,
        }
