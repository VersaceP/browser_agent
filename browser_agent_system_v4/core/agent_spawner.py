"""
agent_spawner.py — Agent 注册表与孵化器

V4 核心设计：统一管理 Agent 的注册与孵化。
- register_builtin_agents(): 注册 4 个内建 Agent
- spawn(): 创建 Context → 过滤工具 → 执行 execution_loop → 收集结果

Lead Agent 通过 spawn_agent 工具调用此模块来派生子 Agent。
递归防护：Verification Agent 禁止再 spawn。
"""

import os
import pathlib
import time
import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional

from core.agent_definition import AgentDefinition, build_builtin_agents
from core.teammate_context import TeammateContext
from core.execution_loop import execute_turn
from core.hook_registry import HookRegistry
from core.context_compactor import ContextCompactor
from core.worktree import WorkTreeManager
from core.llm_provider import BaseLLMProvider
from toolkits.tool_registry import ToolRegistry


class SpawnAgentTool:
    """
    Lead Agent 专用的 spawn_agent 工具。
    
    这不是一个 BaseTool 子类，而是一个特殊的内部工具，
    由 AgentSpawner 直接处理，不走 ToolRegistry 的常规调度流。
    
    Schema 会被注册到 ToolRegistry 中供 LLM 识别。
    """

    @staticmethod
    def get_schema() -> Dict[str, Any]:
        return {
            "name": "spawn_agent",
            "description": (
                "派生一个专业的子 Agent 来执行特定任务。"
                "可选的 agent_type: 'browser'(网页操作), 'coding'(代码执行)。"
                "子 Agent 会在独立的沙箱中执行任务，完成后返回结果摘要。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "description": "子 Agent 类型: 'browser' 或 'coding'",
                        "enum": ["browser", "coding"]
                    },
                    "task": {
                        "type": "string",
                        "description": "分配给子 Agent 的具体任务描述（必须清晰、具体、可执行）"
                    }
                },
                "required": ["agent_type", "task"]
            }
        }


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
    ):
        self.tool_registry = tool_registry
        self.hook_registry = hook_registry
        self.llm_provider = llm_provider
        self.worktree_manager = worktree_manager
        self.compactor = compactor
        self._agent_defs: Dict[str, AgentDefinition] = {}
        self._spawn_count = 0  # 孵化计数器

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
        env_vars: Dict[str, str] = None,
        parent_agent_type: str = "",
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

        # 2. 递归防护
        if parent_agent_type == "verification":
            return {
                "success": False,
                "error": "Verification Agent 禁止派生子 Agent（递归防护）",
            }

        if not agent_def.can_spawn and agent_type != parent_agent_type:
            # can_spawn=False 的 Agent 不能派生其他 Agent，但可以被别人派生
            pass

        # 3. 创建上下文和工作区
        self._spawn_count += 1
        task_id = f"{agent_type}_{int(time.time())}_{self._spawn_count}"
        worktree_path = self.worktree_manager.get_or_create_worktree(task_id)

        # ✨ V4.1 优化：如果任务内容过大，自动溢出到物理文件以防止 504 超时
        PAYLOAD_THRESHOLD = 10_000
        original_task = task
        if len(task) > PAYLOAD_THRESHOLD:
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
            task=task,
            worktree_path=str(worktree_path),
            env_vars=env_vars or {},
        )

        print(
            f"\n[AgentSpawner] 🚀 孵化 {agent_type.upper()} Agent "
            f"(任务摘要: {task[:50]}...)"
        )

        # 4. 执行引擎驱动
        events = []
        final_result = ""

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
                elif event_type == "llm_error":
                    print(f"  [{agent_type}] ❌ LLM 错误: {event['error']}")
                elif event_type == "context_compacted":
                    print(f"  [{agent_type}] 📦 上下文已压缩")

        except Exception as e:
            print(f"  [{agent_type}] 💥 执行异常: {e}")
            return {
                "success": False,
                "agent_type": agent_type,
                "task": task,
                "error": str(e),
                "events": events,
            }

        # 5. 收集结果
        summary = context.to_summary()
        summary["success"] = True
        summary["events_count"] = len(events)
        summary["worktree_path"] = str(worktree_path)

        print(f"[AgentSpawner] ✅ {agent_type.upper()} Agent 完成 (共 {len(events)} 事件)")

        return summary
