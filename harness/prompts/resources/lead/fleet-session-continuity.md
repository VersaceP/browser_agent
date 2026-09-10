---
id: lead.fleet-session-continuity
audience: lead
version: "2026-09-05"
description: Preserve assigned Fleet, page and session continuity while routing retries, continuations and post-HITL work.
sources:
  - harness/fleet/coordinator.py
  - harness/fleet/task_reuse.py
  - harness/spawner/spawner_worker.py
  - harness/tools/lead_tools.py
emitter_sources:
  - harness/tools/browser_tools/bindings.py
  - harness/spawner/spawner_core.py
  - harness/spawner/spawner_slots.py
  - harness/fleet/coordinator.py
  - harness/constants.py
related_tools:
  - spawn_browser_agent
  - wait_browser_agents
  - list_browser_agents
related_methods: []
error_codes:
  - invalid_fleet_routing
  - fleet_auth_gated
  - fleet_auth_resolver_required
  - fleet_assignment_required
  - fleet_reperception_required
  - task_session_binding_violation
  - pinned_browser_context_violation
  - session_fleet_lost
  - page_continuation_lost
  - fleet_assignment_lost
  - session_transport_unavailable
  - session_manual_reset_required
  - session_slot_busy
  - fleet_owner_unavailable
  - fleet_reference_invalid
  - fleet_reference_not_found
  - fleet_inventory_temporarily_unavailable
  - ambiguous_fleet_reference
  - reuse_fleet_lost
  - reuse_session_conflict
  - session_isolation_conflict
  - fleet_routing_conflict
  - session_binding_conflict
  - fleet_session_conflict
  - released_fleet_conflict
  - task_fleet_limit_reached
topics:
  - fleet
  - session continuity
  - routing
  - login state
  - spawn rejection
  - fleet routing rejection
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

## Routing states that reach the Lead

These arrive on a worker result or a spawn rejection. Every spawn rejection
carries its own `next_instruction`, and that receipt is the authority for what
to do: it names the offending reference, whether the tool ran, and whether a
retry is permitted. This section is the background the receipt cannot carry.

`session_fleet_lost` means the named fleet is gone from the owner inventory and
is terminal until an explicit reset or re-authentication. Mark the auth session
stale and follow the login-recovery flow; never retry or silently rebind the
same session_key.

`page_continuation_lost` means an authoritative Page.list confirmed the exact
page holding unfinished page-local state is gone. That state is unrecoverable.
A fresh page does not resume it, so report the loss rather than claiming
continuity.

`fleet_assignment_lost` means stop the worker and ask the coordinator for a
fresh assignment. Do not keep calling against the lost fleetId.

`fleet_auth_gated` means another worker is already resolving login or a CAPTCHA
on the shared fleet: wait, and do not create a second fleet or continue account
actions. Its `reasonKind=fleet_auth_resolver_required` variant is the opposite
situation - the gate is closed with nobody resolving it. Spawn or continue
exactly one worker on the same fleet/session to refresh Page.getState and
DOM.getAXTree, then call Hitl.requestPause to claim resolution. Waiting without
assigning a resolver never clears it.

`fleet_reperception_required` means shared auth state changed: Page.getState and
DOM.getAXTree before any further action.

## Reading a spawn rejection

Follow the receipt's `next_instruction`. Two of them carry more than the
instruction can say.

`fleet_reference_invalid` wants an existing Fleet UUID, or a hexadecimal prefix
of at least eight characters, in `fleet_id` - never in `session_key`, which is
a different routing concept entirely. Its receipt includes `candidateFleetIds`
from the authoritative inventory; choose one of those exact ids or a unique
prefix, and do not invent a label or replacement Fleet.

`task_fleet_limit_reached` is the one that looks like a waiting problem and is
not. The task already holds `runtime_limits.max_task_fleets` fleets and this
spawn demanded a separate identity (`needs_isolated_session` or a new
session_key), or every task fleet is bound to a named session and none may be
lent to a generic worker. An ordinary fleetless spawn is normally NOT rejected
here - it silently reuses a task fleet. Waiting does not help: the harness never
closes a fleet, so a finished worker still holds its own and a named session
stays bound after its worker ends. Continue on a fleet the task already has,
release a binding through auth recovery, or ask the operator to raise
`harness.max_task_fleets`. A fresh fleet is never the answer.

`session_manual_reset_required` is likewise terminal for the Lead: repeated
owner-socket recovery failed, and a host or operator must restore the transport
or reset that exact fleet/generation. Never release or silently rebind it.
