# RFC: provider-aware output continuation

状态：**未实施**，本轮明确列为非目标  
提出时间：2026-09-03  
关联：[agent-event-and-logging-refactor-plan.md](agent-event-and-logging-refactor-plan.md) 阶段 8

---

## 1. 问题

`config.json` 的输出上限（当前 24k token）用完时，provider 在生成中途停下，
`stop_reason=max_tokens`。harness 现在的处理是：记下已收到的文本、注入恢复提示、
连续三次后终止为 incomplete（[agent_harness.py](../agent_harness.py) 的截断分支）。

本轮重构给这条路补了**证据**，没有补**功能**：

- `store_received_model_output()` 把已收到的前缀落盘，返回 `receivedOutputPath`；
- `TruncationInfo` 用 `provider_truncated` / `source_complete` 把它和"harness 自己
  裁剪了模型副本"彻底分开。

它**不能**叫 `fullOutputPath`。Pi/Tau 的 `fullOutputPath` 指向的是本机已经完整产生
的 bash stdout，裁剪只发生在给模型看的副本上；LLM 的后半段则从未被生成，任何磁盘上
都没有它。名字必须说实话，否则下一个读者会去 page 一个不存在的后缀。

## 2. 为什么"带前缀续写"不是一行代码

### Anthropic

- assistant prefill 与 extended thinking 不兼容：带 thinking 的轮次不能用 prefill 续写。
- 断在 `tool_use` 的 JSON 中间的 assistant 消息不是合法消息，回灌会被拒绝。
- thinking 块必须带原样 signature 回传，前缀重组时不能重新编码。

### OpenAI 格式

- 没有官方的 prefill 契约。把完整 assistant 文本作为历史上下文回传是合法的，
  但"从这个字符继续、不重复"没有任何接口保证。
- reasoning 内容（`reasoning_content` / `encrypted_content`）的续写语义未定义。

结论：**不能承诺精确前缀续写**。可以承诺的只有"把已收到的内容作为普通历史上下文 +
一条明确的继续指令"，而那和现有的恢复提示只差一个附件。

## 3. 可行方案（待评估，非本轮范围）

1. **附件式续写**（最小改动）  
   截断时把 `receivedOutputPath` 作为恢复提示的一部分交给模型，让它自己决定重述还是
   接着写。不依赖任何 provider 特性，不承诺不重复。

2. **结构化分段**（改任务契约）  
   对已知会超长的输出（大批量抽取）改成模型分批提交，而不是一次写完。这是把问题从
   provider 层挪到任务层，通常是真正的解法。

3. **provider-aware prefill**（最大改动）  
   仅对确认支持的 endpoint + 关闭 thinking 的角色启用，且必须能证明断点不在
   `tool_use` JSON 中间。需要按 endpoint 建能力表。

## 4. 决策

本轮只做第 2 节的诚实命名与证据保存。方案 1–3 需要按 endpoint 实测后另行决定；
在那之前，任何文档、字段名或提示词都不得暗示 harness 能精确续写模型输出。
