#!/usr/bin/env python3
"""Clear Python bytecode caches under the repository (upstream excluded).

A stale ``__pycache__`` can mask a source change during development, which is
exactly the kind of false signal that wastes an afternoon. The test runner calls
this before collecting, and it is safe to run at any time.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED = {"upstream", ".git", "node_modules", "build"}


def main() -> int:
    """Remove every ``__pycache__`` directory outside the excluded trees."""
    removed = 0
    for path in ROOT.rglob("__pycache__"):
        if any(part in EXCLUDED for part in path.relative_to(ROOT).parts):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    print(f"removed {removed} bytecode cache director{'y' if removed == 1 else 'ies'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
