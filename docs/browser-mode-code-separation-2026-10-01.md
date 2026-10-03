# Browser 模式拆分与 beb662 修复

本轮按已确认的两批计划实施：先修复已核实的问题，再拆分模式代码。没有新增站点、字段或业务完成条件的机械门禁，没有更改任务预算或压缩阈值。

## 入口和依赖

过去 Browser 模式实例化 `LeadAgent(provider=None)`，借用它的启动、派发、收集和恢复服务。这是 Browser 入口借用 Lead 的实现；Lead 使用 Browser worker 的执行能力，并不构成两个模式互相递归调用。

现在 Browser 入口实例化 `BrowserTaskRunner`，直接使用共用 spawner。它不导入 LeadAgent、Lead 派发工具或委派复核。Lead 仍可创建 BrowserAgent worker，两者共享一份浏览器执行循环。

```text
main.py
  browser → harness/agents/browser/task.py → BrowserAgent → browser/loop.py
                                             └ task_control → browser/control.py → browser/review/
  lead    → harness/agents/lead/agent.py → lead/loop.py → 共用 spawner → BrowserAgent

共用：capabilities/bootstrap、runtime/model_support、runtime/worker_recovery、
      observation、fleet、tools、storage、task_control、spawner
```

阅读 Browser 模式建议从下面四处开始：

| 文件 | 职责 |
| --- | --- |
| `harness/agents/browser/task.py` | 直接启动、旧任务恢复、用户补充指令、收集终态、取消收尾 |
| `harness/agents/browser/control.py` | TODO 恢复与更新、复核触发、完成前复核、压缩后补入持久状态 |
| `harness/agents/browser/loop.py` | BrowserAgent 共用执行循环、模型和工具协议处理 |
| `harness/agents/browser/review/` | 独立复核上下文、只读工具、证据与问题记录 |

BrowserAgent 的状态与页面方法在 `browser/agent.py`，通用浏览器提示在 `browser/prompt.py`。Lead 的状态、循环和提示分别在 `lead/agent.py`、`lead/loop.py`、`lead/prompt.py`。

根目录 `agent_harness.py` 仅保留兼容导入，没有 Agent 实现。原 `harness/browser_*review*.py` 四个兼容入口已删除；生产代码、测试及 mock 均直接引用 `harness/agents/browser/review/` 下的实现。

## 保持兼容的任务记录

SQLite 存储和旧 phase 记录保持原格式。`direct_worker` 和早期 Browser 任务使用的 `delegated` 单任务记录仍可恢复。这里的 phase 是启动与恢复记录，并非让 Lead 重新规划任务。

BrowserTaskRunner 直接派发时保留原始请求、用户补充指令、Fleet 身份选择、指定 Skill 版本、历史 handoff 和 spawner 的恢复提示。通用状态检查仍阻止重复启动正在运行或已终结的 phase。

连接故障后的控制面探测提取到 `runtime/worker_recovery.py`，两种入口共用。它只探测连接，不重放浏览器动作或重新派发 worker。取消时仍先排空 worker 与恢复任务，再关闭存储。

## 本轮行为修复

1. **导航额外等待。** `navigate_verified` 已观察到非过渡标题的 `ready` 或失败终态，而预期 URL/标题不匹配时，返回真实的不匹配/失败回执。取消尚未完成的后续重定向等待，不再等待到 45/50 秒期限。已到达的重定向事件、仍加载的页面及过渡标题保持原处理。
2. **复核工具前缀。** progress 与 final 使用相同工具定义，减少因工具枚举变化导致的缓存前缀变化。当前复核阶段能否提交某种判决仍由原有判决协议校验。
3. **SQLite 虚拟文件信息。** `review_file_info` 对任务内数据库资源查询虚拟文件视图，读取大小与哈希，不要求 worktree 下存在物理日志。
4. **目录观察连续性。** `review_list_files` 的返回保存为任务所属的不可变观察，记录资源 URI、版本、运行身份和内容哈希。后续复核可复用该捕获时的证据；它不证明目录当前仍相同。
5. **收尾复核触发。** BrowserAgent 在 `update_task_todo` 中声明 `ready_for_final_review=true`，表示下一步拟提交 final_answer。该轮 TODO 更新交由最终复核覆盖，不再根据复选框全部勾完来猜测；即使清单保留未勾的 final_answer，也适用。如果下一轮继续执行或显式请求复核，则恢复进度复核。done 仍必须通过独立复核。
6. **Workflow 恢复。** 外层单动作的状态恢复义务不再阻止 Workflow 内部的 waitEvent/Page.getState 恢复步骤。共用的 schema、Fleet/页面绑定、权限和 HITL admission 路径保留。不会因为允许该段执行而直接清除本地恢复义务。
7. **时间来源提示。** 要求执行者引用工具回执或媒体元数据中的实际时间，避免估计采集时间。这是提示改进，不是判断所有时间字段的机械门禁。

## 真实平台核验

使用 webcross-browser 技能和独立本地 HTTP 测试页面，不操作用户现有网页。原始请求/回执记录在 `docs/audit-evidence/browser-separation-2026-10-01/live-results.json`。

- 测试页面导航回执为 `loading`。
- 对照：加载期间立即执行只含 `DOM.getAXTree` 的 Workflow，平台在 `steps[0]` 返回 `observation-read-failed`。这是原始返回码，不能表述成已经验证了 `page-not-ready`。
- 恢复：Harness 本地状态有 `requiresStateResync` 时，执行 `waitEvent → Page.getState → DOM.getAXTree`，三个步骤成功；状态读取为 ready，AX 观察为 complete。测试仅验证恢复路径与这个对照，不涵盖所有 Native 生命周期错误码。
- 每次测试创建的页面和 Fleet 已关闭。
- 使用 `/Applications/WebCross.app`，安装包 0.9.38-beta，source revision `95aab2c12d012624dc8d58b2942acd3cd09d6d2a`；工作区 `abcp-platform` HEAD 为 `6924023865e5a1a131ce110bb08974198dbc678d`。本轮没有修改或替换 WebCross，平台结论依据运行实例回执。

## 验证与性能范围

修改前的相关基线为 140 项通过。新增回归覆盖导航无需等待 50 秒、工具前缀稳定、目录证据跨复核复用（file/db/dual）、数据库虚拟文件信息、Workflow 恢复不会取消普通单动作的状态义务。入口生命周期测试覆盖三种存储、正常终态、取消排空和旧任务续跑。

扩展检查包含仓库已有失败；使用本轮修改前的源码副本逐项对照。仅因代码迁移产生的三个测试源码读取路径已修正。最终相关检查：428 passed、1 skipped、55 subtests passed。扩展检查：3846 passed、91 failed、3 skipped；91 项失败均在修改前副本中复现，没有新增失败。另一个旧测试模块仍引用已删除的 TaskClassifierConfig，无法收集，扩展检查中排除了该模块。具体结果见 `docs/audit-evidence/browser-separation-2026-10-01/validation.json`。

本轮未让真实业务任务重新调用模型，不能声称端到端 token/耗时已有量化下降。回归测试验证消除了已识别的导航无效等待与复核重复触发条件；工具前缀的缓存收益和总耗时须由下一轮可比真实任务验证，分别统计 uncached_input、cache_read、cache_creation、模型轮次、平台 I/O、失败重试和扣除人工等待后的耗时。

运行中的 Python 进程不会自动导入这次修改；真实任务测试前应重启 Harness。SQLite 中旧任务可继续使用 `/browser` 后的 `/resume <task_id> [补充需求]`。

## 295b 任务之后的修复（2026-10-02）

任务 `295b8fc3821b400c96ee916a1e6fbef1` 的日志确认：SQLite 中存在 AXTree 行资源，但 `find_in_axtree` 在内存副本释放后仍打开不存在的 worktree 文件。搜索现改用统一任务存储读取器；数据库是权威来源，不回退到相邻旧文件。

连续复核的新请求不再重复加入上下文已有的回执索引、近期回执和未变化的证据事实。已提供的回执 ID 直接从原有复核消息中取得，没有新增持久状态、TODO 哈希或业务门禁。旧位置的回执内容变化会生成不同的既有证据 ID，因此仍会提供给复核者。原始目标、当前 TODO、未解决问题和失效证据仍完整提供；冷启动恢复完整入口，复核上下文压缩后恢复索引与有效事实。最终复核仍由模型检查全部目标，可查询全部历史和授权来源。

复核返回的 AXTree 回执移除 WebCross 主机专用的 `observation` 和 `suggested_prompt` 指引；原始回执、来源身份及哈希保持原样。工具描述与错误回执明确区分 `review_query_observation` 的页面观察 ID 和 `review_read_trace` 的回执 ID。

终端输入结束 HITL 等待时补发含 `elapsedMs` 的等待结束事件，事件与输入同时返回时不会重复记录。`harness-eval` 的提取脚本使用统一资源 codec 解码全部外置事件，并用旧日志的 `hitl.feedback_received` 关闭尚未结束的等待区间。修正后原任务总耗时 2352 秒、人工等待 557.128 秒、净耗时 1794.872 秒；模型轮次及 token 统计不变。没有写入业务数据库。

本轮相关验证：355 项测试、50 个子测试通过；评测脚本另有 4 项统计回归通过。覆盖三种存储、数据库旧物理文件不覆盖权威资源、连续/冷启动/压缩恢复复核、来源变化与过期证据、最终复核复用、HITL 事件竞态、压缩事件读取及损坏资源报错。本轮未重跑真实业务任务，不能据此声称总 token 或端到端时间已下降。测试前重启 Harness，WebCross 无需因本轮改动更新。
