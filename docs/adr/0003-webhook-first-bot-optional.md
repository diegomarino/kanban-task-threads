# ADR-0003 — Webhook-first credential; the bot token is optional and degrading

Status: accepted (2026-09-18)

## Context

Everything the card needs — create a forum post, edit its starter message,
reply, per-message identity — a webhook can do. A webhook URL is write-only
and bound to one channel: a leak costs only the ability to post there, which
is the right credential to ask a stranger to configure. But a webhook cannot
rename a thread, archive it, or change its tags — every feature that puts
state *outside* the post body needs a bot token with channel permissions,
which a cautious installer will not grant.

## Decision

The plugin is complete with the webhook alone. A bot token is a **separate,
optional capability**: supplying one additionally unlocks status tags, renames
after title edits, and archiving on completion. The plugin detects and
degrades — it never requires. Capability names (`title_state`, `tags`) gate
every bot call site.

The forum channel is never configured: `GET` on the webhook returns its
`channel_id` — one credential, one source of truth, nothing to disagree.

## Consequences

- Two secrets, one required. Preflight asymmetry: with a bot token, a
  tag-required forum is detected at startup and fails closed with an
  actionable message; without one, the create-time 400 is mapped to the same
  text.
- The thread name is frozen for webhook-only deployments (ADR-0009 makes that
  a feature); status lives in the card's colour and text, which the webhook
  can always rewrite.
