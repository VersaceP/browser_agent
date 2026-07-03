---
name: taaft-detail-extract
description: |
  Extract Reviews / Pros & Cons / Q&A section text from theresanaiforthat.com product detail pages.
  Triggers on: domain=theresanaiforthat.com, task_type=web_scrape,
  stage_hint=detail_sections, artifact fields ⊇ {reviews, prosCons, qa}.
version: 1
domain: theresanaiforthat.com
task_type: web_scrape
stage_hint: detail_sections
fields: [rank, productName, detailUrl, reviews, prosCons, qa]
allow_auto_captcha: false
---

## 状态：P2 参考 skill（**真站联机抽到真数据**，2026-06-26）

真站 ecrett-music 端到端联机跑通。workflow.json 过编译版 `workflowStepSchema` + `validateWorkflowSteps`，且 `Workflow.execute` 实跑抽到真实内容。

| 部分 | 状态 |
|------|------|
| 同 tab `Page.navigate` 躲 Cloudflare、`Input.press` Escape 关遮罩 | ✅ recipe + 真站联机确认（re-navigate 不触发 Cloudflare 重挑战） |
| **`Runtime.evaluate` 一发取 Reviews/Pros&Cons/Q&A 三段文本** → extract 进 scalar 变量 | ✅ **真站联机抽到真数据**：prosConsText 1613 字、qaText 2000 字（真实内容）。**内容全在 DOM**（tab 只是锚点），不必点 tab |
| reviewsText | ⚠️ 取到评分摘要（"5.0 Average from 1 rating"+星），评论**正文**抓取需再调 JS（heading-block 启发式抓到了评分件）——次要 TODO |
| record_extraction 落盘 | harness 后置步（见 §运行指令 step 4） |

> **关键设计简化（真站驱动）**：原"定位三 tab→逐个点→读 `[role=tabpanel]`"被推翻——TAAFT 无 `[role=tabpanel]`/无 review CSS class，且内容全在 DOM。改为**一发 `Runtime.evaluate`**（按 heading 文本定位每段，返回 innerText）。**`Runtime.evaluate` 的 `expression` 是函数体、必须 `return`**（裸表达式返回 null）；**extract 路径不带 `data.` 前缀**（引擎已解包，如 `"reviews"` 非 `"data.reviews"`）。

## 运行指令
1. 取运行期 pageId / fleetId（来自最近 Page.getState / Page.list）。**pageId 复用已打开的 tab**（同 tab navigate 才躲得过 Cloudflare）。
2. 取运行期 variables：`rank` / `productName` / `detailUrl`（来自上游 collection 阶段的某一行）。
3. 调用：
   ```
   browser_call(Workflow.execute, {
     runId, pageId, fleetId,
     variables: { rank, productName, detailUrl },
     steps: <读 workflow.json.steps>,
     errorConfig: { onError: "stop", maxRetries: 1 }
   })
   ```
4. **持久化（workflow 之外）**：读 `result.variables.{reviewsText, prosConsText, qaText}`，连同 rank/productName/detailUrl 拼一行，调 harness `record_extraction` 落盘。
   - ⚠️ harness validator 坑（recipe）：`field_provenance` 校验要 `<field>EvidenceText` + `sourceTool`/`sourceSelectorOrAxId`，不是裸 `evidence`——落盘前对齐字段名（见 `harness/task_control.py`）。
5. 按 fallback.yaml `success_contract` 判定；不成立 → 走兜底契约。

## 成功判据（人读版，机器版见 fallback.yaml）
- browser_call 无 error（observation 前缀 "Workflow execution completed:"）。
- `reviewsText` / `prosConsText` / `qaText` 至少各非空一项（live-pin 前可能全空 → 触发 contract-unmet 接管，这是预期的慢路径演示）。

## 兜底契约（人读版，结构化见 fallback.yaml takeover）
- **failure-takeover**：`Runtime.evaluate`（onError:stop）失败 → `Workflow.execute` **抛异常**（rich payload 不在异常里）→ 必须二次调 **`Workflow.getStatus(runId)`** 取 `status.results[-1].step`（含 purpose）+ `failedStepPath` + `variables` → agent 接管，`Page.getState`+`DOM.getAXTree` 重新感知后用 DOM 工具/改 JS 继续。
- **contract-unmet**：browser_call 无 error 但 *Text 全空（页面结构变了 / 标题不叫 Reviews/Pros/Q&A）→ agent 接管，调整 JS heading 匹配或改用 DOM.getAXTree+getText。
- HITL：若 navigate 后遇 Cloudflare/挑战 → `Hitl.requestPause`（harness 既有机制），等 `Hitl.resumed`，不自己 resolvePause。

## 次要 TODO
- 评论**正文**抓取：当前 JS 的 heading-block 启发式对 Reviews 抓到评分件；正文需细化（如收集评分件之后到下一 heading 间的评论节点）。pros/qa 已稳。
- self-heal：JS 表达式变更经 1 次 canary 验证后回写 workflow.json v2。
