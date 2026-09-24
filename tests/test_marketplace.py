"""Marketplace hygiene pins: the drift the ecosystem survey saw in the wild
(mismatched versions between files, changelogs that skip releases, manifests
that stop parsing) breaks catalog admission or update flows — cheaper to break
the suite. `hermes plugins validate` is the real gate; these guard what it
cannot see locally."""

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLIC_REPOSITORY = "https://github.com/diegomarino/kanban-task-threads"


def manifest() -> dict:
    # minimal YAML reading without a YAML dependency: flat `key: value` lines
    fields = {}
    for line in (ROOT / "plugin.yaml").read_text().splitlines():
        match = re.match(r"^([a-z_]+):\s*(.+?)\s*$", line)
        if match and not line.startswith(" "):
            fields[match.group(1)] = match.group(2).strip("\"'")
    return fields


def test_versions_agree_everywhere():
    version = manifest()["version"]
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{version}"' in pyproject, (
        f"plugin.yaml says {version} but pyproject.toml disagrees — "
        "the wild's most common release drift"
    )


def test_changelog_covers_the_current_version():
    version = manifest()["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"[{version}]" in changelog, (
        f"CHANGELOG.md has no [{version}] section; RELEASING.md step 2 was skipped"
    )


def test_manifest_required_fields_are_present_and_sane():
    fields = manifest()
    for required in ("name", "version", "description", "license"):
        assert fields.get(required), f"plugin.yaml lacks {required} (validate would fail)"
    assert fields["name"] == "kanban-task-threads"
    assert re.fullmatch(r"\d+\.\d+\.\d+", fields["version"]), "version must be semver"
    assert fields.get("manifest_version", "1") == "1", (
        "manifest_version 2 is refused by the installer"
    )
    assert fields.get("homepage") == PUBLIC_REPOSITORY


def test_manifest_exposes_only_settings_the_runtime_supports():
    text = (ROOT / "plugin.yaml").read_text()
    config_schema = text.split("config_schema:\n", 1)[1]
    assert re.search(r"^  comment_excerpt_chars:\n", config_schema, re.MULTILINE)
    assert re.search(r"^    default: 180$", config_schema, re.MULTILINE)
    assert not re.search(r"^  transport:\n", config_schema, re.MULTILINE)


def test_official_install_uses_the_flat_plugin_identifier():
    readme = (ROOT / "README.md").read_text()
    configuration = (ROOT / "docs" / "configuration.md").read_text()
    assert "hermes plugins install diegomarino/kanban-task-threads" in readme
    assert "hermes plugins enable kanban-task-threads" in readme
    assert "hermes plugins disable kanban-task-threads" in readme
    assert "plugins.entries.kanban-task-threads.settings" in configuration


def test_public_tree_excludes_agent_controls_and_private_development_context():
    for filename in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        assert not (ROOT / filename).exists(), f"agent control file must not ship: {filename}"

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    private_markers = (
        "Hermes" + "Workspace",
        "MarSan" + "Vault",
        "/Users/" + "diego",
        "taskz" + "-test",
        "<" + "owner>",
        "<" + "category>",
    )
    offenders = {}
    for relative in tracked:
        path = ROOT / relative
        if not path.is_file() or path.suffix in {".png", ".pyc"}:
            continue
        text = path.read_text(errors="replace")
        hits = [marker for marker in private_markers if marker in text]
        if hits:
            offenders[relative] = hits
    assert not offenders, f"private or placeholder context remains in public tree: {offenders}"


def test_ci_pins_tools_and_runs_catalog_gates():
    workflow = (ROOT / ".github" / "workflows" / "candidate.yml").read_text()
    assert 'python-version: ["3.11", "3.13"]' in workflow
    assert re.search(r"\bpytest==\d+\.\d+\.\d+\b", workflow)
    assert re.search(r"\bruff==\d+\.\d+\.\d+\b", workflow)
    assert "repository: NousResearch/hermes-agent" in workflow
    assert "ref: ${{ matrix.hermes-ref }}" in workflow
    hermes_matrix = re.search(r"hermes-ref:\n((?:[ \t]+- [^\n]+\n)+)", workflow)
    assert hermes_matrix, "Hermes compatibility matrix is missing"
    refs = re.findall(r"^[ \t]+- ([^\s#]+)", hermes_matrix[1], re.MULTILINE)
    assert refs and all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs), (
        "Every Hermes source must be pinned to a full commit SHA"
    )
    assert "path: hermes-agent" in workflow
    assert "python -m pip install -e hermes-agent" in workflow, (
        "Hermes explicitly refuses non-editable package builds"
    )
    assert "hermes plugins validate plugin --json" in workflow
    assert "python plugin/scripts/check_startup.py" in workflow
    assert "hashgraph-online/ai-plugin-scanner-action@" in workflow
    assert "min_score: 80" in workflow
    assert "format: sarif" in workflow

    external_actions = re.findall(r"^\s*- uses: ([^./][^@]+)@([^\s#]+)", workflow, re.MULTILINE)
    assert external_actions
    unpinned = {}
    for name, ref in external_actions:
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            unpinned[name] = ref
    assert not unpinned, f"GitHub actions must be pinned to full commit SHAs: {unpinned}"


def test_pr_validation_pins_its_actions_and_keeps_read_only_permissions():
    workflow = (ROOT / ".github" / "workflows" / "pr-validation.yml").read_text()

    assert re.search(r"(?m)^permissions:\n  contents: read$", workflow)
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "github.com/rhysd/actionlint/cmd/actionlint@v1.7.9" in workflow
    assert 'echo "$(go env GOPATH)/bin" >> "${GITHUB_PATH}"' in workflow
    assert "python -m pip install --disable-pip-version-check uv==0.8.15" in workflow

    external_actions = re.findall(r"^\s+uses: ([^./][^@]+)@([^\s#]+)", workflow, re.MULTILINE)
    assert external_actions
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for _, ref in external_actions)


def test_required_hermes_check_has_a_stable_name_and_fails_closed():
    workflow = (ROOT / ".github" / "workflows" / "candidate.yml").read_text()
    gate = re.search(r"^  hermes-validate:\n(.*?)(?=^  \S|\Z)", workflow, re.MULTILINE | re.DOTALL)
    assert gate, "Required Hermes check is missing"
    body = gate[1]
    assert "name: Hermes plugin validation\n" in body
    assert "strategy:" not in body, "A matrix changes the required check's name"
    assert "needs: hermes-compatibility" in body
    assert "if: ${{ always() && github.event_name == 'push' }}" in body
    command = 'test "$COMPATIBILITY_RESULT" = success'
    for result in ("success", "failure", "cancelled", "skipped", ""):
        completed = subprocess.run(
            ["bash", "-c", command],
            env={"COMPATIBILITY_RESULT": result},
            capture_output=True,
            text=True,
        )
        assert (completed.returncode == 0) == (result == "success"), result


def test_ci_uploads_sarif_even_when_the_scanner_gate_fails():
    workflow = (ROOT / ".github" / "workflows" / "candidate.yml").read_text()
    assert "output: plugin-scanner.sarif" in workflow
    assert "upload_sarif: false" in workflow
    assert "github/codeql-action/upload-sarif@" in workflow
    assert "if: ${{ always()" in workflow
    assert "sarif_file: plugin-scanner.sarif" in workflow


def test_local_quality_tools_match_ci_pins():
    workflow = (ROOT / ".github" / "workflows" / "candidate.yml").read_text()
    sandbox = (ROOT / "scripts" / "sandbox").read_text()
    pinned = dict(re.findall(r"\b(pytest|ruff)==(\d+\.\d+\.\d+)\b", workflow))
    assert pinned.keys() == {"pytest", "ruff"}
    for tool, version in pinned.items():
        assert f"{tool}=={version}" in sandbox


def test_candidate_gate_has_only_the_planned_expensive_jobs():
    workflow = (ROOT / ".github" / "workflows" / "candidate.yml").read_text()
    assert workflow.count('python-version: ["3.11", "3.13"]') == 1
    assert workflow.count("hermes-ref:") == 1
    assert workflow.count("min_score: 80") == 1
    assert "release-candidate:" in workflow
    assert "needs: [quality, hermes-validate, plugin-scanner]" in workflow


def test_manual_probe_has_an_event_isolated_concurrency_lane():
    workflow = (ROOT / ".github" / "workflows" / "candidate.yml").read_text()

    assert "group: release-candidate-${{ github.event_name }}-${{ github.ref }}" in workflow
    assert "cancel-in-progress: true" in workflow


def test_promotion_identity_does_not_repeat_expensive_candidate_work():
    workflow = (ROOT / ".github" / "workflows" / "promotion-identity.yml").read_text()
    assert "python -m pytest" not in workflow
    assert "ruff " not in workflow
    assert "hermes-agent" not in workflow
    assert "ai-plugin-scanner" not in workflow


def test_privacy_docs_disclose_dependency_announcement_body_excerpt():
    privacy = (ROOT / "README.md").read_text().split("## Privacy and egress", 1)[1]
    assert "first 140 characters" in privacy
    assert "no opt-out" in privacy


def test_no_self_update_machinery_exists():
    # Catalog rule 3 bans self-updaters outright; make the tempting name
    # impossible to add quietly.
    offenders = [
        p
        for p in (ROOT / "kanban_task_threads").glob("*.py")
        if "update_check" in p.name or "self_update" in p.name
    ]
    assert not offenders, f"self-update machinery is banned from the catalog: {offenders}"


def test_license_file_matches_manifest():
    assert manifest()["license"] == "MIT"
    assert "MIT License" in (ROOT / "LICENSE").read_text()
