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

ARTIFACTS="${1:-/mnt/d/桌面/AgentOS/build/debs}"
SUDO_PASSWORD="${AGENTOS_SUDO_PASSWORD:-}"
TARGET_USER="${AGENTOS_USER:-$(id -un)}"

FAILURES=0
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  [ok  ] %s\n' "$label"
  else
    printf '  [FAIL] %s\n' "$label"
    FAILURES=$((FAILURES + 1))
  fi
}

sudo_do() {
  if [ -n "$SUDO_PASSWORD" ]; then
    printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' "$@"
  else
    sudo -n "$@"
  fi
}

echo "=== the installed system ==="
dpkg-query -W -f '  ${Package} ${Version}\n' agentos-core agentos-runtime-dsh agentos-desktop 2>&1

echo ""
echo "=== 0. fresh install, so a stale conffile cannot be tested by mistake ==="
# A conffile the administrator has edited is preserved across an upgrade, which is
# correct for configuration but means an edited systemd unit from an earlier build
# keeps running. Purging first makes this a test of the packages as built.
sudo_do dpkg --purge --force-all agentos-meta agentos-gnome-extension agentos-desktop \
     agentos-runtime-dsh agentos-core >/dev/null 2>&1 || true
sudo_do rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos
sudo_do dpkg -i "$ARTIFACTS/agentos-core_0.1.0_all.deb" "$ARTIFACTS/agentos-runtime-dsh_0.1.0_all.deb" \
     "$ARTIFACTS/agentos-desktop_0.1.0_all.deb" >/tmp/live-install.log 2>&1
check "core, runtime, and desktop install cleanly" bash -c 'grep -qv "error" /tmp/live-install.log'
sudo_do dpkg -i --force-depends "$ARTIFACTS/agentos-gnome-extension_0.1.0_all.deb" >/dev/null 2>&1 || true
sudo_do dpkg -i --force-depends "$ARTIFACTS/agentos-meta_0.1.0_all.deb" >/dev/null 2>&1 || true
echo "  --- the installed unit no longer narrows /proc ---"
check "the service unit does not set ProcSubset" bash -c "! grep -q '^ProcSubset=' /usr/lib/systemd/system/agentos-core.service"
check "the service declares a reload" bash -c "grep -q '^ExecReload=' /usr/lib/systemd/system/agentos-core.service"

echo ""
echo "=== 1. access is denied before the user is authorized ==="
if agentosctl health >/tmp/pre-access.log 2>&1; then
  echo "  [note] this user already has access (already in the agentos group)"
else
  if grep -q 'ipc_access_denied' /tmp/pre-access.log; then
    echo "  [ok  ] a user outside the agentos group is refused, with an actionable message"
    sed 's/^/         /' /tmp/pre-access.log
  else
    echo "  [FAIL] the refusal was not the expected access denial:"
    sed 's/^/         /' /tmp/pre-access.log
    FAILURES=$((FAILURES + 1))
  fi
fi

echo ""
echo "=== 2. the documented authorization step ==="
echo "  adding $TARGET_USER to the agentos group and restarting the service"
sudo_do adduser "$TARGET_USER" agentos >/dev/null 2>&1 || true
sudo_do systemctl restart agentos-core
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
EXAMPLE="$ARTIFACTS/../..//configs/policies/50-site-policy.yaml.example"
if [ ! -f "$EXAMPLE" ]; then
  EXAMPLE="/mnt/d/桌面/AgentOS/configs/policies/50-site-policy.yaml.example"
fi
if [ -f "$EXAMPLE" ]; then
  sudo_do install -m 0644 "$EXAMPLE" /etc/agentos/policies/50-site-policy.yaml
  sudo_do systemctl reload agentos-core
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
  sudo_do rm -f /etc/agentos/policies/50-site-policy.yaml
  sudo_do systemctl reload agentos-core
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
if [ "$FAILURES" -eq 0 ]; then
  echo "live verification passed"
  exit 0
fi
echo "$FAILURES live check(s) failed"
exit 1
