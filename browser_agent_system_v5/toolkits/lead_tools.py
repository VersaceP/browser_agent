import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from toolkits.base_tool import BaseTool
from core.agent_definition import TrustLevel

if TYPE_CHECKING:
    from core.agent_spawner import AgentSpawner

# 记录已通过计划审批的 session（使用 dict 模拟无界集合防内存泄露）
_approved_plan_sessions: dict = {}

def get_approved_plan_sessions() -> set:
    """获取所有已通过审批的 session_id 集合"""
    return set(_approved_plan_sessions.keys())

def clear_approved_plan_session(session_id: str) -> None:
    """清理指定 session 的审批状态"""
    _approved_plan_sessions.pop(session_id, None)

def _check_off_plan_item(worktree_path: str, task_description: str, agent_type: str) -> None:
    """
    在 execution_plan.md 中，将匹配 task_description 的未完成条目打勾。
    
    匹配策略：
    1. 精确匹配：在计划行中找到包含 agent_type 且与 task 关键词匹配的 '- [ ]' 行
    2. 模糊匹配：取 task 前 30 字符作为关键词进行子串匹配
    3. 优先匹配最近未被勾选的条目
    """
    if not worktree_path:
        return
    
    plan_file = Path(worktree_path).parent / "execution_plan.md"
    if not plan_file.exists():
        return
    
    try:
        content = plan_file.read_text(encoding="utf-8")
        lines = content.split("\n")
        updated = False
        
        # 从 task_description 提取关键词（取前 30 字符 + 去除标点）
        task_key = re.sub(r'[^\w\s]', '', task_description[:30]).strip()
        
        for i, line in enumerate(lines):
            # 只处理未完成的 checklist 条目: "- [ ]"
            if "- [ ]" not in line:
                continue
            
            # 策略1: 行中包含 agent_type 且包含 task 关键词（至少匹配 2 个词）
            task_words = task_key.split()[:5]  # 取前5个词
            matched_words = sum(1 for w in task_words if len(w) > 1 and w.lower() in line.lower())
            
            # 需要至少匹配一半的关键词（至少1个），且行中包含 agent_type
            if matched_words >= max(len(task_words) // 2, 1) and agent_type.lower() in line.lower():
                lines[i] = line.replace("- [ ]", "- [x]", 1)
                updated = True
                break  # 只勾选第一个匹配项
        
        if not updated:
            # 策略2: 只匹配 agent_type，勾选该 type 下第一个未完成条目
            for i, line in enumerate(lines):
                if "- [ ]" in line and agent_type.lower() in line.lower():
                    lines[i] = line.replace("- [ ]", "- [x]", 1)
                    updated = True
                    break
        
        if updated:
            plan_file.write_text("\n".join(lines), encoding="utf-8")
            print(f"[LeadTools] ✅ 已在 execution_plan.md 中打勾完成条目")
    except Exception as e:
        print(f"[LeadTools] ⚠️ 更新 execution_plan.md 失败: {e}")


class SubmitPlanTool(BaseTool):
    """
    Lead Agent 的执行计划提交工具。

    在派生任何子 Agent 之前，Lead Agent 必须先调用此工具提交执行计划。
    系统会将计划展示给用户，等待用户确认或提供修改意见。
    """
    name = "submit_plan"
    description = (
        "在派生子 Agent 之前，你必须先调用此工具提交你的执行计划（Todo List）。"
        "系统会将计划展示给用户审批。用户确认后你才能使用 spawn_agent。"
        "如果用户提出修改意见，你需要根据反馈调整计划后重新提交。"
        "对于涉及数据采集或处理的关键步骤，你应该安排 [Verification] 质检步骤，说明需要验证的具体内容。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": (
                    "执行计划的详细内容（Markdown 格式），包含：\n"
                    "1. 任务目标概述\n"
                    "2. 拆解的子任务列表（编号 + 分配的 Agent 类型 + 任务描述）\n"
                    "3. 预期产出物\n"
                    "4. 注意事项"
                )
            }
        },
        "required": ["plan"]
    }
    is_destructive = False
    max_result_chars = 2000
    required_trust_level = TrustLevel.WRITE

    async def execute(self, plan: str = "", **kwargs) -> str:
        if not plan.strip():
            return "[参数错误] 计划内容不能为空"

        session_id = kwargs.get("session_id", "")
        worktree_path = kwargs.get("_worktree_path", "")

        # 将计划写入工作区以便留档
        if worktree_path:
            plan_file = Path(worktree_path) / "execution_plan.md"
            try:
                plan_file.write_text(plan, encoding="utf-8")
            except Exception:
                pass

        # ── 已审批检查：已批准过的 session 不再弹出确认，直接放行 ──
        if session_id and session_id in _approved_plan_sessions:
            return (
                "[计划已确认] ✅ 该 session 已通过审批，可以直接派生子 Agent 执行。"
                "如需提交新计划，请重新调用 submit_plan。"
            )

        # 展示计划并等待用户确认
        print("\n" + "=" * 60)
        print("  📋 Lead Agent 执行计划（待审批）")
        print("=" * 60)
        print(plan)
        print("=" * 60)
        print("  ✅ 输入回车确认  |  输入修改意见后回车")
        print("=" * 60)

        user_input = await asyncio.to_thread(
            input, "\n👤 请审批 > "
        )
        user_input = user_input.strip()

        if not user_input:
            # 加入审批池前，提供一个安全上限防泄漏（保留最近 50 个会话防积累僵尸进程数据）
            if len(_approved_plan_sessions) > 50:
                oldest_key = next(iter(_approved_plan_sessions))
                _approved_plan_sessions.pop(oldest_key, None)
            
            # 用户直接回车 = 确认 (记录值为 timestamp 可用于未来审计)
            import time
            _approved_plan_sessions[session_id] = time.time()
            return (
                "[计划已批准] ✅ 用户已确认你的执行计划，现在可以使用 spawn_agent 派生子 Agent 来执行了。"
            )
        else:
            # 用户提出了修改意见，不标记为已批准
            return (
                f"[计划需修改] ⚠️ 用户对你的计划提出了修改意见：\n"
                f"「{user_input}」\n"
                f"请根据上述反馈调整你的计划，然后重新调用 submit_plan 提交修改后的版本。"
            )


class SpawnAgentToolImpl(BaseTool):
    """
    Lead Agent 派生子 Agent 的工具。

    必须在 submit_plan 获得用户审批后才能调用。
    内部调用 AgentSpawner.spawn() 完成孵化和执行。
    """
    name = "spawn_agent"
    description = (
        "派生一个专业的子 Agent 来执行特定任务。"
        "可选的 agent_type: 'browser'(网页操作), 'coding'(代码执行), 'verification'(结果质检)。"
        "可通过 max_turns 控制子 Agent 的最大执行轮次：简单搜索任务建议 20-30，复杂采集任务建议 40-60，不指定则使用默认值。"
        "子 Agent 会在独立的沙箱中执行任务，完成后返回结果摘要。"
        "注意：必须先调用 submit_plan 获得用户审批后才能使用此工具。"
        "如果你已设置任务进度板，子 Agent 会自动接收当前进度信息。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "description": "子 Agent 类型: 'browser'(网页操作), 'coding'(代码执行), 'verification'(结果质检)",
                "enum": ["browser", "coding", "verification"]
            },
            "task": {
                "type": "string",
                "description": "分配给子 Agent 的具体任务描述"
            },
            "max_turns": {
                "type": "integer",
                "description": (
                    "子 Agent 的最大执行轮次（可选）。"
                    "简单搜索/导航任务: 20-30轮; "
                    "中等复杂度采集任务: 40-50轮; "
                    "复杂多页面采集: 60-80轮。"
                    "不指定则使用 Agent 默认值 (browser=100, coding=50, verification=30)。"
                )
            }
        },
        "required": ["agent_type", "task"]
    }
    is_destructive = False
    max_result_chars = 5000
    required_trust_level = TrustLevel.WRITE

    def __init__(self, spawner: "AgentSpawner"):  # noqa: F821 - Type hints resolved at runtime
        self._spawner = spawner

    async def execute(self, agent_type: str = "", task: str = "", max_turns: int = 0, **kwargs) -> str:
        if not agent_type or not task:
            return "[参数错误] 必须提供 agent_type 和 task"

        session_id = kwargs.get("session_id", "")
        parent_worktree_path = kwargs.get("_worktree_path", "")
        parent_context = kwargs.get("_context") 

        # ── 计划审批门禁 ──
        if session_id and session_id not in _approved_plan_sessions:
            return (
                "[审批拦截] 🚫 你尚未提交执行计划！\n"
                "在派生子 Agent 之前，必须先调用 submit_plan 工具提交执行计划，\n"
                "并获得用户确认后才能使用 spawn_agent。"
            )

        # ── 注入父 Agent 的进度上下文 ──
        progress_context = ""
        if parent_context and hasattr(parent_context, 'progress_board') and parent_context.progress_board:
            progress_context = f"\n\n{parent_context.progress_summary()}"

        effective_task = task
        if progress_context:
            effective_task = f"{task}\n\n━━━ 当前任务进度 ━━━{progress_context}"

        spawn_kwargs = {
            "agent_type": agent_type,
            "task": effective_task,
            "parent_agent_type": "lead",
            "session_id": session_id,
            "env_vars": {"_parent_worktree": parent_worktree_path} if parent_worktree_path else None,
        }
        # 传递 max_turns 覆盖（0 表示使用默认值）
        if max_turns and max_turns > 0:
            spawn_kwargs["max_turns"] = max_turns

        result = await self._spawner.spawn(**spawn_kwargs)

        if result.get("success"):
            # 子 Agent 成功完成后，在 execution_plan.md 中打勾对应条目
            _check_off_plan_item(parent_worktree_path, task, agent_type)
            
            return (
                f"[子 Agent 执行完成]\n"
                f"  类型: {result.get('agent_type')}\n"
                f"  任务: {result.get('task', '')[:100]}\n"
                f"  工作区: {result.get('worktree_path', '')}\n"
                f"  消息轮次: {result.get('message_count', 0)}\n"
                f"  最终结果:\n{result.get('final_answer', '(无输出)')}"
            )
        else:
            return (
                f"[子 Agent 执行失败]\n"
                f"  错误: {result.get('error', '未知错误')}"
            )


class SpawnAgentsParallelTool(BaseTool):
    """
    Lead Agent 并行派生多个子 Agent 的工具。

    适用于多个独立子任务可以同时执行的场景。
    注意：由于 Browser Agent 目前仅支持单实例，并行任务中最多只能包含一个 browser 类型。
    Coding Agent 和 Verification Agent 可以多个并行。
    """
    name = "spawn_agents_parallel"
    description = (
        "并行派生多个子 Agent 同时执行独立任务。"
        "适用于多个子任务之间没有依赖关系、可以同时执行的场景。"
        "⚠️ 重要限制：当前系统同一时间只能运行一个 Browser Agent，"
        "因此 tasks 列表中最多只能包含一个 agent_type 为 'browser' 的任务。"
        "coding 和 verification 类型可以多个并行。"
        "每个任务可单独指定 max_turns 控制执行轮次。"
        "必须先调用 submit_plan 获得用户审批后才能使用此工具。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_type": {
                            "type": "string",
                            "description": "子 Agent 类型: 'browser', 'coding', 'verification'",
                            "enum": ["browser", "coding", "verification"]
                        },
                        "task": {
                            "type": "string",
                            "description": "分配给子 Agent 的具体任务描述"
                        },
                        "max_turns": {
                            "type": "integer",
                            "description": (
                                "该子 Agent 的最大执行轮次（可选）。"
                                "简单任务: 20-30轮; 中等复杂度: 40-50轮; 复杂任务: 60-80轮。"
                                "不指定则使用 Agent 默认值。"
                            )
                        }
                    },
                    "required": ["agent_type", "task"]
                },
                "description": "要并行执行的子任务列表，每项包含 agent_type、task 和可选的 max_turns"
            }
        },
        "required": ["tasks"]
    }
    is_destructive = False
    max_result_chars = 8000
    required_trust_level = TrustLevel.WRITE

    def __init__(self, spawner: "AgentSpawner"):  # noqa: F821
        self._spawner = spawner

    async def execute(self, tasks: Optional[list] = None, **kwargs) -> str:
        if not tasks:
            return "[参数错误] tasks 列表不能为空"

        session_id = kwargs.get("session_id", "")
        parent_worktree_path = kwargs.get("_worktree_path", "")
        parent_context = kwargs.get("_context")  # 父 Agent 的 TeammateContext

        # ── 计划审批门禁 ──
        if session_id and session_id not in _approved_plan_sessions:
            return (
                "[审批拦截] 🚫 你尚未提交执行计划！\n"
                "在派生子 Agent 之前，必须先调用 submit_plan 工具提交执行计划，\n"
                "并获得用户确认后才能使用 spawn_agents_parallel。"
            )

        # ── Browser Agent 单实例限制检查 ──
        browser_count = sum(1 for t in tasks if t.get("agent_type") == "browser")
        if browser_count > 1:
            return (
                "[限制拦截] 🚫 当前系统同一时间只能运行一个 Browser Agent，"
                f"但你的任务列表中包含了 {browser_count} 个 browser 任务。"
                "请将多个 browser 任务拆分为串行执行（使用 spawn_agent），"
                "或将其中一个改为其他类型。"
            )

        # ── 注入父 Agent 的进度上下文到每个子任务 ──
        progress_context = ""
        if parent_context and hasattr(parent_context, 'progress_board') and parent_context.progress_board:
            progress_context = f"\n\n━━━ 当前任务进度 ━━━\n{parent_context.progress_summary()}"

        enhanced_tasks = []
        for t in tasks:
            enhanced_t = dict(t)
            if progress_context:
                enhanced_t["task"] = t["task"] + progress_context
            enhanced_tasks.append(enhanced_t)

        result = await self._spawner.spawn_batch(
            tasks=enhanced_tasks,
            parent_agent_type="lead",
            session_id=session_id,
            parent_worktree_path=parent_worktree_path,
        )

        # 为每个成功的子任务打勾 execution_plan.md
        if result.get("results"):
            for item in result["results"]:
                if item.get("success"):
                    _check_off_plan_item(
                        parent_worktree_path,
                        item.get("task", ""),
                        item.get("agent_type", ""),
                    )

        # 格式化输出
        lines = [
            f"[并行执行完成] 共 {result.get('total', 0)} 个任务, "
            f"{result.get('success_count', 0)} 成功, {result.get('fail_count', 0)} 失败",
            "",
        ]
        for i, item in enumerate(result.get("results", []), 1):
            status = "✅ 成功" if item.get("success") else "❌ 失败"
            lines.append(f"  {i}. [{item.get('agent_type', '?').upper()}] {status}")
            lines.append(f"     任务: {item.get('task', '')[:80]}")
            if item.get("success"):
                lines.append(f"     结果: {item.get('final_answer', '(无输出)')[:200]}")
                lines.append(f"     轮次: {item.get('message_count', 0)}")
            else:
                lines.append(f"     错误: {item.get('error', '未知错误')[:200]}")

        return "\n".join(lines)


class InitProgressTool(BaseTool):
    """
    Lead Agent 初始化任务进度板。

    在执行复杂多步骤任务时，Lead Agent 应先调用此工具设置进度追踪目标，
    然后再派生子 Agent 执行。进度板信息会自动注入到子 Agent 的任务描述中，
    帮助子 Agent 了解已完成的进度，避免重复工作。
    """
    name = "init_progress"
    description = (
        "初始化任务进度板，设置需要追踪的子目标。"
        "适用于涉及多步骤采集、多文件处理等可量化进度的任务。"
        "初始化后，进度板信息会自动注入到后续 spawn 的子 Agent 任务中，"
        "子 Agent 也能通过 update_progress 更新进度。"
        "如果你不需要追踪量化进度（如单步简单任务），可以不调用此工具。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "目标唯一标识（如 'collect_posts', 'process_files'）"
                        },
                        "description": {
                            "type": "string",
                            "description": "目标描述（如 '收集论坛帖子'）"
                        },
                        "target": {
                            "type": "integer",
                            "description": "目标数量（如 30 表示需收集 30 个帖子）。不需要量化时省略"
                        }
                    },
                    "required": ["id", "description"]
                },
                "description": "子目标列表"
            }
        },
        "required": ["goals"]
    }
    is_destructive = False
    max_result_chars = 2000
    required_trust_level = TrustLevel.WRITE

    async def execute(self, goals: Optional[list] = None, **kwargs) -> str:
        if not goals:
            return "[参数错误] goals 列表不能为空"

        ctx = kwargs.get("_context")
        if not ctx:
            return "[系统错误] 无法获取上下文"

        ctx.progress_init(goals)
        return (
            f"[进度板已初始化] 共 {len(goals)} 个子目标\n"
            f"{ctx.progress_summary()}\n\n"
            f"子 Agent 执行时会自动接收当前进度信息。"
            f"你可以在子 Agent 完成后使用 update_progress 更新进度。"
        )


class UpdateProgressTool(BaseTool):
    """
    更新任务进度板。

    所有 Agent（Lead/Browser/Coding）都可以使用此工具更新自己的进度板。
    Browser Agent 典型用法：每收集一批数据后更新进度（如 increment=5）。
    Coding Agent 典型用法：每处理完一个文件后更新进度。
    Lead Agent 典型用法：收到子 Agent 结果后汇总更新进度。
    """
    name = "update_progress"
    description = (
        "更新任务进度板的某个子目标。"
        "Browser Agent：每成功收集/保存一批数据后更新进度（如 increment=5）。"
        "Coding Agent：每处理完一个文件后更新进度。"
        "Lead Agent：收到子 Agent 结果后汇总更新进度。"
        "如果进度板尚未初始化，会自动创建一个默认子目标。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "goal_id": {
                "type": "string",
                "description": "要更新的子目标 ID（如 'collect_posts'）"
            },
            "increment": {
                "type": "integer",
                "description": "进度增量（如已多收集了 5 个帖子则传 5），默认为 1"
            },
            "status": {
                "type": "string",
                "description": "直接设置状态（可选）：pending/in_progress/completed",
                "enum": ["pending", "in_progress", "completed"]
            },
            "note": {
                "type": "string",
                "description": "附加备注（如 '已保存到 data/posts_batch1.txt'），可选"
            }
        },
        "required": ["goal_id"]
    }
    is_destructive = False
    max_result_chars = 1000
    required_trust_level = TrustLevel.READONLY  # 只读即可，因为只是更新内存中的进度板

    async def execute(self, goal_id: str = "", increment: int = 1, status: Optional[str] = None, note: Optional[str] = None, **kwargs) -> str:
        if not goal_id:
            return "[参数错误] 必须提供 goal_id"

        ctx = kwargs.get("_context")
        if not ctx:
            return "[系统错误] 无法获取上下文"

        # 如果进度板未初始化，自动创建默认子目标
        if not ctx.progress_board:
            ctx.progress_init([{"id": goal_id, "description": goal_id}])

        success = ctx.progress_update(
            goal_id=goal_id,
            increment=increment,
            status=status,
            note=note,
        )

        if not success:
            # goal_id 不存在，动态添加
            ctx.progress_board.setdefault("goals", {})[goal_id] = {
                "description": goal_id,
                "target": None,
                "current": increment,
                "status": status or "in_progress",
                "notes": [note] if note else [],
            }
            success = True

        return f"[进度已更新]\n{ctx.progress_summary()}"

