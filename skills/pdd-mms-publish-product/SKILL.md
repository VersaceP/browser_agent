---
name: pdd-mms-publish-product
description: Fill a Pinduoduo merchant product form from a local product folder through
  six saved WebCross JS Workflows, then verify actual field/media/SKU results. Stops
  before save or submit.
compatibility: WebCross 0.9.7 JS source packages; assigned Fleet; signed-in merchant
  session; authorized local files; independent Skill Runtime with orchestration.json.
  Runtime receipt/continuity fixes listed in EXECUTION.md remain pending.
version: 0.5.0
source-task: 8ee7e41ea6e64011aea41ea793e019ec
reference-tasks:
- 295b8fc3821b400c96ee916a1e6fbef1
- 3e7a26f6b33242f7a74c9ae73ce33ed9
---

# 拼多多后台商品填写

本草稿由真实商品填写与局部修复产生，版本 0.5.0。执行覆盖与尚未验证的分支见 [EXECUTION.md](EXECUTION.md)。业务页面已通过独立核验；尚未对最终包做一次从首页到结尾的完整 Harness 复用测试。

## 正式执行入口

入口是 `orchestration.json`，连续执行六个正式源包。`workflows/manifest.json` 是索引；`params/` 是输入示例；两者都不能作为可执行 Workflow。开发探针与旧代码留在来源任务的 `coding/`，不包含在正式执行入口中。

1. 核对本轮需求，读取已授权的商品资料，确定属性、规格、媒体顺序和图片分配规则；不把上一轮业务值当成本轮值。
2. 使用现有 Skill Runtime 绑定指定 Fleet 的首页，准备 `bindings = {homePageId, product}`。`product` 的字段见 `params/product.example.json`。
3. 绑定保存的 `orchestration.json`，调用 `run_skill_workflows` 执行原文件。正常换变量只改输入，不重写程序。
4. 前五段按各自 `/output/ready` 继续；最后的核验段返回模型审查本轮目标。`succeeded` 只说明脚本运行，不能替代业务核验。
5. 当前包只填写和核验，不执行保存草稿或提交上架。本次用户明确自行处理这两个动作。

页面 ID 属于执行身份。首页经 `binding.pageId` 绑定，后续页面取 `01-entry.output.pageId`；Fleet 沿用当前任务绑定。已存在分类页或表单时，先根据原始记录与当前页面确定 `product.resumePageId`，不再从首页创建一个业务对象。

## 输入

| 字段 | 含义 |
| --- | --- |
| `homeUrl`, `categoryKeyword`, `categoryPath[]` | 指定首页和所需类目路径 |
| `title`, `attributes[{label,value}]` | 明确标题及属性值；不存在合适候选时返回模型/用户决定 |
| `specTypes[{name,values[]}]` | 本轮规格类型与值；程序动态读取生成行，不保存上轮节点 ID |
| `stock`, `groupPrice`, `singlePrice`, `referencePrice` | 本轮库存与价格 |
| `carouselImages[]`, `detailImages[]`, `videos[]` | 已授权文件路径，分别进入对应控件 |
| `previewImages[{match,files[]}]` | 按当前行的规格键匹配文件；例如 `match:{颜色:黑色}` 可用于该颜色的各尺寸 |
| `resumePageId`（可选） | 来源证据确认的已有分类页/表单；不是浏览器任意旧标签页 |

本版本示例使用新的 `product` 对象；旧 0.4.0 的分段 payload 不能直接作为本版本 bindings。保留原版本及其字段记录，复用时映射到这里的字段。

示例的“2026年秋季”是本轮用户明确改选；其他任务不自动沿用。成分含量来自原资料的明确范围。源资料没有提供的属性不得猜填。规格图片规则是显式输入，不按控件顺序猜颜色。

## 局部恢复

- 每段先读当前状态，尽量保留已有值和上传结果。文件指派成功与页面接收完成是两项事实；需要所属区域计数、媒体结果或行预览证据。
- 失败时读取完整回执与相关区域，区分当前 UI 状态变化、输入问题、程序缺陷及平台失败。保存已发生的副作用；只修受影响的模块。
- Browser 用于必要的定位、遮挡/候选诊断和 UI 恢复；Coding 延续同一上下文。修代码不会复原页面，重跑前重新确认适用起点。
- 禁止一键复用。不能因为修订源码、试跑成功或检查点缺失而重新创建商品、重复指派文件或重新填写已满足本轮输入的字段。
- 已有图片数量相同不证明文件相同；已有预览图也不证明符合新规则。换文件/规则且已有媒体时，由模型结合本轮输入和历史指派记录决定保留、更换或局部修复。
- 调用 `DOM.getDetails` 时按需求选择 `state`、`attributes` 等视图；不能假定结构视图列出所有属性，不能把静态说明文案当上传/发布结果。

## 已知边界

本样本覆盖两个规格类型、三乘三组合及颜色图片映射。不同规格类型、数组长度、类目、已存在额外规格、替换已上传媒体等情况尚未实际验证。当前预览图分段适配了实际 CSS 背景结果和行重渲染；不是每个站点的通用上传实现。

登录跳转、未知弹窗、候选缺失、未明确的文件替换语义等返回模型判断。程序的有界等待/循环只限制单次执行工作量，不判断用户任务应终止。

当前 Harness 大回执转换、compaction 和修订检查点仍有已证实缺陷；本次直接 WebCross 执行不能证明它们已修复。查看来源任务的 `runtime-recovery-plan.md`。草稿发布仍由用户选择。
