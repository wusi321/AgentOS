#!/usr/bin/env bash
# Verify the one behaviour the design makes a promise about: removing the package
# must not destroy the task and audit database.
#
# It is deliberately narrow, because the earlier failure was a precondition problem
# rather than an uninstall problem: the database had never been created, so the
# retention check passed or failed for the wrong reason. This script asserts every
# step.
#
# Usage: AGENTOS_SUDO_PASSWORD=... verify-data-retention.sh [artifact-directory]
set -uo pipefail

ARTIFACTS="${1:-/mnt/d/妗岄潰/AgentOS/build/debs}"
SUDO_PASSWORD="${AGENTOS_SUDO_PASSWORD:-}"
SUDO_USER_NAME="${AGENTOS_USER:-$(id -un)}"
FAILURES=0

sudo_do() {
  if [ -n "$SUDO_PASSWORD" ]; then
    printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"
  else
    sudo -n "$@"
  fi
}

step() { printf '\n== %s\n' "$1"; }
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then printf '  [ok  ] %s\n' "$label"
  else printf '  [FAIL] %s\n' "$label"; FAILURES=$((FAILURES + 1)); fi
}
state() {
  printf '  state: '
  if sudo_do test -f /var/lib/agentos/agentos.sqlite3; then
    printf 'database present (%s bytes)\n' "$(sudo_do stat -c %s /var/lib/agentos/agentos.sqlite3 2>/dev/null)"
  elif sudo_do test -d /var/lib/agentos; then
    printf 'directory present, no database: %s\n' "$(sudo_do ls -1 /var/lib/agentos 2>/dev/null | tr '\n' ' ')"
  else
    printf 'directory missing\n'
  fi
}

# The state directory is mode 0700 and owned by the service account, so a plain
# `test -f` run as the invoking user cannot traverse it and always reports failure.
# These predicates ask the question through sudo, where the answer is meaningful.
retained_present() { sudo_do test -f /var/lib/agentos/agentos.sqlite3; }
retained_absent() { ! sudo_do test -e /var/lib/agentos/agentos.sqlite3; }
config_removed() { ! sudo_do test -e /etc/agentos/agentos.toml; }

phase_line() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then printf '  [ok  ] %s\n' "$label"
  else printf '  [FAIL] %s\n' "$label"; FAILURES=$((FAILURES + 1)); fi
}

step "clean the machine"
sudo_do dpkg --purge --force-all agentos-meta agentos-gnome-extension agentos-desktop \
     agentos-runtime-dsh agentos-core >/dev/null 2>&1 || true
# The service must be stopped before its state is deleted: systemd recreates a
# Running unit's StateDirectory, so deleting underneath a live service is racy and
# would make the retention check below pass for the wrong reason.
sudo_do systemctl stop agentos-core >/dev/null 2>&1 || true
sudo_do rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos
sleep 1
phase_line "the machine starts with no AgentOS state" retained_absent
state

step "install core and runtime"
sudo_do dpkg -i "$ARTIFACTS/agentos-core_0.1.0_all.deb" "$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb" >/tmp/dr-install.log 2>&1
check "installation succeeded" bash -c "! grep -q '^dpkg: error' /tmp/dr-install.log"
# The install itself must not create task data. If a database exists here, it must
# be empty: a fresh service start creates an empty one, while a leftover from an
# earlier run would carry rows. Asserting emptiness is what makes the retention
# checks below meaningful.
phase_line "no task data exists right after installation" sudo_do python3 -c "
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
state

step "start the service and make the CLI actually use it"
sudo_do systemctl start agentos-core
sleep 4
check "the service is active" bash -c "systemctl is-active --quiet agentos-core"
sudo_do adduser "$SUDO_USER_NAME" agentos >/dev/null 2>&1 || true
sg agentos -c 'agentosctl health' >/tmp/dr-health.log 2>&1 || true
sed 's/^/    /' /tmp/dr-health.log
check "the CLI reached the service" grep -q 'ok=True' /tmp/dr-health.log
sg agentos -c 'agentosctl task run "retention check" --watch' >/tmp/dr-task.log 2>&1 || true
tail -3 /tmp/dr-task.log | sed 's/^/    /'
phase_line "the database now exists" retained_present
state

step "the database has real content"
sudo_do -u agentos python3 -c "
import sqlite3
connection = sqlite3.connect('/var/lib/agentos/agentos.sqlite3')
tasks = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
audit = connection.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
print(f'    tasks={tasks} audit_records={audit}')
assert tasks >= 1, 'no task was recorded'
assert audit >= 1, 'no audit record was written'
" 2>&1 | sed 's/^/  /'

step "stop the service"
sudo_do systemctl stop agentos-core
sleep 3
state
phase_line "stopping the service keeps the database" retained_present

step "remove the packages"
sudo_do dpkg -r agentos-runtime-dsh agentos-core >/tmp/dr-remove.log 2>&1
check "removal succeeded" bash -c "! grep -q '^dpkg: error' /tmp/dr-remove.log"
grep 'agentos-core:' /tmp/dr-remove.log | sed 's/^/    /' || true
state
phase_line "the database survived removal" retained_present
check "the removal said the data remains" grep -q 'remain in /var/lib/agentos' /tmp/dr-remove.log

step "purge the package"
sudo_do dpkg --purge agentos-core >/tmp/dr-purge.log 2>&1
check "purge succeeded" bash -c "! grep -q '^dpkg: error' /tmp/dr-purge.log"
grep 'agentos-core:' /tmp/dr-purge.log | sed 's/^/    /' || true
state
phase_line "the database survived purge" retained_present
check "purge said the data remains" grep -q 'still contains' /tmp/dr-purge.log
check "purge removed the service account" bash -c "! getent passwd agentos"
phase_line "purge removed the configuration" config_removed

step "the database is still readable and intact"
sudo_do python3 -c "
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
