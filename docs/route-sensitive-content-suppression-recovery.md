# 路由敏感型内容抑制：当前恢复合同

状态：核心实现完成
更新时间：2026-07-30

## 1. 问题定义

路由敏感型内容抑制指：

- 详情 URL 已成功到达；
- 页面 shell、标题或顶层结构存在；
- 任务显式要求的详情区域没有 materialize；
- 从列表页真实链接进入时，内容可能恢复。

这不是默认 CAPTCHA/HITL，也不能仅凭 URL、title 或页面外壳判为
`target_absent`。

## 2. 当前边界

生产实现包含：

- `ContentCompletenessTracker`；
- task/Skill 显式 `content_completeness` contract；
- 有界 reveal、scroll、SemanticTree 与 `collect_items` materialization；
- `FleetClickGate` confirmed 顶层 anchor receipt；
- 新标签 `Page.switchTo` 与同页 `Page.go` 返回路径；
- `final_answer` semantic-terminal veto；
- artifact `validated_done` completeness veto。

不包含：

- Artifact completeness ledger/shadow；
- `ArtifactEvidenceSummary`；
- attempt receipt/counterfactual terminal；
- URL/heading 软评分；
- 多 click 并发归因；
- 淘宝字段或站点特判。

## 3. 配置来源

Content completeness 只能来自：

1. 显式 Lead/worker contract；
2. 已选择 Skill 的声明式页面知识。

Strategy Bank 是纯 guidance，不再生成 `content_completeness`，也不提供域名、
入口 URL、字段 token 或页面 marker。

示例：

```json
{
  "content_completeness": {
    "shell_markers": [
      {"id": "title", "markers": ["task-declared title marker"]}
    ],
    "expected_regions": [
      {
        "id": "task-region",
        "markers": ["task-declared region marker"],
        "fields": ["contractField"]
      }
    ],
    "recovery": {
      "mode": "listing_link_click",
      "max_attempts_per_item": 1
    }
  }
}
```

Marker 必须来自用户 contract、Lead 显式声明或经验证 Skill；Harness 核心不维护
reviews/specifications/description 等词表。

## 4. 状态机

主要状态：

```text
inconclusive
  → 仍有有界 materialization 动作

route_recovery_required
  → direct 页面 shell 存在但必需区域仍缺失
  → 可以尝试真实 listing-link click

content_materialized
  → 必需区域已由页面证据确认

blocked_content_suppression
  → confirmed link-click 路线与有界 materialization 均已耗尽
```

`route_recovery_required` 和 `blocked_content_suppression` 都不能被降级成
`target_absent`。

## 5. Direct 与 listing-link 路线

Direct 路线仍是普通默认路径。只有显式 completeness contract 发现 route-sensitive
shortfall 时，LLM 才根据 structured `routeRecovery` guidance 决定是否尝试
listing-link 路线。

Harness 不自动替 LLM 调用 `final_answer`，但会：

- 拒绝与当前 completeness 状态矛盾的成功/缺失终态；
- 验证 B entry 使用真实、重新绑定的 anchor；
- 在 confirmed B entry 后阻止无意义地退回同一 direct 路线；
- 对恢复与返回动作施加预算。

## 6. 点击与归因

所有 click-capable command 先经过进程内、按 Fleet 的 Enforced
`FleetClickGate`。新标签 confirmed 条件为：

```text
new pageId
AND openedBy == popup
AND openerPageId == sourcePageId
AND sourceUrl compatible
AND unique candidate
AND no conflicting late tombstone
```

同标签只接受 source page URL 的真实变化。其它结果为 `unknown/ambiguous`。
不透明 Workflow 不产生逐 click B-route 证据。

## 7. 返回列表页

- 新标签详情：`Page.switchTo(sourcePageId)`；
- 同页详情：`Page.go(back, n=1)`；
- 返回后必须 `Page.getState + DOM.getAXTree`；
- `Page.navigate(sourceUrl)` 只作为显式 fallback。

`pending_recovery_credit`、`pending_explicit_recovery_sources` 与
`pending_return_pages` 跟踪上述责任，不是旧 pending-click attribution。

## 8. Challenge/HITL

内容抑制本身不：

- 累加 CAPTCHA suspicion；
- 自动请求 HITL；
- 等待 `Hitl.resumed`；
- 把普通缺失区域解释为认证问题。

只有页面真实出现登录、验证码、身份验证或需要人工确认的交互时，才进入
ChallengeTracker/FleetAuthBarrier 路径。

## 9. Artifact 与终态

现有 artifact validator 继续负责：

- required fields；
- field nonempty/pattern/provenance；
- row count/unique/set；
- blocker/placeholder/stub；
- file evidence。

Content completeness 负责页面区域与恢复状态。两者都可能否决错误成功，但没有共享
`ArtifactEvidenceSummary`，也没有 Artifact authoritative consumer。

## 10. Strategy 与 Skill

Strategy Bank：

- 跨站、task/stage 维度；
- 只进入 LLM guidance；
- 可以建议感知、验证、恢复和停止步骤；
- 不能修改权限、contract、validator 或 route state。

Skill guidance：

- site/template 维度；
- 可携带经验证的 marker、selector、入口和 quirks；
- 使用前仍需当前页面探针。

Skill workflow：

- 稳定站点流程；
- 执行时继续受 Fleet gate、auth barrier、artifact validator 与 completeness
  terminal veto 约束。

## 11. 验收

- 非电商任务可通过显式 contract/Skill 使用同一 Tracker；
- 没有显式 completeness contract 的任务不会被 Strategy 词表机械门控；
- confirmed link-click receipt 才能形成 link-click route evidence；
- Workflow receipt 永远不冒充单次 anchor attribution；
- blocked content suppression 不归类为 target absent；
- 返回列表后的 page state/AXTree 必须重新感知；
- Strategy 选择不改变 `allowed_methods/forbidden_methods` 或 worker contract。

## 12. 撤销记录

2026-07-30 已删除：

- Artifact completeness ledger/shadow；
- online/final counterfactual evaluation；
- attempt receipts；
- wouldBlock/wouldTerminal telemetry；
- `ArtifactEvidenceSummary` 权威化方案；
- Strategy 自动注入 content-completeness taxonomy。

未来若重启 Artifact 终态门禁或跨进程导航归因，必须另立设计，不从本历史方案恢复。
