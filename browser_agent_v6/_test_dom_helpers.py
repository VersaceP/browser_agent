"""验证新增的 dom_outline / dom_query — 不烧 LLM 配额,主进程直接调。

测试目标:在 theresanaiforthat.com/trending 上跑 dom_outline,看能不能一次拿到
合理的 selector 候选(列表卡片应该在前几个里)。
"""
import os, sys
from pathlib import Path

os.environ.pop("SSL_CERT_FILE", None)
sys.path.insert(0, str(Path(__file__).parent))

from browser.daemon import get_daemon
from browser.helpers import set_active_port, goto, dom_outline, dom_query, dom_classes, page_info
import json

BROWSER_PORT = 9222

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("="*70)
print("  DOM helpers 验证 — TAAFT trending 页面")
print("="*70)

daemon = get_daemon(port=BROWSER_PORT)
daemon.start()
set_active_port(BROWSER_PORT)

print("\n[1] goto trending page...")
info = goto("https://theresanaiforthat.com/trending", wait=3)
print(f"    title = {info['title']}")
print(f"    page_info = {page_info()}")

print("\n[2a] dom_classes(min_count=10) — 看高频 className(列表项指标)")
classes = dom_classes(min_count=10, top_n=15)
print(f"     got {len(classes)} classes appearing >=10 times")
for c in classes[:15]:
    print(f"     {c['count']:4d}× .{c['class_name']:30s} | <{c['tag_sample']}> | {c['text_sample'][:35]}")

# 启发式选 likely_card_class:数量在 30-60 之间(像列表项的)
likely_class = None
for c in classes:
    if 30 <= c['count'] <= 100 and c['tag_sample'] in ('div', 'a', 'li', 'article'):
        likely_class = c['class_name']
        break
print(f"\n[2b] 启发式 likely_list_item_class = {likely_class!r}")
if likely_class:
    print(f"     dom_query('.{likely_class}', max_items=5):")
    try:
        probe = dom_query(f".{likely_class}", max_items=5)
        for i, p in enumerate(probe):
            print(f"     [{i+1}] text={p['text'][:50]!r}  href={p['href']!r}")
    except Exception as e:
        print(f"     ❌ {e}")

print("\n[3] dom_outline(max_items=15) — 看候选 selector 库")
outline = dom_outline(max_items=15)
print(f"    got {len(outline)} candidates")
for i, item in enumerate(outline[:15]):
    print(f"    [{i+1:2d}] {item['selector_hint']:40s} | {item['text_preview'][:40]}...")
    if item.get("attrs"):
        print(f"         attrs: {item['attrs']}")

# 找一个看起来像产品卡的 selector
likely_card = None
for item in outline:
    cls = item.get("classes", [])
    text = item.get("text_preview", "")
    # 启发式:有 ai_link / tool / card 类名,或者 text 看着像产品名
    if any("ai" in c.lower() or "tool" in c.lower() or "card" in c.lower() for c in cls):
        likely_card = item["selector_hint"]
        break
if not likely_card and outline:
    likely_card = outline[0]["selector_hint"]

print(f"\n[3] 启发式选 likely_card_selector = {likely_card!r}")
print(f"    dom_query('{likely_card}', max_items=5):")
try:
    probe = dom_query(likely_card, max_items=5)
    for i, p in enumerate(probe):
        print(f"    [{i+1}] text={p['text'][:50]!r} href={p['href']!r}")
        if p.get("dataset"):
            print(f"         dataset={p['dataset']}")
except Exception as e:
    print(f"    ❌ dom_query 失败: {e}")

print("\n✅ DOM helpers 验证完成 — LLM 拿到这种输出后应能写出对的批量 selector")
