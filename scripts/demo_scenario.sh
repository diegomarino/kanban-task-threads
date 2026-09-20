#!/usr/bin/env bash
# demo_scenario — a photogenic board for README screenshots, published live.
#
# Builds a realistic mix in the sandbox (an epic with children, a running task
# with a live heartbeat, a needs-human block, a review, a completed one) and
# runs the real consumer against the test forum once. Screenshot, then wipe
# with wipe_test_forum.py.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

"$REPO/scripts/sandbox" reset || true
"$REPO/scripts/sandbox" up >/dev/null

env_run() { HERMES_HOME="$REPO/.sandbox" HERMES_KANBAN_HOME="$REPO/.sandbox" "$@"; }
create() { env_run hermes kanban create "$1" --assignee "$2" 2>/dev/null | sed -n 's/^Created \([a-z_0-9]*\).*/\1/p'; }

EPIC=$(create "Q4 observability revamp" MarSan)
C1=$(create "Wire OpenTelemetry traces into the gateway" Coder)
C2=$(create "Dashboards for dispatcher health" Coder)
C3=$(create "Alert routing for stale workers" Coder)
RUN=$(create "Migrate billing webhooks to the v2 API" Coder)
BLK=$(create "Rotate the Stripe keys" Coder)
REV=$(create "Refactor the retry queue into its own module" Coder)

env_run hermes kanban comment "$RUN" "halfway through: consumers migrated, producers next" >/dev/null
env_run hermes kanban block "$BLK" --kind needs_input "need the new keys from the vault owner — access request pending" >/dev/null
env_run hermes kanban comment "$REV" "ready for eyes: 14 files, all tests green" >/dev/null
env_run hermes kanban complete "$C1" --summary "traces flowing end to end; sampled at 10%" >/dev/null

python3 - "$REPO/.sandbox/kanban.db" "$EPIC" "$C1" "$C2" "$C3" "$RUN" "$REV" <<'PY'
import sqlite3, sys, time
db, epic, c1, c2, c3, run, rev = sys.argv[1:]
conn = sqlite3.connect(db)
now = int(time.time())
for child in (c1, c2, c3):
    # prerequisites gate the epic (ADR-0012): subtask = parent, epic = child
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (child, epic))
# states the CLI can't reach without a dispatcher: running with a live heartbeat, review
for tid in (run, c2, epic):
    conn.execute(
        "UPDATE tasks SET status='running', started_at=?, last_heartbeat_at=? WHERE id=?",
        (now - 1800, now, tid))
conn.execute("UPDATE tasks SET status='review' WHERE id=?", (rev,))
bodies = {
    epic: "Umbrella for the Q4 observability push: traces, dashboards, alerting.",
    c1: "Instrument the gateway with OTel; propagate context through the dispatcher.",
    c2: "Grafana boards for dispatcher health: tick latency, reclaims, breaker trips.",
    c3: "Route stale-worker alerts to on-call; escalate after two silent heartbeats.",
    run: "Move billing webhook consumers and producers to the v2 API before the freeze.",
    rev: "Extract retry logic into its own module with tests; no behavior changes.",
}
for tid, body in bodies.items():
    conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, tid))
conn.commit()
print("scenario ready:", epic, "->", c1, c2, c3, "|", run, rev)
PY

python3 "$REPO/scripts/live_run.py" "$REPO/.sandbox/kanban.db"
