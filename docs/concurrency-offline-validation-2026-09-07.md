# 并发修正与离线验证（2026-09-07）

## 范围

本轮先修正 Lead 提示词，再移除隐式 pathfinder 阻塞。未运行浏览器、未调用 LLM，也未改变 P3 数量解析、P4 fleet 错误提示或字段语义审查。原有工作区有大量未提交修改，本轮以修改前文件快照为基线，没有回退那些修改。

提示词区分计划声明与 worker 派发：已知实体在初始计划声明，动态 URL 从已验证产物绑定；未知实体通过有理由的完整 replan 声明；分组和探路由 Lead 根据证据决定，不强制固定算术分组。独立生产者声明 `depends_on=[]`，消费者只依赖真实输入，派发前检查就绪阶段和剩余容量。精确行数可支持批次输入绑定，不会自动创建并发阶段。

删除 `_pathfinder_gate_rejection` 及其调用。相同 stage/task_type 不再形成隐式等待。保留显式依赖、运行容量、slot 容量、身份绑定和 sibling handoff；已有状态查询与 handoff 提供决策事实，不新增替代门禁。

## 按顺序验证

| 阶段 | 修改变量 | 结果 |
|---|---|---|
| 基线 | 无 | 原有相关 78 项测试通过 |
| A | 仅提示词及对应旧断言 | 同一组 78 项测试通过 |
| A 的新增行为测试 | 固定计划、运行状态和 spawn 请求 | 独立同类阶段在同 fleet、另一 fleet 两个子用例均被旧门拒绝；显式依赖、就绪消费者、已完成 handoff 用例通过 |
| B | 删除隐式门，保持 A 的派发策略提示词；同步删除过时的门名称说明 | 最终相关 79 项测试通过，包含此前两个失败子用例 |

79 项不是对 78 项简单增加一项：移除了 7 项旧门专属测试，增加 4 项 admission 测试，并加入 4 项现有容量／身份边界测试。

新增 admission 测试经过真实 `_lead_spawn_browser_agent`，仅替换最后的 spawner 调用，不调用浏览器或模型。它证明请求能够到达 spawner，不代表真实 worker 已经并发执行。容量和身份由另外的现有 spawner 测试覆盖。提示词字符串断言只检查文本合同，不能证明模型会遵循。

## 历史状态回放

读取任务 `4cc0774f0a354d058b8b0ced876957d5` 的实际 plan 和 attempt 时间，截去指定时刻之后的状态，调用现有依赖判断及修改前快照中的 pathfinder 函数。以下时间均为 UTC：

| 时刻／请求阶段 | 依赖判断 | 修改前隐式门 | 删除隐式门后的含义 |
|---|---|---|---|
| 02:48:19 / fleetB_collect | A 列表尚未验证 | 未阻塞 | 依赖仍阻塞；需由新计划明确独立 |
| 02:55:49 / fleetA_detail | 满足 | 未阻塞 | 本来就可考虑派发，属于 Lead 调度选择 |
| 03:14:12 / fleetB_detail | 满足 | pathfinder_in_flight | 这一隐式拒绝来源已删除；完整 admission 行为另由测试覆盖 |

这不是浏览器轨迹重放，也不是吞吐模拟；不推算“修复后几分钟完成”。

## 复跑

从仓库根目录运行，无模型与浏览器调用：

```sh
/Users/versace/opt/miniconda3/envs/agent/bin/python -m unittest \
  tests.test_concurrency_admission \
  tests.test_phase_scheduling \
  tests.test_field_semantics \
  tests.test_spawner_slots.BrowserAgentSlotTests.test_lead_prompt_declares_identity_coverage_and_route_pivot \
  tests.test_spawner_slots.BrowserAgentSlotTests.test_running_cap_rejects_with_limit_semantics \
  tests.test_spawner_slots.BrowserAgentSlotTests.test_concurrent_slot_reservations_respect_instance_cap \
  tests.test_spawner_slots.BrowserAgentSlotTests.test_existing_fleet_reference_failure_never_creates_replacement \
  tests.test_spawner_slots.BrowserAgentSlotTests.test_existing_fleet_reference_conflicts_fail_before_slot_acquisition -q
```

`tests/` 按仓库现有规则被 gitignore；新用例当前保存在本地，尚未提交。测试有现存 `datetime.utcnow()` 弃用提示，与此次修改无关。

## 更便宜的模型行为验证

下一层可以只让 Lead 生成初始计划，或在固定历史切点输出一轮工具调用，禁止执行工具后续动作。旧／新提示词使用相同完整运行上下文、工具 schema、模型配置、产物和状态，做少量配对采样；用原始目标人工／语义评审检查真实依赖、实体覆盖、划分理由、就绪派发和身份边界，不强制某个唯一计划形状。

只使用切点当时已可见的证据，不把未来 worker 结果放入输入。需要同时检查产物绑定合同是否有效；更敢并发但字段、实体或 fleet 错误不是改善。

初始计划草案只含部分 runtime limits，未作为有效样本使用。该短测可以验证模型决策倾向，仍不能证明网页成功率、风险触发率或墙钟收益。只有前两层通过后，才考虑一次小范围 live 确认，无需每次完整重跑七个商品。

## 实际短 A/B 尝试结果

已执行 `scratchpad/short_ab_runner.py`。真实请求使用配置中的 Lead 模型和相同的 A/B 输入；`spawn_browser_agent` 是 record-only，浏览器没有启动。

结果没有形成有效样本：

- 3 组短配置配对（共 6 次请求）均在 1 个模型 turn 内返回 hidden thinking，未产生 `emit_task_plan` 或 `spawn_browser_agent`。
- 1 组显式 4096 thinking budget 校准（共 2 次请求）仍未产生工具调用。
- 1 组生产 24000 token 配置校准（共 2 次请求）也都消耗到 `output=24000`，`acceptedPlan=false`、`toolCalls=[]`。

因此不能从这些请求推断 A 优于 B 或 B 优于 A。阻塞来自当前模型网关的输出预算／hidden thinking 行为，而不是新旧提示词的机械差异。继续增加样本只会重复消耗模型 token，暂不继续。

留存的结果文件为 `artifacts/short_ab_20260907.json`、`artifacts/short_ab_20260907_no_thinking.json`、`artifacts/short_ab_20260907_calibration.json` 和 `artifacts/short_ab_20260907_production_calibration.json`；runner 位于 `scratchpad/short_ab_runner.py`。

## 固定历史切点的单轮派发 A/B

在用户确认后，改用任务 `4cc0774f0a354d058b8b0ced876957d5` 的 `2026-09-07T03:14:12Z` 固定状态：A、B 的列表阶段均已验证完成，A 详情正在运行，B 详情待派发。每一臂得到完全相同的原任务、已接受计划、状态快照、runtime limits 和唯一可见工具 `spawn_browser_agent`；只记录模型工具调用，runner 不执行工具、不开浏览器、不修改任务状态。期望调用为 `fleetB_detail`，带该阶段原有 fleet 和 `reuse_scope=page`。

| 提示词 | 记录轮数 | 期望调用 | 未调用 |
|---|---:|---:|---:|
| A：修改前提示词 | 3 | 2 | 1 |
| B：当前提示词 | 3 | 3 | 0 |

A 的失败轮以 `end_turn` 结束且没有工具调用；其余五轮均输出预期阶段、fleet `fleet-bf6156f4-0f51-4889-91c0-23a819a3205a` 和 `reuse_scope=page`。所有请求的 timeout、connection 和 degenerate retry 均为 0。

这个切点给出了有限的方向性证据：当前提示词在三次配对样本中没有漏掉已就绪的 B 详情，而旧提示词漏掉一次。样本量很小、模型输出有随机性，且该输入把候选阶段压缩为一个，不能据此证明初始计划质量、所有淘宝任务的并发吞吐或实际网页成功率更高。核心并发修正的确定性证据仍是 admission 单测和历史状态回放：显式依赖已满足时，旧的 `pathfinder_in_flight` 隐式拒绝不再阻断请求到达 spawner。

结果文件为 `artifacts/short_ab_dispatch_20260907_calibration.json` 和 `artifacts/short_ab_dispatch_20260907_replication.json`；runner 为 `scratchpad/short_ab_dispatch_runner.py`。为消除偶然性，下次更合适的是增加不同历史切点，而不是在这一切点大量重复抽样。

## 本地审计材料

修改前源文件、提示词阶段快照、两份完整系统提示词、输入草案、历史回放 JSON 和仅本轮差异保存在 `/tmp/abcp-concurrency-review-20260907/`。这些是本机临时审计材料，不能替代版本库提交或长期实验存档。
