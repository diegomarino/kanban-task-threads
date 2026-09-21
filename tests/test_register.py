"""The entry point contract: register() starts only a deferred runtime thread,
every callback accepts **kwargs, unload tears down, and the manifest matches."""

import contextvars
import importlib
import importlib.util
import pathlib
import re
import sys
import threading
import time
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_entry_point():
    spec = importlib.util.spec_from_file_location(
        "ktt_entry", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ktt_entry"] = module
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    profile_name = "default"

    def __init__(self):
        self.hooks = {}
        self.unload_callbacks = []

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.unload_callbacks.append(callback)

    def get_config(self, key, default=None):
        return default


class ConfiguredCtx(FakeCtx):
    def __init__(self, profile_name, settings):
        super().__init__()
        self.profile_name = profile_name
        self.settings = settings

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


def plugin_threads():
    return [t for t in threading.enumerate() if t.name == "kanban-task-threads" and t.is_alive()]


def test_profile_context_refresh_rebuilds_the_captured_profiles_secret_scope(monkeypatch, tmp_path):
    module = load_entry_point()
    active_scope = contextvars.ContextVar("active_scope", default=None)
    latest = {"KANBAN_TASK_THREADS_WEBHOOK_URL": "old"}
    hydrated = []

    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.build_profile_secret_scope = lambda home: dict(latest)
    secret_scope.set_secret_scope = active_scope.set
    agent = types.ModuleType("agent")
    agent.secret_scope = secret_scope

    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: tmp_path / "profile"
    hermes_constants.get_process_hermes_home = lambda: tmp_path / "other-profile"
    hermes_constants.set_hermes_home_override = lambda home: None

    env_loader = types.ModuleType("hermes_cli.env_loader")
    env_loader.hydrate_profile_secret_sources = lambda home: hydrated.append(home)
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.env_loader = env_loader

    for name, fake in (
        ("agent", agent),
        ("agent.secret_scope", secret_scope),
        ("hermes_constants", hermes_constants),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.env_loader", env_loader),
    ):
        monkeypatch.setitem(sys.modules, name, fake)

    base = contextvars.copy_context()
    first = module._refresh_profile_context(base)
    latest["KANBAN_TASK_THREADS_WEBHOOK_URL"] = "repaired"
    second = module._refresh_profile_context(base)

    assert first.run(active_scope.get) == {"KANBAN_TASK_THREADS_WEBHOOK_URL": "old"}
    assert second.run(active_scope.get) == {"KANBAN_TASK_THREADS_WEBHOOK_URL": "repaired"}
    assert hydrated == [tmp_path / "profile", tmp_path / "profile"]


def test_profile_refresh_pins_home_but_keeps_the_latest_attempt_context(monkeypatch, tmp_path):
    module = load_entry_point()
    active_home = contextvars.ContextVar("active_home", default=tmp_path / "process")
    request_marker = contextvars.ContextVar("request_marker", default="missing")
    observed = []

    secret_scope = types.ModuleType("agent.secret_scope")
    secret_scope.build_profile_secret_scope = lambda home: {
        "home": str(home),
        "marker": request_marker.get(),
    }
    secret_scope.set_secret_scope = lambda scope: observed.append(scope)
    agent = types.ModuleType("agent")
    agent.secret_scope = secret_scope

    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = active_home.get
    hermes_constants.get_process_hermes_home = lambda: tmp_path / "process"
    hermes_constants.set_hermes_home_override = active_home.set

    env_loader = types.ModuleType("hermes_cli.env_loader")
    env_loader.hydrate_profile_secret_sources = lambda home: None
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.env_loader = env_loader
    launch_policy = types.ModuleType("tui_gateway.launch_profile_policy")
    launch_policy.launch_secret_scope = lambda home: {
        "home": str(home),
        "marker": request_marker.get(),
    }
    tui_gateway = types.ModuleType("tui_gateway")
    tui_gateway.launch_profile_policy = launch_policy

    for name, fake in (
        ("agent", agent),
        ("agent.secret_scope", secret_scope),
        ("hermes_constants", hermes_constants),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.env_loader", env_loader),
        ("tui_gateway", tui_gateway),
        ("tui_gateway.launch_profile_policy", launch_policy),
    ):
        monkeypatch.setitem(sys.modules, name, fake)

    active_home.set(tmp_path / "registered-profile")
    registered = contextvars.copy_context()
    active_home.set(tmp_path / "wrong-profile")
    request_marker.set("latest-kick")
    attempt = contextvars.copy_context()

    refreshed = module._refresh_profile_context(attempt, identity_context=registered)

    assert refreshed.run(active_home.get) == tmp_path / "registered-profile"
    assert refreshed.run(request_marker.get) == "latest-kick"
    assert observed == [{"home": str(tmp_path / "registered-profile"), "marker": "latest-kick"}]

    observed.clear()
    request_marker.set("later-kick")
    attempt = contextvars.copy_context()
    refreshed = module._refresh_profile_context(
        attempt,
        identity_context=contextvars.Context(),
    )

    assert refreshed.run(active_home.get) == tmp_path / "process"
    assert refreshed.run(request_marker.get) == "later-kick"
    assert observed == [{"home": str(tmp_path / "process"), "marker": "later-kick"}]


def test_register_registers_hooks_and_unload():
    ctx = FakeCtx()
    module = load_entry_point()
    module.register(ctx)
    try:
        assert ctx.hooks, "no hooks registered"
        assert len(ctx.unload_callbacks) == 1, "ctx.on_unload not registered"
    finally:
        ctx.unload_callbacks[0]()


def test_matching_publisher_profile_starts_the_runtime():
    ctx = ConfiguredCtx("publisher", {"publisher_profile": "publisher"})
    load_entry_point().register(ctx)
    try:
        assert ctx.hooks
        assert len(ctx.unload_callbacks) == 1
        assert plugin_threads()
    finally:
        ctx.unload_callbacks[0]()


def test_non_matching_publisher_profile_is_inert():
    ctx = ConfiguredCtx("worker", {"publisher_profile": "publisher"})
    load_entry_point().register(ctx)
    assert ctx.hooks == {}
    assert ctx.unload_callbacks == []
    assert plugin_threads() == []


def test_publisher_profile_match_does_not_normalize_whitespace():
    ctx = ConfiguredCtx("publisher", {"publisher_profile": " publisher "})
    load_entry_point().register(ctx)
    try:
        assert ctx.hooks == {}
        assert ctx.unload_callbacks == []
        assert plugin_threads() == []
    finally:
        for callback in ctx.unload_callbacks:
            callback()


def test_register_wires_fresh_profile_context_into_the_runtime(monkeypatch):
    module = load_entry_point()
    runtime_module = importlib.import_module("ktt_entry.kanban_task_threads.runtime")
    captured = {}

    class RecordingRuntime:
        def __init__(self, build, **kwargs):
            captured.update(kwargs)

        def start(self, context=None):
            captured["start_context"] = context

        def kick(self, context=None):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(runtime_module, "Runtime", RecordingRuntime)
    module.register(FakeCtx())

    assert callable(captured["refresh_context"])
    assert isinstance(captured["start_context"], contextvars.Context)


def test_register_installs_unload_before_runtime_start(monkeypatch):
    module = load_entry_point()
    runtime_module = importlib.import_module("ktt_entry.kanban_task_threads.runtime")
    events = []

    class OrderedCtx(FakeCtx):
        def on_unload(self, callback):
            events.append("unload")
            super().on_unload(callback)

    class RecordingRuntime:
        def __init__(self, build, **kwargs):
            pass

        def start(self, context=None):
            events.append("start")

        def kick(self, context=None):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(runtime_module, "Runtime", RecordingRuntime)
    module.register(OrderedCtx())

    assert events == ["unload", "start"]


def test_an_empty_kick_cannot_replace_the_registered_profile_identity(monkeypatch):
    profile = contextvars.ContextVar("profile", default="process-default")
    profile.set("registered-profile")
    module = load_entry_point()
    runtime_module = importlib.import_module("ktt_entry.kanban_task_threads.runtime")
    captured = {}

    class RecordingRuntime:
        def __init__(self, build, **kwargs):
            captured.update(kwargs)

        def start(self, context=None):
            captured["start_context"] = context

        def kick(self, context=None):
            pass

        def shutdown(self):
            pass

    def copy_profile_context(base, **kwargs):
        return kwargs["identity_context"].copy()

    monkeypatch.setattr(runtime_module, "Runtime", RecordingRuntime)
    monkeypatch.setattr(module, "_refresh_profile_context", copy_profile_context)
    module.register(FakeCtx())

    refreshed = captured["refresh_context"](contextvars.Context())
    assert refreshed.run(profile.get) == "registered-profile"


def test_register_starts_the_runtime_without_any_kick():
    # Every profile that loads the plugin is a lease candidate, not just the one
    # that happens to hold Hermes' singleton dispatcher lock (the only process
    # where kanban hooks fire). Without this the publisher is decided by gateway
    # start order and has no failover.
    ctx = FakeCtx()
    load_entry_point().register(ctx)
    try:
        assert plugin_threads(), "register() left the runtime asleep until a hook"
    finally:
        ctx.unload_callbacks[0]()
    assert not plugin_threads(), "unload did not stop what register() started"


def test_every_hook_callback_accepts_arbitrary_kwargs():
    ctx = FakeCtx()
    load_entry_point().register(ctx)
    for callback in ctx.hooks.values():
        callback(
            telemetry_schema_version=1,
            task_id="t_x",
            board="default",
            unexpected_future_field=object(),
        )
    # cleanup: register() already started the deferred runtime
    ctx.unload_callbacks[0]()
    assert not plugin_threads()


def test_unload_stops_what_a_kick_started():
    # Diego's criterion at the entry-point level: discover_plugins(force=True)
    # and `hermes plugins disable` both invoke this; an orphan thread here
    # would contest the lease from a module nobody can reach anymore.
    ctx = FakeCtx()
    load_entry_point().register(ctx)
    next(iter(ctx.hooks.values()))(task_id="t_x")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not plugin_threads():
        time.sleep(0.01)  # give the kick a beat to spawn (it may degrade fast)
        break
    ctx.unload_callbacks[0]()
    assert not plugin_threads()


def test_manifest_declares_exactly_the_registered_hooks():
    ctx = FakeCtx()
    load_entry_point().register(ctx)
    manifest = (ROOT / "plugin.yaml").read_text()
    block = re.search(r"provides_hooks:\n((?:  - .+\n)+)", manifest)
    assert block, "provides_hooks missing from plugin.yaml"
    declared = set(re.findall(r"  - (\S+)", block.group(1)))
    assert declared == set(ctx.hooks), (
        f"declared {sorted(declared)} != registered {sorted(ctx.hooks)}"
    )
