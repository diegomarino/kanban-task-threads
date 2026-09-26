"""Shared-state publication across independently routed Kanban boards."""

import importlib
import json
import logging
import pathlib
import subprocess
import sys
import threading
import time
import types

import pytest
from conftest import FakeHttp, FakeTransport, add_event, insert_task, make_board
from test_register import ConfiguredCtx, load_entry_point, plugin_threads

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.store import StateStore, state_db_path

NOW = 1_789_700_100
REPO = pathlib.Path(__file__).resolve().parents[1]


def add_lifecycle(board, task_id="t_same"):
    insert_task(board, task_id, status="done")
    add_event(board, task_id, "created", {"status": "ready"})
    add_event(board, task_id, "commented", {"author": "agent", "len": 3})


def assert_per_board_plugin_state(state_path, boards, legacy_bytes=None):
    assert state_path.exists()
    for board in boards:
        legacy_path = state_path.parent / f"{board}.db"
        if legacy_bytes is None:
            assert not legacy_path.exists()
        else:
            assert legacy_path.read_bytes() == legacy_bytes[board]


def seed_legacy_per_board_state(state_path, boards):
    result = {}
    for board in boards:
        legacy_path = state_path.parent / f"{board}.db"
        legacy = StateStore(legacy_path)
        legacy.begin_create(board, "t_same", now=1)
        legacy.complete_create(
            board,
            "t_same",
            thread_id=f"legacy-{board}-thread",
            message_id=f"legacy-{board}-message",
            destination=f"legacy:{board}",
        )
        assert legacy.advance_cursor(board, old=0, new=91) is True
        legacy.close()
        result[board] = legacy_path.read_bytes()
    return result


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def rows_for(state_path, boards):
    store = StateStore(state_path)
    try:
        return {board: store.get_post(board, "t_same") for board in boards}
    finally:
        store.close()


def routes_reached_event(state_path, boards, event_id):
    store = StateStore(state_path)
    try:
        return all(
            store.get_post(board, "t_same") is not None and store.get_cursor(board) == event_id
            for board in boards
        )
    finally:
        store.close()


def make_consumer(board_path, state_path, board, transport, destination):
    return Consumer(
        board_path,
        StateStore(state_path),
        transport,
        board=board,
        holder=f"holder-{board}",
        destination=destination,
    )


class RoutedHttp(FakeHttp):
    """Thread-safe fake Discord boundary keyed by webhook URL and forum id."""

    def __init__(self, webhooks, failures=()):
        super().__init__()
        self._webhooks = webhooks
        self._failures = frozenset(failures)
        self._lock = threading.Lock()
        self._message_number = 0

    def __call__(self, method, url, body, headers=None):
        with self._lock:
            self.calls.append((method, url, body))
            self.headers_seen.append(headers or {})
            route = next(
                (name for name, webhook in self._webhooks.items() if url.startswith(webhook)), None
            )
            if route is not None:
                if method == "GET":
                    if route == "failed" and "rejected_webhook" in self._failures:
                        return 401, {"message": "Invalid Webhook Token"}
                    if route == "failed" and "transient_preflight" in self._failures:
                        return 502, {"message": "bad gateway"}
                    return 200, {
                        "id": f"{route}-webhook",
                        "channel_id": f"{route}-forum",
                        "guild_id": f"{route}-guild",
                        "name": route,
                    }
                self._message_number += 1
                return 200, {
                    "id": f"{route}-message-{self._message_number}",
                    "channel_id": f"{route}-thread-{self._message_number}",
                }
            if method == "GET" and url.endswith("/failed-forum"):
                return 403, {"message": "Missing Access"}
            if method == "GET" and url.endswith("-forum"):
                return 200, {
                    "flags": 0,
                    "available_tags": [
                        {"id": f"tag-{index}", "name": name}
                        for index, name in enumerate(
                            (
                                "triage",
                                "todo",
                                "scheduled",
                                "ready",
                                "running",
                                "blocked",
                                "needs-human",
                                "review",
                                "done",
                                "archived",
                                "failed",
                            ),
                            start=1,
                        )
                    ],
                }
            return 200, {}


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
    monkeypatch.setattr(module, "_refresh_profile_context", lambda context, **_: context.copy())
    return module


def make_runtime_entry(monkeypatch, tmp_path, *, failure=()):
    webhooks = {
        "fleet": "https://discord.com/api/webhooks/fleet/token",
        "web": "https://discord.com/api/webhooks/web/token",
        "failed": "https://discord.com/api/webhooks/failed/token",
        "healthy": "https://discord.com/api/webhooks/healthy/token",
    }
    secrets = {
        "FLEET_WEBHOOK": webhooks["fleet"],
        "WEB_WEBHOOK": webhooks["web"],
        "HEALTHY_WEBHOOK": webhooks["healthy"],
    }
    if "missing_secret" not in failure:
        secrets["FAILED_WEBHOOK"] = webhooks["failed"]
    if "forum_permission" in failure:
        secrets["KANBAN_TASK_THREADS_BOT_TOKEN"] = "test-bot-token"
    return _entry(monkeypatch, tmp_path, secrets, RoutedHttp(webhooks, failure)), webhooks


def registered_group(ctx):
    return ctx.unload_callbacks[-1].__self__


def test_two_routes_publish_to_distinct_forums_through_entry_runtime(tmp_path, monkeypatch):
    state_path = state_db_path(tmp_path)
    fleet_board, web_board = make_board(tmp_path / "fleet.db"), make_board(tmp_path / "web.db")
    add_lifecycle(fleet_board)
    add_lifecycle(web_board)
    legacy_bytes = seed_legacy_per_board_state(state_path, ("fleet", "web"))
    module, webhooks = make_runtime_entry(monkeypatch, tmp_path)
    ctx = ConfiguredCtx(
        "publisher",
        {
            "poll_seconds": 0.01,
            "routes": [
                {"selector": {"board": "fleet"}, "webhook_secret": "FLEET_WEBHOOK"},
                {"selector": {"board": "web"}, "webhook_secret": "WEB_WEBHOOK"},
            ],
        },
    )
    module.register(ctx)
    try:
        assert wait_for(lambda: routes_reached_event(state_path, ("fleet", "web"), 2))
        group = registered_group(ctx)
        assert len(group._runtimes) == 2
        assert all(runtime._built for runtime in group._runtimes)
        store = StateStore(state_path)
        fleet_post, web_post = store.get_post("fleet", "t_same"), store.get_post("web", "t_same")
        assert fleet_post["destination"] == "discord:webhook:fleet-webhook@fleet-forum"
        assert web_post["destination"] == "discord:webhook:web-webhook@web-forum"
        assert fleet_post["thread_id"] != "legacy-fleet-thread"
        assert web_post["thread_id"] != "legacy-web-thread"
        assert store.get_cursor("fleet") == store.get_cursor("web") == 2
        store.close()
        http = importlib.import_module("ktt_entry.kanban_task_threads.transport").urllib_http
        post_urls = [url for method, url, _ in http.calls if method == "POST"]
        assert any(url.startswith(webhooks["fleet"]) for url in post_urls)
        assert any(url.startswith(webhooks["web"]) for url in post_urls)
        assert_per_board_plugin_state(state_path, ("fleet", "web"), legacy_bytes)
    finally:
        ctx.unload_callbacks[-1]()
        assert wait_for(lambda: not plugin_threads())
        fleet_board.close()
        web_board.close()


def test_restart_resumes_both_routes_through_entry_and_runtime_group(tmp_path, monkeypatch):
    state_path = state_db_path(tmp_path)
    fleet_board, web_board = make_board(tmp_path / "fleet.db"), make_board(tmp_path / "web.db")
    add_lifecycle(fleet_board)
    add_lifecycle(web_board)
    legacy_bytes = seed_legacy_per_board_state(state_path, ("fleet", "web"))
    module, webhooks = make_runtime_entry(monkeypatch, tmp_path)
    ctx = ConfiguredCtx(
        "publisher",
        {
            "poll_seconds": 0.01,
            "routes": [
                {"selector": {"board": "fleet"}, "webhook_secret": "FLEET_WEBHOOK"},
                {"selector": {"board": "web"}, "webhook_secret": "WEB_WEBHOOK"},
            ],
        },
    )
    module.register(ctx)
    try:
        assert wait_for(lambda: all(rows_for(state_path, ("fleet", "web")).values()))
        store = StateStore(state_path)
        first_threads = {
            board: store.get_post(board, "t_same")["thread_id"] for board in ("fleet", "web")
        }
        store.close()
        ctx.unload_callbacks[-1]()
        assert wait_for(lambda: not plugin_threads())

        add_event(fleet_board, "t_same", "completed", {"summary": "fleet done"})
        add_event(web_board, "t_same", "completed", {"summary": "web done"})
        module.register(ctx)
        assert wait_for(
            lambda: all(
                row["last_event_id"] == 3 for row in rows_for(state_path, ("fleet", "web")).values()
            )
        )
        store = StateStore(state_path)
        resumed_threads = {
            board: store.get_post(board, "t_same")["thread_id"] for board in ("fleet", "web")
        }
        assert resumed_threads == first_threads
        assert store.get_cursor("fleet") == store.get_cursor("web") == 3
        store.close()
        http = importlib.import_module("ktt_entry.kanban_task_threads.transport").urllib_http
        open_urls = [
            url for method, url, _ in http.calls if method == "POST" and "thread_id" not in url
        ]
        assert len(open_urls) == 2
        assert {url.split("?", 1)[0] for url in open_urls} == {webhooks["fleet"], webhooks["web"]}
        assert_per_board_plugin_state(state_path, ("fleet", "web"), legacy_bytes)
    finally:
        ctx.unload_callbacks[-1]()
        assert wait_for(lambda: not plugin_threads())
        fleet_board.close()
        web_board.close()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_secret",
        "rejected_webhook",
        "transient_preflight",
        "forum_permission",
    ],
)
def test_failed_route_does_not_block_healthy_route_in_one_runtime_group(
    tmp_path, monkeypatch, caplog, failure
):
    caplog.set_level(logging.DEBUG)
    healthy_board = make_board(tmp_path / "healthy.db")
    failed_board = make_board(tmp_path / "failed.db")
    add_lifecycle(healthy_board)
    add_lifecycle(failed_board)
    state_path = state_db_path(tmp_path)
    module, webhooks = make_runtime_entry(monkeypatch, tmp_path, failure=(failure,))
    ctx = ConfiguredCtx(
        "publisher",
        {
            "poll_seconds": 0.01,
            "routes": [
                {"selector": {"board": "failed"}, "webhook_secret": "FAILED_WEBHOOK"},
                {"selector": {"board": "healthy"}, "webhook_secret": "HEALTHY_WEBHOOK"},
            ],
        },
    )
    module.register(ctx)
    try:
        assert wait_for(lambda: rows_for(state_path, ("healthy",))["healthy"] is not None)
        group = registered_group(ctx)
        failed_runtime, healthy_runtime = group._runtimes
        assert healthy_runtime._built is True
        http = importlib.import_module("ktt_entry.kanban_task_threads.transport").urllib_http
        assert any(
            method == "POST" and url.startswith(webhooks["healthy"])
            for method, url, _ in http.calls
        )

        if failure in {"missing_secret", "rejected_webhook"}:
            assert wait_for(lambda: failed_runtime._disabled)
            assert failed_runtime._disabled is True
            assert failed_runtime._built is False
            expected_log = (
                "webhook is not set for board 'failed'"
                if failure == "missing_secret"
                else "Discord rejected the webhook for board 'failed'"
            )
            assert any(expected_log in record.message for record in caplog.records)
        elif failure == "transient_preflight":
            assert wait_for(
                lambda: any("startup deferred" in record.message for record in caplog.records)
            )
            assert failed_runtime._disabled is False
            assert failed_runtime._built is False
        else:
            assert wait_for(
                lambda: any("forum setup failed" in record.message for record in caplog.records)
            )
            assert failed_runtime._disabled is False
            assert failed_runtime._built is True
            assert rows_for(state_path, ("failed",))["failed"] is None

        assert_per_board_plugin_state(state_path, ("healthy", "failed"))
    finally:
        ctx.unload_callbacks[-1]()
        assert wait_for(lambda: not plugin_threads())
        healthy_board.close()
        failed_board.close()


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
    assert_per_board_plugin_state(state_path, ("fleet", "web"))


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
    assert_per_board_plugin_state(state_path, ("fleet",))
    moved.close()
    board.close()
