"""M6 端到端真实测试 — 调真实 LLM,验证 spawn / execute_loop / tool dispatch 全链路。

任务尽量简单(节约 token):
  worker: 'navigate to https://example.com and tell me its title'
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 修复 conda env 的 SSL_CERT_FILE 指向不存在的问题
os.environ.pop("SSL_CERT_FILE", None)

sys.path.insert(0, str(Path(__file__).parent))


async def main():
    from core.llm_provider import LLMFactory, ModelConfig
    from core.agent_spawner import AgentSpawner
    from tools import build_default_registry
    from sandbox.pool import WarmPool
    from browser.daemon import get_daemon
    from browser.helpers import set_active_port

    BROWSER_PORT = 9222

    print("="*60)
    print("  M6 端到端真实测试(真调 LLM)")
    print("="*60)

    # 1. browser daemon
    print("\n[1] 启动 browser daemon...")
    daemon = get_daemon(port=BROWSER_PORT)
    daemon.start()
    set_active_port(BROWSER_PORT)

    # 2. warm pool
    print("\n[2] 启动 warm pool...")
    pool = WarmPool(
        size=2,
        blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    )
    pool.start()

    # 3. LLM provider
    print("\n[3] 加载 LLM provider...")
    config = ModelConfig.load_from_file("config.json")
    llm = LLMFactory.create_provider(config)
    print(f"    {config.provider} / {config.model_id}")

    # 4. spawner
    print("\n[4] 启动 spawner...")
    work_root = Path(tempfile.mkdtemp(prefix="v6_m6_"))
    registry = build_default_registry()
    spawner = AgentSpawner(
        registry=registry,
        llm=llm,
        worktrees_root=str(work_root),
        shared_dir_root=str(work_root),
        pool=pool,
    )
    spawner.register_builtin()
    print(f"    worktrees={work_root}")

    # 5. spawn worker — 最简单任务
    print("\n[5] spawn worker — 'navigate + extract title'")
    print("="*60)
    result = await spawner.spawn(
        agent_type="worker",
        task="Use the navigate tool to open https://example.com, then use extract_text to get the page's <h1> heading. Report the heading text in your final reply.",
        max_turns=10,
    )

    print("\n" + "="*60)
    print(f"  RESULT")
    print("="*60)
    print(f"success      : {result['success']}")
    print(f"agent_type   : {result['agent_type']}")
    print(f"turns_used   : {result['turns_used']}")
    print(f"token_usage  : {result['token_usage']}")
    print(f"stop_reason  : {result['stop_reason']}")
    print(f"fatal_error  : {result['fatal_error']}")
    print(f"final_text   :\n{'-'*40}\n{result['final_text']}\n{'-'*40}")

    # 验证 stop_reason 健康
    assert result["success"] or "Example" in result["final_text"], \
        f"expected success or 'Example' in final_text"

    print("\n✅ M6 真实 LLM 端到端测试通过")
    pool.shutdown()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    asyncio.run(main())
