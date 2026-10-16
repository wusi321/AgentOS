#!/usr/bin/env bash
# Check that `apt-get purge` and the shared helper actually remove the AgentOS
# packages and their conffiles.
#
# This exists because a verification stage failed with "the package configuration is
# installed" while the purge had reported success. The cause was a single substring:
# a removed package reports `deinstall ok config-files`, and an unanchored
# `grep 'install ok installed'` therefore matched a package that was not installed.
# The purge then appeared to succeed, and the next install unpacked the data archive
# while skipping the conffiles, whose recorded md5sums matched nothing on disk.
#
# Usage: AGENTOS_SUDO_PASSWORD=... check-purge.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

ARTIFACTS="${1:-$HERE/../build/debs}"

agentos_require_sudo || exit 3

report() {
  local label="$1"
  local installed_list
  installed_list="$(installed_subset "${AGENTOS_PACKAGES[@]}" | tr '\n' ' ')"
  printf '  %-30s %s\n' "$label" "${installed_list:-none installed}"
  printf '  %-30s %s\n' "" "$(agentos_sudo test -f /etc/agentos/agentos.toml && echo 'config: present' || echo 'config: absent')"
  printf '  %-30s %s\n' "" "$(agentos_sudo test -d /etc/agentos && echo '/etc/agentos exists' || echo '/etc/agentos absent')"
}

echo "=== the predicate itself ==="
# The exact status string, to show why the anchored match matters.
printf '  %-34s %s\n' "dpkg-query -W agentos-core" "$(dpkg-query -W -f='${Status}' agentos-core 2>/dev/null || echo '(not known)')"
printf '  %-34s %s\n' "package_installed agentos-core" "$(package_installed agentos-core && echo yes || echo no)"
printf '  %-34s %s\n' "the loose substring would match?" \
  "$(dpkg-query -W -f='${Status}' agentos-core 2>/dev/null | grep -q 'install ok installed' && echo 'yes (that is the bug)' || echo no)"

echo ""
echo "=== the state before anything ==="
report "before"

echo ""
echo "=== purge, through the shared helper ==="
if agentos_purge "${AGENTOS_PACKAGES[@]}"; then
  echo "  [ok  ] the helper reports every package purged"
else
  echo "  [FAIL] the helper could not purge every package"
fi
report "after purge"

echo ""
echo "=== dpkg's record after a purge ==="
printf '  %-34s %s\n' "agentos-core status" \
  "$(dpkg-query -W -f='${Status}' agentos-core 2>/dev/null || echo '(no record, which is correct)')"

echo ""
echo "=== install from the local files ==="
if agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" install -y \
      "$ARTIFACTS/agentos-core_0.1.0_all.deb" "$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb" \
      >/tmp/cp-install.log 2>&1; then
  echo "  [ok  ] the install succeeded"
else
  echo "  [FAIL] the install failed:"
  tail -10 /tmp/cp-install.log | sed 's/^/         /'
fi
report "after install"

echo ""
echo "=== the conffiles must be on disk after a clean install ==="
missing=0
for path in /etc/agentos/agentos.toml /etc/agentos/credentials.env.example \
            /etc/agentos/policies/default-policy.yaml /etc/agentos/policies/50-site-policy.yaml.example; do
  if agentos_sudo test -f "$path"; then
    printf '  [ok  ] %s\n' "$path"
  else
    printf '  [FAIL] %s\n' "$path"
    missing=$((missing + 1))
  fi
done

echo ""
echo "=== purge again, and confirm it is complete ==="
agentos_purge "${AGENTOS_PACKAGES[@]}"
report "after the second purge"
if no_packages_installed "${AGENTOS_PACKAGES[@]}"; then
  echo "  [ok  ] no package record remains"
else
  echo "  [FAIL] a package record remains: $(installed_subset "${AGENTOS_PACKAGES[@]}" | tr '\n' ' ')"
fi

echo ""
echo "=== final cleanup ==="
agentos_reset_state
report "after cleanup"

echo ""
if [ "$missing" -eq 0 ]; then
  echo "the purge and the install behave as documented"
  exit 0
fi
echo "$missing conffile(s) were not restored by the install"
exit 1
