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

`publisher_profile` is the explicit exception (ADR-0014). Empty is backward
compatible and keeps every profile as a candidate. When set, `register()`
compares it exactly with `ctx.profile_name`; non-matching profiles are inert
and do not create a runtime, hooks, unload callback, secret lookup, database
handle, or network request. Pinning is useful when Discord credentials and
role ownership belong to one existing profile, but it intentionally gives up
ADR-0013's cross-profile failover. Clearing the setting restores automatic
lease candidacy.

The unload callback is load-bearing, not politeness:
`discover_plugins(force=True)` re-imports the module and `hermes plugins
disable` walks the same path — without `ctx.on_unload(runtime.shutdown)`, the
consumer thread would be orphaned, contesting the lease and holding HTTP
sessions from a module nobody can reach anymore.

## Secrets

- `KANBAN_TASK_THREADS_WEBHOOK_URL` — required. Without it the plugin degrades
  to a no-op with one log line.
- `KANBAN_TASK_THREADS_BOT_TOKEN` — optional; may reference or alias the
  selected publisher profile's existing Discord bot token. The plugin does
  not provision a dedicated bot. It unlocks `title_state`/`tags`, the real tag
  preflight, and bounded metadata reconciliation.

The token authenticates the bot; authorization comes from its
integration-managed Discord role. That role needs access to the forum,
`MANAGE_THREADS` (Discord UI: **Manage Threads and Posts**) and
`READ_MESSAGE_HISTORY` so the latest archived posts can be listed. Automatic
status-tag setup additionally needs `MANAGE_CHANNELS` (**Manage Channels**) on
that forum. Scope both management permissions to the forum rather than the
whole server. This role belongs to the integration and is not manually assigned
to people.

On the first bot-enabled pass that holds `consume:<board>`, the plugin reads the
forum's `available_tags`, preserves every unrelated tag, appends missing
managed names, and restores the managed emojis when they drift. Running setup
under the same fenced lease as publication keeps
multiple profile candidates from racing full-list replacements. A successful
PATCH response supplies the IDs used thereafter. If the combined set would
exceed Discord's 20-tag limit, or Discord rejects the PATCH because `Manage
Channels` is absent, that pass publishes nothing and reports an actionable
error; later passes retry. Operators who do not grant that permission may
create the complete name-and-emoji vocabulary manually; no PATCH is issued
when every managed tag already matches.

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

For the process home, refresh uses Hermes' `launch_secret_scope` when available,
preserving its trusted launch-environment snapshot. Older Hermes versions without
`tui_gateway.launch_profile_policy` use `build_profile_secret_scope` instead,
as routed profiles already do. Hermes' `get_secret` retains environment-only
credentials in single-profile mode and fails closed on scope misses when
multiplexing is active. The plugin never copies ambient credentials or changes
the multiplexing flag. A broken dependency inside an available launch policy is
still an error; it is not treated as an absent optional API.

The `>=0.20` requirement is retained: the official
[Hermes 0.20.0 source](https://github.com/NousResearch/hermes-agent/tree/3c27eb6234bf91b8ceee9e9071591b31e9b148cb)
already exposes the profile builder, hydration and home-override APIs used by
this path. CI exercises deferred startup on that baseline and Hermes 0.21.3;
the latter also provides the registration-validation CLI command. Source
inspection is not a claim of full runtime or
Discord compatibility for every intervening version.

## Startup classification: transient vs config verdict

The build runs once per attempt and ends in one of three ways:

| Outcome | Meaning | Effect |
|---|---|---|
| a `Consumer` | configured and preflighted | the loop starts |
| `None` | a **config verdict**: secret present-but-empty in a real scope, Discord rejected the webhook (401/404), Hermes not importable | permanent no-op until a plugin reload; logged once |
| `RetryableStartup` | a reason that may heal: no secret scope in this context, a 5xx/429 from the preflight, any network error | the thread stays alive, refreshes its registered profile scope, and retries every interval (sooner on a kick); warned once per distinct reason, then DEBUG |

The distinction is the point: a DNS blip or an unlucky first kick must not
require a gateway restart, and a genuinely dead config must not be re-probed
on every kick forever.

## The preflight

One credential, one source of truth: the forum channel id is never configured
— `GET` on the webhook returns the webhook object, whose `channel_id` *is* the
forum it posts to. The two cannot disagree because only one exists.

The startup preflight only discovers the forum through the webhook. With a bot
token, the first consumer pass holding the board lease then reads the channel,
provisions the managed vocabulary, and checks `flags & 16` (tag required). If
the forum demands a tag and `discord_applied_tag_ids` is empty, managed
`triage` is used for creation; normal maintenance immediately applies the
task's real state. Without a bot token the flag and tag IDs are unreadable, so
a tag-required forum still needs an explicit `discord_applied_tag_ids`;
Discord error 40067 is mapped to that actionable instruction at create time.

## Settings

Everything under `plugins.entries.<id>.settings`, declared in the manifest's
`config_schema` (see the table in the [README](../README.md)). The split that
matters (ADR-0006):

- **Templatable — taste:** the card body (`card_template`), per-event reply
  text (`reply_templates`), the dashboard link, which events earn a reply
  (`reply_on`), the stale threshold, the poll interval.
- **Not templatable — correctness:** when the card re-renders, idempotency,
  retry policy, truncation limits, `allowed_mentions`.

### Avatar customization and self-hosting

Avatars are enabled by default. With no avatar settings, the plugin uses its
official, immutable catalog:

```text
https://diegomarino.github.io/kanban-task-threads/
  v1/{theme}/{palette}/96px/{message_type}.png
```

`avatar_theme` selects `duotone` (default), `fill`, or `bold`.
`avatar_palette` selects `colored` (default), `black`, or `white`. These two
settings apply only to the official catalog. An unknown value logs a warning
and falls back to `duotone`/`colored`; cosmetic configuration never stops task
publication.

The plugin's bundled manifest owns `v1` and `96px`; neither is configurable.
They are compatibility coordinates, not branding. When a future plugin adopts
`v2`, it will request the `v2` tree automatically and the official host will
retain `v1` for older installations and already-published messages.

#### Custom directory

Set `avatar_base_url` to a public HTTPS **directory**, not a site root. Custom
mode appends only `{message_type}.png`; it never appends the official version,
theme, palette, or size:

```yaml
avatar_base_url: https://assets.example.com/hermes-avatars
```

```text
https://assets.example.com/hermes-avatars/blocked.png
```

The directory needs these 12 files:

```text
default.png
commented.png
blocked.png
unblocked.png
review_requested.png
changes_requested.png
completed.png
gave_up.png
crashed.png
timed_out.png
reclaimed.png
archived.png
```

Unknown future message types use `default.png`. The server must expose the
files over HTTPS without credentials, query parameters, or redirects that
require authentication. A custom directory needs no manifest and owns its own
layout above this final directory. To switch collections, change the URL.
`avatar_theme` and `avatar_palette` are ignored in custom mode and produce an
informational warning when set away from their defaults.

An invalid custom URL logs a warning and disables avatars only. Set
`avatars_enabled: false` to opt out explicitly while preserving the original
webhook identities.

The source manifest and SVGs live under `assets/avatars/`; the renderer writes
the complete static bundle and published manifest under `pages/v1/`. The Pages
workflow deploys that directory after it reaches `main`. The runtime reads the
bundled manifest for the official catalog but never fetches a remote manifest
or probes either host (ADR-0015).

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
