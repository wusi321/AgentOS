#!/usr/bin/env bash
# Run AgentOS checks inside WSL. See scripts/wsl-apt-setup.sh for build dependencies.
#
# Usage: wsl-checks.sh <repo-root-unix-path> [command]
#   command: check | compile | test | doctor
set -euo pipefail

ROOT="${1:?usage: wsl-checks.sh <repo-root> [check|compile|test|doctor]}"
COMMAND="${2:-check}"
cd "$ROOT"

export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

case "$COMMAND" in
  compile)
    python3 -m compileall -q -f src/agentos >/dev/null
    echo "COMPILE-OK $(python3 --version)"
    ;;
  check)
    python3 - <<'PY'
import importlib
import pkgutil
import sys

import agentos

failures = []
for module in pkgutil.walk_packages(agentos.__path__, prefix="agentos."):
    try:
        importlib.import_module(module.name)
    except Exception as error:  # noqa: BLE001 - reporting is the point
        failures.append(f"{module.name}: {type(error).__name__}: {error}")
if failures:
    print("IMPORT-FAILURES:")
    for item in failures:
        print("  " + item)
    sys.exit(1)
print("IMPORT-OK")
PY
    ;;
  test)
    python3 -m pytest -q tests "$@"
    ;;
  doctor)
    python3 -m agentos.cli doctor
    ;;
  *)
    echo "unknown command: $COMMAND" >&2
    exit 2
    ;;
esac
