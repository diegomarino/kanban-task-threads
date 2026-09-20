# ADR-0013 — Every profile that loads the plugin is a lease candidate

Status: accepted (2026-09-20) · closes a gap ADR-0002 left open

## Context

ADR-0002 makes `task_events` past a durable cursor the transport of record and
demotes hooks to best-effort "poll now" kicks with no role in correctness. The
first implementation did not honour that: `kick()` was the only caller that
started the consumer thread, so a hook was not an accelerator but the ignition.

The consequence only shows up with multiple profiles. Hermes fires kanban hooks in exactly
one process — the one holding the board's singleton dispatcher lock, which is
first-come-first-served among gateways with no notion of a default profile. So
the plugin's publisher was elected by gateway start order: the profile that won
a `flock` at boot, not the one the lease picked. Any other enabled profile would
therefore never build a consumer and could not participate in failover.

That also disabled the failover the lease was written to provide. `consume:<board>`
has a 60s TTL and a monotonic fencing token precisely so a dead publisher is
relieved by another — but relief requires a second candidate, and there was none.

The obvious fix, starting the thread in `register()`, collides with the catalog
gate: `hermes plugins validate` calls `register()` against a stub context whose
`__getattr__` returns a no-op for *every* attribute, so the probe cannot be
detected — including via `on_unload`, which the stub happily accepts.

## Decision

`register()` calls `Runtime.start()`, and the thread **waits one poll interval
before its first build**.

The probe is not detected, it is outlived: it exits in milliseconds, long before
the thread resolves a secret or opens a socket, so `register()` still performs no
I/O of its own. A kick collapses the wait, which restores the ADR-0002 reading
exactly — hooks turn the next pass into *now*, and a profile with no hook traffic
is late by one interval rather than absent forever.

## Consequences

- Every profile that loads the plugin contests `consume:<board>`; the lease, not
  the dispatcher lock, decides who publishes, and a dead publisher is relieved
  within the lease TTL.
- Idle candidates cost one lease read per interval each. That is the price of
  failover and is deliberate; it is not a reason to pin a publisher.
- Cold start is up to one poll interval on a profile that receives no kanban
  hooks. Delivery is unaffected — the durable cursor still owns correctness.
- Profile identity is captured at `register()`: an empty dispatcher kick may
  accelerate the next attempt but cannot redirect its secret refresh to the
  process-default profile. Non-identity ContextVars still come from the latest
  startup attempt.
- A future "pin the publisher" setting must default to auto. Pinning trades the
  failover back away and reintroduces exactly the single point of failure this
  record removes.
- `RetryableStartup` had to stop killing the thread. Dying to await a kick was
  coherent while a kick was the ignition; once every profile starts unprompted,
  the profiles that need the retry most are precisely the ones no kick reaches.
  The thread now refreshes that profile's secret scope, retries each interval,
  and warns once per distinct reason. A previously failed external-secret
  hydration can therefore recover after repair without waiting for a gateway
  restart. Successfully hydrated external snapshots continue to follow Hermes'
  own cache invalidation and reload semantics.
