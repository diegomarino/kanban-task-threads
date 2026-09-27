import pytest

from kanban_task_threads.routes import (
    ROUTES_UNSET,
    BoardRoute,
    RouteConfigError,
    normalize_routes,
)


def test_absent_routes_preserve_the_legacy_destination():
    assert normalize_routes(ROUTES_UNSET) == (BoardRoute(None, "KANBAN_TASK_THREADS_WEBHOOK_URL"),)


def test_absent_routes_keep_a_configured_legacy_board():
    assert normalize_routes(ROUTES_UNSET, legacy_board="fleet")[0].board == "fleet"


def test_explicitly_empty_routes_publish_nothing():
    assert normalize_routes([]) == ()


def test_explicit_route_binds_one_board_to_one_named_secret():
    assert normalize_routes(
        [{"selector": {"board": "fleet"}, "webhook_secret": "FLEET_WEBHOOK"}]
    ) == (BoardRoute("fleet", "FLEET_WEBHOOK"),)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        "fleet",
        ["fleet"],
        [{"selector": "fleet", "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "fleet"}}],
        [{"webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": ""}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": 1}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "fleet"}, "webhook_secret": ""}],
        [{"selector": {"board": "fleet"}, "webhook_secret": 1}],
        [{"selector": {"board": "fleet"}, "webhook_secret": "fleet_webhook"}],
        [{"selector": {"board": "fleet"}, "webhook_secret": "https://discord.com/api"}],
        [{"selector": {"board": "fleet"}, "webhook_secret": " FLEET_WEBHOOK"}],
        [{"selector": {"board": " fleet"}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "fleet "}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "Fleet"}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "fleet/web"}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "."}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": ".."}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "../fleet"}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"board": "fleet"}, "webhook_secret": "FLEET_WEBHOOK", "extra": True}],
        [{"selector": {"board": "fleet", "project": "core"}, "webhook_secret": "FLEET_WEBHOOK"}],
        [{"selector": {"project": "core"}, "webhook_secret": "FLEET_WEBHOOK"}],
        [
            {"selector": {"board": "fleet"}, "webhook_secret": "FLEET_WEBHOOK"},
            {"selector": {"board": "fleet"}, "webhook_secret": "OTHER_WEBHOOK"},
        ],
    ],
)
def test_rejects_malformed_explicit_routes(raw):
    with pytest.raises(RouteConfigError):
        normalize_routes(raw)


@pytest.mark.parametrize("legacy_board", [1, " fleet", "fleet ", "Fleet", "fleet/web", ".", ".."])
def test_rejects_invalid_legacy_board_identifiers(legacy_board):
    with pytest.raises(RouteConfigError):
        normalize_routes(ROUTES_UNSET, legacy_board=legacy_board)
