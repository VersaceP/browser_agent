"""task_1778062783 简化版 e2e — 抓 trending top 10 详情,验证全部优化叠加效果。

vs _test_m9_real.py:
- 任务规模 10(不是 50,省时间和 token)
- plan 中明确声明 verification 步骤,验证 verification 强制路径
- 不限制 worker 用单步 vs run_browser_python vs batch_browser_actions —— 看 LLM 怎么选
- 完整报告:cache hit rate / batch 是否被用 / skill 是否被读 / verification 是否触发

跑法:
    conda activate agent
    cd browser_agent_v6
    python _test_e2e_optimized.py
"""
import asyncio, json, os, sys, time
from pathlib import Path

os.environ.pop("SSL_CERT_FILE", None)
sys.path.insert(0, str(Path(__file__).parent))


TASK = """\
请帮我抓取 https://theresanaiforthat.com/trending 这个页面上**第 11 ~ 第 20 名** trending AI 工具的详细信息(跳过前 10 名,只要排名 11 到 20 共 10 个)。

为每个工具收集这些字段(所有详情页面字段):
- rank             排名 11-20(来自 trending 页;跳过前 10)
- name             产品名(来自 trending 页)
- slug             URL slug(用于拼详情页)
- url              详情页完整 URL,格式 https://theresanaiforthat.com/ai/{slug}/
- views            访问量数字
- rating           评分数字
- overview         产品简介/描述(详情页主区域的简介段落,150-500 字)
- pricing          定价信息对象,需包含三个子字段:
                     - summary           定价文本(免费/收费层级/价格区间)
                     - paid_options_from 付费起步价(string,如 "$10/mo";没有则 null)
                     - refund_policy     退款政策(string;没有则 null)
- pros             优点列表(string array,5-10 条)
- cons             缺点列表(string array,5-10 条)
- reviews_count    评论数(整数;若没有则填 0)
- reviews          评论内容列表(array of objects),每条评论对象包含:
                     - text     评论正文(string)
                     - rating   评论给的评分数字(number;没有则 null)
                     - author   评论作者名(string;没有则 null)
                     - date     评论日期文本(string,如 "2 weeks ago" / "2025-10-01";没有则 null)
                   最多抓前 5 条评论;如果该产品没有评论,reviews 填空数组 []。

字段如果在某产品页确实不存在或留空,填 null 或空数组,不要瞎编。

最终把 10 个工具(rank 11-20)的数据保存为 JSON 数组到 shared/ai_tools_rank_11_20.json。

请你按以下流程:
1. 先 submit_plan 声明计划(必须含一个 [verification] 步骤校验最终文件完整性 + 字段完整度)
2. spawn 一个 worker agent 执行抓取
   - worker 应优先使用 batch_browser_actions 一次跑完 10 个详情页(每 iter 多 step 抓多字段)
   - 对探查页面结构 / 处理列表用 run_browser_python
3. verification 通过后,简短汇报成功几条 / 各字段填充率 / 1 条样本(全字段展开)
"""


async def main():
    from core.llm_provider import LLMFactory, ModelConfig
    from core.agent_spawner import AgentSpawner
    from tools import build_default_registry
    from sandbox.pool import WarmPool
    from browser.daemon import get_daemon
    from browser.helpers import set_active_port
    from browser.skills import auto_load as auto_load_skills

    BROWSER_PORT = 9222

    print("="*70)
    print("  e2e 优化效果测试 — TAAFT Trending Top 10")
    print("="*70)

    print("\n[setup] 启动基础设施...")
    daemon = get_daemon(port=BROWSER_PORT); daemon.start()
    set_active_port(BROWSER_PORT)
    pool = WarmPool(
        size=2,
        blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    ); pool.start()

    skills_result = auto_load_skills()
    print(f"[setup] skills loaded={skills_result.get('loaded',0)} failed={skills_result.get('failed',0)}")

    config = ModelConfig.load_from_file("config.json")
    llm = LLMFactory.create_provider(config)
    # 加载 summarizer(可选)
    summarizer = None
    try:
        with open("config.json", encoding="utf-8") as f:
            sum_section = json.load(f).get("summarizer")
        if sum_section:
            sum_cfg = ModelConfig(
                provider=sum_section.get("provider", "openai"),
                model_id=sum_section.get("model_id", ""),
                api_key=sum_section.get("api_key"),
                base_url=sum_section.get("base_url"),
                extra_params=sum_section.get("extra_params", {}),
            )
            summarizer = LLMFactory.create_provider(sum_cfg)
            print(f"[setup] summarizer = {sum_cfg.provider} / {sum_cfg.model_id}")
    except Exception as e:
        print(f"[setup] summarizer 不可用: {e}")

    work_root = Path(__file__).parent / "worktrees"
    spawner = AgentSpawner(
        registry=build_default_registry(),
        llm=llm,
        worktrees_root=str(work_root),
        shared_dir_root=str(work_root),
        pool=pool,
        daemon=daemon,
        summarizer=summarizer,
    )
    spawner.register_builtin()
    # Non-interactive run: force-disable batch_browser_actions's first-iter
    # approval (it would block on stdin → auto-reject in this script).
    spawner.force_disable_iter_approval = True
    print(f"[setup] LLM = {config.provider} / {config.model_id}")
    print(f"[setup] tools = {len(spawner.registry.names())}")

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
    events = result["events"]
    lead_ctx = result["context"]
    session_id = result["session_id"]
    shared_dir = Path(result["shared_dir"])

    spawn_calls = [ev for ev in events if ev.get("type") == "tool_use"
                                          and ev.get("name") in ("spawn_agent", "spawn_agents_parallel")]
    submit_plan_calls = [ev for ev in events if ev.get("type") == "tool_use" and ev.get("name") == "submit_plan"]
    init_progress_calls = [ev for ev in events if ev.get("type") == "tool_use" and ev.get("name") == "init_progress"]

    # 子 agent 内部事件不在 lead.events 里(它们在子 spawn 的 events 里)
    # 但我们可以从 spawner.session_states 看 contracts 状态
    st = spawner.session_states.get(session_id)

    products_file = shared_dir / "ai_tools_rank_11_20.json"
    products = None
    if products_file.exists():
        try:
            products = json.loads(products_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 读 ai_tools_rank_11_20.json 失败: {e}")

    cache = lead_ctx.cache_summary()

    print("\n" + "="*70)
    print("  📊 整体执行结果")
    print("="*70)
    print(f"耗时             : {duration:.1f}s")
    print(f"lead success     : {result['success']}")
    print(f"lead turns       : {result['turns_used']}")
    print(f"lead stop_reason : {result['stop_reason']}")
    print(f"lead 估算 tokens : {result['token_usage']}")
    print(f"")
    print(f"submit_plan 次数 : {len(submit_plan_calls)}")
    print(f"init_progress    : {len(init_progress_calls)}")
    print(f"spawn 调用次数   : {len(spawn_calls)}")
    for i, ev in enumerate(spawn_calls, 1):
        inp = ev.get("input", {})
        if ev["name"] == "spawn_agent":
            print(f"  spawn[{i}] agent_type={inp.get('agent_type')}, task={str(inp.get('task',''))[:80]}")
        else:
            tasks = inp.get("tasks", [])
            print(f"  spawn[{i}] parallel x{len(tasks)} agents")

    print(f"\n📦 contracts(SessionState):")
    if st:
        for name, c in st.contracts.items():
            print(f"  - {name:20s} agent={c.agent_type:13s} pub={c.published} verif={c.verified}")
        print(f"  has_verification_step = {st.has_verification_step}")
        if st.progress:
            for g in st.progress.goals.values():
                print(f"  progress[{g.id}]: {g.current}/{g.target} ({g.status})")
    else:
        print("  <none>")

    # cache 命中
    print(f"\n💾 prompt cache(lead 主上下文):")
    print(f"  llm_calls       : {cache['llm_calls']}")
    print(f"  cache_read      : {cache['cache_read']:>8} tokens")
    print(f"  cache_creation  : {cache['cache_creation']:>8} tokens")
    print(f"  uncached_input  : {cache['uncached_input']:>8} tokens")
    print(f"  output          : {cache['output']:>8} tokens")
    print(f"  total_input     : {cache['total_input']:>8} tokens")
    print(f"  hit_rate        : {cache['cache_hit_rate']*100:.1f}%")

    # ──── 数据完整度 ────
    print(f"\n📁 shared/ai_tools_rank_11_20.json:")
    print(f"  exists   : {products_file.exists()}")
    if isinstance(products, list):
        n = len(products)
        fields = ["rank", "name", "slug", "url", "views", "rating",
                  "overview", "pricing", "pros", "cons", "reviews_count", "reviews"]
        # pricing 是嵌套 dict,看 3 个子字段独立填充率
        pricing_subs = ["summary", "paid_options_from", "refund_policy"]
        # reviews 是 list[dict],看每个 review 对象的子字段填充率
        review_subs = ["text", "rating", "author", "date"]

        def _filled(p, f):
            v = p.get(f)
            if v in (None, "", []):
                return False
            if f == "reviews_count":
                return isinstance(v, int)
            return True

        fill = {f: sum(1 for p in products if _filled(p, f)) for f in fields}
        pricing_fill = {sf: sum(
            1 for p in products
            if isinstance(p.get("pricing"), dict) and p["pricing"].get(sf) not in (None, "")
        ) for sf in pricing_subs}

        # 统计 reviews:总条数 + 各 review 子字段填充率
        total_reviews = sum(
            len(p.get("reviews") or []) for p in products if isinstance(p.get("reviews"), list)
        )
        review_fill = {sf: 0 for sf in review_subs}
        for p in products:
            for r in (p.get("reviews") or []):
                if not isinstance(r, dict):
                    continue
                for sf in review_subs:
                    if r.get(sf) not in (None, "", []):
                        review_fill[sf] += 1

        print(f"  count    : {n}")
        print(f"  字段填充率:")
        for f in fields:
            pct = fill[f] / n * 100 if n else 0
            print(f"    {f:18s}: {fill[f]}/{n}  ({pct:.0f}%)")
        print(f"  pricing 子字段填充率:")
        for sf in pricing_subs:
            pct = pricing_fill[sf] / n * 100 if n else 0
            print(f"    pricing.{sf:14s}: {pricing_fill[sf]}/{n}  ({pct:.0f}%)")
        print(f"  reviews 总数  : {total_reviews}(across {n} 个产品)")
        if total_reviews:
            print(f"  reviews 子字段填充率(以单条 review 为分母):")
            for sf in review_subs:
                pct = review_fill[sf] / total_reviews * 100
                print(f"    review.{sf:14s}: {review_fill[sf]}/{total_reviews}  ({pct:.0f}%)")
        if products:
            print(f"\n  样本(第 1 条,全字段):")
            for k, v in (products[0] or {}).items():
                if isinstance(v, list):
                    print(f"    {k:18s}: list × {len(v)}: {v[:2]}{'...' if len(v) > 2 else ''}")
                elif isinstance(v, str) and len(v) > 120:
                    print(f"    {k:18s}: {v[:120]}... [{len(v)} chars]")
                else:
                    print(f"    {k:18s}: {v}")

    print(f"\n📝 lead final_text:")
    print("-"*70)
    print(result["final_text"][:1200])
    print("-"*70)

    # ──── v5 vs v6 对比 ────
    print(f"\n" + "="*70)
    print("  📈 v5 vs v6 对比")
    print("="*70)
    print(f"  {'metric':28s} | {'v5 task_1777979388 (50)':30s} | v6 (10, 本次)")
    print(f"  {'-'*28} | {'-'*30} | {'-'*20}")
    print(f"  {'lead turns':28s} | {'~17':30s} | {result['turns_used']}")
    print(f"  {'spawn 调用次数':25s} | {'9':30s} | {len(spawn_calls)}")
    print(f"  {'worker 单次最多 turn':24s} | {'163':30s} | (子 agent 内部)")
    print(f"  {'成功详情数':27s} | {'10/50 (20%)':30s} | {len(products) if isinstance(products, list) else 'N/A'}/10")
    print(f"  {'verification 是否跑':24s} | {'是, 但 LLM 配额死掉':30s} | "
          f"{'是' if (st and any(c.verified for c in st.contracts.values())) else '否'}")
    print(f"  {'cache hit rate':28s} | {'未统计':30s} | {cache['cache_hit_rate']*100:.1f}%")

    pool.shutdown()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    asyncio.run(main())
