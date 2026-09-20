"""From a task row to the flat, pre-stringified view the renderer takes.

Stale is computed here, not received (ADR-0001): a SIGKILLed worker runs no exit
path, so "running" with a heartbeat older than the threshold *is* the stale
state — waiting for a graceful signal would wait forever.

The workspace path is an egress decision (ADR-0011): off unless opted in.
"""

from collections.abc import Mapping


def build_view(
    task: Mapping,
    *,  # everything below is context the row alone cannot supply
    now: int,
    stale_after: int = 600,
    include_workspace_path: bool = False,
    block_reason: str = "",
    dashboard_url: str = "",
    unlocks: str = "",
    depends_summary: str = "",
) -> dict:
    """Task row → the flat dict of strings the renderer and templates see.
    An allowlist (ADR-0011): what is not returned here cannot be published."""
    status = task.get("status") or ""
    heartbeat: int | None = task.get("last_heartbeat_at")
    if status == "running" and heartbeat and now - heartbeat > stale_after:
        status = "stale"

    since = task.get("started_at") or task.get("created_at") or now
    view = {
        "title": task.get("title") or "",
        "status": status,
        "assignee": task.get("assignee") or "—",
        "since_rel": f"<t:{since}:R>",
        "block_kind": task.get("block_kind") or "",
        "block_reason": block_reason or "",
        "branch": task.get("branch_name") or "—",
        "workspace_kind": task.get("workspace_kind") or "—",
        "url": dashboard_url or "",
        "unlocks": unlocks or "",
        "depends": depends_summary or "",
        # Metadata a template may want; not in the default card. The view is
        # an allowlist on purpose (ADR-0006/ADR-0011): a template can only leak what this
        # function exposes, so absence is the safe default — additions here
        # are egress decisions, documented in docs/card-and-log.md.
        "priority": str(task.get("priority") or 0),
        "created_by": task.get("created_by") or "—",
        "project_id": task.get("project_id") or "",
        "max_runtime": str(task.get("max_runtime_seconds") or "") or "",
    }
    if include_workspace_path:
        view["workspace_path"] = task.get("workspace_path") or ""
    return view
