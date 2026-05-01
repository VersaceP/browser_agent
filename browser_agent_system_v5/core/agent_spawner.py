"""
agent_spawner.py — Agent 注册表与孵化器

V4 核心设计：统一管理 Agent 的注册与孵化。
- register_builtin_agents(): 注册 4 个内建 Agent
- spawn(): 创建 Context → 过滤工具 → 执行 execution_loop → 收集结果

Lead Agent 通过 spawn_agent 工具调用此模块来派生子 Agent。
递归防护：Verification Agent 禁止再 spawn。
"""

import asyncio
import pathlib
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from permissions.denial_tracker import DenialTracker
from core.resource_manager import ResourceManager
from core.skill_registry import SkillRegistry
from core.agent_definition import AgentDefinition, build_builtin_agents
from core.teammate_context import TeammateContext
from core.execution_loop import execute_turn, evaluate_completion, extract_pending_goals
from core.hook_registry import HookRegistry
from core.context_compactor import ContextCompactor
from core.worktree import WorkTreeManager
from core.llm_provider import BaseLLMProvider
from toolkits.tool_registry import ToolRegistry


class AgentSpawner:
    """
    Agent 注册表与孵化器。
    
    职责：
    1. 维护所有 AgentDefinition 的注册表
    2. 根据 agent_type 创建 TeammateContext
    3. 调用 execution_loop.execute_turn() 驱动 Agent 执行
    4. 收集并返回执行结果
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        hook_registry: HookRegistry,
        llm_provider: BaseLLMProvider,
        worktree_manager: WorkTreeManager,
        compactor: ContextCompactor,
        denial_tracker: "DenialTracker",
        resource_manager: "ResourceManager",
        PAYLOAD_THRESHOLD :int = 10_000,
        repetition_compactor = None,  # RepetitionCompactor 实例，用于 AUTO_COMPACT
        skill_registry: Optional["SkillRegistry"] = None  # 技能注册表，用于 Browser Agent
    ):
        self.tool_registry = tool_registry
        self.hook_registry = hook_registry
        self.llm_provider = llm_provider
        self.worktree_manager = worktree_manager
        self.compactor = compactor
        self.denial_tracker = denial_tracker
        self.resource_manager = resource_manager
        self._agent_defs: Dict[str, AgentDefinition] = {}
        self.PAYLOAD_THRESHOLD = PAYLOAD_THRESHOLD
        self._repetition_compactor = repetition_compactor
        self.skill_registry = skill_registry
        self._browser_lock = asyncio.Lock()

    def register_builtin_agents(self) -> None:
        """注册 4 个内建 Agent 定义"""
        self._agent_defs = build_builtin_agents()
        print(f"[AgentSpawner] ✅ 已注册 {len(self._agent_defs)} 个内建 Agent: "
              f"{', '.join(self._agent_defs.keys())}")

    def register_agent(self, agent_def: AgentDefinition) -> None:
        """注册自定义 Agent 定义"""
        self._agent_defs[agent_def.agent_type] = agent_def

    def get_agent_def(self, agent_type: str) -> Optional[AgentDefinition]:
        """获取 Agent 定义"""
        return self._agent_defs.get(agent_type)

    async def spawn(
        self,
        agent_type: str,
        task: str,
        env_vars: Optional[Dict[str, str]] = None,
        parent_agent_type: str = "",
        session_id: str = "",
        max_turns: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        孵化并执行一个 Agent。
        
        流程：
        1. 查找 AgentDefinition
        2. 递归防护检查
        3. 创建 TeammateContext + WorkTree
        4. 调用 execute_turn() 驱动执行
        5. 收集所有事件，提取最终结果
        
        :param agent_type: Agent 类型（lead/browser/coding/verification）
        :param task: 任务描述
        :param env_vars: 环境变量
        :param parent_agent_type: 父 Agent 类型（用于递归防护）
        :return: 执行结果摘要字典
        """
        # 1. 查找 Agent 定义
        agent_def = self._agent_defs.get(agent_type)
        if not agent_def:
            return {
                "success": False,
                "error": f"未知的 Agent 类型: {agent_type}",
                "available_types": list(self._agent_defs.keys()),
            }

        # 2. 递归防护（防御性检查：阻止无权 Agent 非法调用孵化器）
        if parent_agent_type:
            parent_def = self._agent_defs.get(parent_agent_type)
            if parent_def and not parent_def.can_spawn:
                return {
                    "success": False,
                    "error": f"【系统拦截】{parent_agent_type.upper()} Agent 禁止派生子 Agent！",
                }

        # 3. 创建上下文和存储分区
        if not session_id:
            session_id = f"task_{int(time.time())}"

        # 子 Agent 复用父 Agent 的 WorkTree（通过 env_vars "_parent_worktree" 传递）
        # 这样 verification/coding 等子 Agent 可以直接读写同一 worktree 下的文件
        env_vars = env_vars or {}
        parent_wt = env_vars.pop("_parent_worktree", None)
        if parent_wt:
            worktree_path = parent_wt
        else:
            worktree_path = self.worktree_manager.get_or_create_worktree(session_id, agent_type)
        original_task = task
        if len(task) > self.PAYLOAD_THRESHOLD:
            payload_file = pathlib.Path(worktree_path) / "input_payload.md"
            try:
                payload_file.write_text(task, encoding="utf-8")
                # 重写任务，引导 Agent 去读文件
                task = (
                    f"⚠️ [NOTICE] Your task description and data are too large for the direct prompt.\n"
                    f"They have been successfully saved to '{payload_file.name}' in your worktree.\n"
                    f"PLEASE READ THAT FILE FIRST to get the complete task instructions and data.\n"
                    f"\nTask Snippet (first 200 chars):\n{task[:200]}..."
                )
                print(f"  [{agent_type}] 📦 任务 Payload 过大 ({len(original_task)} 字符)，已溢出到: {payload_file.name}")
            except Exception as e:
                print(f"  [{agent_type}] ⚠️ 溢出文件保存失败: {e}")

        context = TeammateContext(
            agent_type=agent_type,
            session_id=session_id,
            task=task,
            worktree_path=str(worktree_path),
            env_vars=env_vars or {},
        )

        # ── Browser Agent 技能注入 ──
        # 在 append 第一条 user 消息之前完成技能选择和注入。
        injected_skills_content = ""
        if agent_type == "browser" and self.skill_registry:
            from core.hook_registry import HookEvent, HookAction
            from core.prompt_builder import format_skills

            selected_skills = self.skill_registry.select_skills(context.task)
            if selected_skills:
                skill_payload = {
                    "agent_type": agent_type,
                    "session_id": session_id,
                    "task": context.task,
                    "selected_skills": selected_skills,
                    "skill_names": [s.name for s in selected_skills],
                }
                skill_result = await self.hook_registry.emit(HookEvent.PRE_SKILL_INJECT, skill_payload)
                if skill_result.action == HookAction.BLOCK:
                    print(f"  [{agent_type}] ⚠️ 技能注入被阻止: {skill_result.reason}")
                else:
                    injected_skills_content = format_skills(selected_skills)
                    estimated_tokens = sum(len(s.content) / 4 for s in selected_skills)
                    await self.hook_registry.emit(HookEvent.POST_SKILL_INJECT, {
                        "agent_type": agent_type,
                        "session_id": session_id,
                        "injected_skills": [s.name for s in selected_skills],
                        "total_tokens": int(estimated_tokens),
                    })
                    print(
                        f"  [{agent_type}] 🧠 注入 {len(selected_skills)} 个技能: "
                        f"{[s.name for s in selected_skills]}（约 {int(estimated_tokens)} tokens）"
                    )

        from core.prompt_builder import build_dynamic_context
        task_msg = build_dynamic_context(
            task=context.task,
            worktree_path=context.worktree_path,
            env_vars=context.env_vars,
            skills=injected_skills_content,
        )
        if injected_skills_content:
            # 技能内容较大，加上 cache_control 让 Anthropic 缓存这块前缀
            context.append_message("user", [
                {
                    "type": "text",
                    "text": task_msg,
                    "cache_control": {"type": "ephemeral"},
                }
            ])
        else:
            context.append_message("user", task_msg)

        print(
            f"\n[AgentSpawner] 🚀 孵化 {agent_type.upper()} Agent "
            f"(任务摘要: {task[:50]}...)"
        )

        agent_id = f"{session_id}:{agent_type}"
        self.denial_tracker.reset(agent_id)

        # 如果指定了 max_turns，创建覆盖后的 agent_def 副本
        effective_agent_def = agent_def
        if max_turns is not None and max_turns > 0:
            from dataclasses import replace as dc_replace
            effective_agent_def = dc_replace(agent_def, max_turns=max_turns)
            print(f"  [{agent_type}] 🔄 max_turns 覆盖: {agent_def.max_turns} → {max_turns}")

        # 浏览器互斥：使用浏览器工具的 Agent 同一时间只允许一个运行
        # browser: navigate/click/extract/screenshot 等完整浏览器操作
        # verification: navigate/screenshot/extract_text 只读浏览器验证
        # 两者共享同一个浏览器实例，必须互斥以避免页面状态冲突
        _BROWSER_USING_TYPES = {"browser", "verification"}
        if agent_type in _BROWSER_USING_TYPES:
            async with self._browser_lock:
                return await self._drive_execution(context, effective_agent_def)
        else:
            return await self._drive_execution(context, effective_agent_def)

    async def chat(
        self,
        context: TeammateContext,
        message: str,
    ) -> Dict[str, Any]:
        """
        在已有会话上下文的基础上继续对话。
        
        :param context: 已有的 TeammateContext 实例
        :param message: 用户的新指令/增量任务
        :return: 执行结果摘要字典
        """
        agent_def = self._agent_defs.get(context.agent_type)
        if not agent_def:
            return {"success": False, "error": f"未找到 Agent 定义: {context.agent_type}"}

        print(f"\n[AgentSpawner] 💬 继续 {context.agent_type.upper()} 会话 (新指令: {message[:50]}...)")
        
        # 将新指令作为 user 消息追加到上下文
        context.append_message("user", message)
        
        return await self._drive_execution(context, agent_def)

    async def spawn_batch(
        self,
        tasks: List[Dict[str, Any]],
        parent_agent_type: str = "lead",
        session_id: str = "",
        parent_worktree_path: str = "",
    ) -> Dict[str, Any]:
        """
        并行孵化并执行多个 Agent。

        使用 asyncio.gather() 让多个独立任务同时运行。
        Browser Agent 受互斥锁保护，同一时间只运行一个；
        其他类型的 Agent 可以真正并行执行。

        :param tasks: 任务列表，每项为 {"agent_type": str, "task": str}
        :param parent_agent_type: 父 Agent 类型（用于递归防护）
        :param session_id: 会话 ID
        :param parent_worktree_path: 父 WorkTree 路径
        :return: 汇总结果字典，包含每个子任务的结果和整体统计
        """
        if not tasks:
            return {"success": False, "error": "任务列表不能为空"}

        if not session_id:
            session_id = f"task_{int(time.time())}"

        # 校验：每个 task 必须包含 agent_type 和 task
        for i, t in enumerate(tasks):
            if not t.get("agent_type") or not t.get("task"):
                return {
                    "success": False,
                    "error": f"第 {i + 1} 个任务缺少 agent_type 或 task 字段",
                }

        # 校验：父 Agent 是否有 spawn 权限
        if parent_agent_type:
            parent_def = self._agent_defs.get(parent_agent_type)
            if parent_def and not parent_def.can_spawn:
                return {
                    "success": False,
                    "error": f"【系统拦截】{parent_agent_type.upper()} Agent 禁止派生子 Agent！",
                }

        # 校验：agent_type 是否存在
        for t in tasks:
            if t["agent_type"] not in self._agent_defs:
                return {
                    "success": False,
                    "error": f"未知的 Agent 类型: {t['agent_type']}",
                    "available_types": list(self._agent_defs.keys()),
                }

        print(
            f"\n[AgentSpawner] 🚀 并行孵化 {len(tasks)} 个子 Agent "
            f"(类型: {', '.join(t['agent_type'] for t in tasks)})"
        )

        # 构建协程列表
        coros = []
        for t in tasks:
            env_vars: Optional[Dict[str, str]] = {"_parent_worktree": parent_worktree_path} if parent_worktree_path else None
            mt = t.get("max_turns")
            spawn_kwargs = dict(agent_type=t["agent_type"], task=t["task"], env_vars=env_vars, parent_agent_type=parent_agent_type, session_id=session_id)
            if mt and mt > 0:
                spawn_kwargs["max_turns"] = mt
            coros.append(self.spawn(**spawn_kwargs))

        # 并行执行所有任务
        # asyncio.gather(return_exceptions=True) 确保单个任务异常不会影响其他任务
        results = await asyncio.gather(*coros, return_exceptions=True)

        # 汇总结果
        summary_results = []
        success_count = 0
        fail_count = 0

        for i, result in enumerate(results):
            task_info = tasks[i]
            if isinstance(result, Exception):
                summary_results.append({
                    "agent_type": task_info["agent_type"],
                    "task": task_info["task"][:100],
                    "success": False,
                    "error": str(result),
                })
                fail_count += 1
            else:
                result_dict: Dict[str, Any] = result  # type: ignore[assignment]
                summary_results.append({
                    "agent_type": task_info["agent_type"],
                    "task": task_info["task"][:100],
                    "success": result_dict.get("success", False),
                    "final_answer": result_dict.get("final_answer", "")[:500],
                    "message_count": result_dict.get("message_count", 0),
                    "worktree_path": result_dict.get("worktree_path", ""),
                })
                if result_dict.get("success"):
                    success_count += 1
                else:
                    fail_count += 1

        print(
            f"[AgentSpawner] 🏁 并行执行完成: {success_count} 成功, {fail_count} 失败"
        )

        return {
            "success": fail_count == 0,
            "total": len(tasks),
            "success_count": success_count,
            "fail_count": fail_count,
            "results": summary_results,
        }

    async def _drive_execution(
        self,
        context: TeammateContext,
        agent_def: AgentDefinition,
    ) -> Dict[str, Any]:
        """
        驱动执行引擎完成一轮或多轮 Tool-Use 循环。
        """
        agent_type = agent_def.agent_type
        worktree_path = context.worktree_path

        try:
            items = []
            wt_path = pathlib.Path(worktree_path)
            if wt_path.exists():
                for p in wt_path.glob("*"):
                    items.append(f"{'[DIR]' if p.is_dir() else '[FILE]'} {p.name}")
            
            if items:
                snapshot = "\n".join(items)
                env_hint = f"\n[System Hint] Current WorkTree File Snapshot:\n{snapshot}\n"
                
                # 追加到最新的 user 消息中，或者作为新消息
                if context.session_messages and context.session_messages[-1]["role"] == "user":
                    last_content = context.session_messages[-1]["content"]
                    if isinstance(last_content, str):
                        context.session_messages[-1]["content"] = last_content + env_hint
                    elif isinstance(last_content, list):
                        context.session_messages[-1]["content"].append({"type": "text", "text": env_hint})
                else:
                    context.append_message("user", env_hint)
        except Exception as e:
            print(f"  [{agent_type}] ⚠️ 环境快照注入失败: {e}")

        # 4. 执行引擎驱动
        events = []
        final_result = ""
        needs_verification = False
        has_fatal_error = False  # 追踪致命错误（LLM 熔断/不可恢复/早停）

        try:
            async for event in execute_turn(
                context=context,
                tool_registry=self.tool_registry,
                hook_registry=self.hook_registry,
                llm_provider=self.llm_provider,
                agent_def=agent_def,
                compactor=self.compactor,
            ):
                events.append(event)
                event_type = event.get("event", "")

                # 实时打印关键事件
                if event_type == "turn_loop":
                    print(f"  [{agent_type}] 🔄 Turn {event['turn']}/{event['max_turns']}")
                elif event_type == "tool_call":
                    print(f"  [{agent_type}] ⚡ 调用工具: {event['tool']}")
                elif event_type == "tool_blocked":
                    print(f"  [{agent_type}] 🛡️ 工具被拦截: {event['tool']} - {event['reason']}")
                elif event_type == "tool_result":
                    print(f"  [{agent_type}] 📋 工具结果: {event['result_preview'][:80]}...")
                elif event_type == "agent_response":
                    final_result = event.get("text", "")
                    print(f"  [{agent_type}] 💬 Agent 回复: {final_result[:100]}...")
                elif event_type == "turn_completed":
                    final_result = event.get("result", "")
                elif event_type == "verification_rejected":
                    needs_verification = True
                    print(f"  [{agent_type}] 🔍 PRE_TURN_COMPLETE 触发自动质检请求")
                elif event_type == "llm_error":
                    print(f"  [{agent_type}] ❌ LLM 错误: {event['error']}")
                    if event.get("fatal"):
                        has_fatal_error = True
                elif event_type == "early_stop":
                    print(f"  [{agent_type}] ⏹️ 早停: {event.get('reason', '')}")
                    has_fatal_error = True  # 连续 idle 说明 Agent 无法完成任务
                elif event_type == "context_compacted":
                    print(f"  [{agent_type}] 📦 上下文已压缩")

        except Exception as e:
            print(f"  [{agent_type}] 💥 执行异常: {e}")
            return {
                "success": False,
                "agent_type": agent_type,
                "task": context.task,
                "error": str(e),
                "events": events,
            }

        # 5. 自动质检（编排层负责实际 spawn）
        verification_report = ""
        if needs_verification and final_result.strip():
            print(f"\n[AgentSpawner] 🔍 自动拉起 Verification Agent 进行兜底质检...")
            # 提供给 Verification Agent 作为可访问文件的索引
            session_root = pathlib.Path(worktree_path).parent
            file_index_lines = []
            try:
                for agent_dir in sorted(session_root.iterdir()):
                    if agent_dir.is_dir():
                        for f in agent_dir.rglob("*"):
                            if f.is_file():
                                rel = f.relative_to(session_root)
                                file_index_lines.append(f"  {rel}")
            except Exception:
                pass
            file_index = "\n".join(file_index_lines) if file_index_lines else "  (无文件)"

            verify_task = (
                f"请对以下 Lead Agent 的最终执行结果进行对抗性审查：\n\n"
                f"--- Lead Agent 最终输出 ---\n{final_result}\n--- 输出结束 ---\n\n"
                f"审查要点：\n"
                f"1. 数据完整性：结果是否完整，有无缺失信息\n"
                f"2. 逻辑一致性：结论是否自洽，有无矛盾\n"
                f"3. 任务达成度：是否完整回答了用户的原始需求\n\n"
                f"[重要提示] 你的 WorkTree 根目录是 session 根目录。\n"
                f"以下是 session 内所有 agent 产出的文件索引（可直接用相对路径读取）：\n"
                f"{file_index}\n\n"
                f"请输出 JSON 格式的审查报告：\n"
                f'  {{"passed": true/false, "issues": [...], "summary": "..."}}'
            )

            try:
                # 传递 session 根目录给 Verification Agent，
                # 使其 worktree_path 覆盖整个 session，可合法读取所有子 agent 的文件
                verify_result = await self.spawn(
                    agent_type="verification",
                    task=verify_task,
                    parent_agent_type="lead",
                    session_id=context.session_id,
                    env_vars={"_parent_worktree": str(session_root)},
                )
                verification_report = verify_result.get("final_answer", "")
                print(f"[AgentSpawner] 📋 质检结果: {verification_report[:200]}")

                # 将质检报告注入 Lead 的上下文，供后续多轮对话参考
                if verification_report:
                    context.append_message("user", [
                        {"type": "text", "text": f"[系统自动质检报告]\n{verification_report}"}
                    ])

            except Exception as e:
                print(f"[AgentSpawner] ⚠️ 自动质检异常: {e}")

        # 6. 触发 POST_AGENT_COMPLETE 事件
        from core.hook_registry import HookEvent
        complete_payload = {
            "agent_type": agent_type,
            "session_id": context.session_id,
            "final_result": final_result,
            "verification_report": verification_report,
            "events_count": len(events),
        }
        await self.hook_registry.emit(HookEvent.POST_AGENT_COMPLETE, complete_payload)

        # 7. 收集结果
        summary = context.to_summary()
        summary["events_count"] = len(events)
        summary["worktree_path"] = str(worktree_path)
        summary["context"] = context
        if verification_report:
            summary["verification_report"] = verification_report

        # ── 完成度判定（语义在 execution_loop.evaluate_completion，此处只调用）──
        # Lead Agent 看 progress_board：
        #   complete → 全部子目标 completed
        #   partial  → 还有 pending/in_progress（即使早停或致命错误，只要部分已完成都算）
        #   failed   → 没有任何子目标 completed 且发生致命错误
        # 非 Lead Agent 沿用二元判定（fatal → failed，否则 complete）
        # 注：partial 也算 success=True，让 main.py 持久化 context，用户下一轮可继续未完成任务
        completion = evaluate_completion(agent_type, has_fatal_error, context.progress_board)
        summary["completion"] = completion
        summary["success"] = (completion != "failed")
        if completion == "partial":
            summary["pending_goals"] = extract_pending_goals(context.progress_board)

        print(
            f"[AgentSpawner] ✅ {agent_type.upper()} Agent 完成 "
            f"(共 {len(events)} 事件, completion={completion})"
        )

        return summary
