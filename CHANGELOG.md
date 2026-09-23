# Changelog

Format: [Keep a Changelog](https://keepachangelog.com); versioning: semver,
`v`-prefixed git tags. Releases that change the stored state (the plugin's
SQLite schema) always describe the migration in their notes — columns are
added in place, idempotently; deployed state files are never recreated.

## [0.4.1](https://github.com/diegomarino/kanban-task-threads/compare/v0.4.0...v0.4.1) (2026-09-23)


### Bug Fixes

* **runtime:** support Hermes without launch profile policy ([#12](https://github.com/diegomarino/kanban-task-threads/issues/12)) ([d779afb](https://github.com/diegomarino/kanban-task-threads/commit/d779afb86e4f493ba0564ad1af00fd02f0f58b18))

## [0.4.0](https://github.com/diegomarino/kanban-task-threads/compare/v0.3.0...v0.4.0) (2026-09-22)


### Features

* **avatars:** provide zero-config official assets ([#10](https://github.com/diegomarino/kanban-task-threads/issues/10)) ([6945f9e](https://github.com/diegomarino/kanban-task-threads/commit/6945f9edd796769d4f5dc7d5776151ba5527060b))
* **release:** add screenshots and Hermes catalog handoff ([#7](https://github.com/diegomarino/kanban-task-threads/issues/7)) ([f7bd4df](https://github.com/diegomarino/kanban-task-threads/commit/f7bd4df2f724b0dcbc1cf45f030ecb93ad371b3f))

## [0.3.0](https://github.com/diegomarino/kanban-task-threads/compare/v0.2.3...v0.3.0) (2026-09-21)


### Features

* **discord:** add themed webhook avatars ([#6](https://github.com/diegomarino/kanban-task-threads/issues/6)) ([5634821](https://github.com/diegomarino/kanban-task-threads/commit/56348214efef2440a0db9753cffb45bafc50a1ae))


### Bug Fixes

* **discord:** reconcile and provision forum metadata ([#4](https://github.com/diegomarino/kanban-task-threads/issues/4)) ([22161ca](https://github.com/diegomarino/kanban-task-threads/commit/22161cafde30dcf76e880be7b99392a6d67732b8))

## [Unreleased]

### Added
- Optional 96 px Phosphor message avatars for the starter and every reply
  event type, with global `duotone`/`fill`/`bold` themes and opinionated
  `colored`/`black`/`white` palettes. A versioned manifest and official GitHub
  Pages workflow publish the static bundle (ADR-0015); the runtime adds no
  message images, attachments, remote manifest fetches, or mutable upstream
  hotlinks. To prevent Discord grouping different avatars under one header,
  enabled replies use `{profile_id} · {message_type}` (or
  `system · {message_type}` when unsigned), bounded to Discord's 80-character
  webhook-name limit without truncating the message type.

## [0.2.3] — 2026-09-21

### Fixed
- Disabling or reloading the plugin no longer hangs while a consume pass is in
  flight. `shutdown()` acquired the pass lock *before* setting its stop flag,
  so an unload that raced a pass waited for every Discord call that pass had
  left — many tasks x a ten-second HTTP timeout each — with the running pass
  never told to stop and the documented ten-second join unreachable. The stop
  flag is now set first and the bounded join does the waiting; the loop still
  rechecks it before each pass, so none begins after unload.

## [0.2.2] — 2026-09-20

### Fixed
- Every profile that loads the plugin is now a lease candidate: the consumer
  thread starts at register time instead of waiting for a hook kick (ADR-0013).
  Kanban hooks fire only in the process holding Hermes' singleton dispatcher
  lock, so with multiple profiles the publisher was effectively elected by
  gateway boot order and the `consume:<board>` lease had nobody to fail over
  to. The thread waits one poll interval before its first build, so
  `register()` still performs no I/O and `hermes plugins validate` is
  unaffected. Cold start on a hook-less profile is one interval later.
- A deferred startup (`RetryableStartup`) no longer kills the consumer thread.
  It refreshes the registered profile's secret scope and retries every poll
  interval — sooner if a kick arrives, without letting an empty dispatcher
  context change profile identity — and warns once per distinct reason instead
  of on every attempt. Unload also rechecks its stop signal after an in-flight
  preflight, so it cannot begin a new consume pass while shutting down.
  Previously a transient failure stranded the profile until the next gateway
  restart, which is unreachable for profiles that receive no kanban hooks.

## [0.2.1] — 2026-09-20

### Fixed
- CI now validates against an exact Hermes commit using the supported editable
  checkout flow, alongside the Python 3.11–3.13 matrix and HOL scanner.
- Security fixtures no longer resemble a hardcoded Discord credential, so the
  public marketplace scanner passes without a false high-severity finding.
- Direct GitHub installs pass Hermes' install-time scanner without `--force`;
  the README explains how to review any future `CAUTION` verdict without ever
  bypassing `DANGEROUS` findings.

## [0.2.0] — 2026-09-20

### Added
- The card carries a plain one-line `content` (`● status · assignee`) so the
  forum-list preview shows state instead of "Click to see attachment" — with
  or without a bot token.
- Every reply ends with a `[#]` anchor: the task's dashboard page when
  `dashboard_url` is configured (supports a `{task_id}` placeholder), else an
  in-app jump to the live card. `dashboard_url` takes `{task_id}` and
  `{board}`; a literal IP in it logs an advisory (links are forever).
- Prerequisite-opened announcements link to the new thread and carry the
  assignee and a bounded body excerpt.
- CLI mirror comments ("BLOCKED: …" twins of the semantic event) are
  suppressed — one act, one reply.
- Review replies carry their payload (`→ reviewer`, summary, reasons); all
  reply separators are conditional — no more dangling "— ".
- Comment replies quote a bounded excerpt of the comment's text, with a
  `full comment` link when truncated (ADR-0011). `comment_excerpt_chars: 0`
  restores the old author-only line.

### Fixed
- The epic hub now follows `task_links`' real semantics — parents are
  prerequisites (ADR-0012). Cards say `depends on:`/`unlocks:`; the template
  fields `{parent*}`/`{children*}` were replaced by `{depends*}`/`{unlocks*}`
  before any release shipped them.

### Migration
- Existing plugin state databases are upgraded in place with the new columns;
  no operator action or state recreation is required.

## [0.1.0] — 2026-09-20

Initial release.

### Added
- A Discord forum thread per kanban task: live status card rewritten in place,
  replies as the append-only log (ADR-0001), full status vocabulary including
  computed `stale` and dependency waits.
- Durable consumption of `task_events` past a fenced, monotonic per-task
  cursor; hooks as latency kicks only (ADR-0002). Concurrent consumers are
  excluded, while replies remain at-least-once across a crash after send.
- Failure policy (ADR-0007): unknown-outcome creates held for operator
  reconciliation, dead letters, tombstones, per-task 429 backoff,
  destination freeze, dirty-card repaint, two-way stale sweep.
- Optional bot-token extras (ADR-0003): status tags (six-name convention),
  thread renames after title edits, archive on completion.
- Epic hub: parent cards carry a live children rollup; child threads link back.
- Operator reconciliation verbs: `attention`, `clear`, `adopt`, `rearm`,
  `recreate`.
- Safe operator templates (ADR-0006) over an egress-allowlisted view
  (ADR-0011).
