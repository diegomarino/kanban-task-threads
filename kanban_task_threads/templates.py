"""Safe rendering of operator-supplied templates (ADR-0006).

`str.format` traverses attributes and indices, so a template like
`{task.__init__.__globals__[...]}` reads module globals — including a
credential. Rendering therefore goes through `format_map` over a flat dict of
pre-stringified scalars, via a Formatter that rejects any field name
containing "." or "[", and positional fields outright.
"""

import string
from collections.abc import Mapping


class TemplateError(ValueError):
    """A template that must not be rendered as written."""


class _SafeFormatter(string.Formatter):
    def get_field(self, field_name, args, kwargs):
        if not field_name:
            raise TemplateError("positional fields are not allowed")
        if "." in field_name or "[" in field_name:
            raise TemplateError(f"field {field_name!r} is not a plain name")
        try:
            return kwargs[field_name], field_name
        except KeyError:
            raise TemplateError(f"unknown field {field_name!r}") from None


_FORMATTER = _SafeFormatter()


def safe_render(template: str, values: Mapping[str, str]) -> str:
    """Render `template` against flat scalars; raise TemplateError otherwise."""
    try:
        return _FORMATTER.vformat(template, (), dict(values))
    except TemplateError:
        raise
    except (ValueError, IndexError, KeyError) as exc:  # malformed format spec &c.
        raise TemplateError(str(exc)) from exc


def render_template(template: str, values: Mapping[str, str], *, default: str) -> str:
    """Render `template`, falling back to the built-in `default` if it fails.

    The default is trusted; if it fails too, that is a bug and it raises.
    """
    try:
        return safe_render(template, values)
    except TemplateError:
        return safe_render(default, values)
