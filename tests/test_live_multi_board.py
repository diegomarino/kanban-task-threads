"""Offline contracts for the explicitly-invoked two-board live probe.

Each test protects an operator-safety break: accepting an unsafe board path,
mixing two boards in state, leaking a credential, or abandoning a sibling
route after its peer fails.
"""

import importlib.util
import io
import pathlib
import sys

from conftest import add_event, insert_task, make_board

from kanban_task_threads.store import StateStore

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "live_multi_board.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("live_multi_board", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_env(repo, **values):
    (repo / ".env.local").write_text("\n".join(f"{key}={value}" for key, value in values.items()))


def make_repo(tmp_path, *, bot_token=False):
    repo = tmp_path / "repo"
    sandbox = repo / ".sandbox"
    sandbox.mkdir(parents=True)
    boards = []
    for board in ("fleet", "web"):
        connection = make_board(sandbox / f"{board}.db")
        insert_task(connection, "t_same", status="done")
        add_event(connection, "t_same", "created", {"status": "ready"})
        connection.close()
        boards.append(sandbox / f"{board}.db")
    values = {
        "FLEET_WEBHOOK": "https://discord.test/api/webhooks/fleet/secret-fleet",
        "WEB_WEBHOOK": "https://discord.test/api/webhooks/web/secret-web",
    }
    if bot_token:
        values["KANBAN_TASK_THREADS_BOT_TOKEN"] = "shared-test-bot-token"
    write_env(repo, **values)
    return repo, boards


class FakeHttp:
    def __init__(self, destinations=None):
        self.destinations = destinations or {"fleet": "forum-fleet", "web": "forum-web"}
        self.calls = []
        self.headers = []
        self.number = 0

    def __call__(self, method, url, body, headers=None):
        self.calls.append((method, url, body))
        self.headers.append(headers or {})
        route = "fleet" if "/fleet/" in url else "web"
        if method == "GET" and "/webhooks/" in url:
            return 200, {
                "id": f"hook-{route}",
                "channel_id": self.destinations[route],
                "guild_id": f"guild-{route}",
                "name": route,
            }
        self.number += 1
        return 200, {"id": f"message-{self.number}", "channel_id": f"thread-{self.number}"}


def argv(boards, *, second_secret="WEB_WEBHOOK"):
    return ["fleet", str(boards[0]), "FLEET_WEBHOOK", "web", str(boards[1]), second_secret]


def test_probe_runs_equal_task_ids_as_two_board_qualified_rows(tmp_path):
    """Would fail if the probe used one consumer, board, or state per route."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    output = io.StringIO()

    assert probe.main(argv(boards), repo=repo, http=FakeHttp(), stdout=output) == 0

    state = StateStore(repo / ".sandbox" / "live-multi-board-state.db")
    try:
        assert (
            state.get_post("fleet", "t_same")["destination"]
            == "discord:webhook:hook-fleet@forum-fleet"
        )
        assert (
            state.get_post("web", "t_same")["destination"] == "discord:webhook:hook-web@forum-web"
        )
    finally:
        state.close()
    assert "https://discord.test" not in output.getvalue()


def test_probe_rejects_external_and_traversal_board_paths_before_preflight(tmp_path):
    """Would fail if a supplied board path could escape the disposable sandbox."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    output = io.StringIO()
    http = FakeHttp()

    for unsafe_path in (tmp_path / "outside.db", repo / ".sandbox" / ".." / "outside.db"):
        assert probe.main(argv([unsafe_path, boards[1]]), repo=repo, http=http, stdout=output) == 2

    assert http.calls == []
    assert "outside.db" in output.getvalue()


def test_probe_rejects_duplicate_boards_and_secret_names_before_preflight(tmp_path):
    """Would fail if two routes could silently consume one board or credential."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    output = io.StringIO()
    http = FakeHttp()

    assert (
        probe.main(
            argv([boards[0], boards[0]], second_secret="FLEET_WEBHOOK"),
            repo=repo,
            http=http,
            stdout=output,
        )
        == 2
    )
    assert (
        probe.main(argv(boards, second_secret="FLEET_WEBHOOK"), repo=repo, http=http, stdout=output)
        == 2
    )

    assert http.calls == []
    assert "duplicate" in output.getvalue()


def test_probe_rejects_distinct_board_slugs_for_one_physical_board_before_preflight(tmp_path):
    """Would fail if aliases let two consumers publish from one SQLite event stream."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    output = io.StringIO()
    http = FakeHttp()

    assert (
        probe.main(
            ["fleet", str(boards[0]), "FLEET_WEBHOOK", "web", str(boards[0]), "WEB_WEBHOOK"],
            repo=repo,
            http=http,
            stdout=output,
        )
        == 2
    )

    assert http.calls == []
    assert "duplicate board database" in output.getvalue()


def test_probe_rejects_missing_named_secret_without_exposing_env_values(tmp_path):
    """Would fail if an absent route secret reached transport construction or output."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    write_env(repo, FLEET_WEBHOOK="https://discord.test/api/webhooks/fleet/secret-fleet")
    output = io.StringIO()

    assert probe.main(argv(boards), repo=repo, http=FakeHttp(), stdout=output) == 2

    assert "WEB_WEBHOOK" in output.getvalue()
    assert "secret-fleet" not in output.getvalue()


def test_probe_rejects_duplicate_preflight_destination_before_state_or_consumers(tmp_path):
    """Would fail if two webhook credentials could target one forum unnoticed."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    output = io.StringIO()

    assert (
        probe.main(
            argv(boards),
            repo=repo,
            http=FakeHttp({"fleet": "same-forum", "web": "same-forum"}),
            stdout=output,
        )
        == 2
    )

    assert not (repo / ".sandbox" / "live-multi-board-state.db").exists()
    assert "duplicate discovered forum destination" in output.getvalue()


def test_probe_shares_optional_bot_token_between_the_two_transports(tmp_path):
    """Would fail if one route omitted or replaced the optional shared bot token."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path, bot_token=True)
    output = io.StringIO()
    http = FakeHttp()

    assert probe.main(argv(boards), repo=repo, http=http, stdout=output) == 0

    assert all(
        headers.get("Authorization") == "Bot shared-test-bot-token"
        for headers in http.headers
        if headers
    )
    assert "shared-test-bot-token" not in output.getvalue()


def test_probe_closes_both_consumers_and_runs_sibling_after_pass_failure(tmp_path):
    """Would fail if one route exception skipped its sibling or leaked either state connection."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    output = io.StringIO()
    constructed = []

    class StubConsumer:
        def __init__(self, *_args, board, **_kwargs):
            self.board = board
            self.closed = False
            constructed.append(self)

        def run_once(self):
            if self.board == "fleet":
                raise RuntimeError("https://discord.test/api/webhooks/fleet/secret-fleet")
            return type(
                "Report",
                (),
                {"opened": [], "replied": 0, "edited": [], "errors": [], "warnings": []},
            )()

        def close(self):
            self.closed = True

    assert (
        probe.main(
            argv(boards), repo=repo, http=FakeHttp(), consumer_cls=StubConsumer, stdout=output
        )
        == 1
    )

    assert [consumer.board for consumer in constructed] == ["fleet", "web"]
    assert all(consumer.closed for consumer in constructed)
    assert "secret-fleet" not in output.getvalue()


def test_probe_closes_constructed_consumer_and_state_when_second_build_fails(tmp_path):
    """Would fail if a later constructor error bypassed cleanup for the first route."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path)
    output = io.StringIO()
    states = []
    consumers = []

    class StubState:
        def __init__(self, _path):
            self.closed = False
            states.append(self)

        def close(self):
            self.closed = True

    class StubConsumer:
        def __init__(self, _board_db, state, _transport, *, board, **_kwargs):
            if board == "web":
                raise RuntimeError("https://discord.test/api/webhooks/web/secret-web")
            self.state = state
            self.closed = False
            consumers.append(self)

        def close(self):
            self.closed = True
            self.state.close()

    assert (
        probe.main(
            argv(boards),
            repo=repo,
            http=FakeHttp(),
            consumer_cls=StubConsumer,
            state_store_cls=StubState,
            stdout=output,
        )
        == 2
    )

    assert len(consumers) == 1
    assert consumers[0].closed is True
    assert all(state.closed for state in states)
    assert "secret-web" not in output.getvalue()


def test_probe_redacts_report_errors_and_warnings_but_keeps_board_context(tmp_path):
    """Would fail if consumer-reported messages could disclose webhook or bot credentials."""
    probe = load_probe()
    repo, boards = make_repo(tmp_path, bot_token=True)
    output = io.StringIO()

    class StubConsumer:
        def __init__(self, *_args, board, **_kwargs):
            self.board = board

        def run_once(self):
            return type(
                "Report",
                (),
                {
                    "opened": [],
                    "replied": 0,
                    "edited": [],
                    "errors": [
                        "webhook https://discord.test/api/webhooks/fleet/secret-fleet "
                        "bot shared-test-bot-token"
                    ],
                    "warnings": [
                        "retry https://discord.test/api/webhooks/web/secret-web "
                        "bot shared-test-bot-token"
                    ],
                },
            )()

        def close(self):
            pass

    assert (
        probe.main(
            argv(boards), repo=repo, http=FakeHttp(), consumer_cls=StubConsumer, stdout=output
        )
        == 1
    )

    rendered = output.getvalue()
    assert "ERROR board=fleet:" in rendered
    assert "WARNING board=web:" in rendered
    assert "https://discord.test" not in rendered
    assert "secret-fleet" not in rendered
    assert "secret-web" not in rendered
    assert "shared-test-bot-token" not in rendered
