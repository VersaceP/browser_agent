"""PageDisconnectedError 自愈三层验证 — 全用 stub,不真起 Chrome。

覆盖:
  ① helpers._is_disconnect_error / _try_recover 关键词识别
  ② helpers._with_recovery 装饰器:
     - in-proc recovery 成功(端口仍活)→ 自动重试通过
     - in-proc recovery 失败(端口死)→ 抛 PageDisconnectedError
     - 非失联异常透传(ElementNotFoundError 等)
  ③ tools/base.py dispatch 层:
     - 单步工具失联 → daemon.restart() → 重试通过(recovered_via_restart=True)
     - daemon.restart() 自身失败 → 原 error 上加 restart_attempted_failed
     - 没注入 daemon → 不触发重启路径,error 透传
  ④ 公开 helpers 已被装上 _with_recovery
"""
import sys, os, asyncio
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  PageDisconnectedError — 三层自愈验证")
print("="*60)

# ──────────────────────────────────
# ① 失联关键词识别
# ──────────────────────────────────
print("\n[1] _is_disconnect_error 关键词识别")
import browser.helpers as bh
from browser.errors import (
    PageDisconnectedError, ElementNotFoundError, NavigationTimeoutError,
)
class _StubDisc(Exception): pass

assert bh._is_disconnect_error(Exception("WebSocket is closed"))
assert bh._is_disconnect_error(Exception("页面的连接已断开。版本: 4.1.1.2"))
assert bh._is_disconnect_error(Exception("Browser has crashed"))
assert bh._is_disconnect_error(PageDisconnectedError("anything"))
assert not bh._is_disconnect_error(ElementNotFoundError(".x", 5))
assert not bh._is_disconnect_error(ValueError("invalid url"))
print("  ✓ 6 个 case 全部识别正确")

# ──────────────────────────────────
# ② _with_recovery 装饰器行为
# ──────────────────────────────────
print("\n[2] _with_recovery 装饰器")

# 2a: 业务异常透传(ElementNotFound)
@bh._with_recovery
def fn_business_err():
    raise ElementNotFoundError(".x", 5)

try:
    fn_business_err()
    assert False, "应抛 ElementNotFoundError"
except ElementNotFoundError:
    print("  ✓ 业务异常 ElementNotFoundError 透传")

# 2b: 失联 + recover 失败 → PageDisconnectedError
recover_calls = [0]
def stub_recover_fail():
    recover_calls[0] += 1
    return False

orig_recover = bh._try_recover
bh._try_recover = stub_recover_fail
try:
    @bh._with_recovery
    def fn_disc():
        raise Exception("WebSocket connection closed")
    try:
        fn_disc()
        assert False, "应抛 PageDisconnectedError"
    except PageDisconnectedError as e:
        assert e.recovered_attempted is True
    print(f"  ✓ recover 失败 → PageDisconnectedError(recovered_attempted=True), 调 recover {recover_calls[0]} 次")
finally:
    bh._try_recover = orig_recover

# 2c: 失联 + recover 成功 → 自动重试通过
attempt = [0]
def stub_recover_ok():
    return True

@bh._with_recovery
def fn_recoverable():
    attempt[0] += 1
    if attempt[0] == 1:
        raise Exception("page disconnected")
    return "second_call_ok"

bh._try_recover = stub_recover_ok
try:
    out = fn_recoverable()
    assert out == "second_call_ok"
    assert attempt[0] == 2
    print(f"  ✓ recover 成功 → 自动重试通过 (调 {attempt[0]} 次,返回 {out!r})")
finally:
    bh._try_recover = orig_recover

# ──────────────────────────────────
# ③ 公开 helpers 已被装上 _with_recovery
# ──────────────────────────────────
print("\n[3] 关键 helpers 已被 _with_recovery 装饰")
expected_wrapped = ("goto", "click", "fill", "screenshot", "scroll",
                    "js", "dom_outline", "dom_classes", "dom_query",
                    "accept_dialog", "dismiss_dialog", "page_info",
                    "wait_for_element", "current_url", "current_title",
                    "list_tabs", "switch_tab", "new_tab", "close_tab")
not_wrapped = ("set_active_port", "acquire_browser", "release_browser",
               "sleep", "list_skills", "read_skill")
for name in expected_wrapped:
    fn = getattr(bh, name)
    assert getattr(fn, "__wrapped__", None) is not None, f"{name} 应被装上 recovery 却没有"
for name in not_wrapped:
    fn = getattr(bh, name)
    assert getattr(fn, "__wrapped__", None) is None, f"{name} 不该被装 recovery"
print(f"  ✓ {len(expected_wrapped)} 个核心 helper 已装,{len(not_wrapped)} 个工具函数未装(预期)")

# ──────────────────────────────────
# ④ dispatch 层 daemon.restart() fallback
# ──────────────────────────────────
print("\n[4] dispatch 层 daemon.restart() 自动恢复")

from tools.base import dispatch, ToolContext, ToolRegistry, ToolSpec

class StubDaemon:
    def __init__(self):
        self.restart_calls = 0
        self.restart_should_fail = False
    def restart(self, wait_seconds=10.0):
        self.restart_calls += 1
        if self.restart_should_fail:
            raise RuntimeError("simulated restart failure")
        return {"Browser": "stub"}

async def t4_disc_then_ok():
    """失联 → daemon.restart() → 重试通过"""
    daemon = StubDaemon()
    state = {"first": True}
    def handler(tool_input, ctx):
        if state["first"]:
            state["first"] = False
            raise PageDisconnectedError("WebSocket closed", recovered_attempted=True)
        return {"data": "ok-after-restart"}

    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d", input_schema={"type":"object"}, handler=handler))
    ctx = ToolContext(worktree=".", shared_dir=".", session_id="x",
                      agent_type="worker", daemon=daemon)
    res = await dispatch(reg, "t", {}, ctx)
    assert res["status"] == "ok"
    assert res["recovered_via_restart"] is True
    assert daemon.restart_calls == 1
    return res

res = asyncio.run(t4_disc_then_ok())
print(f"  ✓ disc → restart → ok: {res['result']}, recovered_via_restart=True")

async def t4_disc_then_disc():
    """失联 → restart → 仍失联 → 报告 restart_attempted=True"""
    daemon = StubDaemon()
    def handler(tool_input, ctx):
        raise PageDisconnectedError("still dead")

    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d", input_schema={"type":"object"}, handler=handler))
    ctx = ToolContext(worktree=".", shared_dir=".", session_id="x",
                      agent_type="worker", daemon=daemon)
    res = await dispatch(reg, "t", {}, ctx)
    assert res["status"] == "error"
    assert res["restart_attempted"] is True
    assert daemon.restart_calls == 1
    return res

res = asyncio.run(t4_disc_then_disc())
print(f"  ✓ disc → restart → 仍 disc: status=error, restart_attempted=True")

async def t4_restart_fails():
    """失联 → restart 自身失败 → 标 restart_attempted_failed"""
    daemon = StubDaemon()
    daemon.restart_should_fail = True
    def handler(tool_input, ctx):
        raise PageDisconnectedError("dead")
    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d", input_schema={"type":"object"}, handler=handler))
    ctx = ToolContext(worktree=".", shared_dir=".", session_id="x",
                      agent_type="worker", daemon=daemon)
    res = await dispatch(reg, "t", {}, ctx)
    assert res["status"] == "error"
    assert "restart_attempted_failed" in res
    assert "simulated restart failure" in res["restart_attempted_failed"]
    return res

res = asyncio.run(t4_restart_fails())
print(f"  ✓ restart 自身失败 → 原 error + restart_attempted_failed 标记")

async def t4_no_daemon():
    """没注入 daemon 时,失联也不会触发 restart"""
    def handler(tool_input, ctx):
        raise PageDisconnectedError("dead")
    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d", input_schema={"type":"object"}, handler=handler))
    ctx = ToolContext(worktree=".", shared_dir=".", session_id="x",
                      agent_type="worker", daemon=None)
    res = await dispatch(reg, "t", {}, ctx)
    assert res["status"] == "error"
    assert "restart_attempted" not in res
    assert "restart_attempted_failed" not in res
    return res

res = asyncio.run(t4_no_daemon())
print(f"  ✓ 无 daemon 注入 → error 透传不触发 restart 路径")

async def t4_business_err_no_restart():
    """非失联异常(ElementNotFoundError 等)不触发 restart"""
    daemon = StubDaemon()
    def handler(tool_input, ctx):
        raise ElementNotFoundError(".x", 5)
    reg = ToolRegistry()
    reg.register(ToolSpec(name="t", description="d", input_schema={"type":"object"}, handler=handler))
    ctx = ToolContext(worktree=".", shared_dir=".", session_id="x",
                      agent_type="worker", daemon=daemon)
    res = await dispatch(reg, "t", {}, ctx)
    assert res["status"] == "error"
    assert res["exception"] == "ElementNotFoundError"
    assert daemon.restart_calls == 0   # ← 不该触发
    return res

res = asyncio.run(t4_business_err_no_restart())
print(f"  ✓ 业务异常 ElementNotFoundError 不触发 restart (calls=0)")

# ──────────────────────────────────
# ⑤ daemon.restart() 接口 + is_alive() 行为
# ──────────────────────────────────
print("\n[5] BrowserDaemon.restart() / is_alive() 接口存在")
from browser.daemon import BrowserDaemon
d = BrowserDaemon(port=39999)  # 必然不存在的端口
assert hasattr(d, "restart"), "restart 方法没加"
assert callable(d.restart)
assert d.is_alive() is False, "无 chrome 时 is_alive 应 False"
print("  ✓ daemon.restart() / 强 is_alive() 都存在")

print()
print("✅ PageDisconnectedError 三层自愈全部通过")
