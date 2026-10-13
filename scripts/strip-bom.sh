#!/usr/bin/env bash
# Remove a UTF-8 BOM from text files and normalise them to LF endings.
#
# Editing tools on Windows can add a BOM or CRLF endings; both break parsers that
# are stricter than Python's, so this script is run before packaging and checks.
#
# Usage: strip-bom.sh [path ...]   (defaults to the repository root)
set -euo pipefail

root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

mapfile -t files < <(
  find "$root" -type f \
    \( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.json' \
       -o -name '*.js' -o -name '*.css' -o -name '*.xml' -o -name '*.md' -o -name '*.sh' \
       -o -name 'control' -o -name 'rules' -o -name 'changelog' -o -name 'copyright' \
       -o -name 'agentosctl' -o -name 'agentos-core' -o -name '*.service' -o -name '*.socket' \
       -o -name '*.conf' -o -name '*.install' -o -name '*.desktop' \) \
    -not -path '*/.git/*' -not -path '*/upstream/*' -not -path '*/node_modules/*' -not -path '*/build/*' \
    -not -path '*/debian/.debhelper/*' -print
)

changed=0
for file in "${files[@]}"; do
  if head -c 3 "$file" | od -An -tx1 | grep -q 'ef bb bf'; then
    tail -c +4 "$file" > "$file.nobom" && mv "$file.nobom" "$file"
    echo "stripped BOM: ${file#"$root"/}"
    changed=$((changed + 1))
  fi
  if grep -qU $'\r' "$file" 2>/dev/null; then
    sed -i 's/\r$//' "$file"
    echo "normalised to LF: ${file#"$root"/}"
    changed=$((changed + 1))
  fi
done

echo "files adjusted: $changed"
