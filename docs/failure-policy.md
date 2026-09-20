# Failure policy

Consuming a durable event log does not make Discord reliable; it only
guarantees the plugin never *loses* an event. What happens when Discord
misbehaves is decided case by case in `consumer.py`, and none of it is
configurable — this is correctness, not taste (ADR-0007, ADR-0006).

## The create protocol: unknown outcomes are sacred

Opening a forum post is the one non-idempotent operation: a blind retry can
mint a second thread for the same task, permanently.

1. **Before** the call, the attempt is recorded (`posts.pending_create_at`).
2. On success, the row gets its thread/message ids and the pending mark clears.
3. On a **permanent 4xx**, the outcome is known (nothing was created) and the
   task is dead-lettered with the response as detail.
4. On a **network error or any 5xx**, the outcome is *unknown* — a proxy
   answering 502 does not prove Discord created nothing. The pending mark
   stays, the task is reported as an error on **every pass**, and nothing is
   retried until an operator reconciles (checks the forum, then clears or
   completes the entry). Loud on purpose: a silent skip here is how you get a
   thread nobody can find or two threads nobody can explain.

## The full table

| Failure | Action | Store state | Log level |
|---|---|---|---|
| Create, outcome unknown (network / 5xx) | Recorded pre-call; never blindly retried; reported every pass | `pending_create_at` | error |
| Permanent 4xx anywhere | Dead-letter: stop publishing for the task, keep the response as detail | `state='dead_letter'` | error |
| …of which Discord code 40067 | Same, plus the actionable hint: this forum requires a tag — set `discord_applied_tag_ids` | `state='dead_letter'` | error |
| 404, or 400 with Discord code 10003, on edit/append | A human deleted the post: tombstone, stop publishing; recreation is operator-only, never automatic | `state='tombstone'` | error |
| 429 | Per-task backoff from `retry_after`; other tasks unaffected | `backoff_until`, `card_dirty` | — |
| 5xx / network on a reply or card edit | Retry next pass (position did not advance past the failed event) | `card_dirty` | warning |
| Destination changed | Freeze: never replay stale ids against a webhook that cannot edit them | — (computed) | error |
| Lease lost mid-pass | Abort before the next task's side effects | — | warning |
| One task throws anything | Its error is reported; the rest of the batch continues | — | error |

## Poison events

A 400 caused by an over-length field would repeat identically forever, so it
can never happen: rendering truncates deterministically to Discord's limits
(2000 chars of content, 4096 of embed description, 100 of thread title) before
anything is sent. If a 400 arrives anyway (a new Discord rule, a malformed
template value), it is permanent by definition and dead-letters the task
rather than retrying.

## The dirty card

The reply loop advances the per-task position *before* the trailing card
refresh. If only the `edit_card` fails transiently, every event is already
consumed — a task that then goes quiet (e.g. parked in `review`) would keep a
stale card forever, because no future event would bring it back into a batch.
Hence `posts.card_dirty`: any transient publish failure marks it, and every
pass repaints dirty cards **even when the batch is empty**, clearing the flag
on success.

## Tombstones, dead letters, freezes — three different "stops"

- **Tombstone** (404, or 400 with Discord code 10003): a *human* acted on the post. The plugin respects that
  permanently; only an operator can re-arm.
- **Dead letter** (permanent 4xx): *Discord* rejects the payload or the
  operation. Retrying is pointless; the detail says why.
- **Freeze** (destination mismatch): the *operator* re-pointed the plugin at a
  different webhook/forum while the state DB still maps tasks to the old one.
  Publishing would 404 against ids the new webhook cannot address and be
  mistaken for deletion — so the task is skipped with an explicit "migrate or
  clear" error instead. An operational migration must never be punished as if
  someone deleted the post.

None of the three delete anything: terminal is not final on this board
(review→running, done→archived and reclaims all reanimate tasks), so state
rows are kept as compact records, never pruned on "terminal".

## The operator's verbs (`kanban_task_threads/reconcile.py`)

Everything the policy defers to a human is operable without hand-written SQL,
via `scripts/reconcile.py <state.db> …` (in development: `sandbox reconcile`):

| Verb | For | Effect |
|---|---|---|
| `attention` | — | list every row waiting on an operator, across boards |
| `clear` | unknown create, thread confirmed absent on the forum | forget the attempt; the next pass re-creates from the task's durable position |
| `adopt <thread> <msg>` | unknown create, thread found on the forum (search the task id — it is in the thread name) | attach the ids, go live |
| `rearm` | tombstone / dead letter, deliberately revived | keep the thread, resume with future events |
| `recreate` | parked post, fresh start wanted | drop the row; a new post opens (future events only) |

None of them touch Discord; they edit the state DB and the consumer does the
rest on its next pass. `clear` refuses a row that has a thread (that would
leak it) — that situation is an `adopt` or a `recreate`, and the error says so.

## Failure is reported, not swallowed

Every branch above lands in the pass report and is emitted through the stdlib
logger — `error` for what needs an operator (unknown creates, dead letters,
tombstones, freezes), `warning` for what the next pass will retry on its own.
A plugin that has stopped posting must be distinguishable from a quiet board.
