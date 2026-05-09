"""批次 A 验证:熔断器 + lead 边界 prompt"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

from core.fatal_tracker import FatalErrorTracker

print("--- A1: FatalErrorTracker 单测 ---")
t = FatalErrorTracker(max_fatal=3, cool_down_seconds=300)
for i in range(2):
    t.record(f"fatal #{i+1}")
    ok, _ = t.check()
    assert ok
    print(f"  after {i+1} fatals -> still ok")
t.record("fatal #3 (quota exhausted)")
ok, reason = t.check()
assert not ok
print(f"  after 3 fatals -> TRIPPED")
print(f"    reason: {reason[:140]}...")

t.reset()
ok, _ = t.check()
assert ok
print(f"  after reset -> ok again")

# status
t.record("e1"); t.record("e2")
s = t.status()
print(f"  status() = {s}")

print("\n--- A2: LEAD_PROMPT 边界约束 ---")
from core.agent_definition import build_builtin_agents
lead = build_builtin_agents()["lead"]
required = ["Task boundary", "STOP", "circuit-breaker", "auto-spawn"]
for k in required:
    assert k.lower() in lead.system_prompt.lower(), f"缺关键词: {k}"
    print(f"  含 '{k}': ok")

print("\n✅ 批次 A 全部通过")
