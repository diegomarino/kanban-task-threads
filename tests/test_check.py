import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check.py")
check = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)


def test_fast_check_contains_one_python_suite_lint_coverage_and_actionlint():
    commands = check.commands_for("fast")

    assert [command.name for command in commands] == [
        "coverage-run",
        "coverage-report",
        "ruff-check",
        "ruff-format",
        "actionlint",
    ]
    assert "--fail-under=85" in commands[1].argv


def test_full_local_extends_fast_with_offline_plugin_load():
    commands = check.commands_for("full-local")

    assert commands[:5] == check.commands_for("fast")
    assert commands[-1].name == "hermes-doctor"


def test_runner_stops_at_first_failure_and_returns_its_exit_code():
    executed = []

    result = check.run_commands(
        [
            check.Command("first", ("first",)),
            check.Command("second", ("second",)),
        ],
        run=lambda command: executed.append(command.name) or 17,
    )

    assert result == 17
    assert executed == ["first"]
