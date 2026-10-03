# Workflow 页面引用修复与 occluded 现场验证

关联 Harness 任务：`84edb1bc3e3c440caca4b83bec2d29fc`。验证日期：2026-10-01。

## 结论

1. Harness 的 `$vars.pageId` 误拦截已修复，回归测试与实际 WebCross 调用均通过。
2. 原 `occluded` 可以复现；直接调用 WebCross 也会发生，与 Harness 无关。
3. 当前页面确实由“本科”展示项覆盖内部搜索输入框。外层控件点击正常，现有证据不支持认定 Native 点击发生遮挡误判。
4. 可交给 WebCross 开发人员评估的是点击失败诊断与 Workflow 嵌套错误信息的完整性，不应默认增加强制点击、自动重试或站点特判。

## Harness 修复范围

修改 `harness/fleet/runtime.py` 原有页面归属检查：将执行前可确定的初始变量引用和 `$context.pageId` 解析为页面 ID，再应用现有页面库存、Fleet 与 worker 归属检查。支持嵌套对象、数组路径、精确的带点变量名，以及内部参数和平台文档两种形态；请求中的引用原样传给 WebCross，不改写 Workflow。

没有盲目忽略所有 `$` 字符串。指向其他 worker、其他 Fleet 或未知页面的引用仍被拒绝。可能被 `extract` / `transform` 覆写的变量，以及依赖执行结果的动态引用，不被当作已确定的初始值；保持原有边界处理。本次没有在 Harness 内实现 Workflow 执行器或扩大动态页面支持范围。

验证：

- `tests/test_fleet_runtime.py`、`tests/test_workflow_wire_validation.py`、`tests/test_workflow_nested_failure.py`：**81 passed，21 subtests passed**。
- 修复后的实际 `PageLeasedBrowserClient → ABCPClient → WebCross` 调用，内部 `Page.getState.params.pageId` 保留 `$vars.pageId`，成功读取目标页面。Workflow ID：`3a7421c1-e852-4e9e-9a08-a0f62c135e32`。
- 现有进程不会因修改源码自动重新导入；上述现场验证使用新 Python 进程。后续真实任务测试前应重启当前 Harness 进程。

## WebCross 运行现场与版本

- URL：`https://www.yue-accelerator.com/#/sign-up`。
- Fleet：`fa4438cc-3b74-49a1-a64e-70fdbd0bbe18`。
- Page：`e6ee8622-7de1-47a8-a78c-6590ebf626b9`。
- Dispatcher instance：`2b256656-1211-4543-8534-f8b9c09553f9`，与原任务相同。
- documentEpoch：`d_5ef1be7253f4f642c078079fcf336b67`，与原任务相同；重新打开编辑后使用新观察中的节点，未重放原任务的过期节点。
- 运行进程的可执行文件来自 `/Applications/WebCross.app`。安装包版本 `0.9.38-beta`，build ID `wc-95aab2c12d01-22b426263ccbee0dc06732b1`，source revision `95aab2c12d012624dc8d58b2942acd3cd09d6d2a`。
- 本地 WebCross 源码 HEAD 为 `6924023865e5a1a131ce110bb08974198dbc678d`，与安装包 revision 不同。没有重建或替换 WebCross；结论依据运行实例的回执。安装包元数据不是独立的 Dispatcher/Native 构建证明。

## 点击对照

所有点击均为 `force:false`、`clickCount:1`，通过 WebCross CLI 直接执行，不经过 Harness。打开现有教育经历编辑区域后，学历已为本科，学校已为广东工业大学。

| 测试 | 目标与步骤 | 实际结果 |
| --- | --- | --- |
| 直接点击内部输入框 | `n_f9673352ff241ef4` | `occluded` |
| 最小 Workflow | `Page.getState` → 点击同一内部输入框 | 状态读取成功，`steps[1]` 返回 `occluded`；总执行 441ms |
| 原两步顺序 | 点击学校当前的广东工业大学选项 → 点击同一内部输入框 | 第一点击成功，`steps[1]` 返回 `occluded`；总执行 973ms |
| 直接点击学历外层 | `n_8fb11849259661a0` | 成功 |
| 外层 Workflow 对照 | 点击学历外层 → 查询内部输入框状态 | 两步成功，`expanded:true`；总执行 1119ms |

原两步复现 Workflow ID：`ccc43467-5e41-4083-83d2-0922fe293dbf`。
外层对照 Workflow ID：`979c8a9d-929b-48a4-b204-b55ef7775526`。

上述次数是有目的的对照测试，不是成功率统计。稳定页面上的单独点击也失败，因此无需依赖学校点击后的短暂过渡，便可触发同类失败。

## 覆盖事实

结构化 DOM 查询确认以下三个节点属于同一个控件：

- 外层：`DIV.ant-select-selector`，文本“本科”，边界 `(57,370.4453,155.25,36)`。
- 内部输入框：`INPUT#rc_select_7.ant-select-selection-search-input`，value 为空，边界 `(69,371.4453,117.25,30)`。
- 选中项：`SPAN.ant-select-selection-item[title="本科"]`，边界 `(69,371.4453,131.25,34)`。

结构化接口没有计算样式与实时鼠标命中节点，因此另外进行了只读 `Runtime.evaluate` 诊断：仅调用 `getComputedStyle`、`getBoundingClientRect`、`elementsFromPoint`，没有点击、聚焦、滚动、DOM 修改或业务数据修改。

输入框中部横向 25%、50%、75% 三个采样点的最上层节点均为上述选中项，而不是输入框。选中项的 `pointer-events:auto`、`opacity:1`、`visibility:visible`；它的几何范围覆盖输入框。最上层节点属于外层控件，符合外层点击成功的结果。

这些采样不是 Native 内部实际点击点的日志，但结合三次内部点击失败与两次外层点击成功，支持“内部目标确实被控件展示项覆盖”，没有发现 Native 遮挡误判的反证。`targetable`、`actionable`、combobox 身份或可聚焦，不等于适合鼠标点击。

## 建议 WebCross 开发人员评估

这不是要求让点击自动穿透覆盖项，也不是要求按站点重定向目标。建议检查两条通用诊断链：

1. 直接 `Input.click` 失败能否返回实际命中点及覆盖元素的可核验身份。本次只有泛化的 `occluded` 文案，不能直接说明是控件内部覆盖。
2. Workflow 的失败 step 能否保留嵌套 Action 的公共错误消息、观察与建议。本次保留了失败位置、错误码和前一步结果，但失败 step 本身只有 `errorCode:occluded` 与时长，缺少嵌套 Action 的详细反馈。

Harness 在原任务中已将前一步结果和失败轨迹交给模型，未发现它丢弃部分执行结果。模型当时把内部搜索输入框的空值误解为学历未选择，才重新点击；控件自身已有本科选中项。这部分属于模型对控件状态的判断问题。

## 最小复现与资料

重新打开页面编辑区域并观察当前节点后，直接执行以下动作即可复现（`id` 必须替换为新观察中的内部输入框 ID）：

```json
{
  "pageId": "e6ee8622-7de1-47a8-a78c-6590ebf626b9",
  "id": "n_f9673352ff241ef4",
  "force": false,
  "clickCount": 1,
  "purpose": "Reproduce clicking the covered internal degree input"
}
```

对照只需将 `id` 替换为学历外层 `ant-select-selector` 的当前节点 ID。

原始请求、响应、控件局部观察、只读命中诊断与局部截图打包于 `/private/tmp/webcross-occluded-2026-10-01.zip`。资料未包含全页观察、登录凭据或无关表单内容。临时目录可能被系统清理，转交时应保存此压缩包。

测试结束后已关闭下拉框、取消编辑。当前教育经历卡片重新显示“广东工业大学 / 本科 / 经管学院·应用统计学 / 编辑”，与测试前一致；没有保存测试编辑或提交报名。
