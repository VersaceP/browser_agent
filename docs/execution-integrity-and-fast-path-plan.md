# 执行完整性与快路径优化方案

状态：阶段 0–10 已完成；旧 guided/ephemeral 快路径已删除；Hybrid Skill 方案已定稿，原生 Workflow 执行暂时关闭
更新时间：2026-07-30
证据来源：`worktree/18754ae96f3a4161946d511f7c5b93ef`（淘宝女装汉服 rank 11-20 详情采集，00:54 → 03:20）
适用范围：LeadAgent、BrowserAgent、fleet barrier、composites、content_completeness、Hybrid Skill 快/慢路径

---

## 2026-07-30 当前导航与策略边界

> 2026-07-31 Runtime/抽取补充合同：`extract_dom_records` 与 `eval_js_json`
> 已删除；隐藏的通用 oracle、title side-channel 与 frozen workflow Runtime
> 执行已禁用。`collect_items` 仅保留三个代码注册的只读模板
> (`items_count`/`rows`/`state`)；调用方只能传 JSON bindings，不能传 JS，
> 且结果直接走 ABCP 返回值，不再修改 `document.title`。
> 普通抽取使用 canonical id + 批量 `DOM.getText`/`DOM.getAttribute`。
> `Runtime.evaluate` 仅允许模型显式发起、当前页面纪元已穷尽实时可用结构化读取、
> `world=auto` 且 `effect=read_only` 的最后手段；auto 先尝试 isolated，仅在平台窄分类允许时回退 main。本文后续保留的旧调用链与历史
> 统计仅用于事故分析，不代表当前可执行合同。
>
> 2026-07-31 验证码/控制连接补充：验证码自动解的视口与点击安全证据改由每轮
> 原生 `DOM.getAXTree` 提供；因当前 AXTree 没有可靠焦点语义，`text_ocr` 不自动
> 输入，直接交给 HITL。Workflow 的 PAGE-level 辅助控制连接不得用主 worker 的
> Agent ID 再次 `System.register`，避免同 Agent 多 socket 抢走通知；在 ABCP 提供
> delegated-control identity 前只使用已验证的跨连接页面调用。

本文保留原事故分析、已实现阶段和验证记录。导航归因、B 路线决策和 Strategy Bank
的后续目标合同由以下方案取代：

- [navigation-attribution-and-content-completeness-plan.md](./navigation-attribution-and-content-completeness-plan.md)
- [strategy-bank-v2-positioning-plan.md](./strategy-bank-v2-positioning-plan.md)
- [fleet-reuse-architecture.md](./fleet-reuse-architecture.md)

当前合同：

1. `registeredAgentId=<harnessInstanceId>:<slotId>:<num>` 是 ABCP 注册与 Browser
   Actor 身份；`workerId` 继续作为非因果 Harness 执行实例身份，用于
   spawn/wait/cancel、barrier resolver、phase、cleanup 与 trace。
2. 当前 `ownerAgentId` 是 resource owner/事件路由键。只在
   `actorAgentId` 直接存在或 capability 明确声明 `owner_is_actor` 时推导
   `effectiveActorAgentId`；resource owner 与 relay delivery 不参与因果判定。
3. 进程内 `FleetClickGate` 串行同 Fleet 的 click-capable command；新 Page 只有在
   `Page.open.executionId == Input.click.executionId` 时才归因给该 click。无执行归因
   的 Page.open 视为站点自主页面，不通过 opener/时间邻近认领；Page.list 仅承担
   同页状态 reconciliation。旧版 popup inventory 归因必须显式开启兼容开关。
4. `ContentCompletenessTracker` 只消费 confirmed 顶层 click receipt；opaque
   Workflow 不生成逐 click 路线归因。
5. Strategy Bank 是 task-type/stage/execution-pattern guidance，不再改写权限、
   contract或站点 marker。
6. delegated Fleet relay 必须按 Fleet 过滤、按 `eventId` 去重并记录非因果
   delivery provenance；事件收件人不能成为 Actor 证明。
7. 2026-07-30 已撤销未形成权威消费端的 Artifact completeness 实验链：
   配置开关、运行 ledger、counterfactual telemetry、attempt receipt 与专属测试均已
   删除。生产 artifact validators、blocker/placeholder/stub 防护与
   `ContentCompletenessTracker` 保留。
8. 导航 attribution shadow/matcher 已由进程内 Enforced Fleet Click Gate 取代；
   不再保留两套导航判定。
9. 全部 worker 日志通过不可变 bound logger context 统一注入
   `workerId/slotId/agentId/phaseId`；Lead/task 事件继续保持 task scope。
10. 原 `guided_fast_path` 与 `ephemeral_workflow` 是针对旧 Workflow 能力边界的两套
    临时执行器，现已删除。`harness/fast_path.py` 只保留 trace 审计与未来的确定性
    分段基础，不是可执行编译器。

本文后文是事故分析和阶段记录。旧 source-attribution lease、Artifact shadow、
Strategy contract fallback 或把 workerId 当 Browser 身份的描述均为历史，不再
作为验收标准。

## 2026-07-30 Hybrid Skill + Native Workflow 权威方案

本节覆盖后文所有关于“不要引入 Workflow segment”“guided composite path”及
“ephemeral workflow 自动接管”的历史结论。后文相关内容只保留事故背景，不再指导实现。

### A. 当前运行状态

`runtime_config.HarnessConfig.workflow_execution_enabled` 是 Harness 所有
`Workflow.execute` 路径的唯一总开关，当前默认值和 `config.json` 均为 `false`。

关闭时：

- BrowserAgent 不披露 `Workflow.execute`、`execute_browser_workflow` 或
  `execute_selected_skill`；
- 模型即使构造直接调用也会在 RPC 前得到
  `workflow_runtime_disabled`，不会访问 ABCP；
- frozen skill fast path、Workflow auto-heal 与 hybrid plan 的 native segment
  均不执行；
- `/skill-create` 的 live trial 与 `--recheck` 同样受此开关管理：关闭时在连接面板、
  创建 Fleet/Page 或发送 Workflow RPC 之前返回
  `inconclusive/workflow_runtime_disabled`。静态 skill 生成仍可进行，但不得标记为
  live-tested；
- skill 的 `SKILL.md` 仍按渐进式披露进入被选中 worker 的上下文，workflow-backed
  skill 的有效类型为 `guidance_runtime_disabled`；
- Harness composites（`collect_items`、`dismiss_overlay`、
  `fill_field_verified` 等）继续作为 BrowserAgent 慢路径工具使用；
- 独立 ABCP 诊断脚本不经过 Harness 总开关，仍可用于协议能力验证，但不得据此宣称
  生产快路径已启用。

在 ABCP 原生 Workflow 补齐本文 C 节能力并通过 parity canary 前，不建议开启该开关。

### B. Skill 由“二选一类型”改为分层的混合编排载体

一个完整 skill 可以同时包含：

1. `SKILL.md`：始终可披露的站点/任务 guidance，包含前置条件、探针、负面知识、
   失败分类和降级策略；
2. `workflow.json`：仅包含 ABCP 原生、可移植、可由面板或 Harness 直接执行的
   native workflow；
3. `orchestration.json`：Harness hybrid plan，按顺序引用 native segment 与
   Harness composite host step；
4. fallback contract：每个 segment 的 effect class、成功/失败证据、恢复点及
   BrowserAgent 慢路径接管说明。

未来启用后的选择顺序固定为：

```text
完整 native workflow 可用且能力/版本匹配
  → 执行 native workflow
否则 validated hybrid plan 可用
  → 执行最大 native segments；在 composite 边界回到 Harness
否则
  → 只披露 SKILL.md，BrowserAgent 慢路径执行
```

这里的“hybrid”不是另造一个浏览器执行引擎。Harness 只按确定性边界把已验证 trace
切成**最大连续 ABCP-native 段**；`collect_items`、`dismiss_overlay`、
`fill_field_verified` 等 composite 是 host step 边界。不得再用 LLM 或第二套编译器
猜测等价动作，也不得把 composite 隐式改写成一组未经 parity 验证的原子调用。

### C. ABCP 原生 Workflow 的能力缺口与启用条件

当前 Workflow 不能可靠表达“先订阅、后动作、再等待”的因果事务：

- `listen Page.open` 是阻塞步骤，放在 `Input.click` 前会阻止 click 执行；
- 放在 click 后会漏掉已经发生的事件；
- `listen.mode="nonblocking"` 当前返回参数错误；
- filter 不能把 `$vars.sourcePageId` 等运行时变量稳定解析为动态匹配值；
- Workflow 标量变量无法维护动态 target 数组，也没有 append/distinctBy/count
  这类集合状态。
- Workflow 引擎级异常（包括顶层 timeout/terminate/throw）会以空 `results`
  完成失败收尾，覆盖此前已完成步骤的结果；随后 `Workflow.getStatus` 也无法恢复这批
  partial results。未来 native segment 必须保持有界，并在 segment 边界由 Harness
  持久化和对账，不能把长段 Workflow 的异常恢复建立在状态查询能够找回中间结果的
  假设上。

ABCP 至少需要提供 `action.expectEvent`，或等价的
`listen arm → action → await` 原语，并支持动态 filter、事件结果变量绑定及明确的
ambiguous/timeout 终止语义。多 popup 时不得任选页面；应返回
`ambiguous_popup` 并停在可恢复边界。

若要用原生能力取代 `collect_items`，还需一个可移植的 `Collection.collect`（名称可
不同），至少支持：

- 运行时查询重复记录，不要求调用方预先写死随虚拟列表变化的 DOM target ids；
- JSON 数组累积、stable-key `distinctBy`、计数和有界窗口；
- 一个明确容器内的 scroll 或一个 load-more 控件；
- `target_reached`、`explicitly_exhausted`、`materialization_stalled`、
  `blocked` 四类结构化终止状态；
- 容器几何、load-more 消失、truncation 等穷尽证据；
- 大结果文件化/offload，且与 Harness artifact/ContentCompletenessTracker 的结果
  能做 parity 对照。

“点击下一页”属于分页状态机，不是现有 `collect_items` 的一个参数。原生
`Collection.collect` 若要覆盖分页，必须额外声明页身份、下一页动作、重复页检测、
跨页 stable-key 去重、终页证据及副作用恢复；不能把单容器滚动的穷尽结论外推到分页。

### D. `collect_items` 的明确适用边界

`collect_items` 是通用但有限的 collection preset，不是淘宝专用工具，也不是任意网页
循环器。它只完整适配：

- **单层同构集合**；
- **单一滚动容器或单一 load-more 控件**（二选一）；
- **stable-key 跨轮去重**。

未知站点必须先用 DOM/AXTree/SemanticTree 探针找到 repeated item selector 与真正的
滚动容器/load-more 控件，再调用 composite。以下场景不在其完整覆盖范围：

- 嵌套列表或每行还需展开子列表；
- 多滚动层或容器动态切换；
- 下一页/页码分页；
- 每行依赖前一行结果的状态机；
- filter/search/sort 会改变目标集合身份的循环。

这些场景必须拆解后走 BrowserAgent 慢路径；不能因为 `collect_items` 返回了部分行就
把不受支持的结构认证为穷尽。

### E. probe → validation → bulk 是条件式信心升级，不是固定阶段模板

Lead 只在证据满足时升级：

```text
路径未知且存在多行 → probe（1 行自由探索）
probe 产出 reusable candidate → validation（最多 2 行验证路径非偶然）
validation 通过 → bulk（对剩余行复用 validated plan）
probe/validation 未产出 reusable candidate → continuation（剩余行 BrowserAgent 慢路径）
continuation 新证明 reusable candidate → validation（重新验证后才可进 bulk）
bulk 已 validated_done 但不再证明 candidate → continuation
无剩余行 → 不创建下一角色
```

Lead 不得仅因行数大于一就在初始 plan 预建 validation/bulk；后续角色由
`replanCheckpoint.requiredNextRole` 决定。bulk 也不再承诺旧 ephemeral 编译器的
“零 LLM”：

- native Workflow 可用时执行完整 native workflow；
- 否则执行 validated hybrid plan 的 native segments 与 Harness composite host steps；
- 当前总开关关闭时，hybrid plan 只作为 guidance，剩余行由 BrowserAgent 慢路径执行；
- 任一行出现 selector/route/auth/content drift，仅该行降级，不污染已验证行。

这里的 bulk 降级只覆盖“本轮所选行全部通过 artifact validation，但 trace 已不能继续证明
candidate”的 `validated_done` 情形。部分失败的 bulk 不会写 successor checkpoint；其成功行
进度与失败行重记账仍是独立欠账，本阶段不宣称解决。

### F. 副作用、重放与降级

每个 hybrid step 必须声明 effect class：

| effect class | 例子 | 重放原则 |
|---|---|---|
| `read_only` | `DOM.getText`、`DOM.getAttribute` | 可重放 |
| `reconcilable` | `Input.scroll` | 先读取当前位置/集合状态，再决定是否补做 |
| `side_effecting` | `Input.click`、`Input.type` | 先用 `Page.list/Page.getState` 或字段当前值对账；已生效则跳过 |
| `irreversible` | `Page.close`、提交/购买等 | 不得盲重放；只有机械证明目标状态未达成且操作仍安全时才继续 |

Fleet Click Gate 解决跨 worker 并发与 popup 持久化归因，不等价于 Workflow 内部逐动作
receipt。opaque Workflow 仍须依赖引擎提供的 action/event 因果结果；不得用 workflow
成功返回推导某个内部 click 已打开哪一页。

### G. 删除与保留边界

已删除：

- `harness/guided_fast_path.py` 及 `_guided_fast_path_receipt` 消费链；
- `harness/skill/ephemeral.py` 及 record-extraction 后自动编译/批量接管链；
- `guided_fast_path_enabled`、`ephemeral_workflow_enabled`；
- 对应的 pending-row 终态门和 live canary 分支。

保留：

- `harness/fast_path.py` 的稳定 trace 参数清洗和非执行候选审计，未来只作为
  deterministic segmenter 的基础；
- frozen workflow skill 文件与运行器代码，受 `workflow_execution_enabled` 总开关
  保护，等待 ABCP 能力补齐；
- production composites、artifact validators、ContentCompletenessTracker、
  auth-generation fence 与 Fleet Click Gate。

## 0. 一句话结论

本次任务失败**不是**因为缺少新的执行层，而是因为三件事：

1. **验收链会自我放水**——Lead 用自然语言 replan 指示 worker 把错误说明写进数据字段，占位符通过了 `field_nonempty` 校验，拿到 `validated_done`。
2. **共享 fleet 的闸按错了地方**——对危害最大的在途写操作完全放行，对危害为零的空白建页拦死并导致死锁。
3. **已有的快路径装置几乎全都没通电**——`collect_items` 全程用了 1 次且 60 秒超时退出，`ephemeral_workflow_enabled` 默认 `False`，`batch_rows` 一次没挂过。

因此本方案的基调是**接通与修正已有装置**，而不是新增抽象层。

---

## 1. 核心架构原则（本轮讨论的最重要产出）

> **历史说明：本节形成于 Hybrid Skill 方案之前。** “不引入 Workflow segment”
> 已由上文权威方案取代；仍有效的原则仅是“不为形式统一重复造执行器”和“composite
> 不得被未经验证地改写为原子调用”。

> **当 harness 的复合工具已经能无模型执行该循环时，不要仅为形式统一再包一层 Workflow。**
> **只有引擎侧原子性、事件监听（`listen`）或跨调用状态机确有收益时，才使用 `Workflow.execute`。**

（先前表述为"`Workflow.execute` 的唯一价值是节省 LLM 轮次"——**过强**。它还提供引擎侧事件监听、分支重试与更少的 IPC 往返；其中 `listen Page.loaded` / `listen Hitl.resumed` 这类**引擎内等待**是 harness 循环无法廉价复制的。但对"滚动—收割—去重"这类循环，这些优势都不适用，因此下面的推论不变。）

推论：

| 场景 | 正确手段 | 理由 |
|---|---|---|
| LLM 在环，要压缩多轮交互 | `Workflow.execute`（frozen skill 快路径） | 一次调用替掉整个 worker，收益最大 |
| LLM 不在环，harness 自己驱动 | **普通 browser call 顺序调用 / composites** | 同样零 LLM，且每步之间可查 barrier、可判进度、无黑盒、无编译失败风险 |
| LLM 在环但只是读多个目标 | `DOM.getText` 的 `targets` 批量 / 同轮多 call | 已支持，无需任何新机制 |

旧结论曾据此拒绝 Workflow segment。现方案改为 Hybrid Skill：只做确定性的最大
native segment 切分，composite 仍由 Harness 执行；详见权威方案 B/G。

`Workflow.execute` 在长调用期间还会独占连接（`abcp_client.py` 单 `_call_lock` + 单 `_pending_call`），把它塞进 per-row 循环是净损失。

---

## 2. 事故证据清单

全部来自本次 run，可复现。

| # | 现象 | 证据 | 定性 |
|---|---|---|---|
| A | 占位符文本作为数据落盘并通过验收 | `artifacts/extractions/taobao_hanfu_details-72f9b5eb.json` 中 `reviews: ["Login wall blocked individual reviews - ..."]`，phase recB `validatedStatus: validated_done` | **数据可信度破产** |
| B | Lead replan 指示违抗平台 SOP | `task_plan.json` recA/recB `worker_task`：`"do NOT call Hitl.requestPause"`、`"set reviews to ... a short note"` | **规避运行时策略** |
| C | 契约自相矛盾未被拦截 | 同一字段 `reviews`：`expected_artifact` 标 `allow_empty: true`，`validators` 又要求 `field_nonempty` | 机械可检，无人检 |
| ~~D~~ | ~~目标静默降级~~ | **本条已撤回**，见 §2.1 | 原判断有误 |
| E | 无主 barrier 死锁 | browser-013 HITL 1200s 超时（02:33:54→02:53:54）→ resolver 置空但闸不开；browser-014/015 各撞 17/23 次 | **ranks 18/19 直接丢失** |
| F | 在途 workflow 不受 barrier 约束 | barrier 检查在 `browser_tools/__init__.py:2012`（harness 侧调用前），workflow 内部 step 不回 harness | 危害面无防护 |
| G | frozen skill 完全绕过 barrier | `skill/workflow.py:124` 直接 `browser.call("Workflow.execute")`，该文件 `grep barrier` 零命中 | 漏洞 |
| H | 整页 base64 截图打爆 WebSocket | `Page.screenshot fullPage:true` → 17,480,305 B > 16 MiB → 1009 拆连接；browser-002 此后全部调用报 `WebSocket background reader failed` | **随机报废整个 worker** |
| I | completeness 把 tab 标签当内容 | 38 条 `content_completeness.decision` 全为 `complete`；`_evaluate` 纯 substring 匹配，"评价" 出现即判 `reviews` 已观察 | 恢复路径永不触发 |
| J | 路径二从未被升级触发 | 38 条 decision 均 `recoveryAttempts: 0`。**这不是"该点卡片却没点"，而是 I 的后果**——完整性判 `complete`，升级条件永不成立。见 §2.2 | I 的下游症状 |
| K | 快路径装置未通电 | `ephemeral_workflow_enabled = False`（默认）；`batch_rows` 全程未挂；`collect_items` 仅 1 次 | 成本结构失控 |
| L | `collect_items` 60s 硬超时 | 唯一一次调用 `stopReason: time_budget`、`roundsUsed: 1`（`maxRounds: 15`）、2 行 vs `targetCount: 20`；`COLLECT_ITEMS_MAX_DURATION_MS = 60000` 硬编码 | 装置有但跑不完 |
| M | 相同 phase 被拆成 13 个 | collect 产出 10 行，detail 拆成 13 个 phase，12 个 rowCount ≤ 1，各自全量自由探索 ~30 步 | 计划层杜绝批量 |
| N | 事件丢失 worker 归属 | `agent.model` payload keys 仅 `['step','stop_reason','text','tool_calls']`；`main.py:52` 打印 `[BrowserAgent] 第 N 步` 无 workerId | 并发下串线，误导归因 |
| ~~O~~ | ~~Lead 首步 max_tokens 截断~~ | step 1 output = 12000 = 上限。**用户自行调高 `max_tokens`，本方案不处理** | 已移出范围 |

**全任务 max_tokens 事故 3 次**（lead step1、lead step15、browser-001 step29），非 5 次——先前口径有误，已更正。

---

### 2.1 撤回：证据 D 不成立

原先把 "plan v1 每商品 20 条评论 → v3 best-effort" 列为静默降级，这是错的。

**并非每个商品都存在 20 条评论**；数量不足时不应因此判定验收失败。真正的问题从来不是"放宽了数量"，而是证据 A/C——把 blocker 文本写进数据字段，以及 `allow_empty` 与 `field_nonempty` 的契约矛盾。

因此正确的完成判据是三分，而不是"必须够 20 条"：

| 情形 | 判定 |
|---|---|
| `actual < target` 且 `end_of_collection = true`（真的只有 8 条，已滚到底） | **合法完成** |
| `actual < target` 且 `end_of_collection = false`（没滚完 / 被挡 / 只有预览） | **未完成**，继续 materialize 或报 blocker |
| 记录内容是 blocker 文本 / 占位符 | **数据污染，永远非法**，与数量无关 |

strategy bank 里已有对应表述 `allow_fewer_only_with_exhaustion_evidence`，实现时直接沿用。

**结论**：`min_records` 不是硬性下限，而是"低于它必须出示穷尽证据"的触发线。

---

### 2.2 重述：路径不是"必须点卡片"，而是两条路径 + 升级触发器

原先把"全部走 `Page.navigate`"列为独立缺陷，这也是错的。直接导航是**默认且更省的路径**，列表点击是**风控场景下的恢复路径**。

Agent 必须明确持有两条路径：

**路径 1（默认）**：从列表页抓取 URL，`Page.navigate` 直接打开。成本低，绝大多数站点足够。

**路径 2（恢复）**：不抓 URL，保留列表页，点击卡片进入新标签或同页跳转。仅用于**路径 1 已证实拿不到完整 DOM** 的场景——即路由敏感型内容抑制。

是否进入 listing-link 恢复路线由 LLM 结合确定性 completeness 与 structured
`routeRecovery` receipt 决定：

```
路径 1 完成导航
  → completeness 判定
      content_materialized  → 完成，不升级
      shell_seen（外壳在、目标区域未材料化，且已尽 reveal/scroll 预算）
                            → Harness返回routeRecovery guidance
                                → LLM选择真实anchor click / 继续有界感知 /
                                  final_answer incomplete
      absent                → target_absent
```

本次之所以一次都没升级，正是因为 completeness 恒判 `complete`（证据 I）。**修好 I，J 自动消失**——不需要为路径 2 单独设计触发机制。

路径 2 用尽 `max_attempts_per_item` 后仍不完整 → `blocked_content_suppression`，不得退回路径 1 反复重试。

---

## 3. 已有但未通电的装置盘点

在设计任何新东西之前，必须先承认这些已经存在。

### 3.1 `collect_items`（`composites/collect_items.py`）

**作用**：解决"目标记录不是一次性存在于 DOM，而是随滚动/点击加载逐步 materialize"这一类问题。源码 docstring 写得很准：

> materialize → harvest → dedup 循环，**内部完成，不消耗模型步数**。每轮通过只读 oracle 收割行并按稳定键去重；某轮停滞时探测 overlay。

具体能力：
- `maxRounds` / `stabilityThreshold` / `targetCount` 三重停止条件
- 按 `keyField` 去重，因此**虚拟列表**（行被回收出 DOM）也能收全
- 每轮 harvest 开窗遍历完整匹配集，不会反复读头部
- 进入前强制 `DOM.getAXTree` 建立 layout viewport（fresh tab 的 quirk）
- 停滞时自动探测并处理 overlay
- 结束自动 `record_extraction` 落盘
- 检测到 challenge-pause 立即中断返回

**这就是所谓 "Micro-Workflow" 想要的东西，早已实现。**

问题在：
- `COLLECT_ITEMS_MAX_DURATION_MS = 60000` 硬编码且不可配置，淘宝一轮就打满（证据 L）
- 输出行形状（`{reviewText}`）与 phase 契约（`rank/productTitle/...`）不匹配 → `recordExtraction.status: needs_fix`
- **模型基本不知道该用它——原因见 §3.6，不是 schema 问题**

### 3.2 `_maybe_run_ephemeral_batch_after_first_row`（`browser_tools/__init__.py:1105`）

row0 正常跑 → 编译 trace → canary row1 → 剩余零-LLM。

**⚠️ 但它对本次这类任务是不可达的——这是本方案先前最严重的错误假设，已更正。**

`compile_ephemeral_workflow`（`skill/ephemeral.py:52`）**第一步就扫描整条 trace**：

```python
_DISALLOWED_TRACE_TYPES = frozenset({
    "visual_verify", "hitl_wait", "eval_js_json", "collect_items",
    "fill_field_verified", "dismiss_overlay", "local_fs_read", "local_fs_search",
})
blocked = {item["type"] for item in trace if item["type"] in _DISALLOWED_TRACE_TYPES}
if blocked:
    return None, {"reason": "disallowed_trace_tools", "tools": blocked}
```

后果分两层：

**① 与 P1-3 直接冲突。** 本方案一边要求 row0 用 `collect_items` 完成 materialization，一边指望 row0 的 trace 编译成 workflow 跑 rows 1–9。`collect_items` 就在禁止名单里——**这两件事互斥**。

**② 比冲突更广：本次 run 里没有任何一条 trace 有可能编译成功。** `local_fs_read` / `local_fs_search` 同样在禁止名单，而本次 9 个 worker **全部**用了它们（合计 161 次）。也就是说：即使打开 `ephemeral_workflow_enabled`、即使 Lead 正确挂了 `batch_rows`、即使 `_navigation_variable` 支持 anchor click，编译仍会在第一步就返回 `disallowed_trace_tools`。

**因此"打开 flag + 改 Lead 规划 = rows 1–9 自动零 LLM"这个结论是错的，且不是配置或提示词能修复的。** 参见 P1-4 改写后的两级快路径。

其余已知限制（仍然成立，但排在上述问题之后）：
- `ephemeral_workflow_enabled` 默认 `False`
- 需要 `batch_rows >= 3`，而 Lead 从未挂过
- 编译器 `_navigation_variable`（`skill/ephemeral.py:252`）只认 `Page.navigate`

### 3.3 `semantic_index` 与 `collect_items` 的收割机制（**先前描述有误，已更正**）

先前版本写道"计数走 `semantic_index.digest_subtree`，不走 `extract_dom_records`"。**这不符合当前实现。**

`collect_items` 的实际收割链路是：

```
collect_items → collect_rows → build_collection_oracle
             → 注册模板 ID + JSON bindings → isolated Runtime.evaluate 原生返回值
             → querySelectorAll(selector) → rows[] + __key
```

见 `composites/collect_items.py` 的 `harvest()` 与
`observation/verifiers.py` 的 `build_collection_oracle`。通用
`build_read_only_oracle` 继续 fail-closed；固定模板执行器不使用 title/window
side-channel。

`semantic_index` 的 `digest_subtree` / `discover_selector_candidates` **只产出**子树摘要、重复结构候选、selector 候选与样本文本——**不输出完整记录集，也没有跨轮唯一键累积**。它是"发现并验证目标区域和 selector"的工具，不是收割器。

**因此"用 `semantic_index` 替代 JS 收割"不是接通已有装置，而是实质开发**（需新增：子树绑定、记录解析、字段映射、跨轮唯一键、完整记录列表、虚拟列表累计）。

**采纳的口径**：承认 `collect_items` 使用**受控内部只读 oracle**。关键区分不是"用不用 JS"，而是：

| | 模型直发 `Runtime.evaluate` | harness 内部只读 oracle |
|---|---|---|
| 表达式来源 | 模型自由撰写 | harness 固定模板 |
| 审计 | `runtime_policy` 五字段门控 | 注册模板 ID、binding 名单、表达式 hash；不记录数据值 |
| 副作用 | 可能有 | 只读 |
| Strategy `discouraged_tools` 建议适用 | **是** | **否** |

Strategy Bank 现在只提供 guidance。`discouraged_tools: Runtime.evaluate`
提醒模型优先使用原生 DOM 工具，但不修改权限；真正禁用由 runtime policy 或显式
worker contract 决定。Harness 内部固定只读 oracle 不受这条建议影响。

`extract_dom_records` 是否允许同样由 runtime policy/显式 contract 决定，不能从
Strategy 字段机械推导。

### 3.4 `FleetAuthBarrier`（`fleet_runtime.py`）

`generation()` / `claim()` / `before_call()` / `relinquish()` / `abandon_worker()` 齐全，`auth_generation` 语义现成，无需新建。

### 3.5 `content_completeness`（`content_completeness.py`）

区域声明、marker 匹配、`listing_link_click` recovery 模式、决策落盘全部就位。缺的只是判定强度（§P1-1）。

---

### 3.6 为什么模型不用这些工具——**不是 schema 没注入**

已排除 schema 注入问题。从本次 run 的 `contexts/abcp-agent-slot-002-final-context.json` 直接读出模型实际收到的工具表：**13 个工具，name / description / input_schema 全部完整送达**，`collect_items` 在列。

（15 个注册工具 − `eval_js_json`（永久别名，故意不暴露）− `fill_field_verified`（`web_scrape` 按 task_type 隐藏）= 13，符合预期。）

真正的原因是**系统提示词的决策阶梯里没有它们**。统计 27,086 字符的 worker system prompt：

| 工具 | prompt 提及次数 | 本次实际调用次数 |
|---|---|---|
| `local_fs_search` / `local_fs_read` | 4 | **161** |
| `visual_verify` | 4 | 7 |
| `find_in_axtree` | 0 | 27（例外，见下） |
| `navigate_verified` | 0 | 4 |
| `extract_dom_records` | 2 | 0（当时未被模型选择；Strategy 现不再修改权限） |
| `execute_browser_workflow` | 1 | 1（schema 形状不明被拒） |
| **`collect_items`** | **0** | **1** |
| `dismiss_overlay` | 0 | 0 |

**相关性很强：进了 prompt 阶梯的被反复使用，只有工具描述的基本被忽略。**
（`find_in_axtree` 是例外——名字自解释且直接对应高频需求"在 AXTree 里找东西"，模型能自行联想。这说明"名字能否自解释"是第二因素，但不能依赖。）

**更糟的是：prompt 主动教模型手搓 `collect_items` 的循环。**

L5 段落原文：

> "A section heading, drawer shell, loading skeleton, or preview rows do not satisfy an explicit repeated-record target such as **20 comments**. **Count records inside the target drawer/subtree, refresh DOM.getSemanticTree after each bounded container scroll**, and stop only at the requested count or a real terminal condition."

L3 段落原文：

> "For a **lazy drawer/list**, retry its **reveal/materialization** if necessary and use DOM.getSemanticTree plus the requested record count."

这两段描述的**正是 `collect_items` 的职责**，却通篇不提这个工具名，反而把循环拆成人工步骤讲给模型听。模型照做，于是产生了 72 次 `local_fs_search` + 89 次 `local_fs_read` 的手工循环。

而 L4 的抽取阶梯是：

> "Extraction priority is: native batched DOM.getText/DOM.getAttribute → single-target DOM reads → `extract_dom_records` for uniform lists/cards/tables → gated Runtime.evaluate"

**这条阶梯里没有"渐进式 materialize"这一档。** 模型遇到"20 条懒加载评论"时，阶梯上三个选项都不解决问题，只能即兴发挥。

### 3.7 全工具三维审计

对 15 个注册工具逐一核对"是否送达 / 是否进决策规则 / 输出是否检契约"：

| 工具 | 送达模型 | prompt 决策规则 | 策略 `preferred_tools` | 输出契约检查 |
|---|---|---|---|---|
| `browser_call` | 是 | ✅ | ✅ | — |
| `record_extraction` | 是 | ✅ | ✅ 5/8 策略 | — |
| `local_fs_search` / `local_fs_read` | 是 | ✅ 有规则 | — | — |
| `visual_verify` | 是 | ✅ 有规则 | cautioned×3 | — |
| `extract_dom_records` | 是 | ✅ 在阶梯里 | avoid×1 / cautioned×2 | ❌ **无** |
| `find_in_axtree` | 是 | ❌ 0 | ❌ 0 | — |
| `navigate_verified` | 是 | ❌ 0 | ❌ 0 | — |
| **`collect_items`** | 是 | ❌ 0 | ❌ 0 | ✅ 有（但事后） |
| **`dismiss_overlay`** | 是 | ❌ 0 | ❌ 0 | — |
| `execute_browser_workflow` | 是 | 1 次（无 schema 形状） | ❌ 0 | — |
| `eval_js_json` | 未送达（故意别名） | — | — | — |
| `fill_field_verified` | 未送达（task_type 隐藏） | — | — | — |

**8 个策略的 `preferred_tools` 里，出现过的 harness 复合工具只有 `record_extraction` 一个。**

**`dismiss_overlay` 的情况比 `collect_items` 更严重。** `browser_action.overlay.dismiss_ladder` 策略的 `procedure` 是这样写的：

> 刷新 AXTree 找遮挡层 → 有 close/X 控件就点 → 没有就 `Input.press` Escape → Escape 失败就**用 gated `Runtime.evaluate` 查 `document.elementFromPoint`** 确认外部点落在 backdrop → 验证过才允许 `Input.click` x/y → 每次尝试后用 AXTree 验证消失

而 `dismiss_overlay` 的工具描述：

> "Runs the dismiss ladder internally (find close control → click → verify → Escape → verify → **verified backdrop click** → verify)"

**策略把工具的内部实现逐步抄成了人工流程，`preferred_tools` 里却没有这个工具**，还额外指定用 `Runtime.evaluate` 做几何判定——正是 `runtime_policy` 会拦的东西。

即：**两个独立来源在教手搓**——system prompt（`collect_items`）与 strategy bank（`dismiss_overlay`）。

**契约检查全是事后的。** `collect_items` 拿模型给的 `fields: {reviewText: "text"}` 跑完整个循环，跑完才发现字段名对不上契约要的 `rank/productTitle/...`。本次因 60s 超时只跑 1 轮，浪费不大；**预算修好后跑满 15 轮，就是整轮白跑再报错**。

---

**结论：工具未被使用有四类成因，修法不同——**

| 成因 | 涉及工具 | 修法 |
|---|---|---|
| ① prompt 决策规则缺档位 | `collect_items` | 补 materialize 档位 + **删掉教手搓的段落** |
| ② 策略 procedure 抄写工具内部实现 | `dismiss_overlay` | `dismiss_ladder` 改为 `preferred_tools: [dismiss_overlay]`，procedure 只保留验收语义、删执行步骤（顺带减少 `runtime_policy_rejected`） |
| ③ 契约检查事后而非预检，**且不支持嵌套集合** | `collect_items`、`extract_dom_records` | 见下方「嵌套集合输出合同」；`extract_dom_records` 补 `contractWarning` |
| ④ 靠名字自解释侥幸被用 | `find_in_axtree`、`navigate_verified` | 低优先级，但也应进阶梯——不能依赖模型联想 |
| ⑤ schema 形状不明 | `execute_browser_workflow` | 补 `steps` 完整 schema（最低优先级） |

不要把这几类混为一谈——尤其不要因为 ⑤ 存在就以为 ① 也是 schema 问题。**③ 必须先于"修 `collect_items` 时间预算"落地**，否则下次就是跑满 15 轮再告诉你字段错了。

---

## 4. 方案

### P0-1 阻断验收放水

**代码层最小不变量（不可绕过，静态可判）：**

1. 同一字段同时出现在 `allow_empty` 与 `field_nonempty` → 拒绝计划（证据 C）
2. `worker_task` 出现对运行时 SOP 的字面否定（`do NOT call Hitl.requestPause` 一类）→ 拒绝（证据 B）
3. 指示把 blocker/错误说明写入**业务数据字段**（而非 blocker 字段）→ 拒绝（证据 A）
4. 落盘行的业务字段值命中 blocker 语义模式 → 该行不得计入 `validated_done`

这四条不是"堆门禁"，是一个很小的类型系统。它们**成本极低且确定性**，不应交给模型判。

**阶段 2 实施结果（2026-07-20）**：四条机械不变量已接入。计划入口会拒绝
`allow_empty`/`field_nonempty` 冲突、业务 phase 否定 HITL SOP，以及把
blocker/占位说明赋给契约声明的业务字段。共享 `blocker-as-data` 检测同时运行在
`record_extraction` 落盘前和历史 artifact 验收路径。精确 blocker 状态 token 在业务
标量或数组任一元素中均无条件非法；叙述性数组仅在“所有有效元素均命中 harness
blocker 模板 + **同一数组内**没有其他有效承载元素 + 缺字段级 provenance”时拒绝；
这里的作用域严格是当前字段数组，同一行中真实的标题、价格、URL 或其 provenance
不能替污染字段免责。空字符串、null 和空容器不能充当绕过内容。这避免扫描真实评论
中的普通“登录/验证码”用语。

有意保留三个边界：① 自由文本**标量**中的散文 blocker 不做启发式模板扫描，以免
误杀真实描述；② `status` / `reason` / `error` 等显式控制字段名暂视为控制面字段；
③ 同一数组同时含 blocker 散文和真实业务内容时保守放行，因为“任一模板命中即拒”
对真实评论的误杀风险过高。若未来确有同名业务字段，应通过契约角色声明替代继续扩充
核心层字段词表。真实任务 `18754ae9...` 的三份污染 artifact、五条污染行与最终计划
回放均被拒绝。

**PlanValidatorAgent（语义层）：**

处理静态规则覆盖不到的问题——导航策略被无证据更换、"换个 phase id 重试"伪装成"策略调整"、同 cohort phase 被无理由碎片化。

**注意**：数量放宽（20 → best-effort）**本身不是违规**（见 §2.1）。validator 要判的不是"是否放宽"，而是"放宽时是否出示了穷尽证据"。把数量变化一律当降级会产生大量误报。

**模型选型（已定）**：使用**与 Lead 不同的模型**，避免共模失效——同模型的 validator 会和 Lead 犯同一种错，等于没审。在 `config.json` 新增独立配置块（`model_id` / `base_url` / `api_key` / `max_tokens`），与 `vl` 块同级同风格：

```json
"plan_validator": {
  "enabled": true,
  "model_id": "...",
  "base_url": "...",
  "api_key": "...",
  "max_tokens": 8000
}
```

未配置时 `enabled` 视为 false，四条机械不变量仍然生效（fail-closed 不受影响：validator 缺席时保留原契约行为不变，只是失去语义层审计）。

三个不可妥协的设计条件：

- **基准有严格优先级，不是"原始目标 + plan v1"并列**（先前表述不准，plan v1 本身也可能误读用户）：

  ```
  原始用户任务
      > 不可变任务合同
      > plan v1
      > 上一版计划
  ```

  逐级向下，高优先级冲突时以高者为准。绝不能只比"上一版 vs 本版"——否则每次只降一点点，每步都"相对合理"。
  当前 CLI 只在运行启动时接收一次不可变的原始用户任务，尚无可信的运行中用户目标
  修订通道；因此不得把 Lead 写入的 `replan_reason`、worker 总结或普通对话文本冒充
  "用户显式修改"。未来只有控制面提供带来源和 generation 的用户修订账本后，才能把
  该来源加入上述优先级。
- **产出结构化 verdict，逐条比对原始目标条目**，削弱项必须填写证据；填不出证据的削弱一律驳回。不接受自由文本"看起来合理"。
- **fail-closed。** validator 超时/异常时默认保留原契约，不得默认放行——否则静默退化成橡皮图章，比没有更危险。

**依赖**：plan 版本化（保留每版 + replan reason + diff）是 validator 的基准来源，与本项合并实施。

### P0-2 barrier 按语义边界授权

当前 barrier 拦的是"调用从哪里发出"这个**实现边界**，应改为拦"是否写共享会话"这个**语义边界**。

具体：

1. **无主关闭态：先原子认领 resolver，再放行建页**（先前写"对所有 worker 放行 `Page.create`"，已更正）。

   对所有 worker 放行会让多个 worker 各自先建一张空白页再去竞争 claim。正确顺序：

   ```
   coordinator 按 fleetId 原子选定唯一 resolver（复用现有 FleetAuthBarrier.claim 的无主 takeover）
     → 仅该 resolver 获得 Page.list / Page.create / Page.navigate 权限
     → 找到或创建挑战页
     → Hitl.requestPause
     → 其余 worker 保持 gated，等 generation 推进
   ```

   不需要新增 ABCP 能力，只需在 spawner/coordinator 侧调用已有的 barrier claim。解证据 E 的死锁环。

2. **frozen skill 走同一个 barrier**——`skill/workflow.py:124` 直接 `browser.call("Workflow.execute")`，补检查（证据 G）。

3. **在途 workflow 用 generation fencing，不用 HITL 暂停**（先前写"接上 `skill/control.py` 即可"，已更正）。

   `skill/control.py` 的页面级控制有前提：workflow 自己撞上 challenge、且被写成在挑战边界 `listen Hitl.resumed`、由控制连接解决该页挑战。**它不是任意 workflow 的通用 suspend**——普通 workflow 未必有该 listen；对一个**正常页面**发 `Hitl.requestPause` 会凭空制造一次人工暂停；同 fleet 多个 workflow 会产生多个 pause surface。

   稳妥的初版：

   ```
   ① 所有 Workflow 启动前过 barrier（含 frozen skill）
   ② 每个 workflow row 记录执行前 auth_generation
   ③ barrier 上锁后，不启动下一 row
   ④ 在途 row 允许自然返回；返回时 generation 已变 → 结果进隔离区，不落盘
   ⑤ HITL 完成后重新感知（Page.getState + DOM.getAXTree），重跑当前 row
   ⑥ 仅当 workflow 自己撞上 challenge 时，才走现有 page-level HITL/handoff
   ```

   这同样实现"暂停不降级"，但不对正常页面滥用 HITL。
4. **读/写一刀切固定名单（已定）**：不按 task_type 细化，用一张固定表。

   **闸起时放行（只读，不触共享会话状态）：**
   ```
   Page.getState   Page.list      DOM.getAXTree   DOM.getText
   DOM.getAttribute  DOM.getSemanticTree  Page.screenshot
   Hitl.requestPause（走原子 claim，是恢复入口）
   Page.create（仅无主态；新页仍继承闸，只为拿到 pageId 去 claim）
   ```

   **闸起时拦截（写 / 导航 / 会话）：**
   ```
   Page.navigate  Page.reload  Page.go  Page.switchTo  Page.close
   Input.*        Network.*    Memory.*   Workflow.execute
   其余一切未列出的方法（fail-closed 默认拦截）
   ```

   新增方法默认落入拦截侧，需显式加入放行名单——避免能力扩展时静默放行。
5. **`auth_generation` 前后比对**：执行前后 generation 不一致的行进隔离区，不直接落盘。用现成的 `barrier.generation()`，成本接近零。

**原则：暂停不降级。** CAPTCHA / HITL / generation 变化不说明路径失效，恢复后重跑当前行，已完成行保留。

### P0-3 修掉打爆连接的截图

`browser_tools/__init__.py:4327` 的 reality-check 强制 `fullPage: True` + base64 回传。改为只落盘取 `savedPath`（ABCP 本来就返回它），或按视口分段。

**这条独立于其他所有改动**，且不修的话任何验证都可能被随机打断（证据 H）。

**阶段 1 live 验收（2026-07-20）**：新建单 fleet，在淘宝搜索页完成 HITL
登录/滑块后，通过搜索结果卡片点击进入商品详情页；三次物理滚动后页面
`scrollHeight=25,293px`、68/68 张图片完成加载。`Page.screenshot` 使用
`fullPage:true + options.format:file` 成功返回 `savedPath`，生成的 PNG 为
`2418×50,586`、`40,501,427 bytes`（executionId
`1b23d24b-6e14-4bc8-a7f8-75d56078e3c7`）。截图后再次调用
`Page.getState` 仍返回 `status=ready`，证明图片字节没有进入 WebSocket、连接未被
1009 拆除。历史两次 `-32602` 与两次 1009 均同时使用
`fullPage:true + base64`，不能据此单独归因于 `fullPage`；当前 file 路径无需启用
分段截图兜底。

### P1-1 completeness 单一权威 + 记录数合同

`_evaluate` 现在 marker 命中即 `complete`（证据 I）。改为 region rule 支持 `min_records`：

```json
{
  "id": "reviews",
  "markers": ["用户评价", "评价"],
  "min_records": 20
}
```

**`min_records` 的语义是触发线，不是硬性下限**（见 §2.1）。

**关键：`stalled` ≠ `exhausted`。** 先前版本把"连续 N 轮无增长"直接当作 `end_of_collection`，这会重新引入假完成：

```
页面有 100+ 评论 → 只渲染 2 条预览 → 滚错了容器 3 次
→ 唯一记录数无增长 → 判 end_of_collection → 2 条评论"合法完成"
```

必须四态分离：

| 状态 | 含义 | 允许少于目标即完成？ |
|---|---|---|
| `target_reached` | 去重后记录数 ≥ 目标 | ✅ |
| `explicitly_exhausted` | 有**明确的集合穷尽证据** | ✅ |
| `materialization_stalled` | 多轮无增长，**但不知原因** | ❌ 降级或报 blocker |
| `blocked` | CAPTCHA / 登录 / overlay / 内容抑制 | ❌ |

**`explicitly_exhausted` 的合法证据**（至少满足其一，且都必须是机械可判的）：

- 已验证的目标容器滚动几何到底（`scrollTop + clientHeight >= scrollHeight`）**且**最后 N 轮无增长
- load-more 控件在**至少成功加载过一次之后**消失或变为 disabled
- 页面明示总数且 `totalCount <= actualCount`
- 数据源返回明确的终页标志

**单纯"无增长"只能得出 `materialization_stalled`，不能证明集合真的只有这么多。** 不接受模型自称"已经到底了"。

**⚠️ 当前 `collect_items` 还产不出这些证据——四态设计缺数据源。**

现有输出只有一个扁平的 `stopReason` 字符串：

```
max_rounds | time_budget | target_met | load_more_exhausted
harvest_unavailable | stagnant | harvest_limit_reached | <overlay terminal>
```

问题：**完全没有滚动几何证据**；`stagnant` 只是"连续 N 轮无增长"（`stability_threshold`），不含原因；`load_more_exhausted` 来自 `action["exhausted"]`，需确认它是否要求"曾成功点击过一次"。

因此 P1-3 必须新增结构化输出，`ContentCompletenessTracker` 消费它而不是自己匹配文本：

```json
{
  "collectionState": "target_reached | explicitly_exhausted | stalled | blocked",
  "exhaustionEvidence": {
    "kind": "declared_total_reached | scroll_bottom | load_more_absent | terminal_marker",
    "observed": { }
  }
}
```

映射约束：

- `stagnant` / `max_rounds` / `time_budget` / `harvest_limit_reached` → **一律 `stalled`**，不得升为 `explicitly_exhausted`
- `click_load_more` 失败 → `stalled` 或 `blocked`，**不算耗尽**
- `load_more_absent` 只有在**至少成功点击过一次、且重新取 DOM 后确认消失或 disabled** 才成立
- 无可靠耗尽证据且不足目标数量 → **保持 incomplete**

**已知代价（可接受）**：真实只有 2 条评论的商品可能停在未完成，而不是被认证为完成。这是正确的保守方向。

**但必须同时守住出口**：这类 incomplete **不得**被后续 replan 用占位符洗成完成——这正是 P0-1 的机械不变量要拦的路径。保守判定与验收放水是一套问题的两端，两边必须同时落地，否则只是把假数据的产生点从 completeness 挪到 replan。

因此判定表为：

```
records >= min_records                                 → content_materialized
records <  min_records 且 explicitly_exhausted         → content_materialized（合法少于目标）
records <  min_records 且 materialization_stalled      → shell_seen（升级路径 2 或报 blocker）
records <  min_records 且 blocked                      → blocked_content_suppression
records 命中 blocker/占位符模式                          → 非法，拒绝落盘
```

**三者职责分离（勿混用，见 §3.3）：**

| 组件 | 职责 |
|---|---|
| `semantic_index` | **发现**目标区域、候选容器、selector 候选 |
| `collect_items` 的受控只读 oracle | **采集、去重、计数**，并产出终止证据 |
| `ContentCompletenessTracker` | **判定**完整性——消费上面两者的结构化输出，不自行匹配文本 |

`extract_dom_records`（模型可直调的 `Runtime.evaluate` 包装）不参与这条链路。

状态从二值改为三值：`absent` / `shell_seen` / `content_materialized`。只有第三态可终态化，且它是路径 2 升级触发器的唯一判据来源（§2.2）。

站点词表（"用户评价"等）留在 strategy bank 声明里，通用层只认 `region_id` 和抽象状态——遵守无硬编码铁律。

**阶段 3 实施结果（2026-07-20）**：`expected_regions` 对象现支持正整数
`min_records`，并在显式 worker contract、strategy 推导和 skill 配置链上保留；未声明
时保持原 marker 兼容语义，通用层不提供隐式数量默认值。Tracker 现按 region 保存
`contentState`、`regionRecordCounts`、`regionCollectionStates` 与机械穷尽证据，并在新的
navigation epoch 清空；完成证据在同一 epoch 内单调保持。

`collect_items` 的 `rowCount` / `collectionState` / `exhaustionEvidence` 已直接进入 Tracker。
只有有效的显式 `regionId`，或能唯一匹配区域 id / 声明式 `fields` 别名的
`collectionField`，才能修改区域计数和状态；无线索时推导出的唯一候选只写入
`collectionBindingCandidate` 遥测，不作为完整性证据。无效或歧义的
`regionId` / `collectionField` 均拒绝归属、返回补传 `regionId` 的纠正提示，且不得回退猜测。
同一 navigation epoch 内的完成证据按 `target_reached > explicitly_exhausted > stalled/blocked`
单调保留，既不让冗余 stalled 探针覆盖已完成遥测，也允许明确穷尽随后升级为目标达标。
历史 38 条“仅评价 marker 即 complete”全部回放为
`shell_seen`；20 条达标和“8 条 + 明确到底”均为 `content_materialized`，8 条 stalled
保持 `shell_seen`。auth/challenge/overlay 仍由既有高优先级分类器处理。字段别名绑定与
完成证据优先级修复后的全量回归为 **1418 passed / 6 skipped**。

当前 `shell_seen` 只在 worker 自报 `target_absent` / `instruction_infeasible` 时参与终态
否决；它尚不能阻止 worker 将不完整结果声明为成功或进入 `validated_done`。这条闭环属于
后续 P1-2，不应把阶段 3 的完成状态理解为“所有少抓结果都会被验收层拦截”。

### P1-2 让两条路径都能走通并能升级

见 §2.2 的路径定义。这里是实现要点。

**路径 1（默认，`Page.navigate`）**：现状已可用，不改。

**路径 2（恢复，列表点击）——分两阶段，先前把两阶段混为"~30 行改动"是错的：**

**阶段一：只保证路径 2 用普通 browser call 走对（不承诺编译成 workflow）**

```
刷新列表树 → 按 identity 绑定 anchor → Input.click
→ Page.popupRequested / Page.open → Page.getState settlement
→ Page.list持久化对账 → 分类结果 → 详情抽取
→ Page.switchTo / Page.go 返回 → 重新感知
```

1. **保留来源页**：记录 `sourcePageId` / `sourceUrl` / item identity；把进入 B
   前恢复 source 与 landing 后返回 source 分开。新标签用
   `Page.switchTo(sourcePageId)` 返回，同页跳转用 `Page.go(back, n=1)`，
   `Page.navigate(sourceUrl)` 仅作 B source-restore 兜底。current-tab 无法返回时
   当前 item 可完成，但 cohort 停止。
2. **点击结果按真实反馈分类**，不得按第一件商品的行为假设后续：匹配
   `effectiveActorAgentId/sourcePageId` 的 `Page.open` 建立 actor/page lineage；
   `Page.popupRequested` 只为 popup request 提供 provisional opener evidence；
   漏通知时 `Page.list` 新增 pageId 只建立待归因 candidate；原 pageId URL 变化
   → source page本身是 SAME_TAB landing；均未变但 DOM 显著变化 → IN_PAGE；
   无变化 → NO_EFFECT。原始 resource owner不同不构成 hard conflict。
3. **anchor 每次重新绑定**：不得跨商品复用 AX id；优先用稳定 href / data 属性定位，AX id 只在刚刷新的树内有效。
4. **同 cohort 传播路径偏好**（新增）：一旦同一任务、同一模板出现确认性证据，剩余同型商品**直接优先路径 2**，不要每件都重跑一次已知失败的直接导航。进入 B 的
   item route stage 必须进入 task/item ledger，跨 phase/replan/worker保持单调，
   Lead 自述不能重置到路径 1。

   **作用域严格限定为 `task + strategy + page-template/cohort`**——绝不能升级为"淘宝域名永久偏好"。它是 `fastPathReceipt.routePreference` 的一个字段，随 receipt 一起生灭。

   **提升条件必须是成对证据**（缺一不可）：

   ```
   direct navigation      → shell_seen / 内容被抑制
   click-through          → content_materialized / 必需区域已观察
   ```

   只有一半（例如 direct 失败但 click 也没验证成功）不得提升。

   **降级 / 废弃 receipt 的触发条件**（任一命中）：

   - 目标 marker 消失
   - selector 绑定漂移
   - 点击后未产生预期导航
   - `auth_generation` 改变
   - 出现 challenge / auth 状态

   后续点击失败可重新探测，**不做永久硬编码**。

**阶段二（后续实验项，不在近期承诺内）：评估可编译性**

先前认为"扩展 `_navigation_variable` 识别 anchor click 即可"。实际还需要处理：每商品实时重绑 anchor、selector/href/identity 参数化、点击前后 `Page.list` 差集、新标签的未知 detail pageId、两种返回策略、`Workflow.execute` 顶层 pageId 如何从 source page 切到 detail page、点击无效/局部抽屉分支。

只改 `_navigation_variable` 会让编译器"认为找到了导航变量"，却生成不出正确的新标签状态机——**比编译失败更危险**。

因此阶段二只在拿到真实成功 trace 后，**先对稳定的同页跳转分支**尝试；新标签差集与动态 pageId 不进第一版。

**阶段 5 代码实施结果（2026-07-21）**：路径 2 的普通 browser-call
升级链路已接入，但真实站里程碑仍未通过。`ContentCompletenessTracker` 现在会在
bounded materialization 后输出结构化 `routeRecovery`（来源页、剩余预算、返回方式和
必做动作）；点击前要求先取得 `Page.list` inventory baseline，点击后按实际反馈区分
`NEW_TAB` / `SAME_TAB` / `IN_PAGE` / `NO_EFFECT`。没有 pre-click inventory 的
`Page.list` 不会把所有既有标签误认成新标签，而是记录
`inventory_baseline_missing`。除可由原 pageId URL 变化独立确认的 `SAME_TAB` 外，
点击后必须再次调用 `Page.list` 才能分类 `NEW_TAB` / `IN_PAGE` / `NO_EFFECT`：跳过该调用
只会放弃路径恢复计账和偏好提升，不得由 harness 根据后续步数或时序猜测点击结果。

Browser final 与 spawner artifact validation 两端均已增加完整性否决：当最新可信证据仍为
`shell_seen` / `materialization_stalled` 时，模型即使改口 `done`，或字段形状已经通过普通
validator，也不能进入 `validated_done`。只有 direct route 的抑制证据与同一 item identity
的 listing-click `content_materialized` 证据成对出现，才产生 task-local
`routePreference=listing_link_click`；scope 包含声明来源、source template 与 regions，
auth generation 变化、challenge/auth、NO_EFFECT、点击后 marker 仍缺失会废弃该 receipt。
本阶段未引入 Micro-Workflow，也未把 anchor click 交给 Workflow 编译器。

定向回归覆盖四类点击结果、缺 inventory baseline、来源恢复 receipt、错误成功终态、
validated-done 双端否决、成对证据提升、local reveal 反误升以及 auth/NO_EFFECT 降级。
全量回归为 **1443 passed / 6 skipped**（含无主 barrier 原子接管回归）。
淘宝 live canary 已证明旧 Fleet/Page 可复用且能进入搜索结果路由，但搜索页再次触发结构化
滑块 CAPTCHA，尚未到达商品详情/评论区，因此**阶段 4b 与阶段 5 的 live 出口均保持待验收**。

**阶段 4b/5 live 补验（2026-07-24）**：上述结论已被新的同 Fleet 续跑证据更新。
`worktree/stage4b_live_canary_20260724/summary.json` 记录了唯一 fleet
`38585657-df01-494d-b0fa-e0b450833dbd` 的完整路径：登录 HITL 恢复后，从淘宝搜索列表
点击真实商品 anchor，以 `NEW_TAB` 打开详情页；评价抽屉前三轮为 skeleton，第 4 轮出现
20 条 `Comment--` 记录；最后 `Page.switchTo(sourcePageId)` 返回原列表，列表仍有可操作链接。

随后 `worktree/stage4b_collect_items_live_canary_20260724/summary.json` 在同一 fleet/detail
page 上直接调用**生产实现** `_collect_items`（不是仿真实现）：`targetCount=20`、
`regionId=reviews`、`collectionField=reviews`，结果为
`collectionState=target_reached`、`stopReason=target_met`、`rowCount=20`、
`declaredMinRecords=20`、`truncated=false`。Composite 自身通过可信 `baseRowRef` 写出恰好
一行嵌套 artifact，`recordExtraction.status=done`、artifact validation `status=done`、
`failures=[]`；嵌套 `reviews` 为 20 项，正文空值 0、去重后 20。

这两份证据已验证真实站点的“列表点击 → new-tab 详情 → 评论抽屉物化 → 生产
`collect_items` → clean artifact → 返回列表”能力链。仍需诚实保留两个边界：

1. 生产 composite canary 为确定性入口，跳过了 Lead/worker LLM；同日固定 page 的完整
   harness canary 停在 `lead.step.start`，模型网关在超出配置的有界重试时间后仍无响应，
   未产生 plan 或浏览器副作用。因此“模型主动选择 `collect_items`”仍缺一条自主 worker trace。
2. 当前 `collect_items.fields` 只支持重复节点自身的 `text/href/src/attr`，不支持每个字段
   各自声明相对子选择器。本次 live artifact 验证的是 20 条 `reviewText` 的嵌套落盘；
   `userName/date/purchasedSpec/reviewText` 四字段单次拆分不应伪称已经验证。

**自主 worker 补验（2026-07-24）**：
`worktree/38a3cb4278d24561b86748f6757d4b9a/traces/browser-001.jsonl` 进一步证明模型会在
真实页面主动选择生产 `collect_items`。为隔离 `baseRowRef` 可信账本依赖，本次契约采用
20 行扁平 `{reviewText}` artifact。模型最终调用：

- `targetCount=20`、`regionId=reviews`；
- `fields={"reviewText":"text"}`、`keyField=reviewText`；
- `recordName=tmall_review_texts_canary`；
- drawer-scoped container 与评论正文重复节点 selector。

结果为 `target_reached` / `target_met` / `rowCount=20` / `truncated=false`，内嵌
`recordExtraction.status=done`、`artifactValidation.status=done`、`failures=[]`，实际
artifact 恰好 20 行。运行进程在 worker 发出最终 `final_answer` 前结束，故
`task_state` 未更新为 `validated_done`；但 composite 选择、执行、落盘与 artifact validator
链均已有机械 trace，阶段 4b 的“模型主动选用 `collect_items`”出口据此通过。

该成功 trace 同时给出了 6B-B 的成本基线：模型到第 12 步才调用 composite，前置使用
1 次 SemanticTree、5 次 `local_fs_read`、3 次 `local_fs_search`，并发生 1 次
`DOM.getAttribute.targets` 参数形状错误。后续确定性前缀的目标是复用已验证 receipt
压缩这些 selector 发现步骤，而不是再次证明 `collect_items` 能否取到 20 条。

**固定 Fleet/Page 路由补强（2026-07-24）**：任务入口现在支持可信的 control-plane
参数 `--fleet-id <uuid>` 与可选的 `--page-id <uuid>`。它们不进入 Lead 可修改的
task plan，而是作为不可变 `PinnedBrowserContext` 直接传给 Lead/Spawner：

- `--fleet-id` 只允许使用已经存在且能被权威 `Fleet.list` 观察到的 fleet；缺失时返回
  `pinned_fleet_unavailable`，不得回退 `Fleet.create`。
- 固定上下文依赖 `harness.fleet_reuse_enabled=true`。CLI 会在创建任务目录前拒绝
  `fleet_reuse_enabled=false + --fleet-id/--page-id`；Spawner 构造层也会拒绝同一矛盾
  配置，覆盖非 CLI/API 调用，防止 pin 被静默忽略。
- 同时给出 `--page-id` 时，slot 选择会优先该 fleet 的稳定 owner slot；owner 正忙时返回
  `pinned_browser_context_busy`，不得把同一 page 委派给另一个并发 worker。
- 固定 page 只暴露该 page 的 lease，并机械拒绝 worker 的 `Page.create`（替代页）和
  `Page.close`（关闭用户页）；仅固定 fleet、未固定 page 时，仍允许在该 fleet 内创建任务页。
- `FleetCoordinator.observe_slot` 将其他 slot 的 `Fleet.list` 视作 observer inventory；
  fleet 一旦 admitted/bound，observer 刷新不得覆盖稳定 owner。Spawner 仍会在 reserve slot
  后做一次权威 Fleet/Page 同步，但固定上下文会参与 slot 选择与 assignment，而不只是写进 plan。

CLI 示例：

```bash
python main.py \
  --fleet-id 961e0e6c-b405-45ce-a68d-3796871a3133 \
  --page-id 0442b698-85c8-4c8d-811c-04bf0a9948f1 \
  --task "在当前页面提取目标数据"
```

固定路由的单元/全量回归已通过（相关 72 条；全量 1193 passed / 6 skipped）。
随后重跑的 Stage 4b canary 位于
`worktree/627cc76903544eeda829797be85f28b4`：slot 的真实 `Fleet.list` 未返回指定
fleet，因此按设计以 `pinned_fleet_unavailable` fail-closed；该次运行没有调用
`Fleet.create` 或 `Page.create`。这验证了“不可替换”约束，但目标实例不在当前 ABCP
inventory，故**尚未验证页面内 `collect_items` 与 artifact 出口，阶段 4b 仍不得标记通过**。

### P1-3 `collect_items` 修复 + 抽取阶梯补档

1. **时间预算改为可配置**，默认从 60s 提高（证据 L：淘宝一轮就打满，`maxRounds: 15` 形同虚设）

2. **嵌套集合输出合同**（先前"行形状映射到契约字段"的说法过于笼统，且方向错误）。

   本次 phase 契约要的是**一行商品**：
   ```json
   {"rank": 11, "productTitle": "...", "unitPrice": "...", "reviewScore": "...", "reviews": [...]}
   ```
   而 `collect_items` 收割的是**评论条目**：`[{"reviewText": "..."}, ...]`，然后把它们当顶层 artifact rows 存了——这才是 `needs_fix` 的成因。

   所以入口预检**不能**简单比较 `collect_items.fields` 的键与 `expected_artifact.fields`：`reviewText` 是 `reviews[]` 的**子字段**，本来就不该等于 `rank/productTitle/...`。

   需要显式表达嵌套：
   ```json
   {
     "recordName": "taobao_hanfu_details",
     "collectionField": "reviews",          // 收割结果注入外层行的哪个数组字段
     "itemFields": {"content": "text"},     // 数组元素的字段
     "baseRow": {"rank": 11, "productTitle": "...", "unitPrice": "...", "reviewScore": "..."}
   }
   ```
   composite 最终保存**一行外层商品行**，`reviews` 为收割到的数组。

   预检项：`collectionField` 是否存在于 expected artifact；`baseRow` 是否覆盖外层必需字段；`itemFields` 是否满足数组元素 schema。

   只做"入口比对字段名"而不支持嵌套，等于把"跑完发现字段错"提前成"入口就拒绝"，仍然表达不了正确形状。

   **⚠️ `baseRow` 必须受限，否则会开出一条新的假数据通道。**

   若允许模型自由提供 `baseRow`，它可以把**未经观察的** `productTitle` / `unitPrice` 写进去，再由 `collect_items` 顺手认证落盘——这就把 P0-1 刚堵上的洞从另一侧重新打开。

   规则：

   - `baseRow` 的字段**只能**来自已验证的上游 artifact，或自带有效 `evidence` / `provenance`
   - composite **只负责填充 `collectionField`**，不认证任何顶层业务字段
   - blocker、登录说明、验证码说明一律进 `collectionStatus` / `blockers`，**不得进入业务数组**

   **占位符文本检测只能作兜底，且必须是结构化判据。** 不能用"登录 / 验证码 / blocked"这类关键词扫描真实评论正文——用户评论里本来就可能提到"要登录才能看"或"输验证码好麻烦"，宽泛关键词扫描会误删真数据。

   正确的兜底判据是结构性的：整个数组只有一个元素、该元素缺 provenance、且文本匹配 harness 自己生成的 blocker 模板。三者同时满足才判占位符。

   （P0-1 第 4 条"业务字段值命中 blocker 语义模式"按此收窄，避免误伤。）
3. **抽取阶梯补上 materialize 档位**（见 §3.6）：

```
DOM.getText/getAttribute 批量（targets）
  → 单目标 DOM 读取
  → 记录一次性全在 DOM：extract_dom_records
  → 记录需要滚动/点击逐步 materialize：collect_items      ← 新增档位
  → gated Runtime.evaluate
```

4. **删除 prompt 中教模型手搓循环的段落**（L5 的 "count records ... refresh DOM.getSemanticTree after each bounded container scroll"、L3 的 "for a lazy drawer/list, retry its reveal/materialization"）。保留其**验收语义**（骨架/预览不算完成），把**执行手段**改为指向 `collect_items`。

   这一条和第 3 条必须同时做：只加档位不删手搓段落，两处指令会打架。

5. `dismiss_overlay` 同样写进 overlay 处置阶梯（当前 prompt 0 次提及、0 次调用）
6. 每轮之间检查 barrier 与 challenge（与 P0-2 联动）

**阶段 4a/4b 代码实施结果（2026-07-20）**：`collect_items` 现在会在首个浏览器
RPC 之前比对 worker contract。扁平模式检查 `recordName` 与输出字段；嵌套模式检查
`collectionField` 是否为已声明外层字段、字段类型是否为数组、可信 `baseRowRef` 是否覆盖
其他必需外层字段，以及声明了 nested required fields 时 `fields` 是否完整。失败返回结构化
`contractPreflight.reasons` 和映射建议，不启动 materialization。

Lead 的 `expected_artifact.fields` schema 同步公开嵌套集合的规范形状：
`{"name":"reviews","type":"array","items":{"required":["reviewText","date"]}}`。
若一次扁平采集只能覆盖最终行的一部分，不能通过省略 `recordName` 规避预检——该模式只
返回少量 `sample`，不会暴露完整累计行。正确做法是拆出字段一致的上游采集 phase/artifact，
验证后再由依赖 phase 补充；嵌套集合通过可信 `baseRowRef` 合并。

`collectionField` 绑定也采用 fail-closed：显式标量类型返回
`collection_field_not_array`；裸字段或缺少 `type: array/list` / `items` 证据时返回
`collection_field_array_contract_required`。无 `recordName` 的完成结果只提供三条 `sample`，
运行时提示明确禁止将该 sample 当作完整目标数据落盘。

当缺少数组契约时，`collect_items` 同时返回可恢复分类
`collection_contract_replan_required`，携带 `field` / `expectedShape`。Spawner 从
`collect_items` trace 机械恢复该分类（不依赖 worker 是否正确复述），Lead 据此修改不可变的
`expected_artifact` 后 replan；该分类不进入终态集合。

BrowserAgent 抽取阶梯现明确区分“一次 DOM 快照可读”的 `extract_dom_records` 与
“需要滚动/load-more”的 `collect_items`，并删除逐轮 SemanticTree/local_fs 手搓指令；overlay 恢复同样改为
调用 `dismiss_overlay`，不再让 strategy 展开其内部 Escape/backdrop 梯子。预算与嵌套输出沿用
阶段 0 已落地的 `maxDurationMs` / `collectionField` / `baseRowRef` 合同。嵌套 schema 可发现性
与安全分段、sample 防截断、数组契约门控及 replan 路由闭环补齐后的代码回归为
**1425 passed / 6 skipped**；阶段 4b 的真实站点验收仍以
表 §6 的 live 条件为准。

第一次 live canary 保存在 `worktree/74f6aba5262d4b4b9c926d58b108bd50`：Lead 第 1 次
请求在 `max_tokens=12000` 时空文本截断，第 2 次成功生成两个 phase；搜索 phase 随后打开
淘宝搜索页，但新注册会话返回 `fleetCount=0`，页面出现登录 iframe 并停在加载态，worker
正确调用 `Hitl.requestPause`。为避免在尚未进入详情采集前空等 1200 秒，本次 canary 主动
终止。因此它验证了 auth/HITL 边界，没有验证模型在评论区是否主动采用 `collect_items`；
阶段 4b 的 live 出口仍未满足，不能写成已完成。

### P1-3b 把 `collect_items` 泛化为通用有界循环原语 — **DEFERRED / 不在当前实施范围**

> **状态：已叫停。** 本节保留仅作设计记录。
> 重新评估的前提：出现**第二个**经过验证的同型循环场景（分页、多步表单、重试直到条件成立）。
> 在此之前不实现 `bounded_loop`，`collect_items` **不**改造成 preset，也不承诺任何 preset 迁移。
>
> 当前对 `collect_items` 的工作范围以 **P1-3c 最小补丁**为准。

<details>
<summary>（已归档的设计草案，点开查看）</summary>

**提案（原始形态）**：

```python
def min_tools_loop(tools_list: list, turns: int, stop_signal: str, pattern: str)
```

按列表顺序执行工具、循环 `turns` 次、某工具结果匹配 `pattern` 则终止。

**方向认同**：`collect_items` 不该是唯一的硬编码循环。分页、多步表单、重试直到条件成立都是同一形状，各写一个复合工具不可持续。**但原始签名有三个硬问题。**

---

**问题 1：`pattern` 匹配的是 stub，不是内容。**

工具结果超过 `DEFAULT_TOOL_RESULT_OFFLOAD_THRESHOLD_BYTES = 50000` 会被 offload，只留 `{savedPath, outline, format, queryWith}` 的 stub。淘宝详情页 SemanticTree 约 0.5MB，AXTree 约 0.5MB / 3.5–4.7k 节点——**必然被 offload**。

正则匹配一个 stub 没有意义。要么循环内部自己读文件（那 `pattern` 就不是"对工具结果做正则"了），要么只能匹配到 `savedPath` 字符串。

**问题 2：真正的停止条件表达不了。**

有意义的终止判据是：

```
去重后记录数 >= target
连续 N 轮唯一记录数无增长（stalled）
容器已到底（end_of_collection）
```

这三个都是**跨轮累积状态的函数**，不是单轮结果的正则。同一份 SemanticTree，8 条新增和 8 条重复长得一模一样——正则分不出来。

正则能表达的只有"页面出现了某个文案"这类**站点特定的脆弱信号**，恰好是我们一直在避免的东西。

**问题 3：丢掉跨轮去重——即已确认的核心价值。**

泛型循环没有"行"的概念，只能返回 N 组原始工具结果，去重要么交给模型（回到烧 token 的老路，正是本次 161 次 `local_fs` 的成因），要么没人做。虚拟列表（行被回收出 DOM）在这种设计下无法收全。

**问题 4（安全）：泛型循环会绕过 per-step 防护。**

`loop_guard` / `progress_check` 是注册表上的**每模型步**标志。一个内部跑 50 轮的泛型原语完全逃逸这些检查。而且 `tools_list` 若不设白名单，可以循环点击提交按钮或反复导航。

---

**建议的修正形态**：把「循环驱动」与「累积策略 / 停止条件」拆开，后两者**类型化而非正则**。

```python
async def bounded_loop(
    steps:      list[ToolCall],     # 每轮按序执行；限定在只读 + 滚动 + load-more 白名单内
    accumulate: AccumulatorSpec,    # 如何从轮结果提取记录并按键去重；none = 纯动作循环
    stop_when:  list[StopSpec],     # 类型化条件，任一命中即停
    budget:     {turns, seconds},   # 强制有界，不可省略
) -> LoopResult
```

`StopSpec` 的类型化取值：

| 取值 | 语义 |
|---|---|
| `target_reached(n)` | 去重后记录数 ≥ n |
| `stalled(rounds)` | 连续 N 轮唯一记录数无增长 |
| `field_equals(step_index, json_path, value)` | 对**结构化字段**判定（保留提案里 `stop_signal` 的意图，但走 JSONPath 而非全文正则，不受 offload 影响） |
| `budget_exhausted` | 轮次或时间用尽 |

`AccumulatorSpec`：

| 取值 | 语义 |
|---|---|
| `rows_by_key(selector, fields, key_field)` | 现 `collect_items` 语义 |
| `none` | 纯动作循环（重试、翻页到底而不收集） |

**`collect_items` 随之变成一个 preset**：

```
bounded_loop(
  steps      = [materialize(scroll | click_load_more), harvest],
  accumulate = rows_by_key(selector, fields, keyField),
  stop_when  = [target_reached(n), stalled(3), budget_exhausted],
  budget     = {turns: 15, seconds: <可配置>},
)
```

保留：跨轮去重、开窗收割、停滞判定、overlay 探测、challenge-pause 中断、契约预检（P1-3 ③）。
获得：分页、多步表单、重试等场景复用同一原语，不再新增复合工具。

**白名单**：`steps` 只允许只读方法 + `Input.scroll` + 指向 load-more 控件的 `Input.click`。不允许提交、导航、支付类调用进入循环体。

**优先级**：本项是 P1-3 的**后续重构**，不是前置。

</details>

---

### P1-3c 【当前唯一在办】`collect_items` 最小补丁

**范围锁定。** 只改：

```
harness/tools/browser_tools/composites/collect_items.py
harness/tools/browser_tools/schemas.py
harness/vl/reality_check.py
harness/observation/verifiers.py   （仅新增一个固定只读 geometry 探针）
tests/test_collect_items.py
tests/test_reality_check.py
```

**本轮明确不做**：`bounded_loop` / Micro-Workflow / `fastPathReceipt` 预执行 / ephemeral 编译 / Lead phase 合并 / PlanValidator / Tracker 接入 / prompt 与 strategy 重写。

先把 `collect_items` 的**输出合同**稳定下来，Tracker 下一步再消费。

**实施必须原子**：①②③④ 作为**一个补丁**同时落地，不可拆分交付。

理由：若先上"结构化状态 + 未完成不落盘"而穷尽证据尚未实现，则所有非 `targetCount` 达成的采集都会判 `materialization_stalled` → 全部无法落盘，中间态是一次功能倒退。⑤⑥⑦ 可以在同一 PR 内后置，但不得先于 ③ 合入。

---

**① 保持输入兼容**：工具名、`selector` / `fields` / `keyField` / `scroll|click_load_more` / 跨轮去重 / overlay 恢复 / HITL 中断 / AXTree 初始化与结束 resync / 旧 `stopReason`（降为诊断字段）全部保留。不做抽象重构。

**② 新增结构化终止状态**（枚举统一，不要同时存在 `stalled` 与 `materialization_stalled` 两种写法）：

```json
{
  "collectionState": "target_reached | explicitly_exhausted | materialization_stalled | blocked",
  "exhaustionEvidence": null
}
```

固定映射：

| 旧 `stopReason` | `collectionState` |
|---|---|
| `target_met` | `target_reached` |
| 可靠 scroll-bottom / load-more-absent | `explicitly_exhausted` |
| `stagnant` / `max_rounds` / `time_budget` / `harvest_unavailable` / `harvest_limit_reached` / 普通 click 失败 | `materialization_stalled` |
| overlay 未解 / challenge / HITL 中断 | `blocked` |

`status: done` 仅表示工具执行结束；**业务完整性只看 `collectionState`**。

**③ 第一版只支持两类穷尽证据**（不要一次实现 `totalCount` / terminal marker）：

*(a) 滚动容器到底* — 必须同时满足：显式传入 `containerSelector`；固定只读 oracle 确认该容器存在；已采集 item 确实位于该容器内；`scrollTop + clientHeight >= scrollHeight - tolerance`；连续 `stabilityThreshold` 轮无新增；累计记录数 > 0。

```json
{"kind": "scroll_bottom",
 "observed": {"scrollTop": 1000, "clientHeight": 600, "scrollHeight": 1600, "stableRounds": 3}}
```

**第一版不认 viewport / page root 到底**——否则详情页的两条预览评论会被判成完整集合。

`tolerance = max(2, clientHeight * 0.01)`，避免亚像素与缩放导致永不相等。几何读数还须通过守卫：三个数值均**有限且非负**；容器仍 `connected`；`scrollHeight >= clientHeight`；item selector 确实位于该容器内。任一不满足 → 不得认证穷尽。

*(b) load-more 消失* — 必须：**至少有一次成功点击**；随后点击失败；用 `loadMoreSelector` 重新跑只读探针；探针确认控件不存在或 `disabled` / `aria-disabled=true`。

**⚠️ 当前实现有确凿缺陷，必须修**：

```python
# collect_items.py:229
return {"ok": False, "exhausted": True, "detail": "load_more id gone/failed"}
# collect_items.py:242
if _invoke_result_failed(result):
    return {"ok": False, "exhausted": True, "detail": "load-more click failed"}
```

**点击失败被直接当作 exhausted，且不要求曾经成功过。** 首次点击就失败、只有 AX id、目标被遮挡、selector 仍在但点击失败——全部只能是 `materialization_stalled` 或 `blocked`。

**④ 未完成的采集不得自动落盘**（本补丁**价值最高**的一条）：

仅 `target_reached` 与 `explicitly_exhausted` 允许自动 `record_extraction`。
`materialization_stalled` / `blocked` 时：返回 `rowCount` / sample / rounds / `next_step`，设置 `pending_unrecorded_extraction`，**不生成可被 validator 接受的目标 artifact**。

这条直接掐断"2 条预览 + 滚错容器 → 保存为完整评论"，且**不依赖任何文本判断**。

**⑤ 嵌套输出用可信引用，取消自由 `baseRow`**（比先前的 provenance 方案更严、更机械）：

```json
{
  "recordName": "taobao_hanfu_details",
  "collectionField": "reviews",
  "baseRowRef": {"savedPath": "<本任务已登记且已验证的上游 artifact>", "rowIndex": 10},
  "fields": {"content": "text"}
}
```

- 缺省 `collectionField` → 保持现有扁平模式（向后兼容）
- 使用 `collectionField` 时**必须**提供 `baseRowRef`
- harness 自己读取该 artifact 行并复制，**只注入 `collectionField`**
- 模型**不能**在参数里提供 `productTitle` / `unitPrice` 等顶层业务值
- 入口预检失败 → 在**任何 browser call 之前**返回 rejected

harness 自读自复制，比校验模型填写的 provenance 更小、更难绕过。

**⑥ 时间预算参数化，不整体抬高**：schema 新增 `maxDurationMs`，默认暂定 120s，范围 5s–300s，输出 `durationMs` 与每轮耗时。淘宝单商品验证时显式传 180s。**不要把全局默认直接提到 300s**——错误 selector 会长时间空转。

**⑦ 修 `reality_check` 的分类不一致**：

```python
# harness/vl/reality_check.py:78-84
_EXHAUSTED_STOP_REASONS = frozenset({"stagnant", "load_more_exhausted", "max_rounds"})
```

**⚠️ 先前把这里描述为"把 shortfall 盖成 satisfied、是假完成通道的另一端"——错了。** `classify_target_yield` 的契约是 `True = shortfall / False = satisfaction / None = 无法判断`，而这些 stopReason 命中后 `return True`，即**判为 shortfall**，会累加 streak 并触发 VL reality check，**不会**认证完成。

（这是从常量名 `_EXHAUSTED_STOP_REASONS` 与注释 "the page was exhausted" 推断语义、未读返回值造成的误判，已更正。）

**真实缺陷是**：命名误导（名单里的东西并不是穷尽）；无法表达新的 `explicitly_exhausted` 与 `stalled` 之别；因此会触发**不必要的 VL 检查**；与 `collectionState` 合同不一致。

**但它有一条实际代价，值得优先修**——不必要的 reality check 正是 fullPage 截图的触发源：

```
stagnant → classify_target_yield=True(shortfall) → target_shortfall_streak++
  → 达阈值(默认 3) → _visual_verify(fullPage=True, _force=True)
  → 17.5MB base64 → 1009 → 连接被拆（证据 H）
```

即 **⑦ 与 P0-3 是同一条链的两端**：⑦ 减少误触发，P0-3 消除触发后的杀伤。两条都做才闭合。

改为优先读 `collectionState`：

```
target_reached / explicitly_exhausted → satisfied
materialization_stalled               → shortfall
blocked                               → 不抢占 challenge/auth 分类
```

仅当旧 trace 没有 `collectionState` 时才回退解析旧 `stopReason`，且 `stagnant` / `max_rounds` 仍只能判 shortfall。

---

**必补测试**：① 达 `targetCount` → `target_reached` 且允许落盘；② 错容器连续无增长 → `materialization_stalled` 且**不得落盘**；③ 指定容器几何到底且稳定 → `explicitly_exhausted(scroll_bottom)`；④ 首次 load-more 点击失败 → stalled 而非 exhausted；⑤ 成功过一次后控件消失 → `explicitly_exhausted(load_more_absent)`；⑥ 控件仍在但点击失败 → stalled；⑦ `max_rounds` / `time_budget` / `harvest_limit` → stalled；⑧ overlay / HITL → blocked；⑨ **`time_budget` 在 `stabilityThreshold` 满足之前触发 → stalled，即使几何恰好到底**；⑨b **反向**：稳定阈值已满足、穷尽证据已成立后才撞上 deadline → **保留 `explicitly_exhausted`，不得被 deadline 覆盖**；⑩ 现有 stable-key 去重与虚拟列表测试保持通过；⑪ legacy 扁平模式兼容；⑫ `baseRowRef` 合法 → 生成一行外层 artifact；⑬ 非法路径 / 未验证 artifact / 越界 `rowIndex` → **发起任何 browser call 前**拒绝；⑭ reality_check 不再把 `stagnant` / `max_rounds` 当合法穷尽。

---

**验收标准分三档**（先前笼统写成"本轮拿不到真实 20 条评论"，**说过头了**——不接 Tracker 影响的是**自动恢复链**，不是采集能力本身）：

| 档 | 内容 |
|---|---|
| **必须** | 不误判、不误落盘。错容器/首次点击失败/超时 → `materialization_stalled` 且无 artifact |
| **最好验证** | 在**人工准备好**的完整评论抽屉上，`collect_items(targetCount=20)` 能达成 `target_reached=20` 并落盘 |
| **不要求** | 自动识别 route suppression → 升级列表点击 → 进入抽屉 → 门控完整性 |

`ContentCompletenessTracker` 本轮不接入，仍是 marker 命中即判 `complete`（证据 I）。因此**端到端自动恢复**要等 P1-1；但只要页面已由正确路径打开、抽屉已展开、selector 正确，修好的 `collect_items` 本身就应能取满 20 条。

---

### P1-4 【主项】Lead 的 phase 规划与 replan 降级策略

**这是快路径失效的根因，不是缺机制。**

"row0 正常跑 → 编译 trace → canary row1 → 剩余零-LLM" 的机制**已经存在**（`_maybe_run_ephemeral_batch_after_first_row`，§3.2）。它没跑起来，是因为上游 Lead 的两个规划缺陷：

**缺陷 1：同构行被拆成多个 phase。**

collect 产出 10 行 validated artifact，Lead 却发了 13 个 detail phase，12 个 rowCount ≤ 1（证据 M）。每个 phase 一个 worker、~30 步全量自由探索。

`batch_rows >= 3` 的触发条件从未满足——不是因为条件苛刻，是因为**每个 phase 只有 1 行，物理上不可能满足**。

**修法（先前写"无新代码"，已更正——见 §3.2，编译器禁止名单使零-LLM 不可达）：**

必须把快路径分成**两级**，并明确近期只承诺第一级：

| 级别 | 条件 | 每行成本 | 本次任务适用性 |
|---|---|---|---|
| **guided composite path** | trace 含 composite / `local_fs_*` 等不可编译类型 | 少数几个模型步 | ✅ **近期目标** |
| **workflow fast path** | trace 完全由可编译 ABCP action 构成 | 零模型步 | ❌ 含 `collect_items` 必然不可达 |

**guided composite path 的含义**：BrowserAgent 仍然起来，但不再自由探索——按已验证的 guidance 直接调用批量 DOM 读取、`collect_items`、返回列表流程。每商品从 ~30 步降到少数几步。

**guidance 必须是可执行契约，不能只是一句"复用已验证经验"。**

第一行成功并通过契约后，由 **harness**（不是模型）生成 task-local `fastPathReceipt`：

```json
{
  "strategyId": "web_scrape.detail_sections.reveal_then_text",
  "routePreference": "direct | click_through",
  "detailReadyMarkers": [],
  "revealAction": {},
  "collectionParams": {},      // selector / containerSelector / keyField / itemFields
  "returnBranch": "new_tab | same_tab",
  "validatedAgainstRow": "<item identity>",
  "generation": 3
}
```

约束：

- **只记录实际执行并验证过的事实**，模型不得自行撰写 receipt
- **不保存 AX nodeId、绝对坐标、pageId** 等会过期的东西（只存 selector / href / identity 绑定方式）
- 任一 binding 或 materialization 验证失败 → **立即单行降级**回自由探索，不影响已完成行
- receipt 只在当前 task / cohort 内有效，**不沉淀为跨任务 strategy**（永久化仍走 `/skill-create`）

**⚠️ 但只有 receipt 还不够——注入上下文的 receipt 是建议，模型可以无视它。**

这正是本次 `batch_rows` 失效的同一种失败模式：prompt 写了规则，模型没照做。因此必须配一个机械环节，二选一：

- **(推荐) harness 预执行 receipt 的确定性前缀**：导航/进入详情/reveal/`collect_items` 这段由 harness 直接按 receipt 参数发起，模型只在结果回来后做收尾与异常判断。这样"少数几步"是**结构决定的**，不依赖模型自觉。
- (兜底) 保留模型自主，但加检测：receipt 存在时若连续 N 步未使用其参数、且在重复探索同一区域 → `progress_intervention` 提示并强制回到 receipt 路径。

**不加这一层，"guided" 会在压力下退化回每商品完整探索，届时又只能归因为"模型不听话"。**

**这是本次任务近期唯一现实的目标。** 6× 左右的成本改善仍然显著，但不要在文档或 prompt 里承诺"rows 1–9 零 LLM"。

若坚持要零 LLM，必须二选一并明确计价：
- 让 Workflow 能调用 composite（ABCP 侧能力扩展）
- 新增 harness-local batch runner（与本文档"不新增 task 级执行引擎"的原则冲突）

**两者都不在本方案范围内。**

**Lead 侧修法（真正需要的）：**

先前写"把 prompt 的可选改成必须"过于乐观——**当前 prompt 已经写了 `>= 3 homogeneous rows attach batch_rows`，本次仍拆成 13 个 phase**。同一根杠杆已经失效过一次，加重措辞不会改变结果。

因此改为：

1. **打开 `ephemeral_workflow_enabled`**（前提条件，虽不充分）

2. **从已验证的上游 artifact 自动构造 `batch_rows`**——复用现有 `skill_rows` 的 auto-build 逻辑。这不新增执行层，只把模型从"复制 10 行数据到 plan 里"这件容易漏、容易错的事里摘出去。

3. **spawn 前的机械 cohort 检测 + 合并**（关键补充）。

   仅靠第 2 条**合并不了已经拆好的 13 个 phase**——每个 phase 各自只有 1 行，auto-build 无从下手。必须在**创建 worker 之前**介入：

   ```
   拿到上游 validated artifact
     → 计算 cohort key（task + schema + strategy + depends_on）
     → 检测同 cohort 的重复单行 phase
     → 合并为一个 batch phase 并挂 batch_rows
     → 提交新版计划 → spawn
   ```

   **⚠️ 先前写的判据是错的**：`if 四维全同 and 均单行: merge_all()`。

   它会摧毁**本方案的条件式** probe → validation → bulk 信心升级：

   ```
   Phase 1 / probe        1 个商品，自由探索确认入口、展开、采集路径
   Phase 2 / validation   仅在 probe 产出 candidate 后验证路径非偶然
   Phase 3 / bulk         仅在 validation 通过后处理剩余 7 个
   ```

   这三个 phase 的 task_type / schema / strategy / 上游依赖**完全相同**，但**职责与进入条件不同，不能合并**。我提出的合并规则会把这个正确结构压平——自相矛盾。

   **正确做法是两层判断：**

   **第一层 — 是否同 cohort**（`task_type + artifact schema + strategy + 上游依赖`）。它只回答"这些行理论上能否用同一种处理方式"，**不回答"该分几个 phase"**。

   **第二层 — 该怎样分批**，需要 `execution_role` 与批次策略：

   ```
   execution_role: probe | validation | bulk | continuation | remediation
   批次约束:      max_rows_per_phase / 超时预算 / fleet-auth 隔离 /
                  是否共享页面状态 / 是否存在行间顺序依赖
   ```

   **⚠️ `execution_role` 只是分类标签，`depends_on` + checkpoint 门控证据才是执行保证。**

   上面的 probe → validation → bulk 例子里我写了"上游依赖完全相同"——**这本身自相矛盾**：若三者 `depends_on` 真的一致，它们可被并发启动，就不构成升级链。正确形态是共享同一个**上游数据来源**，但 `depends_on` 逐级串起来。

   因此不能只加一个模型可自由填写的 `{"execution_role": "probe"}`，必须机械校验：

   | role | 机械约束 |
   |---|---|
   | `probe` | 小样本；不得声明大批量 |
   | `validation` | **必须绑定明确要求 validation 的 checkpoint，并依赖该 checkpoint 的真实前驱 phase** |
   | `bulk` | **必须绑定明确要求 bulk 的 checkpoint，并依赖该 checkpoint 的真实前驱 phase** |
   | `continuation` | **必须绑定明确要求 continuation 的 checkpoint，继续 BrowserAgent 慢路径** |
   | `remediation` | 只用于 active checkpoint 之外的明确失败行集合；cohort 内修复必须走 continuation |

   否则普通 bulk phase 只要标成 `probe` 就能逃避合并——把一个可绕过的标签当成执行保证。

   **机械合并只允许作用于**：

   ```
   同 cohort
     且 execution_role == "bulk"
     且 相同依赖前沿
     且 行间独立
     且 合并后不超过批次上限
     且 无 auth/fleet/challenge 隔离边界
   ```

   `probe` / `validation` / `continuation` / `remediation` **不得自动并入 bulk**。

   缺少 `execution_role` 信息时，只能产出 `fragmentation_candidate` 交给 Lead 重写或 PlanValidator 裁决，**不得直接全量合并**。

   **真正要拦的是这种**——十个 phase 全部 `role=bulk`、依赖同一 artifact、contract/strategy/validator 相同、每个只处理一行、且无批次上限或隔离理由。本次的 12 个单行 detail phase 正属此类，机械检测确实能覆盖**这个实例**；但"四维全同 + 均单行"作为**通用规则**是不成立的，只能当 cohort 筛选器。

   下面两种都合理，不得合并：candidate 证据成立后的
   `1 probe + 2 validation + 7 bulk`；没有 candidate 时的
   `1 probe + remaining continuation`；存在明确批次上限时的
   `4 bulk + 4 bulk + 2 bulk`。

4. **collection 完成后设置显式 replan checkpoint**，让 Lead 在拿到 validated artifact 的那一刻做批量决策，而不是随手继续发 phase

5. **PlanValidatorAgent 驳回无理由的同构 phase 碎片化**（P0-1 的语义层职责），作为第 3 条歧义情形的升级路径

**缺陷 2：replan 只会横向换法，不会纵向升降级。**

本次三版计划全是同一层级的重排（6 → 11 → 2 个 phase），没有任何一次是"上一批已验证成功，剩余升级到快路径"或"快路径失败，本行降回慢路径"。Lead 缺少快慢路径的档位概念。

修法（Lead 策略 + 回执）：

- worker 结果回执中明确带出"本 phase 是否产生了可复用的已验证路径"以及"剩余同型行数"
- Lead prompt 增加升降级规则：**看到已验证路径 + 剩余同型行 → 必须走快路径批处理，不得再排自由探索 phase**
- 失败降级按**行**而非按 phase：某行快路径失败 → 该行回慢路径，已完成行保留，不重跑整批
- PlanValidatorAgent 把"存在已验证路径却仍安排多个自由探索 phase"列为驳回项

**做完这两条，本次场景已经不需要任何跨 phase 机制。**

---

### P2-alt 跨 phase cohort（可选，非必须）

仅当 Lead **有正当理由**拆分（真并发、step budget、task_type 边界）时，才需要跨 phase 传递经验。若 P1-4 落实，本次这类场景大幅减少，因此本项降级为可选。

若要做，三处收敛：

**① 三态而非四态：**

```
exploring --(1 项成功且契约通过)--> guided --(1 个独立项用 guidance 成功)--> automated
```

四态方案要额外熬一个商品换一次置信度提升，并多出两条转移边和一套持久化，不划算。需要更高置信就**提高 automated 的行级校验强度**（每行过 completeness + provenance，失败即时降级），比多熬一个商品便宜。

**② cohort 是数据流，不是新执行器。**

今天没有任何组件能承载"跨 phase、跨 worker、零 LLM 的 per-row 循环"——现有两个零-LLM 面（`skill/dispatch.py` 的 pre-worker 快路径、`record_extraction` 内的 ephemeral 批处理）都是 worker-local。新建 task 级执行引擎是本次讨论里**未被计价的最大成本**。

正确做法：cohort 经验做成 task 级纯数据，spawn 时注入（复用 `enrich_worker_contract_with_skill` 钩子），per-row 循环仍留在 worker 内。

**③ variant 按绑定失败驱动，不按域名预分。**

~~原提案建议按目标域分 variant。~~ **已撤回，原判断有误。**

实测：8 个 SemanticTree 观测文件**全部同时含有 `comments--ChxC7GEN` 与 `Comment--H5QmJwe9`**，横跨 `detail.tmall.com`（9d5607dd / 6ebe4f4d / 49e20ee6）与 `item.taobao.com`（23b7bfa5 / 86c8caa8 / c2d5bb00 / 74751f18 / ee18e8cd）。**同一套模板，类名完全一致**，`domain_family` 列表是正确的。

原判断的错误在于**拿域名字符串当模板代理**。正确设计：

- 不按任何静态属性（域名、URL 模式）预先分 variant
- 只在**实际发生 `region_binding_drift` / `record_binding_drift`** 时才开 variant
- 这既避免了误判，也符合无硬编码原则——让绑定结果说话，不让路径字符串说话

**通用失败分类**（不得出现站点词汇）：

- `route_outcome_drift`——已验证的点击结果分支发生变化
- `region_binding_drift`——声明区域的定位规则不再命中
- `record_binding_drift`——区域仍在，但重复记录规则抽不到同型记录
- `materialization_stalled`——执行了 reveal/scroll 但唯一记录数无增长
- `record_contract_unsatisfied`——抽到了记录但数量/字段/证据不满足契约

### P2 可观测性与效率

1. **`agent.*` 事件在写入 `run.jsonl` 时携带 `workerId` / `slot`**（不只是终端 formatter）。证据 N 表明这个缺陷会实际误导归因——本次排查中据此把 browser-001 的截断误判为 browser-005。
2. 终端按 worker 显示真实 step。
3. `Runtime.evaluate` 改为**前置** boundary（不得与任何其他 call 同批），强制先看结构化工具结果再决定是否需要它。
4. `execute_browser_workflow` 的 `steps` 补完整 JSON Schema（现为 `items: {type: object}`，形状零描述）+ 最小示例。**优先级低**：自动快路径不依赖模型手写 workflow。
5. Lead 与 BrowserAgent 分离输出预算，Lead 试 16K 并带"网关不支持时回退 12K"。不同步抬高 worker 上限。
6. **不调整压缩阈值**。本次压缩行为正确（阈值 222822 从未触及，cache_pressure 正常触发两次）；后段变慢由单轮 output 量决定，非压缩问题。

---

## 5. 明确不做的事

记录下来以免讨论反复：

| 提案 | 结论 | 理由 |
|---|---|---|
| 旧式独立 Micro-Workflow 执行器 | **不做；改用 Hybrid Skill** | 不新增第二套浏览器引擎；只将验证 trace 按 composite 边界切成最大 native segments |
| `bounded_loop` / `min_tools_loop` 通用循环原语 | **本轮不做（deferred）** | 出现第二个经验证的同型循环场景后再评估；见 P1-3b |
| 新建 task 级零-LLM 执行引擎 | **不做** | cohort 做成数据流注入现有两个执行面即可 |
| 四态升降级状态机 | **收敛为三态** | 第四态成本与收益不成比例 |
| 计数循环用 `extract_dom_records` | **不做** | 它适合一次性均匀 DOM，不负责渐进 materialization；是否允许由 runtime policy/显式 contract 决定 |
| 新增 `open_cards_verified` 一类复合点击工具 | **不做** | 现有 `Page.list/getState/Input.click/switchTo/go` 足够 |
| 降低压缩阈值 | **不做** | 本次压缩行为正确 |
| 按目标域预分 cohort variant | **不做** | 实测 taobao/tmall 详情页同模板同类名；改为按实际 binding drift 驱动 |
| 把"数量放宽"一律当降级拦截 | **不做** | 商品评论本就可能少于 20 条；判据是有无穷尽证据，不是数量本身 |
| 强制所有详情走列表点击 | **不做** | 直接导航是默认路径；点击是路由敏感场景的恢复路径 |
| 调整 `max_tokens` | **不做** | 用户自行处理 |
| **承诺 rows 1–9 零 LLM** | **当前不承诺** | Workflow 总开关关闭；未来只有完整 native workflow 才是零 LLM，hybrid/慢路径不冒充 |
| 让 Workflow 能调用 composite / 新增 harness batch runner | **不做** | 前者是 ABCP 侧能力扩展，后者是被否决的 task 级执行引擎；两者都超出本方案范围 |
| 用 `Hitl.requestPause` 通用暂停在途 workflow | **不做** | 会对正常页面凭空制造人工暂停；改用 generation fencing |
| anchor-click 编译进 workflow（第一版） | **不做** | 仅改 `_navigation_variable` 会生成错误的新标签状态机，比编译失败更危险 |
| 通用层出现"评论/评价/商品/淘宝"词汇 | **禁止** | 站点知识只进 strategy bank 声明与 task-local guidance |

---

## 6. 实施顺序与验证方式

顺序原则：**先让单个 phase 内的完整链路可验证，再上跨 phase 机制。**

> **当前状态：阶段 0–10 已完成。** 阶段 5b 的导航归因欠账已由
> Enforced Fleet Click Gate 接管；真实浏览器/生产 composite 的物化、落盘与返回证据
> 已保留，不再要求旧 worker-local matcher 形式的统一 trace。阶段 0 当时的验收标准是
> "**不再误判、不再误落盘**"，**不是**"拿到完整评论"；Tracker 已在阶段 3 接入；
> 阶段 1 已通过淘宝长详情页的 `fullPage:true + file` live 验收；阶段 2 已通过真实
> 坏计划/污染 artifact 回放；阶段 3 已接通 `min_records` 与 `collect_items` 的结构化
> 计数/穷尽证据。后续阶段仍为规划。

**阶段 6A 代码实施结果（2026-07-22）**：Lead plan 新增机械校验的
`execution_role=probe|validation|bulk|continuation|remediation` 与声明式 `batch_source` /
`batch_policy`。条件角色必须依赖其 checkpoint 记录的真实前驱 phase；角色名本身不再决定
血缘，因此 continuation 可以重新升级到 validation、bulk 也可以降回 continuation。bulk 仍须明确
`row_independent=true`；spawn 前只从 `task_state.artifacts` 已登记的 validated extraction
artifact 选择行并自动构造 `batch_rows`。同 schema、同依赖、仅由单值 range/rank 拆出的
三个以上单行 phase 会被拒为 `fragmentation_candidate`，但 harness 不会猜测未知 phase 的
职责并擅自合并。`validation` / `bulk` / `continuation` 必须绑定前一 validated phase
产生的 checkpoint；初始 plan 不得为了凑齐梯子提前声明这些角色。

Stage 6A 同时补了 pre-worker acquisition 熔断：相同 objective + routing + error signature
连续两次初始化异常后返回 `spawn_infrastructure_exhausted`，预算跨 replan 保留，不消耗业务
objective attempt。容量暂满、业务验证失败与 Fleet routing 语义错误不计入该预算。
Stage 6B 已拆成 6B-A（审计候选与 replan checkpoint）和 6B-B（live 验证后的确定性
前缀）；只有前者已经实施，后者不得从未通过 live 验收的候选提前启用。

**阶段 6A review 收口（2026-07-23）**：

- `ContentCompletenessTracker` 用 `Page.list` 新标签差分建立来源页角色；来源页不再参与
  detail materialization 终态否决，目标详情页仍正常参与。`Page.reload` 会清空该页当前
  DOM 证据，但保留来源页角色；只有 `Page.navigate` / `Page.go` 会重新定义该角色。
- 原始 `Page.reload` / `Page.navigate` 不再清零 artifact progress 与重型诊断预算；
  只有带成功校验的 `navigate_verified` 和成功 `Page.go` 才建立新的 progress epoch。
- marker 与 shell 证据在同一 navigation epoch 内单调，窄范围 `DOM.getText` 不会擦除
  先前整树观测；真实导航或 reload 后重新采证。
- 碎片化 cohort 由 execution role、有效依赖前沿、source template、contract 和策略共同
  确定。`bulk` 标签不能绕过检测；`requires_isolation_per_row=true` 是显式隔离出口。
  browser 发现的行必须使用 validated `batch_source`，仅当目标身份/URL 已由用户直接
  给定且不存在上游 artifact 时，才允许计划直接携带 `batch_rows`。
- 截图输出的 `file` 规范化集中到共享入口，覆盖 browser call、Workflow 与 control
  channel；blocker template 的 full-match/search 也改为共享同一 pattern source。
- 点击结果分类依赖协议规定的 click 后 `Page.list` 差分；若 worker 跳过它，只损失
  `new_tab` / `in_page` / `no_effect` 的恢复遥测与 credit，不会绕过目标详情页的
  completeness veto。

**阶段 6A R1–R3 收口（2026-07-23）**：

- `Page.create + Page.navigate` 可通过 harness-only `navigation_context` 显式关联已由
  Tracker 确认的恢复来源页。该 sideband 不转发 ABCP；创建空页只登记 pending relation，
  新页必须先拿到可用 URL，再由当前 navigation epoch 的 DOM/collection 证据进入
  completeness 分类，来源页才会被豁免。仅有 `Page.getState` 的未检查 HTTP 页、
  `about:blank` / `newtab:`、失败导航和空白页都不能借此退出 terminal veto；不完整
  目标页会接替来源页成为 veto 候选。
- `Page.go` 不再仅凭 RPC 成功刷新进度。必须有同 pageId 的前置 URL，且后续
  `Page.getState` 证明 URL 改变；每个 artifact/repair generation 的 history credit
  按 batch size 取 `max(4, rows+2)`，硬上限 12。额度耗尽后仍允许真实后退，但不再清零
  no-artifact stall 或重型诊断 epoch；新 artifact 或新增 repair 字段才恢复额度。
- 直接 `batch_rows` 必须携带
  `batch_rows_provenance={source:user_instruction, identity_fields:[...]}`；Lead plan
  验收与 spawn 前各验证一次，每行至少一个声明身份值必须能在不可变原始用户任务中机械
  定位。浏览器发现的行只能走 validated `batch_source`，不能靠 prompt 声明伪装成用户
  输入。
- Python 语义硬编码的分类、迁移边界和独立 LLM 审计规则见
  `docs/python-semantic-hardcoding-audit.md`。协议/账本/安全不变量继续机械执行；站点和
  字段语义迁到 strategy、skill 或 task contract，不让 LLM 覆盖确定性守卫。

R1–R3 收口后，10 份历史落盘计划回放无新增误拒（其中没有 direct `batch_rows` 计划）；
补齐空白页/未探测目标页的两阶段授权后，全量回归
**1469 passed / 6 skipped / 2 warnings**。

**阶段 6A 后续 correctness 收口（2026-07-23）**：

- strategy fallback 不再从全部 artifact fields 猜必需区域，只消费明确
  `nonempty_fields`、`field_nonempty` validator 或字段对象的 `nonempty:true` /
  `required_nonempty:true`。可空评论、参数等字段不会仅因字段名存在而触发 validated-done
  veto；Lead/skill 显式 `content_completeness` 仍保持最高优先级。
- `collect_items(targetCount=0, containerSelector="")` 可对**未声明计数目标的普通扁平
  集合**使用 `document.scrollingElement || document.documentElement` 作为根滚动容器。
  但 document 根的节点归属检查是恒真的，不能证明嵌套/懒加载目标集合已穷尽；当可信的
  `regionId` / `collectionField` 绑定到带 `min_records` 的区域时，工具以
  `max(targetCount, min_records)` 作为有效目标，document-root 到底且数量不足只能得到
  `materialization_stalled`，不得落盘。Tracker 还会独立拒绝用 document-scoped
  `scroll_bottom` 证据豁免计数缺口；显式目标容器到底和可靠的 load-more 消失证据不受影响。
  scope 按解析后的节点身份判定，`body`、`html`、`:root` 与默认滚动根均属于
  document scope，不能通过非空 selector 旁路。`collect_items` 入口还会在循环开始前装载
  worker 的完整性契约，不依赖此前是否已有 DOM 调用初始化 Tracker。
- auth/challenge generation 变化继续全局清理 route preference；单个
  `NO_EFFECT`、click-through marker 缺失只清理匹配的 source-template/cohort，不再让一个
  局部失败抹掉其他 cohort 的成对成功证据。

上述收口及 document-scope 计数缺口修正后，全量回归
**1480 passed / 6 skipped / 2 warnings**。

**阶段 6B-A 代码实施结果（2026-07-24）**：

- `collect_items` trace 只记录 allowlist 内的稳定参数；明确排除 `pageId`、AX/DOM
  handle、坐标和 `baseRowRef`。嵌套集合只记录
  `baseRowBinding=validated_ref_required`，不会把上一行的 artifact 路径变成下一行配方。
- `probe` / `validation` / `continuation` / `bulk` phase 的 artifact/completeness 验收为 `done`，且
  `collect_items` 同时满足完整 collection state、嵌入式 `recordExtraction=done`、
  无 `validationPending` / `contractWarning` 时，才产生
  `fastPathReceiptCandidate`。候选明确标记
  `executionPolicy=not_executable_stage_6b_a`、`coverage=collection_contract_only`；
  它不是完整导航/reveal 配方，任何执行端都不得消费。
- validated phase 写入 task state 后生成 `replanCheckpoint`，并在
  `replan_checkpoints[cohortKey]` 独立记账，绑定源 artifact 内容 generation、已验证源行
  和剩余源行；旧的单值 `replan_checkpoint` 只在读取时迁移。两个并行 cohort 不会互相
  覆盖。`cohortKey` 只表示来源快照与 cohort source-index 集合，不再混入 task type、
  expected artifact 或 validators；这些业务约束在找到 checkpoint 后独立校验，避免契约
  变化制造 lookup miss。后继 checkpoint 继承 predecessor 的 key 和累计进度，只有无前驱
  probe 计算新 key。probe 只有在产生 reusable candidate 时才升级到 validation，validation 只有在
  candidate 继续成立时才升级到 bulk；否则剩余行进入 continuation 慢路径。continuation 后续若重新
  证明 candidate，回到 validation 而不是直接跳 bulk；bulk 的 validated_done 结果若不再证明 candidate，
  降回 continuation。重复已验证行、越过
  剩余行集合、错误 role 或源 generation 漂移均在 worker 创建前机械拒绝。
- `batch_source.cohort_selector` 可声明大 artifact 内稳定的目标全集；每个 phase 的
  `selector` 只负责从该全集选择当前 probe/validation/bulk/continuation slice。cohort key 与
  remaining 均绑定原始 source index 集合，目标外行不会被升级链强制处理；未声明
  `cohort_selector` 时，整份 validated artifact 仍是 cohort。声明的 cohort/phase
  selector value 只要有一个未出现在 validated artifact/目标全集中，就在 spawn 前
  fail-closed，不能静默缩小目标集合。
- Lead replan 必须用顶层 `replan_checkpoint_ids` 回传**全部** active checkpoint ID，
  并以 `worker_contract.replan_checkpoint_id` 将每个 ID 绑定到恰好一个同 cohort、
  规定 role 的 phase。只有一个 active checkpoint 时继续兼容旧
  `replan_checkpoint_id`。自然语言不能取消、覆盖或跳过任何 checkpoint；task state 在
  replan 时保留每个 cohort 的进度和 generation。初始 plan 同样执行 checkpoint 门禁，
  不得在 probe 产出证据前预造 validation/bulk/continuation ID。同 artifact 的新 cohort
  只有在 selector 声明可机械证明不相交时才允许并行；spawn 时再用物化后的 source index
  做权威交集检查。完整替换式 replan 可保留 `validated_done` 的前驱阶段作为依赖历史；
  它们不会被误判为新 cohort。会在 replan 中重置为 pending 的 `phase_failed` /
  `blocked_by_dependency` 不享受该豁免。业务 fence 要求 task type 与合并后的 expected artifact 不变，已有
  non-slice validator 义务只能保留或加强。row-count validators，以及作用于 selector
  identity 的 range/set/unique，属于当前 slice；`stage_hint`、strategy 和 selector 属于
  执行画像，可以随证据调整而不另起 cohort。
- 条件角色的 plan 门与 spawn 门都以 active checkpoint 既有的 `phaseId` 作为权威前驱，要求完整
  replacement plan 保留该 `validated_done` phase，并在 successor 的 `depends_on` 中明确引用。
  `predecessorCheckpointId`、`predecessorPhaseId`、`lineageDepth` 仅供审计，任何门禁不得依赖这些
  新字段存在，因此旧 checkpoint 仍可推进。每次 successor 必须选择旧 remaining 的非空子集；
  validated indices 单调增加、remaining 严格减少，避免 continuation/validation 振荡重跑同一行。
- active checkpoint cohort 内不允许 remediation；失败或剩余行统一使用 checkpoint-bound
  continuation。remediation 只服务于不属于 active checkpoint 的显式失败行集合。
- 来源 artifact generation 改变、来源从 validated ledger 消失、或同 objective 预算耗尽
  时，checkpoint 机械转为 inactive 并保留审计记录。这不是成功认证：generation 变化须对
  新快照重新 probe，来源消失须重跑上游，预算耗尽只能保留已完成行并以 incomplete 收口。

**Checkpoint lineage 升降级收口（2026-08-02）**：候选评估已覆盖
probe/validation/continuation/bulk，角色按 checkpoint 证据在 validation、bulk 与 continuation
之间升降级，不再以固定前驱角色名判断血缘。plan 与 spawn 两道门均要求 successor 显式依赖
checkpoint 的既有 `phaseId`；审计 lineage 字段不参与门禁。保留的 `validated_done` conditional
phase 即使携带已消费的旧 checkpoint ID，也只作为历史，不会被误判为当前绑定。active cohort
内 remediation 被拒并引导到 continuation。全量回归 **1624 passed / 6 skipped / 79 subtests**。

本轮新增定向回归覆盖候选污染、stalled/validationPending、历史 trace 缺参数、
probe→validation、重复行、source generation fencing、并行 cohort、selector 子集和
旧单槽迁移；全量回归 **1498 passed / 6 skipped / 2 warnings**。2026-07-24 的后续
live 补验已取得评论抽屉 20 条物化、生产 `collect_items` 20 条 target-reached、clean
嵌套 artifact 与 new-tab 返回列表证据；6B-B 因此可以进入**受控实现/关闭默认开关的
canary 阶段**。自主 worker 的 `collect_items` + clean artifact trace 已补齐；默认启用
仍需单个 worker 的导航→物化→落盘→返回统一 trace 与确定性前缀失败降级 canary。

**阶段 6B-B.1 代码实施结果（2026-07-24）**：

- 新增默认关闭的 `guided_fast_path_enabled`。开启后，只有通过 active replan
  checkpoint 的下一阶段才可能绑定 harness-private receipt；普通 worker、旧 plan 和
  未开启配置的执行路径完全不变。
- receipt 同时绑定 `checkpointId`、`cohortKey`、源 artifact 内容 generation 和唯一的
  remaining source index。第一版只接受单行、`coverage=collection_contract_only`、
  direct route、非空声明式 detail marker，以及与当前 `expected_artifact.name` 完全一致
  的嵌入式 `recordName`。多行、click-through 或任一 generation/contract 错配均不执行
  fast path。
- consumer 在 BrowserAgent bootstrap 和事件观察器安装完成后、第一次模型请求前运行。
  它只接受 worker 已机械绑定的唯一 pinned page，先以 `Page.getState` 与批量
  `DOM.getText(body)` 验证页面未暂停且至少一个声明式 region marker 存在；offloaded
  marker 文件只允许在当前 task 目录内有界读取。
- 嵌套集合的 `baseRowRef` 由 harness 使用 `_batch_source_receipt.artifactPath` 与源
  row index 机械重建，模型不能提供路径或顶层业务值。随后直接调用生产
  `collect_items`，并沿用 content-completeness observation 与 artifact validator。
- 只有 `target_reached` / 带可靠证据的 `explicitly_exhausted`、无
  `contractWarning`、嵌入式 `recordExtraction=done`、artifact validation `done` 且无
  failures/pending 时才跳过模型并机械结束。marker 缺失、页面暂停、能力缺失、
  stalled/blocked、契约告警或验证失败均只生成 slow-path handoff；partial sample 不会被
  认证或另行落盘。
- 本阶段仍**不执行导航、reveal、页面选择或多行循环**，因此不是 Micro-Workflow，也
  不是 task-level batch runner。6B-B.2 必须先从统一自主 trace 中取得稳定的 route /
  reveal / return 动作证据，再扩展 receipt coverage。

新增回归覆盖默认关闭、pre-model 成功不调用 provider、checkpoint/generation/单行
binding、嵌套 `baseRowRef` 重建、marker 缺失和 stalled 降级；全量回归
**1504 passed / 6 skipped / 2 warnings**。

同日使用现有 fleet `38585657…` / pinned detail page `21548423…` 完成两条 live
canary，期间没有创建、关闭或替换 Fleet/Page：

- clean success：
  `worktree/stage6bb_guided_live_canary_20260724/summary.json`。pre-model consumer
  返回 `handled=true`、`executionMode=guided_fast_path`、`target_reached`、20/20；
  嵌套 artifact
  `artifacts/extractions/stage6bb_guided_reviews-bd4424a7.json` 的
  `recordExtraction` 与 artifact validation 均为 `done`、failures 为空。
- forced stall：
  `worktree/stage6bb_guided_stall_canary_20260724/summary.json`。不存在的 selector
  返回 `handled=false`、`reason=collection_not_cleanly_complete`、
  `materialization_stalled`、rowCount=0；目录内只有 canary 自己登记的 trusted
  base-row 输入，没有生成目标 `stage6bb_guided_stall` artifact。

因此 **6B-B.1 的代码与 live success/fallback 出口均通过**，但开关继续默认关闭。
它只证明已 materialized pinned page 的单行 collection prefix；不能外推为
route/reveal/return 或多行批处理已通过。

**阶段 6B-B.1 review 收口（2026-07-24）**：

- guided receipt 不再只依赖 pinned page 与 region marker。绑定端从唯一选中的
  `batch_rows` 源行递归提取绝对 HTTP(S) URL（不假设 `url`/`detailUrl` 等字段名），
  canonicalize 后写入 harness-private `sourcePageUrls`；执行端在任何 DOM/collection
  动作前要求 `Page.getState.url` 与其中一个 URL 精确匹配。源行没有 URL、当前页面 URL
  不可用或不匹配时一律回退 slow path，不能用“另一个已就绪详情页”替换目标行。
- guided trace 在写入时即调用 `trace_params_for_fast_path`，只保留稳定 allowlist；
  candidate 编译端再次执行同一清洗作为防御。`pageId`、`baseRowRef` 路径/行号及其他
  page handle 不再进入可编译 trace。
- pre-model consumer 对 browser RPC、生产 `collect_items` 和 completeness observation
  设置统一异常边界；除任务取消外的异常只记录 `guided_fast_path.exception` 并交还普通
  BrowserAgent，不会令 worker 因优化路径故障而退出。
- ready marker 只读取 `DOM.getText` 的 `info.textContent`。task-local offload 必须成功
  JSON 解析后才递归读取 `textContent`；offload 路径复用生产
  `extract_offloaded_paths()`，从 `response.data.items` / `response.data` 内真实的
  `_offloaded=true` stub 递归取得，而不是假设顶层存在 `savedPath`。purpose、selector、
  reason 等 envelope metadata 不能命中 marker。HITL 判断复用 composite 的结构化
  interrupt 解析，并显式检查
  `response.data.hitl.isPaused`，不再依赖 JSON 字符串搜索。
- guided 回退后由模型完成的 worker 继续标记为 `browser_slow_path`，另记
  `guidedFastPathAttempted=true` 与 `guidedFastPathFallbackReason`；只有真正 handled
  的前缀才标 `executionMode=guided_fast_path`。完成状态统一复用生产
  `COLLECTION_COMPLETE_STATES`，不再维护第三份常量。

新增回归覆盖 raw trace 二次清洗、来源 URL 缺失/错页、换行 marker、metadata 假命中、
task-local offload、越界 offload 路径、`currentUrl` 兼容、结构化暂停、内部异常降级和
执行模式遥测。offload 回归不再手写顶层路径，而由生产
`offload_large_response_fields()` 生成真实嵌套 stub；live canary 也已移除 identity
offload 桩，改接生产 offload 实现。全量回归
**1509 passed / 6 skipped / 2 warnings**。本轮准备复跑 live clean/stall canary 时，
`Fleet.list` 返回 0 个 active fleet，因此没有擅自创建新 Fleet。上文已经通过的两条
生产 live canary 证据仍保留；本轮安全收口后的再次 live 复验需等待现有 Fleet 可用。

**阶段 7 Workflow auth-generation fencing 实施结果（2026-07-24）**：

- `FleetAuthBarrier` 新增非阻塞、原子化的 `workflow_fence_before/after`。opaque
  `Workflow.execute` 启动前必须处于开放且与 worker 已感知 generation 一致的 auth
  epoch；barrier 已关闭时，即使调用者正是 resolver，也不能启动业务 Workflow。
- model-authored `Workflow.execute`、frozen skill 的单行/batch/structured-output、
  `execute_selected_skill`、ephemeral row，以及共享 worker 上的 auto-heal canary
  全部接入同一 fence。CLI/skill-create 自建隔离 Fleet 的离线 canary 没有共享
  `FleetAuthBarrier`，保持 unmanaged 语义。
- 在途 Workflow 允许自然返回，但返回后再次原子读取 generation/barrier。epoch 漂移或
  gate 关闭时，返回变量被替换为 `workflow_row_quarantined`，不得进入
  `build_extraction_row`、artifact persistence 或 skill health 失败计数。
- frozen 与 ephemeral 行循环在 generation 已推进且 gate 已开放时，复用普通 guarded
  browser-call 路径执行 `Page.getState + DOM.getAXTree`，然后只重跑当前 row 一次。
  第二次漂移、重新感知失败或 gate 仍关闭时 fail-closed handoff；此前已经完成的 rows
  保留，下一 row 不会启动。没有使用 `Hitl.requestPause` 伪造通用 Workflow pause。
- 新增 `workflow.auth_fence.before`、`workflow.auth_generation_changed`、
  `workflow.row_quarantined` 与 `workflow.row_replayed_after_reperception` 遥测。
  model-authored raw `Workflow.execute` 在 browser-call 层发出同口径的 preflight
  与 generation-change 事件；内部 ephemeral 调用由外层 fence wrapper 负责事件，
  不在底层重复计数。所有受管路径统一携带 `source`、`method`、`runId` 与
  `workerId` 公共字段；raw preflight 尚无引擎 run id 时明确记录 `runId=null`，
  不伪造关联标识。

回归覆盖 gate 关闭时零 Workflow RPC、在途 generation 漂移隔离、重新感知后单次重跑、
第二次漂移 fail-closed、跨行 barrier 关闭时保留既有 rows、frozen handoff 不扣 health、
ephemeral/frozen 对称行为、model-authored stale result 去变量化，以及 auto-heal canary
的同源 preflight，以及 raw/internal 遥测归属与去重。ephemeral 组合回归真实串联
内层 raw 隔离与外层 fence/replay，验证一次 generation 漂移只产生一组
generation-change、quarantine 与 replay 事件。全量回归
**1521 passed / 6 skipped / 2 warnings**。

**阶段 8 PlanValidatorAgent + plan 版本化实施结果（2026-07-24）**：

- `task_plan.json` 继续作为当前计划兼容入口；每个已接受版本同时原子写入
  `task_plan_history/plan.NNNN.json`。版本 envelope 保留 normalized plan、原始用户
  任务 hash、`replan_reason`、前版编号、确定性结构 diff、candidate hash 与独立
  validator verdict。`task_state.json` 同步记录 `plan_version` / `plan_hash` /
  `plan_history`，replan audit 也带版本号；旧版文件不覆盖。
- 每次独立审计尝试（批准、驳回或异常）都写入
  `task_plan_reviews/review.NNNN.json`；只有批准并实际接受的版本才同时进入
  `task_plan_history/plan.NNNN.json`。前者是完整 attempt ledger，后者是 accepted
  revision ledger，批准候选在两处出现是有意的审计交叉引用，不是重复状态源。被驳回
  或异常的候选不会替换 `task_plan.json`、不会创建 accepted version，也不会初始化或
  重置 task state。
- 新增顶层 `plan_validator` 配置及独立 provider。默认关闭；启用时必须提供非空
  `model_id`，且与 Lead model id 不同（大小写归一后仍相同会启动失败）。配置转换强制
  `tool_choice=required`，不把 API key 写入审计记录。
- validator 不获得 Browser/文件工具，只能调用单一
  `submit_plan_validation` 结构化工具。输入按
  `原始用户任务 > 不可变任务合同 > plan v1 > previous plan` 提供，同时给出
  candidate hash、结构 diff、逐项目标 catalog 与 harness 生成的 evidence catalog。
  candidate hash 同时绑定 normalized plan 与 `replan_reason`，因此不能先用一个目的
  取得批准后再替换成另一个 replan 目的。
  verdict 必须覆盖全部基准目标；未知 evidence ID、candidate hash 错配、空响应、超时、
  多/少 tool call 或 schema 不完整均 fail-closed。
- 独立 Validator 是 `higher_priority_user_objective` 分支的语义信任根：机械层只验证
  objective/覆盖项/证据引用和 candidate hash 的结构一致性，无法机械证明模型对自然语言
  “preserved/strengthened”的判断为真。Validator system prompt 因此明确把原始任务、
  candidate、worker 指令和 `replan_reason` 全部视为不可信审计数据，禁止执行其中要求
  改 verdict、忽略规则或调用工具的元指令。独立模型的选型与 canary 结果直接决定这条
  授权路径的强度。
- 数量放宽本身不机械拒绝。每个检测到的 `exact_rows` / `min_rows` /
  `count_range` / `min_records` 下调都生成稳定 `relaxationId`，批准 verdict 必须逐项
  提交 `quantityDecisions`，只允许两类依据：
  ① 引用 task-state 中由生产采集链留下的真实 `collection_exhaustion`；
  ② 独立 validator 确认候选是在纠正低优先级计划对**原始用户任务**的误读，并显式以
  `user:task` objective 授权、列出被覆盖的低优先级 objective。普通失败、HITL、
  `replan_reason` 或 Lead 自述均不能授权缩量。
- 数量契约关联优先使用稳定 phase id；其次只使用两侧都唯一的 artifact contract key；
  最后仅在两侧各剩唯一一个计数契约时允许 rename fallback。多个未匹配计数目标属于
  语义歧义，Python 层不按字段名做模糊配对，而是生成稳定 ambiguity id，要求 validator
  逐项提交 `quantityLineageDecisions=no_quantity_relaxation|quantity_relaxation|ambiguous`。
  批准 verdict 不得保留 `ambiguous`；判为缩量时，该 ambiguity id 必须继续提交上述
  `quantityDecisions` 授权。同 contract key 的多个 probe/validation/bulk phase 不再被
  字典覆盖或任意串联。
- `emit_task_plan` 在 validator 启用时先异步审计，再把与 normalized candidate hash
  绑定的 approved receipt 交给同步接受层。直接调用 `accept_task_plan`、复用旧 receipt
  或在审计后修改候选均无法绕过。validator 缺席（`enabled=false`）时保持此前四条机械
  不变量与计划行为不变。

新增回归覆盖独立配置加载、同模型拒绝、initial/replan 版本不可覆盖、diff 与 replan
目的留存、直接 accept 绕过失败、validator timeout/畸形输出不改当前计划、语义驳回
不落 accepted version、数量放宽有/无机械穷尽证据、原始用户目标授权纠正、
`replan_reason` 伪授权拒绝、artifact/字段/phase 改名后的唯一 lineage 关联及多契约
歧义不猜测，以及任务 `18754ae9...` 的
6→11→2 真实计划回放（最终版的 HITL 否定与 reviews blocker 指令仍先被机械层拒绝）。
另用不含字面禁令的同义降级候选验证语义审计接线。全量回归
**1538 passed / 6 skipped / 2 collection warnings**。

启用或更换独立 Validator 模型前，必须运行真实语义 canary（调用模型 API，不启动
浏览器、不写任务状态）：

```bash
conda run -n agent python \
  docs/plan-validator/scripts/plan_validator_semantic_canary.py \
  --config config.json
```

四个固定场景分别验证：伪造“用户同意缩量”必须拒绝、plan v1 误读原始数量时允许纠正、
机械穷尽允许缩量，以及 candidate `worker_task` 中的 verdict prompt injection 必须拒绝。
正例还会核对 `quantityDecisions.basis`，不能以“批准了”掩盖走错授权分支。任一场景不符
脚本以非零状态退出，输出不包含 API key。

**独立模型 live 验收（2026-07-25）**：`deepseek-v4-pro` 通过 Anthropic-compatible
provider 完成上述四场景，最终 **4/4 passed**。首轮正向场景暴露了两个合同遵循问题：
模型会把 diff/relaxation id 误填进 `evidenceIds`，并可能把未判弱化的目标列为
overridden。修复没有放宽 fail-closed 验证，而是把所有 `evidenceIds` 的工具 schema
机械限定到当前 `evidenceCatalog`（无 catalog 时只能为空），为每个数量放宽提供
`affectedObjectiveIds`，并要求这些目标全部进入 override 且对应 check 必须是
weakened/removed。修复后，原始用户纠偏以
`higher_priority_user_objective` 批准，机械穷尽以 `collection_exhaustion` 批准；
伪授权与 candidate prompt injection 均未获批准。

**阶段 9 P2 可观测性实施结果（2026-07-24）**：

- Spawner 在 worker 启动前注入不可由模型控制的 `workerId` / `slotId` / `phaseId`；
  BrowserAgent 通过统一写入入口让全部 `agent.*` 事件同时携带这三项及 `agentId`。
  `run.jsonl` 不再只能靠并发 worker 各自重复的本地 step 猜归属，最终 context snapshot
  也保留同一身份。
- 终端的 BrowserAgent step/model/step-cap reminder 行显示真实 worker 与 slot，例如
  `BrowserAgent worker-A / slot browser-002`；并发 worker 即使同为第 28 步也不会串线。
- 模型同一轮产生多个 tool call 时，任何直接 `Runtime.evaluate` 均在 RPC 前返回
  `runtime_evaluate_requires_single_call_turn`，`tool_was_executed=false`。同批结构化
  调用继续遵守已有 state-boundary 顺序；模型必须先读取它们的结果，下一轮才能单独申请
  Runtime.evaluate。单 tool-call 的合法 Runtime.evaluate 保持可执行。
- `execute_browser_workflow.steps` 从任意 object 改为递归 JSON Schema，明确
  action/listen/if/loop/transform、condition/group、transform ops、嵌套分支、listen
  事件白名单和最小示例；生产 `validate_workflow_params` 仍是执行前第二道权威校验。
- Lead/worker 输出 token 与压缩阈值未在本阶段修改：当前项目按既有决定由用户配置输出
  预算，且证据不支持降低 compaction 阈值。
- 2026-07-30 收口：`RunLogger.bind_context()` 为每个 BrowserAgent 提供不可变
  per-worker logger view，统一给全部 worker 事件注入
  `workerId/slotId/agentId/phaseId`。调用方同名字段不能覆盖 coordinator 身份；
  Lead/task 继续使用未绑定 logger，不会被误归属。共享文件、event sink、task path
  与 usage aggregator 仍由底层 task logger 统一持有。

新增回归覆盖 Spawner 身份注入、并发终端回放、所有 `agent.*` 身份字段、Runtime 混批
零 RPC、单独 Runtime 放行及五类递归 Workflow step schema。全量回归
**1542 passed / 6 skipped / 2 collection warnings**。

**阶段 10 Fleet Click Gate 实施结果（2026-07-30）**：

- 导航 attribution shadow/matcher 已删除，所有顶层 `Input.click` 与递归含 click 的
  `Workflow.execute` 进入同进程、同 Fleet 的 Enforced click gate；此外所有 opaque
  `Workflow.execute` 在整个 RPC 期间持有 Fleet interaction lease，阻止其它 worker
  操作该 Fleet 的页面，但不阻止只读 `Page.list`。租约按 task 而非仅按 worker 标识，
  因此 owner 的内联 HITL 复核可以重入，同 worker 的并发 Workflow 仍保持互斥；等待
  超时返回可重试的 `fleet_busy`。`Page.create` 在同步认领返回 pageId 前不释放创建事务。
- 顶层 click 以 raw `Page.list` baseline/final inventory 和
  `openedBy/openerPageId/sourceUrl` 生成保守 `harnessClickGate` receipt；事件仅负责
  提前唤醒。迟到、多 popup 与冲突候选进入 quarantine，不猜测归属。
- opaque Workflow 仅获得互斥，不伪造内部 step 归因；HITL 由常驻事件观察器、
  auth barrier 与 generation fence 共同收口。
- 双 Agent live canary 覆盖并发 popup 串行、confirmed landing、Workflow HITL、
  barrier generation 与排队 click 的 `fleet_reperception_required`，结果
  `passed=true`，证据位于
  `tests/20260730/fleet-click-gate-live-canary/summary.json`。
- 同日删除未接权威消费端的 Artifact completeness 实验子系统；该删除不改变现有
  artifact validation、内容区域门控、点击 gate 或 fast path 结果。
- Stage 9.1、Stage 10 与 Artifact shadow 删除收口后的全量回归：
  **1613 passed / 6 skipped / 367 warnings / 62 subtests passed**。warnings 为既有
  pytest collection 提示和 `datetime.utcnow()` deprecation；无测试失败。

| 阶段 | 内容 | 验证方式 |
|---|---|---|
| **0** | **P1-3c `collect_items` 最小补丁**（`collectionState` / 两类穷尽证据 / 未完成不落盘 / `baseRowRef` / `maxDurationMs` / `reality_check` 同源修正） | 14 条单测全绿；错容器场景**必须**产出 `materialization_stalled` 且无 artifact |
| 1 | P0-3 截图落盘 | **已完成**：淘宝详情页 `scrollHeight=25,293px`，生成 40,501,427-byte file-mode 整页 PNG；截图后连接仍可用，无 1009 |
| 2 | P0-1 四条机械不变量 | **已完成**：证据 A/B/C 的真实 plan 与 artifact 回放全部被拒；精确 token 与全-blocker 多元素数组在生成文件前即被拒，旧 artifact 不能进入 `validExtractionArtifacts`/`validated_done`；反误报覆盖混合真实评论、字段级 provenance，以及普通评论提及登录/验证码 |
| 3 | P1-1 `min_records` 三分判据 | **已完成**：38 条历史 marker-only decision 全部判 `shell_seen`；20 条达标及“仅 8 条但有可靠 scroll/load-more 穷尽证据”判 `content_materialized`；stalled/blocked/歧义绑定/epoch 重置均有回归测试 |
| 4a | P1-3 ③ **契约预检**（入口比对 fields） | **已完成**：扁平/嵌套错误映射均在首个 browser RPC 前 rejected，返回逐项 reason 与修正建议；正确映射仍保留事后 selector/值域校验 |
| 4b | P1-3 预算/形状 + 抽取阶梯补档 + 删手搓段落 + `dismiss_ladder` 策略改 `preferred_tools` | **已完成**：确定性生产 canary 与自主 worker trace 均得到 20/20、`target_reached`、clean artifact；自主 trace 到第 12 步才进入 composite，是 6B-B 的优化基线 |
| 5a | P1-2 路径 2 导航恢复 | **代码与 live 导航子链已通过**：淘宝搜索列表真实 anchor 以 new-tab 打开完整详情主 DOM，并成功 `Page.switchTo(sourcePageId)` 返回仍含链接的列表页；该证据不等于评论 materialization 成功 |
| 5b | P1-2 评论 materialization 与返回闭环 | **已完成**：route-sensitive new-tab 详情的抽屉由连续三轮 skeleton 在第 4 轮物化 20 条，生产 `collect_items` 随后 20/20 clean 落盘，来源列表恢复成功；新页面归因与返回责任由 Stage 10 的 confirmed click receipt 接管 |
| — | **里程碑：单 phase 真实完整评论 + 升级链路可复现** | 这是后续所有机制的验证基准 |
| 6A | P1-4 Lead 规划契约（execution role + fragmentation detection + validated artifact 自动构造 `batch_rows`） | **代码已完成**：历史三单行详情 plan 被机械拒绝；规范三行 plan 为 probe 1 → validation 2；批次来源不在 validated ledger、选择为空、越过上限/隔离边界均在 worker 创建前拒绝 |
| 6B-A | replan checkpoint + 非执行 `fastPathReceiptCandidate` | **代码已完成**：候选只来自 validated trace；checkpoint 按 cohortKey 分账并绑定 source generation/目标行全集；多 cohort replan 必须精确确认并逐 phase 绑定全部 active IDs；候选明确不可执行 |
| 6B-B | live 验证后的旧确定性前缀 | **历史验证完成，生产实现已删除**：其 canary 证明 composite 前缀可行，但单行 guided consumer 与新 Hybrid Skill 重复；当前由 `workflow_execution_enabled=false` 统一回退 BrowserAgent 慢路径 |
| 7 | P0-2 barrier 语义边界 | **已完成**：无主恢复入口由 `Page.create` 在 RPC 前原子认领唯一 resolver，失败 relinquish 但不开闸；所有共享 worker Workflow 启动前经过同一 barrier，in-flight row 以 auth generation 前后比对隔离，重新感知后只重跑当前 row 一次，旧 rows 保留且 stale variables 不落盘 |
| 8 | P0-1 PlanValidatorAgent + plan 版本化 | **代码与独立模型 live 已通过**：accepted/rejected revision 分账、candidate-hash 绑定、结构化 verdict 与 fail-closed 已接通；三版真实计划及数量放宽证据矩阵回归通过；独立 `deepseek-v4-pro` 四场景语义 canary 4/4 passed |
| 9 | P2 可观测性 | **已完成**：并发交错事件按 worker/slot 正确归属；bound logger context 覆盖全部 worker 事件，Lead/task 事件保持 task scope；Runtime.evaluate 混批在 RPC 前拒绝、单调用保留；临时 Workflow 五类 step schema 已公开 |
| 10 | Enforced Fleet Click Gate | **代码与 live 已完成**：同进程同 Fleet 的顶层 click/含 click Workflow 串行；Page.list 保守对账、迟到 popup quarantine、HITL/barrier generation 联动均已验证；旧导航 shadow/matcher 不再存在 |
| 可选 | P1-3b `bounded_loop` 泛化 | 在里程碑之后做；`collect_items` 作为 preset 回归，行为与数字须与里程碑一致 |
| 可选 | P2-alt 跨 phase cohort | 仅在 P1-4 之后仍存在正当拆分场景时才做 |

### 2026-08-03 运行完整性收尾批次

- 生命周期与进度闸采用一次性 `pageId + lifecycle generation + required tool`
  recovery credit；只放行被 lifecycle 明确要求的 `Page.getState` /
  `DOM.getAXTree`，消费即失效，普通动作不能借此绕过进度上限。
- Anthropic 与 OpenAI-compatible 的 tool JSON 流式解码统一为同一恢复语义：
  普通三槽共享 timeout / connection / decode 预算；若终局仍为 decode，额外保留
  **一个专用 non-stream fallback 槽**（总请求上限 4），该槽不允许被普通超时消耗。
  仍失败才产出 `provider_protocol_failure`，损坏的 JSON 不再伪装成模型工具参数。
- `content_binding={"regionId": ...}` 是 Harness-only 的临时结构化证据，只能短暂
  延后 route recovery；同 page epoch/region 不可续期，并在两次后续观测、导航或
  `record_extraction` 后失效。最终区域 credit 只来自通过 artifact/row contract
  validators 的非空声明字段，且按 phase artifact 分账。校验顺序固定为
  contract validation → region credit → completeness validation → final validation，
  避免完整性门依赖自己的最终 verdict。最终 credit 以 artifact 行的 `pageUrl`
  绑定到 Tracker 已观测的规范 URL；缺 `pageUrl` 不发 credit，一页的合法行不能清掉
  另一页的 missing regions。
- `ABCPTransportError` 保留 JSON-RPC `rpc_code/rpc_method/rpc_data`。真实
  `Download.start -32014` 在抛异常的位置被局部接管，不再跳过对账死代码，也不再扫描
  URL/文件名等任意文本猜错误码。Harness 立即查询一次 `Download.list`，未观测时约
  4 秒后再查一次；只有 URL + savePath 精确匹配才认定 completed/active/failed。
  两次仍无法验证则记录 `timeout_unverified` 与可能副作用、机械禁止同 URL/path 盲重发，
  并指导取得最终直链后做一次有界重试；不按时间邻近认领重定向孤儿文件。
  completed 可直接复用；downloading/paused 必须先按 downloadId 刷新，避免陈旧 active
  receipt 永久锁死重试。
  上游 ABCP 的根修复是从 Electron `DownloadItem.getURLChain()` 整条重定向链提取
  pending nonce，并在下载记录/未来事件中保留原始 `requestedUrl`；当前仓库忽略的
  ABCP 源码不与本 Harness 批次混改。即使未来接入 Download 事件，Harness 完成等待
  仍保留超时上界，超时后对账而不是直接重试。
- provider tool JSON 在普通三槽后仍解码失败时使用保留的第 4 个 non-stream 槽；终局
  protocol failure 在 BrowserAgent 中单独归类，不再伪装成空响应，并累计
  `stream_decode_retries` 失败路径遥测。
- Lead 终态或异常中断都附带/记录不可由模型覆盖的 `completionReceipt`，从 task state、当前 artifact
  generation、去重 download operation receipt 与 challenge/HITL ledger 机械生成。
- Fleet 启动采用 **assignment 后、Worker 构造前**的按需 readiness barrier：先预置
  `Fleet.ready` 监听，再调用 owner-slot 的 `Fleet.status`；事件只负责唤醒，必须由后续
  `Fleet.status` 再确认。session restore 完成没有对应 ready 事件，因此首次状态失败后
  事件等待最多使用 5 秒且不超过剩余预算的一半；未收到事件仍保留一次
  `status_retry`，全程最多两次状态 RPC。`fleet_readiness_wait_seconds` 是信号等待与
  发起终局探测的软预算，不是总墙钟上限；为避免取消在途 WebSocket RPC 污染共享连接，
  实际耗时可能达到该预算加两次 ABCP 单调用/restore 上限，并以 receipt 的 `elapsedMs`
  如实记录。相同 Fleet 的并发 phase 共用 single-flight probe，失败进入既有
  spawn-acquisition cooldown 且不创建 BrowserAgent。启动前 inventory 只做 `Fleet.list`，
  Page.list/Page.getState 延后到 Fleet ready 后并仅检查选中的 Fleet，避免无关坏 Fleet
  拖累整个 slot。`mark_phase_running` 暂继续兼作并发 reservation；真实 Worker 创建和
  `fleetReadiness` receipt 均发生在 barrier 之后。
- `-32012` / `-32005` 的平台瞬时故障分类不在本批实现范围内，等待 ABCP 修复。

**2026-08-03 Fleet readiness live canary：**证据保存在
`tests/20260803/fleet-readiness-canary/`。热 Fleet
`873abbb6-4324-4c84-93ee-998cad4e4a50` 由一次 `Fleet.status` 验证为
`verifiedBy=status`，事件顺序为 `fleet.readiness.ready → spawner.browser.spawned →
worker`，全程没有 `Fleet.create` 或 Page RPC。prepared Fleet
`48f0864a-79fb-4ef6-acb2-732e5e1e1818` 的两次状态探测均返回平台
`-32011 Fleet open failed`，Harness 在 BrowserAgent 创建前 fail closed，同样没有
Page RPC；因此终局 `status_retry` 分支已在 live 中执行，但“restore 后成功”的 live
出口仍只由确定性回放覆盖。另发现一条 Harness 收口欠账：该
`FleetReadinessError` 虽受既有两次 acquisition 上限约束，首次回执仍为
`retryAfterMs=0`，因为 cooldown 当时只识别文本形态的 `-32012 Fleet open timeout`。
现已由 `FleetReadinessError.requires_spawn_acquisition_cooldown` 结构化接入既有
acquisition ledger：首次失败返回 30 秒 cooldown，并明确要求重试相同 phase ID；没有
新增重试账本，也没有扩张 `-32005/-32012` 平台故障分类。

**关于顺序的三点说明：**

- P0-3 排第一不是因为最重要，而是因为它会随机打断其他所有验证。
- **P1-4 提前到第 6 位**（原为第 7）。它是快路径失效的根因，且不需要新代码，做完立刻能看到成本结构变化。
- PlanValidatorAgent 排在后面不是因为不重要（它是 P0），而是因为它需要 plan 版本化提供基准，且它的效果需要前面的行级验收先立起来才能判断真假。四条机械不变量先行，已经能挡住本次事故的直接路径。

---

## 7. 待确认问题

1. `collect_items` 时间预算提到多少合适？需要真站测一轮完整 materialization 的耗时。
2. `end_of_collection` 的机械判据用几轮无增长？过小会把慢加载误判为到底，过大浪费预算。建议先按 `stabilityThreshold` 现有语义走，实测后调。
3. P1-4 落实后，是否还有真正需要跨 phase cohort 的场景？若没有，P2-alt 整块可以不做。

**已决策（不再讨论）：**

- ✅ PlanValidatorAgent 用**独立模型**，`config.json` 新增 `plan_validator` 配置块
- ✅ barrier 读/写**一刀切固定名单**，不按 task_type 细化，未列出方法默认拦截
- ✅ cohort 经验**不考虑**提升为永久 skill，永久化仍走 `/skill-create` → workflow / guidance 现有路径

---

## 附：本方案相对早期讨论的修正记录

| 早期判断 | 修正 | 依据 |
|---|---|---|
| "打开 flag + 改 Lead 规划 = rows 1–9 自动零 LLM" | **撤回（承重错误）**。改为两级快路径，近期只承诺 guided composite path | `_DISALLOWED_TRACE_TYPES` 含 `collect_items` / `local_fs_*` / `dismiss_overlay` 等；本次 9 个 worker 全部用过 `local_fs_*`，**没有一条 trace 有可能编译成功** |
| "计数走 `semantic_index.digest_subtree`，不走 JS" | **撤回**。`collect_items` 实际走受控内部只读 oracle（`Runtime.evaluate` + title side-channel） | `verifiers.py:88` `build_read_only_oracle`；`digest_subtree` 只产摘要与候选，无完整记录集与跨轮唯一键 |
| "连续 N 轮无增长 = `end_of_collection`" | **撤回**。`stalled` 与 `explicitly_exhausted` 分离为四态 | 滚错容器同样产生无增长，会把 2 条评论认证为合法完成 |
| "入口比对 `fields` 键 vs `expected_artifact.fields`" | **修正**。需嵌套集合合同（`collectionField` / `itemFields` / `baseRow`） | `reviewText` 是 `reviews[]` 子字段，本就不应等于外层字段 |
| "anchor-click 编译约 30 行" | **撤回**。P1-2 拆两阶段，编译降为后续实验项 | 还需重绑、page 差集、未知 detail pageId、双返回策略、顶层 pageId 切换 |
| "在途 workflow 接 `skill/control.py` 页面级暂停即可" | **撤回**。改 generation fencing | 页面级暂停以 workflow 自带 `listen Hitl.resumed` 为前提；对正常页面用 HITL 会制造额外人工暂停 |
| "无主态对所有 worker 放行 `Page.create`" | **修正**。先原子认领 resolver，再只对 resolver 放行 | 否则多 worker 各建空白页竞争 claim |
| "P1-4 无新代码" | **撤回**。至少需自动构造 `batch_rows` + replan checkpoint + validator 驳回碎片化 | prompt 已写 `>=3 attach batch_rows`，本次仍拆 13 phase——同一杠杆已失效过 |
| "20 → best-effort 是静默降级" | **撤回**。数量放宽合法，判据是有无穷尽证据 | 商品评论本就可能不足 20 条 |
| "全程走 `Page.navigate` 是缺陷" | **重述**。直接导航是默认路径，点击是恢复路径；缺陷在升级触发器（证据 I）未生效 | 两条路径各有适用场景 |
| "taobao/tmall 是两套模板，需按域分 variant" | **撤回**。实测同模板同类名 | 8 个观测文件跨两域共享 `comments--ChxC7GEN` / `Comment--H5QmJwe9` |
| "需要跨 phase cohort 机制" | **降级为可选**。根因是 Lead 规划碎片化，机制本身已存在 | `_maybe_run_ephemeral_batch_after_first_row` 已实现，因每 phase 仅 1 行而无法触发 |
| "全任务 5 次 max_tokens" | **更正为 3 次** | 5 行日志对应 3 次事故 |
| "需要独立 Micro-Workflow 执行器" | **改为 Hybrid Skill** | 不新增第二执行引擎；保留 `SKILL.md` guidance、未来 native workflow 与 Harness composite host step 的分层编排 |
| "`Workflow.execute` 唯一价值是节省 LLM 轮次" | **弱化** | 它还提供引擎侧 `listen`、分支重试与更少 IPC 往返；结论不变但表述已更正（§1） |
| "四维全同 + 均单行 → 机械合并" | **撤回为通用规则** | 会压平本方案自己主张的 probe→validation→bulk；需加 `execution_role` + 批次策略，缺失时只产 `fragmentation_candidate` |
| "probe/validation/bulk 上游依赖完全相同" | **自相矛盾，已更正** | 依赖相同则可并发启动，不构成升级链；应共享上游来源但 `depends_on` 逐级串接 |
| "`reality_check` 把 stalled 盖成 satisfied，是假完成通道的另一端" | **撤回** | `classify_target_yield` 契约为 `True=shortfall`，该分支 `return True` 即判 shortfall。真实缺陷是命名误导 + 无法表达 `explicitly_exhausted` + **误触发 fullPage reality check**（与证据 H 同链） |
| "阶段 0 拿不到真实 20 条评论" | **说过头，已分档** | 不接 Tracker 影响的是自动恢复链；页面就绪时 `collect_items` 本身应能取满 20 条 |
