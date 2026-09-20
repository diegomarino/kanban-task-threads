"""kanban-task-threads — plugin entry point.

`register(ctx)` does registration and nothing else: `hermes plugins validate`
probes it in a subprocess with a stub context whose attributes are no-ops, so
any real work here (opening a DB, resolving a secret, touching the network)
would fail the catalog gate.

Everything real happens lazily, on the first hook kick, inside the Runtime's
own thread — and inside the contextvars snapshot captured at that kick, so
`agent.secret_scope` resolves the *profile's* secrets (a ContextVar scope does
not cross an unbound thread; see docs/configuration.md).

Hooks are best-effort "poll now" kicks (ADR-0002): the durable task_events cursor is
what guarantees delivery; a missed kick costs seconds, not an event.
"""

import contextvars
import logging
import re

logger = logging.getLogger(__name__)

# Every kanban hook is a kick. Some fire inside the dispatch lock, so the
# callback must stay trivial — kick() only sets an event.
KICK_HOOKS = (
    "kanban_task_claimed",
    "kanban_task_completed",
    "kanban_task_blocked",
    "on_kanban_task_updated",
    "on_kanban_worker_spawned",
    "on_kanban_worker_exited",
    "on_kanban_worker_stale_claim",
    "on_kanban_dispatch_tick",
)

_SECRET_WEBHOOK = "KANBAN_TASK_THREADS_WEBHOOK_URL"
_SECRET_BOT = "KANBAN_TASK_THREADS_BOT_TOKEN"


def register(ctx):
    """The Hermes entry point: register the kick hooks and the unload
    callback, construct the (inert) Runtime — and nothing else; the validate
    probe runs this against a stub context."""
    from .kanban_task_threads.runtime import Runtime

    runtime = Runtime(
        lambda: _build_consumer(ctx),
        logger=logger,
        poll_seconds=float(ctx.get_config("poll_seconds", 20) or 20),
    )

    def _kick(**kwargs):
        runtime.kick(context=contextvars.copy_context())

    for hook in KICK_HOOKS:
        ctx.register_hook(hook, _kick)

    # The unload contract: discover_plugins(force=True) re-imports the module
    # and `plugins disable` walks the same path — without this, the consumer
    # thread is orphaned, contesting the lease from an unreachable module.
    # (Guarded because the validate probe's stub context stubs the attribute.)
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(runtime.shutdown)


def _build_consumer(ctx):
    """Runs once, in the runtime thread, inside the first kick's context
    snapshot. Returning None degrades the plugin to a no-op (ADR-0003: requires_env
    is not a load gate; never queue work that can never be sent)."""
    import os
    from pathlib import Path

    from .kanban_task_threads.consumer import Consumer
    from .kanban_task_threads.runtime import RetryableStartup
    from .kanban_task_threads.store import StateStore
    from .kanban_task_threads.transport import DiscordTransport, TransportError, urllib_http

    try:
        from agent import secret_scope
        from hermes_cli.kanban_db import get_current_board, kanban_db_path, kanban_home
    except ImportError as exc:
        logger.error(
            "kanban-task-threads: Hermes runtime not importable (%r); plugin is inactive", exc
        )
        return None

    try:
        webhook_url = secret_scope.get_secret(_SECRET_WEBHOOK)
        bot_token = secret_scope.get_secret(_SECRET_BOT)
    except Exception as exc:  # e.g. UnscopedSecretError under multiplexing
        raise RetryableStartup(f"secret scope unavailable in this kick's context: {exc!r}") from exc
    if not webhook_url:
        if secret_scope.current_secret_scope() is None:
            # The dispatcher tick runs in a deliberately EMPTY contextvars
            # context: this kick cannot see profile secrets, but a later kick
            # from a task hook can. Not a config verdict.
            raise RetryableStartup(
                f"{_SECRET_WEBHOOK} is not in the environment and this kick's "
                "context carries no profile secret scope; waiting for one that does"
            )
        logger.warning(
            "kanban-task-threads: %s is not set; degrading to a "
            "no-op (in production it should be an op:// reference "
            "resolved by the profile env, never a literal)",
            _SECRET_WEBHOOK,
        )
        return None

    tags = tuple(ctx.get_config("discord_applied_tag_ids") or ())
    transport = DiscordTransport(
        urllib_http, webhook_url, bot_token=bot_token, applied_tag_ids=tags
    )

    # Preflight. The channel is asked, never configured (ADR-0003): one credential,
    # one source of truth. With a bot token the tag requirement is checked for
    # real; without one, the consumer maps the create-time 400 to an
    # actionable message instead. Only a Discord *rejection* of the credential
    # is a config verdict; a 5xx/429 or a network error may heal.
    try:
        info = transport.webhook_info()
        # Rebuild with the discovered forum id: the bot tag operations resolve
        # tag names against the forum, and a transport without it raises on
        # the first tag attempt (the milestone-3 review's critical finding).
        transport = DiscordTransport(
            urllib_http,
            webhook_url,
            bot_token=bot_token,
            applied_tag_ids=tags,
            forum_channel_id=str(info["channel_id"]),
        )
        requires_tag = transport.forum_requires_tag(str(info["channel_id"]))
    except TransportError as err:
        if 400 <= err.status < 500 and err.status != 429:
            logger.error(
                "kanban-task-threads: Discord rejected the webhook "
                "(HTTP %s %r) — check %s. Plugin is inactive.",
                err.status,
                err.body,
                _SECRET_WEBHOOK,
            )
            return None
        raise RetryableStartup(f"Discord preflight failed: {err}") from err
    except OSError as exc:  # URLError and socket timeouts are OSError
        raise RetryableStartup(f"network error during preflight: {exc!r}") from exc
    if requires_tag and not tags:
        logger.error(
            "kanban-task-threads: forum %s requires a tag on every post and "
            "discord_applied_tag_ids is empty — every create would 400. "
            "Set discord_applied_tag_ids or drop the forum's tag requirement. "
            "Plugin is inactive.",
            info["channel_id"],
        )
        return None

    # State is board state (ADR-0005): a plugin-owned DB under the board-shared
    # kanban root — never ctx.state, which resolves per profile.
    board = ctx.get_config("board") or get_current_board()
    state = StateStore(
        Path(kanban_home()) / "kanban" / "plugins" / "kanban-task-threads" / f"{board}.db"
    )

    dashboard_url = ctx.get_config("dashboard_url", "") or ""
    if re.search(r"//(\d{1,3}\.){3}\d{1,3}([:/]|$)", dashboard_url):
        logger.warning(
            "kanban-task-threads: dashboard_url contains a literal IP (%s). "
            "Published links are frozen in Discord forever and an IP rots with "
            "the next lease — prefer a stable name that resolves from every "
            "device you will click from.",
            dashboard_url,
        )

    reply_on = ctx.get_config("reply_on")
    kwargs = {"reply_on": tuple(reply_on)} if reply_on else {}
    logger.info(
        "kanban-task-threads: publishing board %r to forum %s as %r",
        board,
        info.get("channel_id"),
        info.get("name"),
    )
    return Consumer(
        kanban_db_path(board),
        state,
        transport,
        board=board,
        holder=f"{ctx.profile_name}:{os.getpid()}",
        destination=f"discord:webhook:{info.get('id')}@{info.get('channel_id')}",
        stale_after=int(ctx.get_config("stale_after_seconds", 600) or 600),
        include_workspace_path=bool(ctx.get_config("include_workspace_path", False)),
        dashboard_url=dashboard_url,
        card_template=ctx.get_config("card_template"),
        reply_templates=ctx.get_config("reply_templates") or {},
        comment_excerpt_chars=int(ctx.get_config("comment_excerpt_chars", 180) or 0),
        guild_id=str(info.get("guild_id") or ""),
        **kwargs,
    )
