# Workflow.execute 实测契约（2026-09-11）

探针环境：本机 WebCross/ABCP 平台，`ws://localhost:9300/ws`，
`catalogRevision: sha256:46c89bae…`，`eventCatalogRevision: event:9e1e48fc`。
探针为只读验证，使用无 remark、无 agent 绑定的 fleet，自建 page 并在结束时关闭。

本文取代 `harness/skill/workflow.py` 头部 2026-06-26 的旧契约注释——旧注释的
三条结论有两条已不成立。

## 1. 成功路径

`Workflow.execute` **返回**（不抛），`data` 的键：

```
workflowId, taskId, status, results, variables, store, storeRevision, timing
```

- `status` = `"succeeded"`。旧注释称「no status」**已过时**。
- `results[]` 每项含 `step`（原样回显）、`stepPath`、`duration`、`status`、`result`。
- `store` 直接在返回体里，配 `storeRevision`。
- **`workflowId` 由平台生成**（uuid）。

## 2. 客户端 `runId` 被忽略

传入 `runId` 不影响返回：平台照样生成自己的 `workflowId`。
`Workflow.getStatus({"runId": ...})` **无效**。

`harness/skill/workflow.py` 旧实现用客户端 `runId` 调 `getStatus`，
是错的；必须改用平台返回/事件里的 `workflowId`。

## 3. 失败路径

`Workflow.execute` **抛** `ABCPTransportError`：

```
rpc_code = -32005
error.code = "workflow-step-failed"
rpc_data.details.failedStepPath = "steps[4]"
```

**错误体里没有 `workflowId`。** 失败后想调 `getStatus`，
只能从 `Workflow.progress` 事件里取 `workflowId`。

## 4. `getStatus` 的能力边界（重要）

`Workflow.getStatus({"workflowId": ...})` 返回：

```
workflowId, status, currentStepPath, variableKeys, resultCount, timing, lastFailure
```

- `variableKeys` **只有变量名，没有值**。
- **没有 `results` 内容**，只有 `resultCount`。

所以失败时 `getStatus` 拿不到已完成步骤的数据。部分结果只能从事件流重建。

## 5. `Workflow.progress` 事件是唯一完整的执行轨迹

每个阶段一条，`payload`：

| phase | 关键字段 |
|---|---|
| `started` | `workflowId`, `variables`, `storeRevision` |
| `step_started` | + `stepPath`, `stepType`, `action`, `status:"running"` |
| `step_finished` | + `duration`, `status:"success"\|"error"`, 失败时 `error`, `errorCode` |
| `failed` | `stepPath`, `error`, `errorCode`, `variables`, `duration` |

**每一条都携带当时的完整 `variables` 值**，以及 `storeRevision`。

结论：失败时的变量快照、已完成步骤、精确失败原因（如 `errorCode: "target-not-found"`），
全部只能从这个事件流拿。这是 ExecObserver 存在的根据，不是可选优化。

事件通过 `System.notification` 送达，信封为
`params.data = {eventId, cursor, revision, event, category, severity, persistence, source, fleetId, pageId, taskId, occurredAt, payload}`。
`Workflow.progress` 的 `fleetId/pageId/taskId` 均为 `null`，只能靠 `payload.workflowId` 关联。

## 5b. 事件窗口：`waitEvent` 看不见前一个 Action 窗口内的事件（2026-09-11 更正）

本节更正本文初版的错误说法。初版写「事件先到也能从 cursor 回放，所以不需要
pre-arm」——那是把两个不同的机制混为一谈了。

**引擎行为**（`abcp-platform/packages/workflow/src/core/engine.ts`）：

```typescript
setLatestActionWindow: (window) => {
  this.latestActionWindow = window;
  this.waitCursor = window.endCursor;   // Action 结束即跳过其窗口内所有事件
},
```

平台自带测试的标题就是 `waitEvent ignores Action-window events and waits for a
future focused event`。初版引用的 `gap-reads an event persisted while the live
subscription is being established` 是另一回事：那说的是**订阅建立期间**的空隙，
不是 Action 窗口。

所以两个步骤分工明确：

| 步骤 | 读什么 |
|---|---|
| `readEvents` | 前一个 Action **执行窗口内**产生的事件，立即返回 |
| `waitEvent` | 该窗口**之后**的未来事件，会阻塞到 timeout |

**但真机上导航仍然安全**（实测，同日）：

| 场景 | readEvents（窗口内） | waitEvent（窗口后） |
|---|---|---|
| example.com 首次访问 | — | ✅ 527ms |
| example.com 缓存重访 | — | ✅ 3ms |
| navigate → readEvents → waitEvent | **空** | ✅ 2ms |
| about:blank（最快） | **空** | ✅ 3ms |

真实 `Page.navigate` 返回时页面尚未 loaded，`Page.loaded` 落在窗口之后，所以
waitEvent 拿得到而 readEvents 是空的。平台单测构造的「窗口内」情形来自 mock
rpcHandler 同步 publish，真实浏览器不会这样。

**结论**：导航用 `waitEvent` 是对的；`readEvents` 是需要时的补充，而不是必须
前置的一步。策略层两者都接受，不强制形状。

## 5c. `DOM.axTreeUpdated`：目录里有，这个部署不发

`System.listEvents` 列出它，但实测**从未观测到**：两次完整导航加 `DOM.getAXTree`
读取，只收到 `Page.startedLoading` / `Page.navigate` / `Page.titleUpdated` /
`Page.loaded` / `Page.open`。三种等待形状全部空超时：

```
navigate → waitEvent(axTreeUpdated)              timedOut=True  6007ms
navigate → readEvents → waitEvent(axTreeUpdated)  两者皆空       4003ms
wheel    → readEvents → waitEvent(axTreeUpdated)  两者皆空       4009ms
```

因为 `waitEvent` 超时**不算失败**，等待它的步骤会静默烧掉整个 timeout（默认
30s）再带着空 events 继续。已从 `LISTENABLE_EVENTS` 移除。

**教训：事件目录里有 ≠ 这个部署会发。** 白名单只收实际观测到的。

## 6. 步骤类型：`listen` 不存在，是 `waitEvent`

| harness 写法 | 平台 | 结果 |
|---|---|---|
| `{"type":"listen","event":"Page.loaded",...}` | 无此类型 | **-32602 参数无效** |
| `{"type":"waitEvent","focus":["Page.loaded"],"timeout":N}` | `waitEvent.ts` | ✅ |
| `{"type":"store","op":"set\|merge\|append\|delete","path":..,"value":..}` | `store.ts` | ✅ |
| `{"action":..,"params":..,"purpose":..,"extract":..,"onError":..}` | `action.ts` | ✅（`type` 可省） |

`waitEvent` 用 `focus`（事件名数组）而非 `event`（单个），且作用域
（`fleetId`/`pageId`/`taskId`）默认继承上一个 Action 的作用域。

## 7. Agent 可见事件目录：26 个

`System.listEvents` 可用，返回 `eventCatalogRevision` + 每个事件的
`event` / `category` / `severity` / `description`。

分类：`blocking`(10)、`navigation`(6)、`page`(5)、`fleet`(2)、`hitl`(2)、`system`(1)。

**`Action.started/succeeded/failed` 与 `Task.*` 不在 agent 可见目录内**
——它们存在于 `control-contracts/src/events.ts` 的控制面全量里，但不对 agent 暴露。
任何「等待 Action 事件」的设计不成立。

`harness/workflow_policy.py` 的 `LISTENABLE_EVENTS` 原有 15 项全部存在于平台目录中
（无失效项）。2026-09-11 补入的是实际会发出、且等待有意义的那些：

- `Download.*`(4)、`File.operationCompleted/Failed` — 下载与文件流程
- `Page.switchTo`

**不收**（各有理由）：`DOM.axTreeUpdated`（见 5c，本部署不发）、
`Workflow.progress`（workflow 等自己的进度流）、`Fleet.ready/stopped`
（fleet 生命周期，不是段内动作的结果）。

## 8. 对实现的直接约束

1. 失败回执必须靠 `Workflow.progress` 事件重建，`getStatus` 只能作补充。
2. `workflowId` 必须从事件流捕获，不能依赖错误体。
3. 所有 `listen` 步骤（含存量 `skills/*/workflow.json` 与 `_template`）必须改为 `waitEvent`。
4. 事件白名单以 `System.listEvents` 为上界，但只收**实际观测到**的（见 5c）。
5. `readEvents` 与 `waitEvent` 是互补的两半，模型侧 schema 必须同时暴露。

## 9. 尚未闭环的缺口

本文记录的是协议探针结果，不等于 exec 已可默认启用。已知缺口：

- **失败时拿不到 store 内容与已完成 Action 的结果数据。** `Workflow.progress`
  只带 `variables` 和 `storeRevision`，不带 store 本身，也不带每步的 `result`。
  一个段用 `store.append` 收了 7 件商品、第 8 件失败时，Harness 只知道
  revision 变了，**取不回那 7 件**。这与 tau 文档要求的「失败时保留
  variables/store/results」不一致，需要平台在失败回执中返回内容或可读引用。
- `ExecObserver` 因此也只能保存步骤状态、耗时和变量。
- 真实任务 A/B 尚未进行。

在这些闭环前，`workflow_execution_enabled` 保持 canary（代码默认 False，
部署显式 opt-in）。
