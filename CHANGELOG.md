# Changelog

Format: [Keep a Changelog](https://keepachangelog.com); versioning: semver,
`v`-prefixed git tags. Releases that change the stored state (the plugin's
SQLite schema) always describe the migration in their notes — columns are
added in place, idempotently; deployed state files are never recreated.

## [Unreleased]

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
