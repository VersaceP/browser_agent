# Architecture decisions

## 2026-08-20: Do not use per-task canonical-contract direct adoption

Status: accepted

Decision:

- The harness will not maintain exact, operator-authored plans for individual
  tasks and will not bypass Lead planning or Plan Validator review by directly
  adopting such plans.
- The canonical-direct prototype and its configuration, shadow, receipt,
  persistence, dispatch, and spawn-lock paths are removed.
- Do not reintroduce this design solely because it improves repeated runs of a
  fixed benchmark task.

Rationale:

- The prototype produced real time and token savings for a repeated,
  navigation-only pilot, but those savings depended on an exact trusted
  contract being authored and kept current outside the normal task flow.
- This project has no product workflow or owner for maintaining those
  per-task contracts. Without one, the fast path is normally disabled and its
  implementation complexity provides no operational value.
- The benchmark exercised a stable special case, while general browser tasks
  still require live planning, page interpretation, recovery, and safety
  checks. Optimizing the special case therefore overfit the benchmark rather
  than improving the general harness.
- Direct adoption introduced a broad trust boundary before an execution-level
  side-effect gate existed. A declarative policy was not sufficient to prove
  that the worker's actual browser actions stayed within scope.

Guardrails for future optimization:

- Prefer improvements that apply to ordinary runs without operator-maintained
  task plans: mechanical validation, compact and queryable observations,
  fewer BrowserAgent round trips, reliable ABCP interaction receipts, and
  evidence-backed termination.
- Evaluate an optimization across multiple tasks and sites, including its
  maintenance cost and failure modes, before adding a new control-flow path.
- If a future product genuinely owns versioned task templates, treat that as
  a new product requirement and design its authorization and action gates from
  first principles; do not revive the removed prototype by default.

## 2026-08-20: Admit historical Fleets only after readiness and trusted memory

Status: accepted

Decision:

- Similar-task Fleet reuse is enabled by default, but it is considered only
  for the first successful Fleet acquisition of a top-level task. Resume and
  every explicit fleet/session/page selector remain stronger.
- A match reuses the browser Fleet only. It never exposes a historical page;
  the worker starts with `page_policy=new`.
- Harness Fleet memory carries a versioned Fleet-level reuse policy. Identity
  use is a monotonic veto: named sessions, pinned identity, and hard isolation
  permanently block automatic reuse. Legacy or foreign memory is not promoted.
- Memory lifecycle records retain the top-level `taskId` and add `workerId`;
  updates are keyed by that pair because multiple workers may concurrently use
  one Fleet. A record is a candidate only after its own worker completed, and
  any fresh running worker record temporarily closes automatic Fleet reuse.
  Active running-worker leases are retained outside the 12-task terminal
  history cap; terminal records collapse by `taskId`, so many workers from one
  task neither erase unrelated task history nor silently lose their own final
  write. Running identity is bounded to 32 records plus one synthetic running
  overflow fence. The fence uses the newest omitted timestamp, so old readers
  remain fail-closed until every represented finite lease expires; TTL zero or
  an untrustworthy timestamp produces a permanent fence. Failed records never
  certify prior task completion. Eligible workers refresh their lease in the
  background and stop that heartbeat before the spawner writes terminal state.
  Cancellation and failure paths make a bounded best-effort terminal write,
  and a configurable 24-hour default TTL recovers after process death. Positive
  TTL configuration is clamped to at least 60 seconds to bound write pressure;
  zero keeps indefinite fail-closed behavior. Identity-free terminal history
  is discarded. Worker records contain only routing identity/status fields,
  and terminal task text is stored once as `rootTask`; prompt-only
  `memory_context` is not persisted in Fleet memory.
- A worker admitted through similar-task reuse must persist its initial running
  lease before browser work starts. Otherwise the older completed record that
  selected the Fleet would remain visible to concurrent processes, so startup
  fails closed instead of silently running without cross-process exclusion.
- Candidate selection is read-only. The spawner verifies/wakes the Fleet first
  and only then atomically admits and binds it in the coordinator. A readiness
  failure therefore needs no coordinator rollback and falls back once through
  ordinary routing/Fleet creation.
- Platform lifecycle inventory uses a positive allow-list: `active` and
  `prepared`. A truly deleted Fleet is absent from `System.register`. Readiness
  is verified separately with a target-scoped `Page.list`: persisted inventory
  can contain idle-looking pages for a prepared Fleet and is not live proof.
  `Fleet.ready` is only a bounded wake-up hint because session restore may
  complete without emitting it; `Fleet.status` remains quarantined while it can
  terminate the caller WebSocket. The probe can wake a cold Fleet: similar-task
  candidates are verified before they are admitted, so a rejected candidate may
  still leave a newly started browser process. This is an accepted, bounded
  consequence of verifying the actual browser execution path.

Rationale:

- Direct `bind_assignment` changed coordinator admission, defaults, groups,
  isolation, ownership, and task Fleet accounting before a candidate was known
  to be usable. Reconstructing all prior values in rollback is more fragile
  than deferring the mutation.
- A task-level first-acquisition gate preserves per-worker isolation for later
  workers and serializes concurrent first spawns through readiness.
- Similarity is ranking evidence, not an identity authorization. Structural
  equality/extension, explicit-origin compatibility, numeric compatibility,
  completed task state, and the Fleet policy must all agree before admission.
