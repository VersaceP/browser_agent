# Browser Agent Micro-Loop 架构方案

> 版本: v1.0  
> 日期: 2026-06-12  
> 状态: 设计阶段

## 一、核心动机

### 1.1 为什么需要 Micro-Loop

Coding agent 之所以高效，是因为 coding 语言是**直观的、可观察的**：写代码 → 测试 → 观察结果 → 修正，形成 **act → observe → judge → adjust** 的紧致循环。每一步的判断标准是二值的（编译通过/不通过，测试通过/不通过）。

Browser agent 面临同样的问题：执行浏览器动作后需要观察结果、判断是否成功、决定下一步。但当前架构中，**每一个这样的判断都需要一次完整的 LLM 调用**，即使判断标准是完全确定性的。

**现状问题举例**：

| 场景 | 当前 LLM steps 消耗 | 真正需要 LLM 的判断 |
|------|-------------------|-------------------|
| 关闭 cookie banner | 3-4 steps | 0（关闭按钮可见就是可见） |
| 滚动收集 5 页内容 | ~10 steps | ~2（何时停止收集） |
| 表单填写 3 字段 + 验证 | ~9 steps | ~3（填什么值） |

### 1.2 已有先例：render_recovery

项目中已经存在一个 code-level micro-loop 的成功实现——`harness/render_recovery.py`：

```
browser.call() → 检测 render_lost → 自动 Page.getState → 
如果不行 → Page.switchTo → 如果不行 → Page.navigate → 自动重试原始调用
```

这个循环**完全自动**，不消耗 LLM step，不消耗 token。它证明了 code-level micro-loop 在当前架构中是可行的、安全的。

### 1.3 设计原则

**判断标准**：一个循环是否应该 code-level 化，取决于**该循环的终止条件和分支判断是否可以用确定性规则表达**。

- **确定性规则可表达** → code-level micro-loop（不消耗 LLM step）
- **需要 LLM 创造力/判断力** → prompt-level（保持 LLM 逐步执行）
- **混合场景** → 分层设计：确定性部分 code-level，关键决策点 yield 回 LLM

---

## 二、架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        LLM (BrowserAgent)                       │
│  发起 tool_call → 拿到增强的 result → 做出下一步决策              │
└──────────┬───────────────────────────────────────┬──────────────┘
           │ tool_call                              │ yield-back
           ▼                                       │
┌──────────────────────────────────────────────────┴───────────────┐
│                   browser_tools.py dispatch 层                    │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  _execute_browser_capability_tool                           │ │
│  │    1. 解析 method/params                                    │ │
│  │    2. 调用 render_recovery_runner.call()          ← 已有    │ │
│  │    3. 附加 errorClassification                    ← 已有    │ │
│  │    4. ★ micro-loop 拦截层                       ← 新增     │ │
│  │    5. 附加 runtimeStrategy + next_instruction      ← 已有   │ │
│  │    6. 返回增强结果给 LLM                                     │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Micro-Loop 执行引擎                             │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ overlay_     │  │ expand_      │  │ form_verify_         │   │
│  │ dismiss      │  │ collect      │  │ type                 │   │
│  │              │  │              │  │                      │   │
│  │ 触发: occluded│  │ 触发: LLM主动│  │ 触发: LLM主动        │   │
│  │ 自动处置遮罩  │  │ 调用工具      │  │ 调用工具             │   │
│  └──────────────┘  └──────────────┘  └──────────────────────┘   │
│                                                                  │
│  共享基础设施:                                                    │
│  - MicroLoopContext (browser引用, logger, VL配置)                │
│  - MicroLoopResult (status, actions_taken, observation, hint)   │
│  - 安全边界 (max_attempts, max_duration, 禁止列表)              │
└──────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     ABCP Browser (WebSocket RPC)                 │
│  DOM.getAXTree / Input.* / Page.* / Runtime.evaluate / ...      │
│  layers: occlusionState, boundsInRoot, occludedByFrameIds       │
│  AXTreeUpdate event (stale-id 自动恢复)                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、三个 Micro-Loop 的详细设计

---

### 3.1 Loop 1: Overlay Dismissal — 遮罩自动处置

#### 3.1.1 触发条件

**被动触发**（不需要 LLM 主动调用），在 `_execute_browser_capability_tool` 返回结果后自动检测：

| 优先级 | 信号来源 | 触发条件 |
|--------|---------|---------|
| P0 | `errorClassification.type === "occlusion_blocked"` | `Input.click` 返回 occluded 错误 |
| P1 | `DOM.getAXTree` 结果中 `layers[].occlusionState === "occluded"` | 浏览器报告 frame 级遮挡 |
| P2 | `page_observer` 检测到 `overlay.type === "business_overlay"` | AXTree 文本分析检测到遮罩 |
| P3 | `ActionFeedback.observation` 含 occlusion 关键词 | ABCP 返回的观察提示 |

触发后进入 micro-loop，**LLM 不参与循环迭代**。

#### 3.1.2 处置流程

```
┌─ overlay_dismiss micro-loop ─────────────────────────────────────────┐
│                                                                       │
│  输入: original_tool_call, original_result, agent 上下文              │
│                                                                       │
│  Step 1: 获取最新页面状态                                             │
│    → 调用 DOM.getAXTree(pageId)                                      │
│    → 检查 response.data.layers[*].occlusionState                     │
│    → 提取 overlay_detector 结果（如 page_stats 中已有）               │
│    → 如果主 frame 仍然 visible → 可能是节点级遮挡，非 frame 级        │
│    → 如果主 frame occluded + occludedByFrameIds → 精确定位遮挡 frame  │
│                                                                       │
│  Step 2: 搜索关闭控件                                                 │
│    → 在 agent.axtree_nodes 中搜索:                                    │
│      - role ∈ {button, link}                                         │
│      - name 匹配 DISMISS_KEYWORDS (close/dismiss/not now/skip/...)   │
│      - 标记为可交互 (#)                                              │
│      - 位于 dialog/alertdialog 区域内或 overlay frame 内              │
│    → 找到 → Input.click(关闭按钮id, pageId) → Step 5                 │
│                                                                       │
│  Step 3: Escape 键尝试                                                │
│    → Input.press(key="Escape", pageId)                                │
│    → 等待 ~500ms                                                      │
│    → 刷新 DOM.getAXTree → 检查 occlusionState                        │
│    → 遮罩消失 → Step 5                                               │
│    → 遮罩仍在 → 继续                                                  │
│                                                                       │
│  Step 4: 遮罩类型路由（安全边界核心）                                 │
│    → overlay.subtype === "auth_prompt" → YIELD 回 LLM               │
│      "登录墙检测到，不能自动操作，需要 HITL 或换策略"                  │
│    → overlay.subtype === "paywall" → YIELD 回 LLM                   │
│      "付费墙检测到，不能自动绕过"                                     │
│    → overlay.subtype === "cookie_banner" →                           │
│      搜索 accept/reject 控件 → 找到 → click → 验证消失 → Step 5    │
│    → overlay.subtype === "modal_dialog"（无关闭按钮）→               │
│      → eval_js_json:                                                  │
│          document.elementFromPoint(x, y) 验证点击目标                │
│          (x, y) 取 overlay boundsInRoot 之外的安全坐标               │
│      → 验证通过 → Input.click(x, y) → 验证消失 → Step 5            │
│      → 验证失败或点击目标为 login/payment 控件 → YIELD              │
│    → 所有子策略失败 → Step 4b                                        │
│                                                                       │
│  Step 4b: VL 降级验证（仅在 VL 启用时）                              │
│    → 用遮挡 frame 的 boundsInRoot 截取区域截图                        │
│    → 调用 visual_verify_image(mode="overlay_classify")               │
│    → VL 判定可安全关闭 → 执行 VL 建议的操作                          │
│    → VL 不确定或判定不安全 → YIELD 回 LLM                            │
│                                                                       │
│  Step 5: 验证遮罩消失                                                 │
│    → 刷新 DOM.getAXTree → layers[*].occlusionState 全部 visible     │
│    → overlay_detector 不再报告遮罩                                    │
│    → 遮罩已消失:                                                      │
│      → 重试原始 tool_call (Input.click 原始目标)                     │
│      → 重试成功 → COMPLETED，返回增强结果                            │
│      → 重试仍 occluded → YIELD（可能有多层遮罩）                     │
│    → 遮罩仍在:                                                        │
│      → attempts < max_attempts → 回到 Step 2                        │
│      → attempts >= max_attempts → YIELD                              │
│                                                                       │
│  输出: MicroLoopResult                                                │
│    status: "completed" | "yielded" | "failed"                        │
│    actions_taken: [...自动执行的动作日志]                             │
│    observation: 增强的工具结果                                        │
│    yield_reason: yield 回 LLM 的原因                                 │
│    yield_hint: 给 LLM 的下一步建议                                   │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

#### 3.1.3 LLM 参与边界

| 阶段 | LLM 参与？ | 原因 |
|------|-----------|------|
| 遮罩检测 | ❌ | `occlusionState` / `overlay_detector` 确定性判断 |
| 搜索关闭按钮 | ❌ | AXTree role+name 匹配，规则确定 |
| 点击关闭/Dismiss 按钮 | ❌ | 对 dismiss 类按钮操作安全 |
| Escape 键 | ❌ | 无副作用 |
| Cookie banner Accept/Reject | ❌ | 明确的接受/拒绝控件，操作安全 |
| 点击遮罩外部 | ⚠️ 条件性 | 必须先 `elementFromPoint` 验证目标安全性 |
| Auth/Paywall 处置 | ✅ | **绝不能自动操作**，必须 LLM 或 HITL |
| 所有策略失败 | ✅ | 需要 LLM 创造性寻找解法 |
| VL 验证后仍不确定 | ✅ | 需要 LLM 判断 |

#### 3.1.4 安全边界

```python
OVERLAY_DISMISS_SAFETY = {
    # 绝不自动点击的按钮关键词
    "never_click_keywords": [
        "sign in", "login", "log in", "authenticate",
        "subscribe", "upgrade", "pay", "purchase",
        "connect with google", "connect with apple",
        "登录", "注册", "付费", "订阅", "购买",
    ],
    # 绝不自动点击的控件组合
    "never_click_patterns": [
        # auth_prompt 内的 submit 按钮
        {"overlay_subtype": "auth_prompt", "action": "any_click"},
        # paywall 内的任何按钮
        {"overlay_subtype": "paywall", "action": "any_click"},
    ],
    # 循环保护
    "max_attempts_per_strategy": 3,
    "max_total_attempts": 6,
    "max_duration_ms": 15000,
    "retry_original_action_after_dismiss": True,  # 自动重试原始动作
}
```

#### 3.1.5 返回给 LLM 的结果格式

**completed 时**（遮罩成功关闭，原始动作已重试）：

```json
{
    "method": "Input.click",
    "params": {"id": "11:26:26", "pageId": "7c576887-..."},
    "response": {
        "observation": "Click succeeded on target after overlay dismissal.",
        "data": { "clickedElement": "提交订单" }
    },
    "micro_loop": {
        "type": "overlay_dismiss",
        "status": "completed",
        "dismissStrategy": "click_close_button",
        "dismissedOverlay": {
            "subtype": "cookie_banner",
            "closeControlId": "11:30:30",
            "closeControlName": "Accept"
        },
        "actionsTaken": [
            {"step": "detect_overlay", "method": "DOM.getAXTree"},
            {"step": "find_close", "found": true, "id": "11:30:30", "name": "Accept"},
            {"step": "dismiss", "method": "Input.click", "id": "11:30:30"},
            {"step": "verify", "overlayGone": true},
            {"step": "retry_original", "method": "Input.click", "id": "11:26:26"}
        ],
        "elapsed_ms": 2340,
        "saved_llm_steps": 3
    }
}
```

**yielded 时**（无法自动处置，回退给 LLM）：

```json
{
    "method": "Input.click",
    "params": {"id": "11:26:26", "pageId": "7c576887-..."},
    "errorClassification": {
        "type": "occlusion_blocked",
        "suggested_action": "refresh_dom_dismiss_overlay_then_retry_once"
    },
    "micro_loop": {
        "type": "overlay_dismiss",
        "status": "yielded",
        "yieldReason": "auth_prompt_overlay_auto_dismiss_blocked",
        "overlay": {
            "subtype": "auth_prompt",
            "confidence": 0.9,
            "evidence": ["sign in to continue", "dialog role"],
            "dismissibleSignal": false
        },
        "attemptsMade": [
            {"step": "detect_overlay", "result": "auth_prompt_detected"},
            {"step": "safety_check", "result": "auth_prompt_auto_dismiss_blocked"}
        ],
        "yieldHint": (
            "Login/auth overlay detected that cannot be automatically dismissed. "
            "Options: 1) Check auth_fleet memory for existing session, "
            "2) Use Hitl.requestPause for manual login, "
            "3) Report blocker via final_answer."
        )
    }
}
```

#### 3.1.6 与现有组件的关系

| 现有组件 | 关系 |
|---------|------|
| `overlay_detector` | **信号源**。micro-loop 直接调用 `detect_overlay_from_result()` 获取遮罩分类 |
| `_attach_runtime_strategy_hints()` | **替代**。当 micro-loop completed 时，不再需要 `runtimeStrategy` 提示；当 yielded 时，yield_hint 已经包含更精确的建议 |
| `page_stats.overlay` | **信号源**。micro-loop 读取 `overlay` 字段作为触发信号之一 |
| `render_recovery` | **并行**。如果 overlay_dismiss 过程中触发 render_lost，交给 render_recovery 处理 |
| `loop_nudge` | **暂停**。micro-loop 执行期间暂停 loop_nudge 判定，避免重复告警 |

---

### 3.2 Loop 2: Expand-Collect — 展开式内容收集

#### 3.2.1 设计思路

这个 loop 是 **LLM 主动调用的工具**，而非被动拦截。LLM 调用 `expand_collect` 工具，指定展开策略和停止条件，harness 内部执行滚动/点击循环，只消耗 1 个 LLM step。

**泛化的 "展开" 动作类型**：

| 动作模式 | 触发方式 | 内容变化模式 | 稳定性判断 |
|---------|---------|------------|-----------|
| `scroll_append` | Input.scroll | 内容追加，id 只增不减 | 连续 N 次 fingerprint 不变 |
| `scroll_replace` | Input.scroll(水平) | 内容替换 | nodeCount 稳定 |
| `click_load_more` | Input.click("Load More") | 内容追加 | 按钮消失 或 fingerprint 不变 |
| `click_pagination` | Input.click("Next") | 内容替换 | 新内容加载完毕(nodeCount 稳定) |
| `expand_sections` | Input.click(accordion/tab) | 区域展开 | 所有目标 section 已展开 |

**共同抽象**：

```
action(动作) → observe(fingerprint变化) → 判断稳定性 → 继续或停止
```

#### 3.2.2 工具定义

```python
# 注册为 BROWSER_TOOLS 的新工具

expand_collect_schema = {
    "name": "expand_collect",
    "description": (
        "Repeatedly expand page content (scroll, click load-more, paginate, "
        "or expand sections) until the page stabilizes or a limit is reached. "
        "Returns the final AXTree path and a collection summary. "
        "Use this instead of manually looping scroll→getAXTree→judge."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pageId": {"type": "string", "description": "Target page ID"},
            "mode": {
                "type": "string",
                "enum": ["scroll_append", "scroll_replace", "click_load_more", 
                         "click_pagination", "expand_sections"],
                "description": "Expansion strategy"
            },
            "direction": {
                "type": "string",
                "enum": ["down", "up", "right", "left"],
                "default": "down",
                "description": "Scroll direction (for scroll modes)"
            },
            "containerId": {
                "type": "string",
                "description": "AXTree id of scroll container (for scroll modes)"
            },
            "actionTargetId": {
                "type": "string",
                "description": "AXTree id of load-more/pagination/accordion button"
            },
            "actionTargetSelector": {
                "type": "string",
                "description": "CSS selector for action target (fallback)"
            },
            "sectionIds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "AXTree ids of sections to expand (for expand_sections mode)"
            },
            "maxIterations": {
                "type": "integer",
                "default": 10,
                "description": "Maximum expansion iterations"
            },
            "stabilityThreshold": {
                "type": "integer",
                "default": 3,
                "description": "Consecutive unchanged observations to declare stable"
            },
            "stabilityPredicate": {
                "type": "string",
                "enum": ["fingerprint_unchanged", "node_count_stable", "target_gone"],
                "default": "fingerprint_unchanged",
                "description": "How to judge if expansion is complete"
            }
        },
        "required": ["pageId", "mode"]
    }
}
```

#### 3.2.3 执行流程

```
┌─ expand_collect micro-loop ──────────────────────────────────────────┐
│                                                                       │
│  输入: tool_input (mode, pageId, direction, containerId, ...)        │
│                                                                       │
│  初始化:                                                              │
│    → 调用 DOM.getAXTree(pageId) 获取 baseline fingerprint            │
│    → 解析 layers 获取 frame bounds                                    │
│                                                                       │
│  循环 (iteration = 0..maxIterations):                                │
│                                                                       │
│    Step 1: 执行展开动作                                               │
│      mode === "scroll_append" | "scroll_replace":                    │
│        → Input.scroll(direction, containerId or pageId)              │
│      mode === "click_load_more":                                     │
│        → Input.click(actionTargetId)                                 │
│        → 检查按钮是否还在 DOM 中（可能已消失）                       │
│      mode === "click_pagination":                                    │
│        → Input.click(actionTargetId)                                 │
│      mode === "expand_sections":                                     │
│        → 逐个 Input.click(sectionIds 中未展开的 id)                  │
│        → 通过 AXTree 检查 section 的 expanded 状态                   │
│                                                                       │
│    Step 2: 观察变化                                                   │
│      → 等待 300-800ms (视 mode 调整)                                 │
│      → 调用 DOM.getAXTree(pageId)                                    │
│      → 计算 PageFingerprint                                          │
│      → 与上次 fingerprint 对比                                        │
│                                                                       │
│    Step 3: 稳定性判断                                                 │
│      stabilityPredicate === "fingerprint_unchanged":                 │
│        → consecutive_unchanged_count += 1                            │
│        → count >= stabilityThreshold → STABLE                        │
│      stabilityPredicate === "node_count_stable":                     │
│        → abs(node_count_delta) < threshold → stable_count += 1      │
│        → stable_count >= threshold → STABLE                          │
│      stabilityPredicate === "target_gone":                           │
│        → actionTargetId 不再出现在 AXTree → STABLE                   │
│                                                                       │
│    Step 4: Overlay 检查                                               │
│      → 如果展开动作触发了 overlay → 调用 overlay_dismiss micro-loop  │
│      → overlay_dismiss yielded → 整个 expand_collect 也 yield        │
│                                                                       │
│    如果 STABLE → 跳出循环                                            │
│    如果 iteration === maxIterations - 1 → 跳出循环                   │
│                                                                       │
│  输出:                                                                │
│    → 最终 AXTree 结果 offload 到 savedPath                           │
│    → 返回结构化摘要给 LLM:                                           │
│      {                                                               │
│        "status": "completed" | "partial",                           │
│        "mode": "scroll_append",                                     │
│        "iterationsUsed": 7,                                         │
│        "stabilizedAt": 7,                                           │
│        "nodeCount": { "initial": 54, "final": 312, "delta": 258 }, │
│        "physicalIds": { "initial": 54, "final": 312 },             │
│        "finalAXTreePath": "observations/...",                       │
│        "layers": [...],                                             │
│        "overlayEncountered": null | { "dismissed": true, ... },     │
│        "saved_llm_steps": 5                                         │
│      }                                                               │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

#### 3.2.4 LLM 使用示例

**之前** (10+ LLM steps)：
```
LLM: browser_call(DOM.getAXTree) → 看到 54 个节点
LLM: browser_call(Input.scroll, direction=down) 
LLM: browser_call(DOM.getAXTree) → 看到 102 个节点
LLM: browser_call(Input.scroll, direction=down)
LLM: browser_call(DOM.getAXTree) → 看到 198 个节点
LLM: browser_call(Input.scroll, direction=down)
LLM: browser_call(DOM.getAXTree) → 看到 198 个节点（没变化）
LLM: browser_call(Input.scroll, direction=down)
LLM: browser_call(DOM.getAXTree) → 看到 198 个节点（还是没变化）
LLM: "好了，滚动到底了，开始提取"
```

**之后** (2 LLM steps)：
```
LLM: expand_collect(mode=scroll_append, pageId=..., direction=down, maxIterations=10, stabilityThreshold=3)
     → harness 内部自动完成 7 次滚动 + 8 次 getAXTree + 稳定性判断
     → 返回: { status: "completed", iterationsUsed: 7, finalNodeCount: 312, ... }
LLM: "收到，312 个节点，开始提取"
```

#### 3.2.5 与 overlay_dismiss 的嵌套

`expand_collect` 执行过程中可能遇到遮罩（如滚动触发了 "Subscribe to continue" 模态框）。处理方式：

```
expand_collect 内部:
  scroll → getAXTree → overlay_detector 检测到遮罩
  → 调用 overlay_dismiss micro-loop
    → completed → 继续 expand_collect
    → yielded (auth/paywall) → expand_collect 整体也 yield
```

---

### 3.3 Loop 3: Form-Verify-Type — 表单输入验证

#### 3.3.1 设计思路

这是**最保守的 micro-loop**。只自动化 "输入 → 验证 → 重试" 这个最小原子操作，不自动化整个表单填写流程。

LLM 仍然决定：填什么值、填哪个字段、何时提交。micro-loop 只确保 **"输入的值确实被写入了"**。

#### 3.3.2 触发方式

LLM 主动调用，作为 `Input.type` 的增强替代：

```python
type_verified_schema = {
    "name": "type_verified",
    "description": (
        "Type text into a target element and verify the value was accepted. "
        "If the initial type fails verification, automatically clears and retries once. "
        "Use this instead of Input.type + manual DOM.getAttribute(value) verification."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pageId": {"type": "string"},
            "id": {"type": "string", "description": "AXTree canonical id"},
            "selector": {"type": "string", "description": "CSS selector (fallback)"},
            "text": {"type": "string", "description": "Text to type"},
            "clearFirst": {
                "type": "boolean",
                "default": true,
                "description": "Clear existing value before typing"
            },
            "verifyMethod": {
                "type": "string",
                "enum": ["axtree_value", "dom_attribute", "vl_fallback"],
                "default": "axtree_value",
                "description": "How to verify the typed value"
            }
        },
        "required": ["pageId", "text"]
    }
}
```

#### 3.3.3 执行流程

```
┌─ type_verified micro-loop ──────────────────────────────────────────┐
│                                                                      │
│  Step 1: 输入                                                        │
│    → 如果 clearFirst:                                                │
│        Input.click(id) → Input.press("Control+a") → Input.press("") │
│    → Input.type(id, text)                                            │
│                                                                      │
│  Step 2: 验证                                                        │
│    verifyMethod === "axtree_value":                                  │
│      → 刷新 DOM.getAXTree → 找到目标节点 → 检查 value 属性          │
│    verifyMethod === "dom_attribute":                                 │
│      → DOM.getAttribute(id, attribute="value")                       │
│    verifyMethod === "vl_fallback":                                   │
│      → 用目标节点的 bounds 截取局部截图                               │
│      → visual_verify_image(mode="form_verify")                      │
│                                                                      │
│  Step 3: 判断                                                        │
│    → 实际值 === 预期值 → COMPLETED                                   │
│    → 实际值 !== 预期值 且 retry_count < 1:                           │
│        → 强制清除: eval_js_json(target.value = "")                   │
│        → 重试 Input.type → 回到 Step 2                              │
│    → 实际值 !== 预期值 且 retry_count >= 1:                          │
│        → YIELD: { expected: X, actual: Y, hint: "..." }             │
│                                                                      │
│  Step 4: Overlay 检查                                                │
│    → 如果 Input.type 返回 occluded → 调用 overlay_dismiss → 重试    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

#### 3.3.4 返回格式

```json
{
    "method": "type_verified",
    "status": "completed",
    "params": {"id": "11:33:33", "text": "hello@example.com"},
    "verification": {
        "method": "dom_attribute",
        "expected": "hello@example.com",
        "actual": "hello@example.com",
        "match": true
    },
    "actionsTaken": [
        {"step": "clear", "method": "Input.click + Ctrl+A"},
        {"step": "type", "method": "Input.type", "text": "hello@example.com"},
        {"step": "verify", "method": "DOM.getAttribute", "attribute": "value"}
    ],
    "saved_llm_steps": 2
}
```

---

## 四、实现计划

### 4.1 新增文件结构

```
harness/
  micro_loops/
    __init__.py                 # 导出公共 API
    base.py                     # MicroLoopContext, MicroLoopResult, BaseMicroLoop
    overlay_dismiss.py          # OverlayDismissLoop
    expand_collect.py           # ExpandCollectLoop
    type_verified.py            # TypeVerifiedLoop
    axtree_search.py            # AXTree 节点搜索工具函数（共享）
```

### 4.2 核心类型定义 — `base.py`

```python
"""
harness.micro_loops.base - Shared types and protocols for code-level micro-loops.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from harness.utils import JsonDict, RunLogger


@dataclass
class MicroLoopContext:
    """Immutable context passed into every micro-loop execution."""
    browser: Any           # ABCPClient instance
    logger: RunLogger
    vl_config: Any         # VLConfig instance
    capability_methods: set
    agent: Any             # BrowserAgent reference (read-only for state queries)
    step: int              # Current LLM step number
    original_tool_call: JsonDict
    original_result: JsonDict


@dataclass
class MicroLoopAction:
    """Single action taken within a micro-loop, for audit trail."""
    step: str              # e.g. "detect_overlay", "dismiss", "verify"
    method: str = ""       # ABCP method called
    params: JsonDict = field(default_factory=dict)
    result_summary: str = ""  # Brief result description
    elapsed_ms: int = 0


@dataclass
class MicroLoopResult:
    """Result returned by a micro-loop execution."""
    loop_type: str         # "overlay_dismiss" | "expand_collect" | "type_verified"
    status: str            # "completed" | "yielded" | "failed" | "not_triggered"
    actions_taken: List[MicroLoopAction] = field(default_factory=list)
    observation: Optional[JsonDict] = None   # Enhanced result for LLM
    yield_reason: str = ""
    yield_hint: str = ""
    elapsed_ms: int = 0
    saved_llm_steps: int = 0   # Estimated LLM steps saved

    def to_dict(self) -> JsonDict:
        return {
            "type": self.loop_type,
            "status": self.status,
            "actionsTaken": [
                {
                    "step": a.step,
                    "method": a.method,
                    "resultSummary": a.result_summary,
                    "elapsedMs": a.elapsed_ms,
                }
                for a in self.actions_taken
            ],
            "elapsedMs": self.elapsed_ms,
            "yieldReason": self.yield_reason,
            "yieldHint": self.yield_hint,
            "savedLlmSteps": self.saved_llm_steps,
        }


class BaseMicroLoop:
    """Base class for code-level micro-loops."""

    loop_type: str = "base"
    max_attempts: int = 3
    max_duration_ms: int = 15000

    def should_trigger(
        self,
        tool_call: JsonDict,
        result: Any,
        agent: Any,
    ) -> bool:
        """Return True if this micro-loop should activate for the given result."""
        raise NotImplementedError

    async def execute(self, context: MicroLoopContext) -> MicroLoopResult:
        """Execute the micro-loop. Must return within max_duration_ms."""
        raise NotImplementedError

    def _check_duration(self, started_at: float) -> bool:
        """Return False if duration limit exceeded."""
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        return elapsed_ms < self.max_duration_ms
```

### 4.3 AXTree 搜索工具 — `axtree_search.py`

```python
"""
harness.micro_loops.axtree_search - Shared AXTree node search utilities.

These functions operate on the parsed AXTree data cached on the agent,
not on raw ABCP responses.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Set

from harness.utils import JsonDict


# AXTree line pattern: [id] role "name" #flags
AXTREE_LINE_RE = re.compile(
    r'^(?P<indent>\s*)\[(?P<id>\d+:-?\d+:-?\d+)\]\s+'
    r'(?P<role>[^\s"]+)(?:\s+"(?P<name>.*?)")?(?P<rest>.*)$'
)

DISMISS_KEYWORDS = {
    "close", "dismiss", "not now", "maybe later", "skip", "got it",
    "continue without", "accept cookies", "accept all", "reject all",
    "agree", "no thanks", "don't show", "remind me later",
    "关闭", "稍后", "跳过", "知道了", "不同意", "拒绝", "接受",
}

NEVER_CLICK_KEYWORDS = {
    "sign in", "login", "log in", "authenticate", "subscribe", "upgrade",
    "pay", "purchase", "connect with google", "connect with apple",
    "sign in with google", "sign in with apple",
    "登录", "注册", "付费", "订阅", "购买", "微信登录", "支付宝",
}

ACTIONABLE_ROLES = {
    "button", "link", "checkbox", "combobox", "menuitem",
    "option", "radio", "switch", "tab", "treeitem",
}


@dataclass
class AXNode:
    """Parsed AXTree node."""
    id: str
    role: str
    name: str
    indent: int
    interactable: bool  # '#' marker
    raw_line: str


def parse_axtree_lines(lines: List[str]) -> List[AXNode]:
    """Parse AXTree text lines into structured nodes."""
    nodes = []
    for line in lines:
        match = AXTREE_LINE_RE.match(line)
        if not match:
            continue
        node = AXNode(
            id=match.group("id"),
            role=match.group("role"),
            name=match.group("name") or "",
            indent=len(match.group("indent")),
            interactable="#" in (match.group("rest") or ""),
            raw_line=line,
        )
        nodes.append(node)
    return nodes


def find_dismiss_controls(
    nodes: List[AXNode],
    *,
    within_roles: Optional[Set[str]] = None,
) -> List[AXNode]:
    """Find close/dismiss buttons that can safely close an overlay.

    Args:
        nodes: Parsed AXTree nodes.
        within_roles: If set, only return controls inside these container roles
                      (e.g. {"dialog", "alertdialog"}).

    Returns:
        List of candidate dismiss controls, ordered by specificity.
    """
    candidates = []
    dialog_depth = None

    for i, node in enumerate(nodes):
        # Track dialog scope
        if node.role in {"dialog", "alertdialog"}:
            dialog_depth = node.indent
            continue
        if dialog_depth is not None and node.indent <= dialog_depth:
            dialog_depth = None

        # Skip non-actionable nodes
        if node.role not in ACTIONABLE_ROLES:
            continue

        # If within_roles specified, only consider nodes inside those roles
        if within_roles and dialog_depth is None:
            continue

        # Check if it's a dismiss control
        name_lower = node.name.lower().strip()
        if any(kw in name_lower for kw in DISMISS_KEYWORDS):
            # Safety: skip never-click keywords
            if any(kw in name_lower for kw in NEVER_CLICK_KEYWORDS):
                continue
            candidates.append(node)

    # Prioritize: explicit "close"/"dismiss"/X first
    def priority(node: AXNode) -> int:
        name = node.name.lower().strip()
        if name in {"close", "x", "×", "关闭"}:
            return 0
        if name in {"dismiss", "skip", "not now", "跳过"}:
            return 1
        return 2

    candidates.sort(key=priority)
    return candidates


def find_nodes_by_role_and_name(
    nodes: List[AXNode],
    *,
    role: Optional[str] = None,
    name_contains: Optional[str] = None,
    interactable_only: bool = True,
) -> List[AXNode]:
    """Generic AXTree node search."""
    results = []
    for node in nodes:
        if interactable_only and not node.interactable:
            continue
        if role and node.role != role:
            continue
        if name_contains and name_contains.lower() not in node.name.lower():
            continue
        results.append(node)
    return results


def extract_overlay_from_layers(layers: List[JsonDict]) -> Optional[JsonDict]:
    """Check layers for occlusion state.

    Returns overlay info if any frame is occluded, None otherwise.
    """
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if layer.get("occlusionState") == "occluded":
            return {
                "type": "frame_occlusion",
                "occludedFrameId": layer.get("frameId"),
                "occludedByFrameIds": layer.get("occludedByFrameIds", []),
                "boundsInRoot": layer.get("boundsInRoot"),
                "visibleBoundsInRoot": layer.get("visibleBoundsInRoot"),
            }
    return None
```

### 4.4 集成点：`browser_tools.py` 修改

**核心修改在 `_execute_browser_capability_tool` 函数中**，在 render_recovery 之后、返回给 LLM 之前，插入 micro-loop 拦截层。

```python
# 在 _execute_browser_capability_tool 函数的 try 块中
# 当前代码（第 616-651 行）之后，添加 micro-loop 拦截：

    # ... 现有代码：render_recovery, capture_artifacts, offload, etc. ...

    # === 新增：micro-loop 拦截层 ===
    from harness.micro_loops import build_micro_loop_pipeline
    pipeline = build_micro_loop_pipeline(agent)
    loop_result = await pipeline.maybe_execute(
        tool_call=tool_call,
        tool_name=tool_name,
        tool_input=tool_input,
        method=method,
        params=params,
        result=result,
        step=step,
    )
    if loop_result is not None and loop_result.observation is not None:
        # micro-loop 完成了自动处置，用增强结果替换原始结果
        result = loop_result.observation
        result["micro_loop"] = loop_result.to_dict()
    elif loop_result is not None and loop_result.status == "yielded":
        # micro-loop 尝试了但需要 LLM 决策
        result["micro_loop"] = loop_result.to_dict()
        # yield_hint 替换原有的 next_instruction
        if loop_result.yield_hint:
            result["next_instruction"] = loop_result.yield_hint
    # =================================

    # ... 现有代码继续：attach_error_classification, runtime_strategy_hints, etc. ...
```

### 4.5 新增配置项 — `config.py`

```python
# 在 HarnessConfig 中新增：

@dataclass
class MicroLoopConfig:
    enabled: bool = True
    overlay_dismiss_enabled: bool = True
    overlay_dismiss_max_attempts: int = 3
    overlay_dismiss_max_duration_ms: int = 15000
    overlay_dismiss_vl_fallback: bool = True  # 是否允许 VL 降级验证
    overlay_dismiss_auto_retry_original: bool = True  # 关闭遮罩后是否自动重试原始动作
    expand_collect_enabled: bool = True
    expand_collect_max_iterations: int = 10
    expand_collect_stability_threshold: int = 3
    expand_collect_scroll_delay_ms: int = 500
    type_verified_enabled: bool = True
    type_verified_max_retries: int = 1

# 添加到 HarnessConfig:
    micro_loops: MicroLoopConfig = field(default_factory=MicroLoopConfig)
```

---

## 五、AXTree layers 与 VL 的交叉验证

### 5.1 当前能力

AXTree 的 `response.data.layers` 现在提供：

```json
{
    "frameId": "11",
    "parentFrameId": null,
    "depth": 0,
    "url": "http://...",
    "viewportBounds": {"x": 0, "y": 0, "width": 2560, "height": 1600},
    "boundsInRoot": {"x": 0, "y": 0, "width": 2560, "height": 1600},
    "visibleBoundsInRoot": {"x": 0, "y": 0, "width": 2560, "height": 1600},
    "visible": true,
    "occlusionState": "visible",
    "occludedByFrameIds": []
}
```

### 5.2 交叉验证场景

#### 场景 A：Overlay 定位 + VL 分类

```
1. layers[0].occlusionState === "occluded"
2. 从 AXTree 找到遮挡 frame 的节点
3. layers[0].boundsInRoot 提供遮挡区域的精确坐标
4. Page.screenshot 裁剪到该区域（如果支持）或整页截图
5. VL 模型判断：可关闭的广告？还是登录墙？
6. VL 返回 verdict + 建议操作 → micro-loop 决策
```

#### 场景 B：表单输入 VL 验证

```
1. Input.type 完成
2. AXTree 显示 value 正确，但某些框架的 input 值不反映在 AXTree
3. 用目标节点的 bounds（需 DOM.getAttribute 获取或从 layers 推算）
4. 截取局部区域 → VL 确认视觉上是否正确
5. 交叉验证通过 → success；失败 → yield
```

#### 场景 C：展开收集完成度确认

```
1. expand_collect 报告页面已稳定
2. 但 LLM 需要确认内容是否真的完整
3. 截取当前页面底部截图 → VL 确认是否有 "加载更多" / 截断指示器
4. VL 确认完整 → 继续；VL 发现未完成 → 调整参数再调用 expand_collect
```

### 5.3 新增 VL mode

在 `harness/vl.py` 的 `build_visual_verify_prompt` 中新增：

```python
if mode == "overlay_classify":
    return (
        "Classify this browser overlay/modal screenshot and determine if it "
        "can be safely dismissed automatically.\n"
        f"question: {question or '(none)'}\n\n"
        "Return exactly one JSON object with keys:\n"
        "- verdict: one of dismissible_overlay, auth_wall, paywall, "
        "content_dialog, uncertain\n"
        "- confidence: number from 0 to 1\n"
        "- overlay_type: one of cookie_banner, newsletter_popup, ad_modal, "
        "auth_prompt, paywall, content_dialog, other\n"
        "- dismiss_method: one of click_close, escape, click_outside, "
        "click_accept, click_reject, not_safe_to_dismiss\n"
        "- dismiss_target: brief description of where to click (if safe)\n"
        "- reason: one short sentence\n"
    )

if mode == "form_verify":
    return (
        "Verify whether the text input field in this screenshot contains "
        "the expected value.\n"
        f"expected: {json.dumps(expected or {}, ensure_ascii=False)}\n"
        f"question: {question or '(none)'}\n\n"
        "Return exactly one JSON object with keys:\n"
        "- verdict: one of match, mismatch, partial_match, uncertain\n"
        "- confidence: number from 0 to 1\n"
        "- visible_value: the text visible in the input field\n"
        "- reason: one short sentence\n"
    )

if mode == "scroll_complete":
    return (
        "Determine if this screenshot shows the bottom/end of scrollable "
        "content, or if more content may load on further scrolling.\n"
        f"question: {question or '(none)'}\n\n"
        "Return exactly one JSON object with keys:\n"
        "- verdict: one of scroll_complete, more_content_likely, "
        "lazy_load_pending, uncertain\n"
        "- confidence: number from 0 to 1\n"
        "- visible_evidence: short array of visible observations\n"
        "- reason: one short sentence\n"
    )
```

---

## 六、对现有组件的修改清单

### 6.1 必须修改

| 文件 | 修改内容 | 风险 |
|------|---------|------|
| `harness/config.py` | 新增 `MicroLoopConfig`，加入 `HarnessConfig` | 低 |
| `harness/tools/browser_tools.py` | `_execute_browser_capability_tool` 中插入 micro-loop 拦截层；新增 `expand_collect` 和 `type_verified` 工具注册 | 中（核心路径） |
| `harness/vl.py` | 新增 `overlay_classify` / `form_verify` / `scroll_complete` mode | 低 |
| `agent_harness.py` | `BrowserAgent.__init__` 中初始化 `micro_loop_pipeline` | 低 |

### 6.2 可选优化

| 文件 | 修改内容 | 风险 |
|------|---------|------|
| `harness/observation/loop_nudge.py` | micro-loop 执行期间暂停 nudge | 低 |
| `harness/observation/page_fingerprint.py` | `expand_collect` 使用现有 `PageFingerprint` 作为稳定性判据 | 无修改，直接复用 |
| `harness/observation/overlay_detector.py` | micro-loop 直接调用，无需修改 | 无修改，直接复用 |
| `strategy_bank/strategy_bank.json` | `overlay.dismiss_ladder` 策略的 procedure 与 micro-loop 对齐 | 低 |
| `agent_harness.py` system prompt | 更新 L5.Recovery 中关于 overlay 的指导，告知 LLM micro-loop 会自动处理 | 低 |

### 6.3 不修改

| 组件 | 原因 |
|------|------|
| `render_recovery` | 独立运行，micro-loop 不干扰 |
| `diagnostics` | micro-loop 结果仍经过 `observe_browser_call` |
| `progress` | `expand_collect` / `type_verified` 作为工具正常计步 |
| `compaction` | 不受影响 |
| `spawner` | 不受影响 |
| `task_control` | 不受影响 |

---

## 七、实施路线图

### Phase 1: 基础设施 (1-2 天)

1. 创建 `harness/micro_loops/` 包
2. 实现 `base.py`（MicroLoopContext, MicroLoopResult, BaseMicroLoop）
3. 实现 `axtree_search.py`（共享的 AXTree 搜索工具）
4. 在 `config.py` 中添加 `MicroLoopConfig`
5. 编写单元测试验证 `axtree_search` 的节点解析和搜索

### Phase 2: Overlay Dismiss (2-3 天)

1. 实现 `overlay_dismiss.py`
2. 在 `browser_tools.py` 中集成拦截层
3. 在 `BrowserAgent.__init__` 中初始化 pipeline
4. 更新 `vl.py` 新增 `overlay_classify` mode
5. 集成测试：使用 ABCP playground 的 stale-id-recovery fixture 测试
6. 更新 system prompt L5.Recovery

### Phase 3: Expand-Collect (2-3 天)

1. 实现 `expand_collect.py`
2. 在 `browser_tools.py` 中注册 `expand_collect` 工具
3. 更新 `vl.py` 新增 `scroll_complete` mode
4. 集成测试：使用长列表页面测试滚动稳定性检测
5. 更新 `strategy_bank.json` 中 `web_scrape.collection.repeated_dom` 策略

### Phase 4: Type-Verified (1-2 天)

1. 实现 `type_verified.py`
2. 在 `browser_tools.py` 中注册 `type_verified` 工具
3. 更新 `vl.py` 新增 `form_verify` mode
4. 集成测试：使用表单页面测试输入验证
5. 更新 `strategy_bank.json` 中 `browser_action.form_interaction.axtree_input` 策略

### Phase 5: 端到端验证 (1-2 天)

1. 完整的端到端测试覆盖三个 micro-loop
2. 性能基准测试：对比有/无 micro-loop 的 step 消耗
3. 安全审计：确认 auth/paywall 场景绝不自动操作
4. 文档更新

---

## 八、测试策略

### 8.1 单元测试

```
tests/
  test_micro_loops/
    test_axtree_search.py       # AXTree 解析、节点搜索
    test_overlay_dismiss.py     # 遮罩检测逻辑、安全边界
    test_expand_collect.py      # 稳定性判断、mode 区分
    test_type_verified.py       # 输入验证、重试逻辑
```

### 8.2 关键测试用例

#### Overlay Dismiss

| 用例 | 输入 | 预期行为 |
|------|------|---------|
| Cookie banner + 关闭按钮 | `overlay_detector` 报告 `cookie_banner` + AXTree 有 "Accept" 按钮 | 自动点击 "Accept"，验证消失，重试原始动作 |
| Cookie banner + 无关闭按钮 | `overlay_detector` 报告 `cookie_banner` + 无 dismiss 控件 | Escape → 仍存在 → click outside → 验证 |
| 登录墙 | `overlay_detector` 报告 `auth_prompt` | **立即 yield**，绝不自动点击 |
| 付费墙 | `overlay_detector` 报告 `paywall` | **立即 yield**，绝不自动点击 |
| 多层遮罩 | 第一个遮罩关闭后第二个出现 | 递归处理，max_attempts 兜底 |
| 超时 | 所有策略尝试后遮罩仍在 | yield 回 LLM |
| occluded frame | `layers[0].occlusionState === "occluded"` | 用 boundsInRoot 定位，按类型路由 |

#### Expand-Collect

| 用例 | 输入 | 预期行为 |
|------|------|---------|
| 滚动到底 | `scroll_append` + 页面有限内容 | 滚动 3 次后 fingerprint 不变 → completed |
| 懒加载 | `scroll_append` + 懒加载页面 | 每次滚动 fingerprint 变化 → 直到稳定 |
| Load More 按钮 | `click_load_more` + "Load More" 按钮 | 点击直到按钮消失或 fingerprint 不变 |
| 翻页 | `click_pagination` + "Next" 按钮 | 每次翻页 nodeCount 稳定后继续 |
| 滚动触发 overlay | `scroll_append` + 滚动后弹出 subscribe 模态框 | 调用 overlay_dismiss → 如果是 paywall 则 yield |

#### Type-Verified

| 用例 | 输入 | 预期行为 |
|------|------|---------|
| 正常输入 | 输入 "hello" → value="hello" | completed |
| 输入后值不匹配 | 输入 "hello" → value="" | 清除 + 重试一次 → 仍不匹配 → yield |
| React 受控组件 | 输入后 AXTree value 延迟更新 | 用 `dom_attribute` 模式验证 |
| 输入被遮罩 | Input.type 返回 occluded | 调用 overlay_dismiss → 重试输入 |

### 8.3 安全审计清单

- [ ] `NEVER_CLICK_KEYWORDS` 中的关键词永远不会被自动点击
- [ ] `overlay.subtype === "auth_prompt"` 时立即 yield，不尝试任何自动操作
- [ ] `overlay.subtype === "paywall"` 时立即 yield，不尝试任何自动操作
- [ ] `elementFromPoint` 验证失败时绝不执行坐标点击
- [ ] micro-loop 执行时间不超过 `max_duration_ms`
- [ ] micro-loop 重试次数不超过 `max_attempts`
- [ ] 所有自动执行的动作都写入 `agent.trace` 和日志
- [ ] yield 回 LLM 时总是附带 `yield_hint`

---

## 九、性能收益预估

### 9.1 单场景估算

| 场景 | 当前 LLM steps | 加入 micro-loop 后 | 节省 | 每步 token 估算 |
|------|---------------|-------------------|------|----------------|
| Cookie banner 关闭 | 3-4 | 0 (自动完成) | ~85% | ~3K input + ~1K output |
| 滚动收集 5 页 | ~10 | ~2 | ~80% | ~5K input + ~2K output |
| 表单 3 字段 + 验证 | ~9 | ~3 | ~67% | ~3K input + ~1K output |

### 9.2 综合任务估算

以一个典型的 web_scrape 任务为例（访问页面 → 关闭 cookie banner → 滚动收集 → 提取）：

| 阶段 | 当前 steps | 优化后 steps |
|------|-----------|-------------|
| 导航 + 初次 getAXTree | 2 | 2 |
| 关闭 cookie banner | 3-4 | 0 |
| 滚动收集 | 8-10 | 2 |
| 数据提取 | 3-5 | 3-5 |
| **总计** | **16-21** | **7-9** |

**总 step 节省：约 50-60%**

每个节省的 step = 1 次 LLM API 调用 ≈ 5-10K input tokens + 1-2K output tokens ≈ 显著的延迟和成本降低。

---

## 十、风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| overlay_dismiss 误操作（点击了不该点的按钮） | 低 | 高 | NEVER_CLICK_KEYWORDS 白名单 + overlay_subtype 安全路由 + elementFromPoint 验证 |
| expand_collect 在动态页面上无限循环 | 中 | 中 | maxIterations + max_duration_ms + PageFingerprint 稳定性检测 |
| type_verified 在自定义组件上误判 | 中 | 低 | 重试 1 次后立即 yield + VL fallback |
| micro-loop 与 render_recovery 冲突 | 低 | 中 | micro-loop 执行过程中如遇 render_lost，优先让 render_recovery 处理 |
| AXTree 解析在新版 ABCP 上不兼容 | 低 | 中 | axtree_search 使用与现有 `_observe_axtree_state_after` 相同的 regex |
| VL 延迟过高影响 micro-loop 性能 | 中 | 低 | VL 只在确定性策略全部失败后才调用（降级路径），不阻塞主路径 |
