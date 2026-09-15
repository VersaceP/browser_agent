# Browser Agent prompt 与 guidance 优化记录

本轮检查 system prompt、Browser 工具说明、按需 guide 和实际 Harness 状态处理代码，修改限定为模型可见指导与对应测试。没有修改 WebCross、URL 解析、工具权限、生命周期门禁、调度、模型或压缩配置，也没有执行真实浏览器任务。

## 发现与处理

| 问题 | 调整 |
| --- | --- |
| “拒绝空值”与合同允许带证据为空冲突 | 空值服从已批准 worker_contract；禁止编造缺失证明，未解决的字段保留为未完成义务。 |
| 要求验证后才 record_extraction | 明确该工具是持久化与校验入口，必须读返回的验证结果；savedPath 不代表合同通过。 |
| Workflow 把“已派发”当成“已成功发生” | 已派发意味着可能产生副作用；依据完成步骤、原始结果与资源状态判断，失败不自动撤销前序效果。 |
| 加载失败被描述成页面未移动 | navigation_not_dispatched 才证明该请求未发送；加载失败不能证明旧 URL/文档未变。 |
| 每次动作后无条件刷新 AX | 对照 axtree_state 的现有规则，说明无导航 Page.go、已验证只读 evaluate 及 Harness 接受的新 AX 事件例外，仍服从实际 freshness 回执。 |
| 默认源卡片路线、peer 缺区必须重入 | 改为结合当前证据选路；保留观察到的 href 和来源，不猜 URL。明确回执要求的恢复仍须遵守。 |
| 所有缺失字段都要求视觉检查 | 视觉工具用于当前能力支持的具体视觉疑问；不把截图设为每个空值的必经步骤。 |
| HITL 后永不再调用与后续新挑战混淆 | 禁止对同一 pending pause 重复请求；新的挑战根据新的观察与回执处理。 |
| local_fs_* 被整体称作只读观察 | 区分 local_fs_read/search 与 local_fs_batch 的文件操作；保留 Desktop 交付和清单说明。 |
| 大段 AX/Workflow 细节与 guide 重复 | AX 语法移到 browser.observation-evidence；Workflow 细节保留在已有、经过示例校验的 browser.workflow-segments。 |

## 指导归属

- system prompt：目标和权限、当前能力、证据与身份、即时重放安全、工具入口、合同与结束标准。
- browser.observation-evidence：AX 标记、当前快照与历史文件的区别、按问题选择定向读取。
- browser.workflow-segments：已决定动作的分段、引用与事件窗口、失败解释、保存定义的复用。
- browser.collection-materialization：集合覆盖、空值、证据不足与验证顺序。
- browser.page-lifecycle：加载失败、未派发、快照时效。
- browser.offload-and-local-fs：有限文件读取、真实文件操作与交付清单。

对浏览器事件与 Workflow 时序的说明基于当前工作区实现和既有协议规则，不把旧 live 测量的某次事件时间提升成每次运行都成立的保证。安装版本是否表现一致需要下一轮真实任务确认。

AX 可见但 WebCross 无法解析操作目标仍是协议/执行问题；此次没有通过提示词增加绕行机制。

## 验证

361 项相关测试通过：prompt/guide 注册与检索、工具说明、Workflow 示例的 schema 与 policy 校验、Workflow 开关、多模态、页面生命周期及 AX 格式。新增覆盖四种 Workflow × 多模态组合，检查旧冲突指令不再出现、必要权限/重放边界仍存在。

旧测试对“必须先走源卡片”以及旧 Lead Workflow 文案的逐字断言，改为当前能力和路由指导的一致性断言。没有改变被测运行时行为。py_compile 与 git diff --check 通过。

## 提示词体积

相同测试能力集合（Page.getState、DOM.getAXTree、Workflow.execute），无 live agentGuide 和部署静态追加文本，包含相同构建路径的能力摘要与 guide manifest；以下为字符，不是模型 token：

| Workflow | 多模态 | 修改前 | 修改后 | 减少 |
| --- | --- | ---: | ---: | ---: |
| 关 | 关 | 42,557 | 38,321 | 10.0% |
| 关 | 开 | 41,410 | 37,249 | 10.0% |
| 开 | 关 | 47,313 | 39,204 | 17.1% |
| 开 | 开 | 46,166 | 38,132 | 17.4% |

没有删除实时能力摘要或改写注入的 WebCross 行为来源来追求缩短。guide 按需读取仍会产生输入成本，实际收益需从下一轮任务验证。

## 下一轮观察

在相同模型与任务口径下比较：每个 worker 的输入/缓存/输出 token、扣除 HITL/资源等待后的耗时、重复完整 AX 读取和相同文件检索次数、Workflow 定义重写量、record_extraction 修复轮数，以及最终交付覆盖。不能仅凭提示词变短就认定任务更快或更可靠。
