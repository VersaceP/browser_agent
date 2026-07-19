# 路由敏感型内容抑制：识别、点击穿透与页面恢复方案

状态：核心实现、离线回归及单 Fleet 淘宝导航恢复验证完成；评论接口的隐蔽跨 frame 验证已接入自动 HITL 识别  
更新时间：2026-07-19  
适用范围：BrowserAgent、LeadAgent、ABCP browser tools、strategy bank、artifact/terminal validators

## 1. 结论

当前问题不应继续描述为“评论不在无障碍 DOM 渲染”，也不应直接归类成 CAPTCHA、页面验证或 `target_absent`。

已有证据支持的准确结论是：目标站点可能根据**导航来源或访问路径**返回不同完整度的详情内容。直接导航到详情 URL 时，页面外壳可以成功加载，但预期详情子树被服务端或页面状态抑制；从搜索/列表页点击商品链接进入时，同一类详情页可以获得完整 DOM。该现象在本文中统一称为：

> 路由敏感型内容抑制（route-sensitive content suppression）

Harness 应做三件事：

1. 在“页面已加载但任务相关内容整体缺失”时识别内容不完整，禁止过早铸成 `target_absent`。
2. 保留列表页上下文，通过真实 `<a>` 卡片串行点击进入详情页，并在每次点击后用现有 Page/DOM 原子工具判断导航结果。
3. 按导航结果恢复列表页：新标签使用 `Page.switchTo`，同页跳转使用最新版 ABCP 的 `Page.go`。

本方案不新增 `open_cards_verified` 一类复合工具，也不新增独立 strategy bank 条目；恢复流程合并到现有 `web_scrape.detail_sections.reveal_then_text`。

## 2. 已确认事实与证据边界

### 2.1 已确认事实

- 原任务 `browser-002` 确实提出过 `DOM.getSemanticTree`，但被 `no_artifact_progress` 门禁拦截，结果包含 `tool_was_executed:false`。因此 ABCP 没有收到 RPC，工作区没有对应 SemanticTree 落盘文件。
- 同一门禁还阻止了 `Page.screenshot`、`visual_verify`、`Page.reload` 和 `Page.create`，说明这不是 SemanticTree 自身的执行故障，而是进度策略覆盖不完整。
- 已渲染样本 `worktree/05abac8c1ef34215a8e48a984f7caa0b/trees_probe_RENDERED/semantictree_outline.txt` 中，评价区包含：评价总数、印象标签、两张评论卡、用户名、日期、SKU、正文和“查看全部评价”。
- 对该页面，AXTree 可以用于定位评价标签和交互控件，但评论正文可能只出现在 SemanticTree。因此“AXTree 没有正文”不能推出“DOM 没有正文”。
- 点击“查看全部评价”后，评论抽屉可以继续通过 DOM/SemanticTree 读取；该交互路径是可行的。
- 实际测试表明：直接输入详情 URL 时详情 DOM 缺失，而从淘宝搜索页点击进入时详情 DOM 完整。
- 最新 ABCP 的实时 capability surface 包含 `Page.go`，用于按 `back`/`forward`、步数、域名锚点或 history tag 移动浏览历史。

### 2.2 不应固化为平台事实的推断

- 当前证据能确认“导航路径相关”，不能把所有站点的同类现象硬编码成“账号级风控”。账号、Cookie、IP、指纹和服务端会话可能是站点内部判定输入，但通用 Harness 不应依赖某一个根因假设。
- `pcResistDetail=true` 可作为站点适配器中的强抑制信号；`pcIdentityRisk=true` 只能作为辅助风险信号。核心门控不得硬编码这两个字段，也不得把任一字段单独当成跨站点结论。
- `document.hasFocus()`、`visibilityState`、`document.hidden` 等页面侧值不能可靠区分该问题，不进入核心判据。
- “页面 URL、标题和顶层外壳正常”只证明导航到达，不证明任务所需内容完整。

## 3. 设计边界

### 3.1 本次纳入

- 通用内容完整性门控与导航来源记录。
- 路由敏感型内容抑制的可恢复分类。
- `target_absent` 终态前的内容完整性否决。
- 列表页真实链接的串行点击恢复。
- 新标签与同页跳转的返回分支。
- `Page.go` 在 AXTree、render recovery、workflow lifecycle、page fingerprint 和 prompt 中的完整接入。
- 修复诊断/恢复工具被 `no_artifact_progress` 错误拦截的问题。
- 扩展现有 strategy bank 规则，不新增重复策略。
- 基于已保存 trace、SemanticTree 和 mock ABCP 事件的回归测试。

### 3.2 本次不纳入

- 不新增 `open_cards_verified`、`click_card_open_detail` 等复合浏览器工具。
- 不并行点击多个卡片，不在尚未收到上一次导航反馈时预先生成下一次点击。
- 不把内容抑制交给 CAPTCHA/HITL 流程；人工通常无法在当前页面上“解除”缺失的服务端内容。
- 不将 `Runtime.evaluate` 作为默认恢复路径，也不以 JS 伪造 referrer/history。
- 不为淘宝字段在核心 Python 代码里写站点特判。
- 不把“评论可空”作为绕过必填目标的成功条件。

## 4. 通用内容完整性门控

新增独立的 `ContentCompletenessTracker`。它与 `ChallengeTracker` 并列，而不是成为 CAPTCHA suspicion score 的一种信号。

### 4.1 输入证据

Tracker 按 `pageId + navigation epoch` 保存以下证据：

```json
{
  "pageId": "...",
  "url": "...",
  "navigation": {
    "kind": "direct|link_click|same_page_history|redirect|unknown",
    "sourcePageId": "...",
    "sourceUrl": "...",
    "targetUrl": "...",
    "itemIdentity": "..."
  },
  "pageShell": {
    "present": true,
    "markers": ["title", "price", "shop"]
  },
  "expectedRegions": ["reviews", "specs", "description"],
  "observedRegions": ["reviews_tab"],
  "materializationAttempts": ["scroll", "click_tab", "semantic_tree"],
  "siteSignals": [
    {"name": "adapter_signal_name", "value": true, "strength": "confirmatory"}
  ]
}
```

证据来源分为三层：

1. 自动来源：`Page.navigate`、`Page.go`、`Page.list`、`Page.getState`、`Page.open`、`Input.click`、页面指纹和 DOM 调用结果。
2. Worker contract/skill 声明：任务需要的区域、页面外壳标记、站点可选信号以及可用列表页来源。
3. LLM 语义判断：在通用标记不足时，依据 AXTree/SemanticTree 判断“外壳存在但多个任务相关区域整体缺失”。该判断只能触发恢复，不能单独生成终态。

### 4.2 判定状态

门控输出四种状态：

| 状态 | 含义 | 允许动作 |
|---|---|---|
| `complete` | 所需区域已出现，或数据已成功抽取 | 正常抽取/完成 |
| `inconclusive` | 内容可能仍在懒加载、标签页、抽屉或 iframe 中 | 继续有限 materialization 探测 |
| `route_recovery_required` | 外壳存在、多个所需区域缺失，且存在更可信的点击来源 | 返回列表页并点击进入 |
| `blocked_content_suppression` | 已按预算执行点击穿透，仍无法取得必需内容 | 以 blocker 结束，不得写成 `target_absent` |

### 4.3 通用判定规则

以下规则按顺序执行：

1. 单个 selector、AXTree 或 SemanticTree 返回空，状态只能是 `inconclusive`。
2. 先执行有限的本页 materialization：刷新状态和树、滚动到相关区域、点击已识别的标签/展开控件、必要时读取 SemanticTree。没有确认性 suppression signal 时，至少需要一次 `DOM.getSemanticTree`，以及一次 `Input.scroll` 或目标区域 reveal click，才允许从 `inconclusive` 升级为路线恢复；确认性信号可直接进入路线恢复。
3. 页面外壳存在，但两个或以上任务相关区域共同缺失，且当前导航是 `direct` 或来源未知时，可以建议 `route_recovery_required`。
4. 如果站点适配器同时给出确认型抑制信号，可直接把第 3 步从“建议”升级为“确认需要恢复”；仍不触发 HITL。
5. 如果页面通过列表点击进入后目标区域出现，则标记此次导航来源有效，并继续抽取。
6. 如果点击穿透达到有界预算仍失败，分类为 `blocked_content_suppression`。
7. `route_recovery_required` 一律 veto `target_absent` / `instruction_infeasible`；`inconclusive` 仅在页面外壳存在、目标区域缺失且有界 materialization 尚未完成时临时 veto。无外壳或预算已完成时，交还 navigation/auth/challenge/既有终态分类。

缺失页面外壳、网络错误、登录页、验证码页和浏览器崩溃仍由已有基础设施/认证/challenge 分类处理，不由此门控吞并。

### 4.4 站点适配声明

核心代码只理解声明式证据，不理解淘宝字段。可在 skill 或 worker contract 中增加可选结构：

```json
{
  "content_completeness": {
    "shell_markers": ["product_title", "price"],
    "expected_regions": ["reviews", "specifications", "description"],
    "suppression_signals": [
      {
        "name": "detail_suppressed",
        "source": "inline_script",
        "locator": "site-owned locator",
        "match": true,
        "strength": "confirmatory"
      }
    ],
    "recovery": {
      "mode": "listing_link_click",
      "max_attempts_per_item": 2
    }
  }
}
```

站点适配器如果需要读取页面内联脚本，应读取 `script.textContent` 后解析；不能假设隔离世界能访问页面主世界的 JS 全局变量。该探针属于可选确认信号，探针不可读时回到通用门控，不得默认判定为正常或目标不存在。

## 5. 点击穿透状态机

### 5.1 为什么不需要复合工具

BrowserAgent 已能在一次模型输出中生成多个 `browser_call`，Harness 会顺序执行这些调用。但所有参数是在收到执行反馈前一次生成的，所以只有相互独立的读取适合批量输出。卡片点击会改变 tab、URL、history 或 DOM，下一步依赖真实反馈，必须按状态机串行推进。

`Input.click` 已负责把目标滚入可交互位置，因此不需要另造“滚动并点击”工具。不同卡片位置差异较大时，每轮重新取树并点击一个目标即可。

### 5.2 点击前保存的上下文

每个列表任务至少保存：

```json
{
  "sourcePageId": "...",
  "sourceUrl": "...",
  "sourceTitle": "...",
  "sourceFingerprint": "...",
  "itemIdentity": "stable product id or normalized href",
  "detailUrl": "href read from the anchor",
  "pagesBeforeClick": ["..."]
}
```

必须通过 `DOM.getAttribute` 读取真实 `<a href>`，并用稳定商品标识绑定卡片。不得用标题模糊匹配后猜 URL，也不得跨页面复用旧 AXTree id。

### 5.3 单商品流程

```text
SOURCE_READY
  -> fresh DOM.getAXTree
  -> locate one real anchor and read href/item identity
  -> Input.click(anchor)
  -> wait for Page.open / Page.loaded / Page.loadFailed as applicable
  -> Page.list + Page.getState
  -> classify navigation outcome

NEW_TAB
  -> Page.switchTo(detailPageId) when needed
  -> Page.getState
  -> DOM.getAXTree
  -> optional DOM.getSemanticTree
  -> completeness gate
  -> extract
  -> Page.switchTo(sourcePageId)
  -> Page.getState + DOM.getAXTree

SAME_TAB
  -> Page.getState
  -> DOM.getAXTree
  -> optional DOM.getSemanticTree
  -> completeness gate
  -> extract
  -> Page.go(back, n=1)
  -> wait Page.loaded or Page.loadFailed
  -> Page.getState + DOM.getAXTree

NO_NAVIGATION_OR_IN_PAGE_REVEAL
  -> Page.getState + DOM.getAXTree
  -> inspect whether an overlay/drawer appeared
  -> retry only with a newly derived target, otherwise record failure
```

### 5.4 导航结果判定

点击后的分支只能从反馈得出：

- `Page.list` 相比点击前新增 `pageId`：新标签/新页面。
- 原 `pageId` 保留但 `Page.getState.url` 改变：同页跳转。
- pageId 和 URL 不变但树发生显著变化：站内弹层、抽屉或 SPA 局部导航。
- 没有页面、URL 或 DOM 变化：点击未生效，不得假定已进入详情。
- 出现 `Page.loadFailed`：按失败信息处理；不能立即重复相同点击。

### 5.5 新标签返回列表

原列表标签仍存在时，最可靠的返回方式是：

```json
{
  "method": "Page.switchTo",
  "params": {
    "pageId": "<sourcePageId>",
    "purpose": "Return to the preserved listing page"
  }
}
```

可在完成详情抽取后关闭详情标签，但关闭不是恢复列表的前置条件。只有原列表标签已丢失或明确决定复用详情标签时，才用 `Page.navigate(sourceUrl)` 重建列表；这种兜底会丢失列表页的滚动位置、筛选状态和 SPA 内存状态。

虽然新标签打开通常不会物理改变列表 DOM，当前 Harness 会在 `Input.click` 和 `Page.switchTo` 后保守失效 AXTree 缓存。因此切回列表后仍须重新 `Page.getState + DOM.getAXTree`，不能复用点击前 node id。

### 5.6 同页跳转返回列表

最新版 ABCP 的 `Page.go` schema 已确认：

```json
{
  "pageId": "<currentPageId>",
  "direction": "back",
  "n": 1,
  "purpose": "Return from the clicked detail page to the originating listing page"
}
```

约束：

- `direction` 只能为 `back` 或 `forward`。
- `n` 可选，`1` 表示一步；`0` 表示移动到该方向边界。
- 可以改用 `domain` 或 `tag` 定位最近的历史锚点。
- `n` 不能与 `domain`/`tag` 同时使用。
- 调用后等待 `Page.loaded` 或 `Page.loadFailed`，再读取状态和 AXTree。

同页返回优先使用 `Page.go(back, n=1)`，因为它能最大程度保留浏览历史、筛选状态、referrer 链和 SPA 上下文。只有 history 不可用、返回到了错误页面或 `Page.go` 失败时，才回退到 `Page.navigate(sourceUrl)`；回退后按 `itemIdentity` 重新定位，而不是使用旧位置或旧 AXTree id。

### 5.7 批量约束

- 一次只点击一个卡片，收到导航和完整性反馈后再处理下一项。
- 每项最多执行有界的点击穿透尝试，默认 2 次；全任务仍受 objective attempt budget 约束。
- 只点击已验证的真实链接。非链接卡片若必须依赖 JS 事件，不纳入第一版自动批量恢复。
- 详情页出现新弹窗、登录页或 challenge 时，分别进入已有 page/auth/challenge 流程。
- 列表回归后检查 URL、页面身份和当前项目游标，避免重复抓取或跳项。

## 6. 评论任务的具体读取顺序

通用门控恢复出完整详情页后，评价采集采用以下顺序：

1. `DOM.getAXTree` 定位“用户评价”区域和“查看全部评价”控件。
2. 滚动到评价区；刷新 AXTree，确认控件仍有效。
3. `DOM.getSemanticTree` 验证预览评论正文是否进入 DOM。AXTree 没有正文不构成缺失结论。
4. `Input.click` 点击“查看全部评价”。
5. 刷新 `Page.getState + DOM.getAXTree`，确认抽屉/弹层已出现。
6. 使用 SemanticTree/`DOM.getText` 读取评论字段；在抽屉的滚动容器内串行滚动并去重，直到取得前 20 条或达到明确终止条件。
7. 每条评论记录稳定来源节点、用户名、日期、SKU、正文和当前详情 URL；不足 20 条时必须区分“页面确实只有 N 条”“滚动预算耗尽”和“内容再次被抑制”。

这部分同时纠正 BrowserAgent prompt 中“SemanticTree 只用于 selector debugging”的过窄表达：SemanticTree 仍是重型诊断工具，但当目标文本已被证明只存在于 SemanticTree 时，它是必要的数据读取面。

## 7. Harness 改动设计

### 7.1 进度门禁统一

当前存在两份不一致的名单：

- `harness/progress.py::ARTIFACT_PROGRESS_TOOLS` 不包含 `DOM.getSemanticTree`、截图、视觉验证以及多个页面恢复动作。
- `harness/tools/browser_tools/__init__.py::PROGRESS_GATE_RECOVERY_TOOLS` 已包含其中一部分。

应提取单一的诊断/恢复 bypass 策略，并让两个门禁复用。建议至少覆盖：

```text
DOM.getAXTree
DOM.getSemanticTree
DOM.getText
DOM.getAttribute
Page.getState
Page.list
Page.screenshot
Page.create
Page.navigate
Page.reload
Page.switchTo
Page.go
Input.scroll
Input.press
visual_verify
System.getCapabilities / describeAction / describeEvent
```

该名单的语义是“允许完成有界诊断或恢复”，不是“已经产出 artifact”。因此：

- 成功调用不应伪造 `record_extraction` 进度。
- 每个 navigation epoch 对重型诊断设置有界 bypass 次数，防止无限 SemanticTree/screenshot 循环。
- 新鲜 DOM offload 结果应允许读取其精确 `savedPath`，不应被本地文件读取门禁再次卡住。
- 成功落地的 `Page.navigate`/`Page.go` 可以通过现有 navigation progress 通知重置导航停滞计数。

### 7.2 `Page.go` 全链路接入

| 文件/模块 | 必要改动 |
|---|---|
| `harness/tools/browser_tools/axtree_state.py` | 将 `Page.go` 加入 `AXTREE_INVALIDATING_METHODS`；同时审计 `Page.reload` 的遗漏。 |
| `harness/constants.py` | 将 `Page.go` 加入 `RENDER_RECOVERY_METHODS`，避免 render loss 时递归恢复；审计 `Page.reload`。 |
| `harness/progress.py` | 将 `Page.go` 识别为导航恢复动作，成功后记 navigation progress。 |
| `harness/observation/page_fingerprint.py` | 接受 `Page.go` 结果，并把返回后的 URL/title/pageId 纳入新 epoch。 |
| `harness/workflow_policy.py` | 把 `Page.go` 视为导航动作：其后要求 settlement，再要求 `Page.getState + DOM.getAXTree`。允许 `Page.loadFailed` 作为失败结算分支，不能只接受 `Page.loaded`。 |
| `agent_harness.py` | 在 BrowserAgent/LeadAgent prompt 中写明新标签与同页跳转的恢复分支。 |

`global_schema_cache` 当前可能仍保存旧 capability 数量。直接调用 RPC 探测不会更新缓存；正常 LeadAgent 启动时，`_bootstrap_schema_cache` 应根据实时 capability hash 原子重建全部 schema。实现测试必须覆盖缓存 hash 变化后生成 `Page.go.json`，不能手工维护一份静态 schema 掩盖 bootstrap 问题。

### 7.3 内容门控与分类

建议新增：

- `harness/content_completeness.py`：tracker、evidence model 和判定矩阵。
- Worker 可恢复分类：`route_sensitive_content_suppression`。
- 有界恢复失败分类：`blocked_content_suppression`。

状态路由：

```text
inconclusive
  -> worker continues bounded materialization

route_sensitive_content_suppression
  -> Lead replans/pivots to listing-link navigation
  -> not terminal, not HITL, not validated_done

blocked_content_suppression
  -> phase blocker after click-through budget is exhausted
  -> blocks dependent phases
  -> never normalized to target_absent
```

`target_absent` 和 `instruction_infeasible` 的 evidence gate 前增加 completeness veto：`route_recovery_required` 必须降级为可恢复分类；`inconclusive` 只有在页面外壳存在、目标区域缺失，且尚未完成滚动与 SemanticTree 等有界 materialization 时才 veto。即使已有截图，截图也只能证明“当前访问路径下未渲染”，不能证明源站没有该内容。

### 7.4 Challenge/HITL 边界

`ChallengeTracker` 保持负责验证码、验证页、可见 interstitial 和需要人工参与的反自动化状态。内容抑制证据可以出现在统一诊断摘要中，但不得：

- 累加 CAPTCHA suspicion score；
- 自动调用 `Hitl.requestPause`；
- 等待 `Hitl.resumed`；
- 因为 `pcIdentityRisk` 等字段就提示用户手工验证。

只有页面实际出现登录/验证码/验证交互时，才进入现有 auth/challenge 路径。

### 7.5 Prompt 调整

BrowserAgent system prompt 增加以下规则：

- URL、title 和页面外壳成功不等于内容完整。
- 预期详情区域整体缺失时，先完成有限的 reveal/scroll/SemanticTree 检查，再评估导航来源。
- 存在列表/搜索来源时，真实链接点击优先于深链直达。
- 导航点击一次一反馈；不能在未知新 pageId/URL 前批量生成依赖调用。
- 新标签返回使用 `Page.switchTo(sourcePageId)`；同页返回使用 `Page.go(back, n=1)`。
- 每次 Input/Page 导航动作后刷新页面身份和 AXTree。
- AXTree 缺少文本时，SemanticTree 可以作为真实 DOM 文本来源。
- 内容被抑制是可恢复/阻断状态，不是 `target_absent`，也不是默认可空字段。

LeadAgent prompt 增加：

- 收到 `route_sensitive_content_suppression` 时，必须重排为列表点击路径，不得把同一批详情 URL 再交给 worker 直连。
- 收到 `blocked_content_suppression` 时，停止同路径重试并保留 blocker；不能把依赖阶段标成成功。
- 批量项目必须保存 source page 和 item identity，允许一个 BrowserAgent 在同一实例内串行管理多标签。

### 7.6 Strategy bank 合并

修改现有 `web_scrape.detail_sections.reveal_then_text`，不增加第九条策略。扩展内容包括：

- 详情外壳存在但多个 section 同时缺失时，不记录空值，先执行 completeness gate。
- 当前页有限 reveal 无效且存在列表来源时，切换到 listing-link navigation。
- 真实 `<a>` 卡片逐个点击；每次点击后用 Page.list/Page.getState/DOM tree 验证结果。
- 新标签用 `Page.switchTo` 回列表；同页跳转用 `Page.go` 回列表。
- 恢复后重新取树并按 item identity 续跑。
- 恢复失败输出 `blocked_content_suppression`，而不是 `target_absent`。

不提高 `compact_strategy_bank` 的 strategy 数量上限，因为没有新增条目。

### 7.7 Skill/contract 可选扩展

给需要稳定重复运行的站点 skill 增加可选 `content_completeness` 声明，但不要求所有任务填写。通用 BrowserAgent 在没有声明时仍可通过“外壳存在 + 多区域缺失 + 直接导航 + 可用列表来源”触发软恢复建议。

第一版不增加自动卡片复合 workflow。待原子工具路径稳定后，才评估是否把已验证 trace 蒸馏成 workflow；即使蒸馏，也必须保留导航 outcome 分支，不能假设所有点击都会开新标签。

## 8. 文件改动清单

| 优先级 | 文件 | 改动 |
|---|---|---|
| P0 | `harness/progress.py` | 修复 no-artifact 诊断/恢复误拦截，纳入 SemanticTree、截图、页面恢复和 `Page.go`。 |
| P0 | `harness/tools/browser_tools/__init__.py` | 复用统一门禁策略；记录点击前后页面集合、导航来源和 completeness 状态。 |
| P0 | `harness/tools/browser_tools/axtree_state.py` | `Page.go` 导致 AXTree epoch 失效；审计 `Page.reload`。 |
| P0 | `harness/constants.py` | 更新 render recovery/action 分类及 worker status 常量。 |
| P0 | `harness/workflow_policy.py` | 支持 `Page.go` 的 settlement/state/tree 生命周期。 |
| P0 | `harness/observation/page_fingerprint.py` | 接收 `Page.go` 与导航来源信息。 |
| P1 | `harness/content_completeness.py` | 新增通用门控和证据模型。 |
| P1 | `harness/task_control.py` | 新 validator、分类映射及 `target_absent` veto。 |
| P1 | `harness/spawner.py` | Lead/phase 路由：可恢复 pivot 与有界失败 blocker。 |
| P1 | `harness/vl/reality_check.py` | 视觉缺失证据不能覆盖已触发的内容抑制证据。 |
| P1 | `agent_harness.py` | 更新 BrowserAgent/LeadAgent system prompt。 |
| P1 | `strategy_bank/strategy_bank.json` | 合并扩展 `reveal_then_text`。 |
| P2 | `skills/_template/SKILL.md`、`workflow.json`、skill contract 校验 | 可选声明式 completeness/risk signal。 |
| P2 | tests | 门控矩阵、进度门禁、Page.go 生命周期和双导航分支回归。 |

实现时应先以 `rg` 确认 skill 模板和 contract 的实际文件位置；上表 P2 路径是现有模板约定，不应在文件不存在时凭空创建平行体系。

## 9. 测试与验收

### 9.1 单元测试

内容门控矩阵至少覆盖：

| 外壳 | 必需区域 | 直接导航 | 确认信号 | 列表来源 | 期望结果 |
|---|---|---|---|---|---|
| 有 | 全部有 | 是 | 任意 | 任意 | `complete` |
| 有 | 缺一个 | 是 | 无 | 有 | `inconclusive` |
| 有 | 缺多个 | 是 | 无 | 有 | `route_recovery_required`（软） |
| 有 | 缺多个 | 是 | 有 | 有 | `route_recovery_required`（确认） |
| 有 | 缺多个 | 点击进入 | 有 | 已尝试 | 有界重试后 `blocked_content_suppression` |
| 无 | 任意 | 任意 | 任意 | 任意 | 交给 navigation/auth/challenge 分类 |

其他单测：

- `DOM.getSemanticTree` 在无 artifact 阈值之后仍会真正 dispatch，trace 出现 `browser.transport.request`。
- `Page.screenshot`、`visual_verify`、`Page.create/reload/go` 不再被旧名单误拦截，但 bypass 有界。
- `Page.go` 使旧 AXTree id 失效。
- Workflow 中 `Page.go -> listen Page.loaded -> Page.getState -> DOM.getAXTree` 通过；缺任一步失败。
- `Page.loadFailed` 能结束等待并返回可诊断错误。
- capability hash 变化后重新生成包含 `Page.go` 的 schema cache。
- 有内容抑制证据时，即使截图显示空白，`target_absent` 也被 veto。
- 内容抑制不会调用 `Hitl.requestPause`。

### 9.2 导航分支集成测试

用 fake ABCP 事件覆盖：

1. 点击新增 pageId：识别为新标签，详情抽取后 `Page.switchTo(sourcePageId)`。
2. 点击原 pageId URL 改变：识别为同页跳转，详情抽取后调用 `Page.go(back, n=1)`。
3. 点击 URL 不变但 DOM 变化：识别为 drawer/SPA reveal。
4. 点击完全无变化：不得标记成功，不得提前点击下一卡片。
5. `Page.go` 返回错误 history：回退 `Page.navigate(sourceUrl)`，重新定位 item identity。

### 9.3 任务回归

使用现有任务资产做离线回归：

- `browser-002` trace 第 74 行附近证明旧门禁问题。
- `trees_probe_RENDERED/semantictree_outline.txt` 证明评论正文存在于 SemanticTree。
- 原 HTML 文件可作为内联站点信号与两条预览评论的 fixture，但不能代替 live DOM 交互测试。

最终验收标准：

- 不再输出“评论不在无障碍 DOM 渲染”这一未经 SemanticTree 执行支持的结论。
- 直链内容不完整时自动切换到列表链接点击路径。
- 每个商品点击结果都被验证为新标签、同页跳转、局部 reveal 或失败之一。
- 同页返回确实使用 `Page.go`，新标签返回优先使用 `Page.switchTo`。
- 评论任务能够点击“查看全部评价”并读取前 20 条；若不足，输出可审计的真实终止原因。
- 内容抑制不会被降级成字段可空，也不会铸成 `validated_done`。

## 10. 可观测性

新增结构化事件：

```text
content_completeness.observed
content_completeness.decision
navigation_recovery.click_started
navigation_recovery.outcome
navigation_recovery.return_started
navigation_recovery.return_settled
navigation_recovery.exhausted
semantic_tree.diagnostic_bypass
```

每个事件至少包含：worker/phase、pageId、navigation epoch、sourceUrl、targetUrl、itemIdentity、attempt、decision、evidence summary。敏感 URL 参数继续走现有日志脱敏规则。

建议统计：

- 直接导航的内容不完整率。
- 点击穿透恢复成功率。
- new-tab/same-tab/in-page 三种 outcome 分布。
- `target_absent` 被 completeness gate veto 的次数。
- 每个成功商品的平均点击/树刷新次数。
- SemanticTree 的调用次数、落盘次数和门禁拦截次数。

## 11. 实施顺序

1. **基础兼容层**：刷新 capability cache 流程测试，接入 `Page.go` 的 AXTree、render recovery、workflow 和 page fingerprint。
2. **解除错误门禁**：统一 progress bypass，让 SemanticTree、截图和恢复动作可执行且有界。
3. **门控与分类**：实现 `ContentCompletenessTracker`、validator、`target_absent` veto 和 Lead 路由。
4. **LLM 行为层**：更新 Browser/Lead prompt，并合并扩展 `reveal_then_text`。
5. **站点声明层**：增加可选 skill/contract completeness 配置；淘宝字段仅作为 fixture/adapter 示例。
6. **测试与回归**：先跑定向测试，再跑全量 pytest；对当前任务按列表点击路径重跑 detail phase。

每一阶段都应单独提交 diff 和测试结果。P0 未完成前不要先做自动卡片 workflow，否则会把 `Page.go`、AXTree epoch 和进度门禁缺陷封装进新的复合工具。

## 12. 最终决策摘要

- 分类名称采用“路由敏感型内容抑制”，不把站点内部字段当作通用根因。
- 内容完整性门控独立于 CAPTCHA/HITL。
- `target_absent` 和 `instruction_infeasible` 必须经过 completeness veto。
- 复用原子工具，不新增 `open_cards_verified`。
- 卡片点击串行执行，`Input.click` 负责自动滚动。
- 点击后通过 Page.list/Page.getState/DOM tree 判断真实 outcome。
- 新标签返回优先 `Page.switchTo`；同页返回优先 `Page.go(back, n=1)`；`Page.navigate(sourceUrl)` 仅兜底。
- 扩展现有 `web_scrape.detail_sections.reveal_then_text`，不新增 strategy bank 条目。
- 评论正文读取优先依据实际树差异：交互定位用 AXTree，正文存在于 SemanticTree 时就使用 SemanticTree。

## 13. 实施与验证记录

截至 2026-07-19，P0/P1 代码已落地：

- `Page.go` 已接入 AXTree 失效、页面生命周期、workflow settlement、render recovery、page fingerprint、进度门禁和 Browser prompt。
- `DOM.getSemanticTree`、`Page.screenshot`、`visual_verify`、`Page.reload`、`Page.create`、`Page.navigate`、`Page.go`、`Page.list` 等诊断/恢复动作不会再因尚未产生 artifact 而在 RPC 前被错误拦截。页面级重型动作按 page/navigation epoch 计数；不携带 pageId 的 `Page.create` / `Page.list` 使用独立 worker 级预算。只有真正 dispatch 的调用扣额度，参数/contract 拒绝不扣额度。
- 已增加独立 `ContentCompletenessTracker`，支持 expected regions、shell marker、结构化 suppression signal、列表链接点击恢复、跨同页/新标签共享的 per-item 恢复次数上限、`target_absent` / `instruction_infeasible` veto，以及 `route_sensitive_content_suppression` / `blocked_content_suppression` 分类。
- Python 核心不再内置 reviews、specifications、description 或中文 tab 词表。现有 `web_scrape.detail_sections.reveal_then_text` 在 Strategy Bank 中声明字段 token 与页面 marker 的映射；策略被选中且显式 contract 缺失时，Lead 根据 required/nonempty artifact fields 把匹配声明注入 worker contract。显式声明仍具有最高优先级。
- suppression signal 的 `strength` 已进入判定：只有 `confirmatory` / `strong` 能作为确认性抑制证据，`supporting` 只保留为辅助证据。
- `inconclusive` 不做全局终态 veto。仅当页面外壳存在、目标区域缺失且尚未完成有界 materialization 时临时 veto；滚动和 SemanticTree 检查完成后交还既有 navigation/auth/challenge 分类。
- 恢复次数 ledger 位于 tracker 级别而不是 page state：同一商品反复打开新标签不会重置预算，不同商品仍分别计数。跨标签选择最新 completeness 状态使用 tracker 单调 observation order，不比较不可跨页排序的 page-local navigation epoch。
- `blocked_content_suppression` 也进入 semantic-absence veto：worker 如果在恢复预算耗尽后仍声称 `target_absent` / `instruction_infeasible`，Browser gate 与 Spawner gate 会保留 blocked 分类，不会把“当前访问路径持续受抑制”误写为“源站不存在目标”。
- 内容门控消费既有 challenge、HITL、auth/paywall overlay、navigation check、lifecycle 和 error classification receipt。登录、验证码、off-target、loading/loadFailed/crashed 或 RPC 失败页面标记为 `upstream_blocked`，不会生成 route suppression veto。
- 导航恢复现已记录 click、outcome、return、exhausted 全链路事件；目标 URL 写日志前去除 query，避免令牌或搜索参数泄露。
- Browser/Lead prompt 与现有 `web_scrape.detail_sections.reveal_then_text` 已同步导航分支和返回规则；未新增复合点击工具或重复 strategy。
- skill/workflow contract 可选地声明 `content_completeness`，显式 worker contract 优先。

离线验证结果：

- 全量测试：`1347 passed, 6 skipped`。
- 两条 warning 均为既有 SemanticTree 测试类带 `__init__` 导致的 Pytest collection warning，与本次修改无关。
- `python -m py_compile`、`python -m json.tool strategy_bank/strategy_bank.json` 与 `git diff --check` 均通过。

Live 验证采用 `docs/abcp-browser-direct/scripts/abcp_taobao_route_recovery_probe.py`，全程只使用一个 Fleet、一个搜索源页并串行点击。验证结果：

1. 首轮搜索页出现真实扫码/密码/短信登录控件，按认证门控发起 HITL；人工登录并恢复后，同一 Fleet/Page 成功继续，没有更换 Cookie、IP 或指纹。
2. AXTree 暴露真实商品 `<a>`，`DOM.getAttribute(href)` 读取到 `detail.tmall.com/item.htm` 链接；`Input.click` 后 `Page.list` 识别为新标签。
3. 新标签 ready 后刷新 AXTree/SemanticTree。详情页同时存在用户评价、参数信息、图文详情；SemanticTree 包含两张评论预览卡正文以及“查看全部评价”。这直接反证“评论不在无障碍 DOM 渲染”。
4. 点击“查看全部评价”成功打开 Drawer；随后按新标签分支使用 `Page.switchTo(sourcePageId)` 返回，搜索页 URL、标题和商品链接均验证恢复。
5. 首次实现暴露了一个 lifecycle 缺陷：新标签刚出现时直接 `Page.switchTo` 返回 `Page not ready`。探针已修正为先对新 pageId settlement，再切换和刷新 AXTree；不得以固定 sleep 代替生命周期确认。
6. 抽屉未加载 20 条评论。Drawer 子树只有一张骨架屏图片；连续 5 次在抽屉容器内滚动并刷新 SemanticTree，结果始终为 `0/20`。底层详情预览的两张 `Comment--` 不计入 Drawer 评论数。
7. 内联状态此时为 `pcResistDetail="false"`、`pcIdentityRisk="false"`，再次证明这两个字段不能作为通用或充分判据；评论相关 MTop 资源同时出现 `_____tmd_____/punish`、`x5step=2`、`action=captcha`。
8. 事后复核同一轮已保存的完整 AXTree，发现它并非“没有可交互 CAPTCHA”：主详情根之外还有一个独立的 depth-zero `rootwebarea "Captcha Interception"`，其子树包含异常流量提示、`button "滑块"` 和 `Please slide to verify`。SemanticTree 只暴露 iframe 外壳，而 AXTree 合并了该跨 frame 可访问性子树。

因此本次 live 验收的准确结论是：**列表点击恢复完整详情 DOM 的方案通过；前 20 条评论被评论接口的独立、嵌入式滑块验证阻断。** 先前“无可交互 CAPTCHA 页面”的判断错误，根因是探针只按主页面树和抽屉子树解释结果，没有检查 AXTree 的所有 depth-zero 根；而且该直接探针绕过了 Harness 的 `ChallengeTracker`。这里应进入 HITL，而不是 `blocked_content_suppression`。只有在完整 AXTree 中确实不存在可交互验证面、同时有 suppression signal 且有界 materialization 仍失败时，才使用后者。

为此，Browser prompt 与原有 `reveal_then_text` 又补充了一条通用验收规则：显式重复记录目标必须在目标 Drawer/子树内计数；标题、抽屉外壳、预览记录和骨架图片都不满足目标条数。探针落盘的 `summary.json`、每轮 AXTree/SemanticTree 和骨架图片保留在 `worktree/taobao_route_recovery_live/`。

## 14. 隐蔽跨 frame 验证的 Harness 修正

评论验证码的“隐蔽”来自两个叠加因素，而不是验证码不存在：

1. 验证面位于独立 iframe；主页面标题和绝大多数详情 DOM 仍正常，整页视觉判断容易把小验证框归为普通加载。
2. `DOM.getAXTree` 的大结果会先落盘，模型可见 outline 只保留头部；验证 frame 位于原始树后段。旧链路在落盘后才喂给 `ChallengeTracker`，因此既丢失结构证据，也无法自动暂停。

修正后的通用规则不依赖 `pcResistDetail`、`pcIdentityRisk` 或淘宝 URL：

- 在 AXTree 落盘前扫描所有 depth-zero `rootwebarea`。
- 只有“根自身具有 CAPTCHA/安全验证/异常流量语义”且“同一 frame 内存在 slider、checkbox、验证按钮等可操作控件”时，生成紧凑 `structuralChallenge` receipt。普通页面里提到 CAPTCHA 的说明文字不会触发。
- 该结构证据优先级高于整页 VL 的 `normal_loading`，直接调用现有 `Hitl.requestPause`；不新增验证码点击或绕过工具。
- 人工恢复后，Harness 在同一个详情 pageId 上重新获取完整 AXTree。若验证 frame 仍存在，就原地再次暂停，不 reload、不 navigate，保留已经打开的评论抽屉状态。
- 只有新 AXTree 确认验证 frame 消失才恢复 worker；随后必须重试被中断的评论 materialization，并以 SemanticTree 中目标 Drawer 的实际记录数验收。正常标题、Drawer 外壳、骨架图或详情页两条预览均不算恢复成功。

离线回归增加了淘宝保存证据的最小 fixture、误报反例、整页 VL 冷却不可压制结构证据、HITL 恢复 checkpoint，以及“验证仍在则同页再暂停、消失后继续”的测试。本轮没有新建 Fleet，因此没有人为制造第二次验证码来做破坏性 live 重放。

## 15. HITL 恢复通知兼容与状态对账

对 `worktree/7c90ee49e4b34f67b6c59454c7a81a28` 的超时不能只归因于“Harness 感知不及时”。已安装 ABCP 的调用链显示：

1. 面板的恢复动作调用 `clearPauseState(pageId, "human")`，内部确实发布 `hitl:resumed`。
2. Dispatcher 把 `Hitl.paused` 和 `Page.crashed` 视为 blocking 事件并发给 `user + agent`，但 `Hitl.resumed` 被归入非 blocking 分支，只发给 `user` audience。
3. Agent WebSocket 使用 `agent` audience 订阅，因此该版本下 Harness 不会收到这条恢复通知。这是确定性的 audience 路由缺口，而不是随机延迟或 Harness 轮询太慢。
4. 此外，较新事件使用 `control.event -> data.kind/payload.sourceEvent/payload.page` 信封；旧 Harness 只解析 legacy `params.data.event`。即使未来 ABCP 修正 audience，旧解析器也可能漏掉新信封。

ABCP 层暂时不可修改，因此 Harness 采用两层兼容：

- 完整解析 legacy 与 `control.event` 信封；显式 `Hitl.resumed` 仍是最快路径。
- 等待期间每 5 秒做一次只读 `Page.getState` 对账，但绝不把“RPC 可调用”直接当作恢复。只有 `data.hitl.isPaused` 明确为 `false`，并且 challenge verifier 判定验证已消失，或标题发生了明确的非挑战变化时，才恢复 worker。这是受 HITL deadline 约束的 Harness-owned reconciliation，不是 Worker 可调用的轮询策略，也不放宽普通 workflow 的“不得 poll Page.getState”规则。
- 若 pause flag 仍为 true、状态仍含挑战语义、标题为空/未变化，或 verifier 仍看到验证面，就继续等待，不调用页面导航，不破坏当前 Drawer。
- 对账路径记录 `hitl.wait.state_reconciliation`，结果标注 `via=state_reconciliation` 和 `signal=platform_pause_cleared`，便于与真正收到的 resume notification 区分。

这个兜底只修复“平台已经解除 pause、但 Agent audience 没收到通知”的情形；它不会自动处理验证码，也不会伪造 `Hitl.resumed`。

## 16. 评审缺陷的回归覆盖

- `DOM.getSemanticTree` 每 page/navigation epoch 允许 12 次，覆盖已验证的 5 轮抽屉滚动；达到上限后返回 `diagnostic_budget_exhausted`，一次已验证导航开启新 epoch 后重新获得预算。
- `Page.list` / `Page.create` 使用 worker 级有限预算，10 商品批量列表检查不会在第 5 次被截断。
- 未执行 RPC 的 params/schema/contract 拒绝不会消耗重型诊断预算。
- 无显式 completeness 声明、但已选 detail strategy 的声明与 required/nonempty artifact 字段匹配时，由 Lead 注入门控 contract；核心 Python 不解释字段语义。
- “只缺一个区域”保持 `inconclusive`；“无页面外壳”不由内容完整性模块抢占 navigation/auth/challenge 分类。
- 登录、auth/paywall、challenge、HITL paused、off-target、loading/loadFailed/crashed 与 browser error 均优先于内容门控。
- `target_absent` 与 `instruction_infeasible` 使用相同的 completeness veto。
- 同一 tab 串行处理不同商品时，恢复次数按 item identity 分开计算。
- capability hash 变化时，bootstrap 从实时 capability 重新生成 `Page.go.json`；测试不依赖仓库内已有的静态 schema 文件。
