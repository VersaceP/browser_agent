# Harness 与 ABCP 2026 更新实施计划

状态：已批准，按本文顺序实施  
批准日期：2026-07-18  
适用范围：BrowserAgent、LeadAgent、skill/workflow fast path、ABCP capability 调用层、task validator 与相关提示词/文档

## 实施结果（2026-07-18）

- 阶段 1—9 已落地：生命周期门控、同 turn 状态屏障、row/phase pacing、统一 Runtime 边界、受控 Workflow、首行 trace 临时编译与 canary、DOM batch/getImg capability gate、文件 validators、prompt/策略/skill/docs 同步。
- 三轮静态复核指出的问题已逐条复验并处理：优先修复文件证据、动态 record_extraction 屏障、Runtime JSON 落盘合同、通用 page binding 与 side-channel 终态判断；随后补齐 ephemeral fallback/部分结果交接、失败 resync、冻结 workflow 预检与 capability-aware world 迁移、上传页面确认合同、Runtime 交互绕过扫描、原生 `File.download.data.downloaded`/`Download.getStatus.data.savePath` 证据以及 `min_bytes=0` 语义。
- Ephemeral workflow 在完成 live ABCP canary 前默认关闭；开启后，编译/canary/部分执行失败会持久化已完成行，把真正剩余行交回慢路径，并阻止仍有剩余行时提前返回 `done/partial`。
- 工具式和纯文本式终态共用同一 pending-row 屏障；side-channel 从 setup 开始即受 `try/finally` 清理保护；空 `Page.getState.data` 不再解除 resync 义务。
- 静态检查通过：`git diff --check`、`python3 -m compileall -q agent_harness.py harness runtime_config.py`。
- 全量测试通过：`1287 passed, 6 skipped`；两条 pytest collection warning 来自既有带 `__init__` 的测试数据类。
- 2026-07-18 尝试 live probe `DOM.getText`、`DOM.getImg`、`Runtime.evaluate` 的 `System.describeAction`，最终复验 `System.getCapabilities` 时本地 `ws://localhost:9300/ws` 仍未监听，均在连接阶段失败。因此当前代码实现与旧版 fallback 已由自动化测试覆盖，实际 ABCP schema/事件时序仍需在服务启动后复验。
- 新版专属能力状态：实现完成、live verification 待 ABCP 服务启动/升级；未把 `targets`、`DOM.getImg` 或 Runtime `world` 标记为 live-verified。

## 1. 背景与目标

本次改造解决四组互相关联的问题：

1. 批量爬取缺少 row 级和 phase 级节奏控制。
2. 同一 LLM turn 可以生成多个 tool call，但后一个调用看不到前一个结果；现有顺序执行会在页面状态变化后继续盲目下发。
3. 没有匹配的冻结 workflow skill 时，系统不能把已验证的首行执行轨迹编译为当前任务内可复用的临时 workflow。
4. ABCP 新版增加或明确了生命周期事件、DOM batch read、DOM image export 和 Runtime world 语义；当前 harness 的 schema、事件门控、文件校验和提示词尚未完整对齐。

最终目标不是在 harness 内再造一套通用工作流语言，而是形成以下分层：

- LLM 同 turn tool calls：仅用于参数完全已知的稳定调用；状态边界后必须重新推理。
- Harness composite：负责结构化、可审计的本地能力。
- ABCP `Workflow.execute`：负责浏览器 action 的确定性串行、简单条件、监听和有界循环。
- Native DOM batch：负责真正减少同页读取 RPC。
- Frozen skill：跨任务持久复用。
- Ephemeral workflow：只在当前批量任务内，经 canary 后复用，不自动写入 skill registry。

## 2. 已确定的设计决策

### 2.1 Pacing 只支持 row 与 phase

不新增 task 级 pacing。

计划合同采用秒作为规范单位：

```json
{
  "pacing": {
    "row_interval_seconds": 30,
    "phase_interval_seconds": 60,
    "jitter_ratio": 0
  }
}
```

- plan 级 pacing 提供默认值，phase 级 pacing 可以覆盖。
- row interval 的锚点是上一 row 完成 workflow、校验和持久化之后。
- phase interval 的锚点是全部依赖 phase 中最晚的 `validated_done` 时间。
- `depends_on=[]` 的独立 phase 没有依赖锚点，不应用 phase interval。
- 默认不加抖动；只有用户明确要求或策略显式设置时才使用 jitter。
- row 等待期间保留 warm tab；phase 等待发生在 slot 分配之前，不占用 BrowserAgent slot。

### 2.2 同 turn 多调用不是数据依赖工作流

当前 harness 对 tool calls 使用 `for ... await` 顺序执行，ABCPClient 还有连接级 `_call_lock`。这保证物理顺序，但不让第二个调用读取第一个结果。

因此：

- 参数完全已知、不会相互改变页面状态的读取可以同 turn 顺序执行。
- 导航、页面切换、DOM mutation、dialog/chooser、Runtime state change 等调用形成状态边界。
- 状态边界后的未执行调用返回 synthetic `tool_result`，状态为 `deferred_due_to_state_change`，下一轮 LLM 读取前序结果后重新生成。
- 所有模型输出的 `tool_use_id` 都必须得到对应 `tool_result`，不能因 `should_stop` 或状态边界留下协议缺口。

### 2.3 Browser-only 串行程序使用受控 Workflow

新增受控 ephemeral workflow 执行入口。不能让裸 `browser_call(method="Workflow.execute")` 绕过 harness policy。

递归预检至少覆盖：

- nested action 是否存在于实时 capabilities。
- task_type 的禁用 domain 和例外规则。
- `ALWAYS_FORBIDDEN_ABCP_METHODS`。
- 禁止嵌套 `Workflow.execute`。
- 最大 step 数、loop 次数、总 timeout、单步 timeout。
- file upload/download 权限边界。
- Runtime.evaluate policy。
- 导航后的 settlement 和重新感知要求。

第一版 ephemeral workflow 禁止包含 `Runtime.evaluate`，避免 Workflow 内部 action 绕过统一 Runtime policy。Frozen workflow 的 Runtime step 走加载/执行前审计和兼容迁移。

### 2.4 在线 workflow 只做当前任务内优化

触发条件：

- 任务是明确的同构多 row 批量任务。
- 没有可执行的冻结 workflow skill，或冻结 fast path 已回退。
- 第一 row 由普通 LLM 路径完成，并通过 page binding、artifact 和 validators。
- 成功 trace 可编译为纯 ABCP action workflow，不包含 HITL、视觉判断、local_fs 或不可编译 composite。
- 至少还有两 row，保证编译有实际收益。
- 第二 row canary 通过同一成功合同。

成功后复用到剩余 rows；任何编译或 canary 失败都回退普通路径。临时 workflow 不写 registry，不改变默认 `skill_selection_mode=manual`。

### 2.5 Runtime.evaluate 只有一个模型自由 JS 入口

模型侧只保留：

```text
browser_call(method="Runtime.evaluate")
```

`eval_js_json` 从模型工具列表隐藏，但保留为兼容别名。所有 harness 直接 Runtime 调用进入统一 `RuntimeEvaluationService`，职责拆分为：

1. policy authorization；
2. native/raw 或 JSON 执行；
3. 必要时的 title side-channel fallback；
4. 可选 record persistence。

模型自由 JS 必须提供 harness-only `runtime_policy`：

```json
{
  "intent": "diagnostic|extract|state_change",
  "effect": "read_only|state_changing",
  "reason_kind": "...",
  "why_structured_tools_insufficient": "...",
  "cross_check_plan": "...",
  "result_mode": "raw|json",
  "record_name": ""
}
```

规则：

- 所有模型自由 JS 都执行硬门控。
- extract 必须提供 cross-check plan。
- 可由 native batch DOM 表达的读取拒绝使用 Runtime。
- 不允许替代表单、点击、上传、权限或结构化交互。
- `world=auto` 仅允许 read-only；state-changing 必须显式 main/isolated。
- world 只有在实时 schema 暴露后才允许传给服务端。
- Frozen skill 可提前声明 world；新版服务端保留，旧版仅从执行副本移除。局部 `var/let/const` 初始化不算页面 mutation，属性/DOM/global 写入仍算 state-changing。
- 表达式扫描是保守的 defense-in-depth heuristic，不是 JavaScript parser 或安全沙箱；ABCP 平台权限与结构化交互边界仍是最终执行边界。
- `extract_dom_records` 是 harness 生成表达式的结构化 composite，使用可信 `structured_dom_composite` policy，不要求模型填写 reason_kind。
- side-channel 只用于 `result_mode=json` 且 native result 不可用时；必须恢复 title 并清理临时 window 状态。
- 自动落盘只适用于明确的 rows JSON 结果。

### 2.6 文件 task_type 不合并

继续保留：

- `file_download`
- `file_upload`

新增文件专用 validators：

- `download_completed`
- `file_integrity`
- `upload_selected`
- `upload_confirmed`

其中 `upload_selected` 只证明 chooser 已成功选择文件，不能替代业务侧上传成功确认。

### 2.7 新 ABCP 方法必须 capability-gated

当前本机 ABCP capabilities 仍是旧版：旧单目标 `DOM.getText/getAttribute` schema、无 `DOM.getImg`、`Runtime.evaluate` 无 `world`。

因此：

- harness 可以先实现新 schema 兼容、结果解析和测试 fixture。
- 只有实时 `System.getCapabilities/System.describeAction` 确认支持时才向模型暴露新字段或方法。
- 旧服务端保持原行为，不发送未知参数。
- 在新版服务端完成 live probe 前，不声称生产环境已经验证新能力。

## 3. 实施顺序

顺序按依赖关系确定；每一步完成后先运行对应测试，再进入下一步。

### 阶段 1：页面生命周期状态机与 settlement gate

实现 per-page 状态：`unknown/loading/settled/failed/crashed`，维护 generation 和可等待事件。

事件规则：

- `Page.startedLoading`：进入 loading，暂停 DOM probe。
- `Page.loaded`：进入 settled。
- `Page.loadFailed`：进入 failed。
- `Page.crashed`：进入 crashed 并使 DOM/AXTree 失效。
- `Page.navigate`、`Page.recovered`：使 DOM/AXTree 失效；settled 后要求 `Page.getState + DOM.getAXTree`。
- `Page.dialogClosed`、`File.chooserClosed`：要求一次 `Page.getState` resync。

notification callback 只更新内存状态并 `Event.set()`，不得在 WebSocket reader callback 内调用 ABCP。

settlement 超时后只调用一次 `Page.getState`，不轮询。改造 `navigate_verified` 使用相同原语。

验收：

- loading 期间 DOM probes 被机械阻止。
- loaded 后恢复。
- 丢失事件时只发生一次 getState resync。
- recovered 事件确实使 AXTree stale。
- callback 无重入 ABCP 调用。

### 阶段 2：同 turn 多调用 barrier

建立 tool call 分类：稳定读取、状态边界、终止调用。

执行状态边界或 `should_stop` 后，为所有剩余 tool_use 生成 synthetic result，不继续下发。

验收：

- 每个 tool_use_id 都有且只有一个 tool_result。
- 导航后的第二个预生成 DOM call 不会盲目执行。
- 多个稳定 batch read 仍能顺序完成。

### 阶段 3：row/phase pacing

扩展 plan schema、normalization、worker contract 和日志。

row pacing 同时接入：

- skill fast path `skill_rows` loop；
- `execute_selected_skill` rows loop；
- 后续 ephemeral workflow rows runner。

phase pacing 在 slot 获取前计算依赖完成时间并等待。

使用可注入 clock/sleep 编写测试，避免测试真实等待。

验收：

- interval=0 完全保持现状。
- 不在第一 row 前或最后 row 后等待。
- phase 等待不占 slot。
- jitter=0 时严格使用用户值。

### 阶段 4：统一 RuntimeEvaluationService

建立共享执行服务并迁移：

- 裸 `browser_call(Runtime.evaluate)`；
- `eval_js_json` 兼容别名；
- `extract_dom_records` 内部 Runtime；
- 其他 harness 内部 Runtime helper。

隐藏模型侧 `eval_js_json` 和直接 Runtime capability tool，保留 browser_call 单入口。加入 `runtime_policy` schema 和审计日志。

side-channel 增加显式 result mode、world 一致性、title/window cleanup；world 由实时 schema gate。

验收：

- 裸 Runtime 无 policy 被拒绝。
- structured composite 不需要模型 reason_kind。
- raw 模式不会误用 JSON wrapper/side-channel。
- state-changing + auto world 被拒绝。
- 旧服务端不收到 world。

### 阶段 5：受控 ephemeral Workflow

新增 workflow validator 和执行工具，裸 Workflow.execute 路由到相同 validator。

第一版禁止 ephemeral Runtime step，递归检查 task_type、method、loop、timeout、nested workflow 与生命周期规则。

验收：

- web_scrape workflow 不能嵌 File.download。
- workflow 不能嵌 Workflow.execute。
- 导航后缺少 settlement/reperception 的 workflow 被拒绝或安全补全。
- 结果进入统一 trace/offload。

### 阶段 6：在线 trace 编译与 canary

将现有 trace distiller 中的纯 ABCP action 提取能力重构为可接受临时 variable template/success contract 的编译器。

执行流程：首行成功 → 编译 → 第二行 canary → 剩余 rows 复用。失败时保留已完成 rows 并回退正常 BrowserAgent。

验收：

- 固定 pageId/AXTree id 不会被带入其他 row。
- canary 失败不会写 registry 或丢失 row。
- 当前 task 结束后临时 workflow 消失。

### 阶段 7：DOM batch read 与 DOM.getImg

更新 target validation、nested AXTree target 扫描、batch envelope 解析和 partial failure 语义。

`DOM.getText` 成功项读取 `info.textContent`；`DOM.getAttribute` 成功项读取 `info.attributes`，保留 null 与空字符串的区别。

`DOM.getImg`：

- 只在 capability 存在时暴露。
- 要求 `options.path` 为输出目录。
- 收集 `info.savedPath`。
- 保留 `info.method=fallback-screenshot`。
- 非 img target 只标记对应 item 错误。

验收：

- targets-only 调用不被旧的顶层 id/selector guard 错误拒绝。
- partial item failure 不升级为整批失败。
- 旧服务端维持单目标行为。

### 阶段 8：文件 validators 与 artifact ledger

建立文件 evidence normalization，验证 Download 状态、实际路径、文件大小/格式/checksum、chooser selection 和业务确认 evidence。

验收：

- completed 但无文件不能通过 download_completed/file_integrity。
- chooser 成功不能自动通过 upload_confirmed。
- DOM.getImg savedPath 可复用 file_integrity。

### 阶段 9：提示词、策略、蒸馏器与文档

同步更新：

- BrowserAgent prompt；
- LeadAgent prompt；
- strategy bank；
- skill create/distiller/hardening；
- agent skill guide；
- workflow orchestration guide；
- fleet reuse architecture 中与 phase 调度相关的说明。

明确优先级：native batch DOM > native single DOM > structured composite > Runtime.evaluate。

### 阶段 10：完整测试与 live verification

自动化测试覆盖：

- 生命周期事件竞态与单次 resync；
- multi-call tool_result 完整性；
- pacing 虚拟时钟；
- Runtime 绕过与 world gate；
- Workflow nested policy bypass；
- DOM batch partial failures；
- DOM.getImg artifact；
- 文件 validators；
- prompt/schema snapshot。

Live verification 分两层：

1. 当前旧版 ABCP：确认 fallback 和兼容路径不回归。
2. 升级版 ABCP：确认 batch schema、DOM.getImg、Runtime world 和事件顺序。

## 4. 非目标

本次不做：

- task 级任务队列与 task 间 pacing。
- 支持 harness 本地任意工具的通用 `$result[n]` workflow 语言。
- 自动把 ephemeral workflow 持久化为永久 skill。
- 合并 file_download/file_upload task type。
- 在 capabilities 不支持时伪造 DOM.getImg 或 Runtime world。
- 将 Runtime.evaluate 用作表单、上传、权限或交互控制的替代品。

## 5. 兼容与回滚

- 所有新功能默认值保持旧行为：interval=0、无 ephemeral 编译时走原慢路径。
- 新 ABCP 方法与字段全部 capability-gated。
- `eval_js_json` 至少保留一个兼容周期，旧 trace/内部调用不立即失效。
- ephemeral 编译和 workflow 执行失败均回退到现有 BrowserAgent 路径。
- 不修改或覆盖用户已有 skill；frozen skill 迁移必须通过 canary。

## 6. 完成定义

只有同时满足以下条件才算完成：

- 阶段 1—9 的代码和文档全部落地。
- 新增测试通过，现有相关测试无回归。
- 当前旧版 ABCP compatibility probe 通过。
- 新版专属能力若尚无可用服务端，明确标为“实现完成、live verification 待服务端升级”，不得伪报已验证。
- 最终交付列出修改文件、测试结果、未完成的外部依赖和升级后的复验命令。
