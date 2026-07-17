---
name: toolify-ai-collection
description: |
  Navigate toolify.ai, click the 'browser extension' tag on the homepage, click the 'top growth' card on the resulting list page, and collect the top 10 (#1-10) products with rank, productName, activeUsersGrowth, growthRate, rating, and absolute detailUrl.
  Triggers on: domain=toolify.ai, task_type=web_scrape,
  stage_hint=collection, artifact fields ⊇ {rank, productName, activeUsersGrowth, growthRate, rating, detailUrl}.
version: 1
domain: toolify.ai
task_type: web_scrape
stage_hint: collection
fields: [rank, productName, activeUsersGrowth, growthRate, rating, detailUrl]
allow_auto_captcha: false
draft: true
generated_by: skill-create-guidance
source_task: d8d9a95665854cbca5e9a51530f1423f
source_trace: browser-001.jsonl
tested: false
---

## 状态：GUIDANCE SKILL（hints-only，/skill-create --guidance，2026-07-17）

本 skill **没有 workflow 快路径**（目录里刻意没有 workflow.json）：worker 仍自己
执行任务，skill 的价值是下方 hints 小节——由 harness 连同探针协议注进 worker
上下文，省去重复探索。hints 是**待验证假设**：agent 会先验证锚点探针，失配即
整段弃用转自由探索并上报 `guidance_stale`。

由任务 `d8d9a95665854cbca5e9a51530f1423f` 的 trace `browser-001.jsonl`
（phase `p1_collect`）蒸馏。仅在用户显式选择
（`/skill toolify-ai-collection`）时启用（skill_selection_mode=manual）。

## 页面知识（hints）

> 建议性知识（d8d9a95665854cbca5e9a51530f1423f/browser-001.jsonl, phase p1_collect 蒸馏），**非事实**：用前先验证锚点探针；失配即整段弃用转自由探索，并在结论中报告 `guidance_stale: <原因>`。

- 入口: https://www.toolify.ai（实测标题 "Best AI Tools Directory & AI Tools List - Toolify"）
- **锚点探针**: `table tr td a[href^="/tool/"]` —— 先证实此选择器在当前页命中（>0 个节点）再采信以下全部 hints；Extract structured DOM records for selector 'table tr td a[href^="/tool/"]'
- 选择器: `img`（来源 Runtime.evaluate） —— Extract structured DOM records for selector 'table tr td a[href^="/tool/"]'
- ⚠️ 过宽选择器: `table tr td a[href^="/tool/"]`（命中 200 元素但只 30 行有效——含 pricing/stats 等噪音，仅作存在性探针，**勿直接当抽取规则**）
- 已证产出: record_extraction `top_growth_products` 落盘 10 行
- 滚动: 成功运行共滚动 1 次，参数样例 {"amount": 800, "direction": "down"}
- 感知: 成功运行拉了 3 次 DOM.getAXTree（发现期用；抽取期优先定向工具 DOM.getText/getAttribute）
- 步数基线: 成功运行约 45 次工具调用；显著超出（≥2×）时把 hints 视为失效信号（见注入协议）

## 校准清单（上线前逐项确认）
- [ ] 锚点探针选择器的耐久性（它失配会让整段 hints 被弃用——选最稳的那个）。
- [ ] 删掉对 agent 没有增量价值的行（hints 要 quirk 密度，不要全）。
- [ ] 负知识是否仍然成立（页面改版后"走不通的路"可能已通）。
- [ ] 若 skills/.guidance_health.json 标了 needs_review：复核后
      `/skill-create --recheck toolify-ai-collection` 清标记。
- [ ] 全部确认后移除 frontmatter `draft: true`。
