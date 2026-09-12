# Harness 提速方案：分层落地计划

- 日期：2026-09-11（当日修订）
- 依据：`docs/replay-baselines/baseline-2026-09-11.json` 固定的 14 个 taskId
  （63 个 browser worker、2832 个 browser-agent 模型回合、2002 次浏览器动作）。
  **按 taskId 选取**——"最新 14 次"的滑动窗口已经复现不出这个样本。
  复现脚本：`docs/replay-baselines/analyze_turn_breakdown.py`
- 状态：L2-exec **部分完成**（协议探针 + 模型入口 + 第一版 observer）；
  L0.1 已改待验证；L0.2 实测否决；L1 / L2 剩余 / L3 待做

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

### L0.1 取消 AXTree 的 offload ⬜ 待做

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

**更好的方向（未做）**：单一全局阈值太粗。按结果类型处理更合理——小树直接返回；
大树返回结构索引加与当前问题相关的候选，原始全树留作证据引用；段内直接用
`transform`/`querySelector` 取值，避免模型再调文件查询工具。

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

### L1.1 `agent_mode=browser` 直达 ⬜ 待做

实测首个浏览器动作要等 **6.6–19.9 分钟**，全是 lead 的计划阶段（单发输出 24000 token，耗时 356–497s）。

**动作**：`main.py` 提供 `agent_mode=lead|browser`，`browser` 不创建 LeadAgent、不生成 phase plan。

### L1.2 PageSession：把会话从 step 预算上解耦 ⬜ 待做

`agent_harness.py:1314` `while not should_finish and step < self.effective_max_steps:` —— 循环一退出，`messages`（全部页面事实）随栈消失，只留一个 `final-context.json` 归档，无人读回。

成本：**33.3% 的 phase 要重建 worker**（`fill_yue_application_form` 用了 4 个），每次重建 = 丢掉全部页面事实 + 重新 spawn + 重新导航 + 重新读树。

**动作**：会话宿主从 worker 进程换成 PageSession（绑 pageId/fleetId），存可跨 worker 复用的：已验证的选择器语义、已填值、已完成的不可重放副作用、失败过的路径。**不存** AX ID / 坐标 / 页面快照值。

### L1.3 动作回执自带局部 AXTree diff ⬜ 待做

现状：`click → getAXTree → find_in_axtree` 三个回合。已有 `snapshot_diff.detected` 事件和 `_precompute_axtree_snapshot`（`axtree_state.py:136`）。

**动作**：`Input.*` 返回时附带动作后受影响区域的树片段，三个回合压成一个。

### L1.4 并行执行 tool_calls ⬜ 待做

`agent_harness.py:1741` 是串行 `for tool_index, tool_call in enumerate(tool_calls):`。对零延迟本地工具无所谓，但阻止了跨页并行读取。

**动作**：按资源分组 `gather`（同 page 的浏览器动作保持串行）。同时在 prompt 里要求模型批量发工具调用（当前 1.21 → 目标 3+）。

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
- **真实任务 A/B 未做。**

因此 `workflow_execution_enabled` **恢复为 canary**：代码默认 False，
部署在 config.json 显式 opt-in。

### L2.2 删响应形状匹配回退 + 多 in-flight ⬜ 待做

`abcp_client.py:597-599`：

```python
if message.get("type") in {"response", "result", "error"}:
    return True
```

未回显 request ID 的响应靠"形状像结果"来匹配当前 pending call。`abcp_client.py:443` 的 `async with self._call_lock` 全局串行，严重到要开第二条连接绕过（`runtime_config.py:1091-1100`）。

**收益是正确性**（乱序/迟到响应串台），不是速度——RPC p50 只有 0.36s。**不是 exec 的前置**。

### L2.3 平台侧绑定 Action（原子 check-then-act）⬜ 待平台配合

harness composite 只能买回合数，买不到原子性：check 和 act 之间隔着 WebSocket 往返。真原子必须在 dispatcher 的 page lane 槽内完成。

过渡方案：Workflow 的 `if` + `action` 连续两步跑在同一次执行里，窗口比模型回合往返小三个数量级。

---

## L3：换基模 + 删 VL 链路 ⬜ 排最后

实测 `visual_verify` 36 次、p50 13.2s、14 次运行合计 9.4 分钟 —— **不是瓶颈**。删它的收益是正确性（消除"图像结论转文字再转回主 agent"的信息损失），不是速度。

**必须排在所有 A/B 敏感优化之后**：删 VL 依赖 BrowserAgent 基模多模态，而当前 worker 是 `deepseek-v4-flash`。换基模会同时改变推理长度、工具调用习惯、AXTree 理解能力，**一旦先换，前面所有优化的 A/B 归因全部失效**。

---

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
