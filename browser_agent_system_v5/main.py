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
from core.hook_registry import HookRegistry, HookEvent, HookAction, HookResult, RepetitionCompactor
from core.context_compactor import ContextCompactor
from core.prompt_builder import build_system_prompt
from core.resource_manager import ResourceManager
from core.agent_spawner import AgentSpawner
from core.execution_loop import execute_turn
from core.session_persistence import save_session, load_session, list_sessions, SessionPersistenceError
from core.skill_registry import SkillRegistry  # 新增导入
from toolkits.tool_registry import ToolRegistry
from toolkits.base_tool import BaseTool
from toolkits.browser_tools import get_all_browser_tools, get_browser_manager
from toolkits.file_tools import get_all_file_tools
from toolkits.code_tools import get_all_code_tools
from toolkits.lead_tools import SubmitPlanTool, SpawnAgentToolImpl, SpawnAgentsParallelTool, InitProgressTool, UpdateProgressTool, clear_approved_plan_session
from permissions.input_sanitizer import sanitize_url, sanitize_path, sanitize_payment_action, sanitize_shell_input, SanitizationError
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
    session_id = payload.get("session_id", "")
    worktree_path = payload.get("worktree_path", "")

    # 构造实例级唯一标识（同类型不同会话的 Agent 各自独立追踪）
    agent_id = f"{session_id}:{agent_type}" if session_id else agent_type

    # 熔断器检查
    if denial_tracker.is_circuit_broken(agent_id):
        return HookResult(
            action=HookAction.BLOCK,
            reason=f"Agent '{agent_id}' 已被熔断器封锁，请等待冷却期结束"
        )

    try:
        # URL 安全校验（浏览器导航工具）
        if tool_name == "navigate":
            url = tool_input.get("url", "")
            if url:
                sanitize_url(url)

        # 路径与文件名/命令安全校验（文件与代码操作工具）
        if tool_name in ("write_file", "read_file", "run_python"):
            filename = tool_input.get("filename", "") or tool_input.get("script_name", "")
            if filename:
                # 防注入：拦截文件名或命令中的 shell 元字符
                sanitize_shell_input(filename)
                # 防路径穿越：拦截沙箱外访问
                if worktree_path:
                    sanitize_path(filename, worktree_path)

        # L4 支付安全拦截（浏览器点击/填写工具）
        if tool_name in ("click_element", "fill_form"):
            sanitize_payment_action(tool_name, tool_input)

        # 校验通过，重置熔断器
        denial_tracker.record_approval(agent_id)
        return HookResult(action=HookAction.ALLOW)

    except SanitizationError as e:
        # 安全校验失败，记录拒绝
        is_broken = denial_tracker.record_denial(agent_id, str(e))
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


async def post_tool_hook(payload: dict) -> HookResult:
    """
    POST_TOOL_EXECUTE Hook：工具执行后的日志审计。

    记录每次工具调用的结果摘要，用于事后分析和调试。
    """
    tool_name = payload.get("tool_name", "")
    agent_type = payload.get("agent_type", "")
    session_id = payload.get("session_id", "")
    tool_result = payload.get("tool_result", "")

    # 记录工具执行日志（仅输出摘要，避免刷屏）
    result_preview = tool_result[:80].replace("\n", " ") if tool_result else "(空)"
    print(f"[Hook:POST_TOOL] 📝 [{session_id}:{agent_type}] {tool_name} → {result_preview}")

    return HookResult(action=HookAction.ALLOW)


async def pre_compact_hook(payload: dict) -> HookResult:
    """
    PRE_COMPACT Hook：上下文压缩前的审计。

    在执行上下文窗口压缩之前触发，可用于：
    - 记录压缩前的 Token 水位
    - 自定义保留策略（如保留关键工具调用结果）
    """
    context = payload.get("context")
    turn = payload.get("turn", 0)

    if context:
        ratio = context.get_token_ratio()
        msg_count = len(context.session_messages)
        print(f"[Hook:PRE_COMPACT] 📦 Turn {turn} | Token 水位: {ratio:.0%} | 消息数: {msg_count}")

    return HookResult(action=HookAction.ALLOW)


async def post_compact_hook(payload: dict) -> HookResult:
    """
    POST_COMPACT Hook：上下文压缩后的审计。

    在执行上下文窗口压缩之后触发，用于：
    - 记录压缩后的 Token 水位
    - 统计压缩效果
    - 可观测性日志
    """
    context = payload.get("context")
    turn = payload.get("turn", 0)

    if context:
        ratio = context.get_token_ratio()
        msg_count = len(context.session_messages)
        print(f"[Hook:POST_COMPACT] ✅ Turn {turn} | 压缩完成 | Token 水位: {ratio:.0%} | 消息数: {msg_count}")

    return HookResult(action=HookAction.ALLOW)


async def pre_turn_complete_hook(payload: dict) -> HookResult:
    """
    PRE_TURN_COMPLETE Hook：Turn 结束前的轻量决策。

    仅负责判断是否需要触发自动质检：
    - Lead Agent 完成时 → 返回 BLOCK，通知编排层拉起 Verification
    - 其他 Agent → 返回 ALLOW，直接放行
    """
    agent_type = payload.get("agent_type", "")
    session_id = payload.get("session_id", "")
    turns_used = payload.get("turns_used", 0)
    final_text = payload.get("final_text", "")

    result_preview = final_text[:100].replace("\n", " ") if final_text else "(无输出)"
    print(f"[Hook:TURN_COMPLETE] ✅ [{session_id}:{agent_type}] 共 {turns_used} 轮 | 结果: {result_preview}")

    # 仅 Lead Agent 需要自动质检兜底
    if agent_type == "lead" and final_text.strip():
        return HookResult(action=HookAction.BLOCK, reason="needs_verification")

    return HookResult(action=HookAction.ALLOW)


async def post_agent_complete_hook(payload: dict) -> HookResult:
    """
    POST_AGENT_COMPLETE Hook：Agent 声明周期结束后的审计。

    在 Agent 完成所有工作（含自动质检）后触发，用于：
    - 记录最终执行统计
    - 审计质检结果
    """
    agent_type = payload.get("agent_type", "")
    session_id = payload.get("session_id", "")
    verify_report = payload.get("verification_report", "")

    if verify_report:
        print(f"[Hook:AGENT_COMPLETE] 📋 [{session_id}:{agent_type}] 含质检报告: {verify_report[:150]}")
    else:
        print(f"[Hook:AGENT_COMPLETE] 🏁 [{session_id}:{agent_type}] 执行结束")

    return HookResult(action=HookAction.ALLOW)


async def skill_security_hook(payload: dict) -> HookResult:
    """
    PRE_SKILL_INJECT 安全验证 Hook。
    
    在技能注入前执行安全检查，防止恶意技能注入提示词攻击。
    
    检测规则：
    1. 提示词覆盖关键词（"ignore previous", "override", "system prompt"）
    2. 过长内容（> 2000 tokens，粗略估计：1 token ≈ 4 字符）
    3. 可疑格式（过多特殊字符）
    4. 敏感操作关键词（"execute", "eval", "delete"）
    
    :param payload: {
        "agent_type": str,
        "session_id": str,
        "task": str,
        "selected_skills": List[Skill],
        "skill_names": List[str]
    }
    :return: HookResult(action=ALLOW/BLOCK, reason=...)
    """
    selected_skills = payload.get("selected_skills", [])
    
    # 可疑关键词列表
    prompt_override_keywords = [
        "ignore previous", "ignore all previous", "忽略之前", "忽略所有",
        "override", "覆盖", "system prompt", "系统提示",
        "forget", "忘记", "disregard", "不要理会"
    ]
    
    sensitive_keywords = [
        "execute", "eval", "exec", "delete", "rm -rf",
        "drop table", "truncate", "__import__"
    ]
    
    for skill in selected_skills:
        content_lower = skill.content.lower()
        
        # 检测 1：提示词覆盖关键词
        for keyword in prompt_override_keywords:
            if keyword in content_lower:
                return HookResult(
                    action=HookAction.BLOCK,
                    reason=f"技能 '{skill.name}' 包含可疑的提示词覆盖关键词: '{keyword}'"
                )
        
        # 检测 2：过长内容（> 2000 tokens ≈ 8000 字符）
        estimated_tokens = len(skill.content) / 4
        if estimated_tokens > 2000:
            return HookResult(
                action=HookAction.BLOCK,
                reason=f"技能 '{skill.name}' 内容过长 (估计 {estimated_tokens:.0f} tokens，限制 2000)"
            )
        
        # 检测 3：可疑格式（过多特殊字符，可能是混淆攻击）
        special_char_count = sum(1 for c in skill.content if not c.isalnum() and not c.isspace() and c not in '.,!?;:()[]{}"\'-—')
        if special_char_count > len(skill.content) * 0.1:  # 超过 10% 是特殊字符
            return HookResult(
                action=HookAction.BLOCK,
                reason=f"技能 '{skill.name}' 包含过多特殊字符 ({special_char_count} 个)，可能是混淆攻击"
            )
        
        # 检测 4：敏感操作关键词
        for keyword in sensitive_keywords:
            if keyword in content_lower:
                print(f"[Hook:SKILL_SECURITY] ⚠️ 技能 '{skill.name}' 包含敏感关键词 '{keyword}'，请谨慎使用")
                # 注意：这里只是警告，不阻止（因为可能是合法的 JavaScript 代码）
    
    # 所有检查通过
    print(f"[Hook:SKILL_SECURITY] ✅ 技能安全验证通过: {[s.name for s in selected_skills]}")
    return HookResult(action=HookAction.ALLOW)


async def skill_audit_hook(payload: dict) -> HookResult:
    """
    POST_SKILL_INJECT 审计日志 Hook。
    
    在技能注入后记录审计信息，用于统计和分析。
    
    :param payload: {
        "agent_type": str,
        "session_id": str,
        "injected_skills": List[str],
        "total_tokens": int
    }
    :return: HookResult(action=ALLOW)
    """
    agent_type = payload.get("agent_type", "")
    session_id = payload.get("session_id", "")
    injected_skills = payload.get("injected_skills", [])
    total_tokens = payload.get("total_tokens", 0)
    
    if injected_skills:
        print(f"[Hook:SKILL_AUDIT] 📝 [{session_id}:{agent_type}] 注入技能: {injected_skills} (约 {total_tokens} tokens)")
    else:
        print(f"[Hook:SKILL_AUDIT] 📝 [{session_id}:{agent_type}] 未注入任何技能")
    
    return HookResult(action=HookAction.ALLOW)


# ══════════════════════════════════════════════════════════════
#  系统初始化
# ══════════════════════════════════════════════════════════════

async def initialize_system(llm_provider: BaseLLMProvider) -> AgentSpawner:
    """
    初始化 V4 系统所有基础设施。
    
    :param llm_provider: LLM 提供方实例
    :return: 已初始化的 AgentSpawner
    """
    print("=" * 60)
    print("  V4 多 Agent 系统初始化")
    print("=" * 60)

    # 1. 注册所有工具
    print("\n[1/5] 注册工具...")
    tool_registry.register_many(get_all_browser_tools())
    tool_registry.register_many(get_all_file_tools())
    tool_registry.register_many(get_all_code_tools())
    print(f"  已注册 {len(tool_registry)} 个标准工具: {tool_registry.list_tools()}")

    # 1.5 初始化 SkillRegistry
    print("\n[1.5/5] 初始化技能注册表...")
    skill_registry = SkillRegistry(base_dir="skills/browser")
    load_result = skill_registry.load_all()
    print(f"  技能加载完成: {load_result['loaded']} 个成功, {load_result['failed']} 个失败")
    if load_result['errors']:
        print(f"  加载失败的文件:")
        for filename, error in load_result['errors'].items():
            print(f"    - {filename}: {error}")

    # 2. 创建 RepetitionCompactor（需要在 AgentSpawner 之前实例化）
    rep_compactor = RepetitionCompactor(threshold=5)

    # 3. 创建 AgentSpawner
    spawner = AgentSpawner(
        tool_registry=tool_registry,
        hook_registry=hook_registry,
        llm_provider=llm_provider,
        worktree_manager=worktree_manager,
        compactor=compactor,
        denial_tracker=denial_tracker,
        resource_manager=resource_manager,
        repetition_compactor=rep_compactor,
        skill_registry=skill_registry,  # 新增参数
    )

    # 4. 注册 Lead Agent 专用工具（计划审批 + 子 Agent 派生）
    plan_tool = SubmitPlanTool()
    spawn_tool = SpawnAgentToolImpl(spawner)
    spawn_parallel_tool = SpawnAgentsParallelTool(spawner)
    init_progress_tool = InitProgressTool()
    update_progress_tool = UpdateProgressTool()
    tool_registry.register(plan_tool)
    tool_registry.register(spawn_tool)
    tool_registry.register(spawn_parallel_tool)
    tool_registry.register(init_progress_tool)
    tool_registry.register(update_progress_tool)
    print(f"  已注册 submit_plan + spawn_agent + spawn_agents_parallel + init_progress + update_progress 工具，共 {len(tool_registry)} 个工具")

    # 5. 注册内建 Agent
    print("\n[2/5] 注册 Agent...")
    spawner.register_builtin_agents()

    # 6. 注册 Hook 处理器
    print("\n[3/5] 注册 Hook 处理器...")
    hook_registry.register(HookEvent.POST_TOOL_EXECUTE, rep_compactor.handler)  # type: ignore[arg-type]
    hook_registry.register(HookEvent.PRE_TOOL_EXECUTE, security_hook)
    hook_registry.register(HookEvent.SESSION_START, session_start_hook)
    hook_registry.register(HookEvent.POST_TOOL_EXECUTE, post_tool_hook)
    hook_registry.register(HookEvent.PRE_COMPACT, pre_compact_hook)
    hook_registry.register(HookEvent.POST_COMPACT, post_compact_hook)  # 新增 POST_COMPACT 处理器
    hook_registry.register(HookEvent.PRE_TURN_COMPLETE, pre_turn_complete_hook)
    hook_registry.register(HookEvent.POST_AGENT_COMPLETE, post_agent_complete_hook)
    # 注册技能相关 Hook
    hook_registry.register(HookEvent.PRE_SKILL_INJECT, skill_security_hook)
    hook_registry.register(HookEvent.POST_SKILL_INJECT, skill_audit_hook)
    print("  已注册技能安全验证和审计 Hook")
    for event in HookEvent:
        count = hook_registry.list_handlers(event)
        print(f"  {event.value}: {count} 个处理器")

    # 7. 预启动浏览器（避免第一次任务冷启动）
    try:
        print("\n[4/5] 预启动浏览器...")
        await resource_manager.acquire_browser("default")
        print("  浏览器预启动完成")
    except Exception as e:
        print(f"  浏览器预启动跳过: {e}")

    # 7. 显示资源状态
    print("\n[系统就绪]")
    print(f"  WorkTree 基础目录: {worktree_manager.base_dir}")
    print(f"  熔断器阈值: {denial_tracker.max_consecutive} 次连续拒绝")
    print(f"  压缩阈值: {compactor.threshold_ratio:.0%}")
    print(f"  已加载技能: {len(skill_registry.list_skills())} 个")
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

    if "context" in result:
        result.pop("context")

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

    if "context" in result:
        result.pop("context")

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
    ctx: TeammateContext = TeammateContext(
        agent_type="test",
        session_id="test_session",
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
    print("  🤖 V4 多 Agent 系统 - 交互模式")
    print("  输入任务描述，Lead Agent 将自动拆解并调度子 Agent 执行")
    print("  命令: /save <路径> | /load <路径> | /sessions [目录] | /delete <task_id> | /delete temp | /reset | quit")
    print("=" * 60)

    # 🚀 V4.1 升级：持久化 Lead Agent 上下文，支持多轮对话
    active_lead_context = None

    while True:
        try:
            print()
            raw = input("📋 > ").strip()

            if not raw:
                continue

            # ── 系统命令处理 ──────
            if raw.lower() in ("quit", "exit"):
                print("👋 已退出!")
                break

            if raw.lower() in ("/reset", "clear"):
                if active_lead_context:
                    denial_tracker.clear_session(active_lead_context.session_id)
                    clear_approved_plan_session(active_lead_context.session_id)
                active_lead_context = None
                print("🧹 会话已重置，记忆、熔断状态与审批记录已清空。")
                continue

            # /sessions — 列出可用 session
            if raw.lower().startswith("/sessions"):
                parts = raw.split(maxsplit=1)
                base = parts[1].strip() if len(parts) > 1 else "."
                sessions = list_sessions(os.path.abspath(base))
                if not sessions:
                    print("📂 未找到任何 session 文件。")
                else:
                    print(f"📂 找到 {len(sessions)} 个 session（按保存时间倒序）:")
                    for s in sessions:
                        task_preview = s["task"][:50] + ("…" if len(s["task"]) > 50 else "")
                        print(f"  [{s['saved_at_str']}] {s['session_id']}")
                        print(f"    Agent: {s['agent_type']} | 消息: {s['message_count']}")
                        print(f"    Task: {task_preview}")
                        print(f"    路径: {s['path']}")
                continue

            # /load — 从文件恢复 session
            if raw.lower().startswith("/load"):
                parts = raw.split(maxsplit=1)
                if len(parts) < 2:
                    print("❌ 用法: /load <文件路径或目录>")
                    continue
                path = parts[1].strip()
                try:
                    active_lead_context, _ = load_session(path)
                    denial_tracker.clear_session(active_lead_context.session_id)
                    # _approved_plan_sessions 永远只增不减，/load 不清空也不自动批准
                    # 每次 submit_plan 仍需人工审批，保证安全
                    print("✅ Session 已加载，继续上次任务吧！")
                except SessionPersistenceError as e:
                    print(f"❌ 加载失败: {e}")
                continue

            # /save — 保存当前 session 到文件
            if raw.lower().startswith("/save"):
                parts = raw.split(maxsplit=1)
                if len(parts) < 2:
                    print("❌ 用法: /save <文件路径或目录>")
                    continue
                path = parts[1].strip()
                if not active_lead_context:
                    print("⚠️ 当前没有活跃的 session，先执行一个任务后再保存。")
                    continue
                try:
                    meta = save_session(active_lead_context, path)
                    print(f"💾 已保存 session → {meta['file_path']}")
                    print(f"   消息数: {meta['message_count']} | Token: {meta['token_usage']}")
                except SessionPersistenceError as e:
                    print(f"❌ 保存失败: {e}")
                continue

            # /delete — 删除任务记录或临时文件
            if raw.lower().startswith("/delete"):
                parts = raw.split(maxsplit=1)
                if len(parts) < 2:
                    print("❌ 用法: /delete <task_id>  删除指定任务的 worktree 目录")
                    print("       /delete temp        清理所有 worktree 下的 spill_*.txt / extract_part*.txt 临时文件")
                    continue
                target = parts[1].strip()

                if target.lower() == "temp":
                    # 清理所有 worktree 下的临时文件
                    base = worktree_manager.base_dir
                    if not base.exists():
                        print("📂 worktrees 目录不存在，无需清理。")
                        continue
                    spill_patterns = ["spill_*.txt", "extract_part*.txt"]
                    deleted_count = 0
                    freed_bytes = 0
                    for task_dir in sorted(base.iterdir()):
                        if not task_dir.is_dir():
                            continue
                        for agent_dir in task_dir.rglob("data"):
                            if not agent_dir.is_dir():
                                continue
                            for pattern in spill_patterns:
                                for f in sorted(agent_dir.glob(pattern)):
                                    size = f.stat().st_size
                                    f.unlink()
                                    deleted_count += 1
                                    freed_bytes += size
                    freed_kb = freed_bytes / 1024
                    if deleted_count == 0:
                        print("🧹 未找到任何临时文件，工作区很干净！")
                    else:
                        print(f"🧹 已清理 {deleted_count} 个临时文件，释放 {freed_kb:.1f} KB 空间")
                else:
                    # 删除指定 task_id 的 worktree
                    task_id = target
                    # 支持完整 task_id（如 task_1776423619）或简写数字 ID
                    if not task_id.startswith("task_"):
                        # 尝试匹配 task_*{task_id}*
                        matches = [d for d in worktree_manager.list_worktrees() if task_id in d]
                        if len(matches) == 0:
                            print(f"❌ 未找到匹配 '{task_id}' 的任务记录。")
                            print(f"   现有任务: {', '.join(worktree_manager.list_worktrees())}")
                            continue
                        elif len(matches) > 1:
                            print(f"⚠️ '{task_id}' 匹配到多个任务，请指定完整 ID:")
                            for m in matches:
                                print(f"   {m}")
                            continue
                        task_id = matches[0]

                    task_dir = worktree_manager.base_dir / task_id
                    if not task_dir.exists():
                        print(f"❌ 任务 '{task_id}' 不存在。")
                        continue

                    # 如果是当前活跃 session，提示确认
                    is_active = (active_lead_context is not None and active_lead_context.session_id == task_id)
                    if is_active:
                        print(f"⚠️ 任务 '{task_id}' 是当前活跃 session，删除后上下文将丢失！")
                        confirm = input("  确认删除？(y/N): ").strip().lower()
                        if confirm != "y":
                            print("已取消。")
                            continue
                        active_lead_context = None
                        denial_tracker.clear_session(task_id)
                        clear_approved_plan_session(task_id)

                    # 计算目录大小
                    total_size = sum(f.stat().st_size for f in task_dir.rglob("*") if f.is_file())
                    total_mb = total_size / (1024 * 1024)

                    worktree_manager.cleanup_worktree(task_id)
                    print(f"🗑️ 已删除任务 '{task_id}'，释放 {total_mb:.2f} MB 空间")
                continue

            # ── 普通任务 ─────
            if active_lead_context:
                result = await spawner.chat(
                    context=active_lead_context,
                    message=raw,
                )
            else:
                result = await spawner.spawn(
                    agent_type="lead",
                    task=raw,
                )

            # 持久化上下文以便下轮对话使用
            if result.get("success") and "context" in result:
                active_lead_context = result.pop("context")

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
    parser.add_argument("--model", help="LLM 模型 ID (覆盖配置)")
    parser.add_argument("--provider", help="LLM 提供方 (覆盖配置)")
    parser.add_argument("--config", default="config.json", help="指定配置文件路径")
    args = parser.parse_args()

    # 1. 优先读取配置文件
    project_root = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(project_root, args.config)
    
    if os.path.exists(config_path):
        print(f"[配置] 正在读取本地配置文件: {config_path}")
        config = ModelConfig.load_from_file(config_path)
    else:
        config = ModelConfig()

    # 2. 命令行参数覆盖
    if args.provider:
        config.provider = args.provider
    if args.model:
        config.model_id = args.model

    try:
        llm_provider = LLMFactory.create_provider(config)
        print(f"[LLM] ✅ 已连接 {args.provider} ({args.model})")
    except ValueError as e:
        print(f"[LLM] ❌ {e}")
        print("[提示] 请设置环境变量 ANTHROPIC_AUTH_TOKEN 和 ANTHROPIC_BASE_URL")
        return

    # 初始化系统
    spawner = await initialize_system(llm_provider)

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
