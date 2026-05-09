"""Batch C 验证:SessionState wiring + B3 progress + 唯一 contract + verification 强制 + 审批门"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  Batch C: SessionState + Progress + Contracts + Verification")
print("="*60)

# 1. import 全部新模块
from core.session_state import SessionState, Goal, ProgressBoard, Contract
from core.fatal_tracker import FatalErrorTracker
from core.agent_spawner import AgentSpawner
from core.agent_definition import build_builtin_agents
from tools import build_default_registry, ALL_PROGRESS_TOOLS
from tools.lead_tools import (
    parse_plan, find_contract_for_task, format_contract_block, _basename_no_ext,
)
print("\n[1] all imports ok")

# 2. registry has init_progress + update_progress
reg = build_default_registry()
print(f"[2] registry: {len(reg.names())} tools")
assert reg.get("init_progress"), "init_progress missing"
assert reg.get("update_progress"), "update_progress missing"
assert reg.get("submit_plan"), "submit_plan missing"
print("    init_progress / update_progress / submit_plan all present")

# 3. agent allowed_tools
ad = build_builtin_agents(force_single_step_browser=False)
assert "init_progress" in ad["lead"].allowed_tools, ad["lead"].allowed_tools
assert "update_progress" in ad["worker"].allowed_tools, ad["worker"].allowed_tools
print("[3] lead has init_progress, worker has update_progress")

# 4. SessionState + Contract behavior
st = SessionState(session_id="s1")
st.add_contract(Contract(name="products", output_path="shared/products.json",
                         agent_type="worker", description="scrape 50"))
st.add_contract(Contract(name="valid", output_path="shared/valid.txt",
                         agent_type="verification", description="check"))
assert st.has_verification_step is True
assert st.mark_published("products") is True
pv = st.pending_verification()
assert len(pv) == 1 and pv[0].name == "products"
print(f"[4] SessionState: contracts={len(st.contracts)} pending_verif={len(pv)} verif_required={st.has_verification_step}")

# 5. ProgressBoard
pb = ProgressBoard()
pb.add_goal(Goal(id="products", description="scrape 50", target=50))
pb.update("products", increment=10, note="first batch ok")
pb.update("products", increment=20)
g = pb.goals["products"]
assert g.current == 30 and g.status == "in_progress"
pb.update("products", increment=20)
assert pb.goals["products"].current == 50 and pb.goals["products"].status == "completed"
assert pb.all_done() is True
snippet = pb.format_for_prompt()
assert len(snippet) > 0
print(f"[5] ProgressBoard: all_done={pb.all_done()} snippet_len={len(snippet)}")
print(f"    snippet preview:\n{snippet}")

# 6. parse_plan + unique-name detect
plan_md = """
- [ ] [worker] scrape 50 products
      output: shared/products.json
      schema: array
- [ ] [worker] scrape 30 articles
      output: shared/articles.json
- [ ] [verification] check both
      output: shared/validation.txt
"""
parsed = parse_plan(plan_md)
print(f"[6] parsed {len(parsed)} steps:")
for c in parsed:
    print(f"      [{c['agent_type']}] {c['description'][:40]} -> {c['output']}")
assert len(parsed) == 3

# duplicate name
dup_plan = """
- [ ] [worker] A
      output: shared/x.json
- [ ] [worker] B
      output: shared/x.json
"""
dup = parse_plan(dup_plan)
names = [_basename_no_ext(c["output"]) for c in dup]
assert len(set(names)) == 1
print(f"[7] duplicate-name detection: ok (basenames={names} - submit_plan would reject)")

# 7. submit_plan 完整路径(Mock spawner)
from tools.base import ToolContext
from tools.lead_tools import _submit_plan_handler

class MockSpawner:
    def __init__(self):
        self.session_states = {}
        self.require_plan_approval = False
    def get_session_state(self, sid):
        if sid not in self.session_states:
            self.session_states[sid] = SessionState(session_id=sid)
        return self.session_states[sid]

mock = MockSpawner()
ctx = ToolContext(worktree=".", shared_dir="./shared", session_id="t1",
                  agent_type="lead", spawner=mock)

# 7a. 重复名 → 拒绝
result_dup = _submit_plan_handler({"plan": dup_plan}, ctx)
assert result_dup["ok"] is False, result_dup
assert "unique" in result_dup["msg"].lower(), result_dup["msg"]
print(f"[8] submit_plan rejects duplicate names: {result_dup['msg'][:80]}")

# 7b. 正常 plan → 接受
result_ok = _submit_plan_handler({"plan": plan_md}, ctx)
assert result_ok["ok"] is True, result_ok
assert result_ok["registered"] == 3
assert result_ok["has_verification_step"] is True
print(f"[9] submit_plan accepts valid: {result_ok['registered']} contracts, verif_required={result_ok['has_verification_step']}")

state = mock.session_states["t1"]
assert state.plan_approved is True  # require_plan_approval=False → 自动批准
assert state.has_verification_step is True
assert "products" in state.contracts
assert "articles" in state.contracts

# 8. find_contract_for_task
cs = list(state.contracts.values())
matched = find_contract_for_task(cs, "worker", "scrape 50 products")
assert matched is not None and matched.name == "products"
print(f"[10] find_contract_for_task -> {matched.name} (output={matched.output_path})")

# format_contract_block
block = format_contract_block(matched)
assert "publish_artifact" in block
assert matched.name in block
assert "final_output" in block
print(f"[11] format_contract_block: {len(block)} chars, includes publish_artifact + name + final_output")

# 9. 审批门 (require_plan_approval=True 且未 approval)
mock2 = MockSpawner()
mock2.require_plan_approval = True
ctx2 = ToolContext(worktree=".", shared_dir="./shared", session_id="t2",
                   agent_type="lead", spawner=mock2)
# 模拟 stdin 不是 tty (CI 环境) → 自动 reject
import io
orig_stdin = sys.stdin
sys.stdin = io.StringIO("")
try:
    result_appr = _submit_plan_handler({"plan": plan_md}, ctx2)
finally:
    sys.stdin = orig_stdin
assert result_appr["ok"] is False, result_appr
assert "approve" in result_appr.get("msg", "").lower() or result_appr.get("approved") is False
print(f"[12] approval gate: non-tty stdin => rejected as expected ({result_appr.get('msg', '')[:60]})")

# 检查 spawn 的审批门
from tools.lead_tools import _check_approval_gate
state2 = mock2.session_states.get("t2")
# state2 在 reject 时被回滚
print(f"[13] state after reject: state2 exists={state2 is not None}, "
      f"approved={state2.plan_approved if state2 else 'N/A'}, "
      f"contracts={len(state2.contracts) if state2 else 0}")

ctx3 = ToolContext(worktree=".", shared_dir="./shared", session_id="t3",
                   agent_type="lead", spawner=mock2)
gate = _check_approval_gate(ctx3)
assert gate is not None and "plan_approval_required" in gate["error"]
print(f"[14] _check_approval_gate blocks spawn when no approved plan: {gate['error'][:60]}")

print()
print("✅ Batch C 全部通过")
