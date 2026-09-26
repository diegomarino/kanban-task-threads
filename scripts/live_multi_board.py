#!/usr/bin/env python3
"""Run one real Discord publication pass for exactly two disposable boards.

This explicit acceptance probe is deliberately narrower than the plugin
runtime.  It only accepts existing SQLite boards below ``.sandbox/``, loads
named webhook values from the repository's ignored ``.env.local``, preflights
both destinations, then gives each board its own consumer and state connection.
It can publish to Discord when invoked with genuine disposable credentials.
"""

from __future__ import annotations

import pathlib
import re
import sys
from dataclasses import dataclass
from typing import TextIO

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.routes import RouteConfigError, normalize_routes
from kanban_task_threads.store import StateStore
from kanban_task_threads.transport import DiscordTransport, urllib_http

STATE_PATH = pathlib.Path(".sandbox/live-multi-board-state.db")
_URL = re.compile(r"https?://\S+")


class ProbeError(ValueError):
    """An operator-correctable preflight or invocation error."""


@dataclass(frozen=True)
class ProbeRoute:
    board: str
    board_db: pathlib.Path
    secret_name: str
    webhook_url: str
    channel_id: str = ""
    webhook_id: str = ""
    guild_id: str = ""


def _parse_env(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file():
        raise ProbeError("live-multi-board: missing repository-root .env.local")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def _parse_routes(argv: list[str], repo: pathlib.Path, env: dict[str, str]) -> list[ProbeRoute]:
    if len(argv) != 6:
        raise ProbeError(
            "usage: live_multi_board.py BOARD_A SANDBOX_DB_A WEBHOOK_SECRET_A "
            "BOARD_B SANDBOX_DB_B WEBHOOK_SECRET_B"
        )
    raw_routes = [
        {"selector": {"board": argv[0]}, "webhook_secret": argv[2]},
        {"selector": {"board": argv[3]}, "webhook_secret": argv[5]},
    ]
    try:
        board_routes = normalize_routes(raw_routes)
    except RouteConfigError as error:
        raise ProbeError(f"live-multi-board: invalid route: {error}") from error
    if len({route.webhook_secret for route in board_routes}) != 2:
        raise ProbeError("live-multi-board: duplicate webhook secret name")

    sandbox = (repo / ".sandbox").resolve()
    routes = []
    for route, raw_path in zip(board_routes, (argv[1], argv[4]), strict=True):
        supplied_path = pathlib.Path(raw_path)
        if supplied_path.is_absolute():
            board_path = supplied_path.resolve()
        else:
            board_path = (repo / supplied_path).resolve()
        if not board_path.is_relative_to(sandbox):
            raise ProbeError(
                f"live-multi-board: board database must be inside .sandbox: {raw_path}"
            )
        if not board_path.is_file():
            raise ProbeError(
                f"live-multi-board: board database is not an existing file: {raw_path}"
            )
        webhook_url = env.get(route.webhook_secret, "")
        if not webhook_url:
            raise ProbeError(f"live-multi-board: missing or empty secret {route.webhook_secret}")
        routes.append(
            ProbeRoute(
                board=route.board or "default",
                board_db=board_path,
                secret_name=route.webhook_secret,
                webhook_url=webhook_url,
            )
        )
    return routes


def _preflight(route: ProbeRoute, http) -> ProbeRoute:
    try:
        status, info = http("GET", route.webhook_url, None)
    except Exception as error:
        raise ProbeError(f"live-multi-board: webhook preflight failed for {route.board}") from error
    if status != 200 or not isinstance(info, dict):
        raise ProbeError(
            f"live-multi-board: webhook preflight rejected for {route.board} (HTTP {status})"
        )
    channel_id = str(info.get("channel_id") or "")
    if not channel_id:
        raise ProbeError(
            f"live-multi-board: webhook preflight has no forum destination for {route.board}"
        )
    return ProbeRoute(
        board=route.board,
        board_db=route.board_db,
        secret_name=route.secret_name,
        webhook_url=route.webhook_url,
        channel_id=channel_id,
        webhook_id=str(info.get("id") or ""),
        guild_id=str(info.get("guild_id") or ""),
    )


def _redact(value: object, secrets: list[str]) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return _URL.sub("[redacted-url]", text)


def _print_report(
    route: ProbeRoute, report, state: StateStore, stdout: TextIO, secrets: list[str]
) -> None:
    print(
        f"board={route.board} secret={route.secret_name} channel={route.channel_id} "
        f"opened={len(report.opened)} replied={report.replied} edited={len(report.edited)}",
        file=stdout,
    )
    for error in report.errors:
        print(f"ERROR board={route.board}: {_redact(error, secrets)}", file=stdout)
    for warning in report.warnings:
        print(f"WARNING board={route.board}: {_redact(warning, secrets)}", file=stdout)
    if route.guild_id:
        for task_id in report.opened:
            post = state.get_post(route.board, task_id)
            if post and post.get("thread_id"):
                print(
                    f"thread=https://discord.com/channels/{route.guild_id}/{post['thread_id']}",
                    file=stdout,
                )


def main(
    argv: list[str] | None = None,
    *,
    repo: pathlib.Path = REPO,
    http=None,
    transport_cls=DiscordTransport,
    consumer_cls=Consumer,
    state_store_cls=StateStore,
    stdout: TextIO | None = None,
) -> int:
    """Execute the bounded probe; dependency parameters keep offline tests local."""
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = pathlib.Path(repo).resolve()
    http = urllib_http if http is None else http
    stdout = sys.stdout if stdout is None else stdout
    try:
        env = _parse_env(repo / ".env.local")
        routes = _parse_routes(argv, repo, env)
        routes = [_preflight(route, http) for route in routes]
        if len({route.channel_id for route in routes}) != 2:
            raise ProbeError("live-multi-board: duplicate discovered forum destination")
    except ProbeError as error:
        print(str(error), file=stdout)
        return 2

    bot_token = env.get("KANBAN_TASK_THREADS_BOT_TOKEN") or None
    secrets = [route.webhook_url for route in routes]
    if bot_token:
        secrets.append(bot_token)
    consumers: list[tuple[ProbeRoute, object, object]] = []
    try:
        for route in routes:
            state = state_store_cls(repo / STATE_PATH)
            try:
                transport = transport_cls(
                    http,
                    route.webhook_url,
                    bot_token=bot_token,
                    forum_channel_id=route.channel_id,
                )
                consumer = consumer_cls(
                    route.board_db,
                    state,
                    transport,
                    board=route.board,
                    holder=f"live-multi-board-{route.board}",
                    destination=f"discord:webhook:{route.webhook_id}@{route.channel_id}",
                    guild_id=route.guild_id,
                )
            except Exception:
                state.close()
                raise
            consumers.append((route, consumer, state))
    except Exception:
        print("ERROR: consumer build failed", file=stdout)
        return 2

    failed = False
    try:
        for route, consumer, state in consumers:
            try:
                report = consumer.run_once()
            except Exception:
                failed = True
                print(f"ERROR board={route.board}: route pass failed", file=stdout)
                continue
            _print_report(route, report, state, stdout, secrets)
            failed = failed or bool(report.errors)
    finally:
        for _route, consumer, _state in consumers:
            consumer.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
