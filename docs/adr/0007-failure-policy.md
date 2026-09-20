# ADR-0007 — The failure policy: unknown creates are sacred

Status: accepted (2026-09-18, extended during implementation)

## Context

A durable event log guarantees the plugin never loses an event; it does not
make Discord reliable. Each failure class needs a decided response, because
the naive ones are all wrong: retrying an ambiguous create mints duplicate
threads, retrying a poison payload 400s forever, recreating a deleted post
overrides a human, and treating a re-pointed webhook's 404s as deletions
punishes an operational migration.

## Decision

- **Create with unknown outcome** (network error *or any 5xx* — a proxy 502
  does not prove Discord created nothing): the attempt is recorded *before*
  the call, never blindly retried, reported every pass until an operator
  reconciles (`attention`/`clear`/`adopt` verbs).
- **Permanent 4xx**: dead-letter the task with the response as detail; the
  tag-required 400 carries an actionable hint.
- **404 on edit/reply**: a human deleted the post — tombstone, stop;
  recreation is operator-only.
- **429**: per-task backoff from `retry_after`; other tasks unaffected.
- **Transient on reply/edit**: retry next pass; an edit-only failure marks the
  card dirty so a quiet task still gets repainted.
- **Destination mismatch**: freeze, never publish — migration ≠ deletion.
- **Poison is prevented, not handled**: deterministic truncation to Discord's
  limits before every send.
- Nothing is swallowed: permanent problems log at `error`, transient at
  `warning`, through the stdlib logger namespace.

## Consequences

- Replies are at-least-once across a crash between send and position write;
  creates are strictly at-most-once per operator decision.
- The operator tooling (reconcile verbs) is part of the policy's contract, not
  an optional extra.
