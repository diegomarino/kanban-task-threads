#!/usr/bin/env python3
"""Publish a bounded, generic screenshot demo to one explicitly confirmed forum.

The webhook is accepted only through ``KANBAN_TASK_THREADS_DEMO_WEBHOOK_URL``.
It is never printed or persisted. ``plan`` is offline; ``publish`` performs one
preflight read followed by a fixed four-post scenario.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from kanban_task_threads.avatars import AvatarSet  # noqa: E402
from kanban_task_threads.render import (  # noqa: E402
    Card,
    render_card,
    render_reply,
)
from kanban_task_threads.transport import (  # noqa: E402
    DiscordTransport,
    Http,
    ThreadRef,
    urllib_http,
)
from kanban_task_threads.view import build_view  # noqa: E402

WEBHOOK_ENV = "KANBAN_TASK_THREADS_DEMO_WEBHOOK_URL"
BOT_TOKEN_ENV = "KANBAN_TASK_THREADS_BOT_TOKEN"
DEFAULT_RUN_ID = "catalog-v1"
DEFAULT_RECEIPT_DIR = REPO / ".sandbox" / "discord-demo"
DEMO_THREAD_NAME_LIMIT = 30
DEFAULT_AVATAR_BASE_URL = (
    "https://diegomarino.github.io/kanban-task-threads/v1/duotone/colored/96px"
)


class DemoRefusal(RuntimeError):
    """The demo refused before making or repeating an external mutation."""


@dataclass(frozen=True)
class ReplySpec:
    kind: str
    actor: str | None
    payload: dict
    template: str


@dataclass(frozen=True)
class PostSpec:
    key: str
    title: str
    status: str
    assignee: str
    block_kind: str = ""
    block_reason: str = ""
    replies: tuple[ReplySpec, ...] = ()
    edit_after_publish: bool = False


@dataclass(frozen=True)
class PublishResult:
    mutations: int
    thread_urls: tuple[str, ...]
    already_complete: bool = False


def scenario(*, anchor: int) -> tuple[PostSpec, ...]:
    """The fixed public-safe story used for catalog screenshots."""
    _ = anchor  # The anchor affects cards, not the scenario contract.
    return (
        PostSpec(
            key="running",
            title="Draft checklist",
            status="running",
            assignee="Builder",
            replies=(
                ReplySpec(
                    "commented",
                    "Builder",
                    {"author": "Builder"},
                    "💬 {author}: added rollout steps and linked the verification evidence",
                ),
            ),
        ),
        PostSpec(
            key="blocked",
            title="Confirm criteria",
            status="blocked",
            assignee="Builder",
            block_kind="needs_input",
            block_reason="waiting for an approval choice",
            replies=(
                ReplySpec(
                    "commented",
                    "Planner",
                    {"author": "Planner"},
                    "💬 {author}: clarified the acceptance criteria and the decision owner",
                ),
                ReplySpec(
                    "blocked",
                    "Builder",
                    {"kind": "needs_input", "reason": "waiting for an approval choice"},
                    "⛔ blocked (`{kind}`) — choose the supported compatibility target",
                ),
            ),
        ),
        PostSpec(
            key="review",
            title="Review retry policy",
            status="review",
            assignee="Builder",
            replies=(
                ReplySpec(
                    "review_requested",
                    "Builder",
                    {"reviewer": "Reviewer", "summary": "verification finished"},
                    "🔎 review requested — implementation ready; "
                    "tests and migration notes attached",
                ),
                ReplySpec(
                    "changes_requested",
                    "Reviewer",
                    {"reviewer": "Reviewer", "reason": "clarify the retry boundary"},
                    "✏️ changes requested — make the retry boundary explicit and rerun verification",
                ),
            ),
        ),
        PostSpec(
            key="done",
            title="Publish release",
            status="done",
            assignee="Builder",
            replies=(
                ReplySpec(
                    "commented",
                    "Planner",
                    {"author": "Planner"},
                    "💬 {author}: confirmed the release notes and final verification scope",
                ),
                ReplySpec(
                    "blocked",
                    "Builder",
                    {"kind": "needs_input", "reason": "approval"},
                    "⛔ blocked (`{kind}`) — final approval required before publication",
                ),
                ReplySpec(
                    "unblocked",
                    "Builder",
                    {},
                    "✅ unblocked — approval recorded; publication can continue",
                ),
                ReplySpec(
                    "completed",
                    "Builder",
                    {"summary": "release summary published"},
                    "🏁 completed — release notes published after final review",
                ),
            ),
            edit_after_publish=True,
        ),
    )


def demo_thread_name(post: PostSpec) -> str:
    """Return the screenshot-safe name, refusing any future wrap regression."""
    name = post.title
    if len(name) > DEMO_THREAD_NAME_LIMIT:
        raise DemoRefusal(
            f"demo thread name for {post.key} is {len(name)} characters; "
            f"limit is {DEMO_THREAD_NAME_LIMIT}"
        )
    return name


def _validate_webhook_url(value: str) -> str:
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise DemoRefusal("demo webhook must be a non-empty URL without whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value) or "\\" in value:
        raise DemoRefusal("demo webhook contains forbidden characters")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise DemoRefusal("demo webhook is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", parsed.path)
    ):
        raise DemoRefusal("demo webhook must be an exact discord.com webhook URL")
    return value.rstrip("/")


def _validate_bot_token(value: str) -> str:
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise DemoRefusal(f"{BOT_TOKEN_ENV} must be a non-empty token without whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DemoRefusal(f"{BOT_TOKEN_ENV} contains forbidden characters")
    return value


def _card(post: PostSpec, *, anchor: int, initial: bool = False) -> Card:
    status = "running" if initial and post.edit_after_publish else post.status
    task = {
        "title": post.title,
        "status": status,
        "assignee": post.assignee,
        "created_at": anchor - 1800,
        "started_at": anchor - 900,
        "last_heartbeat_at": anchor,
        "block_kind": post.block_kind if status == "blocked" else "",
        "workspace_kind": "demo",
        "branch_name": None,
    }
    view = build_view(task, now=anchor, block_reason=post.block_reason)
    return render_card(view)


def _digest(posts: tuple[PostSpec, ...], *, anchor: int, avatar_base_url: str) -> str:
    payload = {
        "anchor": anchor,
        "avatar_base_url": avatar_base_url,
        "posts": [asdict(post) for post in posts],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_receipt(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoRefusal(f"cannot safely read demo receipt at {path}") from exc


def _write_receipt(path: Path, receipt: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _claim_receipt(path: Path, receipt: dict) -> None:
    """Create the run receipt exactly once before the first mutation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DemoRefusal(
            "another publisher claimed this run id; inspect its receipt before retrying"
        ) from exc
    with os.fdopen(descriptor, "w") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _begin(receipt_path: Path, receipt: dict, operation: str) -> None:
    if operation in receipt["operations"]:
        raise DemoRefusal(f"operation {operation} already has a receipt; refusing to repeat it")
    receipt["operations"][operation] = {"status": "pending"}
    _write_receipt(receipt_path, receipt)


def _confirm(receipt_path: Path, receipt: dict, operation: str, **result: str) -> None:
    receipt["operations"][operation] = {"status": "confirmed", **result}
    _write_receipt(receipt_path, receipt)


def publish(
    *,
    http: Http,
    webhook_url: str,
    bot_token: str,
    expected_channel_id: str,
    receipt_path: Path,
    avatar_base_url: str,
    anchor: int,
) -> PublishResult:
    """Publish the exact scenario once, guarded by channel identity and a receipt."""
    url = _validate_webhook_url(webhook_url)
    token = _validate_bot_token(bot_token)
    if not re.fullmatch(r"[0-9]+", expected_channel_id):
        raise DemoRefusal("expected channel id must be a Discord snowflake")

    existing = _read_receipt(receipt_path)
    effective_anchor = int(existing.get("anchor", anchor)) if existing else anchor
    posts = scenario(anchor=effective_anchor)
    digest = _digest(posts, anchor=effective_anchor, avatar_base_url=avatar_base_url)
    avatars = AvatarSet.custom(avatar_base_url)
    preflight = DiscordTransport(http, url).webhook_info()
    actual_channel_id = str(preflight.get("channel_id") or "")
    if actual_channel_id != expected_channel_id:
        raise DemoRefusal(
            f"channel mismatch: expected {expected_channel_id}, "
            f"webhook belongs to {actual_channel_id}"
        )

    if existing:
        if existing.get("channel_id") != expected_channel_id:
            raise DemoRefusal("existing receipt belongs to another channel")
        if existing.get("scenario_digest") != digest:
            raise DemoRefusal("existing receipt describes a different scenario; use a new run id")
        pending = [
            key
            for key, operation in existing.get("operations", {}).items()
            if operation.get("status") == "pending"
        ]
        if pending:
            raise DemoRefusal(
                f"receipt has an ambiguous pending operation ({pending[0]}); "
                "inspect before retrying"
            )
        if existing.get("status") == "complete":
            return PublishResult(
                mutations=0,
                thread_urls=tuple(existing.get("thread_urls", ())),
                already_complete=True,
            )
        raise DemoRefusal("partial receipt found; refusing an automatic resume")

    guild_id = str(preflight.get("guild_id") or "")
    receipt = {
        "status": "publishing",
        "channel_id": expected_channel_id,
        "guild_id": guild_id,
        "anchor": effective_anchor,
        "scenario_digest": digest,
        "operations": {},
        "thread_urls": [],
    }
    _claim_receipt(receipt_path, receipt)

    operation = "forum.prepare-tags"
    _begin(receipt_path, receipt, operation)
    forum_transport = DiscordTransport(
        http,
        url,
        bot_token=token,
        forum_channel_id=expected_channel_id,
    )
    requires_tag = forum_transport.prepare_forum()
    _confirm(receipt_path, receipt, operation, requires_tag=str(requires_tag).lower())

    tag_ids = {}
    for post in posts:
        tag_id = forum_transport.status_tag_id(post.status)
        if tag_id is None:
            raise DemoRefusal(f"forum preparation did not produce the {post.status!r} tag")
        tag_ids[post.status] = tag_id

    refs: dict[str, ThreadRef] = {}
    mutations = 0

    for post in posts:
        transport = DiscordTransport(
            http,
            url,
            applied_tag_ids=(tag_ids[post.status],),
            avatars=avatars,
        )
        operation = f"{post.key}.create"
        _begin(receipt_path, receipt, operation)
        ref = transport.open_thread(
            title=demo_thread_name(post),
            card=_card(post, anchor=effective_anchor, initial=True),
        )
        refs[post.key] = ref
        thread_url = f"https://discord.com/channels/{guild_id}/{ref.thread_id}"
        receipt["thread_urls"].append(thread_url)
        _confirm(
            receipt_path,
            receipt,
            operation,
            thread_id=ref.thread_id,
            message_id=ref.message_id,
        )
        mutations += 1

        for index, reply in enumerate(post.replies, start=1):
            operation = f"{post.key}.reply-{index}"
            _begin(receipt_path, receipt, operation)
            message_id = transport.append(
                ref,
                content=render_reply(reply.kind, reply.payload, template=reply.template),
                username=reply.actor,
                message_type=reply.kind,
            )
            _confirm(receipt_path, receipt, operation, message_id=message_id)
            mutations += 1

        if post.edit_after_publish:
            operation = f"{post.key}.edit"
            _begin(receipt_path, receipt, operation)
            transport.edit_card(ref, _card(post, anchor=effective_anchor))
            _confirm(receipt_path, receipt, operation)
            mutations += 1

    receipt["status"] = "complete"
    _write_receipt(receipt_path, receipt)
    return PublishResult(mutations=mutations, thread_urls=tuple(receipt["thread_urls"]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("plan", help="print the fixed offline scenario")
    publish_parser = subcommands.add_parser("publish", help="publish the fixed scenario once")
    publish_parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    publish_parser.add_argument("--confirm-channel-id", required=True)
    publish_parser.add_argument("--avatar-base-url", default=DEFAULT_AVATAR_BASE_URL)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        posts = scenario(anchor=0)
        print(f"posts={len(posts)} replies={sum(len(post.replies) for post in posts)} edits=1")
        for post in posts:
            print(f"- {post.key}: {post.title} ({post.status})")
        print(
            "effect bound: webhook preflight + idempotent forum-tag preparation + "
            "14 content mutations; no enumeration or deletion"
        )
        return 0

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", arguments.run_id):
        raise DemoRefusal("run id must be a short lowercase ASCII slug")
    webhook_url = os.environ.get(WEBHOOK_ENV, "")
    bot_token = os.environ.get(BOT_TOKEN_ENV, "")
    result = publish(
        http=urllib_http,
        webhook_url=webhook_url,
        bot_token=bot_token,
        expected_channel_id=arguments.confirm_channel_id,
        receipt_path=DEFAULT_RECEIPT_DIR / f"{arguments.run_id}.json",
        avatar_base_url=arguments.avatar_base_url,
        anchor=int(time.time()),
    )
    marker = "already complete" if result.already_complete else f"{result.mutations} mutations"
    print(marker)
    for thread_url in result.thread_urls:
        print(thread_url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DemoRefusal as error:
        print(f"discord_demo: {error}", file=sys.stderr)
        raise SystemExit(2) from error
