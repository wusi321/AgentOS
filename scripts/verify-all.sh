#!/usr/bin/env bash
# Run every check in the order CI would, and stop at the first real failure.
#
# Usage: verify-all.sh [--with-sudo]
#
#   1. source guard tests        (no environment needed)
#   2. unit, contract, security  (no environment needed)
#   3. packaging metadata        (no environment needed)
#   4. the built packages        (needs a previous build)
#   5. the installed system      (needs root, so it is opt-in)
set -uo pipefail

ROOT="${AGENTOS_REPO:-/mnt/d/桌面/AgentOS}"
WITH_SUDO="${1:-}"
cd "$ROOT"

export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

failures=0
stage() {
  local label="$1"; shift
  echo ""
  echo "=============================================================="
  echo "== $label"
  echo "=============================================================="
  if "$@"; then
    echo "-- passed: $label"
  else
    echo "-- FAILED: $label"
    failures=$((failures + 1))
  fi
}

stage "python syntax" python3 -m compileall -q -f src/agentos
stage "python 3.10 compatibility" python3 scripts/check-python-compat.py --target 3.10 "$ROOT"
stage "source and unit tests" python3 -m pytest -q tests
stage "packaging metadata" python3 scripts/check-packaging.py
stage "maintainer script arguments" python3 scripts/check-maintainer-arguments.py "$ROOT"
stage "packaged maintainer scripts" bash scripts/check-packaged-scripts.sh "$ROOT/build/debs"
stage "built packages" bash scripts/verify-debs.sh "$ROOT/build/debs"
stage "end-to-end smoke test" python3 scripts/smoke-core.py

if [ "$WITH_SUDO" = "--with-sudo" ]; then
  stage "installed system" bash scripts/verify-installed.sh "$ROOT/build/debs"
  stage "data retention across removal" bash scripts/verify-data-retention.sh "$ROOT/build/debs"
  AGENTOS_SUDO_PASSWORD="${AGENTOS_SUDO_PASSWORD:-}" \
    stage "live system" bash scripts/verify-live.sh "$ROOT/build/debs"
fi

echo ""
echo "=============================================================="
if [ "$failures" -eq 0 ]; then
  echo "== all stages passed"
  exit 0
fi
echo "== $failures stage(s) failed"
exit 1
