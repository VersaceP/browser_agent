"""supersede 机制端到端验证 — read_file 累积 4 次后,前 3 次的 tool_result 被覆盖成 stub"""
import sys, os, asyncio
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  read_file supersede — 旧 tool_result 自动覆盖为 stub")
print("="*60)

import shutil, json
WORK = Path("./_tmp_super").resolve(); WORK.mkdir(exist_ok=True)
SHARED = WORK / "shared"; SHARED.mkdir(exist_ok=True)

big_records = [{"id": i, "name": f"item_with_some_padding_{i}", "value": i * 7,
                "desc": "X" * 80} for i in range(2000)]
BIG = SHARED / "big.json"
BIG.write_text(json.dumps(big_records, indent=2), encoding="utf-8")
print(f"[fixture] big.json = {BIG.stat().st_size} bytes / {len(BIG.read_text(encoding='utf-8'))} chars")

from tools.file_tools import _read_file_handler, clear_read_history
from tools.base import ToolContext
from core.execution_loop import _supersede_tool_results, _SUPERSEDE_STUB

clear_read_history()

# ──────────────────────────────────
# ① _supersede_tool_results 单元 — 改写匹配 id 的 tool_result content
# ──────────────────────────────────
print("\n[1] _supersede_tool_results 改写 id 命中的 tool_result")
messages = [
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "id_a", "content": "BIG_A", "is_error": False}]},
    {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "id_b", "content": "BIG_B", "is_error": False},
        {"type": "tool_result", "tool_use_id": "id_c", "content": "BIG_C", "is_error": False},
    ]},
]
_supersede_tool_results(messages, ["id_a", "id_b"])

# id_a / id_b 应被改 stub,id_c 不动
assert messages[0]["content"][0]["content"] == _SUPERSEDE_STUB
assert messages[2]["content"][0]["content"] == _SUPERSEDE_STUB
assert messages[2]["content"][1]["content"] == "BIG_C"
print(f"  ✓ id_a / id_b 被改 stub,id_c 保留原内容")

# 重复 supersede 同 id 不报错
_supersede_tool_results(messages, ["id_a"])
assert messages[0]["content"][0]["content"] == _SUPERSEDE_STUB
print(f"  ✓ 重复 supersede 同 id 幂等")

# 空 list / None 不影响
_supersede_tool_results(messages, [])
print(f"  ✓ 空 supersede list 不影响 messages")

# ──────────────────────────────────
# ② file_tools 成功 read 时把 prior ids 塞 ctx.extra
# ──────────────────────────────────
print("\n[2] read_file 成功后塞 supersede_tool_use_ids")
ctx = ToolContext(worktree=str(WORK), shared_dir=str(SHARED),
                  session_id="t_super", agent_type="verification")

# 第 1 次 — 没 prior,不塞
ctx.tool_use_id = "tu_1"
ctx.extra = {}
r1 = _read_file_handler({"path": "shared/big.json", "max_chars": 10000}, ctx)
assert "content" in r1
assert "supersede_tool_use_ids" not in ctx.extra
print(f"  ✓ 第 1 次 read 不塞 supersede(没 prior)")

# 第 2 次 — 应塞 [tu_1]
ctx.tool_use_id = "tu_2"
ctx.extra = {}
r2 = _read_file_handler({"path": "shared/big.json", "max_chars": 20000}, ctx)
assert "content" in r2
assert ctx.extra.get("supersede_tool_use_ids") == ["tu_1"]
print(f"  ✓ 第 2 次 read 塞 supersede=[tu_1]")

# 第 3 次 — 应塞 [tu_1, tu_2]
ctx.tool_use_id = "tu_3"
ctx.extra = {}
r3 = _read_file_handler({"path": "shared/big.json", "max_chars": 50000}, ctx)
assert "content" in r3
assert ctx.extra.get("supersede_tool_use_ids") == ["tu_1", "tu_2"]
print(f"  ✓ 第 3 次 read 塞 supersede=[tu_1, tu_2]")

# 第 4 次 — 走 summarizer fallback (无 summarizer → 截 50K) 也应塞 supersede
ctx.tool_use_id = "tu_4"
ctx.extra = {}
ctx.summarizer = None
r4 = _read_file_handler({"path": "shared/big.json", "max_chars": 100000}, ctx)
# 注意:max_chars=100000 但无 summarizer → 走档 1-3 逻辑(因为 use_summary=False)
# 文件 > 100K → truncated=True,返回 100K 原文
assert "content" in r4, f"r4 should have content, got {r4}"
assert ctx.extra.get("supersede_tool_use_ids") == ["tu_1", "tu_2", "tu_3"]
print(f"  ✓ 第 4 次 read 塞 supersede=[tu_1, tu_2, tu_3]")

# ──────────────────────────────────
# ③ 重复 max_chars 也允许真读 + 也触发 supersede(方案 A 删了 redundant 检测)
# ──────────────────────────────────
print("\n[3] 方案 A:重复 max_chars 不再 redundant,正常成功 + 也触发 supersede")
ctx.tool_use_id = "tu_5"
ctx.extra = {}
r5 = _read_file_handler({"path": "shared/big.json", "max_chars": 30000}, ctx)
assert "content" in r5, f"应正常读: {r5}"
assert r5.get("redundant") is None
# 现在已经第 5 次,supersede 应含 tu_1..tu_4
assert ctx.extra.get("supersede_tool_use_ids") == ["tu_1", "tu_2", "tu_3", "tu_4"]
print(f"  ✓ 重复 max_chars 正常返回 + 仍 supersede 之前 4 个")

# 硬上限兜底:第 6 次还是允许,第 7 次起 throttled
ctx.tool_use_id = "tu_6"
ctx.extra = {}
r6 = _read_file_handler({"path": "shared/big.json", "max_chars": 5000}, ctx)
assert "content" in r6
ctx.tool_use_id = "tu_7"
ctx.extra = {}
r7 = _read_file_handler({"path": "shared/big.json", "max_chars": 5000}, ctx)
assert r7.get("throttled") is True
assert "supersede_tool_use_ids" not in ctx.extra  # 被拦截不读 → 不 supersede
print(f"  ✓ throttled 调用不触发 supersede(没真读到东西)")

# ──────────────────────────────────
# ④ 端到端模拟:用 file_tools + 手动 supersede,确认 messages 缩水
# ──────────────────────────────────
print("\n[4] 端到端:模拟 4 次 read + 自动 supersede 改写 messages")
clear_read_history()

# 模拟 LLM 4 次调用 read_file 的 messages 历史
fake_messages = [
    {"role": "user", "content": "Task: read shared/big.json"},
]

# 4 次 dispatch — 每次记 tool_use,跑 handler,supersede 改 messages
ctx2 = ToolContext(worktree=str(WORK), shared_dir=str(SHARED),
                   session_id="t_e2e", agent_type="verification")

call_results = []
for idx, mc in enumerate([10000, 20000, 50000, 100000], 1):
    tu_id = f"tu_e2e_{idx}"
    ctx2.tool_use_id = tu_id
    ctx2.extra = {}
    r = _read_file_handler({"path": "shared/big.json", "max_chars": mc}, ctx2)
    call_results.append((tu_id, r))

    # 模拟 execution_loop:写 supersede + append 新 tool_result
    supersede_ids = ctx2.extra.pop("supersede_tool_use_ids", None)
    if supersede_ids:
        _supersede_tool_results(fake_messages, supersede_ids)

    # 模拟把新 tool_result append 进 messages
    fake_messages.append({
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": tu_id,
            "content": json.dumps(r, ensure_ascii=False)[:5000],  # 模拟 format_result_for_llm
            "is_error": False,
        }],
    })

# 检查:fake_messages 里有 4 个 tool_result block,前 3 个应是 stub,只有最后一个是真内容
tool_result_blocks = [
    b for m in fake_messages if isinstance(m.get("content"), list)
    for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
]
assert len(tool_result_blocks) == 4
stub_blocks = [b for b in tool_result_blocks if b["content"] == _SUPERSEDE_STUB]
content_blocks = [b for b in tool_result_blocks if b["content"] != _SUPERSEDE_STUB]
assert len(stub_blocks) == 3, f"应有 3 个 stub, got {len(stub_blocks)}"
assert len(content_blocks) == 1
assert content_blocks[0]["tool_use_id"] == "tu_e2e_4"
print(f"  ✓ 4 次 read 后:3 个 stub + 1 个真内容(最后那次 tu_e2e_4)")

# 计算上下文节省的 token 估算
total_chars_with_stubs = sum(len(b["content"]) for b in tool_result_blocks)
total_chars_without = sum(len(json.dumps(r, ensure_ascii=False)[:5000]) for _, r in call_results)
print(f"  ✓ 上下文 chars: with supersede={total_chars_with_stubs}, "
      f"without supersede={total_chars_without}, "
      f"节省 {total_chars_without - total_chars_with_stubs} chars "
      f"({(1 - total_chars_with_stubs/total_chars_without)*100:.1f}%)")

# 清理
shutil.rmtree(WORK, ignore_errors=True)
print()
print("✅ supersede 机制全部通过")
