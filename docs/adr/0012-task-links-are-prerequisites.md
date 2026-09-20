# ADR-0012 — task_links are prerequisites, not a hierarchy

Status: accepted (2026-09-20) · corrects the reading behind the first epic-hub cut

## Context

`task_links(parent_id, child_id)` reads like a parent/child hierarchy, and the
epic hub was first built that way: epic as parent, subtasks as children. The
installed Hermes says otherwise: `_parents_satisfied` in `kanban_db` blocks a
child's completion until **every parent is done or archived** — the parent is
a *prerequisite*, the child is the *dependent*. An "epic" is therefore the
child of its subtasks: gated by them, completing last. The mistake survived a
full test suite because the fixtures encoded the same wrong assumption — the
truth only surfaced when the live CLI refused to complete a demo task
("unknown id or terminal state").

## Decision

The hub follows the graph's real direction, with dependency language:

- A **dependent's** card carries a live `depends on:` rollup of its
  prerequisites' statuses (the epic's card aggregates its subtasks).
- A **prerequisite's** card names what it `unlocks:` (its first dependent).
- A prerequisite opening its thread makes a best-effort `prerequisite opened`
  link-reply in each dependent's thread. Any progress on a prerequisite marks
  its dependents' cards dirty, so the durable signal is the repainted rollup;
  the convenience link is not retried independently.

Template fields are `{depends}`/`{depends_line}` and
`{unlocks}`/`{unlocks_line}`; nothing named "parent" or "children" remains in
the published surface.

## Consequences

- The epic experience is unchanged in spirit — the epic's thread is still the
  hub — but arrows and words now match what completion gating actually does.
- Test fixtures must link `(prerequisite, dependent)`; a fixture that encodes
  the hierarchy reading will fail the live CLI.
- Wake-routing ideas phrased as "notify the parent's assignee" must be re-read
  against these semantics before ever being built.
