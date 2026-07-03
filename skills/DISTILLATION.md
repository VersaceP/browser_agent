# 蒸馏：成功 trace → workflow.json（P3）

> 把一次 BrowserAgent 成功跑完的 **trace**（`traces/<worker>.jsonl`）蒸馏成一份冻结的 `workflow.json`（+ SKILL.md/fallback.yaml 骨架）。
> **发起权在人类**（§4.1：任务完成后人决定是否固化）；本文是蒸馏的规则，`_tools/distill_trace.py` 自动化其中机械部分。
> 配套约定见 [`skills/README.md`](README.md)。

## 0. trace 事件格式（核实自 `harness/diagnostics/judge_trace.py`）
JSONL，每行一个事件。蒸馏只关心两类：
- `{"type":"browser_call", "method":"<Domain.action>", "params":{...,"purpose":...}, "result":{"response":{"observation","data"}, "error"?}}` —— agent 实际执行的 ABCP action。
- `{"type":"record_extraction", "result":{...}}` —— 落盘的抽取行（**不进 workflow**，见 README §4，只用于推断要抽哪些字段）。

## 1. 蒸馏五步

### 1) 抽骨架：只留**成功的确定性 backbone**，丢探索噪声
丢弃：
- `result.error` 非空的失败调用（stale id / 误 selector 的试错）。
- 恢复类：`System.describeAction` / `System.describeEvent` / 连续重复的 `Page.getState` 探活 / `dismiss_overlay: inspect` 的反复 `DOM.getAXTree`。
- 与最终成功路径无关的回头路（同一目标的多次失败尝试，只留**最后成功的那次**）。

保留：navigate / 一次定位用 getAXTree / 成功的 click / 成功的 getText·getAttribute / 真正起作用的 `Input.press`（如 Escape 关遮罩）。

> 真实 trace 例（ecrett-music 详情页）：step4/6/7/8 是 `id="30:17557"`(2 段=stale) 的失败试错→丢；step10 `id="30:17557:17557"` 成功→留。蒸馏后只剩成功 backbone。

### 2) 剥运行期句柄
每个 action 的 `params` 删 `pageId`/`fleetId`（运行期由 `Workflow.execute` 顶层注入）。**保留 `purpose`**（接管语义锚）。

### 3) 值 → `$vars`
- 每任务变化的具体值（detailUrl、搜索词、表单值）→ 提到顶层 `variables` 模板、正文写 `$vars.<name>`。
- trace 里出现**多个同构对象**（如多个产品详情页 URL）= 该 skill 处理"一个对象"，调用方按对象循环；或上游 collection 阶段产出 variables。

### 4) **去硬编码 id**（最关键）
trace 里的 `id="30:17557:17557"` 是 epoch 绑定的，**绝不能冻进 workflow**。换成运行期重解析：
```
DOM.getAXTree
→ transform: find <label> in $cache.axTree.lines → regex 取 id → output <slug>Id
→ if { path:$vars.<slug>Id, operator:"matches", value:"[0-9a-fA-F-]+:\\d+:\\d+" }  ← 不用 exists！
    then: <原 action，id 改 $vars.<slug>Id>
```
- `<label>` 从 **purpose** 推断：`"Click Pros & Cons tab"` → label `Pros & Cons`（剥前导动词 Click/Get/Open/Read，取到停用词 tab/button/section/link/content 前）。
- `<slug>` 由 label slug 化：`Pros & Cons` → `prosCons`。
- 同一 AXTree 快照可喂多个 transform（定位多个 tab），但**点击会改 DOM**：每次 click 后若要再定位，重跑 `DOM.getAXTree`（click 不清 $cache，必须显式刷新）。
- 推不出 label 的 id → 留 `"__TODO_LOCATE__"` 占位 + 在 report 标注，人工补。

### 5) 抽取 → variables，落盘留给 harness
- trace 里"读内容"的 `DOM.getText`/`DOM.getAttribute`（purpose 含 Get/Read + section/text）→ 加 `extract: {<slug>Text: "data.text"}` 写进 scalar 变量。
- trace 里的 `record_extraction` 事件 → **不生成 workflow 步**，只在 report 里列出"返回后 harness 要落盘的字段"（README §4）。

## 2. 套约定（产出前自查，同 README §7）
- id 守卫 `matches`（非 exists）；每 action 有 purpose；listen 事件在白名单；AXTree 取行 `$cache.axTree.lines`；末步不是 record_extraction；`Workflow.execute` 传稳定 runId；关键步 `onError:"stop"`。
- 产出的 workflow.json **必须过编译版 schema 校验**（`workflowStepSchema` + `validateWorkflowSteps`）。

## 3. 工具
```
python3 skills/_tools/distill_trace.py <trace.jsonl> --slug <task-slug> [--out skills/<slug>]
```
自动做 1)–3)、4) 的 scaffold（label 从 purpose 推、加 matches 守卫）、5) 的 extract，产出 `workflow.json`(draft) + `SKILL.md`/`fallback.yaml` 骨架 + `distill_report.md`（决策 + `__TODO_LOCATE__` / 待确认 label 清单）。**draft 必经人工过一遍**（确认 label、补 live-pin 选择器、删多余步）再冻结。
