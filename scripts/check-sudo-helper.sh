#!/usr/bin/env bash
# Check that the verification helper library can actually use sudo.
#
# The installed-system verification hung on a sudo prompt because `-p ''` was being
# dropped by the shell, leaving sudo to parse the next token as an option. This
# script proves the helper works before a long verification run depends on it.
#
# Usage: AGENTOS_SUDO_PASSWORD=... check-sudo-helper.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

echo "=== environment ==="
printf '  SUDO_PASSWORD set: %s\n' "$([ -n "${SUDO_PASSWORD:-}" ] && echo yes || echo no)"
printf '  running as uid:    %s\n' "$(id -u)"

echo ""
echo "=== the raw forms ==="
if printf '%s\n' "${SUDO_PASSWORD:-}" | sudo -S -p ' ' true 2>/dev/null; then
  echo "  [ok  ] printf | sudo -S -p ' ' true"
else
  echo "  [FAIL] printf | sudo -S -p ' ' true"
fi
if sudo -n true 2>/dev/null; then
  echo "  [ok  ] sudo -n true (a credential is cached)"
else
  echo "  [note] sudo -n true: no cached credential"
fi

echo ""
echo "=== through the helper ==="
if agentos_sudo true 2>/dev/null; then
  echo "  [ok  ] agentos_sudo true"
else
  echo "  [FAIL] agentos_sudo true"
fi
if agentos_require_sudo; then
  echo "  [ok  ] agentos_require_sudo"
else
  echo "  [FAIL] agentos_require_sudo refused; the message above explains why"
  exit 1
fi

echo ""
echo "=== the helper can read root-only state ==="
if agentos_sudo test -d /var/lib; then
  echo "  [ok  ] agentos_sudo test -d /var/lib"
else
  echo "  [FAIL] agentos_sudo test -d /var/lib"
  exit 1
fi

echo ""
echo "=== the helper's package predicates ==="
printf '  gnome-shell installed: %s\n' "$(package_installed gnome-shell && echo yes || echo no)"
printf '  agentos-core installed: %s\n' "$(package_installed agentos-core && echo yes || echo no)"
printf '  installed subset of the project packages: %s\n' "$(installed_subset "${AGENTOS_PACKAGES[@]}" | tr '\n' ' ')"

echo ""
echo "the sudo helper works"
