#!/usr/bin/env bash
# Install the AgentOS packages from a directory of .deb files.
#
# Why this script exists: APT resolves dependencies only from configured sources,
# so `sudo apt install ./agentos-meta_0.1.0_all.deb` alone fails with
# "agentos-meta depends on agentos-core but it is not installable" 鈥?the other four
# packages are sitting in the same directory but APT never looks there. Passing all
# five files on one command line lets dpkg see them together.
#
# Usage: install-agentos.sh [artifact-directory]
#
#   sudo bash scripts/install-agentos.sh            # from the build directory
#   sudo bash install-agentos.sh ~/涓嬭浇             # from a folder of .debs
set -uo pipefail

DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

if [ "$(id -u)" -ne 0 ]; then
  echo "this script installs packages and must run as root:" >&2
  echo "  sudo bash $0 ${DIR}" >&2
  exit 2
fi

if [ ! -d "$DIR" ]; then
  echo "no such directory: $DIR" >&2
  exit 2
fi

shopt -s nullglob
DEBS=("$DIR"/agentos-*_*_all.deb)
if [ "${#DEBS[@]}" -eq 0 ]; then
  echo "no agentos-*.deb files found in $DIR" >&2
  echo "expected the five packages: core, runtime-dsh, desktop, gnome-extension, meta" >&2
  exit 2
fi

echo "=== installing ${#DEBS[@]} package(s) from $DIR ==="
for deb in "${DEBS[@]}"; do
  printf '  %s\n' "$(basename "$deb")"
done

# All five files on one command line, so dpkg can configure them together and APT
# can still resolve the distribution dependencies (python3-psutil, python3-dbus,
# GTK4, libadwaita) from the system sources.
echo ""
echo "=== installing with APT, so distribution dependencies resolve ==="
if apt-get install -y "${DEBS[@]}"; then
  echo ""
  echo "=== installed ==="
  dpkg-query -W -f '  ${Package} ${Version} ${Status}\n' \
    agentos-core agentos-runtime-dsh agentos-desktop agentos-gnome-extension agentos-meta 2>/dev/null
else
  status=$?
  echo ""
  echo "APT could not satisfy every dependency. Trying dpkg, which installs the"
  echo "packages themselves and reports exactly which dependency is missing:"
  echo ""
  dpkg -i "${DEBS[@]}" || true
  echo ""
  echo "=== unmet dependencies, if any ==="
  dpkg --audit || true
  apt-get -f install -y || true
  if [ "$status" -ne 0 ]; then
    echo ""
    echo "If a package above is still not configured, run:"
    echo "  sudo apt-get -f install"
    echo "and read docs/packaging.md#troubleshooting."
  fi
fi

echo ""
echo "=== next steps ==="
cat <<'EOF'
  1. Check the installation, which needs no model credential and no network:
       agentosctl doctor

  2. Give your own user access to the service socket. The service accepts IPC only
     from members of the agentos group, so this is required, not optional:
       sudo adduser "$USER" agentos
     Then LOG OUT AND BACK IN: a new group membership only applies to a new session.

  3. Start the service and verify it:
       sudo systemctl start agentos-core
       agentosctl health

  4. Show the AgentOS panel (run as your own user, NOT with sudo):
       gnome-extensions enable agentos@agentos.dev
     Then log out and back in.

  5. Run the keyless end-to-end demonstration:
       agentosctl demo
EOF
