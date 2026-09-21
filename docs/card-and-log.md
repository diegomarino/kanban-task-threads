# The card and the log

The design in one sentence: a forum post's body is a **live status card
rewritten in place**, and its replies are the **append-only log** — the body
answers *"now"*, the thread answers *"how we got here"* (ADR-0001).

## Why the split exists

A flat channel interleaves every task into one stream; a thread of raw updates
per task is only half an answer, because at the thirtieth event *"what is
happening?"* means reading thirty messages and reconstructing state in your
head. So state and history are separated into the two places Discord gives a
forum post: the starter message (editable forever) and the replies
(append-only by nature).

## The card is a pure function of board state

`view.build_view` takes the task row and produces a flat dict of strings;
`render.render_card` turns it into a transport-neutral `Card`. Nothing is
accumulated from event payloads — the card can always be re-derived from the
database, which is what makes "rewrite in place" safe to do at any time, from
any process, after any crash.

What it shows: status (as text *and* embed colour), assignee, its dependency links
(ADR-0012), elapsed time as `<t:…:R>` (Discord renders it client-side,
so it stays true without re-edits), where the work is (workspace kind, branch;
the absolute path only on opt-in), and what is being waited on — the block
kind and its reason, which is the field the plugin exists for.

What it deliberately omits from the **card**: the task body and comment text.
Replies may publish a bounded comment excerpt, recovered conservatively from
`task_comments`; set `comment_excerpt_chars: 0` for author-only replies
(ADR-0011).

## The status vocabulary covers the whole state machine

Hermes has nine task statuses and four block kinds. Two cases need care:

- **`dependency` blocks route to `todo`, not `blocked`** — a renderer keyed on
  `status == "blocked"` never shows a dependency wait. `resolve_status_key`
  maps `todo + dependency` to its own `dependency_wait` entry.
- **`stale` is a first-class state, not an absence.** A worker killed with
  `SIGKILL` runs no exit path, so no completion event ever arrives. The card
  computes staleness from the heartbeat (`running` + heartbeat older than
  `stale_after_seconds`) rather than waiting for a signal that will never
  come. Note the consequence: `stale` appears on the card but never as a
  reply, because it is a render-time computation, not a `task_events` row —
  the card is state, the log is events.

## The signature rule

**The card is the board speaking; the replies are who did what.**

A webhook message's `username` is fixed at creation and cannot be changed by
`PATCH` — it is the only non-editable property of the message (probed live:
content changed, signature did not). The card is rewritten for the task's
whole life, so signing it with the assignee would freeze the *first* assignee
forever; one reassignment and the post is attributed to whoever no longer
holds it, uncorrectably. Hence: cards carry the webhook's own stable name;
attribution goes in the replies, which are created fresh per event and can
each carry the profile that caused it. A test pins that `open_thread` and
`edit_card` never send `username`.

Reply attribution follows the actual actor, in three tiers: a **named actor**
in the payload wins (`author` for comments, `implementer` for a review
request, `reviewer` for requested changes); the **assignee** signs the
worker's own actions (blocked, unblocked, completed); and **system
observations** — crashed, timed out, reclaimed, gave up, archived — are
unsigned because the worker didn't say it crashed: the board observed it.
Without message avatars they fall to the webhook's institutional name. With
avatars enabled they use the explicit typed system identity
`system · {message_type}` so Discord can display each event avatar separately.
Signing a crash or a reviewer's verdict as the assignee would fabricate
attribution.

A cousin of the signature rule governs the **thread name**: a webhook can
never rename a thread, so the name carries only what never changes — the
title as of creation plus the task id (`fix the build · t_ab12`). The id is
the stable key: duplicate titles stay distinguishable, and reconciling an
unknown-outcome create is a deterministic forum search. Truncation eats the
title, never the id. Status lives in the card, where it can be rewritten.

## The view boundary: an allowlist, not an oversight

The task row has ~35 columns; the view exposes a deliberate subset, because
ADR-0006's safe formatter renders over a flat dict — **a template can only leak what
`build_view` exposes**, no matter how it is written. Absence is the safe
default, so every addition is an egress decision. The current boundary:

**Exposed:** title, status (with stale computed), assignee, since,
block kind/reason, branch, workspace kind, prerequisite/dependent rollups,
priority, created_by, project_id, max_runtime — plus workspace_path behind its
opt-in flag.

**Excluded from the card view, with reasons:**

| Fields | Why they stay out |
|---|---|
| `body` | Lives on the board (ADR-0001); duplicating it guarantees divergence |
| `result`, `last_failure_error` | Arbitrary agent output that is *not* the feature — may echo secrets or stack traces (ADR-0011). If ever wanted, it must be an opt-in like `workspace_path` |
| `claim_lock`, `claim_expires`, `worker_pid`, `current_run_id`, `session_id`, `idempotency_key`, `consecutive_failures` | Dispatcher internals; operational noise |
| `model_override`, `provider_override`, `reasoning_effort`, `skills`, `goal_*`, `workflow_*`, `completion_contract`, `tenant` | Execution configuration, not visible state |

The consumer has one explicit egress path outside this card-view allowlist:
a prerequisite-opened announcement includes a whitespace-collapsed excerpt of
the prerequisite task body, truncated to 140 characters. It currently has no
opt-out (ADR-0011).

Reply templates are the looser layer, on purpose: `render_reply` passes
through every *scalar* in the event payload (nested values like
`review_requested`'s `artifacts` list are dropped), so new payload fields
become template-addressable without a plugin release.

Every reply ends with a `[#]` masked link jumping to the thread's starter —
the live card — so any point in the history is one click from "and where is it
now?". Masked links render in webhook content (apps may; humans may not).

## Which events earn a reply

One reply per event worth a permanent record: comments, blocks and unblocks,
completion, review requests and change requests, give-ups, crashes, timeouts,
reclaims, archival. The set is configurable (`reply_on`); the default errs
toward "everything a human would want in the record". Task creation is *not* a
reply — the post opening is the record of it.
