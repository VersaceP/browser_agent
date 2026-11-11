# Workflow 模式重构计划

更新：2026-10-18（收敛版）。本文取代此前的“Workflow 与 Skill 共享执行循环”方案。Skill 模式、Builder 草稿、旧 Workflow 模式的 stage/trial/orchestration 机制全部撤销；新模式基于 Tau 循环重新构建，不继承旧类。

## 1. 定位

Workflow 模式负责把用户目标做成可复用的 Workshop 包，并在复用时处理参数、执行证据和必要的修复。**包执行已经明确的操作；Agent 判断当前事实意味着什么、下一步怎么办；Harness 保证判断和执行对应正确的目标、版本、页面与权限。**

开发通常需要多轮；对已验证范围内的新参数，复用目标是一次执行完成。ReAct 循环处理新情况，不能让每次复用都重新开发。

| 命令 | 行为 |
|---|---|
| `/browser` | 直接完成浏览器任务（不改） |
| `/workflow` | 列出已发布的 Workflow |
| `/workflow <name\|id> [需求/资料]` | 用已发布版本执行；多商品时每商品一个任务 Agent |
| `/workflow-create <需求>` | 新建草稿：作者 Agent 编码、执行、观察、修复 |
| `/workflow-edit <name\|id\|草稿任务ID> <需求>` | 与 resume 共用原作者草稿和上下文；包尚无草稿时才建立首个编辑任务 |
| `/workflow-draft` | 列出草稿 |
| `/workflow-publish <草稿任务ID\|name>` | check → 打包 → 原子写入 `workflows/<slug>/`，生成 `final.workshop.json` |
| `/workflow-rename <name\|id> <新名>` | 只改显示名，模板 ID 与 slug 不变 |
| `/workflow-delete <name\|id>` | 删除已发布包（需确认） |
| `/workflow-stop [任务ID\|all]` | 停止派发并取消已登记的 WebCross 执行，保存实际状态 |
| `/resume <任务ID> [补充]` | 续跑 Workflow 任务 |

不做：Skill 编排与发布、独立 trial 检查点、固定错误码分流、业务门禁、直接写 `webcross.db`、导入 Shop（等 WebCross 开放 `workshop.import`/`replaceWorkflow` 给配对 Profile 后接到 publish 之后）。

## 2. 问题分类

由 Agent 根据原始请求、响应、时序、页面证据和实际代码判断，不做错误码到处理路径的机械映射。

| 类型 | 典型情况 | 处理 |
|---|---|---|
| 业务问题 | 素材不合要求、资料缺失、所需属性无可选值 | 告知用户事实与影响，暂停受影响部分；**不进入 coding，不在包里兼容** |
| 代码缺陷 | 漏读虚拟表格、误认占位图、失焦丢值、选错组件 | 作者修复，局部验证，检查受影响结果 |
| 平台或观测错误 | WebCross 观测失败、结果不可取得 | 保留原始回执，核查已发生的效果；不用业务代码掩盖 |
| 平台风控 | 验证码、身份验证、操作限制 | 停止相关自动操作，交给用户 |
| 原因未明 | 超时、字段为空但证据不足 | 先补证据 |

登录过期不直接归为风控；WebCross 返回错误不等于平台代码缺陷。已明确且不依赖该问题的其他工作可以继续。

## 3. 唯一交付物：Workshop 包

```
workflows/<slug>/                 # 已发布（只保留最新一版）
  workflow.json                   # WebCross 元数据：schemaVersion/name/description/entry/timeoutMs/availability
  contract.json                   # Harness 合同：templateId、参数说明、阶段、报告格式、能力声明、已验证范围
  params.schema.json              # 可选：业务输入 schema
  src/**.js|ts                    # 源码
  fixtures/、tests/               # 可选：离线快照回归
  README.md
  final.workshop.json             # 发布时生成，与上面内容逐字节对应
  publish.json                    # 发布记录：contentHash、显示名、发布时间、来源草稿
```

- **合同与源码同版本**：`workflow.json`、`contract.json`、`params.schema.json`、`src/`、`fixtures/`、`tests/`、`README.md` 共同计算 `contentHash`。`final.workshop.json` 与 `publish.json` 不参与哈希。
- **发布即最新**：`final.workshop.json` 只由 publish 从同一草稿快照生成。执行时校验已发布目录的 `contentHash` 与 `publish.json` 一致；不一致（目录被直接修改）则拒绝执行，提示用 `/workflow-edit`。
- **参数与商品实例分离**：模板不携带真实商品参数。传了 `input.product` 就完整采用；字段不存在才用包内默认值；传入 `null`、空对象或不完整对象不回落到另一商品。
- **三种身份**：模板 ID（长期）、版本 `contentHash`（某次源码）、WebCross `workflowId`（某次执行）。
- **执行快照**：每个任务实际执行的版本按 hash 复制到 `worktree/<任务ID>/workflow/versions/<hash>/`，并发任务固定在启动时的版本，证据可追溯到源码。不做回滚与版本列表。
- **哈希可追溯而非相等**：Harness 会给源码注入权限包装，`sourceHash` 与 `executionPackageHash` 本来不同；验收检查执行包能追溯到固定的 `contentHash`。

## 4. 架构

继续使用已适配的 Tau 核心（`harness/_vendor/tau_agent`、`harness/runtime/agent_loop.py`）。原先放在 `skill_builder`/`skill_runtime` 中但属于 Tau 桥接的通用部分（provider 适配、上下文压缩、用量记录）迁入 `harness/runtime/`。

```
harness/workflow_mode/
  package.py      # 包读取、contentHash、打包 final.workshop.json、check
  registry.py     # 已发布包索引、草稿索引、publish/rename/delete
  platform.py     # 直连 WebCross：执行、状态、取消、只读观测；原始回执落盘
  runs.py         # 执行登记：任务 → 版本 → 有效输入 → 页面 → workflowId；停止
  usage.py        # token 与净时长统计，不设任务预算
  acceptance.py   # 结构、引用、身份与客观一致性校验
  tools.py        # Agent 工具
  session.py      # 任务 Agent / 作者 Agent（同一 Tau 循环，不同工具集）
  prompt.py
  cli.py          # 命令路由
```

| 层 | 职责 |
|---|---|
| Harness | 命令路由、版本快照、页面归属、权限、运行控制、证据存储、用量统计 |
| Agent | 理解目标、准备参数、判断证据、选择恢复、修复代码、判断业务完成 |
| Workshop 包 | 执行已明确的流程、收集结果事实、输出未解决事项及恢复数据 |
| WebCross | 浏览器动作、观测、调度、平台执行记录 |

## 5. 两条流程

### 5.1 新建或修改

```
/workflow-create 或 /workflow-edit → 作者 Agent（单一上下文 ReAct）
  读需求/资料 → 观察页面 → 编辑草稿 → check/离线测试
  → Workflow.execute（选择阶段）→ 读原始回执 + 页面证据 + 业务报告
  → 证实是代码缺陷则修正，产生新草稿版本，按当前事实续跑
  → 业务问题/缺授权 → 问用户
  → 保存草稿（finish）→ 用户 /workflow-publish
```

编码、执行、观察在同一上下文，不另设观察者 Agent；但 Agent 必须能读取原始证据，不只看自己写出的摘要。

### 5.2 运行已有包

```
/workflow <name> → 拆分商品 → 每商品一个任务 Agent（并发，各自页面）
  准备参数 → 本地校验 → 页面绑定 → Workflow.execute → 原始回执、页面证据、业务报告
  → 同一 Agent 对照目标判断：完成 / 按现有能力恢复 / 报告代码缺陷 / 需要用户
```

- 一个任务 Agent 负责一个页面；同页面串行，不同页面独立。同一 Fleet 共享登录态与服务端资源，不承诺完全互不影响。
- 运行中的任务固定使用启动时的版本。任务 Agent 只上报缺陷证据，不改源码；修复由单一作者形成新版本（`/workflow-edit`）。是否停止其他任务由用户决定（`/workflow-stop`）。
- 复用时 Agent 判断适用范围：已有能力满足则直接运行；缺少的能力适合长期复用则先补模板；差异较多或属本次特有，则运行模板能正确完成的部分，再用 Browser 补充，并告知用户模板遗漏的能力。补充顺序服从实际依赖（上游缺失必须先处理）。Browser 补完不等于模板具备该能力。

## 6. 阶段执行合同

修改某个阶段后只运行受影响的阶段，不从头重放。

1. 确认现场：当前页面、对象身份、上一轮已产生的效果。
2. 提交完整源码包给 `Workflow.execute`，通过 `input.stages` 选择范围。
3. 传入当前有效参数与核对过的检查点（如 `input.product.resume`）。
4. 包对每个阶段先检查已有结果，再执行必要动作；阶段声明前置条件、输入与输出。
5. 阶段通过后，任务收尾时核验最终业务结果。

改公共模块时检查所有受影响阶段。代码修改后的续跑是新的 `Workflow.execute`；`Workflow.resume` 只恢复原暂停执行，不加载新代码。Harness 不另做阶段调度。

## 7. 页面转移与权限

Harness 给每次执行注入权限包装：动作只作用于绑定页面，文件只能用已授权的规范路径，`File.write`/`Runtime.evaluate` 需在 `contract.json` 中声明。页面转移顺序：

> 包返回发现的新页面 → Harness 核对来源（opener、Fleet、URL）、任务归属与授权 → 登记绑定 → 下一段 `Workflow.execute` 作用于新页面

不假定长脚本执行中 Harness 能动态更改权限。新建页面、HITL 由 Agent 通过 Harness 工具完成，不向包开放所有页面或 Action。

## 8. 报告与验收

三类记录分开保存：

| 记录 | 产生者 | 内容 |
|---|---|---|
| 执行记录 | Harness／WebCross | 包版本、有效输入、页面绑定、调用请求响应、运行状态、时间 |
| 业务报告 | Workflow | 已观察事实、完成声明、未解决事项、证据引用、恢复数据 |
| 验收判断 | 同一 Agent | 对照原始目标的结论、接受的省略及依据、证据缺口、下一步 |

四步验收：

1. Agent 根据原始用户目标、资料和本次有效输入形成验收要点。
2. 包返回实际执行及结果事实。
3. Harness 检查结构、引用、身份和客观一致性（版本可追溯、执行状态、失败调用已被报告提及、证据引用真实存在、页面归属一致）。
4. Agent 判断这些事实是否足以满足目标。

“写了跳过理由”不等于用户允许跳过。平台运行状态与业务结果分开：`failOnIncomplete` 只改变呈现方式（Shop 抛异常显示失败，Harness 正常返回 `needs-decision`），不改变业务结论；真正的代码异常仍保留异常与原始调用证据。

## 9. 用量统计

Workflow 不设任务 token 或净时长上限，也不因累计用量暂停或询问追加预算。
旧 `harness.workflow_budget` 配置被忽略，旧 `budget.spent` 自动转为用量记录，
恢复草稿不清空历史统计，原 `budget_exhausted` 状态不再阻止继续执行。

- 原始用量事件分别保留 `uncached_input`、`cache_read`、`cache_creation` 与 `output`，以及模型轮次和工具 I/O。
- 展示的计费 token 口径仍为 `uncached_input + cache_write + output`；`cache_read` 单独记录。
- 净时长扣除等待用户、HITL 与子任务；各任务独立记账。
- `/workflow-stop` 继续有效；权限、身份、协议和验收校验不变。
- Agent 根据目标和证据判断是否继续验证、报告缺口或完成，不采用按成本或重试次数强制停止的规则。

## 10. 验证

- 解析与状态逻辑：历史快照离线回归。
- 交互过程：面板开关、失焦、动态字段等状态变化。
- 新参数实跑：固定包版本，只换输入，使用未参与开发的商品。
- 恢复：中断后续跑，核对已完成操作未重复执行。
- 入口：真实 `/workflow <name>` 路径，包括参数准备、页面绑定和权限包装。

指标：首次成功率、无需改代码完成率、恢复后完成率、人工介入原因、扣除人工等待的端到端时间；token 分 `cache_read`、`cache_creation`、`uncached_input` 记录，同时看模型轮次、平台 I/O 与重试。

## 11. 实施步骤

在 `refactor/workflow-mode` 分支进行。

1. 删除 Skill 模式与旧 Workflow 模式（`harness/skill_builder`、`harness/skill_runtime`、`harness/workflow_mode`、`skills/`、`/skill*` 命令、`skill_orchestrator` 配置、`forced_skill_*`），保留 browser 依赖的 `harness/workflow/` 等模块；Tau 桥接迁入 `harness/runtime/`；删除无用配置参数。确认 browser 模式与现有测试不退化。
2. 新建 `harness/workflow_mode/`：包、注册表、平台、执行登记、用量统计、验收、命令。
3. 把 PDD r21 迁移为第一个已发布包（参数与商品实例分离、Harness 入口适配），生成 `final.workshop.json`。
4. 作者 Agent：`/workflow-create`、`/workflow-edit`、草稿与发布命令。
5. 多商品并发：每任务独立参数、页面和执行记录，共享包版本。
