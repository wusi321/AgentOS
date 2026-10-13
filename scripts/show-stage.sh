#!/usr/bin/env bash
# Show one stage's section from a verify-all.log run.
#
# Usage: show-stage.sh <log> <stage label substring>
set -uo pipefail

LOG="${1:?usage: show-stage.sh <log> <label>}"
LABEL="${2:?missing label}"

if [ ! -f "$LOG" ]; then
  echo "no such log: $LOG" >&2
  exit 2
fi

awk -v label="$LABEL" '
  /^== / { printing = index($0, label) > 0 }
  printing { print }
' "$LOG"
