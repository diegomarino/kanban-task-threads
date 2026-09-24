import subprocess
from pathlib import Path

import pytest

from scripts.ci import catalog_handoff

BRANCH = "catalog/kanban-task-threads-v1.2.3"
PIN = "a" * 40


@pytest.mark.parametrize(
    ("open_prs", "branch_prs", "remote_branch", "upstream_pin", "expected"),
    [
        ([], [], None, None, "create"),
        (
            [{"ref": BRANCH, "url": "https://example.test/pr/1"}],
            [],
            {"sha": "b" * 40, "pin": PIN},
            None,
            "reuse",
        ),
        (
            [],
            [{"state": "MERGED", "url": "https://example.test/pr/1"}],
            None,
            PIN,
            "already-merged",
        ),
    ],
)
def test_decide_returns_the_supported_catalog_handoff_actions(
    open_prs, branch_prs, remote_branch, upstream_pin, expected
):
    decision = catalog_handoff.decide(
        open_prs=open_prs,
        branch_prs=branch_prs,
        remote_branch=remote_branch,
        upstream_pin=upstream_pin,
        expected_branch=BRANCH,
        expected_pin=PIN,
    )

    assert decision.action == expected


@pytest.mark.parametrize(
    ("open_prs", "branch_prs", "remote_branch", "upstream_pin", "message"),
    [
        ([{"ref": "catalog/kanban-task-threads-v9.9.9"}], [], None, None, "Another catalog PR"),
        (
            [],
            [{"state": "CLOSED", "url": "https://example.test/pr/1"}],
            None,
            None,
            "closed without merging",
        ),
        (
            [],
            [],
            {"sha": "b" * 40, "pin": "c" * 40},
            None,
            "does not contain the expected release pin",
        ),
        (
            [{"ref": BRANCH}, {"ref": BRANCH}],
            [],
            None,
            None,
            "More than one open catalog PR",
        ),
        (None, [], None, None, "Missing catalog handoff API data"),
        ([], None, None, None, "Missing catalog handoff API data"),
    ],
)
def test_decide_fails_closed_when_catalog_handoff_cannot_be_proven(
    open_prs, branch_prs, remote_branch, upstream_pin, message
):
    with pytest.raises(catalog_handoff.HandoffError, match=message):
        catalog_handoff.decide(
            open_prs=open_prs,
            branch_prs=branch_prs,
            remote_branch=remote_branch,
            upstream_pin=upstream_pin,
            expected_branch=BRANCH,
            expected_pin=PIN,
        )


def test_catalog_pin_requires_one_complete_sha_field():
    assert catalog_handoff.catalog_pin(f"name: kanban-task-threads\nsha: {PIN}\n") == PIN

    with pytest.raises(catalog_handoff.HandoffError, match="exactly one catalog SHA"):
        catalog_handoff.catalog_pin("sha: incomplete\nsha: another\n")


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def _commit(repo: Path, content: str) -> str:
    (repo / "catalog.txt").write_text(content)
    _git(repo, "add", "catalog.txt")
    _git(repo, "commit", "-m", "catalog update")
    return _git(repo, "rev-parse", "HEAD")


def test_force_with_lease_creates_a_catalog_branch_in_a_local_bare_repository(tmp_path: Path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", remote], check=True)
    subprocess.run(["git", "init", source], check=True)
    _git(source, "config", "user.name", "Test User")
    _git(source, "config", "user.email", "test@example.test")
    expected_sha = _commit(source, "v1")
    _git(source, "remote", "add", "origin", str(remote))

    _git(
        source,
        "push",
        f"--force-with-lease=refs/heads/{BRANCH}:",
        "origin",
        f"HEAD:refs/heads/{BRANCH}",
    )

    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == expected_sha


def test_force_with_lease_updates_an_existing_matching_catalog_branch_in_a_local_bare_repository(
    tmp_path: Path,
):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", remote], check=True)
    subprocess.run(["git", "init", source], check=True)
    _git(source, "config", "user.name", "Test User")
    _git(source, "config", "user.email", "test@example.test")
    first_sha = _commit(source, "v1")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    expected_sha = _commit(source, "v2")

    _git(
        source,
        "push",
        f"--force-with-lease=refs/heads/{BRANCH}:{first_sha}",
        "origin",
        f"HEAD:refs/heads/{BRANCH}",
    )

    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}") == expected_sha
