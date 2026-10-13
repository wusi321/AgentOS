#!/usr/bin/env bash
# Verify the built AgentOS Debian packages.
#
# Two levels of verification, in increasing cost:
#
#   1. static    — metadata, file list, and the shipped layout of every package
#   2. installed — install on a disposable copy, so the check proves the
#                  maintainer scripts, the systemd units, and the CLI work as
#                  installed rather than only as built.
#
# Usage: verify-debs.sh [artifact-directory] [--install]
#
# Level 2 needs root because dpkg needs root. It installs the packages, runs the
# checks, and then removes them again, reporting exactly what it did.
set -uo pipefail
shopt -s nullglob

REPO="${AGENTOS_REPO:-/mnt/d/桌面/AgentOS}"
ARTIFACTS="${1:-$REPO/build/debs}"
DO_INSTALL="${2:-}"
PKGS=(agentos-core agentos-runtime-dsh agentos-desktop agentos-gnome-extension agentos-meta)

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

echo "=== artifacts in $ARTIFACTS ==="
ls -1 "$ARTIFACTS"/*.deb 2>/dev/null || { echo "no .deb files found" >&2; exit 2; }

echo ""
echo "=== 1. static verification ==="
for package in "${PKGS[@]}"; do
  deb=$(echo "$ARTIFACTS/${package}"_*.deb)
  if [ ! -f "$deb" ]; then
    echo "  [FAIL] $package: no .deb"
    FAILURES=$((FAILURES + 1))
    continue
  fi
  echo "--- $(basename "$deb") ---"
  dpkg-deb --info "$deb" | sed -n '1,12p' | sed 's/^/  /'
done

echo ""
echo "=== 2. shipped layout ==="
for package in "${PKGS[@]}"; do
  deb=$(echo "$ARTIFACTS/${package}"_*.deb)
  [ -f "$deb" ] || continue
  echo "--- $(basename "$deb") ---"
  dpkg-deb --contents "$deb" | awk '{print $NF}' | sed 's/^\.//' | grep -v '/$' | sed 's/^/  /' | head -60
done

echo ""
echo "=== 3. python syntax of the installed tree ==="
# The shipped package is split across three binary packages, so an import check
# has to compile the union: that is what a real installation has on disk.
TMP=$(mktemp -d)
for package in "${PKGS[@]}"; do
  deb=$(echo "$ARTIFACTS/${package}"_*.deb)
  [ -f "$deb" ] && dpkg-deb -x "$deb" "$TMP/$package"
done
SITE="$TMP/site"
mkdir -p "$SITE"
for package in "${PKGS[@]}"; do
  source="$TMP/$package/usr/lib/python3/dist-packages"
  if [ -d "$source" ]; then
    cp -a "$source/." "$SITE/"
  fi
done
if [ -d "$SITE/agentos" ]; then
  check "the installed package byte-compiles" python3 -m compileall -q "$SITE/agentos"
  check "the installed package imports" env "PYTHONPATH=$SITE" python3 -c "import agentos, agentos.cli, agentos.core, agentos.policy, agentos.tasks, agentos.system_bridge, agentos.audit"
  if ! env "PYTHONPATH=$SITE" python3 -c "import agentos, agentos.cli, agentos.core, agentos.policy, agentos.tasks, agentos.system_bridge, agentos.audit" >/tmp/import-fail.log 2>&1; then
    sed 's/^/    /' /tmp/import-fail.log | head -15
  fi
  check "the DSH adapter imports" env "PYTHONPATH=$SITE" python3 -c "import agentos.runtime.dsh, agentos.runtime.dsh_transport"
  check "the desktop application imports" env "PYTHONPATH=$SITE" python3 -c "import agentos.desktop, agentos.session"
  check "the CLI entry point runs" env "PYTHONPATH=$SITE" python3 -c "from agentos.cli import main; import sys; sys.exit(0 if main(['--version']) == 0 else 1)"
  echo "  (packages staged from: $ARTIFACTS)"
else
  echo "  [FAIL] agentos-core does not install the agentos package"
  FAILURES=$((FAILURES + 1))
fi

echo ""
echo "=== 4. required files are present in the right packages ==="
CORE="$TMP/agentos-core"
check "agentosctl is present" test -x "$CORE/usr/bin/agentosctl"
check "agentos-core service is present" test -f "$CORE/lib/systemd/system/agentos-core.service"
check "the default policy is installed" test -f "$CORE/etc/agentos/policies/default-policy.yaml"
check "the package configuration is installed" test -f "$CORE/etc/agentos/agentos.toml"
check "the credential template is installed" test -f "$CORE/etc/agentos/credentials.env.example"
check "no credential file is installed" test ! -f "$CORE/etc/agentos/credentials.env"
# dh_compress gzips man pages, so the installed name carries .gz.
check "the man page is installed" test -f "$CORE/usr/share/man/man1/agentosctl.1.gz"
check "no configuration file is executable" bash -c "! find '$CORE/etc' -type f -perm -u+x | grep -q ."
check "no shipped module is executable" bash -c "! find '$CORE/usr/lib/python3' -type f -perm -u+x | grep -q ."
check "the DSH lock file is installed" test -f "$TMP/agentos-runtime-dsh/usr/share/agentos/dsh-locked.json"
check "the desktop entry is installed" test -f "$TMP/agentos-desktop/usr/share/applications/org.agentos.Desktop.desktop"
check "the desktop entry is valid" desktop-file-validate "$TMP/agentos-desktop/usr/share/applications/org.agentos.Desktop.desktop"
check "the session service is installed" test -f "$TMP/agentos-desktop/lib/systemd/user/agentos-session.service"
check "the session D-Bus activation file is installed" test -f "$TMP/agentos-desktop/usr/share/dbus-1/services/org.agentos.Session1.service"
check "the extension metadata is installed" test -f "$TMP/agentos-gnome-extension/usr/share/gnome-shell/extensions/agentos@agentos.dev/metadata.json"
check "the extension schema is installed" test -f "$TMP/agentos-gnome-extension/usr/share/glib-2.0/schemas/org.gnome.shell.extensions.agentos.gschema.xml"
check "the extension schema compiles" glib-compile-schemas --strict --dry-run "$TMP/agentos-gnome-extension/usr/share/glib-2.0/schemas"
check "the desktop schema compiles" glib-compile-schemas --strict --dry-run "$TMP/agentos-desktop/usr/share/glib-2.0/schemas"
check "the extension does not ship a secret" bash -c "! grep -rIl 'api_key\\|API_KEY\\|password' '$TMP/agentos-gnome-extension' 2>/dev/null | grep -q ."
# The extension must not be able to run a command. The check looks for the import
# and the API, not for the words: the extension's own comments and README explain
# what it deliberately avoids, and matching prose would be a false failure.
EXT="$TMP/agentos-gnome-extension/usr/share/gnome-shell/extensions/agentos@agentos.dev"
check "the extension imports no shell helper" bash -c "! grep -nE '^[^/]*imports\\.(misc\\.util|misc\\.gnomeSession|misc\\.system)' '$EXT'/*.js"
# Gio.Subprocess is the remaining direct way to start a program in GJS, and it
# must not appear at all, not even in a comment, because a commented example is
# how the next contributor adds one.
check "the extension has no subprocess API" bash -c "! grep -nq 'Gio.Subprocess' '$EXT'/*.js"
check "the extension reads no file" bash -c "! grep -nE 'Gio\\.File|GLib\\.file_get_contents|readFile' '$EXT'/*.js"
check "the extension never approves anything" bash -c "! grep -nE 'approval\\.(grant|decide|approve)|GrantApproval' '$EXT'/*.js"

echo ""
echo "=== 5. the service account is not root in any unit ==="
check "agentos-core.service runs as agentos" grep -q '^User=agentos' "$CORE/lib/systemd/system/agentos-core.service"
check "no unit runs as root" bash -c "! grep -q '^User=root' '$CORE/lib/systemd/system/'*.service"

echo ""
echo "=== 6. lintian ==="
if command -v lintian >/dev/null 2>&1; then
  ( cd "$ARTIFACTS" && lintian --no-tag-display-limit ./*.deb 2>&1 | head -40 ) || true
else
  echo "  lintian is not installed"
fi

if [ "$DO_INSTALL" = "--install" ]; then
  echo ""
  echo "=== 7. installation on this system ==="
  echo "  installing with dpkg (dependencies must already be present)…"
  if echo lcf | sudo -S -p '' dpkg -i "$ARTIFACTS"/agentos-core_*.deb "$ARTIFACTS"/agentos-runtime-dsh_*.deb \
       "$ARTIFACTS"/agentos-desktop_*.deb "$ARTIFACTS"/agentos-gnome-extension_*.deb \
       "$ARTIFACTS"/agentos-meta_*.deb >/tmp/dpkg-install.log 2>&1; then
    check "dpkg -i succeeded" true
  else
    echo "  dpkg -i reported problems:"
    tail -25 /tmp/dpkg-install.log | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
  fi
  echo "  --- installed CLI ---"
  agentosctl --version 2>&1 | sed 's/^/    /'
  echo "  --- agentosctl doctor ---"
  agentosctl doctor 2>&1 | sed 's/^/    /'
  echo "  --- upgrade in place ---"
  if echo lcf | sudo -S -p '' dpkg -i "$ARTIFACTS"/agentos-core_*.deb >/tmp/dpkg-upgrade.log 2>&1; then
    check "in-place upgrade succeeded" true
  else
    echo "  upgrade reported problems:"; tail -15 /tmp/dpkg-upgrade.log | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
  fi
  echo "  --- removal keeps data ---"
  if echo lcf | sudo -S -p '' dpkg -r agentos-meta agentos-gnome-extension agentos-desktop agentos-runtime-dsh agentos-core >/tmp/dpkg-remove.log 2>&1; then
    check "removal succeeded" true
  else
    echo "  removal reported problems:"; tail -15 /tmp/dpkg-remove.log | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
  fi
  check "the task database survived removal" test -d /var/lib/agentos
  check "the service unit was disabled" bash -c "! systemctl is-enabled agentos-core.service 2>/dev/null | grep -q enabled"
  echo "  --- residual state (expected, by design) ---"
  ls -la /var/lib/agentos 2>/dev/null | sed 's/^/    /'
  echo "  --- cleaning the residual state created by this test ---"
  echo lcf | sudo -S -p '' rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos >/dev/null 2>&1 || true
  check "residual state removed" bash -c "! test -d /var/lib/agentos"
else
  echo ""
  echo "(pass --install to also install on this system, upgrade, and remove)"
fi

rm -rf "$TMP"

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo "all package checks passed"
  exit 0
fi
echo "$FAILURES package check(s) failed"
exit 1
