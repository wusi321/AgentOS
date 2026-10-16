
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
﻿#!/usr/bin/env bash
# Build a local APT repository from the AgentOS .deb files, and configure APT to use it.
#
# This is the answer to the most confusing install failure:
#
#   sudo apt install ./agentos-meta_0.1.0_all.deb
#   agentos-meta depends on agentos-core but it is not installable
#
# APT resolves dependencies only from configured sources; it does not look at the
# other .deb files sitting next to the one you named. A local repository makes every
# package visible, so `apt install agentos-meta` resolves the whole set 鈥?and the
# distribution dependencies (python3-psutil, python3-dbus, GTK4, libadwaita) from the
# normal Ubuntu sources at the same time.
#
# Usage: make-local-repo.sh [artifact-directory] [repository-directory]
#
#   sudo bash scripts/make-local-repo.sh build/debs
#   sudo apt update && sudo apt install agentos-meta
#
# Re-running the script after a new build refreshes the repository in place. Pass
# --remove as the first argument to take the repository back out of APT's config.
set -uo pipefail

REMOVE=""
if [ "${1:-}" = "--remove" ]; then
  REMOVE="yes"
  shift
fi

DIR="${1:-$HERE/../build/debs}"
REPO="${2:-/opt/agentos-repo}"
SOURCES_LIST="/etc/apt/sources.list.d/agentos-local.list"

if [ "$(id -u)" -ne 0 ]; then
  echo "this script changes APT configuration and must run as root:" >&2
  echo "  sudo bash $0 ${DIR}" >&2
  exit 2
fi

if [ -n "$REMOVE" ]; then
  echo "=== removing the local AgentOS repository from APT ==="
  rm -f "$SOURCES_LIST"
  rm -rf "$REPO"
  echo "  removed $SOURCES_LIST"
  echo "  removed $REPO"
  echo ""
  echo "  The packages stay installed. To remove them as well:"
  echo "    sudo apt remove agentos-meta agentos-gnome-extension agentos-desktop agentos-runtime-dsh agentos-core"
  exit 0
fi

if [ ! -d "$DIR" ]; then
  echo "no such directory: $DIR" >&2
  exit 2
fi

shopt -s nullglob
DEBS=("$DIR"/agentos-*_*_all.deb)
if [ "${#DEBS[@]}" -eq 0 ]; then
  echo "no agentos-*.deb files found in $DIR" >&2
  exit 2
fi

echo "=== building the repository in $REPO ==="
rm -rf "$REPO"
install -d -m 0755 "$REPO"
cp -f "${DEBS[@]}" "$REPO/"
# The checksum manifest is not a package; keep it beside them for the operator.
[ -f "$DIR/SHA256SUMS" ] && cp -f "$DIR/SHA256SUMS" "$REPO/"
[ -f "$DIR/MANIFEST.json" ] && cp -f "$DIR/MANIFEST.json" "$REPO/"

cd "$REPO"

if command -v dpkg-scanpackages >/dev/null 2>&1; then
  # 2>/dev/null: dpkg-scanpackages warns about the SHA256SUMS file, which is not a
  # package and is not meant to be.
  dpkg-scanpackages --multiversion . /dev/null 2>/dev/null | gzip -9c >Packages.gz
  dpkg-scanpackages --multiversion . /dev/null 2>/dev/null >Packages
else
  echo "dpkg-scanpackages is unavailable; writing the Packages index by hand"
  python3 - "$REPO" <<'PY'
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
entries = []
for deb in sorted(repo.glob('agentos-*.deb')):
    control = subprocess.run(
        ['dpkg-deb', '-f', str(deb)], capture_output=True, text=True, check=True
    ).stdout
    filename = deb.name
    size = deb.stat().st_size
    entry = control.rstrip('\n') + f'\nFilename: ./{filename}\nSize: {size}\n\n'
    entries.append(entry)
(repo / 'Packages').write_text(''.join(entries), encoding='utf-8')
print(f'  wrote {len(entries)} entries')
PY
  gzip -9c Packages >Packages.gz
fi

if [ ! -s Packages ] && [ ! -s Packages.gz ]; then
  echo "the package index is empty; refusing to configure APT" >&2
  exit 1
fi

echo "  index entries: $(grep -c '^Package:' Packages 2>/dev/null || echo '?')"

# Release/InRelease are optional for a [trusted=yes] source. The file is written so
# that tools which expect one do not complain.
if command -v apt-ftparchive >/dev/null 2>&1; then
  apt-ftparchive release . >Release 2>/dev/null || true
fi

echo ""
echo "=== configuring APT ==="
cat >"$SOURCES_LIST" <<EOF
# Local AgentOS repository, created by scripts/make-local-repo.sh.
#
# trusted=yes is required because these packages are not signed. That is acceptable
# for a repository you built yourself from a checksummed artifact set; it is NOT
# acceptable for a repository anyone else can write to.
#
# Remove with: sudo bash scripts/make-local-repo.sh --remove
deb [trusted=yes] file:$REPO ./
EOF
echo "  wrote $SOURCES_LIST"

echo ""
echo "=== refreshing APT ==="
apt-get update -o Dir::Etc::sourcelist="$SOURCES_LIST" -o Dir::Etc::sourceparts="-" -o APT::Get::List-Cleanup="0" 2>&1 | tail -5

echo ""
echo "=== what APT can now see ==="
apt-cache policy agentos-core agentos-meta 2>/dev/null | sed 's/^/  /'

cat <<'EOF'

=== install it ===

    sudo apt install agentos-meta

That resolves all five AgentOS packages plus their distribution dependencies in one
step. For a smaller deployment, install a subset instead:

    sudo apt install agentos-core                    # the service only
    sudo apt install agentos-core agentos-desktop    # add the window
EOF
