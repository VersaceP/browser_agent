# Micro-Loop 实施计划（配套 micro-loop-architecture-v2.md / v4 设计）

> 日期: 2026-06-12
> 状态: 待执行
> 前置阅读: docs/micro-loop-architecture-v2.md（v4 设计）
> 本计划在 v4 基础上修正四处集成问题：只读 oracle 不作废 axtree、collect_items 每轮收割、
> 遮罩检测栈以 elementFromPoint 为首选、VL 坐标 DPR 校准。

## 总览

| Phase | 内容 | 工期 | 依赖 | 风险等级 |
|-------|------|------|------|---------|
| 0 | ABCP 行为探针 | 0.5–1 天 | 无 | 低（纯探测） |
| 1 | 只读 Oracle 通道 + Verifier 框架 | 2 天 | Phase 0 | 中（动 invalidation 逻辑） |
| 2 | Stale Guard 旁路 + recoveredTarget | 1–2 天 | Phase 0 | 中（动核心守卫） |
| 3 | 事件通道 Layer 0 | 1–2 天 | Phase 0 | 低（纯增量） |
| 4 | dismiss_overlay composite tool | 2–3 天 | Phase 1+2 | 中（自动点击） |
| 5 | collect_items composite tool | 2–3 天 | Phase 1+4 | 低 |
| 6 | fill_field_verified composite tool | 1–2 天 | Phase 1+2 | 低 |
| 7 | VL 坐标互证 + 自动拦截 + 端到端 | 2–3 天 | Phase 4+5+6 | 中（被动触发） |

总计约 10–15 个工作日。Phase 1/2/3 在 Phase 0 结束后可并行。

---

## §0.5 Loop 边界登记表（设计期定稿，实现仍只做 3 个）

> 现在就定边界，不是现在就实现更多。每个 loop 的身份 = 它唯一的判定器（oracle）；
> 边界没定清，就不知道在建哪个 verifier。"显式不属于本 loop"那一列是最便宜的保险，
> 防止某个 loop 范围漂移、吞掉相邻场景（filter 漂进收割、submit 漂进填表）。

### 分层（关键）：micro-loop ≠ 编排流

- **Tier 1 — micro-loop**：单一 oracle、确定性、harness 内部跑完一轮闭环。本批只建这 3 个。
- **Tier 1.5 — 收割引擎变体**：同一 MaterializeEngine 内核，换累加器。延后。
- **Tier 2 — 编排流（composed flow）**：编排多个 Tier-1 loop + 导航，含少量判断，**不是 micro-loop**。
  detail_collect 属于此层，必须在 3 个 micro-loop 稳定后再建，否则它模糊的 verifier 边界会污染整套。

### Tier 1（v1 实现）

**dismiss_overlay** — 移除挡住"已决定动作"的瞬时遮挡
- 触发：失败信号 P0（occlusion_blocked 错误）/ P1（frame 遮挡）为主，也可模型主动调用
- oracle：verify_overlay_gone（occluder_probe elementFromPoint → dialog/full-cover 消失）
- 退出：遮罩消失→按白名单条件重试原动作 | 分类为 auth/paywall→blocked 交回模型 | 梯子耗尽→failed
- **显式不属于**：登录/付费墙（永不自动点，返回 blocker）；遮罩关闭后若露出更多内容→那是 collect_items 的活，不是本 loop 续做

**collect_items**（内核 MaterializeEngine；曾用名 scroll_collect，已统一更名，理由见下）— 对**同一结果集**反复执行显现动作，每轮收割、按 stable_key 去重
- 触发：模型主动调用
- oracle：verify_items_grew（count + stable-key delta，虚拟化感知：count 不增但 key 变也算新增）
- 动作 mode：v1 = scroll + click_load_more；v2 = expand_sections、click_pagination（须加"仍是同一结果集"校验）
- 退出：target_count 达成 | 连续 N 轮停滞 | max_rounds | 遇遮罩→dismiss_overlay（blocked→整体 yield）
- **不变式**：同一逻辑结果集、渐进显现。动作一旦改变"在看哪个集合"，就不是本 loop。
- **显式不属于**：filter / search / sort / facet（改变数据集身份）；切换对象的不同 facet 标签（→ sweep/tab）；打开详情页（→ detail_collect）

**fill_field_verified** — 写**单个**字段并确认写入生效
- 触发：模型主动调用
- oracle：verify_field_value（读 .value property，关键词定位，matchCount 感知，敏感值掩码）
- 退出：值匹配→done | 不匹配→清空重试 1 次→yield | 找不到→AXTree refresh fallback | 歧义(matchCount>1)→yield | 遮挡→dismiss_overlay
- **显式不属于**：多字段表单编排（模型自己排序字段）；**submit（独立的 verified action，不可逆，绝不自动）**；触发异步的下拉/combobox 选择；日期选择器（Escape 语义不同）

### Tier 1.5 / Tier 2 / 拒绝（现在定名，延后或不做）

- **sweep_collect / tab_collect**（Tier 1.5）：按 tab/facet **分区**累加（非去重进单一集合），同内核换累加器。延后。
- **detail_collect**（Tier 2，TAAFT 形状）：列表收链接→逐个打开详情→提取→回列表/下一个。编排 navigate + collect_items + dismiss_overlay + 提取，**非 micro-loop**。3 个稳定后再建。
- **act_and_confirm**（验证族）：export/download/submit→等 toast/文件/状态→确认。延后。
- **filter_apply_verified / query_results_verified**（拒绝进收割 loop）：改变数据集身份；若将来要做，单列工具。

### 命名分歧（对 ChatGPT 的一处反驳）

ChatGPT 主张保留 scroll_collect 之名"降低模型学习成本"——**说反了**。模型按名字/描述选工具；一个内部还做
load_more/pagination 的工具叫 "scroll_collect" 是在骗模型：load_more 场景会被**漏触发**，且模型要额外学
"scroll_collect 不止 scroll"，反而**增加**学习成本。该工具尚未上线（Phase 5 才建），不存在"已有名字的迁移成本"，
所以按产出命名：**collect_items**（按 mode 描述动作）。**已定（2026-06-13）**：本计划全文已统一为 collect_items。

---

## Phase 0 — ABCP 行为探针（0.5–1 天）

所有后续设计决策的事实基础。写一个 `probe_micro_loops.py`（沿用现有 probe_*.py 风格），
fixture 用 localhost:9401 playground（stale-id-recovery.html 等）+ 真实 TAAFT 页面。

必须回答的问题：

1. **DOM.axTreeUpdated 事件**：System.describeEvent 确认真实事件名、payload 形状
   （携带完整 lines？id remappings？还是空通知？）——决定 Phase 3 的增量更新策略。
2. **recoveredTarget**：哪些方法触发（只有 Input.click？Input.type 呢？）；
   payload 是否包含新节点的 role/name（决定 Phase 2 的 _validate_rematch 是否需要补一次查询）。
3. **Runtime.evaluate returnByValue 可靠性**：verifier 级小 payload 是否稳定；
   确认 _eval_json_via_title 侧信道对 verifier 可用（taaft extract_trending_25_35.py:426 已记录不可靠案例）。
4. **frame 定向 eval**：Runtime.evaluate 能否指定 iframe 执行？
   （决定 cross-origin iframe 遮罩能否用 JS 处置，还是只能坐标点击/HITL。）
5. **DPR 校准**：Page.screenshot 像素尺寸 vs layers.viewportBounds（2560×1600），
   得出截图坐标 ↔ CSS 坐标的换算系数。Phase 7 VL 坐标回译依赖此值。
6. **只读 eval 是否触发 axTreeUpdated 事件**：决定 Phase 1 invalidation 豁免的安全性。
7. **DOM.getSemanticTree 解禁评估**（06-12 新增，06-13 扩充）：toolify.ai 万级节点
   四组参数未崩溃，但 n=1 站点不构成解禁依据。需探明：
   a. 稳定性：重跑 tests/test_semantic_tree_crash.py 全矩阵（复杂度×状态×重复×并发），
      补 Shadow DOM 与跨 frame 维度；
   b. 锚点字段：**已由 live probe 回答（06-13，reports/semantic_tree_payload_probe_*.json，
      toolify.ai，5966 节点）**——raw node key 并集为
      tag/id/bounds/isVisible/isScrollable/children/className/elementId/text/textTruncated。
      覆盖率：canonical id 100%、bounds 69%、**className 54%、elementId 9%**、text 12%。
      **没有** href/name/placeholder/aria-*/data-*。结论三条：
      ① selector-index 路线**部分复活**：elementId（高可信，#id）→ tag+className
         （中可信，含 "go-home"/"dropdown" 类语义 class）→ text 过滤（低可信）；
      ② 每个节点都带 canonical id → SemanticTree 可**直接产出可操作 id**
         （结构/几何条件筛选 → 拿 id 给 Input.*），很多场景根本不需要生成 CSS selector；
      ③ href/name/aria 缺失 → 表单字段语义关联和链接收割仍走 AXTree/JS oracle，不变。
   b2. 子树查询：**已验证可用且极便宜**（同一 probe）——以 canonical id 锚定，
      返回 4-5 节点、~700 字节、14-165ms。7c 的"全树 3.65x 重"对定向查询不适用；
      SemanticDOMIndex 形态升级为"启动全树一次 + loop 内定向子树补查（预算内）"。
   c. 尺寸实测：**已回答（reports/toolify_run.log:362）**——同页 SemanticTree/AXTree
      节点数 = 10001/2740 = 3.65x，SemanticTree 显著更重（svg path/symbol 等噪音占大头）。
      "SemanticTree 是更轻量 fallback"的主张不成立，不得写入执行方案。
   稳定性通过 → 模型工具面继续封禁，开 harness 内部通道（见 Phase 1.2）。

产出：`docs/abcp-probe-facts.md`，每条事实标注 go/no-go 影响的 Phase。

---

## Phase 1 — 只读 Oracle 通道 + Verifier 框架（2 天）

新建 `harness/observation/verifiers.py`。

### 1.1 只读 eval 通道（本 Phase 最关键的集成修正）

- `_invoke_browser_method` 增加内部参数 `read_only_eval: bool = False`。
- read_only_eval=True 时：跳过 `_invalidate_axtree_snapshot`（Runtime.evaluate 当前在
  AXTREE_INVALIDATING_METHODS 中，不修则每次 verifier 都会作废 id 缓存，
  触发 stale guard → 强制重拉全树，方案负收益）。
- 仅 harness 内部 verifier 可走此通道；模型发起的 eval 行为不变（模型 JS 可能改 DOM）。
- verifier 不经过 eval_js_json 的 policy gate（那是约束模型的）。

### 1.2 SemanticLocator + VerifierResult

按 v4 设计，外加：

- 每个 locator JS 必须返回 `matchCount`；matchCount > 1 时 confidence 降级，
  候选摘要带回（防 "email" 同时命中 "Confirm Email"）。
- 定位器主路径维持 v4 的 AXTree role/name → 关键词语义 JS。
  "SemanticTree 精确 selector 优先"一级是否成立**由 probe_semantic_tree_payload.py
  裁决**（见 Phase 0.7b）：raw node 带 HTML 属性 → 恢复该级；不带 → 永久移除。
  Phase 1 开工不依赖该裁决（主路径与此无关），但 SemanticDOMIndex 的最终形态依赖。
- SemanticTree 的现实价值重定位为 **harness 内部几何/结构索引**（filterNoise 默认开，
  模型工具面继续封禁，config `semantic_tree: off|internal`）：
  ① isScrollable 标记 → collect_items 自动发现正确滚动容器（containerId）；
  ② bounds → VL 截图裁剪区域、视口覆盖型可见节点的 overlay 候选发现；
  ③ hasShadowRoot → shadow host 映射；④ tag 层级 → 卡片/容器结构理解。
  **不得在 loop 每轮迭代中调用**（实测比 AXTree 重 3.65x，每轮拉全树重演
  "full AXTree per scroll" 的成本错误）；它是 loop 启动时的一次性发现工具。
- selector 可信度分级（产出进 SemanticLocator.confidence）：
  高=data-testid/唯一 id/稳定 name/明确 aria-label；中=role+name+容器上下文；
  低=class 链/nth-child/深层路径（仅 fallback，命中即标低置信）。
  Shadow DOM 内目标生成 host → shadowRoot → inner 的分段 locator path。
- trace 与返回值中对 password/token 类字段做掩码（fill 场景的 expected/actual 同样掩码）。
- OVERLAY_GONE_JS 加固：computed style 之外补 rect 尺寸阈值 + position fixed/absolute
  + 视口覆盖率，过滤 display:none 模板节点和零尺寸容器的假阳性。
- **侧信道与 read-only 互斥**（06-13 修正，ChatGPT 评审指出）：_eval_json_via_title
  写 document.title 和 window.__abcpJsonB64，不是只读——且 title 被 challenge_detector
  和 PageFingerprint 当信号消费，侧信道执行期间的 title 读取会被污染。
  因此 read_only_eval 路径只允许 returnByValue；verifier 返回值必须保持小 JSON
  （bool/count/top-k 候选），小 payload 下 returnByValue 失败本身就是异常信号。
  确需侧信道 → 按 mutating 处理：正常 invalidation + 恢复 title 后仍保守标脏。

### 1.3 elementFromPoint 遮挡探针（新增，v4 未含）

```
occluder_probe(target_rect_center) →
  { occluded: bool, occluder: { tag, role, className, textSnippet, rect } }
```

被挡目标 rect 中心点 elementFromPoint，返回元素若非目标或其祖先即为遮罩本体。
这是同文档 div 遮罩的最高精度检测器，Phase 4 检测栈的第一层。

### 1.4 三个 verifier

- `verify_overlay_gone`：快速信号（layers occlusionState）→ occluder_probe → 全局 dialog 检测。
- `verify_items_grew`：PageFingerprint delta 快速信号 → 语义计数 oracle。
  **注意**：oracle JS 同时返回 count + rows（Phase 5 每轮收割复用，一次 RPC 两个用途）。
- `verify_field_value`：value property 读回（非 attribute，兼容 React 受控）；
  多策略定位（label[for] / aria-label / placeholder / name / input.labels）；
  found=false → AXTree refresh fallback（AXTree 可穿透 shadow DOM，JS 不能——
  语义 JS 是首选定位器，AXTree 是 fallback 定位器，不是"不参与定位"）。

### 1.5 测试

fake browser 风格（参照 tests/test_overlay_detector.py）：
- 各 verifier 成功/失败/歧义（matchCount>1）路径
- read_only_eval 不触发 invalidation 的断言
- read_only_eval 下 returnByValue 失败：**不走侧信道**，返回 verifier error，
  断言 AXTree 缓存未被污染（invalidated 仍为 False）
- mutating/fallback 路径使用侧信道：断言 invalidation 已触发、title 尝试恢复后仍保守标脏
- oracle 抛异常的错误处理

---

## Phase 2 — Stale Guard 旁路 + recoveredTarget（1–2 天，P0）

### 2.1 _check_stale_axtree_target 增加 allow_rematch

- 新增 `agent.axtree_seen_ids`：按 page 维度保留最近 N 个 epoch 的历史 id 集合
  （当前 axtree_ids 只存当前快照，"曾经见过"无从判断——v4 漏了这个前置条件）。
- allow_rematch=True 且 id ∈ seen_ids 且 page 匹配 → 放行给浏览器触发原生 rematch；
  page mismatch 或从未见过的 id → 仍拦截。
- 配置 `browser_side_rematch: off | composite_only | on`，默认 composite_only。

### 2.2 recoveredTarget 处理

- `_observe_axtree_state_after` 解析 response 中的 recoveredTarget：
  previousId → currentId 替换进 axtree_ids。
- `_validate_rematch`：rematched 节点 role/name 与原目标一致才接受；
  不一致 → axtree_invalidated = True 且该次动作按失败处理。
  （role/name 来源依 Phase 0 探针结论：response 自带，或补一次 find_in_axtree。）

### 2.3 测试

- 旁路放行 / page mismatch 拦截 / 未见过 id 拦截
- rematch 到同名节点 → 接受；rematch 到异名节点 → 作废 + 失败
- 三档配置行为

---

## Phase 3 — 事件通道 Layer 0（1–2 天）

新建 `harness/observation/event_observer.py`（BrowserEventObserver）。

- 通过 abcp_client.subscribe_notifications 订阅；按 pageId 过滤。
- DOM.axTreeUpdated → 按 Phase 0 探明的 payload 形状做增量更新
  （完整 lines → 重建 ids/epoch；remappings → 逐条 _validate_rematch；空通知 → 标记 invalidated）。
- **三层消费纪律**（v4 §6 已定，此处为执行约束）：
  - Layer 0 状态记账：永不进模型上下文；
  - Layer 1 composite tool 内部：loop 生命周期内有效，结束即弃；
  - Layer 2 结构化摘要：仅经 composite tool 返回值、仅 5 字段级别的归一化摘要。
- 所有原始事件 payload 落 trace JSONL，供调试，不进模型。
- 测试：fake hub 注入事件序列，断言状态更新与"零模型注入"。

---

## Phase 4 — dismiss_overlay（2–3 天）

注册进 BROWSER_TOOLS，内部全部走 `_invoke_browser_method(count_progress=False)`。

### 4.1 检测栈（顺序固定）

1. occluder_probe（elementFromPoint，同文档遮罩，元素级精度）
2. layers.occlusionState + occludedByFrameIds（iframe 遮罩）
3. overlay_detector 文本检测（只做 subtype 分类，不做存在性判定）

### 4.2 流程

```
分类先行：auth_prompt / paywall → 立即返回 {status:"blocked", subtype}，零尝试
（分类是免费的，先路由后爬梯子；与"立即 yield 绝不自动点击"的测试断言一致）
梯子：找 close 控件(NEVER_CLICK 过滤) → click → verify_overlay_gone
   → Escape → verify（表单填写上下文跳过 Escape 优先，Escape 会取消编辑/关闭日期选择器）
   → backdrop click（elementFromPoint 验证点位安全）→ verify
成功 → 条件重试原始动作（经 allow_rematch 通道），**白名单放行而非黑名单拦截**
  （06-13 修正极性：敏感词黑名单永远不全，漏判后果是误点危险按钮；
  白名单漏判后果只是多一个模型 turn——风险不对称决定用白名单）。
  四因素全部满足才自动重试：
  ① method 低风险（scroll/focus/click；Input.type 不自动重试）；
  ② 目标 role 为 link 或非 submit 控件，且不处于 form 提交上下文；
  ③ 目标 text/href 语义不含 checkout/pay/delete/submit/login 类意图；
  ④ 本次未发生 rematch，或 rematch 已过 role/name 校验且 verifier 通过。
  任一不满足 → {status:"dismissed_pending_action"} 交回模型。
全败 → {status:"failed", attempts:[...], hint}
```

### 4.3 边界

- max_total_attempts / max_duration_ms 硬上限；全部动作进 agent.trace。
- 返回给模型的结果是 digest：attempts 摘要 top-k 截断（≤3 条、每条一行），
  全量尝试日志进 trace 不进模型上下文（适用于所有 composite tool）。
- NEVER_CLICK 关键词表（登录/支付/订阅/provider 按钮，中英双语）。
- `_attach_runtime_strategy_hints` 的 occlusion 提示改为直接指名 dismiss_overlay。
- 更新 system prompt L5 + strategy bank 对应条目。

### 4.4 测试

cookie banner 有/无关闭按钮、auth 墙立即 blocked（显式断言零点击）、paywall 同、
多层遮罩递归、超时退出、iframe 遮挡路由、Escape 在表单上下文被降级。

---

## Phase 5 — collect_items（2–3 天）

### 5.1 与 v4 的关键差异：每轮收割（harvest-each-round）

v4 仍是"循环结束统一 record_extraction"——虚拟化列表（React virtualization）滚动时
旧行移出 DOM，计数恒定但内容轮换，最终收割只剩最后一屏，中段数据全丢。改为：

```
每轮：Input.scroll → settle → oracle（一次 RPC 同时返回 count + rows）
   → stable_key 去重并入累积集 → 新增=0 计 stagnant，连续 N 轮 → 停
停止条件：target_met | stagnant | max_rounds
结束：累积行走 record_extraction；一次 DOM.getAXTree resync（恢复 id 缓存）
```

- stable_key 默认 href，可配置字段组合。
- 接口按泛化设计（mode: scroll | click_load_more | click_pagination | expand_sections），
  v1 只实现 scroll + click_load_more（共享同一收割引擎，覆盖绝大多数真实场景）。
  **不纳入 filter/search 类动作**：筛选改变数据集身份，跨筛选累积收割会混入不同集合。
- 支持 containerId 容器内滚动（scrollable div）。
- 遇 overlay → 内部调用 dismiss_overlay；blocked（auth/paywall）→ 整体 yield。
- 注意 memory 已知 quirk：reused tab 卡 25 cards → 工具说明里建议 fresh tab。

### 5.2 端到端基准（taaft 作为 golden baseline）

用 collect_items 复刻 taaft_abcp_extract 的 trending 列表抓取，
与 extract_trending_25_35.py 的 output/*.json 对比行数与关键字段——
这就是"taaft 当测试/验证集"的具体用法。

---

## Phase 6 — fill_field_verified（1–2 天）

```
click(target，经 allow_rematch 通道) → clear(Ctrl+A + Delete) → type
→ verify_field_value（value property 读回；matchCount>1 → 带候选 yield）
→ 不匹配 → 强力清除重试一次 → 仍败 → yield {expected, actual}
→ found=false → AXTree refresh fallback
→ occlusion → dismiss_overlay → 重试
```

测试：正常输入、React 受控、值不匹配重试、元素找不到 fallback、歧义字段 yield。

---

## Phase 7 — VL 坐标互证 + 自动拦截 + 端到端（2–3 天）

### 7.1 VL 互证

- vl.py 新增 overlay_classify mode（v4 设计照用）。
- 截图按遮罩 bounds 裁剪（来源：occluder_probe 的 rect 或 layers.boundsInRoot）。
- VL 返回坐标 → 用 Phase 0 的 DPR 系数回译 → elementFromPoint 安全验证 → 才允许点击。
- VL 仅在确定性梯子全败后调用（仲裁者，不在主路径）。

### 7.2 自动拦截层（被动触发）

- 仅 P0（errorClassification.occlusion_blocked）与 P1（layers frame occluded）允许自动跑
  dismiss_overlay；P2（overlay_detector 文本软检测）/P3（observation 关键词）只附建议不自动执行
  ——软文本信号有假阳性（讲 cookie 的文章会命中 "we use cookies"），自动点击不可接受。
- config 总开关 + per-loop 开关，默认 P0 自动、P1 自动、P2/P3 建议。

### 7.3 验收与灰度

- step 消耗基准：三类任务（遮罩、滚动收集、表单）有/无 micro-loop 对比。
- replay 测试：用现有 traces/ 回放，验证 micro-loop 减少 LLM steps 且未引入误动作。
- 安全审计清单：auth/paywall 零自动点击断言、NEVER_CLICK 覆盖、
  时长/次数硬上限、全动作可审计（trace）、敏感值掩码。
- 遥测进 strategy_telemetry：每 loop attempts/成功率/stop_reason 分布。
- 灰度顺序（每级稳定后才进下一级）：
  ① 默认关闭，仅 probe → ② 工具显式可调用 → ③ strategy 只建议不自动 →
  ④ occlusion 错误自动建议 dismiss_overlay → ⑤ composite_only rematch 开启 →
  ⑥ P0/P1 有限自动触发。

---

## 风险与回退

| 风险 | 缓解 |
|------|------|
| read_only_eval 豁免被误用于会改 DOM 的 JS | 通道仅 harness 内部 verifier 模板可用，模板列表白名单化 |
| 原生 rematch 点错同名元素 | _validate_rematch role/name 校验 + verifier 终态确认双保险 |
| 自动拦截误触发 | P2/P3 降级为建议；config 一键回 off |
| ABCP payload 形状与探针结论漂移 | Phase 0 facts 文档版本化；每次 ABCP 升级重跑 probe |
| 事件通道时序（事件先于/晚于 response 到达） | NotificationHub replay window 按 (pageId, 动作窗口) 归因 |
