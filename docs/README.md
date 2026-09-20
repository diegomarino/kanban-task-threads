# docs/

Topic deep-dives behind [ARCHITECTURE.md](../ARCHITECTURE.md) (the one-page
map) and the [ADRs](adr/README.md) (the rationale). Reading order for someone
new:

1. [card-and-log.md](card-and-log.md) — the idea: a live card rewritten in
   place, replies as the append-only log; the status vocabulary; the
   signature rule.
2. [event-consumption.md](event-consumption.md) — the transport of record:
   `task_events` past a durable cursor, the two watermarks, lease fencing,
   why not hooks and why not `kanban_notify_subs`.
3. [mapping.md](mapping.md) — the cross-reference tables: every event kind,
   hook and task state on the Hermes side ↔ every operation, error code and
   thread trigger on the Discord side.
4. [failure-policy.md](failure-policy.md) — what each Discord failure does:
   the create protocol, dead letters, tombstones, freezes, the dirty card.
5. [state.md](state.md) — the plugin-owned SQLite: schema, location, why
   `ctx.state` is disqualified, terminal-is-not-final.
6. [transport.md](transport.md) — the ADR-0004 seam, the Discord implementation,
   hard-won platform facts, the Telegram appraisal.
7. [configuration.md](configuration.md) — lazy startup, secrets and the
   contextvars subtlety, transient-vs-config classification, the preflight,
   what is templatable and what never will be.
8. [testing.md](testing.md) — the four layers, the sandbox, the doubles, and
   which tests are pins rather than TDD.
