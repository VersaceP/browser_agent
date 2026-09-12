# Skills — 可复用任务胶囊约定（P1 contract）

> 本文定义 `skills/<slug>/` 的目录形态、三份文件的字段 schema、以及兜底契约模板。所有字段均经 ABCP 源码核实（`abcp browser/packages/{workflow,actions}/src/`）。

---

## 0. 一个 skill 是什么

一个可复用的任务胶囊，有**两个可各自缺席的层**（07-07 定案，"层不是类"）：

| 层 | 载体 | 作用 |
|----|------|------|
| **workflow**（快路径） | workflow.json + fallback.yaml | 冻结的 `Workflow.execute` 步骤序列 + 成功判据 + 兜底契约；happy-path 零页面级 LLM |
| **hints**（guidance，慢路径捷径） | SKILL.md 的 `## 页面知识（hints）` 小节 | 建议性页面知识（选择器/负知识/遮罩/步数基线），注进 worker 上下文省去重复探索（见 §8） |

- workflow 命中 → 注入运行期参数 → `browser_call(Workflow.execute, …)` 跑确定性步骤。
- workflow 失败/暂停 → 把失败步 + 累积 variables 交 BrowserAgent 接管。
- **没有 workflow.json 的目录 = hints-only（guidance）skill**：快路径直接跳过，worker 带着 hints 自己干（p1 型探索任务的正确形态）。
- p2-p5 型确定性抽取：workflow 为主，hints 可补接管知识；同一 skill 可双层兼有。

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
    ├── workflow.json      # Workflow.execute 的 steps + variables 模板
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
3. 调 harness `execute_selected_skill({pageId, fleetId, variables, rows:[]})`；runner
   从 registry 读取当前所选 skill 的冻结 recipe。慢路径不得把 workflow steps 复制进
   prompt/browser_call，也不得从 SKILL.md 散文重建它。
4. **持久化在 workflow 之外**：runner 返回后检查结构化行，由 harness/agent 调
   `record_extraction` 落盘（见 §4 持久化铁律）。
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

顶层是 `Workflow.execute` 的参数子集。**只放可冻结的部分**；`pageId/fleetId` 是运行期注入，不写进文件。

```jsonc
{
  "description": "<workflow 目标一句话>",          // 可选
  "variables": { "<var>": "" },                   // 初始变量模板，运行期被实际值覆盖
  "steps": [ /* 见 §3.1 */ ]
}
```

运行期 runner 实际调用：
```python
browser_call("Workflow.execute", {
    **workflow_json,                       # description / variables / steps
    "pageId": pageId, "fleetId": fleetId,  # 自动注入到省略的 step
    "variables": {**workflow_json["variables"], **runtime_overrides},
})
```

### 3.1 七种 step

下表与模型可见 schema 同源；`harness/workflow_schema_source.py` 从平台契约派生，
`tests/test_workflow_schema_drift.py` 在两者出现分歧时失败。

| type | 必填 | 可选 | 说明 |
|------|------|------|------|
| `action`（默认，可省 type） | `action` | `id, params, purpose, extract, onError` | 调一个 ABCP action（`Domain.action`） |
| `waitEvent` | `focus` | `id, pageId, fleetId, taskId, timeout, extract` | 等前一个 Action **之后**的事件；`focus` 见 §3.3 |
| `readEvents` | `focus` | `id, pageId, fleetId, taskId, extract` | 读前一个 Action **自身窗口**内的事件，立即返回 |
| `store` | `op, path` | `id, value`（`delete` 外 `value` 必填） | `op` ∈ `set/merge/append/delete` |
| `if` | `condition, then` | `id, else` | `condition` 见 §3.2；`then/else` 是子 step 数组 |
| `loop` | `maxIterations, condition, body` | `id` | `maxIterations` 正整数；`body` 子 step 数组 |
| `transform` | `input, ops, output` | `id` | 见 §3.5；`output` 写入一个 **flat 变量名** |

> **`action` step 没有 `timeout`，也没有 `maxRetries`。** 平台的
> `workflowActionFields` 不声明它们，而步骤联合是 `.strict()`——带上就是整个
> workflow 被 -32602 拒。唯一的单步上限是 `waitEvent.timeout`；唯一的总预算是
> 顶层 `timeout`。
>
> **`onError` ∈ `stop|continue`，没有 `retry`**，也没有地方能把 retry 挪过去
> （见 §3.4）。
>
> **`listen` 不是步骤类型**。它是 harness 的历史拼写，dispatcher 的步骤联合里
> 没有这个成员，会被 -32602 拒。存量 skill 里的 `listen` 由
> `workflow_policy._normalize_wait_events()` 在传输前改写成 `waitEvent`，但新写的
> 不要再用。
>
> **每个 action step 必写 `purpose`**——失败接管时它是 agent 的语义锚
> （`steps/action.ts` 也把它注入 params.purpose）。

### 3.2 条件（`if` / `loop` 的 condition）
```jsonc
{ "path": "$vars.<key>", "operator": "exists",
  "value": "<可选，equals/contains/gt 等需要>" }
```
operator ∈ `exists, notExists, equals, notEquals, contains, notContains, matches, gt, gte, lt, lte`。
也支持条件组：`{ "operator": "and"|"or", "conditions": [ <condition|group>, ... ] }`。

> ⚠️ **守 transform 输出的 id 别用 `exists`**：transform `find` 无命中时写**空串 `""`**，而 `exists` 对 `""` 判 true → 空 id 漏进 `Input.click` 报 "Invalid params"（联机实测踩坑）。守 id 用：
> `{ "path": "$vars.<id>", "operator": "matches", "value": "[0-9a-fA-F-]+:\\d+:\\d+" }`

### 3.3 `waitEvent` / `readEvents` 的 focus 白名单

以 `harness/workflow_policy.LISTENABLE_EVENTS` 为准（当前 22 个）：

```
Page.open            Page.close           Page.loaded          Page.startedLoading
Page.loadFailed      Page.crashed         Page.recovered       Page.navigate
Page.titleUpdated    Page.switchTo        Page.dialogOpened    Page.dialogClosed
File.chooserOpened   File.chooserClosed   File.operationCompleted
File.operationFailed
Download.waiting     Download.started     Download.progressed  Download.stateChanged
Hitl.paused          Hitl.resumed
```

> ⚠️ **`DOM.axTreeUpdated` 已从白名单移除。** 它在 `System.listEvents` 的目录里，
> 但两次完整导航 + 主动读树 + 滚轮都没观测到它发出，三种等待形状全部空超时。
> 而 `waitEvent` 超时不算失败（见 §3.1），所以等它会**静默烧掉整个 30 秒默认
> timeout** 再带着空 events 继续。在目录里 ≠ 这个部署会发。
>
> ⚠️ `Hitl.humanInput` / `Hitl.resumeEvent` **不存在于平台事件目录**——它们是
> harness 侧通知流的名字。workflow 内侦测 HITL 恢复用 `Hitl.resumed`。

### 3.4 步骤级 onError（没有 workflow 级重试）

`Workflow.execute` 只声明 `description / steps / variables / pageId / fleetId /
timeout` 六个参数。**`errorConfig` 不存在**——这个标识符在整个
`packages/workflow` 里零命中。它曾经写在这里、被 runner 发出去、被平台静默丢弃，
因为 action 的 schema 不是 `.strict()`，顶层未知参数直接剥掉不报错。
同理被丢弃的还有 `stepTimeout`：实测 `stepTimeout: 1000` 的步骤照样跑满 5005ms。

唯一的错误策略在 step 级：

```jsonc
{ "action": "...", "onError": "stop" }   // stop(默认) | continue
```

- `stop`：失败步 terminate + throw → error 信封（**触发 agent 接管的信号**）。
- `continue`：失败步记 error 但继续，整体仍可能成功（用于可选/易抖动步）。
- **没有 `retry`**。步骤联合是 `.strict()`，带 `retry` 的步骤让整个 workflow
  以 -32602 被拒。也没有地方可以把它挪过去。失败的那一步是对着一个你还没重新
  观察过的页面失败的，盲目重试本来就不对——读回执、重新观察、提交新的一段。

### 3.5 变量系统（核实自 `utils/pathResolver.ts` + `steps/action.ts`）

**插值 token**（出现在 `params` 任意字符串值，以 `$` 开头才解析）：

| token | 解析目标 | 嵌套 |
|-------|---------|------|
| `$cache.axTree.lines` / `$cache.lastResult.lines` | WorkflowCache（axTree / semanticTree / lastResult）；**AXTree 行在 `$cache.axTree.lines`**（engine internalRpc 已解包 data 层；2026-06-26 联机实测 `.lines` 命中、`.data.lines` 空。demo 的 `.data.lines` 是另一套 transport，勿照搬） | ✅ 支持点号嵌套 |
| `$last` / `$last.x` | **紧邻的上一步**的结果。engine 的 internalRpc 已解包 data 层，所以 AXTree 行是 `$last.lines`，不是 `$last.data.lines` | ✅ |
| `$store` / `$store.x` | 显式 store 快照 | ✅ |
| `$vars.<key>` / `$<key>` | 变量表 | ❌ **flat-only**：`$vars.a.b` 找的是字面量名为 `"a.b"` 的变量，不是 a 的 b 字段 |

> **没有 `$listen`，也没有 `$steps[N]`。** 引用根只有上表四个
> （`utils/pathResolver.ts:37-62`）。想引用更早的某一步，把它 `extract` 成变量。
> 这也是为什么搜索一次观察的 `transform` 必须**紧跟**那个读取步骤。
>
> ⚠️ **解析不到会抛错，不会保留字面串。** `resolvePath` 在结果为 `undefined` 时抛
> `workflow-reference-not-found`（`pathResolver.ts:62-64`），整个 workflow 终止。
> 旧版文档写的"保留字面 `$...` 串（不会变空）"是错的。
>
> 但要和另一种情况分清：**变量存在但值是空串**不会抛错。`transform` 的 `find`
> 无命中时写的正是 `""`，`$vars.<id>` 解析成功、空 id 一路进 `Input.click` 才报
> 参数错误——这就是 §3.2 那条"守 id 用 `matches` 不用 `exists`"的由来。

**变量写入的 4 个来源**：
1. 顶层 `variables`（初始模板 + 运行期覆盖）。
2. action step 的 `extract: { <varName>: "<result 内点号路径>" }`——按 step 返回值取值写入。**⚠️ 路径是对「引擎已解包的 result」取值，不带 `data.` 前缀**（联机实测：`DOM.getAXTree` 用 `lines`、`Runtime.evaluate` 返回 `{reviews,...}` 用 `reviews`、`DOM.getText` 用 `text`——都不是 `data.xxx`；internalRpc 已 `r.result?.data ?? r.result` 解包）。
3. `transform` step 的 `output`——写入该变量名。
4. 引擎 autoExtract（如从 AXTree 自动抽 exampleId）+ `pageId`/`fleetId` 自动注入每个 action step 的 params。

**所有变量值都是 scalar（string|number|boolean）**（`types/index.ts:68`）。数组/对象不能直接成为
variable。历史上的 `structured_output/json_variable` 通道依赖冻结 workflow 内的
`Runtime.evaluate`，现已不受支持；多行抽取应走 BrowserAgent 的 AXTree 枚举 + 原生批量
`DOM.getText`/`DOM.getAttribute` + `record_extraction`。

**`Runtime.evaluate` 不得出现在冻结 workflow 中**。运行期 BrowserAgent 也只能把它作为所有结构化原生读取均已失败后的只读末级手段，并显式使用 `world="isolated"`；只有 `non_dom_state` 的专用 blocker 可以由 harness 授权一次严格 main 重试，skill authoring 不得冻结这类表达式。

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
| **多行 / 结构化** | 不走冻结 workflow 快路径；由 BrowserAgent 枚举 AXTree canonical ids，批量读取文本/属性，完成有界滚动或 load-more 后调用 `record_extraction` |

> 一句话：**workflow 负责“拿到值”，harness 负责“落盘”**。workflow.json 的最后一步**不是** record_extraction，而是把字段读进 variables 的那一步。

`workflow.json.structured_output` 现由 registry 与 skill-create fail-closed 拒绝，避免把一个
必然被 frozen-workflow Runtime 策略拦截的能力继续暴露为可用契约。

---

## 5. fallback.yaml（结构化成功判据 + 接管策略）

```yaml
row_contract:                                           # workflow skill 的批量行角色声明（v1）
  version: 1
  identity_variables: [targetUrl]                       # 稳定行身份；必须属于 workflow variables
  passthrough_variables: [targetUrl, productName]       # 输入中需原样进入落盘行的变量
  produced_fields: [reviews, price]                     # workflow extract 映射后的输出字段
  variable_types: { targetUrl: uri, productName: string }

success_contract:
  workflow_no_error: true                               # browser_call 返回无 error 标志
  observation_prefix: "Workflow execution completed:"   # 成功 observation 前缀
  variables_required: [<var>, ...]                      # workflow 必须写入的 scalar 变量
  # 持久化在 workflow 之后由 harness 做，这里声明落盘后的期望：
  persisted_rows_at_least: 1
  fields_required: [<field>, ...]
  fields_nonempty: [<field>, ...]
  visual_checks: []          # 可选：VL contract_verify，如 [{type: text_present, text: "提交成功"}]
                             # 可给单条 check 加 capture：
                             # {selector: "#status"}（裁剪视觉上下文）；或 {x: 0, y: 0, width: 300, height: 80}（viewport CSS 区域）。
                             # selector-only 没有 screenshot receipt 的 canonical id + SemanticTree 几何绑定时，VL 只能给建议，不能否决成功契约。

takeover:
  on_call_error:                                        # Workflow.execute 失败 = 抛异常（见 §6）
    recover_via: exec_observer                          # ⚠️ 失败详情只在 Workflow.progress 流里，见 §6
    read: [status.failedStepPath, status.error, status.variables, status.results[-1].step]
    reobserve: [Page.getState, DOM.getAXTree]
    semantic_anchor: status.results[-1].step.purpose
  on_contract_unmet:
    from_step: len(status.results)
    reason: postcondition_unmet

hitl_boundary:
  detect: [Hitl.resumed]      # workflow 侧的 HITL 恢复事件（§3.3）
  action: listen_then_pause   # 侦测到 → 触发 pauseController 暂停，绝不在 workflow 内 resolvePause

maintenance:
  max_revision_per_failure_class: 3       # 同类失败最多修补次数
  disable_after_consecutive_failures: 3   # 连续失败 N 次自动禁用 skill
  canary_ttl_hours: 24
  auto_disable_on_challenge: false        # 遇挑战不自动禁用（环境因素非 skill 缺陷）
```

### 5.1 row_contract（批量输入的机器契约）

`row_contract` 只声明字段的**角色和比较类型**，不声明任何站点规则。它是
`skill_rows` 与上游 validated artifact 做确定性连接的唯一依据：

- `identity_variables`：一行的稳定身份，必须非空、必须同时属于
  `passthrough_variables`，且每项都是 `workflow.json.variables` 中的变量。
- `passthrough_variables`：由上游/任务输入提供并应保留到落盘行的变量；harness
  只补空缺值，绝不覆盖 Lead 明确给出的非空值。
- `produced_fields`：冻结 workflow 自己产出的字段（应用
  `success_contract.variable_to_field` 后的名字），用于区分输入与输出责任。
- `variable_types`：每个 passthrough 变量的比较语义，合法值为
  `scalar|string|integer|number|boolean|uri`。`scalar` 会把数值与等价数字字符串
  归一比较，避免 `39` 与 `"39"` 制造假冲突。

`/skill-create` 会从冻结 workflow、期望 artifact 和 validated 样例行生成并校验
该声明。带变量化 `Page.navigate` 的 workflow 若无法声明稳定 identity 或
produced fields，质量门必须失败；运行时对旧 skill 仍向后兼容，但不会替它猜测
显式 `skill_rows` 的身份或透传字段。候选源只取 validated artifacts，先当前
phase 作用域、后跨 replan 总账；任何集合不一致、重复 identity、字段冲突或多源
歧义都 fail closed。

---

## 6. 结果信封（agent 可见面**无 status 字段**；2026-06-26 联机实测）

`Workflow.execute` 经 `browser_call` 看到的是 action feedback，**没有 `status`**。成功/失败两路**形态不同**（实测）：

```
成功：browser_call 【返回】 data:{ workflowId, taskId, status:"succeeded",
                                 results, variables, store, storeRevision, timing }
失败：browser_call 【抛异常】 ABCPTransportError: -32005
      异常携带的 details 实测【只有】 { failedStepPath }
      ⚠️ 没有 workflowId / results / variables / store
```

**判成败 + 取失败详情**：
- 成功 = `browser_call` 正常返回 → 读 `data.{results, variables, store}`。成功回执
  **有** `status` 字段（旧文档说没有，是错的）。
- 失败 = `browser_call` 抛异常。`Workflow.getStatus` 需要 `workflowId`，而失败错误体里
  没有它；即使拿到了，getStatus 也只返回 `variableKeys`（变量**名**，无值）和
  `resultCount`（**数量**，无内容）。**客户端自造的 `runId` 平台根本不接受。**
- 唯一完整的失败记录是 `Workflow.progress` 通知流：每条 `step_finished` 都带完整的
  `variables` 值。harness 用 `harness/observation/exec_observer.py` 在执行期旁路记录
  这条流，失败时重建 `failedStepPath` / `failedErrorCode` / `variablesAtFailure` /
  `completedSteps`。详见 `docs/workflow-execute-live-contract.md`。

---

## 7. 校验清单（手填后自查）

- [ ] `name` == 目录名；frontmatter 命中四维（domain/task_type/stage_hint/fields）齐全。
- [ ] 每个 action step 有 `purpose`。
- [ ] 没有硬编码 AXTree id / pageId；定位走运行期重解析。
- [ ] `waitEvent`/`readEvents` 的 `focus` 都在 §3.3 白名单内（HITL 用 `Hitl.resumed`；不要等 `DOM.axTreeUpdated`）。
- [ ] 挑战边界（`if $vars.<flag> matches → waitEvent focus=[Hitl.resumed]`）的 `<flag>` **必须由一个 `Runtime.evaluate` 的 `extract` 产出**——`skill_control.make_challenge_poller` 反查这对结构，在第二连接上重跑同一段 JS 做 in-page 轮询；用别的 action 产 flag 会让 in-page 轮询**静默失效**（只剩导航级 onset）。
- [ ] `$vars.*` 引用的都是 flat 变量名，且在使用前已被写入。
- [ ] **id 守卫用 `matches "[0-9a-fA-F-]+:\d+:\d+"`，不用 `exists`**（transform 无命中写空串，exists 对空串判 true → 空 id 进 Input.click 报错；联机实测踩坑）。
- [ ] **最后一步不是 record_extraction**；落盘是 harness 后置步（§4）。
- [ ] 关键步的 step 级 `onError` 是 `stop`（让失败触发接管）；**没有 `retry`，也没有 `errorConfig`**。
- [ ] workflow.json 里没有 `runId` / `stepTimeout` / `errorConfig`——平台不接受，会被静默丢弃。
- [ ] fallback.yaml 的 success_contract 不依赖引擎内部 status。

---

## 8. hints（guidance）层——慢路径捷径

**是什么**：SKILL.md 里一个固定标题的小节（`## 页面知识（hints）`，标题匹配"页面知识"或"hints"开头的 H2），存放建议性页面知识：元素在哪（选择器）、优先用什么工具、网站怎么观察、**负知识**（哪些路走不通——如 `/comment/` 链接是噪音）、遮罩关法、滚动/停止判据、步数基线。**quirk 密度，一行一条，只结构化机器要用的部分**（frontmatter 匹配维度 + 一条锚点探针），其余保持散文——别造第二个 workflow DSL。

**怎么生效**：仅显式选择（`/skill <id>`）时，`selected_skill_context`（harness/skill/contract.py）把 hints 连同**探针协议**注进 worker 上下文：

1. hints 是**待验证假设不是事实**；
2. 采信前先验证锚点探针（hints 首条选择器）在当前页命中；
3. 探针失败/页面矛盾 → **整段弃用**转自由探索，并在最终 answer 里写 `guidance_stale: <原因>`；
4. 绝不硬凑证据迁就过期 hint——validators 才是契约。

hints-only skill 定向注入该小节（正文其余部分不占 6000 字预算）；workflow skill 整份注入不变（兜底契约仍是慢路径菜谱）。

**形态**：
- **hints-only**：目录只有 SKILL.md（刻意无 workflow.json）。快路径直接跳过（`skill.fast_path.hints_only`），不碰引擎、不记 .skill_health.json，autoheal 也不会给它蒸 workflow。适合 p1 型探索任务（lazy-load 滚动、动态停止条件）。
- **双层**：workflow skill 的 SKILL.md 补同名小节即可（`/skill-create --guidance <任务目录> <已有skill-id>` 自动蒸馏叠加）。

**防腐（独立软通道，不碰 .skill_health.json）**：worker 结束后 harness 把「结局 + 工具调用数 + answer 里的 `guidance_stale` 上报」记进 `skills/.guidance_health.json`（gitignored 运行态）。agent 报 stale 一次、或连续失败 ≥2 次 → `needs_review`（CLI 列表标 `[hints待复审]`）。**只标记、永不禁用**——显式选择绕过 health 的 07-07 语义不变。人工闭环：复核/重蒸馏后 `/skill-create --recheck <id>` 清标记。

**生成**：`/skill-create --guidance <任务目录或trace.jsonl> [skill-id] [--phase <phaseId>]`——知识蒸馏器读**完整 trace（含失败调用）**，与步骤蒸馏器目标相反：挑工具调用最多的 validated trace（探索越多知识越全），产出选择器清单/负知识/遮罩/滚动/步数基线。skill-id 已存在 → 写进其 SKILL.md（status=hints_updated）；否则新建 hints-only（status=created）。同域去重与 workflow 蒸馏共用同一裁决（optimize=叠 hints / new / quit）。
