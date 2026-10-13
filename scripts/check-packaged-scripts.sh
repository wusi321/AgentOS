#!/usr/bin/env bash
# Syntax-check the maintainer scripts as they appear inside built packages.
#
# debhelper rewrites maintainer scripts: it substitutes the #DEBHELPER# token with
# generated code, and dh_python3 adds a byte-compilation block. A token placed
# inside a prose comment makes that substitution shred the surrounding comment,
# which produces a script that cannot parse. Only the packaged copy reveals it, so
# this check runs against the .deb rather than against the source tree.
#
# Usage: check-packaged-scripts.sh <artifact-directory>
set -uo pipefail

ARTIFACTS="${1:-/mnt/d/桌面/AgentOS/build/debs}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

status=0
found=0

for deb in "$ARTIFACTS"/agentos-*.deb; do
  [ -f "$deb" ] || continue
  found=$((found + 1))
  package=$(dpkg-deb -f "$deb" Package)
  scripts_dir="$WORK/$package"
  mkdir -p "$scripts_dir"
  dpkg-deb --ctrl-tarfile "$deb" | tar -x -C "$scripts_dir" 2>/dev/null || true

  echo "--- $package ---"
  any=0
  for script in "$scripts_dir"/postinst "$scripts_dir"/preinst "$scripts_dir"/prerm "$scripts_dir"/postrm; do
    [ -f "$script" ] || continue
    any=1
    name=$(basename "$script")
    if sh -n "$script"; then
      printf '  [ok  ] %s parses\n' "$name"
    else
      printf '  [FAIL] %s does not parse after debhelper processing\n' "$name"
      printf '         lines around the failure:\n'
      sh -n "$script" 2>&1 | sed 's/^/           /'
      status=1
    fi
    # A comment line that continues after an inserted block is the signature of a
    # #DEBHELPER# token that was written inside prose.
    if grep -qE '^[[:space:]]+[a-z]+,? (so this|and the|block|which|is invoked)' "$script"; then
      printf '  [FAIL] %s looks like a comment that an inserted block split\n' "$name"
      status=1
    fi
  done
  [ "$any" -eq 1 ] || printf '  (no maintainer scripts)\n'
done

if [ "$found" -eq 0 ]; then
  echo "no packages found in $ARTIFACTS" >&2
  exit 2
fi

echo ""
if [ "$status" -eq 0 ]; then
  echo "all packaged maintainer scripts parse"
fi
exit "$status"
