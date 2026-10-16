#!/usr/bin/env python3
"""Repair the default paths in the shell helpers and make them location-independent.

Two problems are fixed here.

1. **Corrupted non-ASCII paths.** Editing some scripts through a Windows PowerShell
   pipeline mangled the UTF-8 characters in a default path such as
   ``/mnt/d/<workspace>/AgentOS``. A corrupted default is a latent failure for
   anyone who runs the script without an argument.

2. **Hard-coded absolute paths.** A script should not need to know where the
   repository lives. Every default is therefore derived from the script's own
   location, so the helpers work from any checkout path and any working directory.

The rewrite is idempotent: running it again changes nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: Declarations that should resolve relative to the script rather than an absolute path.
REPLACEMENTS: tuple[tuple[str, str], ...] = (
    # Artifact directory: <script dir>/../build/debs
    (
        r'^ARTIFACTS="\$\{1:-[^}]*\}"$',
        'ARTIFACTS="${1:-$HERE/../build/debs}"',
    ),
    # Repository root: <script dir>/..
    (
        r'^ROOT="\$\{AGENTOS_REPO:-[^}]*\}"$',
        'ROOT="${AGENTOS_REPO:-$(cd "$HERE/.." && pwd)}"',
    ),
    (
        r'^REPO_SOURCE="\$\{AGENTOS_REPO:-[^}]*\}"$',
        'REPO_SOURCE="${AGENTOS_REPO:-$(cd "$HERE/.." && pwd)}"',
    ),
    (
        r'^REPO="\$\{AGENTOS_REPO:-[^}]*\}"$',
        'REPO="${AGENTOS_REPO:-$(cd "$HERE/.." && pwd)}"',
    ),
    (
        r'^DIR="\$\{1:-[^}]*\}"$',
        'DIR="${1:-$HERE/../build/debs}"',
    ),
)

#: The lines that must exist for the replacements above to be meaningful.
HERE_DECLARATION = 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'


def ensure_here(text: str) -> str:
    """Insert the ``HERE`` declaration after the shebang block if it is absent."""
    if HERE_DECLARATION in text:
        return text
    lines = text.splitlines()
    insert_at = 0
    in_header = True
    for index, line in enumerate(lines):
        if line.startswith("#!"):
            insert_at = index + 1
            continue
        if in_header and line.startswith("#"):
            insert_at = index + 1
            continue
        in_header = False
        break
    lines.insert(insert_at, "")
    lines.insert(insert_at + 1, HERE_DECLARATION)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def repair(path: Path) -> list[str]:
    """Repair one script and return the changes made."""
    original = path.read_text(encoding="utf-8")
    text = original
    changes: list[str] = []
    for pattern, replacement in REPLACEMENTS:
        updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
        if count:
            for match in re.finditer(pattern, text, flags=re.MULTILINE):
                changes.append(f"  {path.name}: {match.group(0)}  ->  {replacement}")
            text = updated
    if text != original:
        text = ensure_here(text)
        path.write_text(text, encoding="utf-8")
    return changes


def main(argv: list[str]) -> int:
    """Repair every shell helper under ``scripts/``."""
    target = Path(argv[1]) if len(argv) > 1 else SCRIPTS
    all_changes: list[str] = []
    for script in sorted(target.glob("*.sh")):
        all_changes.extend(repair(script))
    if all_changes:
        print("updated:")
        for change in all_changes:
            print(change)
    else:
        print("no script needed a default-path repair")

    # Report anything that still looks like a corrupted absolute path.
    suspicious: list[str] = []
    for script in sorted(target.glob("*.sh")):
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"/mnt/[a-z]/", line) and "AGENTOS_REPO" not in line:
                suspicious.append(f"  {script.name}:{number}: {line.strip()}")
    if suspicious:
        print("")
        print("lines that still contain an absolute mount path (review these):")
        for item in suspicious:
            print(item)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
