# Dispatcher fleet delegation contract

## Status

The harness now supports same-fleet/different-page concurrency behind
`harness.same_fleet_multiworker_enabled`. The current implementation keeps the
fleet owner socket alive, sends delegated calls with an explicit `fleetId` or
`pageId`, and relays owner notifications in-process. This is compatible with
the verified current Dispatcher fast path while the fleet client is online.

The following items are the required platform contract. They are intentionally
documented separately because `abcp-platform/` in this repository is an
ignored reference checkout, not a deliverable source tree.

## Required lease model

- A fleet has one durable owner agent and zero or more delegate agent ids.
- Delegation is explicit, revocable, scoped to a fleet, and carries a lease id
  plus expiry/renewal metadata.
- `Page.create({fleetId})` and page-scoped methods accept an active owner or
  delegate lease. They must not depend on whether the browser client happens to
  already be running.
- Delegates cannot call `Fleet.close`, transfer ownership, change profile/proxy
  identity, archive, reset, or alter retention policy.
- A fabricated fleet id is never created or adopted as a side effect of a
  delegated page call.
- Revocation and expiry fail closed with stable structured error codes.

Required structured fleet errors include at least `FLEET_ARCHIVED`,
`FLEET_NOT_AVAILABLE`, and `FLEET_OWNERSHIP_MISMATCH` (or a versioned mapping
with equivalent semantics). They must use a dedicated error-code field rather
than only English message text. The harness prefers these codes and retains
legacy substring matching solely as a compatibility fallback.

## Required event model

- Multiple sockets may subscribe without overwriting a single `agentId -> ws`
  map entry.
- Events carry `fleetId`, `pageId` when applicable, owner agent id, lease id,
  and a monotonic cursor/generation.
- Owner and authorized delegates can subscribe by lease; page filtering happens
  server-side and replay uses the same authorization check.
- Disconnecting an older socket cannot delete a newer socket's subscription.
- HITL onset/resume and page lifecycle events are delivered to every authorized
  subscriber or are replayable from the durable control event stream.

## Required retention model

- Authenticated fleets can be pinned with a long TTL without transferring
  ownership.
- A fleet with an active auth barrier or active delegate leases is not GC'd.
- Expired delegates do not keep a fleet alive indefinitely.
- `Fleet.close` semantics must distinguish release/delegation revocation from
  archive/destruction. The current prepared/claimable transition is not safe as
  a worker-facing close operation.

## Harness compatibility behavior

Until the platform contract exists, the harness:

- never opens a second socket with the same agent id;
- retains the owner slot and uses distinct acting agent ids;
- relays notifications from owner to delegate in-process;
- serializes only equal `pageId` calls;
- gates login/CAPTCHA fleet-wide and fails closed on timeout;
- treats an ownership error on a delegated call as a platform-contract failure,
  not permission to create another fleet.
