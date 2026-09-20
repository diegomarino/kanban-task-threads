# Configuration, secrets, and the deferred startup

## Why `register()` does no I/O

Hermes probes a plugin's `register()` in two hostile contexts: the catalog
gate (`hermes plugins validate`) runs it in a subprocess against a **stub
context** whose attributes are no-ops, and `plugins doctor` loads it in a
temporary home with **sockets blocked**. So `register()` only registers: the
eight kanban hooks (all mapped to the same cheap kick), the unload callback,
and `runtime.start()`. Anything real — opening a DB, resolving a secret,
touching the network — happens in the runtime's own thread.

`start()` spawns that thread but the thread **sleeps one poll interval before
its first build** (ADR-0013). That is what keeps `register()` honest without
trying to detect the probe: the stub context answers *every* attribute with a
no-op, including `on_unload`, so no attribute test can tell a probe from a real
runtime. The probe is outlived instead — it exits in milliseconds. A hook kick
collapses the wait, so nothing is slower where hooks do fire.

Starting here rather than on the first kick is what makes every profile a lease
candidate. Kanban hooks fire only in the process holding Hermes' singleton
dispatcher lock, so a kick-only start would elect the publisher by gateway boot
order and leave the `consume:<board>` lease with nobody to fail over to.

The unload callback is load-bearing, not politeness:
`discover_plugins(force=True)` re-imports the module and `hermes plugins
disable` walks the same path — without `ctx.on_unload(runtime.shutdown)`, the
consumer thread would be orphaned, contesting the lease and holding HTTP
sessions from a module nobody can reach anymore.

## Secrets

- `KANBAN_TASK_THREADS_WEBHOOK_URL` — required. Without it the plugin degrades
  to a no-op with one log line.
- `KANBAN_TASK_THREADS_BOT_TOKEN` — optional; unlocks the `title_state`/`tags`
  capabilities and the real tag preflight.

Both are resolved through Hermes' profile-aware secret scope rather than read
directly from the process environment, so they resolve per profile. Ship them
through your secret manager as references (e.g. 1Password `op://…`) resolved
by the profile environment; never a literal in config. The names are
deliberately *not* prefixed
`HERMES_KANBAN_`: that prefix is treated as process-global by Hermes and would
bypass profile scoping.

**The contextvars subtlety:** a `ContextVar`-based secret scope does not cross
an unbound thread. `start()` and every kick therefore capture
`contextvars.copy_context()`. Before every build attempt the runtime copies
that registration context and rebuilds the captured profile's secret scope.
Direct profile-file changes and previously failed external-source hydration
are retried on the next interval; successfully hydrated external snapshots
retain Hermes' own cache invalidation and reload semantics. The profile
identity stays pinned to `register()` even when the dispatcher supplies an
intentionally empty context; other ContextVars still come from the latest
startup attempt.
One caller is special: the gateway's embedded dispatcher runs its tick in a
deliberately **empty** `Context()`, so a kick from `on_kanban_dispatch_tick`
may carry no secret scope at all. That case is classified as *retryable* — the
thread keeps running and rebuilds its registered profile scope on the next
interval — never as "unconfigured". A kick can accelerate that attempt but
cannot redirect it to another profile.

## Startup classification: transient vs config verdict

The build runs once per attempt and ends in one of three ways:

| Outcome | Meaning | Effect |
|---|---|---|
| a `Consumer` | configured and preflighted | the loop starts |
| `None` | a **config verdict**: secret present-but-empty in a real scope, Discord rejected the webhook (401/404), tag-required forum with no tags configured, Hermes not importable | permanent no-op until a plugin reload; logged once |
| `RetryableStartup` | a reason that may heal: no secret scope in this context, a 5xx/429 from the preflight, any network error | the thread stays alive, refreshes its registered profile scope, and retries every interval (sooner on a kick); warned once per distinct reason, then DEBUG |

The distinction is the point: a DNS blip or an unlucky first kick must not
require a gateway restart, and a genuinely dead config must not be re-probed
on every kick forever.

## The preflight

One credential, one source of truth: the forum channel id is never configured
— `GET` on the webhook returns the webhook object, whose `channel_id` *is* the
forum it posts to. The two cannot disagree because only one exists.

With a bot token, the plugin also reads the channel and checks `flags & 16`
(tag required): if the forum demands a tag and `discord_applied_tag_ids` is
empty, it fails closed at startup with a message that says exactly what to
set. Without a bot token that flag is unreadable, so the equivalent
create-time 400 (Discord error 40067) is mapped to the same actionable text in
the dead-letter detail. Same answer, delivered at the earliest moment each
credential level allows.

## Settings

Everything under `plugins.entries.<id>.settings`, declared in the manifest's
`config_schema` (see the table in the [README](../README.md)). The split that
matters (ADR-0006):

- **Templatable — taste:** the card body (`card_template`), per-event reply
  text (`reply_templates`), the dashboard link, which events earn a reply
  (`reply_on`), the stale threshold, the poll interval.
- **Not templatable — correctness:** when the card re-renders, idempotency,
  retry policy, truncation limits, `allowed_mentions`.

Templates render through `str.format_map` over a flat dict of pre-stringified
scalars, via a Formatter that rejects any field containing `.` or `[` and all
positional fields — `str.format` on untrusted templates can traverse
`{x.__init__.__globals__[…]}` into module globals, credentials included. A
template that fails to render for any reason falls back to the built-in
default: an operator typo can make a card uglier, never invisible or leaky.

## Links are forever

Every URL the plugin publishes is frozen in a Discord message permanently —
replies are append-only, and even the card only refreshes while the task is
live. So `dashboard_url` must be a **stable name** that resolves from every
device you will ever click from (a Tailscale MagicDNS name, a real DNS record,
`hostname.local` for LAN-only Apple setups) — never a raw IP (DHCP rots it;
the plugin logs a warning if it sees one) and never anything discovered
dynamically at startup: regenerating the value only changes *future* links
while stratifying the published past into eras that rot independently. The
indirection belongs in DNS, resolved at click time — not in configuration,
frozen at publish time. It is also an egress consideration (ADR-0011): the URL
is visible to everyone who can read the channel.

## Editing the templates, concretely

Templates live in **your Hermes configuration**, not in the plugin's files —
editing the plugin directory would be overwritten by `hermes plugins update`.
They are plain strings under `plugins.entries.kanban-task-threads.settings`,
picked up at plugin (re)load:

```yaml
plugins:
  enabled:
    - kanban-task-threads
  entries:
    kanban-task-threads:
      settings:
        card_template: |-
          **{status_label}** · since {since_rel}
          {block_line}assignee: {assignee} · branch: {branch}
          {url_line}
        reply_templates:
          commented: "💬 {author} said something"
          blocked: "⛔ {kind}: {reason}"
          completed: "🏁 {summary}"
```

`reply_templates` overrides per event kind; kinds you don't name keep their
built-in text. A gateway holds its plugin set from startup, so restart it (or
reload plugins) after editing.

**Fields available in `card_template`** — flat names only, no dots or
brackets:

| Field | Content |
|---|---|
| `{title}` | task title (also the thread name, independently of the card) |
| `{status}` / `{status_label}` | raw status key / display label (e.g. `stale — no heartbeat`) |
| `{assignee}`, `{branch}`, `{workspace_kind}` | `—` when absent |
| `{since_rel}` | `<t:…:R>` — Discord renders it live |
| `{block_kind}`, `{block_reason}` | raw values, empty when unblocked |
| `{block_line}` | the composed `waiting on: …` line **with its trailing newline**, empty when not blocked — prefer this over the raw pair so nothing renders on unblocked cards |
| `{depends}` / `{depends_line}` | prerequisite rollup (`1 running · 2 done`) / its composed line (ADR-0012) |
| `{unlocks}` / `{unlocks_line}` | first dependent (`t_xxxx — title`) / its composed line |
| `{url}` / `{url_line}` | `dashboard_url` with `{task_id}` substituted / the composed link line, empty without one |
| `{workspace_path}` / `{workspace_path_part}` | only meaningful with `include_workspace_path: true`; empty otherwise |
| `{priority}`, `{created_by}`, `{project_id}`, `{max_runtime}` | task metadata; not in the default card, available to custom templates |

The view is an allowlist — fields not listed here (task body, results, error
text, dispatcher internals) are unreachable from any template, by design; see
[card-and-log.md](card-and-log.md#the-view-boundary-an-allowlist-not-an-oversight).

**Fields available in every `reply_templates` entry**: `{event_kind}`, plus
`{author}`, `{kind}`, `{reason}`, `{summary}` (safe-defaulted to `someone` or
empty), plus any scalar the event's payload carries. For `commented`,
`{excerpt}` holds the recovered comment text (ADR-0011) when available. Composed
convenience fields `{summary_part}`, `{reason_part}`, `{reviewer_part}` carry
their separators and vanish when the payload lacks the value — prefer them so
nothing renders a dangling "— ".

Rules the formatter enforces: a field name with `.` or `[`, a positional
`{}`, or an unknown field makes the whole template fall back to the built-in
default (logged, never fatal). Output is truncated to Discord's limits after
rendering, so a template cannot overflow a message.
