---
name: taaft-detail-extract
description: |
  Extract Reviews / Pros & Cons / Q&A section text from theresanaiforthat.com product detail pages.
  Triggers on: domain=theresanaiforthat.com, task_type=web_scrape,
  stage_hint=detail_sections, artifact fields ⊇ {reviews, prosCons, qa}.
version: 2
domain: theresanaiforthat.com
task_type: web_scrape
stage_hint: detail_sections
fields: [rank, productName, detailUrl, reviews, prosCons, qa]
suite: taaft-trending
allow_auto_captcha: false
---

## 状态：v2 —— 原生 DOM 抽取（从任务 5d69c57d p2-p5 慢路径成功经验提炼，2026-07-04）

v1 用一发 `Runtime.evaluate`（heading 启发式 JS）读三段文本；任务 `5d69c57de8c0454893ea782940b97a1d` 的 p2-p5 慢路径 worker 在 **11 个真实产品页**上证明了更稳的原生做法：三个 section 落在**稳定容器 id** 里，`DOM.getText` 直读即可，**无需点 tab、无需注入 JS**。

| 部分 | v2 状态 |
|------|---------|
| 同 tab `Page.navigate` 躲 Cloudflare、`Input.press` Escape 关遮罩 | ✅ 沿用 v1（p2/p4 worker live 复确认） |
| **`DOM.getText #rw_cont`** → reviewsText | ✅ 11 产品 live：返回**评论正文全文**（v1 的 reviews 正文 TODO 就此解决；off-viewport 也能读，"not in viewport" 警告不影响取文本） |
| **`DOM.getText #pros-and-cons`** → prosConsText | ✅ 11 产品 live |
| **`DOM.getText #faq`** → qaText | ✅ 11 产品 live |
| section 缺失（如 rank 37 ai-text-converter 无 prosCons/qa） | ✅ `-32005` + `onError:continue` → 变量留空，由 `variables_any_nonempty` 契约裁决 |
| 挑战边界（`Runtime.evaluate` 测 **title-only** → `challengeFlag` → `listen Hitl.resumed`） | ✅ **故意保留 JS**（控制流边界非内容抽取，`make_challenge_poller` 依赖此表达式做第二连接 in-page 轮询）。**只测 title 不测 URL**：CF 清障后 URL 可残留 `__cf_chl_rt_tk` 而页面已正常（07-04 live 实测），URL marker 会误判进 20 分钟 Hitl.resumed 空等 |
| 页面绑定（`Page.getState` → `pageUrl`/`pageTitle`） | ✅ 原生步；harness `page_binding_mismatch` 比对 pageUrl vs detailUrl（忽略 query/尾斜杠/www），不匹配 → `wrong_page` 回落/handoff——防同 tab 批量里 navigate 软失败后**读到上一行页面**（any_nonempty 契约无法分辨）。**fail-closed**：本 skill 声明了绑定步，pageUrl 缺失（Page.getState 自身软失败）= `page_binding_unknown` 同样不放行；未声明绑定步的 skill 不受此约束 |
| record_extraction 落盘 | harness 后置步（见 §运行指令 step 4）；**artifact 名优先取 phase 的 `expected_artifact.name`**（validate_worker_artifacts 按名过滤，名字不匹配= blocking `artifact_required` → needs_fix → 快路径自我否决） |

> **v2 关键设计**：AXTree 是**发现期/接管期**的感知工具，`DOM.getText` 是**抽取期**的原生工具。三个容器 id 由 p2-p5 worker 用 `DOM.getAXTree` 感知发现、11 页验证后**钉进 workflow**；热路径不再每页拉 ~0.5MB AXTree（批量 11 页可省 ~5MB 传输），AXTree 留在接管路径（见 fallback.yaml `reobserve`）。`DOM.getText` 的 extract 路径是 `textContent`（引擎 internalRpc 已解包 `data`，同 v1 `Runtime.evaluate` 不带 `data.` 前缀的原因）。

## 运行指令
1. 取运行期 pageId / fleetId（来自最近 Page.getState / Page.list）。**pageId 复用已打开的 tab**（同 tab navigate 才躲得过 Cloudflare；批量多行时同一 tab 顺序导航，第 2 行起自动吃到暖 tab 红利）。
2. 取运行期 variables：`rank` / `productName` / `detailUrl`（来自上游 collection 阶段的某一行）。批量多行走 `worker_contract.skill_rows`（dispatch 快路径逐行循环，无需 LLM）。
3. 调用当前所选 skill 的受控 runner；禁止搜索 workflow.json 或从本文重建 steps：
   ```
   execute_selected_skill({
     pageId, fleetId,
     variables: { rank, productName, detailUrl },
     rows: []
   })
   ```
4. **持久化（workflow 之外）**：检查 runner 返回的结构化行，调 harness `record_extraction` 落盘。
   - ⚠️ harness validator 坑（recipe）：`field_provenance` 校验要 `<field>EvidenceText` + `sourceTool`/`sourceSelectorOrAxId`，不是裸 `evidence`——落盘前对齐字段名（见 `harness/task_control.py`）。
5. 按 fallback.yaml `success_contract` 判定；不成立 → 走兜底契约。

## 成功判据（人读版，机器版见 fallback.yaml）
- browser_call 无 error（observation 前缀 "Workflow execution completed:"）。
- `reviewsText` / `prosConsText` / `qaText` 至少各非空一项（三段全缺 → contract-unmet 接管；单段缺失是正常情况，如 rank 37）。

## 兜底契约（人读版，结构化见 fallback.yaml takeover）
- **failure-takeover**：非内容步骤失败（navigate/引擎级；内容三步是 `onError:continue` 不会触发）→ `Workflow.execute` **抛异常**（rich payload 不在异常里）→ 必须二次调 **`Workflow.getStatus(runId)`** 取 `status.results[-1].step`（含 purpose）+ `failedStepPath` + `variables` → agent 接管，`Page.getState`+`DOM.getAXTree` 重新感知后用 DOM 工具继续。
- **contract-unmet**：browser_call 无 error 但三段 *Text 全空（页面改版 / 容器 id 变了）→ agent 接管：`DOM.getAXTree` 重新感知 section 容器（v1 的 heading 启发式 JS 是备选发现手段），`DOM.getText` 复抽，成功后 self-heal 回写新选择器。
- HITL：若 navigate 后遇 Cloudflare/挑战 → `Hitl.requestPause`（harness 既有机制），等 `Hitl.resumed`，不自己 resolvePause。

## 版本史
- **v2.1**（2026-07-05，外部 review 三修 + 复审补封）：① 挑战检测改 title-only（URL 残留 `__cf_chl_rt_tk` 误判 → 20 分钟空等）；② 新增 `Page.getState` 页面绑定步 + harness `page_binding_mismatch` 校验（防同 tab 批量串页数据），复审补封 fail-closed：声明绑定步的 skill 缺 pageUrl = `page_binding_unknown` 不放行；③ 快路径 artifact 名优先取 `expected_artifact.name`（否则 named phase 里 `artifact_required` blocking → needs_fix → 快路径恒回落，含单行/批量/partial 三处）。
- **v2**（2026-07-04）：内容抽取从 `Runtime.evaluate` heading 启发式 JS 换成 `DOM.getText` 直读稳定容器（`#rw_cont` / `#pros-and-cons` / `#faq`），选择器来源=任务 5d69c57d p2-p5 共 11 产品 live 验证；顺带解决 v1 reviews 正文 TODO；挑战边界 JS 保留。
- **v1**（2026-06-26）：P2 参考 skill，真站 ecrett-music 联机跑通；`Runtime.evaluate` 一发取三段。
