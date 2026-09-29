# 基于 Tau 的 Skill Builder、Coding Agent 与共享执行能力实施计划

- 更新日期：2026-09-29；根据本轮九项评审修订。
- 状态：设计文档；运行时代码删除、数据库迁移与功能实现待本版方案确认后执行。
- 顺序：第一阶段 Skill Builder；第二阶段共享 Python/Shell 沙箱、grep 与公共网络；第三阶段 Coding / Browser 编排；第四阶段恢复与性能完善。
- 选型：固定 Tau Agent 核心并做必要补丁，应用会话复用当前 Harness；不整体继承 Tau CodingSession。
- 本版调整：面向更长的可复用 Workflow；DSL 编译由 WebCross 负责；明确根目录 skills 与任务 worktree 的 SQLite 索引；取消 CandidateStore、独立生成/验证报告、Skill/Workflow health 与自动晋升；用户选择发布、编辑、删除；Skill 继续显式使用。

## 1. 目标与第一阶段交付

Skill Builder 根据用户目标与历史执行资料，把已经理解的浏览器流程重构为可复用的 Hybrid Skill。目标是尽可能把可由平台完整表达的连续操作、条件、循环和数据处理放进较长 Workflow，减少运行时反复回到模型的次数。长短由实际依赖决定，不按历史 segment 长度、工具名称或固定步数切割。

第一阶段交付：

```text
/skill-create <任务目录或 task_id> [要求]
  → 读取原始目标、轨迹、文件和平台合同
  → 在当前构建任务 worktree 中编写 Skill
  → 发送给 WebCross：平台编译 → 实际执行
  → Agent 阅读真实结果，修改文件或恢复页面后继续
  → 展示文件、版本、字段及试运行事实
  → 用户选择：发布 / 编辑 / 删除
  → 返回入口，或在同一会话继续编辑
```

用户也可退出并保留 worktree 中的文件，之后继续。这里不建立独立的“候选—报告—健康度—晋升”生命周期。执行日志和试运行回执进入既有任务记录；用户界面按需读取这些事实，不要求 Agent 创建 report 文件才能结束。

第一阶段以受限文件工具、Storage、ABCP 原生调用和 Workflow 执行为基础；通用 Shell/Python 和多 Agent 编排在后续阶段加入。模型循环、发布文件、权限与任务状态相互独立：完成一次生成不表示已发布，平台执行成功不自动等于用户业务目标完成。

机械层继续只承担权限、身份、协议、版本、资源与形式一致性。是否可替换 composite、应该合并哪些步骤、是否值得再试和业务目标是否满足，由模型结合原始目标与证据判断。

## 2. 当前代码证据与部署边界

### 2.1 2026-09-29 工作区核对

工作区 HEAD 仍为 `0b0617727573e7fe3a26dd4cd419811eeaafe289`，存在未提交修改。下表描述本次读取的代码，不能据此认定正在运行的程序已经更新。

| 代码 | 已核实事实 | 设计结论 |
| --- | --- | --- |
| [workflow-segments.md](../harness/prompts/resources/browser/workflow-segments.md) | 在线 segment 在需要模型判断、截图、Harness 工具或诊断时交还 Agent；没有要求所有 segment 只有几步 | 保留平台合同知识；Builder 另写长 Workflow 重构指导，不能照抄在线切分条件 |
| [Workflow execute](../abcp-platform/packages/actions/src/domains/Workflow/execute/exec.ts) | 入口先 `compileWorkflowDefinition(params.workflow)`，再创建执行并运行 | DSL 编译权威在 WebCross，不新建 Python 编译器 |
| [compiler.ts](../abcp-platform/packages/workflow/src/core/compiler.ts) | 解析步骤/schema、递归检查 Action 和禁止嵌套 execute | 复用实际平台错误；不在 Harness 重复维护另一套 DSL 语法 |
| [action.ts](../abcp-platform/packages/workflow/src/steps/action.ts) | 参数引用在步骤执行时解析，再调用内部 RPC | 编译通过不表示所有动态引用/业务条件已验证；试运行仍可能产生部分副作用 |
| [workflow_policy.py](../harness/workflow/workflow_policy.py) | 目前混合权限、资源、DSL 结构、固定导航顺序及旧拼写转换；默认有 100 静态步骤、50 循环次数和 600000ms 相关限制 | 拆清职责，删重复 DSL/旧格式修复；资源限制独立配置，不用它定义 Skill 的业务边界 |
| [dismiss_overlay.py](../harness/tools/browser_tools/composites/dismiss_overlay.py) | 同时包含原生操作和 VL 定位/验证分支 | 能确定表达的分支可重构进 Workflow；不能声称所有 composite 都只是原生 RPC 的薄封装 |
| [collect_items.py](../harness/tools/browser_tools/composites/collect_items.py) | 含浏览器采集及 Harness `record_extraction` 落盘 | 浏览器段可展开；任务数据落盘职责仍在 Harness |
| [registry.py](../harness/skill/registry.py) | 默认目录为项目根 `skills/`；当前还有 match、suite、draft/tested 与 `.create_report.json` 逻辑 | 新实现只保留元数据读取、索引、确切名称选择和显式执行职责 |
| [schema.sql](../harness/storage/schema.sql) | `task_resources` 有 task/run、external_path、hash、版本；没有全局 Skill 表 | 任务产物复用资源表；新增独立 Skill 索引，避免任务删除级联删除已发布 Skill |
| [base.py](../harness/storage/base.py) | 外部资源类型已含 `coding_agent_output` | 可复用资源合同；类型存在不表示 Coding Agent 已实现 |
| [main.py](../main.py) 的 `_run_browser_mode` | 使用 LeadAgent 对象承载部分基础设施，但没有 Lead 模型轮次 | `/browser` 后续由 Browser 模型发起 Coding 委派，不能让纯调度代码代替语义判断 |
| [spawner_core.py](../harness/spawner/spawner_core.py) | 管理 Browser Worker、Fleet、租约和认证 | Spawner 是 Harness 内的资源/生命周期组件，不是新增决策 Agent |
| [tool_policy.py](../harness/tools/tool_policy.py)、[abcp_client.py](../abcp_client.py) | 已有 readApi 方法级权限及响应脱敏路径 | 共享调用现有入口；网络诊断仍由 WebCross 执行 |
| [llm](../llm)、[messages](../harness/messages)、[recorder.py](../harness/events/recorder.py) | 已有 provider、类型化消息、日志和用量 | Tau 使用 adapter 接入同一权威记录 |

本轮 `main.py` SHA-256 为 `266451a01ca1dd6fec20220974db3c4414c81811f6693afd2aca413c2670c388`；`lead_tools.py` 为 `8180967faf7ad58e29a9d41940a187e993a5046c951036eba0882bff952c06e9`。上版引用的 `_direct_finalize_from_worker` 已不在本轮 `lead_tools.py` 中，不再把旧函数位置作为当前实现证据。

### 2.2 部署版本须单独核对

[2026-09-26 复现记录](pdd-upload-reproduction-2026-09-26.md)中的安装 WebCross 是 `0.9.35-beta`、sourceRevision `b74fbd72441bb53744dd83c6ed66db2d80b4c798`；当时平台工作区 HEAD 是 `6924023865e5a1a131ce110bb08974198dbc678d`。这些只描述该次运行。

试运行记录 Harness revision/dirty fingerprint、Tau revision/patch、Python/依赖、WebCross 安装版本/sourceRevision、live catalog/schema、模型配置和文件 hash。缺失字段标未知，不能用工作区版本冒充部署版本。

跨层问题先核对原始请求/回执与时序，再区分 Harness 编排/权限、WebCross 协议/执行、模型判断和部署版本责任。不能用重试、提示词或 Runtime.evaluate 绕行来掩盖平台缺陷。

## 3. Tau 核心与应用层：直接修改还是自行重写

### 3.1 本次上游核对基线

2026-09-27 获取并检查了 Tau 源码，基线为：

- 仓库：<https://github.com/huggingface/tau>。
- 提交：`c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3`。
- 提交时间：2026-09-23；`pyproject.toml` 版本：`0.4.5`。
- 包声明：Python `>=3.12`、Pydantic `>=2.11`、MIT 许可；整包还包含 TUI 等依赖。
- 核对方式：源码阅读；尚未在本项目执行 Tau 集成测试。因此它是实施候选基线，不是已验证生产版本。

旧文档的 `0a67734... / 0.4.1` 保留为历史背景，不再作为本版的当前基线。后续升级按固定提交与补丁测试进行，不跟随浮动 main。

上游分层为 `tau_coding → tau_agent → tau_ai`。本轮看到的 `tau_agent` 核心模块依赖标准库和 Pydantic，Provider 是可注入协议；`tau_coding.session` 则接入模型目录、凭据、项目资源、扩展、会话管理和上下文压缩等应用设施。[上游 README](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/README.md)、[依赖声明](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/pyproject.toml)

### 3.2 选型比较与决定

| 方案 | 收益 | 主要代价 | 决定 |
| --- | --- | --- | --- |
| 整体接入/继承 Tau CodingSession 应用 | 现成 CLI、会话、文件工具和模型配置 | 与当前 provider、Storage、CLI、权限、日志形成双重所有权；大量行为需覆盖 | 不采用 |
| 固定 Tau 核心，适配本项目应用与执行服务 | 复用循环、事件、取消和队列；保留现有数据与权限体系 | 维护消息适配及有限核心补丁 | **采用** |
| 自行从零写 Agent loop 与会话核心 | 所有类型可直接沿用 | 自行承担工具配对、取消、steer/follow-up、历史修复与事件边界维护 | 当前不采用 |
| 引入 Pi TypeScript SDK sidecar | 可直接使用 Pi 生态 | 增加 Node/IPC、跨语言消息与部署链路；未解决已有服务复用问题 | 暂不采用 |

明确回答：**核心在 Tau 基础上改；我们的应用会话、工具权限和业务服务自己实现或复用现有代码。** “自己实现应用层”包括 `SkillBuilderSession` 和将来的 Coding profile，不包括再写一个与 Tau 并行的完整 Reason/Act/Observe 循环。

Tau 的 `AgentHarness` 可注入 provider/tools，提供运行、取消、steer/follow-up 和消息管理，适合作为会话底座。[harness.py](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/src/tau_agent/harness.py)、[provider.py](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/src/tau_agent/provider.py)

当前 `CodingSession` 文件约 5,095 行，承接 Tau 自己的 provider 配置、凭据、会话树、资源发现、扩展与自动重试/压缩等职责。这个依赖范围是避免直接继承它的依据，行数本身不用于判断代码质量。项目只选取有明确收益的工具算法或接口设计，保留来源与测试。[session.py](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/src/tau_coding/session.py)

### 3.3 核心复用与补丁边界

| 能力 | 当前基线源码事实 | 接入方案 |
| --- | --- | --- |
| 模型响应与工具结果轮转 | 已有 | 复用循环和事件结构 |
| 工具参数准备 | `AgentTool.prepare_arguments` 有声明，loop 未消费 | 在共享 executor 执行一次；不在 Tau 与 Harness 各处理一遍 |
| 工具参数 JSON Schema 校验 | loop 未实现 | 复用现有工具校验；DSL 本身交给 WebCross |
| before/after hook | 已有，before 位于工具查找前；after 异常未独立隔离 | 通过一个 executor 接入点承接完整生命周期，避免重复 hook |
| `terminate` | 结果类型存在，loop 未据此停止 | 增加受信任的终止控制信号，先持久化结果再结束 |
| `length` | 与普通响应一样可能继续处理 tool calls | 增加截断分支，不执行不完整调用 |
| 工具进度 | `_run_tool` 缓存 updates，执行结束后发出 | 接入有界实时事件队列，保留终态和取消 |
| domain failure | 正常 Python return 默认得到 is_error=False | executor 显式映射失败回执，保留原始状态 |
| 并行执行 | 工具类型声明 execution_mode，当前 loop 逐个执行 | 第一阶段保持串行；不依赖声明推断已有并行 |
| 历史修复 | 核心会补齐/整理工具配对 | 只修复模型上下文；不能据此伪造执行结果或覆盖权威记录 |

以上 loop 事实基于本次固定提交。[loop.py](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/src/tau_agent/loop.py)、[tools.py](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/src/tau_agent/tools.py)、[tool_history.py](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/src/tau_agent/tool_history.py)

Tau 自带 Bash 在应用所在机器上调用 `asyncio.create_subprocess_shell`，以 cwd 指定工作目录；没有构成我们需要的文件、网络和凭据隔离。其非零退出可在 details 中返回，必须映射为执行失败。第一阶段不注册这个 Bash；第二阶段接入项目的真实沙箱后端。[应用工具实现](https://github.com/huggingface/tau/blob/c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3/src/tau_coding/tools.py)

### 3.4 引入与维护方式

首版采用受控 vendoring：保存固定 `tau_agent` 源码及其必要内部模块，放入项目私有命名空间，保留上游许可证、原始提交、文件清单与逐项补丁记录。业务工具、站点知识、Lead 调度和 WebCross 适配全部放在 vendor 目录外。

- 不直接依赖整个 `tau-ai` 应用包，以免把 TUI、凭据与 provider 实现作为运行必需项。
- 不用 `sys.modules` 替换或运行时 monkey patch。内部 import 调整作为可追踪的机械补丁。
- 定义一个可注入的 ToolRunner 边界，向 loop 返回 `result / is_error / trusted_control`，并提供进度事件；业务逻辑留在项目 executor。
- loop 只修改执行接缝、终止、截断和进度等控制行为；外部服务负责参数、权限和持久化。无需把整个 BrowserAgent 导入 Tau。
- 将相关上游核心测试与项目合同测试一并维护；升级时比较上游改动与本地补丁，能撤销的补丁应撤销。
- Tau 消息用于循环内状态；本项目消息/Storage 是权威记录。不能同时落 `.tau/sessions` 与项目任务库作为两份可写真相。
- 正式实施先验证 Python 与 Pydantic 依赖，不按旧文档中的本机路径假定解释器可用。依赖调整必须回归现有 provider、消息和 Storage。

第一阶段初期设置一次选型检查点：跑通真实 provider、多模态/工具配对、终止、取消和持久化桥接。若证据显示必须复制大部分 Tau 应用、引入第二套重试或大量改写核心，暂停扩散改动，提交具体成本与替代方案再评审；不因一个适配失败直接转向从零重写，也不为维持选型强行保留双重基础设施。

## 4. Harness、Spawner 与共享执行服务分别做什么

### 4.1 术语与角色

上版的 Host 指承载 Agent 的应用程序。为避免被理解为新的中控 Agent，本版统一使用 **Harness 运行时**：它就是现有程序中会话入口、工具执行、权限、消息、持久化和资源管理等基础设施的合称，不新增一个 Host 服务或 Host 模型。

| 组件 | 做什么 | 谁作语义决定 |
| --- | --- | --- |
| Lead / Browser / Coding / Builder | 读用户目标，选择工具和下一步，解释结果 | 对应的模型 |
| Harness 运行时 | 接收输入、落实工具调用、校验权限、保存回执、管理会话 | 不替模型选择业务路径 |
| Spawner / Worker 调度器 | 按明确的委派请求启动、等待、恢复和取消 Agent | 调用它的 Agent |
| 文件/沙箱执行模块 | 读取、写入、检索、执行代码、导出产物 | 发起操作的 Agent |
| 浏览器执行模块 | 经 ABCPClient 调用 WebCross，绑定页面、处理回执 | 发起浏览器操作的 Agent |
| WebCross | 编译 DSL、执行原生 Action/Workflow，返回真实结果 | 执行已提交定义；需要新模型判断时交还调用方 |

现有 `/browser` 借用 LeadAgent 类的基础设施容易混淆，应逐步提取可独立使用的运行上下文；不能以实例类型名声称 `/browser` 已有 Lead 推理。

### 4.2 “共享执行服务”的具体含义

它是项目内部共用模块和接口，第一版不需要常驻服务或跨进程 RPC。

- 文件处理：Browser、Builder、Coding 的读写/搜索都调用同一权限与 Storage 实现。第二阶段它们的 Python/Shell 工具调用同一个隔离作业后端，避免各自维护权限、取消、日志和产物规则。
- 网络诊断：Browser 和 Builder 的 `Network.readApi` 仍沿 `browser_call → 现有权限/页面绑定 → ABCPClient → WebCross` 执行。共享的是这个入口及脱敏、回执逻辑，不是把浏览器 Cookie 导入通用 HTTP 客户端。
- 公共网络：第二阶段提供独立的 web_search/web_fetch；与当前登录页面的网络响应观察是不同能力。

例如 Browser 确认图片需要转码后，可直接调用共享沙箱处理一次文件；需要编写和调试一组脚本时，才委派 Coding。是否委派由模型判断，执行后端不会自行启动一个 Agent。

### 4.3 最终结构

```text
Harness 运行时：CLI / 会话 / 权限 / 日志 / SQLite / Spawner
  ├─ /skill-create → Builder（Tau runtime 的 skill_builder profile）
  ├─ /lead         → Lead 决策 → Browser 与 Coding Worker
  └─ /browser      → Browser 决策 → 直接工具或委派 Coding

Builder / Coding → 固定 Tau 核心 + 项目 provider / 消息 adapter
各 Agent 的工具 → 共用文件、沙箱、浏览器执行模块
浏览器执行模块 → ABCPClient → WebCross
```

第一阶段只开放 Builder；第二阶段开放共享沙箱；第三阶段接入 Coding 委派。各角色共享实现，不自动共享权限，也不要求现有 Browser/Lead 全部迁移到 Tau。

建议目录职责：`harness/_vendor/tau_agent/` 固定核心；`agent_runtime/` 会话适配；`skill_builder/` 构建模式；已有 `harness/skill/` 替换为精简加载/索引/发布/执行；`sandbox/` 后端；`coding/` Worker 适配。优先复用已有模块，不为了图上的名称各建一个服务。

## 5. Workflow 抽取与重构规则

### 5.1 与现有 segment guide 的关系

[segment guide](../harness/prompts/resources/browser/workflow-segments.md)主要指导在线 Browser 如何把当前已知操作组成一段 Workflow。Builder 有完整历史资料，能够消除当时的信息缺口，因此**复用其协议知识，重新判断切分位置**。

保留：live Action/DSL 格式、原始回执与 Harness hydration 差异、引用/变量/store 语义、事件窗口、页面身份更新、错误与不确定副作用处理。重新评估：因当时需要截图、某个 composite、一次诊断或模型解读而产生的 segment 边界。

`bounded` 表示有资源和终止边界，不等于短路径。分页、详情循环、条件分支和确定性数据转换可以组成一个较长 Workflow。目标是减少不必要的模型往返，不以增加节点数作为成果。

### 5.2 模型需要完成的重构分析

以下写入 Builder 的工作指导与例子，不实现为固定切分算法：

1. 读原始需求及成功/失败路径，确定输入、输出、实际副作用与运行前提。
2. 沿数据依赖与决策依赖整理流程，不按 trace 的工具调用边界原样复制。
3. 找出历史中的样本值、当次 page/Fleet/node ID、临时路径、凭据和需抽取的参数；保留原数据字段含义。新执行重新观察和绑定目标。
4. 判断哪些在线模型决策现在能用明确输入、页面事实、条件和循环表达；可表达的连续部分合并。
5. 检查 composite 实际职责，把有证据支持且原生 DSL 可表达的部分改写为原生步骤；保留必须依赖模型或 Harness 的部分。
6. 明确各 Workflow 输入/输出、结束条件、外部动作与中断后可恢复信息；通过 WebCross 实跑验证重构。
7. 将确实需要模型的选择和恢复说明写进 SKILL.md；有多个 Workflow 时列明入口文件、数据交接和调用条件。

例如：历史流程是“读列表 → 模型选下一条 → 打开详情 → 读字段 → 模型决定下一条 → 翻页”。如果条目选择和字段提取能由明确规则表达，可以重构成列表/详情/翻页嵌套循环；如果需要判断图片风格或理解不固定文案，那个判断仍交给模型，不把一次样本判断硬编码为普遍规则。

### 5.3 composite 是否构成边界

**composite 名称本身不构成边界；真正边界是下一步依赖的能力与决策。** 不能在未分析实现时声称“大部分可以替代”或“一律不可替代”。

| composite 中的职责 | 可能处理 |
| --- | --- |
| 原生读取、点击、滚动、等待 | 按实际平台合同展开，并与相邻步骤合并 |
| 可明确表达的解析、选择和循环 | 使用平台 transform/if/loop/store |
| 视觉定位或新增模型判断 | 保留 Agent 介入；取截图可原生执行，解释截图仍需模型 |
| `record_extraction`、SQLite 索引、沙箱进程 | 浏览器执行结束后调用 Harness 工具；不能假装是平台 Action |
| 权限、身份验证或敏感信息处理 | 在所属执行层继续落实，不能因展开 composite 而丢失 |

现有 `dismiss_overlay` 含 VL 分支，`collect_items` 含数据落盘。Builder 可以提取其中确定性的浏览器流程，但不会把这些职责一起复制成一段伪原生脚本。

### 5.4 应交还 Agent 的情形

需要新语义判断、用户输入/授权、平台不支持的执行能力、未决副作用补查，或实际证据不足以安全选择下一步时交还 Agent。资源预算与部署能力可能限制长度，必须明确报告限在哪里。

当前 Harness policy 的步骤/循环/时限默认值需与部署能力和累计资源预算重新对齐。审计强制导航步骤顺序等历史规则，删除无协议依据的策略门禁；保留真实身份、权限与资源约束。不能只改抽取提示就声称支持长 Workflow。

第一阶段不引入另一套 Hybrid DSL 解析器：一个 Skill 可以包含一个 `workflow.json` 或多个 `workflows/*.json`，SKILL.md 说明模型介入及这些文件的调用关系。每份平台 Workflow 使用 WebCross DSL，模型在明确边界选择下一份，不虚构 Workflow 内部能直接调用 Harness 工具或嵌套另一个 Workflow。

## 6. Skill Builder 会话、工具与用户操作

### 6.1 入口与编辑

`/skill-create <源任务目录或 task_id> [创建要求]` 创建一个新的构建任务，拥有自己的 task_id/run_id；源任务仅作为资料引用。中间文件写入该构建任务的 worktree。源任务失败或不完整也可作为参考，不设置“必须原任务成功”的机械门槛。

Builder 在阅读、写文件、调用 WebCross、理解错误和编辑之间持续循环；结束/退出后返回主入口。恢复时重新核对文件 hash、权限、页面绑定和未决执行。只清理自身持有的订阅/作业，不关闭用户页面。

### 6.2 工具范围

| 工具/接口 | 阶段 | 用途 |
| --- | --- | --- |
| read/search_task_resources | 一 | 通过 Storage 读原需求、轨迹、回执与文件，兼容文件/DB 来源 |
| workspace_read/write/edit/list/search | 一 | 受限工作目录读写，支持 expected_hash；写入同步 SQLite 资源索引 |
| describe_abcp_action / 获取 Workflow guide | 一 | 查询部署合同与版本 |
| browser_call | 一 | 原生探索、页面恢复及有权限的 readApi |
| run_workflow_trial | 一 | 读取指定文件/hash，经执行准入后调用 WebCross；返回编译或执行事实 |
| read_attempt_context | 一 | 按需读取错误、部分结果和日志 |
| finish_skill_create | 一 | 返回文件引用、简短说明与实际试运行情况，结束模型运行 |
| publish/edit/delete Skill 操作 | 一 | 用户界面/命令触发；落实文件与 SQLite 变更 |
| sandbox_exec/read/cancel、search_files、web_search/web_fetch | 二 | 通用执行、物理文件 grep 与公共网络 |
| delegate_coding / 等待/恢复/取消 | 三 | 由 Lead 或 Browser 模型明确委派 Coding |

删除上版 `save_skill_candidate`、`compile_skill_candidate`、`save_validation_report` 及其独立存储/状态设计。普通文件保存、版本/hash、试运行回执已经足够承担编辑与追溯需求。

### 6.3 发布、编辑、删除

完成一次生成后展示文件和简短试运行摘要，不要求质量报告、健康度或通过率。用户选择：

- **发布**：把当前 worktree 中选定的文件版本写入根目录 `skills/<skill_id>/`，更新全局索引。保留 name/version/description/domain/fields/输入输出等原元数据；使用用户字段或可追溯的新值，不丢弃未知扩展字段。
- **编辑**：继续当前 Builder 会话；编辑已发布 Skill 时先复制其明确版本到新的/当前任务工作目录，用户再次选择发布后更新正式文件。
- **删除**：明确作用对象为当前工作文件或已发布 Skill；按用户所选范围删除并更新对应索引。UI 应展示具体目录/版本，不能因同名把另一份文件删掉。

发布决定绑定当前文件 hash 与目标版本，避免用户选定后文件被改写。原生工具权限、文件结构、版本冲突仍检查；不按试运行成功率或 health 状态拒绝用户选择。试运行失败或未执行如实提示，用户仍可决定发布。

用户退出时可保留工作文件。发布/编辑/删除的结果由工具回执确认，不让模型文本冒充用户动作。运行中的任务固定所选版本/hash；编辑或删除后不能静默切换到新文件，缺失时返回具体状态。

## 7. Provider、消息、事件与工具生命周期

### 7.1 Provider 与消息适配

`HarnessProviderAdapter` 实现 Tau 的 `ModelProvider.stream_response`，转接现有中立 LLM API。保持 provider 原有超时和可重试错误处理，不在 Tau 应用层再叠加自动重试。

适配需保留有序文本/图像/工具块、调用 ID、停止原因、模型元数据、必要的 provider opaque 信息及原始 usage 引用。当前接口若只能返回完整响应，先产生真实 start/end 事件；不把完整文本切片伪装成真实 token 流，不虚构首 token 耗时。后续真实流式接入独立验证。

模型上下文是权威消息的投影。压缩复用现有 context 服务，保留源目标、用户授权、Skill 文件版本、未完成事项和证据引用；历史工具配对修复只影响重放上下文，补合成记录须标记 `synthetic`，不能当作实际执行回执。

Tau usage 默认零值不能覆盖当前 provider 的未知统计。成本与 cache 分类由现有计费记录作为来源，未知记 null/available=false。恢复或事件重放不能重复累计一次模型请求的 token。

### 7.2 单一工具生命周期

```text
接收完整 ToolCall 并登记 ID
  → 查找工具与准备参数
  → schema / 引用校验
  → 权限、绑定、预算、版本与资源准入
  → 可信参数发生变化时重新校验
  → 执行并持续发出进度
  → 记录执行回执与后处理诊断
  → 提交 ToolResult 和权威状态
  → 处理可信终止信号，或继续 Reason
```

- prepare 只复制、按 schema 补齐已声明默认值；不猜业务字段、不把错误类型静默改成正确类型。
- 工具查找由 executor 显式完成。当前 `validate_registered_tool_call` 对未知工具返回空问题列表，调用方不能把它解释为“工具合法”。
- 此处 schema 校验限于工具/RPC 外壳和本项目接口；DSL 语法、递归引用规则及平台编译由 WebCross 负责，见第 9 节。
- page/Fleet、任务 ID、权限集和预算来自 Harness 运行时。显式参数冲突返回冲突事实，不静默改成另一个目标。
- 准入与执行使用同一版本/资源租约；防止通过改名、符号链接或调用后换文件绕过检查。
- after 只记录事实和诊断，不自动重跑、处理浮层或隐藏模型调用。
- after/展示失败保留真实执行结果。权威记录提交失败则停止后续派发并进入恢复流程，不因“没有日志”重做已经派发的外部动作。
- 每个已接收调用 ID 都有终结记录，包括校验拒绝、取消、跳过与派发未知。外部副作用不存在通用 exactly-once 保证，恢复必须保留不确定性。

事件桥接明确单一所有者：Tau 事件转到现有 LifecycleRecorder，executor 提供执行详情，避免两边各写一次 tool_start/tool_end。普通 UI 订阅失败可隔离；权威存储失败需要明确停止派发，不能吞掉。

### 7.3 执行回执合同

```json
{
  "receipt_id": "runtime-generated",
  "task_id": "...",
  "session_id": "...",
  "tool_call_id": "...",
  "attempt_id": "...",
  "execution_kind": "workflow_rpc",
  "status": "failed",
  "stage": "execute",
  "transport": {"request_id": "...", "request_sent": true},
  "action_dispatch_state": "unknown",
  "side_effect_state": "unknown",
  "workflow_id": "platform-generated-or-null",
  "workflow_hash": "...",
  "data_ref": "...",
  "availability": {"store": "unavailable", "results": "partial"},
  "error": {"code": "...", "message": "...", "source": "platform"},
  "diagnostics": [],
  "artifact_refs": []
}
```

合同版本化；字段不适用于某后端时明确标记，不填假值。`request_sent` 为 true/false/null，`action_dispatch_state` 为 not_dispatched/dispatched/unknown，副作用状态独立。模型、RPC、Workflow 和子 Action 的层次不能混淆。

结果内容中的 `terminate` 字符串不具备控制权。终止通过 ToolRunner 的受信任控制通道返回，只允许 profile 声明的终止工具产生。

敏感信息在进入 transport 日志、Storage、模型视图、offload 和导出前遵守统一处理策略。保存可追溯的脱敏回执；确需保存受保护原文时必须有明确存储权限与保留策略，不能默认给模型或沙箱开放。

## 8. 停止、完成、等待与恢复

模型 stop/toolUse/length/error/aborted 描述响应停止原因，不是业务完成或发布状态。Tau 核心补丁保留第 3 节范围。

- toolUse：执行完整合法调用，回填真实结果，再 Reason。
- stop：接收模型结论；没有明确创建结果时提示缺失事实或记录未完成，不因文件存在自动判断目标满足。
- length：不执行可能截断的调用，在累计预算内修复或压缩上下文。
- error：沿用 provider 层重试，不能重做已执行浏览器操作。
- aborted/预算耗尽：停止新派发，保留文件、SQLite 引用、日志与未决状态；取消不等于副作用回滚。

`finish_skill_create` 只需 outcome、文件/资源引用、版本/hash、简短说明与未完成事项。没有 candidate_id、validation_report_id 或生成质量评分。运行时校验引用、权限与形式一致性，语义结论由模型/用户给出。

finish 的可信控制信号提交后结束当前模型运行；同轮后续工具有 skipped 回执。工具返回正文中的 terminate 字样没有控制权。Coding Worker finish 返回委派者，不终止 Browser/Lead 总任务。

等待用户发布/编辑/删除时模型不空转。是否已发布记录在 Skill 索引，试运行状态记录在 attempt，任务状态记录在会话；不增加彼此联动的健康度或晋升状态机。

## 9. DSL 校验与 WebCross 实跑的责任划分

### 9.1 不新增第二套 DSL 静态编译器

现有 WebCross `Workflow.execute` 已先调用 `compileWorkflowDefinition`。Builder 可以直接提交脚本，由平台编译/执行回执驱动 Agent 修改，无须先调用一个 Harness 自建的 compile_skill_candidate。

仍然需要校验，但各层只承担自身职责：

| 层 | 承担的检查 |
| --- | --- |
| Builder 文件工具 | 文件可读取、JSON/YAML 等载体可解析、hash 与用户选定版本一致 |
| Harness 执行准入 | 授权方法、页面/Fleet、资源预算、敏感数据、输入输出路径、显式禁止项；工具/RPC 外壳合法 |
| WebCross 编译/执行 | DSL 结构与 Action 合同、递归步骤、实际运行时变量/引用、事件和平台约束 |
| Agent loop | 解释编译/执行错误、决定如何修正、是否继续与业务目标是否满足 |

Agent loop 不能代替权限约束，也不能把执行后的副作用撤销。平台编译通过后，动态引用和页面条件仍可能到执行时才失败；因此“直接跑一遍”必须在已有授权内进行，失败后不能盲目整段重放。

一次试运行只覆盖实际走到的输入和分支。模型依据改动与目标选择补充验证，不机械要求跑遍所有分支，也不能把一次成功描述为所有参数组合都已验证。

如后续需要无副作用的 lint/dry-run，应由 WebCross 暴露同一 compiler 的接口或复用同一包，而非 Python 重写一份。第一阶段不以新增这种平台接口为前置条件；未提供时明确区分“编译失败前未执行”与“已执行部分步骤”。

### 9.2 现有 Harness policy 的处理

将 `validate_workflow_params` 中的职责分类迁移：权限、身份、资源、数据处理保留；重复 DSL 词汇/引用规则、旧格式修补及无协议依据的步骤顺序规则删除或交回平台。最终保留一个轻量 execution admission，不维护“新旧两套 DSL validator”。

当前 readApi 在 Workflow 内会早于 Harness 脱敏落盘，仍以直接调用恢复；这是具体数据边界。原生/Workflow 权限一致，不能以重构长 Workflow 绕过禁止方法。无法确认准入的复杂结构返回具体原因，而非猜测字段意义。

### 9.3 执行服务与回执

复用 ABCPClient、原始错误解析、页面租约与通知接收；Builder 使用独立能力配置，不伪装为 form_filling 取得权限。模型获知当前可调用方法和拒绝原因，不能自行改权限。

每次 trial 绑定 task/run/attempt、文件路径与 hash、输入、schema/catalog、页面/Fleet。实际执行已读取的文件内容；编辑不会改变在途定义。

正常 RPC return 检查实际 Workflow 状态；failed 映射为工具失败，缺少终态记 unknown。失败详情优先读取平台的 variables/store/results/嵌套 Action 错误，进度与 getStatus 只补可得字段，区分空对象与数据未提供。

ExecObserver 当前首 workflowId 认领存在归属限制：优先使用实际 request/attempt/workflow 关联；未能区分的通知作用域内串行启动。未归属事件不填入当前执行记录。暂停/失败/取消按平台真实状态处理。

### 9.4 恢复与混合执行

模型可决定整段重跑、修改输入、编写剩余流程或恢复页面。新 execute 使用新 attempt/workflowId；paused 才考虑平台 resume。不能按 failedStepPath 机械切片，也不能虚构 initialStore/startStep。

一个 Skill 的多个 Workflow 与 Agent 介入都关联同一 invocation；文件变更后的结果归于实际 hash。中间数据用真实可得结果或任务资源交接，不能把仅存在于失败执行的未导出 store 当作永久产物。

平台协议/执行问题列为 WebCross 修复；Harness 负责准入、结果投影与交接，不靠隐藏重试掩盖。删除旧“一次尝试后永远禁止 Workflow”和 autoheal 重放路径，用实际 invocation/attempt 记录新执行。

## 10. 文件目录与 SQLite 索引

### 10.1 文件布局

使用项目现有的 **`skills/`（复数）**。正式 Hybrid Skill 与其 Workflow 都在此目录；构建/编码中间文件在对应任务 worktree。

```text
项目根目录/
  skills/
    <skill_id>/
      SKILL.md                     元数据、使用与混合执行说明
      workflow.json                单入口时使用
      workflows/*.json             确有多个入口时使用
      ...                          该 Skill 引用的必要文件
  worktree/
    <builder_task_id>/
      coding/<session_id>/         编写中的 Skill、脚本、临时文件
      artifacts/                   需交付/交接的文件
      ...                          现有任务轨迹与观察资源
    <browser_or_lead_task_id>/
      coding/<worker_id>/          该任务 Coding Worker 的中间产物
    harness.db                     默认路径；以现有配置解析为准
```

Builder 是新任务，`source_task_id` 只引用历史任务，不把新产物写回源任务。原有 fields/version 等元数据保留；fallback.yaml 如已有内容仍可作为资料/显式执行说明迁移，不能把它作为旧 health/autoheal 恢活入口，也不强制所有新 Skill 生成该文件。

### 10.2 SQLite 最小索引

| 对象 | 存储方案 | 必需关联 |
| --- | --- | --- |
| Coding/Builder 任务文件 | 复用 `task_resources` 外部文件资源 | task_id/run_id/worker 或 session、logical_path/external_path、hash/size/media_type/resource_version |
| 当前正式 Skill | 新增全局 `skill_index`（表名拟定） | skill_id、根目录相对路径、当前 version/hash、原 metadata JSON、删除标记/时间 |
| Skill 版本记录 | 新增 `skill_versions`（表名拟定） | skill_id/version/hash、文件清单、来源 task/resource、创建/发布时间、该版本内容是否仍可得 |
| trial/正式调用 | 复用 run_events/task_resources 的执行记录 | invocation/attempt、skill/version/hash、输入/输出引用、实际状态 |

全局 Skill 索引不设置会随源任务删除而级联删除的强关联。`task_resources` 受 task/run 外键约束，不能直接用它充当全局 Skill 注册表。

文件是 Skill 与 Coding 文件内容的来源；SQLite 保存索引、元数据和关联，不再建立一份可以独立修改的脚本文本真相。日志按既有 Storage 方式保存。第一阶段这两类索引必须落 SQLite，即使一般 trace 仍采用文件后端，也不能只在内存目录扫描中维护 Skill。

### 10.3 同步、编辑与删除

普通文件写入/改名/导出后更新资源索引；脚本生成的文件由执行结束/显式导出时扫描登记。用户手改文件时，在打开/发布/执行前比较 hash 并更新索引，不能悄悄使用过时记录。

文件系统和 SQLite 不是同一原子事务。发布使用临时文件写入、原子替换和可恢复的操作记录，随后提交索引；崩溃后按文件 hash 完成或回滚索引操作，不能只因 DB 行存在就宣称发布完成。删除同步处理文件与索引，失败明确报告实际剩余状态。

历史 hash 只证明身份，不保证旧字节仍存在。需要恢复的版本保存真实文件快照并索引；没有快照时明确 unavailable。用户删除 Skill 时按所选范围处理版本文件，运行日志可保留删除事实和版本/hash，不假装还能执行已删除内容。

### 10.4 沙箱到浏览器产物桥接

第二阶段：`sandbox output → Harness 登记 artifact/hash → 导出获授权的浏览器可读路径 → Browser 使用与验证`。沙箱路径不能直接冒充浏览器路径。

Harness 自行核对实际文件、hash、大小和范围，脚本提供的 manifest 只是声明。防止路径穿越、符号链接和并发替换；任务账本不可由脚本修改。输出同时登记任务 SQLite 索引，保留来源输入与 job 关系。

## 11. 第二阶段：共享 Python/Shell 沙箱与检索、网络

### 11.1 执行环境选型与边界

优先评估受控 Linux 隔离执行环境；在 macOS 上运行的 Harness 可通过独立 VM 内的容器/作业后端接入，部署方案在第二阶段起始时完成验证。仅设置 cwd、venv、环境变量或命令白名单都不构成所需隔离。未通过验证时返回能力不可用，不退回本机裸 subprocess。

后端协议包括：prepare/import、start、read/status、cancel、export、cleanup。Python 与 Shell 共用 job 管理和权限，不各建一套账本。工具可提供语言入口或命令入口，由 Harness 选择固定解释器与依赖环境。

隔离合同：

- 只导入用户授权输入，默认只读；输出/临时目录单独可写；源任务账本、凭据、浏览器 profile、本机 socket 和控制通道不可见。
- 不继承应用进程环境中的模型 key、代理凭据、SSH agent、Docker socket 或浏览器远程调试端点。
- 资源上限在外部执行层落实：墙钟时间、CPU、内存、进程数、磁盘和输出；具体默认值通过代表性任务校准。
- 取消清理完整作业及后代进程，区分请求取消与确认停止。超时回执保留已生成产物和未确认外部状态。
- Python 包与工具版本使用可复现运行环境；需要新依赖时经受控下载/构建渠道处理，不能默认让脚本拿到应用凭据联网安装。
- 每次运行保存环境镜像/依赖版本、输入清单、权限、job/attempt、退出码、信号、耗时、stdout/stderr 引用及截断信息。

短任务可在一个工具调用中完成；长任务返回 job_id，允许读取进度与取消，不用固定频繁轮询占用模型轮次。使用明确事件/超时等待，不无限等待失联进程。

### 11.2 文件搜索与编辑

第一阶段已有 TaskResource 搜索和构建目录文本编辑；第二阶段加入 ripgrep 后端、上下文行、glob、大小限制与续查。

- DB 内权威轨迹继续走 Storage 查询。需要用脚本分析时，由 Harness 导出带来源/version 的只读快照。
- 搜索执行失败、权限未覆盖、忽略文件、字节/命中上限分别报告；不能把错误当成“0 命中”。
- 写入/编辑使用 expected_hash 或同等版本约束；冲突时返回当前事实供模型重新读取。
- 工具与脚本操作同一文件时共享资源约束；不能只有结构化编辑工具受限制而 Shell 可绕过。

### 11.3 网络能力分层

| 网络类型 | 执行入口 | 权限与证据 |
| --- | --- | --- |
| 当前页面 API 响应观察 | WebCross `Network.readApi` | 绑定页面/任务、沿用脱敏与日志策略，不自动重放请求 |
| 公共网页检索/抓取 | Harness 运行时 `web_search/web_fetch` | 明确 URL/来源/时间/状态、内容截断与重定向；浏览器 Cookie 不传入 |
| 脚本必要网络与依赖下载 | 受控出口/代理或 Harness 网络模块 | 任务级授权目的地/方法/数据范围；无权限时给出原因与申请范围 |

默认脚本无直接网络；后续按任务授权放开所需出口。Harness 的网络模块检查重定向和解析后的目标，防止通过公共抓取访问未授权的本机、内网或元数据服务；合法私有 API 可通过显式任务授权恢复。网络权限约束数据访问范围，不替模型判断应该选哪个业务路径。

readApi 的无记录、HTTP 成功、业务成功和页面结果分别陈述。按操作时间窗查询，首次不预设“成功/格式错误”正文过滤；是否查询由模型依证据选择。不能每次上传强制读网络，也不能依据后缀自动转换图片。

图片检查/转换作为 Python/Shell 的应用场景：检查实际编码、尺寸、大小，模型按页面事实决定是否转换。改变尺寸、压缩质量、透明背景、动画或方向时显式记录；JPG 不支持透明背景等处理不能隐式造成内容变化而不披露。转换成功后仍由 Browser 验证上传结果。

## 12. 第三阶段：三种入口下谁负责决策

### 12.1 Lead 模式

Lead 为当前任务负责人，Browser 与 Coding 是同级 Worker。Lead 模型决定拆解、委派和整体结束；Spawner 只落实明确请求。Coding 不替 Browser 确认上传成功，Browser 不用脚本 exit_code=0 代替页面结果。

### 12.2 Browser 模式

`/browser` 保持 Browser 为任务负责人，不自动启动一个隐藏 Lead。**Browser 模型判断是否需要编码，并显式调用 delegate_coding；Harness/Spawner 校验并执行委派，结果返回原 Browser 会话。**

```text
Browser 观察页面并判断要处理文件
  → 选择直接 sandbox_exec，或 delegate_coding(子目标、输入、约束)
  → Harness/Spawner 启动 Coding Worker
  → Coding 执行并返回文件/结果
  → Browser 恢复原页面上下文，使用产物、验证并继续
```

“平级”表示角色能力与执行环境没有固定上下级，并不禁止一个角色请求另一个角色完成依赖工作。任务负责人按入口确定：lead / browser / skill_builder。结果按 request_id/owner_session 返回，不能由运行时的机械规则猜哪个 Agent 应接手。

首版委派关系必须无环；Coding 在此委派中只接文件/代码子目标，不反过来自动委派 Browser，页面操作留给原 Browser。已有权限内不增加一次无意义的用户确认；需要扩大范围时返回具体授权缺口。

第三阶段交付前，Browser 只能使用已开放的直接工具；不能把不存在的 Coding 路由写成已可用。是否新增显式切换 Lead 的入口可独立设计，不作为本方案的默认恢复方式。

### 12.3 Skill Builder 模式

Builder 自己编写文件并直接调用浏览器执行模块验证，不为每轮试运行启动完整 Lead 或 Browser 子任务。共用页面租约与身份边界，避免和另一个 Browser 同时写页面。

### 12.4 通用委派合同

请求：task/owner/session/delegation ID、原始目标引用、子目标、输入资源、输出要求、权限与累计预算。结果：执行状态、模型语义结论、文件/证据引用、未完成事项、已发生副作用、使用量。

运行时校验身份、引用、资源与形式矛盾；完成多少文件不构成业务完成门槛。finish 只结束子任务运行；needs_lead_review 在 Lead 模式返回 Lead，在 Browser 主导的编码子任务中应使用通用 needs_owner_review，不伪造一个不存在的 Lead。

提取最小 Worker 生命周期接口（spawn/wait/resume/cancel），Browser 适配器保留 Fleet/认证/租约，Coding 适配器管理 worktree 和 job。取消传播到子作业，页面恢复前重新观察；预算跨委派/恢复累计，不借启动新 Worker 重置。

## 13. 显式 Skill 使用与旧代码整体删除

### 13.1 保留的用户功能

`/skill list` 显示根目录 skills 中已登记文件及原元数据；`/skill <确切名称>` 显式选择下一项任务使用的版本；`/skill off` 清除待选择。browser/lead 均支持，任务恢复保留已选版本，新任务不自动继承。

发布、编辑和删除操作展示明确对象及文件版本。选择 Skill 不强制整段 Workflow：模型按照 SKILL.md 使用一个或多个 Workflow，并在有必要时介入。模型生成 `/skill` 文本、历史命中、suite 名或自动匹配都不构成用户选择。

### 13.2 删除范围

第一阶段用新实现完整替换旧 Skill 创建、自动选择、健康度与维护链路；不保留兼容旧算法的运行开关或 fallback。

- 删除旧 `/skill-create-workflow`、`/skill-create-guidance`、`--recheck/--retry` 及专属蒸馏/模板/“最佳轨迹”抽取。
- 删除旧 `create.py`、`distiller.py`、`autoheal.py`、`heal.py`、`health.py` 的相应实现和调用点。
- 删除 guidance health、workflow/skill health 更新、评分、禁用、冷启动评分和自动晋升逻辑。
- 删除 `.create_report.json`、`.skill_health.json` 等运行依赖、UI 标记和重检入口；旧文件不作为新系统的可用性依据。
- 删除 registry/contract/dispatch 内的自动 match、suite 自动扩展、旧质量门槛与隐藏重试；以精简的显式加载、文件元数据、SQLite 索引和执行入口替换。
- 删除上版计划新增的 CandidateStore、独立 report 服务、报告必填终止门槛及相关实现任务。
- 删除只覆盖上述旧行为的测试、配置和文档；增加新显式流程的测试，不能把旧测试删掉后以“无失败”宣称新流程可用。

“旧代码全部删除”指上述被替换的 Skill 子系统和旧业务策略。通用 ABCP 传输、模型、文件/SQLite、权限、生命周期和执行证据继续服务新实现。用户已有 Skill 正文、Workflow、版本、字段与其他原元数据迁移保留，不随旧代码一起删除。

历史任务/回执仍保留真实执行事实；它们不再派生健康度、排名、自动禁用或发布门槛。新加载器从当前文件/索引读取内容，绝不重新读取 health 文件来决定是否可用。

## 14. 分阶段实施与完成标准

### 阶段一：Skill Builder、文件管理与旧 Skill 链路替换

| 步骤 | 交付 | 验收 |
| --- | --- | --- |
| 1A 核心接入 | 固定 Tau 核心、provider/消息/事件 adapter、ToolRunner、finish/length/进度补丁 | 真实 provider 工具往返、消息保真、取消；不重复计费/记录 |
| 1B 资料与工作区 | 独立构建任务、Storage 读取、worktree 文件工具、SQLite task_resources 索引、会话恢复 | 文件和 DB 来源均可读；中间文件属于构建任务；源任务不变 |
| 1C 长 Workflow 构建 | Builder guide、composite 展开指导、元数据保留、轻量准入、平台执行回执 | 不按旧 segment 分割；不新增 Harness DSL 编译器；真实平台错误可供模型修复 |
| 1D 浏览器闭环 | 原生探索、Workflow 实跑、失败数据、通知归属、页面恢复与新 attempt | 原始回执可追溯；编译拒绝/执行失败/未知分别正确；更长流程可实际运行 |
| 1E 用户操作与全局索引 | 发布/编辑/删除；根 skills 目录；全局 SQLite Skill/版本索引 | 文件与索引一致；保留原元数据；删除源任务不级联删除 Skill；用户手改可重新索引 |
| 1F 显式使用与整体替换 | browser/lead 显式加载新 Skill，删除旧创建/匹配/health/报告/autoheal 全链路 | 没有运行调用点和失效 import；旧健康状态不影响选择；用户文件完成迁移 |

阶段一不依赖通用 Shell 或 Coding 委派，但必须有真实 WebCross 试运行。验收案例包括：合并历史短 segment；展开可替换 composite；保留确实需要模型的混合路径；平台返回错误后模型修复；发布/编辑/删除与 SQLite 一致性；显式使用。

这些是工程验收案例，不是每个 Skill 的业务门禁。环境阻断单列，不以 mock 冒充真实闭环。试运行摘要从日志直接呈现，不创建 report/health 系统。

### 阶段二：共享沙箱、grep 与公共网络

依赖阶段一工具/资源合同。验证一个真实隔离后端，接入 Python/Shell job、进度/取消、输入导入/输出登记和浏览器可读路径；增加 ripgrep、公共检索/抓取及受控出口。Browser 与 Builder 可以调用同一后端。

验收：隔离不能通过路径/网络/凭据/socket 绕过；进程超时/取消可确认；产物进入对应 worktree 与 SQLite；Browser 能使用实际文件；没有裸机 subprocess 降级。公共网络与 readApi 权限分别验证。

### 阶段三：Coding Worker 与两种任务负责人

在同一 Tau runtime 增加 coding profile，提取通用 Worker 生命周期；分别让 Lead 和 Browser 模型获得委派工具。结果回到发起会话，取消和预算关联原任务。

验收：`/lead` 与 `/browser` 均能完成编码交接；Browser 模式没有隐藏 Lead 或机械语义路由；子任务结束不结束总任务；页面和文件版本正确；无委派环路。简单直接执行与 Coding 委派对照比较。

### 阶段四：恢复、兼容与性能完善

完善进程中断、DB/文件同步失败、未知平台状态、作业失联及跨版本恢复。在可证明通知归属与资源独立性后放开并发；测量长 Workflow、直接工具与多 Agent 路径的完整成本。

旧 Skill health/报告/自动创建链路在第一阶段完成删除，不能推迟到第四阶段或以它们作新系统 fallback。本阶段也不引入自动 Skill 选择/发布。

## 15. 验证与成本观察

### 15.1 有必要的工程测试

| 类别 | 重点 |
| --- | --- |
| Tau/模型 | 原始块顺序、call ID、provider 信息、length、finish 后 skipped、usage 未知不填零 |
| 执行准入 | 工具参数、权限、页面绑定、路径与资源限制；DSL 错误由平台原样返回 |
| Workflow | 外层 RPC 成功不冒充 Workflow 成功；编译/动态引用错误；部分副作用；事件归属；长循环与多入口文件 |
| 文件与 SQLite | task_resources 与真实文件；Skill 全局索引；同路径版本；用户手改、删除、崩溃恢复；任务删除不误删 Skill |
| 用户管理 | 发布/编辑/删除绑定所选文件版本；显式选择；无报告/health 前置条件 |
| 隔离/委派 | 资源上限、后代进程取消、网络/凭据边界；Browser 与 Lead 作为 owner；子任务结果继续主任务 |
| 删除旧代码 | 无旧 CLI、自动匹配、health/quality/autoheal 与报告调用点；保留元数据和现有用户文件 |

无需新建每个 Skill 的健康度面板或长期质量评分。保留现有运行回执、错误、token 与耗时，供用户查询和工程对照。

### 15.2 效率口径

对照固定源目标/输入、站点起点、模型配置、缓存条件和 Harness/Tau/WebCross 版本。比较：原在线短 segment 与重构长 Workflow；Browser 直接沙箱与委派 Coding；修改前后的完整任务。

分别报告 cache_read、cache_creation、uncached_input、output，以及模型轮次、压缩成本、工具/平台 I/O、失败重试、人工等待和端到端耗时。没有 provider 数据的字段标未知；费用缺少价格时为 null。

端到端耗时取任务墙钟减去人工等待区间并集，不把并行 Worker 耗时简单相加。试运行和正式调用分开；重复终态不重复计数；失败、取消、运行中和未知保留。只比较用户目标和能力可比的任务，不从节点数、调用数或单次返回大小推断整体收益。

这些是工程观察，不反向驱动 Skill 自动禁用、排序、晋升或发布资格。性能优化不得降低证据完整性、正确性或权限边界。

## 16. 机械约束的范围与恢复

| 约束 | 为何由程序处理 | 合法工作受阻时如何恢复 |
| --- | --- | --- |
| 工具/RPC 外壳与平台 DSL 合同 | 执行双方必须理解同一协议；DSL 由平台校验 | 定位具体平台错误，由模型修正或更新部署；不加站点猜测 |
| 方法/路径/网络授权 | 任意代码和嵌套 Action 不能靠模型自律限制权限 | 导入获授权资源或按真实用户授权扩展范围 |
| page/Fleet 与通知归属 | 避免控制错误页面或把他人结果算入当前执行 | 重新绑定/查询；无法区分时等待或使用独立执行范围 |
| 文件版本/hash、SQLite 关联 | 确保执行/发布对象和回执对应，防止竞争覆盖 | 重新读取变化后由用户/模型继续；修复索引操作 |
| 资源上限、取消与委派无环 | 有限资源和生命周期不变量 | checkpoint、具体预算/依赖错误，调整明确授权或任务拆分 |
| 日志脱敏与控制信号来源 | 防止数据泄露和工具正文伪造结束/授权 | 返回可解析事实与可获授权的读取入口 |

不按 composite 名称、固定 segment 长度、字段非空、站点格式、连续无进展次数或试运行成功率机械否决任务。每项拒绝给出实际原因和恢复路径。新增机械规则应先说明适用范围、误拒绝面及为何模型判断不能替代执行保证。

## 17. 迁移、部署与回退

- 第一阶段把旧 Skill 功能作为一个完整替换单元，列出全部调用点、配置、测试和旧状态文件消费者；新显式链路验收后移除旧实现。
- 已有 Skill 文本/Workflow 和原元数据导入新 SQLite 索引。schema/目录损坏明确显示，由用户编辑；不依赖旧 health 评价决定迁移是否允许。
- SQLite 使用版本化迁移；文件发布/删除有可恢复的操作记录。删除旧代码不删除用户内容和历史任务。
- 回退使用明确的代码/配置/数据库版本及可用文件快照，先检查数据兼容性；不能在新入口中暗中回调旧蒸馏或 health 逻辑。
- 每次交付说明源码版本、实际运行实例是否更新、平台是否需要部署、测试范围和未验证项。

主要风险：Tau adapter 丢消息或重复计费；长流程失败后的未知副作用；WebCross 编译/事件合同版本差异；文件系统/SQLite 部分提交；多 Agent 交接增加简单任务耗时。分别通过合同测试、真实回执、版本绑定、恢复记录和可比任务测量处理，不通过新增质量门禁处理。

## 18. 实施确认项与交付清单

第一阶段固定 Tau 基线/补丁、Python/依赖、模型与浏览器授权、构建 task/worktree、项目根 skills 路径和现有 SQLite 配置。配置能从当前程序读取时直接复用；缺少必需信息再询问。

第二阶段再选择隔离后端、镜像、依赖与网络出口；第三阶段加入 Coding 委派。不得为等待完整沙箱或多 Agent 框架而推迟第一阶段的文件/Workflow 型 Builder。

各阶段交付：代码与删除清单、SQLite 迁移和目录约定、操作说明、合同测试和真实执行证据、版本与部署步骤、恢复/回退方法、可比 token/耗时数据及未交付范围。工程交付说明不变成每个 Skill 的生成报告或健康度系统。

本次只修订方案；源代码删除、数据迁移、发布和浏览器操作按后续批准的实施阶段执行。
