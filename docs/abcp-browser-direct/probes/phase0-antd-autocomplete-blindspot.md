# ABCP 1.1.9：结构化表面盲区 canary（原 AntD AutoComplete Phase 0）

> **状态（2026-09-01）**：本文记录的**测量结论有效并继续使用**；它当时驱动的那套
> 实现已被删除。
>
> 当时的产物是三个下拉框专用模块（`harness/vl/select_episode.py` /
> `select_coordinate.py` / `select_recovery.py`）加一条挂在 browser_call 热路径上的
> 影子观察+受限执行 lane。那条路线因为**专用性和机械性**被回撤：它只认 `Input.select`
> 失败、只认 ARIA `aria-controls/aria-owns` 关系、要求唯一 optionLabel 和 canonical
> control id，还要一份 loopback fixture grant 才肯动——真实业务页面永远进不去，而
> 每加一种前端框架就要再写一个 episode 绑定器。
>
> 取代它的是通用路径：`visual_verify` 的 `mode=visual_locate`（VL 定位 → 几何证明 →
> canonical id 或 viewport CSS 坐标），由 BrowserAgent 在确定性恢复失败后**自主调用**，
> 失败回执上的 `visualRecoveryHint` 负责告诉它这条路存在。详见
> `docs/end-to-end-request-flow.md` Q20。
>
> **本文保留的价值**：下面测到的盲区形态——SemanticTree 不投影弹层、AXTree 里同时
> 存在无面积的隐藏 mirror 与真正渲染的行——不是 AntD 独有的，通用方案照样会撞上。
> 它现在的角色是**通用视觉恢复的 canary**：一个已知会让结构化表面失效的真实页面，
> 用来验证 `visual_locate` 在这类页面上是给出 resolvedId、给出 cssPoint、还是诚实地
> `coordinateRefused`。

## 目的与范围

本记录只验证视觉恢复方案的**可行性前置条件**，不实现自动视觉点击，不登录或访问 c6675 的真实自动保存表单，也不授权重放 `Input.select`。

测试对象是 [Ant Design 官方 AutoComplete 示例](https://ant.design/components/auto-complete/)，目标选项为 `Burns Bay Road`。探针位于 `probe_tests/probe_antd_blindspot.py`；它使用隔离 Fleet，每轮结束都关闭 Fleet。

运行命令：

```bash
/Users/versace/opt/miniconda3/envs/agent/bin/python \
  probe_tests/probe_antd_blindspot.py --runs 3 \
  --json /private/tmp/antd_phase0_three.json
```

## 运行态基线

所有已完成运行都确认以下当前运行态：

| 项 | 值 |
| --- | --- |
| catalogRevision | `sha256:cfd8fb90a7d277b16cad96ea1f17db4c9f7bafbbb87796e52dd73fdf68732998` |
| guideRevision | `sha256:d22b31169416a6f4bf7b4b40ae234b1bf63c9f7fb6e1d88032f0ca9a591abbc5` |
| action count | 62 |

## 历史 c6675 证据的边界

历史落盘任务的 catalog revision 为 `sha256:d906…`，不能代替当前 1.1.9 的行为证据。它确实记录过可见的 AntD 弹层、语义树 popup 节点 `children: []`，但同一历史过程的 AXTree 中存在 option/listbox 项。因此不能将它概括为“AXTree、语义树和 inspectSelect 三者同时不可见”。

本 Phase 0 不访问该站点：它需要登录，且真实表单会自动保存。

## 实测结果

三轮基线和修正 stale-ID 处理后的单轮复核得出同一关键结论。

| 问题 | 当前结论 | 证据 |
| --- | --- | --- |
| 语义树是否完整投影弹层 option | 否 | `semantic_popup_not_projected_axtree_options_present`：SemanticTree 未投影 popup，AXTree 同时有 11 个 option。 |
| `DOM.inspectSelect` 错误码 | 当前复核为 `select-popup-not-found` | 三轮基线另有一次 `stale-target`，发生在回收 ID 修正前，不作为平台分类结论。 |
| `Input.select` 错误码 | `invalid-params` | 官方 AutoComplete 不是原生 select；这不能推导为该 Action 对所有下拉框失效。 |
| 能否以 AX option 的 canonical ID 截取 popup | 未证明，当前失败 | `Page.screenshot` 返回 `target-not-found`。 |
| 能否以 AX option 的 canonical ID 原生点击 | 未证明，当前失败 | `Input.click` 返回 `scroll-no-progress`。 |
| 能否用原生读法确认选中 | 未证明 | 因 option click 未成功，无法建立一次成功动作后的 value / AXTree 后置条件。 |

修正 stale-ID 后的复核输出保存于本机 `/private/tmp/antd_phase0_post_recovery.json`。其 Phase 0 摘要为：

```json
{
  "projectionDefectStillPresent": {
    "semantic_popup_not_projected_axtree_options_present": 1
  },
  "geometryFeasibility": null,
  "nativePostconditionFeasibility": null,
  "intentBindingImplemented": false,
  "automaticExecutionAuthorized": false
}
```

`null` 的含义是“没有取得证明”，不是“安全地证明为 false”。最初探针止于原生句柄到截图/动作入口无法接通之处；后续 viewport/region 扩展的结果见下一节。

## 方案 2 的补充探针（viewport / region）

为验证不消费 AX option ID 的路线，探针已改为：SemanticTree 提供真实可见 popup bounds 时截取 region，否则截取 viewport；它绝不将 AX listbox / option ID 传给 `Page.screenshot`。截图回执在当前运行态已证明 `captureScale=2.0`、viewport origin 为 `(0, 0)`，因此**坐标换算链本身可用**。

但当前官方页面（页面标示版本为 6.6.2）不能作为该闭环的通过 canary：对 Basic Usage 输入框真实输入 `b` 后，截图中只出现输入值，未出现任何绘制的候选列表；VL 因而正确返回 `not_found`，没有产生坐标，也没有执行坐标点击。此结果不能证明方案 2 失败，只能说明当前公开页面的该状态不满足“实际可见候选项”的测试前提。

因此官方页面本身仍未取得以下两项证据：

1. VL 对真实渲染 option 行的精确定位；
2. 一次坐标点击后的原生选中后置验证。

## 锁版本本地虚拟列表 fixture（Phase 0 完成）

为避免官网示例随版本和数据源变化，已创建隔离 fixture：
`probe_tests/antd-virtual-select/`。它锁定 `antd@5.24.8`、
`react@18.3.1`、`react-dom@18.3.1` 和 `vite@6.0.11`，并以
`package-lock.json` 固定依赖树；它不访问真实业务站点，也不持久化数据。

该 fixture 使用 AntD `Select` 的真实 `virtual` 路径，包含零尺寸 ARIA
listbox 镜像与实际绘制的 `rc-virtual-list` 行。为了只验证视觉阶段，弹层由
受控状态保持展开；它**不**验证 BrowserAgent 如何打开下拉框、portal/teleport
归属或生产意图绑定。

最终稳定性门要求 popup 连续两次的 bounds 一致、且不含 AntD
`appear` / `enter` / `leave` 过渡类；达不到时探针拒绝输出坐标。避免了早期
探针又点击已展开 trigger、在退场动画中截图而误选 `Beirut 5` 的假阳性。

在该门之后，三轮独立 Fleet 的完整运行均通过：

1. 普通 fixture region 的 element 截图回执中可找到实际
   `.ant-select-dropdown` 的 `visibleBounds`；
2. 用该 bounds 进行第二次 `region` 截图成功，CSS 区域为
   `(344, 281, 577, 200)`，落盘 PNG 为 `1154×400`，故
   `captureScale=2.0`；
3. 三轮 VL 均将 `北京` 定位为归一化点：`(500,83)`、`(500,90)`、
   `(500,100)`；
4. 每轮只执行一次 CSS coordinate click；每轮 fixture 原生
   `<output data-testid="selected-value">` 均返回 `当前选择：北京`；
5. 每轮 control 的原生 `aria-expanded` 均为 `false`。AntD 的 leave 动画节点
   仍可能短暂留在 SemanticTree 中，因此该节点移除只记为诊断，不能覆盖控件的
   原生关闭语义。

受控、可搜索的 AntD Select 仍可能让 inner search input 的 `value` 保持为空；
它不是可靠的选中后置条件。探针因此使用 fixture 明确暴露的原生 `<output>`。
生产场景必须在候选生成前绑定与目标动作等价的原生后置条件，不能照搬该 fixture
的 `data-testid`。

当前正确状态是：

| 条件 | 状态 |
| --- | --- |
| 真实虚拟行的 region 截图与 VL 定位 | 连续 3 次通过 |
| 坐标换算产生 CSS 点击点 | 连续 3 次通过 |
| 点击实际选中了 `北京` | 连续 3 次通过（fixture 原生 output） |
| 可读的原生后置条件 | 连续 3 次通过 |
| 自动执行授权 / 生产状态机 | 未授权 |

三轮中 `DOM.inspectSelect` 均返回 `select-popup-not-found`，`Input.select` 均为
`invalid-params`，而 AX option ID 的原生点击有三次 `scroll-no-progress`；这再次
证明不能把虚拟列表的 ARIA mirror option ID 作为视觉目标。该 fixture 的
SemanticTree 同时会投影部分实际列表子节点（分类为 `options_projected`），所以它
**没有复现** c6675 的 `children: []` 投影缺陷；探针强制走 coordinate 分支，以验证
方案 2 的独立可行性。

此外，该次调用未携带截图时的根滚动量，现有 `promote_locate` 因此正确不给 AXTree
promotion（`scroll_unprovable`），只给可实验的 CSS 坐标。生产路径必须在同一
快照窗口取得 scroll 并通过稳定性与后置条件门，不能以此探针点击替代这些门。

## 机械 bbox → 坐标派发 canary（2026-08-31）

在构建完整视觉恢复状态机前，新增了一个更窄的反证/可行性 probe：
`probe_tests/probe_select_bbox_coordinate.py`。它不调用 VL、不截图、也绝不传
option ID 或 selector 给 `Input.click`。它只回答“可绘制的结构化目标能否走另一条
坐标派发路径”：

```text
S0: fresh SemanticTree + AXTree
    → 根树比例证明（AX root 2560×1600 / Semantic root 1280×800 = 2.0）
    → 仅收集 popup 内、直接命名为“北京”、正面积的矩形
    → 同层 wrapper/inner wrapper 的重叠矩形合成唯一交集
S1: 重新读取，control / popup / scroll / scale / 候选簇 / 点位完全相同
    → 仅一次 Input.click{x, y}
    → 原生 fixture output 为“当前选择：北京”且 aria-expanded=false
```

结果为 **3/3 独立 Fleet 通过**。每轮在 `captureScale=2.0`、根 scroll `(0,0)` 下，
从两个 AXTree 正面积 `genericcontainer`（`3:54:54` 的 `568×32` CSS 框与
`3:55:55` 的内层 `544×22` CSS 框）及对应语义节点形成唯一交集，得到 CSS 点击点
`(632.5, 301.0)`。一次 `Input.click{x,y}` 后，原生 `DOM.getText` 回执均为
`当前选择：北京`，并且控制项的 `aria-expanded=false`。

首次冷启动时曾遇到 AntD 的初始 enter 动画，稳定门正确拒绝、没有点击。probe 现在
只额外做最多 30 次、每次 200ms 的**观察**等待；没有增加第二次点击、按键或 select。
这不是把不稳定情况放行：若观察窗口仍得不到两个相同快照，就以非零退出拒绝。

运行方式（先构建，再以静态 server 提供；本机 Vite dev server 在这次环境中会监听
但不响应 HTTP，不能作为可靠 probe 前提）：

```bash
cd probe_tests/antd-virtual-select
npm run build
python3 -m http.server 4173 --bind 0.0.0.0 -d dist

# 另一个终端
python3 probe_tests/probe_select_bbox_coordinate.py --runs 3 \
  --json /private/tmp/antd_virtual_select_bbox_coordinate.json
```

该结论严格受限于此 fixture：它证明了“ID/selector 派发失败时，**已证明的**可绘制
矩形可以改由坐标派发”的候选路径，因而值得作为生产 resolver 之前的低成本机械分支。
它不证明 c6675 上的正面积节点一定可点，也不证明 VL 会因此无用；对 c6675 的真实
盲区、portal/teleport、非零根/嵌套 scroll、fixed/sticky、iframe、动画期间位移、目标
意图来源及生产后置条件仍没有通过该 canary 验证。任何一个候选簇不唯一、比例/scroll
不可证明或 S0/S1 不一致时，生产设计都必须拒绝而不是猜测。

## Portal 对照（当前 1.1.9，2026-08-31）

fixture 现支持 `?topology=portal`：不传 `getPopupContainer`，让 rc-select 使用默认的
`document.body` portal。其目的不是伪造旧的语义树结果，而是验证“portal 本身”是否足以
复现 c6675 历史记录的 `popup.children: []`。

结果是否定的：当前 catalog `cfd8…` 的三轮对照中，popup 的直接 child 数始终为 `1`
（不是 `0`），并且树中有两个直接标为“北京”、带正面积的语义节点；AXTree 也有两个
对应的正面积 `genericcontainer`。所以**仅 AntD portal + virtual list 不能复现历史投影
缺口**。历史 c6675 的 `d906…` catalog 与业务定制 class 仍是不同变量，不能把旧记录
直接提升为当前 1.1.9 的缺陷。

三轮机械 canary 的结果是 2 次完成选择并通过原生后置条件，1 次在点击前拒绝：S0 的
control ID 是 `3:0:33`，S1 刷新为 `3:33:33`，其余 popup、候选簇和点位未变。由于
fingerprint 把 control ID 纳入稳定性条件，probe 返回非零、没有坐标点击。这个结果不应
“重跑直到 3/3 绿”来掩盖；它证明 canonical ID 替换是实际会发生的竞态，也验证了
S0/S1 门能在副作用之前阻止陈旧候选。

因此当前的正确动作不是构造一个靠 CSS/测试代码强行让 SemanticTree 省略 children 的
假 fixture。只有获得当前 1.1.9 的自然复现（安全的第三方示例或可验证的平台 bug）时，
才值得把它作为视觉 resolver 的放行 canary；否则该缺口只保留为旧版本/业务定制的历史
风险记录。

## 工程结论

方案 2 的**局部技术可行性**已通过：在稳定 popup 上，VL 可以定位、坐标可以选中，且
原生后置条件能确认结果；机械 bbox 坐标 canary 还表明一部分结构化可绘制目标不需要
VL 即可走坐标派发。但当前仍不得开启自动坐标视觉恢复或实现生产状态机：
fixture 使用人工绑定的目标和专用后置条件，尚未证明生产 intent provenance、授权
策略、候选有效期、页面变动失效、跨框架拓扑或 c6675 的 `children: []` 盲区。

因此保持 `visual_locate_enabled` 与 `arbiter_enabled` 关闭；不得把一次“点击成功”的
回执视为选择成功，也不允许自动重放 `Input.select`。

## 已落地：结构化机械分支的影子观测（默认关闭）

在不改变上述生产限制的前提下，harness 现有一个仅供测量的前置分支。开关为
`vl.select_coordinate_shadow_enabled`，默认 `false`，且还必须保持 `vl.enabled=true` 才会
运行。它只在已经执行且失败的 `Input.select` 上工作，要求：

1. 原请求提供 canonical control ID 与唯一 `optionLabels` 项；标签在这里仅是观测查询，
   不是自动操作的意图或授权来源。
2. 新鲜 SemanticTree 中，原 control 或其唯一后代存在 `aria-expanded=true`，并且
   `aria-controls` / `aria-owns` 一致地指向唯一 DOM referent；由该 referent 最近的正面积
   可见祖先确定 popup。不会使用 CSS class、文本邻近或 portal 位置猜归属。
3. 连续两组 `SemanticTree → AXTree` 快照分别产出同一候选。候选必须是同一个 canonical
   ID 在两个树面都有正面积，且文档 AX bbox 经 scroll/scale 换算后与语义树 CSS 矩形一致。
   隐藏 ARIA mirror、iframe、非唯一候选、不可证明 scale/scroll、坐标族不一致及 S0/S1
   改变都会拒绝。

该路径不截图、不调用 VL、不点击、不重放 `Input.select`，也不声称找到了选择后的验证面。
它与已有 `max_checks_per_worker` 共用 `vl_arbiter_count` 配额，不创建独立重试计数器。模型
新增的 `selectCoordinateShadow` 回执最多包含 `status/reason/candidateCount`，不包含新发现的
CSS 坐标、candidate canonical ID、popup ID 或 action recommendation（原始工具结果中模型自己
传入的 control ID 不受此处改变），且固定
`automaticExecutionAuthorized=false`。

这不是 production action lane。要升级为单次坐标动作仍须另行实现并验证：来自 worker
contract 的授权、与页面内容隔离的 intent provenance、针对具体业务控件的已注册原生
postcondition、候选有效期及动作后的新鲜验证。对于 c6675 历史的 `children: []`，这个机械
分支会正确拒绝；它仍属于未来的视觉 resolver canary，而不是用同名文本把 mirror 映射到
渲染行。

## 已落地：受限的本地 fixture 单次执行器（默认关闭）

为验证“影子候选能否安全地接到真实 Input 行为”，harness 还实现了一个**非 production**
执行器。`vl.select_coordinate_execution_enabled` 默认 `false`；即使显式开启，它也只接受
worker contract 中完整的 `select_coordinate_recovery.grants[]` 条目，且当前只支持：

```json
{
  "id": "local-antd-city-beijing",
  "risk": "test_fixture",
  "page_url_prefix": "http://127.0.0.1:4173/",
  "control_element_id": "city-select",
  "option_label": "北京",
  "verifier": {
    "kind": "dom_text_equals",
    "selector": "#selected-value",
    "expected": "当前选择：北京"
  }
}
```

实现会重新读取 `Page.getState`，要求当前 URL 仍是 loopback fixture；先由 `DOM.getText`
证明 expected text **尚未**出现，随后走两组结构候选快照，最后至多一次
`Input.click{x,y}`。无论点击回执如何，都会立即再次 `DOM.getText`：只有精确匹配 expected
text 才返回 `verified`；任何不匹配、读取失败或竞态均为 `outcome_unknown` / `refused`，带
`replayForbidden=true`，绝不追加一次点击或重放 `Input.select`。

坐标、popup/candidate ID、grant 内容和原生文本回执均不进入模型结果。`test_fixture` 以外的
grant（包括自动保存的 c6675 页面和公网 URL）在读页面前即拒绝；它们仍可使用默认关闭的
影子观测统计，但不能执行坐标动作。生产放量仍需另行定义非测试风险模型、用户意图
provenance 和具体业务 verifier，不能仅把此 fixture 机制改一个开关。

## 本地 guarded-action E2E（2026-08-31）

使用锁版本、本机 loopback AntD virtual Select fixture 完成了一次真实 ABCP action-lane
验证。默认配置没有改变；探针在内存中才同时开启 VL 与执行开关，并注入唯一的
`test_fixture` grant。流程和结果如下：

1. `System.register`、`Fleet.create` 成功；测试结束时 `Fleet.close=true`。
2. 对已经展开的 control 发起一次原生 `Input.select`，ABCP 真实返回
   `invalid-params`。该失败经 WebSocket JSON-RPC 进入 harness 后的形态为顶层
   `error` 加公开 `rpcData.error`，而非 `response.error`。
3. 发现并修正执行器及 shadow observer 只认 `response.error` 的入口缺口；现在二者只要
   发现公开的 action `error`（`response.error`、`response.data.error` 或
   `rpcData.error`）才继续。裸连接/本地错误没有该公开对象，仍不会触发操作。
4. 修复 fixture verifier 声明与 DOM 的不一致：grant 使用的 `#selected-value` 现在有同名
   stable HTML id。此前的 `target-not-found` 是 fixture 本身的目标不存在，不是 ABCP
   selector 契约失败。
5. 修复后内层调用顺序严格为：`Page.getState` → pre `DOM.getText` →
   `DOM.getSemanticTree`/`DOM.getAXTree` ×2 → 一次 `Input.click{x,y}` → post
   `DOM.getText`。只有这一处 `Input.click`；最终 receipt 为 `verified`，后置文本精确为
   `当前选择：北京`。

探针：`probe_tests/probe_select_coordinate_execution_entry.py`。它把 audit JSON 写到调用方
指定的临时路径，且刻意不输出 page/canonical IDs、坐标或 grant 内容。它是 local fixture
的 action-lane 验证，不等价于对真实业务表单的放量授权。

## 后续依赖与建议

下一项有价值的工作分为两个独立方向：

1. 向 ABCP 平台确认或修复 popup option 的身份映射：为什么 `DOM.getAXTree` 给出的当前 option ID 会在 `Page.screenshot` 变成 `target-not-found`、在 `Input.click` 变成 `scroll-no-progress`。这阻塞 ID 路径，但不阻塞 viewport/region 的视觉路线。
2. 取得**当前 1.1.9 下自然出现** `children: []` 的安全等价页面，或先由平台确认该
   投影缺口仍存在；再运行相同的稳定性、坐标和原生后置条件门。不得通过 CSS/fixture
   代码伪造 SemanticTree 输出，把历史缺陷冒充为当前复现。

只有第二项 canary 稳定通过、并完成生产 intent/provenance 与授权设计后，才重新批准
生产状态机和 BrowserAgent 自动触发。

## 本地交付状态

本文档与探针均只保存在工作区，未执行 `git add`、提交或推送。
