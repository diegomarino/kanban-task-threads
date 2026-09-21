# The transport seam

Discord is the first transport, not the only conceivable one — but nothing
here gives up a Discord capability to fit a platform that may never be
implemented (ADR-0004). The seam exists to keep a second transport
*possible*, not to build toward it.

## The seam

`transport.Transport` is a Protocol of four operations plus a declaration:

```python
capabilities() -> frozenset          # what this transport can do
open_thread(*, title, card) -> ThreadRef
edit_card(ref, card) -> None
append(ref, *, content, username=None) -> str
```

`set_title` and `archive` are named as capabilities (`title_state`, `tags`)
and arrive with the bot-token path; the renderer asks the transport what it
supports instead of assuming a common subset. Everything else — cursor
consumption, what earns a reply vs a card refresh, idempotency, retry,
truncation — lives in the core, because none of it is platform-shaped.

Capability vocabulary: `rich_card`, `live_timestamps`, `per_message_identity`
(webhook-only), plus `title_state`, `tags` (with a bot token).

## The Discord implementation

Webhook-first, and that is a security posture: a webhook URL is write-only and
bound to one channel, so a leak costs only the ability to post there — the
right credential to ask a stranger to configure. The bot token is a separate,
optional capability: detected, never required.

| Operation | Call |
|---|---|
| `open_thread` | `POST {webhook}?wait=true` with `thread_name` — creates the forum post; the response is the starter message |
| `edit_card` | `PATCH {webhook}/messages/{message_id}?thread_id={thread_id}` |
| `append` | `POST {webhook}?wait=true&thread_id={thread_id}`, optional per-message `username` |
| `webhook_info` | `GET {webhook}` — returns `channel_id`: the forum is asked, never configured |
| `forum_requires_tag` | `GET /channels/{id}` with the bot token; `flags & 16` |
| `prepare_forum` | bot: read `available_tags`; `PATCH /channels/{forum_id}` only when managed names are missing |
| `set_status_tag` | bot: `PATCH /channels/{thread_id}` with `applied_tags`; tag names resolved against the forum's `available_tags`, fetched once |
| `rename` / `set_archived` | bot: `PATCH /channels/{thread_id}` with `name` / `archived` |
| `list_forum_threads` | bot: `GET /guilds/{guild_id}/threads/active`, filtered by forum, plus `GET /channels/{forum_id}/threads/archived/public?limit=25` |

The bot operations are what the `title_state`/`tags` capabilities unlock: the
consumer keeps one plugin-owned tag for every status, renames after title
edits, archives done/archived tasks, and audits Discord's actual metadata.
Normal task events still PATCH their own thread immediately. Successful PATCH
channel bodies are authoritative readback; `last_tag` and `thread_archived`
in SQLite are only traffic-saving hints.
Without a bot token none of this runs and the plugin is complete anyway.

Bot-enabled preflight owns the status-tag vocabulary, not the whole forum tag
list. It preserves every existing tag and appends only missing canonical names.
Changing the forum's `available_tags` requires `MANAGE_CHANNELS`; applying
existing IDs to a thread requires `MANAGE_THREADS`. If all managed names already
exist, no channel PATCH is sent and manual creation is a supported permission
fallback. The operation fails closed before mutation when existing plus missing
tags would exceed Discord's limit of 20.

**The status tag owns `applied_tags` — a decided limitation.** Setting a
status tag replaces the thread's whole tag set. The total mapping is
`triage→triage`, `todo→todo`, `scheduled→scheduled`, `ready→ready`,
`running→running`, `blocked+needs_input→needs-human`, other
`blocked→blocked`, `review→review`, `done→done`, `archived→archived`,
`stale→failed`, `dependency_wait→blocked`. What does *not* survive is a tag a
human applied by hand: the next automated retag wipes it. In a tag-required
forum, bot mode uses managed `triage` for creation when no explicit
`discord_applied_tag_ids` override exists; webhook-only mode still requires an
explicit ID.

The audit is bounded, never a crawler: all active guild threads are read once
and filtered to the forum, then only the latest 25 public archived forum
threads are read. Plugin-owned posts in that set are compared with current
board state. It unarchives before changing an archived tag, applies the tag
before final archive, and PATCHes only mismatches. Clean audits back off 5m,
15m, 30m, then 60m capped; a repair confirms in 1m. A 429 honors
`retry_after`; other failures are warned and retried without blocking normal
event consumption. Schedule state exists only in consumer memory.

Two rules are enforced *inside* the transport so no caller can forget them:

- **`allowed_mentions: {"parse": []}` on every call.** Webhook content parses
  mentions by default, and titles/reasons/summaries are arbitrary agent
  output. Not configurable.
- **Deterministic truncation** to Discord's limits before sending (2000
  content / 4096 embed description / 100 thread title, cut marked with `…`).

## Platform facts that cost an hour each to learn

- **The forum post id IS the thread id.** A forum thread inherits its starter
  message's id — one `ThreadRef` carries both only because the edit endpoint
  addresses the message while the reply endpoint addresses the thread.
- **`username` is fixed at message creation.** `PATCH` changes content, never
  the signature. This single fact decides the card's signature rule (see
  [card-and-log.md](card-and-log.md)).
- **Cloudflare bans urllib's default User-Agent**: `403, error code: 1010` to
  `Python-urllib/3.x`. The HTTP client always sends an identifying UA.
- **Archived threads behave usefully asymmetrically** (probed in design): a
  webhook `PATCH` of the starter succeeds and leaves the thread archived; a
  reply `POST` unarchives it. A finished task keeps refreshing its card
  without resurfacing; a genuine new event resurfaces it. No bot token needed
  to survive auto-archive.
- **Discord renders `<t:UNIX:R>` client-side** — elapsed time stays true
  without re-edits, which is most of what makes the card trustworthy.
- **A tag-required forum (`flags & 16`) rejects every webhook create** lacking
  `applied_tags` — with error code 40067. With a bot token the plugin
  preflights this at startup and fails closed; without one it maps that 400 to
  an actionable dead-letter message.

## The Telegram appraisal (deliberately not built)

An interface extracted before its second implementation exists is the usual
way to get the interface wrong; the mapping is recorded so a future evaluation
is informed (checked against the Bot API at design time):

| Operation | Telegram |
|---|---|
| `open_thread` | `createForumTopic` (forum supergroup) |
| `edit_card` | `sendMessage` + `editMessageText`, **pinned** (a topic has no structural top) |
| `append` | `sendMessage` with `message_thread_id` |
| `set_title` / `archive` | `editForumTopic` / `closeForumTopic` |

And what it loses, which is the part worth knowing first: no `<t:…:R>` (elapsed
time goes stale between edits), one sender identity (no per-message
`username`), no embeds or colour, and a worse credential (no write-only
webhook — the bot token does everything, so the leak-cost argument
evaporates). A README for that transport must say so up front.
