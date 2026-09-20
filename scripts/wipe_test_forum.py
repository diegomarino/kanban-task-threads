#!/usr/bin/env python3
"""Delete every thread in the TEST forum, leaving it clean for real work.

Development tooling only. Three guardrails:
- requires the bot token (webhook cannot delete) and the explicit `--yes` flag,
- resolves the forum from the webhook in `.env.local` (never a typed id),
- refuses outright unless the forum's name contains "test".
"""

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from live_probe import webhook_url_from_env_local  # noqa: E402
from live_run import bot_token_from_env_local  # noqa: E402

from kanban_task_threads.transport import urllib_http  # noqa: E402

API = "https://discord.com/api/v10"


def main() -> int:
    if "--yes" not in sys.argv:
        print("wipe_test_forum: refusing without --yes (this deletes every thread)")
        return 2
    token = bot_token_from_env_local()
    if not token:
        print("wipe_test_forum: needs KANBAN_TASK_THREADS_BOT_TOKEN in .env.local")
        return 2
    auth = {"Authorization": f"Bot {token}"}

    status, hook = urllib_http("GET", webhook_url_from_env_local(), None)
    assert status == 200, hook
    forum_id, guild_id = str(hook["channel_id"]), str(hook["guild_id"])

    status, forum = urllib_http("GET", f"{API}/channels/{forum_id}", None, headers=auth)
    assert status == 200, forum
    if "test" not in forum.get("name", "").lower():
        print(f"wipe_test_forum: forum is named {forum.get('name')!r} — not a test forum, refusing")
        return 1

    threads = []
    status, active = urllib_http(
        "GET", f"{API}/guilds/{guild_id}/threads/active", None, headers=auth
    )
    if status == 200:
        threads += [t for t in active.get("threads", []) if str(t.get("parent_id")) == forum_id]
    status, archived = urllib_http(
        "GET", f"{API}/channels/{forum_id}/threads/archived/public?limit=100", None, headers=auth
    )
    if status == 200:
        threads += archived.get("threads", [])

    print(f"deleting {len(threads)} threads from #{forum['name']}")
    for thread in threads:
        status, body = urllib_http("DELETE", f"{API}/channels/{thread['id']}", None, headers=auth)
        marker = "ok" if status in (200, 204) else f"HTTP {status} {body}"
        print(f"  {thread.get('name', thread['id'])}: {marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
