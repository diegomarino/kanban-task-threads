# Testing: layers, cheapest first

The whole state machine is reachable without agents, credentials or network,
because every kanban lifecycle verb writes real `task_events` rows and a
Hermes home is just a directory and two environment variables. The layers,
each proving what the previous one cannot:

| Layer | Command | Needs | Proves | Cannot reach |
|---|---|---|---|---|
| Unit | `./scripts/sandbox test` | uv | rendering, truncation, template rejection, store CAS/lease/fencing, the consumer's whole failure policy, runtime lifecycle, startup classification, the entry-point contract | Hermes |
| Runtime load | `./scripts/sandbox doctor` | hermes | `register()` loads through the real plugin loader (temp home, sockets blocked) | the network |
| End-to-end, no network | `./scripts/sandbox task && ./scripts/sandbox consume` | hermes | real event rows → one post, replies in order, durable cursor (second run silent), via a console transport | Discord |
| Live, bounded | `scripts/live_run.py` | a test forum's webhook | the real webhook path: create, in-place edit, reply | — |

Run the suite through `./scripts/sandbox test`; `uv` provisions the pinned
Python and pytest environment consistently for contributors and CI.

## The sandbox

`HERMES_HOME` may point at a real deployment, so **no bare `hermes` command is
ever run from this repo**. `scripts/sandbox` overrides
`HERMES_HOME`/`HERMES_KANBAN_HOME` on
every call, refuses to operate on anything outside `.sandbox/`, and refuses to
delete anything that looks like a real home. `sandbox task` walks a task
through comment → blocked → unblocked → completed — five real event rows, no
LLM, no worker.

## What the doubles are

- `FakeHttp` (tests/conftest.py) — records `(method, url, body)` and replays
  canned responses; headers kept separately. What `DiscordTransport` is tested
  against; the allowed-mentions and truncation rules are asserted on every
  recorded body.
- `FakeTransport` — records seam operations and raises queued errors per
  operation; what the consumer's failure policy is tested against.
- `make_board` — a real SQLite file with the real tables' shape (`tasks`,
  `task_events`, `task_links`); the consumer is tested against actual SQL, not
  mocks of it.
- `ConsoleTransport` (scripts/consume_sandbox.py) — the seam printed to
  stdout; what `sandbox consume` runs.

## Tests that are pins, not TDD

Most of the suite was written red-first. A few tests exist instead to make a
*decision* expensive to reverse accidentally — they passed the moment they
were written, on purpose:

- cards never carry `username` (the signature rule; the tempting "fix" builds
  the frozen-assignee bug),
- every call body carries `allowed_mentions: {"parse": []}`,
- the manifest's `provides_hooks` equals exactly what `register()` registers
  (`validate` fails on drift),
- every registered hook callback accepts arbitrary `**kwargs` (`doctor` errors
  on drift),
- after the unload callback, no plugin thread survives and the lease is free
  (the orphan-consumer regression).

## The live layer is bounded by construction

`live_run.py` runs **one** consumer pass and exits — run, observe, stop. The
webhook it reads is bound to one throwaway forum, so it cannot reach a
production channel even by mistake. Pointing the plugin at production is a
configuration act by the operator, not something any script here does.
