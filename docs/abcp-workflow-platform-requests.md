# ABCP Workflow 引擎：问题报告与改动建议

**提出方**：harness 侧（agent 使用者）
**日期**：2026-09-11（问题 1-5）；2026-09-12 补充问题 6
**平台版本**：`catalogRevision: sha256:46c89baed493789bfb9424bc488513360a2f0047f2a530a729933a4bbe4aff12`
**涉及包**：`packages/workflow`、`packages/actions/src/domains/Workflow`

---

## 背景：我们在用 Workflow 做什么

我们把「一段确定性的浏览器操作」整体提交给 `Workflow.execute`，用来替代逐个动作往返模型。同一个表单任务的前后对比（真实运行日志）：

| | 逐动作调用 | 一次提交一段 | 变化 |
|---|---|---|---|
| 墙钟时间 | 27.8 分钟 | 14.3 分钟 | −49% |
| 模型回合 | 79 | 33 | −58% |
| 模型推理总时长 | 12.5 分钟 | 5.7 分钟 | −54% |
| 服务该任务的 worker 数 | 2（第一个步数耗尽） | 1 | — |

**这个收益完全来自 Workflow 引擎**，方向是对的。下面的问题不是反对这个方向，而是它现在还不能安全地承担更长的段。

其中最有价值的模式是「点击 → 观察 → 搜索 → 点击」全部放在一个段里完成。我们实测验证过：一个 3 级级联地区选择器（省 → 市 → 区），11 个步骤、1 次 `Workflow.execute`、7.04 秒、**中间零模型往返**：

```
steps[0]  Input.click    success 1034ms     点开触发器
steps[1]  DOM.getAXTree  success   55ms     lines=6    省级列出现
steps[2]  transform      success    4ms     → "17:23:23"   按标签搜到「广东省」
steps[3]  Input.click    success  629ms
steps[4]  DOM.getAXTree  success   48ms     lines=9    市级列出现
steps[5]  transform      success    5ms     → "17:27:27"
steps[6]  Input.click    success 3042ms
steps[7]  DOM.getAXTree  success   92ms     lines=13   区级列出现
steps[8]  transform      success    3ms     → "17:31:31"
steps[9]  Input.click    success 1827ms
steps[10] DOM.getAXTree  success  133ms

最终：button "广东省深圳市南山区"
```

同一个级联在逐动作模式下花了 **4 个独立模型回合**。

这个模式之所以通用（不依赖前端框架），是因为搜索对象是刚读回来的 AXTree，匹配依据是**人眼可见的标签**，不碰 class、不碰 DOM 结构。它的载体就是 `transform` 的 `find` 算子——**问题 2 正是关于它的**。

---

## 问题 1：`waitEvent` 超时返回 success，无法声明「必须等到」

### 现象

`packages/workflow/src/steps/waitEvent.ts:50-53`：

```ts
const result: WaitEventResult = { events, timedOut };
runContext.cache.lastResult = result;
if (step.extract) runContext.extractVariablesManual(result, step.extract);
return { status: 'success', result, duration: Date.now() - startedAt };
```

等待超时后 `timedOut: true`，但步骤状态仍是 `success`，workflow 继续执行后续步骤。

### 关键在于 `status` 和 `timedOut` 是两个不同的东西

| | 含义 | 谁在读 |
|---|---|---|
| `status: 'success'` | **这个步骤本身执行正常**（没抛异常、没崩） | **引擎**——它只看这个来决定要不要跑下一步 |
| `timedOut: true` | **等待的结果是空的** | 没有任何人被强制读 |

引擎判断「要不要继续」只看 `status`。`timedOut` 是躺在结果里的一个布尔值，**没有任何机制逼下游读它**。

所以调用方**没有办法表达「这个等待是必须的」**。这不是「信息没返回」——信息返回了；是「信息没有约束力」。

### 证据（真机探针）

```
步骤: [{"type":"waitEvent","focus":["Page.dialogOpened"],"timeout":5000}]
页面: about:blank（不可能出现 dialog）

结果: elapsed 5.16s   status=succeeded   stepDuration=5005ms   timedOut=True
      整个 workflow 终态 succeeded
```

### 危险在超时之后那几步

```
steps[0]  Page.navigate                      ← 开始导航
steps[1]  waitEvent focus=[Page.loaded]      ← 等加载完成
steps[2]  Input.click  id="7:23228:23228"    ← 点一个按钮
```

`steps[1]` 超时（页面卡住 / 网络挂了 / 导到了别的地方）后报 `success`，于是
`steps[2]` **照样对着一个从未加载完的页面执行**。那个 AX id 属于上一个页面，
点击结果是任意的——可能报 target-not-found，也可能点到了别的东西。

**而调用方拿到回执时，`steps[2]` 已经跑完了。** 事后从回执里读到
`timedOut: true` 只能做诊断，阻断不了任何事情。这就是为什么这个问题在调用方侧
无解，必须在执行器里解决。

### 另外两点

**`timedOut` 的计算是 `events.length === 0`（`waitEvent.ts:40`），不是「定时器
触发了」。** 所以还有一条路会得到同样的结果：`waitEvent.ts:31-33` 判断 workflow
总预算已耗尽时直接 `return this.complete(step, runContext, [], true, startedAt)`
—— 同样是 `success` + `timedOut: true`，但一毫秒都没等。这两种情况目前从回执上
分不出来，建议失败 details 里带上实际 `waitedMs` 加以区分。

**这条和问题 3 是叠加的。** 等一个永远不会到达的事件，现在的行为是静默烧掉整个
30 秒默认 timeout，然后带着空 events 若无其事地继续。我们就是这么踩到的。

### 影响

1. 没有任何上限约束单个步骤。我们一度在 harness 侧自造了 `stepTimeout` 参数，探针证明它**完全无效**（`stepTimeout=1000` 的步骤仍跑满 5005ms，见问题 5）。目前唯一真实的单步上限就是 `waitEvent.timeout`，而它超时不算失败。
2. 在改动落地之前，我们只能在段的写作规范里要求「必选的等待放在段末」，让超时之后不再有步骤可跑。这是规范约束，不是机制约束——模型不遵守时没有任何东西会拦住它。

### 建议改动

在 `workflowWaitEventStepSchema` 增加：

```ts
onTimeout: z.enum(['continue', 'error']).optional()
  .describe('What an expired wait means; continue returns timedOut and lets later steps run, error fails this step so a mandatory wait cannot pass unnoticed. Defaults to continue.'),
```

`waitEvent.ts` 中，超时且 `onTimeout === 'error'` 时抛出：

```ts
throw new WorkflowError(
  'workflow-wait-timeout',
  'Workflow waitEvent step expired before any awaited event arrived.',
  { focus: step.focus ?? [], timeout: step.timeout ?? DEFAULT_WAIT_TIMEOUT_MS, waitedMs },
);
```

`errors.ts` 的 `WorkflowErrorCode` 与 `WORKFLOW_ERROR_CODES` 增加 `'workflow-wait-timeout'`。

**两点说明**：

- 建议用 `.optional()` 而不是 `.default('continue')`。Zod 的 `.default()` 在生成的 JSON Schema 里会渲染成 `required`（`onError` 现在就是这样：契约显示必填，实际省略合法），默认值放在 handler 里更诚实。
- 默认 `continue` 保持向后兼容，现有 workflow 行为不变。

### 为什么不能只在调用方做

调用方拿到回执时，超时之后的步骤**已经执行完了**。回执侧检查只能做事后诊断，不能阻断。

我们目前的绕法写在段的写作规范里：**必选的等待放在段末**，让超时之后不再有步骤
可跑。这是规范约束不是机制约束——模型不遵守时没有任何东西会拦住它，所以它只是
过渡手段，不是解决方案。

---

## 问题 2：`transform` 的 `find` 没有唯一性和空结果语义（优先级最高）

### 现象

`packages/workflow/src/utils/transformRunner.ts:29-45`：

```ts
case 'find': {
  const lines = Array.isArray(current) ? current.map(String) : safeToString(current).split('\n');
  let foundLine: string | undefined;
  if (op.mode === 'regex') {
    try { const regex = new RegExp(op.pattern); foundLine = lines.find(line => regex.test(line)); }
    catch { foundLine = undefined; }
  } else {
    foundLine = lines.find(line => line.includes(op.pattern));
  }
  current = foundLine ?? '';
  break;
}
```

两个语义缺口：

1. **多匹配静默取第一个**，无唯一性检查
2. **零匹配返回空字符串**，不报错，一路传给下游

### 证据（取自一次真实运行的 AXTree，632 行，深创投投资申请表单）

| 搜索标签 | 匹配行数 | `find()` 实际选中的第一行 |
|---|---|---|
| `8`（日期选择器的「8 日」） | **470** | `rootwebarea "投资申请 - 深创投集团"` |
| `是`（单选「是」） | **4** | `genericcontainer "是否是第一次报名种子训练营"` ← **是标题，不是选项** |
| `广东省` | 2 | `link "广东省" [enabled]` ← 恰好对 |
| `深圳市` | 2 | `link "深圳市" [enabled]` ← 恰好对 |
| `南山区` | 2 | `link "南山区" [enabled]` ← 恰好对 |
| `获取资金支持` | 1 | `genericcontainer "获取资金支持" [enabled off]` ← **是文字标签，不是可点复选框** |
| `天使轮` | 1 | `textfield "天使轮" [enabled]` ← 是已填好的触发器，不是选项 |

### 影响

1. **零匹配**：`find` 返回 `''` → `regex` 对空串返回 `''` → `Input.click{id: ""}` 失败。报错落在 `steps[3]`，错误码是通用的 target 错误，而**真正的错误在 `steps[2]` 的 pattern 上**。调用方要从 `variablesAtFailure` 里看到 `optionId: ""` 才能反推。
2. **多匹配**：静默点错元素。上表的 `是` 会点在标题容器上——这类错误没有任何信号，workflow 报 `succeeded`。
3. 这个算子是「点击→观察→搜索→点击」单段模式的承重件。没有唯一性保证，这个模式就只能当 fast path，不能作为通用方法推广。

### 建议改动

**(a) `find` 增加两个可选字段**（`types/schemas.ts` 的 `transformOpSchema`）：

```ts
z.object({
  op: z.literal('find'),
  pattern: z.string().describe('Text or regular expression used to select a matching item.'),
  mode: z.enum(['contains', 'regex']).optional().describe('Matching mode; defaults to contains.'),
  require: z.enum(['first', 'unique']).optional()
    .describe('Whether several matches are acceptable; first keeps the earliest match, unique fails the step instead of guessing. Defaults to first.'),
  onEmpty: z.enum(['empty', 'error']).optional()
    .describe('Whether no match is acceptable; empty yields an empty string, error fails the step where the miss happened. Defaults to empty.'),
}).strict(),
```

行为：

- 收集**全部**匹配项（当前实现用 `lines.find` 只取第一个，改成 `lines.filter`）
- 0 个且 `onEmpty === 'error'` → 抛 `workflow-transform-empty`，details 带 `{pattern, mode, itemsScanned}`
- \>1 个且 `require === 'unique'` → 抛 `workflow-transform-ambiguous`，details 带 `{pattern, mode, matchCount, matches: 前 5 条}`
- 其余情况与现在完全一致（默认 `first` + `empty`，向后兼容）

**(b) 新增 `findAll` 算子**：

```ts
z.object({
  op: z.literal('findAll'),
  pattern: z.string().describe('Text or regular expression matched against every item.'),
  mode: z.enum(['contains', 'regex']).optional().describe('Matching mode; defaults to contains.'),
  limit: z.number().int().min(1).max(200).optional().describe('Maximum matches returned; defaults to 50.'),
}).strict(),
```

返回 `{ matches: string[], count: number, truncated: boolean, itemsScanned: number }`。

它**不替调用方做选择**——哪个候选是业务上正确的目标，只有调用方能回答。平台只负责把候选事实完整交出来。

**(c) `errors.ts` 增加两个码**：`'workflow-transform-empty'`、`'workflow-transform-ambiguous'`。

### 我们目前的绕法（可以说明这个缺口有多实在）

在平台加上这两个安全阀之前，我们只能在段的写作规范里堆约束。现在写进模型常驻
提示和 `harness/prompts/resources/browser/workflow-segments.md` 的是三条：

1. **用 `mode: "regex"` 把 role + 完整带引号的 accessible name + 方括号状态三者
   同时钉死**，不要用裸标签。实测能把一个 2 命中的标签收敛到 1 个唯一命中。
2. **这类段必须以一个能看出效果的读取结尾**，并在回执里核对。因为点对和点错在
   去看结果之前是分不出来的——多命中时 workflow 报 `succeeded`。
3. **不要用 `if` 守卫把动作步骤在空值时跳过**。跳过会让段「成功」却什么都没做，
   比失败更糟。真需要守卫时用 `matches` 测形状而不是 `exists`——空串也 `exists`。

第 3 条不是新发现：`skills/README.md` 的校验清单里早就写着「id 守卫用 `matches`
（非 `exists`），transform 无命中写空串，`exists` 对空串判 true → 空 id 进
`Input.click` 报错；联机实测踩坑」。这个坑被绕过去了，但绕法要求每个使用方都记得
绕，而且第 2 条那种「只能靠事后核对」的约束，模型不遵守时没有任何东西会拦住它。

`require: 'unique'` + `onEmpty: 'error'` 能把这三条里的两条变成机制约束。

### 我们讨论过但不建议的方案

曾考虑过一个结构化的 `findAX` 算子，用 `{role, name, enabled}` 之类的条件查询 AXTree。**不建议**，两个理由：

1. **role 不可靠**。上表最后两行就是反例：复选框在这个页面上是 `genericcontainer [enabled off]`，整页 632 行里**没有一个 `checkbox` role**（role 分布：`genericcontainer` 379、`link` 80、`listitem` 64、`svgroot` 38、`statictext` 35、`textfield` 19、`generic` 4、`heading` 4）。一个按 role 过滤的查询在这里会 0 命中。
2. 它会把平台耦合到 AXTree 的行文本格式上。

现有的 `find(mode: 'regex')` 已经能表达 role + 精确名 + 状态的组合，例如 `\] (link|option|button) "广东省" \[enabled`——我们实测这个 pattern 把 `广东省` 从 2 个匹配收敛到 **1 个唯一匹配**。**缺的不是查询语言，是 `require: 'unique'` 和 `onEmpty: 'error'` 这两个安全阀。**

---

## 问题 3：`DOM.axTreeUpdated` 在事件目录里，但未观测到发出

### 现象

`System.listEvents` 的 agent 可见事件目录里有 `DOM.axTreeUpdated`，但我们在三种触发条件下都没收到过它。

### 证据

两次完整导航 + 主动 `DOM.getAXTree` 读取 + 滚轮，收到的只有：
`Page.startedLoading` / `Page.navigate` / `Page.titleUpdated` / `Page.loaded` / `Page.open`。

三种等待形状全部空超时（4–6 秒）：

- `Page.navigate → waitEvent(DOM.axTreeUpdated)`
- `Page.navigate → readEvents → waitEvent(DOM.axTreeUpdated)`
- `Page.wheel → readEvents → waitEvent(DOM.axTreeUpdated)`

### 影响

结合问题 1：等待它的步骤会**静默烧掉整个 30 秒默认 timeout**，然后带着空 events 继续。我们已经把它从 harness 的可等待事件白名单里移除了。

### 想请确认的

1. 这个事件当前的实际触发条件是什么？（旧的事件定义描述是「目标 stale 后刷新 AXTree」）
2. 如果它只在 stale-recovery 路径上发，能否在事件目录的 description 里写明触发条件，避免使用方按名字推断？
3. 如果它已经不再发出，能否从 agent 可见目录中移除？

**结论口径**：我们目前只能说「在导航、主动读树和滚轮这三种已测触发器下未观测到」，不能断言这个部署不发。

---

## 问题 4：失败时拿不到 `store` 内容和已完成步骤的结果

### 现象

`Workflow.execute` 失败时抛 `-32005`，错误体 `details` 里**只有 `failedStepPath`**：

```json
{
  "details": { "failedStepPath": "steps[3]" },
  "error": { "code": "workflow-step-failed", "message": "A workflow step failed before the workflow completed." }
}
```

`Workflow.getStatus` 需要 `workflowId`（失败错误体里没有），而且返回的是：

- `variableKeys` —— **变量名，没有值**
- `resultCount` —— **数量，没有 results 内容**

### 影响

一个「翻页 + 逐行提取 + append 到 store」的采集段，如果在第 N 页失败，前 N−1 页已经采到的数据**完全拿不回来**。调用方只能整段重跑。

### 我们目前的绕法

订阅 `Workflow.progress` 通知流。它的每条 `step_finished` 都带完整的 `variables` 值（`engine.ts` 的 `snapshotVariables()`），所以我们在执行期间旁路记录整条流，失败时从流里重建变量值和已完成步骤。

这个绕法能用，但有两个前提：调用方必须自己订阅通知通道，且 RPC 阻塞期间通知通道必须是活的。**不是所有调用方都会这么做。**

### 建议改动

失败的错误体 `details` 里带上终态快照：

```json
{
  "details": {
    "failedStepPath": "steps[3]",
    "workflowId": "...",
    "variables": { ... },
    "store": { ... },
    "storeRevision": 4,
    "results": [ ...已完成步骤... ]
  }
}
```

这些数据在引擎里都是现成的（`this.results` / `this.store` / `this.variables`），只是没有随失败一起返回。

至少请把 **`workflowId`** 放进失败错误体——现在连事后用 `getStatus` 追查都做不到。

---

## 问题 5：生成的 JSON Schema 与运行时行为不一致（影响按契约构建的一方）

这三条不是引擎 bug，但会让按 `System.describeAction` 契约构建的一方判断错误。我们三条都踩过。

### 5.1 `.strict()` 渲染成 `additionalProperties: true`

`Workflow.execute.json` 里每个步骤联合成员都是 `"additionalProperties": true`，但 dispatcher 实际会用 `-32602` 拒绝未知步骤字段。

**代价**：我们的 schema 曾在 action step 上暴露了一个平台不存在的 `timeout` 字段。模型照着写，连续两个回合拿到 `steps.0 anyOf must satisfy an allowed shape`，第三次自己去掉才通过。**白烧 2 个模型回合 + 34K 字符的错误上下文。**

### 5.2 `.default()` 渲染成 `required`

`workflowActionFields.onError` 有 `.default('stop')`，生成的 JSON Schema 把它列进了 `required`，但省略它显然合法。按契约构建的一方会误以为每个 action step 都必须显式写 `onError`。

（这也是为什么问题 1 和问题 2 的新字段我们都建议用 `.optional()` + handler 兜底，而不是 `.default()`。）

**代价（2026-09-11 运行 a686e03f，step 29）**：模型提交了一个四步段，四步都没写 `onError`。平台会接受，我们的参数校验按生成的 schema 判 `required` 缺失，四步全报 `steps.N anyOf must satisfy an allowed shape`，**16,320 字符错误文本回灌 + 白烧一个模型回合**。我们已在 harness 侧修掉（数组元素也吃 schema 默认值），但按契约构建的下一方还会再踩一次。

### 5.3 `Workflow.execute` 的 action schema 不是 `.strict()`，顶层未知参数静默丢弃

`packages/actions/src/domains/Workflow/execute/def.ts:17` 是 `schema: z.object({...})`，没有 `.strict()`。所以顶层未知参数会被剥掉，**不报错**。

**代价**：我们自造过两个顶层参数：

- `stepTimeout` —— 探针实证完全无效（`stepTimeout=1000` 的步骤跑满 5005ms），但模型一直被要求填它，并以为每步有独立上限
- `errorConfig` —— 在整个 `packages/workflow` 里 grep **零命中**，同样静默丢弃

这种「步骤字段报错很响、顶层字段悄悄丢」的不对称，让后者活了很久才被发现。**建议给 `Workflow.execute` 的 schema 加 `.strict()`**，让两类错误一样响。

---

## 问题 6：`Workflow.execute` 吞掉嵌套 Action 的 `suggested_prompt`

**2026-09-12 修订，含现场 A/B 实验。** 本节上一版把 `Input.scroll` 超时和 `Page.wheel` 的位移/`position` 当成三个缺陷提报，经平台侧说明后**撤回其中两条**（见 6.5）。真正的缺陷在 Workflow 侧：**嵌套 Action 失败时，它自己的 `suggested_prompt` 在 RPC 回执和事件流两条通道上都不出现。**

### 6.1 平台侧说明，以及一处实测更正

平台侧 2026-09-12 说明：

- `Input.scroll` 有超时限制，我们运行的旧版本是 10s；运行 `a686e03f` step 43 的 `durationMs: 9102` 就是撞上它。
- 超时后平台会按页面情况返回对应提示；深创投这个页面判断为 transform 导致，提示是「建议改用 `Page.wheel`」。

**第二条在我们运行的这个 build 上没有复现。** 现场单调用实测（见 6.2），`Input.click` 超时后返回的建议是：

> "Inspect the achieved movement and current boundary before deciding whether a smaller bounded scroll is still needed."

整条回执里 `wheel` 出现 0 次、`transform` 出现 0 次。这是一条通用的滚动建议，不是针对 transform 页面的那条。**所以要么那条针对性提示尚未在本 build 落地，要么它走的是另一条我们没触发的路径** —— 这一点请平台侧确认。

不过这不影响本节的主结论：**不管 Action 给的是哪条建议，只要包进 workflow，建议就消失。**

### 6.2 现场 A/B 实验（决定性）

**环境**：fleet `2677c96a-7a2b-4119-bec8-2e56cf93a5cd`，page `7b53357a-ff5e-4d91-9159-3ff2edc56318`，`https://www.szvc.com.cn/apply`，表单全空。
**目标**：`textfield 7:33976:33976`，AXTree 标记 `[off]`，位于视口下方约 1000-1500 CSS px。点击它只会聚焦一个空输入框。
**变量**：只有「包不包 workflow」一个。

**A 路 —— 单条 `Input.click`**，失败，9161ms：

```json
{"observation": "Input.click failed: The Action failed with public error code \"scroll-deadline-exceeded\".",
 "suggested_prompt": "Inspect the achieved movement and current boundary before deciding whether a smaller bounded scroll is still needed.",
 "error": {"code": "scroll-deadline-exceeded",
           "message": "The Action failed with public error code \"scroll-deadline-exceeded\"."}}
```

错误码具体、建议具体，调用方照做即可。

**B 路 —— 同一个 id、同一份 params，作为 `Workflow.execute` 的唯一一步**，失败，9458ms：

```json
{"details": {"failedStepPath": "steps[0]"},
 "observation": "Workflow.execute failed: A workflow step failed before the workflow completed.",
 "suggested_prompt": "Inspect failedStepPath, the nested Action error code, and completed results before deciding whether to compensate or start a new workflow.",
 "error": {"code": "workflow-step-failed",
           "message": "A workflow step failed before the workflow completed."}}
```

B 路同时订阅了事件流（`executionTrace` 是调用方从 `Workflow.progress` 事件重建的，不在 RPC 响应里）。期间收到 4 条 `Workflow.progress`，失败步的载荷是：

```json
{"phase": "step_finished", "stepPath": "steps[0]", "stepId": "probe", "stepType": "action",
 "action": "Input.click", "status": "error", "duration": 9277,
 "errorCode": "scroll-deadline-exceeded", "error": "A nested Action failed."}
```

**把 B 路的 RPC 回执和全部 4 条事件加在一起做关键词统计**：

| 关键词 | B 路（RPC + 事件）出现次数 |
|---|---:|
| `scroll-deadline-exceeded` | 2（两条事件的 `errorCode`） |
| `"A nested Action failed."` | 2（两条事件的 `error`，占位串） |
| `suggested_prompt` | 1（workflow 级通用建议） |
| **`achieved movement`**（A 路建议的关键词） | **0** |
| **`bounded scroll`**（A 路建议的关键词） | **0** |

**结论**：嵌套 Action 的 `suggested_prompt` 在 workflow 路径上完全消失，两条通道都没有。调用方能拿到的只有一个裸 `errorCode` 和占位串 `"A nested Action failed."`——嵌套 Action 自己的 `observation`、`suggested_prompt`、`error.message` 全部没有出口。

### 6.3 日志侧佐证与真实代价

**对照样本**（运行 `0abaf513`，单条 `Input.select` 失败）证明这不是 `scroll-deadline` 独有的：

```json
{"observation": "Input.select failed: The target identity is stale for the current document.",
 "suggested_prompt": "Discard the stale element ID, refresh DOM.getAXTree, and use the replacement ID from the current document.",
 "error": {"code": "stale-target", "message": "..."}}
```

单调用一律带动作级、错误码级的具体建议。

**真实代价**（运行 `a686e03f` step 43）：模型收到的那条 tool_result 全文 22,568 字符，`scroll-deadline-exceeded` 出现 3 次、`wheel` 出现 **0** 次。模型只好在 step 45 自己连调 `System.describeAction(Page.wheel)` 和 `System.describeAction(Input.scroll)` 读 schema，再盲滚三次（15.4s + 15.4s + 8.4s = 39.2s，其间滚过头 270px 还要回修）。**一次本可一回合恢复的失败变成了 4 回合、39.2 秒。**

### 6.4 建议

| # | 建议 |
|---|---|
| 1 | `Workflow.progress` 的 `step_finished` / `failed` 事件保留嵌套 Action 自己的 `observation`、`suggested_prompt` 与 `error.message`，不要替换成 `"A nested Action failed."` |
| 2 | 顶层 `rpcData` 在只有一个失败步时把嵌套建议一并给出（拼进 `suggested_prompt`，或放 `details.nestedSuggestion`） |
| 3 | 最低限度：至少透传嵌套 `error.message`，让调用方不必只靠错误码反查 |
| 4 | 确认 6.1 里那条「transform 页面建议改用 `Page.wheel`」的提示是否已在当前 build 落地 |

前两条落地后，这类失败对调用方就是一回合的事：读建议 → 换 `Page.wheel` → 继续。

### 6.5 已撤回：`Page.wheel` 的位移与 `position`

上一版把下面两条列为缺陷，经平台侧说明后撤回，我们的判断有误：

| 上一版的说法 | 平台侧说明 | 结论 |
|---|---|---|
| `data.position` 恒为 `{0,0}`，读不到滚动偏移 | 模拟滚轮，页面实际没有移动，`{0,0}` 是正确结果；回执返回了多个指标，不应只看 `position` | **撤回** |
| 请求 1300 实走 720、请求 700 实走 720，位移不受控 | `Page.wheel` 的提示词已明确不保证精确距离；鼠标滚轮每一码的位移受多种因素影响，无法精确修正，除非用代码直接设置位置——那就不是模拟滚轮了。1300→720 大概率是该站点自身的模拟滚动实现所致 | **撤回** |

保留这段记录只为说明 6.3 里「滚过头 270px」的由来：那是使用模拟滚轮 API 的固有成本，不是 bug。也正因为它是固有成本，6.2 那条被吞掉的建议才更要紧——平台本来就不该让调用方走到盲滚这一步。

**顺带一个观察，未提为缺陷**：实验中两次用 `Page.wheel(scrollY: -6000)` 做视口归位，都返回 `-32005 scroll-target-changed-after-input`。不影响本实验（两臂起点一致、目标 id 相同），记录在此供平台侧参考。

### 6.6 二次实测：同一缺陷在一次成功运行里吃掉 39% 墙钟

运行 `d5b920de`（2026-09-12，同一张深创投表单，最终 `status: done`、6 行全部验证通过、
无 HITL、无验证码），仍然被同一个缺陷打中：

```
turn 29  48.0s  execute_browser_workflow → failed scroll-deadline-exceeded
                （编写 6 步，只执行 1 步，onError: stop）
turn 30  12.5s  System.describeAction ×2   （模型去读 Page.wheel / Input.scroll 的 schema）
turn 31  15.1s  Page.wheel
turn 32   5.2s  DOM.getAXTree
turn 33   9.9s  Page.wheel
turn 34   5.6s  search_harness_guides
turn 35  12.5s  browser_call
turn 36  11.9s  DOM.getAXTree
turn 37  28.5s  Input.scroll
turn 38  21.8s  browser_call
turn 39   3.1s  DOM.getAXTree
turn 40   5.5s  Input.click
─────────────────────────────────
turn 29-40 合计 179.7s
```

视口对位之后，turn 41 / 42 两个段**共 14.4s** 就完成了段 3 原本要做的全部工作。

**净浪费约 170s，占该次运行总墙钟 439.8s 的 39%。** 调用方这一侧没有任何办法规避：
guide 明令禁止预滚（6.1），而 reveal 超时后的建议又被 workflow 吞掉（6.2）。

### 6.7 复现

探针脚本（调用方侧，非平台代码）：

| 脚本 | 作用 |
|---|---|
| `scratchpad/probe_nested_hint_live.py` | A/B 两臂：单条 `Input.click` vs 同一点击包成一步 workflow |
| `scratchpad/probe_b_with_events.py` | B 路重跑并订阅 `Workflow.progress`，证明事件流里同样没有嵌套建议 |

实验后已复核页面状态：URL 未变、`status: ready`、带值表单节点 0 个、无 focus 节点 —— 三次点击全部在 reveal 阶段超时，未落到元素上，零状态变更。


---

## 优先级建议

| | 问题 | 类型 | 理由 |
|---|---|---|---|
| 1 | 问题 2：`find` 的 `require` / `onEmpty` / `findAll` | 正确性 | 单段「观察→搜索→点击」的承重件；现在会静默点错元素 |
| 2 | 问题 1：`waitEvent` 的 `onTimeout: 'error'` | 正确性 | 调用方侧无法补救（超时后续步骤已执行） |
| 3 | 问题 6：嵌套 Action 的 `suggested_prompt` 被 workflow 吞掉 | 正确性 | **已用现场 A/B 实验证实**：同一目标同一点击，单调用带具体建议，包进 workflow 后 RPC 与事件流都只剩裸错误码；本次因此多烧 4 回合 39.2s。与问题 4「失败回执带终态快照」同源，建议一起做 |
| 4 | 问题 4：失败回执带终态快照 | 数据完整性 | 至少先带 `workflowId` |
| 5 | 问题 5.3：`Workflow.execute` schema 加 `.strict()` | 契约 | 改动最小，能杜绝一整类静默错误 |
| 6 | 问题 3：`DOM.axTreeUpdated` 触发条件 | 文档/确认 | 先确认行为，再决定改目录还是改文档 |
| 7 | 问题 5.1 / 5.2：JSON Schema 生成失真 | 契约 | 影响所有按契约构建的一方 |

问题 1、2 我们都是**向后兼容设计**：新字段全部可选，默认值即现有行为，存量 workflow 不受影响。

---

## 附：复现材料

| 内容 | 位置 |
|---|---|
| 前后对比的两次完整运行日志 | `worktree/de60b7f453aa4f0d8c9857a302f3a5a7/`、`worktree/8208ed493a334d8d904ed800b2205d52/` |
| 上表的 AXTree（632 行）| 上述第二个运行的 `contexts/abcp-agent-slot-001-final-context.json`，段回执内嵌 |
| 问题 6 的完整运行日志（step 43 失败 / 46-47 手工滚动 / 50 成功） | `worktree/a686e03faba0404bb77b25eff7ee3f1b/run.jsonl` |
| 问题 6 的现场 A/B 实验原始回执 | `scratchpad/probe_result.json`、`scratchpad/probe_b_events.json`（脚本见 6.6） |
| 协议实测记录 | `docs/workflow-execute-live-contract.md` |

需要我们提供可直接运行的最小复现脚本，随时说。
