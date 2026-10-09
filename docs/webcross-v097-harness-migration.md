# WebCross 0.9.7 与 Browser Skill 执行同步

更新：2026-10-07。范围为源包协议同步、Skill Builder 合同更新及独立 `/browser` 的显式 Skill 复用策略。

## 1. 实例与源码分别核对

- 实际安装实例为 WebCross `0.9.7-beta`，已在该实例执行隔离页面测试。
- 实时 catalog：`sha256:4353ee6b25dd1c25bb33c594c8231a1d46faab7783a010b1092fd1c91582bd64`；56 个平台方法，缓存保留策略允许的 51 个方法。
- 实时 guideRevision：`sha256:1b124cec1eecd8e343b71802eb4d1f605eda5a0e911c9059166dcb9a288a8375`。
- 平台工作区 HEAD 为 `6b9647d00409c1f261e9edecfe2d6678d9cf452d`，仍使用旧步骤引擎。本轮没有修改平台工作区，也没有把它当作安装实例的执行证据。
- 协议依据来自实时 `System.describeAction`、`Workflow.getGuide`，并对照安装包中的 workflowSdk、workflowBridge、workflowCompiler 和 workflowExecution。安装实现用 esbuild 编译虚拟模块，在 QuickJS 环境执行。

合同快照见 [fixtures](../tests/fixtures/webcross_v097/provenance.json)。

## 2. 当前协议与职责

| 项目 | 当前处理 |
| --- | --- |
| 源码 | `{schemaVersion,name,entry,files,timeoutMs?}` 源包；默认导出异步入口，可相对导入 JS/TS 模块 |
| 本轮变量 | 顶层 `input`，与源包、页面绑定分开；正式工具现有 `variables` 参数映射到 input |
| 页面 | `binding.pageId/fleetId`；Harness 先执行已有身份、Fleet 注入和页面归属准入，再生成受限 API |
| SDK | `actions/context/waitFor/readText/sleep`；Action 返回业务数据；waitFor 先订阅再触发，超时抛错 |
| 观察 | full AXTree 仍为受租约约束的 artifact；详情直接为 `details`，普通 browser_call 兼容归一为 records |
| 执行结果 | `status/output/results/error/timing`；调用记录保留 action、状态和 source。succeeded 不能单独证明业务目标完成 |
| 在线控制 | `getStatus/pause/resume/cancel`；progress 使用 status、completedCalls、activeCalls；原步骤事件只供旧部署兼容 |
| 执行期限 | 客户端至少等待源包期限＋10 秒，单次发送，不自动重放；不确定的传输结果与脚本失败分开表达 |
| 旧格式 | 当前部署明确提示旧 steps/transform/store 需要重建，不做猜测性的业务翻译 |

编译和 JS 语法执行由 WebCross 负责。Harness 继续校验身份、权限、JSON 合同和不变量。

## 3. `/browser` 复用策略

保留 BrowserAgent loop，更新其 JS/TS 源包执行合同；已选版本的源包读取和正式 Workflow 入口识别同步到新协议。独立 Skill Runtime 的职责拆分与后续 Builder 恢复/发布改动不属于本次协议提交，保留在工作区。

没有强制 Workflow 使用次数、覆盖率门槛或站点字段门禁。是否合并 segment、何时继续与是否完成，仍由模型判断。

执行回执不再向模型重复回传业务源码和受信任包装源码。完整请求先保存到任务资源，再返回输入、绑定、源包哈希与资源引用；保存失败时保留原回执，不执行这项省略。大型调用值保存完整记录后返回索引引用，业务 output 保留。省略后的 completedCalls 取完整总数，不取保留下来的列表长度。

## 4. 动态调用的权限边界

原步骤 Action 名称扫描无法限制 JS 的动态方法调用。受信任入口将受限 API 传给未改写的业务源文件，逐次检查方法、页面/Fleet 和文件清单。受信任模块在业务模块之前初始化，固定 SDK 内建对象与原型，防止替换 JSON 编码或权限检查所用函数。

这是所有源包通用的权限约束；业务脚本和模型不能自行扩大授权。误拒范围主要是跨页面所有权操作、独立网络/文件输出、修改内建对象以及未列入本轮清单的计算文件路径。恢复方式是调用相应独立 Harness 工具、完成授权或提供规范路径，源码使用自己的帮助函数。不能让 LLM 的自我声明代替这些权限约束。

- Workflow 暂按单页面/Fleet 执行；创建、发现、认领和关闭页面走独立工具。
- Network.readApi 与 Runtime.evaluate 继续走各自工具边界。
- 截图与 DOM 图片导出涉及文件输出/视觉判断，走独立工具。
- File.handleChooser 仅接受已授权、真实存在且规范化的文件清单；清单来自 input 或单个文件授权，不因字符串出现在输入中而授予权限。
- 原文件与实际包装源包分别记录哈希。包装增加两个源文件，仍受平台包大小和文件数量限制。

当前安装引擎把代理 Action 调用的位置映射到受信任模块；编译错误和业务脚本抛错能映射到业务源文件。保留真实 source，不伪称所有 Action 的 source 都是业务调用行；诊断同时读取 action、actionCode、调用记录和源包版本。

## 5. Builder 的协议调用

Builder 实时读取 Workflow guide 和 Action schema，使用 JS/TS 源包与原生循环、条件和模块。试运行将业务 input、页面/Fleet 绑定与源包分开，并为动态 Action 应用相同的权限约束。

可以授权真实测试页和本轮上传文件；清理只关闭 Builder 自建 Fleet，用户认领页面不被关闭。Workflow 运行状态、调用记录、输出是否存在和失败位置分别记录，不能把 succeeded 自动视为业务完成。

正式发布仍由用户决定。开发探针目录分离、后续日志来源切换、草稿发布恢复与独立 Skill Runtime 属于另外的工作区改动，不包含在本次协议提交中。

## 6. 验证证据与范围

在实际安装实例的新建隔离页面中已验证：

- TypeScript 入口与 `.js` 导入指向 `.ts` 帮助模块。
- 同一源包在两组字符串、不同数组长度下填表并读取当前字段值；原 workflowHash 一致。
- 实际 BrowserAgent 正式 Skill 工具两次使用同一 Skill 哈希，改变 input 后成功，SQLite 两条 invocation 均为 succeeded。测试版本只发布到临时目录。
- full artifact/readText 与 inline details；事件订阅先于导航。
- 工作区内文件指派 `handled=true/selectedFileCount=1`，控件读取到 `C:\\fakepath\\upload.txt`。这是控件指派证据，不能证明任意业务网站上传成功。
- 另一 `/private/tmp` 路径通过 Harness 文件清单后，被平台拒绝为 `workflow-action-failed/actionCode=file-path-not-allowed`。两层路径权限分别保留证据。
- 越界页面调用在宿主调用前拒绝；业务异常前的一次 Input.type 仍在调用记录；编译错误包含 main.js 行列；实时状态 running 可取消为 cancelled。
- 完整请求资源的字节 SHA-256 与回执中的 persistedPayloadSha256 一致。

原始回执：[native-receipts](../tests/fixtures/webcross_v097/native-receipts.json)、[完整请求索引](../tests/fixtures/webcross_v097/native-evidence-index.json)、[平台路径拒绝](../tests/fixtures/webcross_v097/native-path-refusal.json)。测试 Fleet 与新建工作区测试文件均已清理。

本轮相关回归使用项目 `agent` Python 3.13 环境。主回归覆盖源包/权限、Builder/SQLite、模式差异、观察、定义复用、错误/取消、投影、传输脱敏与缓存，结果为 **281 passed / 1 skipped / 13 subtests passed**；本地 Unix socket 测试另在允许创建 socket 的环境运行，结果为 **11 passed / 3 subtests passed**。合计 **292 passed / 1 skipped / 16 subtests passed**。当前方案及后续阶段见 [Tau 实施计划](tau-skill-builder-agent-loop-plan.md)。

不是全量旧测试套件通过声明：仍保留的旧步骤 schema 漂移测试含已失效的语言断言，旧 observer cleanup 测试仍引用此前删除的 Skill 模块；未把它们的要求重新做成运行门禁。

未进行真实模型驱动的拼多多发布任务，也没有可比的业务 token/耗时样本。因此不声明实际 Workflow 采用率、任务速度或 token 收益已验收。后续按模型轮次、cache_read/cache_creation/uncached_input、模型耗时、平台/工具 I/O、失败恢复及排除人工等待后的端到端时间评估。

## 本次提交的隔离检查

仅以 HEAD 加协议适配代码构建临时副本，14 个相关模块得到 **382 passed / 33 subtests passed**；当前工作区同范围为 **383 passed / 33 subtests passed**。差异是一项属于独立 Builder 探针目录生命周期的测试。临时副本使用测试专用配置和样本，未复制配置密钥，也不依赖未提交的 Skill Runtime。新增原始合同回归及 v0.9.6/v0.9.7 快照一并纳入提交。
