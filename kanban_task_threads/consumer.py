"""The consumer (ADR-0002): task_events past a durable cursor.

Model: the board cursor is only a scan low-water mark; the durable position
that dedupes is per task (`posts.last_event_id`), mirroring ADR-0002's
subscription-per-task. A task that cannot make progress (backoff, unknown
create outcome) holds the low-water mark down; its events are re-scanned and
re-skipped cheaply until it can. Replies are at-least-once across a crash
between send and position write; creates are stricter — the attempt is
recorded before the call and an unknown outcome is never blindly retried.

Failure policy per ADR-0007:
- permanent 4xx  → dead-letter the task, with the response as detail
- 404            → a human deleted the post: tombstone, stop publishing
- 429            → per-task backoff from retry_after; other tasks unaffected
- 5xx / network  → report and retry on the next pass
Failures are reported in the run report, never swallowed.
"""

import json
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from .render import (
    COMMENTED_EXCERPT_TEMPLATE,
    CONTENT_LIMIT,
    render_card,
    render_reply,
    render_thread_name,
    reply_username,
    resolve_status_key,
    tag_name_for,
    truncate,
)
from .store import StateStore
from .transport import CAP_TAGS, CAP_TITLE_STATE, ThreadRef, Transport, TransportError
from .view import build_view

# Which events earn a permanent record in the thread. Not templatable (ADR-0006).
DEFAULT_REPLY_ON = (
    "commented",
    "blocked",
    "unblocked",
    "completed",
    "review_requested",
    "changes_requested",
    "gave_up",
    "crashed",
    "timed_out",
    "reclaimed",
    "archived",
)

_RETRYABLE_STATUSES = {429}  # everything else 4xx is permanent for our payloads

# `hermes kanban block|schedule|unblock --reason` mirrors the reason as a
# comment ("PREFIX: reason", see _commented in hermes_cli/kanban.py) alongside
# the semantic event. Publishing both says the same thing twice; the semantic
# reply wins. Only these exact CLI prefixes match — a human comment must start
# with "PREFIX: " verbatim to be caught, which is the mirror's own format.
_CLI_MIRROR_COMMENT = re.compile(r"^(BLOCKED|SCHEDULED|UNBLOCK): ")


@dataclass
class Report:
    """What one consumer pass did — and everything that went wrong, split by
    who has to act: `errors` need an operator, `warnings` retry themselves."""

    acquired: bool = True
    opened: list = field(default_factory=list)
    edited: list = field(default_factory=list)
    replied: int = 0
    errors: list = field(default_factory=list)  # permanent / needs an operator
    warnings: list = field(default_factory=list)  # transient, retried next pass


# Discord error codes. 10003/10008 ("Unknown Channel"/"Unknown Message") mean a
# human deleted the thread or the card — the thread-level deletion answers
# 400 code 10003, NOT 404 (probed live 2026-09-20). One human act, one verdict.
_GONE_CODES = {10003, 10008}
# "A tag is required to create a forum post in this channel".
_TAG_REQUIRED_CODE = 40067
_TAG_REQUIRED_HINT = (
    "this forum requires a tag on every post; set discord_applied_tag_ids in "
    "the plugin settings (or drop the forum's tag requirement)"
)


class Consumer:
    """One pass over a board's task_events, publishing through a Transport.

    Stateless between passes except for what StateStore holds; safe to
    construct in every process — the lease decides who actually publishes.
    """

    def __init__(
        self,
        board_db_path,
        store: StateStore,
        transport: Transport,
        *,
        board: str = "default",
        holder: str = "consumer",
        destination: str = "",
        reply_on: Sequence[str] = DEFAULT_REPLY_ON,
        lease_ttl: int = 60,
        stale_after: int = 600,
        include_workspace_path: bool = False,
        dashboard_url: str = "",
        card_template: str | None = None,
        reply_templates: dict | None = None,
        comment_excerpt_chars: int = 180,
        guild_id: str = "",
    ):
        self._board_db_path = str(board_db_path)
        self._store = store
        self._transport = transport
        self._board = board
        self._holder = holder
        self._destination = destination
        self._reply_on = frozenset(reply_on)
        self._lease_ttl = lease_ttl
        self._stale_after = stale_after
        self._include_workspace_path = include_workspace_path
        self._dashboard_url = dashboard_url
        self._card_template = card_template
        self._reply_templates = reply_templates or {}
        self._comment_excerpt_chars = comment_excerpt_chars
        self._guild_id = guild_id

    def run_once(self, now: int | None = None) -> Report:
        """One full pass: acquire the fenced lease, scan events past the board
        cursor, publish per task, sweep stale/recovered heartbeats, repaint
        dirty cards, advance the cursor by CAS, release. Never raises for
        per-task problems — they land in the Report."""
        # A fixed `now` (tests) freezes the pass clock; otherwise renewals use
        # the real clock, or a long pass would never actually extend the lease.
        self._fixed_now = now
        now = self._now()
        report = Report()
        lease = f"consume:{self._board}"
        token = self._store.acquire_lease(lease, self._holder, now=now, ttl=self._lease_ttl)
        if token is None:
            report.acquired = False
            return report
        conn = sqlite3.connect(f"file:{self._board_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cursor = self._store.get_cursor(self._board)
            events = conn.execute(
                "SELECT id, task_id, kind, payload, created_at "
                "FROM task_events WHERE id > ? ORDER BY id",
                (cursor,),
            ).fetchall()
            by_task: dict = {}
            for row in events:
                by_task.setdefault(row["task_id"], []).append(row)
            aborted = False

            def owns_lease():
                # Fencing: prove ownership before every task's side effects. A
                # holder that lost the lease must not race the new one.
                return self._store.renew_lease(
                    lease, self._holder, token, now=self._now(), ttl=self._lease_ttl
                )

            if events:
                batch_max = events[-1]["id"]
                floor = batch_max
                for task_id, task_events in by_task.items():
                    if not owns_lease():
                        report.warnings.append("lease lost mid-pass; aborting before the next task")
                        aborted = True
                        break
                    try:
                        pos = self._process_task(conn, task_id, task_events, batch_max, now, report)
                    except Exception as exc:  # one poison task must not starve the rest
                        report.errors.append(f"{task_id}: {exc!r}")
                        pos = self._position_of(task_id)
                    floor = min(floor, pos)
                if not aborted and floor > cursor:
                    self._store.advance_cursor(self._board, old=cursor, new=floor)

            # The stale sweep (ADR-0001), two-way — heartbeats are not events:
            # a card showing "running" whose heartbeat went quiet must turn
            # stale, and a card showing "stale" whose heartbeat returned must
            # recover, both without any event arriving. Marked dirty here; the
            # repaint below re-renders from the row and corrects card + tag.
            for shown, goes_stale in (("running", True), ("stale", False)):
                for task_id in self._store.tasks_showing(self._board, shown):
                    if task_id in by_task:
                        continue
                    row = conn.execute(
                        "SELECT status, last_heartbeat_at FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                    if not (row and row["status"] == "running" and row["last_heartbeat_at"]):
                        continue
                    is_stale = self._now() - row["last_heartbeat_at"] > self._stale_after
                    if is_stale == goes_stale:
                        self._store.set_card_dirty(self._board, task_id, True)

            # Cards whose last refresh failed, for tasks with no new events
            # this pass: repaint them, or a task gone quiet stays stale forever.
            for task_id in self._store.dirty_tasks(self._board):
                if aborted or task_id in by_task:
                    continue
                if not owns_lease():
                    report.warnings.append("lease lost mid-pass; aborting before the next repaint")
                    break
                self._repaint(conn, task_id, report)
        finally:
            conn.close()
            self._store.release_lease(lease, self._holder, token)
        return report

    def _now(self) -> int:
        return int(self._fixed_now if self._fixed_now is not None else time.time())

    def _position_of(self, task_id: str) -> int:
        post = self._store.get_post(self._board, task_id)
        return post["last_event_id"] if post else 0

    def _process_task(
        self, conn, task_id: str, events, batch_max: int, now: int, report: Report
    ) -> int:
        """Returns the scan low-water mark this task needs: batch_max when it
        needs nothing more from this batch, its durable position otherwise."""
        store, board = self._store, self._board
        post = store.get_post(board, task_id)

        if post and post["state"] in ("tombstone", "dead_letter"):
            return batch_max
        if (
            post
            and post["destination"]
            and self._destination
            and post["destination"] != self._destination
        ):
            # ADR-0007: a re-pointed webhook cannot edit the old post; replaying the
            # stale ids would 404 and read as a human deletion. Freeze instead.
            report.errors.append(
                f"{task_id}: destination changed (post lives at "
                f"{post['destination']}, now configured {self._destination}); "
                "frozen — migrate or clear the entry"
            )
            return batch_max
        if post and post["pending_create_at"] is not None:
            report.errors.append(
                f"{task_id}: create outcome unknown since "
                f"{post['pending_create_at']}; operator must reconcile"
            )
            return post["last_event_id"]
        if post and post["backoff_until"] > now:
            return post["last_event_id"]

        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task_row is None:  # deleted under us: nothing to render
            return batch_max
        view = self._view_for(conn, task_id, task_row, now)
        card = render_card(view, template=self._card_template)

        # -- ensure the post exists ------------------------------------------
        opened = False
        if post is None or post["thread_id"] is None:
            thread_name = render_thread_name(task_row["title"] or "", task_id)
            store.begin_create(board, task_id, now=now)
            try:
                ref = self._transport.open_thread(title=thread_name, card=card)
            except TransportError as err:
                return self._create_failed_known(task_id, err, now, report, batch_max)
            except Exception as exc:
                # Response lost: unknown outcome, recorded, never blindly retried.
                report.errors.append(f"{task_id}: create outcome unknown: {exc!r}")
                return self._position_of(task_id)
            store.complete_create(
                board,
                task_id,
                thread_id=ref.thread_id,
                message_id=ref.message_id,
                destination=self._destination,
            )
            store.set_thread_state(board, task_id, name=thread_name)
            report.opened.append(task_id)
            self._announce_to_dependents(conn, task_id, task_row, ref, report)
            opened = True
            pos = self._position_of(task_id)
        else:
            ref = ThreadRef(thread_id=post["thread_id"], message_id=post["message_id"])
            pos = post["last_event_id"]

        # -- replies, then one card refresh ----------------------------------
        replied = 0
        start_pos = pos
        edited = False
        try:
            for row in events:
                if row["id"] <= pos:
                    continue
                if row["kind"] in self._reply_on:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                    template = self._reply_templates.get(row["kind"])
                    suppress = False
                    if row["kind"] == "commented":
                        body = self._comment_body(conn, task_id, row, payload)
                        if body is not None and _CLI_MIRROR_COMMENT.match(body):
                            suppress = True  # the semantic event's reply covers it
                        elif body is not None:
                            payload = {**payload, "excerpt": self._excerpt(body, task_id)}
                            if template is None:
                                template = COMMENTED_EXCERPT_TEMPLATE
                    if not suppress:
                        content = self._with_anchor(
                            render_reply(row["kind"], payload, template=template), ref, task_id
                        )
                        username = reply_username(row["kind"], payload, task_row["assignee"])
                        self._transport.append(ref, content=content, username=username)
                        replied += 1
                pos = row["id"]
                store.set_task_position(board, task_id, last_event_id=pos)
            report.replied += replied
            # Refresh the card when something actually happened — or when a
            # prior failure left it dirty. A pinned-cursor rescan with no new
            # events must cost zero Discord traffic.
            progressed = pos > start_pos
            if replied or (not opened and (progressed or (post and post["card_dirty"]))):
                self._transport.edit_card(ref, card)
                self._store.set_card_dirty(board, task_id, False)
                report.edited.append(task_id)
                edited = True
        except TransportError as err:
            report.replied += replied
            return self._publish_failed(task_id, err, now, report, batch_max, pos)
        except Exception as exc:
            report.replied += replied
            self._store.set_card_dirty(board, task_id, True)
            report.warnings.append(f"{task_id}: publish failed, will retry: {exc!r}")
            return pos
        if opened or replied or edited:
            self._maintain_thread(task_id, ref, view, report)
            # The epic hub (ADR-0012): progress on a prerequisite stales its
            # dependents' rollups; the dirty-repaint machinery repaints them.
            # Gated on actual progress: a pinned-cursor rescan must not
            # re-dirty dependents every pass.
            for dependent_id in self._dependent_ids(conn, task_id):
                dep_post = store.get_post(board, dependent_id)
                if dep_post and dep_post["state"] == "live":
                    store.set_card_dirty(board, dependent_id, True)
        return batch_max

    def _view_for(self, conn, task_id: str, task_row, now: int) -> dict:
        return build_view(
            dict(task_row),
            now=now,
            stale_after=self._stale_after,
            include_workspace_path=self._include_workspace_path,
            block_reason=self._last_block_reason(conn, task_id),
            dashboard_url=self._task_url(task_id) if self._dashboard_url else "",
            unlocks=self._unlocks_of(conn, task_id),
            depends_summary=self._depends_summary(conn, task_id),
        )

    def _depends_summary(self, conn, task_id: str) -> str:
        """'1 running · 2 done' across this task's PREREQUISITES (its
        task_links parents — ADR-0012); '' when it depends on nothing.
        Pure function of board state, like everything on the card."""
        rows = conn.execute(
            "SELECT t.status, COUNT(*) AS n FROM task_links l "
            "JOIN tasks t ON t.id = l.parent_id WHERE l.child_id = ? "
            "GROUP BY t.status",
            (task_id,),
        ).fetchall()
        if not rows:
            return ""
        order = (
            "running",
            "blocked",
            "review",
            "ready",
            "scheduled",
            "todo",
            "triage",
            "done",
            "archived",
        )
        counts = {row["status"]: row["n"] for row in rows}
        parts = [f"{counts[s]} {s}" for s in order if s in counts]
        parts += [f"{n} {s}" for s, n in counts.items() if s not in order]
        return " · ".join(parts)

    def _announce_to_dependents(
        self, conn, task_id: str, task_row, new_ref: ThreadRef, report: Report
    ) -> None:
        """A prerequisite's thread just opened: leave a link in each
        dependent's thread — the title linking to the new thread, plus who has
        it and a taste of what it is — and stale their rollups. Best-effort:
        the durable signal is the dependent's card, which the repaint refreshes."""
        label = f"{task_row['title']} · {task_id}"
        if self._guild_id:
            label = f"[{label}](https://discord.com/channels/{self._guild_id}/{new_ref.thread_id})"
        content = f"🧵 prerequisite opened: {label} — assignee: {task_row['assignee'] or '—'}"
        body = (task_row["body"] or "").strip()
        if body:
            content += "\n> " + truncate(" ".join(body.split()), 140)
        for dependent_id in self._dependent_ids(conn, task_id):
            post = self._store.get_post(self._board, dependent_id)
            if not (post and post["state"] == "live" and post["thread_id"]):
                continue
            self._store.set_card_dirty(self._board, dependent_id, True)
            try:
                ref = ThreadRef(thread_id=post["thread_id"], message_id=post["message_id"])
                self._transport.append(ref, content=truncate(content, CONTENT_LIMIT))
            except Exception as exc:
                report.warnings.append(f"{dependent_id}: link not delivered: {exc!r}")

    def _maintain_thread(self, task_id: str, ref: ThreadRef, view: dict, report: Report) -> None:
        """Keep the thread's out-of-band properties (ADR-0003 bot extras) in step
        with the card: tag per status, name per title, archived when done.
        Each is PATCHed only on change — the store remembers what the thread
        currently shows. Failures are transient by policy: the un-updated
        store retries them on the next pass."""
        store, board = self._store, self._board
        post = store.get_post(board, task_id)
        if post is None:
            return
        key = resolve_status_key(view["status"], view["block_kind"])
        store.set_thread_state(board, task_id, status_key=key)  # feeds the sweep
        caps = self._transport.capabilities()
        if CAP_TITLE_STATE not in caps and CAP_TAGS not in caps:
            return
        try:
            wants_archived = key in ("done", "archived")
            if CAP_TITLE_STATE in caps and post["thread_archived"] and not wants_archived:
                # reanimated: unarchive first so the PATCHes below land cleanly
                self._transport.set_archived(ref, False)
                store.set_thread_state(board, task_id, archived=False)
            if CAP_TAGS in caps:
                tag = tag_name_for(key, view["block_kind"])
                if tag and tag != post["last_tag"]:
                    if self._transport.set_status_tag(ref, tag):
                        store.set_thread_state(board, task_id, tag=tag)
                elif tag is None and post["last_tag"]:
                    # reverted to an untagged (pre-run) state: a stale "done"
                    # left applied would lie in the forum's filter view
                    self._transport.clear_status_tag(ref)
                    store.set_thread_state(board, task_id, tag="")
            if CAP_TITLE_STATE in caps:
                name = render_thread_name(view["title"], task_id)
                # A NULL last_name is a legacy row (thread created before names
                # carried the id): the live name is unknown, so PATCH it once
                # rather than recording an assumption that can never self-correct.
                if name != (post["last_name"] or ""):
                    self._transport.rename(ref, name)
                    store.set_thread_state(board, task_id, name=name)
                if wants_archived and not post["thread_archived"]:
                    self._transport.set_archived(ref, True)
                    store.set_thread_state(board, task_id, archived=True)
        except TransportError as err:
            # Dirty the card so the retry has a vehicle: a terminal task may
            # never see another event to re-enter maintenance through.
            store.set_card_dirty(board, task_id, True)
            report.warnings.append(f"{task_id}: thread maintenance failed, will retry: {err}")
        except Exception as exc:
            store.set_card_dirty(board, task_id, True)
            report.warnings.append(f"{task_id}: thread maintenance failed, will retry: {exc!r}")

    def _unlocks_of(self, conn, task_id: str) -> str:
        """First dependent (task_links child) this task gates, labeled."""
        row = conn.execute(
            "SELECT l.child_id, t.title FROM task_links l "
            "LEFT JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return ""
        return f"{row['child_id']} — {row['title']}" if row["title"] else row["child_id"]

    def _dependent_ids(self, conn, task_id: str) -> list:
        return [
            r["child_id"]
            for r in conn.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,))
        ]

    def _repaint(self, conn, task_id: str, report: Report) -> None:
        """A card whose last refresh failed, with no new events to carry it:
        re-render from the row and PATCH again."""
        now = self._now()
        post = self._store.get_post(self._board, task_id)
        if (
            not post
            or post["thread_id"] is None
            or post["backoff_until"] > now
            or (
                post["destination"]
                and self._destination
                and post["destination"] != self._destination
            )
        ):
            return
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task_row is None:
            self._store.set_card_dirty(self._board, task_id, False)
            return
        ref = ThreadRef(thread_id=post["thread_id"], message_id=post["message_id"])
        view = self._view_for(conn, task_id, task_row, now)
        try:
            self._transport.edit_card(ref, render_card(view, template=self._card_template))
            self._store.set_card_dirty(self._board, task_id, False)
            report.edited.append(task_id)
            self._maintain_thread(task_id, ref, view, report)
        except TransportError as err:
            self._publish_failed(
                task_id, err, now, report, post["last_event_id"], post["last_event_id"]
            )
        except Exception as exc:
            report.warnings.append(f"{task_id}: card repaint failed, will retry: {exc!r}")

    def _create_failed_known(
        self, task_id: str, err: TransportError, now: int, report: Report, batch_max: int
    ) -> int:
        store, board = self._store, self._board
        if err.status in _RETRYABLE_STATUSES:
            store.clear_pending(board, task_id)
            store.set_backoff(board, task_id, until=now + _retry_after(err))
            return self._position_of(task_id)
        if 400 <= err.status < 500:
            detail = f"HTTP {err.status} {err.body!r}"
            if isinstance(err.body, dict) and err.body.get("code") == _TAG_REQUIRED_CODE:
                detail = f"{_TAG_REQUIRED_HINT} — {detail}"
            store.mark_dead_letter(board, task_id, detail)
            report.errors.append(f"{task_id}: dead-lettered: {detail}")
            return batch_max
        # 5xx: a proxy answering 502 does not prove Discord did not create the
        # thread. Same protocol as a lost response: recorded, never blindly
        # retried, surfaced until an operator reconciles.
        report.errors.append(
            f"{task_id}: create outcome unknown (HTTP {err.status}); operator must reconcile"
        )
        return self._position_of(task_id)

    def _publish_failed(
        self, task_id: str, err: TransportError, now: int, report: Report, batch_max: int, pos: int
    ) -> int:
        store, board = self._store, self._board
        if err.status == 404 or (
            isinstance(err.body, dict) and err.body.get("code") in _GONE_CODES
        ):
            # A human deleted the post (message-level 404, or thread-level
            # 400/10003). Tombstone; recreation is operator-only.
            store.mark_tombstone(board, task_id, f"404 at event {pos}: {err.body!r}")
            report.errors.append(f"{task_id}: post gone (404), tombstoned")
            return batch_max
        if err.status in _RETRYABLE_STATUSES:
            store.set_backoff(board, task_id, until=now + _retry_after(err))
            store.set_card_dirty(board, task_id, True)
            return pos
        if 400 <= err.status < 500:
            store.mark_dead_letter(board, task_id, f"HTTP {err.status} {err.body!r}")
            report.errors.append(f"{task_id}: dead-lettered: {err}")
            return batch_max
        store.set_card_dirty(board, task_id, True)
        report.warnings.append(f"{task_id}: publish failed, will retry: {err}")
        return pos

    def _task_url(self, task_id: str) -> str:
        """The dashboard URL for one task: `dashboard_url` may carry
        `{task_id}` and `{board}` placeholders; without them it is used
        as-is."""
        return self._dashboard_url.replace("{task_id}", task_id).replace("{board}", self._board)

    def _with_anchor(self, content: str, ref: ThreadRef, task_id: str) -> str:
        """Every reply ends with `[#]`: the task's dashboard page when a
        dashboard is configured (opens the browser), else an in-app jump to
        the live card. Masked links render in webhook message content (apps
        may; humans may not). No dashboard, no guild: no anchor."""
        if self._dashboard_url:
            url = self._task_url(task_id)
        elif self._guild_id:
            url = f"https://discord.com/channels/{self._guild_id}/{ref.thread_id}/{ref.message_id}"
        else:
            return content
        suffix = f" · [#]({url})"
        return truncate(content, CONTENT_LIMIT - len(suffix)) + suffix

    def _comment_body(self, conn, task_id: str, event_row, payload: dict) -> str | None:
        """Recover the comment's text: the event payload carries only
        {author, len}, but the comment row is written in the same transaction,
        so (task_id, author, length, created_at) pin it near-deterministically.
        None on any mismatch (purged comment, drifted schema, excerpts
        disabled): the plain "X commented" line renders — never guess."""
        if not self._comment_excerpt_chars:
            return None
        author, length = payload.get("author"), payload.get("len")
        if not isinstance(author, str) or not isinstance(length, int):
            return None
        row = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND author = ? "
            "AND length(body) = ? AND ABS(created_at - ?) <= 2 "
            "ORDER BY ABS(created_at - ?) LIMIT 1",
            (task_id, author, length, event_row["created_at"], event_row["created_at"]),
        ).fetchone()
        return row["body"] if row else None

    def _excerpt(self, body: str, task_id: str) -> str:
        text = " ".join(body.split())
        excerpt = truncate(text, self._comment_excerpt_chars)
        if excerpt != text and self._dashboard_url:
            excerpt += f" · [full comment]({self._task_url(task_id)})"
        return excerpt

    def _last_block_reason(self, conn, task_id: str) -> str:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row and row["payload"]:
            return str(json.loads(row["payload"]).get("reason", ""))
        return ""


def _retry_after(err: TransportError) -> int:
    value = err.body.get("retry_after", 1.0) if isinstance(err.body, dict) else 1.0
    try:
        return max(1, int(float(value) + 0.999))
    except (TypeError, ValueError):
        return 1
