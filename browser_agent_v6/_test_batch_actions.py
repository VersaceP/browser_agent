"""batch_browser_actions 全面单测 — 用 stub tools 不真起 Chrome。

覆盖:
  ① templating(string + nested dict + list 占位符替换)
  ② full success → publish_artifact 触发
  ③ start_from resume 合并 partial
  ④ 异常分类:PendingDialogError → interaction_required
                ElementNotFoundError → selector_failed
                HumanInterventionRequired → human_required
                NavigationTimeoutError → transient
  ⑤ iter 1 审批 reject(stdin 非 tty 自动 reject)
  ⑥ tool 白名单(spawn_agent 等不允许)
  ⑦ require_first_iteration_approval spawner 全局开关
"""
import sys, os, json, asyncio, io
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  batch_browser_actions — 全功能单测")
print("="*60)

from tools.base import ToolSpec, ToolContext, ToolRegistry
from tools.batch_tool import BATCH_BROWSER_ACTIONS, _render, _classify_exception
from core.session_state import SessionState

# ──────────────────────────────────
# Stub 工具 — 不依赖真 Chrome
# ──────────────────────────────────

# 全局可控的 stub 行为
_NAV_LOG = []
_NAV_FAIL_AT_URL: str = ""        # 这个 URL 触发指定异常
_NAV_FAIL_EXCEPTION: str = ""     # 异常名

def _stub_navigate_handler(tool_input, ctx):
    url = tool_input["url"]
    _NAV_LOG.append(url)
    if url == _NAV_FAIL_AT_URL:
        raise _make_exception(_NAV_FAIL_EXCEPTION, f"stub: failure at {url}")
    return {"url": url, "status": 200, "title": f"page-{url.split('/')[-2] if url.endswith('/') else url.split('/')[-1]}"}

def _stub_extract_handler(tool_input, ctx):
    selector = tool_input["selector"]
    if selector == "":
        raise _make_exception("ElementNotFoundError", "empty selector")
    return {"text": f"extracted-with-{selector}", "selector": selector}

def _stub_dismiss_dialog_handler(tool_input, ctx):
    return {"dialog_dismissed": True}

# 模拟自定义异常
class _Stub:
    pass

def _make_exception(name: str, msg: str) -> Exception:
    """运行时根据 name 造同名 Exception 子类(便于 _classify_exception 的 type(e).__name__ 检测)"""
    cls = type(name, (Exception,), {})
    return cls(msg)

# ──────────────────────────────────
# 公共 fixture
# ──────────────────────────────────

class MockSpawner:
    def __init__(self):
        self.registry = ToolRegistry()
        self.session_states = {}
        self.require_first_iteration_approval = False

def _make_registry_with_stubs() -> ToolRegistry:
    reg = ToolRegistry()
    # 注册一个 publish_artifact stub 让 publish_to 路径能跑通
    publish_log = []
    def _stub_publish_handler(tool_input, ctx):
        path = Path(ctx.shared_dir) / tool_input["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if "spill_path" in tool_input:
            src = Path(tool_input["spill_path"])
            path.write_bytes(src.read_bytes())
        publish_log.append({"name": tool_input["name"], "path": str(path)})
        return {"name": tool_input["name"], "path": str(path), "size_bytes": path.stat().st_size}
    reg._publish_log = publish_log

    reg.register(ToolSpec(name="navigate", description="stub", input_schema={"type":"object"}, handler=_stub_navigate_handler))
    reg.register(ToolSpec(name="extract_text", description="stub", input_schema={"type":"object"}, handler=_stub_extract_handler))
    reg.register(ToolSpec(name="dismiss_dialog", description="stub", input_schema={"type":"object"}, handler=_stub_dismiss_dialog_handler))
    reg.register(ToolSpec(name="publish_artifact", description="stub", input_schema={"type":"object"}, handler=_stub_publish_handler))
    reg.register(BATCH_BROWSER_ACTIONS)
    return reg

def _make_ctx(workdir: str, sid: str = "tbatch", reg=None) -> tuple:
    sp = MockSpawner()
    sp.registry = reg or _make_registry_with_stubs()
    Path(workdir).mkdir(parents=True, exist_ok=True)
    Path(workdir + "/shared").mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(
        worktree=workdir, shared_dir=workdir + "/shared",
        session_id=sid, agent_type="worker", spawner=sp,
    )
    return sp, ctx

# ──────────────────────────────────
# 1. templating
# ──────────────────────────────────

print("\n[1] templating")
out = _render("https://x.com/{slug}/", {"slug": "abc"})
assert out == "https://x.com/abc/", out
out = _render({"url": "https://x.com/{slug}/", "selector": "{tag}.{cls}"}, {"slug":"a","tag":"div","cls":"x"})
assert out == {"url":"https://x.com/a/","selector":"div.x"}, out
out = _render("{missing}", {})
assert out == "{missing}"  # 缺 key 保留字面量
out = _render([{"url":"{x}"}], {"x":"v"})
assert out == [{"url":"v"}]
print("  ✓ string / nested dict / list / missing-key 全 ok")

# ──────────────────────────────────
# 2. full success + publish_to
# ──────────────────────────────────

print("\n[2] full success + publish_to")
async def _t2():
    global _NAV_LOG, _NAV_FAIL_AT_URL
    _NAV_LOG = []; _NAV_FAIL_AT_URL = ""
    sp, ctx = _make_ctx("./_tmp_t2", "t2")
    payload = {
        "steps": [
            {"tool": "navigate", "input": {"url": "https://demo.com/ai/{slug}/"}},
            {"tool": "extract_text", "input": {"selector": ".info"}},
        ],
        "iterations": [{"slug":"a"}, {"slug":"b"}, {"slug":"c"}],
        "throttle_seconds": 0,
        "publish_to": "ai_tools.json",
    }
    result = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert result["completed"] is True, result
    assert result["succeeded_total"] == 3
    assert result["stop_reason"] == "completed"
    assert result["published_to"], "应该 publish_to shared/ai_tools.json"
    assert result["schema"], result["schema"]
    # navigate 该被叫 3 次
    assert len(_NAV_LOG) == 3
    # publish 落到 shared/
    publish_log = sp.registry._publish_log
    assert publish_log and publish_log[-1]["name"] == "ai_tools.json"
    print(f"  ✓ 3/3 succeeded, published_to={result['published_to'][-40:]}")
    print(f"    schema={result['schema']}")
asyncio.run(_t2())

# ──────────────────────────────────
# 3. PendingDialogError → interaction_required
# ──────────────────────────────────

print("\n[3] mid-flight PendingDialogError → interaction_required + start_from resume")
async def _t3():
    global _NAV_LOG, _NAV_FAIL_AT_URL, _NAV_FAIL_EXCEPTION
    _NAV_LOG = []; _NAV_FAIL_AT_URL = "https://demo.com/ai/c/"; _NAV_FAIL_EXCEPTION = "PendingDialogError"
    sp, ctx = _make_ctx("./_tmp_t3", "t3")

    payload = {
        "steps": [
            {"tool": "navigate", "input": {"url": "https://demo.com/ai/{slug}/"}},
            {"tool": "extract_text", "input": {"selector": ".info"}},
        ],
        "iterations": [{"slug":"a"}, {"slug":"b"}, {"slug":"c"}, {"slug":"d"}],
        "throttle_seconds": 0,
    }
    r1 = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r1["completed"] is False
    assert r1["failed_at"] == 2, f"应在 iter 2(slug=c) 挂掉,got {r1['failed_at']}"
    assert r1["stop_reason"] == "interaction_required", r1
    assert r1["succeeded_total"] == 2
    assert "dismiss_dialog" in r1["suggested_action"]
    assert "start_from=2" in r1["suggested_action"]
    assert r1["partial_saved_to"]
    print(f"  ✓ iter 2 stop, stop_reason={r1['stop_reason']}")
    print(f"    suggested_action={r1['suggested_action'][:100]}")

    # 模拟 LLM 调 dismiss_dialog
    # ... 然后用 start_from=2 resume(把那个 URL 标为不再失败)
    _NAV_FAIL_AT_URL = ""
    payload["start_from"] = 2
    r2 = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r2["completed"] is True
    assert r2["succeeded_total"] == 4, r2
    print(f"  ✓ resume 后 succeeded_total={r2['succeeded_total']} (前 2 自动合并)")
asyncio.run(_t3())

# ──────────────────────────────────
# 4. ElementNotFoundError → selector_failed
# ──────────────────────────────────

print("\n[4] ElementNotFoundError → selector_failed")
async def _t4():
    global _NAV_LOG, _NAV_FAIL_AT_URL
    _NAV_LOG = []; _NAV_FAIL_AT_URL = ""
    sp, ctx = _make_ctx("./_tmp_t4", "t4")
    payload = {
        "steps": [
            {"tool": "navigate", "input": {"url": "https://x.com/{slug}/"}},
            # 空 selector → stub raises ElementNotFoundError
            {"tool": "extract_text", "input": {"selector": ""}},
        ],
        "iterations": [{"slug":"a"}, {"slug":"b"}],
        "throttle_seconds": 0,
    }
    r = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r["stop_reason"] == "selector_failed", r
    assert r["failed_at"] == 0
    assert "dom_query" in r["suggested_action"] or "dom_outline" in r["suggested_action"]
    print(f"  ✓ stop_reason={r['stop_reason']}, failed_step_idx={r.get('failed_step_idx')}")
asyncio.run(_t4())

# ──────────────────────────────────
# 5. HumanInterventionRequired → human_required
# ──────────────────────────────────

print("\n[5] HumanInterventionRequired → human_required")
async def _t5():
    global _NAV_LOG, _NAV_FAIL_AT_URL, _NAV_FAIL_EXCEPTION
    _NAV_LOG = []; _NAV_FAIL_AT_URL = "https://x.com/captcha/"; _NAV_FAIL_EXCEPTION = "HumanInterventionRequired"
    sp, ctx = _make_ctx("./_tmp_t5", "t5")
    payload = {
        "steps": [{"tool": "navigate", "input": {"url": "https://x.com/{slug}/"}}],
        "iterations": [{"slug":"safe1"}, {"slug":"captcha"}],
        "throttle_seconds": 0,
    }
    r = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r["stop_reason"] == "human_required", r
    assert "wait_user" in r["suggested_action"]
    assert "start_from=1" in r["suggested_action"]
    print(f"  ✓ stop_reason={r['stop_reason']}, suggested mentions wait_user + start_from=1")
asyncio.run(_t5())

# ──────────────────────────────────
# 6. NavigationTimeoutError → transient
# ──────────────────────────────────

print("\n[6] NavigationTimeoutError → transient")
async def _t6():
    global _NAV_LOG, _NAV_FAIL_AT_URL, _NAV_FAIL_EXCEPTION
    _NAV_LOG = []; _NAV_FAIL_AT_URL = "https://x.com/slow/"; _NAV_FAIL_EXCEPTION = "NavigationTimeoutError"
    sp, ctx = _make_ctx("./_tmp_t6", "t6")
    payload = {
        "steps": [{"tool": "navigate", "input": {"url": "https://x.com/{slug}/"}}],
        "iterations": [{"slug":"slow"}],
        "throttle_seconds": 0,
    }
    r = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r["stop_reason"] == "transient", r
    print(f"  ✓ stop_reason={r['stop_reason']}")
asyncio.run(_t6())

# ──────────────────────────────────
# 7. iter 1 审批 reject(非 tty 自动拒)
# ──────────────────────────────────

print("\n[7] require_first_approval=True + 非 tty → user_rejected")
async def _t7():
    global _NAV_LOG, _NAV_FAIL_AT_URL
    _NAV_LOG = []; _NAV_FAIL_AT_URL = ""
    sp, ctx = _make_ctx("./_tmp_t7", "t7")
    payload = {
        "steps": [{"tool": "navigate", "input": {"url": "https://x.com/{slug}/"}}],
        "iterations": [{"slug":"a"}, {"slug":"b"}, {"slug":"c"}],
        "throttle_seconds": 0,
        "require_first_approval": True,
    }
    # CI 环境 stdin 非 tty,自动 reject
    r = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r["stop_reason"] == "user_rejected", r
    assert r["succeeded_total"] == 1, r  # iter 0 跑过了
    assert "user rejected" in r["suggested_action"].lower() or "Re-design" in r.get("suggested_action","")
    print(f"  ✓ stop_reason={r['stop_reason']}, succeeded={r['succeeded_total']}/3")
asyncio.run(_t7())

# ──────────────────────────────────
# 8. spawner 全局 require_first_iteration_approval 强制开
# ──────────────────────────────────

print("\n[8] spawner.require_first_iteration_approval=True 强制开")
async def _t8():
    global _NAV_LOG
    _NAV_LOG = []
    sp, ctx = _make_ctx("./_tmp_t8", "t8")
    sp.require_first_iteration_approval = True   # ← 全局开
    payload = {
        "steps": [{"tool": "navigate", "input": {"url": "https://x.com/{slug}/"}}],
        "iterations": [{"slug":"a"}, {"slug":"b"}],
        "throttle_seconds": 0,
        # require_first_approval 不传也强制开
    }
    r = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r["stop_reason"] == "user_rejected", r
    print(f"  ✓ spawner 全局开关生效:stop_reason={r['stop_reason']}")
asyncio.run(_t8())

# ──────────────────────────────────
# 9. tool 白名单 — spawn_agent 不允许
# ──────────────────────────────────

print("\n[9] step 中调禁用 tool → 立即报错")
async def _t9():
    sp, ctx = _make_ctx("./_tmp_t9", "t9")
    payload = {
        "steps": [{"tool": "spawn_agent", "input": {"agent_type":"worker","task":"x"}}],
        "iterations": [{}],
    }
    r = await BATCH_BROWSER_ACTIONS.handler(payload, ctx)
    assert r.get("ok") is False, r
    assert "spawn_agent" in r["error"]
    print(f"  ✓ spawn_agent 被拒绝: {r['error'][:80]}")
asyncio.run(_t9())

# ──────────────────────────────────
# 10. start_from 越界 / iterations 空
# ──────────────────────────────────

print("\n[10] 边界:start_from 越界 / iterations 空")
async def _t10():
    sp, ctx = _make_ctx("./_tmp_t10", "t10")
    r1 = await BATCH_BROWSER_ACTIONS.handler({"steps":[{"tool":"navigate","input":{"url":"x"}}], "iterations":[]}, ctx)
    assert r1["ok"] is False and "iterations" in r1["error"]
    r2 = await BATCH_BROWSER_ACTIONS.handler({"steps":[{"tool":"navigate","input":{"url":"x"}}], "iterations":[{}], "start_from": 5}, ctx)
    assert r2["ok"] is False and "start_from" in r2["error"]
    print(f"  ✓ 空 iterations / start_from 越界都被拒")
asyncio.run(_t10())

# ──────────────────────────────────
# 11. _classify_exception 单元
# ──────────────────────────────────

print("\n[11] _classify_exception 全异常映射")
mappings = {
    "PendingDialogError": "interaction_required",
    "HumanInterventionRequired": "human_required",
    "ElementNotFoundError": "selector_failed",
    "JSExecutionError": "selector_failed",
    "NavigationTimeoutError": "transient",
    "JSResultTooLargeError": "result_too_large",
    "RandomUnknown": "unknown",
}
for name, expected in mappings.items():
    out = _classify_exception(name, "msg")
    assert out["stop_reason"] == expected, (name, out)
    assert "suggested_action" in out
print(f"  ✓ {len(mappings)} 类异常分类全 ok")

# ──────────────────────────────────
# 清理
# ──────────────────────────────────

import shutil
for d in ["_tmp_t2","_tmp_t3","_tmp_t4","_tmp_t5","_tmp_t6","_tmp_t7","_tmp_t8","_tmp_t9","_tmp_t10"]:
    try: shutil.rmtree(d, ignore_errors=True)
    except: pass

print()
print("✅ batch_browser_actions 全部 11 组测试通过")
