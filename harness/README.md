# Harness 目录职责

按模块职责组织代码。`harness/__init__.py` 保留包级公共导出；模块之间使用实际所属子包的完整导入路径。

| 目录 | 职责 |
| --- | --- |
| `capabilities/` | 平台能力发现、方法 Schema 加载与缓存 |
| `context/` | 模型上下文压缩、大响应卸载及其模型可见投影 |
| `diagnostics/` | 错误分类、语义裁判轨迹、选择器诊断 |
| `events/` | 类型化事件、发布、记录及输出通道 |
| `evidence/` | 提取产物、字段语义与产物证据 |
| `fleet/` | 多 worker 协调、身份共享与任务复用 |
| `messages/` | 消息与内容块模型、消息转换 |
| `observation/` | 页面状态、树结构解析、滚动回执、页面会话与进度事实 |
| `planning/` | 计划校验、任务类型、模板、节奏与策略候选 |
| `prompts/` | 提示词及指南资源 |
| `results/` | worker 结果、完成回执、恢复信息与数值事实核对 |
| `runtime/` | 生命周期、模型配置、任务恢复与人工介入等待 |
| `skill/` | 技能注册、调度、创建、修复与执行 |
| `spawner/` | worker 创建、槽位管理及执行编排 |
| `storage/` | 文件与数据库存储、虚拟文件系统 |
| `task_control/` | 任务计划状态、阶段生命周期、重规划与不变量校验 |
| `tools/` | 工具定义、分派、参数处理、文件操作及调用策略 |
| `vl/` | 视觉定位、截图几何与视觉判断 |
| `workflow/` | Workflow 定义、平台合同、执行策略、身份围栏与回执投影 |

根目录保留 `constants.py`（共享常量）、`utils.py`（现有共享辅助代码）、`version.py`（源码版本信息）和 `__init__.py`。新增模块应按职责进入对应子包。

分类迁移保留原模块文件名，例如 `harness.workflow_policy` 改为 `harness.workflow.workflow_policy`；仓库内源码、测试和维护脚本同步使用新路径。外部脚本若直接导入旧模块路径，也需相应更新。
