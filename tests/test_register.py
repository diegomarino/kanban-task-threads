"""The entry point contract: register() only registers (validate probes it
with a stub context), every callback accepts **kwargs (doctor errors
otherwise), the unload callback really tears down, and plugin.yaml declares
exactly what gets registered (validate fails on a mismatch)."""

import importlib.util
import pathlib
import re
import sys
import threading
import time

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


def plugin_threads():
    return [t for t in threading.enumerate() if t.name == "kanban-task-threads" and t.is_alive()]


def test_register_only_registers():
    ctx = FakeCtx()
    load_entry_point().register(ctx)
    assert ctx.hooks, "no hooks registered"
    assert len(ctx.unload_callbacks) == 1, "ctx.on_unload not registered"
    assert not plugin_threads(), "register() started real work"


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
    # cleanup: callbacks may have lazily started the runtime
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
