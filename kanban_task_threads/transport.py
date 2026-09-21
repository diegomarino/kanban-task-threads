"""The transport seam (ADR-0004) and its Discord implementation (ADR-0003, ADR-0007).

The seam is small on purpose: `open_thread`, `edit_card`, `append`, plus a
capability declaration the renderer can ask about instead of assuming a common
subset. `set_title` and `archive` are bot-token extras and arrive with the
bot path (ADR-0003) — the capability constants already name them.

Hard rules from ADR-0007, enforced here so no caller can forget them:
- `allowed_mentions: {"parse": []}` on every single call. Titles and reasons
  are arbitrary agent output; webhook content parses mentions by default.
- Deterministic truncation to Discord's limits before sending.
"""

import json
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .avatars import AvatarSet
from .render import CONTENT_LIMIT, THREAD_TITLE_LIMIT, Card, truncate

CAP_RICH_CARD = "rich_card"
CAP_LIVE_TIMESTAMPS = "live_timestamps"
CAP_PER_MESSAGE_IDENTITY = "per_message_identity"
CAP_TITLE_STATE = "title_state"
CAP_TAGS = "tags"

EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
WEBHOOK_USERNAME_LIMIT = 80

# Not configurable (ADR-0006): correctness, not taste.
_NO_MENTIONS = {"parse": []}

_MANAGED_FORUM_TAG_NAMES = (
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
_MAX_FORUM_TAGS = 20


def _typed_webhook_username(username: str | None, message_type: str) -> str:
    """Keep the event type intact and fit the actor into Discord's limit."""
    suffix = f" · {message_type}"
    return f"{truncate(username or 'system', WEBHOOK_USERNAME_LIMIT - len(suffix))}{suffix}"


# The http seam: http(method, url, json_body_or_None, headers=None)
#   -> (status, decoded_body)
Http = Callable[..., tuple[int, dict]]


@dataclass(frozen=True)
class ThreadRef:
    """A forum post: the starter message and the thread it opened.

    On Discord the post id *is* the thread id; both are kept because the
    edit endpoint addresses the message while the reply endpoint addresses
    the thread.
    """

    thread_id: str
    message_id: str


class TransportError(RuntimeError):
    """A non-2xx transport response; `status` and decoded `body` drive the
    failure policy (ADR-0007)."""

    def __init__(self, status: int, body: dict):
        super().__init__(f"transport call failed: HTTP {status} {body!r}")
        self.status = status
        self.body = body


class ForumTagSetupError(RuntimeError):
    """The forum cannot fit or authorize the plugin-owned status tags."""


class Transport(Protocol):
    """The ADR-0004 seam. `capabilities()` declares what this transport can do
    (`rich_card`, `live_timestamps`, `per_message_identity`, `title_state`,
    `tags`); callers gate on it instead of assuming a common subset.
    `open_thread` returns the new thread's ref, `edit_card` rewrites the
    starter in place, `append` posts one reply and returns its message id."""

    def capabilities(self) -> frozenset: ...
    def prepare_forum(self) -> bool: ...
    def open_thread(self, *, title: str, card: Card) -> ThreadRef: ...
    def edit_card(self, ref: ThreadRef, card: Card) -> None: ...
    def append(
        self,
        ref: ThreadRef,
        *,
        content: str,
        username: str | None = None,
        message_type: str = "default",
    ) -> str: ...


class DiscordTransport:
    """Webhook-first (ADR-0003: write-only, bound to one channel). A bot token is a
    separate, optional capability — detected, never required (ADR-0003)."""

    def __init__(
        self,
        http: Http,
        webhook_url: str,
        *,
        bot_token: str | None = None,
        applied_tag_ids: Sequence[str] = (),
        forum_channel_id: str | None = None,
        avatars: AvatarSet | None = None,
    ):
        self._http = http
        self._webhook_url = webhook_url.rstrip("/")
        self._bot_token = bot_token
        self._applied_tag_ids = list(applied_tag_ids)
        self._forum_channel_id = forum_channel_id
        self._forum_channel: dict | None = None
        self._avatars = avatars
        self._tag_ids_by_name: dict | None = None  # fetched once, cached

    def capabilities(self) -> frozenset:
        caps = {CAP_RICH_CARD, CAP_LIVE_TIMESTAMPS, CAP_PER_MESSAGE_IDENTITY}
        if self._bot_token:
            caps |= {CAP_TITLE_STATE, CAP_TAGS}
        return frozenset(caps)

    def open_thread(self, *, title: str, card: Card) -> ThreadRef:
        body = {
            "thread_name": truncate(title, THREAD_TITLE_LIMIT),
            # content: the forum-list preview renders it; embeds show as
            # "Click to see attachment" there
            "content": card.summary,
            "embeds": [self._embed(card)],
            "allowed_mentions": _NO_MENTIONS,
        }
        if self._applied_tag_ids:
            body["applied_tags"] = self._applied_tag_ids
        if self._avatars:
            body["avatar_url"] = self._avatars.url_for("default")
        message = self._call("POST", f"{self._webhook_url}?wait=true", body)
        return ThreadRef(thread_id=str(message["channel_id"]), message_id=str(message["id"]))

    def edit_card(self, ref: ThreadRef, card: Card) -> None:
        self._call(
            "PATCH",
            f"{self._webhook_url}/messages/{ref.message_id}?thread_id={ref.thread_id}",
            {
                "content": card.summary,
                "embeds": [self._embed(card)],
                "allowed_mentions": _NO_MENTIONS,
            },
        )

    def append(
        self,
        ref: ThreadRef,
        *,
        content: str,
        username: str | None = None,
        message_type: str = "default",
    ) -> str:
        body = {
            "content": truncate(content, CONTENT_LIMIT),
            "allowed_mentions": _NO_MENTIONS,
        }
        if self._avatars:
            avatar_type = self._avatars.resolve_message_type(message_type)
            body["username"] = _typed_webhook_username(username, avatar_type)
            body["avatar_url"] = self._avatars.url_for(avatar_type)
        elif username:
            body["username"] = username
        message = self._call(
            "POST", f"{self._webhook_url}?wait=true&thread_id={ref.thread_id}", body
        )
        return str(message["id"])

    # --- bot-token extras (ADR-0003): capability-gated, the webhook cannot do these --

    def set_status_tag(self, ref: ThreadRef, name: str) -> bool | dict:
        """Apply the forum tag with this *name* to the thread (replacing any).
        Names are resolved against the forum definition prepared on the first
        pass that holds the board lease;
        ids stay Discord's business. An unknown name is a no-op returning
        False, which also protects a long-running process if an operator later
        deletes a managed tag."""
        tag_id = self.status_tag_id(name)
        if tag_id is None:
            return False
        return self._bot_patch_thread(ref, {"applied_tags": [tag_id]})

    def status_tag_id(self, name: str) -> str | None:
        """Resolve a plugin status-tag name once against the forum definition."""
        return self._forum_tag_ids().get(name)

    def clear_status_tag(self, ref: ThreadRef) -> bool | dict:
        """Back to the creation default: the configured applied_tag_ids (so a
        tag-required forum stays satisfied), or no tags at all."""
        return self._bot_patch_thread(ref, {"applied_tags": list(self._applied_tag_ids)})

    def rename(self, ref: ThreadRef, name: str) -> bool | dict:
        return self._bot_patch_thread(ref, {"name": name})

    def set_archived(self, ref: ThreadRef, archived: bool) -> bool | dict:
        return self._bot_patch_thread(ref, {"archived": archived})

    def list_forum_threads(self, guild_id: str) -> dict[str, dict]:
        """Return the bounded Discord metadata audit set.

        Discord exposes active threads in one guild-wide bulk read, so filter
        those by this forum. The public archived endpoint is forum-scoped and
        intentionally limited to its latest 25 entries; there is no pagination
        beyond that bounded reconciliation window.
        """
        headers = self._bot_headers()
        active = self._call(
            "GET",
            f"https://discord.com/api/v10/guilds/{guild_id}/threads/active",
            None,
            headers=headers,
        )
        archived = self._call(
            "GET",
            f"https://discord.com/api/v10/channels/{self._require_forum()}"
            "/threads/archived/public?limit=25",
            None,
            headers=headers,
        )
        forum_id = self._require_forum()
        result: dict[str, dict] = {}
        for channel in (*active.get("threads", []), *archived.get("threads", [])):
            if str(channel.get("parent_id") or "") != forum_id:
                continue
            thread_id = str(channel.get("id") or "")
            if not thread_id:
                continue
            result[thread_id] = {
                "applied_tags": tuple(str(tag) for tag in channel.get("applied_tags", [])),
                "archived": bool((channel.get("thread_metadata") or {}).get("archived")),
            }
        return result

    def prepare_forum(self) -> bool:
        """Ensure the plugin-owned status vocabulary exists in the forum.

        Existing tags are preserved byte-for-byte in the PATCH body. Missing
        managed names are appended, then Discord's response becomes the
        authoritative name-to-id cache. Returns whether the forum requires a
        tag on every newly-created post.
        """
        try:
            channel = self._refresh_forum_channel()
        except TransportError as err:
            if err.status == 403:
                raise ForumTagSetupError(
                    "Discord refused to read the forum; grant the bot View Channel on this forum"
                ) from err
            raise
        existing = list(channel.get("available_tags", []))
        existing_names = {str(tag.get("name")) for tag in existing}
        missing = [name for name in _MANAGED_FORUM_TAG_NAMES if name not in existing_names]
        if len(existing) + len(missing) > _MAX_FORUM_TAGS:
            raise ForumTagSetupError(
                "Discord forums allow at most 20 tags; "
                f"{len(existing)} exist and {len(missing)} managed tags are missing"
            )
        if missing:
            try:
                channel = self._call(
                    "PATCH",
                    f"https://discord.com/api/v10/channels/{self._require_forum()}",
                    {"available_tags": existing + [{"name": name} for name in missing]},
                    headers=self._bot_headers(),
                )
            except TransportError as err:
                if err.status == 403:
                    raise ForumTagSetupError(
                        "Discord refused to create the missing status tags; grant the bot "
                        "Manage Channels on this forum or create the tags manually"
                    ) from err
                raise
            self._cache_forum_channel(channel)
        requires_tag = bool(channel.get("flags", 0) & 16)
        if requires_tag and not self._applied_tag_ids:
            triage_id = self.status_tag_id("triage")
            if triage_id:
                self._applied_tag_ids = [triage_id]
        return requires_tag

    def _forum_tag_ids(self) -> dict:
        if self._tag_ids_by_name is None:
            self._cache_forum_channel(self._get_forum_channel())
        return self._tag_ids_by_name

    def _get_forum_channel(self) -> dict:
        if self._forum_channel is None:
            self._refresh_forum_channel()
        return self._forum_channel

    def _refresh_forum_channel(self) -> dict:
        channel = self._call(
            "GET",
            f"https://discord.com/api/v10/channels/{self._require_forum()}",
            None,
            headers=self._bot_headers(),
        )
        self._cache_forum_channel(channel)
        return channel

    def _cache_forum_channel(self, channel: dict) -> None:
        self._forum_channel = channel
        self._tag_ids_by_name = {
            str(tag.get("name")): str(tag.get("id")) for tag in channel.get("available_tags", [])
        }

    def _bot_patch_thread(self, ref: ThreadRef, body: dict) -> bool | dict:
        response = self._call(
            "PATCH",
            f"https://discord.com/api/v10/channels/{ref.thread_id}",
            body,
            headers=self._bot_headers(),
        )
        readback = self._metadata_readback(response)
        return readback if readback is not None else True

    @staticmethod
    def _metadata_readback(channel: dict) -> dict | None:
        """Normalize fields present in a successful Discord channel response.

        PATCH responses are authoritative when Discord supplies these fields;
        tiny test/proxy responses that omit them retain the requested-state
        fallback in the consumer.
        """
        result = {}
        if "applied_tags" in channel:
            result["applied_tags"] = tuple(str(tag) for tag in channel.get("applied_tags", []))
        metadata = channel.get("thread_metadata") or {}
        if "archived" in metadata:
            result["archived"] = bool(metadata["archived"])
        if "name" in channel:
            result["name"] = str(channel["name"])
        return result or None

    def _bot_headers(self) -> dict:
        if not self._bot_token:
            raise RuntimeError("bot operation without a bot token — gate on capabilities()")
        return {"Authorization": f"Bot {self._bot_token}"}

    def _require_forum(self) -> str:
        if not self._forum_channel_id:
            raise RuntimeError("forum_channel_id not set — pass it at construction")
        return self._forum_channel_id

    # --- preflight -----------------------------------------------------------

    def webhook_info(self) -> dict:
        """ADR-0003: one credential, one source of truth. The webhook object's
        `channel_id` is the forum it publishes to — asked, never configured."""
        return self._call("GET", self._webhook_url, None)

    def forum_requires_tag(self, channel_id: str) -> bool | None:
        """flags & 16 means every new post 400s without applied_tags (ADR-0007).
        The channel API needs a bot token; without one the answer is unknown
        (None) and the 400 on the first create is mapped instead."""
        if not self._bot_token:
            return None
        if self._forum_channel_id is None:
            self._forum_channel_id = str(channel_id)
        elif str(channel_id) != self._require_forum():
            raise RuntimeError("forum channel mismatch")
        channel = self._get_forum_channel()
        return bool(channel.get("flags", 0) & 16)

    def _embed(self, card: Card) -> dict:
        return {
            "title": truncate(card.title, EMBED_TITLE_LIMIT),
            "description": truncate(card.description, EMBED_DESCRIPTION_LIMIT),
            "color": card.color,
        }

    def _call(self, method: str, url: str, body: dict | None, headers: dict | None = None) -> dict:
        status, response = (
            self._http(method, url, body, headers=headers)
            if headers
            else self._http(method, url, body)
        )
        if not 200 <= status < 300:
            raise TransportError(status, response)
        return response


# Discord fronts with Cloudflare, which 403s (error code 1010) urllib's
# default User-Agent. Any identifying UA passes; the default is banned.
def _manifest_version() -> str:
    for line in (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator and key == "version":
            return value.strip().strip("\"'")
    raise RuntimeError("plugin.yaml has no top-level version")


_USER_AGENT = f"kanban-task-threads/{_manifest_version()} (Hermes plugin)"


def urllib_http(
    method: str, url: str, body: dict | None, timeout: float = 10.0, headers: dict | None = None
) -> tuple[int, dict]:
    """The real client behind the seam. Tests reach it only with urlopen faked."""
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"User-Agent": _USER_AGENT}
    if data:
        all_headers["Content-Type"] = "application/json"
    if headers:
        all_headers.update(headers)
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            decoded = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            decoded = {"raw": raw.decode(errors="replace")}
        return err.code, decoded
