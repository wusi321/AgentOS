#!/usr/bin/env bash
# Dump the locked DSH revision's SDK profile configuration.
#
# The goal is to read the provider and model route names the locked revision
# actually registers, so that AgentOS configuration, documentation, and tests
# name a real route instead of a guess.
#
# Usage: dump-dsh-config.sh [work-directory]
set -uo pipefail

LOCKED_VERSION="0.2.1-alpha.2"
WORK="${1:-/tmp/agentos-dsh-config}"
NODE_BIN="${AGENTOS_NODE_BIN:-$HOME/.local/bin/node}"
NPM_CLI="$(dirname "$NODE_BIN")/npm"

export PATH="$(dirname "$NODE_BIN"):$PATH"
mkdir -p "$WORK/install"
cd "$WORK/install"

DSH_BIN="$WORK/install/node_modules/@deepseek-ai/dsh/lib/bin.js"
if [ ! -f "$DSH_BIN" ]; then
  echo "installing @deepseek-ai/dsh@$LOCKED_VERSION …"
  "$NODE_BIN" "$NPM_CLI" install --no-audit --no-fund --loglevel=error --ignore-scripts \
    "@deepseek-ai/dsh@$LOCKED_VERSION" >"$WORK/install.log" 2>&1 || {
      tail -20 "$WORK/install.log"; exit 1; }
fi

export DSH_HOME="$WORK/dsh-home"
mkdir -p "$DSH_HOME"

echo "=== CLI help (launcher) ==="
"$NODE_BIN" "$DSH_BIN" --help 2>&1 | head -40

echo ""
echo "=== SDK profile: default config ==="
"$NODE_BIN" "$DSH_BIN" --profile sdk --dump-default-config 2>&1 | head -120

echo ""
echo "=== provider names mentioned in the resolved config ==="
"$NODE_BIN" "$DSH_BIN" --profile sdk --dump-config 2>&1 \
  | grep -iE 'provider|model|adapter' | head -40

echo ""
echo "=== provider route names referenced by the SDK app bundle ==="
BUNDLE_DIR="$WORK/install/node_modules/@deepseek-ai"
if [ -d "$BUNDLE_DIR" ]; then
  grep -rhoE "\"[a-z][a-z0-9-]*(deepseek|openai|anthropic|mock)[a-z0-9-]*\"" "$BUNDLE_DIR" 2>/dev/null | sort -u | head -30
fi
