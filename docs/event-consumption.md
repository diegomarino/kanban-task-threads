# Event consumption: the log, not the hooks

The plugin's transport of record is the board's own `task_events` table,
consumed past a durable cursor. Hermes's plugin hooks are kept only as a
best-effort *"poll now"* kick with no correctness role: a missed kick costs
seconds, not an event. This is the load-bearing decision of the whole design
(ADR-0002), and the reasoning cuts both ways:

**Why not hooks as transport?** The eight kanban hooks cover eight *moments*;
the event log carries twenty-plus kinds, including ones no hook reports
(`commented`, `archived`, `unblocked`, the spawn-failure circuit breaker's
`gave_up`). There is no task-creation hook at all. And a worker process exits
seconds after completing, so anything queued in-process loses exactly the
events that matter most. The event log, by contrast, gives durability (the
cursor is a row), cross-process arbitration (SQLite's writer lock), ordering
(by event id, the commit order) and full coverage — for free.

**Why not the built-in `kanban_notify_subs`?** It looked perfect — it already
has cursors and atomic claims — but those rows are *owned by the gateway
notifier*: it retries deliveries up to a failure cap and purges old
subscriptions for done tasks. A foreign platform squatting in that table risks
the notifier fighting it or garbage-collecting its cursors. So the plugin
keeps its own equivalent, in its own SQLite file, replicating the semantics
without touching another component's state.

## The two watermarks

```
task_events:  1  2  3  4  5  6  7  8  9  10 11 12
              └───────── board cursor ──┘          scan low-water mark (CAS)
task A eventos:  2  3     5              posts[A].last_event_id = 5
task B eventos:     4        6 ... 12    posts[B].last_event_id = 4  (backed off)
```

- **Per-task position** (`posts.last_event_id`) is the watermark that
  suppresses events already recorded as sent. The position advances only
  after the send, so a crash in that interval can replay a reply; it is
  **monotonic** in SQL (`WHERE
  last_event_id < new`), so even a misbehaving writer cannot rewind it and
  cause replays.
- **Board cursor** (`cursor.last_event_id`) is only a scan optimization. After
  a pass it advances (by compare-and-swap) to the *minimum* position any task
  still needs — `batch_max` for tasks that are done with the batch (including
  tombstoned/dead-lettered ones, whose events are permanently skipped), the
  task's own durable position for ones that could not make progress (backoff,
  unknown create outcome). A stuck task therefore holds the cursor down and
  its events are re-scanned and re-skipped cheaply each pass, while every
  other task publishes normally.

Backfill falls out of the model: a task never seen before is read from
position 0, so its whole history lands in the thread in order — including a
board's history when the plugin is first enabled.

## Cross-process exclusion and fencing

Every process that loads the plugin (gateways, the dispatcher, CLI runs) may
run a pass; one at a time does, under a named lease `consume:<board>`:

- `acquire_lease` (inside `BEGIN IMMEDIATE`) returns a **fencing token** — a
  counter that increments on every acquisition, so two acquisitions are always
  distinguishable, even by a reloaded process reusing the same
  `profile:pid` holder string. Release *expires* the row rather than deleting
  it, precisely to keep that counter monotonic.
- The consumer proves ownership **before every task's side effects**
  (`renew_lease` requires holder *and* token to match) and aborts the pass the
  moment renewal fails — no cursor advance, no further sends. The new holder
  simply continues from the durable positions.
- Residual window, documented and accepted: fencing is per task, not per
  Discord call, so one task whose sends outlive the TTL can overlap the new
  holder for that task's duration. The damage is bounded to a duplicated
  reply/edit (a duplicated *thread* only if the overrun lands exactly on a
  create).

Replies are **at-least-once** across a crash between send and position write.
Creates are stricter — see [failure-policy.md](failure-policy.md).

## Who runs the pass, and when

`runtime.Runtime` owns one daemon thread per process. Hooks — all eight kanban
hooks — are wired to `kick()`, which only sets an event (some hooks fire
inside the dispatch lock; the callback must never do work). The thread also
wakes every `poll_seconds` regardless, which is what picks up the event kinds
no hook announces. The first kick lazily builds the consumer; see
[configuration.md](configuration.md) for what the build does and how it
degrades.
