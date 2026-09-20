# ADR-0010 — One webhook, one forum, zero config; routing parked

Status: accepted (2026-09-20) · parks multi-forum routing

## Context

Two forum-per-X requests arose: per *project* (in Hermes, a project anchors
tasks to a repository — it is not an epic) and per *board*. Both are the same
shape: a map `key → webhook-secret-name` with consumers routed by key. Epics
explicitly do **not** get channels: they churn too fast, and each epic already
has a thread — its own — which aggregates its children (rollup + link-replies)
instead.

## Decision

The opinionated safe default is **one webhook, one forum, zero extra
configuration**: someone trying the tool must have a direct first-time
experience. One deployment serves one board; naming the forum after the board
is a documented human convention, never a mechanism (the plugin discovers
channels via the webhook, it does not name or create them).

Multi-forum routing is *allowed someday, never required*: if ever built, build
it once (`route_by: board | project`), not twice. The per-task destination
identity (ADR-0005) and the freeze rule (ADR-0007) already accommodate it.

## Consequences

- FTUX stays: secret + enable + restart, publishing.
- Advanced deployments wait for a real need before any routing map exists.
- Channel renames are free (Discord binds webhooks by id); *moving* a webhook
  is the ceremony, and the freeze rule already governs it.
