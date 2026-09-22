import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import discord_demo

WEBHOOK = "https://discord.com/api/webhooks/123/secret-token"
CHANNEL = "123456789012345678"
BOT_TOKEN = "demo-bot-secret"
MANAGED_TAGS = (
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "needs-human",
    "review",
    "done",
    "archived",
    "failed",
)
TAG_EMOJIS = dict(
    zip(
        MANAGED_TAGS,
        ("🔎", "📋", "📅", "🟢", "🏃", "⛔", "🙋", "👀", "✅", "📦", "❌"),
        strict=True,
    )
)


class FakeHttp:
    def __init__(self, channel_id=CHANNEL, available_tags=MANAGED_TAGS):
        self.channel_id = channel_id
        self.available_tags = [
            {
                "id": f"tag-{index}",
                "name": name,
                "moderated": False,
                "emoji_id": None,
                "emoji_name": TAG_EMOJIS[name],
            }
            for index, name in enumerate(available_tags, start=1)
        ]
        self.calls = []
        self.next_id = 100

    def __call__(self, method, url, body, headers=None):
        self.calls.append((method, url, body, headers))
        if method == "GET" and url == WEBHOOK:
            return 200, {"id": "123", "channel_id": self.channel_id, "guild_id": "guild-1"}
        if method == "GET" and url.endswith(f"/channels/{self.channel_id}"):
            return 200, {
                "id": self.channel_id,
                "type": 15,
                "flags": 0,
                "available_tags": self.available_tags,
            }
        if method == "PATCH" and url.endswith(f"/channels/{self.channel_id}"):
            self.available_tags = [
                {
                    **tag,
                    "id": tag.get("id") or f"tag-{index}",
                    "moderated": tag.get("moderated", False),
                    "emoji_id": tag.get("emoji_id"),
                    "emoji_name": tag.get("emoji_name"),
                }
                for index, tag in enumerate(body["available_tags"], start=1)
            ]
            return 200, {
                "id": self.channel_id,
                "type": 15,
                "flags": 0,
                "available_tags": self.available_tags,
            }
        self.next_id += 1
        if method == "POST" and body and "thread_name" in body:
            return 200, {"id": str(self.next_id), "channel_id": f"thread-{self.next_id}"}
        return 200, {"id": str(self.next_id)}


def test_scenario_is_fixed_generic_and_bounded():
    posts = discord_demo.scenario(anchor=1_700_000_000)

    assert [post.key for post in posts] == ["running", "blocked", "review", "done"]
    assert sum(len(post.replies) for post in posts) == 9
    assert sum(post.edit_after_publish for post in posts) == 1

    thread_names = [discord_demo.demo_thread_name(post) for post in posts]
    assert thread_names == [
        "Draft checklist",
        "Confirm criteria",
        "Review retry policy",
        "Publish release",
    ]

    messages = [
        discord_demo.render_reply(reply.kind, reply.payload, template=reply.template)
        for post in posts
        for reply in post.replies
    ]
    assert min(map(len, messages)) >= 38
    assert all("commented" not in message or ":" in message for message in messages)

    encoded = repr(posts).lower()
    for forbidden in ("marsan", "coder", "stripe", "grafana", "opentelemetry", "/users/"):
        assert forbidden not in encoded


def test_channel_mismatch_performs_one_read_and_no_mutations(tmp_path):
    http = FakeHttp(channel_id="wrong-channel")

    with pytest.raises(discord_demo.DemoRefusal, match="channel mismatch"):
        discord_demo.publish(
            http=http,
            webhook_url=WEBHOOK,
            bot_token=BOT_TOKEN,
            expected_channel_id=CHANNEL,
            receipt_path=tmp_path / "receipt.json",
            avatar_base_url="https://assets.example.test/kanban-task-threads",
            anchor=1_700_000_000,
        )

    assert [call[0] for call in http.calls] == ["GET"]
    assert not (tmp_path / "receipt.json").exists()


def test_publish_has_exact_bound_and_never_persists_secret(tmp_path):
    http = FakeHttp()
    receipt = tmp_path / "receipt.json"

    result = discord_demo.publish(
        http=http,
        webhook_url=WEBHOOK,
        bot_token=BOT_TOKEN,
        expected_channel_id=CHANNEL,
        receipt_path=receipt,
        avatar_base_url="https://assets.example.test/kanban-task-threads",
        anchor=1_700_000_000,
    )

    methods = [call[0] for call in http.calls]
    assert methods.count("GET") == 2
    assert methods.count("POST") == 13  # four starters and nine replies
    assert methods.count("PATCH") == 1
    assert result.mutations == 14
    assert len(result.thread_urls) == 4
    assert "secret-token" not in receipt.read_text()
    assert BOT_TOKEN not in receipt.read_text()
    assert json.loads(receipt.read_text())["channel_id"] == CHANNEL

    message_bodies = [
        body
        for method, url, body, _ in http.calls
        if method in {"POST", "PATCH"} and url.startswith(WEBHOOK)
    ]
    assert all(body["allowed_mentions"] == {"parse": []} for body in message_bodies)

    starters = [
        body for method, _, body, _ in http.calls if method == "POST" and "thread_name" in body
    ]
    assert [body["applied_tags"] for body in starters] == [
        ["tag-5"],
        ["tag-6"],
        ["tag-8"],
        ["tag-9"],
    ]


def test_publish_creates_missing_managed_tags_before_opening_posts(tmp_path):
    http = FakeHttp(available_tags=("running",))

    discord_demo.publish(
        http=http,
        webhook_url=WEBHOOK,
        bot_token=BOT_TOKEN,
        expected_channel_id=CHANNEL,
        receipt_path=tmp_path / "receipt.json",
        avatar_base_url="https://assets.example.test/kanban-task-threads",
        anchor=1_700_000_000,
    )

    forum_patches = [
        body
        for method, url, body, _ in http.calls
        if method == "PATCH" and url.endswith(f"/channels/{CHANNEL}")
    ]
    assert len(forum_patches) == 1
    names = [tag["name"] for tag in forum_patches[0]["available_tags"]]
    assert names[0] == "running"
    assert set(names) == set(MANAGED_TAGS)


def test_confirmed_receipt_makes_republish_a_noop(tmp_path):
    receipt = tmp_path / "receipt.json"
    first = FakeHttp()
    discord_demo.publish(
        http=first,
        webhook_url=WEBHOOK,
        bot_token=BOT_TOKEN,
        expected_channel_id=CHANNEL,
        receipt_path=receipt,
        avatar_base_url="https://assets.example.test/kanban-task-threads",
        anchor=1_700_000_000,
    )

    second = FakeHttp()
    result = discord_demo.publish(
        http=second,
        webhook_url=WEBHOOK,
        bot_token=BOT_TOKEN,
        expected_channel_id=CHANNEL,
        receipt_path=receipt,
        avatar_base_url="https://assets.example.test/kanban-task-threads",
        anchor=1_700_000_999,
    )

    assert [call[0] for call in second.calls] == ["GET"]
    assert result.mutations == 0
    assert result.already_complete is True


def test_concurrent_publishers_share_one_atomic_run_claim(tmp_path):
    barrier = threading.Barrier(2)

    class BarrierHttp(FakeHttp):
        def __call__(self, method, url, body, headers=None):
            if method == "GET" and url == WEBHOOK:
                barrier.wait(timeout=5)
            return super().__call__(method, url, body, headers)

    http = BarrierHttp()
    receipt = tmp_path / "receipt.json"

    def attempt():
        try:
            result = discord_demo.publish(
                http=http,
                webhook_url=WEBHOOK,
                bot_token=BOT_TOKEN,
                expected_channel_id=CHANNEL,
                receipt_path=receipt,
                avatar_base_url="https://assets.example.test/kanban-task-threads",
                anchor=1_700_000_000,
            )
            return "published", result.mutations
        except discord_demo.DemoRefusal as error:
            return "refused", str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(2)))

    assert sorted(outcome[0] for outcome in outcomes) == ["published", "refused"]
    assert [outcome[1] for outcome in outcomes if outcome[0] == "published"] == [14]
    assert sum(method == "POST" for method, *_ in http.calls) == 13
