import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _top_level_block(workflow: str, key: str) -> str:
    """Return one top-level YAML mapping block without parsing expressions."""
    match = re.search(rf"(?ms)^{re.escape(key)}:\s*\n(?P<body>(?:^[ \t]+.*\n|^\s*$)+)", workflow)
    assert match is not None, f"missing top-level {key!r} block"
    return match.group("body")


def _job_block(workflow: str, job: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job)}:\s*\n(?P<body>.*?)(?=^  [a-z0-9-]+:\s*\n|\Z)",
        workflow,
    )
    assert match is not None, f"missing job {job!r}"
    return match.group("body")


def _job_permissions(workflow: str, job: str) -> dict[str, str]:
    block = _job_block(workflow, job)
    match = re.search(r"(?ms)^    permissions:\s*\n(?P<body>(?:^      .*\n)+)", block)
    assert match is not None, f"missing permissions for job {job!r}"
    return dict(re.findall(r"(?m)^      ([a-z-]+): (read|write|none)\s*$", match.group("body")))


def _integration_surface_changed(paths: list[str]) -> bool:
    integration_surfaces = {
        "__init__.py",
        "plugin.yaml",
        "kanban_task_threads/runtime.py",
        "scripts/check_startup.py",
        ".github/workflows/ci.yml",
    }
    return bool(integration_surfaces.intersection(paths))


def _public_versions(root: Path) -> dict[str, list[str]]:
    manifest = json.loads((root / ".release-please-manifest.json").read_text())
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    plugin = (root / "plugin.yaml").read_text()
    lock = tomllib.loads((root / "uv.lock").read_text())

    plugin_match = re.search(r"(?m)^version:\s*([^\s#]+)", plugin)
    assert plugin_match is not None, "plugin.yaml has no top-level version"

    return {
        "manifest": [manifest["."]],
        "pyproject.toml": [pyproject["project"]["version"]],
        "plugin.yaml": [plugin_match.group(1)],
        "uv.lock": [
            package["version"]
            for package in lock["package"]
            if package["name"] == "kanban-task-threads"
        ],
    }


def test_manifest_matches_every_public_version_surface():
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    versions = _public_versions(ROOT)

    assert set(manifest) == {"."}
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", manifest["."])
    assert versions == {path: [manifest["."]] for path in versions}


def test_root_release_updates_every_public_version_surface():
    config = json.loads((ROOT / "release-please-config.json").read_text())
    package = config["packages"]["."]

    assert package["release-type"] == "python"
    assert package["changelog-path"] == "CHANGELOG.md"
    assert package["include-component-in-tag"] is False
    assert package["include-v-in-tag"] is True
    assert package["extra-files"] == [
        {"type": "yaml", "path": "plugin.yaml", "jsonpath": "$.version"},
        {
            "type": "toml",
            "path": "uv.lock",
            "jsonpath": '$.package[?(@.name.value === "kanban-task-threads")].version',
        },
    ]


def test_ci_runs_on_integration_and_public_main_with_read_only_default_permissions():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    trigger = _top_level_block(workflow, "on")
    permissions = _top_level_block(workflow, "permissions")

    assert re.search(r"(?m)^  push:\s*$", trigger)
    assert re.search(r"(?m)^    branches: \[pre-release, main\]\s*$", trigger)
    assert not re.search(r"(?m)^  pull_request:\s*$", trigger)
    assert "workflow_dispatch:" not in trigger

    parsed_permissions = dict(re.findall(r"(?m)^  ([a-z-]+): (read|write|none)\s*$", permissions))
    assert parsed_permissions == {"contents": "read"}


def test_pr_validation_is_one_job_and_cancels_superseded_commits():
    workflow = (ROOT / ".github/workflows/pr-validation.yml").read_text()
    jobs = _top_level_block(workflow, "jobs")
    concurrency = _top_level_block(workflow, "concurrency")

    assert set(re.findall(r"(?m)^  ([a-z0-9-]+):\s*$", jobs)) == {"validate"}
    assert "group: pr-validation-${{ github.event.pull_request.number }}" in concurrency
    assert "cancel-in-progress: true" in concurrency
    assert "name: PR validation" in _job_block(workflow, "validate")


def test_pr_validation_runs_the_canonical_fast_gate_once():
    workflow = (ROOT / ".github/workflows/pr-validation.yml").read_text()
    validate = _job_block(workflow, "validate")

    assert validate.count("python3 scripts/check.py fast") == 1
    assert "matrix:" not in _top_level_block(workflow, "jobs")


def test_pr_validation_classifies_integration_surfaces_from_exact_base_and_head():
    workflow = (ROOT / ".github/workflows/pr-validation.yml").read_text()
    changes = _job_block(workflow, "validate")

    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in changes
    assert "HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in changes
    assert 'git cat-file -e "${BASE_SHA}^{commit}"' in changes
    assert 'git diff --name-only "${BASE_SHA}" "${HEAD_SHA}"' in changes
    assert 'if [ "${CHECKED_OUT_SHA}" != "${HEAD_SHA}" ]; then' in changes


def test_integration_change_classification_includes_workflow_only_changes():
    assert _integration_surface_changed([".github/workflows/ci.yml"])
    assert _integration_surface_changed(["README.md", "plugin.yaml"])
    assert not _integration_surface_changed(["README.md", "docs/testing.md"])


def test_release_waits_for_every_validation_job_and_exports_release_identity():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "needs: [pr-policy, quality, hermes-validate, plugin-scanner]" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "id: release" in workflow
    assert "googleapis/release-please-action@5c625bfb5d1ff62eadeeb3772007f7f66fdcf071" in workflow
    assert "token: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "config-file: release-please-config.json" in workflow
    assert "manifest-file: .release-please-manifest.json" in workflow
    for output in ("release_created", "version", "sha", "tag_name"):
        assert f"{output}: ${{{{ steps.release.outputs.{output} }}}}" in workflow
    assert _job_permissions(workflow, "release-please") == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }


def test_pre_release_opens_or_reuses_one_promotion_pr_after_validation():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    promotion = _job_block(workflow, "promotion-pr")

    assert "needs: [pr-policy, quality, hermes-validate, plugin-scanner]" in promotion
    assert "github.event_name == 'push'" in promotion
    assert "github.ref == 'refs/heads/pre-release'" in promotion
    assert _job_permissions(workflow, "promotion-pr") == {
        "contents": "read",
        "pull-requests": "write",
    }
    assert "repos/${GITHUB_REPOSITORY}/git/ref/heads/pre-release" in promotion
    assert 'if [ "${CURRENT_SHA}" != "${GITHUB_SHA}" ]; then' in promotion
    assert "pre-release advanced while this run was validating" in promotion
    assert "repos/${GITHUB_REPOSITORY}/compare/main...pre-release" in promotion
    assert 'if [ "${AHEAD_BY}" -eq 0 ]; then' in promotion
    assert "repos/${GITHUB_REPOSITORY}/pulls" in promotion
    assert "-f state=open" in promotion
    assert "-f base=main" in promotion
    assert '-f "head=${GITHUB_REPOSITORY_OWNER}:pre-release"' in promotion
    assert "--paginate --slurp" in promotion
    assert 'if [ "${OPEN_PR_COUNT}" -gt 1 ]; then' in promotion
    assert "gh pr edit" in promotion
    assert "gh pr create" in promotion
    assert "--base main" in promotion
    assert "--head pre-release" in promotion
    assert 'PROMOTION_TITLE="chore(release): promote pre-release to main"' in promotion
    assert "Create a merge commit" in promotion
    assert promotion.count('--title "${PROMOTION_TITLE}"') == 2
    assert promotion.count('--body "${PROMOTION_BODY}"') == 2
    assert "secrets.GITHUB_TOKEN" in promotion
    assert "HERMES_CATALOG_TOKEN" not in promotion


def test_main_pr_policy_allows_only_promotion_and_release_please():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    policy = _job_block(workflow, "pr-policy")

    assert "github.event.pull_request.base.ref" in policy
    assert "github.event.pull_request.head.ref" in policy
    assert "github.event.pull_request.head.repo.full_name" in policy
    assert "github.event.pull_request.user.login" in policy
    assert '"${HEAD_REF}" = "pre-release"' in policy
    assert '"${HEAD_REF}" = "release-please--branches--main"' in policy
    assert '"${PR_AUTHOR}" = "github-actions[bot]"' in policy
    assert "Only the pre-release promotion or Release Please may target main" in policy


def test_catalog_pr_is_a_manual_main_only_workflow_with_a_scoped_credential():
    ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    workflow = (ROOT / ".github/workflows/sync-hermes-catalog.yml").read_text()
    trigger = _top_level_block(workflow, "on")
    catalog_job = _job_block(workflow, "sync-hermes-catalog")

    assert "sync-hermes-catalog:" not in ci_workflow
    assert "release_created" not in workflow
    assert re.search(r"(?m)^  workflow_dispatch:\s*$", trigger)
    assert re.search(r"(?m)^      release_tag:\s*$", trigger)
    assert "required: false" in trigger
    assert "sync-hermes-catalog:" in workflow
    assert "group: sync-hermes-catalog" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "if: ${{ github.ref == 'refs/heads/main' }}" in catalog_job
    assert "environment: hermes-catalog" in workflow
    assert catalog_job.count("GH_TOKEN: ${{ secrets.HERMES_CATALOG_TOKEN }}") == 2
    assert "token: ${{ secrets.HERMES_CATALOG_TOKEN }}" not in catalog_job
    assert catalog_job.count("persist-credentials: false") == 2
    assert "gh auth setup-git" in catalog_job
    assert "scripts/update_hermes_catalog.py" in workflow
    assert "python -m pip install pyyaml==6.0.2" in workflow
    assert "scripts/validate_plugin_catalog.py plugin-catalog/" in workflow
    assert "catalog/kanban-task-threads-v${RELEASE_VERSION}" in workflow
    assert "repos/${UPSTREAM_REPOSITORY}/pulls" in workflow
    assert "--paginate --slurp" in workflow
    assert ".head.repo.full_name == $repo" in workflow
    assert ".head.ref | startswith($prefix)" in workflow
    assert '"${OPEN_PLUGIN_PR_REF}" != "${CATALOG_BRANCH}"' in workflow
    assert 'git ls-remote --heads origin "refs/heads/${CATALOG_BRANCH}"' in workflow
    assert "gh pr list" not in workflow
    assert workflow.count("repos/${UPSTREAM_REPOSITORY}/pulls") == 3
    assert workflow.count('-f "head=diegomarino:${CATALOG_BRANCH}"') == 2
    assert workflow.count("-f state=all") == 2
    assert ".state | ascii_upcase" in workflow
    assert 'if .merged_at then "MERGED"' in workflow
    assert '"${PR_STATE}" = "CLOSED"' in workflow
    assert '"${PR_STATE}" = "MERGED"' in workflow
    assert 'git diff --name-only "${UPSTREAM_SHA}" "${REMOTE_SHA}"' in workflow
    assert '--force-with-lease="refs/heads/${CATALOG_BRANCH}:"' in workflow
    assert "--draft" in workflow
    assert re.search(
        r"(?m)^      - name: Open the upstream catalog PR\n"
        r"        if: .*\n"
        r"        working-directory: hermes-agent$",
        catalog_job,
    )
    assert "NousResearch/hermes-agent" in workflow
    assert "diegomarino:${CATALOG_BRANCH}" in workflow
    assert _job_permissions(workflow, "sync-hermes-catalog") == {"contents": "read"}


def test_catalog_workflow_resolves_and_verifies_an_exact_stable_release():
    workflow = (ROOT / ".github/workflows/sync-hermes-catalog.yml").read_text()
    catalog_job = _job_block(workflow, "sync-hermes-catalog")

    assert "github.event.inputs.release_tag" in catalog_job
    assert "repos/${GITHUB_REPOSITORY}/releases/latest" in catalog_job
    assert "repos/${GITHUB_REPOSITORY}/releases/tags/${REQUESTED_TAG}" in catalog_job
    assert ".draft == true or .prerelease == true" in catalog_job
    assert 'git rev-parse "${RELEASE_TAG}^{commit}"' in catalog_job
    assert 'git show "${RELEASE_SHA}:.release-please-manifest.json"' in catalog_job
    assert 'git cat-file -e "${RELEASE_SHA}:docs/assets/catalog-banner.png"' in catalog_job
    assert 'git cat-file -e "${RELEASE_SHA}:docs/assets/catalog-screenshot-full.png"' in catalog_job
    assert 'git cat-file -e "${RELEASE_SHA}:docs/assets/catalog-banner-detail.png"' in catalog_job
    assert "MANIFEST_VERSION" in catalog_job
    assert "version=${RELEASE_VERSION}" in catalog_job
    assert "sha=${RELEASE_SHA}" in catalog_job
    assert "tag_name=${RELEASE_TAG}" in catalog_job


def test_catalog_banner_is_a_two_to_one_png():
    banner = (ROOT / "docs/assets/catalog-banner.png").read_bytes()

    assert banner[:8] == b"\x89PNG\r\n\x1a\n"
    assert banner[12:16] == b"IHDR"
    width = int.from_bytes(banner[16:20], "big")
    height = int.from_bytes(banner[20:24], "big")
    assert (width, height) == (1200, 600)


def test_catalog_screenshot_assets_replace_the_old_forum_demo():
    full = ROOT / "docs/assets/catalog-screenshot-full.png"
    detail = ROOT / "docs/assets/catalog-banner-detail.png"

    assert full.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert detail.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not (ROOT / "docs/assets/forum-demo.png").exists()


def test_release_please_is_not_a_second_parallel_workflow():
    assert not (ROOT / ".github/workflows/release-please.yml").exists()


def _run_catalog_update(path: Path, version: str, sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/update_hermes_catalog.py"),
            str(path),
            version,
            sha,
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def test_catalog_updater_adds_pinned_catalog_media(tmp_path):
    catalog = tmp_path / "kanban-task-threads.yaml"
    catalog.write_text(
        "name: kanban-task-threads\n"
        "repo: https://github.com/diegomarino/kanban-task-threads\n"
        "sha: 0ae90a3869b8f3508bb8d9a96bc87bebb1c03094\n"
        'description: "Old disclosure"\n'
        'version: "0.2.2"\n'
        "capabilities:\n"
        "  provides_hooks:\n"
        "    - kanban_task_claimed\n"
    )

    result = _run_catalog_update(
        catalog,
        "0.3.0",
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854",
    )

    assert result.returncode == 0, result.stderr
    assert catalog.read_text() == (
        "name: kanban-task-threads\n"
        "repo: https://github.com/diegomarino/kanban-task-threads\n"
        "sha: e14bf61b538f5dc1425bc4c3bef7f12f0c212854\n"
        'description: "Give every Hermes kanban task a live Discord forum thread with a '
        "rewritten status card and append-only event log. Disclosure — sends task and "
        "lifecycle data to the configured Discord webhook (no host allowlist). Cards and "
        "event replies use built-in or operator-configured templates and may include "
        "identifiers, titles, status, assignment, branch and workspace metadata, "
        "relationships and blocking details, comments, event actors, reviewers, reasons, "
        "summaries, and configured dashboard links; the comment excerpt length is "
        "configurable. Fixed prerequisite announcements additionally send the prerequisite "
        "task ID, title, assignee, and up to 140 characters of its body and are not "
        "template-configurable. The absolute workspace path is off by default and requires "
        "explicit opt-in. Reads the kanban "
        "database read-only and stores thread mapping state under "
        "<kanban_home>/kanban/plugins/kanban-task-threads/. With an optional existing "
        "publisher-profile Discord bot token, reads forum and thread metadata; creates or "
        "repairs managed status tags and emojis; replaces applied tags on plugin-managed "
        "threads; renames threads; and archives or unarchives them. Discord fetches webhook "
        'avatars from the configured HTTPS asset origin."\n'
        'version: "0.3.0"\n'
        "image: https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854/docs/assets/catalog-banner.png\n"
        "screenshots:\n"
        "  - https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854/docs/assets/catalog-screenshot-full.png\n"
        "  - https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854/docs/assets/catalog-banner-detail.png\n"
        "capabilities:\n"
        "  provides_hooks:\n"
        "    - kanban_task_claimed\n"
    )


def test_catalog_updater_replaces_existing_pinned_catalog_media(tmp_path):
    catalog = tmp_path / "kanban-task-threads.yaml"
    catalog.write_text(
        "name: kanban-task-threads\n"
        "repo: https://github.com/diegomarino/kanban-task-threads\n"
        "sha: 0ae90a3869b8f3508bb8d9a96bc87bebb1c03094\n"
        'description: "Old disclosure"\n'
        'version: "0.2.2"\n'
        "image: https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "0ae90a3869b8f3508bb8d9a96bc87bebb1c03094/docs/assets/catalog-banner.png\n"
        "screenshots:\n"
        "  - https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "0ae90a3869b8f3508bb8d9a96bc87bebb1c03094/docs/assets/old.png\n"
    )

    result = _run_catalog_update(
        catalog,
        "0.3.0",
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854",
    )

    assert result.returncode == 0, result.stderr
    assert catalog.read_text().count("\nimage:") == 1
    assert catalog.read_text().count("\nscreenshots:") == 1
    assert (
        "image: https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854/docs/assets/catalog-banner.png\n"
        in catalog.read_text()
    )
    assert (
        "screenshots:\n"
        "  - https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854/docs/assets/catalog-screenshot-full.png\n"
        "  - https://raw.githubusercontent.com/diegomarino/kanban-task-threads/"
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854/docs/assets/catalog-banner-detail.png\n"
        in catalog.read_text()
    )


@pytest.mark.parametrize(
    ("name", "repo", "error"),
    [
        (
            "another-plugin",
            "https://github.com/diegomarino/kanban-task-threads",
            "expected catalog entry for kanban-task-threads",
        ),
        (
            "kanban-task-threads",
            "https://github.com/someone-else/kanban-task-threads",
            "expected repository https://github.com/diegomarino/kanban-task-threads",
        ),
    ],
)
def test_catalog_updater_rejects_the_wrong_catalog_identity(tmp_path, name, repo, error):
    catalog = tmp_path / "other.yaml"
    original = f'name: {name}\nrepo: {repo}\nsha: {"0" * 40}\nversion: "0.2.2"\n'
    catalog.write_text(original)

    result = _run_catalog_update(
        catalog,
        "0.3.0",
        "e14bf61b538f5dc1425bc4c3bef7f12f0c212854",
    )

    assert result.returncode != 0
    assert error in result.stderr
    assert catalog.read_text() == original


@pytest.mark.parametrize(
    ("version", "sha", "error"),
    [
        ("v0.3", "e14bf61b538f5dc1425bc4c3bef7f12f0c212854", "invalid release version"),
        ("01.2.3", "e14bf61b538f5dc1425bc4c3bef7f12f0c212854", "invalid release version"),
        ("1.2.3-alpha..1", "e14bf61b538f5dc1425bc4c3bef7f12f0c212854", "invalid release version"),
        ("1.2.3-...", "e14bf61b538f5dc1425bc4c3bef7f12f0c212854", "invalid release version"),
        ("1.2.3", "deadbeef", "invalid release SHA"),
    ],
)
def test_catalog_updater_rejects_invalid_release_identity(tmp_path, version, sha, error):
    catalog = tmp_path / "kanban-task-threads.yaml"
    original = (
        "name: kanban-task-threads\n"
        "repo: https://github.com/diegomarino/kanban-task-threads\n"
        f"sha: {'0' * 40}\n"
        'version: "0.2.2"\n'
    )
    catalog.write_text(original)

    result = _run_catalog_update(catalog, version, sha)

    assert result.returncode != 0
    assert error in result.stderr
    assert catalog.read_text() == original
