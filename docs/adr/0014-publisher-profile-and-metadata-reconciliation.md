# ADR-0014 — Optional publisher pinning and bounded Discord metadata reconciliation

Status: accepted (2026-09-21)

## Context

ADR-0013 deliberately makes every profile loading the plugin a lease candidate,
which is the safest zero-configuration behavior. A multi-profile installation
may instead need one known Discord identity to own publication and its bot
permissions. That choice must be explicit: silently selecting a profile by
gateway start order would recreate the bug ADR-0013 removed.

The bot path also kept only a local cache of the last requested tag and archive
state. A webhook-only pass, a manual Discord change, a lost response, or
Discord auto-archive can therefore leave the real thread metadata different
from the board indefinitely when no later task event arrives.

## Decision

Add optional `publisher_profile`. Empty keeps ADR-0013 unchanged: every loaded
profile is a lease candidate. A non-empty value makes every non-matching
profile completely inert for this plugin; only exact `ctx.profile_name` match
may register kicks or start/build the publishing runtime. Pinning deliberately
trades away automatic cross-profile failover.

When the selected runtime has the optional bot capabilities, the consumer
audits Discord metadata immediately on its first pass and thereafter on an
in-memory schedule. One audit reads all active guild threads and filters them
to the webhook's forum, then reads only that forum's latest 25 public archived
threads. It never paginates further and never issues per-thread GETs.

For plugin-owned posts in that bounded set, Discord's `applied_tags` and
`thread_metadata.archived` are compared with current board state. Only
mismatches are PATCHed. An archived thread is unarchived before a needed tag
change; the desired tag lands before a terminal thread is finally archived.
Successful PATCH response fields are authoritative readback. SQLite
`last_tag`/`thread_archived` remain cache hints, not claims of external truth.

The status tag mapping is total. The first bot-enabled pass holding the fenced
board lease reuses exact-name matches and creates any missing managed tags
while preserving unrelated tags:
`triage`, `todo`, `scheduled`, `ready`, `running`, `blocked`, `review`, `done`,
and `archived` map to the same names; blocked `needs_input` maps to
`needs-human`; stale maps to `failed`; dependency wait maps to `blocked`.
These tags are plugin-owned filtering metadata, so manual edits may be
overwritten. Creating missing forum tags requires `MANAGE_CHANNELS`; applying
them requires `MANAGE_THREADS`. If the 20-tag limit would be exceeded, or the
channel PATCH is forbidden, the pass fails closed without deleting anything.
When a forum requires a tag, bot mode uses managed `triage` for creation unless
`discord_applied_tag_ids` explicitly overrides it; webhook-only mode still
needs an explicit creation tag ID.

Clean audits back off through 5, 15, 30, then 60 minutes (capped). Any repair
schedules confirmation in one minute. A 429 honors `retry_after`; another
failure is surfaced and retried after one minute without blocking event
consumption. Timing and level are private process memory only.

## Consequences

- Webhook-only installations remain complete and perform no metadata audit.
- The optional bot token may be the selected publisher profile's existing
  Discord bot token; this plugin neither invents nor provisions another bot.
- The bot's integration-managed Discord role needs forum access,
  `MANAGE_THREADS` (UI: “Manage Threads and Posts”), and
  `READ_MESSAGE_HISTORY` for archived listing. Automatic tag provisioning also
  needs forum-scoped `MANAGE_CHANNELS`; manually creating the complete managed
  vocabulary is the fallback.
- Threads archived outside the active set and latest 25 public archived posts
  are deliberately outside automated repair until they re-enter that window.
- Pinning is explicit and reversible by clearing `publisher_profile`, but a
  pinned publisher has no cross-profile failover while it is unavailable.
- Forum setup runs under `consume:<board>`, so multiple default profile
  candidates cannot race full-list `available_tags` replacements or retain IDs
  invalidated by another candidate. A holder that loses the lease during setup
  leaves setup pending; the next attempt re-reads Discord's current tag IDs.
