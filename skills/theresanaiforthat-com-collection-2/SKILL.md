---
name: theresanaiforthat-com-collection-2
description: |
  Collect the 10 trending-week products ranked 41-50 from https://theresanaiforthat.com/trending/week/, capturing each product's rank, display name, and absolute product detail-page URL.
  Triggers on: domain=theresanaiforthat.com, task_type=web_scrape,
  stage_hint=collection, artifact fields ⊇ {rank, productName, productUrl}.
version: 2
domain: theresanaiforthat.com
task_type: web_scrape
stage_hint: collection
fields: [rank, productName, productUrl]
allow_auto_captcha: false
suite: taaft-trending
draft: false
generated_by: skill-create
source_task: 858577c1b59c4177b49f63fa7be4d548
source_trace: browser-001.jsonl
tested: true
---

## 状态

这是标准 `Workflow.execute` skill；没有新增 execution mode。当前版本已通过静态契约检查，
但迁移后尚未完成 live canary，因此保持 `draft: true`、`tested: false`。运行：

`/skill-create --recheck theresanaiforthat-com-collection-2`

只有页面实跑、结构化输出解析及来源 phase 的 artifact validators 全部通过后，复检才会写入
`.skill_health.json`，并自动改为 `draft: false`、`tested: true`。`--no-test` 只做静态检查，
不会生成健康记录。

## 冻结流程

1. 从当前 phase 的 validator 派生 `targetUrl`、rank 下限/上限和精确行数。
2. 导航到 weekly trending 页面并尝试用 Escape 关闭非认证遮罩。
3. 按 DOM 顺序收集 `a.ai_link`，只保留本站、无 query/hash、路径为 `/ai/<slug>/` 的唯一链接。
4. 如果候选数不足最大 rank，循环滚动并继续合并；不猜测历史产品 URL。
5. 将候选数组 `JSON.stringify` 到标量变量 `structuredRowsJson`。
6. harness 按 DOM 顺序生成 rank、截取 phase 的 rank window、补充 provenance，随后一次性
   `record_extraction`；行数、范围、URL pattern、唯一性或持久化契约任一不满足即回落。

## 健康记账边界

- suite 精确路由：记录 workflow 的成功或确定性失败。
- 直接 `/skill theresanaiforthat-com-collection-2`：强制执行，但不记录健康结果。
- live recheck：作为冷启动 canary，只有结论明确时才记录；浏览器基础设施或 challenge
  导致无法判断时不写成功或失败。
