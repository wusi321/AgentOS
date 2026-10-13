#!/usr/bin/env bash
# Run the AgentOS test suite inside WSL (or any Ubuntu host).
#
# Usage: run-tests.sh [repo-root] [pytest args...]
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
shift || true
cd "$ROOT"

export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

echo "python: $(python3 --version)"
echo "pytest: $(python3 -m pytest --version 2>&1 | head -1)"

missing=()
python3 -c "import pytest_asyncio" 2>/dev/null || missing+=("pytest-asyncio")
python3 -c "import psutil" 2>/dev/null || true

if [ "${#missing[@]}" -gt 0 ]; then
  echo "installing missing test dependencies: ${missing[*]}"
  echo "lcf" | sudo -S -p '' apt-get install -y -qq python3-pytest-asyncio >/dev/null 2>&1 || \
    python3 -m pip install --quiet --break-system-packages pytest-asyncio
fi

exec python3 -m pytest "$@"
