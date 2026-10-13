#!/usr/bin/env bash
# Build the AgentOS Debian packages.
#
# Usage: build-debs.sh [output-directory]
#
# The build is offline: it compiles nothing, fetches nothing, and installs
# nothing. Every shipped file is declared in a debian/*.install file, so the
# result is reproducible from the source tree alone.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/build/debs}"
cd "$ROOT"

echo "== checking the build toolchain =="
missing=()
for tool in dpkg-buildpackage fakeroot dh; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "missing build tools: ${missing[*]}" >&2
  echo "install them with:" >&2
  echo "  sudo apt install build-essential devscripts debhelper dh-python fakeroot lintian python3-all" >&2
  exit 2
fi

echo "== normalising file encoding =="
bash "$ROOT/scripts/strip-bom.sh" "$ROOT"

echo "== verifying that every declared file exists =="
python3 "$ROOT/scripts/check-packaging.py"

echo "== building =="
rm -rf "$OUT"
mkdir -p "$OUT"
# -b builds binary packages only, -us -uc skips signing, -d skips a
# build-dependency check that cannot pass offline on a machine without every
# optional tool installed. The build never needs root: Rules-Requires-Root: no.
dpkg-buildpackage -b -us -uc -d 2>&1 | tail -25

echo "== collecting artifacts =="
shopt -s nullglob
artifacts=("$ROOT"/../agentos_*.deb "$ROOT"/../agentos_*.buildinfo "$ROOT"/../agentos_*.changes)
if [ "${#artifacts[@]}" -eq 0 ]; then
  echo "no artifacts were produced" >&2
  exit 1
fi
for artifact in "${artifacts[@]}"; do
  mv -f "$artifact" "$OUT/" 2>/dev/null || cp -f "$artifact" "$OUT/"
done

echo ""
echo "== artifacts in $OUT =="
ls -1 "$OUT"
echo ""
echo "== checksums =="
( cd "$OUT" && sha256sum ./*.deb | tee SHA256SUMS )
