#!/usr/bin/env bash
# Report whether the core package installed its configuration, and why not if not.
#
# A verification run failed the "configuration is installed" checks while the
# service was running, which points at the postinst rather than the install as a
# whole. This script answers that directly.
#
# Usage: AGENTOS_SUDO_PASSWORD=... diagnose-config.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

agentos_require_sudo || exit 3

echo "=== package state ==="
dpkg-query -W -f='  ${Package} ${Version} ${Status}\n' agentos-core agentos-runtime-dsh agentos-desktop 2>&1

echo ""
echo "=== /etc/agentos ==="
agentos_sudo ls -la /etc/agentos 2>&1 | sed 's/^/  /'
echo "  --- the policy directory ---"
agentos_sudo ls -la /etc/agentos/policies 2>&1 | sed 's/^/  /'

echo ""
echo "=== what the package says it ships ==="
dpkg-query -L agentos-core 2>/dev/null | grep -E '^/etc' | sed 's/^/  /' || echo "  (nothing under /etc)"

echo ""
echo "=== the conffile records ==="
dpkg-query -W -f='${Conffiles}\n' agentos-core 2>/dev/null | sed 's/^/  /'

echo ""
echo "=== dpkg's view of the conffiles ==="
agentos_sudo dpkg --status agentos-core 2>/dev/null | grep -A20 '^Conffiles' | sed 's/^/  /'

echo ""
echo "=== the installation log ==="
tail -25 /tmp/apt-install.log 2>&1 | sed 's/^/  /'

echo ""
echo "=== the postinst, as installed ==="
agentos_sudo sh -c 'dpkg-deb --ctrl-tarfile /var/cache/apt/archives/agentos-core_0.1.0_all.deb 2>/dev/null | tar -xO ./postinst 2>/dev/null | sed -n "1,60p"' 2>&1 | sed 's/^/  /' || echo "  (the archive is gone)"

echo ""
echo "=== the journal for this unit ==="
agentos_sudo journalctl -u agentos-core --no-pager -n 15 2>&1 | sed 's/^/  /'
