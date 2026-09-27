# ADR-0010 — Legacy one-webhook default; explicit board routes

Status: accepted (2026-09-20); superseded for multi-board routing (2026-09-26)

## Context

Two forum-per-X requests arose: per *project* (in Hermes, a project anchors
tasks to a repository — it is not an epic) and per *board*. Both are the same
shape: a map `key → webhook-secret-name` with consumers routed by key. Epics
explicitly do **not** get channels: they churn too fast, and each epic already
has a thread — its own — which aggregates its children (rollup + link-replies)
instead.

## Historical decision

The opinionated safe default is **one webhook, one forum, zero extra
configuration**: someone trying the tool must have a direct first-time
experience. One deployment serves one board; naming the forum after the board
is a documented human convention, never a mechanism (the plugin discovers
channels via the webhook, it does not name or create them).

Multi-forum routing was parked. The per-task destination identity (ADR-0005)
and the freeze rule (ADR-0007) already accommodated a future extension.

## Superseding decision

The zero-configuration path remains: absent `routes` uses the legacy `board`
setting and `KANBAN_TASK_THREADS_WEBHOOK_URL`. Explicit `routes` now lets one
publisher profile host multiple independent board consumers. Every route is
exactly `selector: {board: ...}` plus one profile-scoped `webhook_secret` name;
it has one destination and a board may not appear twice.

Project selectors, multi-destination publication, and any other selector keys
are not available and are rejected. Future extensions must preserve the
board-only meaning of existing routes rather than reinterpret them.

## Consequences

- FTUX stays: secret + enable + restart, publishing.
- Advanced deployments may configure explicit board routes without giving up
  the legacy default.
- Channel renames are free (Discord binds webhooks by id); *moving* a webhook
  is the ceremony, and the freeze rule already governs it.
