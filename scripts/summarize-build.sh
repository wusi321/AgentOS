#!/usr/bin/env bash
# Summarize a completed build: artifacts, checksums, and the size of the source.
#
# Usage: summarize-build.sh [repo-root]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${1:-$(cd "$HERE/.." && pwd)}"
cd "$ROOT"

echo "=== artifacts ==="
ls -lh build/debs/*.deb build/debs/MANIFEST.json build/debs/SHA256SUMS 2>/dev/null

echo ""
echo "=== checksums ==="
cat build/debs/SHA256SUMS 2>/dev/null

echo ""
echo "=== source size ==="
printf '  %-28s %s\n' "python modules" "$(find src -name '*.py' | wc -l) files, $(find src -name '*.py' -exec cat {} + | wc -l) lines"
printf '  %-28s %s\n' "gnome extension" "$(find gnome-extension -type f \( -name '*.js' -o -name '*.xml' -o -name '*.css' \) | wc -l) files, $(find gnome-extension -type f \( -name '*.js' -o -name '*.xml' -o -name '*.css' \) -exec cat {} + | wc -l) lines"
printf '  %-28s %s\n' "tests" "$(find tests -name '*.py' | wc -l) files, $(find tests -name '*.py' -exec cat {} + | wc -l) lines"
printf '  %-28s %s\n' "documentation" "$(find docs -name '*.md' | wc -l) files, $(find docs -name '*.md' -exec cat {} + | wc -l) lines"
printf '  %-28s %s\n' "packaging + scripts" "$(find debian packaging scripts -type f | wc -l) files"
printf '  %-28s %s\n' "adr records" "$(find docs/adr -name 'ADR-*.md' | wc -l)"

echo ""
echo "=== test suite ==="
grep -c 'def test_' tests/unit/*.py tests/security/*.py tests/contract/*.py 2>/dev/null | awk -F: '{sum += $2} END {print "  test functions: " sum}'
