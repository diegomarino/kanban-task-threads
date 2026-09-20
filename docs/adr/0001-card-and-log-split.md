# ADR-0001 — A live card rewritten in place; replies as the append-only log

Status: accepted (2026-09-18)

## Context

A kanban task is nearly invisible while it runs: with terminal-only
notifications, twenty minutes of silence is indistinguishable from a crashed
worker. A flat channel interleaves every task into one stream; a raw
thread-of-updates per task trades that for two dozen unreadable threads —
at the thirtieth event, "what is happening?" means replaying history in your
head.

## Decision

A Discord forum post per task. The post **body** is a status card **rewritten
in place** — a pure function of board state (task row + the last block
reason), never accumulated from event payloads. The **replies** are the
append-only log, one per event worth a permanent record. The body answers
*now*; the thread answers *how we got here*.

The task body is deliberately not on the card: it lives on the board, and
duplicating it guarantees two versions that disagree. The status vocabulary
covers the whole state machine, including two easy-to-miss cases: dependency
blocks route to `todo` (not `blocked`) and get their own presentation, and
`stale` is a first-class computed state — a SIGKILLed worker runs no exit
path, so waiting for a graceful signal would wait forever.

## Consequences

- The card can be re-rendered at any time, from any process, after any crash.
- Replies need per-event attribution (ADR-0008) and dedup (ADR-0002).
- A repaint mechanism must exist for state that changes without events
  (staleness, children rollups) — the dirty-card flag.
