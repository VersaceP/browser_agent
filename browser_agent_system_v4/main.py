"""
main.py — V4 多 Agent 系统终端入口

一键演示完整的 4-Agent 协作工作流：
1. 初始化所有基础设施（WorkTree, ToolRegistry, HookRegistry, ResourceManager...）
2. 注册 4 个内建 Agent
3. 注册 Hook 处理器（安全校验、Verification 触发）
4. 接受用户输入任务 → Lead Agent 拆解 → 派生子 Agent → 完整协作流程
5. 安全回收所有资源

使用方式:
    python main.py                    # 交互模式
    python main.py --demo             # 运行内置演示任务
    python main.py --test-browser     # 测试浏览器工具
    python main.py --test-security    # 测试安全防御
"""

import sys
import os
import asyncio
import argparse
import json
import time

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.agent_definition import AgentDefinition, TrustLevel, build_builtin_agents
from core.teammate_context import TeammateContext
from core.worktree import WorkTreeManager
from core.llm_provider import LLMFactory, ModelConfig, BaseLLMProvider
from core.hook_registry import HookRegistry, HookEvent, HookAction, HookResult
from core.context_compactor import ContextCompactor
from core.prompt_builder import build_system_prompt
from core.resource_manager import ResourceManager
from core.agent_spawner import AgentSpawner, SpawnAgentTool
from core.execution_loop import execute_turn
from toolkits.tool_registry import ToolRegistry
from toolkits.base_tool import BaseTool
from toolkits.browser_tools import get_all_browser_tools, get_browser_manager
from toolkits.file_tools import get_all_file_tools
from toolkits.code_tools import get_all_code_tools
from permissions.input_sanitizer import sanitize_url, sanitize_path, SanitizationError
from permissions.denial_tracker import DenialTracker


# ══════════════════════════════════════════════════════════════
#  全局基础设施
# ══════════════════════════════════════════════════════════════

# 工作区管理器
worktree_manager = WorkTreeManager()

# 工具注册表
tool_registry = ToolRegistry()

# 生命周期 Hook 注册表
hook_registry = HookRegistry()

# 上下文压缩器
compactor = ContextCompactor(threshold_ratio=0.8, keep_recent=4)

# 熔断器
denial_tracker = DenialTracker(max_consecutive_denials=5)

# 浏览器资源管理器
resource_manager = ResourceManager()


# ══════════════════════════════════════════════════════════════
#  Hook 处理器定义
# ══════════════════════════════════════════════════════════════

async def security_hook(payload: dict) -> HookResult:
    """
    PRE_TOOL_EXECUTE 安全校验 Hook。
    
    挂载于工具执行前，对输入参数进行安全审查：
    - URL 协议白名单校验
    - 路径穿越防御
    - 熔断器检查
    """
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    agent_type = payload.get("agent_type", "")
    worktree_path = payload.get("worktree_path", "")

    # 熔断器检查
    if denial_tracker.is_circuit_broken(agent_type):
        return HookResult(
            action=HookAction.BLOCK,
            reason=f"Agent '{agent_type}' 已被熔断器封锁，请等待冷却期结束"
        )

    try:
        # URL 安全校验（浏览器导航工具）
        if tool_name == "navigate":
            url = tool_input.get("url", "")
            if url:
                sanitize_url(url)

        # 路径安全校验（文件操作工具）
        if tool_name in ("write_file", "read_file", "run_python"):
            filename = tool_input.get("filename", "") or tool_input.get("script_name", "")
            if filename and worktree_path:
                sanitize_path(filename, worktree_path)

        # 校验通过，重置熔断器
        denial_tracker.record_approval(agent_type)
        return HookResult(action=HookAction.ALLOW)

    except SanitizationError as e:
        # 安全校验失败，记录拒绝
        is_broken = denial_tracker.record_denial(agent_type, str(e))
        return HookResult(action=HookAction.BLOCK, reason=str(e))


async def session_start_hook(payload: dict) -> HookResult:
    """
    SESSION_START Hook：浏览器环境分配。
    
    检查 Agent 的 env_vars 中是否有 PROFILE_ID，
    如果有则自动分配浏览器实例。
    """
    agent_type = payload.get("agent_type", "")
    env_vars = payload.get("env_vars", {})

    if agent_type == "browser":
        profile_id = env_vars.get("PROFILE_ID", "default")
        try:
            await resource_manager.acquire_browser(profile_id)
            print(f"[Hook:SESSION_START] 🌐 为 Browser Agent 分配浏览器: {profile_id}")
        except Exception as e:
            print(f"[Hook:SESSION_START] ⚠️ 浏览器分配失败: {e}")

    return HookResult(action=HookAction.ALLOW)


# ══════════════════════════════════════════════════════════════
#  SpawnAgent 工具实现（Lead Agent 专用的特殊工具）
# ══════════════════════════════════════════════════════════════

class SpawnAgentToolImpl(BaseTool):
    """
    Lead Agent 派生子 Agent 的工具。
    
    这是 Lead Agent 唯一的工具，用于将子任务委派给专业 Agent。
    内部调用 AgentSpawner.spawn() 完成孵化和执行。
    """
    name = "spawn_agent"
    description = (
        "派生一个专业的子 Agent 来执行特定任务。"
        "可选的 agent_type: 'browser'(网页操作), 'coding'(代码执行)。"
        "子 Agent 会在独立的沙箱中执行任务，完成后返回结果摘要。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "description": "子 Agent 类型: 'browser' 或 'coding'",
                "enum": ["browser", "coding"]
            },
            "task": {
                "type": "string",
                "description": "分配给子 Agent 的具体任务描述"
            }
        },
        "required": ["agent_type", "task"]
    }
    is_destructive = False
    max_result_chars = 5000
    required_trust_level = TrustLevel.WRITE

    def __init__(self, spawner: AgentSpawner):
        self._spawner = spawner

    async def execute(self, agent_type: str = "", task: str = "", **kwargs) -> str:
        if not agent_type or not task:
            return "[参数错误] 必须提供 agent_type 和 task"

        result = await self._spawner.spawn(
            agent_type=agent_type,
            task=task,
            parent_agent_type="lead",
        )

        if result.get("success"):
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


# ══════════════════════════════════════════════════════════════
#  系统初始化
# ══════════════════════════════════════════════════════════════

def initialize_system(llm_provider: BaseLLMProvider) -> AgentSpawner:
    """
    初始化 V4 系统所有基础设施。
    
    :param llm_provider: LLM 提供方实例
    :return: 已初始化的 AgentSpawner
    """
    print("=" * 60)
    print("  V4 多 Agent 系统初始化")
    print("=" * 60)

    # 1. 注册所有工具
    print("\n[1/4] 注册工具...")
    tool_registry.register_many(get_all_browser_tools())
    tool_registry.register_many(get_all_file_tools())
    tool_registry.register_many(get_all_code_tools())
    print(f"  已注册 {len(tool_registry)} 个标准工具: {tool_registry.list_tools()}")

    # 2. 创建 AgentSpawner
    spawner = AgentSpawner(
        tool_registry=tool_registry,
        hook_registry=hook_registry,
        llm_provider=llm_provider,
        worktree_manager=worktree_manager,
        compactor=compactor,
    )

    # 3. 注册 spawn_agent 工具（Lead Agent 专用）
    spawn_tool = SpawnAgentToolImpl(spawner)
    tool_registry.register(spawn_tool)
    print(f"  已注册 spawn_agent 工具，共 {len(tool_registry)} 个工具")

    # 4. 注册内建 Agent
    print("\n[2/4] 注册 Agent...")
    spawner.register_builtin_agents()

    # 5. 注册 Hook 处理器
    print("\n[3/4] 注册 Hook 处理器...")
    hook_registry.register(HookEvent.PRE_TOOL_EXECUTE, security_hook)
    hook_registry.register(HookEvent.SESSION_START, session_start_hook)
    print(f"  PRE_TOOL_EXECUTE: {hook_registry.list_handlers(HookEvent.PRE_TOOL_EXECUTE)} 个处理器")
    print(f"  SESSION_START: {hook_registry.list_handlers(HookEvent.SESSION_START)} 个处理器")

    # 6. 显示资源状态
    print("\n[4/4] 系统就绪!")
    print(f"  WorkTree 基础目录: {worktree_manager.base_dir}")
    print(f"  熔断器阈值: {denial_tracker.max_consecutive} 次连续拒绝")
    print(f"  压缩阈值: {compactor.threshold_ratio:.0%}")
    print("=" * 60)

    return spawner


# ══════════════════════════════════════════════════════════════
#  运行模式
# ══════════════════════════════════════════════════════════════

async def run_demo(spawner: AgentSpawner):
    """
    内置演示：完整的 4-Agent 协作流程。
    Lead Agent → Browser Agent(抓取网页) → Coding Agent(处理数据) → 输出
    """
    print("\n" + "=" * 60)
    print("  🎬 运行内置演示：网页数据抓取与处理")
    print("=" * 60)

    task = (
        "请帮我完成以下任务：\n"
        "1. 使用 browser agent 打开 https://httpbin.org/html 并提取页面文本内容\n"
        "2. 使用 coding agent 将提取到的文本保存为文件并统计字数\n"
        "请按步骤派生对应的专业 Agent 来执行。"
    )

    print(f"\n📋 任务: {task}\n")

    result = await spawner.spawn(
        agent_type="lead",
        task=task,
    )

    print("\n" + "=" * 60)
    print("  📊 演示结果")
    print("=" * 60)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    return result


async def run_browser_test(spawner: AgentSpawner):
    """
    浏览器工具单独测试：直接调用 Browser Agent 完成网页操作。
    """
    print("\n" + "=" * 60)
    print("  🌐 浏览器工具测试")
    print("=" * 60)

    task = (
        "请执行以下操作：\n"
        "1. 打开 https://httpbin.org/html\n"
        "2. 提取页面的完整文本内容\n"
        "3. 将提取到的文本保存为 result.txt 文件\n"
        "4. 截取页面截图保存为 page.png"
    )

    print(f"\n📋 任务: {task}\n")

    result = await spawner.spawn(
        agent_type="browser",
        task=task,
    )

    print("\n📊 浏览器测试结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    return result


async def run_security_test():
    """
    安全防御测试：验证 L2/L3 安全机制。
    """
    print("\n" + "=" * 60)
    print("  🛡️ 安全防御测试")
    print("=" * 60)

    test_results = []

    # 测试 1：路径穿越防御
    print("\n--- 测试 1: 路径穿越防御 ---")
    try:
        sanitize_path("../../../../etc/passwd", "/tmp/test_worktree")
        test_results.append(("路径穿越防御", "❌ 失败 - 未拦截"))
    except SanitizationError as e:
        test_results.append(("路径穿越防御", f"✅ 成功 - {e}"))
        print(f"  ✅ 拦截成功: {e}")

    # 测试 2：URL 协议校验
    print("\n--- 测试 2: URL 协议校验 ---")
    for url in ["file:///etc/passwd", "javascript:alert(1)", "https://example.com"]:
        try:
            sanitize_url(url)
            test_results.append((f"URL '{url}'", "✅ 放行"))
            print(f"  ✅ 放行: {url}")
        except SanitizationError as e:
            test_results.append((f"URL '{url}'", f"🛡️ 拦截 - {e}"))
            print(f"  🛡️ 拦截: {e}")

    # 测试 3：熔断器
    print("\n--- 测试 3: 连续拒绝熔断器 ---")
    test_tracker = DenialTracker(max_consecutive_denials=3)
    for i in range(4):
        broken = test_tracker.record_denial("test_agent", f"测试拒绝 #{i+1}")
        if broken:
            test_results.append(("熔断器触发", f"✅ 第 {i+1} 次触发熔断"))
            print(f"  ✅ 第 {i+1} 次拒绝触发熔断!")
            break

    is_broken = test_tracker.is_circuit_broken("test_agent")
    print(f"  熔断状态: {'已熔断' if is_broken else '正常'}")

    # 测试 4：工具权限过滤
    print("\n--- 测试 4: 工具权限过滤 ---")
    agent_defs = build_builtin_agents()
    for agent_type, agent_def in agent_defs.items():
        tools = tool_registry.filter_tools(agent_def)
        tool_names = [t.name for t in tools]
        test_results.append((f"{agent_type} 可用工具", str(tool_names)))
        print(f"  {agent_type}: {tool_names}")

    # 测试 5：上下文压缩
    print("\n--- 测试 5: 上下文压缩 ---")
    ctx = TeammateContext(
        agent_type="test",
        task="测试任务",
        max_tokens=100,  # 设置很低的阈值以触发压缩
    )
    # 注入大量消息模拟超限
    for i in range(10):
        ctx.append_message("user", f"这是一条很长的测试消息 #{i}，包含大量文本内容。" * 20)
        ctx.append_message("assistant", [{"type": "text", "text": f"收到消息 #{i}" * 10}])

    ratio = ctx.get_token_ratio()
    should = compactor.should_compact(ctx)
    print(f"  Token 水位: {ratio:.1%}")
    print(f"  消息数: {len(ctx.session_messages)}")
    print(f"  需要压缩: {'是' if should else '否'}")

    if should:
        compressed = await compactor.compact_if_needed(ctx)
        print(f"  压缩后消息数: {len(ctx.session_messages)}")
        print(f"  压缩后 Token 水位: {ctx.get_token_ratio():.1%}")
        test_results.append(("上下文压缩", "✅ 成功"))

    # 汇总
    print("\n" + "=" * 60)
    print("  📊 安全测试汇总")
    print("=" * 60)
    for name, result in test_results:
        print(f"  {name}: {result}")

    return test_results


async def run_interactive(spawner: AgentSpawner):
    """交互模式：用户输入任务，Lead Agent 执行"""
    print("\n" + "=" * 60)
    print("  🤖 V4 多 Agent 系统 - 交互模式")
    print("  输入任务描述，Lead Agent 将自动拆解并调度子 Agent 执行")
    print("  输入 'quit' 或 'exit' 退出")
    print("=" * 60)

    while True:
        try:
            print()
            task = input("📋 请输入任务 > ").strip()

            if not task:
                continue
            if task.lower() in ("quit", "exit", "q"):
                print("👋 再见!")
                break

            result = await spawner.spawn(
                agent_type="lead",
                task=task,
            )

            print("\n📊 执行结果:")
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

        except KeyboardInterrupt:
            print("\n👋 已中断，退出!")
            break
        except Exception as e:
            print(f"\n❌ 执行出错: {e}")


async def main():
    """主入口"""
    parser = argparse.ArgumentParser(description="V4 多 Agent 系统")
    parser.add_argument("--demo", action="store_true", help="运行内置演示任务")
    parser.add_argument("--test-browser", action="store_true", help="测试浏览器工具")
    parser.add_argument("--test-security", action="store_true", help="测试安全防御")
    parser.add_argument("--model", default="claude-sonnet-4-20250514", help="LLM 模型 ID")
    parser.add_argument("--provider", default="anthropic", help="LLM 提供方")
    args = parser.parse_args()

    # 创建 LLM Provider
    config = ModelConfig(
        provider=args.provider,
        model_id=args.model,
    )

    try:
        llm_provider = LLMFactory.create_provider(config)
        print(f"[LLM] ✅ 已连接 {args.provider} ({args.model})")
    except ValueError as e:
        print(f"[LLM] ❌ {e}")
        print("[提示] 请设置环境变量 ANTHROPIC_AUTH_TOKEN 和 ANTHROPIC_BASE_URL")
        return

    # 初始化系统
    spawner = initialize_system(llm_provider)

    try:
        if args.test_security:
            # 安全测试不需要 LLM，可以直接跑
            await run_security_test()

        elif args.test_browser:
            await run_browser_test(spawner)

        elif args.demo:
            await run_demo(spawner)

        else:
            await run_interactive(spawner)

    finally:
        # 安全回收所有资源
        print("\n[清理] 回收所有资源...")
        await resource_manager.release_all()
        print("[清理] ✅ 完成")


if __name__ == "__main__":
    asyncio.run(main())
