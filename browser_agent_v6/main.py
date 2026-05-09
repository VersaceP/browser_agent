"""v6 入口 — 启动 daemon / pool / spawner,提供交互 shell 或单次任务模式。

跑法:
    python main.py                      # 交互模式
    python main.py --task "..."         # 单次任务
    python main.py --demo               # 内置演示
    python main.py --config myconfig.json
    python main.py --browser-port 9333
    python main.py --provider openai --model glm-4.7

交互命令:
    /save <path>      保存当前 lead session
    /load <path>      恢复 session
    /sessions [dir]   列出已保存 session
    /reset            清空当前会话(含 plan 注册)
    /stats            打印 pool / token 统计
    /quit, quit       退出
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# ── Windows utf-8 + 修 conda env 的 SSL_CERT_FILE 错配 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# conda env 的 SSL_CERT_FILE 经常指向不存在的路径,httpx 启动会爆
ssl_cert = os.environ.get("SSL_CERT_FILE", "")
if ssl_cert and not os.path.exists(ssl_cert):
    print(f"[main] 检测到 SSL_CERT_FILE={ssl_cert} 不存在,unset 以防 httpx 报错")
    os.environ.pop("SSL_CERT_FILE", None)

sys.path.insert(0, str(Path(__file__).parent))

from core.context import TeammateContext
from core.llm_provider import LLMFactory, ModelConfig
from core.agent_spawner import AgentSpawner
from core.session_persistence import save_session, load_session, list_sessions, SessionPersistenceError
from tools import build_default_registry
from sandbox.pool import WarmPool
from browser.daemon import get_daemon
from browser.helpers import set_active_port
from browser.skills import auto_load as auto_load_skills


# ──────────────────────────────────
# 全局状态(模块级 — 进程内单例)
# ──────────────────────────────────

DAEMON = None
POOL = None
SPAWNER = None
ACTIVE_LEAD_CONTEXT = None  # 活跃的 lead session,用于多轮 chat


# ──────────────────────────────────
# 启动 / 清理
# ──────────────────────────────────

async def init_system(
    config_path: str,
    browser_port: int,
    provider_override: str = "",
    model_override: str = "",
    force_single_step_browser: bool = False,
    require_plan_approval: bool = False,
    require_first_iteration_approval: bool = False,
) -> None:
    """初始化全部基础设施"""
    global DAEMON, POOL, SPAWNER

    print("="*60)
    print("  Browser Agent v6 — 启动中")
    print("="*60)

    # 1. config + LLM
    print("\n[1/4] 加载配置 + LLM provider")
    config = ModelConfig.load_from_file(config_path)
    if provider_override:
        config.provider = provider_override
    if model_override:
        config.model_id = model_override

    sandbox_cfg = {}
    browser_cfg = {}
    agent_cfg = {}
    summarizer_cfg = None
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            full = json.load(f)
            sandbox_cfg = full.get("sandbox", {})
            browser_cfg = full.get("browser", {})
            agent_cfg = full.get("agent", {})
            summarizer_cfg = full.get("summarizer")

    # config 里的 force_single_step_browser 优先级低于 CLI 覆盖
    if not force_single_step_browser and agent_cfg.get("force_single_step_browser"):
        force_single_step_browser = True
    if not require_plan_approval and agent_cfg.get("require_plan_approval"):
        require_plan_approval = True
    if not require_first_iteration_approval and agent_cfg.get("require_first_iteration_approval"):
        require_first_iteration_approval = True

    if browser_cfg.get("remote_debugging_port"):
        browser_port = int(browser_cfg["remote_debugging_port"])

    try:
        llm = LLMFactory.create_provider(config)
        print(f"  ✓ LLM: {config.provider} / {config.model_id}")
    except Exception as e:
        print(f"  ❌ LLM 创建失败: {e}")
        raise

    # 1.4. 低成本 summarizer(给 read_file 大文件摘要用)
    summarizer = None
    if summarizer_cfg:
        try:
            sum_cfg = ModelConfig(
                provider=summarizer_cfg.get("provider", "openai"),
                model_id=summarizer_cfg.get("model_id", ""),
                api_key=summarizer_cfg.get("api_key") or ModelConfig._env(summarizer_cfg.get("api_key_env")),
                base_url=summarizer_cfg.get("base_url"),
                extra_params=summarizer_cfg.get("extra_params", {}),
            )
            summarizer = LLMFactory.create_provider(sum_cfg)
            print(f"  ✓ summarizer: {sum_cfg.provider} / {sum_cfg.model_id}")
        except Exception as e:
            print(f"  ⚠ summarizer 创建失败,大文件摘要功能停用: {e}")
            summarizer = None
    else:
        print("  (no summarizer config — 大文件 read_file 不可摘要,只能渐进读)")

    # 1.5. skill registry — 加载 skills/browser/*.md
    skills_result = auto_load_skills()
    if skills_result.get("loaded", 0) > 0:
        print(f"  ✓ skills: 加载 {skills_result['loaded']} 个站点策略")
    if skills_result.get("failed", 0) > 0:
        print(f"  ⚠ skills: {skills_result['failed']} 个解析失败 — {skills_result.get('errors')}")

    # 2. browser daemon
    print(f"\n[2/4] 启动 browser daemon @ :{browser_port}")
    DAEMON = get_daemon(
        port=browser_port,
        user_data_dir=browser_cfg.get("user_data_dir"),
        binary_path=browser_cfg.get("binary_path"),
        headless=browser_cfg.get("headless", False),
    )
    DAEMON.start()
    set_active_port(browser_port)

    # Standardize the Chrome window so vision-based tools (vision_page_skeleton,
    # analyze_screenshot) get screenshots of consistent, predictable size.
    # Skipped silently if it fails — non-fatal for headless / unusual environments.
    try:
        from browser.helpers import set_window_size
        win_geom = set_window_size(1440, 1740)
        print(f"  ✓ window standardized: outer 1440x1740 → inner {win_geom['innerWidth']}x{win_geom['innerHeight']}")
    except Exception as e:
        print(f"  ⚠ set_window_size failed (non-fatal): {type(e).__name__}: {e}")

    # 3. warm subprocess pool
    print("\n[3/4] 启动 sandbox warm pool")
    POOL = WarmPool(
        size=int(sandbox_cfg.get("warm_pool_size", 3)),
        blocked_imports=sandbox_cfg.get("blocked_imports", ["requests", "httpx", "aiohttp", "urllib"]),
        env_passthrough=sandbox_cfg.get("allowed_env_passthrough",
            ("PATH", "PYTHONPATH", "HOME", "USERPROFILE", "TEMP", "TMP",
             "LANG", "LC_ALL", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA")),
        env_prefix=sandbox_cfg.get("allowed_env_prefix", ("LARK_", "FEISHU_")),
        browser_port=browser_port,
    )
    POOL.start()
    print(f"  ✓ pool stats: {POOL.stats}")

    # 4. spawner
    print("\n[4/4] 注册 agents + spawner")
    worktrees_root = Path(__file__).parent / "worktrees"
    SPAWNER = AgentSpawner(
        registry=build_default_registry(),
        llm=llm,
        worktrees_root=str(worktrees_root),
        shared_dir_root=str(worktrees_root),
        pool=POOL,
        daemon=DAEMON,
        summarizer=summarizer,
        require_plan_approval=require_plan_approval,
        require_first_iteration_approval=require_first_iteration_approval,
    )
    SPAWNER.register_builtin(force_single_step_browser=force_single_step_browser)
    print(f"  ✓ agents: {SPAWNER.list_agents()}")
    print(f"  ✓ tools: {len(SPAWNER.registry.names())}")
    if force_single_step_browser:
        print("  ⚠ FORCE_SINGLE_STEP_BROWSER = True → worker 不能用 run_browser_python")
    if require_plan_approval:
        print("  ⚠ REQUIRE_PLAN_APPROVAL = True → submit_plan 触发终端 y/n 审批")
    if require_first_iteration_approval:
        print("  ⚠ REQUIRE_FIRST_ITERATION_APPROVAL = True → batch_browser_actions iter 1 强制审核")

    print("\n" + "="*60)
    print("  ✅ 系统就绪")
    print("="*60)


def cleanup_system() -> None:
    """优雅关闭"""
    global POOL, DAEMON
    print("\n[cleanup] 关闭 pool...")
    if POOL:
        POOL.shutdown()
    print("[cleanup] 关闭 daemon...")
    if DAEMON:
        DAEMON.stop()
    print("[cleanup] ✅ 完成")


# ──────────────────────────────────
# 模式 1:单次任务
# ──────────────────────────────────

async def run_one_task(task: str, agent_type: str = "lead", max_turns: int = 0) -> dict:
    print(f"\n📋 任务: {task[:120]}{'...' if len(task) > 120 else ''}\n")
    result = await SPAWNER.spawn(
        agent_type=agent_type,
        task=task,
        max_turns=max_turns if max_turns > 0 else None,
    )
    print("\n" + "="*60)
    print("  📊 执行结果")
    print("="*60)
    print(f"success     : {result['success']}")
    print(f"turns_used  : {result['turns_used']}")
    print(f"token_usage : {result['token_usage']}")
    print(f"stop_reason : {result['stop_reason']}")
    print(f"\nfinal_text:\n{'-'*60}")
    print(result["final_text"])
    print('-'*60)
    return result


# ──────────────────────────────────
# 模式 2:演示任务
# ──────────────────────────────────

DEMO_TASK = """\
Use spawn_agent to dispatch one worker. The worker should use run_browser_python
to open https://example.com, extract the page title and the first paragraph
text, save them to shared/example_summary.json with keys 'title' and 'first_paragraph',
then return a one-line summary of what was saved. After the worker returns,
report the outcome to the user.
"""


async def run_demo() -> None:
    print("\n" + "="*60)
    print("  🎬 演示:Lead → Worker → 融合代码模式")
    print("="*60)
    await run_one_task(DEMO_TASK, agent_type="lead", max_turns=10)


# ──────────────────────────────────
# 模式 3:交互 shell
# ──────────────────────────────────

async def run_interactive() -> None:
    global ACTIVE_LEAD_CONTEXT

    print("\n" + "="*60)
    print("  💬 交互模式 — 输入任务,Lead 拆解后调度 worker")
    print("  命令: /save <path> | /load <path> | /sessions [dir] | /reset | /stats | /quit")
    print("="*60)

    while True:
        try:
            print()
            raw = input("📋 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 退出")
            break

        if not raw:
            continue

        # ── 内置命令 ──
        if raw.lower() in ("/quit", "quit", "exit", "/exit"):
            print("👋 再见")
            break

        if raw.lower() == "/reset":
            if ACTIVE_LEAD_CONTEXT:
                SPAWNER.clear_session(ACTIVE_LEAD_CONTEXT.session_id)
            ACTIVE_LEAD_CONTEXT = None
            SPAWNER.fatal_tracker.reset()
            print("🧹 当前会话已重置(含 plan / progress / 审批 / 熔断器记录)")
            continue

        if raw.lower() == "/stats":
            print(f"  pool         : {POOL.stats}")
            print(f"  daemon       : alive={DAEMON.is_alive()} port={DAEMON.port}")
            tr = SPAWNER.fatal_tracker.status()
            mark = "⛔ TRIPPED" if tr["tripped"] else "ok"
            print(f"  fatal_tracker: {mark} {tr['fatal_count_in_window']}/{tr['max_fatal']} in last {tr['cool_down_seconds']}s")
            for e in tr.get("recent_errors", []):
                print(f"    -{e['seconds_ago']}s: {e['reason']}")
            if ACTIVE_LEAD_CONTEXT:
                print(f"  lead session : {ACTIVE_LEAD_CONTEXT.session_id}")
                print(f"  lead tokens  : {ACTIVE_LEAD_CONTEXT.token_usage}/{ACTIVE_LEAD_CONTEXT.max_tokens}")
                print(f"  lead msgs    : {len(ACTIVE_LEAD_CONTEXT.messages)}")
                cs = ACTIVE_LEAD_CONTEXT.cache_summary()
                print(f"  llm calls    : {cs['llm_calls']}  output={cs['output']} tokens")
                print(f"  cache stats  : read={cs['cache_read']} create={cs['cache_creation']} "
                      f"uncached={cs['uncached_input']} (in={cs['total_input']}) "
                      f"hit_rate={cs['cache_hit_rate']:.1%}")
                st = SPAWNER.session_states.get(ACTIVE_LEAD_CONTEXT.session_id)
                if st:
                    s = st.status()
                    print(f"  plan         : approved={s['plan_approved']} contracts={len(s['contracts'])} verif_required={s['has_verification_step']}")
                    if s.get("progress"):
                        for g in s["progress"]["goals"]:
                            print(f"    [{g['status']:>11}] {g['id']}: {g['current']}/{g['target']}  {g['description']}")
            else:
                print("  lead session : <none>")
            continue

        if raw.lower().startswith("/save"):
            parts = raw.split(maxsplit=1)
            if len(parts) < 2:
                print("用法: /save <文件路径或目录>")
                continue
            if not ACTIVE_LEAD_CONTEXT:
                print("⚠️ 当前没有活跃 session,先跑一个任务")
                continue
            try:
                meta = save_session(ACTIVE_LEAD_CONTEXT, parts[1].strip())
                print(f"💾 saved → {meta['file_path']} ({meta['size_bytes']} bytes, {meta['message_count']} msgs)")
            except SessionPersistenceError as e:
                print(f"❌ save failed: {e}")
            continue

        if raw.lower().startswith("/load"):
            parts = raw.split(maxsplit=1)
            if len(parts) < 2:
                print("用法: /load <文件路径或目录>")
                continue
            try:
                ctx, meta = load_session(parts[1].strip())
                ACTIVE_LEAD_CONTEXT = ctx
                print(f"📂 loaded session={ctx.session_id} ({meta['message_count']} msgs)")
            except SessionPersistenceError as e:
                print(f"❌ load failed: {e}")
            continue

        if raw.lower().startswith("/sessions"):
            parts = raw.split(maxsplit=1)
            base = parts[1].strip() if len(parts) > 1 else "."
            sessions = list_sessions(os.path.abspath(base))
            if not sessions:
                print(f"📂 {base}: 无 session 文件")
            else:
                for s in sessions:
                    print(f"  [{s['saved_at_str']}] {s['session_id']} ({s['message_count']} msgs)")
                    print(f"    {s['task_preview']}")
                    print(f"    {s['path']}")
            continue

        # ── 任务执行 ──
        try:
            if ACTIVE_LEAD_CONTEXT:
                # 多轮 — 用 chat 接续
                result = await SPAWNER.chat(ACTIVE_LEAD_CONTEXT, raw)
            else:
                result = await SPAWNER.spawn(agent_type="lead", task=raw)
                ACTIVE_LEAD_CONTEXT = result["context"]

            print("\n" + "="*60)
            print(f"  📊 turns={result['turns_used']} tokens={result['token_usage']} stop={result['stop_reason']}")
            print("="*60)
            print(result["final_text"])
        except Exception as e:
            print(f"\n❌ 执行出错: {e}")
            import traceback
            traceback.print_exc()


# ──────────────────────────────────
# 入口
# ──────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Browser Agent v6")
    parser.add_argument("--task", help="单次任务模式 — 跑一次直接退出")
    parser.add_argument("--demo", action="store_true", help="内置演示")
    parser.add_argument("--config", default="config.json", help="LLM 配置文件路径")
    parser.add_argument("--browser-port", type=int, default=9222, help="Chrome remote debugging 端口")
    parser.add_argument("--provider", default="", help="覆盖 config 的 provider")
    parser.add_argument("--model", default="", help="覆盖 config 的 model_id")
    parser.add_argument("--max-turns", type=int, default=0, help="单次任务的 max_turns")
    parser.add_argument(
        "--force-single-step",
        action="store_true",
        help="强制 worker 只用单步浏览器工具(navigate/click/...),禁用 run_browser_python",
    )
    parser.add_argument(
        "--require-plan-approval",
        action="store_true",
        help="启用计划审批门:submit_plan 触发终端 y/n 提示,审批通过后才能 spawn",
    )
    parser.add_argument(
        "--require-first-iteration-approval",
        action="store_true",
        help="启用 batch_browser_actions iter 1 强制审核:即使 LLM 没传 require_first_approval 也会终端 y/n",
    )
    args = parser.parse_args()

    config_path = str(Path(__file__).parent / args.config) if not Path(args.config).is_absolute() else args.config

    try:
        await init_system(
            config_path=config_path,
            browser_port=args.browser_port,
            provider_override=args.provider,
            model_override=args.model,
            force_single_step_browser=args.force_single_step,
            require_plan_approval=args.require_plan_approval,
            require_first_iteration_approval=args.require_first_iteration_approval,
        )

        if args.demo:
            await run_demo()
        elif args.task:
            await run_one_task(args.task, max_turns=args.max_turns)
        else:
            await run_interactive()
    finally:
        cleanup_system()


if __name__ == "__main__":
    asyncio.run(main())
