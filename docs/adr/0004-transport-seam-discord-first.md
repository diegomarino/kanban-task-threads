# ADR-0004 — A small transport seam; never design to the intersection

Status: accepted (2026-09-18)

## Context

Discord is the first transport. A second one (Telegram maps cleanly on paper —
see [docs/transport.md](../transport.md)) may never be built, and an interface
extracted before its second implementation exists is the usual way to get the
interface wrong. Designing to the intersection of two chat products would cost
live relative timestamps, colour, and per-message identity — the three things
that make the card readable — for a transport nobody asked for.

## Decision

Keep a second transport *possible*, never build toward it. The seam is small:
`open_thread`, `edit_card`, `append`, plus optional `set_title`/`archive`-class
operations and a **capability declaration** the renderer queries instead of
assuming a common subset. Everything else stays in the core because none of it
is platform-shaped: cursor consumption, reply-vs-refresh policy, idempotency,
state, retry, truncation.

Nothing in the plugin gives up a Discord capability to fit a hypothetical
platform. The plugin's name and config carry no platform assumptions where
renaming is expensive (ids, setting keys).

## Consequences

- Discord-specific rules (allowed_mentions, truncation limits) are enforced
  *inside* the Discord transport, unforgettable by callers.
- Test doubles implement the seam trivially (fake, console).
- A future transport must publish its capability losses up front — see the
  Telegram appraisal — rather than let an installer discover them.
