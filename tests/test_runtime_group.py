"""Lifecycle fan-out: one profile hosts independently stoppable board workers."""

import contextvars
import threading
import time

from kanban_task_threads.runtime import Runtime
from kanban_task_threads.runtime_group import RuntimeGroup


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class EmptyReport:
    errors = []
    warnings = []


class IdleConsumer:
    def run_once(self):
        return EmptyReport()


def test_group_copies_context_per_worker():
    """Two concurrent Context.run calls must never enter one Context twice."""
    profile = contextvars.ContextVar("profile", default="missing")
    profile.set("publisher")
    barrier = threading.Barrier(2)
    seen = []

    def build():
        barrier.wait(timeout=1)
        seen.append(profile.get())
        return IdleConsumer()

    group = RuntimeGroup([Runtime(build, poll_seconds=30), Runtime(build, poll_seconds=30)])
    group.kick(contextvars.copy_context())

    assert wait_for(lambda: len(seen) == 2)
    assert seen == ["publisher", "publisher"]
    group.shutdown()


def test_group_signals_all_before_join():
    events = []

    class RecordingRuntime:
        def request_stop(self):
            events.append("stop")

        def join(self, timeout=10.0):
            events.append(("join", timeout))

    RuntimeGroup([RecordingRuntime(), RecordingRuntime()]).shutdown()

    assert events[:2] == ["stop", "stop"]
    assert [event[0] for event in events[2:]] == ["join", "join"]


def test_group_shutdown_has_one_deadline(monkeypatch):
    joined = []

    class RecordingRuntime:
        def request_stop(self):
            pass

        def join(self, timeout=10.0):
            joined.append(timeout)

    clock = iter((100.0, 101.25, 104.75))
    monkeypatch.setattr("kanban_task_threads.runtime_group.time.monotonic", lambda: next(clock))

    RuntimeGroup([RecordingRuntime(), RecordingRuntime()]).shutdown()

    assert joined == [8.75, 5.25]
