#!/usr/bin/env python3
"""Place the shared verification library correctly in the helper scripts.

A shebang must be the first line of a script. The first attempt at this insertion
treated the ``#!`` line as an ordinary comment, so it wrote the library source line
and the sudo pre-flight check *above* the shebang. The kernel then saw no interpreter
directive, and the scripts produced no output at all — the failure mode of a broken
shebang is silence, which is why this is repaired by a checked script rather than by
hand.

This module finds the shebang, removes any previously inserted block, and writes the
block back in the only correct places: immediately after the shebang and its header
comments for the source line, and after the top-level variable declarations for the
guard. It is idempotent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

TARGETS = ("verify-live.sh", "verify-data-retention.sh", "test-install-routes.sh", "check-purge.sh")

HERE_DECLARATION = 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
SOURCE_BLOCK = (
    "# shellcheck source=lib-agentos-verify.sh\n"
    '. "$HERE/lib-agentos-verify.sh"'
)
GUARD_COMMENT = "# Fail immediately with an explanation rather than hanging on a sudo prompt."
GUARD_LINE = "agentos_require_sudo || exit 3"

#: Lines this tool may own and therefore may remove and re-place.
OWNED_MARKERS = (
    "lib-agentos-verify.sh",
    "agentos_require_sudo",
    "Fail immediately with an explanation",
)

#: A UTF-8 byte-order mark, which Windows tooling can leave inside a script. Inside
#: a shebang it makes the line unrecognisable to the kernel and to this tool.
BOM = "\ufeff"


def clean_bom(text: str) -> tuple[str, bool]:
    """Return the text with any byte-order mark removed."""
    if BOM not in text:
        return text, False
    return text.replace(BOM, ""), True


def split_owned(lines: list[str]) -> list[str]:
    """Return the lines with every previously inserted library line removed."""
    kept: list[str] = []
    for line in lines:
        if any(marker in line for marker in OWNED_MARKERS):
            continue
        if line.strip() == "# shellcheck source=lib-agentos-verify.sh":
            continue
        kept.append(line)
    return kept


def shebang_index(lines: list[str]) -> int:
    """Return the index of the shebang line, or -1 when there is none.

    The scan tolerates a byte-order mark, blank lines, and comment lines before the
    shebang, because an earlier edit of this repository pushed the shebang below
    inserted lines and left a BOM on it.
    """
    for index, line in enumerate(lines):
        if line.lstrip(BOM).startswith("#!"):
            return index
        stripped = line.strip()
        # A shebang can only be the first non-comment line; stop at real content.
        if stripped and not stripped.startswith("#"):
            return -1
    return -1


def header_end(lines: list[str], start: int) -> int:
    """Return the index just past the header comment block that follows a shebang."""
    index = start + 1
    while index < len(lines) and (lines[index].startswith("#") or not lines[index].strip()):
        index += 1
    return index


def declarations_end(lines: list[str], start: int) -> int:
    """Return the index just past the leading variable declarations."""
    index = start
    last_declaration = start
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("set "):
            index += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped) or stripped.startswith("readonly "):
            last_declaration = index + 1
            index += 1
            continue
        break
    return last_declaration


def repair(path: Path) -> list[str]:
    """Repair one script and return a description of the changes."""
    text = path.read_text(encoding="utf-8")
    text, had_bom = clean_bom(text)
    lines = split_owned(text.splitlines())
    changes: list[str] = []
    if had_bom:
        changes.append("removed a byte-order mark")

    index = shebang_index(lines)
    if index < 0:
        changes.append("no shebang found; skipping")
        return changes
    if index != 0:
        # The shebang was pushed down by an earlier bad edit. Restore it to line one.
        shebang = lines.pop(index)
        lines.insert(0, shebang)
        changes.append("restored the shebang to the first line")
    else:
        lines[0] = lines[0].lstrip(BOM)
    for position, line in enumerate(lines[:5]):
        if BOM in line:
            lines[position] = line.replace(BOM, "")

    # The HERE declaration belongs with the header, before anything uses it.
    if HERE_DECLARATION not in lines:
        position = header_end(lines, 0)
        lines.insert(position, "")
        lines.insert(position + 1, HERE_DECLARATION)
        changes.append("inserted the HERE declaration")
    here_index = lines.index(HERE_DECLARATION)

    source_lines = SOURCE_BLOCK.splitlines()
    position = here_index + 1
    for offset, line in enumerate(source_lines):
        lines.insert(position + offset, line)
    changes.append("sourced lib-agentos-verify.sh")

    position = declarations_end(lines, here_index + len(source_lines) + 1)
    lines.insert(position, "")
    lines.insert(position + 1, GUARD_COMMENT)
    lines.insert(position + 2, GUARD_LINE)
    changes.append("added the sudo pre-flight check")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


def main(argv: list[str]) -> int:
    """Repair every target script."""
    target = Path(argv[1]) if len(argv) > 1 else SCRIPTS
    for name in TARGETS:
        path = target / name
        if not path.is_file():
            print(f"  (absent) {name}")
            continue
        changes = repair(path)
        print(f"  {name}: {', '.join(changes)}")

    # A broken shebang produces no output at all, so verify it here.
    problems: list[str] = []
    for name in TARGETS:
        path = target / name
        if not path.is_file():
            continue
        first = path.read_text(encoding="utf-8").splitlines()[0] if path.read_text(encoding="utf-8") else ""
        if not first.startswith("#!"):
            problems.append(f"  {name}: first line is not a shebang: {first!r}")
    if problems:
        print("")
        print("scripts whose first line is not a shebang:")
        for problem in problems:
            print(problem)
        return 1
    print("every target script starts with a shebang")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
