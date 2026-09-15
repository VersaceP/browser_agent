# L3 review 与 waitEvent 复测状态

日期：2026-09-12。基线：HEAD `d8223af` 加当前工作区未提交改动。
初始轮次只审查和探测；随后经确认修复了下列三个 P2，未修改原始问题报告。

## 结论

L3 主路径及相关测试现为 58 个通过。三个 P2 已修复，真实 provider canary 仍未执行。
证据来自真实 BrowserAgent 主循环配合模拟 browser/provider 的离线探针，
不是当前部署模型的真实 canary，也不证明真实 provider 已出现这些错误。

waitEvent 真机复测受环境阻塞，执行用例数为 0；不能给出“已修复”或“仍可复现”的结论。

## 修复落实（2026-09-12）

1. `agent_harness.py` 将图片过期处理移到所有成功模型响应的公共路径。纯文本完成、
   截断恢复和工具调用都会消费一次图片；连接、超时和其它未完成请求不消费图片。
2. 待消费图片所在的 assistant/tool-result 对会保留在压缩范围之外；其前置文本历史仍按
   阈值或强制原因压缩。图片不会送进文本 compactor，工具配对也不会被重组破坏。
3. `llm/content_moderation.py` 增加了仅在请求实际附图时才生效的、明确图片协议拒绝识别。
   命中后撤下图片并保留文字回执，最多重试一次；泛化的 schema/参数 400 不会被吞掉。
   `harness/offload.py` 同时验证 PNG/JPEG/GIF/WebP 文件签名，拒绝伪装扩展名的内容。

离线主循环探针复跑结果：纯文本完成和截断恢复的最终上下文图片数均从 1 降到 0；
截断恢复的请求图片序列从 `[0, 1, 1]` 改为 `[0, 1, 0]`；模拟协议拒图改为一次撤图后
成功重试。强制压缩原因在附图回合会传给前置历史压缩器。

## Review findings（修复前记录）

### 1. [P2] 清理位置漏掉纯文本结束及截断重试

位置：`agent_harness.py:1855`，关联 `1845–1851`、`finally` 的 context snapshot。

`_expire_multimodal_image_blocks()` 位于 `if not tool_calls` 分支之后。
合法的纯文本回答在该分支里直接 break，截断回答的恢复路径直接 continue，均到不了清理点。

探针结果：

- 看图后调用 final_answer 工具：请求图片数 `[0,1]`，最终上下文图片数 0。
- 看图后以纯文本结束：请求图片数 `[0,1]`，最终上下文图片数 1。
- 看图后返回截断文本，再纯文本结束：请求图片数 `[0,1,1]`，最终上下文图片数 1。

因此“一次成功模型观察后过期”没有覆盖所有响应形态；截断回合重复发送图片，
纯文本完成及失败退出会让 base64 写进 `contexts/*final-context.json`。
这不是截图 artifact 本身的保存，而是本应临时存在的消息像素额外进入了上下文持久化。

建议：把已消费附件的清理移到所有成功响应共用的路径，明确截断/空响应是否消费图片；
持久化上下文时另做无像素投影，覆盖异常及取消退出。保留文本回执和图片未消费的事实。

### 2. [P2] 有待发送图片时，无条件绕过上下文压缩

位置：`agent_harness.py:1440–1463`。

`pending_images > 0` 时不运行压缩/阈值检查，已有 `force_reason` 也仅推迟。
如果上一轮工具文字回执已把上下文推过窗口，或强制压缩已经必要，下一次模型请求仍会
携带完整历史和图片发出。超限异常最终走 worker 的 `context_limit_exceeded` 终止路径。
单张图片的 4 MiB 文件上限并不能限制文字历史或整个请求的 token 数。

探针在截图回执后设置强制压缩原因，并由模拟 provider 对仍未处理的原因返回
`maximum context length exceeded`：第二步没有调用 compactor，worker 终态为
`context_limit_exceeded`。该用例证明控制流与恢复缺口；没有测真实模型的 token 阈值。

建议：把待发送图片临时保留在压缩输入之外，对文本历史正常预算/压缩，然后将图片
按原 tool_use_id 重新附回受保护的近期回执。校验工具配对完整性，不能靠绕过所有压缩
来避免把 base64 送进文本 compactor。

### 3. [P2] 图片协议拒绝未覆盖撤图恢复

位置：`harness/offload.py:783–789`（附件生成）；
调用链：`agent_harness.py:660–664` → `llm/content_moderation.py:45`。

附件生成仅通过后缀/MIME 推断图片格式，没有验证内容可解码。
探针将 21 字节普通文本写成 `.png`，函数仍返回 `attached: true` 和 image block。
若 provider 拒绝图片数据、尺寸或不支持图像内容，只有命中 moderation 标记的错误会撤图。
模拟 HTTP 400 `invalid_image` 时，第一次附图请求直接向外抛异常；没有发生保留文字后重试。

所以文档中“若 provider 拒绝含图片的请求，moderation fallback 会只撤下图片”的范围
写得过宽，现有逻辑仅覆盖特定内容审核拒绝。

建议：将明确的图片格式/能力协议拒绝与 moderation 区分，有限次撤图重试并返回原因；
通用的参数错误不能一概吞掉。若增加附件可解码检查，它属于与站点和业务意图无关的
协议有效性校验：会排除截断/不支持的图片，恢复路径是保留文字回执、重新截图或调整
格式。不要扩展成站点、字段或视觉内容判断门禁。

## 验证与产物

现有测试：

```text
conda run --no-capture-output -n agent python -m pytest -q \
  tests/test_multimodal_screenshots.py
10 passed

conda run --no-capture-output -n agent python -m pytest -q \
  tests/test_openai_provider_streaming.py tests/test_input_moderation_recovery.py \
  tests/test_context_compaction.py tests/test_compaction.py tests/test_captcha_autosolve.py
48 passed
```

新增离线探针：`scratchpad/review_l3_probe.py`。
结果：`scratchpad/review_l3_probe_results.json`。

```text
conda run --no-capture-output -n agent python scratchpad/review_l3_probe.py
```

探针替换浏览器动作、模型响应及 compactor 边界，运行实际 BrowserAgent 循环、
附件生成、异常分派及最终上下文落盘。仅使用临时生成的测试图片，不发送真实截图。
真实 provider 的视觉理解、模型支持与性能仍需独立 canary。

## waitEvent：受阻事实及后续矩阵

当前配置地址 `ws://localhost:9300/ws`：连接被拒绝，无 TCP listener。
已尝试启动本机 WebCross，但端口仍未监听；UI 工具报告 Mac 锁定。
没有获取到当前平台 catalogRevision、实时 Workflow schema 或任何本轮执行回执。
连接状态记录在 `scratchpad/waitevent_reprobe_status.json`。

解锁并启动服务后，需要按实时契约执行：

1. 隔离空白页等待不会主动产生的已注册事件，记录耗时、events、timedOut、step status
   与 workflow status；确保 page scope 精确，避免别的页面事件干扰。
2. 同样等待之后追加只读 Action，以实际 step trace 判断超时后是否继续。
3. 检查实时 schema/guide 是否新增 mandatory/onTimeout 或显式失败步骤；若有，验证
   其超时阻断行为，而不能只凭默认 timeout 仍返回 success 就认定问题整体未修复。
4. 用受控事件到达路径作正对照，并区分单步 timeout 与总 workflow deadline。

还需校正旧报告的结论边界：即使没有超时失败选项，已有 `if` 也可能根据 timedOut
阻止后续业务动作，这与“让整个 workflow 明确失败”是两种能力。不能把二者等同，
也不能把安全跳过但 workflow succeeded 当成完成了“必须等到”的合同。
