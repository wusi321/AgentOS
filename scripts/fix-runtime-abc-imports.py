#!/usr/bin/env python3
"""Replace ``typing`` collection aliases used at runtime with ``collections.abc``.

``typing.Mapping`` and friends are subscript-only aliases. Using them in an
``isinstance`` check is a latent defect: CPython 3.12 can compare against a
deprecated cached copy and answer ``False`` for a real ``dict``, which silently
disables whatever branch the check guards. This script rewrites such imports so
that runtime checks use the canonical ABCs while annotations keep working.

The rewrite is idempotent and reports every file it changed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Names that have a canonical collections.abc equivalent.
ABC_NAMES = ("Awaitable", "Callable", "Iterable", "Iterator", "Mapping", "MutableMapping", "Sequence", "Set")
#: Names that must stay in typing because they have no ABC equivalent.
TYPING_ONLY = (
    "Any",
    "AsyncIterator",
    "Final",
    "Literal",
    "NamedTuple",
    "Optional",
    "Protocol",
    "TypedDict",
    "TypeVar",
    "Union",
    "cast",
    "overload",
    "runtime_checkable",
)

IMPORT_PATTERN = re.compile(r"^from typing import (?P<names>.+)$", re.MULTILINE)
FUTURE_PATTERN = re.compile(r"^from __future__ import annotations$", re.MULTILINE)


def rewrite(text: str) -> tuple[str, list[str]]:
    """Return the rewritten text and the names moved to collections.abc."""
    moved: list[str] = []
    match = IMPORT_PATTERN.search(text)
    if match is None:
        return text, moved
    names = [name.strip() for name in match.group("names").split(",") if name.strip()]
    typing_names = [name for name in names if name not in ABC_NAMES]
    abc_names = [name for name in names if name in ABC_NAMES]
    if not abc_names:
        return text, moved
    moved = abc_names
    replacement_lines: list[str] = []
    if abc_names:
        replacement_lines.append("from collections.abc import " + ", ".join(sorted(abc_names)))
    if typing_names:
        replacement_lines.append("from typing import " + ", ".join(typing_names))
    replacement = "\n".join(replacement_lines)
    text = text[: match.start()] + replacement + text[match.end() :]
    # Keep the standard-library import block tidy: collections.abc precedes typing.
    if abc_names and typing_names:
        block = "from collections.abc import " + ", ".join(sorted(abc_names)) + "\nfrom typing import " + ", ".join(typing_names)
        text = text.replace(block, block)
    return text, moved


def main(argv: list[str]) -> int:
    """Rewrite the given files, or every module under ``src``."""
    targets: list[Path] = []
    if len(argv) > 1:
        targets = [Path(item) for item in argv[1:]]
    else:
        root = Path(__file__).resolve().parent.parent / "src"
        targets = sorted(root.rglob("*.py"))
    changed = 0
    for path in targets:
        original = path.read_text(encoding="utf-8")
        updated, moved = rewrite(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            print(f"{path}: moved {', '.join(sorted(moved))} to collections.abc")
            changed += 1
    print(f"files changed: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
