"""Service entry point: ``python3 -m agentos`` runs the Core service."""

from __future__ import annotations

import sys

from .core import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
