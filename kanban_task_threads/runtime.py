"""The runtime between Hermes hooks and the consumer.

Hooks are best-effort "poll now" kicks (ADR-0002) with no correctness role, so this
is deliberately small: one daemon thread builds the consumer once, refreshing
the profile context before each startup attempt, then runs a pass on every kick
and every poll interval.

`start()` is what makes a profile a lease candidate, and `register()` calls it
so every profile that loads the plugin is one (ADR-0013). Starting on the first
*kick* instead would silently elect a single publisher: kanban hooks fire only
in the process holding Hermes' singleton dispatcher lock, so the lease would
never get to choose and a crash there would stop publishing with no failover.

The thread waits one interval BEFORE its first build, which is what keeps
`register()` honest: `hermes plugins validate` probes register() against a stub
context whose every attribute is a no-op (so the probe cannot be detected, only
outlived), and it exits in milliseconds — long before the thread resolves a
secret or opens a socket. A kick turns that first interval into "now".

Configuration problems degrade to a no-op after one log line — the build runs
once; a dead config is not retried on every kick.

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
    runs in a deliberately empty Context). The thread stays alive and retries
    on its next interval, or sooner on a kick. The entry point refreshes the
    registered profile context before each attempt. A *config* verdict is the
    build returning None instead: that one is permanent until a plugin reload."""


class Runtime:
    """The deferred bridge between Hermes hooks and the consumer: kick() is cheap
    and hook-safe, the daemon thread does everything else. See the module
    docstring for the retry/degrade state machine."""

    def __init__(
        self,
        build_consumer: Callable,
        *,
        poll_seconds: float = 20.0,
        logger: logging.Logger | None = None,
        refresh_context: Callable[[object], object] | None = None,
    ):
        self._build = build_consumer
        self._poll = poll_seconds
        self._log = logger or _logger
        self._refresh_context = refresh_context
        self._kicked = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        # Linearizes consumer passes with shutdown: once shutdown acquires this
        # lock and sets the stop flag, no later pass can begin.
        self._pass_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._context = None  # latest kick's contextvars snapshot, until built
        self._built = False
        self._disabled = False
        self._warned_deferred_reasons: set[str] = set()

    def start(self, context=None) -> None:
        """Make this process a lease candidate, with no hook required. The
        thread sleeps one interval before building anything, so calling this
        from register() still does no I/O of its own."""
        with self._lock:
            if self._stopping.is_set() or self._disabled:
                return
            self._adopt(context)
            self._spawn()

    def kick(self, context=None) -> None:
        """Cheap and hook-safe: some kanban hooks fire inside the dispatch
        lock, so this only sets an event (and, at most, starts the thread).
        The event collapses the current wait, turning the next pass into now."""
        with self._lock:
            if self._stopping.is_set() or self._disabled:
                return
            self._adopt(context)
            self._spawn()
        self._kicked.set()

    def _adopt(self, context) -> None:
        """Caller holds the lock. Keep the latest attempt context until built.

        A profile-specific refresher may deliberately retain identity from the
        registration context when a kick's Context is empty.
        """
        if context is not None and not self._built:
            self._context = context

    def _spawn(self) -> None:
        """Caller holds the lock. Idempotent: a live thread is left alone, a
        thread that died deferring its build is replaced."""
        # Authoritative terminal-state check under the same lock as spawning.
        # A kick may have passed its fast path just as a build reached a config
        # verdict; it must not resurrect the runtime afterwards.
        if self._stopping.is_set() or self._disabled:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, name="kanban-task-threads", daemon=True
            )
            self._thread.start()

    def shutdown(self) -> None:
        """Stop the loop and wait up to ten seconds for the thread. Idempotent.

        No new consumer pass can begin after shutdown linearizes on
        ``_pass_lock``. A build or pass already blocked longer than the join
        timeout may finish later; consumer leases fence any successor.
        """
        with self._pass_lock:
            self._stopping.set()
            self._kicked.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

    def alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _build_once(self):
        """One build attempt in a freshly prepared profile context.

        None means this thread is done: `_disabled` says whether that is
        permanent (a config verdict) or just deferred.
        """
        try:
            with self._lock:
                context = self._context
            if context is not None:
                if self._refresh_context is not None:
                    context = self._refresh_context(context)
                consumer = context.run(self._build)
            else:
                consumer = self._build()
        except RetryableStartup as exc:
            # Deferred, not disabled: the loop retries on its next interval, and
            # `_adopt` keeps feeding it the latest kick's context until a build
            # sticks. Dying here instead would strand every profile that gets no
            # kanban hooks — there is no kick coming to revive it (ADR-0013).
            # One warning per distinct reason: a retry every interval would
            # otherwise fill the log for as long as the cause lasts.
            reason = str(exc)
            if reason not in self._warned_deferred_reasons:
                self._warned_deferred_reasons.add(reason)
                self._log.warning("kanban-task-threads: startup deferred: %s", exc)
            else:
                self._log.debug("kanban-task-threads: startup still deferred: %s", exc)
            return None
        except Exception:
            self._log.exception("kanban-task-threads: startup failed; plugin is inactive")
            self._disabled = True
            return None
        if consumer is None:
            # A config verdict; the build already logged why. Permanent until
            # a plugin reload — never re-probed on every kick.
            self._disabled = True
            return None
        self._built = True
        return consumer

    def _loop(self) -> None:
        # Wait first, build second: see the module docstring on outliving the
        # validate probe. A kick collapses the wait, so hook-driven profiles
        # keep building immediately.
        consumer = None
        while not self._stopping.is_set():
            self._kicked.wait(self._poll)
            self._kicked.clear()
            if self._stopping.is_set():
                break
            if consumer is None:
                consumer = self._build_once()
                if consumer is None:
                    if self._disabled:
                        return  # a config verdict — permanent until a reload
                    continue  # deferred — try again next interval, or next kick
                if self._stopping.is_set():
                    break  # unload began while the build/preflight was in flight
            with self._pass_lock:
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
