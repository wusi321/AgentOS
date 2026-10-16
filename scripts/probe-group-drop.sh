#!/usr/bin/env bash
# Probe how to run a command with the agentos group genuinely absent.
#
# The access-boundary check needs a process whose group list does not contain
# `agentos`. That is harder than it looks from a shell that already holds the group:
#
#   * `id -nG` reports the caller's group list, not the database;
#   * `sg` ADDS a group to the invoking shell's supplementary groups, so a process
#     that already has `agentos` keeps it and the service correctly answers;
#   * `setpriv --clear-groups` can block when it starts a program that reads stdin.
#
# This script measures each candidate so the verification uses one that works.
#
# Usage: probe-group-drop.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

CALLER="${AGENTOS_USER:-$(id -un)}"
PROBE_USER="agentos-probe"

agentos_require_sudo || exit 3

echo "=== this shell's groups ==="
printf '  %s\n' "$(id -nG | tr ' ' ',')"
if command -v timeout >/dev/null 2>&1; then
  echo "  timeout is available, so a blocking candidate can be bounded"
fi

echo ""
echo "=== a probe account that is deliberately not in agentos ==="
if ! getent passwd "$PROBE_USER" >/dev/null 2>&1; then
  agentos_sudo useradd --create-home --shell /bin/bash "$PROBE_USER" 2>&1 | sed 's/^/  /'
  echo "  created $PROBE_USER"
else
  echo "  $PROBE_USER already exists"
fi
agentos_membership_line "$PROBE_USER"

echo ""
echo "=== candidate 1: sudo -u (rebuilds the credential set from the database) ==="
if command -v timeout >/dev/null 2>&1; then
  RUNNER=(timeout 20)
else
  RUNNER=()
fi
if agentos_sudo -u "$PROBE_USER" "${RUNNER[@]}" id -nG 2>&1 | sed 's/^/  groups: /'; then
  echo "  exit: 0"
else
  echo "  exit: $?"
fi
echo "  --- ask the service ---"
output="$(agentos_sudo -u "$PROBE_USER" "${RUNNER[@]}" agentosctl health 2>&1)"
status=$?
printf '  exit: %s\n' "$status"
printf '%s\n' "$output" | sed 's/^/  /'
if printf '%s' "$output" | grep -q 'ipc_access_denied'; then
  echo "  [ok  ] the refusal is the documented access denial"
elif printf '%s' "$output" | grep -qE 'ok=True|"ok": true'; then
  echo "  [FAIL] the service answered for a user with no agentos group"
else
  echo "  [note] the failure was neither the expected denial nor a success"
fi

echo ""
echo "=== candidate 2: runuser -u ==="
if command -v runuser >/dev/null 2>&1; then
  output="$(agentos_sudo runuser -u "$PROBE_USER" -- "${RUNNER[@]}" agentosctl health 2>&1)"
  printf '  exit: %s\n' "$?"
  printf '%s\n' "$output" | sed 's/^/  /'
else
  echo "  runuser is not installed"
fi

echo ""
echo "=== candidate 3: setpriv --clear-groups (bounded by timeout) ==="
output="$(agentos_sudo setpriv --reuid="$PROBE_USER" --regid="$(id -g "$PROBE_USER")" --clear-groups \
  "${RUNNER[@]}" /usr/bin/env HOME=/home/"$PROBE_USER" agentosctl health 2>&1 </dev/null)"
printf '  exit: %s\n' "$?"
printf '%s\n' "$output" | sed 's/^/  /'

echo ""
echo "=== the probe account's group list, as the service sees it ==="
agentos_sudo -u "$PROBE_USER" "${RUNNER[@]}" id 2>&1 | sed 's/^/  /'

echo ""
echo "=== cleanup ==="
agentos_sudo userdel --remove "$PROBE_USER" >/dev/null 2>&1 || true
printf '  %s\n' "$(getent passwd "$PROBE_USER" >/dev/null 2>&1 && echo 'the probe account still exists' || echo 'the probe account is removed')"
printf '  %-24s %s\n' "the caller's groups" "$(id -nG | tr ' ' ',')"
