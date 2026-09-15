# 下载父目录 live canary

脚本：`devtools/download_path_live_canary.py`。这是手动集成测试，不进入默认 pytest，也不调用模型。

在已安装项目 Python 依赖和 `jsonschema` 的环境运行：

```sh
python devtools/download_path_live_canary.py --ws-url ws://127.0.0.1:61168/ws
```

可通过 `--destination-root` 指定待验证的目录，默认 Desktop；通过 `--report-dir` 指定报告目录。每次运行创建随机命名的子目录，不覆盖原文件。脚本在本机启动只提供固定测试内容的 HTTP 服务，并创建独立 Fleet；结束时关闭本次 Fleet 和 HTTP 服务，保留文件及证据。不要指向远程机器上的 WebCross：本测试需要浏览器访问同一主机的 loopback 服务并由脚本检查本机文件。

脚本先注册并动态读取 Action/Event 契约，再校验调用参数。测试使用 JSON-RPC；不读取 config.json 中的模型密钥。成功必须同时满足：完成事件、Download.list 中同一 downloadId 的完成回执及原目标路径、文件内容 SHA-256 一致。RPC 接受不算完成。不自动重试失败下载。

| 场景 | 调用前父目录 |
|---|---|
| existing_parent | 已存在 |
| missing_nested_parent | 多层英文目录不存在 |
| missing_unicode_parent | 中文和空格组成的多层目录不存在 |
| new_precreated_parent | 本次由测试脚本提前创建 |

报告包含：每次调用前后目录和文件存在性、下载响应或原始错误、按 downloadId 对应的终态事件、下载记录、内容哈希、动态契约版本和 Fleet 清理结果。退出码 0 表示全部通过且未出现清理异常，其他结果为 1。

## 2026-09-14 本机复测

端点：`ws://127.0.0.1:61168/ws`。

报告：`reports/download-path-live/3f0d4fdb4ba6470a91a56466cd70c95f/summary.json`，旁边保存 `contracts.json` 和 `notifications.json`。

| 场景 | 结果 |
|---|---|
| existing_parent | PASS，完成通知、完成记录、文件哈希一致 |
| missing_nested_parent | FAIL，download-path-not-allowed，未创建父目录/文件 |
| missing_unicode_parent | FAIL，download-path-not-allowed，未创建父目录/文件 |
| new_precreated_parent | PASS，完成通知、完成记录、文件哈希一致 |

测试 Fleet 已确认关闭。两次成功下载都有 `Download.stateChanged` 的 `currentState=completed` 通知。

结论限定于本次运行的实际链路：允许该测试根目录下的下载，但不会准备缺失的父目录，并将该情形归为路径越权。源码已有 mkdir 逻辑不能证明正在运行的二进制加载了它；本报告的 catalogRevision 是协议契约版本，不是可执行文件的构建版本。下一步应对照运行二进制与构建产物，定位报错校验层，再复跑同一 canary。
