# WebCross v0.9.6 Harness 适配记录

日期：2026-10-06（Asia/Shanghai）。修改范围是本工作区的 Harness；原有技能构建改动保留。

## 版本和证据边界

安装应用的 Info.plist、app.asar/package.json 和 CLI 均为 `0.9.6-beta`。
运行实例的目录包含 55 个 Action，新增 `DOM.getDetails`，移除 `Page.wheel`。
实际目录版本为 `sha256:f3668c2329565bf4c2d36f63e0002429722d5e7f39b6f442b9a22120c866c78b`。

工作区内独立的 `abcp-platform` 仓库包版本和 shared VERSION 是 `1.0.0`，
其源码不能与已安装二进制视为同一部署版本。本次合同以运行实例的
System.describeAction 和原始回执为准，未修改或重新部署 WebCross。

## 行为变化

- `DOM.getAXTree` 读取完整观察；平台 diff、detail 引用和版本连续编号推断已移除。
  新版本观察交付完整视图，保留现有卸载、搜索索引和本地节点状态比较。
  已交付的同一不可变版本仍可标为 unchanged；这不证明业务动作成功。
- `DOM.getDetails` 同时支持实时 query 和 `version + nodeIds` 历史读取。
  客户端统一验证并读取 artifact。历史详情不更新当前目标有效性，也不进入实时监听基线。
  实时详情只登记成功解析、绑定到正确页面/文档且未被更新事件覆盖的目标。
- 节点监听及 selector 解析改用 getDetails，显式提供 dom/text 查询的声明默认值。
  文档身份变化会结束旧监听上下文。
- 滚动调用统一使用 `Input.scroll`：target、signed delta、edge 和 state read。
  采集工具把其显式 direction/amount 转为 delta；省略 container 时选择页面视口。
  两条截图坐标读取路径使用视口 state read。
- 滚动事实保留 `input.completed`、`observation.status`、`goal.status` 和实测位移。
  `sentDelta` 不计为位移；现代回执缺少位移证据时，不沿用历史“已遍历”推断。
  截图坐标证明要求输入完成、观察稳定和实测零位移。
- Workflow 中 getDetails 返回原始 artifact/summary；通过完整 `$last` 读取其文本。
  `$cache.observation` 保留最新 full view。模型工具说明和示例同步更新。
- Schema 缓存沿用现有目录版本刷新机制，缓存了 50 个策略允许的方法；无需新增缓存框架。
  `observation_diff_view` 保留为旧配置名，目前仅控制同一已交付版本的 unchanged 简写。

校验范围是 Action 形式互斥、页面/文档/版本绑定、artifact 完整性、结果计数及坐标证明。
这些约束适用于所有站点。违反时不执行输入或将观察标为不可用，恢复方式是纠正参数、
获取匹配版本或重新观察。它们属于协议和算术事实，不能由语义判断覆盖；
是否继续、重试、结束采集等业务选择仍由模型结合原始目标判断。

## 验证

相关 16 个测试模块：**513 passed，1 skipped，154 subtests passed**。
包括观察、实时监听、历史详情、参数处理、滚动、采集、Workflow 指南、截图坐标、
任务复核和 Schema 缓存。原始 v0.9.6 合同回归见
`tests/test_webcross_v096_sync.py`；样本位于 `tests/fixtures/webcross_v096/`。

权威全套命令在具备本地 socket 权限的环境执行：

```text
/Users/versace/opt/miniconda3/envs/agent/bin/python -m pytest tests/ -q --continue-on-collection-errors --tb=short
```

结果：**3968 passed，79 failed，3 skipped，1 collection error，900 subtests passed**。
用 HEAD 源码的临时副本保留用户原有无关修改后，对失败项逐一对照：79 个失败用例在
修改前也失败，没有新增失败用例。导入错误来自原有 test_responses_regressions.py 引用
已不存在的 TaskClassifierConfig，在修改前副本也复现。此前 5 个本地 transport 用例及其
3 个子测试受 socket 沙箱限制；在允许本地 socket 的最终环境中已通过。

隔离 Fleet 的实时验证通过：完整观察、四种 live query、完整历史详情和逐节点失败，
修改后客户端的字段读取、节点监听 `idle → finished`、
Workflow `getDetails → $last → transform`、截图前后视口坐标 state read。
所有测试页面和 Fleet 已关闭。实时验证摘要位于样本目录的 native-canary-summary.json。

## 当前安装版本的滚动回执限制

直接 CLI 调用未经过 Harness。原始 samples 的调用顺序是：

1. `scroll_delta`：发送 y=300，回执报告 completed=true、stable、实测位移 0、
   goal=not-reached、no-progress；随后的 full 仍为 y=0。
2. 下一次 container 操作报告 scroll-target-not-visible；其后的 full 才显示根视口 y=300。
3. `scroll_edge`：回执仍报告零位移、not-reached；随后 target 操作报告总位移 -1488，
   最终根视口恢复 y=0。

这证明当前安装实例的动作回执与后续观察存在时序不一致，具体浏览器执行根因尚未确认。
完整请求、回执及后续 full artifact 保留在 samples 中。Harness 未通过重试、提示词或
Runtime.evaluate 掩盖该现象，也未把它固化为某站点的重试门禁。

本次验证属于合同迁移和正确性检查，未运行可比的模型业务任务，不据此推断 token 或耗时收益。
