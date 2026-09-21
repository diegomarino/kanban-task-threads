#!/usr/bin/env python3
"""Render the checked-in Phosphor SVGs into the versioned Pages contract.

Requires ``rsvg-convert`` (librsvg) and performs no network access. An operator
bumps the manifest version for a visual redesign, renders, and commits the new
directory without removing prior versions (ADR-0015).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kanban_task_threads.avatars import AVATAR_MESSAGE_TYPES

DEFAULT_MANIFEST = ROOT / "assets" / "avatars" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "pages"
SOURCE = ROOT / "assets" / "avatars" / "source"
PHOSPHOR_LICENSE = ROOT / "assets" / "avatars" / "PHOSPHOR-LICENSE.txt"


def inner_svg(source: str) -> str:
    match = re.search(r"<svg[^>]*>(.*)</svg>\s*$", source, flags=re.DOTALL)
    if not match:
        raise ValueError("unexpected SVG wrapper")
    return match.group(1)


def avatar_svg(inner: str, foreground: str, background: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"
  viewBox="0 0 256 256">
  <circle cx="128" cy="128" r="124" fill="{background}"/>
  <circle cx="128" cy="128" r="123" fill="none" stroke="{foreground}"
    stroke-opacity="0.16" stroke-width="2"/>
  <g transform="translate(28 28) scale(0.78125)" color="{foreground}" fill="{foreground}">
    {inner}
  </g>
</svg>
'''


def render(manifest_path: Path, output_dir: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != 1:
        raise ValueError("avatar manifest schema must be 1")
    if not re.fullmatch(r"v[1-9][0-9]*", manifest.get("version", "")):
        raise ValueError("avatar manifest version must look like v1")
    if manifest.get("png_size") != 96:
        raise ValueError("avatar manifest png_size must be exactly 96")
    items = manifest["items"]
    if tuple(items) != AVATAR_MESSAGE_TYPES:
        raise ValueError(
            f"manifest items must be exactly {AVATAR_MESSAGE_TYPES!r}, got {tuple(items)!r}"
        )
    themes = tuple(manifest["themes"])
    palettes = manifest["palettes"]
    if tuple(palettes) != ("colored", "black", "white"):
        raise ValueError("avatar palettes must be exactly colored, black, and white")
    version_dir = output_dir / manifest["version"]
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(PHOSPHOR_LICENSE, version_dir / PHOSPHOR_LICENSE.name)
    (output_dir / ".nojekyll").touch()
    with tempfile.TemporaryDirectory(prefix="ktt-avatars-") as temporary:
        svg_path = Path(temporary) / "avatar.svg"
        for theme in themes:
            for palette, palette_config in palettes.items():
                for message_type, item in items.items():
                    source_path = SOURCE / theme / f"{item['icon']}.svg"
                    foreground = palette_config.get("foreground", item["foreground"])
                    background = palette_config.get("background", item["background"])
                    svg_path.write_text(
                        avatar_svg(
                            inner_svg(source_path.read_text()),
                            foreground,
                            background,
                        )
                    )
                    relative = manifest["path_template"].format(
                        version=manifest["version"],
                        theme=theme,
                        palette=palette,
                        size=manifest["png_size"],
                        message_type=message_type,
                    )
                    destination = output_dir / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(
                        [
                            "rsvg-convert",
                            "-w",
                            str(manifest["png_size"]),
                            "-h",
                            str(manifest["png_size"]),
                            "-o",
                            str(destination),
                            str(svg_path),
                        ],
                        check=True,
                    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    render(args.manifest, args.output_dir)


if __name__ == "__main__":
    main()
