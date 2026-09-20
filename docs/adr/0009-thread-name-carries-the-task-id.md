# ADR-0009 — The frozen thread name carries the one stable key

Status: accepted (2026-09-20)

## Context

A webhook can never rename a thread, so the name is frozen at creation for the
baseline deployment. Task titles collide (two "fix the build" threads are
indistinguishable in a forum list), and the failure policy's hardest case —
a create with unknown outcome — requires an operator to check the forum for a
thread that may or may not exist. With only a title, that check is a guess.

## Decision

Thread names are `"{title} · {task_id}"`. Truncation to Discord's 100-char
limit eats the title, never the id. Status stays out of the name (it would go
stale for webhook-only deployments — it lives in the card, which is always
rewritable); with a bot token, a title edit renames the thread to keep the
frozen half honest.

## Consequences

- Reconciliation becomes a deterministic forum search: Discord indexes thread
  names, and the id is in it. The `adopt` verb exists because of this.
- Duplicate titles stay distinguishable forever.
- Ten characters of noise per thread name, paid once.
