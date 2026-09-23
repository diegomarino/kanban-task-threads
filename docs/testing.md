# Testing

Start with the unit suite and lint checks, then use the integration layer that
exercises the boundary being changed. Most checks run without agents,
credentials or network access. Live Discord checks are a separate, explicit
step.

## Validation layers

| Layer | Command | Requirements | Coverage |
|---|---|---|---|
| Unit | `./scripts/sandbox test` | uv | Rendering, transport payloads, SQLite state, leases, cursor handling, failure policy and runtime lifecycle |
| Lint | `./scripts/sandbox lint` | uv | Ruff lint and formatting |
| Runtime load | `./scripts/sandbox doctor` | Hermes | Registration through the real plugin loader, in a temporary home with sockets blocked |
| Deferred startup | `path/to/hermes/python scripts/check_startup.py` | Hermes' Python interpreter | Profile scopes, hook-less startup, retry, unload and a local consumer pass |
| Local event flow | `./scripts/sandbox up`, then `./scripts/sandbox task`, then `./scripts/sandbox consume` | Hermes | Real task events consumed through a console transport, with persistent local state |
| Live delivery | `python3 scripts/live_run.py <sandbox-board.db>` | Dedicated test forum and credentials | One consumer pass through the real Discord transport |

The unit and lint commands provision the tool versions pinned by the project.
CI runs the Python version matrix, Hermes compatibility checks and public plugin
scanner. Local checks do not establish that CI or live delivery has passed.

## Registration and deferred startup

Registration and startup are distinct contracts. `register()` installs hooks,
an unload callback and a thread that waits before building the consumer.
Successful plugin validation therefore does not establish that the deferred
build can resolve the runtime's APIs or initialize a consumer.

Run `check_startup.py` with the Python interpreter belonging to the Hermes
installation being tested. It launches a child with a synthetic environment and
temporary homes, and blocks sockets and subprocesses before importing Hermes.
It exercises the actual profile, hydration and secret-scope APIs supplied by
that installation.

The checks cover:

- The process home and a distinct registered profile, including identity
  preservation when an attempt carries another context.
- Profile-file refresh, legitimate launch credentials and isolation under
  multiplexing.
- Deferred startup without hook traffic, retry after a transient failure and
  thread shutdown through the unload callback.
- The real consumer build and an empty pass over temporary SQLite, with HTTP
  replaced by a deterministic response.

Lifecycle checks use an in-memory consumer observer. API-specific checks report
a skip when the selected Hermes installation does not provide that API; the
unit suite also covers optional-module presence, absence and internal errors.
CI runs the startup check against every pinned Hermes version. It also runs
registration validation on the pin that provides `plugins validate`; older
Hermes releases may not expose that CLI command.

These checks do not contact Discord or verify delivery of pending events.

## Local sandbox

Use `scripts/sandbox` for local Hermes commands rather than invoking `hermes`
against an inherited home. Its integration commands set `HERMES_HOME` and
`HERMES_KANBAN_HOME` to the repository's ignored `.sandbox/` directory.

`sandbox up` initializes the local board. `sandbox task` creates a task and
walks it through comment, blocked, unblocked and completed states without an
LLM or worker. `sandbox consume` prints transport operations and stores its
cursor and post mappings locally. Running it again without new events should
produce no new publication operations.

The offline consumer uses `.sandbox/plugin-state.db`. The live check uses
`.sandbox/live-state.db`, keeping their delivery state separate. Sandbox reset
uses the system trash when available and refuses homes that appear to contain
non-sandbox data.

## Fixtures and test doubles

- `FakeHttp` in `tests/conftest.py` records requests and returns canned HTTP
  responses. It exercises transport payloads, headers and error handling.
- `FakeTransport` records publication operations and supplies controlled
  failures and thread metadata for consumer tests.
- `make_board` creates a real SQLite database with the tables the consumer
  reads. State and cursor tests execute SQL rather than mocking it.
- `ConsoleTransport` in `scripts/consume_sandbox.py` prints publication
  operations instead of sending them to Discord.

Contract tests also protect mention suppression, message identity, avatar
asset coverage, manifest/hook agreement, callback signatures and unload
behavior. Prefer assertions on observable behavior and keep external I/O
behind these boundaries.

## Live checks

Live checks require explicit authorization and a dedicated test forum. Verify
the configured webhook's destination before running them: the credentials
determine where requests go.

`live_run.py` reads test credentials from `.env.local`, consumes the supplied
sandbox board using its separate state database, runs one pass and exits. It
can create posts, edit cards and send replies. Do not include it in routine
offline validation or run it against production boards or cursors.
