#!/usr/bin/env bash
# Run the full verification chain in WSL and report the stage results.
#
# This exists so that invoking the chain from another shell is one short command
# rather than a string of quoted pipelines, and so that the result summary is always
# produced the same way.
#
# Usage: run-full-verification.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ARTIFACTS="${1:-$ROOT/build/debs}"
LOG="${AGENTOS_LOG:-/tmp/agentos-verification.log}"

export PATH="$HOME/.local/bin:$PATH"
export PYTHONDONTWRITEBYTECODE=1

echo "=== rebuilding from a clean tree ==="
rm -rf /tmp/agentos-build "$ROOT/build"
if ! bash "$ROOT/scripts/build-debs-native.sh" >"$LOG.build" 2>&1; then
  echo "  [FAIL] the build failed; see $LOG.build"
  tail -20 "$LOG.build"
  exit 1
fi
echo "  [ok  ] the build produced $(find "$ROOT/build/debs" -name '*.deb' | wc -l) packages"

echo ""
echo "=== running every verification stage ==="
AGENTOS_SUDO_PASSWORD="${AGENTOS_SUDO_PASSWORD:-}" \
  bash "$ROOT/scripts/verify-all.sh" --with-sudo >"$LOG" 2>&1
status=$?

echo ""
python3 "$ROOT/scripts/summarize-verification.py" "$LOG"
echo ""
echo "full log: $LOG"
echo "build log: $LOG.build"
exit "$status"
