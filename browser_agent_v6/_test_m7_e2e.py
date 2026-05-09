"""M7 端到端 — Lead 调度 Worker 完成简单任务。

验证:
1. Lead 拿到任务后正确 spawn_agent(agent_type='worker', ...)
2. Worker spawn 后正确执行子任务并返回 final_text
3. Lead 接收到 worker result 后整合输出最终答案
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

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
    print("  M7 端到端 — Lead 调度 Worker")
    print("="*60)

    print("\n[1] 启动 daemon + pool + LLM + spawner")
    daemon = get_daemon(port=BROWSER_PORT); daemon.start()
    set_active_port(BROWSER_PORT)
    pool = WarmPool(
        size=2,
        blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    ); pool.start()
    config = ModelConfig.load_from_file("config.json")
    llm = LLMFactory.create_provider(config)
    work_root = Path(tempfile.mkdtemp(prefix="v6_m7_"))
    spawner = AgentSpawner(
        registry=build_default_registry(),
        llm=llm,
        worktrees_root=str(work_root),
        shared_dir_root=str(work_root),
        pool=pool,
    )
    spawner.register_builtin()
    print(f"    LLM={config.model_id}, worktrees={work_root}")

    # Lead 任务 — 故意简单,3-5 turn 应能完成
    print("\n[2] spawn lead — 让它派 worker 抓 example.com 标题")
    print("="*60)
    result = await spawner.spawn(
        agent_type="lead",
        task=(
            "Use spawn_agent to dispatch a worker. The worker should: "
            "(1) navigate to https://example.com, "
            "(2) extract the page's <h1> heading text, "
            "(3) report the heading. "
            "After the worker returns, summarize what it found in your final reply."
        ),
        max_turns=8,
    )

    print("\n" + "="*60)
    print(f"  RESULT")
    print("="*60)
    print(f"success      : {result['success']}")
    print(f"turns_used   : {result['turns_used']}")
    print(f"token_usage  : {result['token_usage']}")
    print(f"stop_reason  : {result['stop_reason']}")
    print(f"final_text   :\n{'-'*40}\n{result['final_text']}\n{'-'*40}")

    # 校验:lead 一定要调过 spawn_agent;final_text 应包含 Example Domain
    spawn_called = any(
        ev.get("type") == "tool_use" and ev.get("name") == "spawn_agent"
        for ev in result["events"]
    )
    print(f"\nspawn_agent 被 lead 调用: {spawn_called}")
    assert spawn_called, "lead 没调 spawn_agent — 行为不正确"
    assert "Example" in result["final_text"], "final_text 应包含 worker 抓到的 'Example Domain'"

    print("\n✅ M7 Lead → Worker e2e 通过")
    pool.shutdown()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    asyncio.run(main())
