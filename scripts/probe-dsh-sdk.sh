#!/usr/bin/env bash
# P0 DSH integration probe.
#
# Verifies, on this machine and without a model credential, that:
#   1. the locked DSH revision is available from the npm registry;
#   2. the locked revision's Node.js engine requirement is satisfied locally;
#   3. the `dsh` CLI can be installed from the locked version and reports its version;
#   4. `dsh --profile sdk` starts and answers the JSON-RPC `initialize` handshake.
#
# Step 4 is the SDK wire contract AgentOS's DshAdapter depends on, so passing it
# is what converts "the protocol is documented this way" into "the protocol
# behaves this way on the locked revision".
#
# Usage: probe-dsh-sdk.sh [work-directory]
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCKED_VERSION="0.2.1-alpha.2"
WORK="${1:-/tmp/agentos-dsh-probe}"
NODE_BIN="${AGENTOS_NODE_BIN:-}"

log() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# --- 1. locate Node -------------------------------------------------------
if [ -z "$NODE_BIN" ]; then
  for candidate in "$HOME/.local/bin/node" /usr/local/bin/node /usr/bin/node "$(command -v node 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then NODE_BIN="$candidate"; break; fi
  done
fi
[ -n "$NODE_BIN" ] || fail "no Node.js executable found; install the agentos-runtime-dsh package or a Node.js 22.19+ runtime"

NODE_VERSION="$("$NODE_BIN" --version)"
NODE_MAJOR="${NODE_VERSION#v}"; NODE_MAJOR="${NODE_MAJOR%%.*}"
NODE_REST="${NODE_VERSION#v*.}"; NODE_MINOR="${NODE_REST%%.*}"
log "node:    $NODE_BIN ($NODE_VERSION)"

if [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 19 ]; then
  fail "Node.js $NODE_VERSION does not satisfy the locked DSH requirement '^22.19.0 || >=24.0.0'"
fi
if [ "$NODE_MAJOR" -lt 22 ]; then
  fail "Node.js $NODE_VERSION does not satisfy the locked DSH requirement '^22.19.0 || >=24.0.0'"
fi

NPM_CLI=""
for candidate in "$(dirname "$NODE_BIN")/npm" "$(dirname "$NODE_BIN")/../lib/node_modules/npm/bin/npm-cli.js"; do
  [ -e "$candidate" ] && NPM_CLI="$candidate" && break
done
[ -n "$NPM_CLI" ] || fail "npm was not found next to $NODE_BIN"

rm -rf "$WORK"
mkdir -p "$WORK/install"
log "workdir: $WORK"

# Dependency lifecycle scripts (for example koffi's native build) shell out to
# `node`, so the interpreter's directory must be on PATH for the install itself.
export PATH="$(dirname "$NODE_BIN"):$PATH"

# --- 2. install the locked revision ---------------------------------------
log "installing @deepseek-ai/dsh@$LOCKED_VERSION (this downloads the harness bundle)…"
if ! ( cd "$WORK/install" \
    && "$NODE_BIN" "$NPM_CLI" install --no-audit --no-fund --loglevel=error \
         "@deepseek-ai/dsh@$LOCKED_VERSION" >"$WORK/npm-install.log" 2>&1 ); then
  log "npm install failed; retrying without dependency lifecycle scripts"
  log "(the SDK wire probe needs only the JavaScript bundle, not native rebuilds)"
  if ! ( cd "$WORK/install" \
      && "$NODE_BIN" "$NPM_CLI" install --no-audit --no-fund --loglevel=error --ignore-scripts \
           "@deepseek-ai/dsh@$LOCKED_VERSION" >"$WORK/npm-install.log" 2>&1 ); then
    log "npm install failed; last 30 log lines:"
    tail -30 "$WORK/npm-install.log" >&2
    fail "cannot install the locked DSH revision"
  fi
fi

DSH_BIN="$WORK/install/node_modules/@deepseek-ai/dsh/lib/bin.js"
[ -f "$DSH_BIN" ] || fail "installed package has no lib/bin.js at $DSH_BIN"
log "cli:     $DSH_BIN"

INSTALLED_VERSION="$("$NODE_BIN" "$DSH_BIN" --version 2>"$WORK/version.err" | tail -1)"
log "version: $INSTALLED_VERSION"
if [ -s "$WORK/version.err" ]; then
  log "version stderr: $(head -3 "$WORK/version.err")"
fi
case "$INSTALLED_VERSION" in
  *"$LOCKED_VERSION"*) log "locked version confirmed" ;;
  *) log "WARNING: version output did not contain $LOCKED_VERSION" ;;
esac

# --- 3. probe the SDK profile over JSON-RPC stdio -------------------------
export DSH_HOME="$WORK/dsh-home"
mkdir -p "$DSH_HOME/workspace"

PROBE="$REPO_ROOT/scripts/dsh_sdk_probe.py"
[ -f "$PROBE" ] || fail "missing $PROBE"

log "probing 'dsh --profile sdk' initialize handshake…"
# The locked revision's SDK profile ships `agent-default-model` with
# provider `deepseek-official` and model `deepseek-flash`; the probe uses those
# unless the caller overrides them.
export AGENTOS_PROBE_PROVIDER="${AGENTOS_PROBE_PROVIDER:-deepseek-official}"
export AGENTOS_PROBE_MODEL="${AGENTOS_PROBE_MODEL:-deepseek-flash}"
if PYTHONPATH="$REPO_ROOT/src" python3 "$PROBE" "$NODE_BIN" "$DSH_BIN" "$DSH_HOME" "$DSH_HOME/workspace"; then
  log "SDK wire probe passed"
else
  fail "SDK wire probe failed (see output above)"
fi
