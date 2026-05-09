"""方案 A 验证 — update_progress 子进程 buffer → 主进程 SessionState round-trip。

不真起 LLM,只测:
  ① worker.py _build_namespace 注入了 update_progress + __progress_buffer__
  ② sandbox 子进程内调 update_progress 后,buffer 在 _execute 返回里
  ③ code_tool handler 收到 result 后,正确 flush 到 ctx.spawner.session_states[].progress
  ④ 没有 progress_board 时业务依然不崩,只在 result 加 hint
"""
import sys, os, asyncio, json, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  方案 A — update_progress buffer round-trip")
print("="*60)

# ──────────────────────────────────
# ① worker.py _build_namespace 注入
# ──────────────────────────────────
print("\n[1] _build_namespace 注入 update_progress + __progress_buffer__")
import sandbox.worker as wk
ns = wk._build_namespace(worktree=str(Path.cwd()), shared=[])
assert "update_progress" in ns, "update_progress 没注入"
assert "__progress_buffer__" in ns, "__progress_buffer__ 没注入"
assert callable(ns["update_progress"])
print("  ✓ ns['update_progress'] 存在且可调")
print("  ✓ ns['__progress_buffer__'] 是 list, 当前 len=0")

# ──────────────────────────────────
# ② 在 ns 内调 update_progress → buffer 累积
# ──────────────────────────────────
print("\n[2] 调用 update_progress 累积 buffer")
ns["update_progress"]("tools_scraped", increment=5, note="batch 1 done")
ns["update_progress"]("tools_scraped", increment=3, note="batch 2 done")
ns["update_progress"]("views_extracted", increment=8, status="in_progress")
buf = ns["__progress_buffer__"]
assert len(buf) == 3, buf
assert buf[0]["goal_id"] == "tools_scraped" and buf[0]["increment"] == 5
assert buf[1]["increment"] == 3
assert buf[2]["status"] == "in_progress"
print(f"  ✓ buffer 累积 3 条: {[(b['goal_id'], b['increment']) for b in buf]}")

# ──────────────────────────────────
# ③ _execute 把 buffer 一并打包到 result
# ──────────────────────────────────
print("\n[3] _execute 返回值含 progress_updates 字段")
# 重新建 ns 并跑一段 exec 模拟
ns2 = wk._build_namespace(worktree=str(Path.cwd()), shared=[])
result = wk._execute(
    code="update_progress('items', increment=10, note='in code'); print('ok')",
    namespace=ns2,
)
assert result["status"] == "ok", result
assert "progress_updates" in result
assert len(result["progress_updates"]) == 1
assert result["progress_updates"][0]["goal_id"] == "items"
assert result["progress_updates"][0]["increment"] == 10
print(f"  ✓ result['progress_updates'] = {result['progress_updates']}")

# 异常路径:exec 抛错,buffer 仍要回传
ns3 = wk._build_namespace(worktree=str(Path.cwd()), shared=[])
result_err = wk._execute(
    code="update_progress('items', increment=2); raise RuntimeError('boom')",
    namespace=ns3,
)
assert result_err["status"] == "error"
assert result_err["progress_updates"] == [{"goal_id":"items","increment":2,"status":None,"note":""}]
print(f"  ✓ 异常路径下 buffer 也回传:{result_err['progress_updates']}")

# ──────────────────────────────────
# ④ code_tool handler flush 到 SessionState
# ──────────────────────────────────
print("\n[4] code_tool handler 把 progress_updates flush 到 SessionState")
from tools.code_tool import _run_browser_python_handler
from tools.base import ToolContext
from core.session_state import SessionState, Goal, ProgressBoard

# Mock pool — 直接返回带 progress_updates 的 result
class FakePool:
    def exec_code(self, code, worktree, shared, timeout):
        return {
            "status": "ok",
            "stdout": "...",
            "stderr": "",
            "duration_s": 0.1,
            "progress_updates": [
                {"goal_id": "tools_scraped", "increment": 5, "status": None, "note": "first"},
                {"goal_id": "tools_scraped", "increment": 5, "status": None, "note": "second"},
                {"goal_id": "views_extracted", "increment": 10, "status": "completed", "note": ""},
                {"goal_id": "missing_goal",   "increment": 1, "status": None, "note": "bad id"},
            ],
        }

class MockSpawner:
    def __init__(self):
        self.session_states = {}

sp = MockSpawner()
sid = "tflush"
st = SessionState(session_id=sid)
st.progress = ProgressBoard()
st.progress.add_goal(Goal(id="tools_scraped", description="scrape", target=10))
st.progress.add_goal(Goal(id="views_extracted", description="views", target=10))
sp.session_states[sid] = st

ctx = ToolContext(
    worktree=".", shared_dir="./shared", session_id=sid,
    agent_type="worker", spawner=sp, pool=FakePool(),
)

result = _run_browser_python_handler({"code": "..."}, ctx)
assert "progress_updates" not in result, "应已被 pop 出 result"
assert "progress_applied" in result
applied = result["progress_applied"]
assert isinstance(applied, list)
assert len(applied) == 4
# tools_scraped: 5+5=10
assert st.progress.goals["tools_scraped"].current == 10
assert st.progress.goals["tools_scraped"].status == "completed"  # >= target
# views_extracted: 10 + status=completed
assert st.progress.goals["views_extracted"].current == 10
assert st.progress.goals["views_extracted"].status == "completed"
# missing_goal: ok=False
last = applied[-1]
assert last["goal_id"] == "missing_goal" and last["applied"] is False
print(f"  ✓ tools_scraped: {st.progress.goals['tools_scraped'].current}/10 ({st.progress.goals['tools_scraped'].status})")
print(f"  ✓ views_extracted: {st.progress.goals['views_extracted'].current}/10 ({st.progress.goals['views_extracted'].status})")
print(f"  ✓ missing_goal: applied=False (干净 reject 不抛异常)")

# ──────────────────────────────────
# ⑤ 没有 progress_board 时不崩
# ──────────────────────────────────
print("\n[5] session 无 progress_board 时,buffer 不丢只 hint")
sp2 = MockSpawner()
sp2.session_states["nopb"] = SessionState(session_id="nopb")  # progress=None

ctx2 = ToolContext(
    worktree=".", shared_dir="./shared", session_id="nopb",
    agent_type="worker", spawner=sp2, pool=FakePool(),
)
result2 = _run_browser_python_handler({"code": "..."}, ctx2)
assert isinstance(result2["progress_applied"], str)
assert "no progress board" in result2["progress_applied"]
assert "4 buffered" in result2["progress_applied"]
print(f"  ✓ hint: {result2['progress_applied']}")

print()
print("✅ 方案 A round-trip 全部通过")
