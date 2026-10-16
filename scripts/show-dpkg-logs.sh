#!/usr/bin/env bash
# Print the dpkg logs a verification stage left behind.
#
# Usage: show-dpkg-logs.sh
set -uo pipefail

for log in /tmp/dpkg-upgrade.log /tmp/dpkg-remove.log /tmp/dpkg-purge.log /tmp/dpkg-install.log; do
  [ -f "$log" ] || continue
  echo "=== $log ==="
  tail -14 "$log" | sed 's/^/  /'
  echo ""
done
