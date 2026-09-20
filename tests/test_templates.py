"""ADR-0006: templates render with format_map over flat scalars, and any field name
containing "." or "[" is rejected — str.format would traverse attributes and
leak module globals. A template that fails to render falls back to the default.
"""

import pytest

from kanban_task_threads.templates import TemplateError, render_template, safe_render

VALUES = {"title": "fix the build", "status": "running"}


def test_renders_flat_fields():
    assert safe_render("{title} — {status}", VALUES) == "fix the build — running"


def test_rejects_attribute_traversal():
    with pytest.raises(TemplateError):
        safe_render("{title.__class__}", VALUES)


def test_rejects_index_traversal():
    with pytest.raises(TemplateError):
        safe_render("{title[0]}", VALUES)


def test_rejects_positional_fields():
    with pytest.raises(TemplateError):
        safe_render("{} {title}", VALUES)


def test_rejects_dunder_globals_probe():
    with pytest.raises(TemplateError):
        safe_render("{title.__init__.__globals__[secret]}", VALUES)


def test_missing_key_raises_template_error():
    with pytest.raises(TemplateError):
        safe_render("{nope}", VALUES)


def test_render_template_falls_back_to_default_on_bad_template():
    out = render_template("{title.__class__}", VALUES, default="{title}")
    assert out == "fix the build"


def test_render_template_uses_custom_template_when_valid():
    out = render_template("[{status}] {title}", VALUES, default="{title}")
    assert out == "[running] fix the build"
