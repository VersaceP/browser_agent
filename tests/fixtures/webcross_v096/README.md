# WebCross 0.9.6-beta 实时合同样本

2026-10-06 在独立 Fleet 的本地静态测试页采集，所有测试 Fleet 已关闭。
`responses.json` 保留直接 CLI 请求参数和原始回执；artifact.path 仅改为
同目录文件名，其内容、字节数和 SHA-256 保持原样。

包含 full/repeated/changed 观察、四种 live query、混合成功/失败的 snapshot
详情，以及 state/delta/container/edge/target 滚动模式。snapshot 中未存储
完整详情的短字段返回 detail-not-found，长字段返回完整 node。

DOM.getAXTree.json、DOM.getDetails.json、Input.scroll.json 是同一运行实例
的 System.describeAction 回执。native-canary-summary.json 另记录修改后的
ABCPClient、节点监听、Workflow 的 getDetails → $last 文本读取和截图坐标
校验；其中原始 host path 是已关闭页面的历史引用，不能据此读取当前状态。

滚动回执的限制见 docs/webcross-v096-harness-migration.md，样本未将输入
完成、零位移、目标达成或后续页面变化合并成一个结论。
