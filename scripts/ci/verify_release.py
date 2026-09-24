#!/usr/bin/env python3
"""Fail-closed deterministic checks for promotion and release metadata."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


class ReleaseError(ValueError):
    """Raised when release provenance or metadata cannot be proven."""


RELEASE_ONLY_PATHS = {
    ".release-please-manifest.json",
    "CHANGELOG.md",
    "plugin.yaml",
    "pyproject.toml",
    "uv.lock",
}


def require_release_only(paths: set[str]) -> None:
    unknown = paths - RELEASE_ONLY_PATHS
    if not paths or unknown:
        raise ReleaseError(f"Release metadata paths are missing or invalid: {sorted(unknown)}")


def require_one_version(versions: dict[str, str]) -> str:
    required = {"manifest", "plugin", "project", "lock"}
    if set(versions) != required or any(not value for value in versions.values()):
        raise ReleaseError("Every version surface must be present.")
    unique = set(versions.values())
    if len(unique) != 1:
        raise ReleaseError(f"Version surfaces disagree: {versions}")
    return unique.pop()


def parse_stable_tag(tag: str) -> str:
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag)
    if match is None:
        raise ReleaseError(f"Not a stable release tag: {tag}")
    return match.group(1)


def require_merge_parents(
    commit: str, parents: list[str], *, expected_first: str | None = None
) -> tuple[str, str]:
    if not commit or len(parents) != 2 or parents[0] == parents[1] or commit in parents:
        raise ReleaseError("A release-train main update must be a two-parent merge commit.")
    if expected_first is not None and parents[0] != expected_first:
        raise ReleaseError("The merge first parent is not the previous main commit.")
    return parents[0], parents[1]


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    release_only = subparsers.add_parser("release-only")
    release_only.add_argument("--paths-file", type=Path, required=True)
    versions = subparsers.add_parser("versions")
    versions.add_argument("--json-file", type=Path, required=True)
    merge = subparsers.add_parser("merge-parents")
    merge.add_argument("--json-file", type=Path, required=True)
    merge.add_argument("--expected-first")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "release-only":
            require_release_only(set(arguments.paths_file.read_text().splitlines()))
        elif arguments.command == "versions":
            print(require_one_version(_json(arguments.json_file)))
        else:
            payload = _json(arguments.json_file)
            first, second = require_merge_parents(
                payload["commit"], payload["parents"], expected_first=arguments.expected_first
            )
            print(json.dumps({"first": first, "second": second}))
    except (ReleaseError, OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
