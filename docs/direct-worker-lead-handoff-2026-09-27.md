# Direct Worker 完成后的 Lead 交接修复

事故任务：`3be9419ec5e8496f96114b792eb63def`。

## 证据与原因

该运行记录的源码指纹为 `0b06177-dirty.c5ac06d5ce4d`，与排查时工作区指纹完全一致。原始 run.jsonl 第 1054 行是 Worker 的 final_answer(done)，第 1070 行记录 Worker done / validated_done，第 1083 行是 Lead final。Lead 模型仅运行五轮，最后一个调用仍为 emit_direct_task_plan，没有收到 Worker 结果后显式调用 final_answer。

`_run_direct_worker` 发现 done + validated_done 后调用 `_direct_finalize_from_worker`。旧实现直接调用 `_lead_final_answer` 并设置 `_terminate_lead=True`，最终标成 lead_decided。此前修复只处理 partial 等未完成回执，成功分支仍然自动终止；旧测试也明确断言成功分支必须终止。

另一个已回放确认的问题是 execution_handoff 丢弃直派工具的执行回执。去掉自动结束后，Lead 首次进入 execution 时仍可能失去 Worker 证据和 remainingObjective。

本次任务没有 File.handleChooser 调用。Lead 在计划中明确写了只选类目、不要上传图片、文件夹仅用于交付归属。原始任务文字到确认类目并附文件夹，文件夹用途的语义歧义不能由自动续跑消除。这次控制流修复不承诺模型一定选择继续上传。

## 已实施

- 直派 Worker 返回后保留原始回执及验证事实，返回非终止结果。done + validated_done 同样由 Lead 审阅。首次执行、接管已有 Worker、恢复持久化完成结果共用此路径。
- 新增 lead.direct_worker.handoff 日志，明确 terminal=false；自动直派不再生成 lead_decided。真实 Lead final_answer 仍经过原有一致性及权限校验。
- 规划转执行时保留最新 emit_direct_task_plan 的工具回执，包括 continuation、证据及 offload 引用。工具调用身份用于归属校验，本地文件内容不能伪造交接。早期失败候选仍留在历史日志。
- 提示词说明直派回执需要 Lead 审阅。没有增加站点、字段或业务完成条件的机械判断。

## 验证与代价

隔离完整 Lead 循环测试使用模拟模型与浏览器回执，验证 fresh / attached / recovered 三条路径都有第二轮模型调用，接收证据后显式 final_answer；不重复派发已有 Worker。另覆盖部分结果、外置引用、审批反馈和伪造文件内容。

最终针对性测试（含提示词一致性）：61 passed，13 subtests passed；git diff --check 通过。成功直派增加一轮 Lead 判断；保留的回执沿用已有 offload 机制。未调用真实模型或操作站点，因此没有可比的端到端耗时、cache_read/cache_creation/uncached_input 指标，不作成本改善结论。

修改在工作区源码中，现有运行进程没有热更新；需由加载新源码的运行验证。原事故任务及浏览器页面未重跑。
