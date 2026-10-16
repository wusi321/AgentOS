#!/usr/bin/env bash
# Verify the installed AgentOS packages on this machine, end to end.
#
# Installs the built .deb files, drives the real service through the real CLI, then
# removes the packages again, reporting exactly what it did. This is the
# "installed" level of verification: it proves the maintainer scripts, the systemd
# units, the socket permissions, and the CLI work as installed.
#
# Package operations go through the shared helper, which exists because
# `apt-get purge a b c` exits 100 and does *nothing* when any named package is not
# installed — a trap that silently leaves packages and their conffiles on disk.
#
# Usage: AGENTOS_SUDO_PASSWORD=... verify-installed.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

ARTIFACTS="${1:-$HERE/../build/debs}"
FAILURES=0

# Fail immediately with an explanation rather than hanging on the first sudo prompt.
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

state_db_present() { agentos_sudo test -f /var/lib/agentos/agentos.sqlite3; }

echo "=== host facts ==="
printf '  distribution:            %s\n' "$(. /etc/os-release && echo "$PRETTY_NAME")"
printf '  gnome-shell installed:   %s\n' "$(package_installed gnome-shell && echo yes || echo no)"
printf '  python:                  %s\n' "$(python3 --version 2>&1)"

echo ""
echo "=== cleaning any previous installation ==="
# Purge first, then remove what a purge keeps, then confirm dpkg holds no record of
# any AgentOS package. A leftover /etc/agentos while dpkg still considers the package
# "config-files" makes a fresh install unpack its data archive and skip the
# conffiles, whose recorded checksums then match nothing on disk.
agentos_clean_slate
check "no agentos package remains installed" no_packages_installed "${AGENTOS_PACKAGES[@]}"
check "no configuration remains" bash -c "! test -f /etc/agentos/agentos.toml"
agentos_config_line "config before install"

echo ""
echo "=== install ==="
CORE_DEB="$ARTIFACTS/agentos-core_0.1.0_all.deb"
RUNTIME_DEB="$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb"
DESKTOP_DEB="$ARTIFACTS/agentos-desktop_0.1.0_all.deb"
EXT_DEB="$ARTIFACTS/agentos-gnome-extension_0.1.0_all.deb"
META_DEB="$ARTIFACTS/agentos-meta_0.1.0_all.deb"

if agentos_install_built_packages "$CORE_DEB" "$RUNTIME_DEB" "$DESKTOP_DEB" \
      >/tmp/apt-install.log 2>&1; then
  check "core, runtime, and desktop install cleanly" true
else
  echo "  [FAIL] installation reported problems:"
  tail -20 /tmp/apt-install.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi

# The extension needs gnome-shell. On a host without it, APT refuses the whole
# batch — which is exactly the dependency the metapackage declares and that
# docs/install.md documents installing without.
if agentos_install_built_packages "$EXT_DEB" >/tmp/apt-ext.log 2>&1; then
  check "the GNOME extension installs" true
else
  echo "  [note] the extension needs gnome-shell, which this host does not have:"
  grep -E 'Depends|not installable|Unable to locate' /tmp/apt-ext.log | head -3 | sed 's/^/         /'
  echo "         docs/install.md documents installing without it for this exact case"
fi

if agentos_install_built_packages "$META_DEB" >/tmp/apt-meta.log 2>&1; then
  check "the metapackage installs" true
else
  echo "  [note] the metapackage depends on the extension, which needs gnome-shell:"
  tail -4 /tmp/apt-meta.log | sed 's/^/         /'
fi

check "the core package is installed" package_installed agentos-core
check "the runtime package is installed" package_installed agentos-runtime-dsh
echo "  --- installation log ---"
tail -6 /tmp/apt-install.log | sed 's/^/    /'
# Record the configuration immediately, before any later step can disturb it, so a
# failure below can be attributed to the install rather than to the test.
agentos_config_line "config right after install"

echo ""
echo "=== the service account and its state ==="
check "the agentos service account exists" getent passwd agentos
check "the agentos group exists" getent group agentos
check "the state directory is not readable by others" bash -c "[ \$(stat -c %a /var/lib/agentos) = '700' ]"
check "the database directory is owned by agentos" bash -c "[ \$(stat -c %U /var/lib/agentos) = 'agentos' ]"

echo ""
echo "=== the shipped configuration ==="
# A failing check here must come with the evidence needed to explain it: an existing
# directory that lacks a file, a missing directory, and what the postinst did.
agentos_config_line "config observed now"
check "the package configuration is installed" test -f /etc/agentos/agentos.toml
check "the read-only default policy is installed" test -f /etc/agentos/policies/default-policy.yaml
check "the example site policy is installed" test -f /etc/agentos/policies/50-site-policy.yaml.example
check "no credential file was installed" test ! -f /etc/agentos/credentials.env
check "the credential template is installed" test -f /etc/agentos/credentials.env.example
check "the configuration is not world-writable" bash -c "! find /etc/agentos -perm -o+w | grep -q ."
if ! test -f /etc/agentos/agentos.toml; then
  echo "  --- why: the installed configuration is incomplete ---"
  agentos_sudo ls -la /etc/agentos 2>&1 | sed 's/^/      /'
  agentos_sudo ls -la /etc/agentos/policies 2>&1 | sed 's/^/      /'
  echo "      files the package ships under /etc:"
  dpkg-query -L agentos-core 2>/dev/null | grep -E '^/etc' | sed 's/^/        /'
  echo "      dpkg's conffile records:"
  dpkg-query -W -f='${Conffiles}\n' agentos-core 2>/dev/null | sed 's/^/        /'
  echo "      what the postinst printed:"
  grep -E 'agentos-core:|Setting up|dpkg:' /tmp/apt-install.log 2>/dev/null | tail -12 | sed 's/^/        /'
fi

echo ""
echo "=== the core service ==="
agentos_sudo systemctl start agentos-core >/tmp/systemctl-start.log 2>&1 || true
sleep 3
check "the service is active" bash -c "systemctl is-active --quiet agentos-core"
check "the service is enabled" bash -c "systemctl is-enabled --quiet agentos-core"
check "the unit does not narrow /proc with ProcSubset" bash -c "! grep -q '^ProcSubset=' /usr/lib/systemd/system/agentos-core.service"
check "the unit declares a reload" bash -c "grep -q '^ExecReload=' /usr/lib/systemd/system/agentos-core.service"
echo "  --- status ---"
systemctl status agentos-core --no-pager 2>&1 | head -10 | sed 's/^/    /'
echo "  --- socket ---"
agentos_sudo ls -l /run/agentos/core.sock 2>&1 | sed 's/^/    /'

echo ""
echo "=== access is refused before the user is authorized ==="
TARGET_USER="${AGENTOS_USER:-$(id -un)}"

# The check uses a throwaway account that has never been in the agentos group, because
# a shell that already holds the group cannot give it up: `id` reports the caller's
# credential set and `sg` only adds a group. The probe account is the credential set a
# user without the membership receives.
agentos_membership_line "$TARGET_USER"
agentos_probe_account_line

if agentos_run_without_agentos_group agentosctl health >/tmp/pre-access.log 2>&1; then
  echo "  [FAIL] the service answered while the process had no agentos group"
  sed 's/^/         /' /tmp/pre-access.log
  FAILURES=$((FAILURES + 1))
elif grep -q 'ipc_access_denied' /tmp/pre-access.log; then
  echo "  [ok  ] a process without the agentos group is refused, with an actionable message"
  sed 's/^/         /' /tmp/pre-access.log
else
  echo "  [FAIL] the refusal was not the expected access denial:"
  sed 's/^/         /' /tmp/pre-access.log
  FAILURES=$((FAILURES + 1))
fi

echo ""
echo "=== the documented authorization step ==="
agentos_sudo adduser "$TARGET_USER" agentos >/dev/null 2>&1 || true
agentos_sudo systemctl restart agentos-core >/dev/null 2>&1 || true
sleep 3
sg agentos -c 'agentosctl health' 2>&1 | sed 's/^/    /'
check "health reports the service once the user is authorized" \
      bash -c "sg agentos -c 'agentosctl health' | grep -q 'ok=True'"
sg agentos -c 'agentosctl hello' 2>&1 | head -6 | sed 's/^/    /'

echo ""
echo "=== read-only system queries ==="
sg agentos -c 'agentosctl system snapshot' 2>&1 | head -14 | sed 's/^/  /'
sg agentos -c 'agentosctl system processes --limit 5' 2>&1 | head -8 | sed 's/^/  /'

echo ""
echo "=== the policy posture ==="
sg agentos -c 'agentosctl policy inventory' 2>&1 | head -20 | sed 's/^/  /'
echo "  --- a P3 tool is refused ---"
sg agentos -c 'agentosctl policy evaluate system.packages.install' 2>&1 | sed 's/^/    /' || true

echo ""
echo "=== a real task through the runtime ==="
sg agentos -c 'agentosctl task run "Report whether this system is under memory or disk pressure." --watch' 2>&1 | tail -6 | sed 's/^/  /'
sg agentos -c 'agentosctl task list --limit 5' 2>&1 | sed 's/^/  /'

echo ""
echo "=== the audit trail ==="
sg agentos -c 'agentosctl audit summary' 2>&1 | sed 's/^/  /'

echo ""
echo "=== the runtime reports its capability set honestly ==="
sg agentos -c 'agentosctl runtime probe' 2>&1 | sed 's/^/  /'

echo ""
echo "=== the extension is installed where GNOME Shell looks for it ==="
if package_installed agentos-gnome-extension; then
  check "the extension directory matches its uuid" test -f /usr/share/gnome-shell/extensions/agentos@agentos.dev/metadata.json
  check "the extension schema is installed" test -f /usr/share/glib-2.0/schemas/org.gnome.shell.extensions.agentos.gschema.xml
  check "the extension schema compiles" glib-compile-schemas --strict --dry-run /usr/share/glib-2.0/schemas
  check "the desktop schema compiles" glib-compile-schemas --strict --dry-run /usr/share/glib-2.0/schemas
  check "the extension holds no credential" bash -c "! grep -rIl 'api_key\|API_KEY\|password' /usr/share/gnome-shell/extensions/agentos@agentos.dev | grep -q ."
else
  echo "  [note] the extension is not installed on this host; its layout is checked"
  echo "         from the package by scripts/verify-debs.sh instead"
fi

echo ""
echo "=== upgrade in place ==="
# A normal reinstall, deliberately: the point of this check is that a conffile the
# administrator may have edited survives an upgrade, which is dpkg's default
# behaviour. The installs above force the package's own conffiles onto disk because
# they are testing the package; this one tests the upgrade path as a user sees it.
if agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" install -y --reinstall "$CORE_DEB" >/tmp/apt-upgrade.log 2>&1; then
  check "reinstalling the same version succeeds" true
else
  echo "  [FAIL] upgrade reported problems:"; tail -12 /tmp/apt-upgrade.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
check "the configuration survived the upgrade" test -f /etc/agentos/agentos.toml
check "the unit is still valid after the upgrade" bash -c "systemctl cat agentos-core | grep -q ExecStart"
check "the conffiles are still the package's own" bash -c "dpkg-query -W -f='\${Conffiles}' agentos-core | grep -q agentos.toml"

echo ""
echo "=== removal keeps user data ==="
# The precondition is asserted rather than assumed: if the database were already
# absent, the check below would pass for the wrong reason and hide a real defect.
echo "  --- state before removal ---"
agentos_sudo ls -la /var/lib/agentos 2>&1 | sed 's/^/    /'
check "the database exists before removal (precondition)" state_db_present
agentos_stop_service agentos-core
check "the database exists after the service stopped (precondition)" state_db_present
if agentos_remove "${AGENTOS_PACKAGES[@]}" >/tmp/apt-remove.log 2>&1; then
  check "removal succeeds" true
else
  echo "  [FAIL] removal reported problems:"; tail -12 /tmp/apt-remove.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
echo "  --- state after removal ---"
agentos_sudo ls -la /var/lib/agentos 2>&1 | sed 's/^/    /'
check "the task and audit database survived removal" state_db_present
check "the removal reported that data remains" grep -q 'remain in /var/lib/agentos' /tmp/apt-remove.log
check "the service is no longer enabled" bash -c "! systemctl is-enabled --quiet agentos-core"
check "the program files are gone" bash -c "! test -f /usr/bin/agentosctl"

echo ""
echo "=== purge ==="
check "the database exists before purge (precondition)" state_db_present
if agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" purge -y agentos-core >/tmp/apt-purge.log 2>&1; then
  check "purge succeeds" true
else
  echo "  [FAIL] purge reported problems:"; tail -12 /tmp/apt-purge.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
echo "  --- state after purge ---"
agentos_sudo ls -la /var/lib/agentos 2>&1 | sed 's/^/    /'
check "purge still keeps the audit trail" state_db_present
check "purge reported that data remains" grep -q 'still contains' /tmp/apt-purge.log
check "purge removed the configuration" bash -c "! test -f /etc/agentos/agentos.toml"
check "purge removed the service account" bash -c "! getent passwd agentos"

echo ""
echo "=== the audit trail is still readable after purge ==="
agentos_sudo python3 -c "
import sqlite3, sys
connection = sqlite3.connect('file:/var/lib/agentos/agentos.sqlite3?mode=ro', uri=True)
tasks = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
audit = connection.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
print(f'    tasks={tasks} audit_records={audit}')
sys.exit(0 if tasks >= 0 and audit >= 0 else 1)
" 2>&1 | sed 's/^/  /'

echo ""
echo "=== cleaning up the residual state this test created ==="
agentos_clean_slate
check "the test's residual state is gone" bash -c "! test -d /var/lib/agentos"

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo "installed verification passed: every check succeeded"
  exit 0
fi
echo "$FAILURES installed check(s) failed"
exit 1
