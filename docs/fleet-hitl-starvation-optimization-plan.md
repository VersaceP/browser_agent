# Fleet Auth Barrier / HITL 饥饿优化计划

> 输入：`docs/worktree-48b4d7d7-diagnosis.md`（kimi 审计报告）+ 对
> `worktree/48b4d7d71e62405a87db6fa7f1fc1404` 运行证据与当前工作区源码的独立复核。
> 本文只是计划，未实施任何改动。行号以撰写时工作区代码为准。

## 0. 调研结论：barrier 是不是 gated 的主因

**是，且是唯一来源。** 全 run 71 次 `browser.call.fleet_auth_gated` 全部来自
`FleetAuthBarrier`，没有第二个产生方。

按 worker 复核（数据取自 `run.jsonl`，与 kimi 报告独立重算）：

| worker | 模型步 | LLM 调用 | 浏览器调用 | gated | 存活 |
|---|---|---|---|---|---|
| browser-001 | 25 | — | 17 | 0 | 04:24:11 → 04:29:03 |
| browser-002 | 49 | — | 33 | 0 | 04:29:21 → 04:58:15 |
| browser-003 | 46 | — | 14 | 27 | 04:29:32 → 05:04:52 |
| browser-004 | 40 | — | 19 | 15 | 04:29:42 → 06:22:56 |
| **browser-005** | **30** | **30** | **0** | **29** | 05:21:22 → 06:22:56 |

browser-005 存活 61 分钟，**一次真实浏览器调用都没有发出过**——30 个模型步里 29 步撞
gated，剩下 1 步是 `Memory.save`。

### 对 kimi 报告的一处修正：gated **是**消耗预算的

报告称 gated "不计数、不升级、不消耗任何预算"。前两条成立，第三条不成立：

- browser-005 消耗了 **30/50 步**和 **30 次 LLM 调用**（`llm.usage` 30 条）
- gated 回执之间间隔约 126 秒 ≈ barrier `wait_timeout_seconds`(120s) + 开销

所以真实形状是：**gated 吃 step 和 token 预算，但不吃失败预算、不升级**；因为每步要
等满 120 秒，50 步的自然死亡需要约 100 分钟。browser-005 在第 61 分钟被 cancel，
没走到那一步。

这个修正改变修法：不需要"给 gated 加计数器"（step 已经在计数），需要的是
**让饥饿达到阈值时终态失败，而不是靠耗尽 50 步慢性死亡**。

### HITL 不 resume → 锁死整个 fleet：确认

因果链完整可证：

```
04:30:42  browser-002 遇验证码 → claim barrier（关门，generation 0）→ 进 HITL 等待
04:32:35  browser-003 首次 gated                     ┐
04:34:44  browser-004 首次 gated                     │ 25 分钟全舰队串行
04:50:43  browser-002 HITL 等满 1200s 超时 —— 门没开 │
04:58:15  browser-002 结束，abandon_worker：只清 resolver，门保持关闭
04:58:23  首张 resolverRequired（无主关门）
05:05:09  browser-004 takeover 成为 resolver → 立刻进 HITL 等待
05:23:47  browser-005 首次 gated（resolverWorkerId=browser-004）┐
05:25:39 / 05:46:10 / 06:06:57  browser-004 第 2/3/4 次暂停     │ 59 分钟
06:22:17  browser-005 第 29 次 gated                            ┘
06:22:56  browser-004 / browser-005 双双 cancelled
```

**resolver 自身健康状况不被任何机制感知**：browser-004 名义上持有 resolver 身份
77 分钟，实际全程在等一个不存在的人，而 barrier 对此一无所知，只知道"有主，等着"。

代码侧的三个缺口（均为 CONFIRMED）：

1. `FleetAuthBarrier.abandon_worker`（`harness/fleet_runtime.py:2478`）清空
   `resolver_worker_id` 但**保持门关闭**，无 TTL、无自动开门。
2. HITL 超时后不释放 barrier，也不升级终态。
3. `hitl_no_repause_cooldown_seconds = 8.0` 只是 8 秒冷却；
   `hitl_post_resume_confirm_max_rounds = 3` 只约束**单次工具调用内**的确认轮数。
   **不存在跨调用的累计预算**——这正是 browser-004 能暂停 4 次的原因：每次都是新的
   工具调用，轮次计数从零开始。

---

## 1. 逐条回答

### 1.1 VL captcha 独立开关

**现状**：`runtime_config.py:VLConfig` 里有一条明确注释：

> there is deliberately no separate `captcha_solve_enabled` switch — `enabled` is the
> single VL gate for every role, auto-solve included (operator decision, 2026-07-31)

要加这个开关就是推翻 7-31 的自己。可以做，但**注释必须同步改掉**，否则下一个人
会照着注释把它删回去。

**建议形状**（三态，而不是布尔）：

```python
# None = 跟随 enabled；True/False = 独立覆盖
captcha_solve_enabled: Optional[bool] = None
```

理由：
- provider / model_id / base_url / api_key 都在 `VLConfig` 里，做成完全独立的配置
  块会把这四个字段复制一遍，之后必然漂移。
- 三态同时支持两个方向：「总开关关但 captcha 开」（省钱但保留解锁能力，**这正是
  本 run 最该有的配置**）和「总开关开但 captcha 关」（不想让 VL 碰验证码）。
- 默认 `None` 不改变任何现有部署的行为。

落点：`VLConfig` 增字段 + `from_dict` 解析 + 一个 `captcha_autosolve_allowed()`
辅助方法；调用侧只有 `_maybe_autosolve_before_model_pause` 一处需要改判据。

### 1.2 HITL 配置改动

**单次等待 1200 → 900 秒**：改 `runtime_config.py:625` 一个数，零风险。
注意有 4 处 `getattr(..., 1200.0)` 回退默认值需要同步
（`harness/skill/dispatch.py:1806`、`browser_tools/__init__.py:8498/8692/10767`），
否则配置缺失时行为不一致。

**现在有没有熔断：没有。** 现有的三个相关配置都不是熔断：

| 配置 | 值 | 实际作用 |
|---|---|---|
| `hitl_no_repause_cooldown_seconds` | 8.0 | 8 秒内不重复暂停，防抖动，不是预算 |
| `hitl_post_resume_confirm_max_rounds` | 3 | **单次工具调用内**的恢复确认轮数 |
| `hitl_wait_timeout_seconds` | 1200.0 | 单次等待上限，超时即返回，不累计 |

**建议新增**：

```python
hitl_max_pause_rounds_per_page: int = 3      # 同一页累计暂停轮数
hitl_max_pause_rounds_per_worker: int = 3    # 同一 worker 累计暂停轮数
```

达到上限 → 终态 `hitl_budget_exhausted` → `final_answer`（带 blocker 说明）。

**关键补充（kimi 报告没有提到）**：熔断必须**同时释放 barrier**。否则熔断只是让
resolver 死得快一点，门还是关的，其他 worker 继续 gated——本 run 里 browser-002
正是这样死的（04:58:15 结束，门保持关闭到 05:05:09 才被接管）。

### 1.3 「标题变化永不解除暂停」有没有必要

**kimi 的描述不准确，需要修正。** 逃生口还在，见 `harness/hitl.py:910`：

```
verifier 预算（_SETTLEMENT_VERIFIER_MAX_CALLS = 3）用完后，
_title_clears_challenge() 通过即可解除暂停（gate = title_evidence_after_verifier_budget）
```

只有 `challenge_ever_confirmed = True` 时才永久压制标题证据——也就是
**VL 在这次暂停里真的看见了验证码**。

**这个设计本身是对的，不该删。** 验证码页会自己改标题（加载中→失败→重试），标题
变了完全不等于人解决了；VL 已经确认过页面被挡，凭一个标题就把 worker 放回去，
拿到的是一个已知不可用的页面。

**但它和"verifier 预算 3 次用完"叠加之后变成了死路**：预算耗尽 + 已确认 =
没有任何证据通道，只能等人或等超时。browser-004 就卡在这个组合里。

**建议不是删闩锁，而是补一条更强的证据通道**：

> 当暂停页的 **URL 发生跨路径 / 跨 host 变化**（不是标题变化）时，补发一次预算外的
> verifier 调用。

理由：
- 验证码页自己改标题很常见，自己换 URL 罕见——换 URL 意味着真的离开了挑战页。
- 系统里已经承认这个信号：`_clear_navigation_challenge_state`
  （`browser_tools/__init__.py:4371`）在导航成功时清挑战状态，事件
  `challenge.navigation_cleared` 本 run 触发过 2 次。
- 本 run 反向验证：browser-004 暂停页的 URL 从 05:05 到 06:22 一字未变，所以这条
  补充在本 run **不会**误放行——它只在真的离开挑战页时才给机会。

### 1.4 用 verification agent 替代硬门禁

> **修订（2026-08-07）。** 初稿按"对象"切（世界 / 我们自己），漏了一整格，
> 而 live 任务的缺陷恰好落在漏掉的那格里。决定该谁判的**不是对象，是可判定性**。

| | **可判定**（唯一答案、可复现） | **不可判定**（要解释） |
|---|---|---|
| **我们自己的记录** | ① 调用回执抵达没、pageId 一致没、预算剩多少 → **代码** | ③ 这个计划的意图是什么、这段 blocker 散文在说什么 → **裁判** |
| **外部世界** | ④ URL host、数组长度、scrollTop 变了没、marker 在不在页面上 → **代码** | ② 这页是不是验证码、内容是不是任务要的 → **模型（VL）** |

格子 ④ 是初稿没有的。"判世界就交模型"是错的——世界上有大量可判定的事实，
交给模型看截图判断，比代码读一个字段还不可靠。kimi 的
「裁判能判计划写得对不对，判不了证据是不是真的」就是 **③ 能交、④ 不能交**。

对格子 ③ 还有第三条路，优先于换裁判：**能结构化的别用正则解析散文**。
让上游用枚举字段声明意图，③ 就塌回 ①，裁判只需判"声明是否诚实"。

**裁判的硬边界不是方向，是判错的代价能不能回滚。** `plan_validator` 是纯收紧
方向且 fail-closed（provider 挂了也不批准），它是对的——计划被否 = 重出一版；
`visual_contract` fail-open 也是对的——它错了只是多试一次。同一系统里两个裁判
失败方向相反且都对，说明方向不是自变量。

> 裁判可以决定"再试一次 / 换条路 / 重来一版"，不能决定"到此为止"。
> **终态归代码，因为终态不可回滚。**

四个现有裁判共有一个形状值得抄：**裁判前面永远有一层机械筛**
（`arbiter._NON_VISUAL_TYPES`、`plan_validator` 的 `mechanical_invalid`），
它不做判断、只做分诊——把有确定答案的先摘走，剩下的才给模型。

**过去几轮反复出现的 bug，全部是第二类被第一类的方法污染了**——用代码去判世界：

- `data.error` 里躺着页面上一跳的 `ERR_ABORTED` → 代码判"这次调用失败了"
- 100ms 内没观察到导航 → 代码判"点击没生效"
- 标题变了 → 代码判"挑战清除了"
- reperception 的出口条件是"页面读得动" → 代码判"页面健康"

所以真正该做的**不是新增一个仲裁 agent**，而是两件事同时做：

1. 把现有那些"代码在判世界"的地方**移交**给已经建好的 VL 角色（A–D 都在）。
2. 把"代码判自己"的地方收敛到 `harness/call_outcome.py`（已在进行）。

具体到 reperception 门：它的出口条件应该是「两次读调用**抵达并返回了回执**」
（判自己），而不是「页面读得动」（判世界）。页面坏不坏是**模型该看见的信息**，
不是门该判的。

**风险提示**：仲裁 agent 放在关键路径上会加延迟和不确定性。建议给它划一条硬边界：

> 只让它做**放行方向**的补充证据（能解除封锁），不让它做**收紧方向**的裁决
> （不能凭它把 worker 判死）。

这样它错了只是慢一点或多试一次，不会造成静默的错误成功——和 VL 在
`contract_verify` 里"只有 definitive violated 才否决"是同一条原则。

### 1.5 kimi 报告其余各点评估

| 异常 | 判定 | 说明 |
|---|---|---|
| 1 gated 无饥饿兜底 | **方向成立，细节需修正** | step 是消耗的（见 §0），改成"达阈值终态失败" |
| 2 reperception 白名单漏 `Hitl.requestPause` | **成立，性价比最高** | 两处白名单对同一工具判定相反；p3 实证死于此 |
| 3 HITL 无累计预算 | **成立** | 见 §1.2 |
| 4 标题闩锁 | **描述不准，结论要改** | 见 §1.3，补通道而非删闩锁 |
| 5 p2 语义矛盾 | **成立但优先级下调** | 见下 |
| 6 barrier 无 TTL | **成立** | `abandon_worker` 保持关门 |
| 7 wait 黑洞 + loop_guard 换参绕过 | **成立** | loop_guard 那条是通用问题，值得单独修 |
| 卡点④「就绪即派发」 | **成立但本 run 无收益** | 早派发只会让 browser-005 更早开始被 gated |

**关于异常 5 的分歧**：p2 的 `worker=hitl_timeout` 但 `phase=validated_done`，kimi
建议按 worker 终态降级。**不建议这么改**——p2 拿到了合格产物（`detail_8` 1 行完整
字段），worker 是在产出之后才卡进 HITL 的。降级会把一个本来成功的 phase 判失败。

正确的修法是**记录而非降级**：phase state 附带 `workerStatusCategory` 和
`hitl_wait_seconds`，让报表和 lead 看得见这个 phase 烧掉了一次人工超时，判定逻辑不动。

---

## 2. 优化计划（v2，采纳第一性异议后重排）

v1 是"每个症状配一个阈值"。v2 收敛成 **4 个机制 + 3 个止血**，阈值只作为机制的
参数存在，不再作为独立补丁。

### 机制 A · autosolve 的前置条件不能依赖被验证码破坏的子系统 ⭐ 最高优先级

**证据**：本 run 4 次 autosolve episode 里 **2 次 `vlCalls: 0`——VL 根本没被调用过**：

| worker | 失败点 | 原因 |
|---|---|---|
| browser-003 | `vl_no_screenshot` | 截图没产出 savedPath |
| browser-004 | `vl_viewport_unavailable` | `metrics.reason = "DOM.getAXTree failed"` |

`_viewport_metrics`（`harness/tools/browser_tools/captcha_autosolve.py:315`）**只从
一次新发的 `DOM.getAXTree` 取视口**，失败即 `oracle_unavailable` 直接放弃。而
browser-004 在同一页 7 分钟前刚成功读到 `2560×1600`。

验证码页恰恰是 DOM 读取和截图最先坏掉的地方——autosolve 把自己的前置条件建在了
被攻击的子系统上。

改动：视口取值加回退链 —— 本页最近一次已知视口 → `Runtime.evaluate` 读
`innerWidth/innerHeight`（这本来就是归一化坐标映射的正解，见
`qwen-vl-normalized-coords`）→ 截图尺寸 → 才放弃；截图失败加一次重试 + 换
`fullPage=false/true` 兜底。

**若 autosolve 真的工作，本 run 可能根本不会进 HITL。** 这比任何等待策略都靠前。

### 机制 B · attendance 模式：无人值守是部署事实，不是超时参数

**面板不提供 attendance 信号**（`global_schema_cache/schemas/` 下只有
`Hitl.requestPause` 和 `Hitl.resolvePause`），所以只能是**配置声明**，无法自动推导。

```python
hitl_attendance: str = "attended"   # attended | unattended
hitl_wait_timeout_seconds: float = 900.0        # attended 分支参数
hitl_max_pause_rounds_per_page: int = 3         # attended 分支参数
hitl_max_pause_rounds_per_worker: int = 3
```

- `unattended`：autosolve 失败 → **0 秒等待**，直接 `needs_human` 终态并释放租约。
  本 run 的 82 + 28 分钟全部归零。
- `attended`：900s × 3 轮熔断，即操作者确认的参数。

在无人值守分支上把 1200 调成 900，省的是错误场景的钱——这条 kimi 说得对。

### 机制 C · auth state 与 lease 分离 + 心跳；**过期只失败，不放行**

现状比 kimi 描述的好一点：`_AuthBarrierState` 里 `resolving`（状态）和
`resolver_worker_id`（租约）**已经是两个字段**，`abandon_worker` 干的正是
"状态留、租约走"。真正缺的是三件：

1. **租约没有存活性**——持有 ≠ 在进展。resolver 进 HITL 干等 77 分钟，barrier 无感。
2. **状态没有 `needs_human` 档**——只有二值 `resolving`，下游 phase 照常 spawn。
3. **状态不外发**——lead / spawner 读不到，worker 只能撞门才知道门还关着。

改动：`fleet.auth_state: ok | challenged | needs_human` 提升为一等数据并外发；
租约加心跳（resolver 每产生一次有效浏览器调用或挑战证据变化才续期），无心跳即过期。

> **安全约束（kimi 未提，必须写死）：租约过期不得开门。**
> 过期后放行 = 让其他 worker 去撞同一个仍在风控里的 cookie jar，这正是 barrier 存在
> 的理由。过期的正确语义是**把等待变成失败**，不是把等待变成许可：
> 状态保持 `challenged/needs_human`，等待方拿到终态回执而不是通行证。
>
> 这也是 v1 里"熔断必须同时释放 barrier"的正确版本——释放的是**租约**，不是**门**。

本机制覆盖 v1 的 #6（无主 TTL）、#7（resolver 健康）、#9（lead 可见性）。

### 机制 D · park-not-poll：等待是调度问题，不是认知问题

browser-005 的 30 次 LLM 调用是纯浪费——模型在"gated → 重试 → gated"循环里
做不出任何有价值的判断。

**实现比想象中便宜**：`before_call` 内部已经是 `condition.wait_for`，现在人为把它
截断在 `wait_timeout_seconds = 120s` 然后把"继续等"的责任推回模型。park 就是把这个
上限提到饥饿预算（例如 10 分钟），到期返回**终态**而非 retryable 回执——不是新机制，
是把已有的等待改成一次等到底。

> **前置依赖**：park 必须和机制 C 的"状态外发"一起上。否则 worker 从"看得见的空转"
> 变成"看不见的静默"，lead 更瞎。

30 次 LLM 调用 → 2 次（进 park 前 1 次 + 唤醒后 1 次）。

### 设计不变量（写进文档，防下一次重犯）

> 任何 wait 必须能回答三个问题：(a) 期望的进展源是谁；(b) 进展源的健康信号是什么；
> (c) 进展源死了谁兜底。三个答不上来的 wait 不许合入。

本 run 四个等待里三个答不上来（barrier 等待方等一个在等人的 resolver；HITL 等一个
不存在的人；lead 等一群在等门的 worker）。

### 止血项（与上述机制正交，可立即做）

| # | 改动 | 落点 | 风险 |
|---|---|---|---|
| 1 | reperception 白名单加入 `Hitl.requestPause` | `browser_tools/__init__.py:1815` | 极低，与 `:1782` 白名单对齐 |
| 2 | reperception 记账判据从 `_invoke_result_failed` 改为「调用抵达并返回回执」 | `browser_tools/__init__.py:1899` | 低，解开 `data.error` 死锁 |
| 3 | `VLConfig.captcha_solve_enabled: bool = True`，生效条件 `enabled and captcha_solve_enabled`；同步改掉 7-31 注释 | `runtime_config.py:VLConfig` | 零（默认不改行为） |

### 后续（顺序靠后，但已确认成立）

| # | 改动 | 说明 |
|---|---|---|
| E1 | 事件化 `wait_browser_agents`：任一 worker 状态变化即返回 | 同时解决"黑洞"和 loop_guard 换参绕过（根因是 wait 不在状态变化时返回，lead 只能原样重询才触发 streak），不需要给签名规则开洞 |
| E2 | `blocked_content_suppression` 的路由语义 | 未消耗 `max_attempts=3` 直接终态。unattended 下应译为 `needs_human` + 冻结依赖链，attended 下应挂起重试。分类→路由的语义错位，也是"代码判世界"的一例 |
| E3 | HITL 标题闩锁补 URL 变化证据通道（预算外 1 次 verifier） | 见 §1.3 |
| E4 | phase 记录 `workerStatusCategory` + `hitl_wait_seconds`（只记录不降级） | 见 §1.5 |
| E5 | 只读并行采集的会话隔离策略，在 plan 层显式权衡 | 见下 |

### 关于爆炸半径：共享 fleet 不只是共享故障，它**制造**了故障

kimi 提的"共享命运"成立，但证据比这更强——本 run 三个 worker 在 **16 秒内**从同一
cookie jar 打了三个 1688 详情页：

```
04:29:53  browser-002  →  被弹到 m 站（ERR_ABORTED）
04:30:00  browser-003  →  被弹到 m 站（ERR_ABORTED）
04:30:09  browser-004  →  直接落在 https://dj.1688.com/ci_bb?...（不是详情页）
```

三个全被拦。**并行触发风控 → 风控触发验证码 → 验证码触发全局锁 → 锁把三个 worker
串行化。** 共享 fleet 是这条链的起点，不是终点的受害者。

p2/p3/p4 是三个互相独立的只读详情页，是会话隔离的理想候选。是否共享会话应在 plan
层显式权衡（复用效率 vs 故障隔离），而不是事后靠 barrier 串行化补救。

---

## 2.5 收敛状态与两个待决项

三方（本方案 / kimi 审计 / 操作者）在以下各点已一致，不再讨论：

- 事实层：gated 消耗 step 但不吃失败预算；标题闩锁只在 VL 亲见时压制且逃生口仍在；
  autosolve 四个 episode 的失败层归属；共享 fleet 对风控是因果而非受害
- 机制层：A/B/C/D 四个机制；**租约过期不开门**（唯一合法出口是 takeover，且同一
  challenge episode 内 takeover 上限 2 次，超限翻 `needs_human`）；park 依赖状态外发；
  attendance 只能配置声明
- 开关语义：`captcha_autosolve_allowed() = vl.enabled and captcha_solve_enabled`，
  默认 `True` 保持现有行为
- 验收口径：机制 A 必须真实 1688 复跑验收，不能用单测宣告完成；止血项走 1799 基线
- 执行顺序：止血 1-3 + 机制 A + 机制 B 并行 → 复跑验证 → 机制 C（隔离决策先行）→ 机制 D
- 机制 A 每个回退阶段打遥测（如 `vl.captcha_autosolve.viewport_fallback`），否则无法
  度量救回率

### 待决 1：`Input.drag` 是否纳入机制 A —— 需要推翻一个旧前提

操作者 2026-08-02 裁定：「阿里 nc 滑块对轨迹做行为检测，一次性瞬移基本必被 FB1F4
拒，这里没有必要再做优化」。kimi 要求把拖拽重试纳入机制 A，与该裁定冲突。

**新证据要求重新审视该裁定的前提**（全部 worktree 的 `Input.drag` 只有 3 次）：

| 时间 | worker | 结果 |
|---|---|---|
| 2026-08-04 21:41:33 | a294ed5d / browser-001 | `success: true, dispatchMode: interactive` |
| 2026-08-05 04:30:42 | 48b4d7d7 / browser-002 | **`-32005 Action failed`（平台层，非 FB1F4）** |
| 2026-08-05 04:58:37 | 48b4d7d7 / browser-004 | `success: true` → 04:58:38 `vl_solved` → 04:58:39 **`challenge.autosolve_cleared`** |

即：**一次性瞬移拖拽在本 run 里真的清除过一次挑战**，且本 run 全程 **没有出现过
FB1F4**。VL 对这两个挑战的描述都是 "standard 'slide to verify' bar with **no puzzle
image or gap target**"——纯滑条，不是带缺口的拼图滑块。8-02 的裁定基于 a294ed5d 的
FB1F4（缺口拼图类），前提在纯滑条上不成立。

**建议**：轨迹模拟仍然不做（该裁定对缺口拼图类继续有效）；但把 `-32005` 当作普通
RPC 失败重试一次——这是"重试一次失败的调用"，不是"优化拖拽轨迹"，范围极小。
**需要操作者确认。**

### 待决 2：会话隔离 —— 已决（2026-08-05）

操作者决策：

1. **隔离单位：per-worker**
2. **同 fleet 同出口**：`Fleet.setProxy` / `Fleet.setFppPolicy` 暂不可用，无法给不同
   fleet 不同指纹和 IP
3. **登录态**：一个 fleet 一套指纹下不可能有同一业务网站的两套账号；用户自己在不同
   fleet 用不同指纹登同一账号是用户的选择，不在系统处理范围内

#### 后果 1：per-worker 隔离 —— 比"删掉一行配置"要多做一步

> **更正（2026-08-05，实测推翻本节初稿）**：翻转 `same_fleet_multiworker_enabled`
> **不等于** per-worker 隔离。跑真实 `FleetCoordinator`：
>
> ```
> 现状 flag=on + task 组键        -> w2 拿到 fleet-A   ★与 w1 共享 cookie 罐
> 只翻 flag：flag=off 无组键       -> w2 拿到 fleet-A   ★与 w1 共享 cookie 罐
> needs_isolated_session=True     -> w2 拿到 (None → 新建 fleet)
> ```
>
> 翻转只去掉**跨 slot 的 task 组键委派**；落到同一 slot 的 worker 仍被
> `choose_existing` 的 `slot_default` / `eligible` 兜底选到同一个 fleet。本 run 里
> browser-005 复用 slot-001，正会继承 browser-002 的罐子。
>
> 真正的 per-worker 隔离走 `needs_isolated_session`，已实现为
> `worker_session_isolation_enabled`（默认 False，本机 config.json 置 true）：
> 除非 contract 显式要求共享（`session_key` / `fleet_id` /
> `reuse_from_worker_id` / 显式 `needs_isolated_session` / pinned context），
> 每个 worker 自带一个 Fleet。那几个例外正是登录类流程——一个 Fleet 一套登录身份，
> 拆不开。



**共享不是因为任务文案里写了 "with fleet ec8846fb"** —— lead 从未把 `fleet_id`
传下去。5 次 spawn 的参数实证：

```json
{"workerId":"browser-002","sessionKey":"","fleetGroupKey":"task:48b4d7d7…","reuseScope":"fleet"}
```

唯一的分组来源是 `fleetGroupKey`，它来自 `_fleet_group_key`（`harness/spawner.py:1096`）：

```python
if not same_fleet_multiworker_enabled: return ""          # ← 默认，无分组
if session_key:            return f"session:{session_key}"
if needs_isolated_session: return f"isolated:{worker_id}"
return f"task:{task_id}"                                   # ← 实际走的这条
```

即 `same_fleet_multiworker_enabled: true` 的真实语义是「**整个任务共用一个 fleet**」，
不是「允许共享」。删掉 `config.json` 里那一行，组键变空，每个 worker 各自成 fleet。

> 待验证：无组键路径确实为每个 worker 建独立 fleet（代码如此，需一次 smoke 跑确认）。

#### 后果 2：隔离只买到"不扩散"，买不到"不发生"；剩下唯一的杠杆是速率

指纹和出口 IP 无法分散 → cookie 罐分开了，1688 仍可按 IP/指纹关联。本 run 的三连
拦截（16 秒内 3 个详情页同一出口）**在隔离之后仍会发生**，只是从「1 个验证码冻死
3 个 worker」变成「3 个各自的验证码」。

由此推出一个 v2 里没有的机制：

**机制 E · fanout 节流 / 错峰**。身份维度不能分散，速率是唯一剩下的杠杆。

> **暂缓实现（2026-08-05）——证据不支持我当初提的形状。** 本 run 的实际间距：
>
> ```
> spawn      browser-002 04:29:32 → 003 +10s → 004 +11s
> 首次导航    browser-002 04:29:53 → 003 +7s  → 004 +9s
> ```
>
> **已经是 7–11 秒错峰，三个还是全被弹。** 唯一能观测到的间距值证伪了"错峰"这个
> 机制本身；再拍一个 30s / 60s 就是"给症状配阈值"，正是这轮要避免的做法。
>
> 而且变量可能不是间距而是**并发度**：browser-001 单独打 www.1688.com 完全正常，
> 三个 worker 同分钟打 detail.1688.com 才出事。
>
> 需要的是一次受控实验（N 个 worker × 间距 × 是否隔离，量 bounce 率），不是先上
> 一个没人能正确设置的旋钮。数据本身已在 `run.jsonl` 里可事后算出，不需要新埋点。

以及一个优先级变化：

> **隔离之后没有 resolver 帮忙了。** 每个 worker 撞到验证码只能自解或者死。
> 机制 A（autosolve 健壮性）从"重要"升级为**唯一的生路**。

#### 后果 3：登录类任务应串行单 worker，而不是共享 fleet 多 worker

一个 fleet 只能有一套登录态 → 需要登录的任务无法 per-worker 隔离（只有一个 fleet
有登录）。两条路：

- (a) 共享 fleet 多 worker → barrier / 死锁面全部带回来
- (b) **串行单 worker** ← 建议

理由：登录态本身就是串行语义（一个账号一个会话；并行操作购物车 / 表单 / 限流本来
就危险），(a) 为一个本质串行的场景付出全套并发代价。选 (b) 之后
`same_fleet_multiworker_enabled` 可以永久保持 `false`。

#### 后果 4：机制 C 缩水、机制 D 删除

| 机制 | v2 | 决策后 |
|---|---|---|
| C 租约 / 心跳 / takeover / TTL | 必需 | **删除** —— 一 fleet 一 worker，它自己就是 resolver，无争用 |
| C `auth_state` 外发 + `needs_human` 路由 | 必需 | **保留** —— lead 仍需知道"这 worker 的 fleet 挂了且无人会救"，spawner 仍需据此冻结依赖链 |
| D park-not-poll | 必需 | **删除** —— 没有共享就没有 gated，没有可 park 的东西 |

**保留一份廉价保险**：若日后有人把 `same_fleet_multiworker_enabled` 翻回 `true`，
死锁面会整体回归。最小防线是 v1 的 #8（饥饿终态）—— 连续 gated ≥ N 次即终态失败，
约 30 行，替代整套租约机器。

### 执行顺序（决策后终版）

| 序 | 项 | 说明 |
|---|---|---|
| 1 | 止血 1-3 | reperception 白名单 / 记账判据 / captcha 与门开关 |
| 2 | **机制 A** | autosolve 全前置链（视口回退 + 截图重试 + 各阶段遥测）。隔离后是唯一生路 |
| 3 | 机制 B | attendance；`unattended` 分支 0 秒，`attended` 分支 900s × 3 |
| 4 | 配置翻转 | 删除 `same_fleet_multiworker_enabled: true` + smoke 验证每 worker 独立 fleet |
| 5 | **机制 E** | fanout 节流 / 错峰 |
| 6 | 机制 C 瘦身版 | `auth_state` 外发 + `needs_human` 路由，无租约无心跳 |
| 7 | 共享模式保险 | 饥饿终态，防配置被翻回 |
| — | ~~机制 D~~ | 删除 |

第 1、2、3 项互不相干，可同批完成；第 4 项之后必须真实复跑验收。

## 2.6 live 任务缺陷（2026-08-07，已实施）

三个缺陷同源：**把"我们的声明缺陷 / 未验证"归因成了"对方站点没有这块内容"**。

| 序 | 改动 | 落点 | 归格 |
|---|---|---|---|
| F1′ | `_marker_spec` 拆开 id 与 marker 槽位；只给 id 不给 marker 在计划期报错 | `content_completeness.py` | ① 表达能力 |
| F2′ | 页面健康 + 材料化已尽 + **全部 marker 零命中** → `marker_declaration_suspect`（一次），不再直接 `absent` | 同上 `_decide` / `terminal_veto` | ④ |
| F3′ | `field_nonempty` 区分 `confirmedEmpty` / `unverified`；`target_absent` 必须有视口位移或穷举证据 | `task_control.py` / `spawner.py` | ①④ |
| F2 | plan_validator 准则：marker 须是页面可见文本；禁止"凡 URL 列表必须非空"的死规则 | `plan_validator.py` | ③ |

**根因证据**（F1′）：`_marker_spec` 把裸字符串静默展开成
`{"id": name, "markers": [name]}`，于是 `expected_regions: ["sizeInfo", ...]`
让 harness 去中文页面上找字面量 `sizeInfo`，永远不中 → 永远 `absent` →
`route_recovery_required` / `blocked_content_suppression`。
所以这一条**不是"缺评审准则"，是代码缺陷**；裁判准则是第二道，不是第一道。

**F3′ 合并了 kimi 拆开的两半**：`detailImageUrls` 该不该非空，裁判在计划期
同样判不了（取决于这个商品有没有详情图）。7a 与 7b 是同一个病：
**空值必须携带"确实取过"的证据，否则是 unverified 不是 empty。**
先例已在仓库里——Download 路径的 `timeout_unverified` / `active_unverified`。

**F3′ 的证据来源**：平台不给任何回执形状（60 份 schema 全是 params，无 response
段），所以位移证据只能取自我们自己的账本——成功的 `Input.scroll` 或
`exhaustionEvidence`。真实 trace 复核：browser-001/002 `traversed=True`
（2 / 11 次滚动），browser-003 `traversed=False`。

## 3. 验证口径

每批完成后：

- `pytest tests/ -q` 全绿（当前基线 **1799 passed, 6 skipped**，用
  `/Users/versace/opt/miniconda3/envs/agent/bin/python -m pytest`，不要用
  `unittest discover`）
- 针对性回放：用 `worktree/48b4d7d7…/run.jsonl` 里 browser-003 的
  reperception 序列和 browser-004 的四次暂停序列构造回归用例
- 第二批需要一次真实并发跑（3 worker 同 fleet 打 1688 详情页）确认
  barrier 不再长时间关门
