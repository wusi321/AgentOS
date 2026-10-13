#!/usr/bin/env bash
# Verify the locked DSH revision's availability and shape on the local machine.
#
# The design outline requires that the exact upstream revision is reproducible and
# that its Node.js engine requirement is checked rather than assumed. This script
# reports the facts; scripts/check-dsh-compat.sh turns them into a pass/fail gate.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCKED_VERSION="0.2.1-alpha.2"
LOCKED_COMMIT="d743267388641bc76f17c45ce8b4c231aed1d32c"

echo "=== locked revision ==="
echo "version: $LOCKED_VERSION"
echo "commit:  $LOCKED_COMMIT"

echo "=== upstream checkout ==="
CHECKOUT="$REPO_ROOT/upstream/deepseek-harness"
if [ -d "$CHECKOUT/.git" ]; then
  echo "path:    $CHECKOUT"
  echo "head:    $(git -C "$CHECKOUT" rev-parse HEAD)"
  echo "tag:     $(git -C "$CHECKOUT" describe --tags 2>/dev/null || echo '(none)')"
  echo "engines: $(python3 -c "import json;print(json.load(open('$CHECKOUT/package.json')).get('engines'))")"
else
  echo "path:    $CHECKOUT (absent; run scripts/fetch-dsh.sh)"
fi

echo "=== npm registry ==="
if command -v curl >/dev/null 2>&1; then
  curl -s --max-time 25 "https://registry.npmjs.org/@deepseek-ai%2Fdsh" -o /tmp/agentos-dsh-registry.json || true
  if [ -s /tmp/agentos-dsh-registry.json ]; then
    python3 - <<'PY'
import json
try:
    data = json.load(open("/tmp/agentos-dsh-registry.json"))
except Exception as error:
    print(f"registry response unreadable: {error}")
    raise SystemExit(0)
versions = list(data.get("versions", {}))
print(f"versions published: {len(versions)}")
print(f"latest few: {versions[-6:]}")
locked = data.get("versions", {}).get("0.2.1-alpha.2")
if locked:
    print(f"locked version present: yes (unpacked {locked.get('dist', {}).get('unpackedSize')} bytes)")
    print(f"locked engines: {locked.get('engines')}")
else:
    print("locked version present: no")
PY
  else
    echo "registry unreachable"
  fi
fi

echo "=== node runtime on this machine ==="
for candidate in /usr/bin/node /usr/local/bin/node "$(command -v node 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    echo "$candidate -> $("$candidate" --version)"
  fi
done
