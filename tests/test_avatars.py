"""The avatar catalog is deterministic, complete, and safe to publish by URL."""

import json
import struct

import pytest

from kanban_task_threads.avatars import (
    AVATAR_MESSAGE_TYPES,
    AVATAR_VERSION,
    DEFAULT_AVATAR_COLORS,
    AvatarConfigError,
    AvatarSet,
)
from scripts.render_avatars import render


def test_default_duotone_urls_cover_every_message_type():
    avatars = AvatarSet("https://assets.example.invalid/ktt")

    assert avatars.theme == "duotone"
    assert avatars.palette == "colored"
    assert {
        message_type: avatars.url_for(message_type) for message_type in AVATAR_MESSAGE_TYPES
    } == {
        message_type: (
            f"https://assets.example.invalid/ktt/v1/duotone/colored/96px/{message_type}.png"
        )
        for message_type in AVATAR_MESSAGE_TYPES
    }


def test_unknown_message_type_uses_the_default_avatar():
    avatars = AvatarSet("https://assets.example.invalid/ktt")

    assert avatars.url_for("future_event") == avatars.url_for("default")


def test_theme_and_opinionated_palette_are_globally_configurable():
    avatars = AvatarSet(
        "https://assets.example.invalid/ktt/",
        theme="bold",
        palette="white",
    )

    assert (
        avatars.url_for("blocked")
        == "https://assets.example.invalid/ktt/v1/bold/white/96px/blocked.png"
    )
    assert avatars.url_for("commented").endswith("/v1/bold/white/96px/commented.png")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_url": "http://assets.example.invalid/ktt"}, "HTTPS"),
        ({"base_url": "https://user:pass@example.invalid/ktt"}, "credentials"),
        ({"base_url": "https://assets.example.invalid/ktt?latest=1"}, "query"),
        ({"base_url": "https://assets.example.invalid/ktt?"}, "query"),
        ({"base_url": "https://assets.example.invalid/ktt#"}, "fragment"),
        ({"base_url": "https://bad host.example/ktt"}, "whitespace"),
        ({"base_url": "\x00https://assets.example.invalid/ktt"}, "control"),
        ({"base_url": "https://assets.example.invalid/ktt\\avatars"}, "backslash"),
        ({"base_url": "https://assets.example.invalid:99999/ktt"}, "port"),
        ({"base_url": "https://assets.example.invalid/ktt", "theme": "thin"}, "theme"),
        (
            {"base_url": "https://assets.example.invalid/ktt", "palette": "transparent"},
            "palette",
        ),
    ],
)
def test_invalid_avatar_configuration_is_rejected(kwargs, message):
    with pytest.raises(AvatarConfigError, match=message):
        AvatarSet(**kwargs)


def test_checked_in_assets_match_the_catalog_and_are_96px_pngs():
    root = __import__("pathlib").Path(__file__).resolve().parents[1] / "pages"
    expected = {
        AvatarSet("https://assets.example.invalid", palette=palette).relative_path(
            message_type, theme=theme
        )
        for theme in ("duotone", "fill", "bold")
        for palette in ("colored", "black", "white")
        for message_type in AVATAR_MESSAGE_TYPES
    }
    active_root = root / AVATAR_VERSION
    actual = {path.relative_to(root).as_posix() for path in active_root.rglob("*.png")}

    assert actual == expected
    for relative in expected:
        data = (root / relative).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (96, 96)


def test_published_version_includes_the_phosphor_license():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]

    assert (root / "pages" / AVATAR_VERSION / "PHOSPHOR-LICENSE.txt").read_text() == (
        root / "assets" / "avatars" / "PHOSPHOR-LICENSE.txt"
    ).read_text()


def test_pages_manifest_is_the_published_copy_of_the_render_manifest():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    source = json.loads((root / "assets" / "avatars" / "manifest.json").read_text())
    published = json.loads((root / "pages" / "v1" / "manifest.json").read_text())

    assert published == source
    assert source["version"] == "v1"
    assert source["path_template"] == ("{version}/{theme}/{palette}/{size}px/{message_type}.png")
    assert tuple(source["palettes"]) == ("colored", "black", "white")


def test_pages_workflow_uses_the_official_actions_and_read_only_checkout():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "deploy-avatar-pages.yml").read_text()
    uses_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

    assert "contents: read" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    pinned_actions = {
        ("checkout", "d23441a48e516b6c34aea4fa41551a30e30af803", "v6.1.0"),
        ("configure-pages", "983d7736d9b0ae728b81ab479565c72886d7745b", "v5.0.0"),
        ("upload-pages-artifact", "7b1f4a764d45c48632c6b24a0339c27f5614fb0b", "v4.0.0"),
        ("deploy-pages", "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e", "v4.0.5"),
    }
    expected_actions = {
        f"uses: actions/{action}@{sha} # {version}" for action, sha, version in pinned_actions
    }
    assert expected_actions == set(uses_lines)
    assert all("@v" not in line for line in uses_lines)
    assert "path: pages" in workflow
    assert "contents: write" not in workflow


def test_renderer_rejects_a_size_that_would_mislabel_the_output(tmp_path):
    manifest = {
        "schema": 1,
        "version": "v1",
        "png_size": 128,
        "path_template": "{version}/{theme}/{palette}/{size}px/{message_type}.png",
        "themes": ["duotone"],
        "palettes": {
            "colored": {"description": "test"},
            "black": {"foreground": "#000000", "background": "#FFFFFF"},
            "white": {"foreground": "#FFFFFF", "background": "#000000"},
        },
        "items": {
            message_type: {
                "icon": "unused",
                "foreground": foreground,
                "background": background,
            }
            for message_type, (foreground, background) in DEFAULT_AVATAR_COLORS.items()
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="96"):
        render(manifest_path, tmp_path / "output")
