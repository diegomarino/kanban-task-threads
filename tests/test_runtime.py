"""The deferred runtime: register-time start creates a lease candidate,
kicks accelerate it, retries refresh profile context, and unload stops it.

The unload contract is the load-bearing one: `discover_plugins(force=True)`
re-imports the module and `hermes plugins disable` walks the same path — an
orphaned consumer would keep contesting the lease and holding HTTP sessions.
"""

import contextvars
import itertools
import threading
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


def test_constructing_a_runtime_starts_nothing():
    build = CountingBuild()
    runtime = Runtime(build, poll_seconds=30)
    time.sleep(0.05)
    assert build.calls == 0
    assert runtime.alive() is False
    runtime.shutdown()


def test_start_needs_no_kick_to_run_a_pass():
    # The publisher must not be whoever happens to host the kanban dispatcher:
    # hooks only fire there, so a kick-only start makes every other profile a
    # non-candidate and the lease never gets to choose (ADR-0002 — kicks
    # accelerate delivery, they do not enable it).
    consumer = FakeConsumer()
    build = CountingBuild(consumer)
    runtime = Runtime(build, poll_seconds=0.02)
    runtime.start()
    assert wait_for(lambda: consumer.runs >= 1)
    assert build.calls == 1
    runtime.shutdown()


def test_start_builds_nothing_before_the_first_interval():
    # `hermes plugins validate` runs register() against a stub context whose
    # every attribute is a no-op, so the probe cannot be detected — it is
    # outlived instead. The thread sleeps first and builds second, so a probe
    # that exits in milliseconds never resolves a secret or opens a socket.
    build = CountingBuild(FakeConsumer())
    runtime = Runtime(build, poll_seconds=30)
    runtime.start()
    time.sleep(0.05)
    assert build.calls == 0, "start() built the consumer before its first interval"
    assert runtime.alive() is True
    runtime.shutdown()


def test_a_kick_still_short_circuits_the_wait():
    # Kicks keep their ADR-0002 role: a hook turns the next interval into "now".
    consumer = FakeConsumer()
    runtime = Runtime(CountingBuild(consumer), poll_seconds=30)
    runtime.start()
    runtime.kick()
    assert wait_for(lambda: consumer.runs >= 1)
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


def test_a_racing_kick_cannot_respawn_after_a_config_verdict():
    """A kick admitted before disablement must recheck terminal state."""
    build_started = threading.Event()
    release_build = threading.Event()
    calls = []

    def blocked_config_verdict():
        calls.append(1)
        build_started.set()
        release_build.wait()
        return None

    runtime = Runtime(blocked_config_verdict, poll_seconds=0.01)
    runtime.start()
    assert build_started.wait(timeout=1), "build never started"

    runtime._lock.acquire()
    kicker = threading.Thread(target=runtime.kick)
    try:
        kicker.start()
        assert wait_for(kicker.is_alive), "kick never reached the lifecycle lock"
        release_build.set()
        assert wait_for(lambda: not runtime.alive()), "config-verdict thread did not exit"
    finally:
        runtime._lock.release()
    kicker.join(timeout=1)
    time.sleep(0.05)

    assert calls == [1], "a racing kick rebuilt after permanent disablement"
    assert runtime.alive() is False


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


def test_shutdown_during_build_does_not_start_a_consumer_pass():
    """A completed preflight cannot publish after plugin unload has begun."""
    build_started = threading.Event()
    release_build = threading.Event()
    consumer = FakeConsumer()

    def blocked_build():
        build_started.set()
        release_build.wait()
        return consumer

    runtime = Runtime(blocked_build, poll_seconds=0.01)
    runtime.start()
    assert build_started.wait(timeout=1), "build never started"

    shutdown = threading.Thread(target=runtime.shutdown)
    shutdown.start()
    assert wait_for(runtime._stopping.is_set), "shutdown never signalled the runtime"
    release_build.set()
    shutdown.join(timeout=1)

    assert not shutdown.is_alive(), "shutdown did not return"
    assert consumer.runs == 0, "consumer ran after shutdown began"
    assert runtime.alive() is False


def deferring_build(outcomes, calls):
    def build():
        calls.append(1)
        outcome = outcomes.pop(0) if outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return build


def test_retryable_startup_is_retried_on_the_next_kick():
    # A DNS blip in the preflight must not disable the plugin until a gateway
    # reload — only a *config* verdict (build returns None) is permanent.
    from kanban_task_threads.runtime import RetryableStartup

    consumer = FakeConsumer()
    calls = []
    build = deferring_build([RetryableStartup("dns blip"), consumer], calls)
    runtime = Runtime(build, poll_seconds=30)
    runtime.kick()
    assert wait_for(lambda: len(calls) == 1)
    runtime.kick()
    assert wait_for(lambda: consumer.runs >= 1)
    assert len(calls) == 2
    runtime.shutdown()


def test_a_deferred_startup_retries_on_its_own_interval():
    # The thread must survive a deferral. Since register() starts it, a profile
    # that receives no kanban hooks (every profile but the dispatcher host) has
    # no kick coming: dying here would strand it until the next gateway restart
    # — exactly what a fixed config could then not repair on its own.
    from kanban_task_threads.runtime import RetryableStartup

    consumer = FakeConsumer()
    calls = []
    runtime = Runtime(
        deferring_build([RetryableStartup("no secret scope yet"), consumer], calls),
        poll_seconds=0.02,
    )
    runtime.start()
    assert wait_for(lambda: consumer.runs >= 1), "a deferred build was never retried"
    assert len(calls) == 2
    runtime.shutdown()


def test_an_interval_retry_refreshes_context_without_a_kick():
    """A hook-less candidate can recover after its profile scope changes."""
    from kanban_task_threads.runtime import RetryableStartup

    profile = contextvars.ContextVar("profile", default="empty")
    latest = {"value": "empty"}
    seen = []
    consumer = FakeConsumer()

    def refresh_context(base):
        fresh = base.copy()
        fresh.run(profile.set, latest["value"])
        return fresh

    def build():
        seen.append(profile.get())
        if profile.get() == "empty":
            latest["value"] = "profile-scope"
            raise RetryableStartup("profile scope unavailable")
        return consumer

    runtime = Runtime(build, poll_seconds=0.01, refresh_context=refresh_context)
    runtime.start(contextvars.copy_context())

    assert wait_for(lambda: consumer.runs >= 1), "retry reused the stale context"
    assert seen == ["empty", "profile-scope"]
    runtime.shutdown()


def test_a_repeated_deferral_is_logged_once():
    # Retrying every interval must not spam the log when a profile remains
    # deferred. Same reason -> one warning; a new reason re-arms it.
    from kanban_task_threads.runtime import RetryableStartup

    class RecordingLogger:
        def __init__(self):
            self.warnings = []

        def warning(self, msg, *args):
            self.warnings.append(msg % args if args else msg)

        def debug(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    log = RecordingLogger()
    calls = []

    def build():
        calls.append(1)
        raise RetryableStartup("secret scope missing" if len(calls) < 4 else "discord 503")

    runtime = Runtime(build, poll_seconds=0.01, logger=log)
    runtime.start()
    assert wait_for(lambda: len(calls) >= 5)
    runtime.shutdown()
    assert len(log.warnings) == 2, f"expected one warning per distinct reason, got {log.warnings}"


def test_a_deferred_reason_is_warned_once_even_after_another_reason():
    """A -> B -> A warns for two distinct causes, not three attempts."""
    from kanban_task_threads.runtime import RetryableStartup

    class RecordingLogger:
        def __init__(self):
            self.warnings = []

        def warning(self, msg, *args):
            self.warnings.append(msg % args if args else msg)

        def debug(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    reasons = itertools.cycle(("A", "B", "A"))
    calls = []

    def build():
        calls.append(1)
        raise RetryableStartup(next(reasons))

    log = RecordingLogger()
    runtime = Runtime(build, poll_seconds=0.01, logger=log)
    runtime.start()
    assert wait_for(lambda: len(calls) >= 6)
    runtime.shutdown()

    assert log.warnings == [
        "kanban-task-threads: startup deferred: A",
        "kanban-task-threads: startup deferred: B",
    ]


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

    var.set("profile-scope")
    runtime.kick(context=contextvars.copy_context())  # a real one later
    assert wait_for(lambda: consumer.runs >= 1)
    assert seen == ["empty", "profile-scope"]
    runtime.shutdown()


def test_unload_stops_the_consumer_and_releases_its_lease(tmp_path):
    # Diego's falsable criterion: after the unload callback, the deferred
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
