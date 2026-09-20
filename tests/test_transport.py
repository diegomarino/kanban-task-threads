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
    t = make(http, bot_token="botsecret")
    assert t.forum_requires_tag("777") is True
    method, url, _ = http.calls[0]
    assert url == "https://discord.com/api/v10/channels/777"
    assert http.headers_seen[0]["Authorization"] == "Bot botsecret"


def test_forum_without_flag_16_does_not_require_tags():
    http = FakeHttp()
    http.queue(200, {"id": "777", "flags": 0})
    assert make(http, bot_token="botsecret").forum_requires_tag("777") is False


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
    return DiscordTransport(http, WEBHOOK, bot_token="botsecret", forum_channel_id="777")


def test_set_status_tag_resolves_the_name_against_the_forum():
    http = FakeHttp()
    http.queue(200, FORUM_TAGS)  # GET the forum, once (cached)
    http.queue(200, {})  # PATCH the thread
    t = bot(http)
    assert t.set_status_tag(ThreadRef("901", "900"), "needs-human") is True
    method, url, body = http.calls[1]
    assert (method, url) == ("PATCH", "https://discord.com/api/v10/channels/901")
    assert body == {"applied_tags": ["2"]}
    assert http.headers_seen[1]["Authorization"] == "Bot botsecret"


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
