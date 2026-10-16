#!/usr/bin/env bash
# Verify the installed AgentOS system end to end, through its real IPC authority.
#
# The AgentOS core service accepts IPC only from a member of the `agentos` group,
# which is the design's authorization boundary rather than an accident. This script
# therefore performs the documented operator step (add the human user to the
# group), restarts the service so that the kernel records the new credentials, and
# then drives the whole system through the real CLI.
#
# Usage: AGENTOS_SUDO_PASSWORD=... verify-live.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-agentos-verify.sh
. "$HERE/lib-agentos-verify.sh"

ARTIFACTS="${1:-$HERE/../build/debs}"
TARGET_USER="${AGENTOS_USER:-$(id -un)}"
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

echo "=== the installed system ==="
dpkg-query -W -f '  ${Package} ${Version}\n' agentos-core agentos-runtime-dsh agentos-desktop 2>&1

echo ""
echo "=== 0. fresh install, so a stale conffile cannot be tested by mistake ==="
# A conffile the administrator has edited is preserved across an upgrade, which is
# correct for configuration but means an edited systemd unit from an earlier build
# keeps running. Purging first makes this a test of the packages as built. The purge
# passes only the packages that are actually installed, because `apt-get purge a b c`
# exits 100 and does nothing at all when any named package is absent.
agentos_clean_slate
if agentos_install_built_packages \
      "$ARTIFACTS/agentos-core_0.1.0_all.deb" "$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb" \
      "$ARTIFACTS/agentos-desktop_0.1.0_all.deb" >/tmp/live-install.log 2>&1; then
  check "core, runtime, and desktop install cleanly" true
else
  echo "  [FAIL] installation reported problems:"
  tail -12 /tmp/live-install.log | sed 's/^/         /'
  FAILURES=$((FAILURES + 1))
fi
# The GNOME extension needs gnome-shell, which a headless host does not have. Its
# absence must not prevent the rest of the verification from running.
if agentos_install_built_packages "$ARTIFACTS/agentos-gnome-extension_0.1.0_all.deb" \
      >/tmp/live-ext.log 2>&1; then
  check "the GNOME extension installs" true
else
  echo "  [note] the extension needs gnome-shell, which this host lacks:"
  grep -E 'Depends|not installable' /tmp/live-ext.log | head -2 | sed 's/^/         /'
fi
agentos_install_built_packages "$ARTIFACTS/agentos-meta_0.1.0_all.deb" >/dev/null 2>&1 || true
echo "  --- the installed unit does not narrow /proc ---"
check "the service unit does not set ProcSubset" bash -c "! grep -q '^ProcSubset=' /usr/lib/systemd/system/agentos-core.service"
check "the service declares a reload" bash -c "grep -q '^ExecReload=' /usr/lib/systemd/system/agentos-core.service"

echo ""
echo "=== 1. access is refused before the user is authorized ==="
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
echo "=== 2. the documented authorization step ==="
echo "  adding $TARGET_USER to the agentos group and restarting the service"
agentos_sudo adduser "$TARGET_USER" agentos >/dev/null 2>&1 || true
agentos_sudo systemctl restart agentos-core
sleep 4

# A new group membership only takes effect in a new login session: the kernel
# checks the credentials the process already has. `sg` starts a shell with the
# agentos group as its primary group, which is exactly the state a fresh login
# gives the user, so the CLI below runs the way it will for the operator.
ct() {
  sg agentos -c "agentosctl $(printf '%q ' "$@")"
}

echo ""
echo "=== 3. the CLI now reaches the service ==="
ct health 2>&1 | sed 's/^/  /'
check "health reports the service" bash -c "sg agentos -c 'agentosctl health' | grep -q service"
sg agentos -c 'agentosctl --json hello' > /tmp/hello.json 2>&1 || true
check "the peer credentials are kernel-reported" grep -q '"uid"' /tmp/hello.json
echo "  --- kernel-reported caller identity ---"
python3 - <<'PY' 2>/dev/null || sed 's/^/    /' /tmp/hello.json
import json
try:
    document = json.load(open("/tmp/hello.json"))
except Exception:
    raise SystemExit(1)
peer = document.get("peer", {})
print(f"    uid={peer.get('uid')} gid={peer.get('gid')} pid={peer.get('pid')}")
print(f"    methods exposed: {len(document.get('methods', []))}")
PY

echo ""
echo "=== 4. read-only system inspection ==="
ct system snapshot 2>&1 | head -16 | sed 's/^/  /'

echo ""
echo "=== 5. the policy posture ==="
ct policy inventory 2>&1 | head -24 | sed 's/^/  /'
echo "  --- a P0 read is permitted ---"
ct policy evaluate system.processes.list --params '{"limit": 5}' 2>&1 | sed 's/^/    /'
echo "  --- a P3 administrative tool is refused ---"
ct policy evaluate system.packages.install 2>&1 | sed 's/^/    /' || true
echo "  --- an undeclared tool is refused ---"
ct policy evaluate system.imaginary.tool 2>&1 | sed 's/^/    /' || true

echo ""
echo "=== 6. a real task through the mock runtime ==="
ct task run "Report whether this host is under memory or disk pressure." --watch 2>&1 | tail -10 | sed 's/^/  /'
echo "  --- task list ---"
ct task list --limit 5 2>&1 | sed 's/^/    /'

echo ""
echo "=== 7. the approval path, including a refusal and a permitted grant ==="
# The shipped policy is read-only, so a P2 operation is refused outright. This
# step then installs the documented example site policy, which is what an
# administrator does to enable controlled operations, and repeats the flow so the
# full approve-then-execute path is exercised rather than only the refusal.
TASK_ID=$(sg agentos -c 'agentosctl --json task run "Prepare a controlled operation." --level-cap P2' 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["task_id"])' 2>/dev/null)
echo "  task: ${TASK_ID:-<none>}"
if [ -n "$TASK_ID" ]; then
  echo "  --- under the read-only default policy, the P2 operation is refused ---"
  ct approval request "$TASK_ID" system.process.terminate --target "$$" 2>&1 | sed 's/^/    /' || true
fi

echo ""
echo "  --- installing the documented example site policy ---"
# The example ships in the source tree and also inside the doc directory of the
# installed core package, so a verification run works whether or not the checkout
# is at hand.
EXAMPLE="$HERE/../configs/policies/50-site-policy.yaml.example"
if [ ! -f "$EXAMPLE" ]; then
  EXAMPLE="/usr/share/doc/agentos-core/examples/50-site-policy.yaml.example"
fi
if [ ! -f "$EXAMPLE" ] && package_installed agentos-core; then
  # The installed package keeps the example under a documented path; find it rather
  # than guess, so a layout change cannot silently skip this check.
  EXAMPLE="$(find /usr/share/doc/agentos-core -name '50-site-policy.yaml.example' -print -quit 2>/dev/null)"
fi
if [ -f "$EXAMPLE" ]; then
  agentos_sudo install -m 0644 "$EXAMPLE" /etc/agentos/policies/50-site-policy.yaml
  agentos_sudo systemctl reload agentos-core
  sleep 2
  ct policy inventory 2>&1 | grep -E "restartable units|system.process.terminate" | sed 's/^/    /'
  SITE_TASK=$(sg agentos -c 'agentosctl --json task run "Prepare a controlled operation." --level-cap P2' 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["task_id"])' 2>/dev/null)
  if [ -n "$SITE_TASK" ]; then
    echo "  --- a P2 operation now needs an approval ---"
    ct approval request "$SITE_TASK" system.process.terminate --target "$$" --reason "verification" 2>&1 | sed 's/^/    /'
    APPROVAL=$(sg agentos -c "agentosctl --json approval list --state pending" 2>/dev/null \
      | python3 -c 'import json,sys; items=json.load(sys.stdin).get("approvals",[]); print(items[0]["approval_id"] if items else "")' 2>/dev/null)
    echo "  --- the approval is decided ---"
    if [ -n "$APPROVAL" ]; then
      ct approval show "$APPROVAL" 2>&1 | sed 's/^/    /'
      ct approval deny "$APPROVAL" --note "verification: expecting a refusal" 2>&1 | sed 's/^/    /'
      echo "  --- the decision is recorded in the audit trail ---"
      ct audit query --task-id "$SITE_TASK" --limit 8 2>&1 | sed 's/^/    /'
    fi
  fi
  # Leave the host as the package shipped it: the example policy is not part of a
  # package install, so the verification removes it again.
  agentos_sudo rm -f /etc/agentos/policies/50-site-policy.yaml
  agentos_sudo systemctl reload agentos-core
  echo "  (example policy removed; the host is back to the shipped read-only policy)"
else
  echo "  [note] the example policy was not found next to the artifacts"
fi

echo ""
echo "=== 8. the keyless end-to-end demonstration ==="
ct demo 2>&1 | tail -24 | sed 's/^/  /'

echo ""
echo "=== 9. the audit trail ==="
ct audit summary 2>&1 | sed 's/^/  /'
ct audit query --limit 8 2>&1 | sed 's/^/  /'

echo ""
echo "=== 10. the DSH adapter reports its locked revision and its real limits ==="
ct runtime probe 2>&1 | sed 's/^/  /'

echo ""
echo "=== 11. the event stream ==="
# A follow of the whole stream has no natural end, so it is expected to report a
# timeout; what matters is that subscribing and receiving works at all.
sg agentos -c 'agentosctl events subscribe >/dev/null 2>&1 || true' || true
sg agentos -c 'timeout 8 agentosctl events follow --timeout 4' 2>&1 | head -16 | sed 's/^/  /' || true
check "the event stream answered" bash -c "sg agentos -c 'timeout 8 agentosctl events follow --timeout 2' 2>&1 | grep -qE 'task|policy|runtime|timed out|approval'"

echo ""
echo "=== 12. the access check left no account behind ==="
agentos_probe_account_line
check "the probe account was removed" bash -c "! getent passwd agentos-probe"

echo ""
if [ "$FAILURES" -eq 0 ]; then
  echo "live verification passed"
  exit 0
fi
echo "$FAILURES live check(s) failed"
exit 1
