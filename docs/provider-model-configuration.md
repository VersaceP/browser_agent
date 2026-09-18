# 供应商与模型配置指南

本文件是 Harness 的供应商、协议、模型连接及生成参数的统一配置入口。内容按当前
`runtime_config.py` 与 `llm/` 实现核对，更新于 2026-09-18。服务端是否开放某个模型、
图片或推理能力，仍取决于具体端点与账号；下面的端点表表示代码内置配置，不表示全部能力已验证。

## 1. 最小配置与启动方式

默认读取项目根目录 `config.json`，也可用 `--config` 指定文件。`lead`、`worker`、
`plan_validator` 三段**必须各自完整配置**，互不继承；顶层不再放任何模型字段（写了会直接报错）。
将下面的模型连接部分合并进现有配置，保留 `browser`、`harness` 等运行设置：

```json
{
  "lead": {
    "provider": "juao",
    "api": "anthropic-messages",
    "model_id": "YOUR_LEAD_MODEL_ID",
    "api_key_env": "JUAO_API_KEY",
    "llm_api_timeout_seconds": 180,
    "llm_timeout_max_retries": 1,
    "extra_params": {"max_tokens": 4096, "tool_choice": "auto", "cache_control_mode": "off"}
  },
  "worker": {
    "provider": "juao",
    "api": "anthropic-messages",
    "model_id": "YOUR_WORKER_MODEL_ID",
    "api_key_env": "JUAO_API_KEY",
    "llm_api_timeout_seconds": 180,
    "llm_timeout_max_retries": 1,
    "extra_params": {"max_tokens": 4096, "tool_choice": "auto", "cache_control_mode": "off"}
  },
  "plan_validator": {
    "provider": "qwen-token-plan",
    "api": "openai-chat-completions",
    "model_id": "YOUR_VALIDATOR_MODEL_ID",
    "api_key_env": "QWEN_TOKEN_PLAN_API_KEY",
    "max_tokens": 8000
  }
}
```

替换准确的模型 ID，在启动进程的环境中设置示例用到的密钥环境变量，然后运行：

```sh
python main.py --config ./config.json --task "你的任务"
```

此示例用于说明配置结构；4096 不是所有模型或浏览任务的推荐额度。初次接入先验证
文本与工具调用，再增加模型已支持的推理、缓存或图片选项。修改配置后需要重新启动 Harness。

JSON 不进行 `${VAR}` 插值，不自动加载 `.env`，也没有 `model_id_env` 字段。
`api_key_env` 填环境变量名称，`model_id` 直接填模型 ID。不要把密钥写进文档或提交到仓库。

## 2. 供应商与协议

`provider` 选择服务，`api` 选择请求协议，两者相互独立。命名供应商必须明确指定 `api`：

| api | 简写 | SDK 使用的请求路径 |
|---|---|---|
| `openai-chat-completions` | `openai` | 在 base_url 后追加 `/chat/completions` |
| `openai-responses` | 显式指定 | 在 base_url 后追加 `/responses`；官方基址为 `https://api.openai.com/v1` |
| `anthropic-messages` | `anthropic` | 在 base_url 后追加 `/v1/messages` |

当前没有实现 Responses 或其他协议编码器。供应商支持其中一种协议，不代表它的每个模型
都支持该协议的所有字段。模型 ID 使用该服务实际开放的准确名称，不能用展示名称代替。

内置端点来自 [llm/profiles.py](../llm/profiles.py)：

| provider | OpenAI 格式 base_url | Anthropic 格式 base_url |
|---|---|---|
| `juao` | `https://ai.juaotoken.com/v1` | `https://ai.juaotoken.com` |
| `allinone` | `https://api.juhenextvip.com/v1` | `https://api.juhenextvip.com//v1/responses` | `https://api.juhenextvip.com` |
| `qwen-token-plan` | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/` | `https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic` |
| `volcengine-agent-plan` | `https://ark.cn-beijing.volces.com/api/plan/v3` | `https://ark.cn-beijing.volces.com/api/plan` |
| `volcengine-coding-plan` | `https://ark.cn-beijing.volces.com/api/coding/v3` | `https://ark.cn-beijing.volces.com/api/coding` |
| `deepseek` | `https://api.deepseek.com`（Responses 同址） | `https://api.deepseek.com/anthropic` |

这些服务可以省略 `base_url`。Agent Plan 与 Coding Plan 是两个独立配置，不要混用端点和凭据。
Allinone 的部署地址与文档站不是同一地址；当前实现接受自定义部署根地址或其 `/v1`
地址，并按所选协议调整 SDK 基址。其他自定义服务需要显式提供正确的 `base_url`。
不要把完整 `/chat/completions` 或 `/v1/messages` 请求路径填写为 SDK 基址。

旧写法 `provider: "openai"` 或 `"anthropic"` 仍兼容，可省略 `api`。
它们可以连接原生服务，也可以搭配自定义 `base_url`。完整匹配内置端点时，会选用该服务的
扩展映射；不根据模型名猜测供应商能力。新配置优先使用明确的供应商名和协议。

### DeepSeek 官方（`deepseek`）

依据 [DeepSeek API 文档](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)（2026-09-18 核对）：

- 模型 ID 只有 `deepseek-flash`（DeepSeek-V4.1-Flash）和 `deepseek-v4-pro`。旧名
  `deepseek-v4-flash` 仍可调用并由 Flash 承接；展示名 `DeepSeek-V4.1-Flash` 会返回 400。
  `deepseek-v4-pro` 不支持图片输入，Browser Worker 的截图对它无效。
- 思考模式默认开启、强度默认 high。Chat 格式用 `thinking`（走 extra_body）加
  `reasoning_effort`（none / low / high / max）；Anthropic 格式用 `thinking` 加
  `output_config.effort`，`budget_tokens` 被忽略。思考模式下 temperature 不生效。
- Chat 格式在思考模式下不支持 `tool_choice: required` 或指定工具。
- 请求带工具时，历史推理必须完整回传：Chat 格式回传 `reasoning_content`；Responses 格式
  只接受明文 `content` 的 reasoning 项，不支持 `summary` 与 `encrypted_content`。
- 上下文缓存自动管理，`cache_control` 与 Responses 的 `prompt_cache_key` 都被忽略。Chat
  用量里 `prompt_tokens` 含命中部分，`prompt_tokens_details.cached_tokens` 为命中数。
- Anthropic 格式忽略 tool_result 的 `is_error`。
- 旧写法 `provider: "anthropic"` 加 `base_url: "https://api.deepseek.com/anthropic"` 会自动识别为
  `deepseek`，新配置请直接写 `provider: "deepseek"`。

## 3. 连接字段与角色

### 通用连接字段

| 字段 | 读取规则 |
|---|---|
| `provider` | 必填，供应商名（如 `allinone`、`juao`、`qwen-token-plan`、`deepseek`；`anthropic` / `openai` 表示官方服务） |
| `api` | 请求协议，`openai-chat-completions`、`openai-responses` 或 `anthropic-messages`；命名供应商必填 |
| `model_id` | 必填，该服务准确的模型 ID |
| `api_key` / `api_key_env` | 二选一必填；非空直接值优先，否则读取指定环境变量（变量未设置会在启动时报错） |
| `base_url` / `base_url_env` | 非空直接值优先，否则读取指定环境变量；命名供应商可使用内置端点 |
| `extra_params` | 生成设置与协议扩展；字段位置及限制见后文 |
| `llm_api_timeout_seconds` | 连接或流式空闲等待时限，不是整个任务的总时限 |
| `llm_timeout_max_retries` | 模型请求异常的重试预算；排查时可临时设为 0 |
| `llm_timeout_backoff_seconds` | 重试退避设置 |
| `llm_timeout_retry_interval_seconds` | 可选的重试间隔设置 |

推荐使用显式 `api_key_env`，不要依赖 SDK 的环境变量兜底。尤其命名供应商不应借用另一个
服务的通用密钥环境变量。`vl` 的例外见下表。

### 各角色配置位置

| 配置段 | 连接方式 | 输出预算位置与注意事项 |
|---|---|---|
| `lead` | 独立连接，必须完整配置 | `lead.extra_params.max_tokens` |
| `worker` | 独立连接，必须完整配置，供 BrowserAgent 使用 | `worker.extra_params.max_tokens` |
| `plan_validator` | 独立连接，必须配置且必须开启（`enabled` 不写或写 `true`） | `plan_validator.max_tokens`；默认 8000，覆盖其 extra_params 中同名值 |
| `claim_extractor` | 独立配置；未启用单独配置时，调用方可从 plan_validator 派生 | 段内 `max_tokens`；派生默认 16000，不继承审查器的输出预算 |
| `task_classifier` | 设置 model_id 时用独立连接；未设置时回退到 Worker 连接 | 段内 `max_tokens`，默认 4096；移除部分推理控制，固定温度 0，超时至多 20 秒且不重试 |
| `vl` | 独立连接 | `vl.extra_params.max_tokens`；普通视觉请求未指定时使用 800。连接只支持直接 `api_key` / `base_url`，不支持它们的 `_env` 字段 |

提取和分类模型仍用 `enabled` 控制是否启用，并需填写自己的连接和模型。
显式配置的 plan_validator、claim_extractor 模型 ID 还需与 Lead 不同，满足当前独立审查约束。

三段互不继承：每段的 `extra_params` 写什么就发什么，没有任何合并。某个角色要关掉
某个参数，直接不写即可；不要写 `thinking: {"type": "disabled"}` 去"抵消"，那样仍会把
`thinking` 字段发给服务端。

角色分配示例（Lead 与 Worker 用不同供应商）：

```json
{
  "lead": {
    "provider": "juao",
    "api": "anthropic-messages",
    "model_id": "YOUR_LEAD_MODEL_ID",
    "api_key_env": "JUAO_API_KEY",
    "extra_params": {"max_tokens": 16000, "tool_choice": "auto"}
  },
  "worker": {
    "provider": "qwen-token-plan",
    "api": "openai-chat-completions",
    "model_id": "YOUR_QWEN_MODEL_ID",
    "api_key_env": "QWEN_TOKEN_PLAN_API_KEY",
    "extra_params": {
      "max_tokens": 16000,
      "tool_choice": "auto",
      "cache_control_mode": "off"
    }
  },
  "plan_validator": {
    "enabled": true,
    "provider": "allinone",
    "api": "openai-chat-completions",
    "model_id": "YOUR_VALIDATOR_MODEL_ID",
    "api_key_env": "ALLINONE_API_KEY",
    "max_tokens": 8000,
    "extra_params": {"cache_control_mode": "off"}
  }
}
```

计划审查器、信息提取器和分类器构建请求时会强制使用 `tool_choice: required`，
不能靠其 extra_params 中的 `auto` 覆盖。为这些角色选择支持此工具选择方式的模型模式；
不要直接复制仅支持 thinking + auto 的 Worker 配置。

## 4. 生成参数

### 输出上限与上下文窗口

Lead/Worker 在 `extra_params.max_tokens` 配置原生输出上限；也支持相应 Chat 模型的
`max_completion_tokens`。不要同时填多个预算字段。两者均未提供时，适配器默认发送
`max_tokens: 4096`。不同服务的预算含义和允许范围，以目标模型为准。

`max_tokens` 不控制已积累消息的长度。输入上下文超限需要检查历史、截图编码及上下文投影；
输出停止原因 `max_tokens` 则表示本轮生成被截断，可能只有推理、尚未产生工具调用。
增加重试次数不能保证缓解预算不足。

针对 Juao / deepseek-v4.1-flash / Anthropic、thinking enabled、effort max 的现有
失败上下文，2026-09-17 做过每档三次真实回放：

| 输出上限 | 截断次数 | 实际输出 tokens 范围 |
|---|---|---|
| 8000 | 1/3 | 174–8000 |
| 16000 | 0/3 | 3819–9760 |
| 32000 | 0/3 | 1241–13239 |

该组合可先用 **32000** 留出余量；16000 可作较紧的试运行预算。这只是一个约 82,642
input tokens 上下文的小样本，不是通用最佳值，也不保证完整任务成功。两次 16000
响应存在独立的工具参数错误，不能归因于预算。测试没有执行返回的浏览器动作。
上限不是固定消耗，但更大的上限允许更长、更慢的推理。

SDK 调用者还可使用中立 `GenerationOptions.total_output_tokens` / `answer_tokens`，
分别表达总输出与仅回答预算；它们不是 config.json 的顶层字段。
`extra_params.max_tokens_semantics` 可声明原生上限为 `total` 或 `answer`，不负责调整
上下文窗口。具体编码规则见 [llm/adapters.py](../llm/adapters.py)。

### OpenAI Responses API

思考开关不决定请求协议。使用 Responses 必须显式设置 `"api": "openai-responses"`；旧 `provider: openai` 未指定 api 时仍走 Chat Completions，避免破坏兼容端点。

```json
{
  "provider": "openai",
  "api": "openai-responses",
  "model_id": "你的模型 ID",
  "api_key_env": "OPENAI_API_KEY",
  "base_url": "https://api.openai.com/v1",
  "extra_params": {
    "reasoning_effort": "high",
    "max_output_tokens": 16384
  }
}
```

Lead、worker、plan_validator 和 VL 各自配置协议。服务端必须实际支持 `/responses`；不会自动降级到 Chat Completions。Allinone 可沿用其配置基址，其他兼容供应商请明确填写 Responses 基址，不推测套餐端点支持情况。

Responses 将 effort 编码为 `reasoning.effort`，显式关闭 thinking 编码为 `reasoning.effort: none`（支持档位由模型决定）。`thinking: enabled` 本身不指定档位，使用服务端默认值；不发送 Chat 专用的 `extra_body.thinking`。`max_output_tokens` 包含推理和答案 token；不支持独立 `answer_tokens` 或 thinking token budget。

实现使用无状态完整历史（`store: false`），请求 `reasoning.encrypted_content` 并在工具回合中原样回传。工具使用 `function_call` / `function_call_output` 和 `call_id` 配对。流式中断不会执行未完成的工具参数；重试和非流式兜底仍使用 Responses。

### Thinking、reasoning_effort 与工具选择

这些字段都位于相应角色的 `extra_params`：

| 字段 | Harness 行为 |
|---|---|
| `thinking` | 开关或供应商对象。布尔/字符串简写会规范化；对象保留供应商字段 |
| `reasoning_effort` | 接受 `none/minimal/low/medium/high/xhigh/max`，但不代表目标服务全部支持 |
| `effort` | 简写别名；有效的 reasoning_effort 优先 |
| `tool_choice` | `auto/none/required` 或协议支持的指定工具形式；控制动作选择，不等同推理强度 |

明确关闭 thinking 时，与之冲突的开启型 effort 会被忽略。未填写推理参数不等于关闭服务端
默认推理。`thinking: true` 在 Anthropic 路径会自动生成 budget_tokens；显式对象
`{"type":"enabled"}` 不自动增加该预算，不能把两种写法视为完全相同。

| 请求协议/服务 | 编码映射 |
|---|---|
| 一般 OpenAI Chat | effort → reasoning_effort；thinking → extra_body.thinking |
| Qwen Token Plan Chat | thinking → enable_thinking；budget_tokens → thinking_budget；effort 通过扩展请求体传递 |
| Anthropic Messages | thinking → 原生 thinking；开启型 effort → output_config.effort |

编码成功或服务返回 200 不证明推理强度实际生效。先核对目标协议，再用固定任务比较
实际输出与工具结果，不依据模型名字机械转换能力。

Lead/Worker 未配置工具选择时，角色模型配置默认补 `required`。Juao 的上述 thinking
组合曾实测拒绝 required，因此应显式配置 `auto`：

```json
{
  "worker": {
    "extra_params": {
      "max_tokens": 32000,
      "thinking": {"type": "enabled"},
      "reasoning_effort": "max",
      "tool_choice": "auto"
    }
  }
}
```

auto 允许模型直接回答，不能保证每轮调用工具。这个片段只适用于已验证组合，不应作为
所有供应商的默认配置。普通 VL 视觉核验另有推理处理，不继承全局 vl.extra_params 的推理；
验证码求解可保留该设置并通过 `captcha_solve_extra_params` 覆盖。

### cache_control_mode

此字段只控制 Harness 添加的显式 prompt-cache 标记，不控制服务端所有缓存机制。

| 值 | 当前行为 |
|---|---|
| `auto`（默认） | Anthropic 协议默认添加标记；Chat 仅对代码识别的部分阿里系地址启用 |
| `on` | 尝试添加标记；不保证服务支持，也不保证命中 |
| `off` | 不添加显式标记；供应商仍可能自动缓存 |

旧 `enable_cache_control` 仍兼容，但仅在没有设置 cache_control_mode 时生效。
服务明确拒绝 cache_control 时，provider 有去掉标记后回退的处理；不把所有 400 都当成缓存错误。
实际命中看 `llm.usage` 的 cache_read、cache_creation 和 cache_diagnostics，
不要只看请求是否带有标记。详细判定以 [llm/cache_control.py](../llm/cache_control.py) 为准。

### 图片与额外协议字段

`extra_params.tool_result_image_placement` 是 Harness 编码选项，不会发送给供应商：

- `native`：Anthropic 使用原生嵌套工具结果图片。
- `user`：完整工具结果批次之后，再发送普通 user 图片块及对应 tool_call_id。

Juao、Allinone + Anthropic 默认 user，其他 Anthropic 服务默认 native；Chat 使用普通 user 图片布局。
该设置保留工具结果文本、错误标记和调用 ID，不改变中立消息历史。

两个官方协议都允许工具结果带图：Anthropic 的 `tool_result.content` 可以包含 image 块，
OpenAI Responses 的 `function_call_output.output` 可以包含 `input_image`。但 OpenAI Chat 的
`tool` 消息只接受文本，所以把 Anthropic 协议转接到 GPT 等后端的中转站，可能把嵌套图片连同
base64 一起当正文计 token。Juao（任务 e015e32e）和 Allinone（任务 c6a56d5e：2,184,856 字符的截图
被计为约 148 万 token）都出现过这个问题，因此这两个服务默认 user。新接入的中转站如果在截图后
输入 token 异常暴涨，先把此项设为 `user` 复测，确认后再加入 `llm/profiles.py` 的
`TOOL_RESULT_IMAGE_PLACEMENT`。不应只调整输出上限。是否支持图片仍要按具体模型验证。

上下文估算与编码器读取同一份解析结果（`llm.adapters.tool_result_image_accounting`）。只有表内服务
被显式改回 `native` 时，嵌套图片才按 base64 文本估算（每 1.4 个字符算 1 token，取自上述两次实测）；
其他情况每张图预留 4,096 token。附图前，如果估算的下一次请求超过
`harness.model_context_window_tokens`，这张截图不附像素：`agent.multimodal_screenshot` 记录
`reason=image_exceeds_context_window` 和估算值，给模型的回执也会写明原因。
服务拒绝请求后，Harness 不会去掉图片重试。

`extra_params.extra_body` 用于 SDK 尚未暴露的协议扩展。禁止覆盖 model、messages、system、
tools 及流式传输设置。普通生成项如 temperature、top_p、stop 也必须符合所选模型协议；
Harness 不保证跨服务功能等价，不自动删除工具 schema 中无法识别的约束。

## 5. 接入验证与排错

先确认配置加载、模型 ID、账号通道和协议；再依次验证文本、工具调用、工具结果回传，
最后使用真实角色参数测试推理、缓存、图片和长上下文。单次文本成功不等于 Agent 可用。

仓库现有联网探测命令：

```sh
python -m probe_tests.provider_smoke --config config.json --provider juao --output /tmp/provider-smoke.json
```

`--provider` 可重复指定，支持上表六个命名供应商。探测只发固定提示与本地 echo 回执，
不启动浏览器任务；每个服务测试其登记的全部协议。它固定使用 max_tokens=2048、缓存 off，
Qwen 另关闭 thinking，**不会照搬 Lead/Worker 的实际 extra_params**。
只选取配置中该服务第一份有有效凭据的模型，不逐一验收每个角色；自定义部署的地址处理
也以探测脚本支持范围为准。返回码 0 表示所选检查通过，1 表示失败，2 表示缺少配置。

配置里找不到相应服务时，探测脚本支持以下环境变量前缀：

| provider | 探测兜底前缀 |
|---|---|
| juao | `JUAO` |
| allinone | `ALLINONE` |
| qwen-token-plan | `QWEN_TOKEN_PLAN` |
| volcengine-agent-plan | `ARK_AGENT_PLAN` |
| volcengine-coding-plan | `ARK_CODING_PLAN` |
| deepseek | `DEEPSEEK` |

前缀后接 `_API_KEY`、`_MODEL`，可选 `_BASE_URL`。例如 `JUAO_MODEL` 是**探测脚本兜底**，
不是 Harness 日常运行配置，不能覆盖 config.json 已提供的 model_id。

| 现象 | 优先检查 |
|---|---|
| 命名 provider 缺少 api | 配置加载阶段失败；补明确协议 |
| missing_credentials / 401 / 403 | 当前进程环境、凭据、端点权限与服务错误码 |
| model_not_found / No available channel | 模型 ID、密钥分组、通道是否开放；不要随机换模型猜权限 |
| Thinking mode does not support this tool_choice | 最终角色工具选择与推理组合；区分 Worker 的 auto 和审查器强制 required |
| max_tokens，只有推理无工具 | 输出预算不足或推理持续过长；比较额度与强度，不把它误报成网络空响应 |
| maximum context length，messages 极大 | 历史长度、图片编码、上下文压缩；max_tokens 不是输入长度控制项 |
| 工具参数 schema 错误 | 核对具体字段路径、模型输出与 Harness 声明；不先归因于供应商 |
| cache_control 被明确拒绝 | 关闭显式标记后复测，并检查是否发生 provider 回退 |
| 修改 config 后行为不变 | 是否重启、启动时的 --config、角色覆盖及部署源码版本 |

排错记录保留服务错误码、request/response ID、停止原因和 usage；不记录鉴权头或密钥。
终端的模型截断提示会区分纯推理、无工具和已有工具调用，并显示现有恢复策略将重试还是终止。

## 6. 代码边界与文档维护

调用链为：AgentMessage[] → 上下文投影（压缩、offload、浏览器观察）→ LLMRequest →
适配器编码 → OpenAI/Anthropic SDK → 适配器解码 → LLMResult。
适配器负责协议、调用 ID、块顺序和预算字段转换，不判断业务内容是否可以舍弃。
不透明推理签名不能假定跨模型复用，协议切换后的历史由上下文层处理。

| 实现 | 核对内容 |
|---|---|
| [runtime_config.py](../runtime_config.py) | 配置字段、环境变量及各角色的必填校验 |
| [角色模型配置](../harness/runtime/model_config.py) | Lead/Worker 工具选择默认值 |
| [llm/profiles.py](../llm/profiles.py) | 内置端点、协议识别、图片兼容设置 |
| [llm/adapters.py](../llm/adapters.py) / [llm/contracts.py](../llm/contracts.py) | 中立请求与编码转换 |
| [llm/thinking.py](../llm/thinking.py) / [llm/cache_control.py](../llm/cache_control.py) | 推理映射、缓存策略 |
| [联网探测脚本](../probe_tests/provider_smoke.py) | 文本、工具调用和回传检查 |

配置解释与供应商示例只维护本文件，不再按供应商或日期新建另一份配置指南。
历史原始验证数据归档到 `docs/audit-evidence/provider-model/`，它们不是当前可用性承诺。
保留的 [2026-09-16 协议探测](audit-evidence/provider-model/provider-neutral-smoke-2026-09-16.json)
仅覆盖当时 Qwen、方舟 Agent Plan/Coding Plan 的指定模型与文本/工具链路；Juao 当轮缺少凭据。
新增测试应记录模型、协议、参数、样本量和实际验证范围，避免把历史成功写成供应商全面兼容。
