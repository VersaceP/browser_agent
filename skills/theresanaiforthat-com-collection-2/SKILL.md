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

该 skill 已迁移为 hints-only。旧冻结流程依赖 `Runtime.evaluate` 在页面内维护跨滚动
accumulator，无法满足当前“仅模型显式、isolated、read-only、trace-gated”的 Runtime
边界，因此 `workflow.json` 已移除。运行时由 BrowserAgent 按以下已验证页面知识执行，
并通过 artifact validators 验收。

## 页面知识（hints）

1. 从当前 phase 的 validator 派生 `targetUrl`、rank 下限/上限和精确行数。
2. 导航到 weekly trending 页面并尝试用 Escape 关闭非认证遮罩。
3. 用 `DOM.getAXTree` 枚举当前可见的 canonical ids；相关字段分别通过一次批量
   `DOM.getText` 和一次批量 `DOM.getAttribute` 读取，并按 targets 输入顺序重建行。
4. 需要滚动累积时执行有界原生循环：刷新 AXTree、枚举 canonical ids、批量读取文本/属性、
   本地去重、滚动或点击一次 load-more，再重复；不要用页面 JS accumulator。
5. 只保留本站、无 query/hash、路径为 `/ai/<slug>/` 的唯一链接；不猜测历史产品 URL。
6. 按 DOM 顺序生成 rank、截取 phase 的 rank window、补充 provenance，随后一次性
   `record_extraction`；行数、范围、URL pattern、唯一性或持久化契约任一不满足即回落。

## 健康记账边界

- suite 精确路由：记录 workflow 的成功或确定性失败。
- 直接 `/skill theresanaiforthat-com-collection-2`：强制执行，但不记录健康结果。
- live recheck：作为冷启动 canary，只有结论明确时才记录；浏览器基础设施或 challenge
  导致无法判断时不写成功或失败。
