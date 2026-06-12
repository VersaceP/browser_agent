# Browser Agent Micro-Loop 架构方案 v3

> 版本: v3.0  
> 日期: 2026-06-12  
> 状态: 设计阶段  
> 变更: 基于 ABCP Playground stale-id 案例验证，重写 verifier 策略为 Runtime.evaluate oracle 模式；明确 harness 与 ABCP 新机制的冲突清单及解法

---

## 〇、三方观点对齐

### 我认同的批判

| 批判来源 | 观点 | 我的判断 |
|---------|------|---------|
| Claude | **重心应该是 verifier，不是 loop 骨架** | ✅ 完全正确。loop 是壳，verifier 是核。coding loop 强因为编译器/测试是廉价确定性 oracle，browser loop 的上限取决于"遮罩是否关闭了"这个判定器的质量 |
| Claude | **前置分类器（根据初始 axtree 判断进入哪个 loop）不对** | ✅ 正确。初态没有遮罩、表单在第二步才出现。正确触发模型是失败信号触发，不是初态分类 |
| Claude | **每次滚动 full AXTree 太贵** | ✅ 正确。应该用增量信号（extract_dom_records 行数 + PageFingerprint stagnation_key），只在 loop 结束时刷新一次完整 AXTree |
| Claude | **stale guard 会阻断浏览器 auto-rematch** | ✅ 这是最关键的集成冲突。`_check_stale_axtree_target` 在 line 2173 设置 `tool_was_executed: False`，请求到不了浏览器 |
| ChatGPT | **AXTree 不等于视觉真相** | ✅ 正确。verifier 必须组合多信号，不能只靠 AXTree |
| ChatGPT | **"点击遮罩外"必须是最后手段** | ✅ 与我方案一致，且必须 elementFromPoint 验证 |
| 用户 | **事件观察返回什么、何时返回给 LLM、会不会是噪音** | ✅ 核心疑虑。需要明确事件消费策略 |

### 我不完全认同的批判

| 批判来源 | 观点 | 我的判断 |
|---------|------|---------|
| Claude | **Phase 0 先用 probe 脚本确认 ABCP 事件** | ⚠️ 方向对，但不能阻塞其他 phase。Phase 0 应该和 Phase 1 并行 |
| ChatGPT | **新建 BrowserEventObserver** | ⚠️ 过度设计。NotificationHub 已有订阅能力，只需要加一个薄消费层，不需要新类 |

---

## 一、Harness 与 ABCP 新机制冲突清单

> 基于 ABCP Playground stale-id 案例的实际行为，逐条列出当前 harness 代码与新机制的冲突。

### 1.1 冲突 1：`_check_stale_axtree_target` 阻断 auto-rematch（严重）

**ABCP 新行为**：`Input.click` 用旧 id（如 `4:26:26`）调用时，浏览器自动 rematch 到新 id（`4:65:65`），返回 `recoveredTarget: {previousId, currentId}`。

**Harness 现状**：`_check_stale_axtree_target`（line 2144-2178）在 harness 侧拦截所有不在 `agent.axtree_ids` 中的 id，返回 `stale_element_reference`，请求**根本到不了浏览器**，auto-rematch 永远不触发。

**冲突本质**：harness 的"保守安全"策略完全否定了浏览器端的新能力。

### 1.2 冲突 2：`AXTREE_INVALIDATING_METHODS` 过度作废（中等）

**ABCP 新行为**：`Input.click` 替换 DOM 后，浏览器会发送 `DOM.axTreeUpdated` 事件，携带完整的更新后 AXTree 或 rematch 映射，agent 不需要主动刷新。

**Harness 现状**：`AXTREE_INVALIDATING_METHODS`（line 67-82）将 `Input.click`、`Input.type`、`Input.scroll` 等**所有 Input 动作**以及 `Runtime.evaluate` 都标记为"使 axtree invalid"，导致下一次任何调用都必须先 `DOM.getAXTree`。

**冲突本质**：harness 不知道有事件通道可以增量更新，每次都做 full invalidate + full refresh，浪费大量 token。

### 1.3 冲突 3：Rematch 结果被丢弃（中等）

**ABCP 新行为**：`Input.click` 返回的 `recoveredTarget` 告知旧 id → 新 id 映射，`suggested_prompt` 也提示"不要再用旧 id"。

**Harness 现状**：`_observe_axtree_state_after`（line 2197-2235）只处理 `DOM.getAXTree` 返回的全量 id 集合和 `AXTREE_INVALIDATING_METHODS` 的 invalidate。**没有处理 response 中的 `recoveredTarget` 字段**，rematch 信息被完全忽略。

### 1.4 冲突 4：事件通道未接入（基础缺失）

**ABCP 新行为**：`DOM.axTreeUpdated` 事件通过 Notification 推送，包含更新后的 AXTree 数据或 id 映射。

**Harness 现状**：`abcp_client.py` 有 Notification 基础设施（line 42），但 `BrowserAgent` 没有订阅 `DOM.axTreeUpdated` 事件。当前事件消费只限于 HITL 相关。

### 1.5 冲突总结与解决优先级

| 冲突 | 严重度 | 解决方案 | 优先级 |
|------|--------|---------|--------|
| stale guard 阻断 auto-rematch | 🔴 严重 | `_check_stale_axtree_target` 增加 `allow_rematch` 参数 | P0 |
| rematch 结果被丢弃 | 🟡 中等 | `_observe_axtree_state_after` 处理 `recoveredTarget` | P0 |
| 事件通道未接入 | 🟡 中等 | 订阅 `DOM.axTreeUpdated`，增量更新 `axtree_ids` | P1 |
| 过度 invalidate | 🟡 中等 | 收到事件时用增量更新替代 full invalidate | P1 |

---

## 二、核心设计修正

### 2.1 从 "封装 verifier 工具" 改为 "Runtime.evaluate 原生 oracle"

**关键洞察**（来自 ABCP Playground 案例）：两个 stale-id 测试案例最后都用 `Runtime.evaluate` 直接读 DOM 作为 ground truth 验证，这比我们自己封装一个 `verify_overlay_gone`/`verify_field_value` 工具更可靠，原因：

```
1. Runtime.evaluate 读的是 DOM property（.value, .textContent），这是最终真相
   - 我们自己封装的 verifier 要么调 DOM.getText/DOM.getAttribute（有 stale 风险）
   - 要么自己又调 Runtime.evaluate（那为什么不直接调？）
   - 中间封装层增加了出错面，没有增加确定性

2. Runtime.evaluate 不受 axtree 缓存影响
   - DOM.getText/DOM.getAttribute 用 canonical id，有 stale 风险
   - Runtime.evaluate 用 CSS selector 或 getElementById，绕过整个 axtree 缓存层
   - 这意味着 verifier 可以在 axtree 可能过时的状态下仍然可靠工作

3. Playground 的验证模式已被 ABCP 团队验证
   - 他们用 Runtime.evaluate 做 stale-id recovery 的端到端验证
   - 说明 ABCP 原生支持且鼓励这种方式

4. 复合信号（AXTree + property + event）的组合仍然需要
   - 不是"只用 Runtime.evaluate"，而是"以 Runtime.evaluate 为最终 oracle"
   - AXTree 信号用于快速判断（overlay 是否还在），Runtime.evaluate 用于终态确认
```

**修正后的 verifier 策略**：

```python
async def verify_with_runtime_oracle(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    expression: str,        # Runtime.evaluate 表达式
    expected_check: str,    # 用于 assert 的描述
    fast_signals: Optional[JsonDict] = None,  # AXTree 等快速信号，可选
) -> VerifierResult:
    """
    以 Runtime.evaluate 为 oracle 的验证模式。
    
    1. 先检查 fast_signals（如果有）——AXTree 层面的快速判断
       → 通过 → 标记 confidence=0.7，继续进入 oracle 确认
       → 不通过 → confidence=0.0，直接进 oracle（AXTree 可能缓存过时）
    2. 调 Runtime.evaluate(expression, returnByValue=True)
       → 返回符合预期 → confidence=1.0, ok=True
       → 不符合预期 → confidence=0.0, ok=False
       → 执行异常 → confidence=0.0, ok=False, evidence 包含错误
    """
```

**对三个场景的具体应用**：

```
overlay_gone:
  expression: (() => {
    const overlay = document.querySelector('[role="dialog"], [role="alertdialog"]');
    const blocked = document.getElementById('blocked-target-id');
    return { 
      hasOverlay: overlay !== null,
      blockedVisible: blocked ? getComputedStyle(blocked).visibility : 'gone' 
    };
  })()
  fast_signals: AXTree 中 dialog role 是否消失 + layers occlusionState

field_value:
  expression: (() => {
    const el = document.querySelector('selector-for-target');
    return { value: el?.value ?? '', textContent: el?.textContent ?? '' };
  })()
  fast_signals: AXTree state 中的 value 字段

items_grew:
  expression: (() => {
    const rows = document.querySelectorAll('item-selector');
    return { count: rows.length, lastText: rows[rows.length-1]?.textContent ?? '' };
  })()
  fast_signals: extract_dom_records 行数 + PageFingerprint
```

### 2.2 事件观察策略：三层消费，不向 LLM 灌噪音

用户的核心疑虑是对的：事件通道不是"所有事件都喂给 LLM"。应该分三层：

```
Layer 0: harness 内部消费（自动更新状态，LLM 完全无感）
  → DOM.axTreeUpdated 事件 → 增量更新 agent.axtree_ids / axtree_epoch
  → Input.click 返回的 recoveredTarget → 更新 axree id 映射
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

**核心原则：事件是 harness 的内部信号，不是 LLM 的上下文。** 只有当事件产生了需要 LLM 知道的结论时，才以结构化摘要形式出现。

### 2.3 Stale Guard 集成冲突的解决方案

**解决方案：三级策略，通过配置切换**

```python
# harness/config.py 新增
browser_side_rematch: str = "composite_only"
# "off"           → 完全保留现有行为，stale guard 全拦截
# "composite_only" → 只在 composite tool (dismiss_overlay/scroll_collect/fill_field_verified) 
#                     内部放行 stale id 到浏览器，普通 browser_call 仍拦截
# "on"            → 所有调用都放行 stale id 到浏览器（危险，rematch 可能匹配错目标）
```

**`composite_only` 的安全逻辑**：

```
1. 普通 browser_call (LLM 发起) → 仍然走 stale guard，安全第一
2. composite tool 内部的 browser_call → 旁路 stale guard
   → 但 rematch 后必须校验：rematched 节点的 role/name 是否与原目标一致
   → 一致 → 接受 rematch 结果，更新 axtree_ids
   → 不一致 → 按 stale 处理，刷新 AXTree 后重试
3. composite tool 本身的 verifier 用 Runtime.evaluate oracle 做终态确认
   → 即使 rematch 匹配了错误目标，oracle 会发现"预期效果没发生"
   → 然后走正常的恢复路径
```

---

## 三、三个 Verifier（以 Runtime.evaluate 为 oracle）

### 3.1 `verify_overlay_gone` — 遮罩是否消失

```python
async def verify_overlay_gone(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    original_overlay: JsonDict,      # 之前检测到的 overlay 信息
    blocked_target_selector: str,     # 被遮挡元素的 CSS selector（用于 oracle）
) -> VerifierResult:
    """
    判定遮罩是否已消失。
    
    第一层：快速信号（AXTree）
    - dialog/alertdialog role 是否消失
    - layers occlusionState 是否恢复为 visible
    
    第二层：Runtime.evaluate oracle（终态确认）
    - document.querySelector('[role="dialog"], [role="alertdialog"]') === null
    - 被挡元素 getComputedStyle.visibility !== 'hidden'
    
    快速信号通过 → confidence=0.7 → 进 oracle 确认 → confidence=1.0
    快速信号未通过 → 仍进 oracle（AXTree 可能缓存过时）→ oracle 通过则 ok=True
    oracle 也未通过 → ok=False
    """
```

**为什么两层**：AXTree 快速信号可提前跳过 oracle（节省一次 RPC），但 AXTree 可能缓存过时（如 close 动画还没完成），所以最终以 oracle 为准。

### 3.2 `verify_items_grew` — 滚动后是否有新内容

```python
async def verify_items_grew(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    item_selector: str,              # 重复项的 CSS selector
    before_count: int,               # 之前的 item 数量
    before_fingerprint: Optional[PageFingerprint] = None,  # 可选快速信号
) -> VerifierResult:
    """
    判定滚动后是否有新内容。
    
    第一层：快速信号（PageFingerprint + extract_dom_records 行数）
    - physical_ids 数量是否增长
    - semantic_counts 是否变化
    - 这些在 composite tool 内部可零成本获取
    
    第二层：Runtime.evaluate oracle（终态确认）
    - document.querySelectorAll(item_selector).length > before_count
    
    快速信号有增长 → confidence=0.7 → 进 oracle 确认
    快速信号无增长 → 仍进 oracle（可能 lazy load 延迟）→ oracle 确认
    """
```

**注意**：`item_selector` 由 composite tool 内部从 AXTree 推导，不需要 LLM 提供。

### 3.3 `verify_field_value` — 表单值是否正确写入

```python
async def verify_field_value(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    target_selector: str,            # 目标元素的 CSS selector
    expected_value: str,
) -> VerifierResult:
    """
    判定表单值是否正确写入。只用 Runtime.evaluate oracle。
    
    为什么不需要快速信号层：
    - AXTree state 中的 value 字段可能滞后（React 受控组件）
    - DOM.getAttribute("value") 在 React 下不随输入更新
    - 只有 DOM property (.value) 是最终真相
    - 所以直接走 oracle，不做中间层判断
    
    Runtime.evaluate:
      (() => {
        const el = document.querySelector('target_selector');
        return { 
          value: el?.value ?? '', 
          tagName: el?.tagName ?? '',
          type: el?.type ?? ''
        };
      })()
    
    校验：el.value === expected_value
    """
```

**关键**：这个 verifier 只用 Runtime.evaluate，因为 DOM property 是唯一可靠真相源。AXTree state 和 DOM attribute 都不可靠（React/Vue 受控组件下）。

### 3.4 `verify_with_runtime_oracle` — 通用 oracle 调用器

```python
@dataclass
class VerifierResult:
    ok: bool
    evidence: JsonDict       # oracle 返回的原始数据
    confidence: float        # 0.0 - 1.0, oracle 通过=1.0
    method: str              # "runtime_oracle" | "fast_signal_only"

async def verify_with_runtime_oracle(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    expression: str,
    assertion: Callable[[Any], bool],  # 对返回值的断言函数
    fast_check: Optional[Callable[[], bool]] = None,  # 可选快速信号
) -> VerifierResult:
    """
    通用 Runtime.evaluate oracle 调用器。
    
    所有具体 verifier 都走这个入口，统一错误处理和日志。
    """
```

这样做的好处：
- 具体 verifier 只负责构造 expression 和 assertion
- oracle 调用、错误处理、日志、trace 全部集中在 `verify_with_runtime_oracle`
- 新增场景只需写 expression + assertion，不需要新的验证框架

---

## 四、三个 Composite Tool（按 navigate_verified 模式）

### 4.1 `dismiss_overlay` — 遮罩自动处置

**注册为 BROWSER_TOOLS composite tool，参照 navigate_verified 模式。**

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

**触发方式有两种**：

1. **LLM 主动调用**（当模型看到 occlusion 错误时）：模型调用 `dismiss_overlay(pageId, blockedTargetId)`
2. **自动拦截**（可选，通过配置开启）：在 `_execute_browser_capability_tool` 返回 occlusion 错误时自动触发

**推荐初期只做方式 1**，方式 2 需要更多测试后再开启。

**内部流程**（用 Runtime.evaluate oracle 判定）：

```
dismiss_overlay 内部：
  1. refresh DOM.getAXTree
  2. 检查 layers.occlusionState + overlay_detector → 分类 overlay subtype
  3. auth_prompt / paywall → 立即返回 {status: "blocked", subtype: ...}
  4. 从 AXTree 推导 blocked_target_selector（用 id 反查 selector）
  5. 找 close/dismiss 控件 → click → verify_overlay_gone(oracle) → 通过则重试原始动作
  6. Escape → verify_overlay_gone(oracle) → 通过则重试原始动作
  7. eval_js_json(elementFromPoint) 验证 backdrop → click → verify_overlay_gone(oracle)
  8. 全失败 → 返回 {status: "failed", attempts: [...], hint: "..."}
```

**所有内部调用走 `_invoke_browser_method(count_progress=False)`**，与 navigate_verified 一致，不消耗 LLM step。

### 4.2 `scroll_collect` — 滚动式内容收集

```python
@BROWSER_TOOLS.register(
    name="scroll_collect",
    description=(
        "Scroll a page to collect repeated items until content stabilizes "
        "or a target count is reached. Uses incremental change detection "
        "(fingerprint delta + row count) instead of full AXTree per scroll. "
        "Resyncs full AXTree once at the end."
    ),
    input_schema=_browser_schema_for("scroll_collect"),
    contract_check=True,
    trace_type="scroll_collect",
)
async def _browser_scroll_collect(ctx: ToolContext) -> JsonDict:
    ...
```

**关键修正（vs v2 方案）**：

- **不再每次滚动后调 full DOM.getAXTree**。改用 `extract_dom_records` 行数 + `PageFingerprint` stagnation_key 作为增量信号
- 只在 loop 结束时调一次 `DOM.getAXTree` 做 resync
- 内部用 `verify_items_grew(oracle)` 判定是否继续滚动
- oracle 用 `document.querySelectorAll(item_selector).length` 做 ground truth

```
scroll_collect 内部：
  1. 初始 DOM.getAXTree → baseline fingerprint + extract_dom_records 行数 + item_selector 推导
  2. 循环 (max_rounds):
     a. Input.scroll(direction)
     b. 等待 settle（300-500ms 或 Page.getState 检查 loading 状态）
     c. verify_items_grew(oracle: querySelectorAll.length) → 确认是否有新内容
     d. 无增长 → stagnant_count += 1
     e. stagnant_count >= stability_threshold → 退出
     f. 有 overlay → 调用 dismiss_overlay（内部 composite 调用）
  3. 最终 DOM.getAXTree resync
  4. 自动 record_extraction
  5. 返回 {status, stopReason, rounds, rowCount, finalAXTreePath, ...}
```

### 4.3 `fill_field_verified` — 带验证的表单字段填写

```python
@BROWSER_TOOLS.register(
    name="fill_field_verified",
    description=(
        "Type a value into a field and verify it was accepted. "
        "Uses Runtime.evaluate (.value property readback) as oracle. "
        "Auto-clears and retries once on mismatch. "
        "For complex form workflows, call this per field."
    ),
    input_schema=_browser_schema_for("fill_field_verified"),
    contract_check=True,
    trace_type="fill_field_verified",
)
async def _browser_fill_field_verified(ctx: ToolContext) -> JsonDict:
    ...
```

**最小原子操作**：只管一个字段的 输入→oracle验证→重试，不管整个表单流程。

```
fill_field_verified 内部：
  1. click(target_id) 聚焦（允许 stale id → 触发浏览器 auto-rematch）
  2. clear: Ctrl+A → Delete
  3. type(text)
  4. verify_field_value(oracle: Runtime.evaluate el.value)
     → 匹配 → 完成
     → 不匹配 → 重试一次（更强力的清除：eval_js target.value=""）
     → 仍不匹配 → 返回 {status: "mismatch", expected, actual}
  5. 如有 occlusion → 调用 dismiss_overlay
```

**注意**：step 1 允许 stale id 放行到浏览器，因为 composite tool 内部开启了 `allow_rematch=True`。如果浏览器 auto-rematch 成功，后续 type 和 verify 都用新 id。

---

## 五、Stale Guard + Browser Rematch 集成

### 5.1 当前冲突

```python
# browser_tools.py line 2138-2179
def _check_stale_axtree_target(agent, method, params):
    # 当 target id 不在 current axtree_ids 中时
    # 直接返回 stale_element_reference, tool_was_executed: False
    # 请求永远到不了浏览器 → ABCP 的 auto-rematch 永远不触发
```

### 5.2 修正方案

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
      - 结果中如包含 rematch 信息 → 校验 role/name 一致性
    allow_rematch=False（默认）：
      - 完全保留现有行为
    """
    ...
    missing = sorted(target_ids - current_ids)
    
    # 新增：composite tool 内部放行
    if allow_rematch and not invalidated:
        if page_id and current_page_id and page_id != current_page_id:
            # page mismatch 仍然拦截，太危险
            pass
        elif missing:
            # 放行，但标记需要 rematch 校验
            return None  # 不拦截，让请求到浏览器
    ...
```

**composite tool 内部调用时传入 `allow_rematch=True`**：

```python
# 在 composite tool 内部调用 _invoke_browser_method 时
# 临时设置 agent._allow_stale_rematch = True
# _check_stale_axtree_target 检查此标志
```

### 5.3 Rematch 结果处理（两个入口）

**入口 1：`_observe_axtree_state_after` 处理 response 中的 `recoveredTarget`**

```python
def _observe_axtree_state_after(agent, method, params, result, ...):
    # ... 现有逻辑 ...
    
    # 新增：处理 Input.click 等返回的 recoveredTarget
    data = _response_data(result)
    recovered = data.get("recoveredTarget")
    if recovered and isinstance(recovered, dict):
        previous_id = str(recovered.get("previousId", ""))
        current_id = str(recovered.get("currentId", ""))
        if previous_id and current_id:
            # 校验 role/name 一致性
            if _validate_rematch(agent, previous_id, current_id, result):
                # 更新 axtree_ids：移除旧 id，加入新 id
                agent.axtree_ids.discard(previous_id)
                agent.axtree_ids.add(current_id)
                agent.logger.write("axtree.rematch_accepted", {
                    "previousId": previous_id,
                    "currentId": current_id,
                    "method": method,
                })
            else:
                # rematch 不一致，标记 axtree 需要刷新
                agent.axtree_invalidated = True
                agent.logger.write("axtree.rematch_rejected", {
                    "previousId": previous_id,
                    "currentId": current_id,
                    "reason": "role_name_mismatch",
                })
```

**入口 2：事件通道 `DOM.axTreeUpdated` → 增量更新 `axtree_ids`**

```python
# 新增：在 NotificationHub 消费逻辑中
async def _handle_axtree_updated_event(agent, event):
    """
    Layer 0 消费：只更新内部状态，不注入 LLM context
    """
    payload = event.get("data", {})
    
    # 如果事件携带了完整的新 AXTree
    if payload.get("lines"):
        ids = _axtree_ids_from_value(payload)
        agent.axtree_ids = ids
        agent.axtree_epoch += 1
        agent.axtree_invalidated = False
        agent.logger.write("axtree.event_updated", {
            "source": "DOM.axTreeUpdated",
            "idCount": len(ids),
        })
        return
    
    # 如果事件只携带了 id 映射（rematch）
    remappings = payload.get("remappings")  # [{oldId, newId, role, name}]
    if remappings:
        for r in remappings:
            old_id = str(r.get("oldId", ""))
            new_id = str(r.get("newId", ""))
            if old_id in agent.axtree_ids:
                if _validate_rematch_from_event(agent, r):
                    agent.axtree_ids.discard(old_id)
                    agent.axtree_ids.add(new_id)
                else:
                    # 映射不一致，需要 full refresh
                    agent.axtree_invalidated = True
                    break
        agent.logger.write("axtree.event_remapped", {
            "source": "DOM.axTreeUpdated",
            "remappedCount": len(remappings),
        })
        return
    
    # 事件没有携带数据，只标记需要刷新
    agent.axtree_invalidated = True
```

### 5.4 Rematch 校验

```python
def _validate_rematch(
    agent: Any,
    previous_id: str,
    current_id: str,
    result: JsonDict,
) -> bool:
    """rematched 节点的 role 和 name 必须与原目标一致"""
    # 从缓存的 axtree_nodes 中查找原节点信息
    original = _find_node_in_cache(agent, previous_id)
    # 从返回的 data 中查找新节点信息
    rematched = _find_rematched_node_in_result(result, current_id)
    
    if not original or not rematched:
        return False  # 信息不足，拒绝
    
    return (
        rematched.get("role") == original.get("role")
        and rematched.get("name", "").strip().lower() 
            == original.get("name", "").strip().lower()
    )
```

**不一致的处理**：按 stale 处理，刷新 AXTree 后用新 id 重试。

### 5.5 `AXTREE_INVALIDATING_METHODS` 优化

当前 `Input.click`、`Input.type` 等所有 Input 动作都会 full invalidate axtree。有了事件通道后：

```python
# 改为按需 invalidate：
# 1. 如果事件通道已连接且收到了 DOM.axTreeUpdated → 增量更新，不 invalidate
# 2. 如果事件通道未连接或事件中无有效数据 → 仍然 invalidate（安全回退）
# 3. Page.navigate / Page.create 等结构性变化 → 始终 invalidate

# 但初期（Phase 0）先保留现有行为，事件通道稳定后再优化
```

---

## 六、事件消费策略（明确回答用户的疑虑）

### 6.1 事件分类

| 事件类型 | 消费方式 | 是否注入 LLM | 原因 |
|---------|---------|-------------|------|
| `DOM.axTreeUpdated` (stale-id rematch) | 更新 `agent.axtree_ids` + `axtree_epoch` | ❌ | 纯内部状态维护 |
| `Input.click` 返回的 `recoveredTarget` | 更新 `agent.axtree_ids` id 映射 | ❌ | 纯内部状态维护 |
| `Page.loaded` | 标记 `axtree_invalidated = True` | ❌ | 已有逻辑 |
| `Page.recovered` | 触发 render_recovery | ❌ | 已有逻辑 |
| `Page.dialogOpened` | composite tool 内部使用 | ❌ | loop 内部判断用 |
| `Hitl.resumeEvent` | 触发 HITL wait 流程 | ❌ | 已有逻辑 |
| 其他未知事件 | 忽略 | ❌ | 不处理不认识的事件 |

**结论：没有任何事件会以原始 payload 形式注入 LLM context。**

### 6.2 事件 → LLM 的唯一路径

事件只能通过 **composite tool 的结构化返回值** 间接到达 LLM，且经过高度压缩：

```json
// dismiss_overlay 返回值中的 rematch 信息（仅在 yield 时附带）
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

这比原始事件 payload 小 2 个数量级，且只包含 LLM 做决策需要的结论。

### 6.3 NotificationHub 订阅方式

不需要新建 BrowserEventObserver 类。直接在 composite tool 执行期间订阅：

```python
async def dismiss_overlay(...):
    # 订阅 DOM.axTreeUpdated 事件（仅限本次 loop 生命周期）
    subscription = await browser.wait_for_notification(
        event_type="DOM.axTreeUpdated",
        timeout_ms=5000,
    )
    
    # loop 内部检查事件
    if subscription.has_pending():
        event = subscription.consume()
        # 更新内部状态，不注入 LLM
        await _handle_axtree_updated_event(agent, event)
    
    # loop 结束后 subscription 自动清理
```

---

## 七、修正后的实施计划

### Phase 0: ABCP 事件能力确认（0.5 天，与其他 phase 并行）

- 用 `System.describeEvent` 确认 `DOM.axTreeUpdated` 事件的 payload 格式
- 确认 auto-rematch 的触发条件和返回值格式（`recoveredTarget` 字段）
- 确认 `Runtime.evaluate` 在 ABCP 下的返回值格式（`returnByValue: true` 时的 data 结构）
- 写 probe 脚本验证，但不阻塞其他 phase

### Phase 1: 通用 Oracle 框架 + Verifier（1-2 天，最高优先级）

新建 `harness/observation/verifiers.py`：

```python
@dataclass
class VerifierResult:
    ok: bool
    evidence: JsonDict       # oracle 返回的原始数据
    confidence: float        # 0.0 - 1.0, oracle 通过=1.0
    method: str              # "runtime_oracle" | "fast_signal_only"

async def verify_with_runtime_oracle(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    page_id: str,
    expression: str,
    assertion: Callable[[Any], bool],
    fast_check: Optional[Callable[[], bool]] = None,
) -> VerifierResult:
    """
    通用 Runtime.evaluate oracle。
    所有具体 verifier 的统一入口。
    """
    ...

# 三个具体 verifier 只是 expression + assertion 的封装
async def verify_overlay_gone(...) -> VerifierResult:
    """expression: querySelector('[role="dialog"]') + visibility check"""
    ...

async def verify_items_grew(...) -> VerifierResult:
    """expression: querySelectorAll(item_selector).length"""
    ...

async def verify_field_value(...) -> VerifierResult:
    """expression: document.querySelector(selector).value"""
    ...
```

**这是整个方案的基础**。verifier 质量决定 loop 上限。先写测试用例再写实现。

测试用例：
- `test_verify_overlay_gone`: mock ABCP client 返回不同 DOM 状态
- `test_verify_items_grew`: mock 返回不同 count
- `test_verify_field_value`: mock 返回不同 value（含 React 受控组件场景）
- `test_oracle_error_handling`: Runtime.evaluate 抛异常时的行为

### Phase 2: Stale Guard 修正 + Rematch 处理（1-2 天，P0）

**这是最关键的集成修改**，不解决这个，后续 composite tool 的 auto-rematch 完全不工作。

1. 修改 `_check_stale_axtree_target` 增加 `allow_rematch` 参数
2. 修改 `_observe_axtree_state_after` 处理 `recoveredTarget` 字段
3. 实现 `_validate_rematch` 校验
4. 配置项：`browser_side_rematch: off|composite_only|on`
5. 写测试：
   - stale id 被 stale guard 拦截（默认行为不变）
   - composite tool 内 allow_rematch=True 放行 stale id
   - rematch 一致 → 更新 axtree_ids
   - rematch 不一致 → invalidate
   - page mismatch 仍拦截

### Phase 3: 事件通道接入（1-2 天，P1）

1. 订阅 `DOM.axTreeUpdated` 事件
2. 实现 `_handle_axtree_updated_event` 增量更新逻辑
3. 连接到现有 `_observe_axtree_state_after` 流程
4. 写测试：事件携带完整 AXTree、事件携带 id 映射、事件无数据需 invalidate

### Phase 4: dismiss_overlay composite tool（2-3 天）

1. 注册为 BROWSER_TOOLS，参照 navigate_verified 模式
2. 内部梯子：find close → click → verify_overlay_gone(oracle) → Escape → verify_overlay_gone(oracle) → backdrop → verify_overlay_gone(oracle)
3. 所有内部调用走 `_invoke_browser_method(count_progress=False)`
4. 修改 `_attach_runtime_strategy_hints`：从"描述梯子"改为"指名调用 dismiss_overlay"
5. 写测试（fake ABCP client）：覆盖每层梯子失败、auth/paywall 不自动点

### Phase 5: scroll_collect composite tool（2-3 天）

1. 注册为 BROWSER_TOOLS
2. 内部循环：scroll → settle → verify_items_grew(oracle) → 判断继续/停止
3. 结束时一次 AXTree resync
4. overlay 触发时内部调用 dismiss_overlay
5. 写测试：覆盖 scroll 无新增、lazy load、overlay 中断

### Phase 6: fill_field_verified composite tool（1-2 天）

1. 注册为 BROWSER_TOOLS
2. 内部流程：click → clear → type → verify_field_value(oracle) → 重试一次
3. 用 `DOM property` 验证（通过 Runtime.evaluate oracle，不是 attribute）
4. overlay 触发时内部调用 dismiss_overlay
5. 写测试：覆盖正常输入、React 受控组件、值不匹配

### Phase 7: 自动拦截层 + 端到端验证（1-2 天）

1. 在 `_execute_browser_capability_tool` 中添加可选的自动拦截
2. 配置项：`auto_dismiss_overlay: true|false`（初期默认 false）
3. 端到端测试
4. 性能基准测试
5. 遥测：traceSummary 增加 recoveryLoopAttempts/success/failureReason

---

## 八、与 v2 方案的差异总结

| 维度 | v2 方案 | v3 方案 | 修正原因 |
|------|---------|---------|---------|
| verifier 实现 | 封装独立 verifier 函数 | **Runtime.evaluate oracle + 快速信号两层** | Playground 验证：直接读 DOM property 比中间封装更可靠，且 ABCP 原生支持 |
| 验证方法 | `DOM.getAttribute`/AXTree state | **Runtime.evaluate 读 DOM property (.value, .textContent)** | React 受控组件下 attribute 不更新；Playground 用法已验证 |
| stale guard | 增加参数放行 | **同 + 补充 `recoveredTarget` response 处理** | Playground 显示浏览器通过 response 返回 rematch，不只有事件通道 |
| 事件处理 | 只提了事件通道入口 | **两个入口：response recoveredTarget + 事件 DOM.axTreeUpdated** | Playground 案例中 rematch 信息在 response 里，不在事件里 |
| 冲突清单 | 笼统描述 | **逐条列出 4 个冲突，标注严重度和优先级** | 明确哪些必须先解，哪些可以后做 |
| 实施顺序 | verifier → overlay → scroll → form → stale guard | **oracle 框架 → stale guard(P0) → 事件通道 → overlay → scroll → form → 自动拦截** | stale guard 不解，composite tool 的 rematch 完全不工作，必须提前 |
| overlay 验证 | 2-of-4 共识信号 | **AXTree 快速信号 + Runtime.evaluate oracle** | oracle 是终态确认，比投票更可靠 |
