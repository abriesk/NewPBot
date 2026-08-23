"""The import-boundary test (IMPLEMENTATION.md §3, §18).

No module under app/core/ may import fastapi, aiogram, jinja2, aiosmtplib, or
nh3. The core holds the booking rules; the moment it can reach a transport, the
rules start living inside handlers again and every channel reimplements them.

If this test fails, fix the code. Never the test.

It works on the AST rather than by importing, so it reports every offender in
one run and needs no database or installed transport libraries.
"""

from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "app" / "core"

FORBIDDEN = frozenset({"fastapi", "aiogram", "jinja2", "aiosmtplib", "nh3"})


def _core_modules() -> list[Path]:
    return sorted(CORE.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    """Top-level package name of every import in the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # node.level > 0 is a relative import: never one of the forbidden.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_core_package_exists() -> None:
    assert CORE.is_dir(), f"expected the core package at {CORE}"
    assert _core_modules(), "no modules found under app/core/"


def test_core_does_not_import_channel_libraries() -> None:
    violations: list[str] = []
    for module in _core_modules():
        offending = _imported_roots(module) & FORBIDDEN
        for name in sorted(offending):
            violations.append(f"{module.relative_to(CORE.parents[1])} imports {name}")

    assert not violations, "app/core must not depend on any channel library:\n  " + "\n  ".join(
        violations
    )
