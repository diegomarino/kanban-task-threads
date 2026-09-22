#!/usr/bin/env python3
"""Update this plugin's immutable release metadata in the Hermes catalog."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PLUGIN_NAME = "kanban-task-threads"
PLUGIN_REPOSITORY = "https://github.com/diegomarino/kanban-task-threads"
CATALOG_IMAGE_ROOT = "https://raw.githubusercontent.com/diegomarino/kanban-task-threads"
CATALOG_IMAGE_PATH = "docs/assets/catalog-banner.png"
SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)\."
    r"(?:0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


def _top_level_values(document: str, key: str) -> list[str]:
    return re.findall(rf"(?m)^{re.escape(key)}:\s*['\"]?([^\s'\"]+)['\"]?\s*$", document)


def _replace_one(document: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}:[^\n]*$")
    matches = list(pattern.finditer(document))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one top-level {key!r}, found {len(matches)}")
    return pattern.sub(f"{key}: {value}", document, count=1)


def _upsert_after(document: str, key: str, value: str, *, after: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}:[^\n]*$")
    matches = list(pattern.finditer(document))
    if len(matches) > 1:
        raise ValueError(f"expected at most one top-level {key!r}, found {len(matches)}")
    if matches:
        return pattern.sub(f"{key}: {value}", document, count=1)

    anchor = re.compile(rf"(?m)^{re.escape(after)}:[^\n]*$")
    anchors = list(anchor.finditer(document))
    if len(anchors) != 1:
        raise ValueError(f"expected exactly one top-level {after!r}, found {len(anchors)}")
    return anchor.sub(lambda match: f"{match.group(0)}\n{key}: {value}", document, count=1)


def update_catalog(path: Path, version: str, sha: str) -> None:
    version_match = SEMVER.fullmatch(version)
    if version_match is None or any(
        identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        for identifier in (version_match.group("prerelease") or "").split(".")
    ):
        raise ValueError(f"invalid release version: {version!r}")
    if COMMIT_SHA.fullmatch(sha) is None:
        raise ValueError(f"invalid release SHA: {sha!r}")

    document = path.read_text()
    names = _top_level_values(document, "name")
    if names != [PLUGIN_NAME]:
        raise ValueError(f"expected catalog entry for {PLUGIN_NAME}, found {names!r}")
    repositories = _top_level_values(document, "repo")
    if repositories != [PLUGIN_REPOSITORY]:
        raise ValueError(f"expected repository {PLUGIN_REPOSITORY}, found {repositories!r}")

    updated = _replace_one(document, "sha", sha)
    updated = _replace_one(updated, "version", f'"{version}"')
    image = f"{CATALOG_IMAGE_ROOT}/{sha}/{CATALOG_IMAGE_PATH}"
    updated = _upsert_after(updated, "image", image, after="version")
    path.write_text(updated)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog_path", type=Path)
    parser.add_argument("version")
    parser.add_argument("sha")
    args = parser.parse_args()

    try:
        update_catalog(args.catalog_path, args.version, args.sha)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
