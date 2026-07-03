# Skills — 可复用任务胶囊约定（P1 contract）

> 配套设计文档：[`docs/skill-agentic-workflow-hybrid-plan.md`](../docs/skill-agentic-workflow-hybrid-plan.md)（canonical）
> 本文是该方案 **P1（skill 约定）** 的落地物：定义 `skills/<slug>/` 的目录形态、三份文件的字段 schema、以及兜底契约模板。所有字段均经 ABCP 源码核实（`abcp browser/packages/{workflow,actions}/src/`）。
>
> 凡“怎么填”看本文，凡“为什么这么设计”看 canonical 文档。

---

## 0. 一个 skill 是什么

一个可复用的任务胶囊 = **一份冻结的 `Workflow.execute` 步骤序列（快路径）+ 成功判据 + workflow 走不下去时交给 BrowserAgent 的兜底契约（慢路径）**。

- 命中 → 注入运行期参数 → `browser_call(Workflow.execute, …)` 跑确定性步骤，happy-path 不动页面级 LLM。
- 失败/暂停 → 把失败步 + 累积 variables 交 BrowserAgent 接管。

---

## 1. 目录形态

```
skills/
├── README.md              # 本文（约定）
├── _template/             # 空骨架，复制改名即用
│   ├── SKILL.md
│   ├── workflow.json
│   └── fallback.yaml
└── <task-slug>/           # 一个具体 skill
    ├── SKILL.md           # 任务身份 + 运行指令 + 兜底契约（人/agent 可读）
    ├── workflow.json      # Workflow.execute 的 steps + variables 模板 + errorConfig
    └── fallback.yaml      # 结构化成功判据 + 接管策略（机器可判定）
```

新建 skill：`cp -r skills/_template skills/<your-slug>`，逐个填占位符（`<...>` 和 `__FILL__`）。

---

## 2. SKILL.md

```markdown
---
name: <task-slug>                  # 与目录名一致，唯一
description: |                      # 命中用：自然语言 + 触发条件
  <一句话任务目标>。
  Triggers on: domain=<host>, task_type=<web_scrape|form_filling|file_download|file_upload|web_search|general>,
  stage_hint=<collection|detail_sections|form_interaction|...>,
  artifact fields ⊇ {<field>, ...}.
version: 1                          # 整数；self-heal 回写 +1
domain: <host 或 *.example.com>     # 命中维度（精确或通配）
task_type: <web_scrape|form_filling|file_download|file_upload|web_search|general>
stage_hint: <collection|...>
fields: [<field>, ...]             # expected_artifact 字段子集
allow_auto_captcha: false          # 是否允许 VL 自动解 CAPTCHA（默认 false）
---

## 运行指令
1. 取运行期 pageId / fleetId（来自最近 Page.getState / Page.list）。
2. 取运行期 variables（每个占位 var 的实际值）。
3. 调 `browser_call(Workflow.execute, { runId, pageId, fleetId, variables, steps:<读 workflow.json>, errorConfig:<读 workflow.json> })`。
4. **持久化在 workflow 之外**：workflow 返回后读 `result.variables`，由 harness/agent 调 `record_extraction` 落盘（见 §4 持久化铁律）。
5. 按 fallback.yaml 的 success_contract 判定。

## 成功判据（见 fallback.yaml success_contract 的人读版）
- browser_call 无 error（observation 前缀 "Workflow execution completed:"）。
- 末端 record_extraction 已落盘且行数 ≥ 1，每行含必填字段。

## 兜底契约（见 fallback.yaml takeover 的人读版）
- 触发：browser_call 带 error（"Workflow execution failed: ..."）或 success_contract 不成立。
- 接管输入：result.results[-1]（失败步完整定义+error）、result.variables、result.failedStepPath。
- agent 动作：Page.getState + DOM.getAXTree 重新感知 → 以 failedStep.purpose 为语义锚继续 → 完成 → record_extraction 落盘。
```

---

## 3. workflow.json

顶层是 `Workflow.execute` 的参数子集。**只放可冻结的部分**；`runId/pageId/fleetId` 是运行期注入，不写进文件。

```jsonc
{
  "description": "<workflow 目标一句话>",          // 可选
  "variables": { "<var>": "" },                   // 初始变量模板，运行期被实际值覆盖
  "errorConfig": { "onError": "stop", "maxRetries": 1 },  // 见 §3.4
  "steps": [ /* 见 §3.1 */ ]
}
```

运行期 runner 实际调用：
```python
browser_call("Workflow.execute", {
    **workflow_json,                       # description / variables / errorConfig / steps
    "runId": runId,                        # 关联 pause/resume/progress
    "pageId": pageId, "fleetId": fleetId,  # 自动注入到省略的 step
    "variables": {**workflow_json["variables"], **runtime_overrides},
})
```

### 3.1 五种 step（字段经 `packages/workflow/src/types/schemas.ts` 核实）

| type | 必填 | 可选 | 说明 |
|------|------|------|------|
| `action`（默认，可省 type） | `action` | `id, params, purpose, extract, onError, maxRetries, timeout` | 调一个 ABCP action（`Domain.action`） |
| `if` | `condition, then` | `id, else` | `condition` 见 §3.2；`then/else` 是子 step 数组 |
| `loop` | `maxIterations, condition, body` | `id` | `maxIterations` 正整数；`body` 子 step 数组 |
| `listen` | `event` | `id, mode, filter, timeout(≥1000), onTimeout(stop\|continue), extract, listenerId` | `event` 必须在白名单（§3.3） |
| `transform` | `input, ops, output` | `id` | 见 §3.5；`output` 写入一个 **flat 变量名** |

> `action` step：`onError` ∈ `stop\|continue\|retry`；`retry` 时 `maxRetries` 0–10；`timeout`/per-step 上限 300000ms。
> **每个 action step 必写 `purpose`**——失败接管时它是 agent 的语义锚（`steps/action.ts:30` 也把它注入 params.purpose）。

### 3.2 条件（`if` / `loop` 的 condition）
```jsonc
{ "path": "$vars.<key>", "operator": "exists",
  "value": "<可选，equals/contains/gt 等需要>" }
```
operator ∈ `exists, notExists, equals, notEquals, contains, notContains, matches, gt, gte, lt, lte`。
也支持条件组：`{ "operator": "and"|"or", "conditions": [ <condition|group>, ... ] }`。

> ⚠️ **守 transform 输出的 id 别用 `exists`**：transform `find` 无命中时写**空串 `""`**，而 `exists` 对 `""` 判 true → 空 id 漏进 `Input.click` 报 "Invalid params"（联机实测踩坑）。守 id 用：
> `{ "path": "$vars.<id>", "operator": "matches", "value": "[0-9a-fA-F-]+:\\d+:\\d+" }`

### 3.3 listen 事件白名单（`utils/listenableEvents.ts`，**唯此 15 个可 listen**）
```
Page.open  Page.close  Page.loaded  Page.startedLoading  Page.loadFailed
Page.crashed  Page.recovered  Page.navigate  Page.titleUpdated
Page.dialogOpened  Page.dialogClosed
File.chooserOpened  File.chooserClosed
Hitl.resumed                      ← workflow 侧唯一 HITL 相关可 listen 事件
DOM.axTreeUpdated
```
> ⚠️ `Hitl.humanInput` / `Hitl.resumeEvent` **不在** workflow listen 白名单内——它们是 harness 侧通知流的事件，不能写进 workflow listen step。workflow 内要侦测 HITL 恢复用 `Hitl.resumed`。

### 3.4 errorConfig（`workflowRetryConfigSchema`，全部有默认值）
```jsonc
{ "onError": "stop",          // stop(默认) | continue | retry
  "maxRetries": 2,            // 0–10
  "retryDelay": 1000,         // 100–30000ms
  "backoffMultiplier": 2,     // 1–5
  "maxBackoffDelay": 10000 }  // 1000–60000ms
```
- `stop`：失败步 terminate + throw → error 信封（**触发 agent 接管的信号**）。
- `continue`：失败步记 error 但继续，整体仍可能成功（用于可选/易抖动步，写在 step 级 `onError`）。
- `retry`：重试 maxRetries 次指数退避。

### 3.5 变量系统（核实自 `utils/pathResolver.ts` + `steps/action.ts`）

**插值 token**（出现在 `params` 任意字符串值，以 `$` 开头才解析）：

| token | 解析目标 | 嵌套 |
|-------|---------|------|
| `$cache.axTree.lines` / `$cache.lastResult.lines` | WorkflowCache（axTree / semanticTree / lastResult）；**AXTree 行在 `$cache.axTree.lines`**（engine internalRpc 已解包 data 层；2026-06-26 联机实测 `.lines` 命中、`.data.lines` 空。demo 的 `.data.lines` 是另一套 transport，勿照搬） | ✅ 支持点号嵌套 |
| `$last.data.x` | 上一步结果 | ✅ |
| `$listen.x` | listener 捕获值 | ✅ |
| `$vars.<key>` / `$<key>` | 变量表 | ❌ **flat-only**：`$vars.a.b` 找的是字面量名为 `"a.b"` 的变量，不是 a 的 b 字段 |

> 解析不到时**保留字面 `$...` 串**（不会变空）——authoring 时务必确保引用的变量在该步之前已被写入。

**变量写入的 4 个来源**：
1. 顶层 `variables`（初始模板 + 运行期覆盖）。
2. action step 的 `extract: { <varName>: "<result 内点号路径>" }`——按 step 返回值取值写入。**⚠️ 路径是对「引擎已解包的 result」取值，不带 `data.` 前缀**（联机实测：`DOM.getAXTree` 用 `lines`、`Runtime.evaluate` 返回 `{reviews,...}` 用 `reviews`、`DOM.getText` 用 `text`——都不是 `data.xxx`；internalRpc 已 `r.result?.data ?? r.result` 解包）。
3. `transform` step 的 `output`——写入该变量名。
4. 引擎 autoExtract（如从 AXTree 自动抽 exampleId）+ `pageId`/`fleetId` 自动注入每个 action step 的 params。

**所有变量值都是 scalar（string|number|boolean）**（`types/index.ts:68`）。结构化/多行数据进不了 variables——见 §4。

**`Runtime.evaluate` 契约（联机实测）**：`expression` 当**函数体**跑、**必须 `return`**（裸表达式如 `1+1`/`({a:1})` 返回 null）。一发返回一个 JSON 对象，配 `extract: {reviewsText:"reviews", ...}` 把多个字段拆进 scalar 变量——是"内容全在 DOM、CSS/AXTree 取不到单容器"时的拼装首选。`Runtime.evaluate` 已从 strategy_bank `avoid_tools` 解禁（仍 cautioned，勿滥用于 DOM 工具能干的活）。

### 3.6 元素定位纪律（authoring 必守）
- **运行期重解析**：`DOM.getAXTree → transform(find+regex 取 id) → if matches(id 形) → 操作`（守卫用 `matches` 非 `exists`，见 §7 校验清单）。**绝不**把 epoch 绑定的 AXTree id / pageId 冻进 workflow.json（导航后引擎自动清 `$cache`，旧 id 必失效）。
- CSS 仅用于真正稳定的 hook；优先 role+name 文本定位。
- 导航/crash 后在 workflow 内**重跑 `DOM.getAXTree`** 再用 id。

---

## 4. 持久化铁律（record_extraction 是 harness 后置步，不是 workflow step）

**核实结论（`abcp browser` 全包零 `record_extraction`）**：`record_extraction` 是 **harness 侧 Python 工具**（`harness/tools/...`），**不是 ABCP action**。workflow 引擎的 `internalRpc` 把每个 `step.action` 当 JSON-RPC method 发给 **ABCP rpcRouter**（`core/context.ts:95`），ABCP action 全是 `Domain.action` 形态，没有 `record_extraction`。**workflow step 里写 `{"action":"record_extraction"}` 会被当成 ABCP 方法 → method not found → 失败。**

因此 structured-output 的正确通道是：

| 数据形态 | 通道 |
|---------|------|
| **定 schema 的单行**（如一个详情页的 reviews/pros/cons/qa） | workflow 用 `extract`/`transform` 把每个字段写进 **scalar variables** → workflow 返回 → **harness/agent 读 `result.variables` 拼行 → 调 `record_extraction` 落盘** |
| **多行 / 结构化** | workflow 内 `Memory.save`（ABCP 原生 action）逐行存，或走 title/base64 侧信道（`Runtime.evaluate`，见记忆 `abcp-side-channel-pattern`）→ harness 读回 → 逐行 `record_extraction` |

> 一句话：**workflow 负责“拿到值”，harness 负责“落盘”**。workflow.json 的最后一步**不是** record_extraction，而是把字段读进 variables 的那一步。

---

## 5. fallback.yaml（结构化成功判据 + 接管策略）

```yaml
success_contract:
  workflow_no_error: true                               # browser_call 返回无 error 标志
  observation_prefix: "Workflow execution completed:"   # 成功 observation 前缀
  variables_required: [<var>, ...]                      # workflow 必须写入的 scalar 变量
  # 持久化在 workflow 之后由 harness 做，这里声明落盘后的期望：
  persisted_rows_at_least: 1
  fields_required: [<field>, ...]
  fields_nonempty: [<field>, ...]
  visual_checks: []          # 可选：VL contract_verify，如 [{type: text_present, text: "提交成功"}]

takeover:
  on_call_error:                                        # Workflow.execute 失败 = 抛异常（见 §6）
    recover_via: Workflow.getStatus(runId)              # ⚠️ rich payload 不在异常里，必须二次调 getStatus
    read: [status.failedStepPath, status.error, status.variables, status.results[-1].step]
    reobserve: [Page.getState, DOM.getAXTree]
    semantic_anchor: status.results[-1].step.purpose
  on_contract_unmet:
    from_step: len(status.results)
    reason: postcondition_unmet

hitl_boundary:
  detect: [Hitl.resumed]      # workflow 侧唯一可 listen 的 HITL 事件（§3.3）
  action: listen_then_pause   # 侦测到 → 触发 pauseController 暂停，绝不在 workflow 内 resolvePause

maintenance:
  max_revision_per_failure_class: 3       # 同类失败最多修补次数
  disable_after_consecutive_failures: 3   # 连续失败 N 次自动禁用 skill
  canary_ttl_hours: 24
  auto_disable_on_challenge: false        # 遇挑战不自动禁用（环境因素非 skill 缺陷）
```

---

## 6. 结果信封（agent 可见面**无 status 字段**；2026-06-26 联机实测）

`Workflow.execute` 经 `browser_call` 看到的是 action feedback，**没有 `status`**。成功/失败两路**形态不同**（实测）：

```
成功：browser_call 【返回】 { observation:"Workflow execution completed: runId=...",
                             suggested_prompt, data:{ runId, results, variables }, taskId }
失败：browser_call 【抛异常】 ABCPTransportError: -32005 Action Workflow.execute failed
      异常携带的 error.data 实测【只有】 { observation:"Workflow execution failed: Step <action> failed: ...",
                                        suggested_prompt, method, taskId }
      ⚠️ 没有 results / variables / failedStepPath —— rich payload 不过 JSON-RPC error 边界
```

**判成败 + 取失败详情**（绝不读 `.status`）：
- 成功 = `browser_call` 正常返回 → 读 `data.{runId,results,variables}`。
- 失败 = `browser_call` 抛异常 → 用 observation 前缀 `"Workflow execution failed:"` 判定 → **二次调 `Workflow.getStatus(runId)`** 取 `{status:"error", failedStepPath, error, variables（失败时刻快照）, results[]（含每步 step+purpose+status）}`。**所以 execute 必须传稳定 runId。**

---

## 7. 校验清单（手填后自查）

- [ ] `name` == 目录名；frontmatter 命中四维（domain/task_type/stage_hint/fields）齐全。
- [ ] 每个 action step 有 `purpose`。
- [ ] 没有硬编码 AXTree id / pageId；定位走运行期重解析。
- [ ] listen `event` 都在 §3.3 白名单内（HITL 用 `Hitl.resumed`，不是 humanInput/resumeEvent）。
- [ ] 挑战边界（`if $vars.<flag> matches → listen Hitl.resumed`）的 `<flag>` **必须由一个 `Runtime.evaluate` 的 `extract` 产出**——`skill_control.make_challenge_poller` 反查这对结构，在第二连接上重跑同一段 JS 做 in-page 轮询；用别的 action 产 flag 会让 in-page 轮询**静默失效**（只剩导航级 onset）。
- [ ] `$vars.*` 引用的都是 flat 变量名，且在使用前已被写入。
- [ ] **id 守卫用 `matches "[0-9a-fA-F-]+:\d+:\d+"`，不用 `exists`**（transform 无命中写空串，exists 对空串判 true → 空 id 进 Input.click 报错；联机实测踩坑）。
- [ ] **最后一步不是 record_extraction**；落盘是 harness 后置步（§4）。
- [ ] `Workflow.execute` 传了稳定 `runId`（失败时靠 `Workflow.getStatus(runId)` 取详情，§6）。
- [ ] errorConfig.onError 关键步为 `stop`（让失败触发接管）。
- [ ] fallback.yaml 的 success_contract 不依赖引擎内部 status。
