# ADR-0002 — Transport of record: the event log past a durable cursor

Status: accepted (2026-09-18) · supersedes the first draft's hook transport

## Context

Hermes offers eight kanban plugin hooks and a `task_events` table with 20+
event kinds. The hooks cover eight *moments* and miss whole categories: the
spawn-failure circuit breaker, archival, unblocks and comments fire no hook at
all. Worse, a worker process exits seconds after completing — anything queued
in-process loses exactly the events that matter most. And the built-in
per-plugin state store is profile-scoped while the board is shared, so a
hook-fed dedup set would fork per profile and serialize nothing.

Hermes's own notifier subscriptions (`kanban_notify_subs`) have the right
shape but the wrong owner: the gateway notifier retries their deliveries and
purges old rows — a foreign consumer squatting there gets garbage-collected.

## Decision

The plugin consumes `task_events` past a **durable cursor of its own**
(ADR-0005): per-task positions dedupe (monotonic, advanced after send), a
board-level cursor is only a scan low-water mark advanced by CAS, and a fenced
lease arbitrates one consumer pass at a time across processes. Backfill is
explicit: an unseen task reads from position zero, so history lands in order.

Hooks are kept only as a **"poll now" kick** with no correctness role, plus a
poll interval for the kinds no hook announces. A missed kick costs seconds,
never an event.

## Consequences

- Durability, ordering, cross-process mutual exclusion and full kind coverage
  come from SQLite semantics, not from callback discipline. Replies remain
  at-least-once across a crash between send and cursor advance.
- Latency is seconds (kick) to `poll_seconds` (worst case), not instant.
- A stuck task holds the scan low-water mark down; its events are re-scanned
  and re-skipped cheaply while every other task publishes normally.
