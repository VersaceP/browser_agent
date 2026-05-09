"""M5 联合测试 — 模拟一次完整的 LLM tool 调用流程,验证 4 层协作。

不依赖 LLM,直接构造 tool_input 来调用 dispatch。
"""
import asyncio, sys, tempfile, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools import build_default_registry, dispatch, format_result_for_llm, ToolContext
from sandbox.pool import WarmPool
from browser.daemon import get_daemon
from browser.helpers import set_active_port

BROWSER_PORT = 9222


async def main():
    print("="*60)
    print("  M5 端到端联合测试")
    print("="*60)

    # 0. 启动 browser daemon(主进程负责 Chrome 生命周期)
    daemon = get_daemon(port=BROWSER_PORT)
    daemon.start()
    set_active_port(BROWSER_PORT)  # 主进程的 helpers 也走这个端口
    print(f"\n[0] Browser daemon started @ :{BROWSER_PORT}")

    # 1. 准备 worktree + shared
    work_root = Path(tempfile.mkdtemp(prefix="v6_e2e_"))
    worktree = work_root / "worker_1"
    worktree.mkdir(parents=True)
    shared = work_root / "shared"
    shared.mkdir(parents=True)
    print(f"\n[1] worktree={worktree}")
    print(f"    shared  ={shared}")

    # 2. 启动 warm pool — 把 browser_port 注入 worker
    pool = WarmPool(
        size=2,
        blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    )
    pool.start()
    print(f"\n[2] WarmPool started, stats={pool.stats}")

    # 3. 构造 ToolContext
    ctx = ToolContext(
        worktree=str(worktree),
        shared_dir=str(shared),
        session_id="e2e_test",
        agent_type="worker",
        pool=pool,
    )

    # 4. 构造 ToolRegistry
    registry = build_default_registry()
    print(f"\n[3] Registered tools: {registry.names()}")

    # 5. Test cases
    print("\n" + "="*60)
    print("  TEST CASE 流")
    print("="*60)

    # A. navigate(单步 tool)
    print("\n[A] navigate → example.com")
    r = await dispatch(registry, "navigate", {"url": "https://example.com"}, ctx)
    print(f"   {format_result_for_llm(r)[:200]}")
    assert r["status"] == "ok"

    # B. extract_text(单步 tool)
    print("\n[B] extract_text(selector='h1')")
    r = await dispatch(registry, "extract_text", {"selector": "h1"}, ctx)
    print(f"   {format_result_for_llm(r)}")
    assert r["status"] == "ok" and "Example" in r["result"]

    # C. screenshot(单步 tool,落到 worktree)
    print("\n[C] screenshot(filename='test.png')")
    r = await dispatch(registry, "screenshot", {"filename": "test.png"}, ctx)
    print(f"   {format_result_for_llm(r)}")
    assert r["status"] == "ok"
    assert Path(r["result"]["path"]).exists()

    # D. write_file
    print("\n[D] write_file(path='note.txt', content='hello v6')")
    r = await dispatch(registry, "write_file", {"path": "note.txt", "content": "hello v6"}, ctx)
    print(f"   {format_result_for_llm(r)}")

    # E. list_files
    print("\n[E] list_files(recursive=true)")
    r = await dispatch(registry, "list_files", {"recursive": True}, ctx)
    print(f"   {format_result_for_llm(r)[:300]}")

    # F. read_file
    print("\n[F] read_file(path='note.txt')")
    r = await dispatch(registry, "read_file", {"path": "note.txt"}, ctx)
    print(f"   {format_result_for_llm(r)[:200]}")
    assert r["result"]["content"] == "hello v6"

    # G. publish_artifact (content)
    print("\n[G] publish_artifact(name='deliv.json', content={...})")
    r = await dispatch(registry, "publish_artifact", {
        "name": "deliv.json",
        "content": [{"slug": "a"}, {"slug": "b"}],
        "description": "sample list",
    }, ctx)
    print(f"   {format_result_for_llm(r)}")
    assert (shared / "deliv.json").exists()
    assert json.loads((shared / "deliv.json").read_text()) == [{"slug": "a"}, {"slug": "b"}]

    # H. run_browser_python — 融合方案的核心:LLM 写一段代码 + helpers 调浏览器 + save_artifact
    print("\n[H] run_browser_python — 融合代码模式(navigate + js + save)")
    code = '''
goto("https://example.com")
title = js("return document.title")
para = js("return document.querySelector('p')?.textContent.trim()")
data = {"title": title, "first_p_chars": len(para)}
print(f"page title: {title}")
print(f"first paragraph: {para[:60]}...")
saved = save_artifact("from_code.json", data)
print(f"saved → {saved}")
'''
    r = await dispatch(registry, "run_browser_python", {"code": code, "timeout": 30}, ctx)
    if r["status"] == "ok" and r["result"]["status"] == "ok":
        print(f"   ✅ ok, duration={r['result']['duration_s']}s")
        print(f"   stdout:\n{r['result']['stdout']}")
    else:
        print(f"   ❌ {format_result_for_llm(r)}")

    # I. 验证沙箱安全:requests 被拦截
    print("\n[I] run_browser_python — import requests(应被拦)")
    r = await dispatch(registry, "run_browser_python",
                  {"code": "import requests; requests.get('https://x.com')"}, ctx)
    inner = r["result"]
    assert inner["status"] == "error" and inner["exception"] == "BlockedImportError"
    print(f"   ✅ BlockedImportError 触发: {inner['message'][:100]}")

    # J. 验证沙箱安全:路径穿越被拦
    print("\n[J] run_browser_python — save_artifact 写到沙箱外(应被拦)")
    r = await dispatch(registry, "run_browser_python",
                  {"code": "save_artifact('../../escape.txt', 'pwn')"}, ctx)
    inner = r["result"]
    assert inner["status"] == "error" and inner["exception"] == "PathEscapeError"
    print(f"   ✅ PathEscapeError 触发")

    # K. tool not found 优雅返回
    print("\n[K] dispatch 不存在的 tool")
    r = await dispatch(registry, "no_such_tool", {}, ctx)
    print(f"   {format_result_for_llm(r)[:120]}")
    assert r["status"] == "error" and r["exception"] == "ToolNotFound"

    # 收尾
    print("\n" + "="*60)
    print("  ✅ M5 端到端联合测试全部通过")
    print("="*60)
    pool.shutdown()


if __name__ == "__main__":
    # Windows utf-8
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    asyncio.run(main())
