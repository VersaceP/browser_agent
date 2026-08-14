# 基于 tau 源码研究的 harness 简化与经验闭环实施方案（v4.1）

- 日期：2026-08-13（v4.1，执行版；历经四轮交叉 review，分歧已全部收敛）
- 状态：待执行（自包含，执行者无需其他对话上下文）
- 研究对象：https://github.com/huggingface/tau （shallow clone，结论经源码核实）
- v4→v4.1：max_attempts 改声明式预算（无默认硬墙）；修复 partial 追溯计数矛盾；
  批 R 移到批 2 后并改读 raw attempts；challenge 结构帧移出硬信号；批 8/9 合并；
  compaction 成为批 1 明确交付项；判据三改 shadow A/B 回放；loop_guard 终态明确
  （warn 实为拦截，一并候删）

---

## 0. 背景与动机

### 0.1 病名与两条铁律

**病名：harness 级过拟合**——语义判断被写进代码（关键词表、枚举、阈值表、
指纹猜测、默认预算）替模型决定"该信什么结论/该不该停"。

> **铁律一（判定归属）**：代码只处理可由原始回执唯一计算的事实与安全边界；
> 模型判断这些事实意味着什么。所有模型判断在传递时必须保留归属：
> 谁说的、依据是什么、是否有反证。

> **铁律二（反预算反射）**：把语义裁决换成"机械重试 N 次"不是简化，是换一种
> 硬编码。**计数是事实，"N 次后必须停"是政策。** 合法的算术界只有两类：
> ①全局资源预算（Lead/worker step、token、time、quota、并发/限速）；
> ②**用户或 Lead 显式声明的资源预算**（执行声明是执行意图）。
> harness 不得发明默认的 phase 级次数墙。副作用安全靠 action receipt、
> 幂等键与确认边界，永远不靠重试上限。

推论（裁判改证人）：现有门里的**事实采集**（滚动回执、marker 命中、账本比对、
次数/增量统计、重复调用检测）一律保留并流入 handoff；**裁决权**（终态、否决、
拦截、强停）按本方案退役或候删。

### 0.2 实证坐标（执行者可复核）

1. 词法误召回：`authorName` 含 `auth` → 评论采集被注入登录策略
   （strategy_bank.json:292；strategy_attempts.jsonl:230，task 5e614a…）。
2. 限定词剥离链：worker「AXTree 未找到…**似乎**单页」（browser-002.jsonl:110，
   分类 target_absent）→ Lead「**确认**没有分页」（run.jsonl:919）→ plan v3
   「已确认的关键信息」（plan.0003.json:652）；SemanticTree:31136 就是「查看更多」。
3. partial 冻结：`mark_phase_result` 只看 `validation.status=="done"`；
   `WORKER_STATUS_PARTIAL → CATEGORY_DONE`（constants.py:234）。
4. 模型可自报 HITL 终态：`MODEL_ALLOWED_SOFT_STATUSES`（constants.py:257）含
   blocked_by_challenge / hitl_required / page_settled_after_hitl /
   stale_pause_deadlock（diagnostics/__init__.py:350 照单接受）。
5. content_completeness 在裁决：`terminal_veto()`（content_completeness.py:2110）
   经 spawner.py:234 可否决 shape-valid 的 artifact validation。
6. VL 终态 bounce 是一次性仪式：browser_tools/__init__.py:6902「One bounce only —
   a second attempt always goes through」。
7. S3 假锁风险：failureSignature 不含 rowCount/页面变化（task_control.py:4462），
   递增中的分页批次可共享同一失败签名。
8. objective_exhausted=6 硬停（task_control.py:5719）基于 prose-URL 指纹猜
   「两个计划语义相同」（代码注释自认 AUXILIARY）。
9. final 两病并存：06fc numeric 门 3 拒 4 步（2 次 extractor_unusable）；
   0f75 receipt 0/0/0 却 final「全部采集…已完全耗尽」（run.jsonl:511/514）。
10. 仪式门：stage_hint_reason 40 字符门（task_control.py:1342）。
11. **loop_guard warn 实为拦截**：达到 warn 阈值即拒绝执行本次工具
    （loop_guard.py:180「The tool is NOT executed this turn」）。
12. **partial 追溯计数矛盾**：`_count_budgeted_phase_attempts`（task_control.py:5620）
    只排除 interrupted；[partial, partial, validation_failed] 第三次后即
    phase_exhausted——partial 当时不触发门，事后仍被计入预算。
13. **max_attempts=3 是 harness 发明的默认政策**：prompt 指令
    （agent_harness.py:3653「Use max_attempts=3 by default」）+ 归一化
    （task_control.py:1623 `default=3`）+ spawn 拒绝（task_control.py:5692）。
14. 5e614 终止真因是 429 quota（run.jsonl:1114）——quota 中断样本，
    不是提前 final 样本。
15. 范围备注：docs/harness-mechanical-gates-analysis.md 自限「一次 browser_call
    链路」；本方案附录 A 是 browser_call 链路 + Lead 侧门禁合并后的判定归属清单。

### 0.3 tau 研究结论（源码核实）

| # | 机制 | 结论 |
|---|---|---|
| T1 | loop.py 328 行，仅 before/after_tool_call 两挂钩 | 运行时干预点应少而稳 |
| T2 | skill 索引进 prompt，正文模型自读；仅当有 read 工具才注入 | pull 式渐进披露 |
| T3 | 全仓 grep embedding/vector/rag = 0 | 文件系统即检索层 |
| T4 | 压缩=固定模板检查点，增量更新保留路径与错误原文 | handoff 格式现成 |
| T5 | 认识论规则=一句 guideline，非门 | 非拦截式引导 |
| T6 | prompt 文本归工具所有，组装去重 | system 由零件组装 |
| T7 | run 内 system/tools 不变 | 缓存纪律 |

边界：tau 是单 agent，无跨代理证据传递；借它的 pull 模式与投影格式，不以
「tau 没有」论证「我们不需要」。tau 检查点是 LLM 生成物，格式≠正确，
故 handoff 带归属标签，由消费方批判。

---

## 1. 执行铁律（每批适用）

1. §0.1 两条铁律与「裁判改证人」推论。
2. 不新增：embedding/检索中间件、新模型工具、意图触发器、status 枚举、
   task_state 字段、任何新的次数/阈值表、任何新的默认预算。
3. 单 run 内 system prompt 与 tools schema 不变；动态内容走 user message /
   tool result。
4. 仓规：通用层禁站点/字段硬编码；配置只加 runtime_config.py；测试口径
   `python3 -m pytest tests/`；tests/ gitignore 例外 test_spawner_slots.py。
5. 每批独立可测；报告贴 `git diff -- <触碰文件>`；输出中文。
6. 基线：2306 passed / 6 skipped / 2 failed。红线：numeric 红测试
   （unresolved-pass）归附录 B 立项，任何批不得顺手改；receipt 红测试
   （zero-receipt-done）由批 R 专属修复。
7. 状态语义迁移类改动（批 8）动手前必须先产出**消费点清单**（grep 全部引用
   逐一裁决），随 PR 提交。
8. 113/158/119 等具体行数只允许出现在 replay fixture 断言里，禁止进入生产规则。

---

## 2. 批次

> 顺序：批 -1 → 批 0 → 批 1 → 批 2 → 批 R → 批 7 → 批 3 → 批 4 → 批 8（含原批 9）
> → §3 replay 决策点 →（判据触发才做）批 6 与 progress/loop 拦截层拆除。
> 批 5 不实施。

### 批 -1 — 回放基线（不改行为）

`docs/replay-baselines/` 三份基线，三样本三用途（不得混用）：
- **06fc0bb4**：Lead 主动早 final（31/50）+ numeric 门摩擦（3 拒 4 步）；
- **0f75b23c**：receipt 0/0/0 却报完成；
- **5e614ada**：plan 仪式开销（3 版）+ partial 续跑 + quota 中断（接 /resume）。

五指标：plan/replan 次数、validator 审次数、final 重试次数、每步上下文大小、
system+tools prefix hash。

**回放设施要求**：fixture 支持 **enforcement 旁路模式**（shadow/A-B 用，见 §3）：
A 组保持现行为；B 组照常计算全部事实（重复次数、progress 计数、增量）但不拦截
调用、不强停。生产路径无任何开关变更。

### 批 0 — Strategy Bank 索引化 + 删词法评分

**文件**：harness/strategy_bank.py、agent_harness.py（≈3561-3648）、
harness/compaction.py

1. 删 `_keyword_hits` 词法评分。路由只剩 task_type + stage_hint 精确匹配。
   副作用注释：fallback/cross_cutting 条目不再自动注入（auth 有专职机械兜底）。
2. Lead system 撤 `<strategy_bank>` 全文（≈21.5KB）与相邻指令段；索引
   （id + 一行 description + 适用 stage，≤2KB）放**首条 user message**，附
   「需要全文用 local_fs_search 按 id 检索 strategy_bank/strategy_bank.json」。
3. 现 8 条无 description：一次性人工撰写，随 PR review。
4. known_skills 若全文内嵌则同样瘦成索引。
5. compaction `verifiedData` 更名 `persistedRows`（语义修复在批 1）。
6. worker 侧 spawn 注入路径不动。

**验收**：Lead system -20KB+；authorName 用例不再命中 auth 策略；对照基线
replay，plan 质量不退化。

### 批 1 — 唯一的有归属 handoff 投影（含 compaction 交付）

**文件**：harness/tools/lead_tools.py（wait 组装）、harness/offload.py、
harness/worker_result.py、harness/compaction.py

1. `wait_browser_agents` 每个完成 worker 内联投影（≤4KB/worker，超限截断；
   不进 generic offload；全量落盘附指针）。节名归属制：
   ```
   workerId / phaseId
   Original goal  ← phase objective 原文
   Raw receipts   ← 原始 status（partial 就写 partial）、validatedStatus、
                    artifacts(路径+rowCount)、attempts 计数、上轮 rowCount 与
                    本轮增量（重试场景）、traversal/滚动回执摘要、
                    strategy_attempts.jsonl 路径
   Worker claims  ← answer 截断 + 语义分类声明（标注"worker 自述，未经验证"）
   Unresolved / counterevidence ← blockers 原文（含"尚未证实/反证"）
   Suggested next experiment    ← nextSteps 原文
   Evidence paths ← trace / observations / 关键 offload 文件
   ```
2. **模型可见面不得出现 statusCategory**（partial 映射在 done 类会污染推理；
   category 仅供 harness 内部反应路由；值改名列为后续机械重构）。
3. worker 最终回执 prompt 加一句：blockers 必列尚未证实的推断与已知反证，
   保留精确路径与错误消息原文。
4. **compaction 明确交付（非"后续顺势"）**：compaction 的 worker 摘要改为渲染
   同一投影（六节全保留），删除仅凭 rowCount+savedPath 构造摘要的路径。
   **验收标准：压缩前后六节语义等价**——源投影中非空的 raw status / claims /
   unresolved / next experiment / evidence paths，压缩产物必须逐节保留。
5. 统一复用：wait、offload、批 3 validator 输入、continuation context、
   compaction 五方全部调同一投影函数，禁止各自从机械字段重新推导业务事实。

**验收**：145KB wait → Lead 直接可见 partial + blockers；投影无 statusCategory；
compaction 语义等价用例（含 blockers/nextSteps 的 worker 结果压缩后逐节在场）。

### 批 2 — 非对称信任：phase 生命周期修正

**文件**：harness/task_control.py `mark_phase_result`、
`_count_budgeted_phase_attempts`

1. `validated_done` 条件 = `result_status == WORKER_STATUS_DONE 且
   artifact_validation.status == "done"`。禁用 statusCategory。
   不改 CATEGORIES 映射本身（内部路由桶）。
2. partial（校验通过）→ phase 留非终态 `partial`。artifact 路径保留在
   phase attempt evidence 与 handoff 中，但不进入 `task_state.artifacts` 或
   `phase.validated_artifacts`；validated ledger 只承载可供 completion receipt、
   numeric facts 和 `batch_source` 信任的最终产物。
3. **partial 不进任何重试预算，含追溯**：`_count_budgeted_phase_attempts`
   （task_control.py:5620）改为同时排除 `interrupted` 与 `partial`——修复
   [partial, partial, validation_failed]→3 次→phase_exhausted 的追溯计数矛盾
   （§0.2-12）。
4. 零进展只作算术事实进批 1 投影（上轮 rowCount/本轮增量/attempts 计数）。
   代码注释：
   > 负面自评（partial/failed）可信其"未完成"；正面自评（done）不可信其"完成"。
   > 负面自评可以阻止终态；正面自评永远不能创造终态。终态仍由校验代码判。

**验收**：done+pass→validated_done；partial+pass→非终态且入账；failed+pass→不得
validated_done；**[partial×2, validation_failed] 后 phase 不因计数被拒 spawn**；
5e614 collect_comments 可续跑；正常任务终止不变。

### 批 R — completion receipt 终态矛盾检查（依赖批 2）

**文件**：harness/completion_receipt.py、harness/tools/lead_tools.py
（final_answer 路径）

唯一规则（确定性矛盾，非完整性证明）：

```
final status == done
∧ 当前计划存在要求产出 artifact 的 required phase
∧ 该 phase 的最新 raw attempt 未达（result_status==done ∧ validation==done）
⇒ 拒绝 done（返回具体矛盾 phase 与其状态，一次性可修复信息）
```

**实现要求**：直接读取各 phase 最新 attempt digest 的 raw status 与 validation
状态，**不信任 phase 级聚合状态**（防上游污染，双保险；这也是它排在批 2 后的
原因——批 2 之前 partial+pass 已被写成 validated_done，聚合态看不见矛盾）。

**禁止**写成 `validatedArtifacts == 0 → 一律禁 done`（浏览器操作/表单类合法无
extraction artifact）。命名与文案一律用「终态一致性」，不得用「完整性」。

**验收**：0f75 replay：0/0/0 + required phase 未完成 → done 被拒且点名矛盾
phase；无 artifact 需求的操作类任务不受影响；receipt 红测试转绿；numeric
红测试保持原样。

### 批 7 — plan schema 收敛（声明推导 + 删仪式 + 声明式预算）

**文件**：harness/task_control.py、harness/tools/lead_tools.py（emit schema）、
agent_harness.py（plan prompt 段）

1. `expected_artifact` 是唯一常规输出契约源：required_fields / field_nonempty /
   exact_rows 类 validators 机械派生（派生结果与现行结构完全同形，下游零改动）；
   只有不可派生的特殊校验显式写。
2. worker_contract 默认由 phase + 上一份批 1 投影自动构建；仅特殊权限/会话需求
   显式覆盖。
3. 删 stage_hint_reason 40 字符门（task_control.py:1342），字段改 optional；
   plan 级与 phase 级 task_type 去重；emit schema 与 plan prompt 段同步瘦身。
4. **max_attempts 改声明式预算（铁律二落地）**：
   - schema 改 optional，**无默认值**；归一化（task_control.py:1623）删
     `default=3`；prompt 删「Use max_attempts=3 by default」指令行；
   - 未声明 → 无 phase 级次数墙，有界性=全局预算；
   - 已声明 → spawn 达限拒绝，报文只说「本 phase 声明的资源预算已用完」，
     **不得**出现"目标不可行/必须 replan"类语义推导；
   - 计数事实（budgeted attempts）始终进批 1 投影。
5. 战术（selector/坐标/IIFE/下一实验）不回写 plan，经 spawn 参数与批 1 投影
   传递；plan 只存目标、依赖、交付物契约。
6. 顺手删 task_state 死字段 `banned_strategies`。

**验收**：派生 validators 与旧手写逐字段相等（对照用例）；无声明 max_attempts
的 phase 失败 N 次后仍可 spawn（全局预算内）；声明 max_attempts=2 的 phase 第
3 次 spawn 被拒且报文为资源措辞；plan prompt 与 emit schema 字数显著降；
5e614 场景 plan 版本数下降；全量绿。

### 批 3 — Reviewer 范围收窄 + 输入补全（依赖批 1、批 7）

1. validator 只审 **scope/topology 变化**（目标、phase 增删、依赖图、
   expected_artifact 契约）。**明确排除**：战术 continuation、
   **已声明 max_attempts 数值的调整**（资源分配，非拓扑）——Lead 续跑同一
   phase 不需伪造新 objective，也不触发全审。
2. review 输入加批 1 投影；prompt 加一句：plan 中「已确认/无 X」断言必须指向
   Raw receipts 证据；回执原文「似乎/未找到/仅单一观察面」不得升格。
3. 改名规避检测归它：replan 时同一目标换皮（对照 objective 计数与历史投影）
   作为**语义审查意见**提出，不作机械阻断。

**验收**：hedge-stripping replay 用例被拒；战术 continuation 与预算调整不触发
审；现有 plan_validator 测试绿。

### 批 4 — 认识论 guideline + 工具自带截断提醒

1. worker prompt 证据区一条（通用表述）：
   > 截断的搜索结果或单一观察面的未命中，只支持「有范围的未观察」结论；
   > 声明 absence 前必须列出已检查的观察面，并确认更完整观察面（若存在）已单独查询。
2. 搜索/枚举类工具 `truncated=true` 时回执自带一行：「结果已截断，仅证明命中项
   存在，不证明未命中项不存在」。

**验收**：worker 侧净增 ≤3 行；截断回执行含单测。

### 批 5 —（不实施）strategy_attempts 保持审计态

多策略共现同一失败无法归因，注记信息量趋零。保留 append-only telemetry 供审计
与批 6 蒸馏；批 1 投影重试场景带账本路径，模型自行读取。

### 批 8 — 语义裁决权退役（含原批 9；依赖批 1；先出消费点清单）

**文件**：harness/task_control.py、harness/constants.py、
harness/diagnostics/__init__.py、harness/spawner.py、
harness/tools/browser_tools/__init__.py、harness/content_completeness.py

一次迁移合并完成（原批 8+9 分开会留下「content veto → validation_failed →
计数」的中间态污染路径），一份消费点清单覆盖全部。

**8a 语义终态退役**：
1. target_absent / instruction_infeasible / blocked_content_suppression 移出
   phase 状态空间——不再 TERMINAL，也**不移入任何重试预算**（铁律二）。worker
   分类与证据（含 spawner.py:6491 起的 traversal/滚动回执、账本比对——采集
   保留）写入 attempt digest 的 claim 区。phase 机械结果只由 artifact validation
   决定。
2. 删 S3 同签名硬锁（spawn rejection + phase_locked_must_finalize）：签名、
   行数 delta、artifact delta 保留为投影事实。
3. 删 objective_exhausted=6 硬阻断（task_control.py:5719）：objective 计数保留
   为投影事实与批 3 审查输入。
4. 删 VL layer-3 终态 bounce（browser_tools:6902）；预算内 reality_check/VL
   仲裁保留为 advisory。

**8b HITL 按证据来源**：
5. `MODEL_ALLOWED_SOFT_STATUSES` 收缩为 {done, incomplete, partial,
   extraction_inconclusive}。模型自报 blocked_by_challenge / hitl_required /
   page_settled_after_hitl / stale_pause_deadlock 时：**diagnostics 账本存在
   pause 机器回执**（requestPause 成功、paused 通知、wait timeout、resume、
   session lost）→ 维持该状态；无回执佐证 → 归一 incomplete + claim 原文进投影。
   **DOM/AX/VL 的 challenge 结构检测不是硬信号**——它是语义检测，只能作为
   请求 pause 的依据（观察/advisory）；随后真实进入 paused 的回执才是硬事实。
   机械路径（wait_for_hitl_resume 七出口）产生的 hitl_* 终态不变。

**8c content_completeness 降观察器**：
6. `terminal_veto()` 改名为仅供诊断的 `unresolved_observation()`；删除
   `_apply_content_completeness_validation_veto` 裁决权，并从模型可见结果删除
   route_recovery_required / blocked_content_suppression 等决策状态与指令输出。
7. 保留并转投影的事实：命中 marker、声明区域缺失清单、已做点击/导航/滚动及
   回执、尝试次数、页面变化。per-page 观察进批 1 投影与 attempt digest。
8. Lead prompt 的 content_completeness 声明指令段（agent_harness.py:3665）随
   裁决权退役同步简化为「声明 markers/regions 供观察采集」。

**消费点清单必须覆盖**：TERMINAL_PHASE_STATUSES / RETRYABLE /
BLOCKING_DEPENDENCY_STATUSES / REPLAN_RESET_STATUSES /
SEMANTIC_TERMINAL_CLASSIFICATIONS / replan checkpoint 逻辑（objective_exhausted
退休原因）/ progress 门对语义终态与 completeness 的引用 / spawner veto 调用点 /
diagnostics 状态归一化。

**验收**：假 absent replay（5e614 分页、淘宝懒挂载构造用例）：phase 不被冻结，
Lead 收到带观察面证据的 claim；真 absent：validation 失败后由 Lead 决定
final/replan（无声明预算时不被计数拒绝）；模型无回执自报 hitl_required → 归一
incomplete 且 claim 可见；机械 HITL 路径行为不变；shell-marker 用例：投影含
缺失区域事实、validation 不再被机械否决；route-sensitive replay 场景由模型驱动
完成恢复。

### 批 6 — cases/ 案例库（条件启动，见 §3）

`strategy_bank/cases/<slug>/CASE.md`（不用 SKILL.md 名），frontmatter
`kind: case`，description 以结构化键开头（method + 错误签名 + 观察面）；
distill_trace + /skill-create 质量门产出；读侧 v1 pull（索引进 worker 首条
user message，仅当持有 local_fs 工具）；v2 精确键 push 不实现，仅判据二成立才
评审。多案例跨站复现 + replay 验证后才蒸馏升格为 bank 策略。

---

## 3. replay 决策点（批 8 后）

三样本重放，三判据。**判据三必须用 shadow A/B**：

- **判据一（批 6 立项否）**：模型在「truncated + 单面未命中 + 更完整面未查」下
  是否自行继续验证；
- **判据二（cases v2 push 否）**：cases 试点后模型撞同签名错误是否主动检索；
- **判据三（progress/loop 拦截层拆除否）**：A 组保持现行为；B 组**旁路全部
  enforcement**（不拦截、不强停）但照常计算并注入事实（「同签名连续 N 次，
  上次回执已含本次将返回的内容」「过去 N 步 artifact 增量 0」）。比较 B 组在
  批 1 投影 + 批 4 guideline 下能否自行转向或终止。
  - 结论为能 → 拆除：loop_guard 的 warn 拦截（当前 warn 即拒绝执行，
    loop_guard.py:180）与 force 强停一并删除，仅保留检测 + 事实注入；
    progress 八子门硬拦同步降为投影事实；
  - 结论为否 → 维持现状，出差距分析再评。
  - 生产行为在结论前不变；旁路只存在于 replay fixture。

按证据升级/拆除，不按担心预建，也不按哲学批量拆。

---

## 附录 A — 判定归属总清单（browser_call 链路 + Lead 侧合并）

**本方案删**：词法评分（批 0）；40 字符门、banned_strategies、plan 三重复述、
**默认 max_attempts=3 政策**（批 7）；语义终态熔断、S3 硬锁、objective_exhausted
硬锁、VL 终态 bounce、模型自报 HITL 终态白名单、content_completeness 裁决权
（批 8）。

**replay 判据三候删（shadow A/B 后）**：progress 八子门硬拦、loop_guard warn
拦截与 force 强停（检测与事实注入永久保留）。

**卫生项**：page quarantine 无 TTL（中危，修 TTL）。truncation 分桶方案撤回
（不加阈值表）。

**明确保留（安全/算术/权限）**：JSON/schema 合法性、capability 与 Runtime
read-only、fleet/page/session 绑定与 stale handle、pause/resume/timeout/
session-lost **回执**判定（机械路径；结构检测只是请求 pause 的依据）、文件存在/
hash/下载对账、明确数值账本冲突（附录 B 边界内）、全局 step/token/time/并发/
限速、quota 冷却与 checkpoint/resume、批 R 终态矛盾检查、依赖执行顺序（语义
失败不再传播为依赖终态——随批 8 消解）、**显式声明的资源预算**、O4/O5 状态
边界、S4 infra 预算。

## 附录 B — 独立立项：numeric claim 门（本方案不实施）

边界（06fc 实证：3 拒中 2 次 extractor_unusable）：
- 已解析、已绑定、与账本数值明确冲突 → 硬拒（确定性比较）；
- 全部 unresolved → 报告不得为 passed（红测试保持），但不阻断 final，结果进
  receipt 与模型自审上下文；
- extractor 不可用/不可解析/span 绑定失败 → 检查器自身故障：不阻断、不驱动
  格式重写循环，receipt 记「本答案未经数值核验」；
- coverage/unresolved → 模型自审上下文。

## 附录 C — 复杂度账目（v4.1）

**删**：词法评分、21.5KB bank 内嵌、40 字符门、banned_strategies、plan 三重
复述、默认 max_attempts 政策、语义终态熔断、S3 硬锁、objective_exhausted 硬锁、
VL 终态 bounce、模型自报 HITL 终态、content_completeness 裁决权、（判据三后）
progress/loop 拦截层、（拟）战术出 plan。
**改**：1 投影函数（wait/offload/validator/continuation/compaction 五方复用）、
1 生命周期判定 + 1 计数谓词、validator 派生、reviewer 范围、2 句 guideline、
1 更名、MODEL_ALLOWED 白名单收缩。
**增**：批 -1 基线（含 enforcement 旁路 fixture）、批 R 一条矛盾检查、
（条件）批 6 cases 目录。
**净**：裁决点大幅减少；新子系统 0；新模型工具 0；新枚举/字段/阈值表/默认预算 0。
