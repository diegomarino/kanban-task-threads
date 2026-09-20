#!/usr/bin/env bash
# demo_updates — staggered story beats for the screenshot demo, so thread
# timestamps read as a real working session instead of one batch publish.
# Runs ~11 minutes (sleeps of 2-3 min between beats); each beat writes real
# board events and runs one live consumer pass. Companion of demo_scenario.sh.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DB="$REPO/.sandbox/kanban.db"

env_run() { HERMES_HOME="$REPO/.sandbox" HERMES_KANBAN_HOME="$REPO/.sandbox" "$@"; }
by_title() { python3 -c "
import sqlite3, sys
print(sqlite3.connect('$DB').execute(
    'SELECT id FROM tasks WHERE title LIKE ?', (sys.argv[1] + '%',)).fetchone()[0])" "$1"; }
publish() { python3 "$REPO/scripts/live_run.py" "$DB"; }
beat() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$1"; }

STRIPE=$(by_title "Rotate the Stripe keys")
BILLING=$(by_title "Migrate billing webhooks")
DASH=$(by_title "Dashboards for dispatcher health")

beat "billing: progress comment (Coder)"
HERMES_PROFILE=Coder env_run hermes kanban comment "$BILLING" \
    "producers migrated too — replaying yesterday's failed deliveries now" >/dev/null
publish

sleep 150
beat "stripe: vault access approved (MarSan)"
HERMES_PROFILE=MarSan env_run hermes kanban comment "$STRIPE" \
    "vault access approved — restricted keys issued, handing over to rotation" >/dev/null
publish

sleep 160
beat "stripe: unblocked and running"
env_run hermes kanban unblock "$STRIPE" >/dev/null
python3 -c "
import sqlite3, time
conn = sqlite3.connect('$DB'); now = int(time.time())
conn.execute(\"UPDATE tasks SET status='running', started_at=?, last_heartbeat_at=? WHERE id=?\",
             (now, now, '$STRIPE'))
conn.commit()"
HERMES_PROFILE=Coder env_run hermes kanban comment "$STRIPE" \
    "rotating: staging first, canary watching the webhook error rate" >/dev/null
publish

sleep 140
beat "dashboards done; billing wraps up"
# `complete` refuses a raw-SQL 'running' (no claim/run behind it): return the
# task to ready first — the card only shows the final 'done' either way.
uncook() { python3 -c "
import sqlite3
conn = sqlite3.connect('$DB')
conn.execute(\"UPDATE tasks SET status='ready', last_heartbeat_at=NULL WHERE id=?\", ('$1',))
conn.commit()"; }
uncook "$DASH"
env_run hermes kanban complete "$DASH" \
    --summary "dispatcher health boards live; alerts wired to the on-call topic" >/dev/null
HERMES_PROFILE=Coder env_run hermes kanban comment "$BILLING" \
    "replays finished — error rate back to baseline, closing out" >/dev/null
publish

sleep 170
beat "stripe: rotated in prod"
uncook "$STRIPE"
env_run hermes kanban complete "$STRIPE" \
    --summary "rotated in production; old keys revoked after a 24h grace window" >/dev/null
publish

beat "done — threads now span ~11 minutes of organic history"
