#!/usr/bin/env python3
"""Bounded live test: the REAL consumer against the sandbox board, publishing
to the throwaway test forum. Run, observe, stop — one consume pass per
invocation, then exit. This is NOT a runner and must not be looped against
production; the production path is a configuration change owned by Diego.

Webhook: `.env.local` (test forum; a webhook is bound to one channel, so this
cannot reach production). State: `.sandbox/live-state.db`, so reruns are
incremental exactly like the plugin would be.

Usage:  ./scripts/sandbox reset && ./scripts/sandbox up && ./scripts/sandbox task
        python3 scripts/live_run.py <board.db>   # (sandbox wires the path)
"""

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from live_probe import webhook_url_from_env_local  # noqa: E402

from kanban_task_threads.consumer import Consumer  # noqa: E402
from kanban_task_threads.store import StateStore  # noqa: E402
from kanban_task_threads.transport import DiscordTransport, urllib_http  # noqa: E402


def bot_token_from_env_local() -> str | None:
    for line in (REPO / ".env.local").read_text().splitlines():
        if line.strip().startswith("KANBAN_TASK_THREADS_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def main():
    board_db = sys.argv[1]
    url = webhook_url_from_env_local()
    bot_token = bot_token_from_env_local()
    transport = DiscordTransport(urllib_http, url, bot_token=bot_token)

    info = transport.webhook_info()  # the preflight the plugin runs
    # Mirror the production wiring: the discovered forum id feeds the rebuild
    # (the delta review's critical finding — tag ops need it).
    transport = DiscordTransport(
        urllib_http, url, bot_token=bot_token, forum_channel_id=str(info["channel_id"])
    )
    mode = "webhook+bot" if bot_token else "webhook-only"
    print(f"webhook   : {info.get('name')!r} -> channel {info.get('channel_id')} [{mode}]")

    state = StateStore(REPO / ".sandbox" / "live-state.db")
    consumer = Consumer(
        board_db,
        state,
        transport,
        board="default",
        holder="live-run",
        destination=f"discord:webhook:{info.get('id')}@{info.get('channel_id')}",
        guild_id=str(info.get("guild_id") or ""),
    )
    report = consumer.run_once()
    print(f"opened={report.opened} replies={report.replied} edited={report.edited}")
    for line in report.errors:
        print("ERROR  :", line)
    for line in report.warnings:
        print("WARNING:", line)
    guild = info.get("guild_id")
    if guild:
        for thread in report.opened:
            post = state.get_post("default", thread)
            print(f"see it  : https://discord.com/channels/{guild}/{post['thread_id']}")


if __name__ == "__main__":
    main()
