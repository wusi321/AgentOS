# Installing AgentOS

There are three ways to install, and the one that looks most natural is the one
that fails. This page explains why, and gives you the route that works.

## The failure you will probably hit first

```sh
$ sudo apt install ./agentos-meta_0.1.0_all.deb
正在解析依赖...有错误!
无法满足的依赖关系:
 依赖: agentos-core (>= 0.1.0) 但无法安装它
 依赖: agentos-runtime-dsh (>= 0.1.0) 但无法安装它
 依赖: agentos-desktop (>= 0.1.0) 但无法安装它
 依赖: agentos-gnome-extension (>= 0.1.0) 但无法安装它
```

**This is expected behaviour, not a broken package.** APT resolves dependencies only
from *configured sources*. It does not look at the other `.deb` files sitting beside
the one you named. `agentos-meta` is a metapackage: its entire purpose is to depend on
the other four, so installing it alone can never work.

Pick one of the three routes below.

---

## Route 1: one command, all five files (simplest)

Name every package on one command line. APT then sees them together, and can still
resolve the *distribution* dependencies (python3-psutil, python3-dbus, GTK4,
libadwaita) from your normal Ubuntu sources.

```sh
cd ~/下载
sudo apt install ./agentos-core_0.1.0_all.deb \
                 ./agentos-runtime-dsh_0.1.0_all.deb \
                 ./agentos-desktop_0.1.0_all.deb \
                 ./agentos-gnome-extension_0.1.0_all.deb \
                 ./agentos-meta_0.1.0_all.deb
```

If you would rather not type that, the repository ships a script that does it and
then tells you the next steps:

```sh
sudo bash scripts/install-agentos.sh ~/下载
```

**If APT reports unmet dependencies here**, it is naming a *distribution* package,
and the message tells you which one. See "If the extension will not install" below.

## Route 2: a local repository (recommended, and needed for upgrades)

This makes `agentos` visible to APT as a source, so `apt install` works normally and
those dependencies resolve together with the distribution's.

```sh
sudo bash scripts/make-local-repo.sh ~/下载
sudo apt update
sudo apt install agentos-meta
```

That is the whole install. The script writes `/opt/agentos-repo` and
`/etc/apt/sources.list.d/agentos-local.list`, prints what APT can now see, and is
safe to re-run after a new build to refresh the repository in place.

Two things to know about it:

* it uses `[trusted=yes]`, because these packages are not signed. That is acceptable
  for a repository **you** built from a checksummed artifact set. It is not
  acceptable for a directory anyone else can write to.
* because APT now knows the packages, an upgrade is `sudo apt install --reinstall
  agentos-core` or `sudo apt upgrade`, rather than re-running `dpkg -i`.

To undo it:

```sh
sudo bash scripts/make-local-repo.sh --remove
```

## Route 3: install only what you need

The metapackage is a convenience, not a requirement. For a headless machine or a
smaller footprint:

```sh
# The service only. Read-only system inspection, tasks, policy, audit. No GUI.
sudo apt install ./agentos-core_0.1.0_all.deb ./agentos-runtime-dsh_0.1.0_all.deb
agentosctl doctor

# Add the window and the session broker. Needs GTK4 and libadwaita, which Ubuntu
# Desktop already has.
sudo apt install ./agentos-core_*.deb ./agentos-desktop_*.deb

# Add the GNOME Shell panel entry. Needs gnome-shell, so this is Desktop only.
sudo apt install ./agentos-gnome-extension_*.deb
```

---

## If the extension will not install

`agentos-gnome-extension` depends on `gnome-shell (>= 42)`. On Ubuntu Desktop that is
already satisfied. On a server or minimal install it is not, and APT will either
refuse or try to pull in an entire GNOME desktop — which is why this package is
deliberately **not** a dependency of `agentos-core`.

If you do not have a GNOME desktop, skip the extension and the metapackage:

```sh
sudo apt install ./agentos-core_0.1.0_all.deb ./agentos-runtime-dsh_0.1.0_all.deb \
                 ./agentos-desktop_0.1.0_all.deb
```

`agentosctl`, the REST-free CLI, the tasks, the policy engine, and the audit trail
all work without GNOME. Only the top-bar panel entry needs it.

## If a dependency is genuinely missing

`apt install` asks for what it needs from your configured sources. On an offline
machine, copy the required `.deb` files in as well, or point APT at a local mirror.
For reference, the complete dependency set is:

| Package | Depends on |
|---|---|
| `agentos-core` | `python3` (>= 3.10), `python3-psutil`, `adduser` |
| `agentos-runtime-dsh` | `python3`, `agentos-core` — Node.js is only a **Recommends**, see below |
| `agentos-desktop` | `python3-gi`, `python3-gi-cairo`, `python3-dbus`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `dbus-user-session`, `agentos-core` |
| `agentos-gnome-extension` | `gnome-shell` (>= 42), `agentos-desktop` |
| `agentos-meta` | all four above |

**Node.js is a Recommends, not a Depends, on purpose.** No Node.js in Ubuntu 22.04 or
24.04 satisfies the harness requirement `^22.19.0 || >=24.0.0`, so a hard dependency
would make the package uninstallable on the target release. The adapter refuses to
start on an unsupported interpreter and names the requirement instead. You can
install everything and use the mock runtime forever with no Node.js at all.

---

## After installing: three steps that are not optional

### 1. Check the installation

```sh
agentosctl doctor
```

This needs no model credential and no network. It reports the configuration, the
service state, the runtime, the Node.js requirement if the DSH runtime is selected,
the credential file's permissions, and the loaded policy.

### 2. Give your user access to the service socket

```sh
sudo adduser "$USER" agentos
```

Then **log out and back in.** The service accepts IPC only from members of the
`agentos` group, and a new group membership applies only to a new session — the
kernel checks the credentials a process already has. Until you re-login you will
see:

```
error [agentos.ipc_access_denied]: permission denied connecting to
/run/agentos/core.sock; add this user to the agentos group and start a new login session
```

That message is the design working: the socket is mode 0660, group `agentos`, and
per-request authorization then uses your kernel-recorded uid.

### 3. Start the service and confirm

```sh
sudo systemctl start agentos-core
agentosctl health
agentosctl demo
```

`agentosctl demo` runs the whole chain with no credential and no network: a task, a
runtime turn, a policy decision that allows a read, a policy decision that denies an
administrative tool, an approval request, and the audit trail.

### 4. Optional: show the panel

Run this **as your own user, not with sudo** — extension state is per-user dconf:

```sh
gnome-extensions enable agentos@agentos.dev
```

Then log out and back in. A package cannot do this for you, which is why the install
printed the command instead of failing.

---

## Verifying the install

```sh
# Identity, and the kernel-recorded caller uid
agentosctl hello

# Read-only system inspection
agentosctl system snapshot
agentosctl system processes --limit 10

# The policy posture: read-only by default
agentosctl policy inventory

# A real task through the keyless mock runtime
agentosctl task run "Report memory and disk pressure." --watch
agentosctl audit summary
```

The full acceptance test, including the GNOME Shell and approval-path checks, is in
[docs/vm-verification.md](vm-verification.md).

## Upgrading

**With the local repository configured** (route 2), APT handles it:

```sh
sudo apt update && sudo apt upgrade
```

**Without it** (routes 1 and 3), name the new files. Because configuration files are
conffiles, your `/etc/agentos` edits are preserved and dpkg asks before replacing
them. An edited systemd unit from an earlier build is a modified conffile and is
therefore *kept*, so if a release changes a unit, take the packaged version:

```sh
sudo dpkg -i --force-confnew /path/to/agentos-core_*.deb
sudo systemctl daemon-reload && sudo systemctl restart agentos-core
```

Check which units changed with:

```sh
dpkg-query -W -f='${Conffiles}\n' agentos-core
```

### If an upgrade leaves the configuration empty

A conffile that dpkg believes it already delivered is **not** written again, even when
the file is no longer on disk. That state is easy to reach:

```sh
sudo apt remove agentos-meta agentos-gnome-extension agentos-desktop \
                agentos-runtime-dsh agentos-core    # removes the packages
sudo rm -rf /etc/agentos                             # or a purge for part of the set
# ... then reinstalling produces /etc/agentos with the files missing
```

The reinstall reports success and the service runs, because the package's binaries are
unpacked normally. Only the conffiles are skipped. dpkg is behaving as documented —
the recorded checksum of each conffile matched, so it did not overwrite a file it
assumed was there.

Two ways out, and the first is the one you want:

```sh
# Restore any conffile dpkg considers missing, then reload the service.
sudo apt install --reinstall -o Dpkg::Options::=--force-confmiss ./agentos-core_*.deb
sudo systemctl daemon-reload && sudo systemctl restart agentos-core

# Or start from a genuinely clean state: purge everything first, then install.
sudo apt purge agentos-core agentos-runtime-dsh agentos-desktop agentos-gnome-extension agentos-meta
sudo apt install ./agentos-*.deb
```

Purging *every* package is the part that matters. `apt purge a b c` exits with status
100 and performs **no action at all** when any one of them is not installed, so a
partial purge can leave one package in the `config-files` state and reproduce exactly
this symptom on the next install.

## Removing

```sh
# Stops the service and removes the program files.
sudo apt remove agentos-meta agentos-gnome-extension agentos-desktop \
                agentos-runtime-dsh agentos-core
```

Both `remove` and `purge` **keep** `/var/lib/agentos`, which holds your tasks,
approvals, and the audit trail, and both say so. Removing that record is a deliberate
act:

```sh
sudo rm -rf /var/lib/agentos
```

`purge` additionally removes `/etc/agentos` and the `agentos` account.

## Common questions

**Do I need a model credential to install?**
No. The shipped runtime is `mock`, which is scripted and needs nothing. Everything
except a real model answer works without a credential.

**Do I need Node.js?**
Only to use DeepSeek Harness. Install without it and the service runs the mock
runtime; `agentosctl doctor` reports the Node.js requirement whenever you switch
`runtime_id` to `dsh`.

**Where do I put a model credential?**
`/etc/agentos/credentials.env`, mode `0600`, root-owned. It is never installed by a
package — only a template is. The service **refuses** to read the file when it is
readable by group or other, and says why. See
[docs/permissions.md](permissions.md) and [ADR-011](adr/ADR-011-credentials.md).

**Why does `apt install ./agentos-meta_*.deb` not just pull the others in?**
Because APT does not treat a file:// path as a source it can search. Name all five on
one command line, or configure the local repository. Both are routes above.

**Why does `apt purge` sometimes do nothing?**
`apt purge a b c` exits with status 100 and performs no action when any one of the
named packages is not installed. Purge only the packages that are installed:

```sh
# Show what is installed first
dpkg-query -W -f='${Package} ${Status}\n' 'agentos-*'
# Then purge exactly those names
sudo apt purge agentos-core agentos-desktop
```

`sudo apt purge 'agentos-*'` does not help, because the shell expands the glob against
the filesystem rather than the package database.

**The service is running but the panel says unavailable.**
Check `systemctl --user status agentos-session`. The panel talks to the session
broker, not to the core service directly, and the broker is a per-login user unit.
