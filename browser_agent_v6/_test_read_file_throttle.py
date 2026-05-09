"""read_file 方案 A 验证:删 redundant 检测 + 摘要缓存 + 硬上限 6"""
import sys, os, asyncio
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  read_file 方案 A — 删 redundant + 摘要缓存 + 硬上限 6")
print("="*60)

import shutil, json
WORK = Path("./_tmp_a").resolve(); WORK.mkdir(exist_ok=True)
SHARED = WORK / "shared"; SHARED.mkdir(exist_ok=True)

big_records = [{"id": i, "name": f"item_with_some_padding_{i}", "value": i * 7,
                "desc": "X" * 80} for i in range(2000)]
BIG = SHARED / "big.json"
BIG.write_text(json.dumps(big_records, indent=2), encoding="utf-8")
print(f"\n[fixture] big.json size = {len(BIG.read_text(encoding='utf-8'))} chars")

SMALL = SHARED / "small.json"
SMALL.write_text(json.dumps([{"x": 1}, {"x": 2}]), encoding="utf-8")

from tools.file_tools import (
    _read_file_handler, clear_read_history,
    _READ_FILE_CALLS, _SUMMARY_CACHE, _READ_FILE_HARD_CAP,
)
from tools.base import ToolContext

clear_read_history()
print(f"\n[constants] HARD_CAP={_READ_FILE_HARD_CAP}")

# ──────────────────────────────────
# ① 渐进 10K → 20K → 50K 全部正常返回(不再有 redundant 检测)
# ──────────────────────────────────
print("\n[1] 渐进 10K → 20K → 50K 正常通过")
ctx = ToolContext(worktree=str(WORK), shared_dir=str(SHARED),
                  session_id="t1", agent_type="verification")
ctx.tool_use_id = "tu_1"
r1 = _read_file_handler({"path": "shared/big.json", "max_chars": 10000}, ctx)
assert "content" in r1 and len(r1["content"]) == 10000
ctx.tool_use_id = "tu_2"
r2 = _read_file_handler({"path": "shared/big.json", "max_chars": 20000}, ctx)
assert "content" in r2 and len(r2["content"]) == 20000
ctx.tool_use_id = "tu_3"
r3 = _read_file_handler({"path": "shared/big.json", "max_chars": 50000}, ctx)
assert "content" in r3 and len(r3["content"]) == 50000
print(f"  ✓ 3 次渐进,calls_so_far={r3['calls_so_far']}")

# ──────────────────────────────────
# ② 重复同 max_chars(原本 redundant)— 现在允许真读
# ──────────────────────────────────
print("\n[2] 重复 max_chars 允许真读(不再 redundant)")
ctx.tool_use_id = "tu_4"
r4 = _read_file_handler({"path": "shared/big.json", "max_chars": 10000}, ctx)
assert "content" in r4, f"应正常读,不该 redundant: {r4}"
assert r4.get("redundant") is None
assert r4["calls_so_far"] == 4
print(f"  ✓ 同 max_chars=10000 重发,真读返回 {len(r4['content'])} chars(不 redundant)")

# ──────────────────────────────────
# ③ 上次 truncated=false 后再读(原本 redundant)— 现在允许
# ──────────────────────────────────
print("\n[3] 上次 truncated=false 后再读 — 允许")
ctx_s = ToolContext(worktree=str(WORK), shared_dir=str(SHARED),
                    session_id="t_small", agent_type="verification")
ctx_s.tool_use_id = "ts_1"
rs1 = _read_file_handler({"path": "shared/small.json", "max_chars": 10000}, ctx_s)
assert rs1["truncated"] is False  # 文件 < max_chars
ctx_s.tool_use_id = "ts_2"
rs2 = _read_file_handler({"path": "shared/small.json", "max_chars": 50000}, ctx_s)
assert "content" in rs2, f"应允许重读,不再 redundant: {rs2}"
assert rs2.get("redundant") is None
print(f"  ✓ 上次 truncated=false,重读仍正常")

# ──────────────────────────────────
# ④ 摘要缓存:第 1 次调 Haiku,第 2 次命中 cache(零 Haiku 调用)
# ──────────────────────────────────
print("\n[4] 摘要缓存命中")

class StubSummarizer:
    class config: model_id = "stub-haiku"
    def __init__(self): self.calls = 0
    async def generate_response(self, system_prompt, messages, tools):
        self.calls += 1
        summary_json = json.dumps({
            "schema": {"id": "int"},
            "total_records": 2000,
            "summary": "ok",
        })
        return summary_json, [], "end_turn", {}

stub = StubSummarizer()
ctx_sum = ToolContext(worktree=str(WORK), shared_dir=str(SHARED),
                      session_id="t_sum", agent_type="verification",
                      summarizer=stub)
ctx_sum.tool_use_id = "tsum_1"
rs1 = _read_file_handler({"path": "shared/big.json", "max_chars": 100000}, ctx_sum)
assert rs1["summarized"] is True
assert rs1["cached"] is False
assert stub.calls == 1
print(f"  ✓ 第 1 次摘要:Haiku 被调 1 次, cached=False")

# 第 2 次同 session 同 path → 命中缓存
ctx_sum.tool_use_id = "tsum_2"
rs2 = _read_file_handler({"path": "shared/big.json", "max_chars": 100000}, ctx_sum)
assert rs2["summarized"] is True
assert rs2["cached"] is True
assert stub.calls == 1, "Haiku 不该被再调"
print(f"  ✓ 第 2 次摘要:命中 cache, Haiku 调用次数仍为 {stub.calls}(零增长)")

# 不同 session 应不命中(缓存按 session 隔离)
ctx_other = ToolContext(worktree=str(WORK), shared_dir=str(SHARED),
                        session_id="t_other_session", agent_type="verification",
                        summarizer=stub)
ctx_other.tool_use_id = "to_1"
ro = _read_file_handler({"path": "shared/big.json", "max_chars": 100000}, ctx_other)
assert ro["cached"] is False
assert stub.calls == 2
print(f"  ✓ 不同 session 不共享 cache(隔离)")

# ──────────────────────────────────
# ⑤ 硬上限 6 次
# ──────────────────────────────────
print("\n[5] 硬上限 6 次")
ctx_loop = ToolContext(worktree=str(WORK), shared_dir=str(SHARED),
                       session_id="t_loop", agent_type="verification")
last = None
for i in range(8):
    ctx_loop.tool_use_id = f"tl_{i}"
    last = _read_file_handler({"path": "shared/big.json", "max_chars": 10000 + i}, ctx_loop)

assert last.get("throttled") is True, last
assert last["calls_so_far"] == _READ_FILE_HARD_CAP
print(f"  ✓ 第 7-8 次 throttled, calls_so_far={last['calls_so_far']}, hard_cap={last['hard_cap']}")

# ──────────────────────────────────
# ⑥ clear_read_history 同步清 _SUMMARY_CACHE
# ──────────────────────────────────
print("\n[6] clear_read_history 联动清 _SUMMARY_CACHE")
# t_sum 的 cache 应该存在
assert any(k[0] == "t_sum" for k in _SUMMARY_CACHE)
clear_read_history("t_sum")
assert not any(k[0] == "t_sum" for k in _SUMMARY_CACHE), "t_sum 的 cache 应被清"
# t_other_session 仍存
assert any(k[0] == "t_other_session" for k in _SUMMARY_CACHE)
print(f"  ✓ clear_read_history('t_sum') 清掉了 t_sum 的 cache,其他 session 保留")

clear_read_history()
assert len(_SUMMARY_CACHE) == 0
assert len(_READ_FILE_CALLS) == 0
print(f"  ✓ 全清后 _SUMMARY_CACHE 和 _READ_FILE_CALLS 都空")

# 清理
shutil.rmtree(WORK, ignore_errors=True)
print()
print("✅ 方案 A 全部通过(redundant 检测已删,supersede + cache + hard cap 6 工作正常)")
