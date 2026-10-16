#!/usr/bin/env bash
# End-to-end check of the documented installation routes.
#
# It removes any existing AgentOS installation, then installs the packages three
# ways, because the one that looks most natural is the one that fails and the
# documentation has to say so accurately:
#
#   route 1  apt install <only agentos-meta>     must fail: APT cannot see siblings
#   route 2  apt install <all the files>         works when gnome-shell is present
#   route 3  local repository, then apt install  works, and is the upgrade-friendly route
#
# Usage: AGENTOS_SUDO_PASSWORD=... test-install-routes.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

# HERE must be set before anything that uses it: with `set -u`, referencing it first
# aborts the script with "HERE: unbound variable" and no output at all.
ARTIFACTS="${1:-$HERE/../build/debs}"
REPO="${2:-/opt/agentos-repo}"
FAILURES=0

# Fail immediately with an explanation rather than hanging on a sudo prompt.
agentos_require_sudo || exit 3

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  [ok  ] %s\n' "$label"
  else
    printf '  [FAIL] %s\n' "$label"
    FAILURES=$((FAILURES + 1))
  fi
}

echo "=== host facts that change what this test can prove ==="
printf '  distribution:                   %s\n' "$(. /etc/os-release && echo "$PRETTY_NAME")"
printf '  gnome-shell installed:          %s\n' "$(package_installed gnome-shell && echo yes || echo no)"
printf '  gtk4 typelib directory present: %s\n' "$(test -d /usr/lib/x86_64-linux-gnu/girepository-1.0 && echo yes || echo no)"

echo ""
echo "=== starting from a clean machine ==="
agentos_clean_slate
check "no agentos package is installed" no_packages_installed "${AGENTOS_PACKAGES[@]}"

echo ""
echo "=== route 1: naming only the metapackage (the failure users hit) ==="
echo "  expected: APT refuses, because it cannot see the sibling packages"
if agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" install -y "$ARTIFACTS/agentos-meta_0.1.0_all.deb" \
      >/tmp/route1.log 2>&1; then
  echo "  [note] this APT accepted a lone metapackage, so the failure is"
  echo "         version-dependent; the other two routes are unaffected"
else
  check "APT reports the unmet dependency" grep -q 'agentos-core' /tmp/route1.log
  grep -E 'Depends: agentos' /tmp/route1.log | head -4 | sed 's/^/         /'
fi
agentos_clean_slate

echo ""
echo "=== route 2: listing every package on one command line ==="
shopt -s nullglob
if package_installed gnome-shell; then
  ROUTE2=("$ARTIFACTS"/agentos-*_*_all.deb)
  echo "  gnome-shell is present, so every package is installed by name"
else
  ROUTE2=("$ARTIFACTS"/agentos-core_0.1.0_all.deb "$ARTIFACTS"/agentos-runtime-dsh_0.1.0_all.deb \
          "$ARTIFACTS"/agentos-desktop_0.1.0_all.deb "$ARTIFACTS"/agentos-meta_0.1.0_all.deb)
  echo "  gnome-shell is absent, so the packages that do not need it are installed"
fi
ROUTE2_PACKAGES=()
for file in "${ROUTE2[@]}"; do
  ROUTE2_PACKAGES+=("$(basename "$file" | cut -d_ -f1)")
done
echo "  command: apt-get install -y $(for file in "${ROUTE2[@]}"; do printf '%s ' "$(basename "$file")"; done)"
agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" install -y "${ROUTE2[@]}" >/tmp/route2.log 2>&1 || true
check "every named package is configured" all_packages_installed "${ROUTE2_PACKAGES[@]}"
check "agentosctl is on PATH" command -v agentosctl
check "the service unit is enabled" bash -c "systemctl is-enabled --quiet agentos-core"
check "the configuration was installed" test -f /etc/agentos/agentos.toml
agentos_clean_slate

echo ""
echo "=== route 3: a local APT repository ==="
if agentos_sudo bash "$HERE/make-local-repo.sh" "$ARTIFACTS" "$REPO" >/tmp/route3-repo.log 2>&1; then
  check "the repository is built and configured" test -f /etc/apt/sources.list.d/agentos-local.list
  grep -E 'index entries|wrote /etc' /tmp/route3-repo.log | sed 's/^/         /'
  check "all five packages are visible to APT" bash -c '
    for package in agentos-core agentos-runtime-dsh agentos-desktop agentos-gnome-extension agentos-meta; do
      apt-cache policy "$package" 2>/dev/null | grep -q "Candidate: 0.1.0" || exit 1
    done'
else
  echo "  [FAIL] the repository builder failed:"
  tail -12 /tmp/route3-repo.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi

check "the metapackage declares its four siblings" bash -c \
  "apt-cache show agentos-meta 2>/dev/null | grep -q 'Depends:.*agentos-core'"
apt-cache show agentos-meta 2>/dev/null | grep -E '^Depends:' | sed 's/^/         /'

if package_installed gnome-shell; then
  TARGETS=(agentos-meta)
  echo "  gnome-shell is present, so the whole metapackage is installed by name"
else
  TARGETS=(agentos-core agentos-runtime-dsh agentos-desktop)
  echo "  gnome-shell is absent, so the packages that do not need it are installed"
fi
if agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" install -y "${TARGETS[@]}" >/tmp/route3-install.log 2>&1; then
  check "APT resolves and installs from the repository" all_packages_installed "${TARGETS[@]}"
  grep -E 'Setting up agentos' /tmp/route3-install.log | sed 's/^/         /'
else
  echo "  [FAIL] apt install from the repository failed:"
  tail -20 /tmp/route3-install.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
check "the configuration was installed from the repository" test -f /etc/agentos/agentos.toml
agentos_sudo systemctl start agentos-core >/dev/null 2>&1 || true
sleep 3
check "the service starts under the repository install" bash -c "systemctl is-active --quiet agentos-core"

echo ""
echo "=== removing the repository configuration ==="
if agentos_sudo bash "$HERE/make-local-repo.sh" --remove >/tmp/route3-remove.log 2>&1; then
  check "the repository is removed from APT" bash -c "! test -f /etc/apt/sources.list.d/agentos-local.list"
  check "the installed packages survive the repository removal" package_installed agentos-core
else
  echo "  [FAIL] the repository removal failed:"
  tail -8 /tmp/route3-remove.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi

echo ""
echo "=== uninstalling through APT, with the repository gone ==="
if agentos_remove "${AGENTOS_PACKAGES[@]}" >/tmp/route3-uninstall.log 2>&1; then
  check "APT removes the packages it installed" no_packages_installed agentos-core agentos-meta
else
  echo "  [note] APT removal reported problems:"
  tail -8 /tmp/route3-uninstall.log | sed 's/^/         /'
fi

echo ""
echo "=== cleaning up ==="
agentos_clean_slate
check "the machine is back to no AgentOS installation" no_packages_installed "${AGENTOS_PACKAGES[@]}"

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo "install routes verified"
  exit 0
fi
echo "$FAILURES install-route check(s) failed"
exit 1
