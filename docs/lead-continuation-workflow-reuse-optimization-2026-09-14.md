# Lead / Browser 综合优化报告：执行正确性、上下文与续跑复用

日期：2026-09-14。范围：当前工作区的 Harness、WebCross Workflow 实现，以及本会话已核验的运行证据。

本文最初是实施设计与验收计划。2026-09-14 已按本文批准范围完成首批 Harness 实现；状态见下表。模型配置、WebCross 原生实现及用户产物未随本批修改。

| 实施项 | 当前状态 |
|---|---|
| 下载操作与页面状态失效解耦 | 已实现；下载自身不再制造页面重观察门禁，真实导航/弹窗事件仍负责失效 |
| `browser-continuation-v1` 决定协议 | 已实现；非终态 worker 在既有结束回合内输出，`done` 不得请求续跑 |
| phase 续跑账本、预算与回执身份 | 已实现；重复回执不刷新预算，历史 `reserved/uncertain` 失败关闭并交回 Lead，不宣称跨层 exactly-once |
| 续跑接入统一完成事件循环 | 已实现；派发函数不嵌套长期 wait，支持运行中兄弟 worker 和真实 `_handles`/`asyncio.Task` 路径 |
| Workflow 不可变定义引用与局部修改 | 已实现为 Harness 工具；原 WebCross `Workflow.execute` schema 保持不变，敏感变量每次重绑定 |
| Lead 紧凑交接与遥测 | 已实现续跑决定、定义引用、预算/派发状态及 Lead 唤醒原因；完整证据仍可按引用读取 |
| WebCross 下载父目录与 AX 目标解析 | 未在本批修改；仍按 §7 由 WebCross 运行链处理 |
| 任意 Workflow 原实例断点恢复 | 未实现；仍依赖 WebCross 后续平台能力 |
| Browser 上下文与模型配置对照 | 未执行；仍按 §6 单变量测量后决定 |

本文沿用原文件路径，现已合并前述 Browser 优化项；历史下载归属、页面交接、partial 续跑与 Workflow 复用共同纳入统一计划，不再为每一点另建专项优化报告。Browser 上下文和模型配置方案标记为待讨论，AX 目标解析列为 WebCross 侧依赖。

### 2026-09-14 复审修复

本轮修复限于 Harness 工作区，未修改或验证 WebCross 安装实例：

- Workflow 定义采用 `workflow-definition-v2`。哈希同时绑定内容、可执行标记、敏感值重绑标记和必填变量名，避免脱敏后的相同内容覆盖不同执行约束。加载时重新核验。旧 v1 引用明确拒绝复用，需要从原始授权定义重新保存；不能安全地从旧哈希推断原约束。
- 在执行 RPC 前，将定义引用写入日志与 trace；异常、取消路径也保留引用到 worker 回执。准备记录的执行结果标为 unknown，不作为成功步骤证据，不触发自动重放。
- 重规划重置失败或依赖阻塞 phase 的执行状态时，保留续跑账本和派发输入。已经消耗的预算不会清零，旧决定仍受计划版本和合同身份约束。
- `Download.list` 只刷新已经归属于当前 worker 的文件，不把清单中的其他路径加入 artifacts；清单仍保留为诊断证据。此修复不等同于为所有下载控制操作建立了完整的来源追踪。

针对性回归覆盖哈希冲突、约束篡改、异常前引用持久化、重规划预算和下载清单归属；本地测试不能证明部署端断线后原 Workflow 的执行进度，原位恢复仍依赖平台回执。

## 1. 结论与范围

可以复用 page-session，但应复用其**观察事实提取、脱敏、摘要和上下文组装**，不能直接把它当成预算、派发去重或 Workflow 检查点。

建议形成三类记录，分别沿用已有基础设施：

| 记录 | 回答的问题 | 权威来源 | 主要消费者 |
|---|---|---|---|
| page-session | 这个页面之前观察到什么、执行过什么 | 浏览器调用和执行事件 | 后续 Browser worker |
| phase 续跑账本 | 哪个回执已处理、预算是否使用、是否已经派发 | Harness 状态转换及派发回执 | Harness 调度器 |
| Workflow 定义与执行证据 | 原脚本是什么、哪次执行返回了哪些事实 | 原定义、平台回执及事件 | Browser worker、恢复逻辑、按需查看的 Lead |

Worker 对“是否继续当前目标”的判断作为独立的模型决定保存，不能写成 page-session 中已经发生的页面事实。

统一优化范围如下；各项状态不能互相替代：

| 主题 | 状态与责任 | 在本报告中的位置 |
|---|---|---|
| 下载父目录准备与错误分类 | live 已复现；WebCross 执行链及部署版本待核对 | §7 |
| 下载状态与页面状态解耦 | Harness 已移除无条件页面同步；真实页面事件仍负责失效 | §7 |
| 历史下载归属与交付回执 | Harness 产物采集问题，纳入统一证据整理 | §3、§6 |
| 页面交接与 partial 续跑 | 已有修复基础，后续协议与预算方案已列出 | §3–4 |
| Workflow 原定义与局部修改复用 | Harness 可先实现；原位断点恢复依赖 WebCross | §5 |
| Browser 长上下文、重复检索与过量输出 | 需分原因测量，方案待讨论 | §6 |
| AX 节点可见但操作无法定位 | WebCross 侧待修，不在 Harness 内绕过 | §7 |

续跑与复用方向保留两个主题：

1. 将符合原合同的 partial 续跑接入完成事件循环，并持久化预算及回执消费记录。
2. 将 Workflow 定义保存为可引用资源，允许模型用短引用和局部修改复用，减少断线或失败后重新生成整个脚本。

通用的 Workflow 原位断点恢复需要 WebCross 进一步支持，不作为上述两项完成的前提。下载目录与页面状态失效问题单列，不能因为 Lead 调度修复而宣称它们已解决。

## 2. 已核查事实与证据边界

### 2.1 已修复并核查的内容

- 普通 `wait_browser_agents(worker_ids=null, mode=all, timeout_seconds=null)` 在 Harness 内按完成事件推进。
- 同一批多个已验证完成的前驱可以共同满足唯一后继的依赖，只执行一次原 spawn 入口。
- 明确 deadline 不触发自动后继派发或自动续跑。
- 去重集合明确只在当前 Lead 进程内有效，没有被宣称为持久化恢复账本。
- 直接调用和 Workflow 内的 `Page.getState` 可更新已有 page-session 的 URL。
- 只读 URL 更新不新建 page-session、不增加 worker 历史、不记录成业务动作。
- Workflow 变量 URL 解析后再脱敏，导航历史与最终 URL 都使用脱敏结果。

前一轮独立选测为 142 passed，包含 page-session、自动续跑/交接、Lead E2E、下载事件账本、下载超时对账；自动续跑/交接模块单独为 30 passed。此处引用该次检查，未在本报告编写轮重新运行。外部报告的 155 项或更大全量测试结果不计入本报告的独立验证数量。

源码入口：

- [调度与自动续跑](../harness/tools/lead_tools.py)
- [page-session](../harness/page_session.py)
- [worker 结果与 page-session 记录](../harness/spawner/spawner_worker.py)
- [worker 结果投影](../harness/results/worker_result.py)

### 2.2 性能基线只用于确定方向

任务 `60730ec6178449208e8ae6ccdc0a8b0f` 的既有日志分析：

| 指标 | 数值 |
|---|---:|
| 总墙钟时间 | 61分02秒 |
| 人工计划确认 | 2分11秒 |
| 扣除人工等待的任务墙钟 | 58分51秒 |
| Lead 模型调用 / 输出 token | 90 / 115,777 |
| Browser 模型调用 / 输出 token | 221 / 584,707 |
| Browser worker 累计模型时间 | 47分48秒 |
| Browser worker 累计执行时间 | 55分24秒，包含并行重复计时 |
| 完成阶段 | 3/4 |

出处：[任务日志](../worktree/60730ec6178449208e8ae6ccdc0a8b0f/run.jsonl)。这是未完成的旧运行，不能直接拿它与完整成功任务比较总速度。Lead 等待 worker 的时间与 Browser 执行重叠，不能从任务墙钟中再次扣除。输出 token 包含推理消耗；缓存读取不能按普通输入成本计算。

本报告没有量化“断线后 Workflow 重写占多少 token”，也不预先承诺降低固定百分比。后续通过定义引用命中率、实际输出 token 与完成率作对照。

## 3. page-session 能复用什么

### 3.1 可直接复用的逻辑

| 已有逻辑 | 续跑方案中的用途 | 注意事项 |
|---|---|---|
| `extract_page_sessions` / Workflow 执行步骤提取 | 告诉新 worker 原页面做过哪些动作 | 动作返回成功不等于业务目标已经完成 |
| URL 更新和脱敏 | 提供最近观察的页面位置及导航历史 | 最终 URL 仍是历史观察，不是当前状态保证 |
| `filledValues` | 减少重复填表 | 来源是之前执行的输入动作；仍需核对当前回显 |
| `completedActions` | 提醒模型避免重复点击、提交或下载 | 不作为通用幂等判据 |
| `failedPaths` 聚合 | 提供已尝试路径与失败次数 | 次数是事实，不自动裁决业务路径不可行 |
| `render_page_session_context` | 为续跑 worker 组装页面交接内容 | 不再把同一页面摘要重复塞入额外的续跑文本 |
| `page_session_context_for_pages` | 只取实际复用页面的记录 | 保持任务、Fleet、页面绑定的现有范围 |

### 3.2 不应复用为调度依据的部分

当前 page-session 会按数量裁剪数组；动作去重按 `(method, purpose)`，失败按 `(method, purpose, errorCode)` 聚合；这些都是摘要键，不是执行身份。较早的两次不同操作可能合并成一个摘要。

它还通过物理 JSON 文件读写，并将写入失败记录后吞掉。这个行为适合“丢失后只损失效率”的辅助上下文，不适合“丢失后可能多派一次任务”的控制状态。

因此：

- 不能用 `workers` 列表充当已消费回执集合。
- 不能用 `completedActions` 数量充当续跑次数。
- 不能把 `failedPaths` 的次数当作强制停止业务尝试的门禁。
- 不能根据某个页面记录消失，认定此前没有执行或没有下载。
- 不能从裁剪后的记录还原 Workflow 的精确变量、循环位置或执行栈。

预算和去重优先扩展现有 [phase 状态](../harness/task_control/phase_lifecycle.py) 与 [状态存储](../harness/task_control/state_store.py)，不复制一套 page-session 文件式账本。

### 3.3 复用前需要保留的边界

page-session 当前把 worker artifacts 的一个截断列表附给各页面，不能视为已经完成文件到页面的准确归属。历史 `Download.list` 污染 artifacts 的问题也尚未因本方案消失。

续跑包应优先引用已验证的 phase 产物和对应执行回执；page-session 的文件列表只作辅助。文件归属后续按实际操作身份及明确引用关系整理，不靠目录名、时间接近或站点名称猜测。

## 4. partial 续跑的具体设计

### 4.1 已实施结构

`_auto_continue_phase` 现在逐一处理同批完成回执，允许仍有 pending worker；它只派发，不在函数内部等待。是否继续来自 Browser 结束回合的结构化决定，Harness 只检查协议、合同、身份、预算和路由，再回到统一完成事件循环。direct worker 的既有判断保持不变。

三个优化点：

1. 将续跑派发与等待分离，统一回到已有完成事件循环。
2. 将“是否继续当前业务目标”的判断放到 worker 已有结束回合中，而不是由 partial 状态码自动推断。
3. 将自动预算与回执消费持久化，避免函数重新进入后重新得到一份预算。

不直接修改 browser mode 共用决策函数的全部语义。先为 Lead phase 引入明确协议，保留 direct worker 的既有行为，避免无意扩大本轮范围。

### 4.2 结构化决定放在哪里

现有 `final_answer` 包含 status、字符串 answer 和 reason；`answer.next_steps` 属于自由文本建议，不宜直接机械派发。

现已新增可选、带版本的结构化 `continuation` 对象：

```json
{
  "action": "continue_current_phase",
  "reason": "已有可验证结果，剩余目标可在原合同内继续完成",
  "remainingObjective": "对尚未完成部分继续执行",
  "evidenceRefs": ["已存在的产物或执行回执引用"],
  "workflowRef": "可选的原脚本引用"
}
```

`action` 的另一值为 `needs_lead_review`。对象不允许模型自行指定新的 phaseId、Fleet 身份或扩大权限；这些由当前派发上下文绑定。语义理由、剩余目标始终标记为 worker 的判断，证据引用指向独立记录，不把判断提升为验证事实。

在 Worker 的现有结束回合输出，避免新增一次“是否继续”的专用模型调用。策略切换、是否继续搜索、空字段是否可接受等问题仍由模型结合原目标判断。

### 4.3 没有结束回执时怎么办

致命断线、上下文硬限制或进程中断可能发生在 final_answer 之前。这时没有新增的 continuation 决定，不能声称该方案对所有中断都能零模型回合恢复。

默认返回 Lead，附上已保存页面记录、执行定义引用、已完成证据及结果未知范围。未来若沿用更早的模型决定，必须明确它关联的证据版本与失效条件；首版不根据旧自由文本自动续跑。

### 4.4 完成事件处理顺序

1. 接收 worker 终态，先保存验证结果、产物引用和模型决定。
2. 在持久化状态中认领该回执；重复到达只读取已记录结果。
3. `done` 且阶段已验证完成，走现有唯一后继调度逻辑。
4. 未完成但有有效的 `continue_current_phase` 决定，检查原合同、绑定及剩余预算，经原 spawn 入口派发。
5. 派发成功后记录关联 workerId，回到统一事件等待；不在续跑帮助函数内再次长期等待。
6. 需要 Lead 决策的回执及时返回，其余运行中的 worker 保持原状态，不能因一个失败而全部取消。

同批多个可续跑 worker：依赖及独占资源不冲突时允许逐个走原 spawn 入口；存在资源冲突时进入可审计队列或返回 Lead。不能以到达顺序暗中决定业务优先级，也不默认取消正在运行的兄弟任务腾位置。

显式 deadline 的原行为保持：等待到期交回控制，不在调用者背后触发新的自动工作。

### 4.5 预算、去重和崩溃窗口

建议在既有 phase 状态下加入续跑控制记录，身份至少包括 taskId、计划版本/合同标识、phaseId、来源 workerId 和回执版本。使用稳定回执版本或规范化摘要；不要对包含时间戳的整个结果随意哈希作为去重键。

| 状态 | 含义 | 进程恢复时动作 |
|---|---|---|
| received | 回执已持久化，尚未决定派发 | 从相同回执继续处理 |
| reserved | 已预留一次预算和派发身份 | 先查 worker/phase 派发记录，不直接再次 spawn |
| dispatched | 已记录新 workerId | 等待该 worker，不再使用来源回执派发 |
| rejected | 原 spawn 入口明确拒绝且未启动 | 保存原因；按拒绝类型返还预留或交回 Lead |
| uncertain | 派发结果无法确认 | 对账；不能把缺少结果当作未执行 |

同一 phase 的自动次数跨 wait 累计，进程重启不能重置。计划修改后如何继承额度需有明确规则：语义不变的修订不应免费刷新资源预算；新批准预算可建立新版本，但仍受任务总预算约束。

数据库模式使用既有事务/版本检查；file 模式使用串行状态变更与原子替换；dual 保持 FileStore 为 primary。先核实当前状态存储的并发能力，再实现预留与提交，不能凭 JSON 字段存在就宣称具备原子性。

“发出 spawn”与“记录 dispatched”之间仍可能崩溃。没有跨两层的幂等派发与对账证明前，只能承诺不盲目重试，不能宣称任意崩溃下 exactly-once。

### 4.6 机械校验的普适性与恢复路径

| 校验 | 为什么适合机械层 | 可能影响 | 恢复路径 |
|---|---|---|---|
| 对象结构与状态一致性 | 协议事实，与业务无关 | 旧 worker 无新字段 | 返回 Lead，兼容旧回执 |
| task/phase/合同归属 | 防止错任务或过期授权派发 | 计划修订使旧决定过期 | Lead 结合新计划重新判断 |
| Fleet、权限、页面独占 | 现有授权与并发约束 | 资源暂时不可用 | 保留决定，等待资源或返回 Lead |
| 明确资源预算 | 已授权算术约束 | 有价值任务也可能用尽预算 | Lead 处理资源分配，不伪造完成 |
| 回执去重和派发预留 | 状态一致性 | 崩溃时会停在未知状态 | 对账后再决定，不重复副作用 |

无进展次数、字段是否为空、何时换策略、是否需要更多样本不新增硬门禁。将这些事实送入已有模型判断，避免额外专用裁判回合。

## 5. Workflow 定义引用与局部修改复用

### 5.1 现有能力

Harness 已有 `ExecObserver`、`executionTrace`、`completedSteps`、`variablesAtFailure` 和完整响应落盘；这些优先复用。现有响应投影不是完整的“执行前保存原定义、失败后按引用调用”协议。

WebCross 新 `Workflow.execute` 创建新 workflowId，并从首步执行；`Workflow.resume` 只恢复 paused 状态。[执行入口](../abcp-platform/packages/actions/src/domains/Workflow/execute/exec.ts)、[恢复入口](../abcp-platform/packages/actions/src/domains/Workflow/resume/def.ts)、[运行状态服务](../abcp-platform/packages/dispatcher/src/runtime/workflowService.ts) 均可核对。

### 5.2 定义身份与执行身份分开

| 对象 | 保存内容 | 生命周期 |
|---|---|---|
| Workflow 定义 | steps、变量模板、上下文约束、版本与内容摘要 | 不可变，可被多次显式引用 |
| Workflow 执行 | definitionRef、workflowId、来源 worker、事件范围、状态、证据引用 | 每次执行独立 |
| Workflow 修改版本 | baseRef、局部变更、变更后摘要 | 新定义，不覆盖原定义 |

持久化必须发生在发送前，避免 RPC 未返回时连原脚本都无法定位。复用通用 Storage 资源入口支持 file/dual/db；不只把内容放进可能截断的 run 日志。

脱敏 trace 不能直接当可执行源。敏感参数按现有授权机制重绑定；不得用掩码字符串执行，也不另建明文密钥档案。无法恢复必要变量时返回具体缺失项。

### 5.3 模型调用形态

Harness 侧现已提供 `execute_saved_browser_workflow`，接受 `definitionRef`、`definitionHash` 和可选局部变更。首次执行保留现有完整定义输入方式，发送前保存并返回引用。该入口是 Harness 工具，不冒充或修改 WebCross Action catalog。

局部变更可采用带 base 摘要的结构化路径操作。路径是否存在、修改结构是否完整、变更后是否仍符合 schema 属于机械校验；变更是否解决业务问题由模型判断。任何变更都产生新版本，不能原地修改已派发实例。

模型无需重新复制长 URL、大数组、重复下载步骤，只输出引用与实际变化。执行前仍走当前工作类型、权限、Fleet/page 绑定、动态 schema 和 Workflow 校验入口。

### 5.4 重连后的复用决策

| 可观察状态 | 建议动作 |
|---|---|
| 原实例仍 running | 继续观察原实例，避免另开一次 |
| paused 且当前身份仍有访问权 | 按已授权决定调用 resume |
| succeeded 且证据完整 | 消费原结果，不重跑 |
| failed 且有执行回执 | 提供原定义引用、成功步骤与失败点，由模型选择修改或重跑 |
| 结果未知、实例丢失、事件不完整 | 查询可得操作事实，明确未知范围，再交模型判断 |

失败不等于整个 Workflow 没有副作用。复用定义解决的是 LLM 重写成本，不自动赋予整份重跑的安全性。

首版不机械地删除“成功步骤”生成后缀：循环、分支、变量提取、store 状态和已过期元素目标都可能形成依赖。模型可以提交局部变更，但不能把任意步骤裁剪伪装成平台保证的断点恢复。

### 5.5 WebCross 后续能力

若需要任意 Workflow 的精确断点恢复，平台需要持久化定义、程序位置、变量/store、循环及分支状态、授权归属和副作用回执，并明确恢复接口的幂等语义。

Harness 先实现引用复用，即使平台无法恢复原执行实例，仍能让模型通过短请求选择下一步；无需等待整套平台检查点系统。

## 6. Lead / Browser 上下文、证据交接与输出开销

### 6.1 已有机制与统一产物归属

page-session 主要供 Browser worker 使用，不应每次完整注入 Lead。Lead 的默认交接仅包含：阶段状态、验证结论、续跑决定及派发结果、未解决问题、证据/定义引用。

完整脚本、逐步回执、文件全集保持可查询。复用已有 `worker_handoff_projections` 与结果落盘，避免新建第二套摘要系统；投影不以“是否超过文件落盘阈值”决定语义字段是否齐全。

控制层去重与模型上下文去重分别处理：一个回执没有重复触发 spawn，不代表它没有被重复放进对话。后续 wait 应返回新事件及必要当前状态，显式历史查询仍可获得完整证据。

已有文档中的 306 KB / 367 KB 是完整等待结果大小，不是全部直接进入模型的 token 数。评估应记录最终投影字节数和真实模型 usage。

历史下载不应通过 `Download.list` 的响应路径采集自动成为本次交付物。将“查询到的下载”“本次发起的操作”“明确引用的已有资源”“已验证交付”区分为带来源的事实，在同一产物索引中表达，不复制多套下载账本。来源依据操作身份和明确引用，不按路径名称或时间邻近猜测。

这同时改善 Lead 回执、page-session 和后续 Browser 上下文。默认交接提供数量、验证结论、异常及清单引用；完整清单仍可按需查询。先修正归属再做摘要，避免把错误材料压缩成更难发现的错误结论。

### 6.2 Browser 的三个问题不能混为一谈（待讨论）

任务 `60730ec6178449208e8ae6ccdc0a8b0f` 中，Browser 有 221 次调用、584,707 输出 token，输入缓存命中率约 97.83%，单次输入峰值 372,898 token。日志中 `local_fs_search` 为 49 次、`local_fs_read` 为 37 次、`find_in_axtree` 为 25 次。这些是不同工具的调用数量，不等于无效查询次数。

| 现象 | 已有证据能支持的判断 | 不能直接推出的结论 |
|---|---|---|
| 上下文很长 | 模型反复接收大量历史材料 | 单凭长度不能证明它造成了长考或失败 |
| 重复检索 | 页面材料定位与历史查找占用多轮 | 重读未必浪费，页面改变或前次结果缺失时可能必要 |
| 输出很大 | 包含推理、工具参数、结构化结果等成本 | 不能把全部输出理解为可见啰嗦文本 |

97.83% 缓存命中只说明输入复用较多，不保证注意力使用有效，也不能代替延迟和输出指标。缓存 token 与未缓存输入分别统计；不把所有历史输入量都计为能省下的普通输入费用。

部分检索明确是 WebCross 的目标解析错误与下载执行错误引起的恢复行为。这些回合记入上游缺陷成本，不能据此给 Browser 加“最多再查一次”之类机械限制。

### 6.3 先做逐回合归因，再决定改动

对相同 Browser worker 的模型请求、工具调用与响应建立对照，区分下列内容来源：

- 系统指令、工具定义和重复 schema；
- 当前页面证据与同一页面的过期证据；
- 历史错误、恢复尝试和文件检索结果；
- 已执行或重新输出的 Workflow 定义；
- 提取结果、产物清单、交付回执；
- 可见工具参数、可见文本，以及供应商明确报告的 reasoning token。

若供应商不提供 reasoning token 明细，只报告总输出及可见内容大小，不能用“输出 token 减去字符数”伪造推理消耗。耗时同样区分模型响应时间与已知工具时间，不把模型响应延迟全部叫作思考时间。

每轮标记输入的证据引用、文档身份/版本（可得时）、查询范围、截断标志、返回位置；分析相同资料是否被反复检索、重复注入或因摘要缺字段而回读。计数用于诊断，不直接限制模型行动。

### 6.4 可讨论的优化选择

| 方向 | 建议设计 | 风险与验证 |
|---|---|---|
| 让证据更容易查到 | 复用已有 offload 索引，返回稳定引用、内容版本、结果范围与截断原因，支持按范围/字段继续读取 | 不能把未取到的内容说成不存在；分别测检索成功率与回合数 |
| 减少重复注入 | 同一证据版本重复返回时提供引用与变化说明；保留显式完整读取入口 | 页面变更必须产生新证据身份，不能仅按 URL 或查询文本缓存 |
| 整理旧页面历史 | 用 page-session 的动作事实和可回溯证据替代部分过期全文 | 保留用户目标、未解决矛盾和失败信息；记录不能冒充当前 DOM |
| 降低工具参数重写 | Workflow 定义引用和局部修改复用 | 执行检查、敏感值恢复和目标新鲜度要求仍适用 |
| 控制输出重复 | 已落盘的提取结果和交付清单使用引用，避免 final_answer 再复制全文 | 不删需要 Lead 判断的失败、偏差和不确定性 |
| 模型/思考配置对照 | 验证真实发送参数与供应商支持后，分别比较当前配置、较低思考配置或另一模型 | 不假定所有 provider 都支持相同 reasoning 档位；不同时改多个变量 |

当前不决定统一缩短上下文窗口、降低所有响应上限或提前频繁压缩。现有代码已有 offload、结果投影和上下文压缩；应先确定它们在哪些实际调用中失效或造成反复回读，再改变阈值。

也不因为输入很长就强制拆更多 worker；交接会产生暖启动和页面证据重建成本。先比较同一 worker 的证据组织改善与拆分方案，再作选择。

### 6.5 讨论顺序及对照设计

建议先保持模型配置不变，分析代表性搜索 worker 与详情 worker 的实际请求，确定“上游缺陷恢复、重复材料、长脚本输出”各占多少，再选择第一项改动。

上下文投影对照使用同一份固定证据，检查输出正确性及证据覆盖；这类离线对照不能替代网页交互验证。执行效率对照需要相同的页面条件和任务完成标准，另外记录网站波动。

模型配置对照独立成组，避免同时改上下文、模型和工具实现而无法归因。至少报告完成率、目标字段/文件正确率、模型输出、非缓存输入、缓存读取、实际时间和恢复次数，不能只比较单轮速度。

上述上下文与模型方向仍待用户讨论，不视为已批准更换模型、调整推理强度或裁剪关键证据。

## 7. 跨层责任、下载正确性与 WebCross 依赖

### 7.1 排查顺序与责任边界

先核对原始请求、原始响应、调用时序和实际执行代码，再定位 Harness、WebCross、模型或部署版本。运行实例与工作区源码分别记录版本；协议 catalogRevision 不能替代安装包构建身份。

| 问题 | 当前责任划分 | 下一步与范围 |
|---|---|---|
| AX 中节点可见，操作无法解析 | WebCross 目标解析/浏览器执行链 | WebCross 侧处理；Harness 保留原始证据与错误，不写绕行逻辑 |
| 缺失父目录报越权 | WebCross 实际下载执行链，需区分旧部署与其他校验层 | 先核对运行构建和拒绝层，再由对应层修复 |
| 下载前强制页面同步 | Harness 页面生命周期处理 | 将下载账本与页面状态解耦 |
| 查询到的历史文件进入当前 artifacts | Harness 采集及归属处理 | 修正来源，再统一投影 |
| 部分长考与重复输出 | 模型行为、输入组织及上游错误可能共同影响 | 依据逐回合证据分摊，不预先只归因 Harness |

### 7.2 下载路径与完成通知

2026-09-14 在 `ws://127.0.0.1:61168/ws` 的 [live canary](download-path-live-canary.md) 结果：已有父目录和提前创建的新目录成功；缺失多层父目录失败，英文、中文空格路径均返回 `download-path-not-allowed`。

两次成功都收到 `Download.stateChanged: completed`，下载记录和文件 SHA-256 一致。源码存在 mkdir 不代表运行中的构建已经加载，须单独核对构建/进程与实际报错层。

Harness 已不再因 `Download.start / Download.control` 本身设置 `requires_state_resync`。下载状态继续进入下载账本；页面实际导航或弹窗仍由页面事件处理。事件补读和 downloadId 对账保持不变，并已用页面生命周期、下载事件账本和下载对账测试覆盖。

本轮续跑与定义复用不会修复父目录问题，也不允许模型默默改写用户交付路径。测试中的失败结果不得计作“下载优化已经上线”。

下载交付要记录用户指定位置、实际位置和差异。机械层校验真实文件、权限、状态及明确路径约束；是否接受替代归档方式由模型结合原任务判断，不能下载成功就自动宣称全部交付完成。

### 7.3 AX 问题不在 Harness 内绕过

此前已有 `component-unresolved` / `component-context-truncated` 的原始失败证据。此项按当前任务划分为 WebCross 侧依赖，不承诺通过本仓库 Harness 优化解决。确切原生组件或部署版本的根因仍由 WebCross 侧证据确定。

交付 WebCross 侧的最小复现应包括：运行构建身份、产生节点的观察请求与响应、随后失败的操作请求与响应、两者时间差、page/frame/document 身份及版本（平台实际提供的字段）、中间导航事件和失败码。

Harness 不新增站点选择器、重复刷新循环、提示词重试策略或 Runtime.evaluate 绕行掩盖问题。复用已有标准恢复能力不等于增加专门补丁；无法定位时保留失败事实，由模型判断其他已授权业务路径。

WebCross 修复后对同一观察和操作链路验收；在此之前，把相关恢复回合单独标记，不能当作 Harness 上下文优化已经能消除的成本。

## 8. 实施顺序与交付物

| 阶段 | 改动 | 主要文件/模块 | 完成标准 |
|---|---|---|---|
| 前置核验 | 运行包/源码版本及 WebCross 拒绝层定位 | 原始协议记录、运行构建信息、下载 canary | 明确运行链责任，不用源码状态代替 live 结果 |
| 正确性 | 下载状态解耦、历史文件来源与交付回执统一 | page_lifecycle、产物采集、worker_result | 下载终态不制造页面门禁，历史查询不自动成为本次交付 |
| A | 续跑决定协议与 page-session 引用复用 | browser tools schema、final_answer、worker_result、spawner_worker | 模型决定与事实分离，旧回执仍可返回 Lead |
| B | phase 续跑账本、预算预留与对账 | phase_lifecycle、state_store、lead_tools | 重复 wait 和进程恢复不刷新额度、不盲目重复派发 |
| C | 自动续跑纳入完成事件循环 | lead_tools、现有 spawner 入口 | 兄弟任务运行时也可按合同续跑，不新增嵌套长期等待 |
| D | Workflow 定义保存、引用与局部修改 | browser capability、workflow_policy/projection、Storage | 同一原定义无需模型全文重写，仍经过原执行检查 |
| E | 紧凑交接与性能遥测 | worker_result、offload、事件日志 | 模型收到的是增量回执，完整证据可读 |
| WebCross 待修 | 下载父目录、AX 目标解析 | WebCross 对应执行层及部署 | 下载 canary 全通过；AX 原始复现链通过，不以 Harness 绕过验收 |
| 待讨论 | Browser 证据检索、上下文组织、输出及模型对照 | 现有 offload/compaction、Browser 工具结果、模型请求遥测 | 单变量对照，完成质量不下降，收益可归因 |

优先级：先核对实际部署，推进下载正确性、文件来源和下载页面状态解耦；A–C 与 D 继续推进，B 是自动续跑可靠性的前置条件。AX 由 WebCross 侧处理，不阻止其余独立 Harness 改进。Browser 上下文与模型参数先讨论和做归因，不预先承诺调整配置。

## 9. 验证矩阵

| 场景 | 必须验证的行为 |
|---|---|
| 单 worker partial，原合同内请求继续 | 只派一次，同 phase，不产生额外 Lead 决策回合 |
| partial 与运行中兄弟任务并存 | 通过现有资源入口调度；不中断兄弟任务 |
| 同批多个续跑请求 | 不丢回执、不重复派发；资源冲突可见 |
| done 与 partial 混合 | 成功事实保留；续跑不被误算为已验证完成 |
| 缺 continuation / 旧格式 | 返回 Lead，不强迫模型补答导致循环 |
| 进程在 final_answer 前中断 | 不编造模型决定，保留已知证据及未知范围 |
| 重复回执、重复 wait | 预算及 spawn 次数不增加 |
| reserved 后崩溃 / spawn 后提交前崩溃 | 恢复先对账，不能假定未执行 |
| 多次 wait、计划修订、进程重启 | 预算按既定规则继承，不隐式刷新 |
| deadline、身份变化、合同变更 | 原权限与状态约束仍有效，失败原因可恢复 |
| page-session 写入失败或被裁剪 | 不影响派发账本正确性 |
| 只读 URL 更新 | 不新增业务动作/worker 历史，敏感值不泄露 |
| 无历史文件 / 历史下载混在列表 | 续跑引用不把历史下载提升为本次产物 |
| Workflow 引用执行 | 实际定义与指定摘要一致，不重复生成长 JSON |
| 局部修改引用过期、路径非法 | 返回结构化冲突，原定义保持不变 |
| Workflow 已执行部分副作用后失败 | 不因 failed 自动整份重跑 |
| Workflow 原实例仍活跃 | 不因客户端断线再新开实例 |
| 过期页面、元素目标、敏感变量缺失 | 暴露事实并按现有机制重新绑定/观察，不猜参数 |
| db 模式有同名旧物理文件 | DB 管理资源按 DB 读取 |
| file / dual 模式 | 保持原 primary 语义，读写失败可审计 |
| 下载完成/失败事件到达、丢失后补读 | 按 downloadId 更新事实，不无条件要求 Page.getState |
| 下载同时发生真实导航/弹窗 | 页面观察义务仍由真实页面事件产生 |
| 已有/缺失/预创建父目录 | 同一 live canary 区分允许路径与创建问题，文件和终态一致 |
| 用户指定目录与实际目录不同 | 偏差完整返回，不能静默宣称完成 |
| 同一证据重复读 / 同 URL 不同文档版本 | 前者可引用复用，后者不能用旧内容冒充新观察 |
| 投影省略字段或截断 | 缺失范围与回读路径可见，不把省略当成不存在 |
| WebCross AX 操作失败 | 原错误与版本保留，不触发新增站点特判或执行绕行 |

调度测试至少包含真实 asyncio Future 完成与 `_handles` 注册表路径，不能全部依赖恒定返回的 fake_wait。外部业务网站不进入普通单测；本机固定 fixture 的 live 测试用于运行时验证。崩溃恢复测试使用故障注入覆盖预留、发出请求和提交三处窗口。

## 10. 性能指标与上线判据

新增或补齐遥测：

- 已满足原合同的自动交接次数、Lead 被唤醒原因、交接延迟。
- 每个 phase 的累计自动预算、预留、派发和回执去重结果。
- 每次续跑的 page-session 引用、产物引用、Workflow 定义引用；不记录明文秘密。
- 原定义字节数、修改字节数、引用命中率、完整重写次数。
- 模型实际输入/缓存/输出 token、模型时间、工具时间、人工等待、并行区间。
- 成功交付率、重复副作用、恢复失败和结果未知的次数。
- Browser 各类上下文来源、同一证据重复读取/注入、上游缺陷恢复回合及投影后的实际请求大小。
- 当前运行构建身份与工作区源码身份；无法取得的字段明确记为 unavailable，不用协议版本推断二进制版本。

上线判据首先是行为正确：重复回执不重复执行、partial 不冒充 done、定义复用不越权、未知副作用不被抹掉。然后验证自动交接无需额外 Lead 回合、引用复用无需全文输出。

性能比较使用相同任务范围、完成标准、模型配置及运行构建，记录多次结果及波动。不能把少完成工作、减少人工确认或改变模型配置的收益归到调度实现上；不以单次采样宣称根治。

## 11. 兼容与回滚

新协议采用版本字段和可选字段；旧 worker 不输出 continuation 时仍能正常结束并返回 Lead。新定义引用入口与原完整定义入口并存。

自动续跑和定义引用执行可分别关闭。关闭自动续跑后保留账本及证据，不重新消费历史完成事件。回滚不删除已下载文件、不重置 phase 预算、不改已批准任务范围。

新控制状态的读取失败必须显式报告，不套用 page-session 的“写入失败只损失优化”策略。已有运行在升级后缺少派发关联时，先对账再自动推进。

## 12. 对用户问题的直接回答

最新任务复核及两项已批准的压缩修改见 §13；其中发现的文件验收缺口优先级高于进一步调模型参数。

可以复用 page-session，而且应当复用，避免新 worker 再次从头整理页面事实。最合适的复用方式是：**page-session 提供已有观察，phase 状态保存调度控制，Workflow 定义引用避免模型重写**。三者通过身份与证据引用关联，不互相冒充权威。

本报告统一包含此前提出的 Lead 与 Browser 优化点。page-session、产物归属、续跑、Workflow 和上下文共享同一证据体系；AX 保留为 WebCross 侧待修依赖。首批 Harness 运行逻辑已经实现；上下文与模型配置仍待单独对照，WebCross 依赖也不能用本批 Harness 测试代替验收。

## 13. 任务 4e4c66973eca41a69827a18083da75b7 的续跑复核

### 13.1 范围与已落实修改

证据来自本地 `worktree/4e4c66973eca41a69827a18083da75b7/run.jsonl`、当前任务计划及工件、Harness 与 WebCross 源码，以及安装包中的 Client 主进程脚本。本节事件编号均指 `resume-20260914T122339006476Z-cd65ad48` 内的 `sequenceNo`，不能与原 run 同号事件混用。worker 编号也会在 `/resume` 后重新计数。

用户已批准并落实的压缩修改：

1. Browser 例行压缩在剩余轮数 ≤5 时跳过，包括缓存压力触发的例行强制压缩。轮数使用 `effective_max_steps - step + 1`，计入当前尚未调用模型的轮次，尊重延期后的上限。写入 `agent.compaction_skipped` 事件。估算已经达到上下文窗口时仍保留压缩保护；这是窗口算术检查，不改变业务继续/结束决定，也不新增请求重试。
2. `estimate_prompt_tokens` 不再把图像块中的 base64 或图像 URL 当作普通正文计算。每张图暂留 4,096 token 的预算估值；它不是任何模型的精确图像计费公式。普通文本、工具参数及工具 schema 保持文本估算，真实发给模型的图片不被修改。图片保护对和现有请求异常处理保持原有行为。

验证：使用项目可用的 Miniconda Python 运行 `test_multimodal_screenshots.py`、`test_compaction.py`、`test_context_compaction.py`，38 passed；覆盖 6/5/1 轮边界、延期上限、缓存压力、超过上下文窗口、图像字节大小独立性、普通工具参数不误删、图片保护对。系统 `/usr/bin/python3` 缺少 pydantic，不能用它的收集失败判断代码测试失败。

随后落实了通用交付与状态协议：Browser 新增受限的批量文件工具，可创建目录、写 UTF-8 文本/JSON、stat/hash 和复制归档；没有 shell、Python、JavaScript、删除或移动入口。复制保留源文件，覆盖必须显式声明。写入仅允许当前任务的 `observations/`、`deliverables/`、`scratchpad/` 以及 Desktop 交付目录，并拒绝控制文件、源码、路径穿越和符号链接目标。

每批文件操作生成 `browser-file-manifest-v1`，登记 worker/phase、逐项状态、路径、字节数、SHA-256 和复制来源。file 模式写物理 manifest；db 模式写数据库逻辑资源并可由 `local_fs_read` 读回；dual 继续遵守 FileStore primary。成功输出进入当前 attempt 的 artifacts，Browser prompt 要求在 `record_extraction` 中声明同一交付路径。

文件验证现在只让记录行显式声明且归属于当前 phase 的文件满足合同，并允许同 phase 后续 attempt 对已登记文件重新核验。Workflow 内成功的 Download 子步骤按真实 action、stepPath、workflowId 写入与直接下载相同的文件证据和 downloadId 账本；失败子步骤不认领产物。Lead handoff 投影直接给出工件失败、当前/历史交付文件数量和 manifest 引用。摘要模型已经返回 usage 时，先计账再判断 `max_tokens`、工具调用或摘要格式失败。

Lead 计划提示同步要求：仅依赖同一 producer 的兄弟 phase 若应并发，必须显式声明相同依赖和 dispatch wave；用户指定的交付根目录及布局必须进入 worker task 和文件合同，fallback 落点不能在未获授权时冒充完成。

本轮没有更换模型或修改 WebCross，也没有搬动历史任务文件；历史任务不会因当前源码更新而重新运行。

### 13.2 实际执行和成本

原 run 的 `search_collect` 已由原 browser-001 产出三行并通过验证；后面的 worker 在做商品详情。不能把重复的详情尝试算成“三个搜索 worker 都未取得三个链接”。

续跑的四个 worker 如下，耗时从 spawn 到 result，包含各自模型、工具、压缩及检索时间。并行 worker 的耗时不能直接相加作为任务墙钟：

| 续跑 worker | phase | 耗时 | Browser 模型调用 | 输出 token | 结果 |
|---|---|---:|---:|---:|---|
| browser-001 | detail_rank4 | 11分51秒 | 20 | 133,362 | partial，目录交付未满足 |
| browser-002 | detail_rank4 收尾 | 6分29秒 | 17 | 82,819 | done，但文件验收使用了空白页截图 |
| browser-003 | detail_rank5 | 20分12秒 | 53 | 166,376 | done，实际文件在 observations |
| browser-004 | detail_rank6 | 17分27秒 | 53 | 175,341 | done，实际文件在 observations |

续跑事件墙钟约 56分39秒，未作为“剔除 HITL/等待的模型耗时”呈现。四个 Browser 合计 143 次、557,898 输出 token；Lead 23 次、53,506 输出 token。Browser-001 返回后到收尾 worker 派发另有 7分20秒，包含 Lead 查日志、误读状态及两次被 scheduling wave 拦下的派发。

续跑触发了六次摘要生成，按 compactionId 配对 start/end 后累计等待约 **8分27秒**，并行区间取并集为 **7分29秒**。先前写成10分07秒是加总错误，现已更正。其中四次在当时预算只剩 ≤5 轮：browser-003 的 47、52 轮，browser-004 的 49、52 轮；这四次累计约 5分27秒，不代表端到端可直接缩短同样时间。六次均由 `multimodal_pending_attachment` 强制触发；被压缩的历史文本本身均低于 425,000 阈值。

另有观测缺陷：六次里两次摘要返回 `max_tokens` 后走机械 fallback（事件 4287、4563），但 `_generate_summary` 在记录 usage 前抛异常，账面仅记四次摘要调用、63,436 输出 token。因此日志中的摘要 token 总量是下界，不能宣称已经统计完整。应先记已返回的 usage，再判断摘要是否可用；不得臆测缺失 token 数。

### 13.3 为什么文件落到了 worktree

工件逐路径检查结果：第4名 18 个、第5名 36 个、第6名 26 个，共 80 个清单文件实际存在且非空，全部位于该任务的 `observations/`。三份清单分别为 `product_assets-0f2dbc19.json`、`product_assets-0cbbc942.json`、`product_assets-d5150f2c.json`。这里核验的是存在性、大小和位置，不等于独立确认图片内容、视频可播放或所有商品素材已收齐。

用户目标明确要求“桌面按商品×类别分文件夹”。这 80 个是商品交付文件，不能用“worktree 本来保存日志”解释其位置，也不能因为项目目录恰好位于 Desktop 之下就宣称符合归档要求。

原始调用给出了比模型总结更准确的边界：

| 事件 | Download.start 目标父目录 | 结果 |
|---|---|---|
| 217 / 254 | Desktop 下商品分类的新目录 | `download-path-not-allowed`；直接调用返回 -32005 |
| 312 | worktree 内新建的多层 deliverables 目录 | 同样失败 |
| 312 | 已有项目根目录，位于任务 worktree 外 | 成功 |
| 354 | 已有 worktree/observations | 成功 |
| 354 | worktree 内新的一级目录、项目根目录下的新目录 | 同样失败 |

**这些事实不支持“只允许写入 task worktree”的说法。** WebCross Client 的 `DownloadPathPolicy.resolve` 先检查目标父目录；`PathSecurity.isPathSafe` 调用 `realpathSync.native`，目录不存在时返回 false，被统一翻译为“超出允许工作区”。前置检查因此会在缺失父目录时提前拒绝请求。默认允许路径还包括 Home、Desktop。

安装版本也已核对：`/Applications/WebCross.app/.../WebCross Client.app/Contents/Resources/app.asar` 内 `dist/main/index.js` 保留相同的父目录检查、realpath 失败分支及 Desktop 默认路径。Client package version 为 `0.9.0-beta`，该脚本 SHA-256 为 `4f945271f11890d78f828b4cbda02f279882c26fde65e18e059e5fcd8cfe55f2`。这是当前安装文件的身份，未用版本号冒充运行期间的内存构建快照；源码、安装脚本和任务回执在本处一致。

责任划分：

- WebCross：缺失父目录和越权路径被混成同一种拒绝；应在正确验证可创建目标、现有祖先和符号链接边界后准备父目录，并保留 Native 写入前复核。不能单纯提前 mkdir 绕过权限检查。
- 模型：将公开错误文案推导成未经证明的全局沙箱限制；Lead 进一步接受 worktree 交付，并把这个结论传给第5、6名 worker。
- Harness：没有把“文件完整性通过”和“用户要求的归档位置已满足”作为不同事实表达给决策层。当前计划保留桌面目标，但文件合同只有 `file_integrity(min_files=3)`，没有目标目录/布局约束。

因此不在 Harness 中自动改路径、偷偷复制文件或用浏览器脚本绕过拒绝。本轮没有新增 live 下载；运行本身已有直接调用和 Workflow 对照证据。

### 13.4 Harness 的文件验收缺口（P1）

这是本次最应优先修复的 Harness 问题：

1. `_capture_file_action` 只处理直接文件方法，未将 Workflow 内的成功下载动作纳入相同文件证据入口。`Download.list` 为避免污染 artifacts，仅刷新已归属于当前 worker 的文件；该限制不应回退为“所有历史下载都算本次产物”。
2. 新 worker 的 `file_action_evidence` 是空列表；历史工件合并路径只选 extraction JSON，没有将同 phase 的实际交付文件引用承接到文件验收中。
3. `_run_file_validator` 从当前 artifacts 获取所有非 extraction 文件并计数，`file_integrity` 没有绑定清单引用。因此它一方面漏掉先前下载的真实商品文件，另一方面接受无关截图。

实际复现已发生在续跑 browser-002：第3轮重存商品 JSON，得到 `fileArtifacts=[]`、`fileEvidenceCount=0`；第4–8轮检索日志研究验证器；第9–14轮新建空白页、处理页面门禁并截图；第15轮重存相同商品数据后通过。事件1274 的 `artifactValidation.fileArtifacts` 恰为三张 `/tmp/screenshot_...png`，不是清单里的18个商品文件。第5、6名最终回执也仅以截图作为 fileArtifacts。

建议统一修复，避免为这一个站点或字段打补丁：

- 将直接下载、Workflow 子步骤、完成事件归并到已有下载账本；用真实 downloadId、请求/结果、task、phase、attempt 关系建立来源。
- 允许同 phase 的后续尝试引用已登记的交付文件，重新检查存在性、大小及已有摘要；合同变化、文件变化、来源缺失作为结构化失败事实返回。
- 由计划/模型显式声明交付清单引用及目标位置，文件验证仅验证被引用的交付集合。截图也可以是合法交付物，不能一概禁止；但诊断截图不能自动替代另一份清单的商品文件。
- 目标位置与分类布局由原始用户目标、明确合同及语义审查决定。机械层负责引用完整性、路径事实、文件一致性和身份，不能凭文件名猜测商品类别或认为某一种目录结构“正确”。

普适性：适用于所有文件交付和跨 attempt 续跑。误杀面：旧合同缺少显式引用、外部文件变化或旧 run 无来源记录。恢复路径：返回可核验的缺失关联和现存文件清单，由模型补充引用/决定复核，不能静默认领整个历史 Download.list，也不能要求重新下载才能制造来源。机械检查的理由是文件身份、字节状态和引用关系客观可判定；“这些文件是否满足业务目标”仍交语义层。

### 13.5 新增优化实施结果

| 优先级 | 工作 | 状态与验收 |
|---|---|---|
| P1 | 统一下载来源、Workflow 子步骤与跨尝试交付清单 | 已实现；Workflow 成功子步骤进入统一账本，声明文件与 attempt/phase 归属绑定，同 phase 历史文件可重新验证，无关截图不能抵扣已声明交付路径 |
| P1 | 交付目标与实际落点的结构化差异回执 | 已实现协议基础；计划保留目标根目录/布局，worker 回执暴露文件失败、实际数量和 manifest。业务布局是否满足仍由语义层判断 |
| P2 | wait 回执的状态投影与按需查询 | 已实现；worker 状态、artifactSchemaStatus、工件失败、当前/历史文件计数和 manifest 分开投影 |
| P2 | 摘要失败 usage 计账 | 已实现；有 provider usage 的不可用摘要先计账，真正请求异常不编造 usage |
| P1 | Browser 受限批量文件能力 | 已实现；mkdir、文本/JSON 写入、stat/hash、复制归档与统一 manifest，无代码执行、删除或移动能力 |
| 外部依赖 | WebCross 下载父目录与错误分类 | Harness 未改；仍需 WebCross 部署后运行路径 canary |

当前自动派发闸门在本例拦下下游是有事实依据的：前驱 worker 为 partial、要求 Lead review。不能为省时间把 artifactSchemaStatus=done 直接改成 phase done。文件事实和业务完成判断继续分离。

## 14. 续跑并发、时间分解与工具能力选择

本节先记录只读复核及方案选择；后续已按 14.4 增加受限文件工具，但没有调整模型配置或增加代码执行器。WebCross 新问题已补入 `docs/abcp-workflow-platform-requests.md` 问题9。

### 14.1 四个 worker 是两次串行加一组并行

下表时间均为2026-09-14北京时间，从 spawn 到 result：

| worker | 阶段 | 开始 | 结束 | 调度关系 |
|---|---|---|---|---|
| browser-001 | detail_rank4 | 20:29:54 | 20:41:45 | 独立运行 |
| browser-002 | detail_rank4 收尾 | 20:49:05 | 20:55:34 | 在001结束之后 |
| browser-003 | detail_rank5 | 20:56:40 | 21:16:52 | 与004并行 |
| browser-004 | detail_rank6 | 20:56:41 | 21:14:07 | 与003并行 |

最大并发2，003与004相隔约0.66秒派发，重叠约17分27秒。后者早结束约2分45秒。前两个 worker 都属于第4名详情，并非搜索阶段。当前计划的 dispatch_wave 将第5、6名放在第4名之后；调度器是在遵守批准的先后次序，不能在运行时擅自移除。若下次规划确认只有 search_collect 是真实输入依赖、后续商品互不依赖，可由模型直接规划同一 wave，并按资源容量并发；此项收益需要对照验证。

### 14.2 时间口径与 Lead / Browser 对比

模型时间使用同 run、messageId 的 `lifecycle.message.start/end` 配对，包含该调用的服务端等待、生成及内部重试，不能称作纯推理计算时间。工具时间用 `lifecycle.tool.end.durationMs`；Lead 单列 wait_browser_agents，避免把 worker 运行时间记成 Lead 浪费。摘要按 compactionId 配对。下表为累计服务时间，有并行重叠，不直接相加成为任务墙钟。

| 角色 | 模型调用等待/生成 | 摘要生成 | 工具执行（不含Lead wait） | 输出 token（模型调用，不含摘要） |
|---|---:|---:|---:|---:|
| Lead | 17分44秒 | 0 | 35秒 | 53,506 |
| browser-001 | 10分26秒 | 51秒 | 31秒 | 133,362 |
| browser-002 | 6分19秒 | 0 | 7秒 | 82,819 |
| browser-003 | 12分27秒 | 6分02秒 | 1分37秒 | 166,376 |
| browser-004 | 14分03秒 | 1分35秒 | 1分41秒 | 175,341 |
| Browser合计 | 43分16秒 | 8分27秒 | 3分56秒 | 557,898 |

Lead 的三次 wait_browser_agents 累计38分08秒，是等待 worker 的时间，不再算入上表的主动耗时。35秒工具时间中约31秒来自 final_answer 的校验路径。Browser模型区间去重为33分02秒，摘要区间去重为7分29秒；两者之间也可能重叠，仍不可直接相加。Browser工具时间可能包含平台内部等待，不是浏览器CPU时间。

完整任务若按“原 run + resume”汇总，Lead模型时间为32分01秒（42次调用），Browser模型累计为72分33秒（307次）；不把暂停到/resume的间隔加进去。原run的repair_task_plan工具含审批/审计等待，本节不把它算作Lead模型推理时间。

续跑关键路径为：恢复到首次spawn约6分14秒 → rank4约11分51秒 → Lead处理间隔7分20秒 → rank4收尾6分29秒 → 下次派发间隔1分06秒 → rank5约20分12秒（rank6并行并早结束）→ 收尾约3分27秒。合计约56分39秒。模型时间已经包含在这些区段，不能再相加。

### 14.3 是否提高压缩阈值

当前 Harness 配置为窗口500,000、比例0.85，即425,000阈值；该配置不是从模型能力自动推导。六次压缩都是图像估算导致的强制触发，因此应先评估已修复的图像估算和≤5轮跳过规则，不能把六次都归因于正常上下文满了。

该续跑 `llm.usage` 报告的峰值输入为 Lead 137,087、Browser 380,300（缓存读+未缓存输入），它们是修复前运行的已发送请求，不足以预测完全不压缩会长到多大。worker 使用 `deepseek-flash`，Lead 使用 `glm-5.3`。GLM-5.3的官方1M规格不能自动证明别名为deepseek-flash的实际通道也支持1M：[GLM-5.3官方文档](https://docs.z.ai/guides/llm/glm-5.3)。

建议：先保留425k取得修复后基线；确认具体worker通道的实际窗口后，将硬容量对齐，并另行测试约600k–700k的压缩目标，留出输出、工具结果突增及估算误差余量。不要直接把压缩比例顶到95%或取消压缩。角色容量应区分，不能全局套用其中一个模型的规格。比较任务正确率、真实输入峰值、模型延迟、摘要调用次数/时间、重建前缀后的未缓存输入，不能只看摘要次数。

### 14.4 是否参考 Pi 引入 coding 工具

Browser目前提供local_fs_read/search，但没有本地mkdir、write、copy或通用shell。对于商品文本落盘和分类归档，工具面确有缺口；它与WebCross父目录误报是两件事。

Pi默认提供read/write/edit/bash；它的write实现会递归创建父目录，并允许替换底层文件操作接口。可以借鉴这种可替换工具后端，而不是必须在Python Harness内嵌整个TypeScript agent：[Pi README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)、[write实现](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/write.ts)。

第一阶段已经添加通用、可批量的 `local_fs_batch`：准备目录、写文本/JSON、stat/hash、复制归档，并返回统一文件清单。它能直接完成商品信息 txt、分类目录和核验，不需要把本地文本编码成 data URL 再经 Download.start 写入。复制保留原件；覆盖必须显式声明，权限以用户授权目标为准。

工具实现统一登记task/phase/attempt及路径、大小、摘要、来源/派生关系，既支持文件复用也避免“生成文件却不被验收认领”。机械检查限定路径权限、对象身份、覆盖行为、资源预算和证据完整性；目录/字段含义仍由模型决定。不得允许写入权威任务账本来伪造完成状态。

当前不加入 Python、JavaScript 或 shell。若以后确有复杂 JSON 转换、批量解析或文件内容处理需求，再单独评估受隔离执行器；仅设置 cwd 或依靠 prompt 不能构成权限隔离。

### 14.5 不换模型的优化顺序

1. 修复交付引用和跨attempt验收，消除研究验证器、截图凑数及重复收尾。这次6分29秒的收尾worker和7分20秒Lead间隔提供了量级依据，但不能预先把全部时间承诺为可节省。
2. 状态回执直接给出phase状态、工件检查状态、未满足交付约束、来源和候选下一步，并提供结构化查询。Lead本次有17次local_fs_read、7次local_fs_search；Browser合计132次这两类调用。文件读写本身很快，反复模型往返才贵，不能通过禁止必要检索解决。
3. 文件操作按清单批量执行，统一验收并复用结果；减少重复输出长清单、路径和data URL。保留按需读取完整证据的入口。
4. 先观察本轮压缩修复，再调整容量/目标和摘要预算，补齐失败usage计账。
5. 不换模型也可做推理强度对照。Lead当前max并不意味着状态读取和收尾都需要max；可测试high/low的调用阶段策略，规划与复杂裁决单独评估。GLM-5.3官方支持low/high/max但不支持关闭thinking；Browser通道是否支持相同参数需独立验证。不要只压低max_tokens，把长考变成截断失败。

工具执行累计只占约3分56秒，不代表WebCross缺陷不重要：错误回执和定位失败会引发模型反复诊断，其成本计在模型时间里。平台问题9已经补充，未把Harness验收漏洞混入平台需求，也未把历史AX问题重新报成新问题。

## 15. 任务 9876a8ff89e340969b2fd68da51558b3 的复测与第二轮修复

本节使用 run `run-20260914T152756159656Z-348fe323` 的 4,643 条事件复核。任务墙钟为 3,491.05 秒（58分11秒）；operator approval 为 31.47 秒，无其他 HITL，剔除该等待后的活动墙钟约 57分40秒。首次 worker 在任务开始 1,334.39 秒后派发，因此本轮最大的剩余关键路径仍在 Lead 规划，而不是 Browser 工具执行。

### 15.1 与上一轮的可比改善

| 指标 | 4e4c 原任务+resume | 9876 本轮 | 变化 |
|---|---:|---:|---:|
| 剔除人工等待后的活动墙钟 | 110分49秒 | 57分40秒 | -48.0% |
| 模型调用数 | 360 | 184 | -48.9% |
| 输出 token | 1,160,313 | 555,294 | -52.1% |
| 未缓存输入 token | 1,947,218 | 998,220 | -48.7% |
| 缓存读取 token | 45,601,344 | 25,132,800 | -44.9% |
| Browser compaction | 6 次 | 0 次 | 已消除本样本的摘要等待 |

这组数据证明本轮整体有明显改善，但不能把全部差异归因于 Harness：两轮虽使用相同模型名，模型输出长度、页面状态和失败路径都不同。可归因的结果是：近步数上限不再压缩，本轮触发 5 次 `near_step_cap` skip 且没有摘要调用；64 个声明交付文件全部位于 Desktop、存在且非空，SHA-256 与 manifest 一致，总大小约 13.6 MB。

四个 worker 的单次表现如下：

| worker | phase | 墙钟 | 模型调用 | 输出 token | 模型调用累计时间 |
|---|---|---:|---:|---:|---:|
| browser-001 | 搜索第4–6名 | 8分56秒 | 47 | 94,171 | 7分31秒 |
| browser-002 | 第4名详情 | 12分13秒 | 35 | 136,493 | 10分18秒 |
| browser-003 | 第5名详情 | 10分24秒 | 30 | 121,869 | 8分58秒 |
| browser-004 | 第6名详情 | 9分10秒 | 48 | 99,333 | 8分06秒 |

browser-003 与 browser-004 相隔约 0.41 秒派发，属于并发执行。worker-003 的 30 步比 worker-002 的 35 步略少，worker-004 为 48 步，说明路线经验只减少共同的入口探索，不能消除商品页面各自的懒加载、AX/selector 失败、缺失视频的证据闭合和下载数量差异。

### 15.2 worker-002 的经验是否传给了 003/004

结论是“传了，但自动通道丢掉了最有价值的部分”。事件 `spawn.sibling_route_attached`（sequence 2207、2222）对 003、004 各出现一次；page/session 路由事实进入了两者上下文。然而当 worker-002 的 handoff 超过 3.5KB 时，降级投影将 `suggestedNextExperiment` 清空，只留下 status、文件计数和少量路径。worker-002 最终给出的三条关键经验——使用 canonical PC URL、从 `v-detail-r` shadow root 读取详情、以 Desktop 绝对路径下载——没有通过自动 sibling handoff 保留。

003/004 最终仍收到了这三条经验，是因为 Lead 读取完整 worker 回执后，将其重新写入两个 spawn task。这会产生一次 Lead 上下文膨胀和重新表达成本，也解释了为什么用户观察到自动复用并未明显减少调用步数。

本轮已修改通用 handoff 投影：

- 即使进入最终 `handoff_size_budget` 降级，也保留一条最多 500 字符的 worker 建议，供 sibling 作为路线提示；该建议仍标记为 worker claim，不提升为机械事实。
- 优先保留 validator 确认的 extraction artifact 路径与 file manifest 路径，避免“前两个 artifacts 恰好是普通下载文件”挤掉权威索引。
- `totalExtractedRows` 改用 validator 合并去重后的 `rowCount`，不再把当前 artifact 与 prior attempt artifact 相加。本次搜索实际为3行，旧投影曾显示6行。

用本轮 browser-002 的原始 151,890-byte wait payload 回放新投影后，model-facing handoff 为 2,096 bytes；上述三段路线建议完整保留，同时保留 validated extraction 与两个 file manifest 路径，未超过 3,500-byte 预算。

机械层不据此决定页面业务路径；后续 worker仍须核对自己的页面和目标。该修改只保证模型产出的可复用候选路线不会因大小降级而无声消失。

### 15.3 调度快照与 dispatch wave 不一致

批准计划为 wave 1 搜索、wave 2 第4名、wave 3 第5/6名。旧 `schedule_snapshot` 只看 `depends_on`，搜索完成时把三个详情 phase 全部报告为 ready；实际 spawn 闸门又按 wave 拒绝第5/6名。自动后继选择因此得到 `no_unique_ready_successor`，搜索完成到第4名派发之间产生约 68.7 秒 Lead 往返。

现已将 `dispatch_wave_blockers` 提取为调度快照和 spawn 共用的客观门禁。搜索完成后快照只报告 wave 2 的第4名 ready，Harness 可经原 spawn 闸门直接派发；第4名完成后 wave 3 的两个 phase 同时 ready，仍返回 Lead/既有并发逻辑处理，不替模型选择独立业务分支。wait 回执同时携带最新 `scheduleSnapshot`，大型 worker 结果被 offload 时也保留该字段，减少额外 list/read。

### 15.4 下载账本污染与文件合同漏检

本轮最终完成回执统计了 73 条 download receipt，但按来源拆分后只有 61 个当前交付媒体文件，另有一个 rank5 诊断 HTML，以及 11 条来自旧任务 `4e4c.../observations` 的记录；rank6 回执还包含 rank5 的下载。根因是 Workflow 中 `Download.list/control` 的成功结果会遍历 fleet 级下载列表，并把每条记录都登记为当前 worker 的副作用。

现已按动作来源收窄：Workflow `Download.start` 可创建新归属；`Download.list/control` 只能刷新当前 worker artifacts 或下载账本中已经存在相同 operation identity 的记录。该规则只判断可验证的操作身份，不用路径名称猜业务归属，也不会阻止直接 `Download.start` 登记新下载。

文件验证另有两个独立缺口：路径字段只识别少数精确键，遗漏 `info_file_path`；且 `min_files` 达标后会忽略同一行声明的其他缺失文件。现在默认识别规范化后以 `file_path`/`saved_path` 结尾的字段，并允许合同用 `path_fields` 声明自定义键。只要 row 显式声明了属于本 validator 范围的目标路径，每个路径都必须有 phase 归属并通过存在性、大小和摘要检查；按 extension/pattern 划分的其他交付类型不会被误杀。

### 15.5 Desktop 文件工具可用性

本轮 rank4 首次调用 `local_fs_batch` 使用 `Desktop/...` 相对路径，被旧实现解释为 task-relative 并失败；对 Desktop 目录本身执行 stat 也因“必须是普通文件”失败。模型随后改用绝对路径才完成交付。

现在每个操作可声明 `base="desktop"`，同时支持 `Desktop/...` alias；stat 可读取允许范围内的目录并返回 kind、size、mtime。默认仍为 task-relative，写入范围、禁止控制文件/隐藏敏感路径、显式覆盖和无代码执行边界不变。

### 15.6 仍未解决的主要成本

Lead 共 20 次模型调用、88,552 输出 token，模型调用累计约 21分44秒。前两次调用分别耗时约 7分30秒/26,608 输出 token 与 6分53秒/30,000 输出 token：第一次只产生两个 guide 读取动作，第二次生成的单体计划在 max_tokens 处截断。随后又经历 tool args reject 和三轮 mechanical repair。最终首个 worker 在 22分14秒后才启动。

规划 prompt 已补充两条结构化建议：四个及以上、且 phase task/contract 较详细时直接使用 draft API；初始规划协议已经足够，不要在没有具体 validation/review receipt 指向某份 guide 时先花一整轮读取 guide。这能减少相同失败形态，但仍属模型遵循性优化，不能宣称已根治无产出长考。下一步应对 Lead 规划模型/推理强度做固定输入对照，并分别记录首个可执行计划时间、截断率、机械错误数与最终合同质量。

Browser 的剩余成本主要是页面特有失败而非压缩：四个 worker 合计 160 次模型调用、451,866 输出 token，累计模型调用时间约 34分52秒。worker-004 的 48 步集中在 AX/selector 不可定位、详情区懒挂载、重复 attribute 读取以及对“无视频”的证据闭合。AX 可见但无法操作仍属于 WebCross 已知问题，本报告不在 Harness 中增加站点选择器或 Runtime.evaluate 绕行。可继续优化的是将成功 sibling 的路线建议可靠交接、减少 Lead 重述，以及在 WebCross 提供稳定目标解析后再对重复观察做对照。

### 15.7 本轮代码验证

新增回归覆盖：调度快照与 spawn 的 wave 一致性；Workflow list/control 不收编陌生下载且能刷新自有记录；`info_file_path`、缺失的第二个声明文件、validator 范围过滤和自定义 path fields；Desktop base/alias 与目录 stat；超大 handoff 保留路线建议、权威 extraction 与 manifest；validator 行数不被 attempt 重复累加；wait 结果携带最终调度快照。相关 256 项通过；完整测试为 4,065 passed、3 skipped、5 failed。5 项失败均为既有环境差异：Python 环境缺少 `openai`/`anthropic` 包，以及 Python 3.9 `pathlib.glob` 对非法反向字符范围抛错；失败用例未触及本轮代码。`compileall` 与 `git diff --check` 通过。

### 15.8 同类独立详情改为同 wave 并发

后续决策调整了 15.3 所描述的默认编排：先完成一个详情样本再启动其余详情，不再是同类详情任务的默认策略。本轮 rank4 用时 12分13秒，随后 rank5/rank6 并发段最长 10分24秒，详情关键路径为 22分37秒。若三个详情在搜索工件验证后同时启动，关键路径由三者最大值决定，即约 12分13秒；在页面状态近似的假设下可减少约 10分24秒墙钟。该估算不代表 token 同比例下降，因为三个 worker 的模型与工具工作仍然存在。

新策略不以“task_type 相同”作为唯一依据。规划模型只有在输入已绑定、页面/账户状态独立、没有真实数据依赖时，才把 homogeneous sibling 放入同一个最早可用 `dispatch_wave`。一页 checkpoint 改为有证据的例外：共同未知入口、认证边界、共享可变状态或重复失败代价预计超过串行等待时才使用，并且样本页必须完成一个真实交付实体。

Harness 对批准计划执行以下客观规则：

- 一个完成事件使同 wave 多个 phase ready 时，按计划顺序逐个通过原 spawn 闸门，直到填满 `max_browser_agents`。
- `max_browser_agents` 是唯一的运行 worker 并发上限。`max_browser_agent_instances` 只声明希望长期保留的复用 slot 数；有效 slot 池至少提升到 `max_browser_agents`，因此较小的 instance 配置不会暗中压低并发。
- 容量不足时，剩余 ready phase 保持 pending；任一同 wave worker 完成后，无期限 event wait 继续补位，不为机械补位唤醒 Lead。
- 明确 deadline 仍禁止后台派发；spawn 权限、Fleet/session、合同或基础设施拒绝会停止本批并返回 Lead，不绕过原闸门。
- 没有共同显式 dispatch wave 的多个 ready phase 仍由 Lead 判断，Harness 不从任务类型或站点猜测它们可以并发。

新增回归覆盖同 wave 三 phase 批量派发、`max_browser_agents` 容量截断、spawn 中途拒绝停止、容量满时 event wait 保持停驻等待补位，以及较小 `max_browser_agent_instances` 不压低运行容量。当前本机配置的 `max_browser_agents` 已对齐为4。相关调度/slot/规划测试261项通过；完整测试为4,069 passed、3 skipped、5 failed。5项仍是既有环境差异：缺少 `openai`/`anthropic` 包，以及 Python 3.9 `pathlib.glob` 对非法反向字符范围抛错。`compileall` 与 `git diff --check` 通过。
