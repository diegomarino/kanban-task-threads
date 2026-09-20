"""ADR-0005: the task → post mapping is board state in a store the plugin owns —
never ctx.state, which is per profile, has no delete, and races on
read-modify-write. Cross-process exclusion comes from SQLite CAS/lease
semantics; replies remain at-least-once across a crash after send."""

import pytest

from kanban_task_threads.store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.db")


# --- cursor -------------------------------------------------------------------


def test_cursor_defaults_to_zero(store):
    assert store.get_cursor("default") == 0


def test_cursor_cas_advances_once(store):
    assert store.advance_cursor("default", old=0, new=18) is True
    assert store.get_cursor("default") == 18


def test_cursor_cas_rejects_stale_writer(store):
    store.advance_cursor("default", old=0, new=18)
    assert store.advance_cursor("default", old=0, new=12) is False
    assert store.get_cursor("default") == 18


def test_cursors_are_per_board(store):
    store.advance_cursor("default", old=0, new=18)
    assert store.get_cursor("other") == 0


# --- lease --------------------------------------------------------------------
# acquire returns a fencing token (or None): two acquisitions are always
# distinguishable, even by the same holder string (a same-pid reload).


def test_lease_excludes_second_holder(store):
    assert store.acquire_lease("consume:default", "p1", now=100, ttl=60) is not None
    assert store.acquire_lease("consume:default", "p2", now=110, ttl=60) is None


def test_lease_expires(store):
    store.acquire_lease("consume:default", "p1", now=100, ttl=60)
    assert store.acquire_lease("consume:default", "p2", now=161, ttl=60) is not None


def test_release_frees_lease_only_for_holder_and_token(store):
    token = store.acquire_lease("consume:default", "p1", now=100, ttl=60)
    store.release_lease("consume:default", "p2", token)  # not the holder: no-op
    assert store.acquire_lease("consume:default", "p3", now=110, ttl=60) is None
    store.release_lease("consume:default", "p1", token)
    assert store.acquire_lease("consume:default", "p3", now=110, ttl=60) is not None


def test_tokens_are_unique_across_acquisitions(store):
    t1 = store.acquire_lease("consume:default", "p1", now=100, ttl=60)
    store.release_lease("consume:default", "p1", t1)
    t2 = store.acquire_lease("consume:default", "p1", now=110, ttl=60)
    assert t1 != t2  # a reload with the same profile:pid is a different holder


def test_renew_extends_only_for_the_token_owner(store):
    token = store.acquire_lease("consume:default", "p1", now=100, ttl=60)
    assert store.renew_lease("consume:default", "p1", token, now=150, ttl=60) is True
    # renewed to 210: still excluded at 190
    assert store.acquire_lease("consume:default", "p2", now=190, ttl=60) is None
    # expiry passes; p2 steals; the stale token can no longer renew
    stolen = store.acquire_lease("consume:default", "p2", now=211, ttl=60)
    assert stolen is not None
    assert store.renew_lease("consume:default", "p1", token, now=212, ttl=60) is False


def test_stale_release_does_not_free_a_stolen_lease(store):
    t1 = store.acquire_lease("consume:default", "p1", now=100, ttl=60)
    t2 = store.acquire_lease("consume:default", "p2", now=161, ttl=60)
    store.release_lease("consume:default", "p1", t1)  # stale: must be a no-op
    assert store.renew_lease("consume:default", "p2", t2, now=162, ttl=60) is True


# --- posts --------------------------------------------------------------------


def test_unknown_task_has_no_post(store):
    assert store.get_post("default", "t_x") is None


def test_create_protocol_records_attempt_before_outcome(store):
    store.begin_create("default", "t_x", now=100)
    post = store.get_post("default", "t_x")
    assert post["pending_create_at"] == 100
    store.complete_create(
        "default", "t_x", thread_id="th1", message_id="m1", destination="webhook:123"
    )
    post = store.get_post("default", "t_x")
    assert post["pending_create_at"] is None
    assert (post["thread_id"], post["message_id"]) == ("th1", "m1")
    assert post["state"] == "live"


def test_tombstone_and_dead_letter_persist(store):
    store.begin_create("default", "t_x", now=100)
    store.complete_create(
        "default", "t_x", thread_id="th1", message_id="m1", destination="webhook:123"
    )
    store.mark_tombstone("default", "t_x", "post deleted by a human")
    assert store.get_post("default", "t_x")["state"] == "tombstone"
    store.begin_create("default", "t_y", now=100)
    store.mark_dead_letter("default", "t_y", "HTTP 400 tag required")
    post = store.get_post("default", "t_y")
    assert post["state"] == "dead_letter"
    assert "400" in post["detail"]


def test_backoff_and_task_position_roundtrip(store):
    store.begin_create("default", "t_x", now=100)
    store.complete_create(
        "default", "t_x", thread_id="th1", message_id="m1", destination="webhook:123"
    )
    store.set_backoff("default", "t_x", until=200)
    store.set_task_position("default", "t_x", last_event_id=7)
    post = store.get_post("default", "t_x")
    assert post["backoff_until"] == 200
    assert post["last_event_id"] == 7


def test_thread_maintenance_state_roundtrip(store):
    store.begin_create("default", "t_x", now=100)
    store.set_thread_state(
        "default", "t_x", status_key="running", tag="running", name="fix · t_x", archived=False
    )
    post = store.get_post("default", "t_x")
    assert (
        post["last_status_key"],
        post["last_tag"],
        post["last_name"],
        post["thread_archived"],
    ) == ("running", "running", "fix · t_x", 0)


def test_tasks_showing_running_lists_candidates_for_the_stale_sweep(store):
    store.begin_create("default", "t_a", now=100)
    store.set_thread_state("default", "t_a", status_key="running")
    store.begin_create("default", "t_b", now=100)
    store.set_thread_state("default", "t_b", status_key="done")
    assert store.tasks_showing("default", "running") == ["t_a"]


def test_old_state_dbs_are_migrated_in_place(tmp_path):
    # a pre-existing DB without the maintenance columns must open and work
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE posts (
            board TEXT NOT NULL, task_id TEXT NOT NULL, thread_id TEXT,
            message_id TEXT, destination TEXT, state TEXT NOT NULL DEFAULT 'live',
            detail TEXT, last_event_id INTEGER NOT NULL DEFAULT 0,
            backoff_until INTEGER NOT NULL DEFAULT 0, pending_create_at INTEGER,
            PRIMARY KEY (board, task_id));
        INSERT INTO posts (board, task_id, thread_id, message_id)
        VALUES ('default', 't_old', 'th1', 'm1');
    """)
    conn.commit()
    conn.close()
    store = StateStore(db)
    post = store.get_post("default", "t_old")
    assert post["card_dirty"] == 0  # earlier added column
    assert post["last_status_key"] is None  # new columns exist, defaulted
    store.set_thread_state("default", "t_old", status_key="running")
    assert store.get_post("default", "t_old")["last_status_key"] == "running"


def test_task_position_never_regresses(store):
    # a stale writer resuming after losing its lease must not rewind the
    # deduping watermark — that would replay already-published events
    store.begin_create("default", "t_x", now=100)
    store.set_task_position("default", "t_x", last_event_id=9)
    store.set_task_position("default", "t_x", last_event_id=4)
    assert store.get_post("default", "t_x")["last_event_id"] == 9
