#!/usr/bin/env python3
"""Make fail-closed catalog handoff decisions from captured local state."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HandoffError(ValueError):
    """Raised when catalog publication state cannot be proven safely."""


@dataclass(frozen=True)
class Decision:
    action: str


def catalog_pin(document: str) -> str:
    pins = re.findall(r"(?m)^sha:\s*['\"]?([0-9a-f]{40})['\"]?\s*$", document)
    if len(pins) != 1:
        raise HandoffError("Expected exactly one catalog SHA field.")
    return pins[0]


def _require_pr_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise HandoffError("Missing catalog handoff API data.")
    return value


def decide(
    *,
    open_prs: object,
    branch_prs: object,
    remote_branch: object,
    upstream_pin: str | None,
    expected_branch: str,
    expected_pin: str,
) -> Decision:
    """Choose create, reuse, or already-merged only when evidence is unambiguous."""
    open_catalog_prs = _require_pr_list(open_prs)
    release_prs = _require_pr_list(branch_prs)
    if len(open_catalog_prs) > 1:
        raise HandoffError("More than one open catalog PR exists for this plugin; refusing.")
    if open_catalog_prs and open_catalog_prs[0].get("ref") != expected_branch:
        raise HandoffError("Another catalog PR is already open for this plugin; refusing.")
    if len(release_prs) > 1:
        raise HandoffError(f"More than one catalog PR exists for {expected_branch}; refusing.")

    state = release_prs[0].get("state") if release_prs else None
    if state == "CLOSED":
        raise HandoffError("Prior catalog PR was closed without merging.")
    if state == "MERGED":
        if upstream_pin != expected_pin:
            raise HandoffError("Merged catalog PR does not contain the expected release pin.")
        return Decision("already-merged")
    if state not in (None, "OPEN"):
        raise HandoffError(f"Unexpected catalog PR state: {state}")

    if remote_branch is None:
        if state == "OPEN":
            raise HandoffError("Open catalog PR has no source branch.")
        return Decision("create")
    if not isinstance(remote_branch, dict) or not remote_branch.get("sha"):
        raise HandoffError("Missing catalog handoff API data.")
    if remote_branch.get("pin") != expected_pin:
        raise HandoffError("Existing catalog branch does not contain the expected release pin.")
    return Decision("reuse")


def _json(path: Path) -> object:
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-prs-file", type=Path, required=True)
    parser.add_argument("--branch-prs-file", type=Path, required=True)
    parser.add_argument("--remote-branch-file", type=Path, required=True)
    parser.add_argument("--remote-catalog-file", type=Path, required=True)
    parser.add_argument("--upstream-catalog-file", type=Path, required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--expected-pin", required=True)
    arguments = parser.parse_args(argv)
    try:
        remote_branch = _json(arguments.remote_branch_file)
        if remote_branch is not None:
            if not isinstance(remote_branch, dict):
                raise HandoffError("Missing catalog handoff API data.")
            remote_pin = catalog_pin(arguments.remote_catalog_file.read_text())
            remote_branch = {**remote_branch, "pin": remote_pin}
        upstream_catalog = arguments.upstream_catalog_file.read_text()
        upstream_pin = catalog_pin(upstream_catalog) if upstream_catalog else None
        decision = decide(
            open_prs=_json(arguments.open_prs_file),
            branch_prs=_json(arguments.branch_prs_file),
            remote_branch=remote_branch,
            upstream_pin=upstream_pin,
            expected_branch=arguments.expected_branch,
            expected_pin=arguments.expected_pin,
        )
    except (HandoffError, OSError, json.JSONDecodeError) as error:
        print(error)
        return 1
    print(decision.action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
