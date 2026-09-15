# b224 实测评估：机械验收改善，Page.create 故障及串行恢复仍拖慢任务

日期：2026-09-16。任务：`b224ebca3b224e8493d8ce6ecb210804`。本轮仅核查日志、源码、安装包和交付文件，不修改运行代码，不重新调用浏览器或重放业务动作。

## 1. 结论

本轮四个 phase 最终全部 `validated_done`。实际启动11个 worker，上轮997ab5是10个；编号到015包含启动失败和拒绝，不能理解为15个已运行 worker。

固定 absence-proof 导致已有产物反复被拒的链路在本轮没有出现：第31名 packaging 为空、第32名 videoFiles 为空，均作为 worker 语义判断进入回执，最终通过。第32名第一次记录把键写为 videoAbsence，在同一 worker 内改成 videoFilesAbsence 后完成，没有因此再派 worker。三个成功详情 worker 都在首次最终产物验收时通过。

本轮新增的七个失败详情 worker，均在交付产物形成前受 Page.create/连接异常影响。它们共25次模型调用、38,335输出 token，占本轮 Browser 输出7.7%。上轮六个重复详情 worker 输出995,413 token，占69.2%。因此 worker 个数略多，但代价的性质和量级不同。

连接中断对应 WebCross dispatcher 三次进程重启。三次都紧邻 Page.create 初始加载失败；第三次已是单 worker 运行，不能据此把并发认定为根因。服务端未持久化确切退出堆栈，尚不能证明 Page.create 内部哪一条异常导致退出。

## 2. 时间与 token

来源：[本轮原始日志](</Users/versace/Desktop/abcp browser system/worktree/b224ebca3b224e8493d8ce6ecb210804/run.jsonl>)、[上轮审计报告](</Users/versace/Desktop/abcp browser system/docs/mechanical-validation-audit-997ab5-2026-09-15.md>)。以下 token 按 llm.usage.source 汇总；耗时按事件时间。并行 worker 累计时间不当作任务墙钟，wait_browser_agents 中的实际 Browser 执行不当作无效等待。

| 指标 | 997ab5 | b224 | 变化 |
|---|---:|---:|---:|
| 首条至末条事件墙钟 | 101分44秒 | 60分28秒 | -40.6% |
| 扣除计划人工确认 | 100分57秒 | 60分07秒 | -40.5% |
| 首次派发 | 5分09秒 | 5分27秒 | 慢17秒 |
| 实际启动 worker | 10 | 11 | +1 |
| Browser 调用 | 465 | 181 | -61.1% |
| Browser 输出 token | 1,438,506 | 495,809 | -65.5% |
| Browser 非缓存输入 | 1,915,123 | 875,721 | -54.3% |
| Browser 缓存读取 | 70,073,984 | 22,192,000 | -68.3% |
| Lead 调用 | 52 | 33 | -36.5% |
| Lead 输出 token | 117,130 | 34,591 | -70.5% |
| Lead 非缓存输入 | 177,634 | 162,738 | -8.4% |
| Lead 缓存读取 | 5,613,696 | 2,021,824 | -64.0% |
| Lead 模型调用累计耗时 | 9分35秒 | 12分15秒 | +27.8% |
| Browser 模型调用累计耗时（含并行相加） | 123分26秒 | 39分20秒 | -68.1% |
| 上下文压缩调用 | 4 | 0 | 本轮没有压缩 |

人工确认分别47.374秒和21.373秒。本轮没有 challenge HITL。Lead 时间按 lifecycle.message.start/end 配对，requestTelemetry 的累计734.435秒与生命周期734.878秒吻合。

不能把所有提升归因于代码：商品与排名不同；Lead 从 deepseek-flash 改为 glm-5.3-flash，Browser 均为 deepseek-flash。本轮 Lead 配置仍为 thinking enabled、reasoningEffort max。Lead 的输出少了，但生成时间增加；首次两个 Lead 调用仍耗202.354秒，产出为读取 guide 和开始计划草稿。首次计划还因三个详情同时填 inputs.artifact 与 input_artifacts 被拒，修复一次后批准，未反复重规划。

本轮全角色合计220次 usage 事件，输出549,432、非缓存输入1,077,269、缓存读取24,216,921。辅助角色单列，避免把它们算进 Browser：计划审计1次/1,941输出，数字抽取2次/7,431，数字抽取修复2次/8,230，字段语义审查1次/1,430。缓存读取量不等于可等额节省的费用。

## 3. Worker 到底如何增加

| phase | 实际启动顺序 | 结果 |
|---|---|---|
| collect | 001 | 完成，47步，6分33秒 |
| save-p30 | 002 → 006 → 010 | 前两次传输失败；010完成，39步，11分47秒 |
| save-p31 | 003 → 007 → 011 | 003传输失败；007 Page.create 初始加载失败；011完成，42步，14分00秒 |
| save-p32 | 004 → 008 → 012 → 015 | 004/012 Page.create 初始加载失败；008传输失败；015完成，28步，10分24秒 |

005、009、013在 Fleet.list 启动获取阶段遇到已失效连接，没有启动业务 worker；014因复用的 browser-012 slot 已被清理而拒绝。原始证据：run.jsonl 1335、1704、4336、4372行。

collect 完成约1.2秒后，002/003/004由 Harness 自动并发派发。第一次恢复后，Lead 又并发派006/007/008。第二次恢复后，Lead 在第18步明确提出“两个中断都在三个 worker 共享 Fleet 时发生，改为一次一个”这一假设，随后010、011、015确实串行。原文见 run.jsonl 1747行；后续派发见1767、2979、4406行。

这不是 max_browser_agents=4 被机械层改成1，也不是本次删除验收规则触发额外自动派发；是 Lead 根据当时故障作出的调度选择。它作为临时降级有现实理由，但第三次单 worker 仍触发同样的服务端重启，削弱了“并发根因”的推断。不能将该推断固化成默认串行规则。

本轮关键路径按事件切段：

| 区段 | 耗时 |
|---|---:|
| 规划、确认、首次启动 | 326.54秒 |
| collect | 392.77秒 |
| 两轮启动失败、恢复及再派发，直到010启动 | 243.39秒 |
| 成功详情010 | 707.16秒 |
| Lead交接到011 | 12.94秒 |
| 成功详情011 | 840.16秒 |
| p32再失败、恢复、过期slot拒绝，直到015启动 | 148.50秒 |
| 成功详情015 | 624.25秒 |
| 收尾（读取、生成、数字及语义审查） | 332.52秒 |

三个成功详情实际累计36分12秒；仅以本次单 worker 时长不变为假设，理想完全并行会取最长14分钟，差22分11秒。这是串行的量级分析，不是稳定并发后的可兑现节省承诺。010/011/015内部模型生成耗时分别565.68/693.08/583.43秒，占各自worker墙钟80.0%/82.5%/93.5%；稳定后仍值得评估模型调用和输出规模。

## 4. 三次连接中断的原始证据

下表为北京时间，跨越9月15日至16日。所有实例均运行同一个 build：`wc-cac0fe42c9f8-ab3c724d5be7d6b6d89eabc7`。

| 时间 | 服务端事实 | Harness表现 |
|---|---|---|
| 9月15日23:41:44.975 | PID20356 Page.create -32005；13毫秒后移除runtime描述文件；随后新PID56604启动 | 004先被结束；003 Page.getState requestSent=true断连；002请求前已发现reader失败；005启动失败 |
| 9月15日23:43:44.359 | PID56604 Page.create -32005；19毫秒后移除描述文件；新PID56943启动 | 007先被结束；006/008连接失败；009启动失败 |
| 9月16日00:12:31.088 | PID56943 Page.create -32005；13毫秒后移除描述文件；新PID61580启动 | 012被结束；下一次复用旧连接的013启动失败。此时是串行运行 |

可直接打开的旧进程日志：

- [第一次，467行](</Users/versace/Library/Application Support/webcross/logs/dispatcher-host/b73d4d86-cda9-4ab4-9b14-5e0ae5ff4eb9/dispatcher-host.2026-09-15.1.log:467>)
- [第二次，29行](</Users/versace/Library/Application Support/webcross/logs/dispatcher-host/70f3642d-894e-4f09-abec-7ade7ad2b33f/dispatcher-host.2026-09-15.1.log:29>)
- [第三次，23行](</Users/versace/Library/Application Support/webcross/logs/dispatcher-host/ac786d5f-fdd2-4b1e-8a0d-9032ab8c0f9d/dispatcher-host.2026-09-16.1.log:23>)

原始失败 executionId 分别为0a34966f-c798-44c7-88f4-150f2c044034、055182cf-fdad-4929-8e6a-f703674bc09c、7eddf968-7905-467b-aef9-013739e31c50，可用于WebCross侧关联调查。

只读查询 webcross-agent-identity.db 的 runtime_instances，三个旧实例的 stop_reason 均为 dispatcher-host-restarted；新实例的启动时间与日志吻合。运行日志与当前安装包 resources/build-info.json 的 sourceRevision 均为 cac0fe42c9f85b86e498f24c929cc3f9bb334aee；工作区 abcp-platform HEAD 为 f71794812ed365ba6ab1a17c30e59d8ffb7fb316，不能直接拿工作区源码当成运行版本。

另核对安装包 app.asar 内 dispatcher-host/dist/cli.js：fatalExit 会清理描述文件、向父进程发送 host.fatal 后退出，没有持久写入异常堆栈。该证据说明为什么现有日志无法给出更具体的退出原因；描述文件清理本身不唯一证明 fatalExit。已确认的是服务端重启及其时序，未确认异常堆栈/退出触发来源。

Harness侧也须澄清：004/007/012的“blocked”答案由 page_create.py 的terminal分支自动生成，并非模型看见错误后自行放弃。新worker没有已委派页面，恢复探测范围为空，框架结束worker并交给Lead。因此Lead后来说“worker没有执行备用方案”并不准确；框架没有给它下一个模型回合。保留页面权限边界是合理的，解决方法不应是允许worker任意接管其他页面。需要服务端提供创建副作用的明确结果/页面身份，再由Harness按归属处理。

## 5. 本地交付与优化命中情况

从最终选中的三个交付JSON取路径，并对每个路径做本地stat：

| 排名 | 商品图片 | 视频 | 详情图片 | 声明交付路径 | 存在且非空 |
|---|---:|---:|---:|---:|---:|
| 30 | 5 | 1 | 16 | 22 | 22 |
| 31 | 5 | 1 | 11 | 17 | 17 |
| 32 | 8 | 0 | 31 | 39 | 39 |
| 合计 | 18 | 2 | 58 | 78 | 78 |

全部位于 `/Users/versace/Desktop/1688女装汉服/第30名`、`第31名`、`第32名` 对应分类子目录。没有只把worktree提取JSON冒充素材交付。本轮只核验文件存在/非空和路径归档，不额外断言图像/视频内容穷尽性。

明确观察到的优化效果：

- 第31名packaging、第32名videoFiles的空值声明通过，回执标注mechanicallyProven=false，而非要求八个布尔字段。
- 001在剩余5步、4步时分别记录compaction_skipped；本轮无压缩调用。见run.jsonl 889、909行。
- 所有详情已保存的有效产物都在同一worker结束时通过，没有因验收再派一个完整详情worker。
- 首次三个详情的同wave自动派发正常。

仍有计数口径问题：011回执fileArtifacts含23个路径，其中17个交付文件、5个DOM.getImg原始图、1个截图；015含78个路径，其中39个交付路径和39个原始导出文件。这些是本worker的过程产物，不能据此说历史下载又混入。Lead却一度把011的23个路径说成23个交付文件。最终总完成回执中的downloads.completed=39也不是这次78个交付路径的总数；下载操作计数、原始文件和最终交付清单应明确区分。

## 6. 收尾新发现：数字审查仍有真实错误和误报混合

最后一个worker结束后又过5分33秒任务才结束。其中两次final_answer工具调用耗88.791秒和108.785秒，共3分18秒；包含四次数字模型调用和一次语义审查。不能把这整段都记为Lead自身推理。

第一次数字核对有两条contradicted：

1. Lead写第30名属性20项，实际数组22项。这是真实数值错误，算术校验应保留。
2. Lead说product30_delivery文件中列明22个文件路径；数字模型将其提取成该JSON的row_count，于是机械层得到1行并判22≠1。这是语义映射错误，不是交付文件缺失。

第二次没有contradicted，但仍有12项unresolved，原因均为metric=count与unit=items不匹配。当前numeric_facts.py的schema允许items，描述引导对products/entities用items；校验器却只允许count对应field_entries。metric描述还把“per-artifact number”宽泛引导为row_count。这是需要单独修正的抽取协议/指导不一致，不适合通过扩展商品词表或删除算术比较来解决。

## 7. 下一步建议（本轮未执行）

1. **WebCross优先**：按上述executionId和运行build排查Page.create初始加载失败到dispatcher退出的链路，持久化fatal/exit原因，并在创建已发生但导航失败时返回可验证的页面身份和副作用状态。Harness不通过加重试、改URL规则或Runtime.evaluate掩盖它。
2. **Harness/审查输入**：统一数字抽取schema、prompt与校验器的metric/unit定义；不能把无法确定计数对象的语义结果当作已证实的数值矛盾。保留真实20≠22这类检查。
3. **交付回执**：将最终声明交付、临时导出/截图、下载操作分别计数，Lead最终答复依据声明交付清单；无需新增站点或字段词表。
4. **恢复事实与调度**：明确区分框架终止worker、连接已恢复、页面是否仍可绑定、旧slot是否已退役。让Lead基于这些事实决定恢复与并发；不将本次猜测固化为默认串行，也不让失败phase被自动当成首次待派发重新执行。

下一轮性能评估应固定Lead/Browser模型和思考配置，优先验证WebCross稳定性；否则串行降级、重启和模型更换会继续混淆优化收益。
