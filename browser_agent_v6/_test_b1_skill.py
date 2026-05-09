"""B1 验证:skill 加载 + 匹配 + navigate 自动带 available_skills"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("SSL_CERT_FILE", None)
if sys.platform == "win32":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

print("="*60)
print("  B1 Skill 加载 + 匹配 + navigate 联动")
print("="*60)

# 1. 加载 skill registry
from browser.skills import auto_load, get_registry
result = auto_load()
print(f"\n[1] auto_load: {result}")
reg = get_registry()
print(f"    loaded skills: {[s.name for s in reg.list_all()]}")

# 2. URL 匹配
print("\n[2] URL 匹配 — taaft trending")
matched = reg.match_by_url("https://theresanaiforthat.com/trending/")
print(f"    matched: {[s.name for s in matched]}")
assert any(s.name == "taaft_aitools" for s in matched), "taaft_aitools 应匹配 theresanaiforthat.com"

print("\n[3] URL 匹配 — 不相关网站")
matched_other = reg.match_by_url("https://google.com")
print(f"    matched: {[s.name for s in matched_other]}")
assert len(matched_other) == 0, "google.com 不应匹配任何 skill"

# 3. 关键字匹配
print("\n[4] 关键字匹配")
matched_kw = reg.match_by_keywords("scrape trending AI tools")
print(f"    matched: {[s.name for s in matched_kw]}")

# 4. read_skill 内容
print("\n[5] read_skill 内容样本")
from browser.helpers import read_skill, list_skills
skill_md = read_skill("taaft_aitools")
print(f"    length: {len(skill_md)} chars (~{len(skill_md)//4} tokens)")
print(f"    head:\n{'-'*40}\n{skill_md[:300]}\n{'-'*40}")

# 5. navigate 自动带 available_skills(需启动 daemon)
print("\n[6] navigate 自动 available_skills")
from browser.daemon import get_daemon
from browser.helpers import set_active_port, goto
daemon = get_daemon(port=9222)
daemon.start()
set_active_port(9222)
nav = goto("https://theresanaiforthat.com/trending/", wait=2)
print(f"    nav result keys: {list(nav.keys())}")
assert "available_skills" in nav, "navigate 应自动带 available_skills"
print(f"    available_skills: {nav['available_skills']}")

# 6. 不相关 url
nav_other = goto("https://example.com")
print(f"\n[7] example.com nav keys: {list(nav_other.keys())}")
assert "available_skills" not in nav_other, "example.com 不应有 skill 提示"

print("\n✅ B1 全部通过")
