#!/usr/bin/env bash
# Prepare an Ubuntu/Debian build host for AgentOS .deb packaging.
# Safe to re-run; only installs missing development dependencies.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Password may be supplied as AGENTOS_SUDO_PASSWORD; otherwise rely on passwordless sudo.
if [[ -n "${AGENTOS_SUDO_PASSWORD:-}" ]]; then
  sudo() { command sudo -S -p '' "$@" <<<"$AGENTOS_SUDO_PASSWORD"; }
fi

sudo apt-get update -qq

sudo apt-get install -y -qq --no-install-recommends \
  build-essential devscripts debhelper dh-python fakeroot lintian \
  python3-all python3-venv python3-pytest python3-psutil python3-pip \
  python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
  desktop-file-utils xvfb dbus-x11 xdg-utils ca-certificates curl xz-utils

echo "=== toolchain ==="
dpkg-buildpackage --version | head -1
dh --version
fakeroot --version
lintian --version
python3 --version
echo "=== python3-pydantic availability ==="
apt-cache policy python3-pydantic | head -3
echo "=== DONE-APT ==="
