---
name: theresanaiforthat-com-collection
description: |
  Collect the 11 trending-week products ranked 35-45 with their product names and detail-page URLs
  Triggers on: domain=theresanaiforthat.com, task_type=web_scrape,
  stage_hint=collection, artifact fields ⊇ {rank, productName, productUrl}.
version: 1
domain: theresanaiforthat.com
task_type: web_scrape
stage_hint: collection
fields: [rank, productName, productUrl]
suite: taaft-trending
allow_auto_captcha: false
draft: false
generated_by: skill-create-guidance
source_task: 5d69c57de8c0454893ea782940b97a1d
source_trace: browser-001.jsonl
tested: True
---

## 状态：GUIDANCE SKILL（hints-only，/skill-create --guidance，2026-07-07）

本 skill **没有 workflow 快路径**（目录里刻意没有 workflow.json）：worker 仍自己
执行任务，skill 的价值是下方 hints 小节——由 harness 连同探针协议注进 worker
上下文，省去重复探索。hints 是**待验证假设**：agent 会先验证锚点探针，失配即
整段弃用转自由探索并上报 `guidance_stale`。

由任务 `5d69c57de8c0454893ea782940b97a1d` 的 trace `browser-001.jsonl`
（phase `p1_collection`）蒸馏。仅在用户显式选择
（`/skill theresanaiforthat-com-collection`）时启用（skill_selection_mode=manual）。

## 页面知识（hints）

> 建议性知识（5d69c57de8c0454893ea782940b97a1d/browser-001.jsonl, phase p1_collection 蒸馏），**非事实**：用前先验证锚点探针；失配即整段弃用转自由探索，并在结论中报告 `guidance_stale: <原因>`。

- 入口: https://theresanaiforthat.com/trending/week/（实测标题 "Trending Tools"）
- **锚点探针（采集配方）**: `.tasks a[href*="/ai/"]` —— collect_items 已证抽出 80 行；先证实它在当前页命中(>0)再采信以下全部 hints
- 选择器: `img`（来源 Runtime.evaluate） —— Extract structured DOM records for selector '.tasks a[href^="https://theresanaif
- 选择器: `.tasks a[href^="https://theresanaiforthat.com/ai/"]:not([href*="/comment/"])`（来源 Runtime.evaluate） —— Extract structured DOM records for selector '.tasks a[href^="https://theresanaif
- ⚠️ 过宽选择器: `.tasks a[href^="https://theresanaiforthat.com/ai/"]:not([href*="/comment/"])`（命中 168 元素但只 60 行有效——含 pricing/stats 等噪音，仅作存在性探针，**勿直接当抽取规则**）
- 负知识: Input.click {"id": "3:821:821"} 失败：ABCP Browser call Input.click failed: -32005 Action Input.click failed（意图: dismiss_overlay: retry original action）
- 模型判断(原文摘录): I now understand the URL pattern: `https://theresanaiforthat.com/ai/{slug}/`. Let me verify that the category links don't also use /ai/ hrefs, to ensure my selector will be precise.
- 模型判断(原文摘录): The selector was too broad - it matched 80 rows including comment links (URLs with `/comment/`). Let me read the saved artifact to see all rows and filter out comment links to identify the true produc
- 模型判断(原文摘录): I can see the pattern clearly now. Comment links contain `/comment/` and there are also `free.theresanaiforthat.com` links from user mentions. Let me read the rest of the artifact to get all 80 rows.
- 模型判断(原文摘录): I now have all 80 rows. After filtering out comment links (containing `/comment/`) and mention links (on `free.theresanaiforthat.com`), I can identify 50 product links in rank order. Let me verify my 
- 已证产出: record_extraction `trending_week_ranks_35_45` 落盘 11 行
- 遮罩: Input.press Escape 有效（Try Escape to dismiss the auth overlay per dismiss ladder step 2）
- 遮罩: Input.press Escape 有效（dismiss_overlay: escape）
- 感知: 成功运行拉了 6 次 DOM.getAXTree（发现期用；抽取期优先定向工具 DOM.getText/getAttribute）
- 步数基线: 成功运行约 53 次工具调用；显著超出（≥2×）时把 hints 视为失效信号（见注入协议）

## 校准清单（上线前逐项确认）
- [ ] 锚点探针选择器的耐久性（它失配会让整段 hints 被弃用——选最稳的那个）。
- [ ] 删掉对 agent 没有增量价值的行（hints 要 quirk 密度，不要全）。
- [ ] 负知识是否仍然成立（页面改版后"走不通的路"可能已通）。
- [ ] 若 skills/.guidance_health.json 标了 needs_review：复核后
      `/skill-create --recheck theresanaiforthat-com-collection` 清标记。
- [ ] 全部确认后移除 frontmatter `draft: true`。
