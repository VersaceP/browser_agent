"""
execution_loop.py — 核心驱动大循环（纯函数异步生成器）

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
"""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional

from core.agent_definition import AgentDefinition
from core.teammate_context import TeammateContext
from core.hook_registry import HookRegistry, HookEvent, HookAction
from core.context_compactor import ContextCompactor
from core.prompt_builder import build_system_prompt, build_dynamic_context, format_skills
from toolkits.tool_registry import ToolRegistry
from core.llm_provider import BaseLLMProvider

# 延迟导入以避免循环依赖
from typing import TYPE_CHECKING
from core.skill_registry import SkillRegistry


async def execute_turn(
    context: TeammateContext,
    tool_registry: ToolRegistry,
    hook_registry: HookRegistry,
    llm_provider: BaseLLMProvider,
    agent_def: AgentDefinition,
    compactor: ContextCompactor,
    skill_registry: Optional["SkillRegistry"] = None,  # 新增参数
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    核心驱动大循环 — 纯函数异步生成器。
    
    这是 V4 架构的心脏。它不持有任何状态，所有输入通过参数传入，
    所有输出通过 yield 事件流式传递给调用方。
    
    :param context: 会话状态容器（纯数据）
    :param tool_registry: 全局工具注册表
    :param hook_registry: 生命周期 Hook 注册表  
    :param llm_provider: LLM 提供方（BaseLLMProvider 实例）
    :param agent_def: Agent 人格定义（决定行为差异）
    :param compactor: 上下文压缩器
    :param skill_registry: 技能注册表（可选，用于 Browser Agent）
    
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

    # ── 0.5 技能注入（仅 Browser Agent 的第一个 Turn）──
    injected_skills_content = ""
    if skill_registry and agent_def.agent_type == "browser" and not context.session_messages:
        # 选择相关技能
        selected_skills = skill_registry.select_skills(context.task)
        
        if selected_skills:
            # 触发 PRE_SKILL_INJECT Hook（安全验证）
            skill_payload = {
                "agent_type": agent_def.agent_type,
                "session_id": context.session_id,
                "task": context.task,
                "selected_skills": selected_skills,
                "skill_names": [s.name for s in selected_skills],
            }
            skill_result = await hook_registry.emit(HookEvent.PRE_SKILL_INJECT, skill_payload)
            
            if skill_result.action == HookAction.BLOCK:
                yield {"event": "skill_inject_blocked", "reason": skill_result.reason}
                # 技能注入被阻止，但继续执行（不注入技能）
                print(f"[ExecutionLoop] ⚠️ 技能注入被阻止: {skill_result.reason}")
            else:
                # 格式化技能内容
                injected_skills_content = format_skills(selected_skills)
                
                # 估算 token 数量
                estimated_tokens = sum(len(s.content) / 4 for s in selected_skills)
                
                # 触发 POST_SKILL_INJECT Hook（审计日志）
                await hook_registry.emit(HookEvent.POST_SKILL_INJECT, {
                    "agent_type": agent_def.agent_type,
                    "session_id": context.session_id,
                    "injected_skills": [s.name for s in selected_skills],
                    "total_tokens": int(estimated_tokens),
                })
                
                yield {
                    "event": "skills_injected",
                    "skills": [s.name for s in selected_skills],
                    "estimated_tokens": int(estimated_tokens),
                }
        
        # 创建第一条 user message（包含任务和技能）
        first_message_content = build_dynamic_context(
            task=context.task,
            worktree_path=context.worktree_path,
            env_vars=context.env_vars,
            skills=injected_skills_content
        )

        if injected_skills_content:
            context.append_message("user", [
                {
                    "type": "text",
                    "text": first_message_content,
                    "cache_control": {"type": "ephemeral"}
                }
            ])
        else:
            context.append_message("user", first_message_content)

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
                        
                    res_text, _, _ = await llm_provider.generate_response(
                        system_prompt=summary_prompt,
                        messages=[{"role": "user", "content": "需要摘要的历史对话记录:\n" + msg_text}],
                        tools=[]
                    )
                    return res_text
                    
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
            error_str = str(e).lower()
            yield {"event": "llm_error", "error": str(e), "turn": turns}

            # ── 不可恢复错误：直接退出避免无限死循环 ──
            unrecoverable_keywords = [
                "api key", "unauthorized", "auth", "not found",
                "401", "404", "model not found",
                "invalid_request_error",
                # 注意："tool call result does not follow" 已从不可恢复列表移除
                # 该错误由消息格式问题导致，配合消息修复器可自动恢复
            ]
            if any(k in error_str for k in unrecoverable_keywords):
                break

            # ── 连续错误熔断：防止非致命但持续的错误无限循环 ──
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                yield {"event": "llm_error", "error": f"连续 {consecutive_errors} 次 LLM 错误，自动终止", "turn": turns}
                break

            # ── 安全注入错误提示：确保不破坏 user/assistant 交替结构 ──
            error_hint = f"[系统提示] LLM 调用出错: {e}。请调整策略重试。"
            if context.session_messages and context.session_messages[-1]["role"] == "user":
                # 最后一条已是 user 消息 → 合并到已有消息中，避免连续 user
                last = context.session_messages[-1]
                if isinstance(last["content"], str):
                    last["content"] += f"\n{error_hint}"
                elif isinstance(last["content"], list):
                    last["content"].append({"type": "text", "text": error_hint})
            else:
                context.append_message("user", error_hint)

            # ── 对于 529 (overloaded) 和 500 错误使用更长的等待时间 ──
            if "529" in error_str or "overloaded" in error_str or "500" in error_str:
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
            # 由上方延迟注入逻辑在 tool_result 之后写入。
            # 这里删掉刚才追加的重复工具结果和紧随的摘要消息，让 LLM 重新开始
            if post_result.action == HookAction.AUTO_COMPACT:
                yield {"event": "auto_compact", "tool": tool_name, "reason": post_result.reason, "turn": turns}
                # 删除紧随 tool_result 注入的摘要消息（如果有的话）
                if inject_content and context.session_messages and context.session_messages[-1]["role"] == "user":
                    last = context.session_messages[-1]
                    last_content = last.get("content", "")
                    # 摘要消息的特征：content 为字符串且以 "[系统自动压缩]" 开头
                    if isinstance(last_content, str) and last_content.startswith("[系统自动压缩]"):
                        context.session_messages.pop()
                # 删掉刚才 append 的那条重复工具结果
                if context.session_messages and context.session_messages[-1]["role"] == "user":
                    last_content = context.session_messages[-1]["content"]
                    if isinstance(last_content, list) and last_content[0].get("type") == "tool_result":
                        context.session_messages.pop()
                # 跳过本轮剩余工具，退出 for 循环 → 进入下一轮（重新调用 LLM）
                break

        # for 正常结束后进入下一轮 while（重新调用 LLM）

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
