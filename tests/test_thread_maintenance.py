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


@pytest.mark.parametrize(
    ("status", "block_kind", "expected_tag"),
    [("todo", None, "todo"), ("blocked", "needs_input", "needs-human")],
)
def test_statuses_get_their_total_mapped_tag(board, tmp_path, status, block_kind, expected_tag):
    _, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", status=status, block_kind=block_kind)
    add_event(board, "t_1", "created", {"status": status})
    consumer.run_once(now=NOW)
    assert [call[2] for call in transport.ops("set_status_tag")] == [expected_tag]


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


def test_terminal_retag_unarchives_before_patch_and_rearchives_last(board, tmp_path):
    _, transport, consumer = build(tmp_path, BOT_CAPS, reply_on=())
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    before = len(transport.calls)
    board.execute("UPDATE tasks SET status='archived' WHERE id='t_1'")
    board.commit()
    add_event(board, "t_1", "archived", {})

    consumer.run_once(now=NOW + 10)

    assert [call[0] for call in transport.calls[before:] if call[0] != "edit_card"] == [
        "set_archived",
        "set_status_tag",
        "set_archived",
    ]
    assert [call[2] for call in transport.ops("set_archived")] == [True, False, True]
    assert [call[2] for call in transport.ops("set_status_tag")] == ["done", "archived"]


def test_unarchive_patch_response_is_authoritative_readback(board, tmp_path):
    class RefusingUnarchive(FakeTransport):
        def set_archived(self, ref, archived):
            super().set_archived(ref, archived)
            return {"applied_tags": (), "archived": True}

    store = StateStore(tmp_path / "state.db")
    transport = RefusingUnarchive(BOT_CAPS)
    consumer = Consumer(tmp_path / "kanban.db", store, transport, board="default", holder="p1")
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    consumer.run_once(now=NOW)
    board.execute("UPDATE tasks SET status='ready' WHERE id='t_1'")
    board.commit()
    add_event(board, "t_1", "reclaimed", {})

    consumer.run_once(now=NOW + 10)

    assert store.get_post("default", "t_1")["thread_archived"] == 1


def test_unarchive_readback_with_removed_tag_reapplies_the_desired_tag(board, tmp_path):
    class TagRemovedDuringUnarchive(FakeTransport):
        def set_archived(self, ref, archived):
            self.calls.append(("set_archived", ref, archived))
            return {"applied_tags": (), "archived": False}

    store = StateStore(tmp_path / "state.db")
    insert_task(board, "t_1", status="ready")
    store.begin_create("default", "t_1", now=NOW)
    store.complete_create(
        "default", "t_1", thread_id="th1", message_id="msg1", destination="discord:test"
    )
    store.set_thread_state("default", "t_1", tag="ready", archived=True)
    store.set_card_dirty("default", "t_1", True)
    transport = TagRemovedDuringUnarchive(BOT_CAPS)
    consumer = Consumer(tmp_path / "kanban.db", store, transport, board="default", holder="bot")

    consumer.run_once(now=NOW)

    assert [call[2] for call in transport.ops("set_status_tag")] == ["ready"]


def test_tag_patch_readback_with_concurrent_archive_unarchives_active_task(board, tmp_path):
    class ArchivedDuringTagPatch(FakeTransport):
        def set_status_tag(self, ref, name):
            self.calls.append(("set_status_tag", ref, name))
            return {"applied_tags": (name,), "archived": True}

    store = StateStore(tmp_path / "state.db")
    insert_task(board, "t_1", status="ready")
    store.begin_create("default", "t_1", now=NOW)
    store.complete_create(
        "default", "t_1", thread_id="th1", message_id="msg1", destination="discord:test"
    )
    store.set_thread_state("default", "t_1", tag="done", archived=False)
    store.set_card_dirty("default", "t_1", True)
    transport = ArchivedDuringTagPatch(BOT_CAPS)
    consumer = Consumer(tmp_path / "kanban.db", store, transport, board="default", holder="bot")

    consumer.run_once(now=NOW)

    assert [call[2] for call in transport.ops("set_archived")] == [False]
    assert store.get_post("default", "t_1")["thread_archived"] == 0


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
    for op in ("set_status_tag", "rename", "set_archived", "list_forum_threads"):
        assert transport.ops(op) == []


def test_actual_state_audit_repairs_webhook_only_post_without_a_new_event(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    webhook = FakeTransport()
    insert_task(board, "t_1", status="todo")
    add_event(board, "t_1", "created", {"status": "todo"})
    Consumer(tmp_path / "kanban.db", store, webhook, board="default", holder="webhook").run_once(
        now=NOW
    )

    bot = FakeTransport(BOT_CAPS)
    bot.thread_inventory["th1"] = {"applied_tags": ("done",), "archived": True}
    report = Consumer(
        tmp_path / "kanban.db",
        store,
        bot,
        board="default",
        holder="bot",
        guild_id="guild-9",
    ).run_once(now=NOW + 1)

    assert report.warnings == []
    assert bot.ops() == ["list_forum_threads", "set_archived", "set_status_tag"]
    assert bot.thread_inventory["th1"] == {"applied_tags": ("todo",), "archived": False}


def test_audit_honors_archive_state_from_tag_patch_readback(board, tmp_path):
    class ConcurrentArchiveTransport(FakeTransport):
        def set_status_tag(self, ref, name):
            self.calls.append(("set_status_tag", ref, name))
            return {"applied_tags": (name,), "archived": True}

    store = StateStore(tmp_path / "state.db")
    insert_task(board, "t_1", status="todo")
    store.begin_create("default", "t_1", now=NOW)
    store.complete_create(
        "default", "t_1", thread_id="th1", message_id="msg1", destination="discord:test"
    )
    transport = ConcurrentArchiveTransport(BOT_CAPS)
    transport.thread_inventory["th1"] = {"applied_tags": ("done",), "archived": False}
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="bot",
        guild_id="guild-9",
    )

    consumer.run_once(now=NOW)

    assert transport.ops() == ["list_forum_threads", "set_status_tag", "set_archived"]
    assert store.get_post("default", "t_1")["thread_archived"] == 0


def test_audit_rechecks_the_fenced_lease_before_each_tasks_patches(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport(BOT_CAPS)
    for number in (1, 2):
        task_id = f"t_{number}"
        thread_id = f"th{number}"
        insert_task(board, task_id, status="todo")
        store.begin_create("default", task_id, now=NOW)
        store.complete_create(
            "default",
            task_id,
            thread_id=thread_id,
            message_id=f"msg{number}",
            destination="discord:test",
        )
        transport.thread_inventory[thread_id] = {
            "applied_tags": ("done",),
            "archived": False,
        }
    renewals = iter((True, True, False))
    store.renew_lease = lambda *args, **kwargs: next(renewals)
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="bot",
        guild_id="guild-9",
    )

    report = consumer.run_once(now=NOW)

    assert [call[1].thread_id for call in transport.ops("set_status_tag")] == ["th1"]
    assert any("lease lost during Discord metadata audit" in item for item in report.warnings)


def test_audit_skips_posts_frozen_to_another_webhook_in_the_same_forum(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport(BOT_CAPS)
    for task_id, thread_id, destination in (
        ("frozen", "th-frozen", "discord:webhook:old@forum-1"),
        ("eligible", "th-eligible", "discord:webhook:new@forum-1"),
    ):
        insert_task(board, task_id, status="todo")
        store.begin_create("default", task_id, now=NOW)
        store.complete_create(
            "default",
            task_id,
            thread_id=thread_id,
            message_id=f"msg-{task_id}",
            destination=destination,
        )
        transport.thread_inventory[thread_id] = {
            "applied_tags": ("done",),
            "archived": True,
        }
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="bot",
        destination="discord:webhook:new@forum-1",
        guild_id="guild-9",
    )

    consumer.run_once(now=NOW)

    patched_threads = {
        call[1].thread_id
        for call in transport.calls
        if call[0] in ("set_status_tag", "set_archived")
    }
    assert patched_threads == {"th-eligible"}
    assert transport.thread_inventory["th-frozen"] == {
        "applied_tags": ("done",),
        "archived": True,
    }


def test_clean_audit_backoff_resets_to_one_minute_after_a_patch(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    webhook = FakeTransport()
    insert_task(board, "t_1", status="todo")
    add_event(board, "t_1", "created", {"status": "todo"})
    Consumer(tmp_path / "kanban.db", store, webhook, board="default", holder="webhook").run_once(
        now=NOW
    )
    bot = FakeTransport(BOT_CAPS)
    bot.thread_inventory["th1"] = {"applied_tags": ("todo",), "archived": False}
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        bot,
        board="default",
        holder="bot",
        guild_id="guild-9",
    )

    consumer.run_once(now=NOW + 1)  # clean -> 5m
    consumer.run_once(now=NOW + 300)
    consumer.run_once(now=NOW + 301)  # clean -> 15m
    assert len(bot.ops("list_forum_threads")) == 2
    consumer.run_once(now=NOW + 1200)
    consumer.run_once(now=NOW + 1201)  # clean -> 30m
    assert len(bot.ops("list_forum_threads")) == 3
    consumer.run_once(now=NOW + 3000)
    assert len(bot.ops("list_forum_threads")) == 3
    consumer.run_once(now=NOW + 3001)  # clean -> 60m
    assert len(bot.ops("list_forum_threads")) == 4
    consumer.run_once(now=NOW + 6600)
    assert len(bot.ops("list_forum_threads")) == 4
    consumer.run_once(now=NOW + 6601)  # clean -> 60m cap
    assert len(bot.ops("list_forum_threads")) == 5
    consumer.run_once(now=NOW + 10200)
    assert len(bot.ops("list_forum_threads")) == 5

    bot.thread_inventory["th1"]["applied_tags"] = ("done",)
    consumer.run_once(now=NOW + 10201)  # patch -> confirm in 1m
    assert len(bot.ops("list_forum_threads")) == 6
    consumer.run_once(now=NOW + 10260)
    assert len(bot.ops("list_forum_threads")) == 6
    consumer.run_once(now=NOW + 10261)
    assert len(bot.ops("list_forum_threads")) == 7


def test_audit_429_honors_retry_after_without_blocking_event_consumption(board, tmp_path):
    from kanban_task_threads.transport import TransportError

    store, transport, consumer = build(tmp_path, BOT_CAPS, guild_id="guild-9")
    insert_task(board, "t_1", status="todo")
    add_event(board, "t_1", "created", {"status": "todo"})
    transport.queue_error("list_forum_threads", TransportError(429, {"retry_after": 12}))

    report = consumer.run_once(now=NOW)
    assert report.opened == ["t_1"]
    assert any("metadata audit rate limited" in warning for warning in report.warnings)
    consumer.run_once(now=NOW + 11)
    assert len(transport.ops("list_forum_threads")) == 1
    consumer.run_once(now=NOW + 12)
    assert len(transport.ops("list_forum_threads")) == 2


def test_audit_retry_after_starts_when_the_rate_limit_response_arrives(
    board, tmp_path, monkeypatch
):
    from kanban_task_threads import consumer as consumer_module
    from kanban_task_threads.transport import TransportError

    clock = {"now": NOW}

    class SlowRateLimitedTransport(FakeTransport):
        def list_forum_threads(self, guild_id):
            self.calls.append(("list_forum_threads", guild_id))
            if len(self.ops("list_forum_threads")) == 1:
                clock["now"] += 120
                raise TransportError(429, {"retry_after": 60})
            return {}

    monkeypatch.setattr(consumer_module.time, "time", lambda: clock["now"])
    store = StateStore(tmp_path / "state.db")
    transport = SlowRateLimitedTransport(BOT_CAPS)
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="bot",
        guild_id="guild-9",
    )

    consumer.run_once()
    clock["now"] = NOW + 179
    consumer.run_once()
    assert len(transport.ops("list_forum_threads")) == 1
    clock["now"] = NOW + 180
    consumer.run_once()
    assert len(transport.ops("list_forum_threads")) == 2


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


def test_reverting_to_ready_replaces_the_terminal_tag(board, tmp_path):
    store, transport, consumer = build(tmp_path, BOT_CAPS)
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    consumer.run_once(now=NOW)
    board.execute("UPDATE tasks SET status='ready' WHERE id='t_1'")
    board.commit()
    add_event(board, "t_1", "reclaimed", {})
    consumer.run_once(now=NOW + 10)
    assert [call[2] for call in transport.ops("set_status_tag")] == ["done", "ready"]
    assert store.get_post("default", "t_1")["last_tag"] == "ready"
    consumer.run_once(now=NOW + 20)  # idempotent: no third tag PATCH
    assert len(transport.ops("set_status_tag")) == 2


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
