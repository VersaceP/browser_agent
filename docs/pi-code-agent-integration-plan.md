# pi 代码 agent 接入实施计划（Option A）

- 日期：2026-08-08
- 状态：待 review，未落代码
- 关联：`docs/execution-integrity-and-fast-path-plan.md`（Stage 5b/5c 落盘上下文）、`skills/1688-to-pdd-draft/`（宽落盘真实范例）

## 1. 背景与已确认决策

5b/5c 区域的"落盘"在窄义 harness 语义下已解决且严格类型化：`record_extraction`(worker)
与 `lead_save_artifact`(lead) 只接受 `name + rows(list[dict]) + schema`，写单一 JSON 到
`<task_dir>/artifacts/extractions/`。用户要的是更宽的"落盘"——任意格式/位置/媒体/manifest、
`subprocess`（如 `sips` 转图）、代码生成/补丁——这些 harness 今天**零能力**（`agent_harness.py`
+ `harness/` 全仓无 `subprocess/Popen`）。原方案"再造一个通用落盘工具"被否，改为直接上代码 agent。

已确认三项决策：

1. **职责边界**：pi 代码 agent **只补宽落盘**（任意文件写文本+二进制、subprocess、代码生成/补丁）。
   已校验的 rows-JSON 仍走 `record_extraction`/`lead_save_artifact`，两工具分工，最小侵入。
2. **安全姿态**：worktree 受限 + 策略门（见 §5）。unattended 下对无沙箱 bash 取软约束 +
   审计，硬隔离（低权用户/容器）列为 v2。
3. **路径**：Option A——`pi --mode json` 一次性子进程。本机 `/opt/homebrew/bin/pi` v0.84.1
   已装，flag 已 `pi --help` 核实。

## 2. 方案总览

harness 新增一个 `code_agent` 工具（与 `local_fs_search`/`record_extraction` 同级）。
模型调用时，工具在 task worktree 内 `spawn` 一次 `pi`，pi 跑自己的 LLM 工具循环用内置
`bash/read/write/edit` 完成目标，stdout 回 NDJSON 事件流；工具解析事件，做策略门审计与
worktree diff，写 logger 事件 + `agent.trace`，把结果（含 `savedPath` 列表）回喂模型。
每次调用 `--no-session` 无状态隔离。

```
BrowserAgent.run 工具循环
  └─ 模型调 code_agent(goal, context_paths, expected_outputs, constraints)
       └─ @BROWSER_TOOLS.register _browser_code_agent(ctx)
            ├─ 校验 expected_outputs 在 worktree 内 (resolve_task_file 范式)
            ├─ 快照 worktree 文件集 (before)
            ├─ 写 prompt 文件 (/tmp/abcp_code_task_<uuid>.md)
            ├─ subprocess.run: pi --mode json --no-session --no-extensions
            │                  --no-skills --no-context-files
            │                  --tools bash,read,write,edit
            │                  --model ark/ark-code-latest  @prompt.md
            │   cwd=task_dir, env 注入 ARK_API_KEY, wall-clock/step 预算
            ├─ 解析 NDJSON: agent_end / agent_settled / stopReason:error / errorMessage
            │              + 收集 tool_execution_end(bash/write) 做 bash 审计
            ├─ worktree diff (after) -> 实际写入文件列表
            ├─ 策略门: 扫 bash 命令/写入路径, 危险则记 violation(不阻断 one-shot, 见 §5)
            ├─ logger.write(tool.code_agent, {goal, command, savedPaths, violations})
            ├─ agent.trace.append({type:"code_agent", savedPaths, status})
            └─ return {status, savedPaths, summary, violations?}
```

目标命令形态（flag 均 `pi --help` 已核实）：

```bash
pi --mode json --no-session --no-extensions --no-skills --no-context-files \
   --tools bash,read,write,edit \
   --model ark/ark-code-latest \
   @/tmp/abcp_code_task_<uuid>.md
```

> `--mode json` 本身即非交互事件流模式，处理完退出；不需叠加 `-p`。`-p` 是纯文本 print
> 模式，作为更简解析的备选（只拿最终文本，丢结构化错误语义）。

## 3. 接入落点（已核实 file:line）

### 3.1 新工具注册 — `harness/tools/browser_tools/__init__.py`

镜像 `local_fs_search`(L1136) / `record_extraction`(L1094) 注册范式：

```python
@BROWSER_TOOLS.register(
    name="code_agent",
    description=(
        "Run an autonomous code agent (pi) to perform wide-persistence work the"
        " typed record_extraction cannot: write arbitrary text/binary files to any"
        " path inside the task worktree, run ad-hoc scripts (e.g. sips image"
        " conversion), and generate/patch code or manifests. Confined to the"
        " worktree; use record_extraction for validated rows-JSON."
    ),
    input_schema=_browser_schema_for("code_agent"),
    contract_check=True,
    progress_check=True,
    trace_type="code_agent",
)
async def _browser_code_agent(ctx: ToolContext) -> JsonDict:
    return _code_agent(ctx.agent, ctx.tool_input)
```

实现放新模块 `harness/tools/browser_tools/code_agent.py`（避免 `__init__.py` 进一步膨胀），
`_code_agent(agent, tool_input)` 负责校验/快照/spawn/解析/审计/落盘/回喂。

### 3.2 工具 schema — `harness/tools/browser_tools/schemas.py:170`

在 `_browser_input_schemas()` 返回的字典里加 `code_agent`（与 `local_fs_search`/
`record_extraction` 同处）。字段（初稿）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `goal` | string | 是 | 代码/落盘任务的自然语言描述 |
| `context_paths` | array<string> | 否 | 输入文件路径（须在 worktree 内，经 `resolve_task_file` 校验） |
| `expected_outputs` | array<string> | 否 | 预期产出路径（须在 worktree 内，调用前校验） |
| `constraints` | string | 否 | 额外约束（格式/schema/命名） |
| `allow_network` | bool | 否 | 默认 false；true 时放宽 egress 策略门 |
| `timeout_seconds` | int | 否 | 覆盖 config 默认 wall-clock |

### 3.3 task_type 可见性 — `hidden_harness_tools_for_task_type`

`__init__.py:92` 导入 `hidden_harness_tools_for_task_type`。需确认 `code_agent` 是否
应对某些 task_type 隐藏（例如只读 task_type 不暴露写/执行工具）。实施时读该函数定义，
按需把 `code_agent` 加入对应 task_type 的可见集或保持全可见。

### 3.4 dispatcher — 无需改

`build_browser_tool_dispatcher(agent)`（`__init__.py:455`）自动接管所有 `BROWSER_TOOLS`
注册项，`code_agent` 注册即经既有 dispatcher 分派，自动继承 lifecycle/loop-guard/
contract/progress 门。**不要绕开 dispatcher**（会丢门）。

### 3.5 不变量 — 镜像 `save_extraction_artifact` / `lead_save_artifact`

- 写一条 logger 事件：`logger.write("tool.code_agent", {...})`（镜像 `lead_save_artifact`
  的 `event_type="tool.lead_save_artifact"`，`lead_tools.py:1662`）。
- 写一条 `agent.trace.append({"type":"code_agent", "savedPaths":[...], "status":...})`
  （镜像 `record_extraction` 的 trace 范式）。
- 把 `savedPaths` 回喂模型，供下游 `record_extraction`/`final_answer` 引用。
- 路径包含校验：用 `harness/utils.py:690` `resolve_task_file(logger, raw_path)` +
  `lead_tools.py:1666` `_validate_lead_save_sources` 的 `path.relative_to(task_root)`
  范式，强制所有 `context_paths`/`expected_outputs`/实际写入路径留在 worktree 内。

## 4. provider 复用（一次性配置，已核实）

abcp 主链路（`config.json`）：`provider="anthropic"`（协议）、`model_id="ark-code-latest"`、
`base_url="https://ark.cn-beijing.volces.com/api/coding"`、字面量 `api_key`。pi 默认
`anthropic` provider 打 `api.anthropic.com` 会 401，故在 `~/.pi/agent/models.json` 新建
自定义 provider：

```json
{
  "providers": {
    "ark": {
      "baseUrl": "https://ark.cn-beijing.volces.com/api/coding",
      "apiKey": "$ARK_API_KEY",
      "api": "anthropic-messages",
      "models": [
        {
          "id": "ark-code-latest", "name": "ark-code-latest",
          "reasoning": false, "input": ["text", "image"],
          "contextWindow": 200000, "maxTokens": 16384,
          "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        }
      ]
    }
  }
}
```

- 把 `config.json` 字面量 `api_key` 导出为同名 ENV `ARK_API_KEY`（harness 启动 pi 时注入
  `env`，不落盘到 pi 配置）。
- 代理若拒 eager streaming / strict tools / adaptive thinking（调研 D caveat#4），在
  provider 加 compat（`supportsEagerToolInputStreaming:false`、`supportsStrictTools:false`、
  `forceAdaptiveThinking`、`allowEmptySignature` 等），按实际 401/400 调整。
- 鉴权解析顺序：`--api-key` > auth.json > env > models.json apiKey。**别**用 `pi /login`
  存冲突的 auth.json 条目，否则会 shadow 掉共享 ENV。

## 5. 安全：worktree 受限 + 策略门

**诚实前提**：one-shot `pi -p` 的 `bash` 无沙箱、全用户权限，`--tools` 白名单**不沙箱
bash**（bash 可 `cd` 逃逸、可写任意路径）。从外部对 one-shot bash 做硬写 confinement 不可
能——硬隔离只能靠低权用户/容器（v2）。v1 取与手动跑 `skills/1688-to-pdd-draft/scripts/`
同级信任，叠加以下软约束 + 审计：

1. **cwd=task_dir**：`subprocess.run(..., cwd=task_dir)`，pi 默认在 worktree 内工作。
2. **system prompt 收窄**：prompt 文件明确指示 pi "只允许在 `<task_dir>` 内读写；不得
   `rm -rf`、`sudo`、外发网络（除非 allow_network）、写 worktree 外路径"。
3. **预校验 expected_outputs**：调用前用 `resolve_task_file` 校验所有 `expected_outputs`
   在 worktree 内，越界直接 `rejected` 不启动 pi。
4. **worktree diff 审计**：调用前后快照 worktree 文件集，diff 出实际新增/修改文件；越界
   写入（diff 外的路径无法靠 diff 抓，但能抓 worktree 内的产出）记 `violation`。
5. **bash 命令审计**：解析 NDJSON `tool_execution_end`（toolName=bash）收集所有 bash 命令，
   扫 deny-list（`rm -rf /`、`sudo`、`curl`/`wget` 外发、`>` 重定向到 worktree 外绝对路径），
   命中记 `violation` 写入 logger 事件。one-shot 模式下不阻断已发生的命令，仅事后审计 +
   可配置 `fail_on_violation` 把含 violation 的结果标记 `status:"violated"` 不回喂 savedPaths。
6. **goal deny-list**：对 `goal` 文本做轻量危险词扫描，明显破坏性 goal 直接 `rejected`。
7. **预算**：wall-clock timeout（config 默认 120s）+ pi 内部步数上限（prompt 里限定
   "最多 N 次工具调用"），防 pi 循环失控。

**v2 硬隔离（out of scope）**：专用低权系统用户或容器跑 pi，`task_dir` 设为该用户唯一
可写目录。当 unattended 暴露面扩大时启用。

## 6. config 块 — `runtime_config.py`

镜像 `PlanValidatorConfig`(L189) 加 `CodeAgentConfig`：

```python
@dataclass
class CodeAgentConfig:
    enabled: bool = False                 # 总开关，默认关
    provider: str = "ark"
    model_id: str = "ark-code-latest"
    api_key_env: str = "ARK_API_KEY"      # ENV 名（不存字面量）
    tools: Tuple[str, ...] = ("bash", "read", "write", "edit")
    timeout_seconds: int = 120
    max_steps: int = 20
    fail_on_violation: bool = True
    allow_network_default: bool = False
    # from_dict(...) 镜像 PlanValidatorConfig.from_dict
```

- 在 `RuntimeConfig`(L1101) 加字段 `code_agent: CodeAgentConfig = field(default_factory=CodeAgentConfig)`。
- 把 `"code_agent"` 加入 audit 白名单（`runtime_config.py` ~L1120，与 `"vl"`/`"plan_validator"`
  同列），否则 `audit_config_keys` 会报未知键。
- `config.json` 加 `"code_agent": {...}` 段（默认 `enabled:false`，显式开启）。
- `ModelConfig`(L98) 已有 provider/base_url/api_key 模型，`CodeAgentConfig` 可复用其
  解析或独立（倾向独立，因 pi 侧用 models.json 而非 abcp 的 client）。

## 7. 错误语义与预算

- NDJSON 解析：首行 `{"type":"session",...}`，关注 `agent_end`(messages + willRetry)、
  `agent_settled`（真正停）、`message_end`(最终 assistant 文本)、`stopReason:"error"` +
  `errorMessage`。**不能只看退出码**——pi 内部工具失败变 `isError` 由 pi 自循环处理，
  退出码 0 不代表子任务成功。
- 区分三类结果：`success`（有 savedPaths 且无 violation）/ `violated`（有 violation，
  按 `fail_on_violation` 决定是否回喂 savedPaths）/ `failed`（pi 报 error 或超时）。
- 超时：`subprocess.run(timeout=...)` 触发 `TimeoutExpired` -> `status:"timeout"`，
  记已写入文件（diff）。
- token/步数：pi 自报 usage 在 `agent_end.messages` 的 `usage`；harness 侧记入 logger
  事件做成本归集，但不硬限（硬限靠 prompt 里的步数指示 + wall-clock）。

## 8. 实施步骤（顺序）

1. **provider smoke test**（先验证 pi 能打通 ark）：
   `ARK_API_KEY=... pi --mode json --no-session --no-extensions --no-skills \
   --no-context-files --tools bash,read,write,edit --model ark/ark-code-latest \
   "write hello world to /tmp/pi_smoke.txt then read it back"` + `models.json`。
   确认 NDJSON 正常、无 401/compat 报错。失败则调 compat flag。
2. `harness/tools/browser_tools/code_agent.py`：实现 `_code_agent(agent, tool_input)`
   ——校验、快照、spawn、解析、审计、落盘、回喂。
3. `harness/tools/browser_tools/schemas.py:170`：加 `code_agent` schema。
4. `harness/tools/browser_tools/__init__.py`：`@BROWSER_TOOLS.register` + import；确认
   `hidden_harness_tools_for_task_type` 可见性。
5. `runtime_config.py`：`CodeAgentConfig` + `RuntimeConfig.code_agent` + audit 白名单。
6. `config.json`：`"code_agent": {"enabled": true, ...}` 段。
7. 回归：跑既有测试套件确认无破坏（`pytest`，注意用 pytest 而非 unittest discover，
   见记忆 `pytest-vs-unittest-runner-gap`）。
8. live smoke：一个 `code_agent` 工具调用——让 pi 生成一个 `product_manifest.json` +
   `sips` 转一张图，确认 savedPaths 回喂、worktree diff 正确、trace/logger 事件落地。

## 9. 风险与回退

| 风险 | 缓解 | 回退 |
|---|---|---|
| 安全（最高）：unattended 下 bash 无沙箱全权限 | §5 软约束+审计；v2 低权用户/容器 | `enabled:false` 总开关，默认不启用 |
| provider 复用失败（ark 代理 compat） | §4 compat flag 调整 | 为 code_agent 单独配一个更便宜/兼容的模型 |
| 双重 LLM 成本 | 紧收 prompt + 步数上限 + wall-clock | 低频场景成本可控；高频再评估 Option B |
| NDJSON 解析契约漂移（pi 升级） | 解析容错（只取关键字段，未知事件忽略） | 锁 pi 版本 0.84.1 |
| pi 冷启动延迟（~1-3s/次） | 可接受（每轮仅几次落盘） | 若高频再上 Option B 长驻 |

## 10. 明确不做（v1）

- 不起 `pi --mode rpc` 长驻服务（协议未文档化，收益边际）。
- 不写 TS extension 桥接（不倒置编排，保住 abcp 门/观测/快路径投资）。
- 不嵌入 `pi-agent-core` 自建 Node host（不重新发明 `pi -p` 已提供的）。
- 不接管 `record_extraction`（validated rows-JSON 路径不变）。
- 不上容器/低权用户硬隔离（v2）。
- 不在 harness 通用层新增站点/字段硬编码（遵守通用性铁律）。

## 11. 验收

- [ ] provider smoke：pi 经 ark 代理成功跑一次 bash+write 循环，NDJSON 含 `agent_settled`。
- [ ] `code_agent` 工具被 dispatcher 识别，模型可调用，contract/progress 门生效。
- [ ] 一次调用写出 worktree 内的 `product_manifest.json` + 转一张图，`savedPaths` 正确回喂。
- [ ] 越界 `expected_outputs` 被 `rejected` 不启动 pi。
- [ ] violation 场景（goal 含 `rm -rf`）被 deny-list 拦或记 violation 且 `fail_on_violation` 生效。
- [ ] logger 事件 + `agent.trace` 落地，既有回归全绿。
