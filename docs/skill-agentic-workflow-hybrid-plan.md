# Skill: Agentic + Workflow 混合执行容器

> 版本: v1.21
> 日期: 2026-06-28
> 状态: 设计阶段（终稿候选）
> 依据: ABCP workflow + actions 源码核实（`abcp browser/packages/{workflow,actions}/src/`）+ harness 现有接入点核实
>
> 本文**自洽**，不引用 `decision-skill-workflow-hybrid.md`。
>
> v1.1 修订：① 结果信封——agent 经 browser_call 看到的面**无 `status` 字段**（§1.3.1，端到端核实 exec.ts/def.ts）；② 终端手动选 skill（§5.1）；③ 厘清 Workflow 级与 Hitl 级两套 pause 的组合（§1.5）；④ L0–L4 权威层阶梯（§7.1）；⑤ **简化人类恢复路径**——取消 harness 恢复点复验，人类在 playground 点恢复=playground 调 resolvePause→发 `Hitl.resumed` 即权威，harness 仅等待、不复验、不自调 resolvePause（§8）。
>
> v1.2 修订（P1 源码核实，纠正两处 falsified claim）：⑥ **record_extraction 不是 workflow step**——它是 harness 侧 Python 工具，workflow 引擎 internalRpc 只能调 ABCP `Domain.action`，调不到它；structured-output 改为「workflow 写 scalar variables → harness 返回后落盘」（§1.8/§3/§7/§10）；⑦ **HITL 恢复事件正名**——`Hitl.resumeEvent` 在 ABCP 不存在，真名 `Hitl.resumed`（既是权威 push 事件也是 workflow listen 白名单成员；`Hitl.humanInput` 同样不在 listen 白名单）（§3.3/§8）。
>
> v1.3 修订：⑧ **`Input.drag` 人性化轨迹确认**——基于 Chromium 内核层面优化（非瞬移），visual_self_consistent slider/rotate 求解真实可行（§13.4/§13.6）；⑨ ~~AXTree 取行路径改 `.data.lines`~~（**此条 v1.6 联机实测推翻：正确是 `$cache.axTree.lines`**）；⑩ **P2 参考 skill 落地** `skills/taaft-detail-extract/`（离线 authoring + 编译版 schema 静态校验通过，live 闭环待面板/JWT），P1 交付 `skills/README.md` + `skills/_template/`。
>
> v1.4 修订（**P2 联机实测**，headless 连 localhost:9300 跑通）：⑪ **成功信封 LIVE 确认**——返回 `{observation:"...completed...", data:{runId,results,variables}}`、无 status、变量/插值/autoExtract 机制可用、onError:stop 生效（§1.3.1）；⑫ **失败 takeover 机制修正**——失败 **抛异常**且 rich payload 不过 JSON-RPC error 边界，必须 catch + `Workflow.getStatus(runId)` 取 `{failedStepPath,error,variables 快照,results[]}`（§1.3.1/§9.2，实测可恢复）；⑬ **`if exists` 守卫踩坑修正**——transform 无命中写空串、`exists` 对空串判 true → 空 id 进 click 报 Invalid params，id 守卫改 `matches "[0-9a-fA-F-]+:\d+:\d+"`（§7，已修 skill+template+§3.2）；⑭ TAAFT skill 完整 10 步**联机 example.com 跑通**（无命中分支优雅跳过）；TAAFT 真站 live-pin 仍受 Cloudflare 新标签页超时阻塞（需预热标签页）。
>
> v1.5 修订（**P3 蒸馏辅助落地**）：⑮ `skills/_tools/distill_trace.py` + `skills/DISTILLATION.md`——trace→workflow.json 自动化（丢失败/恢复噪声、切单对象、url→$vars、id 经 purpose 推 label 去硬编码+matches 守卫、extract→scalar、record_extraction 留 harness 后置），在真实 TAAFT trace（ecrett-music，28 browser_call→12 步 draft）上跑通且过编译版 schema 校验；label 歧义（tab vs 同名 content）/CSS selector 耐久性/多对象循环由 report 标注交人工（§4.1/§11 P3）。
>
> v1.6 修订（**TAAFT 真站联机实测**，用户手动可过 Cloudflare 的预热标签页）：⑯ **AXTree 取行路径修正（推翻 v1.3 ⑨）**——engine internalRpc 已解包 data 层，正确是 **`$cache.axTree.lines`**（`.data.lines` 实测返回空、`.lines` 命中），demo 的 `.data.lines` 是另一套 transport，已全线改回（skill/template/distiller/README/§3.2）；⑰ **runtime 重解析 LIVE 确认成立**——真站 ecrett-music 上 reviewsTabId/prosConsTabId/qaTabId 三个 tab 全部运行期解析命中（id 每次加载都变，证明不可冻结），点击成功、re-navigate 不触发 Cloudflare 重挑战；⑱ **TAAFT 内容读 live-pin 结论**——TAAFT 评论区**无 `[role=tabpanel]`/无 review CSS class/无单一文本容器**，内容是 `heading "Reviews"` 下一串扁平 statictext，单发 `getText(selector)` 取不到；需内容拼装策略（多节点读 或 `Runtime.evaluate` innerText），属待定设计点。
>
> v1.7 修订（**Runtime.evaluate 方案真站抽到真数据 + 解禁**）：⑲ **TAAFT skill 改 `Runtime.evaluate` 一发取三段**（按 heading 文本定位 Reviews/Pros/Q&A 返回 innerText→extract 进 scalar），真站 ecrett-music 实测 prosConsText 1613 字、qaText 2000 字（真数据）；**内容全在 DOM，tab 只是锚点，skill 砍到 4 步**（navigate→listen→Escape→evaluate）；reviews 正文细化为次要 TODO；⑳ **`Runtime.evaluate` 契约**——`expression` 是**函数体、必须 `return`**（裸表达式返回 null），且 **workflow `extract` 路径不带 `data.` 前缀**（引擎已解包，写 `"reviews"` 非 `"data.reviews"`，与 `.lines` 一致）；㉑ **harness 层解禁 Runtime.evaluate**——从 `strategy_bank/strategy_bank.json` 的 `avoid_tools` 移除（avoid→merge 进 worker_contract.forbidden_methods 会被 browser_call 拒），保留 `cautioned_tools`（软提醒，非禁）。
>
> v1.8 修订（**skill 接进 harness——P6 核心 + §9 运行器落地**）：㉒ `harness/skill_registry.py`（SkillRegistry.load/match/get，确定性预过滤+域通配+fields 子集+终端手动选）+ `harness/tools/browser_tools/workflow_skill.py`（build_execute_params / run_skill_workflow 两调模式 / check_success_contract）；`tests/test_skill_integration.py` 15 测试全绿；真站 ecrett-music 端到端 **match→run→contract→真数据**（reviewsText 1377/prosConsText 1613/qaText 2000）联机跑通；剩 LeadAgent/worker 调用点接线。㉓ reviews 正文 JS 调干净：`Runtime.evaluate` 把 reviews 抓取 scope 到 `.comment_content/.comment_body`（剥评分件），真站抽到真实评论。
>
> v1.9 修订（**skill 调用点接进 worker 编排——P6 闭环**）：㉔ `harness/skill_dispatch.py`（maybe_run_skill_fast_path：select_skill 显式 skill_id 优先/否则自动匹配 → derive_variables → run → 契约 → record_extraction 落盘 → completed_via_skill 跳过 LLM）接进 `spawner._run_browser_worker`（harness.run 前），config `skill_fast_path_enabled` 门控默认开，fail-safe 回落慢路径；27 单测全绿 + 真站 wired dispatch 联机落盘真数据。㉕ **修隐性 bug**：fallback.yaml flow 序列含 `[`（`status.results[-1].step`）→非法 YAML→registry 静默吞成 {}→success_contract gating 失效；已加引号 + registry 坏 YAML 打 stderr 警告。
>
> v1.10 修订（**P5 自愈机制 + ④ LeadAgent 显式填 skill_id/skill_variables**）：㉖ **P5**：`harness/skill_health.py`（per-skill outcome 跟踪 + 按 maintenance 阈值自动禁用 rotted skill，dispatch 跑前查 is_disabled、跑后 record；状态存 `skills/.skill_health.json`）+ `harness/skill_heal.py`（write_candidate→canary_validate→promote/reject + .heal_history + 成功后 health.reset；候选 workflow 由 agent 兜底产出）。㉗ **④**：`harness/skill_contract.py` 的 `enrich_worker_contract_with_skill` 接进 `lead_tools._lead_spawn_browser_agent`（spawn 前），显式 skill_id 优先、否则按 contract 维度+task URL 自动 stamp skill_id+skill_variables；worker_contract schema 文案提示 LLM 可显式设。㉘ **修 required_filled 语义**：required = 「steps 里被 `.<key>` 引用」的变量（非「template 默认空」）——rank/productName 是输出透传不算 required，detailUrl 被引用才算。37 单测全绿 + 真站 enrich→dispatch+health 端到端联机抽到真数据。
>
> v1.11 修订（**自动生成候选闭环——agent 兜底成功→产出候选 workflow→self_heal**）：㉙ `harness/skill_autoheal.py`（`distill_trace_to_workflow` 复用 P3 蒸馏器跑内存 trace→候选 workflow，seed 活 skill 的 variable_template/errorConfig；`is_degraded` 门控；`maybe_autoheal_from_trace` 编排 distill→`self_heal`，best-effort 全吞错）接进 `spawner._run_browser_worker`：**快路径回落（skill_answer is None）+ 慢路径 validated_done + skill 匹配 + degraded（health 有近期失败）→ 蒸馏慢路径 trace 成候选→canary 门控 promote**；新 config `skill_auto_heal_enabled` 默认开；`skill_dispatch.resolve_skill_and_variables` 复用同一选择+取变量逻辑（不从快路径穿状态）。**蒸馏器增强**：`Runtime.evaluate` 步从返回对象的 keys 合成 `extract`（`{k}Text→k`）+ `onError:continue`，使自动候选真能填变量（标量返回则保留 verbatim 由 canary 拒）。候选 `workflow.v*.json` 归档进 .gitignore。43 单测全绿（+TestDistillToCandidate 2 +TestAutoHeal 4）。
>
> v1.12 修订（**P4 pause-resume——改"交接慢路径"（推翻 §8.4 并发驱动假设）**）：㉚ **falsified §8.4 假设修正**——§8.4 假设 harness 能在 `Workflow.execute` 阻塞期间**并发** `Workflow.pause/resume` 来 flip pauseController，但 `abcp_client.py:324` 把每次 `call()` 串行在单 `_call_lock`+单 `_pending_call` 上：execute 阻塞在 paused 步时**同连接发不出任何控制调用**（pause/resume/resolvePause 都会被锁住到 execute 返回）；只有**通知**经 NotificationHub 独立于锁照常到达。故 P4 落为 **observe-only 交接**：`harness/skill_pause.py`（`HitlOnsetMonitor` 订阅式只观测 paused/挑战 onset 通知 + `classify_run_for_hitl` 后置按 paused/挑战 marker 兜底判定）接进 `skill_dispatch`——skill 跑被 HITL/挑战打断 → 判 `hitl_required` → **dispatch 返回 None → BrowserAgent 慢路径接管那张仍 paused 的页**（页在 worker slot fleet 内、慢路径可感知，慢路径已有 VL+人类兜底 HITL 机制 `harness/hitl.py`）。**铁律**：HITL 中断 ≠ skill 失败——**不记 health 失败、不触发 self-heal**（plain 失败才记，靠 paused/challenge marker 精确区分）。成功的 run 永不交接（数据已回=挑战已清）。59 单测全绿（+16：onset 检测 6/classify 5/monitor 2/dispatch 交接 3）。**不开第二连接、不在 workflow 内 resolvePause**（守 §8.3 铁律）。VL-first 自动解（§6 最优解）需第二控制连接，因单连接串行约束暂缓。
>
> v1.13 修订（**主动控制——第二控制连接，execute 阻塞期可随时驱动 pause/resume**）：㉛ 用户要"不只观测、要能随时控制调用"。单 `_call_lock` 下唯一出路=**开第二 ABCP 连接**：`harness/skill_control.py` 的 `ControlChannel`（`from_browser` 镜像主连接 config/auth 开第二 `ABCPClient`；`pause(runId)`/`resume(runId)`/`call`；`try_open` 永不抛=失败即降级）+ `run_workflow_with_control` 协程（主连接以 task 跑 `Workflow.execute`，**主连接 NotificationHub 旁路锁照常观测** onset → `asyncio.wait(FIRST_COMPLETED)` 在"execute 完成"与"onset 到达"间竞速 → 第二连接 `Workflow.pause(runId)` → `on_pause` 解决 → `Workflow.resume(runId)` → execute 跑完剩余步；每个 pause cycle 全 try/except，失败留 paused→下游 classify 交接）+ 默认人路解析器 `resolve_via_hitl`（`Hitl.requestPause`→复用 `harness/hitl.py:wait_for_hitl_resume` 等 `Hitl.resumed`，VL-first 作 `on_pause` 扩展点）。接进 `skill_dispatch._run_skill_with_optional_control`：flag 开且第二连接能开→主动控制，**任何控制失败→降级 observe-only 交接**（P4 v1.12 仍是安全网）。新 config `skill_workflow_active_control_enabled` **默认 OFF**（跨连接 runId/page 可达性面板未验证）。66 单测全绿（+7：ControlChannel 3/coordinator pause-resolve-resume·未解·无 pause 3/gating 1）。**⚠️ 跨连接控制（连 B `pause(runId)` 是否冻住连 A 的 execute？连 B 是否见主连接的 page？）live 未验证**，待面板起来验证后才 flip on。
>
> v1.14 修订（**面板联机实测——㉛ 引擎级 pause/resume 跨连接 DEAD，但页面级控制 WORKS**）：㉜ 三连接探针实测（probe_xconn/probe_scope/probe_hitl_xconn）钉死跨连接语义：**`runId` 是 session-bound**——`Workflow.pause`/`resume`/`getStatus(runId)` 从**非 owner 连接**一律 **`-32005`**（连"已完成的 run"也查不到；只有 owner 连接 `getStatus` OK），而 owner 连接正阻塞在自己的 `execute` 里 → **任何连接都无法 pause 一个运行中的 workflow**，**㉛ 的 `Workflow.pause/resume(runId)` 第二连接方案被实测推翻**。**但页面级跨连接全通**：第二连接对主连接的 page 跑 `Page.getState`/`Page.navigate`/`Hitl.requestPause`/`Hitl.resolvePause` **全部 OK**。结论:execute 期间"能控制"的是**页面**不是**workflow 引擎**——第二连接可在 execute 阻塞时 VL 解挑战或 HITL 暂停/恢复**页面**;要让 workflow 借此继续,需 skill **显式 listen Hitl.resumed 边界**(否则挑战步 onError:stop 先终止)。`harness/skill_control.py` 的引擎-pause 协程已知失效(flag 默认 OFF→`Workflow.pause` -32005→自动降级 observe-only 交接,不破坏快路径);页面级主动控制的正解待定(见下方决策)。**`ControlChannel`+`from_browser`+`resolve_via_hitl`(Hitl.requestPause→等 Hitl.resumed)本身用的是已验证可跨连接的页面级原语**。
>
> v1.15 修订（**主动控制 pivot 到页面级——落地（用户定向）**）：㉝ 据 ㉜ 实测重写 `harness/skill_control.py`:删掉死的 `Workflow.pause/resume(runId)`;`ControlChannel` 改提供**页面级** `request_pause(pageId)`/`resolve_pause(pageId)`(都是已验证跨连接 OK 的 `Hitl.requestPause/resolvePause`);`run_workflow_with_control` 不再冻引擎——观测到挑战 onset 就在第二连接跑 `on_pause`(默认 `resolve_via_hitl`:页面级 requestPause→等 `Hitl.resumed`),**workflow 靠自身的"挑战门控 `listen Hitl.resumed` 边界"继续**(结束 page pause 的同一个 `Hitl.resumed` 也满足该 listen)。skill 授权模式:`Runtime.evaluate` 按 title/url 标记检测挑战→`challengeFlag`(happy path 为空,**`if matches "yes"` 跳过=零开销**)→命中才 `listen Hitl.resumed`(1200s/onTimeout continue)。**关键坑**:`challengeFlag` 是运行期 var,**不可在 `variables` 声明**(否则 `_referenced_vars` 把它当必填输入→`required_filled` False→快路径被跳过;与 reviewsText 等 extract 产物一致)。已把该边界接进 TAAFT skill(6 步)+ `_template`(7 步),两者过编译版 `validateWorkflowSteps`(`Hitl.resumed`∈ `WORKFLOW_LISTENABLE_EVENTS`、`matches`∈ 算子表)。dispatch 仍 flag 默认 OFF + 失败降级 observe-only。68 单测全绿(ControlChannel 4:页面级委派/无引擎 pause-resume 方法/连接失败/无 config;coordinator 4:挑战解决续跑·未解·解析器抛错·无挑战)。**端到端 live(真挑战+人工清)待跑——需用户在 playground 清挑战触发 `Hitl.resumed`**。
>
> v1.16 修订（**VL 模型优化——§13.4 `captcha_solve` 落地 + VL-first 解析器接进页面级主动控制**）：㉞ **VL `captcha_solve` mode**（`harness/vl.py`）：prompt 让 VL 分类挑战并**只对视觉自洽型**给 `solve_plan`(归一化 0-1000 步:slider drag / rotate drag_arc / grid·click_target click / text_ocr type)。`_finalize_captcha_solve` **代码层强制诚实短路**——`challenge_category != visual_self_consistent`(behavioral_risk/unknown)一律 verdict→`unsolvable`+`solve_plan=[]`(不管模型说啥;行为型打分轨迹/时序/指纹,硬刚升级难度或封号),`solvable` 但无可用步→`uncertain`;逐步校验+丢弃畸形步。新 config `vl.captcha_solve_max_retries`(默认 2)。㉟ **VL-first 解析器**(`harness/skill_control.py`)`resolve_via_vl_then_hitl`=§13.4 闭环:截图→VL `captcha_solve`→视觉自洽+solvable→`solve_plan_to_input_calls`(纯函数,归一化→CSS:`css=norm/1000*innerWidth`,实测 `Input.drag {x,y,dx,dy}`/slider、`{x,y,toX,toY}`/rotate、`Input.click {x,y,clickCount}`、`Input.type {text,clear,delay}` 先 click 聚焦)→**逐步 `elementFromPoint` 安全门**(命中 login/pay/submit/oauth 拒绝)→第二连接执行 Input→`Page.getState` url/title 验挑战消失(L0,VL 不自证)→`control.resolve_pause`(发 `Hitl.resumed`→workflow 续跑);**unsolvable/behavioral/uncertain/重试耗尽/VL 关→降级 `resolve_via_hitl` 人路**。I/O 全可注入(截图/VL/metrics/safety/exec/verify)→编排+翻译单测化;默认 wiring 用已实测原语(`Page.screenshot` 返 `data.savedPath` 文件路径直喂 VL;`Runtime.evaluate` 取 innerWidth/elementFromPoint)。dispatch 据 `vl.enabled` 选 VL-first 或人路解析器。**83 单测全绿**(+15:captcha_solve finalize 7/翻译 1/VL loop 5/fallback 2)。
>
> v1.17 修订（**真 CAPTCHA 端到端联机实测通过**）：㊱ yue-accelerator 手机号登录滑块（"请按住滑块拖动到最右边"）全链路真实跑通：真 qwen3-vl `captcha_solve`→`solvable/slider/visual_self_consistent`（诚实区分生效）+drag plan；`solve_plan_to_input_calls` 归一化→CSS `(483.5,377.5)` 精确命中把手；`_default_safety` 门通过；`Input.drag`（内核人性化轨迹）→**滑块消失、验证通过、短信已发（"47s 重新获取" 倒计时确认）**。§13.4 闭环 live 验证成功。
>
> v1.18 修订（**VL Role A —— visual_locate + bbox→id 提升落地 + live 证**）：㊲ **先验证后做**——两探针钉死 Role A 地基:① `DOM.getAXTree` lines **确实带 `# @x,y,w,h`** 视口 px bbox（如 `[3:13:13] link "Learn more" # @512,398,164,39`，仅 positioned/interactive 元素有）;② **AXTree bbox 空间 == `Page.screenshot` 像素空间**（探针双 2560×1600 一致;`innerWidth` headless 读 0 是量测怪象，无关——VL grounding 在截图上）→ `px = norm/1000 * shotW` 与 bbox 同空间，containment 反查得 id。新模块 `harness/vl_locate.py`:`parse_axtree_bboxes`（正则抽 `[id] role "name" # @x,y,w,h`）+`point_to_id`（**最小面积**含点框=最具体元素;**排除 rootwebarea/webarea/document** 容器角色→只剩根含点=blind spot）+`promote_locate`（norm→截图 px→id;命中给**耐久 id**，未命中给 `cssPoint=px/dpr` 坐标兜底）+`locate_target` 编排（截图→VL `visual_locate`→promote;`is_consequential` 上浮供调用方挡敏感目标）。vl.py 加 `visual_locate` mode（找任意描述目标返归一化点+安全标），config `visual_locate_enabled` 默认 OFF。**promote-then-heal 纪律**：坐标绝不进 skill，命中即转耐久 id。**96 单测全绿**（+13:bbox 提升 5/finalize 3/locate_target 5）。**live 证**:example.com 上真 qwen3-vl 视觉定位 "Learn more" 链接→`promote_locate` 反查得 canonical id **`3:13:13`，与 AXTree ground-truth 完全一致**（conf 0.98）。**已接进 BrowserAgent 慢路径** `visual_verify` 工具:`mode=visual_locate` 命中→`_promote_visual_locate`（getAXTree+dpr→`promote_locate`）→给 `resolvedId`（耐久句柄,指示 agent 用 id 而非坐标操作）或 `cssPoint`（blind spot 一次性坐标兜底）,gated `visual_locate_enabled`、schema mode 描述已加、best-effort 不破坏其他 mode。98 单测全绿（+2 工具路径 wiring）。
>
> v1.19 修订（**VL Role B —— contract_verify 视觉裁判 落地 + live 证**）：㊳ vl.py `contract_verify` mode（判 `success_contract.visual_checks`→`satisfied/violated/uncertain`+`failed_checks`）+ `harness/skill_visual_contract.py` `evaluate_visual_contract`（变量契约过后截图→VL 判可见末态;I/O 可注入）接进 `skill_dispatch`：变量契约过 → 视觉契约 → **仅 `violated` 否决**（记 health 失败+不落盘+返 None 交慢路径）。**权威纪律（§7.1 阶梯）**：VL 是 L4 弱层——`uncertain`/截图失败/VL 关一律 **fail-open**（绝不否决已过的变量契约,VL 不能凭"拿不准"沉掉结构性成功）。new config `contract_verify_enabled` 默认 ON。skill `fallback.yaml` 的 `visual_checks` 现在真执行（之前只声明不跑）;`visual_verify` 工具也支持 contract_verify mode。**110 单测全绿**（+12:finalize 4/evaluate 6/dispatch 否决·放行 2）。**live**:example.com 真 qwen3-vl 正确判 "Example Domain"+无挑战→`satisfied`、"Payment Successful"不在→`violated`。**VL 四角色:A✅ B✅ C✅ live 证 / D 部分(overlay_classify)**。
>
> v1.20 修订（**P4+VL "合龙"——核心机制 live 证全（分两半）+ 主动控制补 in-page 轮询/signal_resumed**）：㊴ **决定性 probe**:工作流 `listen Hitl.resumed`（连 A 的 execute 内）**确实收到连 B 的 `resolvePause` 发出的 `Hitl.resumed`**（probe_listen_resumed:execute 8.8s 续跑,远短于 30s timeout）——**这同时解了长期悬置的"Hitl.resumed live"问题**（不再依赖 playground:B 自己 requestPause→resolvePause 即可发）。㊵ 据此补 `skill_control.py`:`ControlChannel.signal_resumed`（requestPause→resolvePause 发 Hitl.resumed,让 in-page 解题后续跑 workflow listen;page 本未 HITL-pause）+ `_wait_for_challenge` **双腿挑战检测**（导航级走主连接**通知**;in-page 控件走第二连接**主动轮询** `poll_challenge_fn`——主连接阻塞在 execute,轮询必须在第二连接跑）+ `run_workflow_with_control` 加 `poll_challenge_fn`/`poll_interval`;`_vl_solve_loop` 成功改调 `signal_resumed`。**coordinator e2e live 证**:真连接上 `Workflow.execute` 阻塞 `listen Hitl.resumed`→coordinator 轮询检测→`on_pause`→`signal_resumed`→workflow 续跑（`continued:yes`,11.3s«60s timeout,intervention `resolved:true`）。**至此每条链路均 live 证**(workflow-listen←B-resumed / 真 VL 解真滑块 / coordinator 轮询→signal_resumed→续跑),**唯一未串成单进程的是真 yue 站导航**——卡在**间歇性 headless 视口退化**（某些 session `window.innerWidth`/`getBoundingClientRect`→0 而页面实渲染 1280,坐标点击落空;cap1-8 那次视口健康才成）,属环境怪象非机制缺陷;正解=用 **Role A `visual_locate`** 驱动 yue 导航(VL 在真截图上 ground,免疫该怪象)。110 单测全绿。active-control flag 仍默认 OFF（in-page 挑战检测需 per-skill `poll_challenge_fn` 接线）。
>
> v1.21 修订（**VL Role D —— 全局视觉仲裁器 落地 + live 证**）：㊶ `harness/vl_arbiter.py`（`route_failure`/`is_visual_failure`/`arbitrate`）把 browser_call 失败按 errorClassification type+文本路由到正确 VL role 并出恢复建议,统一 A/B/C/overlay 于一个失败驱动入口;**非视觉类(timeout/contract_error/page_crashed)不调 VL**;locator 失败复用 Role A `locate_target` 提升耐久 id,consequential 目标降级 hitl。122 单测绿（routing 4+arbitrate 8）。live:example.com locator-not-found→`retry_by_id 3:13:13`。㊷ **hot-path auto-trigger wiring 完成**:`_maybe_vl_arbitrate` 接进 `_execute_browser_capability_tool`(overlay auto-intercept 之后),视觉类失败 + `vl.arbiter_enabled` → arbiter → 恢复建议挂回 result + `next_instruction`(非视觉/VL 关 no-op,per-worker 上限,internal 不触发);config `arbiter_enabled` 默认 OFF;126 单测绿(+4);live 模拟失败 Input.click→`vlArbiter:{retry_by_id,3:13:13}`。**VL 四角色全部 A✅ B✅ C✅ D✅ 能力+live+wiring（C 真 CAPTCHA；A/B/D example.com）**。
>
> v1.22 修订（**收尾未完项——Role C 独立门 + per-skill in-page 轮询接线 + 本地测试债清零**）：㊸ **Role C `captcha_solve_enabled` 独立 opt-in（§13.7 落地）**：`VLConfig.captcha_solve_enabled`（默认 OFF）+ `skill_dispatch._should_vl_solve(vl_config)`（`enabled && captcha_solve_enabled` 才选 VL-first 解题器，否则人路）——自动解 CAPTCHA 不再随 `vl.enabled` 一并打开（acting-on-challenge 是 ToS/法律层面最敏感的 VL 动作，必须显式开）；Role A/B/D 不受影响。㊹ **补齐 v1.20 留口 per-skill `poll_challenge_fn` 接线**：`skill_control.make_challenge_poller(skill)`——从 skill 自身的 `if $vars.<flag> matches → listen Hitl.resumed` 边界反查出那条 `Runtime.evaluate` 的 challengeFlag 表达式，在第二连接上**原样跑同一段 JS** 检测 in-page 挑战（构造上忠实，skill 无边界→返 None 不轮询，任何错→None fail-safe）；`_run_skill_with_optional_control` 据此把 `poll_challenge_fn` 喂进 `run_workflow_with_control`——主连接阻塞在 execute 期间，第二连接主动轮询发现"不触发导航的 in-page 控件"。**至此 v1.20 那条"active-control flag 仍默认 OFF（需 per-skill poll_challenge_fn 接线）"的最后接线已补齐**（flag 仍默认 OFF，需真站 e2e 才 flip）。135 单测绿（+9：captcha 门 2 / poller 6 / coordinator in-page 腿 1）。㊺ **本地测试债清零**：`tests/` 整目录 gitignore（本地工作区），`97f105e` 重构/回滚后遗留一批测已删模块/特性的脏测——pytest 因 7 个 import 错**整体 collection abort**、unittest `5 fail+13 err`。清理：① 测**已删模块**的 8 文件改名 `obsolete__*.py`（脱离 `test_*` 发现）② 测**已删 helper/特性**的个别方法 `@unittest.skip("obsolete: … removed in 97f105e")`（保留同文件对现存代码的有效覆盖）③ 个别**陈旧断言**就地校准到现行行为（targetRef→selector/id、offload 现 strip 良性 suggested_prompt、collect_items 去掉 selectorQuality 块）④ 1 条 `_loop_interrupt_from_result`（skipped_stale_challenge_verdict autoHitl 仍被当中断）先标 **POSSIBLE REGRESSION** 显式 surface 而非静默掩盖。`tests/README.md` 记约定。**结果：pytest 566 passed / 7 skipped / 0 failed；unittest Ran 473 OK(skipped=7)**——两 runner 全绿，本会话 0 新失败。
>
> v1.28 修订（**代码组织整理——VL / skill 收进子包（用户定向）**）：⓽ 把散在 `harness/` 顶层的 13 个模块按职责收进两个子包:**`harness/vl/`**（`core`←vl.py / `arbiter`←vl_arbiter.py / `locate`←vl_locate.py；`__init__` re-export core,故 `from harness.vl import visual_verify_image` **不变**,子角色走 `harness.vl.arbiter`/`harness.vl.locate`）+ **`harness/skill/`**（`registry`/`dispatch`/`control`/`pause`/`health`/`heal`/`autoheal`/`contract`/`visual_contract` ← 各 `skill_*.py`，`workflow` ← `browser_tools/workflow_skill.py`）。外部 import 统一 `from harness.skill.<module> import`（53 处 sed 批量 + 4 处 `from harness import skill_X` 改 `as skill_X` 别名）。**关键坑**:`registry`/`health`/`autoheal` 的 `Path(__file__).resolve().parent.parent / "skills"` 因文件下沉一层失效（指到 `harness/skills`）→ 改 `.parent.parent.parent`（回项目根 `skills/`，registry 重新加载到 taaft-detail-extract）。**逻辑零改动**,577 passed/6 skipped/0 failed。⚠️ scratchpad e2e 脚本（p4_full.py 等）的 `from harness.skill_control import` 需手改为 `harness.skill.control` 才能复跑。

> v1.27 修订（**第二轮 review 排查——2 条真问题修复 + 2 条排查澄清（虚惊/不成立）**）：⓸ **glm【中】"provenance 强校验让 TAAFT 快路径恒降级" → 排查=虚惊不成立**：`task_control` 的 field_provenance validator 确实对**敏感字段**自动注入（`SENSITIVE_PROVENANCE_FIELD_MARKERS` 含 `rank`，TAAFT 6 字段里**仅 rank 命中**），但 `_is_advisory_record_failure` 把 `field_provenance`/`min_rows` 列为 **advisory（非 blocking）**——所以 row 缺 `rankEvidenceText` 时 record_extraction 的 `status` 仍是 `done`/`validationPending`（**不是 `needs_fix`**），`check_persisted_contract` 只对 `needs_fix` 否决 → **快路径正常通过，不恒降级**。已在 check_persisted_contract docstring 固化此结论。⓹ **glm 建议清理 `_result_has_auto_hitl` → 不成立**：它在 navigate-retry 路径仍有 2 个调用点（browser_tools `__init__.py:1299/1316`），只是 `_loop_interrupt_from_result` 改用了更严的 `_auto_hitl_is_actionable`（v1.23）；不能删。⓺ **chatgpt 异议"#3 匹配过宽不能只靠 #2 缓解" → 成立，已修**：#2 落盘契约只防"错数据 completed"，挡不住误命中的**副作用**——打开页/导航/可能触发挑战/消耗 slot，**最严重是 contract-unmet 记 `health.record(False)` 污染健康分 → 反复误命中可把一个好 skill 误 disable**，还会参与 self-heal 判断。修：`select_skill` 加 **domain-only 命中保护**——task 侧 task_type/stage_hint/fields **全空时不自动命中**（返回 None → 慢路径，doc §5.2 确定性预过滤需 task_plan）；显式 `/skill <id>` 仍命中（人工 opt-in），任一任务维度即重新启用匹配。⓻ **chatgpt 小缺口 `persisted_rows_at_least` 未检查 → 已补**：`check_persisted_contract` 加 `row_count`（快路径单行，>1 要求 → 回落）。⓼ **低危（注释/不改）**：`_default_verify` 只查 url/title（导航级），对 in-page 视觉挑战默认放行 → 加注释明确"in-page 必须传自定义 verify_fn（yue slider e2e 即如此），max_retries+contract_verify 兜底"；`_ensure_page` 不复用现有页是 cleared-tab 通用默认（可接受）。+2 回归测试（domain-only 不命中 / persisted_rows）。**577 passed/6 skipped/0 failed**。

> v1.26 修订（**外部 review（chatgpt+glm）定夺——修 5 条通用层边界/泛化缺陷，2 条记录为权衡**）：⓵ 两个 reviewer 的 8 条 findings **逐条核实全部属实**,且都是 **harness 通用层**的边界/泛化缺陷（非 TAAFT 特化——registry/contract/health/heal/pause/control/VL 四角色经核实均通用）。**修 5 条**:**(a) `allow_auto_captcha` skill 安全门**（doc §13.8）——`_should_vl_solve(vl_config, skill)` 增第二道 AND 门读 skill frontmatter,默认 deny;TAAFT 声明 `false` → 即便全局双门开也不自动解,走人路。**(b) 落盘契约执行**——新 `workflow_skill.check_persisted_contract`(fields_required/fields_nonempty 查 row + `record_extraction` 返回 `status:needs_fix` 否决,后者透传覆盖 provenance/schema 校验),接进 dispatch 落盘后:变量契约过 ≠ 落盘行完整,不完整/验证失败 → 回落慢路径（此前只查 workflow 变量,会保存字段不全的行仍报 completed）。**(c) text_ocr safety 拦截 bug**——`_vl_solve_loop` 改为只对**带坐标**的 call 过 `elementFromPoint` 安全门;无坐标的 `Input.type`(text_ocr focus-then-type,跟在已校验的聚焦点击后)不再被 `_default_safety`(x None→False)误 abort。**(d) `build_extraction_row` 去 TAAFT 耦合**——字段名改由 skill `fallback.yaml success_contract.variable_to_field` 声明（无声明=变量名原样,**不再硬剥 `*Text`**）,provenance 从 skill 自己的 extract 步 action 推（不再硬编码 `Runtime.evaluate`）;TAAFT fallback.yaml 加 `variable_to_field: {reviewsText:reviews,prosConsText:prosCons,qaText:qa}` 保持落盘字段名。**(e) URL 变量名泛化**——`_url_variable(skill)` 从 `Page.navigate` 步反推 `$vars.<x>`,不再死认 detailUrl/targetUrl/url 三名。⓶ **记录为权衡(不改)**:skill 匹配过宽（task 侧缺 task_type/stage_hint/fields 时只靠 domain 命中,chatgpt #3）——不收紧匹配（破坏现有命中风险高）,靠 (b) 落盘契约**间接缓解**（误命中跑了也因 fields/needs_fix 不过而回落,不产错数据）;蒸馏器 `detailUrl` 默认（glm #3）属 authoring 工具,人工 review,低优先;`_find_challenge_boundary` 只认 Runtime.evaluate 产 flag 是 template 约定（authoring 纪律）。⓷ **测试**:+7 回归测试（allow_auto_captcha 门/无映射不剥离+provenance/落盘契约 fields+needs_fix/url 反推/text_ocr 不被拦）;修了 DispatchBrowser mock 让 Workflow.execute 真实透传输入 variables（P2 live 实证 variables 含输入）。**575 passed/6 skipped/0 failed**。

> v1.25 修订（**P4 真站单进程端到端 PASS ✅✅——"视口退化"真因=面板 quirk #7，getAXTree 一解即通**）：㊾ **决定性反转**:v1.24 把 P4 卡点归为"间歇 headless 视口退化（环境怪象）"是**误判**——真因是 **abcp-panel-quirks #7**:fresh tab 在**首次 `DOM.getAXTree`/`Page.screenshot` 之前没有 layout**（`window.innerWidth/innerHeight`=0、`getBoundingClientRect` 全 0、坐标点击落空）。我之前所有探测都在 navigate 后**直接读 iw**（漏了触发 layout 这步）才看到 iw=0 并误以为退化。**修复=导航前补一次 `DOM.getAXTree`→layout 初始化→iw 0→1280（实测 3/3 page 全修复）**，坐标点击随即生效。㊿ **P4 真站单进程闭环 LIVE PASS**（yue 手机号登录滑块，`scratchpad/p4_full.py`）:① `Page.create`(about:blank)→`Page.navigate`(yue)→**`DOM.getAXTree` 触发 layout（iw=1280）**;② DOM `getBoundingClientRect` 中心坐标导航（精确文本匹配避开导航栏容器）:登录→modal opened True→手机号登录 tab→输入手机号 13302424940→发送验证码→**SLIDER appeared True**;③ workflow（连 A）`Workflow.execute` 阻塞 `listen Hitl.resumed`;④ 第二连接（连 B）`poll_challenge_fn` **检测到 in-page 滑块**;⑤ `resolve_via_vl_then_hitl`→真 qwen3-vl `captcha_solve` 判 **`solvable`/`visual_self_consistent`/`slider`**（诚实区分生效）→`solve_plan`→`Input.drag`（内核轨迹）→**`slider gone=True`**;⑥ `signal_resumed`（requestPause→resolvePause 发 `Hitl.resumed`）→**workflow listen 续跑 `succeeded:True`**、intervention `resolved:true`。**P4 全链路（视口修复→导航→execute 阻塞→第二连接检测→VL 解滑块→signal_resumed→续跑）真站单进程一气呵成 PASS。** 唯一 minor:续跑后即时 evaluate 的 `sentFlag` 空（解锁→yue 发短信+倒计时文案有网络延迟，workflow 验证步太快;cap1-8 已确认同路径真发短信）。**至此 §6/§8 主动控制 + VL Role C 在真站真挑战上单进程闭环 PASS**;active-control flag 可据此考虑 flip（仍建议默认 OFF + 显式 opt-in）。
>
> v1.24 修订（**两真站场景重测——P2 完整闭环盖章 ✅；P4 仍卡视口退化（环境），但根因彻底定性**）：㊼ **P2 真站完整闭环 LIVE PASS**：真 `run_skill_workflow` 对真 TAAFT 详情页（ecrett-music）——①**快路径**抽到真数据 reviewsText 1377/prosConsText 1613/qaText 2000 字 + `success_contract ok:true`、challengeFlag 空（无 CF）；②**注入坏步**（extract 前插 DOM.getText deadbeef:99:99 onError:stop）→ `run_skill_workflow` takeover 返 `succeeded:False` + `failedStepPath:5` + 识别失败步 purpose + priorResults 5 步 + variables 快照；③**agent 接管**从失败点续跑剩余真 extract → 恢复全部抽取（reviews/pros/qa 齐）。关键手法:**cleared-tab + Page.navigate 绕 fresh-tab Cloudflare**（Page.create(about:blank)→navigate；navigate **CALL** 报 -32001 超时但页面**实际加载成功**，DOM/AXTree 到位且无挑战）→ 据此给 skill navigate 步加 `onError:continue`（taaft+_template，重资源真站健壮化）。㊽ **P4 真站单进程仍卡间歇性 headless 视口退化**——这次重测把根因彻底钉死:(a) 视口退化 **per-page 间歇**（同 fleet 重建 page，有时第 6 次命中 iw>0，但**本时段 14 次 navigate 全 iw=0**，命中率随时段剧烈波动）;(b) 视口退化时**所有坐标点击失效**（硬编码坐标 **和** VL `cssPoint` 都落空——VL 正确定位"登录"给 cssPoint，但 Input.click 不响应）+ AXTree bbox 缺失（Role A `promote` 拿不到耐久 id）;(c) **无设视口 API**（`Emulation.setDeviceMetricsOverride`/`Page.setViewport` 等一律 `-32601 Method not found`，describeAction 也无 schema）;(d) **瓶颈确证 = ABCP 坐标点击在视口退化下失效，非 VL 定位**（VL 截图定位免疫退化，但点击坐标系混乱）。P4 机制本身 v1.20 已每链路 live 证（Hitl.resumed 跨连接 / coordinator 轮询→signal_resumed→续跑 / 真 VL 解真滑块 cap1-8 视口健康那次），真站单进程串联曾是唯一缺口。**⚠️ 本条"卡环境"结论被 v1.25 推翻**——所谓"间歇视口退化"实为面板 quirk #7（fresh tab 在首次 `DOM.getAXTree`/`Page.screenshot` 前无 layout，`innerWidth`=0），导航前补一次 `DOM.getAXTree` 即 100% 修复（iw 0→1280），P4 真站单进程随即 PASS。
>
> v1.23 修订（**收尾那条 POSSIBLE REGRESSION——查清=stale 非 regression，仍加防御守卫**）：㊻ 深挖 autoHitl 构造点定性:**重构后 `result['autoHitl']` 只在真触发 HITL 时写**(`confirmed_challenge`/`high_confidence_hit`→`_request_hitl_for_challenge` 真跑 `Hitl.requestPause`);skipped/cooldown/stale verdict 走 `suspected_challenge.adjudication`,**根本不设 autoHitl 键**(`skipped_stale_challenge_verdict` 字符串生产代码已不存在)。故那条测的是 pre-refactor 形状=**stale 非 regression**。但 `_loop_interrupt_from_result` 把任意 dict autoHitl 当中断仍不够严谨→加精确守卫 `_auto_hitl_is_actionable(auto)`(`tool_was_executed is False` 或 `status` 以 "skipped" 开头→non-actionable,不中断 composite loop,fall through 到 pausedState 检查),让函数名副其实并防未来短路写出 skipped-shaped autoHitl。取消该测 skip 改成守卫回归保护 + 加 `test_loop_interrupt_fires_on_executed_auto_hitl`(真 actionable autoHitl 仍中断,证守卫没误伤真中断)。**两 runner 全绿:pytest 568 passed/6 skipped/0 failed,unittest Ran 474 OK(skipped=6)**。**所有 audit 留项 + 那条 flag 全部清零。**

## 0. 一句话定义

**Skill = 一个可复用的任务胶囊**，封装：①任务身份与触发条件 ②一份冻结的 `Workflow.execute` 步骤序列 ③成功判据 ④workflow 走不下去时的 agent 接管/兜底策略。

- 快路径：`Workflow.execute` 跑确定性步骤，happy-path 不动 LLM，省 token。
- 慢路径：workflow 在某步失败/暂停 → 把"失败步 + 累积状态 + 前序产出"交给 BrowserAgent，agent 用全套工具灵活探索完成剩余部分。
- 闭环：任务→BrowserAgent 探索一次→蒸馏成 workflow→用 `Workflow.execute` 验证可复现→冻结为 skill。下次同类任务直接走快路径，失败才唤醒 agent。

---

## 1. 已核实的 ABCP 能力（方案基石）

来源：`abcp browser/packages/workflow/src/`，全部经现有测试套件（25/25 通过）证实。

### 1.1 执行模型
`Workflow.execute` 是 ABCP 原生 action，经 harness 的 `browser_call` 直接可调，**无 blocklist 拦截**。单次原子阻塞调用，不可嵌套，执行前校验（拒绝嵌套 + 非白名单 listen 事件）。

### 1.2 五种 step 类型
`action` / `if` / `loop` / `listen` / `transform`（`engine.ts:70-74` 注册全部 handler）。

### 1.3 结果信封（成功/失败）
```ts
// 引擎内部类型 types/index.ts:64-71（agent 不可见）
interface WorkflowExecutionResult {
  runId?: string;
  status: 'success' | 'error';   // ⚠️ 引擎内部，action 层被拆掉
  results: StepResult[];
  variables: Record<string, string | number | boolean>;
  error?: string;
  failedStepPath?: string;        // 如 "0.1.2" = 第0步.then分支.第1子步.第2步
}
interface StepResult {             // types/index.ts:54-62
  step: WorkflowStep;             // 失败步带【完整 step 定义】含 purpose
  stepPath?: string;
  status: 'success'|'error'|'skipped';
  result?: unknown;               // 成功步的 action 输出
  error?: string;
  duration?: number;
  retryCount?: number;
}
```
- 成功：`engine.ts:111-116`。
- 失败：`engine.ts:126-133`，`error` = `"Step <action> failed: <底层ABCP错误原文>"`（`action.ts:126`），`variables` 是失败时刻的累积快照（`engine.ts:130`），`failedStepPath` 精确定位嵌套位置（`engine.ts:215`）。

### 1.3.1 ⚠️ agent 实际可见的信封（关键：无 status 字段）

上述 `status` 是**引擎内部类型**，在 action 层被拆掉。agent 经 `browser_call(Workflow.execute, …)` 看到的是 action feedback，**没有 status 字段**：

```
// 成功（exec.ts:71-75）—— action 正常返回
{ runId, results, variables }                          // 无 status

// 失败 —— action throw → browser_call 抛异常（ABCPTransportError: -32005），不是返回信封。
// JSON-RPC error.data 实测（2026-06-26 联机）只有 ↓ —— rich payload 不过 error 边界：
{
  observation: "Workflow execution failed: Step <action> failed: <底层错误>",
  suggested_prompt: "...", method: "Workflow.execute", taskId
  // ⚠️ 实测【无】 results / variables / failedStepPath —— def.ts 的 ...workflowResult 展开不过 JSON-RPC error 边界
}
```

**怎么判定成败**（绝不读 `.status`，它不存在）：
- **成功**：browser_call 返回**无 error 标志** + observation 前缀 `"Workflow execution completed: runId=..."`
- **失败**：browser_call **抛异常**（`ABCPTransportError: -32005`），`error.data.observation` 前缀 `"Workflow execution failed: ..."`

**失败 takeover（2026-06-26 联机实测修正——P2 关键发现）**：失败时 rich payload **不过 JSON-RPC error 边界**（`error.data` 实测只有 observation/suggested_prompt/method/taskId）。所以 failure-takeover **不能从 error 读** `results`/`variables`/`failedStepPath`，改为两步：
1. catch 异常 → 用 observation 前缀 `"Workflow execution failed:"` 判定失败（并可从中解析失败 action 名）。
2. 调 **`Workflow.getStatus(runId)`** 取结构化快照。实测返回 `{ status:"error", failedStepPath, error, variables（失败时刻累积快照——已抽取的字段不丢）, results[]（每步含 step 完整定义+purpose+status）}`——`results[-1].step.purpose` 作语义锚、`variables` 作部分快照、`failedStepPath` 作定位，**接管所需全在这里**。
**前提**：`Workflow.execute` 必须传稳定 `runId`（skill 已传）。
✅ **成功路径不变**（实测）：browser_call **返回** `{ observation:"...completed...", data:{runId,results,variables} }`，无 status，`data` 三件套齐全，变量/插值/autoExtract 机制联机可用。

### 1.4 错误策略
- `onError:'stop'`（默认）：失败步 terminate + throw → 返回 error 信封。**这是触发 agent 接管的信号。**
- `onError:'continue'`：失败步记 error 但继续，整体仍可能 `success`。用于可选/易抖动步。
- `onError:'retry'`：重试 `maxRetries` 次，指数退避。终态失败时 `stepResult.retryCount` 透传（`action.ts:125-136`，旧 bug 已修）。

### 1.5 暂停/恢复（关键新能力）

**注意：存在两套独立的 pause 系统，不可混淆**：

**A. Workflow 级暂停**（`Workflow.pause`/`Workflow.resume` action + engine pauseController）：
- `engine.ts:242-270`：每步执行前 `pauseController.checkPause(snapshot)`，paused 则发 `paused` 进度事件并 `waitForResume`，恢复后发 `resumed` 事件。
- `Workflow.pause(runId)` action（`pause/exec.ts`）→ `workflowRunManager.requestPause` → 设暂停标志。
- `Workflow.resume(runId)` action（`resume/exec.ts`）→ `workflowRunManager.resume` → 清暂停标志 → engine 继续。
- `Workflow.execute` 支持 `runId` 参数（`execute/def.ts:11`），用于 pause/resume/status 关联。
- **用途**：workflow 运行中，harness 检测到挑战 → `Workflow.pause(runId)` 冻住 workflow → 跑 VL/HITL → 解决后 `Workflow.resume(runId)`。

**B. Hitl 级暂停**（`Hitl.requestPause`/`Hitl.resolvePause`）：
- 页面级人工介入，与 workflow 引擎无关。
- **用途**：VL 解不了 CAPTCHA → `Hitl.requestPause` 交人类 → 人类在 playground 操作 → harness 收恢复通知 → `Hitl.resolvePause`。

**正确组合**（CAPTCHA 出现在 workflow 运行中时）：
```
harness 检测到挑战
  → Workflow.pause(runId)        // 冻住 workflow 在步边界
  → 跑 §8.1 VL/HITL 流程
    ├─ VL 成功 → agent 验证 → Workflow.resume(runId)
    └─ VL 失败 → Hitl.requestPause（页面级）→ 人类解决 → Hitl.resolvePause → Workflow.resume(runId)
```
**不可混用**：`Workflow.pause` 冻的是引擎步进，`Hitl.requestPause` 冻的是页面工具。两者正交。

### 1.6 进度流
`engine.ts:237-240` 发射 `started/step_started/step_finished/paused/resumed/completed/failed` 事件，含 `stepPath/stepId/stepType/action/duration/error/variables`。harness/agent 可实时观测推进，不必等阻塞返回。

### 1.7 感知缓存生命周期
`$cache.axTree`/`$cache.semanticTree` 在 `Page.navigate/loaded/crashed/recovered` 时自动清空（`engine.ts:83-93`）。导航后必须重新 `DOM.getAXTree` 再用旧 id。

### 1.8 变量约束 + 持久化通道
`variables` 只能是 `string|number|boolean`（`types/index.ts:68`，engine Map 同类型）。**结构化数据进不了 variables**。

⚠️ **`record_extraction` 不是 workflow step**（P1 源码核实：`abcp browser` 全包零 `record_extraction`）。它是 **harness 侧 Python 工具**（`harness/tools/...`）；workflow 引擎的 `internalRpc` 把每个 `step.action` 当 JSON-RPC method 发给 **ABCP rpcRouter**（`core/context.ts:95`），而 ABCP action 全是 `Domain.action` 形态、根本没有 `record_extraction`——workflow step 写 `{"action":"record_extraction"}` 会被当成 ABCP 方法 → method not found → 失败。

**正确通道**：workflow 用 `extract`/`transform` 把每个字段读进 **scalar variables** → workflow 返回 → **harness/agent 读 `result.variables` 拼行 → 调 `record_extraction` 落盘**。多行/结构化则 workflow 内用 `Memory.save`（ABCP 原生 action）或 title/base64 侧信道导出，harness 读回再逐行落盘。一句话：**workflow 拿值，harness 落盘**。

---

## 2. 核心架构

```
┌──────────────────────────── Skill（复用容器，纯文件） ────────────────────────────┐
│  SKILL.md           workflow.json              fallback.yaml（或内联 SKILL.md）    │
│  ├─ name            ├─ description             ├─ success_contract（成功判据）      │
│  ├─ description     ├─ variables（模板，运行期注入） ├─ takeover（失败/暂停→agent）  │
│  │  = 触发条件       ├─ steps[]                 ├─ self_heal（可选回写策略）        │
│  └─ 入口指令         ├─ errorConfig             └─ hitl_boundary（HITL 边界）        │
└──────────────────────────────────────────────────────────────────────────────────┘
        │ 任务命中
        ▼
   browser_call(Workflow.execute, {pageId, fleetId, variables, steps, errorConfig})
        │
        ├─ 无 error + success_contract 成立 ─────────────────────▶ 完成（省 token）
        │
        ├─ progress.phase:'paused' ──▶ Workflow.pause(runId) → agent 在暂停点介入 → Workflow.resume(runId)
        │
        └─ 带 error（observation: "Workflow execution failed: ..."）──▶ 读 failedStepPath/step/error/variables
                                 │
                                 ▼
                        BrowserAgent 接管（慢路径，全套 browser 工具）
                        ① Page.getState + DOM.getAXTree 重新感知（缓存已清）
                        ② 以 failedStep.step.purpose 为语义锚继续
                        ③ HITL 走既有 harness 机制，不在 workflow 内等
                        ④ 成功后 (可选) 蒸馏补丁 → 回写 workflow.json v+1
```

### 2.1 职责边界
- **Workflow 拥有**：可预先写定的 action 序列、`if`/`loop`/`listen`/`transform` 逻辑层、步级超时与重试、暂停/恢复。
- **Agent 拥有**：CAPTCHA/登录/HITL、视觉判断、未知内容探索、异常恢复、selector 自愈。
- **交接点**：workflow 关键步留 `onError:'stop'`；遇到 HITL 类事件用 `listen` 侦测后触发暂停交出。

---

## 3. Skill 落盘形态

目录：`skills/<task-slug>/`
```
skills/
└── taaft-detail-extract/
    ├── SKILL.md            # 任务身份 + 运行指令 + 兜底契约（人/agent 可读）
    ├── workflow.json       # Workflow.execute 的 steps + variables 模板 + errorConfig
    └── fallback.yaml       # 结构化成功判据 + 接管策略（可选，也可内联 SKILL.md）
```

### 3.1 SKILL.md
```markdown
---
name: taaft-detail-extract
description: |
  Extract reviews, pros, cons, Q&A from theresanaiforthat.com product detail pages.
  Triggers on: domain=theresanaiforthat.com, task_type=web_scrape,
  stage_hint=detail_sections, artifact fields ⊇ {reviews,pros,cons,qa}.
version: 1
---

## 运行指令
1. 取运行期 pageId / fleetId（来自最近 Page.getState / Page.list）。
2. 取运行期 variables：每个产品的 rank/productName/detailUrl。
3. 调用 `browser_call(Workflow.execute, { runId, pageId, fleetId, variables, steps: <读 workflow.json>, errorConfig:{onError:"stop"} })`。
4. **持久化在 workflow 之外**：workflow 返回后读 `result.variables`，拼行，由 harness 调 `record_extraction` 落盘（record_extraction 非 workflow step，见 §1.8）。
5. 按下方契约判定结果。

## 成功判据（success_contract）
- browser_call 返回无 error（observation 前缀 "Workflow execution completed:"）
- workflow 已把 reviews/pros/cons/qa 等字段写入 variables；harness 返回后调 record_extraction 落盘，行数 >= 1
- 每行包含 reviews/pros/cons/qa 字段（允许空字符串 + 缺席证据）

## 兜底契约（takeover）
- 触发：browser_call 带 error（observation: "Workflow execution failed: ..."）或 success_contract 不成立
- 接管输入：读 result.results[-1]（失败步完整定义 + error）、result.variables（累积状态）、result.failedStepPath
- agent 动作：
  1. Page.getState + DOM.getAXTree 重新感知（导航类失败后 $cache 已被引擎清空）
  2. 以 failedStep.step.purpose 为语义意图锚，用全套 browser 工具继续
  3. HITL：若 error 含 challenge/captcha，走 Hitl.requestPause（harness 既有机制），不自己 resolvePause
- self_heal：成功后若定位路径变化，产出 workflow.json v+1，经 1 次 canary 验证后 promote
```

### 3.2 workflow.json（示例：TAAFT 详情页抽取）
```json
{
  "description": "Extract reviews/pros/cons/qa from TAAFT detail page",
  "variables": { "rank": "", "productName": "", "detailUrl": "" },
  "errorConfig": { "onError": "stop", "maxRetries": 1 },
  "steps": [
    { "action": "Page.navigate", "params": { "url": "$vars.detailUrl" }, "purpose": "Open product detail page" },
    { "type": "listen", "event": "Page.loaded", "timeout": 15000, "onTimeout": "continue" },
    { "action": "DOM.getAXTree", "purpose": "Read page structure to locate review/section ids" },
    { "type": "transform", "input": "$cache.axTree.lines",
      "ops": [ {"op":"find","pattern":"Reviews","mode":"contains"}, {"op":"regex","pattern":"\\[([0-9a-fA-F-]+:\\d+:\\d+)\\]","group":1} ],
      "output": "reviewsHeadingId" },
    { "type": "if", "condition": { "path": "$vars.reviewsHeadingId", "operator": "matches", "value": "[0-9a-fA-F-]+:\\d+:\\d+" },
      "then": [
        { "action": "DOM.getText", "params": { "id": "$vars.reviewsHeadingId" }, "purpose": "Read reviews section text", "extract": { "reviews": "data.text" } }
      ],
      "else": [
        { "action": "Input.click", "params": { "id": "$vars.reviewsHeadingId" }, "onError": "continue", "purpose": "Try expanding reviews tab" }
      ]
    }
  ]
}
```
**关键**：元素定位用"运行期重解析"（getAXTree→transform→if exists→操作），不冻结 epoch 绑定的 AXTree id / pageId。variables 模板化，运行期注入。**末步不调 record_extraction**——workflow 只把字段读进 variables，落盘由 harness 在返回后做（§1.8）。

### 3.3 fallback.yaml（可选结构化成功判据）
```yaml
success_contract:
  workflow_no_error: true            # browser_call 返回无 error 标志
  observation_prefix: "Workflow execution completed:"   # 成功 observation 前缀
  variables_required: [reviews, pros, cons, qa]   # workflow 必须写入的 scalar 变量
  # 落盘由 harness 在 workflow 返回后做（record_extraction 非 workflow step）：
  persisted_rows_at_least: 1
  fields_required: [rank, productName, detailUrl]
  fields_nonempty: [rank, productName, detailUrl]
takeover:
  on_call_error:                       # browser_call 带 error（非读引擎内部 .status）
    read: [results[-1].step, results[-1].error, variables, failedStepPath]
    reobserve: [Page.getState, DOM.getAXTree]
    semantic_anchor: results[-1].step.purpose
  on_contract_unmet:
    from_step: len(results)
    reason: postcondition_unmet
hitl_boundary:
  detect: [Hitl.resumed]      # workflow 侧唯一可 listen 的 HITL 事件（白名单见 §3.3）
  action: listen_then_pause   # 不在 workflow 内 resolvePause
maintenance:                   # skill 健康/禁用策略
  max_revision_per_failure_class: 3      # 同类失败最多修补次数
  disable_after_consecutive_failures: 3   # 连续失败 N 次自动禁用 skill
  canary_ttl_hours: 24                    # canary 证据有效期
  auto_disable_on_challenge: false        # 遇挑战不自动禁用（环境因素非 skill 缺陷）
```

### 3.4 失败分类法（接管后如何治）

接管模式（§6 的 pause-resume / failure-takeover / contract-unmet）回答"何时介入"，失败分类回答"介入后怎么治"。两者正交：

| 失败类型 | 含义 | 治疗方向 |
|---------|------|---------|
| `schema_invalid` | workflow JSON/action params 无效 | 编码修复，无需 browser |
| `locator_not_found` | 目标不再可解析 | BrowserAgent 重观察；可能 VL target_locate |
| `action_failed` | browser action 被拒 | 查 ActionFeedback，重观察 |
| `postcondition_failed` | 步骤跑了但任务未达成 | 分类数据/状态不匹配 |
| `overlay_blocked` | modal/overlay 阻挡 | 确定性 dismiss 阶梯，失败则 VL |
| `challenge_or_hitl` | CAPTCHA/登录/安全/人工门 | HITL/授权路径，非 workflow 修复 |
| `download_or_dialog` | 文件/对话框状态异常 | BrowserAgent 接管 |
| `site_changed` | 页面结构实质变化 | 修补或禁用 skill |

**关键纪律**：`challenge_or_hitl` 类失败**不修补 workflow**（环境因素非 skill 缺陷）；`site_changed` 才修补；VL 坐标命中成功**本身不是耐久修复**，必须经 bbox→id 提升回语义句柄后才算修复。

---

## 4. 两条生命周期

### 4.1 Authoring（agent 探索 → 人类决定 → 蒸馏 → 验证 → 冻结）

**关键变更**：skill 生成**不由 agent 自动决定**，而是任务完成后由**人类决定**是否将当前任务蒸馏为 skill。

1. 任务到达，无 skill。
2. **BrowserAgent 探索并完成任务**，产出 trace + record_extraction 证据 + 完整执行记录。
3. **任务完成 → 人类审核**：人类查看任务结果和执行 trace，判断"这个任务是否值得复用"。
   - 人类决定 **YES** → 进入蒸馏。
   - 人类决定 **NO** → 不生成 skill，结束。
4. **蒸馏**（人类批准后）：从成功 trace 抽最小确定性步骤序列 → 生成 `Workflow.execute` 的 steps。
   - 具体 id/文本**模板化**为 `$vars`，绝不冻结 AXTree id / pageId。
   - 元素定位优先"运行期重解析"：`DOM.getAXTree → transform(find+regex 取 id) → if matches(id 形) → 操作`（守卫用 `matches` 非 `exists`，见 §7）。
5. **验证**：在新页面用 `Workflow.execute` 跑一遍 → 无 error 且 success_contract 成立 = 可复现。这一步是"可验证"的本体。
6. **冻结**：写 `SKILL.md` + `workflow.json` + `fallback.yaml`，落盘为 skill。

**人类门控的理由**：不是所有成功任务都值得固化——有些是一次性的、有些页面太不稳定、有些任务结构太特殊。agent 无法判断"这个任务未来会不会再出现"，人类可以。agent 的职责是**做好任务并保留完整证据**，是否复用是人类决策。

**蒸馏辅助可以是自动的**：人类决定"生成 skill"后，蒸馏过程（trace → workflow.json）可以用 coding agent 自动化，但**发起权在人类**。已实现 **`skills/_tools/distill_trace.py`**（规则见 `skills/DISTILLATION.md`）：读 `traces/<worker>.jsonl` → 丢失败/恢复噪声 → 切单对象 → 值→`$vars` → id 经 purpose 推 label 去硬编码（matches 守卫）→ 产出过 schema 校验的 workflow.json draft + SKILL/fallback 骨架 + distill_report（决策与 TODO）。draft 必经人工过一遍（确认 label、补 live-pin 选择器）再冻结。

### 4.2 Reuse（skill → workflow → agent 兜底）：省 token 复用
1. 命中 skill（见 §5 命中机制）。
2. agent 注入运行期 pageId/fleetId/variables，调 `browser_call(Workflow.execute, …)`。
3. 判定：
   - **无 error** + success_contract 成立 → 完成，**全程零页面级 LLM**。
   - `progress.phase:'paused'` → `Workflow.pause(runId)` → agent 在暂停点介入 → `Workflow.resume(runId)` 继续。
   - **带 error**（observation: "Workflow execution failed: ..."）→ 读 `results[-1]`（失败步 + error + variables 快照 + failedStepPath）→ agent 接管。
4. agent 接管：重新感知 → 从失败步语义继续 → 完成任务 →（可选）自愈回写。

---

## 5. Skill 命中与派发

**目标**：命中不烧 token。三种入口，优先级从高到低。

### 5.1 终端手动指定（最高优先级）
人类在终端显式指定 skill，**优先级高于一切自动匹配**：
- `/skill <skill-id>` —— 直接指定
- `/skill list` —— 列出可用 skill 供选择
- `/skill auto` —— 显式降级到自动匹配

手动指定跳过所有预过滤和 LLM 决策，零歧义、零 token。

### 5.2 确定性自动预过滤
task_plan 已有 `domain`/`task_type`/`stage_hint` 三元组，先用它做确定性预过滤：

```
任务到达 LeadAgent
   │
   ├─ 人类已 /skill <id> 指定 ──▶ 直接用该 skill（最高优先级）
   │
   ├─ 有 task_plan（含 domain/task_type/stage_hint + expected_artifact.fields）
   │     │
   │     ▼
   │  确定性预过滤：用 (domain, task_type, stage_hint, fields 子集) 匹配 skills/*/SKILL.md frontmatter
   │     │
   │     ├─ 唯一命中 ──▶ 直接走 workflow 快路径（零 LLM 决策）
   │     ├─ 多义/无命中 ──▶ 一次 LLM 决策：读候选 skill 描述 + 当前任务，选一个或 fallback 到纯 BrowserAgent
   │     └─ 纯 BrowserAgent（无 skill 可用）
   │
   └─ 无 task_plan（开放式任务） ──▶ 纯 BrowserAgent 探索；成功后可触发 authoring 蒸馏
```

命中维度（frontmatter 声明）：
- `domain`（精确或通配，如 `theresanaiforthat.com` / `*.example.com`）
- `task_type`（web_scrape / form_filling / file_download / file_upload / web_search / general；旧名 form_fill / download_file / browser_action / browser_data_collection 仅作兼容 alias）
- `stage_hint`（collection / detail_sections / form_interaction / …）
- `fields`（expected_artifact 字段子集匹配，判断"这个 skill 抽的东西跟任务要的东西一致"）

---

## 6. 三种接管模式（按介入时机）

| 模式 | 触发 | 介入时机 | token 成本 | 适用 |
|------|------|---------|-----------|------|
| **pause-resume** | `progress.phase:'paused'`（workflow 级暂停） | workflow 运行中，某步边界暂停 | 最低（agent 只处理卡点，workflow 跑剩余） | HITL/风控、需视觉判断的单点 |
| **failure-takeover** | browser_call **带 error**（observation: "Workflow execution failed: ..."） | workflow 终止后 | 中（agent 从失败步重做到尾） | selector 失效、结构变化 |
| **contract-unmet** | browser_call **无 error** 但 success_contract 不成立 | workflow 跑完但结果不对 | 中高（全做完但结果假成功） | 静默错误、placeholder 数据 |

`pause-resume` 是 workflow 级 `Workflow.pause`/`resume` 带来的**最优解**：agent 不必等 workflow 整体失败，在卡住的那一步暂停、介入、恢复。比 failure-takeover 省 token（不重跑前序成功步）。

---

## 7. 必须遵守的 authoring 纪律

| 风险 | 纪律 |
|------|------|
| 冻结 epoch 绑定的 AXTree id → 必失效 | workflow 内**运行期重解析 id**（getAXTree→transform→if），禁止硬编码 id/pageId |
| **`if … exists` 守不住 transform 输出（联机实测踩坑）** | transform `find` 无命中时写**空串 `""`** 进变量（transformRunner），而 `exists` 对 `""` 判 true（conditionEvaluator）→ 空 id 漏进 `Input.click` → "Invalid params"。**id 守卫一律用 `operator:"matches", value:"[0-9a-fA-F-]+:\\d+:\\d+"`**，不用 `exists` |
| CSS selector 改版即腐 | 优先 role+name 文本定位；CSS 仅用于真正稳定的 hook |
| `status:'success'` ≠ 任务达成（引擎内部 status，agent 不可见） | skill **必须**定义独立 success_contract，以 browser_call 无 error + 契约成立收尾，不读引擎内部 status |
| workflow 里硬等 HITL/CAPTCHA | 改为 `listen` 到 Hitl/challenge 事件 → 触发暂停 → 交 agent + 既有 harness HITL 机制 |
| 复用时参数没模板化 | authoring 时把具体值替成 `$vars`，运行期经顶层 `variables` 注入 |
| 跨页缓存失效 | 导航/crash 后 workflow 内重跑 `DOM.getAXTree` 再用 id（引擎自动清缓存） |
| 结构化进不了 variables；record_extraction 非 ABCP action | workflow 用 `extract`/`transform` 把字段读进 scalar variables；**record_extraction 是 harness 返回后的后置落盘步**，不写进 workflow（§1.8） |
| `step.purpose` 缺失 → agent 接管无语义锚 | **每个 action 步强制写 purpose**（自然语言意图，如"点击 Reviews tab"） |
| self-heal 回写覆盖可用版 | 写 `workflow.v2.json`，经 1 次 canary 验证才 promote 替换 v1 |

### 7.1 权威层 vs 证据层（L0–L4 强度阶梯）

判定"任务是否达成"必须遵循权威强度阶梯，**不让 VL 自证**（VL 解了 CAPTCHA 后仍需更强证据层确认）：

```
L0 action feedback / Page.getState     ← 最强：浏览器原生状态
L1 AXTree role/name/current text        ← 强：语义结构
L2 read-only JS oracle / DOM.getText    ← 强：DOM 真值
L3 SemanticTree digest                  ← 中：结构推断（定位修复用）
L4 visual_verify (VL)                   ← 弱：视觉佐证，不单独定论
```

**应用**：
- success_contract 的真值层 = L0/L1/L2，不以 status 单信号收尾（§7 已述）。
- VL 解 CAPTCHA 后的"挑战消失"验证：VL contract_verify（L4）是**佐证**，应叠加 Page.getState url/title（L0）才定论。VL 不自证自己解成功了。
- HITL 人类恢复：人类点击（权威动作）= 定论，不需 VL 复验（§8 已述）。

---

## 8. HITL 边界与重构（VL 优先 → 人类兜底）

### 8.1 重构后的 HITL 流程（VL-first）

**重构目标**：为 VL 自动解 CAPTCHA 测试铺路。VL 解决优先于 HITL；VL 失败才上人；人类在 ABCP playground 中点击恢复（= playground 调 `Hitl.resolvePause`，唯一恢复源），harness 只等 `Hitl.resumed`，**不在恢复点复验、不自己 resolvePause**。

```
挑战检测（ChallengeTracker / workflow listen / notification）
    │
    ▼
VL captcha_solve（仅视觉自洽型；行为型短路到步骤 4）
    │
    ├─ 成功（solve_plan 执行后挑战消失）
    │     │
    │     ▼
    │  agent 验证页面状态：
    │  ① Page.getState（url/title/status）
    │  ② 截图 → VL contract_verify（challenge_gone）
    │  ③ 确认恢复 → 继续 workflow（无需 HITL）
    │
    ├─ 失败 / 行为型 / 未知型
    │     │
    │     ▼
    │  Hitl.requestPause（交人类）
    │     │
    │     ▼
    │  人类在 ABCP playground 中解决挑战
    │     │
    │     ▼
    │  人类点击"恢复" = playground 调 Hitl.resolvePause（唯一恢复源）→ 发 Hitl.resumed
    │     │
    │     ▼
    │  harness 仅【等待 Hitl.resumed】，不复验、不自己 resolvePause：
    │     ├─ 收到 → pause 已被 ABCP 权威解除（页面可操作）→ resume workflow
    │     └─ 未收到 → 页面仍 paused，任何 action 都 ERR_PAGE_PAUSED → 复验也无意义
    │  （"人提前点了但挑战没真解"不在恢复时拦，由末端 success_contract / contract_verify 兜）
    │  ⚠️ 依赖：playground 的"恢复"按钮必须真的调 Hitl.resolvePause 并发出 Hitl.resumed（事件真名，
    │     源码核实 DispatcherBridge.ts:322 / requestPause.def.ts:23）。历史上面板可能不主动发该事件——
    │     此为联机/面板核实项；若不发即 playground 缺陷（需补能力），harness 不为此兜底
    │     （没 Hitl.resumed = 没解除 = 发什么 action 都徒劳）。
    │
    └─ 误判（not_a_challenge）→ resume workflow
```

### 8.2 与现有 HITL 机制的关键差异

| 环节 | 现有（harness/hitl.py） | 重构后 |
|------|------------------------|--------|
| 检测（requestPause） | url/title/VL | **不变**——url/title/VL 判定挑战后 requestPause |
| VL 角色 | 只判存（challenge_detection），不解决 | **先尝试解决**（captcha_solve），解决才由 agent 验证自己的解 |
| 恢复触发（VL 成功） | 自动检测（通知事件 / verified settlement） | agent 验证页面状态（Page.getState + 截图 + VL contract_verify）后继续 |
| 恢复触发（VL 失败→人类） | 自动检测（通知事件 / verified settlement） | **人类在 playground 点恢复 = 唯一恢复源**；harness 仅等 `Hitl.resumed` |
| resolvePause | harness 验证后调 | **人类/playground 调**（人类路径）；harness/agent 不主动 resolvePause |
| 恢复点复验 | url/title/VL verified settlement | **取消**——`Hitl.resumed` 即权威；"假恢复"由末端 contract_verify 兜 |

**核心变更**：VL 从"只判存"升级为"先尝试解"。解决成功则绕过 HITL（agent 验证自己的解后继续）；解决失败则 `Hitl.requestPause` 交人类，恢复**只由人类在 playground 点击**触发（= playground 调 resolvePause → 发 `Hitl.resumed`）。**harness 在人类恢复路径不复验、不主动 resolvePause**——`Hitl.resumed` 即权威信号；未收到则页面仍 paused、任何 action 徒劳；"人提前点但没真解"由末端 success_contract / contract_verify 兜。

### 8.3 workflow 与 HITL 的边界

workflow 不自己处理 HITL，只**侦测并交出**：
1. workflow 内 `listen` 的 HITL 事件是 `Hitl.resumed`（`Hitl.humanInput` / `Hitl.resumeEvent` 都不在 listen 白名单——白名单见 §3.3 / skills/README.md §3.3）。
2. 侦测到 → 触发 `pauseController` 暂停（发 `paused` 进度事件）。
3. harness 侧收到暂停信号 → 走 §8.1 流程（VL 优先 → 人类兜底）。
4. harness 完成恢复 → 调 `pauseController.waitForResume` 的 resolve → workflow 发 `resumed` 事件 → 继续跑剩余步。

**铁律**：workflow step 内绝不调 `Hitl.resolvePause`。VL 自解路径由 agent 验证后 `Workflow.resume(runId)`；人类路径的 `Hitl.resolvePause` 归人类/playground，harness 只负责 `Workflow.pause/resume`（workflow 级）与 `Hitl.requestPause`（请求，不 resolve）。

### 8.4 pauseController 暂停竞态分析

**疑虑**：pauseController 在步边界检查，问题出现在步中间时，workflow 可能已跑到下面 N 个节点。

**核实结论**（基于 `engine.ts:165` waitIfPaused 在 step 执行**前**调用）：

```
Step N:   waitIfPaused() → 未暂停 → handler 执行
          [挑战在此出现]
Step N+1: waitIfPaused() → checkPause()
          ├─ harness 已通过并发通知监听检测到 → 暂停 ✓（gap=0）
          └─ 尚未检测到 → 未暂停 → handler 执行
               ├─ 动作类 → ABCP 返回 ERR_PAGE_PAUSED → onError:stop → error 返回
               └─ 读取类 → 在挑战页成功 → 继续
Step N+2: waitIfPaused() → harness 必已检测到（通知延迟 ms 级）→ 暂停 ✓
```

**最坏情况**：1 个读取步在挑战页跑完。读取无害（幂等，缓存下次 navigate 自动清）。动作类立即失败（ABCP 在 paused 页拒绝工具）。

**"N 步之后"需同时满足**：无 pauseController + 多个读取在挑战页成功 + 连续 N 步无动作。即便如此 onError:stop 在首个失败动作终止，contract_verify 兜住静默错误。

**三层防御**：
1. pauseController（harness 并发监听通知 → 设暂停标志）— gap ≤ 1 步
2. onError:stop（paused 页 action 失败）— 首个动作即终止
3. contract_verify（返回后 VL 验证）— 兜住"跑完但结果错"

**关键要求**：harness 必须跑**并发通知监听器**，收到 Page/Hitl 事件就 flip pauseController 标志。agent 被 await 阻塞，但 harness 事件循环没阻塞。

> ⚠️ **v1.12 修正（§8.4 落地约束）**：上面"flip pauseController"假设 harness 能并发发 `Workflow.pause(runId)`，但 `abcp_client.py:324` 把每次 `call()` 串行在单 `_call_lock`+单 `_pending_call`——`Workflow.execute` 阻塞期间**同连接发不出控制调用**（pause/resume/resolvePause 全被锁到 execute 返回）。**通知**经 NotificationHub 独立于锁照常到达，故 harness 能"观测"暂停但不能"驱动"恢复。结论：同连接做不到主动驱动，P4 默认落为 **observe-only 交接慢路径**（v1.12 ㉚ / `harness/skill_pause.py`）。**v1.13 增量**：真正的"workflow 级 pause→解决→resume"（§6 最优解）已实现为**第二控制连接**（`harness/skill_control.py:ControlChannel`+`run_workflow_with_control`，config `skill_workflow_active_control_enabled` 默认 OFF，失败降级交接）——主连接被 execute 阻塞，第二连接照常发 `Workflow.pause/resume(runId)`+`Hitl.*`。⚠️ 跨连接 runId/page 可达性 live 未验证（面板阻塞），验证通过前保持 OFF。

---

## 9. 与 harness 现有代码的衔接（改动极小）

### 9.1 零改动即可跑通
`Workflow.execute` 已可经 `browser_call`（`browser_tools/__init__.py:242`）直接调用，无 blocklist。

### 9.2 建议的薄 helper（降样板，可选）——**两调模式（联机实测后修正）**

成功路径读 `browser_call` 返回的 `data`；失败路径 **catch 异常 + 二次调 `Workflow.getStatus(runId)`**（rich payload 不在 error 里，见 §1.3.1）。在 `harness/tools/browser_tools/` 新增 `workflow_skill.py`：
```python
async def run_skill_workflow(browser_call, run_id, params) -> dict:
    """跑一个 skill workflow，统一成功/失败两路返回标准化结构。"""
    try:
        res = await browser_call("Workflow.execute", {**params, "runId": run_id})
        data = (res or {}).get("data") or {}              # 成功：{runId, results, variables}
        return {"succeeded": True, "runId": run_id,
                "variables": data.get("variables") or {},
                "results": data.get("results") or []}
    except Exception as exc:                               # 失败：execute 抛异常（ABCPTransportError）
        # rich payload 不在异常里 → 二次取快照
        status = await browser_call("Workflow.getStatus", {"runId": run_id})
        snap = (status or {}).get("data") or {}           # {status,failedStepPath,error,variables,results}
        results = snap.get("results") or []
        last = results[-1] if results else {}
        return {
            "succeeded": False, "runId": run_id,
            "failedStepPath": snap.get("failedStepPath"),
            "failedError": snap.get("error") or last.get("error"),
            "failedPurpose": ((last.get("step") or {}).get("purpose")),
            "variables": snap.get("variables") or {},     # 失败时刻累积快照——已抽取字段不丢
            "priorResults": results[:-1] if results else [],
            "raw_exc": str(exc),
        }
```
关键：**成功读 `data`，失败必须 `Workflow.getStatus(runId)`**；不读引擎内部 `status`（agent 不可见），成败判据 = execute 是否抛异常。

### 9.3 skill 命中（✅ 已实现 `harness/skill_registry.py`）
`SkillRegistry.load()` 读 `skills/*/SKILL.md` frontmatter + workflow.json + fallback.yaml（跳过 `_template`/`_tools`），`.match(domain,task_type,stage_hint,fields)` 做确定性预过滤（唯一命中返回 skill、多义返回 None 交一次 LLM 决策、`.candidates()` 列候选）、`.get(id)` 供终端 `/skill <id>` 手动选。`domain` 支持 `*.x` 通配，`fields` 子集匹配。

**运行器** `harness/tools/browser_tools/workflow_skill.py`：`build_execute_params`（冻结 steps + 注入 runId/pageId/fleetId/variables）→ `run_skill_workflow`（§9.2 两调模式）→ `check_success_contract`（读 fallback.yaml 的 variables_required/variables_any_nonempty 判定）。

**调用点已接进 worker 编排**（`harness/skill_dispatch.py` + `spawner._run_browser_worker`，在 `harness.run()` 之前）：`maybe_run_skill_fast_path`（config `skill_fast_path_enabled` 门控，默认开）→ `select_skill`（显式 `worker_contract.skill_id` 优先，否则 contract 维度自动匹配）→ `derive_variables`（从 `skill_variables`/contract/phase/task 内 URL 推；required 填不齐就跳过）→ 跑 skill → 契约成立则 `_record_extraction` 落盘 + 返回 `completed_via_skill` answer（**跳过 LLM 循环**）；no-match/失败/契约不成立 → 返回 None 走正常 BrowserAgent 慢路径。**fail-safe**：异常一律回落慢路径，绝不破坏 worker。`tests/test_skill_integration.py` **27 测试全绿**；真站 ecrett-music **端到端 wired dispatch 联机跑通**（match→run→契约→record_extraction 落盘真数据）。

> ⚠️ 修了个隐性 bug：fallback.yaml 的 `read: [..., status.results[-1].step]` 是**非法 YAML**（flow 序列里含 `[`），`yaml.safe_load` 抛错→registry 静默吞成 `{}`→success_contract 消失（gating 失效）。已给含 `[` 的列表项加引号，并让 registry 对坏 YAML 打 stderr 警告（不再静默）。

### 9.4 workflow 级暂停接入（pause-resume 模式）
若要用 pause-resume 模式，harness 需：
1. `Workflow.execute` 时传 `runId`（关联 pause/resume）。
2. harness 跑**并发通知监听器**，收到挑战/HITL 事件 → 调 `browser_call(Workflow.pause, {runId})` 冻住 workflow。
3. 跑 §8.1 VL/HITL 流程。
4. 解决后调 `browser_call(Workflow.resume, {runId})` → workflow 继续。
这是 §6 pause-resume 模式的接入点，P4 再做。

---

## 10. 三个跨场景骨架（证明非单一场景专用）

三者都用"运行期重解析 id"，selector 腐烂面最小化。各骨架末尾的 `record_extraction` 是**示意落盘点**——实际是 harness 在 workflow 返回后读 variables 落盘，不是 workflow step（§1.8）。

### 10.1 网页搜索
```
Page.navigate(搜索URL) → listen Page.loaded → DOM.getAXTree
→ transform 取结果链接 id → loop(翻页) collect → record_extraction
兜底：0 结果 / CAPTCHA → agent
```

### 10.2 表格填写
```
loop over 字段表 { DOM.getAXTree → transform 取 field id → Input.type → if 校验(.value) }
→ 提交 → listen 落地页事件 → record_extraction(结果)
兜底：动态校验错误 → agent
```

### 10.3 附件下载
```
Page.navigate → Input.click(下载钮) → listen File.chooserOpened 或 loop Download.list(轮询完成)
→ record_extraction(文件路径)
兜底：chooser / 鉴权 → agent
```

---

## 11. 落地阶段

| 阶段 | 目标 | 验收 |
|------|------|------|
| **P0 探针** | 核实步级报错契约 | ✅ 已完成（源码 + 测试套件 25/25 通过） |
| **P1 skill 约定** | 定 `skills/<slug>/` 的 SKILL.md/workflow.json/fallback.yaml schema + 兜底契约模板 | 文档 review 通过；模板可手工填写 |
| **P2 端到端参考 skill** | 挑一个有现成证据的真实任务（如 TAAFT 详情页），跑通完整闭环：authoring→验证→冻结→复用→注入失败→agent 接管→(可选)自愈 | ✅ **真站完整闭环盖章（v1.24，2026-06-28 重测）**——真 `run_skill_workflow` 对真 TAAFT 详情页（ecrett-music）：①快路径抽到真数据 reviews 1377/pros 1613/qa 2000 字 + success_contract ok；②注入坏步 → takeover 拿 failedStepPath:5 + variables 快照 + priorResults;③agent 从失败点续跑剩余 extract → 恢复全部抽取。cleared-tab+navigate 绕 Cloudflare；navigate 步加 onError:continue 容重资源真站调用超时 |
| **P3 蒸馏辅助** | trace → workflow.json（id 模板化、优先 AXTree-transform 定位） | ✅ 已完成——`skills/_tools/distill_trace.py` + `skills/DISTILLATION.md`，在真实 TAAFT trace（ecrett-music 详情页）上跑通：丢失败/恢复噪声、切单对象、url→$vars、id 经 purpose 推 label 去硬编码（matches 守卫）、产出过编译版 schema 校验的 draft + report |
| **P4 pause-resume 接入** | `pauseController` 桥接 harness HITL 机制 | ✅ **两层均落地（已联机校准）**——实测:引擎级 `Workflow.pause/resume(runId)` 跨连接 DEAD（session-bound），**页面级跨连接 OK**。(默认) observe-only 交接 `harness/skill_pause.py`；(opt-in) **页面级主动控制** `harness/skill_control.py`（第二 `ControlChannel`：execute 阻塞期由第二连接 `Hitl.requestPause→等 Hitl.resumed` 解决**页面**，workflow 靠自身"挑战门控 `listen Hitl.resumed` 边界"续跑），TAAFT skill+`_template` 已带该边界（过编译 schema），config 默认 OFF、失败降级交接。68 单测绿。✅ **端到端 live PASS（v1.25，yue 滑块真站单进程）**——`DOM.getAXTree` 触发 layout 解视口（quirk #7）→execute 阻塞 listen→第二连接 VL 解滑块（solvable/slider）→signal_resumed→续跑 `succeeded:True`、intervention `resolved:true` |
| **P5 self-heal** | agent 兜底成功后产出补丁 workflow.json（version+1），经 canary 验证 promote | ✅ **全闭环已实现**——`harness/skill_health.py`（按 `maintenance.disable_after_consecutive_failures` 自动禁用 rotted skill，已接进 dispatch）+ `harness/skill_heal.py`（write_candidate→canary_validate→promote/reject + .heal_history + health.reset）+ **`harness/skill_autoheal.py` 自动生成候选闭环**（快路径回落+慢路径 validated_done+degraded → 蒸馏慢路径 trace 成候选 → canary 门控 promote，接进 spawner，config `skill_auto_heal_enabled` 默认开）。15 单测绿 |
| **P6 skill 命中自动化** | `skill_registry.py` 索引 + 确定性预过滤 | ✅ **核心已实现**——`harness/skill_registry.py`（discover/match/get）+ `workflow_skill.py`（run+contract），15 单测绿 + 真站端到端联机 match→run→真数据；✅ 调用点已接进 spawner._run_browser_worker（§9.3），27 单测+真站 wired 联机绿 |

---

## 12. 待讨论的开放项

1. **skill 目录位置**：`skills/<slug>/` 放 harness 仓库 vs task-local worktree？前者跨任务复用，后者随任务隔离。建议：稳定 skill 放仓库 `skills/`，task-local 探索期的 skill 放 worktree，验证通过后 promote 到仓库。
2. **success_contract 表达形式**：内联 SKILL.md（人/agent 可读，但难机器判定）vs 独立 `fallback.yaml`（结构化可机器判定，但多文件）。建议：两者都要——SKILL.md 写自然语言版给人/agent 读，fallback.yaml 写结构化版给 harness 判定。
3. **pause-resume 的触发权**：workflow 主动 `listen` HITL 事件后自暂停，还是 harness 侧检测到挑战后外部触发暂停？前者 workflow 自治但需 listen 覆盖全；后者 harness 统一控制但 workflow 不知情。建议：workflow `listen` 为主（声明式），harness 外部触发为兜底（覆盖 listen 未覆盖的情况）。
4. **skill 与现有 strategy_bank 的关系**：strategy_bank 是"软策略提示"（preferred_tools/procedure 文本），skill 是"硬可执行胶囊"（冻结的 workflow）。是否让 skill 成为 strategy_bank 的一种硬化形态？命中 skill 时跳过 strategy_bank 提示，直接走 workflow。
5. **前一版 harness workflow 装置的去留**：`harness/tools/workflow_tools.py`/`workflow_protocol.py`/`workflow_store.py`/WorkflowAgent/WorkflowAnalysisAgent 是否退役？方向上可退役（执行交回 ABCP、复用交给 skill、兜底交给 BrowserAgent），但**待 P2 参考 skill 跑通证明新路径稳定后再清理**，不擅自删码。

---

## 13. VL 模型四角色：AXTree 补盲 / 裁判 / CAPTCHA 驱动 / 全局视觉仲裁

> 依据：现有 `harness/vl.py` 已有 3 mode（`overlay_classify` 返回归一化坐标、`challenge_detection` 判存不解决、`action_outcome`）；ABCP `Input.drag` 支持 `dx,dy` 相对偏移、`Input.click` 支持 `x,y` 坐标、`Input.type` 接文本；AXTree 行尾 `# @x,y,w,h` 是视口 CSS 像素 bbox。底座已具备，本节定义 VL 角色及与 skill/workflow/agent 的衔接。
>
> 范围扩展（吸收对照文档）：VL 不只服务 workflow，也是 **BrowserAgent 全局视觉仲裁层**——普通 browser_call 失败同样可触发 VL 兜底（视觉/遮挡/挑战/布局相关失败）。

### 13.1 现状与缺口

| 现有能力 | 位置 | 缺口 |
|---------|------|------|
| `overlay_classify`：返回 `dismiss_point {x,y}` 归一化坐标，harness 反算+elementFromPoint 安全校验后 click | `vl.py:33-60`、`dismiss_overlay.py:316` | 只用于关 overlay，无法定位任意目标 |
| `challenge_detection`：判 `confirmed_challenge/normal_loading/unrelated_block/uncertain` | `vl.py:62-76` | **只判存不解决**——确认挑战后直接走 HITL 交人类 |
| `action_outcome`：`match/mismatch/blocked/uncertain` | `vl.py:77-87` | 通用验证，未与 skill success_contract 衔接 |
| 坐标体系：归一化 0-1000，harness 反算像素 | `vl.py:47-50` | 已有，可复用于所有需要坐标的 mode |
| AXTree bbox：行尾 `# @x,y,w,h`（视口 CSS 像素，x,y 为左上角） | ABCP axPerception | 未用于 VL 坐标→canonical id 的反查提升 |

**核心缺口**：VL 能"看见并指出"（overlay_classify 已证明），但缺少"指出任意目标"、"给出解决动作序列"、"坐标提升回耐久句柄"的能力。

### 13.2 角色 A：VL 作为 AXTree 补盲（精确指目标）

> ✅ **v1.18 已落地 + live 证**：`harness/vl_locate.py`（`parse_axtree_bboxes`/`point_to_id` 最小面积含点框+排容器/`promote_locate`/`locate_target`）+ vl.py `visual_locate` mode + config `visual_locate_enabled`。地基两探针实测:AXTree lines 带 `# @x,y,w,h` 且 bbox 空间==截图像素空间（双 2560×1600）。live:example.com 真 VL 定位 "Learn more"→promote 得 canonical id `3:13:13`（与 ground-truth 一致）。坐标绝不进 skill（promote-then-heal）。**已 wiring 进慢路径** `visual_verify` 工具（`mode=visual_locate`→`_promote_visual_locate`→`resolvedId`/`cssPoint`，gated `visual_locate_enabled`）。98 单测绿。

**场景**：AXTree 够不着的视觉元素——canvas 绘制、图片内文字、纯视觉布局无语义节点、shadow DOM 不可达。VL 用归一化坐标精确指出目标，harness 反算后用 `Input.click(x,y)` 或 `Input.drag(x,y→toX,toY)` 操作。

**新增 mode：`visual_locate`**

```
输入：screenshot + target_description（自然语言，如"页面右下角的蓝色提交按钮"）
输出：
  verdict: located | not_found | uncertain
  targets: [{ x, y, label, confidence }]    # 归一化 0-1000，可有多个
  fallback_hint: string                      # 给 agent 的语义提示
```

**与 workflow 衔接**：workflow 内无法直接调 VL（VL 不是 ABCP action）。两种路径：
- **workflow 暂停→harness VL 定位→resume**：workflow 步骤声明 `"vlLocate": "target_description"`（harness 扩展的非标 step 字段），执行到该步时 harness 截图→VL 定位→把坐标写回 `$vars`→继续。需 pauseController 配合。
- **agent 兜底路径**：workflow 失败后 agent 调 `visual_verify`（已有工具）的 `visual_locate` mode，拿到坐标直接 `Input.click(x,y)`。

**安全约束**（继承 overlay_classify 既有纪律）：坐标反算后必须经 `elementFromPoint` 独立校验，确认指向的元素与 VL 描述一致，避免误点敏感控件。

**bbox→id 提升（promote-then-heal）**：VL 一次性像素命中不应止步于坐标操作。AXTree 行尾 `# @x,y,w,h` 是视口 CSS 像素 bbox（x,y 为左上角，w,h 为宽高）。VL 点 `(px,py)` 命中某 bbox 当且仅当 `x≤px≤x+w 且 y≤py≤y+h`（前提：AXTree 快照与 VL 截图取自同一滚动位置）。命中后反查到 canonical id → 用耐久句柄（id / role+name）而非坐标继续操作和给 workflow 打补丁。即：VL 当场救场 → 立刻把像素转回耐久定位器喂回快路径。**坐标绝不进 workflow.json**（比 CSS selector 腐烂更快），self-heal 只用提升后的句柄。

**定位阶梯**：
```
AXTree canonical id  >  语义属性(aria/name/role)  >  稳定 CSS  >  VL 坐标（最后手段，仅 agent 慢路径）
```

### 13.3 角色 B：VL 作为裁判（success_contract 验证）

> ✅ **v1.19 已落地 + live 证**：vl.py `contract_verify` mode（判 visual_checks→`satisfied/violated/uncertain`+`failed_checks`）+ `harness/skill_visual_contract.py` `evaluate_visual_contract`（变量契约过后截图→VL 判可见末态）接进 `skill_dispatch`（变量契约过→视觉契约→`violated` 才否决+记 health 失败+不落盘→交慢路径）。**权威纪律**：VL 是 L4 弱层，**仅 `violated` 否决**；`uncertain`/截图失败/VL 关一律 fail-open（不否决已过的变量契约）。config `contract_verify_enabled` 默认 ON。skill `fallback.yaml` 的 `visual_checks` 现在真执行（`_template` 已注释新语义）。`visual_verify` 工具也加了 contract_verify mode（schema 已加描述）。110 单测绿。**live**:example.com 真 qwen3-vl 正确判 "Example Domain"+无挑战→`satisfied`、"Payment Successful 订单已支付"不在→`violated`(failed_checks 命中)。

**场景**：skill 的 success_contract 需要视觉确认（如"页面显示提交成功""表格已渲染 N 行可见数据""挑战已消失"）。browser_call 无 error 只代表没步骤抛错，不代表任务达成。

**新增 mode：`contract_verify`**

```
输入：screenshot + contract（结构化成功判据）
输出：
  verdict: satisfied | violated | uncertain
  evidence: [string]                 # 可见证据
  failed_checks: [string]            # 哪些判据未满足
  confidence: number
```

**与 skill 衔接**：
- skill 的 `fallback.yaml` 声明 `success_contract.visual_checks`（如 `{"type":"text_present","text":"提交成功"}`、`{"type":"challenge_gone"}`）。
- workflow 跑完（browser_call 无 error）后，harness 读 skill 契约→若含 visual_checks→截图调 VL `contract_verify`→不满足则触发 contract-unmet 接管。
- 这比 `action_outcome` mode 更结构化：直接对 success_contract 逐条判定，而非笼统的 match/mismatch。

### 13.4 角色 C：VL 作为 CAPTCHA 解决驱动（核心新增）

> ✅ **v1.16 已落地**：`vl.py` `captcha_solve` mode（含代码层"诚实短路"行为型→HITL）+ `skill_control.py` `resolve_via_vl_then_hitl`（截图→VL→`solve_plan_to_input_calls`→`elementFromPoint` 安全门→第二连接 Input→`Page.getState` 验消失→`resolve_pause`，失败降级人路）。接进 dispatch（`vl.enabled` 选路）。83 单测绿。Input 参数名经面板实测（`Input.drag {x,y,dx,dy}`/`{x,y,toX,toY}`、`Input.click {x,y,clickCount}`、`Input.type {text,clear,delay}`）；`Page.screenshot` 返 `data.savedPath`。
>
> ✅✅ **v1.17 真 CAPTCHA 端到端联机实测通过（2026-06-27，yue-accelerator 手机号登录滑块）**：真实 qwen3-vl `captcha_solve` 把"请按住滑块拖动到最右边"正确判为 `solvable`/`slider`/**`visual_self_consistent`**（诚实区分生效，没误判 behavioral）+ 给出 drag solve_plan；归一化 grounding `from=(395,525)` 经 `solve_plan_to_input_calls`→CSS `(483.5,377.5)` **精确命中滑块把手**（与视觉对齐）；`_default_safety` elementFromPoint 门通过（把手非敏感控件）；`Input.drag`（内核人性化轨迹）拖动→**滑块消失、验证通过、短信已发（按钮转 "47s 重新获取" 倒计时）**。§13.4 全链路（VL 分类→翻译→安全门→Input→验证）真实闭环验证成功。
>
> ✅ **v1.22 独立 opt-in 门（§13.7 落地）**：Role C 自动解 CAPTCHA 现需独立的 `vl.captcha_solve_enabled`（默认 OFF），**不再随 `vl.enabled` 一并生效**——`skill_dispatch._should_vl_solve(vl_config)` 要求 `enabled && captcha_solve_enabled` 才选 `resolve_via_vl_then_hitl`，否则一律走人路 `resolve_via_hitl`（acting-on-challenge 是 ToS/法律层面最敏感的 VL 动作，必须显式开）。Role A/B/D 仍只看各自的 flag，不被此门影响。

**场景**：slider 滑块、grid 选格、rotate 旋转、click 点目标、text OCR。VL 不仅诊断挑战类型，还**输出可执行的解决动作序列**，harness 翻译成 ABCP Input 调用。

**新增 mode：`captcha_solve`**

```
输入：screenshot + optional context（如"这是一个 Cloudflare Turnstile"）
输出：
  verdict: solvable | unsolvable | not_a_challenge | uncertain
  challenge_type: slider | grid | rotate | click_target | text_ocr | behavioral | hybrid | unknown
  challenge_category: visual_self_consistent | behavioral_risk | unknown
  solve_plan: [SolveStep]              # 有序解决步骤（仅 visual_self_consistent 非空）
  confidence: number
  evidence: [string]

SolveStep（按 challenge_type 不同）：
  slider:   { action: "drag", from:{x,y}, dx: number, dy: 0 }
            # Input.drag(x,y, dx,dy) — 相对偏移，slider 的自然表达
  grid:     { action: "click", at:{x,y}, label: "选中含交通灯的格子" }
            # 多个 step 逐格点；每步后可重新截图验证
  rotate:   { action: "drag_arc", from:{x,y}, to:{x,y} }
            # 翻译为 Input.drag(from.x,from.y → to.x,to.y)
  click_target: { action: "click", at:{x,y}, label: "点击含指定图案的图块" }
  text_ocr: { action: "type", text: "识别出的文字", into:{x,y} }
            # 先 click(at) 聚焦输入框，再 Input.type(text)
```

**挑战类型可解性硬区分（必须诚实）**：
- **视觉自洽型**（`challenge_category: visual_self_consistent`）：slider 缺口、grid 选格、rotate 旋转、click 点目标、text OCR。答案纯视觉，VL 能解，真实收益。`solve_plan` 非空。
- **行为风控型**（`challenge_category: behavioral_risk`）：reCAPTCHA v2/v3、hCaptcha、Cloudflare Turnstile。打分对象是鼠标轨迹/时序/指纹/熵，**视觉答案正确也可能失败或升级难度**；VL 解不了"行为"。这类**短路到 HITL**，`solve_plan` 为空。反复硬刚会触发更难挑战或封禁——故有界轮次 + HITL 兜底是刚需。
- **未知型**（`unknown`）：VL 无法分类 → 短路 HITL，不盲目尝试。

**Input.drag 轨迹（✅ 已确认，2026-06-26 用户）**：`Input.drag` **已做人性化轨迹优化，基于 Chromium 内核层面**——不是仅起止点的瞬移，而是带中间轨迹的拖拽。**结论**：视觉自洽型 slider（缺口滑块）求解真实可行——VL 给缺口偏移、`Input.drag` 产出人性化轨迹，连**带轨迹检测的滑块**也有过的机会（不再受"瞬移必被识破"限制）。注意边界：`behavioral_risk` 型（reCAPTCHA/hCaptcha/Turnstile）打分的是轨迹 + 时序 + 指纹 + 熵的综合，单凭人性化轨迹不足以过，仍短路 HITL；人性化轨迹直接提升的是 **visual_self_consistent slider/rotate** 这类「答案就在视觉、动作就是一次拖拽」的挑战。

**执行闭环（VL 驱动 → ABCP 执行 → VL 验证 → 重试/放弃）**：

```
workflow listen Hitl.resumed / 检测到挑战
    │
    ▼
harness: 截图 → VL captcha_solve
    │
    ├─ verdict: solvable + solve_plan 非空
    │     │
    │     ▼
    │  for each SolveStep:
    │     反算归一化坐标 → 像素
    │     elementFromPoint 安全校验（非敏感控件误击）
    │     执行 Input.click / Input.drag / Input.type
    │     截图 → VL contract_verify(challenge_gone)
    │     ├─ satisfied → 解决成功，resume workflow
    │     └─ not_satisfied → 继续下一 step 或重试本步
    │
    ├─ verdict: unsolvable / uncertain / 重试耗尽
    │     │
    │     ▼
    │  降级：Hitl.requestPause 交人类（既有 HITL 机制）
    │
    └─ verdict: not_a_challenge
          └─ 误判，resume workflow
```

**关键设计决策**：
- **VL 不直接操作**：VL 只输出 solve_plan（归一化坐标 + 动作类型），harness 翻译成 Input 调用并做安全校验。这保留了 harness 对所有 Input 动作的可观测/可拦截/可审计。
- **逐步验证**：每个 SolveStep 后重新截图验证，不盲跑整个 plan。slider 可能需多次微调，grid 可能逐格确认。
- **重试上限**：solve_plan 最多重试 N 次（配置），耗尽则降级 HITL。不无限循环。
- **坐标安全**：继承 `overlay_classify` 的 `elementFromPoint` 校验纪律——反算坐标后独立验证指向元素，防止 VL 幻觉导致误点。
- **敏感动作拦截**：solve_plan 中的 click 若命中登录/支付/提交类控件（`is_consequential` 判定，复用 overlay_classify 逻辑），拒绝执行并降级 HITL。

### 13.5 与 skill/workflow/agent 三层架构的衔接

```
┌─ Workflow.execute（快路径）────────────────────────────────────────┐
│  ... 正常步骤 ...                                                    │
│  listen Hitl.resumed → 触发 pauseController 暂停                     │
│  （或 harness 外部检测到挑战 → 外部暂停）                             │
└──────────────────────────────────────────────────────────────────────┘
                              │ paused
                              ▼
┌─ Harness VL Solver（暂停点介入）────────────────────────────────────┐
│  1. Page.screenshot                                                   │
│  2. VL captcha_solve → solve_plan                                     │
│  3. for SolveStep: 反算坐标 → 安全校验 → Input.click/drag/type        │
│  4. 截图 → VL contract_verify(challenge_gone)                         │
│  5. solved → resume workflow | failed → Hitl.requestPause 交人类      │
└──────────────────────────────────────────────────────────────────────┘
                              │ resume
                              ▼
┌─ Workflow.execute 继续剩余步骤 ──────────────────────────────────────┐
│  ... （$cache 已被引擎在 navigate/loaded 时自动清，重跑 getAXTree）... │
└──────────────────────────────────────────────────────────────────────┘
```

**三个角色的使用时机**：

| 角色 | 何时用 | 谁触发 | 在哪跑 |
|------|--------|--------|--------|
| AXTree 补盲（visual_locate） | AXTree 找不到目标，需视觉定位 | workflow 暂停步 / agent 兜底 / **普通 browser_call 失败** | harness 侧 |
| 裁判（contract_verify） | workflow 跑完需视觉确认成功 | skill success_contract | harness 侧（workflow 完成后） |
| CAPTCHA 驱动（captcha_solve） | 遇到 CAPTCHA 类挑战 | workflow listen / harness 检测 | harness 侧（workflow 暂停时） |
| 全局视觉仲裁（browser_call 失败兜底） | click 落空/遮挡/视 layout 异常/AXTree 与视觉不一致 | BrowserAgent 失败恢复链 | harness 侧 |

**共同点**：四个角色都不在 workflow step 内直接调（VL 非 ABCP action），而是通过 **pause-resume**、**post-completion verify** 或 **browser_call 失败恢复链** 由 harness 侧介入。这保持了 workflow 的纯 ABCP-action 性质，VL 作为 harness 层能力注入。

> ✅ **v1.21 Role D 已落地 + live 证**：`harness/vl_arbiter.py` 统一失败驱动入口——`route_failure`（errorClassification type + 文本标记 → VL mode；`occlusion_blocked`→overlay_classify/dismiss、`hitl_paused_state`/captcha 文本→challenge_detection/hitl、locator 文本→visual_locate/retry_by_id、`render_lost`→action_outcome/reperceive；**非视觉类 timeout/contract_error/page_crashed→None 不调 VL**）+ `arbitrate`（截图→对应 VL mode→verdict 映射成恢复建议 `{action: dismiss|hitl|retry_by_id|coordinate|reperceive|continue|none}`；locator 走复用 Role A `locate_target` 提升耐久 id；consequential 目标→hitl；I/O 全可注入）。**统一了 A/B/C/overlay 四种 VL 能力于一个失败驱动入口**。**已接进慢路径热路径自动触发**:`_execute_browser_capability_tool` 在确定性恢复（overlay auto-intercept）后调 `_maybe_vl_arbitrate`——视觉类失败（且 `vl.arbiter_enabled`）→ arbiter → 把恢复建议（`resolvedId`/`hitl`/`dismiss`/`reperceive` + `next_instruction`）挂回 result 给 agent；非视觉失败/VL 关 no-op,per-worker 上限 `max_checks_per_worker`,internal 调用不触发,best-effort 不破调用链。126 单测绿（+4 wiring）。**live**:模拟失败 `Input.click`(目标解析不了)→`_maybe_vl_arbitrate`→真 VL 定位→`vlArbiter:{retry_by_id, 3:13:13}`+指示 agent 用 id 重试。config `arbiter_enabled` 默认 OFF（每次视觉失败一次 VL 调用,opt-in）。

**VL 作为全局能力（不只 workflow）**：普通 browser_call 失败同样可触发 VL 兜底。恢复顺序：browser_call 失败 → 查 ActionFeedback/errorClassification → Page.getState → 刷新 DOM.getAXTree → 确定性恢复（dismiss_overlay 阶梯等）→ **仅当失败是视觉/遮挡/挑战/layout 相关时才 VL** → 一次有界动作（若安全）→ 重观察验证。VL 不用于批量列表/表格抽取（DOM 可精确回答的场景）。

**VL 安全门结构（所有坐标输出 mode 通用）**：
```
safety: {
  is_consequential: bool,         # 是否登录/支付/提交/授权类控件
  requires_dom_cross_check: bool, # 是否需 DOM/AX 二次确认
  allowed_action: "coordinate_click_once" | "coordinate_drag_once" | "none",
  blocked_reason: string          # 拒绝原因（空=允许）
}
post_action_verification: {
  type: "dom_or_state_required",
  suggested_checks: [Page.getState, DOM.getAXTree, DOM.getText]
}
```
`is_consequential=true` 的目标拒绝坐标操作，降级 HITL。每次坐标动作后必须 DOM/状态验证。

### 13.6 ABCP Input 能力与 SolveStep 映射

| CAPTCHA 类型 | VL SolveStep | ABCP Input 调用 | 说明 |
|-------------|-------------|----------------|------|
| slider 滑块 | `drag, from:{x,y}, dx:N` | `Input.drag(x,y, dx=N, dy=0)` | 相对偏移，slider 的自然表达 |
| grid 选格 | `click, at:{x,y}` × N | `Input.click(x,y)` × N | 逐格点击，每步后可重新截图 |
| rotate 旋转 | `drag, from:{x,y}, to:{x,y}` | `Input.drag(x,y → toX,toY)` | 弧线拖拽近似 |
| click 点目标 | `click, at:{x,y}` | `Input.click(x,y)` | 单/多次点击指定图案 |
| text OCR | `type, text:"...", into:{x,y}` | `Input.click(x,y)` + `Input.type(text)` | 先聚焦再输入 |

**前提**：VL 模型必须能精确输出归一化坐标（0-1000 体系，与现有 overlay_classify 一致）。这是选用 VL 模型的硬要求——不能只给文字描述，必须给可执行坐标。

> ✅ `Input.drag` 已是 Chromium 内核层面的人性化轨迹（非瞬移，见 §13.4），所以 slider/rotate 这两行的拖拽对带轨迹检测的滑块也有过的机会。

### 13.7 配置扩展

`VLConfig`（`harness/config.py:21`）字段（✅ **v1.22 全部落地**）：
```python
captcha_solve_enabled: bool = False        # ✅ Role C 自动解 CAPTCHA 独立 opt-in（与 vl.enabled 解耦）
captcha_solve_max_retries: int = 2         # ✅ solve_plan 最多重试次数
visual_locate_enabled: bool = False        # ✅ Role A AXTree 补盲默认关
contract_verify_enabled: bool = True       # ✅ Role B 裁判默认开（低成本验证）
arbiter_enabled: bool = False              # ✅ Role D 失败驱动仲裁默认关
```

CAPTCHA 解决默认关闭——这是高风险能力（自动操作安全验证），需用户显式 opt-in。**门由 `skill_dispatch._should_vl_solve(vl_config)` = `enabled && captcha_solve_enabled` 把守**，二者缺一则 mid-execute 暂停一律走人路 `resolve_via_hitl`。判存（challenge_detection）和裁判（contract_verify）可默认开。

### 13.8 风险与纪律

| 风险 | 纪律 |
|------|------|
| VL 幻觉给出错误坐标 → 误点敏感控件 | 所有坐标经 `elementFromPoint` 独立校验；敏感控件（登录/支付/提交）拒绝执行，降级 HITL |
| CAPTCHA 解决被反爬识别为机器人行为 | 逐步验证 + 随机化动作时序（人类化）；solve_plan 失败不无限重试 |
| VL 模型不支持坐标输出 | 选型硬要求：必须支持归一化坐标输出；不支持则该 mode 不可用 |
| 自动解 CAPTCHA 的合规性 | 默认关闭，需用户显式 opt-in；skill 声明是否允许自动解（`allow_auto_captcha: false` 默认） |
| solve_plan 与页面状态不同步 | 每个 SolveStep 后重新截图，不假设页面静止；导航/重载后 $cache 已清，重跑 getAXTree |
| VL 调用成本 | `max_checks_per_worker` 既有限额扩展到覆盖 solve 重试；CAPTCHA 解决消耗多倍配额 |

### 13.9 落地优先级

1. **角色 B（裁判）优先**：`contract_verify` mode，风险最低，直接强化 skill success_contract。P2 参考 skill 即可接入。
2. **角色 A（补盲）次之**：`visual_locate` mode，扩展 overlay_classify 的坐标定位能力到任意目标。P3。
3. **角色 C（CAPTCHA 驱动）最后**：`captcha_solve` mode，风险最高、收益最大。先在受控环境验证 VL 坐标精度和 solve_plan 正确率，再接入真实 CAPTCHA。P4-P5。

角色 C 的验证里程碑：在已知类型的测试 CAPTCHA（如本地搭建的 slider/grid）上，VL 输出坐标精度 ±5% 归一化单位内，solve_plan 首次成功率 ≥ 70%，才认为可用。
