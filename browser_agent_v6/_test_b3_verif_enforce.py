"""验证 _enforce_verification_if_needed 的判定逻辑(纯单元,不真起 LLM)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

from core.session_state import SessionState, Contract
from core.agent_spawner import _has_verification_in_input

print("="*60)
print("  Verification 强制 — helper 判定逻辑")
print("="*60)

# 1. _has_verification_in_input
print("\n[1] _has_verification_in_input")
assert _has_verification_in_input({"agent_type": "verification"}) is True
assert _has_verification_in_input({"agent_type": "worker"}) is False
assert _has_verification_in_input({"tasks": [{"agent_type": "worker"}, {"agent_type": "verification"}]}) is True
assert _has_verification_in_input({"tasks": [{"agent_type": "worker"}]}) is False
assert _has_verification_in_input({}) is False
print("    单 agent / parallel tasks 两种形态 + 边界 case 全 ok")

# 2. _sync_contract_state — 模拟 agent_type='verification' 完成后,把 published 全升 verified
print("\n[2] _sync_contract_state with verification agent")
import asyncio
from core.agent_spawner import AgentSpawner
from tools import build_default_registry

class StubLLM:
    pass

spawner = AgentSpawner(
    registry=build_default_registry(),
    llm=StubLLM(),
    worktrees_root=str(__import__("pathlib").Path(__file__).parent / "worktrees"),
)
st = SessionState(session_id="t_sync")
st.add_contract(Contract(name="products", output_path="shared/products.json", agent_type="worker", description="d"))
st.add_contract(Contract(name="valid", output_path="shared/valid.txt", agent_type="verification", description="d"))

# 物理建一个 shared/products.json
shared = spawner.shared_dir_root / "t_sync" / "shared"
shared.mkdir(parents=True, exist_ok=True)
(shared / "products.json").write_text('{"x":1}', encoding="utf-8")

# 没跑过 verification → published 应被同步,verified 仍 False
spawner._sync_contract_state(st, agent_type="worker")
assert st.contracts["products"].published is True, st.contracts["products"]
assert st.contracts["products"].verified is False
print(f"    after worker sync: published=True, verified=False ✓")

# 跑了 verification → 升 verified
spawner._sync_contract_state(st, agent_type="verification")
assert st.contracts["products"].verified is True
print(f"    after verification sync: verified=True ✓")

# 无 contract → noop
st_empty = SessionState(session_id="t_empty")
spawner._sync_contract_state(st_empty, agent_type="worker")  # should not raise
print(f"    empty contracts: no error ✓")

print()
print("✅ Verification 强制 helper 全部通过")
