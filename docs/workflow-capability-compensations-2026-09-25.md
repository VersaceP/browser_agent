# Workflow 能力边界补偿记录（2026-09-25）

> 背景：lazy-image 滚动工作流（662f6f90）失败诊断后，本轮针对当前 worker 模型
> （deepseek-v4.1-flash）与 WebCross 0.9.3 的能力边界做了若干 harness 侧补偿。
> 本文逐项记录**删除条件**：模型或平台能力覆盖后应删掉对应补偿，避免形成废代码。
>
> 判定原则（本轮确立）：guide 只写契约事实（字段名、绑定规则、对象形状），
> 不写策略决策（修订还是降级、段写大写小）——决策留给模型，能力提升后指引
> 不残留误导。

## 1. 模型能力补偿（模型变强后删除/精简）

### 1.1 DSL 反模式对照 — `workflow-segments.md` "Step shapes" 段

内容：loop 步骤是 `maxIterations/condition/body`（不是 `steps`/`stopWhen`）；
条件步骤 type 是 `"if"`（不是 `"condition"`）；transform 是 `input/ops/output`
（不是 `expression/inputs/outputs`）；`extract` 是 action 步骤的字段，不是步骤类型。

补偿属性：正面字段名是 schema 契约的复述（**永久保留**）；`never X` 反模式清单
是对 flash 实测混淆模式（reports/scroll-generation-capability.json、
reports/scroll-revision-loop.json）的针对性防御。强模型读 schema + 示例即可写对，
反模式行随之冗余。

删除条件：用 `probe_tests/probe_scroll_workflow_generation.py` 以生产 guide 为
prompt（去掉反模式行后）复测，schemaValid ≥ 5/6 且无 DSL 结构类错误
（type/const、required 于 body/condition/input/ops/output），连续两轮成立即可删
反模式行，仅保留正面字段名陈述。

### 1.2 占位符 pageId 与对象形状告警 — `workflow-segments.md` "Runtime binding" 段

内容：action params 不写 `pageId`/`fleetId`（运行时 binding 注入）；占位符字符串
（`"$pageId"`、`"{{pageId}}"`）会作为字面量下发并失败；编造 UUID 能过校验但运行时
`page-not-found`；Input.scroll 的 `target`/`container` 是 `{"id":"n_..."}` 对象，
不是裸字符串。

补偿属性：核心规则（binding 注入、省略字段、对象形状）是**永久契约事实**；
"占位符陷阱"与"编造 UUID"两句是 flash 实测失败模式（修订轮曾把占位符"修"成假
UUID，reports/scroll-revision-loop.json root-viewport run2）的针对性告警。

删除条件：生成探针中 placeholder-pageId 出现率连续多轮为 0，可删占位符句；
WebCross 编译期错误带上 details（已反馈平台）后，"编造 UUID 过校验"句可删。

## 2. 平台能力补偿（WebCross 补齐后精简）

### 2.1 `harness/workflow/nested_failure.py` 多源合并

内容：`attach_nested_action_failure` 按失败步骤路径从三处匹配合并底层公开反馈
——progress trace 步骤、terminal failure 事件、RPC `details.results` 行——并输出
`feedbackAvailability` / `missingFields` / `sources` 可用性标记。

补偿属性：实测（reports/nested-failure-contract.json）WebCross 0.9.3 把嵌套
Action 的 `observation`/`suggested_prompt`/`error.message` 压缩为
"A nested Action failed."，只保留错误码；harness 只能多来源尽力拼。已反馈 WebCross。

删除条件：WebCross 失败信封直接携带底层 Action 的 observation / suggested_prompt /
error.message 后——多源合并退化为直接读字段；`feedbackAvailability`/`missingFields`/
`sources` 三个标记可删（字段恒在，不再需要可用性描述）。**保留**白名单投影与
长度上限（见 §3），那是安全纪律不是补偿。

### 2.2 `harness/observation/exec_observer.py` 失败事件反馈保留

内容：`step_finished`（error）与 `failed` 事件中保留 observation / suggested_prompt /
error 对象，以及 `action` / `stepRunId`（整数，平台契约）。

补偿属性：progress 事件是目前底层反馈唯一可能的载体（外层信封已丢失），
作为 §2.1 的合并源之一存在。

删除条件：与 §2.1 相同。信封直接携带后，事件侧的 public_failure 提取可简化；
stepRunId/action 标识保留（那是 progress 事件自身的契约字段）。

## 3. 永久改动（非补偿，勿删）

- `public_failure` 白名单投影（仅 observation / suggested_prompt / error.code /
  error.message）+ 1000 字符上限 + 4 层包装下钻上限：投影纪律，与能力边界无关。
- `capability.py` 失败时从 `rpc_data.details` 提升 workflowId / failedStepPath /
  variables / store / completedSteps：WebCross 0.9.3 已定契约
  （docs/workflow-execute-live-contract.md §10）。
- `workflow-segments.md` 的正面契约陈述（字段名、binding 注入规则、对象形状）。
- `Page.wheel` schema 自带输出契约（observedDelta / completedReason 及枚举），
  移动回执字段无需在 guide 另行记载。
- 四个探针 + reports/ 证据：可复跑的实测基础设施，同时是上述删除条件的度量工具：
  - `probe_tests/probe_workflow_nested_failure_contract.py` — 失败信封结构实测
  - `probe_tests/probe_dsl_error_feedback.py` — 各类坏文档的平台反馈实测
  - `probe_tests/probe_scroll_workflow_generation.py` — 生成能力矩阵（baseline vs augmented）
  - `probe_tests/probe_scroll_workflow_revision.py` — 真实修订循环收敛性

## 4. 已否决 / 未落地项（记录理由，防止重提）

- **harness DSL 语法归一化**（`_platform_step` 把 `condition`→`if`、`loop.steps`→`body`
  等确定性转换）：决定不做。语法错误应由模型读对 guidance 解决，归一化会掩盖
  模型缺陷且随平台 DSL 演进变成维护负担。
- **失败策略决策引导**（"修一轮比重交便宜""DSL 结构错立即降级 browser_call"）与
  **"最小段"倾向句**：曾写入 guide，按"不做模型决策"原则移除。修订/降级/段大小
  由模型自行判断。
- **滚动行为增强规则**（先 Page.getState 门控、单次 ≤400px、回执提取、
  boundary-reached/state-read/零移动即停、不重复固定坐标 wheel）：探针验证有效
  （状态门控 0/6→5/6，移动提取 0/6→5/6），但属任务域决策引导，不落地生产 guide。
  规则文本保留在 `probe_scroll_workflow_generation.py` 的 `AUGMENTED_RULES` 作
  模型能力升级时的复测基准。
- **abcp-platform 侧 nestedActionFailure 透传**：曾实现并通过 tsc 构建验证，
  按决定整体回退（平台仓库保持干净）；底层信息支持由 WebCross 官方实现。

## 5. 复测命令

```bash
# 失败信封结构（需本机 WebCross 运行中）
conda run -n agent python probe_tests/probe_workflow_nested_failure_contract.py --json reports/nested-failure-contract.json

# 坏文档的平台反馈
conda run -n agent python probe_tests/probe_dsl_error_feedback.py

# 生成能力矩阵（删除条件 §1.1/§1.2 的度量）
conda run -n agent python probe_tests/probe_scroll_workflow_generation.py --runs 3 --json reports/scroll-generation-capability.json

# 修订循环收敛性
conda run -n agent python probe_tests/probe_scroll_workflow_revision.py
```
