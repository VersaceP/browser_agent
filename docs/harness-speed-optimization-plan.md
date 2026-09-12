# Harness 提速方案：分层落地计划

- 日期：2026-09-11（当日修订）
- 依据：`docs/replay-baselines/baseline-2026-09-11.json` 固定的 14 个 taskId
  （63 个 browser worker、2832 个 browser-agent 模型回合、2002 次浏览器动作）。
  **按 taskId 选取**——"最新 14 次"的滑动窗口已经复现不出这个样本。
  复现脚本：`docs/replay-baselines/analyze_turn_breakdown.py`
- 状态（2026-09-12 更新）：

  | 项 | 状态 | 提交 |
  |---|---|---|
  | L0.1 offload 阈值 8000→50000 | ✅ 已改，单站点有效，跨站点未证 | `85f0ac6` |
  | L0.1 按结果类型分级 | ⏸ 有触发条件才做，见下 | — |
  | L0.2 降 worker 推理预算 | ❌ 实测否决 | — |
  | L0.3 lead max_tokens | ❌ 已撤回 | — |
  | L1.1 `--agent-mode browser` | ✅ 已完成 | `11ce1e5` |
  | L1.2 页面记录 + 阶段自动续跑 | ✅ 已完成（范围已修正，见下） | `5cc850b` `a9522ad` |
  | L1.3 动作回执带局部 AXTree diff | ⬜ 待做，与段化重叠，需重测 | — |
  | L1.4 并行 tool_calls | ⏸ 实测无靶子（82.4% 回合单调用） | — |
  | L2.1 Workflow 执行通道 | ⚠️ 部分完成；真实任务 A/B 已补 | `5d4d8f9` `6467e9f` |
  | L2.2 删响应形状匹配回退 | ✅ 已完成 | `980f7b1` |
  | L2.3 平台侧绑定 Action | ⬜ 待平台配合 | — |
  | L3 换基模 + 删 VL | ⬜ 排最后 | — |
  | **新增** VL reality check 异步化 | ✅ 已完成 | `d41a510` |
  | **新增** 段化率回归 | ✅ 已定性，结论反转（段少而大更快） | — |

## 结论的证据等级

下表的数字是观测事实；由它们推出的**因果**结论是待验证假设，不是定论。
已知混杂因素（详见 baseline json 的 `confounders`）：

1. 样本混了两个 worker 模型（glm-5.2 n=2222、deepseek-v4-flash n=180），
   且 output token、任务难度、Agent 角色互相纠缠，相关不等于因果。
2. 「一个 phase 由多个 worker 服务 33.3%」是**spawn 计数**，没有区分
   step cap 耗尽、计划并发、失败重派和恢复。要断言是 step cap 导致，
   必须关联 `reachedStepCap`、spawn reason 与页面连续性丢失。
3. 「上下文影响极小」后来被自己的数据推翻：见 L0.1 的非线性修正。

## 为什么：时间花在哪

| 实测指标 | 数值 |
|---|---|
| 模型推理占 worker 生命周期 | 中位数 **83.0%**（p25 68.9%，p75 87.8%） |
| `browser_call` 端到端 | p50 **0.36s**（浏览器不是瓶颈） |
| 每个模型回合平均工具调用数 | **1.21**（83.3% 的回合只发 1 个） |
| 完全花在零延迟本地工具的回合 | **35.3%**（1000/2832） |
| 一个 phase 被多于一个 worker 服务 | **33.3%**（14/42，最多一个 phase 用了 4 个 worker） |
| `DOM.getAXTree` 被 offload 的比例 | **95.7%**（553/578） |

模型延迟与 output token 强相关（因果未证），上下文影响**非线性**：

| output token | 回合数 | 延迟 p50 |
|---|---|---|
| <300 | 959 (34%) | 5.4s |
| 300–800 | 1146 (40%) | 8.3s |
| 800–2000 | 525 (19%) | 18.3s |
| >2000 | 202 (7%) | 41.7s |

> out<300 的 959 个回合里：低上下文三分之一 ctx 中位 33161 → 4.8s；高上下文三分之一
> ctx 中位 108680 → 6.2s（+29%）。但把单轮探针一起看就不成立了：同一个
> deepseek-v4-flash，288 token 输入是 **1.8s**，62K 上下文的真实 run 是 **7.5s**。
> **288→33K 是 +167%，33K→108K 才 +29%——影响非线性，前 33K 最陡。**

**占回合数 25.7% 的大输出回合，吃掉 55% 的总模型时间。**

工作假设（待 A/B 验证）：主要杠杆是**减少回合数**；上下文在 33K 以上边际影响小，
但跨过低区间的代价不可忽略，所以"把大树塞进上下文换回合"必须实测净收益。

---

## L0：配置层（0 代码，当天见效）

### L0.1 取消 AXTree 的 offload ✅ 已改（`85f0ac6`），跨站点未证

`runtime_config.py:36` `DEFAULT_OFFLOAD_THRESHOLD_BYTES = 8000`。

offload 省 token 却要花模型回合换回来，账是负的：

| 通道 | 注入模型的总字节 |
|---|---|
| `DOM.getAXTree`（offload 后的存根） | 1.82 MB |
| `local_fs_read` | 4.02 MB |
| `local_fs_search` | 1.57 MB |
| `find_in_axtree` | 1.37 MB |
| **合计** | **8.78 MB** |

为省 1.82MB 注入了 6.96MB，外加约 1000 个模型回合（35.3%）。

`progress_obs.py:76-78` 的注释自己承认过这个问题，但那次修复只把 `local_fs_*` 引导到 `find_in_axtree`，回合数一个没少。

**已改**：`runtime_config.py` 的 `DEFAULT_OFFLOAD_THRESHOLD_BYTES` 8000 → **50000**
（不是早先文中写的 40000；50000 让 HN 级别 49.5KB 的树刚好留在上下文里）。

**首次实测（run de60b7f4）**：`find_in_axtree` 占 browser 回合从基线 31%
（871/2832）降到 **8.9%**（7/79），worker 的 `local_fs_read` 从 379 次降到 1 次。
回合数确实下来了，但**净收益仍未证**——没有同任务的 before/after 对照。

### L0.1 续：阈值改动的实测，以及分级还剩什么（2026-09-12）

阈值改动前后，全仓按日期切开。注意 `tool_result.model_visible.rawBytes` 是
offload **之后**的存根大小，原始大小要读 `fieldOriginalBytes`。

| | 改前（8000） | 改后（50000） |
|---|---:|---:|
| browser turn | 6,216 | 275 |
| AXTree 样本 | 914 | 83 |
| 原始字节 p50 | 28,152 | 28,376 |
| **原始字节 p90** | **119,667** | **38,344** |
| **>50000 的树** | **291 = 31.8%** | **0** |
| 实际 offload | 96.2% | **1.2%** |
| **被迫查询回合**（`local_fs_read/search`） | **1,350 = 21.7%** | **8 = 2.9%** |
| 主动 `find_in_axtree` 回合 | 1,024 = 16.5% | 8 = 2.9% |

被迫查询 21.7% → 2.9% 是真实收益。但**净收益仍未证**，理由具体：改后的 275 个
turn、83 棵树**全部来自同一个站点**（深创投 apply 表单，树稳定在 28-47KB）。改前
数据里 p90 是 119,667、31.8% 的树超过 50000——那些是别的站点。在那些站点上
50000 照样 offload，被迫查询照样回来。

**「按结果类型分级」原本是三条，现在只剩一条。**

| 原方案 | 现状 |
|---|---|
| 小树直接返回 | ✅ 50000 阈值已实现 |
| 段内用 `transform`/`querySelector` 取值，免得模型再查文件 | ✅ 段化后模型自己就在这么做（run d5b920de 的段里有 `transform`+`find` 步骤在段内解析目标 id） |
| **大树返回结构索引 + 相关候选，原始全树留作证据引用** | ❌ 未做 |

剩下那条的形状：今天对一棵 120KB 的树只有二选一——全塞上下文（约 30K token，
且反复塞），或落盘（模型花 1-3 个回合去查）。分级给第三条路：

```
回执里带：  结构索引（有几个 form/table/list，各自 role+name+行号范围）  ~几百字节
          + 相关候选行（用调用必填的 purpose 字段在落盘前筛出 top-N）   ~1-2KB
落盘保留：  全树 savedPath，需要精确定位时 find_in_axtree 照常可用
```

大树场景下额外回合为 0，上下文只付几 KB。用 `purpose`（自然语言）筛树是启发式，
但**筛错的代价是退化到现状**（模型拿到没用的候选，再去 find_in_axtree），不会更
糟——这让它风险很低。

**触发条件（不满足就不要做）**：一个真实任务里 `fieldOriginalBytes > 50000` 的
AXTree 出现率超过 20%，或被迫查询回合回到 10% 以上。两个数都能从 `run.jsonl`
直接算。当前 5 个 run 里靶子出现 **0 次**，而所有 A/B 基线又都建立在深创投这一个
任务上，所以先换站点验证再谈。

### L0.2 降 worker 的推理预算 ❌ 实测否决

`config.json` 对 lead / worker / VL 三个角色全开 `reasoning_effort: "max"` + `thinking: enabled`。

**这是一个假设，不是结论**：`llm/thinking.py:43-50` 记录了 Ark 端点的实测——`reasoning_effort` / `output_config.effort` 都被接受但 thinking 照样开着，**只有 `thinking.type` 是真开关**；而 `glm-5.3-flash` 关 thinking 会 400。

**结果**：`deepseek-v4-flash` 对 `reasoning_effort` 基本不敏感（n=6：
max 1.8s/231tok、high 1.8s/260、medium 1.6s/167、low 1.7s/238，全在噪声内，
工具调用 6/6 正确）。`glm-5.3` 在 n=6 下 max 10.2s/523 vs high 8.9s/515，
差异也不显著且方差大。`thinking: disabled` 在 Ark 端点直接 400。

**不改。** 这个杠杆在 glm-5.2 上曾经显著，换模型后消失——**推理预算是模型相关的，
换模型必须重测**。

### L0.3 ~~lead max_tokens=8000~~ ❌ 已撤回

实测 422 次 lead 调用：p50=844，p95=10032，**>8000 的占 6.4%**，而这 6.4% 正是计划生成那几发（out=23705/24000/23729）。`agent.truncated_response` 事件为 0 —— 当前 30000 预算从没截断过，不该动。

---

## L1：Harness 层（小改动）

### L1.1 `agent_mode=browser` 直达 ✅ 已完成（`11ce1e5`）

实测首个浏览器动作要等 **6.6–19.9 分钟**，全是 lead 的计划阶段（单发输出 24000 token，耗时 356–497s）。

**动作**：`main.py` 提供 `agent_mode=lead|browser`，`browser` 不创建 LeadAgent、不生成 phase plan。

**已落地（`11ce1e5`）**：一次有界分类调用取代那一发计划生成，之后机械合成同形 raw
声明，编译/校验/PlanValidator 审计/操作员审批/派发/收尾全部走原函数。分类器不继承
lead 的推理预算（继承就会原样复现那 254 秒），literal items 必须逐字出现在任务里。

**实测**：计划阶段 254s → 约 14s。但 browser 模式的 worker 侧并无提速（也不该有），
见 `a686e03f` 复盘。分类器早期产出的 output_contract 会把任务里枚举的条目当成产物的
列，导致收尾返工——已在 prompt 里加列/行区分修掉。

### L1.2 页面记录 + 阶段自动续跑 ✅ 已完成（2026-09-12）

原方案是"会话宿主从 worker 换成 PageSession"。做之前先量，量完把范围改了。

#### 为什么原方案只能吃到 15%

两处测量（下面两张表都是从历史 run.jsonl 逐事件算的）：

| 换 worker 的代价 | 单次 | 50 次合计 |
|---|---:|---:|
| 第 2+ 个 worker 的热身（对照：第 1 个 18.8s） | 28.4s | **1,421s** |
| **worker 结束 → 下一个 worker 起步（走 lead 一趟）** | p50 **96.0s** | — |
| 　其中上一个是 `step_budget_exhausted` 的（n=50） | p50 **122.0s** | **9,350s** |

间隔里平均 **4.2 次 lead LLM 调用、8,586 out token**；重新发布计划 65 次。

只传结构化记录 → 省热身那 1,421s；lead 往返那 9,350s 一分不动。**1,421 : 9,350 ——
所以"传记录"必须和"砍往返"一起做，单做任何一件都不成立。**

#### step cap 到底该不该留：24 个可配对样本

| `step_budget_exhausted` worker 的尾部 5 回合 | 数量 |
|---|---:|
| 仍在产出新的状态变更动作 → cap 砍掉了有效工作 | **14（58%）** |
| 空转 / 高重复 → cap 在止损 | 10（42%） |

结论：**cap 保留**（42% 的时候它在救命），但"撞 cap"不该等于"phase 停下等 lead"。
单 worker 的 cap 防的是单个 worker 的上下文耗尽和死循环；phase 该不该继续，换成
进展判据。

#### 落地的两块

**① 页面记录**（`harness/page_session.py`，`5cc850b`）

每个 worker 终态时从 trace **机械提取**，按 pageId 落到 `<task_dir>/page_sessions/`：

- 存：已填值（Input.type / Input.select）、成功的状态变更动作、失败路径 + 平台 errorCode、artifact 路径、导航
- **不存**：AX id、selector、坐标、页面快照 —— 它们随 document 世代过期，给出去比不给更差（`stale-target` 要一个往返才发现）
- 标签用 `params.purpose`：平台对所有状态变更方法标了 `requiresPurpose`，模型本来就写了一句"这一步在干什么"，**是运行期数据，不是 harness 里的站点/字段硬编码**
- 过滤掉 `dismiss_overlay:` / `captcha autosolve:` 两个恢复类 composite 自己写的 purpose（是机械噪声，对下一个 worker 零价值）
- **不问模型**：worker 自述是 claim，下一个 worker 会当成页面状态读
- 注入进下一个 worker 的第一条 user message（不做成工具 —— guide 几乎无人按需读），且只注入这个 worker 实际被授权的 pageId；文案写明"这是上一个 worker 当时的观察，不是当前页面状态的保证"

**② 阶段自动续跑**（`harness/tools/lead_tools.py`，`a9522ad`）

`wait_browser_agents` 返回后，若「恰好一个完成的 worker + 无 pending + phase 未终态」，
harness 直接重派同一个 phase，不回 lead。

三条机械判据全部复用 direct-worker 已有的那套，没有新发明：

| 判据 | 实现 | 谁写的 |
|---|---|---|
| A 完成 | `status=done` 且 phase `validated_done` | 既有 |
| B 无进展 | `repeated_no_progress_same_signature`（行数不增 + 失败签名相同） | `_direct_continuation_decision` |
| C 预算 | 自动续跑次数单独封顶（默认 2） | 新增 config |

C 为什么不复用 `phase.max_attempts`：`_count_budgeted_phase_attempts` **把 `partial` 排除在预算外**，
所以 phase 预算单独兜不住 partial 循环。

仍然回 lead 的情况（每条都记事件）：未解决的验证码 / HITL、两个完成的 worker（是合并
决策）、还有 worker 在跑（是并发决策）、spawn 被拒（phase_exhausted / 依赖闸 / replan 检查点）。

续跑时**原样重发 lead 自己那次 dispatch**（含 session_key / fleet_id / page_policy /
worker_contract 覆盖 —— 这些 phase 本身表达不出来），外加一段只讲回执的说明：上一个
worker 的 status、行数、已落盘 artifact 路径。**不编造"还剩哪些行"**——能枚举的那类
契约由既有的 `_direct_continuation_context` 给精确单元表。

lead 侧 rule 11 已改：看到 `phaseContinuation` 回执就知道 harness 已经续过，旁边那个
worker 结果是最后一次。

#### 配置

`runtime_config.py` → `harness.phase_auto_continuation_enabled`（默认开）、
`phase_auto_continuation_max_attempts`（默认 2，clamp 0–6）。设 0 即完全回到旧行为。

#### 明确不做（留给 `docs/persistent-browser-runtime-redesign-plan.md` 阶段 B）

接管 PageSession 生命周期、跨 worker 复用浏览器连接、删掉 step 预算终止路径本身。

#### 验收

`3932 passed, 3 skipped, 1012 subtests`（此前 3902），新增 30 个测试：
`tests/test_page_session.py`（14）、`tests/test_phase_auto_continuation.py`（16）。
**真实任务 A/B 未跑** —— 上面的数字全部是历史 run 的回放测量，收益需要下一次同类任务实跑验证。

### L1.3 动作回执自带局部 AXTree diff ⬜ 待做（与段化重叠，需重测）

现状：`click → getAXTree → find_in_axtree` 三个回合。已有 `snapshot_diff.detected` 事件和 `_precompute_axtree_snapshot`（`axtree_state.py:136`）。

**动作**：`Input.*` 返回时附带动作后受影响区域的树片段，三个回合压成一个。

**2026-09-12 修订**：这一项和段化打同一个靶子（纯观察 turn），而段化更彻底——它把
观察收进段里，连动作 turn 一起消掉。`8208ed49` 已把纯观察 turn 从 44% 压到 21%。
所以 L1.3 的剩余价值是"段与段之间的间隙"，**必须在段化率稳定之后重测再定**，不能
把两者的收益相加。

### L1.4 并行执行 tool_calls ⏸ 当前无靶子（2026-09-12 实测后暂缓）

`agent_harness.py:1741` 是串行 `for tool_index, tool_call in enumerate(tool_calls):`。对零延迟本地工具无所谓，但阻止了跨页并行读取。

**动作**：按资源分组 `gather`（同 page 的浏览器动作保持串行）。同时在 prompt 里要求模型批量发工具调用（当前 1.21 → 目标 3+）。

**2026-09-12**：传输层的阻塞已经拆掉（L2.2 删了全局 `_call_lock`，实测 5 个并发请求
全部按 id 正确配对），所以这一项不再有传输层前置。但实测之后，**它当前没有靶子**。

全仓 6,478 个 browser 模型回合、7,876 次工具调用（均值 **1.22/回合**）：

| 每回合调用数 | 回合 | 占比 |
|---:|---:|---:|
| 1 | 5,338 | **82.4%** |
| 2 | 946 | 14.6% |
| 3 | 147 | 2.3% |
| ≥4 | 47 | 0.7% |

多调用批次（1,140 个，占 17.6%）的构成：

| 构成 | 批次 | 占多调用批次 |
|---|---:|---:|
| **纯本地工具** | 869 | **76%** |
| 纯浏览器动作 | 156 | 14% |
| 混合 | 113 | 10% |

三条理由：

1. **能安全并行的那部分不值钱。** 76% 的多调用批次是纯本地工具，而本地工具
   p50 只有 5-81ms（`find_in_axtree` 5ms、`local_fs_read` 10ms、`local_fs_search`
   81ms）。并行两个 10ms 的调用省不到 100ms。
2. **值钱的那部分不能并行。** 纯浏览器多调用批次只有 156 个（全部回合的 2.4%），
   而其中同 page 的动作必须串行——一个动作会让排在它后面的 handle 全部失效，这正是
   现有 deferral 逻辑存在的原因（实际只触发 42 次，推迟 46 个调用）。
3. **批量浏览器动作已经有正确形态了。** `execute_browser_workflow` 在平台侧顺序执行、
   有 `onError` 语义、失败带 `executionTrace`。harness 侧再做一套并行是重复且更危险的
   实现。

**所以 L1.4 真正的前置是"让模型多批量发"，而那已经由段化覆盖。** 重开条件：每回合
工具调用数升到 2+，且其中跨 page 的浏览器动作批次占到有意义的比例。

---

## L2：协议层

### L2.1 开 Workflow 执行通道 / exec ⚠️ **部分完成，未闭环**

**已做**：live contract 探针；`listen`→`waitEvent` 契约修复（存量 skill 全部会被
平台 -32602 拒绝）；`onError: retry` 拦截；模型侧 schema 暴露 `waitEvent` /
`readEvents` / `store`；`ExecObserver` 第一版；失败回执改用 `workflowId`。

**未闭环**（详见 `workflow-execute-live-contract.md` 第 9 节）：

- **失败时取不回 store 内容与已完成 Action 的结果**。`Workflow.progress` 只带
  `variables` 和 `storeRevision`。段里 `store.append` 收了 7 行、第 8 行失败时，
  拿不回那 7 行。需要平台在失败回执返回内容或可读引用。
- **事件窗口语义曾写错并已更正**：`waitEvent` 跳过前一个 Action 的事件窗口
  （engine.ts 把 waitCursor 推到 window.endCursor），不是我初版说的"会回放"。
  真机上导航仍安全（Page.loaded 落在窗口外），但这是页面性质，不是协议保证。
- **`DOM.axTreeUpdated` 目录里有、这个部署不发**，已从可等待集合移除。
- ~~真实任务 A/B 未做。~~ **已做（2026-09-12 补记）**，同任务对照：

  | | `de60b7f4` 逐动作 | `8208ed49` 段化 | 变化 |
  |---|---:|---:|---:|
  | worker 数 | 2（第一个步数耗尽） | 1 | |
  | turn | 79 | 33 | **−58%** |
  | turn 墙钟 | 956.2s | 464.3s | **−51%** |
  | 模型时间 | 759.7s | 341.4s | −55% |
  | **纯观察 turn** | **35（44%）** | **7（21%）** | **−80%** |
  | out token | 130,339 | 65,477 | −50% |

  **收益全部来自"观察 turn 被吞掉"，不是"批量执行动作"。** 逐动作路径里每个动作
  后面跟着一个观察 turn，段把观察吞进去了。段的形态是 2-3 步的
  `[Input.click …] + [DOM.getAXTree]`，平均 **2.0 个动作/段**——都很小。

因此 `workflow_execution_enabled` **恢复为 canary**：代码默认 False，
部署在 config.json 显式 opt-in。

### L2.2 删响应形状匹配回退 + 多 in-flight ✅ 已完成（`980f7b1`）

`abcp_client.py:597-599`：

```python
if message.get("type") in {"response", "result", "error"}:
    return True
```

未回显 request ID 的响应靠"形状像结果"来匹配当前 pending call。`abcp_client.py:443` 的 `async with self._call_lock` 全局串行，严重到要开第二条连接绕过（`runtime_config.py:1091-1100`）。

**收益是正确性**（乱序/迟到响应串台），不是速度——RPC p50 只有 0.36s。**不是 exec 的前置**。

**实测前置（2026-09-12，真实面板）**：

- id 回显覆盖 **17/17**：System / Fleet / Page / DOM / Workflow 五族，成功与错误
  路径都有，没有一条响应缺 id。
- 并发配对 **5/5**，且**返回顺序与发出顺序不同**：
  发 `[b8c7387f, 3f589502, e59815c4, dd8bade5, 1a69bf18]`，
  回 `[3f589502, 1a69bf18, b8c7387f, e59815c4, dd8bade5]`。

乱序这条说明旧回退的危险今天只是被全局锁掩盖着，而**超时后迟到的响应照样会串台**。

**已落地**：`_pending` 改成按 request id 索引的字典，删 `_call_lock`，删形状回退。
配不上 id 的类响应消息走 `orphan_response` 事件——不静默丢、也不交给任何人，万一
平台哪天不回显 id 会在传输日志里现形，而不是表现为"调用莫名超时"。

**连带**：`skill_workflow_active_control_enabled`（第二条连接）的存在理由消失了一半
——in-band 控制不再被锁挡住，剩下的约束是 `Workflow.pause/resume` 会话绑定到 run 的
owner。注释已更正，**默认值没动**，需要单独验证。

### L2.3 平台侧绑定 Action（原子 check-then-act）⬜ 待平台配合

harness composite 只能买回合数，买不到原子性：check 和 act 之间隔着 WebSocket 往返。真原子必须在 dispatcher 的 page lane 槽内完成。

过渡方案：Workflow 的 `if` + `action` 连续两步跑在同一次执行里，窗口比模型回合往返小三个数量级。

---

## L3：换基模 + 删 VL 链路 ⬜ 排最后

实测 `visual_verify` 36 次、p50 13.2s、14 次运行合计 9.4 分钟 —— **不是瓶颈**。删它的收益是正确性（消除"图像结论转文字再转回主 agent"的信息损失），不是速度。

**必须排在所有 A/B 敏感优化之后**：删 VL 依赖 BrowserAgent 基模多模态，而当前 worker 是 `deepseek-v4-flash`。换基模会同时改变推理长度、工具调用习惯、AXTree 理解能力，**一旦先换，前面所有优化的 A/B 归因全部失效**。

---

## 新增项（2026-09-12 测量后补入计划）

### N1 VL reality check 异步化 ✅ 已完成（`d41a510`）

把全仓 7,599 次 worker 工具调用按「结论是否被当次消费」分类：

| 类别 | 次数 | 总秒 | 占比 | 能否异步 |
|---|---:|---:|---:|---|
| HITL 等人 | 33 | 12,094.5 | 52.7% | 无意义（本来就是等人） |
| 干净工具时间 | 7,366 | 5,237.7 | 22.8% | 否（结果要用） |
| **reality check（advisory）** | **137** | **4,527.8** | **19.7%** | **可以** |
| `visual_verify` 工具自身 | 58 | 985.7 | 4.3% | 否（模型主动要答案） |

**15 个工具里除 reality check 外没有一个可以异步**——其余结果都被模型用来决定下一步。
而 reality check 挂在的宿主工具本身几乎不花时间：`find_in_axtree` p50 **5ms**、
`local_fs_read` p50 **10ms**。它的 4,527.8s 里还有 **3,019s 是零产出**（28 次超时
p50 64s、20 次 VL 返回空）。

划分原则，可复用到别的 VL 调用：**advisory 可以晚到，terminal 不可以。**
- 可异步：reality check
- 不可异步：验证码自解（决定要不要 HITL，是决策门）、模型主动调的 `visual_verify`
- 本来就免费：`visual_recovery_hint`（静态文本，不发 VL 请求）

**实测（run `d5b920de`）**：承载它的 `find_in_axtree` 从 **41,608ms → 5ms**，
verdict 在两个 turn 后投递，模型正确读作 advisory。

### N2 段化率回归 ✅ 已定性，且结论反转

**先说结论：原假设证伪，而且"提高段化率"这个目标本身是错的。**

#### 原假设（strategy_bank 的高门槛指引）已被证伪

| run | strategy_bank 高门槛段指引 | 段工具在 preferred_tools | 段化率 |
|---|---|---|---:|
| `8208ed49` | 无 | 无 | **45%** |
| `a686e03f` | 有 | 有 | 7% |
| `d5b920de` | **无** | 有 | **11%** |

`d5b920de` 已经是在撤回那条指引之后跑的，段化率仍然只有 11%。**那条指引不是主因。**
（撤回本身仍然保留——它描述的场景确实罕见，留着没有好处。）

#### 真正的变量：常驻 prompt 的段指引篇幅

| run | 段指引区块 | 子条目 | 段工具描述 | 段化率 | **动作/段** |
|---|---:|---:|---:|---:|---:|
| `8208ed49` | **2,304 字符** | **6 条** | 317 字符 | **45%** | 1.9 |
| `a686e03f` | 4,445 | 10 条 | 598 字符 | 7% | 2.8 |
| `d5b920de` | 4,445 | 10 条 | 598 字符 | 11% | **4.0** |

后两次的段指引完全相同，`8208ed49` 只有它的一半。新增的 4 条正是 `transform` /
`$last` / regex 唯一匹配 / 失败切片这些高级用法——**它们把"开一个段"从轻量动作变成
了需要斟酌的重型动作**。段的大小随指引复杂度单调上升（1.9 → 2.8 → 4.0 动作/段），
不像随机。

#### 目标修正：段化率只是代理指标，墙钟才是目标

```
8208ed49   45% 段化, 1.9 动作/段   464.3s
d5b920de   11% 段化, 4.0 动作/段   439.8s   ← 最快
                                   其中 179.7s 被平台 scroll-deadline 吃掉
                                   扣掉约 270s
```

**"段少而大"实测比"段多而小"更快。** 早先"把段化率打回 45%"的建议方向是错的，撤回。

#### 为什么不做那个 A/B

严谨验证要两臂各 3 次（6 次运行、约 50 分钟），而 `scroll-deadline-exceeded` 会随机
吃掉约 40% 墙钟（N3），信噪比很差。现有数据已经指向当前配置在墙钟上领先，再花 50
分钟确认一个已经领先的配置，不如把时间放到还没被触碰的结构性问题上。

**重开这个 A/B 的条件**：平台修好 `scroll-deadline-exceeded` 之后（噪声源消失），
或者出现墙钟明显退化的运行。

#### 反例约束（不要再加）

「少于 3 步不要开段」和「段用在步骤彼此相似的动作串上」这两条都是错的——`8208ed49`
的 15 个段平均 2.0 动作、全部 2-3 步、且填的是不同字段，加上这两条会把它们全部排除。

### N3 平台侧 `scroll-deadline-exceeded` ⬜ 归 ABCP

不是 harness 能修的，但它是当前最大的单一时间黑洞。run `d5b920de`：

```
turn 29  48.0s  execute_browser_workflow → failed scroll-deadline-exceeded（6 步只执行 1 步）
turn 30-40      System.describeAction ×2 / Page.wheel ×2 / Input.scroll / … 共 11 个 turn
────────────────
turn 29-40 合计 179.7s
恢复之后 turn 41/42 两个段 14.4s 就把剩下的活干完了
```

**净浪费约 170s = 该次总墙钟 439.8s 的 39%。** 详见
`docs/abcp-workflow-platform-requests.md` 问题 6。

## 验收指标（每阶段复跑）

- 模型回合数（`agent.model` 事件计数）
- 每回合工具调用数（基线 **1.21**）
- 零延迟本地工具独占的回合占比（基线 **35.3%**）
- 首个浏览器动作耗时（基线 **6.6–19.9 分钟**）
- phase 被多个 worker 服务的比例（基线 **33.3%**）
- 任务 p50/p95 总耗时，人工等待单列

A/B 框架复用 `scratchpad/short_ab_runner.py` + `artifacts/short_ab_*.json`。

---

## 2026-09-11 晚间补充：b6d3156e / 8208ed49 同任务对照后的修订

同任务（szvc.com.cn/apply 表单填写）两轮对照，**不是严格 A/B**（git dirty hash、
system prompt、worker task 合同、初始页面均不同），只能作方向性证据：

| | 8208ed49（用了 16 个段） | b6d3156e（纯单发） |
|---|---:|---:|
| 总 wall | 14.27m | 10.81m |
| 计划阶段（lead→spawn） | 4.77m（含一次 plan 修复） | 4.54m |
| worker 执行 | 7.74m | 6.03m |
| worker 模型回合 | 33 | **58（cap 50+延期 8，压线完成）** |
| worker 输出 token | 65,477 | 43,967 |

三个新事实进计划：

1. **计划阶段仍是最大单项**。b6d3 总 wall 10.81m 里 lead 计划生成占 4.54m
   （42%）——一次 13,123 输出 token 的 emit_direct_task_plan。L1.1 的优先级
   由「基线 6.6–19.9 分钟」升级为「今天两轮实测 4.5–4.8 分钟、占 42%」。
2. **单发模式会撞 step cap**。b6d3 在第 49 步触发 cap、申请延期 8 步、
   在第 58 步（= 新上限）完成，零余量；中途还有一次 loop_nudge
   （DOM.getAXTree 连发 8 次）。这给了 L1.2（会话与 step 预算解耦）第一个
   运行时证据，不再是 spawn 计数推断。
3. **段采用率是独立问题，与段本身是否划算分开**。b6d3 的 tool list、gate、
   prompt 规则全部就绪但模型零次调用。已修（2026-09-11 晚）：
   strategy_bank form_interaction 加 `execute_browser_workflow`、退
   `Input.select`；工具描述去掉"complete action sequence known in advance"；
   常驻规则补「何时单发/何时成段」判别（不设固定步数阈值——盈亏平衡点随
   模型延迟漂移，写死数字是把假设当事实）。
4. **段盈亏口径**：`exec.segment.result` 已补原始指标
   （authored/executed 步数、按步类型计数、durationMs、resultBytes），
   刻意**不**在运行时写盈亏结论——breakeven 需要该 run 自己的模型延迟，
   是离线计算（本轮 8208 的 2.2 步/段均值卡在平衡线附近，但那是该轮
   单发基线 5.86s 下的结论，不可迁移）。

**后续 A/B 的最低要求**（采纳 GPT 标准）：相同 git commit、相同任务合同、
全新页面，workflow-guided 与 neutral 交错各 ≥3 组。

### L1.1 实施顺序调整

原计划把 L1.1 列为「提供 `agent_mode=lead|browser`」。结合 b6d3 已有的
`emit_direct_task_plan → direct_worker` 链路，落地路径改为：**保留 lead 的
编排/恢复/收尾机制，跳过计划生成这一次 LLM 调用**——合成单 phase 计划
（worker_task = 原始用户任务，execution_mode=direct_worker），复用
`_run_direct_worker` 及其 finalization。细节见下方实施计划。

### L1.1 设计约束（2026-09-11 GPT review 采纳，实施时遵守）

1. **模式命名 `lead|browser`**，"direct" 不作为用户概念暴露（内部管线名除外）。
2. **分类失败在派发前结构化失败**，不得用弱化的 web_scrape 合同继续执行。
3. **不伪造 assistant tool_use 消息**复用 LeadAgent 循环；直接调用已有的
   编译、审批、worker 派发、收尾内部管线。
4. **resume 直接读取已批准的持久化 plan**，不做紧凑入参反推 + hash 比对。
5. **分类调用延迟单独基准**：不能从 worker 普通回合推导（单轮探针低估
   真实延迟的教训见 memory；分类上下文小，预期 2s 级，需实测）。
6. 实施前置：段遥测两处计数错误（action 简写、>120 步封顶低报）先修——
   **已完成（2026-09-11 夜）**，否则 A/B 数据失真。
