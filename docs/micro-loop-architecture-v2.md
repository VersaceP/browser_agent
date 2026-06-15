# Browser Agent Micro-Loop 架构方案 v4

> 版本: v4.0  
> 日期: 2026-06-12  
> 状态: 设计阶段  
> 变更: 基于 taaft_abcp_extract 项目实践，将 verifier 从"AXTree id → CSS selector → oracle"改为"语义选择器直接在 JS 中定位 → oracle"；消除 canonical id 与 CSS selector 之间的推导难题

---

## 〇、核心认知演进

### v1→v2→v3→v4 的思路变化

| 版本 | Verifier 思路 | 问题 |
|------|-------------|------|
| v1 | 封装独立 verifier 工具 | 中间封装层增加出错面 |
| v2 | Runtime.evaluate oracle，假设 CSS selector 可用 | selector 从哪来？没回答 |
| v3 | pre-extract HTML 属性，或从 AXTree 推导 selector | 从 canonical id 推导 selector 不可靠；pre-extract 多一次 RPC |
| **v4** | **语义选择器在 JS 中直接定位，不需要 canonical id 做 selector** | **taaft 项目已验证此模式可行** |

### 关键洞察：taaft_abcp_extract 的做法

taaft 项目（`taaft_abcp_extract/`）是一个完整的 ABCP 浏览器爬虫，它**从不调 DOM.getAXTree，从不使用 canonical id 做 selector**：

```js
// 直接用语义选择器遍历 DOM
document.querySelectorAll('a[href]')                        // 找所有链接
document.querySelectorAll('h1,h2,h3,[role="heading"]')      // 找所有标题
document.querySelectorAll('[role="tab"]')                   // 找所有 tab

// 用业务逻辑过滤
anchors.filter(a => /\/ai\/[^/]+\/$/.test(a.href))          // URL 模式匹配产品
labels.find(l => l.textContent.includes('Email'))           // 用 label 文本找关联 input

// 用 DOM 结构遍历
nearestCard = el.parentElement.querySelector('a[href*="/ai/"]')  // 沿 DOM 树导航
```

**这证明了**：当你可以执行 JS 时，不需要"先拿 AXTree id，再转换成 CSS selector"。AXTree 的价值是**告诉 LLM 页面上有什么**（语义信息），不是**告诉 JS 怎么找元素**（定位方式）。

**核心转变**：
```
v3 思路：AXTree canonical id → 推导/提取 CSS selector → Runtime.evaluate
v4 思路：AXTree 语义信息（role + name）→ 生成语义定位 JS → Runtime.evaluate
```

---

## 一、Harness 与 ABCP 新机制冲突清单

> 基于 ABCP Playground stale-id 案例的实际行为，逐条列出当前 harness 代码与新机制的冲突。

### 1.1 冲突 1：`_check_stale_axtree_target` 阻断 auto-rematch（严重）

**ABCP 新行为**：`Input.click` 用旧 id（如 `4:26:26`）调用时，浏览器自动 rematch 到新 id（`4:65:65`），返回 `recoveredTarget: {previousId, currentId}`。

**Harness 现状**：`_check_stale_axtree_target`（line 2144-2178）在 harness 侧拦截所有不在 `agent.axtree_ids` 中的 id，返回 `stale_element_reference`，请求**根本到不了浏览器**，auto-rematch 永远不触发。

### 1.2 冲突 2：`AXTREE_INVALIDATING_METHODS` 过度作废（中等）

**Harness 现状**：`AXTREE_INVALIDATING_METHODS`（line 67-82）将所有 Input 动作和 `Runtime.evaluate` 都标记为"使 axtree invalid"，导致每次 action 后都必须 full refresh。

### 1.3 冲突 3：Rematch 结果被丢弃（中等）

**ABCP 新行为**：`Input.click` 返回 `recoveredTarget` 和 `suggested_prompt`。

**Harness 现状**：`_observe_axtree_state_after` 没有处理 `recoveredTarget` 字段。

### 1.4 冲突 4：事件通道未接入（基础缺失）

**Harness 现状**：`abcp_client.py` 有 Notification 基础设施，但 BrowserAgent 没有订阅 `DOM.axTreeUpdated` 事件。

### 1.5 冲突总结

| 冲突 | 严重度 | 解决方案 | 优先级 |
|------|--------|---------|--------|
| stale guard 阻断 auto-rematch | 🔴 严重 | `_check_stale_axtree_target` 增加 `allow_rematch` 参数 | P0 |
| rematch 结果被丢弃 | 🟡 中等 | `_observe_axtree_state_after` 处理 `recoveredTarget` | P0 |
| 事件通道未接入 | 🟡 中等 | 订阅 `DOM.axTreeUpdated`，增量更新 | P1 |
| 过度 invalidate | 🟡 中等 | 收到事件时用增量更新替代 full invalidate | P1 |

---

## 二、核心设计：语义定位 Verifier

### 2.1 为什么不需要"canonical id → CSS selector"的转换

传统思路假设 verifier 需要用 CSS selector 在 DOM 里定位元素，而 canonical id 不是 DOM id，所以需要转换。但 taaft 项目证明了：

1. **AXTree 的 role 和 name 本身就是语义定位器**——不需要转成 CSS selector
2. **JS 可以直接用语义匹配**——`querySelectorAll('[role="button"]')` + 文本过滤
3. **业务结构往往比 AXTree 更稳定**——URL 模式、DOM 层级、label-for 关联

### 2.2 语义定位 JS 模板

每个 verifier 场景对应一种 JS 定位策略，**都不需要 canonical id 参与**：

#### `overlay_gone` — 全局语义检测

```js
(() => {
  // 不需要知道任何特定元素 id
  // 直接查 DOM 里有没有 dialog
  const dialogs = document.querySelectorAll(
    '[role="dialog"], [role="alertdialog"], .modal, .overlay, [class*="modal" i]'
  );
  const visibleDialogs = Array.from(dialogs).filter(el => {
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  });
  return { 
    dialogCount: visibleDialogs.length, 
    hasOverlay: visibleDialogs.length > 0 
  };
})()
```

#### `items_grew` — 语义模式计数

```js
// composite tool 在调用前，从 AXTree 的 role 模式推导 JS 定位策略
// 不是推导一个 CSS selector 字符串，而是生成一段 JS 代码
//
// AXTree 中看到 20 个 role="listitem" → 生成：
(() => {
  const items = document.querySelectorAll('[role="listitem"], li');
  return { count: items.length };
})()

// AXTree 中看到 15 个 role="article" → 生成：
(() => {
  const items = document.querySelectorAll('article, [role="article"]');
  return { count: items.length };
})()

// AXTree 中看到重复的 a[href*="/product/"] → 生成：
(() => {
  const items = document.querySelectorAll('a[href*="/product/"]');
  return { count: items.length };
})()
```

#### `field_value` — 语义关联定位

**这是最关键的转变**。v3 方案用 pre-extract HTML 属性来获取 selector，v4 方案用语义关联在 JS 中直接定位：

```js
// 策略 1：通过 label 文本找关联 input（最可靠，label[for] → input[id]）
(() => {
  const labels = Array.from(document.querySelectorAll('label'));
  const target = labels.find(l => 
    l.textContent.toLowerCase().includes('email')
  );
  if (!target) return { value: null, found: false };
  const input = target.htmlFor 
    ? document.getElementById(target.htmlFor)
    : target.querySelector('input, textarea, select');
  return { 
    value: input?.value ?? '', 
    found: !!input,
    tagName: input?.tagName ?? '',
    type: input?.type ?? ''
  };
})()

// 策略 2：通过 aria-label 或 placeholder 定位
(() => {
  const input = document.querySelector(
    'input[aria-label*="email" i], input[placeholder*="email" i]'
  );
  return { value: input?.value ?? '', found: !!input };
})()

// 策略 3：通过 name 属性定位
(() => {
  const input = document.querySelector(
    'input[name="email"], input[name*="mail" i]'
  );
  return { value: input?.value ?? '', found: !!input };
})()

// 策略 4：通过 role + 位置关系（AXTree 语义的 DOM 投影）
(() => {
  // AXTree 告诉我们有一个 textbox，name="Email"
  // 在 DOM 中找 type=text 的 input，通过 label 或上下文关联
  const textboxes = Array.from(
    document.querySelectorAll('input[type="text"], input[type="email"], input:not([type])')
  );
  for (const input of textboxes) {
    const label = input.labels?.[0] 
      || input.closest('label')
      || input.getAttribute('aria-label')
      || input.getAttribute('placeholder');
    if (label && /email/i.test(label.textContent || label)) {
      return { value: input.value, found: true };
    }
  }
  return { value: null, found: false };
})()
```

### 2.3 Verifier 统一框架

```python
@dataclass
class SemanticLocator:
    """
    语义定位器：描述"怎么在 JS 里找到目标"，而不是"CSS selector 是什么"。
    
    从 AXTree 的 role + name + 上下文自动生成。
    不依赖 canonical id，不受 stale id 影响。
    """
    js_template: str              # JS 定位代码模板
    bindings: Dict[str, str]      # 模板变量绑定
    
    def render(self) -> str:
        """渲染为可执行的 JS expression"""
        return self.js_template.format(**self.bindings)


@dataclass
class VerifierResult:
    ok: bool
    evidence: JsonDict
    confidence: float        # 0.0 - 1.0
    method: str              # "semantic_oracle" | "axtree_refresh_fallback"


async def verify_with_semantic_oracle(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    locator: SemanticLocator,
    assertion: Callable[[Any], bool],
    fast_check: Optional[Callable[[], bool]] = None,
) -> VerifierResult:
    """
    通用语义 oracle 调用器。
    
    1. fast_check：从 AXTree 缓存快速判断（零 RPC）
    2. Runtime.evaluate(locator.render())：语义定位 + oracle 确认
    3. 如果 oracle 执行失败（元素不存在等）→ AXTree refresh fallback
    """
```

### 2.4 SemanticLocator 的生成

```python
def build_locator_from_axtree(
    *,
    target_role: str,
    target_name: str,
    context: JsonDict,          # AXTree 上下文（父节点、兄弟节点信息）
    verify_scenario: str,       # "overlay_gone" | "items_grew" | "field_value"
) -> SemanticLocator:
    """
    从 AXTree 语义信息生成 SemanticLocator。
    
    关键：这个函数的输入是 AXTree 的语义层（role, name, 上下文），
    输出是一段 JS 定位代码。不是 canonical id → selector 的映射。
    """
    
    if verify_scenario == "overlay_gone":
        return SemanticLocator(
            js_template=OVERLAY_GONE_JS,  # 全局检测，不需要绑定
            bindings={},
        )
    
    if verify_scenario == "items_grew":
        # 从 AXTree 的重复 role 模式推导 JS 选择策略
        role = target_role  # "listitem", "article", etc.
        return SemanticLocator(
            js_template=ITEMS_GREW_JS,
            bindings={"role": role},
        )
    
    if verify_scenario == "field_value":
        # 从 AXTree 的 name 生成 label 搜索关键词
        # name="Email Address" → 搜索关键词 "email"
        keywords = extract_keywords(target_name)
        return SemanticLocator(
            js_template=FIELD_VALUE_JS,
            bindings={"keywords": json.dumps(keywords)},
        )
```

### 2.5 事件观察策略：三层消费，不向 LLM 灌噪音

```
Layer 0: harness 内部消费（自动更新状态，LLM 完全无感）
  → DOM.axTreeUpdated 事件 → 增量更新 agent.axtree_ids / axtree_epoch
  → Input.click 返回的 recoveredTarget → 更新 axtree id 映射
  → Page.loaded / Page.recovered → 标记 axtree_invalidated
  → 这些事件只更新内部状态，永不注入 LLM context

Layer 1: composite tool 内部消费（loop 执行期间使用，loop 结束后丢弃）
  → overlay_dismiss 执行期间订阅页面变化事件，检测遮罩消失
  → scroll_collect 执行期间检测 scroll 定位事件
  → 这些事件的生命周期 = loop 执行周期，loop 结束后不保留

Layer 2: 结构化摘要（极少数事件提炼为结构化信息，注入 tool_result）
  → 只在 loop yield 回 LLM 时，附带一个精简的事件摘要
  → 例如：{ rematchedFrom: "11:26:26", rematchedTo: "11:28:28", 
            role: "button", name: "提交订单", verified: true }
  → 绝不注入原始事件 payload
```

**核心原则：事件是 harness 的内部信号，不是 LLM 的上下文。**

### 2.6 Stale Guard 集成冲突的解决方案

**三级策略，通过配置切换**：

```python
# harness/config.py 新增
browser_side_rematch: str = "composite_only"
# "off"           → 完全保留现有行为，stale guard 全拦截
# "composite_only" → 只在 composite tool 内部放行 stale id
# "on"            → 所有调用都放行（危险）
```

**`composite_only` 的安全逻辑**：

```
1. 普通 browser_call (LLM 发起) → 仍然走 stale guard
2. composite tool 内部 → 旁路 stale guard → 但 rematch 后校验 role/name 一致性
3. composite tool 本身的 verifier 用语义 oracle 做终态确认
   → 即使 rematch 匹配错目标，oracle 会发现"预期效果没发生"
```

---

## 三、三个 Verifier（语义 oracle）

### 3.1 `verify_overlay_gone` — 全局语义检测

**不需要任何元素定位**——查 DOM 里有没有 dialog 即可。

```python
OVERLAY_GONE_JS = r"""
(() => {
  const selectors = '[role="dialog"], [role="alertdialog"], .modal, [class*="modal" i], [class*="overlay" i]';
  const candidates = document.querySelectorAll(selectors);
  const visible = Array.from(candidates).filter(el => {
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  });
  return { dialogCount: visible.length, hasOverlay: visible.length > 0 };
})()
"""

async def verify_overlay_gone(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    original_overlay: JsonDict,
) -> VerifierResult:
    """
    Layer 0: 快速信号（AXTree 缓存 + layers occlusionState）
    Layer 1: 语义 oracle（全局检测 dialog 是否存在）
    """
```

### 3.2 `verify_items_grew` — 语义模式计数

**从 AXTree 的 role 模式生成 JS 定位代码**。

```python
ITEMS_GREW_JS = r"""
(() => {{
  const items = document.querySelectorAll('{role_selectors}');
  return {{ count: items.length }};
}})()
"""

async def verify_items_grew(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    target_role: str,           # AXTree 中的重复 role
    before_count: int,
) -> VerifierResult:
    """
    Layer 0: 快速信号（PageFingerprint delta）
    Layer 1: 语义 oracle（querySelectorAll 计数）
    
    role_selectors 映射：
      "listitem" → '[role="listitem"], li'
      "article"  → 'article, [role="article"]'
      "link"     → 'a[href]'
      默认        → '[role="{role}"]'
    """
```

### 3.3 `verify_field_value` — 语义关联定位

**从 AXTree 的 name 生成关键词，在 JS 中通过 label/placeholder/name/aria-label 多策略定位**。

```python
FIELD_VALUE_JS = r"""
(() => {{
  const keywords = {keywords};  // ["email", "邮箱"]
  
  // 策略 1：label[for] → input[id]
  for (const label of document.querySelectorAll('label')) {{
    const text = (label.textContent || '').toLowerCase();
    if (!keywords.some(kw => text.includes(kw))) continue;
    const input = label.htmlFor
      ? document.getElementById(label.htmlFor)
      : label.querySelector('input, textarea, select');
    if (input) return {{ value: input.value, found: true, strategy: 'label' }};
  }}
  
  // 策略 2：aria-label / placeholder
  for (const kw of keywords) {{
    const sel = `input[aria-label*="${{kw}}" i], input[placeholder*="${{kw}}" i], textarea[aria-label*="${{kw}}" i], textarea[placeholder*="${{kw}}" i]`;
    const input = document.querySelector(sel);
    if (input) return {{ value: input.value, found: true, strategy: 'aria_or_placeholder' }};
  }}
  
  // 策略 3：name 属性
  for (const kw of keywords) {{
    const input = document.querySelector(`input[name*="${{kw}}" i], textarea[name*="${{kw}}" i]`);
    if (input) return {{ value: input.value, found: true, strategy: 'name_attr' }};
  }}
  
  // 策略 4：input.labels 关联
  for (const input of document.querySelectorAll('input, textarea, select')) {{
    const labelTexts = Array.from(input.labels || []).map(l => l.textContent.toLowerCase());
    const ariaLabel = (input.getAttribute('aria-label') || '').toLowerCase();
    const placeholder = (input.getAttribute('placeholder') || '').toLowerCase();
    const allText = labelTexts.join(' ') + ' ' + ariaLabel + ' ' + placeholder;
    if (keywords.some(kw => allText.includes(kw))) {{
      return {{ value: input.value, found: true, strategy: 'input_labels' }};
    }}
  }}
  
  return {{ value: null, found: false, strategy: 'not_found' }};
}})()
"""

async def verify_field_value(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    target_name: str,           # AXTree 中的 name（如 "Email Address"）
    expected_value: str,
) -> VerifierResult:
    """
    Layer 0: 无快速信号（AXTree state 中的 value 不可靠）
    Layer 1: 语义 oracle（多策略定位 + .value 读回）
    Layer 2: 如果 oracle 返回 found=false → AXTree refresh + DOM.getText fallback
    """
```

**为什么这个方案比 pre-extract 更好**：

| 维度 | v3 pre-extract | v4 语义定位 |
|------|---------------|-----------|
| 额外 RPC | 需要 1-2 次 DOM.getAttribute | **0 次**——JS 内部完成所有定位 |
| stale 风险 | pre-extract 在 action 前，canonical id 必须有效 | **无**——不依赖 canonical id |
| 覆盖率 | 依赖元素有 HTML id/class | **高**——label、aria-label、placeholder、name 四种策略 |
| 代码复杂度 | composite tool 里插入 pre-extract 步骤 | **低**——verifier 自包含 |

### 3.4 AXTree layers 字段含义

`DOM.getAXTree` 返回的 `layers` 数组描述页面的帧布局和遮挡关系：

| 字段 | 含义 | 通俗解释 |
|------|------|---------|
| `frameId` | 帧编号 | 画框编号 |
| `isMainFrame` | 是否主页面 | 最外面的大画框（true）还是 iframe（false） |
| `parentFrameId` | 父帧编号 | 这个画框挂在哪个画框里（iframe 有父帧，主帧为 null） |
| `depth` | 嵌套深度 | 第几层画框（主帧=0，iframe=1，iframe 的 iframe=2...） |
| `url` | 帧加载的网址 | 画框里显示的网页地址 |
| `boundsInRoot` | 在根坐标系中的位置 | 画框在整面墙上的位置和大小 |
| `visibleBoundsInRoot` | 实际可见区域 | 你能看到的部分（被遮挡时比 boundsInRoot 小） |
| `visible` | 是否可见 | 画框是否可见 |
| `occlusionState: "visible"` | 遮挡状态 | `"visible"` = 没被挡住，`"occluded"` = 被挡住了 |
| `occludedByFrameIds` | 被谁挡住 | 哪些帧挡住了当前帧（overlay 出现时，主帧的此字段会列出 overlay 帧） |
| `viewportBounds` | 浏览器视口大小 | 你看到的那块区域 |

**对 verifier 的用途**：`overlay_gone` 快速信号检查 `occlusionState` 是否从 `"occluded"` 变回 `"visible"`。

---

## 四、三个 Composite Tool

### 4.1 `dismiss_overlay` — 遮罩自动处置

```python
@BROWSER_TOOLS.register(
    name="dismiss_overlay",
    description=(
        "Attempt to dismiss an overlay/modal that blocks a target action. "
        "Runs the dismiss ladder internally: find close control → click → "
        "verify → Escape → verify → verified backdrop click → verify. "
        "Returns structured result. Auth/paywall overlays are never auto-dismissed."
    ),
    input_schema=_browser_schema_for("dismiss_overlay"),
    contract_check=False,
    trace_type="dismiss_overlay",
)
async def _browser_dismiss_overlay(ctx: ToolContext) -> JsonDict:
    ...
```

**内部流程**：

```
dismiss_overlay 内部：
  1. refresh DOM.getAXTree
  2. 检查 layers.occlusionState + overlay_detector → 分类 overlay subtype
  3. auth_prompt / paywall → 立即返回 {status: "blocked", subtype: ...}
  4. 找 close/dismiss 控件 → click → verify_overlay_gone(语义oracle) → 通过则重试原始动作
  5. Escape → verify_overlay_gone(语义oracle) → 通过则重试原始动作
  6. eval_js_json(elementFromPoint) 验证 backdrop → click → verify_overlay_gone(语义oracle)
  7. 全失败 → 返回 {status: "failed", attempts: [...], hint: "..."}
```

**所有内部调用走 `_invoke_browser_method(count_progress=False)`**，不消耗 LLM step。

### 4.2 `scroll_collect` — 滚动式内容收集

```python
@BROWSER_TOOLS.register(
    name="scroll_collect",
    description=(
        "Scroll a page to collect repeated items until content stabilizes "
        "or a target count is reached. Uses semantic oracle for change detection."
    ),
    input_schema=_browser_schema_for("scroll_collect"),
    contract_check=True,
    trace_type="scroll_collect",
)
async def _browser_scroll_collect(ctx: ToolContext) -> JsonDict:
    ...
```

**内部流程**：

```
scroll_collect 内部：
  1. 初始 DOM.getAXTree → 分析 role 模式 → 生成 SemanticLocator
  2. 初始语义 oracle → baseline count
  3. 循环 (max_rounds):
     a. Input.scroll(direction)
     b. 等待 settle
     c. verify_items_grew(语义oracle: querySelectorAll 计数) → 确认是否有新内容
     d. 无增长 → stagnant_count += 1
     e. stagnant_count >= stability_threshold → 退出
     f. 有 overlay → 调用 dismiss_overlay
  4. 最终 DOM.getAXTree resync
  5. 自动 record_extraction
  6. 返回 {status, stopReason, rounds, rowCount, ...}
```

### 4.3 `fill_field_verified` — 带验证的表单字段填写

```python
@BROWSER_TOOLS.register(
    name="fill_field_verified",
    description=(
        "Type a value into a field and verify it was accepted. "
        "Uses semantic oracle (label/placeholder/name matching) for verification. "
        "Auto-clears and retries once on mismatch."
    ),
    input_schema=_browser_schema_for("fill_field_verified"),
    contract_check=True,
    trace_type="fill_field_verified",
)
async def _browser_fill_field_verified(ctx: ToolContext) -> JsonDict:
    ...
```

**内部流程**：

```
fill_field_verified 内部：
  1. 从 AXTree 缓存获取 target 的 role + name
  2. 生成 SemanticLocator（用 name 提取关键词 → label/placeholder 定位策略）
  3. click(target_id) 聚焦（允许 stale id → 触发浏览器 auto-rematch）
  4. clear: Ctrl+A → Delete
  5. type(text)
  6. verify_field_value(语义oracle)
     → 找到元素 + value 匹配 → 完成
     → 找到元素 + value 不匹配 → 重试一次（更强力的清除）
     → 找不到元素 → AXTree refresh + DOM.getText fallback
  7. 如有 occlusion → 调用 dismiss_overlay
```

---

## 五、Stale Guard + Browser Rematch 集成

### 5.1 修正方案

```python
def _check_stale_axtree_target(
    agent: Any,
    method: str,
    params: JsonDict,
    *,
    allow_rematch: bool = False,   # ← 新增参数
) -> Optional[JsonDict]:
    """
    allow_rematch=True 时：
      - 同 pageId + id 曾经见过但不在当前 snapshot → 放行给浏览器
      - pageId mismatch 或完全没见过的 id → 仍然拦截
    allow_rematch=False（默认）：
      - 完全保留现有行为
    """
    ...
    missing = sorted(target_ids - current_ids)
    
    if allow_rematch and not invalidated:
        if page_id and current_page_id and page_id != current_page_id:
            pass  # page mismatch 仍然拦截
        elif missing:
            return None  # 放行，让请求到浏览器
    ...
```

### 5.2 Rematch 结果处理（两个入口）

**入口 1：response 中的 `recoveredTarget`**

```python
def _observe_axtree_state_after(agent, method, params, result, ...):
    # ... 现有逻辑 ...
    
    data = _response_data(result)
    recovered = data.get("recoveredTarget")
    if recovered and isinstance(recovered, dict):
        previous_id = str(recovered.get("previousId", ""))
        current_id = str(recovered.get("currentId", ""))
        if previous_id and current_id:
            if _validate_rematch(agent, previous_id, current_id, result):
                agent.axtree_ids.discard(previous_id)
                agent.axtree_ids.add(current_id)
            else:
                agent.axtree_invalidated = True
```

**入口 2：事件通道 `DOM.axTreeUpdated` → 增量更新**

```python
async def _handle_axtree_updated_event(agent, event):
    payload = event.get("data", {})
    
    # 事件携带完整新 AXTree
    if payload.get("lines"):
        ids = _axtree_ids_from_value(payload)
        agent.axtree_ids = ids
        agent.axtree_epoch += 1
        agent.axtree_invalidated = False
        return
    
    # 事件携带 id 映射
    remappings = payload.get("remappings")
    if remappings:
        for r in remappings:
            old_id = str(r.get("oldId", ""))
            new_id = str(r.get("newId", ""))
            if old_id in agent.axtree_ids:
                if _validate_rematch_from_event(agent, r):
                    agent.axtree_ids.discard(old_id)
                    agent.axtree_ids.add(new_id)
                else:
                    agent.axtree_invalidated = True
                    break
        return
    
    # 事件无数据，标记需刷新
    agent.axtree_invalidated = True
```

### 5.3 Rematch 校验

```python
def _validate_rematch(agent, previous_id, current_id, result) -> bool:
    """rematched 节点的 role 和 name 必须与原目标一致"""
    original = _find_node_in_cache(agent, previous_id)
    rematched = _find_rematched_node_in_result(result, current_id)
    
    if not original or not rematched:
        return False
    
    return (
        rematched.get("role") == original.get("role")
        and rematched.get("name", "").strip().lower() 
            == original.get("name", "").strip().lower()
    )
```

---

## 六、事件消费策略

### 6.1 事件分类

| 事件类型 | 消费方式 | 是否注入 LLM |
|---------|---------|-------------|
| `DOM.axTreeUpdated` | 更新 `agent.axtree_ids` + `axtree_epoch` | ❌ |
| `Input.click` 返回的 `recoveredTarget` | 更新 `agent.axtree_ids` | ❌ |
| `Page.loaded` | 标记 `axtree_invalidated = True` | ❌ |
| `Page.recovered` | 触发 render_recovery | ❌ |
| `Page.dialogOpened` | composite tool 内部使用 | ❌ |
| `Hitl.resumeEvent` | 触发 HITL wait 流程 | ❌ |
| 其他未知事件 | 忽略 | ❌ |

### 6.2 事件 → LLM 的唯一路径

事件只能通过 **composite tool 的结构化返回值** 间接到达 LLM：

```json
{
    "status": "yielded",
    "rematch": {
        "originalId": "11:26:26",
        "rematchedTo": "11:28:28", 
        "role": "button",
        "name": "提交订单",
        "consistent": true
    },
    "yieldHint": "..."
}
```

---

## 七、实施计划

### Phase 0: ABCP 事件能力确认（0.5 天，并行）

- 用 `System.describeEvent` 确认 `DOM.axTreeUpdated` 的 payload 格式
- 确认 `recoveredTarget` 的触发条件和返回值格式
- 确认 `Runtime.evaluate` 在 ABCP 下的 `returnByValue: true` 行为
- 写 probe 脚本验证，不阻塞其他 phase

### Phase 1: 语义 Oracle 框架 + Verifier（1-2 天，最高优先级）

新建 `harness/observation/verifiers.py`：

```python
@dataclass
class SemanticLocator:
    js_template: str
    bindings: Dict[str, str]
    def render(self) -> str: ...

@dataclass
class VerifierResult:
    ok: bool
    evidence: JsonDict
    confidence: float
    method: str  # "semantic_oracle" | "axtree_refresh_fallback"

async def verify_with_semantic_oracle(...) -> VerifierResult: ...

async def verify_overlay_gone(...) -> VerifierResult:
    """OVERLAY_GONE_JS：全局检测 dialog"""

async def verify_items_grew(...) -> VerifierResult:
    """ITEMS_GREW_JS：语义模式计数"""

async def verify_field_value(...) -> VerifierResult:
    """FIELD_VALUE_JS：语义关联定位 + .value 读回"""

def build_locator_from_axtree(...) -> SemanticLocator:
    """从 AXTree 语义信息生成 SemanticLocator"""
```

测试用例：
- `test_overlay_gone`: mock ABCP client 返回不同 dialog 状态
- `test_items_grew`: mock 返回不同 count
- `test_field_value`: mock 返回不同 value（含 React 受控组件、label/placeholder/name 定位）
- `test_field_value_not_found`: oracle 找不到元素 → fallback 到 AXTree refresh
- `test_oracle_error_handling`: Runtime.evaluate 抛异常时的行为

### Phase 2: Stale Guard 修正 + Rematch 处理（1-2 天，P0）

1. 修改 `_check_stale_axtree_target` 增加 `allow_rematch` 参数
2. 修改 `_observe_axtree_state_after` 处理 `recoveredTarget` 字段
3. 实现 `_validate_rematch` 校验
4. 配置项：`browser_side_rematch: off|composite_only|on`
5. 写测试

### Phase 3: 事件通道接入（1-2 天，P1）

1. 订阅 `DOM.axTreeUpdated` 事件
2. 实现 `_handle_axtree_updated_event` 增量更新逻辑
3. 连接到现有 `_observe_axtree_state_after` 流程
4. 写测试

### Phase 4: dismiss_overlay composite tool（2-3 天）

1. 注册为 BROWSER_TOOLS
2. 内部梯子 + verify_overlay_gone(语义oracle)
3. 修改 `_attach_runtime_strategy_hints`
4. 写测试

### Phase 5: scroll_collect composite tool（2-3 天）

1. 注册为 BROWSER_TOOLS
2. 内部循环 + verify_items_grew(语义oracle)
3. overlay 触发时内部调用 dismiss_overlay
4. 写测试

### Phase 6: fill_field_verified composite tool（1-2 天）

1. 注册为 BROWSER_TOOLS
2. 内部流程 + verify_field_value(语义oracle)
3. 写测试：正常输入、React 受控组件、值不匹配、元素找不到→fallback

### Phase 7: 自动拦截层 + 端到端验证（1-2 天）

1. 可选的自动拦截
2. 端到端测试
3. 性能基准测试
4. 遥测

---

## 八、版本差异总结

| 维度 | v3 方案 | v4 方案 | 修正原因 |
|------|---------|---------|---------|
| 定位策略 | canonical id → CSS selector → oracle | **AXTree 语义 → 语义定位 JS → oracle** | taaft 项目证明：JS 里直接用语义选择器，不需要 canonical id 做 selector |
| selector 来源 | pre-extract HTML 属性 或 AXTree role 推导 | **JS 内部多策略定位（label/placeholder/name/aria-label）** | 不依赖 HTML id/class 存在，覆盖率更高 |
| field_value | pre-extract 2次额外 RPC + oracle | **0 额外 RPC，oracle 内部完成定位** | 减少调用次数，简化 composite tool 逻辑 |
| field_value fallback | 无 selector 时 AXTree refresh | **oracle 返回 found=false 时 AXTree refresh** | fallback 条件更精确：只在 JS 找不到元素时才 fallback |
| oracle 可行性 | 按 selector 可得性分级 | **所有场景都可行**——语义定位不需要 selector | 消除了"selector 从哪来"这个根本问题 |
| AXTree 的角色 | 提供 canonical id + 推导 selector | **提供语义信息（role + name），不参与定位** | AXTree 是 LLM 的页面地图，不是 JS 的定位工具 |
| stale 影响 | pre-extract 在 action 前必须 canonical id 有效 | **语义定位完全绕过 canonical id，stale id 不影响 oracle** | verifier 与 stale id 解耦 |
