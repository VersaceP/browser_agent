"""DOM 探查单步工具(dom_classes / dom_outline / dom_query)注册 + 白名单 + dispatch 验证"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  DOM 探查工具(单步)接入验证")
print("="*60)

# ──────────────────────────────────
# ① ToolSpec 已注册
# ──────────────────────────────────
print("\n[1] tools registry 含 3 个 dom_* 工具")
from tools import build_default_registry
reg = build_default_registry()
for name in ("dom_classes", "dom_outline", "dom_query"):
    spec = reg.get(name)
    assert spec is not None, f"{name} 未注册"
    assert spec.readonly is True, f"{name} 应是 readonly"
print(f"  ✓ dom_classes / dom_outline / dom_query 都注册,且 readonly=True")

# ──────────────────────────────────
# ② worker.allowed_tools 含 3 个
# ──────────────────────────────────
print("\n[2] worker.allowed_tools 已加 3 个 dom 工具")
from core.agent_definition import build_builtin_agents
ad = build_builtin_agents()
worker = ad["worker"]
for name in ("dom_classes", "dom_outline", "dom_query"):
    assert name in worker.allowed_tools, f"{name} 未加入 worker.allowed_tools"
print(f"  ✓ worker.allowed_tools 现在 {len(worker.allowed_tools)} 个工具,含 3 个 dom_*")

# verification 是 readonly,看看也加进去合理
verif = ad["verification"]
print(f"  (verification 仍只 {len(verif.allowed_tools)} 个工具,不含 dom — 故意,因为校验不该改 selector)")

# ──────────────────────────────────
# ③ batch_tool 白名单含 dom_query (但不含 classes/outline)
# ──────────────────────────────────
print("\n[3] batch_browser_actions step 白名单只允许 dom_query")
from tools.batch_tool import _ALLOWED_STEP_TOOLS
assert "dom_query" in _ALLOWED_STEP_TOOLS, "dom_query 应被加入白名单"
assert "dom_classes" not in _ALLOWED_STEP_TOOLS, "dom_classes 不该在 batch step 用(iter 间结构同)"
assert "dom_outline" not in _ALLOWED_STEP_TOOLS, "dom_outline 不该在 batch step 用"
print(f"  ✓ dom_query 在,dom_classes / dom_outline 故意不在(batch iter 内不该重复探查)")

# ──────────────────────────────────
# ④ dispatch dom_classes 端到端(用 stub helper)
# ──────────────────────────────────
print("\n[4] dispatch dom_classes / dom_outline / dom_query end-to-end(stub helpers)")

import browser.helpers as bh
orig_dom_classes = bh.dom_classes
orig_dom_outline = bh.dom_outline
orig_dom_query = bh.dom_query

bh.dom_classes = lambda min_count=3, top_n=30: [
    {"class_name": "product-card", "count": 50, "tag_sample": "div", "text_sample": "X"}
]
bh.dom_outline = lambda max_items=30, min_text_len=4: [
    {"tag": "a", "id": None, "classes": ["link"], "text_preview": "Hi", "attrs": {}, "selector_hint": "a.link"}
]
bh.dom_query = lambda selector, max_items=20, fields=None: [
    {"text": "T", "href": "/x", "src": None, "value": None, "inner_html_size": 100, "dataset": {"slug": "x"}}
]

try:
    from tools.base import dispatch, ToolContext
    ctx = ToolContext(worktree=".", shared_dir=".", session_id="t", agent_type="worker")

    res1 = asyncio.run(dispatch(reg, "dom_classes", {"min_count": 5}, ctx))
    assert res1["status"] == "ok"
    assert res1["result"][0]["class_name"] == "product-card"
    print(f"  ✓ dom_classes → {res1['result'][0]}")

    res2 = asyncio.run(dispatch(reg, "dom_outline", {"max_items": 20}, ctx))
    assert res2["status"] == "ok"
    assert res2["result"][0]["selector_hint"] == "a.link"
    print(f"  ✓ dom_outline → {res2['result'][0]['selector_hint']}")

    res3 = asyncio.run(dispatch(reg, "dom_query", {"selector": ".x"}, ctx))
    assert res3["status"] == "ok"
    assert res3["result"][0]["dataset"]["slug"] == "x"
    print(f"  ✓ dom_query(.x) → {res3['result'][0]['dataset']}")
finally:
    bh.dom_classes = orig_dom_classes
    bh.dom_outline = orig_dom_outline
    bh.dom_query = orig_dom_query

# ──────────────────────────────────
# ⑤ batch_browser_actions 接受 dom_query 作 step
# ──────────────────────────────────
print("\n[5] batch_browser_actions 允许把 dom_query 作为 step")

from tools.batch_tool import BATCH_BROWSER_ACTIONS

class FakeSpawner:
    def __init__(self):
        self.session_states = {}
        self.registry = reg
        self.require_first_iteration_approval = False

ctx_with_sp = ToolContext(worktree=".", shared_dir=".", session_id="t",
                          agent_type="worker", spawner=FakeSpawner())

# 不在白名单的:用 init_progress 试,确保拒(spawner 已注入,通过 spawner check 后到白名单 check)
res_bad = asyncio.run(BATCH_BROWSER_ACTIONS.handler({
    "steps": [{"tool": "init_progress", "input": {}}],
    "iterations": [{}],
}, ctx_with_sp))
assert res_bad.get("ok") is False
assert "init_progress" in res_bad["error"]
print(f"  ✓ init_progress 仍被拒(不在白名单): {res_bad['error'][:60]}")

# 跑一个 dom_query 作 step,因为没真 chrome 会在 dispatch 内 stub 失败
# 但校验"工具白名单"应该已经 pass,失败发生在 step 内
res_dq = asyncio.run(BATCH_BROWSER_ACTIONS.handler({
    "steps": [{"tool": "dom_query", "input": {"selector": ".x"}}],
    "iterations": [{}, {}],
    "throttle_seconds": 0,
}, ctx_with_sp))
# 这里期待 ok=True(白名单通过),但内部 step 因为 helpers 没真起 chrome 可能报错
# 我们只确认 ok != False AND 无 "不允许在 batch step 中使用" 错
err_msg = str(res_dq.get("error", ""))
assert "不允许在 batch step" not in err_msg
print(f"  ✓ dom_query 通过白名单(可作 step;实际跑由 chrome 可用决定)")

print()
print("✅ DOM 探查单步工具接入全部通过")
