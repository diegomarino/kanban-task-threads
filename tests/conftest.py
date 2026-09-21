"""Shared fixtures. No network anywhere in this tree.

The repo root is inserted into sys.path so `kanban_task_threads` (the logic
package) imports without Hermes being present.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


import sqlite3

BOARD_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
    created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
    completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
    workspace_path TEXT, branch_name TEXT, last_heartbeat_at INTEGER,
    block_kind TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL, child_id TEXT NOT NULL
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL
);
"""


def add_comment_row(conn, task_id, author, body, created_at=1_789_700_001):
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, author, body, created_at),
    )
    conn.commit()


def link_tasks(conn, parent_id, child_id):
    conn.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent_id, child_id)
    )
    conn.commit()


def make_board(path):
    """A board DB with the real tables' shape (the subset the plugin reads)."""
    conn = sqlite3.connect(path)
    conn.executescript(BOARD_SCHEMA)
    conn.commit()
    return conn


def insert_task(conn, task_id, **fields):
    row = {
        "id": task_id,
        "title": "fix the build",
        "assignee": "sandbox",
        "status": "ready",
        "created_at": 1_789_700_000,
    }
    row.update(fields)
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({', '.join(':' + c for c in row)})", row)
    conn.commit()


def add_event(conn, task_id, kind, payload=None, created_at=1_789_700_001):
    import json

    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload) if payload is not None else None, created_at),
    )
    conn.commit()


class FakeTransport:
    """Records the seam's operations; failures are queued per operation."""

    def __init__(self, capabilities=None):
        self.calls = []
        self.errors = {}  # op name -> list of exceptions to raise, in order
        self._n = 0
        self._capabilities = frozenset(
            capabilities or {"rich_card", "live_timestamps", "per_message_identity"}
        )
        self.thread_inventory = {}
        self.status_tag_ids = {}

    def queue_error(self, op, exc):
        self.errors.setdefault(op, []).append(exc)

    def _maybe_raise(self, op):
        if self.errors.get(op):
            raise self.errors[op].pop(0)

    def capabilities(self):
        return self._capabilities

    def set_status_tag(self, ref, name):
        self.calls.append(("set_status_tag", ref, name))
        self._maybe_raise("set_status_tag")
        tag_id = self.status_tag_id(name)
        if ref.thread_id in self.thread_inventory and tag_id is not None:
            self.thread_inventory[ref.thread_id]["applied_tags"] = (tag_id,)
            return dict(self.thread_inventory[ref.thread_id])
        return True

    def clear_status_tag(self, ref):
        self.calls.append(("clear_status_tag", ref))
        self._maybe_raise("clear_status_tag")

    def rename(self, ref, name):
        self.calls.append(("rename", ref, name))
        self._maybe_raise("rename")
        return None

    def set_archived(self, ref, archived):
        self.calls.append(("set_archived", ref, archived))
        self._maybe_raise("set_archived")
        if ref.thread_id in self.thread_inventory:
            self.thread_inventory[ref.thread_id]["archived"] = archived
            return dict(self.thread_inventory[ref.thread_id])
        return None

    def status_tag_id(self, name):
        return self.status_tag_ids.get(name, name)

    def list_forum_threads(self, guild_id):
        self.calls.append(("list_forum_threads", guild_id))
        self._maybe_raise("list_forum_threads")
        return {thread_id: dict(metadata) for thread_id, metadata in self.thread_inventory.items()}

    def open_thread(self, *, title, card):
        from kanban_task_threads.transport import ThreadRef

        self.calls.append(("open_thread", title, card))
        self._maybe_raise("open_thread")
        self._n += 1
        return ThreadRef(thread_id=f"th{self._n}", message_id=f"msg{self._n}")

    def edit_card(self, ref, card):
        self.calls.append(("edit_card", ref, card))
        self._maybe_raise("edit_card")

    def append(self, ref, *, content, username=None):
        self.calls.append(("append", ref, content, username))
        self._maybe_raise("append")
        self._n += 1
        return f"msg{self._n}"

    def ops(self, name=None):
        if name is None:
            return [c[0] for c in self.calls]
        return [c for c in self.calls if c[0] == name]


class FakeHttp:
    """Records every request and replays canned responses.

    Matches the transport's http seam: http(method, url, body) -> (status, dict).
    """

    def __init__(self):
        self.calls = []  # (method, url, body) — headers kept apart so
        self.headers_seen = []  # existing 3-tuple unpacking stays valid
        self.responses = []  # queue of (status, dict); default is a message stub

    def queue(self, status, body):
        self.responses.append((status, body))

    def __call__(self, method, url, body, headers=None):
        self.calls.append((method, url, body))
        self.headers_seen.append(headers or {})
        if self.responses:
            return self.responses.pop(0)
        return 200, {"id": "111", "channel_id": "222"}
