# Packaging

AgentOS ships as five Debian packages built from one native source tree. This page
records what each package contains, where it installs, what happens on upgrade and
removal, and how the build is verified.

## Packages

| Package | Architecture | Contents |
|---|---|---|
| `agentos-core` | all | the core service, `agentosctl`, the configuration and policy, the systemd unit, the D-Bus policy, the man page |
| `agentos-runtime-dsh` | all | the DeepSeek Harness adapter, its wire transport, and the locked-revision record |
| `agentos-desktop` | all | the GTK4 application, the session broker, the desktop entry and icon, the GSettings schema, the user unit |
| `agentos-gnome-extension` | all | the Shell extension and its settings schema |
| `agentos-meta` | all | a metapackage that installs everything |

Every package is `Architecture: all`: the shipped code is Python and GJS, and the
Node.js runtime a real DSH deployment needs comes from a source that deployment
chooses. An architecture-specific artifact would force a rebuild per architecture
for no benefit.

### Dependency decisions

* `agentos-runtime-dsh` declares Node.js as a **Recommends**, not a Depends. No
  Node.js in Ubuntu 22.04 (12) or 24.04 (18) satisfies the locked harness
  requirement `^22.19.0 || >=24.0.0`, so a hard dependency would make the package
  uninstallable on the target release. The adapter refuses to start on an
  unsupported interpreter and names the requirement instead.
* `agentos-core` depends on `python3-psutil` for the same reason it declares it:
  the first release reads `/proc` and `statvfs` directly, and the declaration is
  kept so that a future collector has an obvious place to land.
* `agentos-desktop` depends on `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, and
  `python3-dbus`. It does **not** depend on the DSH adapter: the desktop talks to
  Core, and Core can run the mock runtime forever.

## Installation layout

| Path | Owner | Package |
|---|---|---|
| `/usr/bin/agentosctl`, `/usr/bin/agentos-desktop`, `/usr/bin/agentos-session` | root | core, desktop |
| `/usr/libexec/agentos/agentos-core` | root | core |
| `/usr/lib/python3/dist-packages/agentos/` | root | core, runtime-dsh, desktop |
| `/etc/agentos/agentos.toml` | root (conffile) | core |
| `/etc/agentos/policies/default-policy.yaml` | root (conffile) | core |
| `/etc/agentos/policies/50-site-policy.yaml.example` | root (conffile) | core |
| `/etc/agentos/credentials.env.example` | root (conffile) | core |
| `/lib/systemd/system/agentos-core.service` | root | core |
| `/lib/systemd/user/agentos-session.service` | root | desktop |
| `/usr/share/dbus-1/system.d/org.agentos.Core1.conf` | root | core |
| `/usr/share/dbus-1/services/org.agentos.Session1.service` | root | desktop |
| `/usr/share/glib-2.0/schemas/org.agentos.Desktop.gschema.xml` | root | desktop |
| `/usr/share/glib-2.0/schemas/org.gnome.shell.extensions.agentos.gschema.xml` | root | gnome-extension |
| `/usr/share/gnome-shell/extensions/agentos@agentos.dev/` | root | gnome-extension |
| `/usr/share/man/man1/agentosctl.1.gz` | root | core |
| `/var/lib/agentos/`, `/var/cache/agentos/`, `/var/log/agentos/` | `agentos` | created by the postinst and by `StateDirectory` |
| `/run/agentos/core.sock` | `agentos:agentos`, mode 0660 | created by the service |

Nothing is installed into a user's home directory, and nothing is installed
setuid or setgid.

## The install step

`agentos-core.postinst` performs, in order:

1. create the `agentos` system group and user (no login shell, no home);
2. create the state, cache, log, workspace, and harness-home directories with the
   ownership and mode the service needs;
3. create `/etc/agentos/`, `/etc/agentos/policies/`, and a mode-0700
   `/etc/agentos/private/` for operator-managed material;
4. `systemctl daemon-reload`, then **enable** the unit — enabling is the package's
   decision, starting is the administrator's. An administrator who stopped the
   service on purpose does not have it started by an upgrade.

Every action checks its own precondition, so the script is idempotent and safe on
install, on upgrade, and when run twice. It contacts no network, compiles nothing,
and assumes no GNOME session exists.

`agentos-desktop.postinst` enables the per-login session broker for all users with
`systemctl --global enable`, and prints the command a user must run to enable the
Shell extension. It does not try to enable the extension itself: extension state is
per-user dconf, and a postinst runs outside any session.

## Maintainer script arguments

dpkg calls maintainer scripts with a documented, finite argument set, and it does so
at moments the package author does not control — during an upgrade of the **old**
package, after a failed upgrade, and while cleaning up an aborted one. A script
that refuses an argument it will really receive makes every future upgrade fail.

| Script | Arguments it must accept |
|---|---|
| `postinst` | `configure`, `abort-upgrade`, `abort-remove`, `abort-deconfigure` |
| `prerm` | `remove`, `deconfigure`, `upgrade`, `failed-upgrade`, `abort-install` |
| `postrm` | `remove`, `purge`, `upgrade`, `disappear`, `failed-upgrade`, `abort-install`, `abort-upgrade` |

Two rules follow, and both are enforced by `scripts/check-maintainer-arguments.py`
and `scripts/check-packaged-scripts.sh`:

1. **Every one of those arguments is accepted.** During development the `postrm`
   omitted `upgrade`, `failed-upgrade`, and `abort-upgrade`, and every reinstall of
   the package failed as a result. The check exists because dpkg reports the
   failure as a bare exit status with no indication of the cause.
2. **The `#DEBHELPER#` token appears on a line of its own.** debhelper replaces it
   with a generated code block. Writing it inside a prose comment makes that
   substitution shred the comment and leave the rest of the script unparseable —
   which also happened during development, in `agentos-desktop.postinst`. The
   check parses the copies that are actually inside the built `.deb`, because only
   the packaged copy reveals it.

## Upgrade behaviour

* Configuration files are conffiles: dpkg preserves administrator edits and asks
  before replacing them.
* The state directory, the database, and the extension's GSettings keys are not
  touched by an upgrade.
* The database schema carries a monotonic version and migrates on startup.
  A database from a **newer** build is refused rather than misread.
* A reload of configuration or policy that fails validation keeps the previous
  configuration and writes the reason to the journal.
* Because an edited unit file is a modified conffile, an upgrade that changes a
  unit is offered as a diff. To take the package's version:

  ```sh
  sudo dpkg -i --force-confnew /path/to/agentos-core_*.deb
  sudo systemctl daemon-reload && sudo systemctl restart agentos-core
  ```

## Removal and purge

| Action | Effect |
|---|---|
| `apt remove` | stops the service, disables the unit, removes the program files. **Keeps** `/var/lib/agentos` with tasks, approvals, history, and the audit trail, and says so. |
| `apt purge` | additionally removes `/etc/agentos/` and the `agentos` account. **Still keeps** `/var/lib/agentos`, and says so. |

An uninstall never destroys the record of what an agent did. Removing that
directory is a deliberate act:

```sh
sudo rm -rf /var/lib/agentos
```

## Building

```sh
python3 scripts/check-packaging.py        # metadata consistency, module coverage
bash scripts/build-debs-native.sh         # writes build/debs/*.deb
```

`build-debs-native.sh` builds from a copy on the native Linux filesystem. A Windows
drive mounted through WSL reports every file as mode 0777 and ignores `chmod`, and
Debian packaging depends on the executable bit: an executable `debian/*.install`
is treated as a helper script, and a non-executable maintainer script is a policy
violation. The script normalises the modes and then builds.

`scripts/build-debs.sh` is the same build without the copy, for a checkout that
already lives on a Linux filesystem.

The build fetches nothing, compiles nothing, and needs no root:
`Rules-Requires-Root: no`.

## Verification

Three levels, each proving something the previous one cannot.

```sh
# 1. metadata: every declared file exists, no module is left unshipped
python3 scripts/check-packaging.py

# 2. built packages: layout, byte-compilation, imports, schemas, units, and the
#    security properties of the extension
bash scripts/verify-debs.sh build/debs

# 3. installed: install, upgrade, remove, purge, and the live service
AGENTOS_SUDO_PASSWORD=… bash scripts/verify-installed.sh build/debs
AGENTOS_SUDO_PASSWORD=… bash scripts/verify-live.sh build/debs
```

`verify-debs.sh` additionally runs `lintian` and reports every finding, and
`verify-installed.sh` additionally checks that removal keeps the database and that
purge still keeps it.

`verify-live.sh` performs the documented authorization step (adding the human user
to the `agentos` group), restarts the service so the kernel records the new
credentials, and then drives the whole system through the real CLI: health, the
peer identity, the read-only system views, the policy posture, a real task, the
approval path, the audit trail, and the DSH capability report. It also verifies
that a user **outside** the group is refused with an actionable message, which is
the behaviour the design requires and the reason the group exists.

## Lintian

The build runs `lintian` and every finding is reviewed. The remaining warnings are
explained:

| Tag | Why it is present | Action |
|---|---|---|
| `maintainer-script-calls-systemctl` | the postinst enables the unit, which is what the package promises | none: systemctl is the supported interface |
| `no-manual-page [usr/bin/agentos-desktop, usr/bin/agentos-session]` | those two commands have no page yet | deferred; `agentosctl` is documented, and the desktop application has a desktop entry and a settings view |
| `initial-upload-closes-no-bugs` | this is a first upload | expected |

The `copyright-not-using-common-license-for-apache2` error and the
`stray-folder-in-manual`, `subdir-in-usr-bin`, `odd-place-for-manual-page`,
`duplicate-changelog-files`, and `executable-not-elf-or-script` errors and warnings
that appeared during development are all **fixed**: they were caused by a
`#DEBHELPER#` token inside a comment, `dh_install` duplicating a directory
source's basename, `dh_installchangelogs` being given `debian/changelog`
explicitly, and the Windows mount's file modes.

## Reproducibility

* The source tree is the package: `3.0 (native)`, no upstream tarball.
* Every shipped file is listed explicitly in a `debian/*.install` file, so the
  shipped layout is readable from the packaging alone.
* `check-packaging.py` includes a **module coverage** check: a module that exists
  in `src/agentos` but appears in no install list fails the build. That is how the
  class of "it works in my checkout and fails on the target" is closed.
* The DSH revision is pinned by version and commit in `constants.py`, in
  `packaging/dsh/dsh-locked.json`, and in `docs/dsh-integration.md`, and the
  packaging metadata asserts it.

## ISO integration (P6, not in this release)

The next phase integrates the built packages into a customised Ubuntu Desktop
ISO. The intended inputs and order, per the design outline:

1. a specific Ubuntu 22.04 Desktop ISO with its SHA-256;
2. the built `.deb` files with their versions and checksums;
3. the default dconf settings that enable the extension;
4. the first-boot wizard, which must never require a credential on a command line;
5. the artifact manifest: source commit, package list, build parameters, and
   checksums.

An ISO must not contain a developer's credential, a test database, or a machine
identity, and a first boot must generate its own instance data. Until that flow
exists, `agentos-meta` is the supported installation path.
