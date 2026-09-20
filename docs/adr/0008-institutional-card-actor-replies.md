# ADR-0008 — The card is the board speaking; replies are signed by the actor

Status: accepted (2026-09-19)

## Context

A webhook message's `username` is fixed at creation and cannot be changed by
`PATCH` — probed live: content changed, signature did not. It is the only
non-editable property of a message. The card is rewritten for the task's whole
life; the replies are created fresh per event.

## Decision

**The card carries the webhook's own stable name, never a person's.** Signing
it with the assignee would freeze the *first* assignee forever — one
reassignment and the post is attributed to someone who no longer holds it,
uncorrectably. Do not "fix" this by passing `username` on the create: that is
how the bug is built (a test pins it).

**Replies are signed by the actual actor**, in three tiers: a named actor in
the payload wins (`author`, `implementer` for review requests, `reviewer` for
requested changes); the assignee signs the worker's own actions (blocked,
unblocked, completed); and system observations — crashed, timed out,
reclaimed, gave up, archived — are **unsigned**, falling to the institutional
name, because the worker didn't say it crashed: the board observed it. Signing
a crash or a reviewer's verdict as the assignee fabricates attribution.

## Consequences

- Reassignments, review lanes and reclaims never mis-attribute the card.
- An unknown actor degrades to the board's voice, never to a guess.
