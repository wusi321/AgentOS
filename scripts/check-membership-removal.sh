#!/usr/bin/env bash
# Test whether group membership can actually be removed, and why not if it cannot.
#
# The access-boundary check depends on removing the human user from the agentos
# group, asking the service, and seeing a refusal. If the removal does not happen,
# the service legitimately answers and the check reports a failure that is really a
# test-harness limitation. This distinguishes the two.
#
# Usage: AGENTOS_SUDO_PASSWORD=... check-membership-removal.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

TARGET_USER="${AGENTOS_USER:-$(id -un)}"

agentos_require_sudo || exit 3

echo "=== before ==="
agentos_membership_line "$TARGET_USER"

echo ""
echo "=== gpasswd -d $TARGET_USER agentos ==="
if agentos_sudo gpasswd -d "$TARGET_USER" agentos; then
  echo "  gpasswd exit: 0"
else
  echo "  gpasswd exit: $?"
fi
agentos_membership_line "$TARGET_USER"

echo ""
echo "=== deluser $TARGET_USER agentos ==="
if agentos_sudo deluser "$TARGET_USER" agentos; then
  echo "  deluser exit: 0"
else
  echo "  deluser exit: $?"
fi
agentos_membership_line "$TARGET_USER"

echo ""
echo "=== through the helper ==="
if agentos_remove_user_from_group "$TARGET_USER" agentos; then
  echo "  [ok  ] the helper removed the membership"
else
  echo "  [FAIL] the helper could not remove the membership"
fi
agentos_membership_line "$TARGET_USER"

echo ""
echo "=== is agentos this user's primary group? ==="
printf '  primary group:      %s\n' "$(id -gn "$TARGET_USER")"
printf '  primary group id:   %s\n' "$(id -g "$TARGET_USER")"
printf '  agentos group id:   %s\n' "$(getent group agentos | cut -d: -f3)"

echo ""
echo "=== the raw group entry ==="
getent group agentos | sed 's/^/  /'
echo "  --- every group this user is in, from the database ---"
getent group | awk -F: -v u="$TARGET_USER" '$4 ~ u {print "  " $1}' || true
echo "  --- and the user's passwd entry, which names its primary group ---"
getent passwd "$TARGET_USER" | sed 's/^/  /'

echo ""
echo "=== restore for later steps ==="
agentos_sudo adduser "$TARGET_USER" agentos >/dev/null 2>&1 || true
agentos_membership_line "$TARGET_USER"
