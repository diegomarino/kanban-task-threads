"""Plugin-owned durable state (ADR-0005).

The task → post mapping and the consumption cursor are *board* state, so they
live in a SQLite file the plugin owns at a board-shared path — never in
`ctx.state`, which resolves per profile while the board is shared, has no
delete, and races on read-modify-write across get/set.

Cross-process mutual exclusion comes from SQLite's writer lock: the cursor
advances by compare-and-swap and the consumer runs under a fenced lease, both
arbitrated by `BEGIN IMMEDIATE`. Replies remain at-least-once across a crash
between send and cursor advance.

Terminal is not final (ADR-0005): tombstones and dead letters are rows, not
deletions, so a reanimated task cannot silently re-create a post a human
removed or an operator parked.
"""

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    board TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    board TEXT NOT NULL,
    task_id TEXT NOT NULL,
    thread_id TEXT,
    message_id TEXT,
    destination TEXT,
    state TEXT NOT NULL DEFAULT 'live',      -- live | tombstone | dead_letter
    detail TEXT,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    backoff_until INTEGER NOT NULL DEFAULT 0,
    pending_create_at INTEGER,               -- set: a create with unknown outcome
    card_dirty INTEGER NOT NULL DEFAULT 0,   -- set: last card refresh failed
    last_status_key TEXT,                    -- what the card currently shows
    last_tag TEXT,                           -- forum tag currently applied
    last_name TEXT,                          -- thread name currently set
    thread_archived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (board, task_id)
);
CREATE TABLE IF NOT EXISTS leases (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    token INTEGER NOT NULL DEFAULT 0
);
"""


class StateStore:
    """The plugin's durable memory: cursor, task→post mapping, fenced leases.
    One SQLite file per board; every method is safe to call from any process
    (WAL, busy timeouts, explicit BEGIN IMMEDIATE where races matter)."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # timeout + busy_timeout: two processes open this file concurrently by
        # design; without them, even the WAL pragma (a write) fails instantly
        # with "database is locked" instead of waiting its turn.
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # explicit transactions only
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._set_wal()
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _set_wal(self) -> None:
        """WAL is a property of the *file*, set once by whoever creates it.
        The journal-mode switch returns SQLITE_BUSY without consulting the
        busy handler (waiting there could deadlock), so a concurrent opener
        retries briefly and accepts an already-WAL database as done."""
        for _ in range(200):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                row = self._conn.execute("PRAGMA journal_mode").fetchone()
                if row and str(row[0]).lower() == "wal":
                    return
                time.sleep(0.05)
        raise sqlite3.OperationalError("could not switch the state DB to WAL: persistently locked")

    def _migrate(self) -> None:
        """CREATE IF NOT EXISTS never adds columns to an existing table, and
        deployed state DBs are never recreated — so new columns are added in
        place, idempotently (duplicate-column errors mean 'already there')."""
        for ddl in (
            "ALTER TABLE posts ADD COLUMN card_dirty INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE posts ADD COLUMN last_status_key TEXT",
            "ALTER TABLE posts ADD COLUMN last_tag TEXT",
            "ALTER TABLE posts ADD COLUMN last_name TEXT",
            "ALTER TABLE posts ADD COLUMN thread_archived INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE leases ADD COLUMN token INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise

    # --- cursor ---------------------------------------------------------------

    def get_cursor(self, board: str) -> int:
        row = self._conn.execute(
            "SELECT last_event_id FROM cursor WHERE board = ?", (board,)
        ).fetchone()
        return row["last_event_id"] if row else 0

    def advance_cursor(self, board: str, *, old: int, new: int) -> bool:
        """Compare-and-swap: only the writer that saw `old` moves the cursor."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO cursor (board, last_event_id) VALUES (?, 0)", (board,)
            )
            moved = (
                self._conn.execute(
                    "UPDATE cursor SET last_event_id = ? WHERE board = ? AND last_event_id = ?",
                    (new, board, old),
                ).rowcount
                == 1
            )
            self._conn.execute("COMMIT")
            return moved
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    # --- lease ----------------------------------------------------------------

    def acquire_lease(self, name: str, holder: str, *, now: int, ttl: int) -> int | None:
        """Returns a fencing token, or None if someone else holds the lease.

        The token increments on every acquisition, so two acquisitions are
        always distinguishable — including a same-process reload that reuses
        the same holder string."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT holder, expires_at, token FROM leases WHERE name = ?", (name,)
            ).fetchone()
            if row and row["expires_at"] > now and row["holder"] != holder:
                self._conn.execute("COMMIT")
                return None
            token = (row["token"] if row else 0) + 1
            self._conn.execute(
                "INSERT OR REPLACE INTO leases (name, holder, expires_at, token) "
                "VALUES (?, ?, ?, ?)",
                (name, holder, now + ttl, token),
            )
            self._conn.execute("COMMIT")
            return token
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def renew_lease(self, name: str, holder: str, token: int, *, now: int, ttl: int) -> bool:
        """Extend the lease iff (holder, token) still own it. False means it
        was stolen: the caller must stop publishing immediately."""
        return (
            self._conn.execute(
                "UPDATE leases SET expires_at = ? WHERE name = ? AND holder = ? AND token = ?",
                (now + ttl, name, holder, token),
            ).rowcount
            == 1
        )

    def release_lease(self, name: str, holder: str, token: int) -> None:
        # Release by expiring, not deleting: the row keeps the token counter
        # monotonic across acquisitions (uniqueness is what fencing rests on).
        self._conn.execute(
            "UPDATE leases SET expires_at = 0 WHERE name = ? AND holder = ? AND token = ?",
            (name, holder, token),
        )

    # --- posts ----------------------------------------------------------------

    def get_post(self, board: str, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM posts WHERE board = ? AND task_id = ?", (board, task_id)
        ).fetchone()
        return dict(row) if row else None

    def begin_create(self, board: str, task_id: str, *, now: int) -> None:
        """Record the attempt *before* the call (ADR-0007): a create whose response
        is lost is an unknown outcome, never a blind retry."""
        self._conn.execute(
            "INSERT INTO posts (board, task_id, pending_create_at) VALUES (?, ?, ?) "
            "ON CONFLICT(board, task_id) "
            "DO UPDATE SET pending_create_at = excluded.pending_create_at",
            (board, task_id, now),
        )

    def complete_create(
        self, board: str, task_id: str, *, thread_id: str, message_id: str, destination: str
    ) -> None:
        self._conn.execute(
            "UPDATE posts SET thread_id = ?, message_id = ?, destination = ?, "
            "pending_create_at = NULL WHERE board = ? AND task_id = ?",
            (thread_id, message_id, destination, board, task_id),
        )

    def clear_pending(self, board: str, task_id: str) -> None:
        self._conn.execute(
            "UPDATE posts SET pending_create_at = NULL WHERE board = ? AND task_id = ?",
            (board, task_id),
        )

    def mark_tombstone(self, board: str, task_id: str, detail: str) -> None:
        self._set_state(board, task_id, "tombstone", detail)

    def mark_dead_letter(self, board: str, task_id: str, detail: str) -> None:
        self._set_state(board, task_id, "dead_letter", detail)

    def _set_state(self, board: str, task_id: str, state: str, detail: str) -> None:
        self._conn.execute(
            "UPDATE posts SET state = ?, detail = ?, pending_create_at = NULL "
            "WHERE board = ? AND task_id = ?",
            (state, detail, board, task_id),
        )

    def set_card_dirty(self, board: str, task_id: str, dirty: bool) -> None:
        self._conn.execute(
            "UPDATE posts SET card_dirty = ? WHERE board = ? AND task_id = ?",
            (1 if dirty else 0, board, task_id),
        )

    def dirty_tasks(self, board: str) -> list:
        return [
            row["task_id"]
            for row in self._conn.execute(
                "SELECT task_id FROM posts WHERE board = ? AND card_dirty = 1 "
                "AND state = 'live' ORDER BY task_id",
                (board,),
            )
        ]

    def set_thread_state(
        self,
        board: str,
        task_id: str,
        *,
        status_key: str | None = None,
        tag: str | None = None,
        name: str | None = None,
        archived: bool | None = None,
    ) -> None:
        """What the Discord thread currently shows — so maintenance PATCHes
        (tag, rename, archive) happen only on change, never every pass."""
        sets, params = [], []
        for column, value in (
            ("last_status_key", status_key),
            ("last_tag", tag),
            ("last_name", name),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if archived is not None:
            sets.append("thread_archived = ?")
            params.append(1 if archived else 0)
        if not sets:
            return
        self._conn.execute(
            f"UPDATE posts SET {', '.join(sets)} WHERE board = ? AND task_id = ?",
            (*params, board, task_id),
        )

    def tasks_showing(self, board: str, status_key: str) -> list:
        return [
            row["task_id"]
            for row in self._conn.execute(
                "SELECT task_id FROM posts WHERE board = ? AND last_status_key = ? "
                "AND state = 'live' ORDER BY task_id",
                (board, status_key),
            )
        ]

    def set_backoff(self, board: str, task_id: str, *, until: int) -> None:
        self._conn.execute(
            "UPDATE posts SET backoff_until = ? WHERE board = ? AND task_id = ?",
            (until, board, task_id),
        )

    def set_task_position(self, board: str, task_id: str, *, last_event_id: int) -> None:
        # Monotonic: a stale writer that lost its lease can never rewind the
        # deduping watermark and cause already-published events to replay.
        self._conn.execute(
            "UPDATE posts SET last_event_id = ? WHERE board = ? AND task_id = ? "
            "AND last_event_id < ?",
            (last_event_id, board, task_id, last_event_id),
        )
