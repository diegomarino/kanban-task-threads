# ADR-0006 — Allowlisting templates; correctness is not templatable

Status: accepted (2026-09-18)

## Context

Operators want to restyle the card and the replies. But `str.format` on
operator-supplied templates traverses attributes and indices —
`{task.__init__.__globals__[...]}` reads module globals, credentials included —
and schema validation only warns. And some behavior must never vary by taste:
a deployment that turns off truncation or mention-neutralization is broken,
not customized.

## Decision

Templatable (settings): the card body, per-event reply text, which events earn
a reply, cosmetic knobs. **Not templatable**: when the card re-renders,
idempotency, retry policy, truncation limits, `allowed_mentions`.

Rendering is `str.format_map` over a **flat dict of pre-stringified scalars**,
through a Formatter that rejects positional fields and any field name
containing `.` or `[`. A template that fails to render for any reason falls
back to the built-in default — an operator typo makes a card uglier, never
invisible or leaky. The dict is an allowlist (ADR-0011): a template cannot
reference what the view does not expose.

## Consequences

- Template fields are flat names only; composed convenience fields
  (`{block_line}`, `{url_line}`) carry their own newlines and vanish when
  empty.
- New event-payload scalars become template-addressable automatically; new
  task-row fields require a deliberate view addition.
