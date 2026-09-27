"""Pure validation for board-to-webhook-secret publication routes."""

import re
from collections.abc import Mapping
from dataclasses import dataclass


class RouteConfigError(ValueError):
    """Raised when the publication route configuration is structurally invalid."""


@dataclass(frozen=True)
class BoardRoute:
    """A single board destination, with ``None`` reserved for the legacy board."""

    board: str | None
    webhook_secret: str


ROUTES_UNSET = object()

_BOARD_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SECRET_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_LEGACY_WEBHOOK_SECRET = "KANBAN_TASK_THREADS_WEBHOOK_URL"


def normalize_routes(raw: object, *, legacy_board: object = None) -> tuple[BoardRoute, ...]:
    """Validate configured routes without resolving secrets or touching Hermes state."""
    if raw is ROUTES_UNSET:
        return (BoardRoute(_normalize_legacy_board(legacy_board), _LEGACY_WEBHOOK_SECRET),)
    if not isinstance(raw, list):
        raise RouteConfigError("routes must be a list when configured")

    routes = []
    boards = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise RouteConfigError("each route must be a mapping")
        _require_exact_keys(entry, {"selector", "webhook_secret"}, "route")

        selector = entry["selector"]
        if not isinstance(selector, Mapping):
            raise RouteConfigError("route selector must be a mapping")
        _require_exact_keys(selector, {"board"}, "route selector")
        board = _validate_board(selector["board"], "route selector board")
        if board in boards:
            raise RouteConfigError(f"duplicate board route: {board}")
        boards.add(board)

        routes.append(BoardRoute(board, _validate_secret_name(entry["webhook_secret"])))
    return tuple(routes)


def _require_exact_keys(value: Mapping[object, object], expected: set[str], label: str) -> None:
    keys = set(value)
    if keys != expected:
        missing = expected - keys
        extra = keys - expected
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unsupported {', '.join(sorted(str(key) for key in extra))}")
        required = ", ".join(sorted(expected))
        raise RouteConfigError(f"{label} must contain exactly {required}: {'; '.join(details)}")


def _normalize_legacy_board(value: object) -> str | None:
    if value is None or value == "":
        return None
    return _validate_board(value, "legacy board")


def _validate_board(value: object, label: str) -> str:
    if not isinstance(value, str) or not _BOARD_SLUG_RE.fullmatch(value):
        raise RouteConfigError(
            f"{label} must be a 1-64 character lowercase board slug using letters, "
            "numbers, '-' or '_'"
        )
    return value


def _validate_secret_name(value: object) -> str:
    if not isinstance(value, str) or not _SECRET_NAME_RE.fullmatch(value):
        raise RouteConfigError("webhook_secret must be an environment-style secret name")
    return value
