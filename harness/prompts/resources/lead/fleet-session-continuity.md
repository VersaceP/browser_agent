---
id: lead.fleet-session-continuity
audience: lead
version: "2026-09-03"
description: Preserve assigned Fleet, page and session continuity while routing retries, continuations and post-HITL work.
sources:
  - harness/fleet/coordinator.py
  - harness/fleet/task_reuse.py
  - harness/spawner/spawner_worker.py
  - harness/tools/lead_tools.py
emitter_sources:
  - harness/tools/browser_tools/bindings.py
  - harness/spawner/spawner_core.py
related_tools:
  - spawn_browser_agent
  - wait_browser_agents
  - list_browser_agents
related_methods: []
error_codes:
  - invalid_fleet_routing
  - fleet_auth_gated
  - fleet_assignment_required
  - fleet_reperception_required
  - task_session_binding_violation
  - pinned_browser_context_violation
topics:
  - fleet
  - session continuity
  - routing
  - login state
  - page ownership
aliases:
  - 会话
  - 登录态
  - 复用
  - 路由
  - 绑定
---
# Fleet and session continuity

Use this guide when a worker reports page_crashed, a continuation needs an
existing page, a named/authenticated Fleet is involved, or a HITL outcome
changes routing.

Fleet routing is coordinator-owned. A normal task can share a task Fleet while
workers use distinct pages; same-page calls serialize. Do not manufacture a
replacement Fleet because a page crashed, a worker returned partial, or a login
wall appeared. A pinned, named or authenticated Fleet has stronger continuity
requirements than ordinary task routing.

Use reuse_scope=page only when the continuation genuinely needs exposed prior
page candidates, and then require fresh Page.getState/Page.switchTo plus AX
evidence before action. A non-secret session_key denotes one exact reusable
Fleet and is mutually exclusive with an explicit fleet_id. needs_isolated_session
does not imply per-row isolation.

After HITL, follow the structured next_instruction and preserve its required
Fleet/session. Do not escape a blocked or timed-out login by silently planning a
fresh Fleet. A page crash can be recoverable within the same assigned session,
but exact unsaved page-local continuation may be irrecoverably lost; report that
distinction rather than claiming resumed work.
