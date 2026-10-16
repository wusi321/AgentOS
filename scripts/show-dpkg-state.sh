#!/usr/bin/env bash
# Compare the two forms of dpkg-query, because they do not mean the same thing.
#
# The verification used `dpkg-query -W -f='${Status}' <name>`, which prints a status
# line for a name whether or not the package is installed — a purged package reports
# `deinstall ok config-files`, not an error. That made "is it installed" checks read
# a status string rather than an answer. This compares the forms so the right one is
# used, and reports the dpkg database files that record conffile ownership.
#
# Usage: AGENTOS_SUDO_PASSWORD=... show-dpkg-state.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

agentos_require_sudo || exit 3

echo "=== the two query forms, for a package that is not installed ==="
for package in agentos-core agentos-meta; do
  printf '  %s\n' "$package"
  printf '    -W -f Status <name>      : %s\n' "$(dpkg-query -W -f='${Status}' "$package" 2>&1)"
  printf '    -W -f Status <name> exit : %s\n' "$(dpkg-query -W -f='${Status}' "$package" >/dev/null 2>&1; echo $?)"
  printf '    -f Status <name>         : %s\n' "$(dpkg-query -f='${Status}' "$package" 2>&1)"
  printf '    --show <name> exit       : %s\n' "$(dpkg-query --show "$package" >/dev/null 2>&1; echo $?)"
  printf '    -s <name> exit           : %s\n' "$(dpkg-query -s "$package" >/dev/null 2>&1; echo $?)"
  printf '    package_installed        : %s\n' "$(package_installed "$package" && echo yes || echo no)"
  printf '    -L <name> (files)        : %s\n' "$(dpkg-query -L "$package" 2>&1 | wc -l) line(s)"
done

echo ""
echo "=== dpkg's database records for agentos-core ==="
echo "  --- the status entry ---"
agentos_sudo dpkg --status agentos-core 2>/dev/null | sed -n '1,3p' | sed 's/^/    /' || echo "    (no entry)"
echo "  --- the control-information files ---"
agentos_sudo ls -la /var/lib/dpkg/info/agentos-core.* 2>&1 | sed 's/^/    /' || echo "    (none)"
echo "  --- a conffile list that still exists means conffile ownership is retained ---"
if agentos_sudo test -f /var/lib/dpkg/info/agentos-core.conffiles; then
  agentos_sudo cat /var/lib/dpkg/info/agentos-core.conffiles | sed 's/^/    /'
else
  echo "    no .conffiles file, so no conffile is owned"
fi

echo ""
echo "=== every agentos entry in dpkg's status database ==="
agentos_sudo grep -n '^Package: agentos' -A2 /var/lib/dpkg/status 2>/dev/null | sed 's/^/  /' || echo "  (none)"

echo ""
echo "=== the same for the available database ==="
agentos_sudo grep -c '^Package: agentos' /var/lib/dpkg/available 2>/dev/null | sed 's/^/  agentos entries in available: /' || echo "  (no available file)"

echo ""
echo "=== /etc/agentos now ==="
agentos_sudo ls -la /etc/agentos 2>&1 | sed 's/^/  /'
