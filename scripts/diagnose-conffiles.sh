#!/usr/bin/env bash
# Determine why the conffiles of agentos-core are missing from /etc after an install.
#
# The evidence: /etc/agentos exists with only the `private/` directory that the
# postinst creates, dpkg's conffile records still list all four configuration files,
# and `dpkg-query -L` says the package ships them. So dpkg unpacked the directory but
# not the files, or something removed them afterwards. This script distinguishes
# those possibilities instead of guessing.
#
# Usage: AGENTOS_SUDO_PASSWORD=... diagnose-conffiles.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

ARTIFACTS="${1:-$HERE/../build/debs}"
CORE_DEB="$ARTIFACTS/agentos-core_0.1.0_all.deb"

agentos_require_sudo || exit 3

echo "=== 1. are the conffiles actually inside the .deb? ==="
dpkg-deb --contents "$CORE_DEB" 2>&1 | grep -E 'etc/agentos' | sed 's/^/  /'
echo "  --- extracting them to prove the archive has content ---"
TMP=$(mktemp -d)
dpkg-deb --extract "$CORE_DEB" "$TMP"
find "$TMP/etc" -type f -printf '  %s bytes  %p\n' 2>/dev/null
echo "  --- the pristine agentos.toml, first 3 lines ---"
head -3 "$TMP/etc/agentos/agentos.toml" 2>&1 | sed 's/^/    /'
rm -rf "$TMP"

echo ""
echo "=== 2. what dpkg's database says ==="
agentos_sudo dpkg --status agentos-core 2>/dev/null | grep -E '^(Status|Conffiles)' -A6 | sed 's/^/  /'

echo ""
echo "=== 3. fresh install, watching /etc/agentos at each step ==="
agentos_purge "${AGENTOS_PACKAGES[@]}"
agentos_reset_state
printf '  after purge:            '; agentos_sudo ls -1 /etc/agentos 2>&1 | tr '\n' ' '; echo ""
agentos_install_built_packages "$CORE_DEB" >/tmp/conffile-install.log 2>&1
printf '  after install:          '; agentos_sudo ls -1 /etc/agentos 2>&1 | tr '\n' ' '; echo ""
printf '  dpkg unpack output:     '; grep -cE 'Unpacking|Selecting' /tmp/conffile-install.log

echo ""
echo "=== 4. does a reinstall restore them? ==="
agentos_install_built_packages --reinstall "$CORE_DEB" >/tmp/conffile-reinstall.log 2>&1
printf '  after reinstall:        '; agentos_sudo ls -1 /etc/agentos 2>&1 | tr '\n' ' '; echo ""

echo ""
echo "=== 5. does a forced conffile overwrite restore them? ==="
agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" -o Dpkg::Options::=--force-confmiss install -y --reinstall "$CORE_DEB" \
  >/tmp/conffile-force.log 2>&1
printf '  after --force-confmiss: '; agentos_sudo ls -1 /etc/agentos 2>&1 | tr '\n' ' '; echo ""

echo ""
echo "=== 6. what dpkg recorded, package by package ==="
agentos_sudo dpkg --status agentos-core 2>/dev/null | sed -n '/^Conffiles/,/^[A-Z]/p' | sed 's/^/  /'

echo ""
echo "=== 7. is another package or hook touching /etc/agentos? ==="
echo "  --- files in the core package that mention /etc/agentos ---"
dpkg-deb --ctrl-tarfile "$CORE_DEB" 2>/dev/null | tar -t 2>/dev/null | sed 's/^/    /'
echo "  --- the postinst, verbatim ---"
WORK=$(mktemp -d)
dpkg-deb --ctrl-tarfile "$CORE_DEB" | tar -x -C "$WORK" 2>/dev/null
grep -n 'etc/agentos' "$WORK/postinst" | sed 's/^/    /'
echo "  --- does any maintainer script remove configuration? ---"
grep -nE 'rm .*/etc/agentos' "$WORK"/* 2>/dev/null | sed 's/^/    /' || echo "    (no removal of /etc/agentos)"
rm -rf "$WORK"

echo ""
echo "=== 8. dpkg audit ==="
agentos_sudo dpkg --audit 2>&1 | sed 's/^/  /'
