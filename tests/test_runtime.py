"""The lazy runtime: nothing starts at register time, a hook kick starts the
consumer thread, and unload stops it and releases its lease.

The unload contract is the load-bearing one: `discover_plugins(force=True)`
re-imports the module and `hermes plugins disable` walks the same path — an
orphaned consumer would keep contesting the lease and holding HTTP sessions.
"""

import time

from conftest import FakeTransport, add_event, insert_task, make_board

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.runtime import Runtime
from kanban_task_threads.store import StateStore


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class CountingBuild:
    def __init__(self, consumer="unset"):
        self.calls = 0
        self.consumer = consumer

    def __call__(self):
        self.calls += 1
        return None if self.consumer == "unset" else self.consumer


class FakeConsumer:
    def __init__(self):
        self.runs = 0

    def run_once(self, now=None):
        self.runs += 1
        from kanban_task_threads.consumer import Report

        return Report()


def test_nothing_starts_before_the_first_kick():
    build = CountingBuild()
    runtime = Runtime(build, poll_seconds=30)
    time.sleep(0.05)
    assert build.calls == 0
    assert runtime.alive() is False
    runtime.shutdown()


def test_kick_starts_and_runs_the_consumer():
    consumer = FakeConsumer()
    build = CountingBuild(consumer)
    runtime = Runtime(build, poll_seconds=30)
    runtime.kick()
    assert wait_for(lambda: consumer.runs >= 1)
    assert build.calls == 1
    runtime.shutdown()


def test_unconfigured_build_degrades_to_noop_and_builds_only_once():
    build = CountingBuild(consumer=None)
    runtime = Runtime(build, poll_seconds=0.01)
    runtime.kick()
    assert wait_for(lambda: build.calls == 1)
    assert wait_for(lambda: not runtime.alive())  # the thread exits, quietly
    for _ in range(3):
        runtime.kick()
    time.sleep(0.05)
    assert build.calls == 1  # no rebuild loop chewing CPU on a dead config
    runtime.shutdown()


def test_a_failing_pass_does_not_kill_the_loop():
    class Exploding:
        def __init__(self):
            self.runs = 0

        def run_once(self, now=None):
            self.runs += 1
            raise RuntimeError("boom")

    consumer = Exploding()
    runtime = Runtime(CountingBuild(consumer), poll_seconds=30)
    runtime.kick()
    assert wait_for(lambda: consumer.runs >= 1)
    runtime.kick()
    assert wait_for(lambda: consumer.runs >= 2)
    assert runtime.alive()
    runtime.shutdown()


def test_retryable_startup_is_retried_on_the_next_kick():
    # A DNS blip in the preflight must not disable the plugin until a gateway
    # reload — only a *config* verdict (build returns None) is permanent.
    from kanban_task_threads.runtime import RetryableStartup

    consumer = FakeConsumer()
    outcomes = [RetryableStartup("dns blip"), consumer]
    calls = []

    def build():
        calls.append(1)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    runtime = Runtime(build, poll_seconds=30)
    runtime.kick()
    assert wait_for(lambda: len(calls) == 1)
    assert wait_for(lambda: not runtime.alive())  # died without disabling
    runtime.kick()
    assert wait_for(lambda: consumer.runs >= 1)
    assert len(calls) == 2
    runtime.shutdown()


def test_retry_uses_the_latest_kick_context():
    # The dispatcher tick runs in a deliberately EMPTY contextvars context; if
    # that kick arrives first, the retry must use a later, richer context —
    # never stay married to the first snapshot.
    import contextvars

    from kanban_task_threads.runtime import RetryableStartup

    var = contextvars.ContextVar("probe", default="empty")
    seen = []
    consumer = FakeConsumer()

    def build():
        seen.append(var.get())
        if var.get() == "empty":
            raise RetryableStartup("no secret scope in this context")
        return consumer

    runtime = Runtime(build, poll_seconds=30)

    runtime.kick(context=contextvars.copy_context())  # the empty first kick
    assert wait_for(lambda: seen == ["empty"])
    assert wait_for(lambda: not runtime.alive())

    var.set("profile-scope")
    runtime.kick(context=contextvars.copy_context())  # a real one later
    assert wait_for(lambda: consumer.runs >= 1)
    assert seen == ["empty", "profile-scope"]
    runtime.shutdown()


def test_unload_stops_the_consumer_and_releases_its_lease(tmp_path):
    # Diego's falsable criterion: after the unload callback, the lazily started
    # consumer is stopped and its lease is free for another holder.
    board = make_board(tmp_path / "kanban.db")
    insert_task(board, "t_1", status="running")
    add_event(board, "t_1", "created", {"status": "ready"})
    store = StateStore(tmp_path / "state.db")
    transport = FakeTransport()
    consumer = Consumer(
        tmp_path / "kanban.db", store, transport, board="default", holder="runtime-under-test"
    )
    runtime = Runtime(lambda: consumer, poll_seconds=30)

    runtime.kick()
    assert wait_for(lambda: "open_thread" in transport.ops())  # it really ran

    runtime.shutdown()  # what ctx.on_unload invokes
    assert runtime.alive() is False
    assert (
        store.acquire_lease("consume:default", "someone-else", now=int(time.time()), ttl=60)
        is not None
    )
