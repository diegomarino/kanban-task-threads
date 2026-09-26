"""Shared-state publication across independently routed Kanban boards."""

import importlib
import json
import pathlib
import subprocess
import sys
import types

import pytest
from conftest import FakeHttp, FakeTransport, add_event, insert_task, make_board
from test_register import FakeCtx, load_entry_point

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.routes import BoardRoute
from kanban_task_threads.store import StateStore, state_db_path
from kanban_task_threads.transport import ForumTagSetupError

NOW = 1_789_700_100
REPO = pathlib.Path(__file__).resolve().parents[1]


def add_lifecycle(board, task_id="t_same"):
    insert_task(board, task_id, status="done")
    add_event(board, task_id, "created", {"status": "ready"})
    add_event(board, task_id, "commented", {"author": "agent", "len": 3})


def make_consumer(board_path, state_path, board, transport, destination):
    return Consumer(
        board_path,
        StateStore(state_path),
        transport,
        board=board,
        holder=f"holder-{board}",
        destination=destination,
    )


def assert_no_per_board_plugin_state(state_path, boards):
    assert state_path.exists()
    assert all(not (state_path.parent / f"{board}.db").exists() for board in boards)


def test_two_routes_publish_to_distinct_forums(tmp_path):
    state_path = state_db_path(tmp_path)
    fleet_board, web_board = make_board(tmp_path / "fleet.db"), make_board(tmp_path / "web.db")
    add_lifecycle(fleet_board)
    add_lifecycle(web_board)
    fleet_transport, web_transport = FakeTransport(), FakeTransport()
    fleet = make_consumer(
        tmp_path / "fleet.db", state_path, "fleet", fleet_transport, "discord:webhook:fleet@10"
    )
    web = make_consumer(
        tmp_path / "web.db", state_path, "web", web_transport, "discord:webhook:web@20"
    )

    assert fleet.run_once(now=NOW).opened == ["t_same"]
    assert web.run_once(now=NOW).opened == ["t_same"]

    fleet_post = fleet._store.get_post("fleet", "t_same")
    web_post = web._store.get_post("web", "t_same")
    assert fleet_transport.ops().count("open_thread") == 1
    assert web_transport.ops().count("open_thread") == 1
    assert fleet_transport.ops().count("append") == 1
    assert web_transport.ops().count("append") == 1
    assert fleet_post["destination"] == "discord:webhook:fleet@10"
    assert web_post["destination"] == "discord:webhook:web@20"
    assert_no_per_board_plugin_state(state_path, ("fleet", "web"))

    fleet.close()
    web.close()
    fleet_board.close()
    web_board.close()


def test_restart_resumes_both_routes(tmp_path):
    state_path = state_db_path(tmp_path)
    fleet_board, web_board = make_board(tmp_path / "fleet.db"), make_board(tmp_path / "web.db")
    add_lifecycle(fleet_board)
    add_lifecycle(web_board)
    first_fleet, first_web = FakeTransport(), FakeTransport()
    fleet = make_consumer(tmp_path / "fleet.db", state_path, "fleet", first_fleet, "fleet-forum")
    web = make_consumer(tmp_path / "web.db", state_path, "web", first_web, "web-forum")
    fleet.run_once(now=NOW)
    web.run_once(now=NOW)
    fleet_thread = fleet._store.get_post("fleet", "t_same")["thread_id"]
    web_thread = web._store.get_post("web", "t_same")["thread_id"]
    fleet.close()
    web.close()

    add_event(fleet_board, "t_same", "completed", {"summary": "fleet done"})
    add_event(web_board, "t_same", "completed", {"summary": "web done"})
    resumed_fleet, resumed_web = FakeTransport(), FakeTransport()
    fleet = make_consumer(tmp_path / "fleet.db", state_path, "fleet", resumed_fleet, "fleet-forum")
    web = make_consumer(tmp_path / "web.db", state_path, "web", resumed_web, "web-forum")

    fleet.run_once(now=NOW + 10)
    web.run_once(now=NOW + 10)

    assert resumed_fleet.ops("open_thread") == resumed_web.ops("open_thread") == []
    assert resumed_fleet.ops("append")[0][1].thread_id == fleet_thread
    assert resumed_web.ops("append")[0][1].thread_id == web_thread
    assert_no_per_board_plugin_state(state_path, ("fleet", "web"))

    fleet.close()
    web.close()
    fleet_board.close()
    web_board.close()


def _entry(monkeypatch, tmp_path, secrets, http):
    module = load_entry_point()
    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.get_secret = lambda name, default=None: secrets.get(name, default)
    secret_scope.current_secret_scope = lambda: secrets
    agent = types.ModuleType("agent")
    agent.secret_scope = secret_scope
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    kanban_db.get_current_board = lambda: "legacy"
    kanban_db.kanban_db_path = lambda board=None: tmp_path / f"{board or 'legacy'}.db"
    kanban_db.kanban_home = lambda: tmp_path
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.kanban_db = kanban_db
    for name, fake in (
        ("agent", agent),
        ("agent.secret_scope", secret_scope),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.kanban_db", kanban_db),
    ):
        monkeypatch.setitem(sys.modules, name, fake)
    transport = importlib.import_module("ktt_entry.kanban_task_threads.transport")
    monkeypatch.setattr(transport, "urllib_http", http)
    return module


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("missing_secret", "inactive"),
        ("rejected_webhook", "inactive"),
        ("transient_preflight", "retryable"),
        ("forum_permission", "permanent"),
    ],
)
def test_failed_route_does_not_block_healthy_route(tmp_path, monkeypatch, failure, expected):
    healthy_board = make_board(tmp_path / "healthy.db")
    add_lifecycle(healthy_board, "t_healthy")
    state_path = state_db_path(tmp_path)
    healthy_transport = FakeTransport()
    healthy = make_consumer(
        tmp_path / "healthy.db", state_path, "healthy", healthy_transport, "healthy-forum"
    )

    if failure == "forum_permission":
        failed_board = make_board(tmp_path / "failed.db")
        add_lifecycle(failed_board, "t_failed")
        failed_transport = FakeTransport({"tags"})
        failed_transport.record_prepare = True
        failed_transport.queue_error("prepare_forum", ForumTagSetupError("grant Manage Channels"))
        failed = make_consumer(
            tmp_path / "failed.db", state_path, "failed", failed_transport, "failed-forum"
        )
        failure_result = failed.run_once(now=NOW)
        assert failure_result.errors == ["Discord forum setup failed: grant Manage Channels"]
        failed.close()
        failed_board.close()
    else:
        http = FakeHttp()
        secrets = {"HEALTHY_WEBHOOK": "https://discord.com/api/webhooks/healthy/token"}
        if failure == "rejected_webhook":
            secrets["FAILED_WEBHOOK"] = "https://discord.com/api/webhooks/failed/token"
            http.queue(401, {"message": "Invalid Webhook Token"})
        elif failure == "transient_preflight":
            secrets["FAILED_WEBHOOK"] = "https://discord.com/api/webhooks/failed/token"
            http.queue(502, {"message": "bad gateway"})
        module = _entry(monkeypatch, tmp_path, secrets, http)
        if expected == "retryable":
            entry_runtime = importlib.import_module("ktt_entry.kanban_task_threads.runtime")
            with pytest.raises(entry_runtime.RetryableStartup):
                module._build_consumer(FakeCtx(), BoardRoute("failed", "FAILED_WEBHOOK"))
        else:
            assert module._build_consumer(FakeCtx(), BoardRoute("failed", "FAILED_WEBHOOK")) is None

    healthy_report = healthy.run_once(now=NOW)
    assert healthy_report.opened == ["t_healthy"]
    assert healthy_transport.ops().count("open_thread") == 1
    assert healthy._store.get_post("healthy", "t_healthy")["state"] == "live"
    assert_no_per_board_plugin_state(state_path, ("healthy", "failed"))
    healthy.close()
    healthy_board.close()


CONCURRENT_WORKER = """
import json, pathlib, sys, time
sys.path.insert(0, {repo!r})
from kanban_task_threads.consumer import Consumer
from kanban_task_threads.store import StateStore
from kanban_task_threads.transport import ThreadRef

class FileTransport:
    def __init__(self, log, board):
        self.log, self.board, self.n = pathlib.Path(log), board, 0
    def capabilities(self):
        return frozenset({{"rich_card", "live_timestamps", "per_message_identity"}})
    def open_thread(self, *, title, card):
        with self.log.open("a") as fh:
            fh.write(json.dumps({{"board": self.board, "op": "open_thread"}}) + "\\n")
        self.n += 1
        return ThreadRef(
            thread_id=f"{{self.board}}-th{{self.n}}", message_id=f"{{self.board}}-m{{self.n}}"
        )
    def edit_card(self, ref, card):
        pass
    def append(self, ref, *, content, username=None, message_type="default"):
        with self.log.open("a") as fh:
            fh.write(json.dumps({{"board": self.board, "op": "append"}}) + "\\n")
        self.n += 1
        return f"{{self.board}}-m{{self.n}}"

board_db, state_db, log, barrier, board = sys.argv[1:]
barrier = pathlib.Path(barrier)
(barrier / f"ready-{{board}}").touch()
deadline = time.monotonic() + 10
while len(list(barrier.glob("ready-*"))) < 2:
    if time.monotonic() >= deadline:
        raise RuntimeError("barrier timed out")
    time.sleep(0.005)
consumer = Consumer(board_db, StateStore(state_db), FileTransport(log, board), board=board,
                    holder=f"proc-{{board}}", destination=f"discord:{{board}}")
report = consumer.run_once()
if report.opened != ["t_same"] or report.replied != 1:
    raise RuntimeError(f"unexpected report: {{report}}")
consumer.close()
"""


def test_concurrent_boards_share_initial_state_file(tmp_path):
    state_path = state_db_path(tmp_path)
    for board_name in ("fleet", "web"):
        board = make_board(tmp_path / f"{board_name}.db")
        add_lifecycle(board)
        board.close()
    script = tmp_path / "concurrent_worker.py"
    script.write_text(CONCURRENT_WORKER.format(repo=str(REPO)))
    log, barrier = tmp_path / "ops.jsonl", tmp_path / "barrier"
    log.touch()
    barrier.mkdir()
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(tmp_path / f"{board_name}.db"),
                str(state_path),
                str(log),
                str(barrier),
                board_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for board_name in ("fleet", "web")
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], outputs

    ops = [json.loads(line) for line in log.read_text().splitlines()]
    assert sorted((entry["board"], entry["op"]) for entry in ops) == [
        ("fleet", "append"),
        ("fleet", "open_thread"),
        ("web", "append"),
        ("web", "open_thread"),
    ]
    store = StateStore(state_path)
    assert store.get_cursor("fleet") == store.get_cursor("web") == 2
    assert store.get_post("fleet", "t_same")["destination"] == "discord:fleet"
    assert store.get_post("web", "t_same")["destination"] == "discord:web"
    store.close()
    assert_no_per_board_plugin_state(state_path, ("fleet", "web"))


def test_changed_route_freezes_existing_destination(tmp_path):
    state_path = state_db_path(tmp_path)
    board = make_board(tmp_path / "fleet.db")
    add_lifecycle(board)
    original = make_consumer(tmp_path / "fleet.db", state_path, "fleet", FakeTransport(), "forum-A")
    original.run_once(now=NOW)
    original.close()
    add_event(board, "t_same", "completed", {"summary": "later"})
    moved_transport = FakeTransport()
    moved = make_consumer(tmp_path / "fleet.db", state_path, "fleet", moved_transport, "forum-B")

    report = moved.run_once(now=NOW + 10)

    assert moved_transport.calls == []
    assert any("destination changed" in error for error in report.errors)
    assert moved._store.get_post("fleet", "t_same")["destination"] == "forum-A"
    assert_no_per_board_plugin_state(state_path, ("fleet",))
    moved.close()
    board.close()
