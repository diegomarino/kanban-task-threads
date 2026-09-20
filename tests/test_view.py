"""ADR-0001/ADR-0011: the view is built from the task row alone; stale is computed, not
reported; the workspace path never leaves the machine unless opted in."""

from kanban_task_threads.view import build_view

TASK = {
    "id": "t_ab12",
    "title": "fix the build",
    "assignee": "coder",
    "status": "running",
    "created_at": 1000,
    "started_at": 2000,
    "completed_at": None,
    "workspace_kind": "worktree",
    "workspace_path": "/home/example/private-layout/project",
    "branch_name": "task/x",
    "last_heartbeat_at": 5000,
    "block_kind": None,
}


def task(**overrides):
    t = dict(TASK)
    t.update(overrides)
    return t


def test_view_is_flat_strings():
    view = build_view(task(), now=5010)
    assert all(isinstance(v, str) for v in view.values())
    assert view["title"] == "fix the build"
    assert view["status"] == "running"
    assert view["since_rel"] == "<t:2000:R>"  # started_at wins over created_at


def test_since_falls_back_to_created_at():
    view = build_view(task(started_at=None), now=5010)
    assert view["since_rel"] == "<t:1000:R>"


def test_running_with_old_heartbeat_is_stale():
    view = build_view(task(last_heartbeat_at=1000), now=5000, stale_after=600)
    assert view["status"] == "stale"


def test_done_task_is_never_stale():
    view = build_view(task(status="done", last_heartbeat_at=1000), now=99999)
    assert view["status"] == "done"


def test_workspace_path_is_off_by_default():
    view = build_view(task(), now=5010)
    assert "secret-layout" not in " ".join(view.values())
    assert view["workspace_kind"] == "worktree"


def test_workspace_path_on_optin():
    view = build_view(task(), now=5010, include_workspace_path=True)
    assert view["workspace_path"] == "/home/example/private-layout/project"


def test_block_reason_flows_into_view():
    view = build_view(
        task(status="blocked", block_kind="needs_input"),
        now=5010,
        block_reason="waiting on a decision",
    )
    assert view["block_kind"] == "needs_input"
    assert view["block_reason"] == "waiting on a decision"


def test_depends_summary_flows_into_view():
    view = build_view(task(), now=5010, depends_summary="2 done · 1 running")
    assert view["depends"] == "2 done · 1 running"


def test_depends_default_empty():
    assert build_view(task(), now=5010)["depends"] == ""


def test_unlocks_flows_into_view():
    view = build_view(task(), now=5010, unlocks="t_ep42 — the epic")
    assert view["unlocks"] == "t_ep42 — the epic"


def test_metadata_fields_are_accessible_to_templates():
    view = build_view(
        task(priority=2, created_by="diego", project_id="proj-x", max_runtime_seconds=900),
        now=5010,
    )
    assert view["priority"] == "2"
    assert view["created_by"] == "diego"
    assert view["project_id"] == "proj-x"
    assert view["max_runtime"] == "900"


def test_metadata_fields_default_to_empty_or_dash():
    view = build_view(task(), now=5010)
    assert view["priority"] == "0"
    assert view["created_by"] == "—"
    assert view["project_id"] == ""
    assert view["max_runtime"] == ""


def test_missing_optionals_render_empty_not_none():
    view = build_view(task(assignee=None, branch_name=None), now=5010)
    assert view["assignee"] == "—"
    assert view["branch"] == "—"
