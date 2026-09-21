# The mapping: Hermes surface ↔ plugin ↔ Discord surface

The cross-reference the other documents imply but don't tabulate: every input
the plugin consumes (event kinds, task statuses, hooks) and every output and
trigger on the Discord side (operations, error codes, thread lifecycle), with
the plugin behavior that connects them.

## 1 · Hermes `task_events` kinds → plugin action

The event log carries more kinds than any hook reports; this is the full set
observed in `kanban_db` and what each one causes. "Card refresh" means the
trailing `edit_card` re-rendered from the task row — every batch a task
appears in ends with one, so *every* kind refreshes the card; the column marks
which kinds additionally earn a permanent reply (the default `reply_on` set).

| Event kind | Reply (default) | Signed by | Notes |
|---|---|---|---|
| `created` | — | — | Triggers the post **creation** if the task is unseen (thread named `{title} · {task_id}` — frozen, so it carries the stable key). Not a reply: the post opening is the record |
| `commented` | 💬 yes | payload `author` | Reply quotes a bounded excerpt recovered from `task_comments` (ADR-0011); fallback: plain "commented". CLI mirror comments (`BLOCKED:`/`SCHEDULED:`/`UNBLOCK:` — written by `hermes kanban block\|schedule\|unblock` alongside the semantic event) are suppressed: one act, one reply |
| `blocked` | ⛔ yes | assignee | Reply carries kind + reason; the card's "waiting on" line comes from the row's `block_kind` + this event's reason |
| `unblocked` | ✅ yes | assignee | Payload is `None` |
| `completed` | 🏁 yes | assignee | With summary |
| `review_requested` | 🔎 yes | payload `implementer` | `review` is a full status; see section 3 below |
| `changes_requested` | ✏️ yes | payload `reviewer` | A *reviewer's* act — never signed by the assignee; board voice if unnamed |
| `gave_up` | 🛑 yes | board (unsigned) | The spawn-failure circuit breaker fires **no hook** — only this event row. Log-only consumption is why the card doesn't show "running" forever |
| `crashed` | 💥 yes | board (unsigned) | A system observation, not the worker speaking |
| `timed_out` | ⏱️ yes | board (unsigned) | |
| `reclaimed` | ♻️ yes | board (unsigned) | Stale-claim reclaim; the card returns to a non-running state |
| `archived` | 📦 yes | board (unsigned) | `archive_task` fires no hook either; payload is `None` |
| `assigned`, `promoted`, `promoted_manual`, `claimed`, `claim_rejected`, `dependency_wait`, `edited`, `attached`, `attachment_removed`, `unlinked`, `review_reopened`, `descendant_invalidated`, `status`, `specified`, `scheduled`, `spawned` | — | Card refresh only. Any of them can be added to `reply_on` |

Rules that are event-shaped rather than kind-shaped:

- **A claim never creates a post**: `claim_review_task` fires a *second*
  `claimed` for the same task; creation keys on "task unseen", never on claim.
- Unknown/future kinds are safe by construction: not in `reply_on` → card
  refresh only.

## 2 · Hermes hooks → plugin

All eight kanban hooks map to the **same** action: `kick()` — set an event,
maybe start the thread. No payload is read; correctness never depends on a
hook firing (the poll interval covers hook-less kinds like `commented`).

| Hook | Fires in | Why it still matters |
|---|---|---|
| `kanban_task_claimed` | dispatcher, **inside the dispatch lock** | latency: card flips to running within seconds |
| `kanban_task_completed` | the worker, seconds before it exits | latency for the event that matters most |
| `kanban_task_blocked` | worker | latency for the field the plugin exists for |
| `on_kanban_task_updated` | whichever process wrote | edits/assignments repaint sooner |
| `on_kanban_worker_spawned` | dispatcher, inside the lock | |
| `on_kanban_worker_exited` | dispatcher | |
| `on_kanban_worker_stale_claim` | dispatcher | the SIGKILL case reaches the card fast |
| `on_kanban_dispatch_tick` | dispatcher, after the lock, **in an empty contextvars Context** | the steady heartbeat — and the reason startup treats a scope-less kick as retryable, not as "unconfigured" |

## 3 · Hermes task states → card rendering and forum tag

`VALID_STATUSES` (9) plus two computed presentations:

| Board state | Card shows | Colour | Bot tag |
|---|---|---|---|
| `triage` | triage | grey | `triage` |
| `todo` | todo | grey-blue | `todo` |
| `todo` **+ `block_kind='dependency'`** | **waiting on dependency** | yellow — dependency blocks route to `todo`, not `blocked`; keying on status alone would hide the wait | `blocked` |
| `scheduled` | scheduled | blue | `scheduled` |
| `ready` | ready | teal | `ready` |
| `running` | running | green | `running` |
| `running` + heartbeat older than `stale_after_seconds` | **stale — no heartbeat** | red — computed, since a SIGKILLed worker reports nothing; appears on the card but never as a reply (state vs events) | `failed` |
| `blocked` + `needs_input` | blocked + kind + reason | orange | `needs-human` |
| other `blocked` | blocked + kind + reason | orange | `blocked` |
| `review` | review | purple | `review` |
| `done` | done | green | `done` |
| `archived` | archived | slate | `archived` |

`VALID_BLOCK_KINDS`: `dependency` (see above), `needs_input`, `capability`,
`transient` — the latter three surface on the `blocked` card's "waiting on"
line with the reason from the last `blocked` event.

## 4 · Plugin operations → Discord API

| Seam op | HTTP | Discord semantics relied on |
|---|---|---|
| `open_thread` | `POST {webhook}?wait=true` + `thread_name` | creates forum post; **post id = thread id**; `?wait=true` returns the message so ids are known |
| `edit_card` | `PATCH {webhook}/messages/{mid}?thread_id={tid}` | edits starter content+embeds (content = plain summary for the forum-list preview); **cannot** change `username`; on an archived thread: succeeds **without unarchiving** |
| `append` | `POST {webhook}?wait=true&thread_id={tid}` | per-message `username` (signature); **unarchives** the thread |
| `webhook_info` | `GET {webhook}` | returns `channel_id`: the forum is asked, never configured |
| `forum_requires_tag` | `GET /channels/{id}` (bot token) | `flags & 16` = tag required |
| `set_status_tag` | `PATCH /channels/{thread_id}` `applied_tags` (bot token) | tag *names* resolved against the forum's own list, fetched once; unknown name = degraded no-op |
| `rename` | `PATCH /channels/{thread_id}` `name` (bot token) | keeps the frozen thread name honest after a title edit |
| `set_archived` | `PATCH /channels/{thread_id}` `archived` (bot token) | done/archived tasks archive; reanimation unarchives first |
| `list_forum_threads` | bulk `GET` active guild threads + latest 25 public archived forum threads (bot token) | actual `applied_tags` and `thread_metadata.archived`; filter active results to this forum, never per-thread GET or deeper archive scan |

Every mutating body carries `allowed_mentions: {"parse": []}` and
deterministic truncation (2000 / 4096 / 100 / 256 embed title).

## 5 · Discord responses and triggers → plugin behavior

| Discord says | Meaning | Plugin does |
|---|---|---|
| 2xx | ok | advance the per-task watermark; for bot PATCHes, use returned channel metadata as authoritative readback when present |
| 400, code **40067** | forum requires a tag | dead-letter with "set `discord_applied_tag_ids`" |
| other permanent 4xx | payload/operation rejected | dead-letter with detail |
| **404**, or **400 / code 10003**, on edit/append | a human deleted the post/thread | tombstone; stop; operator-only recreation |
| 401/404 on the webhook itself (preflight) | bad credential | config verdict: plugin inactive with message |
| **429** + `retry_after` | rate limit | per-task publication backoff, or metadata-audit retry at the supplied delay; normal event consumption continues |
| 5xx on **create** | outcome UNKNOWN (proxy may have relayed) | pending-create, reported every pass, operator reconciles |
| 5xx on reply/edit | transient | retry next pass; card dirty if the edit failed |
| **403, error code 1010** | Cloudflare bans the default urllib UA | never happens: identifying User-Agent always sent |
| network error anywhere | unknown/transient per the same split | create → pending; publish → retry; preflight → `RetryableStartup` |
| thread auto-archived | Discord housekeeping | nothing needed: card PATCHes stay archived, real events unarchive via the reply |

## 6 · What deliberately has no mapping

- **Discord → Hermes**: nothing flows back. The plugin is write-only
  visibility; humans replying in a thread are read by whatever the deployment
  already does with Discord messages, not by this plugin (ADR-0011).
- **Waking an agent** on an event: out of scope by design (ADR-0001's scope) — visibility
  and waking are different concerns.
- Discord events the plugin never consumes: it registers no gateway intents
  and reads no messages. The optional bot performs only the bounded channel
  metadata reads above.
