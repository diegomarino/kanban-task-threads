# ADR-0005 — Plugin-owned SQLite at board scope; terminal is not final

Status: accepted (2026-09-18)

## Context

The task → post mapping and the consumption cursor are *board* state. Hermes's
`ctx.state` is disqualified three ways: it resolves per profile while the
board is shared across profiles by design (N gateways would get N state files
pointed at one board); it has `get`/`set` only, no delete; and read-modify-
write across the two is a lost-update race — exactly what a cursor or a dedup
set is. The board's own DB is also not an option: its schema belongs to
Hermes, and the notifier-owned subscription table garbage-collects rows it
thinks are its own (ADR-0002).

## Decision

A SQLite file the plugin owns, under the board-shared kanban root, one per
board: `cursor` (CAS-advanced scan mark), `posts` (per-task position,
thread/message ids, **destination identity**, lifecycle state, maintenance
state), `leases` (fenced tokens). WAL, busy timeouts, explicit
`BEGIN IMMEDIATE` for the races that matter. New columns are added by
in-place, idempotent migration — deployed files are never recreated.

**Terminal is not final**: review→running, done→archived and reclaims all
reanimate tasks, so rows are never pruned on "terminal". Tombstones and dead
letters are states, not deletions — a reanimated task must not silently
re-create a post a human removed.

## Consequences

- Cross-process exclusion comes from SQLite's writer lock, with fencing tokens
  for lease overruns and monotonic positions against watermark rewind. Replies
  remain at-least-once across a crash between send and cursor advance.
- Each post row records which webhook/forum it belongs to; re-pointing the
  plugin freezes mismatches instead of corrupting them (ADR-0007).
- Uninstall leaves the state files behind deliberately: they map to posts that
  are never deleted.
