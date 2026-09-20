"""Bot-token thread maintenance (ADR-0003 extras) and the stale sweep (ADR-0001's crash
acceptance, at the consumer level): tags follow the card's status, the name
follows the title, done archives — each PATCHed only on change — and a worker
that dies silently is noticed without any event arriving."""

import pytest
from conftest import FakeTransport, add_event, insert_task, make_board

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.store import StateStore

NOW = 1_789_700_100
BOT_CAPS = {"rich_card", "live_timestamps", "per_message_identity", "title_state", "tags"}


@pytest.fixture
def board(tmp_path):
    return make_board(tmp_path / "kanban.db")


def build(tmp_path, capabilities=None, **kwargs):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport(capabilities)
    consumer = Consumer(
        tmp_path / "kanban.db", store, transport, board="default", holder="p1", **kwargs
    )
    return store, transport, consumer


def test_running_task_gets_its_tag_once(board, tmp_path):
    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", status="running", last_heartbeat_at=NOW)
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    assert "set_status_tag" in [c[0] for c in transport.calls]
    ((_, _, tag),) = transport.ops("set_status_tag")
    assert tag == "running"
    add_event(board, "t_1", "commented", {"author": "x", "len": 1})
    consumer.run_once(now=NOW + 10)  # status unchanged: no second tag PATCH
    assert len(transport.ops("set_status_tag")) == 1


def test_done_task_is_tagged_done_and_archived_once(board, tmp_path):
    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    consumer.run_once(now=NOW)
    tags = [c[2] for c in transport.ops("set_status_tag")]
    assert tags == ["done"]
    assert [c[2] for c in transport.ops("set_archived")] == [True]
    add_event(board, "t_1", "commented", {"author": "x", "len": 1})
    consumer.run_once(now=NOW + 10)
    assert [c[2] for c in transport.ops("set_archived")] == [True]  # no repeat


def test_reanimated_task_is_unarchived_before_retagging(board, tmp_path):
    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    consumer.run_once(now=NOW)
    board.execute(
        "UPDATE tasks SET status='running', last_heartbeat_at=? WHERE id='t_1'", (NOW + 10,)
    )
    board.commit()
    add_event(board, "t_1", "reclaimed", {})
    consumer.run_once(now=NOW + 10)
    archived_calls = [c[2] for c in transport.ops("set_archived")]
    assert archived_calls == [True, False]
    ops = [c[0] for c in transport.calls]
    assert ops.index("set_archived", ops.index("set_archived") + 1) < len(ops) - 1 - ops[
        ::-1
    ].index("set_status_tag")  # unarchive before retag


def test_title_change_renames_the_thread(board, tmp_path):
    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", title="old title", status="running", last_heartbeat_at=NOW)
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    assert transport.ops("rename") == []  # name fresh from creation
    board.execute("UPDATE tasks SET title='new title' WHERE id='t_1'")
    board.commit()
    add_event(board, "t_1", "edited", {})
    consumer.run_once(now=NOW + 10)
    ((_, _, name),) = transport.ops("rename")
    assert name == "new title · t_1"


def test_webhook_only_deployment_never_calls_bot_operations(board, tmp_path):
    store, transport, consumer = build(tmp_path)  # webhook capabilities only
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    consumer.run_once(now=NOW)
    for op in ("set_status_tag", "rename", "set_archived"):
        assert transport.ops(op) == []


def test_failed_maintenance_is_retried_via_the_dirty_card(board, tmp_path):
    # A "done" task whose archive PATCH 500s may never get another event:
    # the failure must mark the card dirty so the next pass retries.
    from kanban_task_threads.transport import TransportError

    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    transport.queue_error("set_status_tag", TransportError(500, {"message": "boom"}))
    report = consumer.run_once(now=NOW)
    assert report.warnings
    consumer.run_once(now=NOW + 10)  # no new events: dirty repaint retries
    assert [c[2] for c in transport.ops("set_status_tag")] == ["done", "done"]
    assert [c[2] for c in transport.ops("set_archived")] == [True]


def test_reverting_to_an_untagged_status_clears_the_tag(board, tmp_path):
    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    consumer.run_once(now=NOW)
    board.execute("UPDATE tasks SET status='ready' WHERE id='t_1'")
    board.commit()
    add_event(board, "t_1", "reclaimed", {})
    consumer.run_once(now=NOW + 10)
    assert transport.ops("clear_status_tag"), "stale 'done' tag was left applied"
    assert store.get_post("default", "t_1")["last_tag"] == ""
    consumer.run_once(now=NOW + 20)  # idempotent: no second clear
    assert len(transport.ops("clear_status_tag")) == 1


def test_legacy_rows_without_a_stored_name_get_renamed_once(board, tmp_path):
    # Pre-delta threads were created without the task id in the name; storing
    # the computed name without PATCHing would freeze the mismatch forever.
    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", title="old thread", status="running", last_heartbeat_at=NOW)
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    store._conn.execute("UPDATE posts SET last_name = NULL WHERE task_id = 't_1'")
    add_event(board, "t_1", "commented", {"author": "x", "len": 1})
    consumer.run_once(now=NOW + 10)
    ((_, _, name),) = transport.ops("rename")
    assert name == "old thread · t_1"


def test_stale_recovers_when_the_heartbeat_returns(board, tmp_path):
    # The sweep must be two-way: heartbeats are not events, so a worker that
    # hung and recovered would otherwise show stale/failed forever.
    store, transport, consumer = build(tmp_path, BOT_CAPS, stale_after=600)
    insert_task(board, "t_1", status="running", last_heartbeat_at=NOW)
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    consumer.run_once(now=NOW + 3600)  # goes stale
    assert store.get_post("default", "t_1")["last_status_key"] == "stale"
    board.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id='t_1'", (NOW + 7000,))
    board.commit()
    report = consumer.run_once(now=NOW + 7010)  # no events: recovery sweep
    assert "t_1" in report.edited
    assert store.get_post("default", "t_1")["last_status_key"] == "running"
    assert [c[2] for c in transport.ops("set_status_tag")][-1] == "running"


def test_silently_killed_worker_reaches_stale_without_any_event(board, tmp_path):
    # ADR-0001 acceptance: kill -9 leaves no event behind. The card must leave
    # "running" once the heartbeat threshold passes — the sweep notices
    # tasks whose card shows running while their heartbeat has gone quiet.
    store, transport, consumer = build(tmp_path, BOT_CAPS, stale_after=600)
    insert_task(board, "t_1", status="running", last_heartbeat_at=NOW)
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    ((_, _, card1),) = transport.ops("open_thread")
    assert "running" in card1.description

    # the worker is SIGKILLed: heartbeats stop, no event will ever arrive
    report = consumer.run_once(now=NOW + 3600)
    assert "t_1" in report.edited
    edits = transport.ops("edit_card")
    assert "stale" in edits[-1][2].description.lower()
    assert [c[2] for c in transport.ops("set_status_tag")][-1] == "failed"
    assert store.get_post("default", "t_1")["last_status_key"] == "stale"
