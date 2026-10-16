#!/usr/bin/env bash
# Run the install/verify cycle repeatedly to measure how reproducible a failure is.
#
# An intermittent failure must be characterized before it can be fixed: a test that
# fails one run in five points at state or timing, while a test that fails every time
# points at the code. This runs the same cycle N times and reports which step failed
# in which iteration.
#
# Usage: AGENTOS_SUDO_PASSWORD=... stress-install-phase.sh [iterations] [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

ITERATIONS="${1:-3}"
ARTIFACTS="${2:-$HERE/../build/debs}"

agentos_require_sudo || exit 3

echo "=== the conffiles control file inside the package ==="
TMP=$(mktemp -d)
dpkg-deb --ctrl-tarfile "$ARTIFACTS/agentos-core_0.1.0_all.deb" 2>/dev/null | tar -x -C "$TMP" 2>/dev/null
if [ -f "$TMP/conffiles" ]; then
  sed 's/^/  /' "$TMP/conffiles"
else
  echo "  (no conffiles file; the package declares none)"
fi
rm -rf "$TMP"

failures=0
for iteration in $(seq 1 "$ITERATIONS"); do
  echo ""
  echo "################ iteration $iteration of $ITERATIONS ################"

  # A single install-and-check cycle, exactly as verify-installed performs it, but
  # with the configuration probe immediately after the install.
  agentos_purge "${AGENTOS_PACKAGES[@]}"
  agentos_reset_state

  if ! agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" install -y \
        "$ARTIFACTS/agentos-core_0.1.0_all.deb" "$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb" \
        "$ARTIFACTS/agentos-desktop_0.1.0_all.deb" >"/tmp/stress-$iteration.log" 2>&1; then
    echo "  [FAIL] iteration $iteration: the install command failed"
    tail -6 "/tmp/stress-$iteration.log" | sed 's/^/         /'
    failures=$((failures + 1))
    continue
  fi

  agentos_config_line "  config right after install"
  agentos_config_line "  config a second later  "
  sleep 1
  agentos_config_line "  config after a second  "

  if agentos_sudo test -f /etc/agentos/agentos.toml; then
    echo "  [ok  ] iteration $iteration: the configuration is present"
  else
    echo "  [FAIL] iteration $iteration: the configuration is missing"
    failures=$((failures + 1))
    echo "         /etc/agentos:"
    agentos_sudo find /etc/agentos 2>&1 | sed 's/^/           /'
    echo "         the install log:"
    tail -20 "/tmp/stress-$iteration.log" | sed 's/^/           /'
  fi
done

echo ""
if [ "$failures" -eq 0 ]; then
  echo "the install phase is reproducible: $ITERATIONS of $ITERATIONS passed"
  exit 0
fi
echo "the install phase failed $failures of $ITERATIONS times"
exit 1
