"""V4 系统完整验证脚本 — 不依赖 LLM API Key"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def run_all_tests():
    results = []
    
    # ── 测试 1: 基础设施层导入 ──
    print("=" * 60)
    print("  测试 1: 基础设施层导入")
    print("=" * 60)
    try:
        from core.agent_definition import AgentDefinition, TrustLevel, build_builtin_agents
        from core.teammate_context import TeammateContext
        from core.worktree import WorkTreeManager
        from core.hook_registry import HookRegistry, HookEvent, HookAction, HookResult
        from core.context_compactor import ContextCompactor
        from core.prompt_builder import build_system_prompt, build_dynamic_context
        from core.resource_manager import ResourceManager
        from core.execution_loop import execute_turn
        from core.agent_spawner import AgentSpawner
        from core.llm_provider import ModelConfig, LLMFactory
        
        agents = build_builtin_agents()
        print(f"  ✅ 4 个内建 Agent: {list(agents.keys())}")
        results.append(("基础设施层导入", "✅ 通过"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("基础设施层导入", f"❌ {e}"))
        return results

    # ── 测试 2: 工具层导入与注册 ──
    print("\n" + "=" * 60)
    print("  测试 2: 工具层导入与注册")
    print("=" * 60)
    try:
        from toolkits.base_tool import BaseTool
        from toolkits.tool_registry import ToolRegistry
        from toolkits.browser_tools import get_all_browser_tools
        from toolkits.file_tools import get_all_file_tools
        from toolkits.code_tools import get_all_code_tools
        
        tr = ToolRegistry()
        tr.register_many(get_all_browser_tools())
        tr.register_many(get_all_file_tools())
        tr.register_many(get_all_code_tools())
        print(f"  ✅ 注册 {len(tr)} 个工具: {tr.list_tools()}")
        results.append(("工具层注册", "✅ 通过"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("工具层注册", f"❌ {e}"))
        return results

    # ── 测试 3: 工具权限过滤 ──
    print("\n" + "=" * 60)
    print("  测试 3: 工具权限过滤")
    print("=" * 60)
    try:
        for agent_type, agent_def in agents.items():
            tools = tr.filter_tools(agent_def)
            names = [t.name for t in tools]
            print(f"  {agent_type:15s} -> {names}")
        results.append(("工具权限过滤", "✅ 通过"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("工具权限过滤", f"❌ {e}"))

    # ── 测试 4: 安全防御 ──
    print("\n" + "=" * 60)
    print("  测试 4: 安全防御 (路径穿越 + URL 校验 + 熔断器)")
    print("=" * 60)
    try:
        from permissions.input_sanitizer import sanitize_path, sanitize_url, SanitizationError
        from permissions.denial_tracker import DenialTracker
        
        # 路径穿越
        try:
            sanitize_path("../../../../etc/passwd", "/tmp/worktree")
            print("  ❌ 路径穿越未拦截!")
            results.append(("路径穿越防御", "❌ 失败"))
        except SanitizationError:
            print("  ✅ 路径穿越攻击已拦截")
            results.append(("路径穿越防御", "✅ 通过"))
        
        # URL 校验
        blocked = 0
        for url in ["file:///etc/passwd", "javascript:alert(1)"]:
            try:
                sanitize_url(url)
            except SanitizationError:
                blocked += 1
        try:
            sanitize_url("https://example.com")
            print(f"  ✅ URL 校验: 拦截 {blocked} 个危险 URL，放行 https")
            results.append(("URL 协议校验", "✅ 通过"))
        except SanitizationError:
            print("  ❌ 误拦截合法 URL")
            results.append(("URL 协议校验", "❌ 失败"))
        
        # 熔断器
        dt = DenialTracker(max_consecutive_denials=3)
        for i in range(3):
            dt.record_denial("test", f"测试 #{i+1}")
        if dt.is_circuit_broken("test"):
            print("  ✅ 熔断器: 连续 3 次拒绝后触发熔断")
            results.append(("熔断器", "✅ 通过"))
        else:
            print("  ❌ 熔断器未触发")
            results.append(("熔断器", "❌ 失败"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("安全防御", f"❌ {e}"))

    # ── 测试 5: 上下文压缩 ──
    print("\n" + "=" * 60)
    print("  测试 5: 上下文压缩")
    print("=" * 60)
    try:
        ctx = TeammateContext(agent_type="test", task="测试", max_tokens=100)
        for i in range(10):
            ctx.append_message("user", f"测试消息 #{i} " * 50)
            ctx.append_message("assistant", [{"type": "text", "text": f"回复 #{i} " * 30}])
        
        comp = ContextCompactor(threshold_ratio=0.8, keep_recent=4)
        ratio_before = ctx.get_token_ratio()
        msg_before = len(ctx.session_messages)
        
        compressed = await comp.compact_if_needed(ctx)
        ratio_after = ctx.get_token_ratio()
        msg_after = len(ctx.session_messages)
        
        print(f"  压缩前: {msg_before} 条消息, Token 水位 {ratio_before:.1%}")
        print(f"  压缩后: {msg_after} 条消息, Token 水位 {ratio_after:.1%}")
        if compressed and msg_after < msg_before:
            print("  ✅ 上下文压缩成功")
            results.append(("上下文压缩", "✅ 通过"))
        else:
            results.append(("上下文压缩", "❌ 未触发压缩"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("上下文压缩", f"❌ {e}"))

    # ── 测试 6: Prompt 组装 ──
    print("\n" + "=" * 60)
    print("  测试 6: Prompt 组装")
    print("=" * 60)
    try:
        for agent_type, agent_def in agents.items():
            prompt = build_system_prompt(agent_def)
            print(f"  {agent_type:15s} -> {len(prompt)} 字符")
        
        dyn = build_dynamic_context("测试任务", "/tmp/wt", {"PROFILE_ID": "sp_test"})
        print(f"  动态上下文: {len(dyn)} 字符")
        results.append(("Prompt 组装", "✅ 通过"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("Prompt 组装", f"❌ {e}"))

    # ── 测试 7: Hook 事件总线 ──
    print("\n" + "=" * 60)
    print("  测试 7: Hook 事件总线")
    print("=" * 60)
    try:
        hr = HookRegistry()
        call_log = []
        
        async def test_hook(payload):
            call_log.append(payload.get("test_id"))
            return HookResult(action=HookAction.ALLOW)
        
        async def block_hook(payload):
            return HookResult(action=HookAction.BLOCK, reason="测试拦截")
        
        hr.register(HookEvent.PRE_TOOL_EXECUTE, test_hook)
        result = await hr.emit(HookEvent.PRE_TOOL_EXECUTE, {"test_id": 1})
        assert result.action == HookAction.ALLOW
        assert call_log == [1]
        print("  ✅ Hook ALLOW 正常")
        
        hr.register(HookEvent.SESSION_START, block_hook)
        result = await hr.emit(HookEvent.SESSION_START, {})
        assert result.action == HookAction.BLOCK
        print("  ✅ Hook BLOCK 正常")
        results.append(("Hook 事件总线", "✅ 通过"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("Hook 事件总线", f"❌ {e}"))

    # ── 测试 8: WorkTree 沙箱 ──
    print("\n" + "=" * 60)
    print("  测试 8: WorkTree 沙箱")
    print("=" * 60)
    try:
        wt = WorkTreeManager()
        path = wt.get_or_create_worktree("test_v4_verify")
        print(f"  创建工作区: {path}")
        
        spill_path = wt.save_spilled_data("test_v4_verify", "test_spill.txt", "溢出测试数据 " * 100)
        print(f"  溢出落盘: {spill_path}")
        
        # 路径穿越测试
        try:
            wt.resolve_path("test_v4_verify", "../../../../Windows/System32/config")
            print("  ❌ 路径穿越未拦截!")
        except PermissionError:
            print("  ✅ WorkTree 路径穿越已拦截")
        
        wt.cleanup_worktree("test_v4_verify")
        print("  ✅ 工作区已清理")
        results.append(("WorkTree 沙箱", "✅ 通过"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("WorkTree 沙箱", f"❌ {e}"))

    # ── 测试 9: DrissionPage 浏览器 ──
    print("\n" + "=" * 60)
    print("  测试 9: DrissionPage 浏览器 (高潜隐模式)")
    print("=" * 60)
    try:
        from toolkits.browser_tools import get_browser_manager, NavigateTool, ExtractTextTool
        
        nav = NavigateTool()
        result = await nav.execute(url="https://httpbin.org/html")
        print(f"  导航结果: {result[:100]}")
        
        ext = ExtractTextTool()
        result = await ext.execute(selector="")
        text_len = len(result)
        print(f"  文本提取: {text_len} 字符")
        print(f"  内容预览: {result[:150]}...")
        
        mgr = get_browser_manager()
        await mgr.close()
        print("  ✅ 浏览器测试通过，已关闭")
        results.append(("DrissionPage 浏览器", "✅ 通过"))
    except Exception as e:
        print(f"  ❌ {e}")
        results.append(("DrissionPage 浏览器", f"❌ {e}"))

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print("  📊 V4 系统验证汇总")
    print("=" * 60)
    passed = sum(1 for _, r in results if "✅" in r)
    total = len(results)
    for name, r in results:
        print(f"  {name:20s} {r}")
    print(f"\n  总计: {passed}/{total} 通过")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_all_tests())
