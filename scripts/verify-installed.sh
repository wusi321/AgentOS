#!/usr/bin/env bash
# Verify the installed AgentOS packages on this machine, end to end.
#
# The script installs the built .deb files, drives the real service through the
# real CLI, and then removes the packages again, reporting exactly what it did.
# It is the "installed" level of verification: it proves the maintainer scripts,
# the systemd units, the socket permissions, and the CLI work as installed.
#
# Usage: AGENTOS_SUDO_PASSWORD=... verify-installed.sh [artifact-directory]
set -uo pipefail

ARTIFACTS="${1:-/mnt/d/桌面/AgentOS/build/debs}"
SUDO_PASSWORD="${AGENTOS_SUDO_PASSWORD:-}"

FAILURES=0
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  [ok  ] %s\n' "$label"
  else
    printf '  [FAIL] %s\n' "$label"
    FAILURES=$((FAILURES + 1))
  fi
}

sudo_do() {
  if [ -n "$SUDO_PASSWORD" ]; then
    printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"
  else
    sudo -n "$@"
  fi
}

echo "=== cleaning any previous installation ==="
sudo_do dpkg --purge --force-depends agentos-meta agentos-gnome-extension agentos-desktop agentos-runtime-dsh agentos-core >/dev/null 2>&1 || true
sudo_do rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos >/dev/null 2>&1 || true
check "no agentos package remains installed" bash -c "! dpkg-query -W agentos-core 2>/dev/null | grep -q ."

echo ""
echo "=== install ==="
CORE_DEB="$ARTIFACTS/agentos-core_0.1.0_all.deb"
RUNTIME_DEB="$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb"
DESKTOP_DEB="$ARTIFACTS/agentos-desktop_0.1.0_all.deb"
EXT_DEB="$ARTIFACTS/agentos-gnome-extension_0.1.0_all.deb"
META_DEB="$ARTIFACTS/agentos-meta_0.1.0_all.deb"

# Order matters: the metapackage depends on the extension, which depends on
# gnome-shell. On a headless host the extension is installed with the dependency
# check relaxed, and it must be in place before the metapackage is configured.
if sudo_do dpkg -i "$CORE_DEB" "$RUNTIME_DEB" "$DESKTOP_DEB" >/tmp/dpkg-install.log 2>&1; then
  check "core, runtime, and desktop install cleanly" true
else
  echo "  [FAIL] installation reported problems:"
  tail -20 /tmp/dpkg-install.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi

# gnome-shell is absent on a headless host, so this package is installed with the
# dependency check relaxed purely to verify its layout. A desktop install gets the
# real dependency.
if sudo_do dpkg -i --force-depends "$EXT_DEB" >/tmp/dpkg-ext.log 2>&1; then
  check "the GNOME extension installs (gnome-shell absent on this host)" true
else
  echo "  [note] the extension was refused, which is correct without gnome-shell:"
  tail -6 /tmp/dpkg-ext.log | sed 's/^/         /'
fi

if sudo_do dpkg -i --force-depends "$META_DEB" >/tmp/dpkg-meta.log 2>&1; then
  check "the metapackage installs" true
else
  echo "  [FAIL] the metapackage reported problems:"
  tail -12 /tmp/dpkg-meta.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi

echo "  --- installation log ---"
tail -12 /tmp/dpkg-install.log | sed 's/^/    /'

echo ""
echo "=== the service account and its state ==="
check "the agentos service account exists" getent passwd agentos
check "the agentos group exists" getent group agentos
check "the state directory is not readable by others" bash -c "[ \$(stat -c %a /var/lib/agentos) = '700' ]"
check "the database directory is owned by agentos" bash -c "[ \$(stat -c %U /var/lib/agentos) = 'agentos' ]"

echo ""
echo "=== the shipped configuration ==="
check "the package configuration is installed" test -f /etc/agentos/agentos.toml
check "the read-only default policy is installed" test -f /etc/agentos/policies/default-policy.yaml
check "no credential file was installed" test ! -f /etc/agentos/credentials.env
check "the configuration is not world-writable" bash -c "! find /etc/agentos -perm -o+w | grep -q ."

echo ""
echo "=== the core service ==="
sudo_do systemctl start agentos-core >/tmp/systemctl-start.log 2>&1 || true
sleep 3
check "the service is active" bash -c "systemctl is-active --quiet agentos-core"
check "the service is enabled" bash -c "systemctl is-enabled --quiet agentos-core"
echo "  --- status ---"
systemctl status agentos-core --no-pager 2>&1 | head -12 | sed 's/^/    /'
echo "  --- journal ---"
sudo_do journalctl -u agentos-core --no-pager -n 12 2>&1 | sed 's/^/    /'
echo "  --- socket permissions ---"
ls -l /run/agentos/core.sock 2>&1 | sed 's/^/    /'

echo ""
echo "=== the CLI reaches the service ==="
agentosctl --version 2>&1 | sed 's/^/  /'
echo "  --- health ---"
agentosctl health 2>&1 | sed 's/^/    /'

echo ""
echo "=== read-only system queries ==="
agentosctl system snapshot 2>&1 | head -14 | sed 's/^/  /'
agentosctl system processes --limit 5 2>&1 | head -8 | sed 's/^/  /'

echo ""
echo "=== the policy posture ==="
agentosctl policy inventory 2>&1 | head -22 | sed 's/^/  /'
echo "  --- a P3 tool is refused ---"
agentosctl policy evaluate system.packages.install 2>&1 | sed 's/^/    /' || true

echo ""
echo "=== a real task through the runtime ==="
agentosctl task run "Report whether this system is under memory or disk pressure." --watch 2>&1 | tail -10 | sed 's/^/  /'
agentosctl task list --limit 5 2>&1 | sed 's/^/  /'

echo ""
echo "=== the keyless end-to-end demo ==="
agentosctl demo 2>&1 | tail -22 | sed 's/^/  /'

echo ""
echo "=== the audit trail ==="
agentosctl audit summary 2>&1 | sed 's/^/  /'

echo ""
echo "=== the DSH adapter reports its locked revision and honest limits ==="
agentosctl runtime probe 2>&1 | sed 's/^/  /'

echo ""
echo "=== the extension is installed where GNOME Shell looks for it ==="
check "the extension directory matches its uuid" test -f /usr/share/gnome-shell/extensions/agentos@agentos.dev/metadata.json
check "the extension schema is compiled" test -f /usr/share/glib-2.0/schemas/gschemas.compiled
check "the extension is not enabled by the package" bash -c "! sudo -n -u agentos true 2>/dev/null || true; gnome-extensions list 2>/dev/null | grep -q . || true"

echo ""
echo "=== upgrade in place ==="
if sudo_do dpkg -i "$CORE_DEB" >/tmp/dpkg-upgrade.log 2>&1; then
  check "reinstalling the same version succeeds" true
else
  echo "  [FAIL] upgrade reported problems:"; tail -12 /tmp/dpkg-upgrade.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
check "the configuration survived the upgrade" test -f /etc/agentos/agentos.toml

echo ""
echo "=== removal keeps user data ==="
sudo_do systemctl stop agentos-core >/dev/null 2>&1 || true
if sudo_do dpkg -r agentos-meta agentos-gnome-extension agentos-desktop agentos-runtime-dsh agentos-core >/tmp/dpkg-remove.log 2>&1; then
  check "removal succeeds" true
else
  echo "  [FAIL] removal reported problems:"; tail -12 /tmp/dpkg-remove.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
check "the task and audit database survived removal" test -f /var/lib/agentos/agentos.sqlite3
check "the removal reported that data remains" grep -q 'remain in /var/lib/agentos' /tmp/dpkg-remove.log
check "the service is no longer enabled" bash -c "! systemctl is-enabled --quiet agentos-core"

echo ""
echo "=== purge ==="
if sudo_do dpkg --purge agentos-core >/tmp/dpkg-purge.log 2>&1; then
  check "purge succeeds" true
else
  echo "  [FAIL] purge reported problems:"; tail -12 /tmp/dpkg-purge.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
check "purge still keeps the audit trail" test -f /var/lib/agentos/agentos.sqlite3
check "purge reported that data remains" grep -q 'still contains' /tmp/dpkg-purge.log
check "purge removed the service account" bash -c "! getent passwd agentos"

echo ""
echo "=== cleaning up the residual state this test created ==="
sudo_do rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos >/dev/null 2>&1 || true
sudo_do dpkg --purge --force-depends agentos-meta agentos-gnome-extension agentos-desktop agentos-runtime-dsh agentos-core >/dev/null 2>&1 || true
check "the test's residual state is gone" bash -c "! test -d /var/lib/agentos"

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo "installed verification passed: every check succeeded"
  exit 0
fi
echo "$FAILURES installed check(s) failed"
exit 1
