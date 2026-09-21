"""The ADR-0004 seam: four operations behind a capability declaration. ADR-0007 hard rules:
allowed_mentions {"parse": []} on every call, deterministic truncation."""

import pytest
from conftest import FakeHttp

from kanban_task_threads.render import Card
from kanban_task_threads.transport import (
    CAP_LIVE_TIMESTAMPS,
    CAP_PER_MESSAGE_IDENTITY,
    CAP_RICH_CARD,
    CAP_TAGS,
    CAP_TITLE_STATE,
    DiscordTransport,
    ForumTagSetupError,
    ThreadRef,
    TransportError,
)

WEBHOOK = "https://discord.com/api/webhooks/123/tok"

CARD = Card(
    title="fix the build", description="a card", color=0x3BA55D, summary="● running · coder"
)


def make(http=None, **kwargs):
    return DiscordTransport(http or FakeHttp(), WEBHOOK, **kwargs)


# --- capabilities -------------------------------------------------------------


def test_webhook_only_capabilities():
    t = make()
    assert t.capabilities() == frozenset(
        {CAP_RICH_CARD, CAP_LIVE_TIMESTAMPS, CAP_PER_MESSAGE_IDENTITY}
    )


def test_bot_token_adds_title_state_and_tags():
    t = make(bot_token="Bot xyz")
    assert {CAP_TITLE_STATE, CAP_TAGS} <= t.capabilities()


# --- open_thread --------------------------------------------------------------


def test_open_thread_posts_forum_post_and_returns_ref():
    http = FakeHttp()
    http.queue(200, {"id": "900", "channel_id": "901"})
    ref = make(http).open_thread(title="fix the build", card=CARD)
    method, url, body = http.calls[0]
    assert method == "POST"
    assert url == WEBHOOK + "?wait=true"
    assert body["thread_name"] == "fix the build"
    assert body["embeds"][0]["description"] == "a card"
    assert ref == ThreadRef(thread_id="901", message_id="900")


def test_card_messages_carry_the_summary_as_content():
    http = FakeHttp()
    t = make(http)
    ref = t.open_thread(title="t", card=CARD)
    t.edit_card(ref, CARD)
    for _, _, body in http.calls:
        assert body["content"] == "● running · coder"


def test_open_thread_truncates_thread_name_to_100():
    http = FakeHttp()
    make(http).open_thread(title="t" * 300, card=CARD)
    assert len(http.calls[0][2]["thread_name"]) == 100


def test_open_thread_sends_applied_tags_when_configured():
    http = FakeHttp()
    make(http, applied_tag_ids=("55",)).open_thread(title="t", card=CARD)
    assert http.calls[0][2]["applied_tags"] == ["55"]


# --- edit_card ----------------------------------------------------------------


def test_edit_card_patches_starter_message_in_thread():
    http = FakeHttp()
    make(http).edit_card(ThreadRef("901", "900"), CARD)
    method, url, body = http.calls[0]
    assert method == "PATCH"
    assert url == WEBHOOK + "/messages/900?thread_id=901"
    assert body["embeds"][0]["title"] == "fix the build"


def test_embed_description_truncated_to_4096():
    http = FakeHttp()
    big = Card(title="t", description="d" * 10000, color=1)
    make(http).edit_card(ThreadRef("901", "900"), big)
    assert len(http.calls[0][2]["embeds"][0]["description"]) == 4096


# --- append -------------------------------------------------------------------


def test_append_posts_reply_into_thread_signed_per_message():
    http = FakeHttp()
    http.queue(200, {"id": "902", "channel_id": "901"})
    mid = make(http).append(ThreadRef("901", "900"), content="blocked: waiting", username="MarSan")
    method, url, body = http.calls[0]
    assert method == "POST"
    assert url == WEBHOOK + "?wait=true&thread_id=901"
    assert body["content"] == "blocked: waiting"
    assert body["username"] == "MarSan"
    assert mid == "902"


def test_append_truncates_content_to_2000():
    http = FakeHttp()
    make(http).append(ThreadRef("901", "900"), content="c" * 5000)
    assert len(http.calls[0][2]["content"]) == 2000


# --- the rule that is never optional ------------------------------------------


def test_every_call_carries_empty_allowed_mentions():
    http = FakeHttp()
    t = make(http)
    ref = t.open_thread(title="@everyone hi", card=CARD)
    t.edit_card(ref, CARD)
    t.append(ref, content="@here reply")
    assert len(http.calls) == 3
    for _, _, body in http.calls:
        assert body["allowed_mentions"] == {"parse": []}


# --- preflight ------------------------------------------------------------


def test_webhook_info_asks_the_webhook_where_it_posts():
    # ADR-0003: one credential, one source of truth — the channel id is discovered
    # via GET on the webhook itself, never configured.
    http = FakeHttp()
    http.queue(200, {"id": "123", "name": "example-forum", "channel_id": "777", "guild_id": "9"})
    info = make(http).webhook_info()
    method, url, _ = http.calls[0]
    assert (method, url) == ("GET", WEBHOOK)
    assert info["channel_id"] == "777"


def test_forum_requires_tag_is_unknown_without_bot_token():
    http = FakeHttp()
    assert make(http).forum_requires_tag("777") is None
    assert http.calls == []  # no bot token, no channel API


def test_forum_requires_tag_reads_channel_flags_with_bot_token():
    http = FakeHttp()
    http.queue(200, {"id": "777", "flags": 16})
    t = make(http, bot_token="x")
    assert t.forum_requires_tag("777") is True
    method, url, _ = http.calls[0]
    assert url == "https://discord.com/api/v10/channels/777"
    assert http.headers_seen[0]["Authorization"] == "Bot x"


def test_forum_without_flag_16_does_not_require_tags():
    http = FakeHttp()
    http.queue(200, {"id": "777", "flags": 0})
    assert make(http, bot_token="x").forum_requires_tag("777") is False


def test_prepare_forum_creates_only_missing_managed_tags_and_caches_response():
    http = FakeHttp()
    existing = [
        {
            "id": "custom-1",
            "name": "customer",
            "moderated": False,
            "emoji_id": None,
            "emoji_name": "✨",
        },
        {
            "id": "tag-1",
            "name": "triage",
            "moderated": False,
            "emoji_id": None,
            "emoji_name": None,
        },
    ]
    http.queue(200, {"id": "777", "flags": 0, "available_tags": existing})
    created_names = [
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
    ]
    resulting_tags = existing + [
        {"id": f"new-{index}", "name": name} for index, name in enumerate(created_names, start=1)
    ]
    http.queue(200, {"id": "777", "flags": 0, "available_tags": resulting_tags})

    transport = bot(http)
    assert transport.prepare_forum() is False

    assert http.calls == [
        ("GET", "https://discord.com/api/v10/channels/777", None),
        (
            "PATCH",
            "https://discord.com/api/v10/channels/777",
            {"available_tags": existing + [{"name": name} for name in created_names]},
        ),
    ]
    assert transport.status_tag_id("done") == "new-8"
    assert len(http.calls) == 2


def test_prepare_forum_does_not_patch_when_all_managed_tags_exist():
    http = FakeHttp()
    names = [
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
    ]
    http.queue(
        200,
        {
            "id": "777",
            "flags": 16,
            "available_tags": [
                {"id": f"tag-{index}", "name": name} for index, name in enumerate(names, start=1)
            ],
        },
    )

    assert bot(http).prepare_forum() is True
    assert [call[0] for call in http.calls] == ["GET"]


def test_prepare_forum_fails_before_patch_when_managed_tags_exceed_discord_limit():
    http = FakeHttp()
    http.queue(
        200,
        {
            "id": "777",
            "flags": 0,
            "available_tags": [
                {"id": f"custom-{index}", "name": f"custom-{index}"} for index in range(10)
            ],
        },
    )

    with pytest.raises(ForumTagSetupError, match="20"):
        bot(http).prepare_forum()
    assert [call[0] for call in http.calls] == ["GET"]


def test_prepare_forum_turns_missing_manage_channels_into_actionable_error():
    http = FakeHttp()
    http.queue(200, {"id": "777", "flags": 0, "available_tags": []})
    http.queue(403, {"message": "Missing Permissions", "code": 50013})

    with pytest.raises(ForumTagSetupError, match="Manage Channels"):
        bot(http).prepare_forum()


def test_prepare_forum_turns_missing_forum_access_into_actionable_error():
    http = FakeHttp()
    http.queue(403, {"message": "Missing Access", "code": 50001})

    with pytest.raises(ForumTagSetupError, match="View Channel"):
        bot(http).prepare_forum()


def test_required_forum_uses_managed_triage_tag_for_creation_when_no_default_is_set():
    http = FakeHttp()
    http.queue(
        200,
        {
            "id": "777",
            "flags": 16,
            "available_tags": [{"id": "triage-id", "name": "triage"}],
        },
    )
    http.queue(
        200,
        {
            "id": "777",
            "flags": 16,
            "available_tags": [
                {"id": "triage-id", "name": "triage"},
                {"id": "todo-id", "name": "todo"},
                {"id": "scheduled-id", "name": "scheduled"},
                {"id": "ready-id", "name": "ready"},
                {"id": "running-id", "name": "running"},
                {"id": "blocked-id", "name": "blocked"},
                {"id": "needs-human-id", "name": "needs-human"},
                {"id": "review-id", "name": "review"},
                {"id": "done-id", "name": "done"},
                {"id": "archived-id", "name": "archived"},
                {"id": "failed-id", "name": "failed"},
            ],
        },
    )
    http.queue(200, {"id": "900", "channel_id": "901"})
    transport = bot(http)

    assert transport.prepare_forum() is True
    transport.open_thread(title="fix", card=CARD)

    assert http.calls[-1][2]["applied_tags"] == ["triage-id"]


# --- bot-token extras (ADR-0003): title state, tags, archiving --------------------
# All capability-gated: the webhook cannot do any of this.

FORUM_TAGS = {
    "id": "777",
    "available_tags": [
        {"id": "1", "name": "running"},
        {"id": "2", "name": "needs-human"},
        {"id": "3", "name": "blocked"},
        {"id": "4", "name": "review"},
        {"id": "5", "name": "done"},
        {"id": "6", "name": "failed"},
    ],
}


def bot(http):
    return DiscordTransport(http, WEBHOOK, bot_token="x", forum_channel_id="777")


def test_set_status_tag_resolves_the_name_against_the_forum():
    http = FakeHttp()
    http.queue(200, FORUM_TAGS)  # GET the forum, once (cached)
    http.queue(200, {})  # PATCH the thread
    t = bot(http)
    assert t.set_status_tag(ThreadRef("901", "900"), "needs-human") is True
    method, url, body = http.calls[1]
    assert (method, url) == ("PATCH", "https://discord.com/api/v10/channels/901")
    assert body == {"applied_tags": ["2"]}
    assert http.headers_seen[1]["Authorization"] == "Bot x"


def test_forum_tags_are_fetched_once_then_cached():
    http = FakeHttp()
    http.queue(200, FORUM_TAGS)
    http.queue(200, {})
    http.queue(200, {})
    t = bot(http)
    t.set_status_tag(ThreadRef("901", "900"), "running")
    t.set_status_tag(ThreadRef("902", "902"), "done")
    gets = [c for c in http.calls if c[0] == "GET"]
    assert len(gets) == 1


def test_unknown_tag_name_is_a_no_op_not_an_error():
    http = FakeHttp()
    http.queue(200, FORUM_TAGS)
    t = bot(http)
    assert t.set_status_tag(ThreadRef("901", "900"), "no-such-tag") is False
    assert [c for c in http.calls if c[0] == "PATCH"] == []


def test_rename_patches_the_thread_name():
    http = FakeHttp()
    bot(http).rename(ThreadRef("901", "900"), "new name · t_1")
    method, url, body = http.calls[0]
    assert (method, url) == ("PATCH", "https://discord.com/api/v10/channels/901")
    assert body == {"name": "new name · t_1"}


def test_set_archived_patches_the_thread():
    http = FakeHttp()
    bot(http).set_archived(ThreadRef("901", "900"), True)
    assert http.calls[0][2] == {"archived": True}


def test_bulk_thread_listing_parses_and_filters_active_and_latest_archived():
    http = FakeHttp()
    http.queue(
        200,
        {
            "threads": [
                {
                    "id": "active-here",
                    "parent_id": "777",
                    "applied_tags": ["todo"],
                    "thread_metadata": {"archived": False},
                },
                {
                    "id": "active-elsewhere",
                    "parent_id": "888",
                    "applied_tags": ["other"],
                    "thread_metadata": {"archived": False},
                },
            ]
        },
    )
    http.queue(
        200,
        {
            "threads": [
                {
                    "id": "archived-here",
                    "parent_id": "777",
                    "applied_tags": ["done"],
                    "thread_metadata": {"archived": True},
                }
            ]
        },
    )

    threads = bot(http).list_forum_threads("guild-9")

    assert threads == {
        "active-here": {"applied_tags": ("todo",), "archived": False},
        "archived-here": {"applied_tags": ("done",), "archived": True},
    }
    assert [(method, url) for method, url, _ in http.calls] == [
        ("GET", "https://discord.com/api/v10/guilds/guild-9/threads/active"),
        ("GET", "https://discord.com/api/v10/channels/777/threads/archived/public?limit=25"),
    ]


def test_bot_patch_returns_thread_metadata_as_authoritative_readback():
    http = FakeHttp()
    http.queue(
        200,
        {
            "id": "901",
            "applied_tags": ["5"],
            "thread_metadata": {"archived": True},
        },
    )
    assert bot(http).set_archived(ThreadRef("901", "900"), True) == {
        "applied_tags": ("5",),
        "archived": True,
    }


def test_bot_operations_refuse_without_a_token():
    import pytest as _pytest

    t = make(FakeHttp())  # webhook-only
    for call in (
        lambda: t.set_status_tag(ThreadRef("9", "9"), "done"),
        lambda: t.rename(ThreadRef("9", "9"), "x"),
        lambda: t.set_archived(ThreadRef("9", "9"), True),
    ):
        with _pytest.raises(RuntimeError):
            call()


def test_card_messages_are_never_signed():
    # ADR-0008: a webhook username is fixed at creation and unPATCHable, and the card
    # is rewritten for life — so the card is the board speaking, never a person.
    # Pinned so nobody "fixes" it by adding username to the create.
    http = FakeHttp()
    t = make(http)
    ref = t.open_thread(title="t", card=CARD)
    t.edit_card(ref, CARD)
    for _, _, body in http.calls:
        assert "username" not in body


# --- errors -------------------------------------------------------------------


def test_non_2xx_raises_transport_error_with_status():
    http = FakeHttp()
    http.queue(400, {"code": 220001, "message": "tag required"})
    with pytest.raises(TransportError) as exc:
        make(http).open_thread(title="t", card=CARD)
    assert exc.value.status == 400
