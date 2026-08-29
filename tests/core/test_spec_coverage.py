"""The suite's own index (IMPLEMENTATION.md §3, §18).

Every test module opens by naming the spec sections it enforces. That
convention is what makes the suite navigable without reading it, and these
tests are what keep the convention true, because prose beside code drifts
unless something breaks when it does.

Three properties, and the third is the one a docstring cannot supply on its
own. A file can say what it covers; nothing can say what nothing covers. So
the last test reads §-headings out of IMPLEMENTATION.md and fails on a
top-level section no module claims -- which means a section added to the
normative spec cannot pass unnoticed. It has to gain a test, or an entry in
EXEMPT saying in words why it never will.

Scope is deliberately the module docstring and not the whole file. Prose
inside a test resolves against the context it sits in and is sometimes not a
spec reference at all -- test_calendar.py cites RFC 5545, section 3.1 -- so
widening this would buy stricter checking at the cost of false failures.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
SPECS = {
    "IMPLEMENTATION.md": REPO_ROOT / "docs" / "IMPLEMENTATION.md",
    "DESIGN.md": REPO_ROOT / "docs" / "DESIGN.md",
}

# A top-level section of IMPLEMENTATION.md that no test claims, and why.
# An entry here is a statement that the section specifies nothing a test
# could fail on -- not that testing it is inconvenient.
EXEMPT = {
    1: "how to read the document; addressed to a person, not the code",
    2: "the dependency list. Its one behavioural claim -- hard rule 3, no "
       "APScheduler or Celery or Redis -- is asserted in test_worker_jobs.py",
    5: "enum declarations. Their values are asserted where they are used: "
       "the state machines in test_booking.py and test_slots.py, the column "
       "types in test_schema.py",
    21: "features deliberately absent. There is no behaviour to assert, and "
        "a test that one does not exist would pass forever by accident",
}

# "DESIGN.md §22.4" and "IMPLEMENTATION.md §7.1" bind explicitly; a bare
# "§14" means the normative document.
_QUALIFIER = re.compile(r"(DESIGN\.md|IMPLEMENTATION\.md)")
_REFERENCE = re.compile(r"§(\d+(?:\.\d+)*)")
_HEADING = re.compile(r"^#{2,3} (\d+(?:\.\d+)*)\.? ")


def _headings(path: Path) -> set[str]:
    if not path.exists():
        raise AssertionError(
            f"{path} is not readable. docs/ is mounted by docker-compose.dev.yml "
            "and is not in the image -- run pytest with the dev overlay set:\n"
            '  export COMPOSE_FILE="docker-compose.yml;docker-compose.dev.yml"'
        )
    return {
        m.group(1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if (m := _HEADING.match(line))
    }


def _module_docstring(path: Path) -> str | None:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))


def _references(docstring: str) -> set[tuple[str, str]]:
    """Every (document, section) the docstring claims.

    The document is whichever name was last mentioned before the § on that
    same line, so one line may bind two references to two documents.
    """
    found: set[tuple[str, str]] = set()
    for line in docstring.splitlines():
        for match in _REFERENCE.finditer(line):
            names = _QUALIFIER.findall(line[: match.start()])
            found.add((names[-1] if names else "IMPLEMENTATION.md", match.group(1)))
    return found


TEST_MODULES = sorted(TESTS_ROOT.rglob("test_*.py"))


def test_the_suite_was_actually_found() -> None:
    """Guards every test below: an empty glob would pass all of them."""
    assert len(TEST_MODULES) > 20


@pytest.mark.parametrize("module", TEST_MODULES, ids=lambda p: p.name)
def test_every_module_names_the_sections_it_enforces(module: Path) -> None:
    docstring = _module_docstring(module)
    assert docstring, f"{module.name} has no module docstring"
    assert _references(docstring), (
        f"{module.name} names no spec section. Open with the § it enforces, "
        "so the suite can be read as an index of the specification."
    )


@pytest.mark.parametrize("module", TEST_MODULES, ids=lambda p: p.name)
def test_every_referenced_section_exists(module: Path) -> None:
    """A renumbered or deleted section must not leave a test pointing at air."""
    docstring = _module_docstring(module)
    assert docstring is not None
    for document, section in sorted(_references(docstring)):
        assert section in _headings(SPECS[document]), (
            f"{module.name} cites {document} §{section}, which has no heading "
            f"in {document}."
        )


def test_every_top_level_specification_section_is_claimed() -> None:
    """The one thing no single file can assert: what nothing covers."""
    claimed = set()
    for module in TEST_MODULES:
        docstring = _module_docstring(module)
        if not docstring:
            continue
        claimed |= {
            int(section.split(".")[0])
            for document, section in _references(docstring)
            if document == "IMPLEMENTATION.md"
        }

    sections = {int(h.split(".")[0]) for h in _headings(SPECS["IMPLEMENTATION.md"])}
    unclaimed = sorted(sections - claimed - set(EXEMPT))
    assert not unclaimed, (
        "No test module claims IMPLEMENTATION.md "
        + ", ".join(f"§{n}" for n in unclaimed)
        + ". Cover it, or add it to EXEMPT with the reason it needs no test."
    )


def test_exempt_lists_no_section_that_has_since_been_tested() -> None:
    """An exemption that stopped being true is a lie in the coverage report."""
    claimed = set()
    for module in TEST_MODULES:
        docstring = _module_docstring(module)
        if docstring:
            claimed |= {
                int(section.split(".")[0])
                for document, section in _references(docstring)
                if document == "IMPLEMENTATION.md"
            }
    stale = sorted(claimed & set(EXEMPT))
    assert not stale, (
        "EXEMPT still excuses "
        + ", ".join(f"§{n}" for n in stale)
        + ", which a test module now claims. Drop the entry."
    )


def test_exempt_names_only_real_sections() -> None:
    sections = {int(h.split(".")[0]) for h in _headings(SPECS["IMPLEMENTATION.md"])}
    assert not sorted(set(EXEMPT) - sections), "EXEMPT names a section that is gone"
