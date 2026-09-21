"""ADR-0001: the card is a pure function of board state; the status vocabulary covers
the whole state machine (nine statuses, four block kinds, plus stale).
Truncation is deterministic at Discord's limits (ADR-0007)."""

from kanban_task_threads.render import (
    CONTENT_LIMIT,
    DESCRIPTION_LIMIT,
    STATUS_META,
    THREAD_TITLE_LIMIT,
    render_card,
    render_thread_name,
    reply_username,
    resolve_status_key,
    truncate,
)

BASE_VIEW = {
    "title": "fix the build",
    "status": "running",
    "assignee": "coder",
    "since_rel": "<t:1758190000:R>",
    "block_kind": "",
    "block_reason": "",
    "branch": "main",
    "workspace_kind": "worktree",
    "url": "https://dash.example/t_ab12",
    "unlocks": "",
}


def view(**overrides):
    v = dict(BASE_VIEW)
    v.update(overrides)
    return v


# --- truncation ---------------------------------------------------------------


def test_truncate_leaves_short_text_alone():
    assert truncate("hola", 100) == "hola"


def test_truncate_cuts_to_exact_limit_with_ellipsis():
    out = truncate("x" * 5000, DESCRIPTION_LIMIT)
    assert len(out) == DESCRIPTION_LIMIT
    assert out.endswith("…")


def test_limits_are_discords():
    assert (CONTENT_LIMIT, DESCRIPTION_LIMIT, THREAD_TITLE_LIMIT) == (2000, 4096, 100)


# --- thread name ----------------------------------------------------------------
# Frozen at creation (a webhook can never rename), so it carries the one stable
# key: the task id. Reconciliation and duplicate titles depend on it.


def test_thread_name_carries_the_task_id():
    assert render_thread_name("fix the build", "t_ab12") == "fix the build · t_ab12"


def test_thread_name_truncation_preserves_the_id():
    name = render_thread_name("t" * 300, "t_ab12")
    assert len(name) == THREAD_TITLE_LIMIT
    assert name.endswith(" · t_ab12")


def test_thread_name_without_id_is_just_the_title():
    assert render_thread_name("fix the build", "") == "fix the build"


def test_thread_name_survives_an_absurdly_long_task_id():
    # upstream bounds ids, but the invariant must hold locally: never > 100
    name = render_thread_name("title", "t_" + "x" * 300)
    assert 0 < len(name) <= THREAD_TITLE_LIMIT


# --- reply signature ------------------------------------------------------------
# The replies are "who did what": named actor > worker action (assignee) >
# system observation (None -> the webhook's institutional voice).


def test_commented_signs_as_the_author():
    assert reply_username("commented", {"author": "reviewer"}, "coder") == "reviewer"


def test_worker_actions_sign_as_the_assignee():
    for kind in ("blocked", "unblocked", "completed"):
        assert reply_username(kind, {}, "coder") == "coder"


def test_review_requested_signs_as_the_implementer():
    payload = {"implementer": "coder2", "reviewer": "marsan"}
    assert reply_username("review_requested", payload, "coder") == "coder2"


def test_changes_requested_signs_as_the_reviewer():
    payload = {"implementer": "coder", "reviewer": "marsan"}
    assert reply_username("changes_requested", payload, "coder") == "marsan"


def test_changes_requested_without_reviewer_is_the_board_not_the_assignee():
    # blaming the assignee for a reviewer's action is a false attribution
    assert reply_username("changes_requested", {}, "coder") is None


def test_reply_parts_are_conditional():
    from kanban_task_threads.render import render_reply

    # no dangling separators when the payload lacks the optional field
    assert render_reply("completed", {}) == "🏁 completed"
    assert render_reply("completed", {"summary": "ok"}) == "🏁 completed — ok"
    assert render_reply("gave_up", {}) == "🛑 gave up"
    assert render_reply("gave_up", {"reason": "breaker"}) == "🛑 gave up: breaker"
    assert render_reply("blocked", {"kind": "transient"}) == "⛔ blocked (`transient`)"


def test_review_replies_carry_their_payload():
    from kanban_task_threads.render import render_reply

    assert (
        render_reply(
            "review_requested",
            {"implementer": "coder", "reviewer": "marsan", "summary": "14 files"},
        )
        == "🔎 review requested → marsan — 14 files"
    )
    assert (
        render_reply("changes_requested", {"reviewer": "marsan", "reason": "tighten tests"})
        == "✏️ changes requested: tighten tests"
    )


def test_system_observations_are_unsigned():
    for kind in ("crashed", "timed_out", "reclaimed", "gave_up", "archived"):
        assert reply_username(kind, {}, "coder") is None


# --- status -> forum tag --------------------------------------------------------
# The tag convention: running, needs-human, blocked, review, done, failed.


def test_status_tag_mapping():
    from kanban_task_threads.render import tag_name_for

    assert tag_name_for("running") == "running"
    assert tag_name_for("review") == "review"
    assert tag_name_for("done") == "done"
    assert tag_name_for("archived") == "archived"
    assert tag_name_for("stale") == "failed"  # presumed dead wants eyes
    assert tag_name_for("dependency_wait") == "blocked"
    assert tag_name_for("triage") == "triage"
    assert tag_name_for("todo") == "todo"
    assert tag_name_for("scheduled") == "scheduled"
    assert tag_name_for("ready") == "ready"


def test_blocked_tag_depends_on_the_block_kind():
    from kanban_task_threads.render import tag_name_for

    assert tag_name_for("blocked", "needs_input") == "needs-human"
    assert tag_name_for("blocked", "capability") == "blocked"
    assert tag_name_for("blocked", "transient") == "blocked"
    assert tag_name_for("blocked", "") == "blocked"


# --- status vocabulary --------------------------------------------------------


def test_every_status_has_meta():
    for status in (
        "triage",
        "todo",
        "scheduled",
        "ready",
        "running",
        "blocked",
        "review",
        "done",
        "archived",
        "stale",
    ):
        label, color = STATUS_META[resolve_status_key(status, "")]
        assert label and isinstance(color, int)


def test_dependency_wait_is_visible_on_todo():
    # dependency blocks route to `todo`, not `blocked` — a renderer keyed on
    # status alone never shows the wait (ADR-0001)
    key = resolve_status_key("todo", "dependency")
    assert key == "dependency_wait"
    assert STATUS_META[key][0] != STATUS_META["todo"][0]


# --- the card -----------------------------------------------------------------


def test_card_summary_is_a_plain_one_liner():
    # the forum list preview can't render embeds ("Click to see attachment"):
    # the card carries a plain-text content line so state shows in the list
    card = render_card(view())
    assert card.summary == "● running · coder"
    stale = render_card(view(status="stale"))
    assert stale.summary.startswith("● stale")


def test_card_carries_status_color_and_title():
    card = render_card(view())
    assert card.color == STATUS_META["running"][1]
    assert card.title == "fix the build"
    assert "coder" in card.description
    assert "<t:1758190000:R>" in card.description


def test_card_title_truncated_to_thread_limit():
    card = render_card(view(title="t" * 300))
    assert len(card.title) == THREAD_TITLE_LIMIT


def test_card_description_truncated_to_embed_limit():
    card = render_card(view(block_reason="r" * 10000, status="blocked", block_kind="needs_input"))
    assert len(card.description) <= DESCRIPTION_LIMIT


def test_blocked_card_shows_kind_and_reason():
    card = render_card(
        view(status="blocked", block_kind="needs_input", block_reason="waiting on a decision")
    )
    assert "needs_input" in card.description
    assert "waiting on a decision" in card.description


def test_done_card_hides_historical_block_reason():
    # the reason comes from the last `blocked` event, which outlives the block:
    # only a present block_kind earns the waiting line
    card = render_card(view(status="done", block_kind="", block_reason="waiting on a decision"))
    assert "waiting on" not in card.description


def test_stale_card_says_stale_not_running():
    card = render_card(view(status="stale"))
    assert card.color == STATUS_META["stale"][1]
    assert "stale" in card.description.lower()


def test_card_shows_depends_rollup_when_present():
    card = render_card(view(depends="2 done · 1 running"))
    assert "depends on: 2 done · 1 running" in card.description


def test_card_shows_unlocks_when_present():
    card = render_card(view(unlocks="t_ep42 — the epic"))
    assert "unlocks: t_ep42 — the epic" in card.description


def test_card_omits_link_lines_without_links():
    card = render_card(view())
    assert "depends" not in card.description and "unlocks" not in card.description


def test_default_template_shows_workspace_path_when_present():
    # ADR-0011 opt-in must be visible without a custom template: the view only
    # carries workspace_path when the operator opted in
    card = render_card(view(workspace_path="/home/x/proj"))
    assert "/home/x/proj" in card.description


def test_default_template_omits_workspace_path_when_absent():
    card = render_card(view())
    assert "()" not in card.description  # no empty parenthesis artifact


def test_custom_template_falls_back_when_malicious():
    card = render_card(view(), template="{title.__class__}")
    assert "__class__" not in card.description
    assert "**running**" in card.description  # default template took over
