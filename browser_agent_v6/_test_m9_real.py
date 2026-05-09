"""M9 真实业务测试 — 抓 theresanaiforthat.com 前 10 个 trending AI 工具详情。

对比 v5 task_1777979388:
- v5: lead 调 9 次 spawn,worker 2 个跑了 99/163 turns,只成功 10 个详情(其中 reviews/qa 空)
- v6 期望: lead 1-2 次 spawn,worker 一段 run_browser_python 批量抓完,总 turn < 30

跑完报告:
- 总耗时 / 总 token / 总 turn 数
- spawn_agent 调用次数
- 实际抓到的产品数(读 shared/ai_tools_top10.json)
- 字段填充率
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ.pop("SSL_CERT_FILE", None)
sys.path.insert(0, str(Path(__file__).parent))


TASK = """\
请帮我抓取 https://theresanaiforthat.com/trending 这个页面上前 10 个 trending AI 工具的详细信息。

为每个工具收集这些字段:
- rank (排名,1-10)
- name (产品名)
- slug (URL slug)
- url (产品详情页完整 URL,格式 https://theresanaiforthat.com/ai/{slug}/)
- views (访问量,从详情页 .stats_opens 元素的文本)
- rating (评分,从详情页 .rating_top 元素提取数字)
- pros (优点列表,从详情页 .pac-info-item-pros .pac-elem 全部提取)
- cons (缺点列表,从详情页 .pac-info-item-cons .pac-elem 全部提取)

最终把所有 10 个工具的数据保存为 JSON 数组到 shared/ai_tools_top10.json。

完成后简短报告:成功几个、失败几个、views 和 rating 字段的填充率、前 1-2 条样本。
"""


async def main():
    from core.llm_provider import LLMFactory, ModelConfig
    from core.agent_spawner import AgentSpawner
    from tools import build_default_registry
    from sandbox.pool import WarmPool
    from browser.daemon import get_daemon
    from browser.helpers import set_active_port

    BROWSER_PORT = 9222

    print("="*70)
    print("  M9 真实业务测试 — TAAFT Trending Top 10")
    print("="*70)

    print("\n[setup] 启动基础设施...")
    daemon = get_daemon(port=BROWSER_PORT); daemon.start()
    set_active_port(BROWSER_PORT)
    pool = WarmPool(
        size=2,
        blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    ); pool.start()
    config = ModelConfig.load_from_file("config.json")
    llm = LLMFactory.create_provider(config)
    work_root = Path(__file__).parent / "worktrees"
    spawner = AgentSpawner(
        registry=build_default_registry(),
        llm=llm,
        worktrees_root=str(work_root),
        shared_dir_root=str(work_root),
        pool=pool,
    )
    spawner.register_builtin()
    print(f"[setup] LLM={config.model_id}")

    print("\n" + "="*70)
    print("  📋 TASK 开始")
    print("="*70)
    print(TASK)
    print("="*70 + "\n")

    started = time.time()
    result = await spawner.spawn(
        agent_type="lead",
        task=TASK,
        max_turns=15,
    )
    duration = time.time() - started

    # ──── 分析事件流 ────
    spawn_calls = [ev for ev in result["events"] if ev.get("type") == "tool_use" and ev.get("name") in ("spawn_agent", "spawn_agents_parallel")]
    n_spawns = len(spawn_calls)

    # 子 agent 数量(lead events 包含 tool_result,但子 agent 内部 events 在 spawner 自己的 events 里,不容易聚合;
    # 这里只能看 lead 的 turn 计数,子 agent 计数通过 final_text 中的报告或 worktrees 目录推断)
    session_id = result["session_id"]
    shared_dir = Path(result["shared_dir"])
    products_file = shared_dir / "ai_tools_top10.json"

    # ──── 读取实际数据 ────
    products = None
    if products_file.exists():
        try:
            products = json.loads(products_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 读 shared/ai_tools_top10.json 失败: {e}")

    print("\n" + "="*70)
    print("  📊 M9 RESULT")
    print("="*70)
    print(f"耗时           : {duration:.1f}s")
    print(f"lead success   : {result['success']}")
    print(f"lead turns     : {result['turns_used']}")
    print(f"lead tokens    : {result['token_usage']}")
    print(f"stop_reason    : {result['stop_reason']}")
    print(f"spawn 调用次数 : {n_spawns}")
    print(f"shared 目录    : {shared_dir}")
    print(f"目标文件存在   : {products_file.exists()}")

    if isinstance(products, list):
        n = len(products)
        # 字段填充率
        fields = ["rank", "name", "slug", "url", "views", "rating", "pros", "cons"]
        fill = {f: sum(1 for p in products if p.get(f)) for f in fields}
        print(f"\n抓到产品数     : {n}")
        print(f"字段填充率     :")
        for f in fields:
            pct = fill[f] / n * 100 if n else 0
            print(f"  {f:8s}: {fill[f]}/{n}  ({pct:.0f}%)")
        if products:
            print(f"\n样本(第 1 个):")
            sample = products[0]
            print(f"  name={sample.get('name')}, slug={sample.get('slug')}")
            print(f"  views={sample.get('views')}, rating={sample.get('rating')}")
            print(f"  pros count={len(sample.get('pros', []) or [])}, cons count={len(sample.get('cons', []) or [])}")

    print(f"\n[final_text — lead 给用户的最终汇报]:\n{'-'*70}")
    print(result["final_text"])
    print("-"*70)

    # ── v5 对比 ──
    print("\n" + "="*70)
    print("  v5 vs v6 对比(v5 数据来自 task_1777979388 worktree)")
    print("="*70)
    print(f"  {'metric':25s} | {'v5 (50 task)':20s} | v6 (10 task)")
    print(f"  {'-'*25} | {'-'*20} | {'-'*20}")
    print(f"  {'lead turns':25s} | {'~17':20s} | {result['turns_used']}")
    print(f"  {'spawn 调用次数':23s} | {'9':20s} | {n_spawns}")
    print(f"  {'worker 单次最多 turn':21s} | {'163':20s} | (子 agent 内部,见 stdout)")
    print(f"  {'成功完成详情数':23s} | {'10/50 (20%)':20s} | {n if isinstance(products, list) else 'N/A'}/10")
    print(f"  {'verification 死掉':23s} | {'是 (LLM 配额)':20s} | (本次未派 verification)")

    pool.shutdown()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    asyncio.run(main())
