# 增量派遣审查与辅助模型清理（2026-09-28）

## 范围和原因

Lead 已采用 `spawn_browser_agent(assignment=...)`，但辅助审查仍保留整份计划、初始计划基线和旧修订协议。两者对审查对象的理解不一致：下一项合理工作可能被当作不完整的整任务计划，先前计划又可能被当成用户原始需求。

本次修改工作区 Harness 源码。不迁移旧任务，不修改 WebCross，也没有重启或验证已运行的进程。这里记录的是代码路径和回归验证结果，不能据此确认历史上传事故的原因，或认定部署实例已经使用这些修改。

## 现在的职责

| 模块 | 输入与责任 |
| --- | --- |
| Lead | 根据原始目标、用户补充、执行证据决定下一项工作及最终状态 |
| 派遣审查（配置名仍为 `plan_validator`） | 审查本次 assignment，结合相关依赖、明确的替代关系、预算使用和证据；返回 findings 与 remainingWork |
| 审批意图分类 | 使用与终端显示完全相同的 assignment 视图和本轮之前的用户输入，解释用户答复 |
| `/browser` 分类 | 保留完整原始需求，输出可选的交付契约；未声明时使用观察回执 |
| Worker 回收 | 返回状态、契约检查、原始证据、用户补充和待判断事项；下一次派遣由 Lead 显式发起 |
| 字段语义审查 | 使用原始目标、有序用户补充、产物所属 assignment、来源和截断标记判断字段证据 |
| 数字证据检查 | 继续使用现有算术核验；只把当前有效交付当作最终交付，保留替代前产物用于审计 |

未新增逐次 Worker 返回的模型裁判，也未引入站点、字段、价格或库存特判。

## 删除的旧实现

- 整计划语义审查、初始计划不可变基线、旧裁判输出协议、模糊数量血缘匹配和数量授权枚举。
- 旧候选审批哈希兼容、旧契约豁免参数、完整计划修复分支及旧 browser 工具 schema 包装。
- 隐藏的下游自动派遣及其配置；等待 Worker 只负责收集、去重、交回结果。
- 字段语义拒绝达到次数后强制以 `partial` 结束的分支。
- 强制表单任务采用“一控件一行”的分类约束。

内部 `plan/phase` 名称仍用于持久化账本、依赖和预算；这不是恢复旧的 Lead 计划工具。保留既有 `plan_validator` 配置名以表示审查模型配置。

## 协议、证据和权限

1. 每次只能追加一项 assignment，已接受的历史契约不能被原地改写。替代必须明确指定前项，并保留预算 lineage 和已用次数。
2. `candidateHash` 绑定编译后候选及其修订原因；`reviewContextHash` 还绑定任务身份、原始需求、有效用户输入、当前证据和审查事实。审批期间上下文改变时必须重新审查；相同候选的既有用户审批可复用。
3. 审查回执必须满足结构、身份、证据引用和判决一致性校验。拒绝和不可用均不派遣，也不自动结束主任务；协议修复和调用重试有界，近期错误有期限缓存。
4. HITL 原始输入在恢复动作前落盘；单独保存输入顺序，避免存储层对字典键排序导致上下文乱序。纯审批确认归档但不当成新增需求。
5. Worker 声明、集合耗尽报告和产物结构通过均有证据范围；不会自动升级为网页接纳或业务完成。数组值、截断状态及来源引用实际进入字段审查输入。
6. 以上机械检查约束身份、协议、状态、权限和记账一致性。是否应减少数量、继续尝试、允许空字段或选择另一条业务路径，仍由模型结合事实判断。失败的契约或审查会返回可修正的原因与证据。

另外修正了 Harness 对实时平台指南的提取边界：只注入编号的行为章节，避免平台附录改名后把不可读取的工作流文件指引混入 Worker 提示词；WebCross 原文件未修改。

## 验证

新增 `tests/test_assignment_review.py` 使用真实编译、接受和派遣代码，模拟模型和浏览器 I/O，覆盖：

- 首次及后续派遣的审查失败、缓存到期、协议修复、错误引用与身份绑定。
- 替代关系和预算保留、互不相关的数量不做模糊匹配。
- 审批时用户补充与执行证据变化、显示和分类目标一致、输入存储顺序。
- HITL 意见在恢复前留存并随等待结果交回；等待不会派遣新 Worker。
- 数组截断与来源送达语义审查、替代产物的历史归属和当前产物复用。
- `cache_read`、`cache_creation`、`uncached_input` 三类计量保留及缓存命中时的模型调用次数。

扩展回归命令（使用现有 `agent` 环境的解释器，避免 `conda run` 在环境目录创建临时文件）：

```sh
/Users/versace/opt/miniconda3/envs/agent/bin/python -m pytest -q \
  tests/test_assignment_review.py tests/test_delegation.py \
  tests/test_plan_validator.py tests/test_browser_mode_classifier.py \
  tests/test_field_semantics.py tests/test_completion_receipt.py \
  tests/test_task_plan_validator_schema.py tests/test_task_plan_compact_contracts.py \
  tests/test_semantic_simplification_batch1.py tests/test_resume_projection.py \
  tests/test_phase_scheduling.py tests/test_empty_response_guards.py \
  tests/test_llm_connection_retry.py tests/test_resume_hitl_reactivation.py \
  tests/test_resume_runtime.py tests/test_resume_recovery_only.py \
  tests/test_resume_review_safety.py tests/test_resume_bootstrap_safety.py \
  tests/test_resume_ids_and_producer_binding.py tests/test_resume_state.py \
  tests/test_resume_receipt.py tests/test_prompt_guides.py
```

结果：**385 passed，127 subtests passed**；266 条警告来自现有 `datetime.utcnow()` 弃用用法。随后补充裁判把 `decision` 写成对象/数组、把证据引用写成对象的异常类型覆盖，针对 assignment 与字段审查再次运行，结果为 **64 passed**。两次结果存在覆盖，不相加计数。`git diff --check` 通过。

这些测试验证控制流和证据协议，不能证明模型对真实任务的理解质量。尚未进行真实站点和线上模型的可比任务评测，因此不报告 token 或端到端耗时收益。实际比较需同时记录三类输入 token、模型轮次、工具/平台 I/O、重试及扣除人工等待的耗时。
