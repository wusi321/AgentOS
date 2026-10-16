#!/usr/bin/env bash
# Print one stage's section from a verify-all log, keeping only the check lines.
#
# Usage: show-stage.sh <log> <stage label substring>
set -uo pipefail

LOG="${1:?usage: show-stage.sh <log> <label>}"
LABEL="${2:?missing label}"
[ -f "$LOG" ] || { echo "no such log: $LOG" >&2; exit 2; }

awk -v label="$LABEL" '
  /^== / { printing = index($0, label) > 0 }
  printing { print }
' "$LOG" | grep -E 'FAIL|\[ok|\[note|^== |verified|passed|failed'
