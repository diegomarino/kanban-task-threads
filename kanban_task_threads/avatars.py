"""Message-avatar catalog and deterministic static-asset URLs (ADR-0015)."""

import json
from pathlib import Path
from urllib.parse import urlsplit

_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "assets" / "avatars" / "manifest.json"
AVATAR_MANIFEST = json.loads(_MANIFEST_PATH.read_text())
AVATAR_VERSION = AVATAR_MANIFEST["version"]
AVATAR_SIZE = AVATAR_MANIFEST["png_size"]
AVATAR_THEMES = tuple(AVATAR_MANIFEST["themes"])
AVATAR_PALETTES = tuple(AVATAR_MANIFEST["palettes"])
AVATAR_MESSAGE_TYPES = tuple(AVATAR_MANIFEST["items"])

DEFAULT_AVATAR_COLORS = {
    key: (item["foreground"], item["background"]) for key, item in AVATAR_MANIFEST["items"].items()
}


class AvatarConfigError(ValueError):
    """Avatar settings cannot map safely to a static public asset."""


class AvatarSet:
    """Resolve message types to the versioned, operator-hosted Phosphor bundle."""

    THEMES = frozenset(AVATAR_THEMES)
    PALETTES = frozenset(AVATAR_PALETTES)

    def __init__(
        self,
        base_url: str,
        *,
        theme: str = "duotone",
        palette: str = "colored",
    ):
        self._base_url = self._validated_base_url(base_url)
        if theme not in self.THEMES:
            raise AvatarConfigError(
                f"avatar theme {theme!r} is not supported; choose duotone, fill, or bold"
            )
        self.theme = theme
        if palette not in self.PALETTES:
            raise AvatarConfigError(
                f"avatar palette {palette!r} is not supported; choose colored, black, or white"
            )
        self.palette = palette

    @classmethod
    def _validated_base_url(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise AvatarConfigError("avatar_base_url must be a non-empty HTTPS URL")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise AvatarConfigError("avatar_base_url must not contain control characters")
        if any(character.isspace() for character in value):
            raise AvatarConfigError("avatar_base_url must not contain whitespace")
        if "\\" in value:
            raise AvatarConfigError("avatar_base_url must not contain a backslash")
        if "?" in value:
            raise AvatarConfigError("avatar_base_url must not contain a query")
        if "#" in value:
            raise AvatarConfigError("avatar_base_url must not contain a fragment")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise AvatarConfigError(f"avatar_base_url is not a valid HTTPS URL: {exc}") from exc
        if parsed.scheme != "https" or not parsed.hostname:
            raise AvatarConfigError("avatar_base_url must be an absolute HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise AvatarConfigError("avatar_base_url must not contain credentials")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise AvatarConfigError(f"avatar_base_url has an invalid port: {exc}") from exc
        return value.rstrip("/")

    def relative_path(
        self,
        message_type: str,
        *,
        theme: str | None = None,
        palette: str | None = None,
    ) -> str:
        key = self.resolve_message_type(message_type)
        selected_theme = theme or self.theme
        if selected_theme not in self.THEMES:
            raise AvatarConfigError(f"unsupported avatar theme {selected_theme!r}")
        selected_palette = palette or self.palette
        if selected_palette not in self.PALETTES:
            raise AvatarConfigError(f"unsupported avatar palette {selected_palette!r}")
        return AVATAR_MANIFEST["path_template"].format(
            version=AVATAR_VERSION,
            theme=selected_theme,
            palette=selected_palette,
            size=AVATAR_SIZE,
            message_type=key,
        )

    @staticmethod
    def resolve_message_type(message_type: str) -> str:
        return message_type if message_type in AVATAR_MESSAGE_TYPES else "default"

    def url_for(self, message_type: str) -> str:
        return f"{self._base_url}/{self.relative_path(message_type)}"
