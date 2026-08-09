"""harness.skill.guidance — skill 的 hints（指导）层。

一个 skill 是分层载体而非二选一类型：
  workflow       冻结的 ABCP-native 步骤（受 Workflow 总开关保护）
  orchestration  native segments 与 Harness composite host steps 的混合计划
  hints          给 LLM 慢路径的建议性页面知识 = 本模块

目录里没有 workflow.json 的 skill 是 *hints-only*（guidance）skill：它从不碰
workflow 引擎；当 Workflow 总开关关闭时，带 workflow.json 的 skill 同样只披露
guidance。价值全在 SKILL.md 的 `## 页面知识（hints）` 小节，由
harness.skill.contract.selected_skill_context 注进 worker 上下文。hints 是
**待验证假设而非事实**：注入协议（guidance_protocol_text）要求 agent 先探针
锚点、失配即整段弃用转自由探索——这是对锚定偏差（被过期 hints 预热后硬凑
证据）的防御，不能省。

防腐不走 .skill_health.json：显式选择的 skill 完全绕过 health（07-07 用户
定调，dispatch.py:761），而 manual 模式下所有命中都是显式的，所以 guidance
的结局/步数/stale 弱信号进独立软通道（.guidance_health.json），只标记
needs_review 供 /skill-create --recheck 人工闭环——**永不禁用、永不否决**。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_GUIDANCE_HEALTH_PATH = (
    Path(__file__).resolve().parent.parent.parent / "skills" / ".guidance_health.json"
)

# SKILL.md 里 hints 小节的固定标题约定（H2）。宽松匹配"页面知识"或"hints"
# 开头的 H2，让手写（## 页面知识）和生成（## 页面知识（hints））都命中。
HINTS_HEADING = "## 页面知识（hints）"
_HINTS_HEADING_RE = re.compile(r"^##\s*(?:页面知识|hints)\b.*$", re.IGNORECASE | re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^##\s", re.MULTILINE)

# agent 报告 hints 失效的约定标记（协议文案要求写进最终 answer）。
GUIDANCE_STALE_MARKER = "guidance_stale"
_STALE_REPORT_RE = re.compile(r"guidance_stale\s*[:：]?\s*([^\n\"}]{0,160})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# hints 小节：提取 / 替换
# ---------------------------------------------------------------------------

def extract_hints_section(skill_md: str) -> str:
    """返回 SKILL.md 中 hints 小节全文（含标题行），没有则空串。

    注入定向化的实现点：guidance skill 只把这一节给 worker，正文里的
    版本史/校准清单等噪音不占 6000 字预算。"""
    text = str(skill_md or "")
    m = _HINTS_HEADING_RE.search(text)
    if m is None:
        return ""
    start = m.start()
    nxt = _NEXT_H2_RE.search(text, m.end())
    end = nxt.start() if nxt else len(text)
    return text[start:end].strip()


def replace_hints_section(skill_md: str, new_section: str) -> str:
    """把 hints 小节替换为 new_section（无则追加到文末）。new_section 需自带标题。"""
    text = str(skill_md or "")
    section = str(new_section or "").strip()
    m = _HINTS_HEADING_RE.search(text)
    if m is None:
        joiner = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + joiner + section + "\n"
    nxt = _NEXT_H2_RE.search(text, m.end())
    end = nxt.start() if nxt else len(text)
    return text[: m.start()] + section + "\n\n" + text[end:].lstrip("\n")


_FRONTMATTER_SUITE_RE = re.compile(r"^suite:.*$", re.MULTILINE)


def set_frontmatter_suite(skill_md: str, suite: str) -> str:
    """在 SKILL.md 的 YAML frontmatter 里设置/更新 `suite: <名>` 行。

    只动第一个 `---...---` 块；已有 suite 行就替换，否则插在闭合 `---` 前。
    没有合法 frontmatter 则原样返回（不硬塞，避免破坏手写文档）。"""
    text = str(skill_md or "")
    suite = str(suite or "").strip()
    if not suite or not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    head, rest = text[:end], text[end:]  # head=frontmatter 内容（不含闭合 ---）
    if _FRONTMATTER_SUITE_RE.search(head):
        return _FRONTMATTER_SUITE_RE.sub(f"suite: {suite}", head, count=1) + rest
    return head.rstrip("\n") + f"\nsuite: {suite}" + rest


def has_guidance(skill: Any) -> bool:
    """该 skill 是否携带 guidance 层（hints-only，或 SKILL.md 有 hints 小节）。"""
    if getattr(skill, "is_hints_only", False):
        return True
    return bool(extract_hints_section(str(getattr(skill, "skill_md", "") or "")))


# ---------------------------------------------------------------------------
# 注入协议（写死进上下文，与 hints 内容一起注入）
# ---------------------------------------------------------------------------

def guidance_protocol_text() -> str:
    """hints 使用协议：先探针、失配即整段弃用、失效要上报。

    英文与其余注入文案一致；`guidance_stale` 标记被 harness 弱信号通道
    （record_guidance_outcome）扫描。"""
    return (
        "GUIDANCE PROTOCOL — the hints below are UNVERIFIED HYPOTHESES distilled"
        " from past successful runs, not facts about the current page."
        " (1) Before relying on them, PROBE the anchor: verify the anchor"
        " selector/landmark from the hints against the live page (fresh"
        " DOM.getAXTree or one targeted DOM call)."
        " (2) If the probe fails, or the page contradicts any hint, DISCARD the"
        " ENTIRE hints section, continue by normal free exploration, and include"
        " `guidance_stale: <short reason>` in your final answer."
        " (3) Never force evidence to fit a hint — extraction evidence must come"
        " from the live page, and validators are the contract, hints are not."
        " (4) If the hints work, follow them instead of re-discovering the page."
    )


# ---------------------------------------------------------------------------
# 知识蒸馏（trace → 页面知识；确定性，无 LLM）
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r'title="([^"]{1,120})"')
# model 事件文本里"关于选择器/过滤/排名"的判断才留（确定性关键词门，滤掉闲聊）。
# 这些正是探索期最烧步数、成功 trace 步骤序列里不含的判断（发现 1）。
_JUDGMENT_KEYWORDS = (
    "selector", "filter", "过滤", "ordering", "order", "顺序",
    "noise", "噪音", "matched", "exclud", "binding", "定位",
)


def _is_failure(ev: Dict[str, Any]) -> bool:
    res = ev.get("result") or {}
    if not isinstance(res, dict):
        return False
    if res.get("error"):
        return True
    obs = str(((res.get("response") or {}).get("observation")) or "")
    return "failed" in obs.lower() and "Action" in obs


def _gist_params(params: Dict[str, Any], limit: int = 120) -> str:
    kept = {k: v for k, v in (params or {}).items()
            if k not in ("pageId", "fleetId", "purpose")}
    text = json.dumps(kept, ensure_ascii=False, default=str)
    return text[:limit]


def distill_guidance_from_trace(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从一条（成功）trace 提取页面知识——蒸馏目标是"知识"不是"步骤"。

    与 P3 步骤蒸馏器的本质区别：这里连**失败调用**也要（负知识：哪些路走不
    通，是探索期最烧步数、成功 trace 步骤序列里天然不含的部分）。全部确定性
    规则提取，产出给 render_hints_markdown 渲染成 SKILL.md 小节。"""
    urls: List[str] = []
    titles: List[str] = []
    selectors: List[Dict[str, str]] = []
    negative: List[str] = []
    overlay: List[str] = []
    recipes: List[Dict[str, Any]] = []       # collect_items 的已证采集配方
    broad_selectors: List[Dict[str, Any]] = []  # matched >> rows：只作存在性探针，非抽取规则
    outcomes: List[Dict[str, Any]] = []      # record_extraction 落盘产出
    judgments: List[str] = []                # model 关于选择器/过滤/排名的关键判断（原文摘录）
    scroll_count = 0
    scroll_sample: Dict[str, Any] = {}
    axtree_calls = 0
    tool_calls = 0
    max_step = 0
    seen_selectors: set = set()
    seen_negative: set = set()
    seen_recipe: set = set()

    def _add_selector(sel: str, via: str, note: str) -> None:
        sel = str(sel or "").strip()
        if sel and sel not in seen_selectors:
            seen_selectors.add(sel)
            selectors.append({"selector": sel, "via": via, "note": note[:80]})

    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")

        # --- 非 browser_call 事件：真正完成任务的"成功经验"（发现 1）---
        if etype == "collect_items":
            res = ev.get("result") or {}
            sel = str(res.get("selector") or "").strip()
            key = ("collect", sel)
            if sel and key not in seen_recipe:
                seen_recipe.add(key)
                recipes.append({"tool": "collect_items", "selector": sel,
                                "rowCount": res.get("rowCount"),
                                "mode": res.get("mode"),
                                "stopReason": res.get("stopReason")})
            continue
        if etype == "record_extraction":
            res = ev.get("result") or {}
            if str(res.get("status") or "") == "done":
                outcomes.append({"name": res.get("name"), "rowCount": res.get("rowCount")})
            continue
        if etype == "model":
            text = str(ev.get("text") or "").strip()
            if text and any(k in text for k in _JUDGMENT_KEYWORDS):
                judgments.append(text[:200])
            continue

        if etype != "browser_call":
            continue

        # --- browser_call：URL/标题/负知识/遮罩/滚动/感知/裸选择器 ---
        tool_calls += 1
        try:
            max_step = max(max_step, int(ev.get("step") or 0))
        except (TypeError, ValueError):
            pass
        method = str(ev.get("method") or "")
        params = ev.get("params") or {}
        purpose = str(params.get("purpose") or "").strip()

        if _is_failure(ev):
            err = str((ev.get("result") or {}).get("error") or "")[:120]
            key = (method, _gist_params(params, 80))
            if key not in seen_negative:
                seen_negative.add(key)
                negative.append(
                    f"{method} {_gist_params(params, 80)} 失败"
                    + (f"：{err}" if err else "")
                    + (f"（意图: {purpose[:60]}）" if purpose else "")
                )
            continue

        if method in ("Page.navigate", "Page.create"):
            url = str(params.get("url") or "").strip()
            if url and url != "about:blank" and url not in urls:
                urls.append(url)
        elif method == "Page.getState":
            obs = str((((ev.get("result") or {}).get("response") or {}).get("observation")) or "")
            m = _TITLE_RE.search(obs)
            if m and m.group(1) not in titles:
                titles.append(m.group(1))
        elif method == "DOM.getAXTree":
            axtree_calls += 1
        elif method == "Input.press" and str(params.get("key") or "") == "Escape":
            overlay.append(purpose or "Input.press Escape")
        elif method in ("Input.scroll", "Page.scroll"):
            scroll_count += 1
            scroll_sample = {k: v for k, v in params.items()
                            if k not in ("pageId", "fleetId", "purpose")}
        _add_selector(params.get("selector"), method, purpose)

    return {
        "urls": urls[:5],
        "titles": titles[:3],
        "recipes": recipes[:6],
        "broad_selectors": broad_selectors[:4],
        "selectors": selectors[:12],
        "negative": negative[:8],
        "overlay": overlay[:4],
        "judgments": judgments[:4],
        "outcomes": outcomes[:3],
        "scroll": {"count": scroll_count, "sample": scroll_sample} if scroll_count else None,
        "perception": {"axtree_calls": axtree_calls},
        "steps_baseline": {"tool_calls": tool_calls, "max_step": max_step},
    }


def knowledge_is_empty(knowledge: Dict[str, Any]) -> bool:
    """没有任何可写进 hints 的页面知识（连 URL 都没有）。"""
    return not any(knowledge.get(k) for k in
                   ("urls", "recipes", "selectors", "negative", "overlay"))


def render_hints_markdown(knowledge: Dict[str, Any], *, provenance: str = "") -> str:
    """把蒸馏出的知识渲染成 `## 页面知识（hints）` 小节（quirk 密度、一行一条）。

    第一条选择器升格为"锚点探针"——注入协议的第一步就验它。"""
    lines: List[str] = [HINTS_HEADING, ""]
    src = f"（{provenance} 蒸馏）" if provenance else ""
    lines.append(
        f"> 建议性知识{src}，**非事实**：用前先验证锚点探针；失配即整段弃用转"
        "自由探索，并在结论中报告 `guidance_stale: <原因>`。"
    )
    lines.append("")
    for url in knowledge.get("urls") or []:
        title = (knowledge.get("titles") or [None])[0]
        lines.append(f"- 入口: {url}" + (f"（实测标题 \"{title}\"）" if title else ""))

    # 采集配方（发现 1）：collect_items 是已证能抽出行的选择器。
    recipes = list(knowledge.get("recipes") or [])
    selectors = list(knowledge.get("selectors") or [])
    anchor_sel = ""
    if recipes:
        top = recipes[0]
        anchor_sel = top["selector"]
        rc = top.get("rowCount")
        lines.append(
            f"- **锚点探针（采集配方）**: `{top['selector']}` —— {top.get('tool')} 已证抽出"
            + (f" {rc} 行" if rc is not None else " 行")
            + "；先证实它在当前页命中(>0)再采信以下全部 hints"
        )
        for r in recipes[1:]:
            rc = r.get("rowCount")
            lines.append(
                f"- 采集配方: `{r['selector']}`（{r.get('tool')}"
                + (f"，{rc} 行" if rc is not None else "") + "）"
            )
    if selectors:
        # 无采集配方时，锚点退回最有区分度的裸选择器（含 class/id/属性者优先，再
        # 取最长）——`img` 这种裸标签任何页面都命中，当锚点等于没探针（5d69 教训）。
        def _specificity(item: Dict[str, str]) -> tuple:
            sel = item["selector"]
            return (any(c in sel for c in ".#["), len(sel))

        raw_anchor = None
        if not anchor_sel:
            raw_anchor = max(selectors, key=_specificity)
            anchor_sel = raw_anchor["selector"]
            lines.append(
                f"- **锚点探针**: `{raw_anchor['selector']}` —— 先证实此选择器在当前页命中"
                "（>0 个节点）再采信以下全部 hints"
                + (f"；{raw_anchor['note']}" if raw_anchor.get("note") else "")
            )
        for item in selectors:
            if item is raw_anchor or item["selector"] == anchor_sel:
                continue
            note = f" —— {item['note']}" if item.get("note") else ""
            lines.append(f"- 选择器: `{item['selector']}`（来源 {item['via']}）{note}")

    # 过宽选择器（发现 1）：matched >> rowCount，只能作存在性探针，别当抽取规则
    for b in knowledge.get("broad_selectors") or []:
        lines.append(
            f"- ⚠️ 过宽选择器: `{b['selector']}`（命中 {b.get('matchedCount')} 元素但只"
            f" {b.get('rowCount')} 行有效——含 pricing/stats 等噪音，仅作存在性探针，"
            "**勿直接当抽取规则**）"
        )
    for item in knowledge.get("negative") or []:
        lines.append(f"- 负知识: {item}")
    # 模型判断摘录（发现 1）：过滤/排名规则等探索期最烧步数的结论
    for j in knowledge.get("judgments") or []:
        lines.append(f"- 模型判断(原文摘录): {j}")
    for o in knowledge.get("outcomes") or []:
        rc = o.get("rowCount")
        lines.append(
            f"- 已证产出: record_extraction `{o.get('name')}`"
            + (f" 落盘 {rc} 行" if rc is not None else "")
        )
    for item in knowledge.get("overlay") or []:
        lines.append(f"- 遮罩: Input.press Escape 有效（{item}）")
    scroll = knowledge.get("scroll")
    if scroll:
        sample = json.dumps(scroll.get("sample") or {}, ensure_ascii=False)
        lines.append(f"- 滚动: 成功运行共滚动 {scroll['count']} 次，参数样例 {sample}")
    perception = knowledge.get("perception") or {}
    if perception.get("axtree_calls"):
        lines.append(
            f"- 感知: 成功运行拉了 {perception['axtree_calls']} 次 DOM.getAXTree"
            "（发现期用；抽取期优先定向工具 DOM.getText/getAttribute）"
        )
    baseline = knowledge.get("steps_baseline") or {}
    if baseline.get("tool_calls"):
        lines.append(
            f"- 步数基线: 成功运行约 {baseline['tool_calls']} 次工具调用；"
            "显著超出（≥2×）时把 hints 视为失效信号（见注入协议）"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 防腐软通道（独立于 .skill_health.json；只标记、永不禁用）
# ---------------------------------------------------------------------------

_NEEDS_REVIEW_CONSECUTIVE = 2


class GuidanceHealth:
    """guidance 弱信号记账：结局 + 步数 + agent 上报的 stale。

    与 SkillHealth 的根本区别：这里没有 is_disabled / 否决语义——显式选择的
    skill 用户拍板必须放行（07-07），本通道只把"疑似腐烂"聚合成 needs_review
    给人看（/skill-create --recheck、CLI 列表标记）。"""

    def __init__(self, path: Path | str = DEFAULT_GUIDANCE_HEALTH_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        except OSError:
            pass  # 弱信号 best-effort，绝不致命

    def entry(self, skill_id: str) -> Dict[str, Any]:
        return dict(self._state.get(skill_id) or {})

    def needs_review(self, skill_id: str) -> bool:
        return bool((self._state.get(skill_id) or {}).get("needs_review"))

    def record(self, skill_id: str, *, ok: bool, steps: int = 0,
               stale: bool = False, reason: str = "") -> Dict[str, Any]:
        with self._lock:
            entry = self._state.setdefault(skill_id, {
                "total_runs": 0, "total_failures": 0, "consecutive_failures": 0,
                "stale_reports": 0, "recent_steps": [], "last_reasons": [],
                "needs_review": False, "updated": None,
            })
            entry["total_runs"] = int(entry.get("total_runs", 0)) + 1
            if steps:
                recent = list(entry.get("recent_steps") or [])
                recent.append(int(steps))
                entry["recent_steps"] = recent[-10:]
            if ok and not stale:
                entry["consecutive_failures"] = 0
            else:
                if not ok:
                    entry["total_failures"] = int(entry.get("total_failures", 0)) + 1
                    entry["consecutive_failures"] = (
                        int(entry.get("consecutive_failures", 0)) + 1)
                if stale:
                    entry["stale_reports"] = int(entry.get("stale_reports", 0)) + 1
                if reason:
                    reasons = list(entry.get("last_reasons") or [])
                    reasons.append(str(reason)[:160])
                    entry["last_reasons"] = reasons[-5:]
            # agent 明确报 stale = 最高置信信号，一次即标；连续失败是兜底弱信号
            if stale or int(entry.get("consecutive_failures", 0)) >= _NEEDS_REVIEW_CONSECUTIVE:
                entry["needs_review"] = True
            entry["updated"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return dict(entry)

    def mark_reviewed(self, skill_id: str) -> None:
        """人工复审/重蒸馏后清标记（--recheck 通过、--guidance 重写 hints 时调）。"""
        with self._lock:
            if skill_id in self._state:
                self._state[skill_id].update({
                    "needs_review": False, "consecutive_failures": 0,
                    "stale_reports": 0,
                    "updated": datetime.now(timezone.utc).isoformat(),
                })
                self._save()


_singleton: Optional[GuidanceHealth] = None


def default_guidance_health() -> GuidanceHealth:
    global _singleton
    if _singleton is None:
        _singleton = GuidanceHealth()
    return _singleton


# ---------------------------------------------------------------------------
# spawner 弱信号钩子
# ---------------------------------------------------------------------------

def detect_stale_report(answer: str) -> str:
    """扫 worker 最终 answer 里的 `guidance_stale: <原因>` 标记，返回原因（无则空串）。"""
    m = _STALE_REPORT_RE.search(str(answer or ""))
    if m is None:
        return ""
    return (m.group(1) or "").strip() or "unspecified"


def record_guidance_outcome(
    registry: Any,
    worker_contract: Dict[str, Any],
    *,
    validated_ok: bool,
    fast_path_handled: bool,
    steps: int = 0,
    answer: str = "",
    health: Optional[GuidanceHealth] = None,
    logger: Any = None,
) -> Optional[Dict[str, Any]]:
    """worker 结束后记 guidance 弱信号。返回记账后的 entry，未记账返回 None。

    只在 guidance 真正驱动了慢路径时记：skill 必须由 suite 按 phase 精确路由，
    且快路径没有接管（fast_path_handled=False——workflow 快路径成功时 hints 根本
    没被消费）。直接强制单个 guidance 不记账，避免部分适配阶段污染统计。"""
    if registry is None or not isinstance(worker_contract, dict):
        return None
    if fast_path_handled:
        return None
    try:
        from harness.skill.contract import _selection_skill_id, is_suite_routed
        if not is_suite_routed(worker_contract):
            return None
        skill = registry.get(_selection_skill_id(worker_contract))
    except Exception:
        return None
    if skill is None or not has_guidance(skill):
        return None
    # Stage 匹配门（发现 2）：A 方案集合路由已保证被盖章的 skill 四维匹配该
    # phase；但单值强制会把 skill 无条件盖到只部分适配的 phase 上。若 skill 与
    # 该 phase 的 stage_hint 都声明且不等，本次运行不属于该 guidance 的适用阶段
    # ——不记账（避免不适用阶段的成败污染 needs_review）。任一方缺 stage 不拦。
    skill_stage = str(getattr(skill, "stage_hint", "") or "").strip()
    phase_stage = str(worker_contract.get("stage_hint") or "").strip()
    if skill_stage and phase_stage and skill_stage != phase_stage:
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.guidance.stage_mismatch", {
                    "skill": skill.skill_id,
                    "skill_stage": skill_stage, "phase_stage": phase_stage,
                })
            except Exception:  # pragma: no cover
                pass
        return None

    stale_reason = detect_stale_report(answer)
    health = health or default_guidance_health()
    entry = health.record(
        skill.skill_id,
        ok=validated_ok,
        steps=steps,
        stale=bool(stale_reason),
        reason=stale_reason or ("" if validated_ok else "validation_failed"),
    )
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write("skill.guidance.recorded", {
                "skill": skill.skill_id, "ok": validated_ok, "steps": steps,
                "stale": bool(stale_reason), "stale_reason": stale_reason,
                "needs_review": bool(entry.get("needs_review")),
            })
        except Exception:  # pragma: no cover
            pass
    return entry
