# 基于 Tau 的 Hybrid Skill Builder：Agent Loop 与工具生命周期

- 日期：2026-09-06
- 状态：设计评审；本文不代表运行时代码已实现。
- 范围：独立 `/skill-create` 程序、工具调用周期、停止协议、试运行恢复与发布。
- 决策：采用 Tau 的 Python Agent 核心二开，替代此前独立 Builder 使用 Pi TypeScript SDK 的建议。现有主程序继续消费生成的 Hybrid Skill。
- 2026-09-06 补充：原生 ABCP RPC 与 Workflow 共用现有 WebSocket 传输；当前 Harness 增加恢复执行适配与指标；发布由用户决定，生成完成与发布状态独立。

## 1. 已核实的基础与选型

Tau 是 Hugging Face 维护的 Pi 风格 Python 实现，不是 Pi 官方 TypeScript SDK 的逐接口镜像。源码分为 `tau_ai`（模型适配）、`tau_agent`（可复用循环与会话）、`tau_coding`（终端编码应用）。Builder 基于 `tau_agent` 及所需 provider 适配构建，不需要启动完整编码 TUI。

本次核对版本：提交 `0a67734fe4c89821c652c02fe74c1e0434fd36f6`，包版本 0.4.1。实施时固定版本并跑契约测试，不直接跟随 main。

源码证据：

- [loop.py](https://github.com/huggingface/tau/blob/0a67734fe4c89821c652c02fe74c1e0434fd36f6/src/tau_agent/loop.py)：`run_agent_loop`、工具执行、before/after 钩子。
- [tools.py](https://github.com/huggingface/tau/blob/0a67734fe4c89821c652c02fe74c1e0434fd36f6/src/tau_agent/tools.py)：`AgentTool`、`prepare_arguments`、`AgentToolResult.terminate`。
- [messages.py](https://github.com/huggingface/tau/blob/0a67734fe4c89821c652c02fe74c1e0434fd36f6/src/tau_agent/messages.py)：模型停止原因及 ToolResult 协议。
- [harness.py](https://github.com/huggingface/tau/blob/0a67734fe4c89821c652c02fe74c1e0434fd36f6/src/tau_agent/harness.py)：会话、steer、follow_up、取消、历史修复。
- [pyproject.toml](https://github.com/huggingface/tau/blob/0a67734fe4c89821c652c02fe74c1e0434fd36f6/pyproject.toml)：Python ≥3.12，Pydantic ≥2.11，MIT 许可。

本机项目环境 `.../envs/agent/bin/python` 为 3.13.5，可满足要求；系统 `python3` 为 3.9.6，不能作为 Builder 启动解释器。依赖冲突和当前模型网关兼容性仍需实施阶段验证，不能由版本号推定。

### Tau 当前核心 loop 的实际能力

| 项目 | 源码现状 | Builder 处理 |
| --- | --- | --- |
| Reason → tools → results → Reason | 已有；依据实际 tool calls 决定继续 | 复用 |
| `prepare_arguments` | AgentTool 上已声明，但核心 loop 没调用 | 接入调用链 |
| 通用工具参数 JSON Schema 校验 | 核心 loop 没实现 | 增加本地校验；不依赖 provider strict |
| `before_tool_call` | 已有；返回 `(blocked, reason)`，位于工具查找之前 | 扩展为校验后的准入；`True` 表示阻止 |
| `after_tool_call` | 已有；可变更 result/is_error；钩子异常未被核心隔离 | 保留原始回执，隔离钩子异常 |
| 工具异常 | 普通异常转为错误结果；取消异常抛出 | 保留并补充执行阶段与派发事实 |
| `AgentToolResult.terminate` | 类型已有，核心 loop 没消费 | 明确实现工具终止语义 |
| `execution_mode` | 类型有 parallel/sequential；当前 loop 逐个执行 | 第一版保持串行 |
| 工具进度 | `_run_tool` 先缓存 updates，结束后才发出 | 长 Workflow 需桥接实时事件队列 |
| `max_turns` | 超限生成 stop_reason=error | 外围另记录 budget_exhausted |
| 工具返回 details 中的失败 | 正常返回会被 `_run_tool` 标记为非异常 | 适配器显式映射 domain failure → is_error |

因此“使用 Tau”仍包含一个小范围核心补丁，不能只注册工具就声称五阶段周期和 finish 工具已经有效。补丁保留上游许可、固定基线；避免复制整个终端应用。

## 2. 程序边界与创建流程

用户完成原任务并自行确认满足需求后，主动在独立 Builder 中调用：

```text
/skill-create <历史任务目录> [创建要求]
```

源任务不要求出现 probe/validation phase，也不要求机器再次裁定源任务成功。历史轨迹、观察文件、产物与原始需求构成参考资料。历史工具输出按数据处理，不能成为修改当前工具权限或系统指令的来源。

Builder 使用一个持续的 Agent 会话完成阅读、编写、编译、试运行、修订和发布。阶段名称只是展示与产物状态，不是要求模型严格按固定阶段次数前进的门禁。

```text
读取源任务 → 推理可复用路径与参数 → 保存候选
     ↑                           ↓
补读证据 ← 工具观察结果 ← 编译 / live trial
                                 ↓
                     修复页面或修改候选后再试
                                 ↓
                     生成与验证报告 → finish_skill_create
                                 ↓
                     用户决定发布 / 暂不发布
```

LLM 决定哪些步骤进入 Workflow、哪些恢复经验写入 prompt、参数如何抽取、试运行覆盖什么。schema、权限、引用、版本绑定及实际写入回执由程序校验。

原任务的 11–20 页应作为历史样本。若 LLM 声明 `startPage/endPage` 为输入，新用户的“完整第一页”绑定为 1/1；参数准备层不得擅自把 11/20 换成 1，也不应按数字字面量一概拒绝候选。

创建阶段要求实际试运行；无需证明所有参数组合或随机浮层必然被覆盖。两页分页测试用于验证新增的翻页分支，不能成为所有 Skill 的统一准入条件。保留试运行日志，但不引入 draft-only、canary、自动 promotion 或后台自动抽取。

## 3. Reason → Act → Observe 的循环

Reason 是一次模型调用：输入原任务目标、当前创建要求、已提交消息和工具定义；输出可见说明与 tool calls。不要求模型显式输出完整内部思考，程序也不依赖思考文本解析控制流。

Act 执行本轮完整工具调用。Observe 将每个调用的结构化结果以相同 tool_call_id 回填历史，然后再调用模型。一次 Workflow 工具可在 ABCP 内执行几十个 Action，它们的进度可以展示；直到该工具返回终止或暂停交接回执，才算该次 ToolResult 完成。

第一版同一轮工具按顺序执行：共享页面操作、候选修改、编译和发布都有依赖。以后只对声明为独立的只读操作并行化。

建议注册的工具：

| 工具（新接口名称） | 用途与回执 |
| --- | --- |
| `read_task_bundle` | 返回源需求、trace/observation/artifact 索引，支持现有文件/DB 后端 |
| `read_task_resource` | 按资源及范围读取，超大数据返回可继续查询的引用 |
| `describe_abcp_action` | 读取 live capability/schema；以部署版本为准 |
| `save_skill_candidate` | 写入构建目录，返回 candidate_id 与内容 hash；不注册 Skill |
| `compile_skill_candidate` | 静态语法、引用和合同一致性检查；返回定位到步骤的问题 |
| `run_workflow_trial` | 执行候选，返回 workflowId、输入、状态、部分结果和失败路径 |
| `browser_call` | 经现有 WebSocket 发送原生 ABCP RPC，用于编写前探索、单步可行性测试和失败恢复 |
| `publish_skill` | Host 用户确认后的执行接口，发布用户选定内容 hash；不由模型自行决定调用 |
| `finish_skill_create` | 结束生成并返回候选、验证和限制报告，或说明待输入/未完成状态 |

如需 `dismiss_overlay`，以可选 Host 工具适配器暴露给 Agent；也可以由 Agent 使用原生观察和 Input 操作完成。它不是每次失败后必跑的 hook。

## 4. 工具生命周期

设计顺序：

```text
收到完整 ToolCall
→ prepareArguments
→ validateToolArguments
→ beforeToolCall
→ 若参数发生变化，再 validateToolArguments
→ toolExecute
→ afterToolCall
→ 提交 ToolResult / 持久化观察
→ 处理终止或进入下一次 Reason
```

Python 实现使用 snake_case；这里的阶段名称是项目接口设计，不冒充 Tau 已有的完整 API。

### prepareArguments

复制原始参数，仅做无损规范化和 schema 声明的默认值补齐；保留 raw/effective 参数差异。不得猜页码、URL、选择器、元素 ID 或业务字段值。错误类型转换交给 LLM 修正，例如不把字符串 `"3"` 静默改成数字 3。

可信 page/fleet 会话上下文由 Host 持有；显式用户参数与绑定冲突时返回冲突事实，不静默覆盖。准备失败生成 stage=prepare_arguments、dispatched=false 的 ToolResult，回到 Reason。

### validateToolArguments

先确认工具存在，再检查结构、必填、类型、枚举、引用和声明约束。`browser_call` 还要校验对应 live ABCP Action 的参数；Workflow 引用在编译阶段校验合法性，具体解析值由 ABCP 子 Action 校验。

当前项目 [argument_pipeline.py](../harness/tools/argument_pipeline.py) 已具备无损准备、默认值和 schema 检查；应抽取复用其纯函数及测试，不把整个 BrowserAgent 导入 Builder。该 validator 是已支持词汇的实现，遇到新版未知 schema 约束需明确兼容性问题，不能静默视为校验通过。

校验失败返回 field/path/keyword/message，工具没有执行。普通参数错误不终止整个创建任务。

### beforeToolCall

承担权限与执行上下文准入：取消状态、累计预算、页面归属、候选版本、输出目录范围和必要资源锁。读取原子状态并把准入与执行放在同一受保护范围，避免校验后对象被改写。

字段是否允许为空、是否应该关闭浮层、是否值得再试等业务判断由 LLM 作出；hook 只给出可验证事实。不得在这个 hook 中隐藏调用模型、关闭弹窗或重试 Workflow。

若 hook 注入了可信参数，必须重新校验；工具名变更应作为新调用处理，不能用改名绕过工具声明。原始参数始终保留在日志中。

### toolExecute

执行器有三种后端：原生 ABCP RPC、ABCP Workflow RPC、Host 工具。前两者复用现有 [ABCPClient.call](../abcp_client.py) 的 WebSocket JSON-RPC 通路及通知接收，不要求所有浏览器调用先包装为 Workflow。

```text
Tau ToolCall → 参数生命周期 → BrowserExecutionAdapter
                               ├─ browser_call(method, params)
                               │    → ABCPClient.call(method, params)
                               └─ run_workflow_trial(candidate, variables)
                                    → ABCPClient.call("Workflow.execute", params)
                                     ↕ WebSocket RPC / notifications
                                    ABCP 浏览器
```

Agent 可先用 `Page.getState / DOM.getAXTree / DOM.getSemanticTree` 观察结构，再使用 Input 等原生 Action 验证目标、事件等待与页面变化，必要时使用 Runtime.evaluate；这些观察指导 Workflow 编写。探索是可随时调用的工具能力，不要求历史任务必须含 probe 阶段。单步成功仍不能代替实际 Workflow 试运行，后者还要验证变量引用、循环、事件窗口与数据输出。

Adapter 复用现有传输的连接、请求关联、异常分类、敏感值脱敏、`subscribe_notifications` 和 `wait_for_notification`。Host 保留相关事件游标与 page/fleet 身份；读通知不能吞掉其他消费者需要的事件。原生操作与 Workflow 都进入同一执行记录和统计入口。

当前 ABCPClient 使用 `_call_lock` 串行处理请求，需保留这一事实：不能假设同连接在长 Workflow execute 未返回时还可以发送状态查询或恢复 Action。恢复在终止/明确暂停交接后调度；进度由后台通知接收。若确需并发控制 RPC，须先验证平台连接身份与控制契约，再设计独立控制连接，不在同一受锁调用内部递归调用客户端。

仅执行这次调用的职责，生成结构化回执。建议统一字段：

```json
{
  "status": "failed",
  "stage": "execute",
  "dispatched": true,
  "sideEffectState": "unknown",
  "data": {},
  "error": {"code": "...", "message": "..."},
  "artifactRefs": [],
  "diagnostics": []
}
```

上述 dispatched 仅表示该层派发事实；正式回执补充 `executionKind=native_rpc|workflow_rpc|host`、method、tool_call_id、request_id（传输层可取得时）、attempt_id、workflowId（平台返回时）。分别保存 `transport.requestSent=true|false|null`、`action.dispatchState=not_dispatched|dispatched|unknown` 和 sideEffectState，不能把未知值压成 false。原生 RPC 和 Workflow 子 Action 都遵循这个区分。

当前 ABCPClient 对未连接报告 request_sent=false、发送异常报告 null、等待超时报告 true；应原样保留。发送日志产生不代表发送成功，发送成功不代表 Action 完成。Workflow 外层请求成功也不代表某个子 Input 已派发。子 Action 是否改变页面只能引用平台明确回执，否则记 unknown。

Workflow status=failed 即使通过正常 Python return 返回，也必须映射到工具 is_error=true，并保留 variables/store/results/failedStepPath。普通工具失败进入下一轮 Reason；致命连接错误、取消和预算耗尽由外围控制器处理。

### afterToolCall

记录耗时、原始回执引用、脱敏后的模型视图、失败步骤与部分产物；可以附加事实和候选恢复动作。它不关闭浮层、不整段重跑、不把失败伪装成成功。

原始执行回执与呈现层结果分开保存。after hook 出错时，保留执行结果并附加 hook_error，避免因日志失败重复执行已经成功的操作。

每个已接收 tool_call_id 都有一个终结回执；prepare/validate/before 拒绝也走统一结果收尾，但 after 必须能处理“没有 effective arguments”的情况。取消/进程退出采用持久化恢复记录补齐，不谎称中断前的外部副作用已撤销。

现有 [dispatch.py](../harness/tools/browser_tools/dispatch.py) 的 `build_browser_tool_dispatcher` 已有上述主干、before 后再校验和 after 异常隔离。新 Builder 应复用这一经验；当前早期校验拒绝直接返回，需要在新 adapter 中统一收尾。

## 5. stopReason 与创建结果分开设计

Tau 的 `AssistantMessage.stop_reason`（JSON 为 stopReason）只有：`stop / length / toolUse / error / aborted`。这是模型响应的结束原因，由 provider adapter 根据实际响应映射，不能设置成 `skill_created`。

| 模型 stopReason | Builder 行为 |
| --- | --- |
| toolUse | 校验并执行完整工具调用，回填结果，再 Reason |
| stop | 没有工具时本轮循环可结束；由候选及报告判断生成完成、待输入或未完成，发布另记 |
| length | 不派发可能截断的调用；返回截断事实，在剩余预算内继续生成/缩减上下文 |
| error | 保留 provider 错误；仅对可重试模型请求作有界重试，不重放浏览器操作 |
| aborted | 停止新派发并交还用户；对已经在运行的 Workflow 查询真实状态 |

核心 Tau 对 error/aborted 直接结束，其他情况依据 tool calls 继续；它没有上述 length 专门分支。长度截断处理属于明确二开项。stop/toolUse 与内容自相矛盾时记录协议错误并让模型/provider 修复，不能执行残缺调用。

Builder 自己维护独立的任务结果：

```text
running
→ completed          候选与验证报告已生成，提交用户审阅
→ waiting_user       模型提出需要用户提供的信息/HITL
→ incomplete         模型结束但未完成候选/验证报告
→ budget_exhausted   累计 turn/tool/token/time 达到配置预算
→ cancelled          用户取消
→ failed             无法恢复的基础设施/内部错误
```

`max_turns` 是模型轮数；Workflow 的子 Action 数量另行记账。若外围分多次调用 prompt/continue_，必须累计预算，不能每次重置 Tau 的 max_turns。预算只负责客观资源上限，不用“连续无 artifact”机械判定业务停滞。

### finish_skill_create 的终止合同

建议输入：`outcome=completed|waiting_user|incomplete`，以及 candidate_id、candidate_hash、validation_report_id、摘要/限制或问题。工具验证引用与声明一致性；completed 表示创建报告完成，不表示已发布，也不把试运行失败自动解释成生成流程没有完成。报告必须如实列出成功、失败、未测试和 Agent 介入情况。

发布状态独立为 `not_requested / awaiting_user / declined / published / publish_failed`。用户暂不发布或拒绝发布时，生成任务仍可 completed；UI 显示“生成完成，未发布”。等待用户发布选择时无须保持模型循环空转。

成功时返回受信任的 `terminate=True`；补丁让 loop 在提交此 ToolResult 后结束。不能因为 Tau 类型定义了 terminate 就认为当前会自动停止。

若同轮还有未执行工具，先为它们写 `skipped_due_to_termination` 回执再发 agent_end，不继续执行排在 finish 后的页面操作。finish 前已执行的调用保留真实结果。只有 finish 等显式终止工具可请求此控制信号，页面返回的 JSON 字段不能终止 Agent。

模型只说“已创建”但未 finish：Host 核查候选及验证报告是否存在并一致；完整则可记录生成完成，否则反馈缺少的结构化事实，在预算内允许补做。若模型明确无法继续，则返回 incomplete。不得因未发布而催促模型继续或重复试运行。

### 主循环伪代码

以下表示需要实现的行为，不是 Tau 现成函数调用示例：

```python
while session.status == "running":
    enforce_cumulative_budget()
    assistant = await provider_reason(committed_messages, tools)
    commit_assistant(assistant)
    if assistant.stop_reason in {"error", "aborted", "length"}:
        await handle_provider_termination(assistant)
        continue
    calls = complete_tool_calls(assistant)
    if not calls:
        await reconcile_model_stop_with_build_state(assistant)
        continue
    for index, call in enumerate(calls):
        receipt = await execute_tool_lifecycle(call)
        commit_tool_result(call.id, receipt)
        if receipt.trusted_termination:
            commit_skipped_results(calls[index + 1:])
            session.status = receipt.build_status
            break
    # 已提交的 ToolResult 就是下一轮 Observe 输入。
```

## 6. Agent 处理浮层后重新执行 Workflow

允许重新运行。失败后的下一步可以是 Agent 修复页面，然后再次调用 Workflow；无需把第一次失败永久转换成纯慢路径。需要区分：已失败执行不能凭空恢复为 running；再次 execute 是新 workflowId，而 paused 执行是否可 resume 则使用部署版本真实能力。

示例：提取 10 件商品，第 8 件打开评论区时被广告遮挡。

1. Workflow 返回失败步骤、已完成 7 件、当前变量/store、公共错误反馈。
2. Agent 观察页面，确认浮层并关闭，刷新目标状态。
3. Agent 判断继续方式：整个 Workflow 重跑、把输入缩到第 8–10 件后重跑，或创建一个从评论区提取开始的 continuation Workflow。
4. 新执行使用新 workflowId，并记录 derived_from_workflow_id、输入及版本关联；保留前 7 件结果，按声明的身份键合并。
5. 新执行再失败，继续回到 Reason；受统一累计预算限制，没有“一个 worker 只能执行一次”的固定限制。

重跑选择由 LLM 根据事实决定：

| 选择 | 适用情况 | 必须携带的事实 |
| --- | --- | --- |
| 整段重跑 | 读取流程、可重建起点，重复成本可接受 | 当前页面与前置条件、已执行副作用 |
| 改输入重跑 | Workflow 已参数化，支持剩余行/页 | 剩余输入、已完成结果引用 |
| continuation | Agent 可根据步骤和变量构造剩余片段 | 引用依赖、当前状态、候选代码与编译结果 |
| 慢路径继续 | 剩余部分仍需探索 | 已完成数据、失败上下文、原用户目标 |

不能机械地按 failedStepPath 切片：嵌套循环的当前轮、分支条件和变量初始化都可能丢失。需要 LLM 构造可独立编译的片段，或原 Workflow 显式支持剩余输入。不要虚构 ABCP 的 startStep/initialStore 参数；若使用 `$store`，新执行可通过开头合法 store 步骤从初始 variables 重建必要数据，并校验引用依赖。

已提交订单、表单提交等操作不能仅因 Workflow 失败就自动重复。提供是否已派发/结果是否已观察的事实，由 LLM 决定补查和恢复；通用权限与明确禁止重放的协议边界继续执行。

试运行中 Agent 介入应写入 validation 记录。例如“主 Workflow + 遮挡恢复 + 新 Workflow 成功”，不能标为无人介入纯 Workflow 成功。整段重跑相同候选成功即可记录；若修改了主候选或生成了新片段，只能把证据归于实际执行的内容 hash。

## 7. 发布、验证与数据

输出沿用 `SKILL.md + workflow.json + fallback.yaml`，增加验证回执。候选保存在 Builder 构建目录，发布后进入 `/skill` 注册表。用户显式创建和后续显式调用是入口，不做隐式自动升级。

发布由用户明确决定。Builder 展示候选、实际试运行结果、Agent 介入、限制及预期发布位置；用户选择发布、继续修改或暂不发布。仅用户选择发布后，Host 调用发布接口，不依赖 LLM 声称“用户已同意”。确认绑定 candidate_hash 和目标版本；内容变化后重新展示变化并由用户选择。

发布执行层只检查文件/结构、引用一致性、权限、目标路径和版本冲突。不按模型业务评分、试运行成功率、字段是否为空或是否出现过浮层否决用户选择。创建流程仍要求尝试 live validation；试运行失败或环境阻断应明确展示，用户可以据此决定发布，不以成功率门槛代替用户决定。未发布构建输出只保存在构建目录，不额外引入 draft 等级或 draft-only 命令。

LLM 的验证报告描述试运行输入、观察产物、覆盖范围、是否介入、已知限制，并给出业务结果判断。程序不新增“必须两页”“所有字段非空”“不能出现过浮层”门禁。候选改动后重测受影响范围；不要求每次说明文字修改都完整重跑。

试运行写操作仍遵守原任务/当前创建要求的授权范围；原任务已完成不自动意味着允许重复提交。需要试运行提交类路径时可使用已有测试环境或验证无提交前缀，把覆盖事实写清楚。

Workflow 产生的数据交给现有 Host 存储适配器。当前任务库与外部业务库分别配置；发布 Skill 的成功与业务数据写库成功分别记录，数据库重试不重新执行浏览器流程。

## 8. 现有 Harness 的执行/编排适配

Builder 的探索/试运行与主程序的 Skill 使用要共享一组执行服务，避免在 Tau 与 Python BrowserAgent 中维护两套重跑、快照解析和统计逻辑。模型循环可不同，执行结果契约保持一致。

| 当前代码证据 | 问题 | 拟调整 |
| --- | --- | --- |
| `harness/tools/browser_tools/dispatch.py` 的 `_selected_skill_workflow_attempted` 拒绝分支 | 同一 worker 第二次执行被直接拒绝，即使 Agent 已修复页面 | 用 invocation/attempt 记录替代一次性布尔拒绝；允许 Agent 发起新 attempt |
| `harness/skill/dispatch.py` 的 `_run_with_transient_retry`、`_run_with_auth_generation_fence` | 外围禁止重跑，底层又隐藏重试；超时重试可能重放未知副作用 | 所有重试进入统一 attempt 记录；连接修复与业务重跑分离，后者交还 Agent，身份屏障仍保留 |
| `harness/skill/workflow.py` | 正常 return 直接 succeeded=True，失败后用 runId 获取旧式快照 | 依据部署协议解析 terminal status、workflowId 和异常 partial；本地 runId 仅作关联标识 |
| 显式 Skill 调用失败摘要 | 仅 rows/failedRow 等摘要，不足以重建中断片段 | 输出 RecoveryContext，包括原始回执引用、failedStepPath、变量/store、已完成数据 |
| `abcp_client.py` | WebSocket 发送与通知已实现；派发未知值已有表达 | 抽取共用 adapter，保留传输事实，不为 Builder 另造传输 |

建议统一 Host 服务（新接口）：`execute_native_action`、`execute_workflow_attempt`、`read_attempt_context`、`record_recovery_decision`。Agent 仍通过工具使用它们；after hook 只记账，编排层不按单站点失败文本自动选择恢复路径。

关联模型：一个 task 包含多次 Skill invocation；一次 invocation 包含多次 Workflow attempt，以及中间的原生 RPC 恢复。每次重跑增加 attempt_id；新 workflowId 由 ABCP 返回。同一 attempt 的重复终止通知幂等更新，不能重复累计。构建试运行另带 builder_run_id 与 candidate_hash。

恢复决策记录 `source_attempt_id / decision / evidence_refs / next_inputs / candidate_hash`；决策可为 full_rerun、remaining_inputs、continuation 或 slow_path。修改输入与生成 continuation 必须重新经过参数与 Workflow 编译链，不允许借恢复绕过当前工具授权。仅剩余数据补齐时按声明的身份合并，不猜业务去重键。

## 9. 完成率与成本统计

统计只报告事实与语义判决来源，不驱动自动封禁 Skill、自动升级或发布门禁。复用当前任务事件/SQLite 存储，按投影生成统计，无需另建独立遥测平台。

三层结果分别记录：Workflow 协议执行状态、输出目标是否满足、Agent 最终任务结果。工作流 succeeded 可能只表示所有步骤执行完，不能等同于用户需求完成。语义结果使用 `satisfied / not_satisfied / unknown`，附 judge_source（LLM/user）、依据与时间；用户判定单独存储，保留原判定历史。

统计以开始时间选定 cohort，并标注统计截止时间，避免跨天重试造成分子分母错位。构建试运行、正式 Skill 调用、原生探索三类用途分开展示；同一 Skill 不同 workflow hash 分开聚合。计数包含成功/失败/取消/进行中/未知，未知不填零，不只展示百分比。

| 指标 | 明确口径 |
| --- | --- |
| Workflow 执行成功率 | 平台明确 succeeded 的 attempt / 平台明确 succeeded 或 failed 的 attempt；另列无终态、取消、派发失败数量 |
| Workflow 首次完成率 | 首次 attempt 已执行成功且目标 satisfied、没有 Agent 恢复的 invocation / 所有已发起首次执行的 invocation；尚未终止/未知单列，展示截至当前的保守比例 |
| Hybrid 调用最终完成率 | 最终目标 satisfied 的 invocation / 同 cohort 已发起的 invocation；保留 cancelled/running/unknown 明细 |
| Agent 任务完成率 | 最终目标 satisfied 的 task / 同 cohort 已启动的 task；一次任务只计一次，不因重试增加任务分母 |
| 用户确认完成率 | 用户明确确认完成的 task / 用户明确评价完成或未完成的 task；另报反馈覆盖率=已评价/已启动 |
| 恢复成功率 | Agent 介入后最终 satisfied 的 invocation / 有 Agent 介入的 invocation；未结束数并列 |
| 发布采用情况 | 用户发布/拒绝/未选择的数量，发布操作失败另计；不把它命名为任务完成率 |

首次完成率保守比例受 pending 影响，因此同时提供“已判定样本首次完成率”及判定覆盖率；后者的分母为首次结果已明确 satisfied 或 not_satisfied 的 invocation。前置参数拒绝不算浏览器 attempt 成功/失败，但单独记录拒绝并影响调用最终结果。执行成功后输出判决 unknown 的样本不能进入已判定成功分子。

示例：100 次正式调用各一次 Workflow，其中 70 次首次满足目标，30 次失败；Agent 修复后仅对这 30 次各重跑一次，20 次满足目标，10 次确认未完成。假设无取消/未知，所有协议成功均满足目标，则首次完成率 70%，attempt 执行成功率 90/130≈69.2%，Hybrid 最终完成率 90%，恢复成功率 20/30≈66.7%。若每次调用各对应一个独立 Agent task，Agent 任务完成率才同为 90%。

每次同时记录模型 token/cost（未提供价格时 cost=null）、模型轮数、原生 RPC 数、Workflow attempt 数、可观测子 Action 数、耗时、Agent 介入次数、失败步骤与公共错误码。按模型报告 token 使用量记账，不能用“Workflow 节点数 × 假定单步 token”声称节省；Token 节省比例需要同任务/同输入的实际慢路径基线。

仪表盘或 CLI 摘要首版直接显示计数及上述比例即可。业务 DB 写入失败另列 sink_status；任务是否要求写库由用户目标决定，不能一概覆盖浏览器执行成功状态。

## 10. 最小实施路径与验证

1. 建立独立 Python `skill_builder` 包及固定 Tau 依赖，核实 provider/tool schema/stream 与现有模型配置兼容。
2. 小范围补齐 Tau loop：prepare、validate、钩子异常隔离、domain error 映射、terminate、length、累计预算和即时进度事件。保留与上游比较的补丁。
3. 实现 TaskBundle、CandidateStore、ABCP 与 ResultSink 适配器；绑定 live Workflow 协议并保存失败快照。
4. 接入原生 WebSocket RPC 探索、候选编译、真实试运行、Agent 页面恢复及新 Workflow 重跑。
5. 接入用户发布决策，将创建结果和发布状态分开；主程序与 Builder 共享执行服务。
6. 增加 invocation/attempt 事件及统计投影，验证未知状态、重试和用户反馈的统计口径；后续按原评审范围清理自动升级链路。本文更新不授权顺带替换运行时全部 Harness。

测试重点：

- 错误参数在 execute 前拒绝，before 改坏参数也不能执行。
- prepare/before/execute/after 各阶段异常均有可归因回执；after 失败不重复已执行操作。
- 工具正常 return 的 Workflow failed 仍正确映射 is_error，并保留部分结果。
- finish 真正停止 loop，同轮剩余调用有 skipped 回执；纯文本 stop 不冒充完成。
- length 时不执行截断调用；取消、超时、断连保留未知副作用状态。
- 浮层修复后再次执行相同/剩余 Workflow，生成新 ID 并保留前序产物。
- 修改候选后旧验证证据不被错误绑定；混合恢复成功不会被报告为纯快路径成功。
- Python provider smoke、任务 DB 读取、发布失败后幂等重试。
- 编写前原生 RPC 探索成功，既有 WebSocket 通知可观察，超时的 requestSent 与副作用未知状态不丢失。
- Agent 清掉浮层后同一 worker 可以发起新 attempt；隐藏重试不会造成漏记或重复计数。
- 用户不发布仍报告生成完成，未确认不能发布；失败试运行如实展示，不按质量分数否决用户选择。
- 一败一成产生两个 attempt、一个 invocation；重复通知只算一次，pending/unknown 与试运行样本不混入已判定正式成功样本。

现阶段不需要新建站点/字段语义门禁。新增校验均限于通用结构、身份、权限、资源预算和回执一致性；失败要有定位与恢复方式，语义选择留在 Agent Reason 中。
