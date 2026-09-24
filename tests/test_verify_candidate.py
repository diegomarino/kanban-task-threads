import json
import subprocess
import sys

import pytest

from scripts.ci import verify_candidate


def test_candidate_is_reusable_only_for_exact_successful_sha():
    runs = [
        {"headSha": "old", "conclusion": "success"},
        {"headSha": "candidate", "conclusion": "failure"},
    ]

    assert verify_candidate.has_success(runs, "candidate") is False


def test_missing_or_ambiguous_candidate_data_fails_closed():
    with pytest.raises(verify_candidate.CandidateError):
        verify_candidate.require_exact_success([], "candidate")


@pytest.mark.parametrize(
    "path",
    [
        "kanban_task_threads/runtime.py",
        "__init__.py",
        "plugin.yaml",
        ".github/workflows/candidate.yml",
        "scripts/check_startup.py",
    ],
)
def test_full_gate_inputs_include_runtime_manifest_and_ci(path):
    assert verify_candidate.requires_full_gate([path])


def test_cli_requires_one_exact_successful_candidate_run(tmp_path):
    payload = tmp_path / "runs.json"
    payload.write_text(json.dumps([{"headSha": "candidate", "conclusion": "success"}]))

    result = subprocess.run(
        [sys.executable, "scripts/ci/verify_candidate.py", "--runs-file", str(payload), "--sha", "candidate"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
