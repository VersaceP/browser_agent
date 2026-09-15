# 997ab5 机械校验审计与删减方案

日期：2026-09-15。状态：**第5节 A–G 已按批准方案实施；验证记录见第10节，真实任务性能待用户下一轮实测。**

## 1. 结论

本次拖慢运行的主要链路是 Harness 的空值证明验收，而不是首轮并发调度：

1. 三个详情 phase 在搜索完成后不到一秒全部启动。
2. rank10 有视频，首次验收通过；rank8、rank9 的 videoFiles 为空。
3. rank8、rank9 的每份 worker 最终合同校验均只报 `field_nonempty/videoFiles/absence_proof_incomplete`。
4. 机械层要求固定八项 absence-proof，但错误回执暴露的义务名称与实际读取的 JSON 键不同；模型持续尝试动作、证据和键名变体。
5. worker 多次明确请求 Lead 判断；Lead 再次派发，或尝试修改合同。第二次计划审计仍要求保留同一套证明，修改被拒，最终 partial。

不能以“补全八个键的 few-shot”作为根治。该校验并不读取原始工具证据来证明八项事实，只检查声明中的布尔值、非空字符串及正整数。它既能拒绝有实际观测的结果，也能接受没有实际观测的声明。

上轮修复解决了审计员对条件非空合同的误读，但没有审查这套底层 absence-proof 是否合理。本次证据表明，后者必须一起处理。

## 2. 数据与边界

来源：两个任务的 `run.jsonl`，997ab5 的 `task_plan_reviews/review.0001.json`、`review.0002.json`、最终 worker 产物，以及当前 Harness 执行代码。没有打印模型隐藏推理或配置密钥。

### 2.1 总量比较

| 指标 | 9876a8ff… | 997ab5e3… |
|---|---:|---:|
| 首条至末条日志墙钟 | 58分11秒 | 101分44秒 |
| 首次 spawn 距首条日志 | 22分14秒 | 5分09秒 |
| 实际启动 worker | 4 | 10 |
| Browser 模型调用 | 160 | 465 |
| Browser 输出 token | 451,866 | 1,438,506 |
| Browser 非缓存输入 token | 715,304 | 1,915,123 |
| Browser 缓存读取 token | 23,310,208 | 70,073,984 |
| Lead 模型调用 | 20 | 52 |
| Lead 输出 token | 88,552 | 117,130 |
| Lead 非缓存输入 token | 226,549 | 177,634 |
| Lead 缓存读取 token | 1,822,592 | 5,613,696 |

997ab5 另有四次压缩，输出 81,539 token，未并入 Browser 输出栏；计划审计两次输出 4,551 token，数字核对一次输出 1,858 token。

首次派发等待减少约76.8%，但 Browser 调用变成2.91倍，输出变成3.18倍。两个任务的商品、页码不同，Lead 模型也由 glm-5.3 变为 deepseek-flash，因此这些是运行结果对照，不是严格模型或代码 A/B。两次 Browser 均为 deepseek-flash。

墙钟口径包含人工及等待；本报告不将并行 worker 时长相加当成任务墙钟，也不把缓存读取 token 当作可等额节省的费用。

本次实际出现 `lead.planning_history_handoff`：206,418 → 28,093 JSON 字符，说明规划历史交接优化已在该运行执行。字符数不是 token。

### 2.2 派发核对

| worker | phase | 运行时间 | 最终情况 |
|---|---|---:|---|
| 001 | collect_page2 | 6分43秒 | validated_done |
| 002 | detail_rank8 | 12分50秒 | videoFiles absence-proof 未通过 |
| 003 | detail_rank9 | 11分11秒 | 同上 |
| 004 | detail_rank10 | 9分11秒 | validated_done |
| 005 | detail_rank9 | 19秒 | Page.create 初始加载失败；继承产物仍有上述验收失败 |
| 009 | detail_rank9 | 23分26秒 | videoFiles absence-proof 未通过 |
| 010 | detail_rank8 | 15分11秒 | 同上 |
| 011 | detail_rank9 | 27分20秒 | 同上，期间还发生页面实例故障 |
| 012 | detail_rank8 | 20分49秒 | 同上 |
| 013 | detail_rank9 | 20分30秒 | 同上 |

精确计数是 **1个搜索 + 9个详情实际启动**。详情另有3次启动前失败/拒绝：006、008 的 Fleet.list 遇到连接致命错误；另一次指定复用 browser-003 时 slot 已不存在。因此不能按最大 worker 序号推算成功启动数。

首批详情之后的六个 worker 累计运行6,456秒（107分36秒，有并行），产生995,413个 Browser 输出 token，占本次 Browser 输出约69.2%。这些是受返工波及的工作量，不是承诺全部可省：其中包含连接问题、重新观察及部分文件处理。

rank8 共保存7份提取记录，rank9 共13份。最终两份产物的检查器只剩四个缺失义务。

### 2.3 本地文件

读取最终产物并实际 stat：rank8 声明的20张图片、rank9的22张图片均为存在的非空本地文件；rank10声明的21张图片及1个视频同样存在且非空，位于 Desktop 下的任务分类目录。

这次与上次“根本没有执行下载”不同。但 stat 只能证明存在及非空，不证明图片内容完整、视频可播放或页面没有其他视频。不能据此机械宣布所有业务目标已满足。

## 3. 根因复现

### 3.1 义务名称与实际输入键不一致

位置：`harness/results/row_ledger.py:99`。

| 错误回执义务 | 实际读取 |
|---|---|
| current_epoch_region_materialized | regionMaterialized 必须为布尔 true |
| overlay_clear | overlayClear 必须为布尔 true |
| target_collection_exhausted | enumerationExhausted 必须为布尔 true |
| selector_calibrated | selectorCalibratedBy 非空字符串 |
| source_provenance | sourceTool + sourceSelectorOrAxId 非空字符串 |
| positive_evidence_text | evidenceText 非空字符串 |
| navigation_epoch_bound | navigationEpoch 正整数 |
| outcome_declared_absent | outcome 等于 confirmed_absent |

例如 browser-013 提交了 `currentEpochRegionMaterialized: true`、`current_epoch_region_materialized: true`、区域滚动回执，以及 `navigationEpochBound: true`。检查器只读取另一套固定键，因此仍拒绝。browser-012 同样如此。

这些嵌套声明通过 `record_extraction` 原样保留。没有证据表明 WebCross 吞掉了字段，也不是内容 tracker 将正确的键改写成缺失值。

### 3.2 八项齐全仍不构成证明

直接调用当前 `absence_proof`，只提供 true、任意非空字符串、`navigationEpoch=999999` 和 confirmed_absent，完全不提供工具回执，结果为 `state=complete`。

`navigationEpoch > 0` 不等于绑定当前页面纪元；固定“正样本选择器校准”也不是所有空值都需要的步骤。没有列表、选择器或导航动作的可信来源，不能被强制转换成同一套网页枚举仪式。

### 3.3 还有独立的空值冲突

位置：`harness/task_control/validators.py:779`。

最小复现：packagingInfo 为空，附完整旧式 absence-proof、来源工具和证据文本。`field_nonempty` 返回通过；`field_provenance` 仍因 packagingInfo 本身为空而失败。

来源校验暗中执行了另一份非空规则。单改 absence-proof 会留下这条失败链。当前任务包装信息有值，故它不是本次实际阻断原因。

## 4. 责任边界

* **Harness 主责**：固定八项证明、输入与反馈不一致、将声明当成已证明事实、不同校验器重复施加空值要求。
* **Lead/审计模型的放大作用**：Lead 对 worker 的 needs_lead_review 多次选择继续派发；审计回合要求恢复现有证明约束，并称修复可达。模型没有识别框架本身的问题。但增加重试或换模型都不能修复机械合同。
* **WebCross 独立故障**：005 的 Page.create 返回 -32005，006/008 遭遇 WebSocket reader failure；011也报告页面实例故障。这些不能归入 absence-proof，也不能仅凭这些回执断言 dispatcher 重启。此轮不修改 WebCross。
* **版本边界**：日志中的缺失义务与当前 `row_ledger.py` 执行结果一致；规划历史交接、显式连接恢复也有运行事件。因此有足够证据定位本次 Harness 路径。此轮未对运行包与整个工作区做逐文件一致性核验，不宣称 WebCross 安装包与源码相同。

`lead.phase_continuation.blocked` 中003是 continuation_contract_changed，002及多位后续 worker 是 continuation_requires_lead。没有证据支持通过取消身份/合同指纹检查或扩大自动续跑预算解决本次问题。

## 5. 已批准并实施的删减范围

### A. 删除固定八义务 absence-proof 及其强制动作菜单（首要）

涉及 `row_ledger.py`、`validators.py`、提取回执、续跑上下文、相关 guide 和审计规则。

* 删除“八项全部齐全才算空值成立”的机械判决，不增加字段别名表，不补站点/视频特判。
* worker 在已有结束回合表达：观察结果、缺失/不可获得的判断、证据引用、是否需要 Lead 复核；不会为每个空字段额外调用一个模型。
* 页面、工具、文件与步骤引用的真实性由 Harness 校验；这些引用是否足以支持业务结论由 worker/Lead 或现有语义审查判断。
* 空值仍要与已批准合同及原始目标一致。缺少字段、没有观察、明确被阻断、经观察判定不存在，不能混成同一种成功状态。
* 机械层不得因仅缺“校准/物化/穷尽”布尔标志把阶段判 validation_failed；也不得把一个空数组本身升级成 confirmed_absent。
* 保留枚举值、身份、合同版本及证据引用完整性检查；任何语义结论都不能扩大权限、篡改要求的数量或隐去真实文件缺失。

### B. 拆除空值上的重复非空要求（首要）

* `field_provenance` 只验证来源关联，不要求业务值再次非空。字段存在性由 required_fields负责，是否允许空值由合同和语义结果负责。
* `field_nonempty`、array_length、行账本、累计产物选择、最终 phase 校验使用同一份结果语义，避免一次记录通过而结束时被旧规则驳回。
* `file_integrity` 保留对每个已声明文件的存在、大小、哈希和归属检查；去掉把完整性等同于“无论如何至少一个文件”的隐含最低数量。数量按显式合同检查，允许明确的 min_files=0，不能静默钳制为1。
* 不用其他图片来冒充视频已交付。获准为空的文件集合与声称存在但实际丢失的文件必须区分。

### C. 删除按自然语言和字段名猜业务的硬拒绝

1. `plan_validation.py:413/627/770`：移除登录阶段关键词分类、HITL否定句判断、probe→登录阶段的文字匹配硬拒绝。真实 HITL 暂停、恢复代次及授权边界仍保留。
2. `artifact_evidence.py:262`：删除 `detect_blocker_data_rows` 的字段名词表和错误词汇拒绝；不再用它阻止 record_extraction 和产物验收。实际复现：合法要收集的 observedErrorCode="auth_required" 被拒收；换成豁免词表里的字段名却可通过。原文及结构化 blocker事实交给模型判断。
3. **删除业务产物的 allowed_domain 硬校验，包括 skill 候选行过滤路径**，域名只作为来源事实交给模型判断。模型把域名写进合同，并不使它成为客观权限边界。不得将相同业务限制迁移成 url_pattern 正则继续阻断。保留用户/管理员明确配置的访问权限边界，两者不是同一用途。
4. 范围、正则、集合比较也不能仅因“出现在模型合同里”就自动成为合理硬约束。明确的数量、身份集合或用户要求的逐字格式可以机械比较；模型推测的平台归属、字段意义、页面规范化后是否等价，应提供事实交给语义判断。

已经仅作提示的 placeholder 文案检测、singleton fragmentation 提示及 required_collection_facts，不误报成仍存在的硬门禁。清理时避免把这些词表重新接回拒绝路径。

### D. 删除“证明缺失必走指定工具”的机制

`record_extraction.py:226` 和 `spawner_worker.py:858`：启用 VL 时，修复结果声明 confirmed_absent 就必须调用指定 visual_verify，否则最终失败。移除这一默认强制路径。视觉验证作为模型可选择的证据工具；用户或已批准合同明确要求视觉检查时仍按显式约束执行。

当前 `semantic_terminal_claimed` 自然语言词表未找到生产调用点，不把死代码描述为本次执行中的门禁；可一起清理其无用辅助代码和失效说明。

### E. 收窄观察和执行前置条件

* `dispatch.py:73`：保留 navigation_context 的对象结构、允许的方法和源页面所有权检查；删除“tracker必须先认定有内容恢复问题”才能接受来源声明、否则连 Page.getState 都不执行的条件。声明只记为模型声明，不能因此获得新页面权限或被当成真实 opener 证据。本次该条件拒绝一次。
* `dispatch.py:257`：保留真实生命周期、过期句柄及身份保护；删除不使用旧 AX 句柄的操作仍一律要求先读 AXTree 的前置要求。根据实际传入目标及其代次检查，不继续扩展方法豁免词表。本次有12次生命周期门禁事件，尚不能声称12次全部多余。
* `agent_harness.py:1973`：删除 Runtime.evaluate 只因和其他调用处于同一模型回合就被拒的规则，本次触发7次。保留原有脚本权限/用途检查、参数校验和有序执行；遇到实际页面变更仍检查后续目标有效性。不将 Runtime.evaluate 用来绕开 WebCross 缺陷。

### F. 校正标签与能力/预算的关系

* `plan_validation.py:697`：删除 file_integrity 必须伴随 file_download 或 image_exported 的任务标签限制。文件完整性适用于复制、文本输出、归档等来源；是否允许具体动作由实际工具权限判断。
* `plan_validation.py:1260`：probe≤1行、validation≤2行是框架内置策略，不是普适算术事实。建议取消默认按角色施加的固定样本数；保留已批准的显式资源预算、所选行存在性、checkpoint身份和依赖关系。此项不是本次热路径。
* 不增加“同一错误三次就终止业务”的新门禁。历史尝试次数、变化的证据和未变的失败原因作为决策事实；已有显式预算继续机械执行。

### G. 同步审计和迁移

* 删除 prompt/guide 中“完整八义务证明”的要求及暗示其一定可达的说明。原始用户目标优先于模型自己设计的证明仪式。
* 既有 `<field>Absence` 内容作为历史证据读取，不将旧布尔值当作已核验证据；新回执不要求补旧键，也不删除旧日志。
* 对历史 validation_failed 的 phase 提供重新校验已有产物的路径，复用原 phase身份、计划版本和尝试历史；有语义不确定性时由 Lead 判断。不会自动重跑下载，也不会把不确定结果静默标为完成。

以上范围已实施。保留旧合同的读取入口，删除相应硬拒绝；具体迁移行为和证据边界见第10节。

## 6. 全部声明式校验器盘点（当前21类）

| 校验器 | 去留/边界 |
|---|---|
| artifact_required | 保留：声明的产物是否存在 |
| required_fields | 保留：字段键是否存在，不替代非空或语义判断 |
| field_nonempty | 保留显式合同比较，删除固定八义务例外机制 |
| field_pattern / url_pattern | 仅保留明确格式/协议约束的用途；模型推测的业务URL形状、域名归属降为观察信息，不能替代已删除的allowed_domain |
| field_provenance | 修改：只负责可追溯来源，删除隐藏非空要求 |
| allowed_domain | 删除业务验收及候选行过滤中的硬拒绝；提供实际host、来源和已有重定向记录作为语义判断事实 |
| cross_field_contains | 用户明确要求字面包含关系时可执行；不能以字符串包含代替字段语义、归属或页面回显等价判断 |
| action_outcome | 保留明确动作结果与成功集合比较 |
| range | 保留数值区间；与数组/对象类型冲突继续拒绝 |
| array_length | 保留显式数量，统一条件空值处理 |
| min_rows / max_rows / exact_rows | 保留数量比较及相互矛盾检查 |
| unique | 保留显式去重约束 |
| set_equals | 保留集合一致性，不自作主张施加到规范化回显值 |
| download_completed | 保留对应下载记录的完成事实；不把任意历史下载当本次成功 |
| file_integrity | 修改隐含数量与按名字猜路径的范围；保留已声明文件的实际检查，优先明确 path_fields 和交付清单 |
| upload_selected / upload_confirmed | 保留明确上传操作与确认回执，不混同选择文件和提交成功 |
| image_exported | 保留对应导出回执及归属 |

## 7. 其他机械层盘点

| 层 | 保留 | 删除/调整 |
|---|---|---|
| 计划编译 | schema、枚举、引用、依赖环、重复ID、冲突数量/类型、版本 | 文字推测登录阶段、任务标签限制通用文件校验、隐含角色样本数 |
| Fleet/page | 显式Fleet绑定、页面归属、租约、会话代次、真实HITL状态 | tracker语义判断成为来源声明的前置许可 |
| 工具入口 | 运行时capability/schema、明确禁止动作、目标身份与过期状态 | 与目标无关的强制AX刷新、evaluate整回合独占 |
| Workflow | 协议结构、变量引用、权限、状态与副作用不确定性 | 不改WebCross执行器，不自动重放不确定副作用 |
| 数据落盘 | 合法行结构、明确身份、来源引用、文件归属 | blocker/字段词表拒收、指定证明仪式 |
| 行账本/产物选择 | 原始结果、因果和执行状态的客观记录 | 将布尔声明当作证实不存在；不得新旧两套判定并存 |
| 交接/续跑 | 并发上限、已批准依赖、去重、合同版本、显式预算 | 本次不放宽这些闸门；修正回执事实后复用原调度 |
| 最终回答 | 对实际产物/数量的核对，禁止将失败回执静默写成成功 | 原始目标与有效证据优先，不能要求无意义的动作次数 |
| 内容完整性/placeholder提示 | 当前已有的观察事实可继续提供给模型 | 不再由提示间接导出机械拒绝，去除未使用的旧硬门代码 |

这是对当前主要验收与执行入口的源码盘点，不是形式化证明整个仓库不存在其他缺陷。以上未实际触发的项目均与本次原因分开标注；WebCross内部校验不在可修改范围内。

## 8. 实施顺序与验收

1. 先A/B/G：统一空值语义、证据来源、产物验收与历史重验；先闭合本次失败链。
2. 再C/D：删除词表拒绝与强制视觉证明，更新所有调用方和guide。
3. 再E/F：删除与实际权限/目标状态无关的前置条件；保留客观边界。
4. 跑有意义的反例回归及历史离线回放，再由用户跑真实任务对比。

必要反例：有真实观察但没有旧八键；八键齐全但引用不存在；空值和未采集不能混淆；packagingInfo的来源与空值不再冲突；真实auth_required业务值能保存；声明文件缺失/错误哈希仍失败；未授权页面和过期句柄仍拒绝；显式行数及预算不被放宽；普通观察不因tracker未知受阻；重复回执不再次派发；历史重验不重放副作用。

审计阶段先读取原始日志、产物 stat，并完成三个最小复现（任意声明可通过 absence-proof、合法错误码被拒收、provenance 重复非空）。实施后的回归及只读回放见第10节。本轮没有进行新的真实浏览器任务，也没有把“空数组存在”当成已证实无视频。

## 9. 用户标准复核：有限代码不等于客观约束

后续复核修正了本报告原先“保留显式allowed_domain合同”的建议。判断标准应同时满足：输入是明确的结构化事实；约束来源明确；结论无需解释业务含义；失败原因和恢复路径明确。不能只看比较代码是否短。

当前allowed_domain不是网络访问权限门，而是产物验收/候选行筛选：

* `validators.py:302` 使用netloc、只剥离www.前缀，再与domains精确比较。因此即便允许taobao.com，detail.taobao.com仍会失败，更不用说业务流程允许进入另一个域名的情况。
* `skill/dispatch.py:514` 又读取domain/value并接受子域名，却不读取同一套domains字段。这是另一份语义不同的实现，可能出现候选行过滤和最终验收不一致。
* 继续补主域、子域、关联平台、跳转站和CDN规则，无法解决“这个页面是否属于用户要的商品”这一业务问题。

因此删除两条硬拒绝路径及配套引导。旧计划中的allowed_domain保留原始声明和迁移提示，转为模型可见信息，不静默丢失历史意图，也不因旧规则成为unknown_validator而再次卡住恢复。

同一标准适用于其他校验：文件是否存在、哈希是否一致、页面是否归当前worker、并发是否超过明确上限，是客观事实；文件是否就是用户想要的内容、是否还应继续寻找、某个空值是否合理、不同URL是否属于同一业务目标，是语义判断。前者保留机械处理，后者提供有来源的事实，交给模型。

这里不提出新的站点名单、正则替代门禁或逐字段模型调用。当前仍只修订方案，未改运行代码。


## 10. 实施记录与验证边界

### 10.1 代码行为

| 范围 | 实施结果 |
|---|---|
| A/B 空值及文件 | `confirmed_absent` 与非空 `evidenceText` 表达 worker 的语义判断；不再检查固定八键。回执明确 `mechanicallyProven=false`。缺字段、阻断状态、缺少声明仍不能冒充条件空值。provenance 不再重复要求业务值非空。`file_integrity` 默认最低数量为0，检查每个已声明文件；显式 `min_files` 仍执行。指定 `path_fields` 时不借用无关文件补数，也不按字段后缀猜路径。 |
| C 文案与域名 | 删除 blocker 词表和登录/HITL 否定句硬拒绝。`allowed_domain` 在验收和候选筛选均不再拒绝，旧合同作为 advisory 读取。pattern/contains 默认提供语义观察；只有显式 `enforcement=literal` 才执行字面比较，guide/审计明确其用途限于用户逐字要求或协议格式，不可迁移域名归属限制。 |
| D 视觉工具 | 删除“修复空值必须 visual_verify”的自动要求及最终失败覆盖。视觉工具仍可由模型按证据需要选择。 |
| E 工具入口 | 普通 navigation_context 不再依赖 tracker 先认定恢复问题；页面所有权仍检查。AX 刷新前置要求只针对实际传入的 AX 句柄；只读根节点或 selector 不因此被拒。删除 Runtime.evaluate 同回合独占规则，保留权限和调用后的状态检查。 |
| F 计划结构 | 删除通用文件完整性校验的任务标签限制，以及 probe/validation 固定1/2行上限。显式行选择、checkpoint身份、预算及依赖仍检查；prompt/schema 同步更新。 |
| G 历史重验 | 新增 Lead 工具 `revalidate_phase_artifacts`，先只读验收已存产物，再由 Lead 带理由接受。接受记录独立保存，保留旧失败回执、worker身份和续跑预算；校验通过并不自动产生业务成功。没有重放下载或派发 worker。 |

实施中还发现 `record_extraction` 将内容提示并入 `schemaWarnings`，导致普通文案提示间接变成 `needs_fix`。现将其作为 `contentObservations` 返回，结构不合法仍按 schema 报错。

### 10.2 证据和迁移

新空值声明可提供 `evidenceRefs` 文件路径列表；Harness 检查列表结构和引用文件存在性。路径存在只证明该引用可读取，不证明内容支持“不存在”的结论。页面/工具/步骤的历史描述仍是待核对证据，不因旧 `navigationEpoch` 或布尔标志而升级成机械证明。语义观察进入已有 Lead 回执和语义审查，不为每个空字段新增模型调用。

旧 `<field>Absence` 继续读取；旧分离的 `<field>Outcome` / `<field>EvidenceText` 仍可表达同一判断。八键本身不再决定通过与否。`allowed_domain` 旧声明保留为可见信息，不造成 unknown-validator 阻断。

历史重验只收集当前 phase 的已知产物，以及该 phase/worker 对应的原始文件工具响应；不把任意历史下载归入本 phase。DB 模式按 DB 日志读取，dual/file 保持文件 primary。接受前再次验收，重复调用不能绕过文件删除或内容变化；原始失败历史不被改写。

### 10.3 验证

使用 conda `agent` 环境（Python 3.13.5）验证：

- 全量测试：**4,130 passed、3 skipped**；另有1,019个 subtest 通过。
- 最后收尾变更后的相关回归：**143 passed**，包含完整的新增专项23项。
- 新增专项覆盖空值声明、引用缺失、域名变化、字面规则、缺失文件/错误哈希、历史重验、二进制文件摘要、DB日志优先级及回执归属。最后两项在全量测试启动后补充，已包含在上述143项回归中。
- 修改的运行模块编译通过，`git diff --check` 通过。

测试输出含既有弃用/收集警告，未出现失败；3个跳过项未计为通过。

对 `997ab5e3b62d4e0fa42cfa37399890f7` 的原产物做只读回放：

| phase | 新机械验收 | 对外状态 | 重放业务动作 |
|---|---|---|---:|
| detail_rank8 | done，0 failures | review_required | 0 |
| detail_rank9 | done，0 failures | review_required | 0 |

原任务状态、日志和交付文件均未修改。这证明原来的固定 absence-proof / 重复非空阻断已消失；没有据此声称两件商品确实无视频。是否满足原始目标仍由 Lead 根据证据判断。

### 10.4 下一轮实测

重启 Harness 进程加载修改后，用户运行新的真实任务。比较实际详情 worker 数量、续跑理由、Browser/Lead 模型调用数、非缓存输入与输出 token、剔除人工等待后的关键路径耗时，以及用户目标目录中的实际文件。旧日志回放不能代替这一性能验收。

本轮没有修改 WebCross，也未放宽 Fleet/page 身份、工具权限、真实 HITL、明确数量、合同版本或并发预算。其他机械层是否继续调整，留到这轮实测后再评估。
