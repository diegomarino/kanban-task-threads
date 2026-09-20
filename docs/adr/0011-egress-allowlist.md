# ADR-0011 — The view is an egress allowlist; absence is the safe default

Status: accepted (2026-09-20, boundary present since the design)

## Context

Everything this plugin publishes leaves the machine permanently, visible to
everyone who can read the channel. The task row has ~35 columns; some are the
feature (titles, block reasons, summaries — arbitrary agent output, published
because visibility *is* the product), some are internals, and some are
outright egress hazards (results and failure text can echo secrets or stack
traces; the workspace path exposes usernames and layout).

## Decision

Templates render only what `build_view` exposes — an **allowlist**, enforced
structurally by the safe formatter (ADR-0006): a field not in the view is
unreachable from any template, malicious or typo'd. Every addition to the view
is therefore an explicit egress decision. `workspace_path` ships behind an
opt-in flag, off by default. `result` and `last_failure_error` stay out; if
ever wanted, they must be opt-ins of the same shape. Comment *text* is
published as a bounded excerpt in the reply (whitespace-collapsed, truncated to
`comment_excerpt_chars`, default 180, with a link to the full text when cut) —
recovered near-deterministically from `task_comments` via
`(task_id, author, length, created_at)`, falling back to the author-only line
on any mismatch, never a guess. Deployments that must not publish comment
bodies set `comment_excerpt_chars: 0`.

One separate, non-template path publishes task content: when a prerequisite
thread opens, the best-effort announcement sent to existing dependent threads
includes a whitespace-collapsed excerpt of the prerequisite body, truncated to
140 characters. It currently has no opt-out. This is intentionally distinct
from the card-view allowlist and from configurable comment excerpts.

## Consequences

- The published surface is auditable in one function.
- Dependency announcements are an explicit egress exception implemented in
  the consumer, not a field exposed to card templates.
- Reply payloads are looser by design (scalar passthrough) because event
  payloads are already curated by the board's own writers.
- The exclusion table lives in [docs/card-and-log.md](../card-and-log.md) and
  must move with any view change.
