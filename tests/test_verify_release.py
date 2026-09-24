import json
import subprocess
import sys

import pytest

from scripts.ci import verify_release

RELEASE_ONLY_PATHS = {
    ".release-please-manifest.json",
    "CHANGELOG.md",
    "plugin.yaml",
    "pyproject.toml",
    "uv.lock",
}


def test_release_pr_accepts_only_release_metadata():
    verify_release.require_release_only(RELEASE_ONLY_PATHS)


def test_release_pr_rejects_runtime_or_workflow_changes():
    with pytest.raises(verify_release.ReleaseError):
        verify_release.require_release_only(RELEASE_ONLY_PATHS | {"kanban_task_threads/runtime.py"})


def test_all_version_surfaces_must_match():
    assert (
        verify_release.require_one_version(
            {"manifest": "0.5.0", "plugin": "0.5.0", "project": "0.5.0", "lock": "0.5.0"}
        )
        == "0.5.0"
    )


@pytest.mark.parametrize("tag", ["0.5.0", "v0.5", "v0.5.0-rc.1", "latest"])
def test_stable_tag_parser_rejects_non_stable_tags(tag):
    with pytest.raises(verify_release.ReleaseError):
        verify_release.parse_stable_tag(tag)


def test_merge_parent_verification_requires_exactly_two_distinct_parents():
    assert verify_release.require_merge_parents(
        "merge", ["main-parent", "candidate"], expected_first="main-parent"
    ) == (
        "main-parent",
        "candidate",
    )
    with pytest.raises(verify_release.ReleaseError):
        verify_release.require_merge_parents("merge", ["only-one"])
    with pytest.raises(verify_release.ReleaseError):
        verify_release.require_merge_parents("merge", ["same", "same"])
    with pytest.raises(verify_release.ReleaseError):
        verify_release.require_merge_parents(
            "merge", ["wrong-main", "candidate"], expected_first="main-parent"
        )


def test_cli_fails_closed_on_missing_or_invalid_json(tmp_path):
    missing = tmp_path / "missing.json"
    result = subprocess.run(
        [sys.executable, "scripts/ci/verify_release.py", "versions", "--json-file", str(missing)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"manifest": "0.5.0", "plugin": "0.5.1"}))
    result = subprocess.run(
        [sys.executable, "scripts/ci/verify_release.py", "versions", "--json-file", str(invalid)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
