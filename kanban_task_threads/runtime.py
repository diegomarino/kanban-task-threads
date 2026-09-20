"""The lazy runtime between Hermes hooks and the consumer.

Hooks are best-effort "poll now" kicks (ADR-0002) with no correctness role, so this
is deliberately small: the first kick starts one daemon thread, the thread
builds the consumer once (inside the contextvars snapshot captured at that
kick, so profile-scoped secrets resolve — a ContextVar scope does not cross an
unbound thread), then runs a pass on every kick and every poll interval.

`register()` constructs a Runtime but starts nothing: the validate probe runs
register() against a stub context, and real work there would fail the catalog
gate. Configuration problems degrade to a no-op after one log line — the
build runs once; a dead config is not retried on every kick.

`shutdown()` is the unload contract: `discover_plugins(force=True)` re-imports
the module and `hermes plugins disable` walks the same path, so an orphaned
thread would keep contesting the lease and holding HTTP sessions. After
shutdown the thread is joined; the lease is already released per pass (the
consumer holds it only while a pass runs).
"""

import logging
import threading
from collections.abc import Callable

_logger = logging.getLogger(__name__)


class RetryableStartup(Exception):
    """Startup failed for a reason that may heal — a network blip, or a kick
    whose contextvars context carries no secret scope (the dispatcher tick
    runs in a deliberately empty Context). Retried on the next kick, with
    that kick's fresher context. A *config* verdict is the build returning
    None instead: that one is permanent until a plugin reload."""


class Runtime:
    """The lazy bridge between Hermes hooks and the consumer: kick() is cheap
    and hook-safe, the daemon thread does everything else. See the module
    docstring for the retry/degrade state machine."""

    def __init__(
        self,
        build_consumer: Callable,
        *,
        poll_seconds: float = 20.0,
        logger: logging.Logger | None = None,
    ):
        self._build = build_consumer
        self._poll = poll_seconds
        self._log = logger or _logger
        self._kicked = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._context = None  # latest kick's contextvars snapshot, until built
        self._built = False
        self._disabled = False

    def kick(self, context=None) -> None:
        """Cheap and hook-safe: some kanban hooks fire inside the dispatch
        lock, so this only sets an event (and, at most, starts the thread)."""
        if self._stopping.is_set() or self._disabled:
            return
        with self._lock:
            if context is not None and not self._built:
                self._context = context  # latest wins: a retry gets a fresh one
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="kanban-task-threads", daemon=True
                )
                self._thread.start()
        self._kicked.set()

    def shutdown(self) -> None:
        """The unload contract: stop the loop and join the thread. Idempotent;
        after it returns no plugin thread survives and the lease is free."""
        self._stopping.set()
        self._kicked.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

    def alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _loop(self) -> None:
        try:
            if self._context is not None:
                consumer = self._context.run(self._build)
            else:
                consumer = self._build()
        except RetryableStartup as exc:
            # The thread dies undisabled: the next kick spawns a fresh one and
            # retries the build with that kick's context.
            self._log.warning("kanban-task-threads: startup deferred: %s", exc)
            return
        except Exception:
            self._log.exception("kanban-task-threads: startup failed; plugin is inactive")
            self._disabled = True
            return
        if consumer is None:
            # A config verdict; the build already logged why. Permanent until
            # a plugin reload — never re-probed on every kick.
            self._disabled = True
            return
        self._built = True
        while not self._stopping.is_set():
            self._kicked.wait(self._poll)
            self._kicked.clear()
            if self._stopping.is_set():
                break
            try:
                report = consumer.run_once()
                for line in report.errors:
                    self._log.error("kanban-task-threads: %s", line)
                for line in getattr(report, "warnings", ()):
                    self._log.warning("kanban-task-threads: %s", line)
            except Exception:
                self._log.exception("kanban-task-threads: consume pass failed; will retry")
