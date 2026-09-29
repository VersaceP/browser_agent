# Harness 参数修补审查与边界（2026-09-26）

## 决策

保留小规模、可移除的参数位置兼容层，不建设通用“替 LLM 猜参数”的纠错器。
本轮没有扩大自动修补的字段白名单，重点是补齐拒绝、保留原值、动态引用和
schema 变化的边界。用户授权本次审查、必要补强及文档留档；不包含部署、提交或推送。

“模型将来更强，兼容代码可能删除”是合理预期。兼容层只集中在
`harness/tools/capability_repairs.py`，不让下游执行器接受更多别名，不把业务策略
或站点特例写进兼容逻辑。LLM 仍需学习标准参数，而非把修补当成新的公开 API。

### 四项优化是否必要

| 修改 | 审查结论 | 证据和限制 |
| --- | --- | --- |
| 唯一字段位置迁移 | 有现实用途，有限保留 | 原任务 54 次相关调用中 4 次可迁移，迁移后通过当前 schema；不能据此宣称浏览器执行成功 |
| Workflow 字面量覆盖 | 必要，避免直接调用和段内调用处理不一致 | 复制定义、递归处理 then/else/body；不改保存定义或 hash，不求值动态表达式 |
| 预授权收窄 | 应保留，属于权限边界 | 选择当前请求对应的最窄声明；本轮进一步只请求实际使用的 READ 或 WRITE |
| ClaimExtractor 独立 auto | 合理的传输兼容调整 | 检查了独立配置和编码测试；原有单工具响应、数字绑定、覆盖校验及不可用处理策略未改变，不能保证模型每次按要求调用工具 |

## 自动修补合同

只允许以下两条，无站点、业务字段、目标选择器特判：

1. `DOM.getAXTree.targets` → `query.targets`：请求已经明确给出 view，当前 schema
   只有一个对应分支，该分支声明 targets，顶层未正式声明 targets。
2. `Input.type.target.id/selector` → 顶层 `id/selector`：target 是非空对象，
   只含当前 schema 支持的这两个字段，且 schema 未正式声明 target。

迁移不得改变值、顺序、重复项、类型、权限、page/fleet 身份、clear 或其他操作选项。
相同重复声明可合并；冲突声明拒绝整个修补，不能挑一个值。修补结果仍必须经过
原有调用入口的合同、绑定、生命周期、目标和 schema 检查；平台运行时检查仍然有效。

### 普适性、误拒绝与恢复

- 这是 Action 接口合同的适配，不是网站或某次任务的策略；同一接口在所有站点行为一致。
- 机器只做可证明唯一的字段搬运；LLM 当然也能改，但这类搬运不需要再做语义推理。
  如果无法证明唯一，就向 LLM 提供字段路径和错误类型，由它按原始目标修正。
- 已知别名形状不明、同名字段冲突或错误 view 不允许悄悄被平台丢弃。这是协议一致性
  检查；不推断缺失目标，不选择 view，不把字段删除以换取“执行成功”。
- Workflow 动态参数与错位字段混用时，保守拒绝该段；模型可以改用 canonical 路径。
  完整的 canonical 动态参数保持原样，交给平台按执行时的值验证。
- 当前自动修补只识别明确的 schema 分支形式。没有 schema 时不授权任何迁移；
  分支无法唯一识别时不猜测。新的合法 canonical 请求不应为兼容旧错误而被改写。
- 未列出的拼写错误、未知方法、类型错误或缺失必填值继续走已有校验和反馈。
  这里不承诺识别所有错误，也没有新增通用删字段器或模糊相似度纠错。

## 审查发现及本轮补强

1. **合法文本误判为动态引用**：旧实现遇到任意 `$` 或 `{{` 都当成 Workflow 引用，
   导致 `price $10` 这样的普通输入与字面量目标一起被拒绝。
   现在按平台 `resolveParams` 合同，仅将“整个字符串以 `$` 开头”识别为引用；
   对象键不求值。`{{literal}}`、字符串中间的 `$` 保持原值。
   以 `$10` 开头的值仍属于平台引用语法，本层不猜测它是否本来想表示货币。
2. **schema 演进误搬字段**：未来或旧版 schema 若正式支持顶层 targets，旧代码仍可能
   搬入 query。现在正式 schema 优先；Input.type 的正式 target 同理。
3. **异常 view 导致 Python 异常**：list/dict 等错误 view 曾触发不可哈希类型异常。
   现在产生参数诊断，避免进入异常恢复慢路径；不自动转换类型或选择 view。
4. **模糊 target 被忽略**：Input.type 的 target 若为空、字符串或带额外键，旧实现会
   原样继续。有的 Action schema 会丢弃未知字段，顶层已有 id 时可能悄悄忽略 target。
   现在在确认该字段不是正式 schema 字段后拒绝这种模糊别名，返回 canonical 路径说明。
5. **权限模式提前扩大**：选中一个声明后，旧实现会对声明内所有 modes 提前询问。
   现在按 `(最窄声明, 当前请求的 mode)` 去重；只有确实同时读写的调用才分别询问两项。
   不扩大父目录、不请求无关输入目录；保留拒绝、保护路径、符号链接和任务隔离检查。

权限声明若只有过宽父目录，Harness 不凭空猜交付根目录，仍由计划审核和用户的实际
授权决定。本次收窄不能代替计划审核，也不能证明所有路径型参数或动态 Workflow
文件路径均已被完整建模。

## 覆盖矩阵

| 类别 | 验证内容 |
| --- | --- |
| 正常迁移 | 四种 query view；id、selector、双定位；顺序、重复项、空文本、Unicode 原样保留 |
| 重复与冲突 | 相同值去重；不同值拒绝；部分字段已迁移后遇到冲突仍整体回滚 |
| 错误类型 | params 非对象；view 为 null/list/dict/bool/number；非法 id/text/clear/delayMs 不被转为合法值 |
| 语义不明 | 缺 view、未知 view、view 与选项混用、target 额外字段／空值／字符串拒绝或交已有 schema 校验 |
| Schema 变化 | 缺 schema 不修补；未来正式支持别名时不改写；新 view 选项不误拒；无法识别分支不猜 |
| Workflow | then/else/body 递归；不遍历 store 的业务载荷；整体失败回滚；定义不可变；重复修补幂等 |
| 动态值 | vars/last/cache/context/store 及短引用；canonical 路径不变；错位字段混用拒绝；普通 `$`／花括号文本不误拒 |
| 执行边界 | 直接和 wire Workflow 入口；修补仍经过目标检查；冲突在浏览器执行前停止；大回执保留修补路径 |
| 权限 | 最窄声明、无关目录、同模式共享、按需 READ/WRITE、混合操作去重、模式拒绝隔离、保护路径、符号链接、取消和非交互 |
| 数值核验 | auto 在两类协议中编码；不修改审核器或 thinking；原数字约束与覆盖回归 |

测试是风险边界覆盖，不代表穷举所有 JSON、页面状态或模型错误。
本次没有新增字段修补规则来追求更高“自动修复率”。

## 回放与回归证据

运行 `probe_tests/replay_capability_repair_review.py`，读取任务
`46108145d8364c228f1dcee6ac2b5465` 的 `agent.model` 原始调用。
报告为 `reports/capability-repair-review-replay.json`，含原始文件和 schema SHA-256，
仅记录位置、方法、修补路径及错误类型，不复制参数值。

- 54 次相关调用；4 次可迁移，4 次迁移后通过当前 schema。
- 4 次 query view 混用返回精确错误；没有自动选择另一种 view。
- 没有浏览器调用、LLM 调用、自动重试；不构成端到端成功率、时延或 token 收益证据。

核心回归命令：

```sh
python -m pytest -q tests/test_capability_repairs.py tests/test_phase_local_access_intent.py tests/test_tool_argument_pipeline.py tests/test_local_path_authorization.py tests/test_numeric_facts.py
```

扩展检查包含 Workflow policy/wire/reuse/auth/runtime、参数、双定位、授权等待和 schema
drift。发现 15 个失败集中在 `test_workflow_policy.py` 与 `test_workflow_schema_drift.py`。
以本轮修改前的两个生产模块在独立进程加载复核，仍出现相同 15 个失败，包括旧事件名、
旧平铺执行格式、旧 transform.find 语义及 UUID/schema 映射断言。
这些失败需要单独核对合同与测试，不在本次修补中隐藏或更改断言。
不能将它们笼统归因于用户转述的 6 个 SDK 测试失败；本次没有独立复现那 6 个失败。

JUnit 留档：`reports/capability-repair-review-core.xml`、
`reports/capability-repair-review-baseline.xml`、`reports/capability-repair-review-current.xml`。
最终计数见本文件末尾验证记录。

## 可观测性、收益判断和移除条件

`browser.call.arguments_repaired` 和回执 `argumentRepairs` 只含迁移字段路径。
它们证明发生了参数准备，不证明通过后续门禁或浏览器执行成功。
分析时必须与同一次调用的拒绝／派发／完成记录关联，不能把修补次数当成功次数。

在可比任务中同时观察：原始错位比例、修补后的拒绝和浏览器失败、模型轮次、平台 I/O、
重试、排除人工等待的端到端耗时，以及分别统计的 cache_read、cache_creation、
uncached_input 和 output。离线回放无法提供这些整体收益。

模型升级后，先观察原始输出，必要时在隔离回放中让别名返回诊断而不自动迁移，对比
完成率和成本。如果别名已罕见或自动修补没有净收益，可以删除对应迁移规则及接入代码；
保留标准 schema、目标身份、权限、状态检查和明确错误反馈。不因模型变强而删掉执行边界。

当前测试目录和 probe_tests 被仓库忽略规则忽略。本轮未改变忽略规则、未暂存或提交。
后续提交需显式纳入 `tests/test_capability_repairs.py`、
`tests/test_phase_local_access_intent.py`、已有接入验证 `tests/test_tool_argument_pipeline.py`
以及需要保留的回放脚本，避免只提交生产修改而丢失回归证据。

## 最终验证记录

- 核心五个测试文件：**181 passed**。
- 扩展九个文件：**170 passed、14 subtests passed、15 failed**。
  两组不重复，合计 **351 passed、15 failed**；不能表述为“全部回归通过”。
- 修改前模块对照：两份失败测试文件仍为 **88 passed、15 failed**；JUnit 的失败用例
  标识集合与修改后完全相同。没有为了消除失败而修改这些测试或相关 Workflow 合同。
- `git diff --check`（本轮生产修改与文档范围）通过；WebCross 平台仓库仍干净。
- 代码只补强 `harness/tools/capability_repairs.py` 与
  `harness/tools/path_authorization.py`；ClaimExtractor 和其他已有实现经过审查，
  本轮未再修改。新增／补充测试和离线回放脚本见上文。
- 当前改动仅在工作区，尚未部署到运行进程；本轮未启动业务任务或宣称端到端性能提升。
