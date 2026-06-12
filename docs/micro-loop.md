结论
你的方向是对的，但要补一个关键限制：browser agent 不能简单等同 coding agent。代码测试通常有强 oracle；浏览器任务是部分可观测、异步、页面会漂移、AXTree 不一定表达视觉遮挡。所以方案成立的前提是：把 loop 做成有触发条件、观察信号、成功判据、失败出口的 harness 微状态机，而不是只写进 prompt。
我看了现有代码，项目已经有雏形，但还不是完整闭环：
page_stats / snapshot_diff / loop_nudge 已经在 BrowserAgent 主循环里注入下一轮模型上下文：agent_harness.py (line 327)
overlay 目前是文本/role 软检测：overlay_detector.py (line 92)
occlusion 会附加 overlay recovery 提示，但不会自动执行：browser_tools.py (line 1927)
现在的 stale AXTree guard 会在调用浏览器前拦截旧 id：browser_tools.py (line 589)，这会挡住你说的“浏览器端旧 id 自动重匹配 + axtreeupdate event”能力。
通用通知基础设施存在：abcp_client.py (line 42)，但 BrowserAgent 还没有把 axtreeupdate 这类事件统一变成本地观察信号。
需要修正的点
DOM.getAXTree 不能单独承担 overlay 判断。很多遮罩没有好 role/name，需要结合 occlusion_blocked、Page.getState、geometry、必要时 VL。
“点击遮罩外”必须是最后手段。需要先用 elementFromPoint 验证点位落在 backdrop/container，而不是登录、支付、provider、同意提交等敏感控件。
web_scrape 里的“每次滚动都会更新 AXTree”不能假设。应该看 snapshot_diff、node delta、semantic delta、row count delta；连续 N 次无变化才判定到底/卡住。
当前 harness 的 stale id 拦截策略和浏览器新能力冲突。这个是第一优先级，否则自动重匹配根本没有机会发生。
实施计划
阶段 1：事件观察层
新增一个轻量 BrowserEventObserver，订阅 NotificationHub，标准化 System.notification，记录最近事件、pageId、event type、suggested_prompt。对 axtreeupdate：若 payload 带新 AXTree/重匹配信息，就更新本地 AXTree epoch；否则至少标记“需刷新 AXTree”。未知 event 不后台乱调 RPC，只提示模型可用 System.describeEvent 查询。
阶段 2：调整 stale AXTree 策略
改 _check_stale_axtree_target：同 pageId、曾经来自旧 epoch 的 Input.click/Input.type/DOM.getText/DOM.getAttribute 可以放行给浏览器尝试自动重匹配；page mismatch、完全没见过的 id、无 pageId 仍拦截。结果中若出现重匹配/axtreeupdate 信息，更新 agent.axtree_ids/axtree_epoch，并把 remap 证据写入 trace。
阶段 3：Overlay 微循环
做 OverlayRecoveryPlanner，触发条件包括 occlusion_blocked、page_stats.overlay、DOM 可见性/遮挡警告。流程固定为：刷新 AXTree -> 找显式 close/dismiss/not now/skip/X -> 点击 -> 刷新 AXTree 验证消失 -> 原动作只重试一次。没有显式关闭时再 Escape；最后才是 verified backdrop click。登录、支付、Cloudflare、人机验证、provider 按钮一律不自动点。
阶段 4：Scroll/Lazy-load 微循环
给 web_scrape 增加一个 bounded scroll_observe 类 helper：Input.scroll -> 等事件或 Page.getState -> DOM.getAXTree -> snapshot_diff -> row/semantic delta。停止条件：目标行数满足、到底、连续 2-3 次无新增、或 challenge/overlay 阻塞。这样比让模型反复“scroll + getAXTree”更像 coding loop。
阶段 5：Form 填写微循环
实现 verified input：定位 label/control -> click/type -> DOM.getAttribute(value) 或 AXTree state 验证 -> 不一致则 clear 重填一次 -> submit 前 Page.getState -> submit 后用 URL/title/DOM text/attribute/VL 验证结果。敏感登录、支付、CAPTCHA 走 HITL，不自动绕过。
阶段 6：评估与防回归
给 traceSummary 增加 recoveryLoopAttempts/success/failureReason/remapCount/scrollDeltaStats。测试用 fake ABCP client 覆盖：stale id remap、overlay close、overlay no close、scroll no delta、form value mismatch。再用现有 trace 对比步骤数和失败类型。
我建议先做阶段 1-3。它们直接解决你提到的 overlay 和 axtreeupdate 新能力，收益最大、风险也最可控。等你确认，我再按这个计划改代码。