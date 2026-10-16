#!/usr/bin/env bash
# Build the AgentOS Debian packages in a native Linux working directory.
#
# The repository usually lives on a Windows drive mounted through the WSL 9p
# filesystem, which reports every file as mode 0777 and silently ignores chmod.
# Debian packaging depends on the executable bit: debhelper treats an executable
# debian/*.install as a script, and a non-executable maintainer script is a
# policy violation. The build therefore runs from a copy on the native
# filesystem, and the resulting artifacts are copied back.
#
# Usage: build-debs-native.sh [extra root to exclude]

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -euo pipefail
# nullglob makes an unmatched glob expand to nothing instead of to the literal
# pattern, which is what the artifact collection below relies on to detect an
# empty build result.
shopt -s nullglob

REPO_SOURCE="${AGENTOS_REPO:-$(cd "$HERE/.." && pwd)}"
WORK="${AGENTOS_BUILD_DIR:-/tmp/agentos-build}"
OUT="${AGENTOS_OUT:-$REPO_SOURCE/build/debs}"

echo "== source tree =="
echo "  from: $REPO_SOURCE"
echo "  to:   $WORK"
echo "  out:  $OUT"

# The upstream harness checkout is a verification input, not a packaging input:
# it is large and its permissions are irrelevant. Excluding it keeps the copy
# fast and the build tree small.
EXCLUDES=(
  --exclude=upstream
  --exclude=.git
  --exclude=build
  --exclude=__pycache__
  --exclude='*.pyc'
  --exclude='*.deb'
  --exclude='*.buildinfo'
  --exclude='*.changes'
  --exclude=debian/.debhelper
)

rm -rf "$WORK"
mkdir -p "$WORK"
tar -C "$REPO_SOURCE" "${EXCLUDES[@]}" -cf - . | tar -C "$WORK" -xf -

cd "$WORK"

echo "== applying Unix file modes lost by the Windows mount =="
# The Windows filesystem reports every file as mode 0777 and ignores chmod, and
# tar preserves that. Normalising here is what makes the copy behave like a
# source tree rather than like a share: lintian rejects a data file that carries
# the executable bit, and debhelper treats an executable debian/*.install as a
# helper script.
find . -type d -exec chmod 0755 {} +
find . -type f -exec chmod 0644 {} +
# Restore the executable bit only where it is required.
chmod 0755 debian/rules
for script in debian/agentos-*.postinst debian/agentos-*.prerm debian/agentos-*.postrm debian/agentos-*.preinst; do
  [ -e "$script" ] && chmod 0755 "$script"
done
chmod 0755 bin/agentosctl bin/agentos-core bin/agentos-session bin/agentos-desktop
for helper in scripts/*.sh scripts/*.py; do
  [ -e "$helper" ] && chmod 0755 "$helper"
done
# The extension's JavaScript is data, not a program GNOME Shell executes.
chmod 0644 gnome-extension/*.js gnome-extension/*.json gnome-extension/*.css 2>/dev/null || true
echo "  directories 0755, data files 0644, entry points and maintainer scripts 0755"

echo "== verifying that every declared file exists =="
python3 scripts/check-packaging.py

echo "== syntax-checking the maintainer scripts =="
for script in debian/agentos-*.postinst debian/agentos-*.prerm debian/agentos-*.postrm; do
  [ -e "$script" ] || continue
  sh -n "$script" || { echo "syntax error in $script" >&2; exit 1; }
done
echo "  maintainer scripts parse"

echo "== building =="
rm -rf "$OUT"
mkdir -p "$OUT"
# dpkg-buildpackage writes into the parent of the build tree, which for a copy at
# /tmp/agentos-build is /tmp. The artifact collection below reads from there.
ARTIFACT_DIR="$(dirname "$WORK")"
rm -f "$ARTIFACT_DIR"/agentos_*.deb "$ARTIFACT_DIR"/agentos_*.buildinfo "$ARTIFACT_DIR"/agentos_*.changes

# -b binary only, -us -uc unsigned, -d skip the build-dependency check that
# cannot pass offline. Rules-Requires-Root: no, so no fakeroot is needed.
dpkg-buildpackage -b -us -uc -d

echo "== collecting artifacts =="
# The artifact set is derived from the package names in debian/control rather
# than from a filename pattern, so a renamed or missing package is reported
# explicitly instead of being silently absent from the collected output.
mapfile -t packages < <(
  sed -n 's/^Package:[[:space:]]*//p' "$WORK/debian/control" | sort -u
)
debs=()
for package in "${packages[@]}"; do
  candidate="$ARTIFACT_DIR/${package}_0.1.0_all.deb"
  if [ -f "$candidate" ]; then
    debs+=("$candidate")
  else
    # Fall back to a search, which also covers an epoch or revision change.
    while IFS= read -r found; do
      [ -n "$found" ] && debs+=("$found")
    done < <(find "$ARTIFACT_DIR" -maxdepth 1 -type f -name "${package}[_-]*.deb" -print | sort)
  fi
done
mapfile -t others < <(
  find "$ARTIFACT_DIR" -maxdepth 1 -type f -name 'agentos_*.buildinfo' -print
  find "$ARTIFACT_DIR" -maxdepth 1 -type f -name 'agentos_*.changes' -print
)
echo "  expected packages: ${packages[*]}"
echo "  found ${#debs[@]} package(s) and ${#others[@]} metadata file(s) in $ARTIFACT_DIR"
if [ "${#debs[@]}" -eq 0 ]; then
  echo "no .deb was produced; expected them in $ARTIFACT_DIR" >&2
  exit 1
fi
if [ "${#debs[@]}" -ne "${#packages[@]}" ]; then
  echo "warning: ${#packages[@]} packages are declared but ${#debs[@]} were built" >&2
fi
cp -f "${debs[@]}" "$OUT/"
if [ "${#others[@]}" -gt 0 ]; then
  cp -f "${others[@]}" "$OUT/"
fi

echo ""
echo "== artifacts =="
ls -lh "$OUT"
echo ""
echo "== checksums =="
( cd "$OUT" && sha256sum ./*.deb | tee SHA256SUMS )
echo ""
echo "== package contents summary =="
for deb in "${debs[@]}"; do
  echo "--- $(basename "$deb") ---"
  dpkg-deb -f "$deb" Package Version Architecture
  echo "    Depends: $(dpkg-deb -f "$deb" Depends)"
  echo "    files:   $(dpkg-deb -c "$deb" | grep -c '^-')"
done

echo ""
echo "== release manifest =="
python3 "$REPO_SOURCE/scripts/write-manifest.py" "$OUT"
