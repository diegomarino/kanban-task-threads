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
| `set_status_tag` | bot: `PATCH /channels/{thread_id}` with `applied_tags`; tag names resolved against the forum's `available_tags`, fetched once |
| `rename` / `set_archived` | bot: `PATCH /channels/{thread_id}` with `name` / `archived` |

The three bot operations are what the `title_state`/`tags` capabilities
unlock: the consumer keeps tag-per-status (the six-name convention: running,
needs-human, blocked, review, done, failed), renames after title edits, and
archives done tasks — each PATCHed only on change, tracked in the state store.
Without a bot token none of this runs and the plugin is complete anyway.

**The status tag owns `applied_tags` — a decided limitation.** Setting a
status tag replaces the thread's whole tag set, and reverting to an untagged
state restores the creation defaults (`discord_applied_tag_ids`), so a
tag-required forum stays satisfied. What does *not* survive is a tag a human
applied by hand: the next automated retag wipes it. If manual thread tags
matter in your forum, keep them as the required creation tags or accept the
loss — merging human and automated tags would need a read-modify-write per
retag and an ownership convention that does not exist.

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
