# ABCP × Harness 集成架构计划

> 目标：把当前只能终端运行的 Python browser agent harness 集成进 ABCP Electron 应用，
> 打包成对外分发的成品 app（Mac universal + Windows），最终用户**双击即用、免装 Python、
> 免开终端、免自带 LLM key**（LLM key 走账号体系云端下发）。
>
> 本文档基于一次 11-agent 调研 + 3 视角对抗审查收敛，并整合了产品决策。

---

## 1. 决策记录（已拍板）

| 维度 | 结论 | 影响 |
|---|---|---|
| harness 定位 | **内置原生默认 browser agent**，首启自起，WebSocket 连 abcp browser 不变 | 面板开箱即有一个自带 brain 的 agent；首启 provision + autoStart |
| MCP | 退为调试入口；未来 CLI 接 Claude Code/Codex 为外部 agent 路径 | 需 `agentType` 身份模型区分 harness/mcp_proxy/cli_external（P2+） |
| LLM key | **账号体系云端下发，不本地持久化** | 需新建账号后端（P5）+ key 下发协议（P4） |
| 账号体系 | **本项目内一起规划后端** | 新增 P5 账号后端服务 |
| Mac 架构 | universal（arm64 + x64） | harness 双架构冻结 + lipo 合并；user eb arch 改 universal |

---

## 2. 现状速览

**ABCP 侧（`abcp browser/`，pnpm + nx + Electron monorepo）**
- 双应用：`user`（面板/驾驶舱）+ `client`（定制浏览器）；中间 `dispatcher`（调度大脑，WebSocket 9300，JSON-RPC）。
- `dispatcher` 已内置 `AgentProcessManager`（`packages/dispatcher/src/core/agentProcessManager.ts`）：`spawn(command, args, {cwd, env, ABCP_AGENT_ID})`，配置存 SQLite `agent_configs` 表（name/command/args/env/cwd/autoRestart，env 加密）。
- 面板已有 `startAgent/stopAgent` 全链路（`useAgentStore` -> IPC -> `DispatcherBridge` -> `AgentProcessManager`）和 `AgentMessageFlow`/`AgentInputBar`/`AgentSelector` UI。
- 外部 Agent 协议已就绪：WebSocket 连 `wss://localhost:9300/ws?token=<JWT>`，`System.register` -> 业务调用；`Agent.sendMessage`/`System.streamMessage` 上行；`System.notification` 下行（含 `user_message`）。

**harness 侧（根目录 Python）**
- 入口 `main.py` 是**交互式 CLI**：`input()` 取任务、`print` 输出，单任务跑完退出。
- `abcp_client.py` 已能连 9300、`System.register`、订阅通知（NotificationHub）。
- 直依赖仅 4 个（anthropic/openai/websockets/PyYAML），但传递依赖含原生扩展（pydantic-core/cryptography/jiter）。
- 数据目录：`worktree/`（运行时可写任务根）、`skills/`+`strategy_bank/`（只读资源，但运行时写 health/heal json）、`global_schema_cache/`（首启可重建）。
- **安全问题**：`config example.json:4` 含真实 sk-kimi 明文密钥，已进 git 历史。

---

## 3. 总体架构：6 层改造

| 层 | 现状 | 目标 |
|---|---|---|
| **L1 harness 入口** | 交互式 CLI，单任务退出 | 常驻进程：订阅 `user_message` 当任务、第二连接流式回推进度 |
| **L2 dispatcher** | 只 spawn，不注入 token/ws、不现签 JWT、无种子 | 种子配置 + 路径占位符解析 + 现签短期 JWT + autoStart/autoRestart 分离 |
| **L3 面板** | `+` 死按钮、无密钥/配置 UI | 复用消息流 UI + 新增账号登录/配置编辑 |
| **L4 打包** | extraResources 嵌 client/mcp，无 harness；签名公证全空 | PyInstaller universal 冻结 + extraResources 嵌入 + 签名公证 |
| **L5 密钥认证** | 明文密钥进 git；CA 私钥配置随包；本地 JWT 90d 无续签 | 三套凭证分离（内部 JWT / 账号 session / LLM key），key 不落盘 |
| **L6 账号后端**（新增） | 无 | 注册/登录/订阅/计费 + LLM key 池与短期下发 |

---

## 4. 阶段路线

依赖关系：`P0 -> P1 -> P2 -> P3`（用本地 dev key 跑通）∥ `P5`（账号后端，可并行）-> `P4`（集成账号，替换本地 key）。

### P0 — 面板启动 harness 跑通（dev 态，不打包）
**目标**：面板点 start 拉起 `python main.py --panel`，harness 连上 dispatcher 并 `System.register`，AgentSelector 出现 harness。

步骤：
1. dispatcher 首启 provision：新建 `provisionAgentConfigs.ts`，`container.ts:90` AppContext 末尾按 `name='harness'` 查重 -> insert 默认 config（command/args/cwd 用占位符 `${abcpRoot}`，不存绝对路径）。
2. `AgentProcessManager.start`（`agentProcessManager.ts:42`）加路径解析钩子：`${resourcesPath}`/`${abcpRoot}` 占位符替换，复用 `paths.ts:16`；dev 态 `app.isPackaged?resourcesPath:workspaceRoot`。
3. `AgentProcessManager.start` 注入 `ABCP_WS_URL` + `ABCP_JWT_TOKEN` env 约定（dev 态 TLS off + 无 token）。
4. harness：`main.py:425 load_runtime_config` 加 `ABCP_AGENT_ID`/`ABCP_WS_URL`/`ABCP_JWT_TOKEN` env 读取，优先级 **CLI > env > config.json > 默认**（CLI 最终决定权）。
5. **立即拆雷**：`electron-builder.cjs:76` 删 certs extraResources；`config example.json:4` 删明文 sk-kimi + 作废轮换该 key + git 历史清理评估。

验证：面板 startAgent(harness) -> dispatcher 日志见 `System.register` -> AgentSelector 出现 harness 绿点 -> 发 'hello' 见 `user_message` 推送（harness 可暂不消费）。

### P1 — 面板闭环（user_message 下发 + streamMessage 回报 + HITL）
**目标**：面板下发任务 -> harness 消费执行 -> 流式回报进度 + 终态结果 -> HITL 放行续跑。

步骤：
1. 新建 `harness/observation/task_input_observer.py` 订阅 NotificationHub，**按实际格式过滤**（`agentMessenger.ts:23-30`：`method=='System.notification'` -> `params.type=='user_message'` -> `params.data.content`，**不是** `params.data.event`）入 `asyncio.Queue`。
2. `main.py:1040 run_cli` 重构为**常驻循环**（`while queue.get()`，跑完不退出）；每任务边界 reset `_LAST_LOGGER` 等全局态。autoRestart 只管意外崩溃。
3. 新建 `harness/panel_reporter.py`：PanelReporter 作为 RunLogger 第二 `on_event` sink；**同步回调->异步发送桥接**用 `asyncio.Queue + consumer task`（`on_event` 只 `put_nowait`，consumer 异步发），严禁 fire-and-forget。
4. PanelReporter 经**进程级独立第二连接**（不复用 worker slot 连接）调 `Agent.sendMessage`/`System.streamMessage`；`main.py:1131` 终态答案处用同一进程级连接补发。lead 模式也要有进程级连接（不依赖 `browser` 参数）。
5. event_type 白名单转发（`lead.step.start`/`agent.step.start`/`lead.tool.result`/`task_plan.*`/`progress.intervention`/`run.error`/`lead.final`），transport 噪音全丢；streamMessage partial/complete 正确配对。
6. HITL 起步用方案 A（harness 自主 VL-verify + resolvePause，面板放行仅 UI）；live 验证面板 resolvePause 与 harness `signal_resumed` 双发 `Hitl.resumed` 的幂等性，不幂等则加 dedup。
7. **第二连接身份隔离**：第二连接用独立 agentId（`agent:<configId>:control`）的 JWT，dispatcher 识别 `:control` 后缀不覆盖主连接 `agentSockets`（或支持主+控制双 socket）。**TLS 开启前必须解决**（TLS off 时 `certAgentId=undefined` 不覆盖，是假象）。

验证：面板输入"打开 example.com 截图" -> AgentMessageFlow 见流式进度 + 终态答案 -> 触发挑战弹 HITL -> 放行续跑完成。pytest 回归全绿。无 `_call_lock` 死锁。

### P2 — PyInstaller 打包 harness（universal）
**目标**：harness 冻结成 mac-arm64 + mac-x64 自包含可执行（onedir），含只读资源，可写目录重定向。

步骤：
1. 新建 `pyproject.toml`（`python_requires>=3.10`）+ `pip-tools compile` 生成 `requirements.lock` 固化 pydantic-core/cryptography/jiter 版本。
2. 新建 `harness.spec`（PyInstaller onedir）：entry=main.py；`--add-data skills`（只读模板）+ `strategy_bank/strategy_bank.json` + `config example.json` 模板；为 pydantic/cryptography/jiter 加 hiddenimports + hooks 收集 `.so/.pyd/.dylib`。
3. 新建 `harness/paths.py` 统一定位：只读走 `sys._MEIPASS` 回退 + `ABCP_RESOURCES_DIR` env；可写走 `ABCP_WORKROOT`（默认 `app.getPath('userData')/harness`）。改 `registry.py:41` + `strategy_bank.py:16` + `schema_cache.py:32` + `utils.py:239` 全走 helper，不留 cwd 锚定。
4. 可写目录重定向：worktree/global_schema_cache/.skill_health/.create_report/.heal_history/.guidance_health -> `ABCP_WORKROOT`；首启从 _MEIPASS 拷贝 skills 种子到可写 overlay；**overlay 合并优先级**：userData 同名 skill 覆盖 resources 基线，auto-heal 新 skill 只写 userData，拷贝种子记 manifest 供升级决策。
5. 回归：`inspect.get_type_hints` 路径覆盖 `skill/registry.py:339`/`create.py:1036`/`schema_loader.py:282`，确认 PEP 604 不炸。
6. **双架构原生构建**：mac-arm64 + mac-x64 各自跑 PyInstaller -> `lipo` 合并 universal；不能单平台产物分发。win-x64 另建。cryptography 在 win-x64 的 OpenSSL 静态链接确认。

验证：双架构各产 onedir；`lipo` 合并 universal；双击 `<exe> --version` 起；连本地 dispatcher 跑最小 task 抽真数据（skills 加载/worktree 写入/schema bootstrap/LLM 调用全通）。

### P3 — 随 Electron 分发（extraResources + 签名公证 + per-install CA）
**目标**：harness 产物嵌入 .app/.exe，签名公证过 Gatekeeper/SmartScreen，每机 CA 独立。

步骤：
1. `electron-builder.cjs:64` extraResources 追加 `{from:'../../harness-dist/universal', to:'harness'}`；**补 win 分支**（现有 `from:'../client/release/mac-${arch}'` 是 mac 写死，win 打包会找不到 -- 必须用平台条件目录命名或函数式 config）。
2. `package.json:25` dist:mac/dist:win 脚本链加 `harness-freeze` 前置步骤（在 eb:user 之前）。
3. `shared/paths.ts` 扩展 AppPaths 增 `harnessBinDir`/`harnessResourcesDir`/`harnessWorktreesDir`；agent_config command 指向 `process.resourcesPath/harness/<exe>`。
4. **per-install CA**（提前到 P0 做兜底，P3 完善）：`tlsConfig.ts:25` 加 `app.getPath('userData')/certs` 分支优先；首启 dispatcher 跑 `loadOrProvisionCa()`（`gen-token.mjs:20-50` init 逻辑下沉为模块，**selfsigned 库从 devDependencies 提到 dependencies**，或换 node 内置 crypto 自签）；CA 私钥权限 macOS 0600/Win ACL。
5. `AgentProcessManager.start` 每次 spawn 前用 CA 私钥**现签短期 JWT**（1-7d，非 90d）注入 `ABCP_JWT_TOKEN` -- 续签问题消失；`autoRestart` 加 maxRetries + 指数退避 + `code===null`（SIGTERM 来自 stop 不重启）区分；仿 `fleetManager.ts:82` 给 per-agent log 文件。
6. **签名公证**（从零搭）：Developer ID + notarization creds；PyInstaller 产物先 `codesign --deep --options=runtime` 再嵌入；user eb afterSign 钩子走 `@electron/notarize`；entitlements.plist + hardenedRuntime；嵌套 client .app + harness 二进制随主包签名；Windows WIN_CSC_LINK；先 `dist:mac:adhoc` 内测。

验证：全新机器装 dmg/exe -> 双击起 -> 无 Gatekeeper/SmartScreen 拦截 -> 首启自起 harness -> 跑真 task；解包确认无 ca.key/无明文 api_key；`codesign --verify` 过。

### P4 — 账号体系集成（登录 + key 云端下发 + 不落盘）
**目标**：最终用户登录账号 -> 后端下发短期 LLM key -> harness 内存使用 -> 不落盘。

步骤：
1. 面板新增 `LoginView`（账号密码/OAuth）-> 后端返回 session JWT -> 存 safeStorage（加密落盘的是 session，**不是 LLM key**）。
2. `AgentProcessManager.start` spawn 前：用 session JWT 调后端 `POST /v1/llm/credentials` -> 返回短期 LLM key + endpoint -> 注入 harness env `ABCP_LLM_API_KEY`/`ABCP_LLM_BASE_URL`。
3. harness 内存用 key；key 过期（401）-> harness 推错误 -> dispatcher 重新换取注入（或重启现换）；key 绝不落盘。
4. `agentConfigModel.ts decodeEnv` 加 try/catch：跨机恢复解密失败标记 `needs_reinput` 触发重输弹窗（现状是抛错崩溃，不是静默丢弃）。
5. logger redact 全通道：`agentProcessManager.ts:60` stdout/stderr handler + dispatcher/user 所有 logger 对 env 输出统一 redact `*KEY*`/`*TOKEN*`/`*SECRET*`；日志文件权限 0600。
6. VL api_key 一并走 `ABCP_VL_API_KEY`（`vl/core.py:574` 补 `api_key_env` 间接寻址，anthropic 路径 `:625` 同步）。
7. JWT 三方一致性：JWT sub / spawn `ABCP_AGENT_ID` / harness `System.register` agentId 同源（`agent:<configId>` 带前缀），server register 可选校验防冒充。

验证：全新用户登录 -> harness 拿到短期 key 跑通真 task -> grep 日志无明文 key -> key 过期自动续 -> 跨机恢复 DB 解密失败有引导重输。

### P5 — 账号后端服务（与 P0-P3 并行规划/建设）
见第 6 节设计骨架。

---

## 5. 必须先拆的雷（blockker 清单）

| # | 雷 | 修正 |
|---|---|---|
| 1 | **进程模型矛盾**：综合稿原版"跑完退出由 autoRestart 重启"，但退出码 0 不触发 autoRestart，面板消息直接报 "Agent not connected" | P1 改常驻循环 `while queue.get()` |
| 2 | **CA 私钥随包**：`electron-builder.cjs:76` 配 certs extraResources（目录不存在才静默失败），补目录即泄露 | P0 删配置 + per-install CA 生成兜底（不分阶段） |
| 3 | **明文密钥进 git**：`config example.json:4` 真实 sk-kimi 已入历史 | P0 删 + 作废轮换 + git 历史清理评估 + CI 守卫 |
| 4 | **TLS 第二连接覆盖主连接**：`server.ts:209` JWT 认证时 `agentSockets.set` 无条件覆盖，第二连接复用主 JWT 顶掉主路由 | P1 第二连接用 `agent:<configId>:control` 独立身份 |
| 5 | **PanelReporter 同步/异步鸿沟**：`RunLogger.write` on_event 同步，无法 await 第二连接 | `asyncio.Queue + consumer task` 桥接 |
| 6 | **user_message 格式假设错误**：不是 `params.data.event`，是 `params.type=='user_message'` | P1 按实际格式实现 |
| 7 | **token 命名不统一**：方案四处用 `ABCP_AGENT_TOKEN`/`ABCP_JWT_TOKEN` 两个名 | 全仓统一 `ABCP_JWT_TOKEN`（对齐 `abcp_client.py:199`） |
| 8 | **env > CLI 优先级破坏 CLI 决定权** | 改 **CLI > env > config.json > 默认** |
| 9 | **终态回推连接作用域**：`main.py:1131` 在 `async with ABCPClient` 作用域外 | 维护进程级独立连接 |
| 10 | **JWT 90d 无续签** | 每次 start 现签短期 token（1-7d） |
| 11 | **decodeEnv 跨机恢复抛错崩溃**（非静默丢弃） | try/catch + `needs_reinput` 标记 |
| 12 | **logger 零 redact**：harness stdout/stderr 原样进日志文件 | 全通道 redact |
| 13 | **win extraResources from 路径 mac 写死** | 平台条件目录命名或函数式 config |
| 14 | **selfsigned 在 devDependencies**：打包后 per-install CA 生成 require 失败 | 提到 dependencies 或换 node crypto |
| 15 | **路径锚点不一致**：skills 锚 `__file__`、strategy_bank 锚 cwd、schema_cache 锚 worktree 父目录 | `harness/paths.py` 统一 + `ABCP_WORKROOT` 重定向 |
| 16 | **autoRestart 无上限 + code===null 误重启** | maxRetries + 退避 + null 语义区分 + stop() 竞态修复 |

---

## 6. 账号后端设计骨架（P5）

### 6.1 职责
用户注册/登录、订阅管理、LLM key 池管理与短期下发、用量计费。

### 6.2 三套凭证分离（关键设计）
| 凭证 | 用途 | 签发 | 存储位置 | 有效期 |
|---|---|---|---|---|
| ABCP 内部 JWT | agent <-> dispatcher 认证 | per-install CA（本地） | 不存（dispatcher 现签注入 env） | 1-7d 现签 |
| 账号 session JWT | 客户端 <-> 账号后端 | 账号后端（云端） | 客户端 safeStorage 加密 | 1h，可刷新 |
| LLM key | harness 调 LLM | 账号后端 key 池下发 | **仅 harness 进程内存，不落盘** | 1h 短期 |

三者职责分离，绝不混用。

### 6.3 key 下发协议（解决"不本地持久化"）
1. 面板登录 -> 后端返 session JWT -> safeStorage 加密落盘（session，非 key）。
2. dispatcher spawn harness 前，用 session JWT 调 `POST /v1/llm/credentials` -> 返回短期 LLM key + endpoint。
3. dispatcher 注入 harness env（`ABCP_LLM_API_KEY`/`ABCP_LLM_BASE_URL`）-> harness 内存用。
4. key 用完即弃；过期 401 -> dispatcher 重新换取注入；harness 长跑靠短期 key + 刷新。
5. 客户端永远拿不到长期 LLM key（key 池在后端）。

### 6.4 核心 API
- `POST /v1/auth/register` / `POST /v1/auth/login` -> session JWT
- `POST /v1/auth/refresh`
- `GET /v1/account`（订阅状态）
- `POST /v1/llm/credentials` -> 短期 key + endpoint（凭 session JWT，绑定 session + 用量限额）
- `POST /v1/usage/report`（harness 上报用量，计费）
- `POST /v1/billing/subscribe`

### 6.5 安全要点
- LLM key 池在后端，短期下发 + 绑定 session + 用量限额 + 轮换。
- session JWT 与 ABCP 内部 JWT 分离；传输全 TLS。
- 短期 key 绑定单次会话，泄露影响面小。

### 6.6 待决策（见第 7 节）
技术栈、自建 vs BaaS、计费模型、LLM key 池来源、部署。

---

## 7. 待用户决策

1. **账号后端技术栈**：Node/NestJS（与 ABCP TS 栈一致）/ Go / Python/FastAPI（与 harness 栈一致）？
2. **自建 vs BaaS**：自建全栈 vs Supabase/Auth0/Clerk（认证）+ 自建 key 下发？
3. **计费模型**：订阅制 / 按量计费 / 免费+额度？
4. **LLM key 池来源**：你自己的 key 转售 vs 接 LLM 厂商代理 vs 用户 BYOK 到后端？
5. **部署**：Docker + 云（哪家）/ 自托管？
6. **签名公证证书**：Developer ID + notarization creds（mac）/ EV or 普通代码签名（win）-- 申请周期数天到数周，需尽早启动。

---

## 8. 风险登记（按严重度）

- **[CRITICAL 安全]** CA 私钥随包 + 明文密钥进 git（雷 2/3）-> P0 立即处理。
- **[CRITICAL 架构]** `_call_lock` 单连接串行 + TLS 第二连接覆盖（雷 4/5）-> PanelReporter 必须第二连接且独立身份。
- **[高 跨平台]** 原生扩展（pydantic-core/cryptography/jiter）不跨平台，必须每架构原生 PyInstaller；cryptography win OpenSSL 静态链接未验证。
- **[高 分发]** 签名公证管线全缺，嵌入 PyInstaller 嵌套二进制签名是硬门槛，证书申请周期长。
- **[高 分发]** 无 electron-updater，harness 二进制升级走整包重发；易变部分（skills/workflow.json/schema_cache）放 userData overlay 缓解。
- **[高 账号]** 账号后端从零建，key 下发协议安全性 + 计费体系是新工程量。
- **[中 生命周期]** autoRestart 无上限 thrash（雷 16）；进程模型必须常驻（雷 1）。
- **[中 路径]** 打包后 cwd/__file__ 依赖，PyInstaller _MEIPASS 只读（雷 15）。
- **[中 协议]** HITL resolvePause 双发幂等未 live 验证；spawner 衍生 agentId 与面板路由兼容性未验证。
- **[中 体积]** PyInstaller onedir 30-80MB + client Electron + user runtime，dmg/nsis 可能 200MB+。
- **[低 身份模型]** dispatcher 不区分 agent 类型，产品化前补 agentType（P2+）。

---

## 9. 落地节奏建议

- **立即**：P0（不依赖账号体系/打包，5 个步骤，含拆雷 2/3）。
- **并行**：P5 账号后端技术栈决策 -> 开建（独立后端，可与 P0-P3 并行）。
- **随后**：P1 -> P2 -> P3 -> P4。
- **尽早启动**：签名公证证书申请（数天到数周，阻塞 P3 上线）。
