"""Fail-closed selection of a Release candidate run for one exact commit."""

import argparse
import json
from pathlib import Path
from typing import Any


class CandidateError(ValueError):
    """Raised when GitHub data cannot prove an exact successful candidate."""


FULL_GATE_INPUTS = {
    "__init__.py", "plugin.yaml", "kanban_task_threads/runtime.py",
    "scripts/check_startup.py", ".github/workflows/candidate.yml",
}


def requires_full_gate(paths: list[str]) -> bool:
    return any(path in FULL_GATE_INPUTS for path in paths)


def has_success(runs: list[dict[str, Any]], sha: str) -> bool:
    return any(run.get("headSha") == sha and run.get("conclusion") == "success" for run in runs)


def require_exact_success(runs: list[dict[str, Any]], sha: str) -> None:
    if not isinstance(runs, list) or not sha or not has_success(runs, sha):
        raise CandidateError(f"No successful Release candidate run exists for {sha}.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-file", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    arguments = parser.parse_args()
    try:
        require_exact_success(json.loads(arguments.runs_file.read_text()), arguments.sha)
    except (CandidateError, OSError, json.JSONDecodeError) as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
