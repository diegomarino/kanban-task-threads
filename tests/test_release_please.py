import json
import os
import re
import subprocess
import sys
import textwrap
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


def _integration_classifier() -> str:
    workflow = (ROOT / ".github/workflows/pr-validation.yml").read_text()
    match = re.search(
        r"(?ms)^      - name: Classify integration-sensitive changes\n.*?^        run: \|\n"
        r"(?P<script>(?:^          [^\n]*\n)+?)(?=^      - name:|^        if:|\Z)",
        workflow,
    )
    assert match is not None, "missing integration classifier step"
    return textwrap.dedent(match.group("script"))


def _commit(repo: Path, relative_path: str, content: str, message: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", relative_path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _history_with_change(tmp_path: Path, relative_path: str) -> tuple[Path, str, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Workflow tests"], cwd=repo, check=True)
    base = _commit(repo, "README.md", "base\n", "base")
    head = _commit(repo, relative_path, "changed\n", "change")
    return repo, base, head


def _run_integration_classifier(
    repo: Path, base: str, head: str
) -> subprocess.CompletedProcess[str]:
    output = repo / "github-output"
    environment = os.environ | {"BASE_SHA": base, "HEAD_SHA": head, "GITHUB_OUTPUT": str(output)}
    return subprocess.run(
        ["bash", "-c", _integration_classifier()],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
    )


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
    workflow = (ROOT / ".github/workflows/candidate.yml").read_text()
    trigger = _top_level_block(workflow, "on")
    permissions = _top_level_block(workflow, "permissions")

    assert re.search(r"(?m)^  push:\s*$", trigger)
    assert re.search(r"(?m)^    branches: \[pre-release\]\s*$", trigger)
    assert not re.search(r"(?m)^  pull_request:\s*$", trigger)
    assert "workflow_dispatch:" in trigger
    assert "hermes_ref:" in trigger
    assert "required: true" in trigger

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


def test_integration_classifier_marks_workflow_only_changes(tmp_path: Path):
    repo, base, head = _history_with_change(tmp_path, ".github/workflows/candidate.yml")

    result = _run_integration_classifier(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert (repo / "github-output").read_text() == "integration_changed=true\n"


def test_integration_classifier_skips_docs_only_changes(tmp_path: Path):
    repo, base, head = _history_with_change(tmp_path, "docs/testing.md")

    result = _run_integration_classifier(repo, base, head)

    assert result.returncode == 0, result.stderr
    assert (repo / "github-output").read_text() == "integration_changed=false\n"


def test_integration_classifier_fails_closed_for_missing_or_mismatched_pull_request_shas(
    tmp_path: Path,
):
    repo, base, head = _history_with_change(tmp_path, "plugin.yaml")

    missing_base = _run_integration_classifier(repo, "0" * 40, head)
    mismatched_head = _run_integration_classifier(repo, base, base)

    assert missing_base.returncode != 0
    assert mismatched_head.returncode != 0


def test_candidate_aggregates_every_expensive_validation_job():
    workflow = (ROOT / ".github/workflows/candidate.yml").read_text()

    aggregate = _job_block(workflow, "release-candidate")
    assert "needs: [quality, hermes-validate, plugin-scanner]" in aggregate
    assert "QUALITY_RESULT" in aggregate
    assert "HERMES_RESULT" in aggregate
    assert "SCANNER_RESULT" in aggregate
    assert "release-please-action" not in workflow


def test_release_workflow_preserves_release_please_public_contract():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    trigger = _top_level_block(workflow, "on")
    release = _job_block(workflow, "release-please")

    assert re.search(r"(?m)^  push:\s*$", trigger)
    assert re.search(r"(?m)^    branches: \[main\]\s*$", trigger)
    assert "name: Release Please" in release
    assert "needs: verify-main-provenance" in release
    assert "googleapis/release-please-action@5c625bfb5d1ff62eadeeb3772007f7f66fdcf071" in release
    assert "token: ${{ secrets.GITHUB_TOKEN }}" in release
    assert "config-file: release-please-config.json" in release
    assert "manifest-file: .release-please-manifest.json" in release
    for output in ("release_created", "version", "sha", "tag_name"):
        assert f"{output}: ${{{{ steps.release.outputs.{output} }}}}" in release
    assert _job_permissions(workflow, "release-please") == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }


def test_release_stages_are_provenance_only_and_release_pr_is_allowlisted():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "strategy:" not in workflow
    assert "hermes-agent" not in workflow
    assert "ai-plugin-scanner" not in workflow
    assert "python -m pytest" not in workflow
    assert "verify_release.py release-only" in workflow
    assert "verify_release.py versions" in workflow
    assert "verify_candidate.py --runs-file" in workflow
    assert "name: Release metadata" in workflow
    assert "github.com/rhysd/actionlint/cmd/actionlint@v1.7.9" in workflow
    assert "commits/${SHA}/pulls" in workflow
    assert "BEFORE_SHA: ${{ github.event.before }}" in workflow
    assert '--expected-first "${BEFORE_SHA}"' in workflow
    assert '.head.ref == "release-please--branches--main"' in workflow
    assert '.user.login == "github-actions[bot]"' in workflow
    assert workflow.count(".merge_commit_sha == $merge") == 2
    assert 'test "${RELEASE_PR_COUNT}" = 1' in workflow
    assert '.head.ref == "pre-release"' in workflow
    assert ".head.sha == $candidate" in workflow
    assert 'test "${PROMOTION_PR_COUNT}" = 1' in workflow
    assert "release-please-action" in _job_block(workflow, "release-please")


def test_pre_release_opens_or_reuses_one_promotion_pr_after_validation():
    workflow = (ROOT / ".github/workflows/candidate.yml").read_text()
    promotion = _job_block(workflow, "promotion-pr")

    assert "needs: release-candidate" in promotion
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


def test_promotion_identity_fails_closed_on_wrong_origin_or_candidate_data():
    workflow = (ROOT / ".github/workflows/promotion-identity.yml").read_text()
    identity = _job_block(workflow, "verify-promotion-identity")

    assert "head.repo.full_name == github.repository" in identity
    assert "head.ref == 'pre-release'" in identity
    assert "git/ref/heads/pre-release" in identity
    assert 'test "${CURRENT_SHA}" = "${HEAD_SHA}"' in identity
    assert "actions/workflows/candidate.yml/runs?head_sha=${HEAD_SHA}&event=push" in identity
    assert ".workflow_runs[]" in identity
    assert '.event == "push"' in identity
    assert "verify_candidate.py --runs-file candidate-runs.json --sha" in identity
    assert 'git cat-file -e "${HEAD_SHA}:.github/workflows/candidate.yml"' in identity


def test_catalog_pr_is_a_manual_main_only_workflow_with_a_scoped_credential():
    ci_workflow = (ROOT / ".github/workflows/candidate.yml").read_text()
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
    assert "scripts/ci/catalog_handoff.py" in workflow
    assert "--open-prs-file" in workflow
    assert "--branch-prs-file" in workflow
    assert "--remote-branch-file" in workflow
    assert "--upstream-catalog-file" in workflow
    assert "python -m pip install pyyaml==6.0.2" in workflow
    assert "scripts/validate_plugin_catalog.py plugin-catalog/" in workflow
    assert "catalog/kanban-task-threads-v${RELEASE_VERSION}" in workflow
    assert "repos/${UPSTREAM_REPOSITORY}/pulls" in workflow
    assert "--paginate --slurp" in workflow
    assert ".head.repo.full_name == $repo" in workflow
    assert ".head.ref | startswith($prefix)" in workflow
    assert '--expected-branch "${CATALOG_BRANCH}"' in workflow
    assert 'git ls-remote --heads origin "refs/heads/${CATALOG_BRANCH}"' in workflow
    assert "gh pr list" not in workflow
    assert workflow.count("repos/${UPSTREAM_REPOSITORY}/pulls") == 3
    assert workflow.count('-f "head=diegomarino:${CATALOG_BRANCH}"') == 2
    assert workflow.count("-f state=all") == 2
    assert ".state | ascii_upcase" in workflow
    assert 'if .merged_at then "MERGED"' in workflow
    assert 'case "${ACTION}" in' in workflow
    assert "already-merged)" in workflow
    assert 'git diff --name-only "${UPSTREAM_SHA}" "${REMOTE_SHA}"' in workflow
    assert '--force-with-lease="refs/heads/${CATALOG_BRANCH}:"' in workflow
    assert "it will not retry an ambiguous push" in workflow
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
