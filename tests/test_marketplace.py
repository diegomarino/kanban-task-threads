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
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert 'python-version: ["3.11", "3.12", "3.13"]' in workflow
    assert re.search(r"\bpytest==\d+\.\d+\.\d+\b", workflow)
    assert re.search(r"\bruff==\d+\.\d+\.\d+\b", workflow)
    assert "repository: NousResearch/hermes-agent" in workflow
    assert re.search(r"\n\s+ref:\s*[0-9a-f]{40}\b", workflow), (
        "Hermes source must be pinned to a full commit SHA"
    )
    assert "path: hermes-agent" in workflow
    assert "python -m pip install -e hermes-agent" in workflow, (
        "Hermes explicitly refuses non-editable package builds"
    )
    assert "hermes plugins validate plugin --json" in workflow
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


def test_ci_uploads_sarif_even_when_the_scanner_gate_fails():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "output: plugin-scanner.sarif" in workflow
    assert "upload_sarif: false" in workflow
    assert "github/codeql-action/upload-sarif@" in workflow
    assert "if: ${{ always()" in workflow
    assert "sarif_file: plugin-scanner.sarif" in workflow


def test_local_quality_tools_match_ci_pins():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    sandbox = (ROOT / "scripts" / "sandbox").read_text()
    pinned = dict(re.findall(r"\b(pytest|ruff)==(\d+\.\d+\.\d+)\b", workflow))
    assert pinned.keys() == {"pytest", "ruff"}
    for tool, version in pinned.items():
        assert f"{tool}=={version}" in sandbox


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
