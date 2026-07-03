# P2 闭环测试计划（TAAFT 参考 skill）

> 状态：**离线 authoring + 静态校验 + 核心架构联机实测均已完成**（2026-06-26，headless 连 `ws://localhost:9300/ws`，**无需 JWT**）；TAAFT 真站内容 live-pin 待预热标签页/面板。
> 这是文档 §11 P2「架构成立的关键里程碑」的验收脚本。

## ✅ 联机实测结果（2026-06-26，核心架构成立）
- [x] **成功信封**：`Workflow.execute` 返回 `{observation:"...completed...", data:{runId,results,variables}}`，**无 status**；变量绑定/`$scope` 插值/autoExtract（exampleId）联机可用。
- [x] **onError:stop**：失败步后续步骤被 skip（实测 3 步工作流只跑 2 步）。
- [x] **失败 takeover 机制**：execute 失败**抛异常**，rich payload 不过 error 边界 → 二次调 **`Workflow.getStatus(runId)`** 成功取回 `{status:"error", failedStepPath:"1", error, variables（失败时刻快照，已抽取段不丢）, results[]（含每步 step+purpose+status）}`。**failure-takeover 可恢复，确认。**
- [x] **本 skill 完整 10 步联机跑通**（对 example.com）：navigate→listen→press→getAXTree→transform→if×3 全 success，无命中分支优雅跳过（reviews/prosCons/qaText 为空，零 spurious click）。
- [x] **修复一个联机才暴露的 bug**：`if … exists` 守不住 transform 空串输出 → 改 `matches "[0-9a-fA-F-]+:\d+:\d+"`（已修 workflow.json + template + 文档 §3.2/§7）。
- [ ] **TAAFT 真站内容抽取**：被 Cloudflare 新标签页 `Page.create` 超时（-32001）阻塞——需预热（已过 Cloudflare 的）标签页；这是下方 A.4 的 live-pin，待面板。

## 已完成（offline，无需 JWT）
- [x] **authoring**：从 task 55f5fbe6 证据 + ABCP action schema 蒸馏出 workflow.json（同 tab navigate / Escape 关遮罩 / 三 tab 运行期重解析→click→读面板）。
- [x] **静态验证**：workflow.json 过编译版 `workflowStepSchema` + 引擎 `validateWorkflowSteps`（10 步，仅用 Page.navigate/Input.press/DOM.getAXTree/Input.click/DOM.getText 真实 action）。
- [x] **契约**：SKILL.md + fallback.yaml（success_contract / failure-takeover / contract-unmet / hitl_boundary）。

## 待执行（live，需 JWT + 已打开且过了 Cloudflare 的 tab）

### A. 复用快路径（happy path）
1. 准备：一个已打开 TAAFT 的 tab（拿到 pageId/fleetId），从 leaderboard 取一行 rank/productName/detailUrl。
2. `browser_call(Workflow.execute, { runId, pageId, fleetId, variables:{rank,productName,detailUrl}, steps, errorConfig })`。
3. **验收**：observation 前缀 "Workflow execution completed:"；`result.variables` 含 reviewsText/prosConsText/qaText。
4. **live-pin**：若三段全空 → 用 DOM.getSemanticTree/VL 钉死 active `[role=tabpanel]` 真实选择器/容器 id，更新 workflow.json，重跑至非空。**这一步是 P2 真正的产出**（把离线 best-effort 选择器换成 live 验证过的耐久定位）。

### B. 持久化（harness 后置步）
5. 读 result.variables 拼行 → `record_extraction`，字段对齐 `<field>EvidenceText` + sourceTool/sourceSelectorOrAxId（recipe validator 坑）。
6. **验收**：落盘行数 ≥ 1，fields_required 非空，provenance 通过。

### C. 注入失败 → failure-takeover
7. 故意把某 tab 的 find pattern 改成不存在的标签（如 "ReviewsXYZ"）使 reviewsTabId 解析为空——但这只会 skip（if-else），不触发 error。要触发 click 失败：把 reviewsTabId 注入一个**格式合法但不存在**的 id（如 `1:99999:99999`）让 `Input.click`（onError:stop）失败。
8. **验收**：browser_call 带 error，observation 前缀 "Workflow execution failed:"，`failedStepPath` 指向该 click，`results[-1].step.purpose` == "Open the Reviews tab"。
9. agent 接管：以 purpose 为锚 Page.getState+DOM.getAXTree 重定位 → 完成 → 落盘。

### D.（可选）self-heal
10. 接管成功后产出 workflow.v2.json（把 live 钉死的 panel 定位写回），canary 跑 1 次通过 → promote 替换 v1。

### E. HITL（与面板修复一并验）
11. 若 navigate 落地遇 Cloudflare → 确认走 `Hitl.requestPause`；人类在 playground 点恢复 → 确认收到 `Hitl.resumed`（**这同时验掉延后的"恢复按钮是否真发 Hitl.resumed"**）→ resume workflow。

## 退出标准（P2 通过 = 架构成立）
A happy-path 非空抽取 + B 落盘 + C 失败接管闭合，三者全绿即认为 skill-as-container 混合架构在真实任务上成立；D/E 为加分项。
