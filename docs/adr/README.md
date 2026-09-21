# Architecture Decision Records

The plugin's load-bearing decisions, one per file, numbered. **Immutability
begins at the first published release**: until then a changed decision is
edited in place (there is no external history to protect); after v0.2.0 ships
publicly, amend a record only to change its Status or point at a superseding
one — never renumber, never rewrite published history. Code comments cite
these numbers (`ADR-0002`), and `tests/test_design_refs.py` fails if a cited
record stops existing.

The [docs/](../README.md) topic files explain *how* things work; these records
say *why they are this way and what was rejected*. New decision → new record,
even when it supersedes an old one.

| ADR | Decision |
|---|---|
| [0001](0001-card-and-log-split.md) | A live card rewritten in place; replies as the append-only log |
| [0002](0002-consume-the-event-log-not-hooks.md) | Transport of record: `task_events` past a durable cursor; hooks are kicks |
| [0003](0003-webhook-first-bot-optional.md) | Webhook-first credential; the bot token is optional and degrading |
| [0004](0004-transport-seam-discord-first.md) | A small transport seam; never design to the intersection of platforms |
| [0005](0005-plugin-owned-board-scoped-state.md) | Plugin-owned SQLite at board scope; terminal is not final |
| [0006](0006-safe-templates.md) | Templates render through an allowlisting formatter; correctness is not templatable |
| [0007](0007-failure-policy.md) | Unknown creates are sacred; tombstones, dead letters, freezes, per-task backoff |
| [0008](0008-institutional-card-actor-replies.md) | The card is the board speaking; replies are signed by the actual actor |
| [0009](0009-thread-name-carries-the-task-id.md) | The frozen thread name carries the one stable key |
| [0010](0010-single-forum-default.md) | One webhook, one forum, zero config — multi-forum routing parked |
| [0011](0011-egress-allowlist.md) | The view is an egress allowlist; absence is the safe default |
| [0012](0012-task-links-are-prerequisites.md) | `task_links` are prerequisites, not a hierarchy — the hub follows the real arrows |
| [0013](0013-every-profile-is-a-candidate.md) | Every profile that loads the plugin is a lease candidate; the thread waits before it builds |
| [0014](0014-publisher-profile-and-metadata-reconciliation.md) | Optional publisher pinning and bounded Discord metadata reconciliation |
