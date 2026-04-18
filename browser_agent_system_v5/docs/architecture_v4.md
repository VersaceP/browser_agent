# 浏览器特工系统 V4 架构设计白皮书 (Browser Agent System V4)

基于“端到端任务闭环”的真实业务需求，结合 Claude Code 架构特性的深度剖析，V4 版本在原有“全能跨域操作引擎”的基础上，确立了**高并发、解耦、动态调度以及极致安全**的大统一核心架构原则。

---

## 一、 系统核心设计理念

> [!IMPORTANT]
> **“频道解耦路由 + 数据驱动 Agent 注册表 + 4层安全防御 + 运行时活体热换脑 + WorkTree 严格物理隔离”**

1. **Agent 不是类，而是数据**：彻底抛弃庞大的 `AsyncTeammate` 上帝对象。执行引擎（`execution_loop.py`）是一个通用的纯函数生成器。所有 Agent（Lead, Browser, Coding, Verification）的差异化完全由 `AgentDefinition` 配置（工具白名单、Trust Level、System Prompt）驱动。
2. **执行指令与状态数据的强制解耦**：状态数据被收敛在纯净的 `TeammateContext` 中。支持在不丢失 Session 记忆的前提下，底层无感取消协程并热切换大模型底座（Hot Model Swap）。
3. **四层沙箱安全防御与上下文压缩**：面对不可控的互联网环境，建立 L1-L4 的严格权限拦截闭环（Prompt 隔离 → 权限熔断 → 工具参数校验 → 浏览器隔离）。针对海量 DOM 日志，提供两级上下文压缩管道（即时截断落盘 + 80% 阈值 LLM 摘要），防止 Token 爆炸。
4. **生命周期 Hook 拦截器**：内建 5 大核心生命周期 Hook（Session 启动、工具前置、工具后置、压缩前置、Turn结束），为实现“对抗性验证 (Verification)”以及数据清洗提供回调注胶孔。

---

## 二、 全局统筹组件宏观流向图

```mermaid
graph TD
    subgraph "指令分配分派台 (GUI 路由中心)"
        GUI["外部系统面板/命令行"]
        CR["ChannelRouter<br>(频道分发与资源撮合总局)"]
    end

    subgraph "Lead Agent 层 (任务脑区)"
        LEAD["LeadAgent<br>(包工头/拆解任务/汇总数据)"]
        AS["AgentSpawner<br>(Agent孵化器与注册表)"]
    end

    subgraph "纯净执行流与核心引擎"
        CTX["TeammateContext<br>(纯数据态: 记忆与状态)"]
        LOOP["ExecutionLoop<br>(异步生成器/核心驱动循环)"]
        HOOK["HookRegistry<br>(生命周期拦截器)"]
        COMPACT["ContextCompactor<br>(两级上下文压缩管道)"]
    end

    subgraph "数据驱动的 Agent 衍生分支"
        BA["BrowserAgent<br>(浏览专精/网页提取)"]
        CA["CodingAgent<br>(数据清洗/代码执行)"]
        VA["VerificationAgent<br>(只读审查/对抗验证)"]
    end

    subgraph "安全挂载层与执行池 (Execution Pool)"
        TR["ToolRegistry<br>(全局工具防抖/调度中心)"]
        SAN["InputSanitizer<br>(L3安全防波堤/路径校验)"]
        DENY["DenialTracker<br>(L2权限连续拒绝熔断器)"]
    end

    subgraph "绝对隔离物理外域"
        WT["WorkTree<br>(强制目录死锁与隔离保存)"]
        BROWSER["Browser Context<br>(接管的 CDP 实例)"]
    end

    subgraph "自研环境池与 MCP 协议枢纽"
        RM["ResourceManager<br>(配额与环境账本)"]
        SP["ShadowPilot 浏览器集群<br>(自研指纹伪装引擎)"]
        DISP["内置 Dispatch 模块<br>(MCP 协议接收网关)"]
    end

    GUI -->|派发任务指令| CR
    CR -->|唤醒主干协程| LEAD
    LEAD -->|创建独立上下文| CTX
    LEAD -->|查询 DB 获取账号参数| CTX
    LEAD -->|请求孵化打工人| AS
    
    AS -.->|配置组装/挂载 PROFILE_ID| BA
    AS -.->|配置组装| CA
    AS -.->|配置组装| VA

    BA & CA & VA -->|将专属上下文推入| LOOP
    LOOP <-->|触发五大生命周期| HOOK
    LOOP <-->|检测 Token 水位| COMPACT
    COMPACT -.超限强制落盘.-> WT
    
    HOOK -.->|"1.[session_start] 申请池化资源"| RM
    RM -.->|2.调用 API 拉起浏览器实例| SP
    SP -.->|3.拉起完毕并暴露端口| DISP
    DISP -.->|4.MCP 客户端初始化连接| BROWSER

    LOOP -->|1.调用工具请求| TR
    TR <-->|2.前置安全审查| SAN & DENY
    TR <-->|3.MCP 指令下发| DISP
    DISP <-->|4.物理驱动引擎| WT & BROWSER
    TR -->|5.返回统计摘要| LOOP
    
    LOOP -->|最终精简 Summary/JSON| LEAD
```

---

## 三、 四大内建特工拓扑 (4-Agent Topology)

系统不再是简单的二元模型，而是构建了严密的流水线分工体系。

```mermaid
mindmap
  root((Agent Spawner))
    LeadAgent
      职能: 任务统筹与指令拆解
      权限: WRITE
      工具: SpawnAgent, 聚合汇总
      约束: 严禁底层抓取或运行代码
    BrowserAgent
      职能: 网页导航与特征提取
      权限: WRITE
      工具: Navigate, Extract, Click
      约束: 严禁执行 Python, 严禁派生
    CodingAgent
      职能: 数据清洗与 pandas 操作
      权限: ADMIN (需显式授权)
      工具: RunPython, AnalyzeData
      约束: 限制在 WorkTree 内读写
    VerificationAgent
      职能: 对抗性质检 (寻找最后 20% 缺陷)
      权限: READONLY (系统级拦截写操作)
      工具: 纯只读比对探测
      触发: pre_turn_complete 自动激活
```

---

## 四、 端到端核心业务流时序图 (含 Hook 钩子与压缩)

此流程演示了 LeadAgent 如何调度 BrowserAgent 抓取网页，以及底层的拦截机制。

```mermaid
sequenceDiagram
    participant Lead as LeadAgent
    participant Engine as ExecutionLoop
    participant Hook as HookRegistry
    participant Context as TeammateContext
    participant TR as ToolRegistry
    participant RM as ResourceManager
    participant SP as ShadowPilot实例
    participant Ext as 内置 Dispatch 模块 (MCP Server)
    
    Note over Lead,Context: 1. Lead 统筹目标并查询 DB 获取账号 ID
    Lead->>Context: 组装任务与环境 env_vars={"PROFILE_ID": "sp_j9sd2kx"}
    Lead->>Engine: yield from execute_turn(BrowserContext)
    
    Engine->>Hook: emit("session_start")
    
    Note over Hook,Ext: [前置环境分配与指纹防关联]
    Hook->>RM: 拦截并解析 context 携带的 PROFILE_ID
    RM->>SP: HTTP GET /start?user_id=sp_j9sd2kx
    SP-->>RM: 唤醒成功，暴露 WebSocket 端口
    RM->>Ext: Agent 建立 WebSocket 连接 (MCP 协议)
    Ext-->>Hook: MCP Server 握手成功并输出 Schema
    Hook-->>Engine: 环境组装流转完成
    
    loop API生成循环 (Tool Execution)
        Engine->>Engine: Query Model -> 决策出 [navigate_url]
        
        Note over Engine,TR: [Hook 点 1] 工具执行前进行 L2/L3 安全校验
        Engine->>Hook: emit("pre_tool_execute", {tool: "navigate_url"})
        Hook->>Hook: 内置 InputSanitizer 审查
        Hook-->>Engine: allow (修改/放行)
        
        Engine->>TR: push_to_pool() -> 发起请求
        TR->>Ext: [MCP Protocol] CallTool(navigate_url)
        Ext->>SP: 驱使浏览器执行底层动作
        SP-->>Ext: 执行结果 / DOM
        Ext-->>TR: 返回海量 500 万字 JSON/DOM String
        
        Note over TR,Engine: 突破 max_result_chars, 第一级压缩落盘降级
        TR->>Ext: 强制 DOM 写入 WorkTree/data.json
        TR-->>Engine: 降级返回: {"status": "ok", "saved": "data.json"}
        
        Note over Engine,Context: [Hook 点 2] 工具执行后处理
        Engine->>Hook: emit("post_tool_execute")
        Engine->>Context: context.append_message(工具摘要日志)
        
        Note over Engine,Context: 检查 Token 水位，超 80% 触发第二级压缩
        alt 触发压缩
            Engine->>Hook: emit("pre_compact")
            Engine->>Context: 将历史 Message 交由小模型浓缩成 [COMPACTED_SUMMARY]
        end
    end
    
    Note over Engine,Lead: BrowserAgent 任务即将收尾
    Engine->>Hook: emit("pre_turn_complete", final_response)
    
    alt 对抗性验证触发 (以 LeadAgent 视角)
        Hook->>Hook: 隐蔽拉起 Verification Agent
        Hook-->>Engine: block("数据缺失 40行, 请继续") -> 返回错漏
        Engine->>Engine: 将 Error 注入上下文，继续大循环抢修！
    else 验证通过
        Hook-->>Engine: allow
    end
    
    Engine-->>Lead: 生成器终结，返回纯净 JSON 成果！
    Lead->>Hook: emit("session_end") -> 关停触发
    Hook->>RM: 结束 MCP 链接，请求回收 ShadowPilot 实例
```

---

---

## 五、 自研浏览器 ShadowPilot 与 MCP 协议接管方案

在应对真实世界的电商、社交媒体等风控场景时，不可使用本地原生的浏览器进程。V4 架构摒弃了传统的底层 CDP 直接相连模式，转而利用**自研跑分指纹浏览器 ShadowPilot** 配合 Model Context Protocol (MCP) 规范实现解耦和高度安全接管：

### 1. 本地存储物理映射隔离
账号环境不写在代码中，Agent 的大脑只认识业务标签，资源层才认识物理 ID。
系统在本地维护一张 DB 字典映射表：任务诉求（如：“我要用美国站一号亚马逊账号”） ➔ Backend 静态 ID（如：`profile_id: "sp_j9sd2kx"`）。 

### 2. Context 携带意图与 Hooks 拦截
Lead Agent 在组装出 Browser Agent 时，不再让 Agent 去自动冷启动 Chrome。而是通过配置 `env_vars={"PROFILE_ID": "sp_j9sd2kx"}` 写死在 `TeammateContext` 中。
引擎 `ExecutionLoop` 启动那一刻就会触发 `session_start` 钩子。由内置的资源管控 Handler 拦截到该意图，转交给内部的 `ResourceManager`。

### 3. ShadowPilot 拉起与 MCP 协议栈无缝交接
`ResourceManager` 通过 HTTP Local API 唤醒 ShadowPilot。浏览器引擎在底层自动部署专属静态代理 IP，并挂载指定屏幕/字体/显卡等硬件指纹。
启动成功后，ShadowPilot 内部自带的 **Dispatch 模块** 将暴露一个 WebSocket 端口兼作 MCP Server。`BrowserAgent` 便以 MCP Client 的身份，通过该端口（`ws_endpoint`）接入。后续由大模型制定的所有 Web 操作意图（`navigate`、`click`）均以标准的 MCP `CallTool` 请求越过边界，由浏览器那头的 Dispatch 模块本地解析执行并返回安全序列化好的 DOM JSON/文本。在生命周期结束（`session_end`）后断开重连握手，并通知后台回收浏览器进程。

---

## 六、 V4 推荐微服务模块规范区划

针对高内聚、低耦合架构，重构后的核心文件区划如下：

```text
browser_agent_system_v3/
├── core/                       # (司令塔台 - 严控流程生命与上下文循环)
│   ├── execution_loop.py       # 【核心驱动】纯函数生成器大循环，不带状态的纯净发动机
│   ├── teammate_context.py     # 【记忆载体】会话短记忆与长偏好数据体（解耦状态）
│   ├── agent_definition.py     # 【配置模板】声明各 Agent 身份档（白名单/系统提示词/权限）
│   ├── agent_spawner.py        # 【孵化路由】统一维护 Agent 注册表并组装 Context
│   ├── prompt_builder.py       # 【指令工厂】Cache静态装配与动态边界划分，防御提示词注入
│   ├── context_compactor.py    # 【瘦身中间件】两级管道拦截防 Token 爆炸
│   ├── hook_registry.py        # 【生命拦截】5大挂载点事件总线系统
│   └── profiler.py             # 【量化诊断】Checkpoint 性能测速打点
│
├── permissions/                # (防护装甲层 - L2/L3 拦截拦截哨)
│   ├── input_sanitizer.py      # L3 防穿越与注入（挂载于 pre_tool_execute）
│   └── denial_tracker.py       # L2 断路器，权限连续失败自动熔断
│
├── toolkits/                   # (战术武器库 - 受限制的物理手臂)
│   ├── base_tool.py            # 工具契约声明（强制标明破坏性与 max_result_chars 限制）
│   ├── tool_registry.py        # 工具池化路由集合与排期防抖机制
│   ├── browser_sandbox_tools.py # 降级输出版的新生网页操控模组
│   └── data_analysis_tools.py  # Pandas 与代码执行区（隔离在 WorkTree 执行的危险品）
│
└── tests/                      # (对抗材质试验场)
    └── v4_verification_suite/  # 完整的落盘截断、沙箱出逃、递归派生等自动化抗压脚本
```
