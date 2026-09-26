"""Startup classification (ADR-0003): a missing profile scope or a Discord
blip is RetryableStartup (interval retry, sooner on a kick); a genuinely absent
secret or rejected webhook is a config verdict (None, permanent no-op)."""

import sqlite3
import sys
import types

import pytest
from conftest import FakeHttp
from test_register import FakeCtx, load_entry_point

from kanban_task_threads.routes import BoardRoute


def install_fake_hermes(monkeypatch, tmp_path, secrets, scope_present=True):
    def get_secret(name, default=None):
        return secrets.get(name, default)

    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.get_secret = get_secret
    secret_scope.current_secret_scope = lambda: secrets if scope_present else None
    agent = types.ModuleType("agent")
    agent.secret_scope = secret_scope
    kanban_db = types.ModuleType("hermes_cli.kanban_db")
    kanban_db.get_current_board = lambda: "default"
    kanban_db.kanban_db_path = lambda board=None: tmp_path / "kanban.db"
    kanban_db.kanban_home = lambda: tmp_path
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.kanban_db = kanban_db
    for name, module in [
        ("agent", agent),
        ("agent.secret_scope", secret_scope),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.kanban_db", kanban_db),
    ]:
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def entry(monkeypatch, tmp_path):
    module = load_entry_point()

    def prepare(secrets, scope_present=True, http=None):
        install_fake_hermes(monkeypatch, tmp_path, secrets, scope_present)
        import importlib

        transport_mod = importlib.import_module("ktt_entry.kanban_task_threads.transport")
        monkeypatch.setattr(transport_mod, "urllib_http", http or FakeHttp())
        return module

    return prepare


WEBHOOK = {"KANBAN_TASK_THREADS_WEBHOOK_URL": "https://discord.com/api/webhooks/1/t"}


def retryable_of(module):
    import importlib

    return importlib.import_module("ktt_entry.kanban_task_threads.runtime").RetryableStartup


def test_no_scope_and_no_secret_is_retryable(entry):
    module = entry({}, scope_present=False)
    with pytest.raises(retryable_of(module)):
        module._build_consumer(FakeCtx())


def test_unscoped_explicit_route_names_its_board_and_secret(entry):
    module = entry({}, scope_present=False)

    with pytest.raises(retryable_of(module)) as raised:
        module._build_consumer(FakeCtx(), BoardRoute("fleet", "FLEET_WEBHOOK"))

    message = str(raised.value)
    assert "fleet" in message
    assert "FLEET_WEBHOOK" in message
    assert "KANBAN_TASK_THREADS_WEBHOOK_URL" not in message


def test_scope_present_without_secret_is_a_config_verdict(entry):
    module = entry({}, scope_present=True)
    assert module._build_consumer(FakeCtx()) is None


def test_secret_scope_error_is_retryable(entry, monkeypatch):
    module = entry(WEBHOOK)
    sys.modules["agent.secret_scope"].get_secret = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("unscoped")
    )
    with pytest.raises(retryable_of(module)):
        module._build_consumer(FakeCtx())


def test_preflight_5xx_is_retryable(entry):
    http = FakeHttp()
    http.queue(502, {"message": "bad gateway"})
    module = entry(WEBHOOK, http=http)
    with pytest.raises(retryable_of(module)):
        module._build_consumer(FakeCtx())


def test_preflight_network_error_is_retryable(entry):
    def exploding_http(method, url, body, headers=None):
        raise OSError("dns failure")

    module = entry(WEBHOOK, http=exploding_http)
    with pytest.raises(retryable_of(module)):
        module._build_consumer(FakeCtx())


def test_rejected_webhook_is_a_config_verdict(entry):
    http = FakeHttp()
    http.queue(401, {"message": "Invalid Webhook Token"})
    module = entry(WEBHOOK, http=http)
    assert module._build_consumer(FakeCtx()) is None


def test_bot_build_defers_tag_provisioning_until_the_consumer_holds_the_lease(entry):
    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    secrets = dict(WEBHOOK, KANBAN_TASK_THREADS_BOT_TOKEN="bot")
    module = entry(secrets, http=http)
    assert module._build_consumer(FakeCtx()) is not None
    assert [call[0] for call in http.calls] == ["GET"]


def test_bot_token_wiring_can_resolve_forum_tags(entry):
    # The preflight-discovered channel_id must reach the transport so tag
    # resolution can query the correct forum.
    import importlib

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})  # webhook_info
    http.queue(
        200,
        {
            "id": "7",
            "flags": 0,
            "available_tags": [
                {"id": f"tag-{index}", "name": name}
                for index, name in enumerate(
                    [
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
                    ],
                    start=1,
                )
            ],
        },
    )
    secrets = dict(WEBHOOK, KANBAN_TASK_THREADS_BOT_TOKEN="bot")
    module = entry(secrets, http=http)
    consumer = module._build_consumer(FakeCtx())
    transport_mod = importlib.import_module("ktt_entry.kanban_task_threads.transport")
    http.queue(200, {})
    ref = transport_mod.ThreadRef(thread_id="901", message_id="900")
    assert consumer._transport.set_status_tag(ref, "done") is True


def test_happy_path_builds_a_consumer(entry, tmp_path):
    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)
    consumer = module._build_consumer(FakeCtx())
    assert consumer is not None
    assert (tmp_path / "kanban" / "plugins" / "kanban-task-threads" / "threads-state.db").exists()
    consumer.close()


def test_route_builder_uses_named_secret(entry):
    http = FakeHttp()
    http.queue(200, {"id": "fleet-webhook", "channel_id": "fleet-forum", "name": "fleet"})
    http.queue(200, {"id": "legacy-webhook", "channel_id": "legacy-forum", "name": "legacy"})
    secrets = {
        "FLEET_WEBHOOK": "https://discord.com/api/webhooks/fleet/token",
        "KANBAN_TASK_THREADS_WEBHOOK_URL": "https://discord.com/api/webhooks/legacy/token",
        "KANBAN_TASK_THREADS_BOT_TOKEN": "shared-bot-token",
    }
    module = entry(secrets, http=http)
    current_board_calls = []
    sys.modules["hermes_cli.kanban_db"].get_current_board = lambda: (
        current_board_calls.append("called") or "legacy"
    )

    consumer = module._build_consumer(FakeCtx(), BoardRoute("fleet", "FLEET_WEBHOOK"))

    assert consumer._board == "fleet"
    assert consumer._transport._bot_token == "shared-bot-token"
    assert http.calls[0][1] == "https://discord.com/api/webhooks/fleet/token"
    assert current_board_calls == []
    consumer.close()

    legacy = module._build_consumer(FakeCtx(), BoardRoute(None, "KANBAN_TASK_THREADS_WEBHOOK_URL"))

    assert legacy._board == "legacy"
    assert current_board_calls == ["called"]
    legacy.close()


def test_legacy_state_is_untouched(entry, tmp_path):
    from kanban_task_threads.store import StateStore, state_db_path

    old_path = tmp_path / "kanban" / "plugins" / "kanban-task-threads" / "default.db"
    old_store = StateStore(old_path)
    assert old_store.advance_cursor("default", old=0, new=23) is True
    old_store.close()
    old_bytes = old_path.read_bytes()

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    consumer = entry(WEBHOOK, http=http)._build_consumer(FakeCtx())

    assert old_path.read_bytes() == old_bytes
    assert consumer._store.get_cursor("default") == 0
    assert state_db_path(tmp_path).exists()
    consumer.close()


def test_build_failure_closes_opened_state(entry, monkeypatch):
    import importlib

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)
    consumer_mod = importlib.import_module("ktt_entry.kanban_task_threads.consumer")
    captured = {}

    def failing_consumer(board_db_path, state, transport, **kwargs):
        captured["state"] = state
        raise RuntimeError("consumer construction failed")

    monkeypatch.setattr(consumer_mod, "Consumer", failing_consumer)

    with pytest.raises(RuntimeError, match="consumer construction failed"):
        module._build_consumer(FakeCtx())
    with pytest.raises(sqlite3.ProgrammingError):
        captured["state"].get_cursor("default")


def test_route_preflight_error_names_board_without_logging_credentials(entry, caplog):
    import logging

    http = FakeHttp()
    http.queue(401, {"detail": "sensitive-webhook-value"})
    module = entry({"FLEET_WEBHOOK": "https://discord.com/api/webhooks/fleet/token"}, http=http)

    with caplog.at_level(logging.ERROR):
        assert module._build_consumer(FakeCtx(), BoardRoute("fleet", "FLEET_WEBHOOK")) is None

    assert "fleet" in caplog.text
    assert "sensitive-webhook-value" not in caplog.text


def test_literal_ip_dashboard_url_warns_but_proceeds(entry, caplog):
    import logging

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)

    class Ctx(FakeCtx):
        def get_config(self, key, default=None):
            if key == "dashboard_url":
                return "http://192.168.1.34:9119/kanban"
            return default

    with caplog.at_level(logging.WARNING):
        consumer = module._build_consumer(Ctx())
    assert consumer is not None  # advisory, never a verdict
    assert any("frozen" in r.message or "stable name" in r.message for r in caplog.records)


def test_zero_config_uses_the_official_versioned_avatar_catalog(entry):
    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)

    consumer = module._build_consumer(FakeCtx())

    assert consumer._transport._avatars.url_for("blocked") == (
        "https://diegomarino.github.io/kanban-task-threads/v1/duotone/colored/96px/blocked.png"
    )


def test_avatars_can_be_disabled_explicitly(entry):
    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)

    class Ctx(FakeCtx):
        def get_config(self, key, default=None):
            if key == "avatars_enabled":
                return False
            return default

    consumer = module._build_consumer(Ctx())

    assert consumer._transport._avatars is None


def test_custom_avatar_directory_ignores_official_theme_and_palette(entry, caplog):
    import logging

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)

    class Ctx(FakeCtx):
        settings = {
            "avatar_base_url": "https://assets.example.invalid/ktt",
            "avatar_theme": "fill",
            "avatar_palette": "black",
        }

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

    with caplog.at_level(logging.WARNING):
        consumer = module._build_consumer(Ctx())

    assert consumer._transport._avatars.url_for("blocked") == (
        "https://assets.example.invalid/ktt/blocked.png"
    )
    assert any("ignored" in record.message for record in caplog.records)


def test_invalid_custom_avatar_directory_disables_only_avatars(entry, caplog):
    import logging

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)

    class Ctx(FakeCtx):
        def get_config(self, key, default=None):
            if key == "avatar_base_url":
                return "http://mutable.example.invalid/latest"
            return default

    with caplog.at_level(logging.WARNING):
        consumer = module._build_consumer(Ctx())

    assert consumer is not None
    assert consumer._transport._avatars is None
    assert [call[0] for call in http.calls] == ["GET"]
    assert any(
        "avatar" in record.message and "HTTPS" in record.message for record in caplog.records
    )


def test_invalid_official_selection_falls_back_to_defaults(entry, caplog):
    import logging

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)

    class Ctx(FakeCtx):
        def get_config(self, key, default=None):
            if key == "avatar_theme":
                return "thin"
            return default

    with caplog.at_level(logging.WARNING):
        consumer = module._build_consumer(Ctx())

    assert consumer._transport._avatars.url_for("blocked") == (
        "https://diegomarino.github.io/kanban-task-threads/v1/duotone/colored/96px/blocked.png"
    )
    assert any("falling back" in record.message for record in caplog.records)


def test_malformed_official_selection_cannot_stop_publication(entry, caplog):
    import logging

    http = FakeHttp()
    http.queue(200, {"id": "1", "channel_id": "7", "name": "taskz"})
    module = entry(WEBHOOK, http=http)

    class Ctx(FakeCtx):
        def get_config(self, key, default=None):
            if key == "avatar_theme":
                return []
            return default

    with caplog.at_level(logging.WARNING):
        consumer = module._build_consumer(Ctx())

    assert consumer is not None
    assert consumer._transport._avatars.url_for("blocked").endswith(
        "/v1/duotone/colored/96px/blocked.png"
    )
