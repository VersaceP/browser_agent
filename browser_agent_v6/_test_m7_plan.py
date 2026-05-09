"""M7 plan parser + lead_tools 单测(不调 LLM)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools.lead_tools import parse_plan, find_contract_for_task, format_contract_block

print("="*60)
print("  M7 plan parser + contract injector 单测")
print("="*60)

# Case 1:多行格式
plan = """\
# Task: scrape AI tools

- [ ] [worker] Scrape 50 trending AI tools detail pages
      output: shared/products.json
      schema: array of {name, slug, views, rating}

- [ ] [worker] Generate Excel report from products.json
      output: shared/report.xlsx

- [ ] [verification] Verify all 50 records have non-null views/rating
      output: shared/validation.txt
"""

contracts = parse_plan(plan)
print(f"\n[1] 多行格式: parsed {len(contracts)} contracts")
for c in contracts:
    print(f"  agent={c['agent_type']}, output={c['output']}, schema={c['schema']!r}")
assert len(contracts) == 3
assert contracts[0]["output"] == "shared/products.json"
assert contracts[0]["schema"] == "array of {name, slug, views, rating}"
assert contracts[2]["agent_type"] == "verification"

# Case 2:单行紧凑格式
plan2 = """\
- [ ] [worker] 抓取 → output: shared/x.json
- [ ] [worker] 处理数据 → output: shared/y.csv
"""
contracts2 = parse_plan(plan2)
print(f"\n[2] 单行紧凑: parsed {len(contracts2)} contracts")
assert len(contracts2) == 2
assert contracts2[0]["output"] == "shared/x.json"

# Case 3:contract 匹配 — 词重叠
matched = find_contract_for_task(
    contracts,
    agent_type="worker",
    task="Generate Excel report — read shared/products.json and produce report"
)
print(f"\n[3] match worker+'Generate Excel...' → output={matched['output'] if matched else None}")
assert matched and matched["output"] == "shared/report.xlsx"

# Case 4:contract 注入到 task 末尾
block = format_contract_block(matched)
print(f"\n[4] contract block:\n{block}")
assert "shared/report.xlsx" in block
assert "publish_artifact" in block
assert "name='report'" in block

# Case 5:plan 校验 — 缺 output 应报错
from tools.lead_tools import _submit_plan_handler
class FakeCtx:
    session_id = "test_session"

bad_plan = "- [ ] [worker] do something  (no output declared)"
r = _submit_plan_handler({"plan": bad_plan}, FakeCtx())
print(f"\n[5] 缺 output 校验: ok={r['ok']}, msg={r['msg'][:80]}")
assert r["ok"] is False

# Case 6:output 不以 shared/ 开头应报错
bad_plan2 = "- [ ] [worker] x\n      output: data/x.json"
r = _submit_plan_handler({"plan": bad_plan2}, FakeCtx())
print(f"\n[6] output 必须 shared/ 开头: ok={r['ok']}, msg={r['msg'][:80]}")
assert r["ok"] is False

# Case 7:正常 plan 注册
r = _submit_plan_handler({"plan": plan}, FakeCtx())
print(f"\n[7] 正常注册: ok={r['ok']}, registered={r.get('registered')}")
assert r["ok"] is True
assert r["registered"] == 3

print("\n" + "="*60)
print("  ✅ M7 plan parser + contract injector 全部通过")
print("="*60)
