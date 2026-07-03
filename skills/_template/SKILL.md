---
name: __FILL__-slug
description: |
  <一句话任务目标，例如：Extract reviews/pros/cons/qa from a product detail page>.
  Triggers on: domain=<host>, task_type=<web_scrape|form_filling|file_download|file_upload|web_search|general>,
  stage_hint=<collection|detail_sections|form_interaction|...>,
  artifact fields ⊇ {<field>, ...}.
version: 1
domain: <host 或 *.example.com>
task_type: <web_scrape|form_filling|file_download|file_upload|web_search|general>
stage_hint: <collection|detail_sections|form_interaction|...>
fields: [<field>, <field>]
allow_auto_captcha: false
---

## 运行指令
1. 取运行期 pageId / fleetId（来自最近 Page.getState / Page.list）。
2. 取运行期 variables：本次任务每个占位变量的实际值（见 workflow.json 的 `variables`）。
3. 调用：
   ```
   browser_call(Workflow.execute, {
     runId, pageId, fleetId,
     variables: { ...workflow.json.variables, ...<本次实际值> },
     steps: <读 workflow.json.steps>,
     errorConfig: <读 workflow.json.errorConfig>
   })
   ```
4. **持久化在 workflow 之外**：workflow 返回后读 `result.variables`，拼成一行，调 harness `record_extraction` 落盘。
   （workflow.json 的最后一步只负责把字段读进 variables，不调 record_extraction——见 skills/README.md §4。）
5. 按 fallback.yaml 的 `success_contract` 判定成功；不成立或带 error → 走兜底契约。

## 成功判据（人读版，机器判定见 fallback.yaml）
- browser_call 无 error（observation 前缀 "Workflow execution completed:"）。
- workflow 写入了 `variables_required` 列出的全部变量。
- harness 落盘后行数 ≥ 1，每行含必填字段。

## 兜底契约（人读版，结构化见 fallback.yaml takeover）
- 触发：browser_call 带 error（"Workflow execution failed: ..."）或 success_contract 不成立。
- 接管输入：`result.results[-1]`（失败步完整定义 + error）、`result.variables`（累积快照）、`result.failedStepPath`。
- agent 动作：
  1. `Page.getState` + `DOM.getAXTree` 重新感知（导航类失败后 $cache 已被引擎清空）。
  2. 以 `failedStep.step.purpose` 为语义意图锚，用全套 browser 工具继续。
  3. 完成后读 variables 拼行 → `record_extraction` 落盘。
  4. HITL：若 error 含 challenge/captcha，走 `Hitl.requestPause`（harness 既有机制），不自己 resolvePause。
- self_heal（可选）：成功后若定位路径变化，产出 workflow.json v+1，经 1 次 canary 验证后 promote。
