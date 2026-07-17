# Fleet P1+P2 代码 review 完整发现（2026-07-16）

> **历史台账，不代表当前代码状态。** 原始发现 1–10 来自 Claude；3b 是
> ChatGPT 提出、Claude 复核确认的竞态；“对 ChatGPT 修复计划”的段落是 Claude
> 对当时方案的裁决。本文件不混入后续 GLM review。当前实现与验收状态以
> `docs/fleet-reuse-architecture.md` 和现行测试为准；其中 `mode="single"` 已按产品
> 决策整体删除，不能把下文第 6 条误读为恢复 single 的待办。

Reviewer/source: Claude（主会话内完成 8 角度审查；子代理因会话限额不可用）。
范围：工作树未提交 diff（13 文件，~957 行新增）+ 未跟踪新文件 `harness/fleet_coordinator.py`。
回归证据：`python3 -m pytest tests/ -q` → **1152 passed / 6 skipped**（含本次新增 7 测）。
平台事实核验：对照 `abcp-platform/` 源码逐条验证（Page/create/exec.ts、fleetRepository.ts、server.ts、memoryService.ts）。

判定说明：CONFIRMED = 代码路径直接可证；PLAUSIBLE = 需要特定但现实的状态/时序。

## 发现（按严重度排序）

### 1. [CONFIRMED/correctness] 隔离 fleet 变成 slot 默认 — fleet_coordinator.py:272
`bind_assignment` 无条件执行 `self._slot_defaults[assignment.slot_id] = fleet_id`，
包括 `needs_isolated_session` 新建的 fleet。
后果：worker-B 声明隔离 → 新建 fleet-Y 成为 slot 默认 → 下一个普通 worker 经
`slot_default` 命中 fleet-Y，与"隔离"会话共享 cookie/storage；更早 worker-A 的
page-continuation 的 `allowed_fleet_ids` 也被指到 fleet-Y，其真实 page 候选被
slot_context 过滤消失。

### 2. [CONFIRMED/correctness] 不同 session_key 坍缩进同一 fleet — fleet_coordinator.py:193
`choose_existing` 不排除已绑定到**其它** session_key 的 fleet；新 session_key 无既有
绑定时落进 `slot_default` 分支。
后果：phase-1 `session_key="shop:A"` 绑定 fleet-A（成为默认）；phase-2
`session_key="shop:B"` → 命中 slot_default=fleet-A → `bind_assignment` 把 shop:B
也映射到 fleet-A。账号 B 的登录流程跑在账号 A 的 cookie 罐里，两个 key 此后永久
指向同一 fleet。

### 3. [CONFIRMED/correctness] session 绑定静默重绑 — fleet_coordinator.py:274（根因含 observe_slot:132-146）
绑定的 fleet 不在所选 slot 候选中时（如原 slot transport 损坏被
`_cleanup_retired_slots` 回收），`choose_existing` 静默选择其它 fleet 且 L274 重写
`_session_bindings[key]`；`observe_slot` 在 fleet 消失时直接删除绑定，使同 key 下次
如首次使用一样重新选 fleet。现有测试
`test_fleet_coordinator_prefers_session_then_invalidates_stale_binding` 把该错误行为
固化为断言（断言 affinity 变 None）。
后果：已验证登录的会话静默降级为全新 fleet，worker 以为持有会话实则未登录，继续
执行写操作，无任何 `session_fleet_lost` 信号。

### 3b. [CONFIRMED/correctness] 首次绑定并发竞态 — spawner.py:377 + 1824（ChatGPT 提出，已核实）
assignment 在 `_run_browser_worker`（async task，L1824）内才发生，而
`spawn_browser_agent` 在 `create_task`（L377）后立即返回 running。同一 lead 回合并发
两个相同 session_key 的 spawn：两者都在绑定建立前通过 `preferred_slot_for_session`
检查 → 各占一个 slot、各建一个 fleet → 双重绑定，后写者覆盖。
修复方向（认可 ChatGPT 方案）：把 slot 准备 + fleet assignment 前移到
`spawn_browser_agent` 返回之前，并加 assignment/reservation 锁；失败时释放 slot 并
`cancel_phase_running_reservation`。

### 4. [PLAUSIBLE/correctness] 运行中 fleet 被归档无出路（约定的 recovery 路径未实现） — browser_tools/__init__.py:843
Fleet.create 对 worker 全面拒绝 + `_recover_page_create_32005` 限定 assigned fleet，
但计划约定的"fleet 损坏 + recovery authorization → 协调器建替代 fleet"未实现。
后果：用户在 User UI 归档/重置该 fleet 时 worker 正在运行 → 每次 Page.create 返回
"Fleet X is archived"，worker 烧完步数终态失败，只能整次 respawn。
（注：client 崩溃≠归档——markOffline 只写 orphaned_at 不改 status，Fleet.list 仍列出，
显式 Page.create 会经 ensureClient 自动重启，该场景无需处理。）
（07-16 补充事实：agent 侧 Fleet.close 实为置 'prepared' + 清所有权，任意 agent 凭
fleetId 可 claimPrepared 复活——guard 代码里 "close archives the reusable session"
的措辞不准确，但结论反而更强：worker 泄漏/误用 Fleet.close ≈ 把会话让渡给任意
claimer，拒绝 Fleet.close 的策略必须保留，文案应改为所有权让渡风险。）

### 5. [PLAUSIBLE/correctness] System.register 刷新只增不删，30s TTL 窗口内可指派已归档 fleet — spawner.py:745
`_prepare_slot_for_worker` 每 worker 都调 System.register（返回权威 data.fleets），但
`_update_slot_registry_from_value` 只做增量；权威删除只在 `_sync_slot_registry`
（SLOT_FULL_SYNC_TTL_SECONDS=30 门控）。
后果：上次 sync 后 30s 内 fleet 被归档 → 新 worker 仍被指派该 fleet → 整个 attempt
的 Page.create 全部失败。修复便宜：register 响应同样做替换式收敛（同 Fleet.list）。

### 6. [PLAUSIBLE/correctness] single 模式任意复用残留 fleet — agent_harness.py:879
`_ensure_fleet_assignment` 每次新建 FleetCoordinator，所有候选 last_used=0 并列 →
max() 实际按 fleetId 字典序取最大 = 任意残留 fleet（可能带陈旧登录态）被静默复用，
reason 标 "slot_healthy_fleet" 却无健康/身份探测。

### 7. [CONFIRMED/efficiency] 绑定回执污染每一个 browser_call 结果 — browser_tools/__init__.py:1130
`result.update(fleet_binding_receipt)` 把 assignedFleetId/assignmentReason/
fleetInjected:false 三键 merge 进**每个**结果（含与 fleet 无关方法）。40 步 worker
= 每步 3 个无关键进入模型上下文、trace、l2 汇总。建议只在 Page.create/Fleet.* 或
fleetInjected=true 时附带。

### 8. [CONFIRMED/reuse] 校验与提取器三处重复 — task_control.py:443
reuse_scope/page_policy 枚举 + "existing 需 page" 规则在 validate_task_plan 重编码，
未复用 normalize_reuse_scope/normalize_page_policy（漂移风险）；
`_extract_fleet_items` 近拷贝 `_extract_page_items`；`fleet_ids_from_value` 重复
spawner 注册表 walker（三者各自 skip methodSchema）。

### 9. [CONFIRMED/correctness-low] FleetRecord 永不清退，fleetRouting 误导 — fleet_coordinator.py:299
记录无淘汰，`slot_snapshot`（list_browser_agents 的 fleetRouting）把已归档 fleet 永久
报成 status=active；映射随长会话无界增长。（ChatGPT 计划第 3 点的 status=missing
可一并解决。）

### 10. [CONFIRMED/simplification] locals().get("assignment") 惯用法 — spawner.py:2069、2130
异常/收尾路径用 `locals().get("assignment")` 取回局部变量，对重构脆弱（改名后恒
None，失败路径静默丢失 fleetAssignment 字段）。改为 try 前初始化 `assignment = None`。

## 对 ChatGPT 修复计划（6 点）的修订意见

整体认可，暂停 P3 先修正确。三处修订：

1. **"新 session 只能认领未绑定 fleet"改为"新 session_key 一律新建 fleet"**
   （assignment_reason=session_bootstrap）。认领共享默认 fleet 有两个问题：
   (a) 把带累积 cookie 的共享罐子交给具名会话 = 起点污染；
   (b) 该 fleet 随即变 session-bound，普通任务被排除 → 被迫再建，净开销相同但语义更脏。
   显式交接（reuse_from_worker_id 指向的 fleet 且该 fleet 未绑定其它 key）可作例外。

2. **点 3 的 missing 语义补平台事实**：fleet 从 Fleet.list 消失当前只有归档一种成因
   （offline 仍列为 active），归档 = 用户显式 reset 前不可复活 → `session_fleet_lost`
   在进程内基本是终态，lead 收到后应走 auth-interrupt SOP 重登录（配合 auth ledger
   的 stale 流程），文档要写明这不是可重试状态。fleet 重新出现恢复 active 的逻辑保留
   （对应用户 reset 后重建同名场景）。

3. **顺手修发现 5**：竞态修复把 assignment 前移后，`_prepare_slot_for_worker` 的
   System.register 响应应与 Fleet.list 同样做替换式收敛，一行工作量，消掉 30s 窗口。

回归测试清单认可，补一条：**同一 slot 上隔离 worker 之后的 page-continuation 仍能
看到自己 fleet 的 page 候选**（对应发现 1 的第二后果）。

## 提交提醒
`harness/fleet_coordinator.py`、`docs/fleet-reuse-architecture.md`、本文件均为未跟踪
新文件，commit 须显式 `git add`；`tests/test_spawner_slots.py` 是 gitignore 跟踪例外，
单独 add（勿只 `git add -u`）。
