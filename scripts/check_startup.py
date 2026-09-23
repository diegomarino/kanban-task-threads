#!/usr/bin/env python3
"""Exercise deferred startup against the invoking interpreter's real Hermes APIs.

Run with Hermes' Python, not the unit-test environment. The parent launches an
isolated child with an explicit, credential-free environment and temporary homes.
Lifecycle checks observe an in-memory consumer; a separate real consumer check
uses synthetic SQLite and replaces only HTTP. No agent is run.
"""

import contextvars
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def isolated_check():
    # Do not copy the caller's environment: it may contain real credentials.
    with tempfile.TemporaryDirectory(prefix="ktt-startup-") as directory:
        subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--isolated"],
            cwd=directory,
            env={
                "PATH": os.defpath,
                "HOME": directory,
                "HERMES_HOME": directory,
                "HERMES_KANBAN_HOME": directory,
                "OP_LOAD_DESKTOP_APP_SETTINGS": "false",
                "OP_BIOMETRIC_UNLOCK_ENABLED": "false",
            },
            check=True,
            timeout=30,
        )


def run_checks():
    # Install before importing Hermes. An unexpected external effect fails the
    # check; the real scope loader may only read the synthetic profile files.
    def deny_external_effects(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system"}:
            raise AssertionError(f"Unexpected external effect: {event}")

    sys.addaudithook(deny_external_effects)

    import hermes_cli
    import hermes_constants
    from agent import secret_scope

    spec = importlib.util.spec_from_file_location(
        "ktt_startup_check", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    entry = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = entry
    spec.loader.exec_module(entry)
    runtime_module = __import__(f"{spec.name}.kanban_task_threads.runtime", fromlist=["Runtime"])
    try:
        policy_present = importlib.util.find_spec("tui_gateway.launch_profile_policy") is not None
    except ModuleNotFoundError as exc:
        if exc.name != "tui_gateway":
            raise
        policy_present = False
    print(
        f"Hermes {hermes_cli.__version__}; launch policy present: {policy_present}",
        flush=True,
    )

    class StartupChecks(unittest.TestCase):
        def setUp(self):
            self.directory = tempfile.TemporaryDirectory(dir=os.environ["HOME"])
            self.addCleanup(self.directory.cleanup)
            self.home = Path(self.directory.name)
            self.other = self.home / "secondary"
            self.other.mkdir()
            self.environment = patch.dict(
                os.environ,
                {"HERMES_HOME": str(self.home), "KTT_CHECK_AMBIENT": "launch-only"},
            )
            self.environment.start()
            self.addCleanup(self.environment.stop)
            previous = secret_scope.is_multiplex_active()
            self.addCleanup(secret_scope.set_multiplex_active, previous)
            secret_scope.set_multiplex_active(False)

        def identity(self, home):
            context = contextvars.Context()
            context.run(hermes_constants.set_hermes_home_override, home)
            return context

        def refresh(self, home):
            return entry._refresh_profile_context(
                contextvars.Context(), identity_context=self.identity(home)
            )

        def test_process_home_refresh_and_env_only_launch_credentials(self):
            (self.home / ".env").write_text("KTT_CHECK_PROFILE=first\n")
            first = self.refresh(self.home)
            (self.home / ".env").write_text("KTT_CHECK_PROFILE=repaired\n")
            second = self.refresh(self.home)
            self.assertEqual(first.run(secret_scope.get_secret, "KTT_CHECK_PROFILE"), "first")
            self.assertEqual(second.run(secret_scope.get_secret, "KTT_CHECK_PROFILE"), "repaired")
            self.assertEqual(
                second.run(secret_scope.get_secret, "KTT_CHECK_AMBIENT"), "launch-only"
            )

        def test_secondary_profile_isolation_and_latest_attempt_context(self):
            secret_scope.set_multiplex_active(True)
            (self.home / ".env").write_text("KTT_CHECK_PROFILE=launch\n")
            (self.other / ".env").write_text("KTT_CHECK_PROFILE=secondary\n")
            marker = contextvars.ContextVar("attempt_marker")
            attempt = self.identity(self.home)
            attempt.run(marker.set, "latest-kick")
            refreshed = entry._refresh_profile_context(
                attempt, identity_context=self.identity(self.other)
            )
            self.assertEqual(refreshed.run(hermes_constants.get_hermes_home), self.other)
            self.assertEqual(refreshed.run(marker.get), "latest-kick")
            self.assertEqual(
                refreshed.run(secret_scope.get_secret, "KTT_CHECK_PROFILE"), "secondary"
            )
            self.assertIsNone(refreshed.run(secret_scope.get_secret, "KTT_CHECK_AMBIENT"))
            self.assertEqual(attempt.run(hermes_constants.get_hermes_home), self.home)

        def test_legacy_multiplex_process_home_does_not_copy_ambient_credentials(self):
            if policy_present:
                self.skipTest("Legacy policy: requires Hermes without launch_profile_policy")
            secret_scope.set_multiplex_active(True)
            (self.home / ".env").write_text("KTT_CHECK_PROFILE=launch\n")
            refreshed = self.refresh(self.home)
            self.assertEqual(refreshed.run(secret_scope.get_secret, "KTT_CHECK_PROFILE"), "launch")
            self.assertIsNone(refreshed.run(secret_scope.get_secret, "KTT_CHECK_AMBIENT"))

        def test_modern_launch_snapshot_survives_ambient_changes(self):
            if not policy_present:
                self.skipTest("Modern policy: requires Hermes with launch_profile_policy")
            from tui_gateway.launch_profile_policy import activate_multi_profile_hosting

            activate_multi_profile_hosting()
            os.environ["KTT_CHECK_AMBIENT"] = "secondary-contamination"
            refreshed = self.refresh(self.home)
            self.assertEqual(
                refreshed.run(secret_scope.get_secret, "KTT_CHECK_AMBIENT"), "launch-only"
            )
            secondary = self.refresh(self.other)
            self.assertIsNone(secondary.run(secret_scope.get_secret, "KTT_CHECK_AMBIENT"))

        def test_real_consumer_build_reaches_preflight_and_a_local_pass(self):
            from hermes_cli import kanban_db

            transport = __import__(
                f"{spec.name}.kanban_task_threads.transport", fromlist=["urllib_http"]
            )
            fixture_spec = importlib.util.spec_from_file_location(
                "ktt_board_fixture", ROOT / "tests" / "conftest.py"
            )
            fixture = importlib.util.module_from_spec(fixture_spec)
            fixture_spec.loader.exec_module(fixture)

            class Context:
                profile_name = "synthetic"

                def get_config(self, key, default=None):
                    return "default" if key == "board" else default

            def http(method, url, body, **kwargs):
                self.assertEqual((method, url, body), ("GET", "https://example.invalid/hook", None))
                return 200, {"id": "1", "channel_id": "2", "name": "synthetic"}

            (self.home / ".env").write_text(
                "KANBAN_TASK_THREADS_WEBHOOK_URL=https://example.invalid/hook\n"
            )
            with patch.dict(os.environ, {"HERMES_KANBAN_HOME": str(self.home)}):
                board_path = kanban_db.kanban_db_path("default")
                self.assertTrue(board_path.is_relative_to(self.home))
                board_path.parent.mkdir(parents=True, exist_ok=True)
                fixture.make_board(board_path).close()
                runtime = runtime_module.Runtime(
                    lambda: entry._build_consumer(Context()),
                    refresh_context=entry._refresh_profile_context,
                )
                runtime._context = self.identity(self.home)
                with patch.object(transport, "urllib_http", http):
                    consumer = runtime._build_once()
                    self.assertIsNotNone(consumer, "Real consumer build failed")
                    try:
                        report = consumer.run_once()
                        self.assertTrue(report.acquired)
                        self.assertEqual(report.errors, [])
                    finally:
                        consumer._store._conn.close()
                with sqlite3.connect(board_path) as connection:
                    self.assertEqual(
                        connection.execute("SELECT count(*) FROM task_events").fetchone(), (0,)
                    )

        def test_deferred_start_retry_and_unload_without_hooks(self):
            self.check_deferred_start(self.home)
            self.check_deferred_start(self.other)

        def check_deferred_start(self, home):
            passed = threading.Event()
            attempts = []
            observed = []
            callbacks = []

            class Context:
                profile_name = "synthetic"

                def get_config(self, key, default=None):
                    return 0.01 if key == "poll_seconds" else default

                def register_hook(self, name, callback):
                    pass  # Deliberately never kick: every profile must start itself.

                def on_unload(self, callback):
                    callbacks.append(callback)

            class Consumer:
                def run_once(self):
                    passed.set()
                    return types.SimpleNamespace(errors=[], warnings=[])

            def build(ctx):
                attempts.append(hermes_constants.get_hermes_home())
                observed.append(secret_scope.get_secret("KTT_CHECK_PROFILE"))
                if len(attempts) == 1:
                    (home / ".env").write_text("KTT_CHECK_PROFILE=repaired\n")
                    raise runtime_module.RetryableStartup("synthetic transient failure")
                return Consumer()

            (home / ".env").write_text("KTT_CHECK_PROFILE=initial\n")
            with patch.object(entry, "_build_consumer", build):
                self.identity(home).run(entry.register, Context())
                try:
                    self.assertTrue(passed.wait(3), "Deferred startup never reached a pass")
                    self.assertEqual(attempts, [home, home])
                    self.assertEqual(observed, ["initial", "repaired"])
                finally:
                    for callback in callbacks:
                        callback()
            self.assertFalse(any(t.name == "kanban-task-threads" for t in threading.enumerate()))

    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(StartupChecks)
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--isolated"]:
        raise SystemExit(run_checks())
    isolated_check()
