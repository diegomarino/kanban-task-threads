"""Anti-stale guard for decision citations. Code, tests and docs cite ADRs
(`ADR-0002`); every citation must resolve to a record in docs/adr/, and no
legacy `§N` design-document references may remain — the design doc is not part
of the distributable repo."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = (
    list((ROOT / "kanban_task_threads").glob("*.py"))
    + [ROOT / "__init__.py"]
    + list((ROOT / "scripts").glob("*.py"))
    + list((ROOT / "tests").glob("*.py"))
    + list((ROOT / "docs").rglob("*.md"))
    + [ROOT / "README.md", ROOT / "ARCHITECTURE.md", ROOT / "plugin.yaml"]
)


def existing_adrs() -> set:
    return {p.name[:4] for p in (ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")}


def test_every_cited_adr_exists():
    adrs = existing_adrs()
    assert adrs, "no ADRs found under docs/adr/"
    dangling = {}
    for source in SOURCES:
        for match in re.finditer(r"ADR-(\d{4})", source.read_text()):
            if match.group(1) not in adrs:
                dangling.setdefault(match.group(1), set()).add(source.name)
    assert not dangling, f"citations of ADRs that do not exist: {dangling}"


def test_no_legacy_design_section_references_remain():
    stale = {}
    for source in SOURCES:
        if source.name == "test_design_refs.py":
            continue
        hits = re.findall(r"§\d+[a-z]?", source.read_text())
        if hits:
            stale[source.name] = hits
    assert not stale, (
        f"legacy design-section references remain: {stale} — cite an ADR from docs/adr/ instead"
    )
