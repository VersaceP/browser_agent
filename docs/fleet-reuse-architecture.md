# Fleet reuse architecture

## Verified platform constraints

- Fleetless `Page.create` delegates to Dispatcher origin-based selection. It
  does not guarantee reuse of the desired login fleet and can create another
  fleet.
- `fleetId` is usable across connections, but `Fleet.list` discovery is scoped
  to the registered owner agent.
- Archived fleets cannot be silently revived. Fleet inventory, ownership,
  retention, archive, and GC therefore remain Dispatcher responsibilities.
- Page/DOM/Runtime/Input/screenshot calls route by `pageId`. The harness now
  uses this for same-fleet, different-page concurrency while retaining one
  owner socket and an explicit compatibility guard around the current
  Dispatcher behavior.
- The current Dispatcher has one notification socket per `agentId`; multiple
  same-agent sockets can overwrite and then delete that mapping. The harness
  therefore uses distinct acting agent ids and relays owner notifications
  in-process. The formal platform contract is specified in
  `docs/dispatcher-fleet-delegation-contract.md`.

## Implemented boundary (phases 1 and 2)

`FleetCoordinator` owns only ephemeral orchestration metadata:

- worker -> assigned fleet and assignment reason;
- allowed fleet ids for the worker;
- slot default fleet;
- process-local `session_key` affinity;
- last-observed/last-used metadata for deterministic selection.

Before `spawn_browser_agent` returns, the spawner reserves a slot, refreshes its
owner inventory, and either binds an eligible observed fleet or creates one.
Slot capacity is reserved under a short pool lock; slow registration and page
state RPCs run outside that lock. Startup is serialized only among workers that
share the same `session_key`, so independent sessions can prepare concurrently
without reintroducing the first-binding race. A new `session_key` and every
unnamed isolated worker start with a fresh fleet. The only handoff exception is an explicit
`reuse_from_worker_id` whose fleet is not bound to another session key. It never
falls back to fleetless `Page.create`.

At the browser-tool boundary:

- fleetless `Page.create` receives `assignedFleetId` and returns a
  `fleetInjected` receipt;
- an explicit fleet id outside the assignment fails with
  `fleet_binding_violation`;
- missing assignment fails with `fleet_assignment_required`;
- conflicting `session_key`, `reuse_from_worker_id`, or `preferred_slot_id`
  selectors fail with `fleet_routing_conflict` instead of silently choosing one;
- model-initiated `Fleet.create` fails because creation is coordinator-owned;
- model-initiated `Fleet.close` fails because agent-side close clears ownership
  and leaves the fleet prepared/claimable; an accidental close could transfer a
  logged-in session to another agent that knows the fleet id.

Page handles are delegated independently from fleet handles:

- a normal fresh-page worker starts with no allowed historical `pageId`;
- each successful `Page.create` registers only the returned page in the
  worker's delegation, and `Page.close` removes it;
- `reuse_scope=page` may receive existing pages, filtered to the assigned fleet
  and excluding `newtab.html`, `about:blank`, and `chrome://` internal pages;
- any DOM/Runtime/Input/Page/custom-tool call carrying an undelegated `pageId`
  fails with `page_binding_violation` before reaching Dispatcher;
- `Page.list` is unavailable to a fresh-page worker because its unfiltered
  response would reveal prior workers' page handles. For an explicit page
  continuation it returns only pageIds already delegated at spawn time or by
  that worker's successful `Page.create`; listing never expands delegation;
- `Page.create -32005` recovery in coordinator mode probes only page handles
  already delegated to that worker. It never scans fleet inventory and adopts
  another worker's page. Before probing, each candidate must have an explicit
  coordinator page-to-fleet mapping matching the fleet targeted by the failed
  `Page.create`; `Page.getState` verifies page usability but does not report
  fleet ownership.

The workflow skill fast path uses the same explicit assignment. Disabling
`harness.fleet_reuse_enabled` restores the legacy path for rollback.
Skill-heal canaries likewise require an assigned `fleet_id` when they need to
create a page; they no longer create an uncoordinated fleet.

If spawn is cancelled while registration, synchronization, or fleet creation
is in flight, the reserved slot is marked broken and its client is closed before
the cancellation propagates. This avoids an invisible `starting` slot leak and
also prevents a late response from the cancelled connection from satisfying a
later worker call. Cancellation after the worker harness starts instead records
the phase attempt as `cancelled` and returns the healthy slot to idle, so neither
slot state nor task state remains stuck at `running`. Cancellation counts toward
the phase's bounded retry budget but not the cross-replan objective failure
budget, because it is not evidence that the objective is infeasible.

## Reuse contract

- `reuse_scope=connection` (default): fresh page in the slot's assigned fleet;
  prior pages are hidden.
- `reuse_scope=fleet`: fresh page with explicit fleet/session affinity; prior
  pages are hidden.
- `reuse_scope=page`: prior page candidates may be exposed for a related
  continuation. `page_policy=existing` is valid only in this scope.
- `session_key`: a non-secret process-local affinity label. Its first use
  creates a fresh fleet; later uses bind only to that exact fleet. It is not an
  auth ledger and must not contain credentials.
- `needs_isolated_session=true`: create a distinct fleet because cookies,
  storage, proxy identity, or account state must not be shared. An isolated or
  named-session fleet is never promoted to the slot's generic default fleet.

If a named session's fleet disappears from the authoritative owner inventory,
the coordinator retains a process-local lost tombstone and returns
`session_fleet_lost`. It never silently binds that `session_key` to another
fleet. This is a terminal routing result: the lead must mark the auth session
stale and use the auth-interrupt/login recovery path rather than retrying.

A transport failure alone is not authoritative fleet loss. The slot first
enters `suspect`; before retirement the spawner creates a new socket, registers
the same owner `agentId`, and consumes the returned owner inventory. A
successful registration restores the original binding to `active`. Exhausted
bounded reconnect attempts leave the binding `suspect` and initially return
retryable `session_transport_unavailable`; lack of transport is not proof of
fleet loss. After the configured number of failed recovery cycles, spawn
returns non-retryable `session_manual_reset_required`, but still does not
release the binding automatically.
Only a successful authoritative registration whose inventory omits the fleet
moves it to `missing`. The failed browser RPC is never replayed because a lost
response does not prove that a mutation was not executed.

Named-session recovery is generation guarded. The internal-only
`release_session_binding(session_key, expected_fleet_id, expected_generation,
reason)` primitive performs a compare-and-swap release after trusted auth
recovery authorizes it. The old fleet remains `released` and is never admitted
to generic reuse; the next bootstrap for that key uses the incremented
generation and a fresh fleet. There is deliberately no model or Lead tool that
can invoke this primitive directly.
For prolonged transport failure, the host-only
`reset_auth_session(session_key, expected_fleet_id, expected_generation,
reason)` API performs the same CAS discipline, marks any persisted ledger entry
stale, and retires barrier bookkeeping. It refuses busy slots and is not exposed
to either model.

`session_fleet_lost` and `fleet_assignment_lost` are infrastructure hard
signals. They are accepted only from terminal browser-tool results derived from
real Dispatcher errors; a model final answer cannot manufacture either state.

An unnamed `fleet_assignment_lost` has different semantics: it remains a
recoverable phase status so the next attempt can receive a fresh coordinator
assignment. It counts against the phase's `max_attempts` budget and becomes
`phase_failed` when exhausted, but it does not consume the cross-replan
objective failure budget because inventory loss is not evidence that the task
objective is infeasible.

## Ownership boundary

The harness does not close reusable fleets and does not implement fleet GC.
`Fleet.list`/registration are treated as the owner inventory snapshot; removed
unbound fleets are dropped from assignment candidates, while named-session
bindings remain only as lost tombstones. Dispatcher remains authoritative for
leases, pinning, retention, archive, and cleanup. `newtab.html` bootstrap pages
are tolerated as internal pages and are not evidence of a task page.

## Implemented phases 3 and 4

The harness-owned auth ledger is persisted at
`<worktree_dir>/<auth_fleet_ledger_path>`. It records only evidence-verified
session handles and non-secret metadata. Barrier clearance and durable
verification are deliberately separate: an observed post-HITL `Page.getState`,
a fresh `DOM.getAXTree`, and a non-paused page reopen the current fleet but do
not by themselves write the ledger. Persistence additionally requires a
pre-HITL `worker_contract.auth_verification` whose protected URL and
authenticated UI marker both match harness-observed state. Protected URLs use
canonical origin equality plus a path-segment boundary, so a sibling host such
as `example.com.evil.test` or a path such as `/accounting` cannot satisfy an
`https://example.com/account` declaration. Markers are structured AX node
declarations such as `{"role":"button","name":"Sign out","match":"exact"}`;
the role and normalized accessible name must match a visible, non-blocked AX
node exactly. Plain text substrings and hidden nodes are not authentication
evidence. Without that proof,
cookies may be shared for the current task but the fleet is not trusted across
tasks or process restarts. `accountLabel` remains null
unless a future trusted identity probe supplies evidence; model assertions are
not accepted. On restart, owner inventory is reconciled against this ledger
before generic admission. Unknown reclaimed fleets stay quarantined, while
ledger fleets recover their named-session and isolation restrictions.

The fleet-wide auth barrier is fail-closed. The first worker detecting or
requesting HITL becomes resolver; other workers wait for a bounded interval and
receive `fleet_auth_gated` on timeout. Verified clearance increments the fleet
generation. Every other worker must then complete `Page.getState` and
`DOM.getAXTree` before another action is admitted. The target generation is
latched until both observations complete; if another clearance changes the
generation mid-sequence, both observations restart for the new generation.
Manual HITL claims the barrier before `Hitl.requestPause`; losing workers
receive `fleet_auth_gated` without entering a second human flow. If the pause
request itself fails, the worker relinquishes resolver ownership immediately
but the gate stays closed. While a closed gate has no resolver, only
`Page.getState` and `DOM.getAXTree` diagnosis plus an explicit
`Hitl.requestPause` claim are admitted; an arbitrary business call can never
become resolver implicitly.

The task fleet group makes the first generic worker's fleet the default for all
parallel phases in the same task. A delegated worker keeps its own connection
and page but does not take ownership. Owner notifications are relayed to the
delegate's notification hub. A process-wide page lease serializes equal
`pageId` calls across slow-path tools and Workflow fast paths; calls to different
pages remain concurrent.

Configuration:

- `fleet_reuse_enabled`: deterministic assignment and fleetless-call guard.
- `same_fleet_multiworker_enabled`: opt-in (default off) task/session fleet
  groups, owner/delegate routing, notification relay, and same-page leases.
- `fleet_auth_barrier_enabled`: fleet-wide authentication gate.
- `fleet_auth_barrier_wait_seconds`: bounded non-resolver wait.
- `auth_fleet_ledger_path`: path relative to `worktree_dir` unless absolute.
- `fleet_slot_manual_reset_after_failures`: failed transport recovery cycles
  before operator intervention is required; it never auto-releases a session.

## Platform follow-up

The harness compatibility layer is complete, but Dispatcher-native leases,
multi-subscriber delivery, auth pinning, and retention semantics remain a
platform deliverable. The exact contract and safe failure behavior are defined
in `docs/dispatcher-fleet-delegation-contract.md`. The harness never silently
falls back to creating another fleet when delegation is unavailable.

## 2026 ABCP lifecycle and batching integration

Fleet/page reuse now shares one harness-owned lifecycle contract. Notification
callbacks update per-page state only; they never perform an RPC. A
`Page.startedLoading` event closes the DOM gate until a settlement event. On
timeout the caller performs exactly one `Page.getState` resynchronization, not
a poll loop. `Page.navigate` and `Page.recovered` also require a new
`Page.getState` and `DOM.getAXTree`; dialog/chooser closure requires state
resynchronization before further work. The same boundary defers remaining tool
calls from one LLM turn after a state-changing call, so a second call cannot act
on pre-navigation observations.

Batch pacing is explicit contract data. `row_interval_seconds` applies between
completed `skill_rows` while retaining the warm tab;
`phase_interval_seconds` is anchored to the latest dependency completion and
waits before reserving a worker slot. `jitter_ratio` applies bounded symmetric
jitter. No task-level interval is implemented because one CLI invocation is one
task rather than a task queue.

When no frozen workflow skill exists, the slow path may compile the first
validated row trace into an in-memory ephemeral workflow for the remaining
homogeneous rows. It validates recursively, forbids Runtime and nested
workflows, hardens navigation settlement/state/AX refresh, runs one canary, and
falls back to ordinary LLM turns on any compile/canary/contract failure. It does
not write the skill registry. Native `DOM.getText`/`DOM.getAttribute` batch
reads and `DOM.getImg` are exposed only when the live ABCP schema/capabilities
support them; older servers retain single-target behavior.

JavaScript authorization now lives at the Runtime boundary. All model-facing
`Runtime.evaluate` calls require the same `runtime_policy`; the hidden
`eval_js_json` alias and structured harness composites cannot create an
ungated path. `world` is accepted only when the live schema advertises it.
Frozen skills preserve an authored world on upgraded servers and omit it only
from a legacy execution copy. The expression scanner is conservative
defense-in-depth, not a JavaScript sandbox.

## Remaining operational probes

Unit/integration coverage exercises generation changes, resolver competition,
failed-pause handoff, ledger proof, and same-page leases. Two live platform
probes remain intentionally separate from code correctness:

- two workers on distinct pages in one real authenticated fleet while one page
  enters and resolves HITL;
- cross-page `Input.drag` concurrent with another page input operation.

These probes require an operator-selected real login target and live ABCP
credentials. The release default for `same_fleet_multiworker_enabled` remains
off until that canary evidence is recorded.
