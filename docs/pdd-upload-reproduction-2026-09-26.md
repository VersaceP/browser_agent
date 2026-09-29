# 拼多多上传复现与诊断权限建议

日期：2026-09-26。原任务：`e1f69e0118444068b920c8080d4e5cfe`。

## 实测结论

在同一 Fleet 的新建「女装/女士精品 > 汉服 > 汉服套装」表单复现了：原 WebP 的 `File.handleChooser` 返回成功，轮播图计数仍为 0。先点击上传控件再指派同一 WebP，结果仍相同。刷新页面后直接指派已有 JPG，未在新文档中点击上传按钮，上传成功。

| 操作 | 指派回执 | 页面结果 | Network.readApi 结果 |
| --- | --- | --- | --- |
| 原 WebP 直接指派 | handled=true，selectedFileCount=1 | 0 张；全量观察 unchanged | 操作后窗口内 3 条背景响应，未捕获上传接口响应 |
| Input.click 后再指派同一 WebP | 点击反馈列出 image/jpeg、image/png；指派仍成功 | 查询结果 0 张 | 操作后窗口内 2 条背景响应，未捕获上传接口响应 |
| 刷新后直接指派 JPG | handled=true，selectedFileCount=1 | 查询结果 1 张 | POST /v3/store_image，HTTP 200，返回图片 URL、800×800、size=88673 |

实测文件均来自原任务商品目录，未转换或改写：

- WebP：`图片/dom_img_1790148598234_0_00106c55.webp`，实际 WebP 编码，799×1066，47480 字节。
- JPG：`详情图/dom_img_1790148587655_0_7a2d30e3.jpg`，800×800，88504 字节。

控件属性明确为 `accept="image/jpeg,image/png"`。点击后的页面帮助区明确显示「图片格式仅支持JPG,PNG格式」。这两个事实，加上 JPG 对照成功，支持原 WebP 不符合该控件格式要求；不能把这次失败归因于必须额外点击才能上传。

## 证据及边界

- [操作回执、控件属性、页面计数与网络摘要](../reports/pdd-upload-reproduction-2026-09-26/evidence.json)
- [页面格式规则摘录](../reports/pdd-upload-reproduction-2026-09-26/page-rules.txt)
- JPG 上传请求 ID：`40323.333:0`，请求时间 `1790426409329`，耗时 583ms，responseBodyAvailable=true。
- 三次网络读取均未设置 responsePattern；使用相应文件指派前记录的 sinceMs。采集开始时间早于操作，evictedCount=0，pendingCount=0。

Network.readApi 是 Fetch/XHR/Beacon 的受支持响应快照。这里没有捕获到 WebP 上传接口响应，不能单凭这一点证明浏览器绝未尝试请求。也没有捕获到「服务端明确拒绝 WebP」的响应。页面规则、输入框 accept、原生文件指派、页面事件处理和服务端业务响应是不同层面的证据。

WebP 与 JPG 对照使用不同图片，因此不是只改变编码的实验；本轮未测试将原 WebP 转码后的表现。采样 AX 没有给出该次 WebP 的明确拒绝文案。具体是原生赋值阶段过滤，还是页面处理文件时丢弃，仍需原生赋值后状态或事件事实才能进一步区分。

原任务页面已关闭；此次是当前安装版本和同类目页面的复现，不是对原请求的网络回放。本轮创建的测试页面为 `8669ed3b-118a-4199-a374-825a005f3990`，商品表单 ID `202146781352`，goods_id `1010014865394`，留有一张 JPG。未执行提交并上架；站点在成功上传后自动调用了素材草稿接口。

## 运行版本与各层责任

已通过进程路径确认使用 `/Applications/WebCross.app` 及其随包 Client。

- 安装版本：0.9.35-beta，安装包 sourceRevision：`b74fbd72441bb53744dd83c6ed66db2d80b4c798`。
- WebCross 工作区 HEAD：`6924023865e5a1a131ce110bb08974198dbc678d`。两者不同。
- live catalogRevision：`sha256:57787ad534cac0789a4ec5118fb0a773583a9790bab09bbabd5f03df913b3180`。
- 单独检查安装 Client bundle：File 指派通过 abcpHandleFileChooser；selectedFileCount 由传入 files.length 构造。readApi 通过 abcpReadApi，并将 sinceMs 映射为 timestamp。这两段入口行为与当前对应源码一致。

责任区分：

1. 页面：明示轮播图只支持 JPG、PNG，原 WebP 不符合规则。
2. WebCross：指派回执不能证明页面接纳。当前计数来源为请求文件数，没有暴露实际赋值后文件集合、过滤结果或页面拒绝原因。JPG 成功说明当前目标解析和上传执行链路能够工作。
3. Harness：tool_policy.py 禁用了整个 Network 域，form_filling/file_upload 没有 readApi 例外，Worker 因而不能使用已可用的网络诊断能力。file-upload.md 与 page-lifecycle.md 仍以前置点击描述上传，而 live contract 允许直接指派实际文件 input。
4. 模型：应把计数仍为 0 当作未确认完成，结合属性、页面规则和可用网络证据选择诊断动作；不应只依据后缀臆造服务端拒绝原因或重复相同指派。

前一轮修复的直派交接缺陷解释了为什么系统提前结束；它与本轮上传未被接纳的原因不同。

## 已批准并实施的 Harness 修改

### Harness 提示词

已修改 browser/file-upload.md 与 browser/page-lifecycle.md，统一遵循 live Action contract：实际文件 input 已知时可直接指派；需要通过可见控件建立目标时点击一次；是否存在后续提交动作由页面证据决定。

已补充通用诊断指导：指派回执只证明工具阶段；页面结果未达成时，根据已有证据选择读 accept/属性、页面错误或 Network.readApi。读取网络时从操作时间窗口开始，首次不预设成功/失败正文匹配。结合业务响应及页面结果判断完成；没有记录、HTTP 200、计数未变分别有不同含义。保留未确认目标交回 Lead。

不增加站点选择器、格式黑名单、固定点击顺序、每次上传必查网络或自动重复上传的机械规则。

### Harness 权限及结果处理

- 已按完整方法名为 form_filling、file_upload 开放只读 Network.readApi，沿用当前 Worker 的 page/Fleet 绑定、租约和显式 forbidden_methods；Cookie 读写、缓存清理、请求拦截仍按原策略管理。
- 已在 ABCPClient 的 Network.readApi 响应进入 transport 日志和模型回执前屏蔽 requestBody、JSON 正文中的凭据字段及 URL 凭据参数。非 JSON 正文会屏蔽。诊断仍可使用 requestId、时间、HTTP 状态、业务错误码/信息和采集覆盖信息。该策略识别结构化凭据字段，不声称识别无标记自由文本内的所有秘密。
- WebCross 工作流会在 Harness 收到结果前保存子动作回执，因此工作流步骤中的 Network.readApi 被拒绝；Agent 可直接调用同一只读方法。这是跨任务通用的日志与权限边界，代价是不能把该读取放入工作流批处理，恢复路径是直接调用。已失去调用归属的迟到响应只记录请求 ID 等诊断元数据，避免原始正文进入传输日志。
- 新增针对性测试验证方法例外、合同禁止、页面越界、凭据不出现在传输日志或模型回执，并保留可解析的业务失败正文。offload 接收的是同一份已处理的模型回执。

### WebCross 后续候选

如果继续改善失败定位，优先让原生文件回执暴露可验证的实际赋值结果，而不再把请求文件数作为唯一计数；不要让 Harness 重试或脚本注入掩盖原生层的不确定性。是否推进这部分需另行核查原生实现。本轮已验证 readApi 能读取 JPG 上传成功的原始响应，无证据支持为本案例重写网络读取入口。

## 本轮改动范围

本轮已修改 Harness 源码和提示词；运行中的 Agent 尚未因工作区修改自动更新。安装版 WebCross 未修改。Python/Shell 沙箱能力留待后续讨论。
