#!/usr/bin/env bash
# Check the built artifacts for corruption before blaming the installer.
#
# Intermittent failures across unrelated stages have two possible causes: the
# environment, or the artifacts themselves. The checksums settle which, because a
# .deb that does not match its recorded digest cannot be trusted to install
# consistently.
#
# Usage: check-artifacts.sh [artifact-directory]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:-$HERE/../build/debs}"

echo "=== artifact checksums against SHA256SUMS ==="
if [ ! -f "$DIR/SHA256SUMS" ]; then
  echo "  no SHA256SUMS in $DIR" >&2
  exit 2
fi
if (cd "$DIR" && sha256sum -c SHA256SUMS); then
  echo ""
  echo "  every artifact matches its recorded digest"
else
  echo ""
  echo "  at least one artifact does not match; rebuild before verifying" >&2
  exit 1
fi

echo ""
echo "=== the core package is readable and well formed ==="
CORE="$DIR/agentos-core_0.1.0_all.deb"
for field in Package Version Architecture Depends Conffiles; do
  printf '  %-14s %s\n' "$field" "$(dpkg-deb -f "$CORE" "$field" 2>&1 | head -1)"
done
printf '  %-14s %s\n' "data files" "$(dpkg-deb --contents "$CORE" 2>/dev/null | grep -c '^-')"
printf '  %-14s %s\n' "conffiles" "$(dpkg-deb --ctrl-tarfile "$CORE" 2>/dev/null | tar -tO ./conffiles 2>/dev/null | grep -c '^/etc' || echo '?')"

echo ""
echo "=== the configuration files are inside the archive ==="
TMP=$(mktemp -d)
dpkg-deb --extract "$CORE" "$TMP"
find "$TMP/etc" -type f -printf '  %s\t%P\n' 2>/dev/null | sort
rm -rf "$TMP"

echo ""
echo "=== repeated extraction gives the same bytes ==="
FIRST=$(mktemp -d)
SECOND=$(mktemp -d)
dpkg-deb --extract "$CORE" "$FIRST"
dpkg-deb --extract "$CORE" "$SECOND"
if diff -r "$FIRST" "$SECOND" >/dev/null 2>&1; then
  echo "  two extractions are byte-identical"
else
  echo "  two extractions differ; the archive is unstable" >&2
  rm -rf "$FIRST" "$SECOND"
  exit 1
fi
rm -rf "$FIRST" "$SECOND"

echo ""
echo "artifacts are intact"
