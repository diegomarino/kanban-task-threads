"""ADR-0002: the consumer walks task_events past a durable cursor. Discord being
unreliable is handled here: unknown-outcome creates are recorded and never
blindly retried, permanent 4xx dead-letter, 404 tombstones, 429 backs off
per task. Everything runs against a real SQLite board fixture — no network."""

import pytest
from conftest import FakeTransport, add_event, insert_task, make_board

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.store import StateStore
from kanban_task_threads.transport import ForumTagSetupError, TransportError

NOW = 1_789_700_100


@pytest.fixture
def board(tmp_path):
    return make_board(tmp_path / "kanban.db")


@pytest.fixture
def parts(tmp_path, board):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(tmp_path / "kanban.db", store, transport, board="default", holder="p1")
    return store, transport, consumer


def walk_lifecycle(conn, task_id="t_1"):
    insert_task(conn, task_id, status="done")
    add_event(conn, task_id, "created", {"assignee": "sandbox", "status": "ready"})
    add_event(conn, task_id, "commented", {"author": "default", "len": 12})
    add_event(conn, task_id, "blocked", {"reason": "waiting on a decision", "kind": "needs_input"})
    add_event(conn, task_id, "unblocked")
    add_event(conn, task_id, "completed", {"summary": "walked by the sandbox"})


# --- the happy path -----------------------------------------------------------


def test_lifecycle_opens_once_replies_and_refreshes_card(board, parts):
    store, transport, consumer = parts
    walk_lifecycle(board)
    report = consumer.run_once(now=NOW)
    assert report.opened == ["t_1"]
    assert transport.ops().count("open_thread") == 1
    # commented, blocked, unblocked, completed earn replies; created does not
    replies = transport.ops("append")
    assert len(replies) == 4
    assert transport.ops()[-1] == "edit_card"  # card re-rendered at the end
    assert not report.errors


def test_reply_texts_carry_the_payload(board, parts):
    _, transport, consumer = parts
    walk_lifecycle(board)
    consumer.run_once(now=NOW)
    texts = [c[2] for c in transport.ops("append")]
    assert any("default" in t for t in texts)  # commented author
    assert any("waiting on a decision" in t for t in texts)  # block reason
    assert any("walked by the sandbox" in t for t in texts)  # summary


def test_second_run_is_silent(board, parts):
    _, transport, consumer = parts
    walk_lifecycle(board)
    consumer.run_once(now=NOW)
    n = len(transport.calls)
    report = consumer.run_once(now=NOW + 10)
    assert len(transport.calls) == n
    assert report.opened == [] and report.replied == 0


def test_new_events_append_without_reopening(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    add_event(board, "t_1", "commented", {"author": "coder", "len": 5})
    consumer.run_once(now=NOW + 10)
    assert transport.ops().count("open_thread") == 1
    assert transport.ops().count("append") == 1


def test_blocked_card_shows_reason_from_event_log(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_1", status="blocked", block_kind="needs_input")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "blocked", {"reason": "waiting on a decision", "kind": "needs_input"})
    consumer.run_once(now=NOW)
    _, _, card = transport.ops("open_thread")[0]
    assert "waiting on a decision" in card.description


# --- cross-process exclusion --------------------------------------------------


def test_lease_holder_excludes_second_consumer(board, tmp_path):
    walk_lifecycle(board)
    store = StateStore(tmp_path / "state.db")
    t1, t2 = FakeTransport(), FakeTransport()
    c1 = Consumer(tmp_path / "kanban.db", store, t1, board="default", holder="p1")
    c2 = Consumer(tmp_path / "kanban.db", store, t2, board="default", holder="p2")
    token = store.acquire_lease("consume:default", "p1", now=NOW, ttl=60)
    report = c2.run_once(now=NOW + 1)
    assert report.acquired is False and t2.calls == []
    store.release_lease("consume:default", "p1", token)
    c1.run_once(now=NOW + 2)
    c2.run_once(now=NOW + 3)
    assert t1.ops().count("open_thread") == 1
    assert t2.ops().count("open_thread") == 0


def test_forum_preparation_is_serialized_by_the_publish_lease(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    capabilities = {
        "rich_card",
        "live_timestamps",
        "per_message_identity",
        "title_state",
        "tags",
    }
    blocked_transport = FakeTransport(capabilities)
    winner_transport = FakeTransport(capabilities)
    blocked_transport.record_prepare = True
    winner_transport.record_prepare = True
    blocked = Consumer(
        tmp_path / "kanban.db", store, blocked_transport, board="default", holder="p2"
    )
    winner = Consumer(tmp_path / "kanban.db", store, winner_transport, board="default", holder="p3")
    token = store.acquire_lease("consume:default", "p1", now=NOW, ttl=60)

    assert blocked.run_once(now=NOW + 1).acquired is False
    assert blocked_transport.ops() == []

    store.release_lease("consume:default", "p1", token)
    assert winner.run_once(now=NOW + 2).acquired is True
    assert winner_transport.ops() == ["prepare_forum"]


def test_forum_setup_error_blocks_publication_and_retries_on_the_next_pass(board, tmp_path):
    walk_lifecycle(board)
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport({"title_state", "tags"})
    transport.record_prepare = True
    transport.queue_error("prepare_forum", ForumTagSetupError("grant Manage Channels"))
    consumer = Consumer(tmp_path / "kanban.db", store, transport, board="default", holder="bot")

    first = consumer.run_once(now=NOW)
    assert first.errors == ["Discord forum setup failed: grant Manage Channels"]
    assert transport.ops() == ["prepare_forum"]
    assert store.get_cursor("default") == 0

    second = consumer.run_once(now=NOW + 1)
    assert second.opened == ["t_1"]
    assert transport.ops()[:2] == ["prepare_forum", "prepare_forum"]


def test_losing_lease_during_forum_setup_forces_fresh_setup_after_reacquire(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport({"title_state", "tags"})
    transport.record_prepare = True
    consumer = Consumer(tmp_path / "kanban.db", store, transport, board="default", holder="bot")
    renewals = iter((True, False))
    real_renew = store.renew_lease
    store.renew_lease = lambda *args, **kwargs: next(renewals)

    first = consumer.run_once(now=NOW)
    assert first.warnings == ["lease lost during Discord forum setup"]
    assert transport.ops() == ["prepare_forum"]

    store.renew_lease = real_renew
    second = consumer.run_once(now=NOW + 1)
    assert second.acquired is True
    assert transport.ops() == ["prepare_forum", "prepare_forum"]


# --- Discord being Discord ----------------------------------------------------


def test_unknown_outcome_create_is_never_blindly_retried(board, parts):
    store, transport, consumer = parts
    walk_lifecycle(board)
    transport.queue_error("open_thread", OSError("connection dropped mid-flight"))
    report = consumer.run_once(now=NOW)
    assert report.errors
    post = store.get_post("default", "t_1")
    assert post["pending_create_at"] is not None
    n = len(transport.calls)
    report2 = consumer.run_once(now=NOW + 300)
    assert len(transport.calls) == n  # no second create attempt
    assert report2.errors  # still surfaced, not swallowed


def test_permanent_400_dead_letters_the_task(board, parts):
    store, transport, consumer = parts
    walk_lifecycle(board)
    transport.queue_error("open_thread", TransportError(400, {"message": "tag required"}))
    consumer.run_once(now=NOW)
    assert store.get_post("default", "t_1")["state"] == "dead_letter"
    n = len(transport.calls)
    consumer.run_once(now=NOW + 10)
    assert len(transport.calls) == n


def test_404_on_edit_tombstones_and_stops_publishing(board, parts):
    store, transport, consumer = parts
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    add_event(board, "t_1", "commented", {"author": "x", "len": 1})
    transport.queue_error("append", TransportError(404, {"message": "Unknown Message"}))
    consumer.run_once(now=NOW + 10)
    assert store.get_post("default", "t_1")["state"] == "tombstone"
    n = len(transport.calls)
    add_event(board, "t_1", "commented", {"author": "x", "len": 1})
    consumer.run_once(now=NOW + 20)
    assert len(transport.calls) == n


def test_429_backs_off_the_task_then_recovers(board, parts):
    store, transport, consumer = parts
    walk_lifecycle(board)
    transport.queue_error("open_thread", TransportError(429, {"retry_after": 5.0}))
    consumer.run_once(now=NOW)
    n = len(transport.calls)
    consumer.run_once(now=NOW + 2)  # still inside the backoff window
    assert len(transport.calls) == n
    consumer.run_once(now=NOW + 10)  # window passed: full lifecycle out
    # the fake records attempts: the throttled one plus the successful retry
    assert transport.ops().count("open_thread") == 2
    assert transport.ops("append")  # replies followed
    assert store.get_post("default", "t_1")["state"] == "live"


def test_tag_required_400_dead_letters_with_an_actionable_message(board, parts):
    # A forum with flags=16 rejects every webhook create that lacks
    # applied_tags. Without a bot token that is only observable as this 400 —
    # the installer must get told what to do, not an opaque error dump.
    store, transport, consumer = parts
    walk_lifecycle(board)
    transport.queue_error(
        "open_thread", TransportError(400, {"code": 40067, "message": "A tag is required..."})
    )
    report = consumer.run_once(now=NOW)
    detail = store.get_post("default", "t_1")["detail"]
    assert "discord_applied_tag_ids" in detail
    assert any("discord_applied_tag_ids" in e for e in report.errors)


def test_transient_publish_failures_are_warnings_and_retried(board, parts):
    # ADR-0007 + logging levels: a 502 on a REPLY retries next pass (warning) —
    # nothing was created, the position simply did not advance.
    store, transport, consumer = parts
    walk_lifecycle(board)
    transport.queue_error("append", TransportError(502, {"message": "bad gateway"}))
    report = consumer.run_once(now=NOW)
    assert report.warnings and not report.errors
    consumer.run_once(now=NOW + 5)  # retried and delivered
    assert store.get_post("default", "t_1")["state"] == "live"
    texts = [c[2] for c in transport.ops("append")]
    assert any("walked by the sandbox" in t for t in texts)  # reached the end


def test_5xx_on_create_is_an_unknown_outcome_not_a_retry(board, parts):
    # A proxy 502 does not prove Discord did not create the thread: retrying
    # can orphan a duplicate post. Same protocol as a lost response.
    store, transport, consumer = parts
    walk_lifecycle(board)
    transport.queue_error("open_thread", TransportError(502, {"message": "bad gateway"}))
    report = consumer.run_once(now=NOW)
    assert report.errors and not report.warnings
    assert store.get_post("default", "t_1")["pending_create_at"] is not None
    n = len(transport.calls)
    consumer.run_once(now=NOW + 300)
    assert len(transport.calls) == n  # never blindly retried


def test_thread_opens_with_the_task_id_in_its_name(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_1", title="fix the build", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    ((_, title, _),) = transport.ops("open_thread")
    assert title == "fix the build · t_1"


def test_system_events_land_unsigned(board, parts):
    # crashed is the board observing the worker, not the worker speaking:
    # username must be None so the webhook's institutional name signs it
    _, transport, consumer = parts
    insert_task(board, "t_1", status="ready")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "crashed", {})
    consumer.run_once(now=NOW)
    ((_, _, _, username),) = transport.ops("append")
    assert username is None


def test_changes_requested_reply_signed_by_the_reviewer(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_1", status="ready", assignee="coder")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(
        board,
        "t_1",
        "changes_requested",
        {
            "reason": "tighten tests",
            "implementer": "coder",
            "reviewer": "marsan",
            "status": "ready",
        },
    )
    consumer.run_once(now=NOW)
    ((_, _, _, username),) = transport.ops("append")
    assert username == "marsan"


def test_destination_change_freezes_instead_of_publishing(board, tmp_path):
    # ADR-0007: re-pointing the plugin at a different forum must freeze old entries
    # explicitly — never silently address messages the new webhook cannot
    # edit (which would 404 and be mistaken for a human deletion).
    store = StateStore(tmp_path / "state.db")
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    old = Consumer(
        tmp_path / "kanban.db",
        store,
        FakeTransport(),
        board="default",
        holder="p1",
        destination="discord:webhook:A@1",
    )
    old.run_once(now=NOW)

    add_event(board, "t_1", "commented", {"author": "x", "len": 1})
    t2 = FakeTransport()
    repointed = Consumer(
        tmp_path / "kanban.db",
        store,
        t2,
        board="default",
        holder="p1",
        destination="discord:webhook:B@2",
    )
    report = repointed.run_once(now=NOW + 10)
    assert t2.calls == []  # frozen, not replayed
    assert any("destination" in e for e in report.errors)
    assert store.get_post("default", "t_1")["state"] == "live"  # not tombstoned


def test_failed_card_refresh_is_repainted_without_new_events(board, parts):
    # The reply loop advances the position BEFORE the trailing edit_card; if
    # only the edit fails transiently, no future event may ever arrive for
    # this task — the dirty card must be repainted on the next pass anyway.
    store, transport, consumer = parts
    insert_task(board, "t_1", status="review")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "review_requested", {})
    transport.queue_error("edit_card", TransportError(502, {"message": "bad gateway"}))
    report = consumer.run_once(now=NOW)
    assert report.warnings
    n_edits = len(transport.ops("edit_card"))
    report2 = consumer.run_once(now=NOW + 10)  # no new events at all
    assert len(transport.ops("edit_card")) == n_edits + 1
    assert "t_1" in report2.edited
    report3 = consumer.run_once(now=NOW + 20)  # repainted once, then silent
    assert len(transport.ops("edit_card")) == n_edits + 1
    assert not report3.edited


def test_losing_the_lease_mid_pass_aborts_before_the_next_task(board, parts):
    # Fencing: the lease is renewed per task; a holder that lost it must stop
    # publishing immediately instead of racing the new holder.
    store, transport, consumer = parts
    walk_lifecycle(board, "t_1")
    walk_lifecycle(board, "t_2")
    renewals = {"n": 0}
    real_renew = store.renew_lease

    def stolen_after_first(name, holder, token, *, now, ttl):
        renewals["n"] += 1
        if renewals["n"] >= 2:
            return False  # someone else holds it now
        return real_renew(name, holder, token, now=now, ttl=ttl)

    store.renew_lease = stolen_after_first
    report = consumer.run_once(now=NOW)
    assert report.opened == ["t_1"]  # t_2 never touched
    assert any("lease" in w for w in report.warnings)
    store.renew_lease = real_renew
    report2 = consumer.run_once(now=NOW + 10)  # a clean pass finishes the job
    assert report2.opened == ["t_2"]


def test_pinned_cursor_rescans_do_not_republish(board, parts):
    # A stuck task holds the board cursor down; other tasks are re-scanned
    # every pass with no new events of their own — that must cost zero
    # Discord traffic (no edit, no parent-dirtying), not a PATCH per pass.
    store, transport, consumer = parts
    walk_lifecycle(board, "t_pin")  # will get stuck: unknown create outcome
    walk_lifecycle(board, "t_ok")
    transport.queue_error("open_thread", OSError("connection dropped"))
    consumer.run_once(now=NOW)  # t_pin pends; t_ok fully published
    n = len(transport.calls)
    consumer.run_once(now=NOW + 10)  # rescan: t_pin skipped, t_ok no progress
    consumer.run_once(now=NOW + 20)
    assert len(transport.calls) == n


def test_one_poison_task_does_not_starve_the_rest(board, parts):
    store, transport, consumer = parts
    walk_lifecycle(board, "t_bad")
    walk_lifecycle(board, "t_good")
    transport.queue_error("open_thread", TransportError(400, {"message": "boom"}))
    report = consumer.run_once(now=NOW)
    assert store.get_post("default", "t_bad")["state"] == "dead_letter"
    assert "t_good" in report.opened


# --- comment excerpts -----------------------------------------------------------


def comment_and_event(conn, task_id, author, body, at=1_789_700_050):
    from conftest import add_comment_row

    add_comment_row(conn, task_id, author, body, created_at=at)
    add_event(conn, task_id, "commented", {"author": author, "len": len(body)}, created_at=at)


def test_comment_reply_carries_an_excerpt(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    comment_and_event(board, "t_1", "MarSan", "vault access approved — keys issued")
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert reply == "💬 MarSan: vault access approved — keys issued"


def test_long_comments_are_excerpted_with_a_link_to_the_full_text(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="p1",
        dashboard_url="https://dash.example",
        comment_excerpt_chars=40,
    )
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    comment_and_event(board, "t_1", "Coder", "x" * 400)
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert "x" * 39 + "…" in reply
    assert "[full comment](https://dash.example)" in reply
    assert "x" * 41 not in reply


def test_multiline_comments_collapse_in_the_excerpt(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    comment_and_event(board, "t_1", "Coder", "line one\n\n   line two")
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert "line one line two" in reply


def test_missing_comment_row_falls_back_to_the_old_line(board, parts):
    # a comment purged from the board, or an author/len mismatch: never guess
    _, transport, consumer = parts
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "commented", {"author": "ghost", "len": 99})
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert reply == "💬 ghost commented"


def test_excerpt_zero_disables_the_lookup(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="p1",
        comment_excerpt_chars=0,
    )
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    comment_and_event(board, "t_1", "Coder", "anything at all")
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert reply == "💬 Coder commented"


# --- reply permalinks -------------------------------------------------------------


def test_replies_link_to_the_dashboard_task_when_configured(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="p1",
        guild_id="9",
        dashboard_url="https://dash.example/tasks/{task_id}",
    )
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "commented", {"author": "ghost", "len": 99})
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert reply.endswith(" · [#](https://dash.example/tasks/t_1)")
    (_, _, card) = transport.ops("open_thread")[0]
    assert "[open in dashboard](https://dash.example/tasks/t_1)" in card.description


def test_replies_end_with_a_jump_link_to_the_card(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="p1",
        guild_id="9",
    )
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "commented", {"author": "ghost", "len": 99})
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert reply.endswith(" · [#](https://discord.com/channels/9/th1/msg1)")


def test_jump_link_survives_truncation(board, tmp_path):
    from kanban_task_threads.render import CONTENT_LIMIT

    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="default",
        holder="p1",
        guild_id="9",
    )
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    comment_and_event(board, "t_1", "Coder", "y" * 5000)
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert len(reply) <= CONTENT_LIMIT
    assert reply.endswith(")") and "[#](https://discord.com/channels/9/" in reply


def test_no_guild_means_no_link(board, parts):
    _, transport, consumer = parts  # default consumer: guild_id=""
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "commented", {"author": "ghost", "len": 99})
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert "[#]" not in reply


def test_cli_mirror_comments_are_suppressed(board, parts):
    # `hermes kanban block` writes both a `blocked` event AND a mirror comment
    # "BLOCKED: <reason>" — publishing both says the same thing twice. The
    # semantic reply wins; the mirror is skipped (its position still advances).
    _, transport, consumer = parts
    insert_task(board, "t_1", status="blocked", block_kind="needs_input")
    add_event(board, "t_1", "created", {"status": "ready"})
    comment_and_event(board, "t_1", "default", "BLOCKED: waiting on a decision")
    add_event(board, "t_1", "blocked", {"reason": "waiting on a decision", "kind": "needs_input"})
    consumer.run_once(now=NOW)
    replies = [c[2] for c in transport.ops("append")]
    assert len(replies) == 1
    assert replies[0].startswith("⛔ blocked")


def test_human_comments_that_merely_start_loud_still_publish(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    comment_and_event(board, "t_1", "diego", "BLOCKED FOR NOW on the vendor, see ticket")
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert "BLOCKED FOR NOW on the vendor" in reply  # no colon-prefix: not a mirror


def test_deleted_thread_400_10003_is_a_tombstone_not_a_dead_letter(board, parts):
    # A human deleting the whole THREAD surfaces as 400 {code: 10003 Unknown
    # Channel} — not as the 404 the message-level deletion gives. Same human
    # act, same verdict: tombstone (probed live, 2026-09-20).
    store, transport, consumer = parts
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    add_event(board, "t_1", "commented", {"author": "x", "len": 1})
    transport.queue_error(
        "append", TransportError(400, {"message": "Unknown Channel", "code": 10003})
    )
    consumer.run_once(now=NOW + 10)
    assert store.get_post("default", "t_1")["state"] == "tombstone"


def test_dashboard_url_substitutes_board_too(board, tmp_path):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db",
        store,
        transport,
        board="web",
        holder="p1",
        dashboard_url="https://dash.example/{board}/tasks/{task_id}",
    )
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "commented", {"author": "x", "len": 9})
    consumer.run_once(now=NOW)
    (reply,) = [c[2] for c in transport.ops("append")]
    assert reply.endswith("[#](https://dash.example/web/tasks/t_1)")
