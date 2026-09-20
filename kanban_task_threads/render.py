"""The card (ADR-0001): a pure function of board state.

The renderer receives a flat view of pre-stringified scalars — never Task
objects — so templates stay safe (ADR-0006) and the output is a `Card` the transport
maps to its own message shape (ADR-0004).

Truncation is deterministic at Discord's limits (ADR-0007): a poison event must
render to the same, in-bounds text every time rather than 400 forever.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .templates import render_template

CONTENT_LIMIT = 2000  # message content
DESCRIPTION_LIMIT = 4096  # embed description
THREAD_TITLE_LIMIT = 100  # forum thread name

# status key -> (label, embed colour). Covers all nine VALID_STATUSES, plus
# `stale` (a first-class state, ADR-0001: a SIGKILLed worker never reports back) and
# `dependency_wait` (dependency blocks route to `todo`, not `blocked` — keyed
# on status alone the wait would be invisible).
STATUS_META = {
    "triage": ("triage", 0x95A5A6),
    "todo": ("todo", 0x99AAB5),
    "scheduled": ("scheduled", 0x3498DB),
    "ready": ("ready", 0x1ABC9C),
    "running": ("running", 0x3BA55D),
    "blocked": ("blocked", 0xE67E22),
    "review": ("review", 0x9B59B6),
    "done": ("done", 0x2ECC71),
    "archived": ("archived", 0x607D8B),
    "stale": ("stale — no heartbeat", 0xE74C3C),
    "dependency_wait": ("waiting on dependency", 0xF1C40F),
}

DEFAULT_CARD_TEMPLATE = (
    "**{status_label}** · since {since_rel}\n"
    "assignee: {assignee}\n"
    "{block_line}"
    "{depends_line}"
    "{unlocks_line}"
    "workspace: {workspace_kind}{workspace_path_part} · branch: {branch}\n"
    "{url_line}"
)

# One reply per event worth a permanent record (ADR-0001), overridable by taste (ADR-0006).
# The `*_part` fields are composed (separator included, empty when the payload
# lacks the value) so no template ever renders a dangling "— " or ": ".
DEFAULT_REPLY_TEMPLATES = {
    "commented": "💬 {author} commented",
    "blocked": "⛔ blocked (`{kind}`){reason_part}",
    "unblocked": "✅ unblocked",
    "completed": "🏁 completed{summary_part}",
    "review_requested": "🔎 review requested{reviewer_part}{summary_part}",
    "changes_requested": "✏️ changes requested{reason_part}",
    "gave_up": "🛑 gave up{reason_part}",
    "crashed": "💥 worker crashed",
    "timed_out": "⏱️ timed out",
    "reclaimed": "♻️ claim reclaimed",
    "archived": "📦 archived",
}
# Used instead of the "commented" default when the comment's text could be
# recovered from task_comments (the event payload carries only {author, len}).
COMMENTED_EXCERPT_TEMPLATE = "💬 {author}: {excerpt}"
GENERIC_REPLY_TEMPLATE = "• {event_kind}"


@dataclass(frozen=True)
class Card:
    """Transport-neutral card. The transport decides how it becomes a message.
    `summary` is a plain one-liner for surfaces that cannot render the rich
    body (e.g. Discord's forum-list preview shows message content only)."""

    title: str
    description: str
    color: int
    summary: str = ""


def truncate(text: str, limit: int) -> str:
    """Deterministic: same input, same in-bounds output. Marks the cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def resolve_status_key(status: str, block_kind: str) -> str:
    """Board status → presentation key. The one non-identity case: a
    dependency block routes to `todo` on the board, so it gets its own key or
    the wait would be invisible (ADR-0001). Unknown statuses render as triage
    rather than crashing on a future Hermes addition."""
    if status == "todo" and block_kind == "dependency":
        return "dependency_wait"
    return status if status in STATUS_META else "triage"


def render_card(view: Mapping[str, str], *, template: str | None = None) -> Card:
    """Flat view → Card: composes the optional lines (block, parent, children,
    url, workspace path), renders the body through the safe template
    machinery, and truncates everything to Discord's limits."""
    key = resolve_status_key(view.get("status", ""), view.get("block_kind", ""))
    label, color = STATUS_META[key]

    values = dict(view)
    values["status_label"] = label
    block_kind = view.get("block_kind", "")
    block_reason = view.get("block_reason", "")
    # The reason is read from the last `blocked` event and outlives the block;
    # only a block that is *present* (a kind on the task row) earns the line.
    values["block_line"] = f"waiting on: `{block_kind}` — {block_reason}\n" if block_kind else ""
    # task_links are prerequisites (ADR-0012): a dependent rolls up what
    # gates it; a prerequisite names what it unlocks.
    depends = view.get("depends", "")
    values["depends_line"] = f"depends on: {depends}\n" if depends else ""
    unlocks = view.get("unlocks", "")
    values["unlocks_line"] = f"unlocks: {unlocks}\n" if unlocks else ""
    url = view.get("url", "")
    values["url_line"] = f"[open in dashboard]({url})" if url else ""
    # Present only when the operator opted in (ADR-0011): the view omits the key
    # otherwise, so the path cannot leak through a template by accident.
    path = view.get("workspace_path", "")
    values["workspace_path_part"] = f" (`{path}`)" if path else ""

    description = render_template(
        template if template is not None else DEFAULT_CARD_TEMPLATE,
        values,
        default=DEFAULT_CARD_TEMPLATE,
    )
    return Card(
        title=truncate(view.get("title", ""), THREAD_TITLE_LIMIT),
        description=truncate(description, DESCRIPTION_LIMIT),
        color=color,
        summary=truncate(f"● {label} · {view.get('assignee', '—')}", 100),
    )


# status key -> forum tag name (the six-tag convention: running, needs-human,
# blocked, review, done, failed). Pre-run states stay untagged; `stale` maps to
# `failed` because a presumed-dead worker is what that tag exists to surface.
_STATUS_TAGS = {
    "running": "running",
    "stale": "failed",
    "review": "review",
    "done": "done",
    "archived": "done",
    "dependency_wait": "blocked",
}


def tag_name_for(status_key: str, block_kind: str = "") -> str | None:
    """Status key → forum tag name per the six-tag convention; None for the
    pre-run states, which stay untagged."""
    if status_key == "blocked":
        return "needs-human" if block_kind == "needs_input" else "blocked"
    return _STATUS_TAGS.get(status_key)


def render_thread_name(title: str, task_id: str) -> str:
    """The thread name is frozen at creation (a webhook can never rename), so
    it carries the one stable key a human or an operator needs: the task id.
    Duplicate titles stay distinguishable, and reconciling an unknown-outcome
    create becomes a deterministic forum search instead of a guess. The title
    is what gets truncated; the id always survives."""
    suffix = f" · {task_id}" if task_id else ""
    if len(suffix) >= THREAD_TITLE_LIMIT:  # absurd id: keep the invariant anyway
        return truncate(task_id, THREAD_TITLE_LIMIT)
    return truncate(title, THREAD_TITLE_LIMIT - len(suffix)) + suffix


# Actions the assignee performs; everything else that lacks a named actor is
# the board observing, and stays unsigned (ADR-0008: the card is the board speaking,
# the replies are who did what — and "what" includes "who really did it").
_WORKER_ACTION_KINDS = frozenset({"blocked", "unblocked", "completed"})


def reply_username(kind: str, payload: Mapping, assignee: str | None) -> str | None:
    """Who signs a reply: the named actor when the payload carries one, the
    assignee for the worker's own actions, and nobody (the webhook's
    institutional name) for system observations or unknown actors — signing a
    crash or a reviewer's verdict as the assignee is a false attribution."""

    def named(key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    if named("author"):
        return named("author")
    if kind == "review_requested":
        return named("implementer") or assignee or None
    if kind == "changes_requested":
        return named("reviewer")
    if kind in _WORKER_ACTION_KINDS:
        return assignee or None
    return None


def render_reply(kind: str, payload: Mapping, *, template: str | None = None) -> str:
    """One reply per event. Values are pre-stringified payload scalars with
    safe defaults, so an operator template can never fail closed — the worst
    case is the generic line."""
    values = {
        "event_kind": kind,
        "author": "someone",
        "kind": "",
        "reason": "",
        "summary": "",
        "reviewer": "",
    }
    values.update({k: str(v) for k, v in payload.items() if isinstance(v, (str, int, float, bool))})
    values["summary_part"] = f" — {values['summary']}" if values["summary"] else ""
    values["reason_part"] = f": {values['reason']}" if values["reason"] else ""
    values["reviewer_part"] = f" → {values['reviewer']}" if values["reviewer"] else ""
    chosen = (
        template
        if template is not None
        else DEFAULT_REPLY_TEMPLATES.get(kind, GENERIC_REPLY_TEMPLATE)
    )
    return truncate(render_template(chosen, values, default=GENERIC_REPLY_TEMPLATE), CONTENT_LIMIT)
