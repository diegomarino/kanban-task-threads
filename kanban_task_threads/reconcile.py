"""Operator reconciliation over the plugin state DB.

The failure policy (ADR-0007) deliberately defers three situations to a human:
a create whose outcome is unknown, a post a human deleted (tombstone), and a
payload Discord permanently rejected (dead letter). This module is the
operator's side of that contract — the verbs that were previously hand-written
SQL. Nothing here touches Discord: every verb edits the state DB and lets the
consumer act on its next pass.

Verbs:
- attention  what needs a human, across all boards in the file
- clear      forget an unknown-outcome create (checked: no thread exists) —
             the next pass re-creates from the task's durable position
- adopt      the create DID succeed on Discord: attach the found thread ids
- rearm      revive a tombstoned/dead-lettered post, keeping its thread —
             publishing resumes with FUTURE events (the board cursor has
             moved on; history is not replayed)
- recreate   drop the row entirely so a fresh post opens on the next pass
             (same future-events caveat)
"""

import sqlite3
from pathlib import Path


class ReconcileError(RuntimeError):
    """The verb does not apply to this row's actual situation."""


def _connect(db_path) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise ReconcileError(f"no state DB at {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _post(conn, board: str, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM posts WHERE board = ? AND task_id = ?", (board, task_id)
    ).fetchone()
    if row is None:
        raise ReconcileError(f"{board}:{task_id}: no such post row")
    return row


def attention(db_path) -> list[dict]:
    """Rows waiting on an operator: unknown creates and parked posts."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT board, task_id, state, pending_create_at, thread_id, "
            "message_id, destination, detail FROM posts "
            "WHERE pending_create_at IS NOT NULL OR state != 'live' "
            "ORDER BY board, task_id"
        ).fetchall()
        return [dict(row) for row in rows]


def clear(db_path, board: str, task_id: str) -> None:
    """Forget an unknown-outcome create. Refuses if a thread exists — that is
    an `adopt` or a `recreate`, and clearing it would leak the thread."""
    with _connect(db_path) as conn:
        row = _post(conn, board, task_id)
        if row["thread_id"] is not None:
            raise ReconcileError(
                f"{board}:{task_id}: has thread {row['thread_id']} — use "
                "adopt (attach it) or recreate (abandon it), not clear"
            )
        conn.execute("DELETE FROM posts WHERE board = ? AND task_id = ?", (board, task_id))


def adopt(db_path, board: str, task_id: str, *, thread_id: str, message_id: str) -> None:
    """The create actually succeeded: attach the thread found on the forum
    (its id is in the thread name, `title · task_id`) and go live."""
    with _connect(db_path) as conn:
        _post(conn, board, task_id)
        conn.execute(
            "UPDATE posts SET thread_id = ?, message_id = ?, state = 'live', "
            "pending_create_at = NULL, detail = NULL "
            "WHERE board = ? AND task_id = ?",
            (thread_id, message_id, board, task_id),
        )


def rearm(db_path, board: str, task_id: str) -> None:
    """Revive a parked post, keeping its thread. Future events publish again;
    events skipped while parked are not replayed."""
    with _connect(db_path) as conn:
        _post(conn, board, task_id)
        conn.execute(
            "UPDATE posts SET state = 'live', detail = NULL, "
            "pending_create_at = NULL WHERE board = ? AND task_id = ?",
            (board, task_id),
        )


def recreate(db_path, board: str, task_id: str) -> None:
    """Drop the row: the next pass opens a fresh post (future events only)."""
    with _connect(db_path) as conn:
        _post(conn, board, task_id)
        conn.execute("DELETE FROM posts WHERE board = ? AND task_id = ?", (board, task_id))


def main(argv: list[str]) -> int:
    """CLI: reconcile <state.db> [attention|clear|adopt|rearm|recreate] ..."""
    if len(argv) < 2:
        print(__doc__)
        return 2
    db_path, verb, args = argv[0], argv[1], argv[2:]
    try:
        if verb == "attention":
            rows = attention(db_path)
            if not rows:
                print("nothing needs an operator")
                return 0
            for row in rows:
                mark = "pending-create" if row["pending_create_at"] else row["state"]
                print(
                    f"{row['board']}:{row['task_id']}  [{mark}]  "
                    f"thread={row['thread_id'] or '—'}  {row['detail'] or ''}"
                )
            return 0
        board, task_id = args[0], args[1]
        if verb == "clear":
            clear(db_path, board, task_id)
        elif verb == "adopt":
            adopt(db_path, board, task_id, thread_id=args[2], message_id=args[3])
        elif verb == "rearm":
            rearm(db_path, board, task_id)
        elif verb == "recreate":
            recreate(db_path, board, task_id)
        else:
            print(f"unknown verb {verb!r}")
            return 2
        print(f"{verb} {board}:{task_id}: done — the next consumer pass acts on it")
        return 0
    except (ReconcileError, IndexError) as exc:
        print(f"reconcile: {exc}")
        return 1
