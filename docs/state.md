# State: what is stored, and why not in `ctx.state`

The plugin's durable state is three tables in a SQLite file it owns:

```sql
cursor (board PK, last_event_id)      -- scan low-water mark, advanced by CAS
posts  (board, task_id PK,
        thread_id, message_id,        -- where the task's post lives
        destination,                  -- which webhook/forum it belongs to
        state,                        -- live | tombstone | dead_letter
        detail,                       -- why, for the two terminal states
        last_event_id,                -- the deduping watermark (monotonic)
        backoff_until,                -- per-task 429 backoff
        pending_create_at,            -- create attempt with unknown outcome
        card_dirty,                    -- last card refresh failed; repaint
        last_status_key,               -- cached card presentation
        last_tag, last_name,           -- Discord metadata cache hints
        thread_archived)               -- cache hint, not external truth
leases (name PK, holder, expires_at, token)   -- consume:<board>, fenced
```

Location: `<kanban root>/kanban/plugins/kanban-task-threads/<board>.db`,
resolved through `kanban_home()` — the same root the board itself lives under.

## Why not `ctx.state`

Hermes gives every plugin a key-value store (`ctx.state`), and it looks made
for exactly this. Three properties disqualify it (ADR-0002/ADR-0005 — both
design reviews independently found this):

1. **It resolves per profile; the board is shared across profiles by
   design.** `PluginState` lives under `get_hermes_home()` (profile-scoped),
   while the kanban root deliberately resolves to a shared location so five
   profiles see one board. Five gateways would get five `state.json` files
   pointed at one board — and any claim/dedup scheme built on them serializes
   nothing.
2. **It has `get` and `set` only — no delete, no atomicity across the two.**
   The lock lives inside `set`, so read-modify-write is a lost-update race,
   which is exactly what a dedup set or a cursor is.
3. **The board's own DB is also not an option** — its schema belongs to
   Hermes, and squatting foreign tables (or the notifier-owned
   `kanban_notify_subs`, see [event-consumption.md](event-consumption.md)) in
   another component's file invites migrations and garbage collectors to eat
   your state.

Hence: a file the plugin owns, at board scope, with real SQL transactions —
the same durability and writer-lock arbitration the board itself relies on.

## The mapping is keyed `(board, task_id)`

Task ids are unique per board, and several boards are supported; one state
file per board keeps a board's publishing history self-contained (and
removable) without touching the others.

Each `posts` row also records the **destination** — which webhook and forum
the post was created through. On every publish the consumer compares it with
the currently configured destination and freezes mismatches instead of
publishing: a re-pointed webhook cannot edit the old channel's messages, and
the resulting 404s would be indistinguishable from a human deleting posts.

`last_tag`, `last_name`, and `thread_archived` are traffic-saving cache hints,
not assertions about Discord's present state. With bot capability, the bounded
metadata audit refreshes tag/archive hints from bulk Discord reads and from
fields returned by successful PATCHes. Audit timing and backoff are private
consumer memory; they add no table, migration, or durable queue (ADR-0014).

## Terminal is not final

`review → running`, `blocked → ready`, `running → ready` on reclaim and
`done → archived` all reanimate a task, so nothing is pruned when a task
"finishes". Tombstones (a human deleted the post) and dead letters (Discord
permanently rejected it) are states on the row, not deletions — a reanimated
task must not silently re-create a post someone removed on purpose.

Uninstalling the plugin leaves the state files behind deliberately: they hold
the task→post mapping, and the posts themselves are never deleted.
