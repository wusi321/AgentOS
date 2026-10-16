# Shared shell helpers for the AgentOS verification scripts.
#
# Source this file; do not execute it. It exists because three of those scripts
# manage packages, and each of them needs the same four things done correctly:
#
#   * wait for the APT lock instead of failing when Ubuntu's daily timers hold it;
#   * pass only *installed* package names to apt, because `apt-get purge a b c`
#     exits 100 and does *nothing at all* when any one of them is not installed —
#     which silently leaves packages and their conffiles in place;
#   * ask about installation state through dpkg, whose output must be inspected
#     because it exits 0 either way;
#   * treat `systemctl` on a missing unit as a non-event.
#
# Usage:
#   . "$(dirname "$0")/lib-agentos-verify.sh"
#   purge_packages agentos-core agentos-runtime-dsh
#
# Requires SUDO_PASSWORD in the environment for the non-interactive sudo path, and
# sets AGENTOS_APT_OPTS for callers that need the same APT options.

#: How long to wait for a concurrent APT or dpkg lock, in seconds.
AGENTOS_LOCK_TIMEOUT="${AGENTOS_LOCK_TIMEOUT:-300}"

#: Accept either spelling of the password variable. The scripts and the
#: documentation have used AGENTOS_SUDO_PASSWORD, while the helper reads
#: SUDO_PASSWORD; normalizing here means neither is wrong and nobody has to
#: remember which one a particular script expects.
if [ -z "${SUDO_PASSWORD:-}" ] && [ -n "${AGENTOS_SUDO_PASSWORD:-}" ]; then
  SUDO_PASSWORD="$AGENTOS_SUDO_PASSWORD"
fi

#: Options every apt-get invocation should carry.
#:
#: `--force-confold` is the default and is kept for ordinary installs: an
#: administrator's edit to a conffile survives. Verification needs more than that,
#: and `agentos_install_built_packages` explains why.
AGENTOS_APT_OPTS=(-o "DPkg::Lock::Timeout=$AGENTOS_LOCK_TIMEOUT")

#: Install the built packages so that the *package's* files are what ends up on disk.
#:
#: This is deliberate and specific to verification. dpkg records a conffile's md5sum
#: when it unpacks, and on a later install it will not replace a conffile whose
#: recorded checksum matches — even if the file is no longer on disk, which happens
#: after `apt-get remove` and a purge that removed configuration. The result is an
#: install that reports success while `/etc/agentos` stays empty, which is a property
#: of the machine's history rather than of the package.
#:
#: `--force-confmiss` restores a conffile dpkg considers missing, so the verification
#: tests the packages as built. It is not used for real installations: `apt install`
#: and `dpkg -i` are.
agentos_install_built_packages() {
  agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" \
    -o Dpkg::Options::=--force-confmiss \
    -o Dpkg::Options::=--force-confnew \
    install -y "$@"
}

#: Return success when the core package's conffiles are all present on disk.
agentos_configuration_present() {
  local path
  for path in /etc/agentos/agentos.toml /etc/agentos/credentials.env.example \
              /etc/agentos/policies/default-policy.yaml \
              /etc/agentos/policies/50-site-policy.yaml.example; do
    agentos_sudo test -f "$path" || return 1
  done
  return 0
}

#: Packages this project ships, in dependency order.
AGENTOS_PACKAGES=(agentos-core agentos-runtime-dsh agentos-desktop agentos-gnome-extension agentos-meta)

#: Run a command through sudo, using the environment's password when one is set.
#:
#: `sudo -n` is deliberate for the passwordless path: a bare `sudo` writes its prompt
#: to the terminal, and a script with no terminal attached can block there forever
#: waiting for input that will never come. A hang is far worse to debug than a
#: refusal, so every call either authenticates from the piped password or fails
#: immediately and visibly.
agentos_sudo() {
  if [ -n "${SUDO_PASSWORD:-}" ]; then
    # -p ' ' rather than -p '': an empty prompt argument is dropped by some shells,
    # which leaves sudo parsing the next token as an option and refusing the
    # command. A single space is a valid prompt and cannot be mistaken for one.
    printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p ' ' "$@"
  else
    sudo -n "$@"
  fi
}

#: Verify that sudo can be used non-interactively, and explain how to fix it if not.
#:
#: Returns non-zero with an actionable message rather than letting the first real
#: command hang. Callers should run this before doing any work.
agentos_require_sudo() {
  if agentos_sudo true 2>/dev/null; then
    return 0
  fi
  cat >&2 <<'EOF'
This verification script needs root and cannot prompt for a password interactively.

Choose one of these:

  1. Pass the password in the environment (available to this script only):

       AGENTOS_SUDO_PASSWORD=… sudo -v          # cache a credential first, or

  2. Allow passwordless sudo for your user, for the duration of the test:

       echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/agentos-test
       sudo chmod 0440 /etc/sudoers.d/agentos-test
       # remove it afterwards:
       sudo rm /etc/sudoers.d/agentos-test

  3. Run the script as root:

       sudo -i
       bash scripts/verify-installed.sh
EOF
  return 1
}

#: Return success when a package is fully installed.
#:
#: dpkg-query exits 0 whether or not the package is installed, so its output has to
#: be inspected; and the format string is single-quoted so the outer shell does not
#: expand dpkg's ${Status} placeholder.
#:
#: `grep -x` is essential rather than tidy. A removed package reports
#: `deinstall ok config-files`, which *contains* the substring `install ok
#: config-files` — and an unanchored `grep 'install ok installed'` therefore matched
#: a package that was not installed at all, which made a purge appear to succeed and
#: left the next install to unpack files while skipping the missing conffiles.
package_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -qx 'install ok installed'
}

#: Return success when every named package is fully installed.
all_packages_installed() {
  local package
  for package in "$@"; do
    package_installed "$package" || return 1
  done
  return 0
}

#: Return success when none of the named packages is installed.
no_packages_installed() {
  local package
  for package in "$@"; do
    if package_installed "$package"; then
      return 1
    fi
  done
  return 0
}

#: Print the installed subset of the arguments, one per line.
installed_subset() {
  local package
  for package in "$@"; do
    if package_installed "$package"; then
      printf '%s\n' "$package"
    fi
  done
}

#: Stop a unit if it exists, ignoring every failure mode.
agentos_stop_service() {
  agentos_sudo systemctl stop "${1:-agentos-core}" >/dev/null 2>&1 || true
}

#: Purge the named packages, passing only the ones that are installed, and verify it.
#:
#: Two traps are handled here, and both were hit during development:
#:
#: 1. `apt-get purge -y a b c` returns 100 and performs **no action at all** when any
#:    named package is not installed. Filtering to the installed subset fixes that.
#: 2. A package left in the `config-files` state is still "installed" as far as dpkg
#:    is concerned, and reinstalling over that state makes dpkg **unpack the data
#:    archive but skip a conffile that is missing from disk** — its recorded md5sum
#:    matches nothing, so it is not restored. The next stage then finds a package that
#:    installed cleanly and has no configuration. Purging until the packages are
#:    genuinely gone, verified by querying dpkg, removes that failure mode.
agentos_purge() {
  local attempt
  for attempt in 1 2 3; do
    local -a present=()
    mapfile -t present < <(installed_subset "$@")
    if [ "${#present[@]}" -eq 0 ]; then
      return 0
    fi
    agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" purge -y "${present[@]}" >/dev/null 2>&1 || true
    # A second pass with dpkg, which tolerates a package that vanished between the two
    # calls and reports the outcome per package rather than aborting the whole batch.
    mapfile -t present < <(installed_subset "$@")
    if [ "${#present[@]}" -gt 0 ]; then
      agentos_sudo dpkg --purge --force-depends "${present[@]}" >/dev/null 2>&1 || true
    fi
    if no_packages_installed "$@"; then
      return 0
    fi
  done
  # Report rather than fail silently: a package that cannot be purged will make a
  # later stage's install behave inexplicably.
  printf '  [warning] these packages could not be purged after 3 attempts: %s\n' \
    "$(installed_subset "$@" | tr '\n' ' ')"
  return 1
}

#: Remove the named packages, passing only the ones that are installed, and report
#: success through the exit status.
agentos_remove() {
  local -a present=()
  mapfile -t present < <(installed_subset "$@")
  if [ "${#present[@]}" -eq 0 ]; then
    return 0
  fi
  agentos_sudo apt-get "${AGENTOS_APT_OPTS[@]}" remove -y "${present[@]}"
}

#: Remove the service's state so a verification run starts from nothing.
#:
#: The service is stopped first: systemd recreates a *running* unit's
#: StateDirectory, so deleting it underneath a live service is racy and would make
#: a "data was preserved" check pass for the wrong reason.
#:
#: `dpkg --audit` is checked afterwards and reported: a package left half-configured
#: by an earlier stage makes the *next* stage's install unpack code but skip
#: conffiles, which is a confusing way to fail. Reporting it here attributes the
#: problem to the stage that caused it.
agentos_reset_state() {
  agentos_stop_service agentos-core
  agentos_sudo rm -rf /var/lib/agentos /var/cache/agentos /var/log/agentos /etc/agentos /run/agentos
  local audit
  audit="$(agentos_sudo dpkg --audit 2>&1)"
  if [ -n "$audit" ]; then
    printf '  [warning] dpkg reports an unfinished package configuration:\n'
    printf '%s\n' "$audit" | sed 's/^/            /'
  fi
}

#: Report the state directory and database, for a verification log.
agentos_state_line() {
  local label="${1:-state}"
  printf '  %-24s ' "$label"
  if agentos_sudo test -f /var/lib/agentos/agentos.sqlite3; then
    printf 'database present (%s bytes)\n' "$(agentos_sudo stat -c %s /var/lib/agentos/agentos.sqlite3 2>/dev/null)"
  elif agentos_sudo test -d /var/lib/agentos; then
    printf 'directory present, no database: %s\n' "$(agentos_sudo ls -1 /var/lib/agentos 2>/dev/null | tr '\n' ' ')"
  else
    printf 'directory missing\n'
  fi
}

#: Report the installed configuration, for a verification log.
#:
#: A conffile that is missing right after a successful install is a serious finding,
#: and diagnosing it needs to distinguish three cases: the directory is absent, it is
#: present but empty, or it is present with some files. This prints exactly that.
agentos_config_line() {
  local label="${1:-config}"
  printf '  %-24s ' "$label"
  if ! agentos_sudo test -d /etc/agentos; then
    printf '/etc/agentos is absent\n'
    return 0
  fi
  local files
  files="$(agentos_sudo find /etc/agentos -type f 2>/dev/null | sort | tr '\n' ' ')"
  if [ -z "$files" ]; then
    printf '/etc/agentos is present but holds no files\n'
  else
    printf '%s\n' "$files"
  fi
}

#: Put the machine into a state where an install behaves predictably.
#:
#: Purge first, then remove what a purge is documented to keep, then confirm dpkg no
#: longer holds a record of any AgentOS package. The order matters and was learned
#: from a failure: a directory left behind by a package that dpkg still considers
#: "config-files" makes a subsequent install unpack its data archive and skip the
#: conffiles, because their recorded checksums match nothing on disk.
agentos_clean_slate() {
  agentos_purge "${AGENTOS_PACKAGES[@]}"
  agentos_reset_state
  local attempt
  for attempt in 1 2 3; do
    if no_packages_installed "${AGENTOS_PACKAGES[@]}"; then
      return 0
    fi
    agentos_purge "${AGENTOS_PACKAGES[@]}"
    sleep 1
  done
  return 0
}

#: Print a trace line when AGENTOS_TRACE is set, so a log shows the operation order.
agentos_trace() {
  if [ -n "${AGENTOS_TRACE:-}" ]; then
    printf '  [trace] %s\n' "$*"
  fi
}

#: Return success when a user is listed as a member of a group.
#:
#: This asks the group database rather than `id`, and the distinction matters. `id`
#: reports the *calling process's* credential set, which still contains a group that
#: was removed a moment ago. `sg` cannot help either: it adds a group on top of the
#: groups the invoking shell already has rather than rebuilding them from the
#: database. A check that used either would report a denial that was never
#: established, which is worse than no check at all.
agentos_user_in_group() {
  local user="$1" group="$2"
  if getent group "$group" >/dev/null 2>&1; then
    if getent group "$group" | awk -F: '{print $4}' | tr ',' '\n' | grep -qx "$user"; then
      return 0
    fi
  fi
  # A user whose primary group is the one in question is a member too.
  if [ "$(id -gn "$user" 2>/dev/null)" = "$group" ]; then
    return 0
  fi
  return 1
}

#: Remove a user from a group, whichever tool the platform provides, and verify it.
agentos_remove_user_from_group() {
  local user="$1" group="$2"
  agentos_sudo gpasswd -d "$user" "$group" >/dev/null 2>&1 \
    || agentos_sudo deluser "$user" "$group" >/dev/null 2>&1 \
    || true
  ! agentos_user_in_group "$user" "$group"
}

#: Report whether a user is in the agentos group, for a verification log.
agentos_membership_line() {
  local user="${1:-$(id -un)}"
  printf '  %-24s %s\n' "membership of $user" \
    "$(agentos_user_in_group "$user" agentos && echo 'in agentos' || echo 'not in agentos')"
  printf '  %-24s %s\n' "group database" "$(getent group agentos 2>/dev/null || echo 'the agentos group does not exist')"
  printf '  %-24s %s\n' "this shell's groups" "$(id -nG | tr ' ' ',')"
}

#: Run a command with the agentos group absent from the process's group list.
#:
#: The honest way to test the access boundary is to use an account that has never been
#: in the agentos group, and that is what this does: it creates a throwaway probe
#: account, switches to it, and removes it afterwards. Every cheaper approach was
#: measured against a running service and rejected:
#:
#:   * `id -nG` reports the caller's group list, not the group database;
#:   * `sg` *adds* a group to the invoking shell's supplementary groups, so a process
#:     that already holds `agentos` keeps it and the service correctly answers — the
#:     check then reports a failure that is really a property of the harness;
#:   * `setpriv --clear-groups` works but blocked a verification run rather than
#:     failing it;
#:   * removing the caller from the group and restoring it afterwards leaves the
#:     account's membership changed if the run is interrupted.
#:
#: `runuser -u` and `sudo -u` both rebuild the credential set from the database, so the
#: probe account genuinely has no agentos group.
#:
#: Usage: agentos_run_without_agentos_group <command...>
agentos_run_without_agentos_group() {
  local probe="${AGENTOS_PROBE_USER:-agentos-probe}"
  local created=0
  if ! getent passwd "$probe" >/dev/null 2>&1; then
    if agentos_sudo useradd --create-home --shell /bin/bash "$probe" >/dev/null 2>&1; then
      created=1
    else
      printf 'could not create the probe account %s\n' "$probe" >&2
      return 127
    fi
  fi
  # A user created with useradd holds only its own primary group, so nothing has to be
  # removed to establish that it is outside the agentos group.
  local -a runner=()
  if command -v timeout >/dev/null 2>&1; then
    runner=(timeout 60)
  fi
  local status=0
  if command -v runuser >/dev/null 2>&1; then
    agentos_sudo runuser -u "$probe" -- "${runner[@]}" "$@" || status=$?
  else
    agentos_sudo -u "$probe" "${runner[@]}" "$@" || status=$?
  fi
  if [ "$created" -eq 1 ]; then
    agentos_sudo userdel --remove "$probe" >/dev/null 2>&1 || true
  fi
  return "$status"
}

#: Report whether the probe account was left behind, for a verification log.
agentos_probe_account_line() {
  local probe="${AGENTOS_PROBE_USER:-agentos-probe}"
  printf '  %-24s %s\n' "probe account" \
    "$(getent passwd "$probe" >/dev/null 2>&1 && echo "$probe still exists" || echo "$probe is absent, as it should be")"
}
