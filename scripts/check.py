#!/usr/bin/env python3
"""Run the repository's deterministic local validation profiles."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Command:
    name: str
    argv: tuple[str, ...]

    def __iter__(self):
        return iter(self.argv)


def commands_for(profile: str) -> list[Command]:
    workflow_dir = ROOT / ".github/workflows"
    workflows = tuple(str(path.relative_to(ROOT)) for path in sorted(workflow_dir.glob("*.yml")))
    fast = [
        Command(
            "coverage-run",
            (
                "uv",
                "run",
                "--python",
                "3.13",
                "--with",
                "pytest==9.1.1",
                "--with",
                "coverage==7.10.7",
                "--",
                "coverage",
                "run",
                "--branch",
                "--source=kanban_task_threads",
                "--source=.",
                "-m",
                "pytest",
                "tests",
            ),
        ),
        Command(
            "coverage-report",
            (
                "uv",
                "run",
                "--python",
                "3.13",
                "--with",
                "coverage==7.10.7",
                "--",
                "coverage",
                "report",
                "--fail-under=85",
            ),
        ),
        Command("ruff-check", ("uvx", "--from", "ruff==0.16.8", "ruff", "check", ".")),
        Command("ruff-format", ("uvx", "--from", "ruff==0.16.8", "ruff", "format", "--check", ".")),
        Command("actionlint", ("actionlint", *workflows)),
    ]
    if profile == "fast":
        return fast
    if profile == "full-local":
        return [*fast, Command("hermes-doctor", ("./scripts/sandbox", "doctor"))]
    raise ValueError(f"unknown check profile: {profile}")


def run_commands(
    commands: Iterable[Command],
    run: Callable[[Command], subprocess.CompletedProcess | int] = subprocess.run,
) -> int:
    for command in commands:
        print(f"==> {command.name}")
        result = run(command)
        exit_code = result if isinstance(result, int) else result.returncode
        if exit_code:
            return exit_code
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: check.py {fast|full-local}", file=sys.stderr)
        return 2
    try:
        commands = commands_for(arguments[0])
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    return run_commands(commands)


if __name__ == "__main__":
    raise SystemExit(main())
