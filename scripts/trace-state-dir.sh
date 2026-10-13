#!/usr/bin/env bash
# Determine exactly when /var/lib/agentos disappears.
#
# The verification found the database missing after `dpkg -r`, so this walks the
# lifecycle one step at a time and reports the state directory after each step. The
# answer decides whether the package, the unit, or something else is responsible.
#
# Usage: AGENTOS_SUDO_PASSWORD=... trace-state-dir.sh [artifact-directory]
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

report() {
  local label="$1"
  if sudo_do test -d /var/lib/agentos; then
    printf '  [present] %-40s %s\n' "$label" "$(sudo_do ls -1 /var/lib/agentos 2>/dev/null | tr '\n' ' ')"
  else
    printf '  [MISSING] %-40s\n' "$label"
  fi
}

echo "=== start from nothing ==="
sudo_do dpkg --purge --force-all agentos-meta agentos-gnome-extension agentos-desktop \
     agentos-runtime-dsh agentos-core >/dev/null 2>&1 || true
sudo_do rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos
report "after purge and cleanup"

echo ""
echo "=== install ==="
sudo_do dpkg -i "$ARTIFACTS/agentos-core_0.1.0_all.deb" >/dev/null 2>&1
report "after dpkg -i"

echo ""
echo "=== start the service ==="
sudo_do systemctl start agentos-core
sleep 3
report "after systemctl start"

echo ""
echo "=== stop the service ==="
sudo_do systemctl stop agentos-core
sleep 2
report "after systemctl stop"

echo ""
echo "=== postrm with remove, run by hand ==="
WORK=$(mktemp -d)
dpkg-deb --ctrl-tarfile "$ARTIFACTS/agentos-core_0.1.0_all.deb" | tar -x -C "$WORK"
sudo_do sh "$WORK/postrm" remove
report "after postrm remove"

echo ""
echo "=== postrm with purge, run by hand ==="
sudo_do sh "$WORK/postrm" purge
report "after postrm purge"
rm -rf "$WORK"

echo ""
echo "=== which packaged files mention the state directory ==="
for file in postinst prerm postrm; do
  echo "--- $file ---"
  WORK=$(mktemp -d)
  dpkg-deb --ctrl-tarfile "$ARTIFACTS/agentos-core_0.1.0_all.deb" | tar -x -C "$WORK"
  grep -n 'var/lib/agentos\|rm -rf\|rmdir' "$WORK/$file" 2>/dev/null | sed 's/^/    /' || echo "    (no reference)"
  rm -rf "$WORK"
done

echo ""
echo "=== the unit's state directory declaration ==="
grep -nE 'StateDirectory|RuntimeDirectory' /usr/lib/systemd/system/agentos-core.service 2>/dev/null | sed 's/^/  /'
systemctl show agentos-core -p StateDirectory -p StateDirectoryMode -p RuntimeDirectory 2>/dev/null | sed 's/^/  /'
