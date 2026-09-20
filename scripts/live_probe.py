#!/usr/bin/env python3
"""One-shot live probe of the Discord path. Requires explicit authorization.

Exercises exactly the three operations the milestone names — open a real forum
post, edit its card in place, land one reply in the same thread — and exits.
This is NOT a runner: nothing loops, nothing consumes events.

The webhook URL comes from `.env.local` (gitignored; a disposable test-forum
webhook, deliberately not in the vault). The forum channel is never
configured: `GET /webhooks/{id}/{token}` returns the webhook object and its
`channel_id` is the forum it publishes to — one credential, one source of
truth (ADR-0003). A webhook is bound to one channel, so this probe cannot
reach production even by mistake.
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from kanban_task_threads.render import render_card, render_reply
from kanban_task_threads.transport import DiscordTransport, urllib_http
from kanban_task_threads.view import build_view

ENV_LOCAL = pathlib.Path(__file__).resolve().parents[1] / ".env.local"


def webhook_url_from_env_local() -> str:
    if not ENV_LOCAL.exists():
        sys.exit("live_probe: no .env.local at the repo root")
    candidates = {}
    for line in ENV_LOCAL.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        candidates[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("KANBAN_TASK_THREADS_WEBHOOK_URL",):
        if candidates.get(key):
            return candidates[key]
    webhookish = [v for k, v in candidates.items() if "WEBHOOK" in k.upper()]
    if len(webhookish) == 1:
        return webhookish[0]
    sys.exit(
        f"live_probe: could not pick a webhook URL from .env.local (keys: {sorted(candidates)})"
    )


def main():
    url = webhook_url_from_env_local()

    # One credential, one source of truth: ask the webhook where it posts.
    status, hook = urllib_http("GET", url, None)
    if status != 200:
        sys.exit(f"live_probe: GET webhook failed: HTTP {status} {hook!r}")
    print(f"webhook   : {hook.get('name')!r}")
    print(f"channel_id: {hook.get('channel_id')}  (the forum, discovered not configured)")

    transport = DiscordTransport(urllib_http, url)
    now = int(time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    task = {
        "id": "t_probe",
        "title": f"live probe — {stamp}",
        "assignee": "sandbox",
        "status": "running",
        "created_at": now - 900,
        "started_at": now - 300,
        "workspace_kind": "scratch",
        "workspace_path": None,
        "branch_name": None,
        "last_heartbeat_at": now,
        "block_kind": None,
    }

    # 1 · a real post opens, card says running
    card = render_card(build_view(task, now=now))
    ref = transport.open_thread(title=task["title"], card=card)
    print(f"opened    : thread {ref.thread_id}, starter message {ref.message_id}")

    # 2 · the card is rewritten in place: running -> blocked, with the reason
    task["status"], task["block_kind"] = "blocked", "needs_input"
    blocked_card = render_card(
        build_view(task, now=now, block_reason="waiting on a decision (live probe)")
    )
    transport.edit_card(ref, blocked_card)
    print("edited    : same starter message now shows blocked + reason")

    # 3 · one reply lands in the same thread, signed per message
    reply = render_reply(
        "blocked", {"reason": "waiting on a decision (live probe)", "kind": "needs_input"}
    )
    message_id = transport.append(ref, content=reply, username="sandbox")
    print(f"replied   : message {message_id} in thread {ref.thread_id}")

    guild = hook.get("guild_id")
    if guild:
        print(f"see it    : https://discord.com/channels/{guild}/{ref.thread_id}")


if __name__ == "__main__":
    main()
