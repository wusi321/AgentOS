#!/usr/bin/env bash
# Reproduce the exact removal sequence that loses the database, step by step.
#
# Usage: AGENTOS_SUDO_PASSWORD=... reproduce-state-loss.sh [artifact-directory]
set -uo pipefail

ARTIFACTS="${1:-/mnt/d/桌面/AgentOS/build/debs}"
SUDO_PASSWORD="${AGENTOS_SUDO_PASSWORD:-}"
sudo_do() {
  if [ -n "$SUDO_PASSWORD" ]; then
    printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"
  else
    sudo -n "$@"
  fi
}

show() {
  printf '  %-34s ' "$1"
  if sudo_do test -e /var/lib/agentos/agentos.sqlite3; then
    printf 'database present\n'
  elif sudo_do test -d /var/lib/agentos; then
    printf 'directory present, database MISSING: %s\n' "$(sudo_do ls -1 /var/lib/agentos 2>/dev/null | tr '\n' ' ')"
  else
    printf 'directory MISSING\n'
  fi
}

echo "=== clean install ==="
sudo_do dpkg --purge --force-all agentos-meta agentos-gnome-extension agentos-desktop \
     agentos-runtime-dsh agentos-core >/dev/null 2>&1 || true
sudo_do rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos
sudo_do dpkg -i "$ARTIFACTS/agentos-core_0.1.0_all.deb" "$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb" \
     "$ARTIFACTS/agentos-desktop_0.1.0_all.deb" >/dev/null 2>&1
show "after install"

echo ""
echo "=== start and use the service ==="
sudo_do systemctl start agentos-core
sleep 3
show "after start"
sg agentos -c 'agentosctl task run "state check" --watch' >/dev/null 2>&1 || true
sleep 1
show "after running a task"
sudo_do ls -la /var/lib/agentos | sed 's/^/    /'

echo ""
echo "=== stop the service ==="
sudo_do systemctl stop agentos-core
sleep 2
show "after systemctl stop"

echo ""
echo "=== disable the unit, as dpkg's prerm does ==="
sudo_do systemctl disable agentos-core >/dev/null 2>&1 || true
show "after systemctl disable"

echo ""
echo "=== remove the packages ==="
sudo_do dpkg -r agentos-desktop agentos-runtime-dsh agentos-core 2>&1 | tail -8 | sed 's/^/    /'
show "after dpkg -r"

echo ""
echo "=== purge ==="
sudo_do dpkg --purge agentos-core 2>&1 | tail -6 | sed 's/^/    /'
show "after dpkg --purge"

echo ""
echo "=== the state directory contents, in full ==="
sudo_do ls -la /var/lib/agentos 2>&1 | sed 's/^/  /'
