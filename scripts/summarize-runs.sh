#!/usr/bin/env bash
# Summarize several verification logs at once, to show reproducibility.
#
# A single passing run is weak evidence for a chain that installs packages. This
# prints the stage results of every log it is given, so a failure that appears in one
# run out of three is visible rather than hidden by the last run's success.
#
# Usage: summarize-runs.sh <log> [log...]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [ "$#" -eq 0 ]; then
  echo "usage: summarize-runs.sh <log> [log...]" >&2
  exit 2
fi

for log in "$@"; do
  echo "################ $log ################"
  if [ ! -f "$log" ]; then
    echo "  (no such log)"
    continue
  fi
  python3 "$ROOT/scripts/summarize-verification.py" "$log"
  echo ""
done
