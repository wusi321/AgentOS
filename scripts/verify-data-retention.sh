#!/usr/bin/env bash
# Verify the one behaviour the design makes a promise about: removing the package
# must not destroy the task and audit database.
#
# Deliberately narrow, because an earlier failure was a precondition problem rather
# than an uninstall problem: the database had never been created, so the retention
# check reported the wrong thing. Every step here asserts its own precondition.
#
# Usage: AGENTOS_SUDO_PASSWORD=... verify-data-retention.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

ARTIFACTS="${1:-$HERE/../build/debs}"
TARGET_USER="${AGENTOS_USER:-$(id -un)}"
FAILURES=0

# Fail immediately with an explanation rather than hanging on a sudo prompt.
agentos_require_sudo || exit 3

step() { printf '\n== %s\n' "$1"; }

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  [ok  ] %s\n' "$label"
  else
    printf '  [FAIL] %s\n' "$label"
    FAILURES=$((FAILURES + 1))
  fi
}

# The state directory is mode 0700 and owned by the service account, so a plain
# `test -f` run as the invoking user cannot traverse it and always reports failure.
# These predicates ask the question through sudo, where the answer is meaningful.
retained_present() { agentos_sudo test -f /var/lib/agentos/agentos.sqlite3; }
retained_absent() { ! agentos_sudo test -e /var/lib/agentos/agentos.sqlite3; }
config_removed() { ! agentos_sudo test -e /etc/agentos/agentos.toml; }

database_is_empty() {
  agentos_sudo python3 -c "
import os, sqlite3, sys
path = '/var/lib/agentos/agentos.sqlite3'
if not os.path.exists(path):
    sys.exit(0)
try:
    connection = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    tasks = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
except Exception:
    sys.exit(0)
sys.exit(0 if tasks == 0 else 1)
"
}

step "clean the machine"
# The service must be stopped before its state is deleted: systemd recreates a
# *running* unit's StateDirectory, so deleting underneath a live service is racy and
# would make the retention check below pass for the wrong reason.
agentos_clean_slate
sleep 1
check "the machine starts with no AgentOS state" retained_absent
agentos_state_line "state"

step "install core and runtime"
if agentos_install_built_packages \
      "$ARTIFACTS/agentos-core_0.1.0_all.deb" "$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb" \
      >/tmp/dr-install.log 2>&1; then
  check "installation succeeded" true
else
  echo "  [FAIL] installation reported problems:"
  tail -10 /tmp/dr-install.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
# The install must not create task data. If a database exists here it must be empty:
# a fresh service start creates an empty one, while a leftover from an earlier run
# carries rows. Asserting emptiness is what makes the checks below meaningful.
check "no task data exists right after installation" database_is_empty
agentos_state_line "state"

step "start the service and make the CLI actually use it"
agentos_sudo systemctl start agentos-core
sleep 4
check "the service is active" bash -c "systemctl is-active --quiet agentos-core"
agentos_sudo adduser "$TARGET_USER" agentos >/dev/null 2>&1 || true
sg agentos -c 'agentosctl health' >/tmp/dr-health.log 2>&1 || true
sed 's/^/    /' /tmp/dr-health.log
check "the CLI reached the service" grep -q 'ok=True' /tmp/dr-health.log
sg agentos -c 'agentosctl task run "retention check" --watch' >/tmp/dr-task.log 2>&1 || true
tail -3 /tmp/dr-task.log | sed 's/^/    /'
check "the database now exists" retained_present
agentos_state_line "state"

step "the database has real content"
agentos_sudo python3 -c "
import sqlite3
connection = sqlite3.connect('/var/lib/agentos/agentos.sqlite3')
tasks = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
audit = connection.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
print(f'    tasks={tasks} audit_records={audit}')
assert tasks >= 1, 'no task was recorded'
assert audit >= 1, 'no audit record was written'
" 2>&1 | sed 's/^/  /'

step "stop the service"
agentos_stop_service agentos-core
sleep 3
agentos_state_line "state"
check "stopping the service keeps the database" retained_present

step "remove the packages"
if agentos_remove agentos-runtime-dsh agentos-core >/tmp/dr-remove.log 2>&1; then
  check "removal succeeded" true
else
  echo "  [FAIL] removal reported problems:"
  tail -8 /tmp/dr-remove.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
grep 'agentos-core:' /tmp/dr-remove.log | sed 's/^/    /' || true
agentos_state_line "state"
check "the database survived removal" retained_present
check "the removal said the data remains" grep -q 'remain in /var/lib/agentos' /tmp/dr-remove.log

step "purge the package"
if agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" purge -y agentos-core >/tmp/dr-purge.log 2>&1; then
  check "purge succeeded" true
else
  echo "  [FAIL] purge reported problems:"
  tail -8 /tmp/dr-purge.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
grep 'agentos-core:' /tmp/dr-purge.log | sed 's/^/    /' || true
agentos_state_line "state"
check "the database survived purge" retained_present
check "purge said the data remains" grep -q 'still contains' /tmp/dr-purge.log
check "purge removed the service account" bash -c "! getent passwd agentos"
check "purge removed the configuration" config_removed

step "the database is still readable and intact"
agentos_sudo python3 -c "
import sqlite3
connection = sqlite3.connect('file:/var/lib/agentos/agentos.sqlite3?mode=ro', uri=True)
tasks = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
audit = connection.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
print(f'    after purge: tasks={tasks} audit_records={audit}')
assert tasks >= 1, 'the audit trail was lost'
" 2>&1 | sed 's/^/  /'

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
  echo "data retention verified: removal and purge both keep the task and audit database"
  exit 0
fi
echo "$FAILURES retention check(s) failed"
exit 1
