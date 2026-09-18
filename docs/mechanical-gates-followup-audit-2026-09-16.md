# 其余机械层复核及 M1–M7 修复记录

日期：2026-09-16。状态：**M1–M7 已按授权实施，验证结果与边界见 §13。** §1–§12 保留修复前审计结论；本次未修改原任务、WebCross或配置。

## 1. 判断

还有问题。最需要避免的是把“有一段有限代码能比较”误认为“比较结果足以决定业务成败”。当前仍有按固定动作顺序判断证据有效性、把历史判决当作现状、把历史数量下降当作数据损坏的入口。

本轮确认7项问题，其中数字抽取协议问题在b224已观察到，其余为本次补充核查；Workflow固定AX刷新与旧schemaWarnings是上轮删减没有彻底覆盖的入口。另列两项策略/说明问题，不与确定的正确性缺陷混为一谈。

优先级按影响范围和恢复代价评估，不代表都在b224触发：

| 编号 | 优先级 | 问题 | 证据 |
|---|---|---|---|
| M1 | P1 | Workflow仍强制固定AX刷新，和普通工具入口冲突 | 本地复现 |
| M2 | P1 | 历史schemaWarnings仍永久阻断当前产物重验 | 同一行数据，仅删除旧警告即由failed变done |
| M3 | P1 | 历史数组更长直接判data_conflict，合法去重也会被拒 | 当前3项、历史4项含重复，准确报告3仍被判冲突 |
| M4 | P1 | 数字抽取metric/unit及计数对象映射不一致 | b224实测 + 本地复现 |
| M5 | P2 | DB逻辑资源在统计/恢复层被本地文件读取绕过 | 真正SQLite后端复现，非替身 |
| M6 | P2 | 下载completed缓存不检查文件现状，也无明确失效重验路径 | 删除文件后仍返回completed/reused，不再发请求 |
| M7 | P2 | Workflow事件允许表夹带“是否值得等待”的策略 | 缓存协议允许Fleet.ready，Harness静态拒绝 |

## 2. M1：Workflow没有共享已收窄的生命周期边界

位置：[workflow_policy.py:313](</Users/versace/Desktop/abcp browser system/harness/workflow/workflow_policy.py:313>)、[capability.py:297](</Users/versace/Desktop/abcp browser system/harness/tools/browser_tools/capability.py:297>)、[skill/workflow.py:124](</Users/versace/Desktop/abcp browser system/harness/skill/workflow.py:124>)。两个生产调用方均传enforce_lifecycle=True，不是闲置代码。

复现序列：Page.navigate → readEvents(Page.loaded) → Page.getState → DOM.getText(selector=body)。没有使用任何旧AX句柄，仍得到：

```
steps[3] must call DOM.getAXTree after Page.getState
steps ends with unresolved lifecycle obligation 'axtree'
```

关闭此固定顺序检查后，同一输入通过其余检查。这不是声称Page.getState在任意情况下都保证页面成功加载；问题是当前门只看动作名称次序，不读运行时状态，还要求与目标无关的AX操作。

建议：共享基于页面代次、真实状态、实际目标引用的边界；允许Workflow以“取得状态/事件供模型判断”为终点。删除无条件AX顺序要求和蒸馏器里的对应默认插入。保留旧句柄不能跨导航复用、页面权限及真实loading/crashed/HITL状态校验。不要简单将enforce_lifecycle全部关闭，也不要新增方法豁免词表。

验收：selector/文档根读取无需AX；确实引用旧AX句柄仍拒绝；直接调用、Workflow、skill路径一致。

## 3. M2：已撤销规则通过历史警告继续生效

位置：[artifact_validation.py:304](</Users/versace/Desktop/abcp browser system/harness/task_control/artifact_validation.py:304>)、[validators.py:1844](</Users/versace/Desktop/abcp browser system/harness/task_control/validators.py:1844>)。单产物与累计产物都把非空schemaWarnings直接当作schema失败。

上轮已把新record_extraction内容提示移到contentObservations，但旧JSON中的警告不变。复现：业务要记录observed="placeholder"这个实际文本；相同行数据、相同当前合同，有旧placeholder_like_extraction_value警告就failed，去掉这段元数据则done。数据本身未变。

这也影响新加的历史重验工具，因为它复用相同验收入口。历史警告只能证明“当时某规则曾给出这个警告”，不能证明当前结构仍有错误。

建议：基于当前合同及实际rows重新计算结构错误，旧警告保留为带来源/版本的观察信息。不能仅删除所有schemaWarnings判断而遗漏原本在记录入口才做的结构检查；必须补齐当前结构重验，并验证缺键/错类型仍失败。不按警告文案不断追加豁免表。

## 4. M3：把数量减少误当成必然丢失数据

位置：[numeric_facts.py:102](</Users/versace/Desktop/abcp browser system/harness/results/numeric_facts.py:102>)、[numeric_facts.py:377](</Users/versace/Desktop/abcp browser system/harness/results/numeric_facts.py:377>)、[lead_tools.py:6072](</Users/versace/Desktop/abcp browser system/harness/tools/lead_tools.py:6072>)。

当前索引把extractions目录里所有未激活JSON都归为historicalArtifacts，未要求它们曾通过验收或属于同一替代链。只要历史同subject同字段数组更长，就判data_conflict，并指导Lead恢复那些“丢失”的内容。

复现：历史values=[a,a,b,c]；当前去重后的values=[a,b,c]。当前答案准确报告3，resolver仍返回actualValue=3、historicalValue=4、verdict=data_conflict；该状态会阻止final_answer。

建议：历史长度变化提供结构化差异和来源，由语义审查结合原始目标判断是去重、纠错、筛选还是丢失。只有明确的数量/身份保留合同被违反时，执行对应机械约束。不能默认对任何数组施加“只能增加”的隐含合同；不能因失败草稿较长就强迫恢复无效数据。

验收：去重和用户授权筛选可以完成；当前答案与当前数组长度真正不一致仍被指出；明确要求保留的ID集合缺失仍被检测。

## 5. M4：数字抽取协议和计数对象未对齐

位置：[numeric_facts.py:268](</Users/versace/Desktop/abcp browser system/harness/results/numeric_facts.py:268>)、[numeric_facts.py:449](</Users/versace/Desktop/abcp browser system/harness/results/numeric_facts.py:449>)。详见[b224评估](</Users/versace/Desktop/abcp browser system/docs/live-evaluation-b224-2026-09-16.md>)。

- schema允许unit=items并指导模型对products/entities使用它，resolver却要求count对应field_entries。合法schema输出无法被解析器验证。
- metric说明将per-artifact number宽泛引导成row_count。b224中“该文件列明22个文件路径”被映射成“该JSON有22行”，与实际1行比较后误报。
- 同一次b224核对还发现真正的“属性写20、实际22”错误，不能一并取消算术核对。

建议：对齐schema、prompt、resolver的类型关系；表达明确的artifact/row/field引用和聚合方式，保留原文定位。语义映射不确定时回传事实给审查，不把错误的计数对象包装成机械证明。不要靠商品、属性、文件等词表不断识别自然语言。最终交付清单、临时素材、下载操作也需分开投影，避免模型拿原始fileArtifacts数量充当交付数量。

## 6. M5：DB选源仍未贯穿恢复和完成回执

位置：[numeric_facts.py:56](</Users/versace/Desktop/abcp browser system/harness/results/numeric_facts.py:56>)、[completion_receipt.py:269](</Users/versace/Desktop/abcp browser system/harness/results/completion_receipt.py:269>)、[phase_lifecycle.py:666](</Users/versace/Desktop/abcp browser system/harness/task_control/phase_lifecycle.py:666>)。

使用真实create_storage(backend="db")保存2行产物：

| 条件 | 正常read_task_file_text | 数字索引 | 恢复完整性 |
|---|---|---|---|
| 无本地文件 | 读到DB的2行 | validated_artifacts=0、validated_rows=0；完成回执行数也为0 | 能通过 |
| 同路径留下旧文件rows=[] | 仍读到DB的2行 | 读成旧文件的0行 | 与DB正确摘要比较，误报sha256_mismatch |

这不是DB本身失败，而是内部消费者绕过选源API。额外源码缺口：legacy CSV/JSONL语法检查即使先取到DB文本，仍在对应分支open物理路径；该分支本轮未另做集成复现。

建议：统计、哈希、语法检查、存在性和语义证据统一使用资源读取抽象。db模式由DB逻辑资源优先；dual/file的File primary不变；外部下载文件继续检查物理字节。不要通过忽略hash错误使恢复“通过”。

## 7. M6：下载历史完成不等于当前文件仍可交付

位置：[downloads.py:527](</Users/versace/Desktop/abcp browser system/harness/tools/browser_tools/downloads.py:527>)、[capability.py:569](</Users/versace/Desktop/abcp browser system/harness/tools/browser_tools/capability.py:569>)。

当前worker缓存里存在URL+savePath的completed后，再请求相同Download.start直接返回success/completed/reused，不查询下载状态或检查文件。复现中目标文件已删除，仍返回completed；执行入口因复用结果非空而不调用平台。

结果可能是：文件完整性层正确报缺失，但模型重试下载又被缓存短路。下载历史没有错，错在把历史完成当成当前交付仍有效。

建议：把下载操作历史、当前物理文件验证和是否允许重新下载拆开；给出artifact_missing/changed事实及明确的失效重验路径。对已证实缺失的交付，允许模型在原权限范围内选择重新下载；仍保留未知副作用、进行中的下载、页面保留窗口等幂等保护。不能对任何异常自动重放，也不能按URL业务词汇判断是否能重试。

## 8. M7：事件静态允许表在替模型决定等待是否有价值

位置：[workflow_policy.py:39](</Users/versace/Desktop/abcp browser system/harness/workflow/workflow_policy.py:39>)、[workflow_policy.py:375](</Users/versace/Desktop/abcp browser system/harness/workflow/workflow_policy.py:375>)。

缓存的Workflow.execute schema在waitEvent/readEvents事件枚举里包含Fleet.ready、Fleet.stopped、DOM.axTreeUpdated，Harness却排除它们。注释理由分别包括“等待Fleet只能烧超时”“某次live没观察到AX事件”。本地调用waitEvent(Fleet.ready)在发出平台请求前被拒。

协议宣称支持不等于部署一定发事件；某次没观察到也不等于它在所有场景均不合法。应分离两个结论。

建议：机械层按当前协议的事件名、权限范围、目标身份与明确等待上限验证；部署是否实际发出、最近是否多次超时作为观察信息交给模型。对协议缺失或版本不一致明确报来源差异，不把一次探测结论永久写成静态禁令。本轮没有重新发live等待，也未断言当前部署确实会发上述事件。

## 9. 两项应单独讨论的策略/说明

### 9.1 固定连续20次同参调用的隐藏预算

[loop_guard.py:35](</Users/versace/Desktop/abcp browser system/harness/tools/loop_guard.py:35>)在第21次相同工具输入时终止worker，不查看结果是否有进展；max_steps=100的本地复现也会在21次停止。连续翻页/滚动可能需要相同输入但每次取得不同内容。

这里比较的是客观调用次数，不能简单认定所有资源预算都应删除；问题是固定20不来自当前显式任务预算，且其价值依赖业务行为。建议将它作为可见、可配置的资源政策单独讨论，或用已有显式总预算约束。重复次数和结果变化仍可供LLM判断，不新增站点特例。本轮未证明b224触发此门，不列为其耗时根因。

### 9.2 Prompt仍描述已经不存在的“自动驳回”

[agent_harness.py:3078](</Users/versace/Desktop/abcp browser system/agent_harness.py:3078>)仍说target_absent证据未经核实会被降级成可重试失败；当前spawner_classification.py则保留worker声明，只附counterevidence，不自动改分类。这是指导与执行不一致，应同步文字，不能为迎合旧prompt重新恢复机械语义判决。

## 10. 明确保留的边界及责任

本轮检查了计划schema/冲突、Workflow、Browser工具入口、下载复用、进度/循环观察、phase预算与依赖、页面隔离、产物验收、恢复、数字及完成回执。不是对整个仓库的形式化无缺陷证明。

不建议删除：

- Fleet/page身份与租约，防止worker借错误恢复接管其他任务页面。
- 已知失效AX句柄、真实页面崩溃/HITL状态及权限边界。
- 计划引用、依赖环、重复ID、类型冲突、明确数量和集合约束。
- 显式并发/步数/尝试预算，重复回执去重和计划版本校验。
- 已声明文件的真实存在性/字节完整性，与不确定副作用的重放保护。

旧2026-08-05门禁报告写“quarantine无TTL”，当前代码已有300秒TTL、轮转复查和退役路径，不能复用旧结论。下载操作引发页面状态失效的旧问题也已移除；进度次数目前主要是观察信息，不应误报为硬门。

b224的Page.create → dispatcher重启仍归WebCross调查，已有安装包/服务端日志证据。Harness将no_delegated_page_candidate转成terminal、结束004/007/012是单独的执行事实；不能把它说成模型不愿恢复，也不能通过扩大页面权限掩盖平台缺少创建副作用身份的问题。

## 11. 复现与测试

- [复现脚本](</Users/versace/Desktop/abcp browser system/docs/audit-evidence/mechanical-gates-2026-09-16/repro.py>)：在项目根目录用conda agent Python运行；只写临时目录，不发浏览器/模型请求。
- [复现结果](</Users/versace/Desktop/abcp browser system/docs/audit-evidence/mechanical-gates-2026-09-16/results.txt>)：11个输出案例，包括两个对照结果。
- 现有Workflow、数字、resume、下载复用、下载事件5组测试：**124 passed**。这说明原测试没有覆盖上述反例，不能据此判定门禁设计正确。

## 12. 实施顺序（已获授权执行）

1. 先修M1/M2：补齐上轮入口遗漏，统一直接调用/Workflow和新产物/历史重验行为；同步旧prompt说明。
2. M3/M4：准确执行可绑定的算术约束，历史差异与语义不确定性转为审查事实；明确交付计数来源。
3. M5/M6：修正资源选源与下载完成缓存的有效性，保留幂等和真实完整性检查。
4. M7：由协议能力决定可等待事件，部署观测只提供建议；固定重复调用预算单独确认，不顺带修改。

每项实施均加入反例与正例回归，避免用“全部放行”消除误杀。不新增站点、字段、URL或选择器特判，不额外给每个工具调用增加LLM审查。


## 13. M1–M7 实施结果（2026-09-16）

本节是本次修复后的状态；前面各节与原 `results.txt` 保留修复前的证据。变更只涉及 Harness，未修改或重启 WebCross，也未运行真实模型/浏览器任务。

| 项目 | 已落地行为 | 仍保留的约束 |
|---|---|---|
| M1 | Workflow 在导航落定、Page.getState 后，可直接读取文本/属性/selector 或结束；autoheal 只补缺失的 state，不再自动追加 AXTree；Browser system prompt 与两份 guide 同步 | 页面落定、状态同步、方法权限、实际目标身份检查继续有效。取消必读 AX 不会使旧 ID 有效；Workflow 内执行时的句柄解析仍由 WebCross 负责 |
| M2 | 单份产物及累计产物都根据当前 rows 和当前合同重新检查结构；不再把历史 schemaWarnings 当作判决。旧警告以 advisory warnings 保留 | rows 必须是数组、每行必须是对象，当前必需字段及明确的 JSON 类型仍须正确；显式数量、集合与文件约束照常检查 |
| M3 | 历史数量差异写入 historicalDifferences，当前准确数字可以通过；只有历史数据能对应的声明保持 unresolved | 当前声明与确定绑定的当前值不一致仍为 contradicted；历史下降不自动推出丢失，也不再指导强制恢复旧行 |
| M4 | 对齐 count 的 field_entries/items 单位；增加 artifactPath/rowIndex/field 精确绑定和多数组 bindings 求和；说明 row_count 只数顶层 JSON 行 | 模糊绑定、混用单字段与聚合绑定、重复聚合引用保持 unresolved。数组长度求和不能充当去重实体数、文件存在证明或成功下载次数 |
| M5 | 数字索引、当前/累计完成回执、产物代际合并、恢复哈希及 JSON/JSONL/CSV 语法检查统一经资源选源入口读取；完成回执可显式传 logger | db 下存在的逻辑资源优先；dual/file 维持文件 primary；外部下载检查物理字节。缺失文件和摘要不符仍不通过 |
| M6 | 复用 completed 回执前核验物理文件及已知大小/完成时 stat 指纹。确定缺失时撤销 URL/path 复用别名，保留 downloadId 历史；调用者的新 Download.start 可继续走原入口 | 文件变化或无法核验返回具体事实，不冒充成功；进行中、保留窗口和超时副作用不明的保护仍在。不自动重放断线 Workflow |
| M7 | waitEvent/readEvents 分别取当前绑定的 Workflow.execute schema 的 focus 枚举；模型工具 schema 和执行校验使用同一来源 | 未声明事件仍被拒；合同缺少事件枚举明确报 schema 不可用。协议支持不等于本次操作必定发出该事件，是否值得等待由模型结合回执判断 |

补充修正了 `target_absent` 的过时 prompt：当前实现保留 worker 分类并附反证，不能再声称会机械降级。固定连续重复调用预算不在本轮修改范围内。

### 13.1 结构校验为什么仍属于机械层

重验只使用已声明的结构和类型，不推测字段内容是否充分、是否应为空或业务是否应继续。它适用于所有 JSON 行合同；未知业务类型名称不被猜测成某个 JSON 类型，空数组本身合法。恢复路径是修正当前结构，或由有权限的 Lead 显式修订不合理合同；历史警告本身不能继续阻止有效数据。没有新增站点、字段、URL 或选择器特判。

### 13.2 回归验证与局限

新增 `tests/test_mechanical_gate_followup.py`，使用真实 SQLite/File/Dual 后端和临时物理文件覆盖：

- DB 资源无物理文件、同名旧文件、当前与累计完成回执、带哈希的合并代际。
- 三种后端中的有效/非法 CSV、JSONL，以及外部二进制文件存在/删除。
- 历史警告不阻止有效空数组；当前缺字段、错类型、非对象行仍失败。
- 一行中的 22 个数组条目不再按 22 行比较；准确数组计数通过、错误数值失败、重复求和引用无法形成证明。
- completed 文件删除、替换、迟到旧回执以及 active 刷新为 completed 时的缺文件检查。
- 切换 schema 目录后新增事件同时进入模型 schema 和执行校验，切回后不泄漏该能力。

新增回归 **33 passed**。最终全量验证 **4165 passed、3 skipped**，另有 **1019 个 subtests passed**；运行耗时 158.71 秒。`compileall` 与 `git diff --check` 均通过。

下载复用的快速核验使用存在性、大小和已观察到的 stat 指纹，没有每次重新计算完整视频哈希。它可以发现已删除、大小不符、常见替换，并不保证识别刻意保持大小与时间戳的内容变动；最终交付合同中的完整性校验继续独立执行。首次读到旧 completed 回执而没有历史指纹时，不能声称已经证明文件自下载以来未变。

没有宣称本次已修复 WebCross dispatcher 重启，也没有依据离线测试给出 worker 数量、墙钟或 token 的节省比例。下一轮应使用重新启动的 Harness 进程跑同类任务，比较首次派发、各 phase 实际尝试次数、校验驳回原因和净执行耗时。


修复后复现见 `docs/audit-evidence/mechanical-gates-2026-09-16/repro_after.py` 和 `results_after.txt`。该脚本为 DB 消费者传入实际 logger/backend 上下文，未改写原始修复前证据。原 11 个输出中：Workflow AX 误拒和事件误拒消失；去重后的准确计数、items 数组计数通过；已删除文件不再复用成功；DB 的两行在无文件及旧同名文件两种情况下均正确计数且通过哈希；旧内容警告不再阻止有效数据。固定重复调用预算的复现结果保持原样。

全量测试第一次跑完时唯一失败是旧测试断言 `next_instruction` 必须含字面量 “DATA problem”。已改为验证真实矛盾回执中的 actualValue=3、claimedValue=18；没有为通过测试恢复旧的“历史减少必然丢数据”提示词。Fleet 恢复测试包含 120 秒真实 barrier 等待，独立验证通过；本次未改动该生产逻辑或其超时。


最终验证命令：`/Users/versace/opt/miniconda3/envs/agent/bin/python -m pytest tests -q`。完整输出保存在 `docs/audit-evidence/mechanical-gates-2026-09-16/tests_after.txt`。测试目录沿用仓库现有的 Git 忽略设置，本次未修改 `.gitignore`；新增测试可直接在当前工作区运行。
