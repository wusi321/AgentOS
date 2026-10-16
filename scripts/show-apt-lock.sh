#!/usr/bin/env bash
# Report what currently holds the dpkg or APT lock, if anything.
#
# A verification run that installs packages can fail with "dpkg frontend lock was
# locked by another process", which is an environment condition rather than a
# package fault. This script answers "who held it" so that a failure can be
# attributed correctly.
#
# Usage: show-apt-lock.sh
set -uo pipefail

echo "=== processes holding an APT or dpkg lock ==="
if command -v fuser >/dev/null 2>&1; then
  for lock in /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock /var/cache/apt/archives/lock; do
    [ -e "$lock" ] || continue
    holders=$(fuser "$lock" 2>/dev/null || true)
    if [ -n "$holders" ]; then
      printf '  %s held by:%s\n' "$lock" "$holders"
      for pid in $holders; do
        printf '    pid %s: %s\n' "$pid" "$(ps -o comm= -p "$pid" 2>/dev/null || echo unknown)"
      done
    fi
  done
fi

echo ""
echo "=== APT and dpkg related processes ==="
ps -eo pid,comm,args 2>/dev/null | grep -E 'unattended|apt-get|apt |dpkg|apt.systemd' | grep -v grep | sed 's/^/  /' || echo "  (none)"

echo ""
echo "=== timers that can run APT unattended ==="
systemctl list-timers --all 2>/dev/null | grep -Ei 'apt|unattended|daily' | sed 's/^/  /' || echo "  (none)"

echo ""
echo "=== the unattended-upgrades configuration ==="
if [ -f /etc/apt/apt.conf.d/20auto-upgrades ]; then
  grep -vE '^\s*(//|$)' /etc/apt/apt.conf.d/20auto-upgrades | sed 's/^/  /'
else
  echo "  /etc/apt/apt.conf.d/20auto-upgrades is absent"
fi
