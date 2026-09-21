import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _top_level_block(workflow: str, key: str) -> str:
    """Return one top-level YAML mapping block without parsing expressions."""
    match = re.search(rf"(?ms)^{re.escape(key)}:\s*\n(?P<body>(?:^[ \t]+.*\n|^\s*$)+)", workflow)
    assert match is not None, f"missing top-level {key!r} block"
    return match.group("body")


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


def test_workflow_runs_only_on_public_main_with_documented_minimum_permissions():
    workflow = (ROOT / ".github/workflows/release-please.yml").read_text()
    trigger = _top_level_block(workflow, "on")
    permissions = _top_level_block(workflow, "permissions")

    assert re.search(r"(?m)^  push:\s*$", trigger)
    assert re.search(r"(?m)^    branches: \[main\]\s*$", trigger)
    assert "pull_request:" not in trigger
    assert "workflow_dispatch:" not in trigger

    parsed_permissions = dict(re.findall(r"(?m)^  ([a-z-]+): (read|write|none)\s*$", permissions))
    assert parsed_permissions == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }


def test_workflow_uses_only_github_token_and_the_reviewed_release_files():
    workflow = (ROOT / ".github/workflows/release-please.yml").read_text()

    assert "googleapis/release-please-action@5c625bfb5d1ff62eadeeb3772007f7f66fdcf071" in workflow
    assert "token: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "config-file: release-please-config.json" in workflow
    assert "manifest-file: .release-please-manifest.json" in workflow
    assert not re.search(r"(?i)(personal.access|\bpat\b|github.app)", workflow)
