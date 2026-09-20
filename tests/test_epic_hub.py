"""The epic hub, on task_links' REAL semantics: `parent_id` is a
PREREQUISITE — a child cannot complete until every parent is done/archived
(`_parents_satisfied` in kanban_db). An "epic" is therefore the *child* of its
subtasks: it completes last, gated by them.

The hub follows the graph's true direction: the dependent's card carries a
live `depends on:` rollup of its prerequisites; a prerequisite's card names
what it `unlocks:`; a prerequisite opening its thread leaves a link-reply in
its dependents' threads; and any progress on a prerequisite marks its
dependents' cards dirty so the rollup stays current."""

import pytest
from conftest import FakeTransport, add_event, insert_task, link_tasks, make_board

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.store import StateStore

NOW = 1_789_700_100


@pytest.fixture
def board(tmp_path):
    return make_board(tmp_path / "kanban.db")


@pytest.fixture
def parts(tmp_path, board):
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db", store, transport, board="default", holder="p1", guild_id="9"
    )
    return store, transport, consumer


def epic_with_subtasks(board, statuses=("done", "done", "running")):
    # the epic DEPENDS on its subtasks: each subtask is a parent of the epic
    insert_task(board, "t_epic", title="the epic", status="running", last_heartbeat_at=NOW)
    add_event(board, "t_epic", "created", {"status": "ready"})
    for i, status in enumerate(statuses):
        sub = f"t_s{i}"
        insert_task(
            board,
            sub,
            title=f"subtask {i}",
            status=status,
            last_heartbeat_at=NOW,
            body=f"the plan for subtask {i}, in some detail",
        )
        link_tasks(board, sub, "t_epic")  # parent=subtask (prerequisite), child=epic
        add_event(board, sub, "created", {"status": "ready"})


def test_dependent_card_carries_the_depends_rollup(board, parts):
    _, transport, consumer = parts
    epic_with_subtasks(board)
    consumer.run_once(now=NOW)
    epic_opens = [c for c in transport.ops("open_thread") if c[1].endswith("t_epic")]
    (card,) = [c[2] for c in epic_opens]
    # active states lead the rollup; terminal ones close it
    assert "depends on: 1 running · 2 done" in card.description


def test_prerequisite_card_names_what_it_unlocks(board, parts):
    _, transport, consumer = parts
    epic_with_subtasks(board, statuses=("running",))
    consumer.run_once(now=NOW)
    sub_opens = [c for c in transport.ops("open_thread") if c[1].endswith("t_s0")]
    (card,) = [c[2] for c in sub_opens]
    assert "unlocks: t_epic — the epic" in card.description


def test_prerequisite_opening_links_into_the_dependent_thread(board, parts):
    store, transport, consumer = parts
    epic_with_subtasks(board, statuses=("running",))
    consumer.run_once(now=NOW)
    epic_ref = store.get_post("default", "t_epic")["thread_id"]
    links = [c for c in transport.ops("append") if c[1].thread_id == epic_ref]
    assert len(links) == 1
    content = links[0][2]
    sub_thread = store.get_post("default", "t_s0")["thread_id"]
    # the title links to the new thread; assignee and a body excerpt give context
    assert f"[subtask 0 · t_s0](https://discord.com/channels/9/{sub_thread})" in content
    assert "assignee: sandbox" in content
    assert "> the plan for subtask 0" in content


def test_prerequisite_progress_refreshes_the_dependent_rollup(board, parts):
    store, transport, consumer = parts
    epic_with_subtasks(board, statuses=("running",))
    consumer.run_once(now=NOW)
    board.execute("UPDATE tasks SET status='done' WHERE id='t_s0'")
    board.commit()
    add_event(board, "t_s0", "completed", {"summary": "ok"})
    report = consumer.run_once(now=NOW + 10)
    assert "t_epic" in report.edited  # dependent repainted without its own event
    epic_ref = store.get_post("default", "t_epic")["thread_id"]
    epic_edits = [c for c in transport.ops("edit_card") if c[1].thread_id == epic_ref]
    assert "depends on: 1 done" in epic_edits[-1][2].description


def test_unlinked_tasks_pay_nothing(board, parts):
    _, transport, consumer = parts
    insert_task(board, "t_solo", status="running", last_heartbeat_at=NOW)
    add_event(board, "t_solo", "created", {"status": "ready"})
    consumer.run_once(now=NOW)
    (_, _, card) = transport.ops("open_thread")[0]
    assert "depends on" not in card.description
    assert "unlocks" not in card.description
