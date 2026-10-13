# ADR-009: Five binary packages from one native source tree

**Status:** accepted
**Date:** 2026-10-13

## Context

The design lists more than a dozen possible packages and warns explicitly against
making the dependency chain complicated for the sake of modularity. The first
release has to decide how to split, how to build, and what an uninstall preserves.

## Options considered

For the split:

1. **One package** containing the service, the adapter, the desktop, and the
   extension.
2. **The full design list**: `agentos-runtime-api`, `agentos-policy`,
   `agentos-audit`, `agentos-task-manager`, `agentos-session`, and so on.
3. **Five packages split by dependency and installation target.**

For the build:

4. **`pybuild`** with a `pyproject.toml`.
5. **`dh_install` with explicit per-package install lists.**
6. **A `debian/rules` that copies files with shell commands.**

## Decision

Option 3, built with option 5, from a **native** source tree (`3.0 (native)`).

| Package | Why it is separate |
|---|---|
| `agentos-core` | it is the service, and it must be installable alone |
| `agentos-runtime-dsh` | it is the only package with a Node.js interest, so a deployment that does not want a harness does not get its dependency |
| `agentos-desktop` | it is the only package with a GTK dependency, and it is per-user rather than system-wide |
| `agentos-gnome-extension` | it depends on `gnome-shell`, which a server install does not have |
| `agentos-meta` | it is the supported "install everything" path |

## Reasoning

**Why not one package.** A headless or server installation would pull in GTK4,
libadwaita, and the GNOME Shell, and would fail outright where `gnome-shell` is
unavailable. The split is therefore driven by a real installation difference, not
by a desire for tidiness.

**Why not the full design list.** `agentos-policy`, `agentos-audit`, and
`agentos-task-manager` are Python modules that import each other constantly. A
package boundary between them would not create an enforceable interface — Python
would happily cross it — so it would add five `Depends` lines and no isolation.
They ship inside `agentos-core`, where their real boundary (the IPC surface) already
exists. The design outline's own guidance is to merge packages in the MVP and split
when a stable need appears.

**Why native packaging.** The project is packaged by its own maintainers and has no
separate upstream tarball. `3.0 (native)` says exactly that, and it means
`dpkg-buildpackage -b` builds from this tree with no network access and no source
artifact to keep in sync.

**Why `dh_install` with explicit lists rather than `pybuild`.** The project has
several top-level source trees — Python modules, CLI scripts, the GNOME extension,
packaging assets — and they install to different places. `pybuild` models one tree.
Explicit install lists make the shipped layout readable from the packaging alone,
and `scripts/check-packaging.py` verifies both halves of every line.

Two `dh_install` behaviours were discovered the hard way and are now encoded in the
checks:

* **A directory source is copied to `<destination>/<basename>/`.** Installing
  `src/agentos/policy` into `.../agentos/policy/` produced
  `.../agentos/policy/policy/`. Sub-packages are therefore listed as explicit globs
  (`src/agentos/policy/*.py`).
* **A destination that is not a directory path creates a directory named after the
  file.** `packaging/man/agentosctl.1 usr/share/man/man1/agentosctl.1` produced
  `usr/share/man/man1/agentosctl.1/agentosctl.1.gz`. Every destination is now a
  directory ending in `/`.

## Configuration and data policy

* Configuration files are **conffiles**, so an administrator's edit survives an
  upgrade and dpkg asks before replacing it.
* The package installs `agentos.toml`, the read-only default policy, an example
  site policy, and a credential **template**. It never installs a credential.
* `/var/lib/agentos` is created by the maintainer script **and** declared as
  `StateDirectory` in the unit, so the service also works when started outside
  systemd.
* `remove` stops the service and keeps the task and audit database. `purge` removes
  configuration and the service account and **still** keeps the database, and says
  so. Deleting it is a deliberate `rm -rf`.

## Build environment

The build runs from a copy on the native Linux filesystem
(`scripts/build-debs-native.sh`), because the development checkout lives on a
Windows drive mounted through WSL, which reports every file as mode 0777 and
ignores `chmod`. Debian packaging depends on the executable bit: debhelper treats an
executable `debian/*.install` as a helper script and refuses to parse it as a list,
and a non-executable maintainer script is a policy violation. The script normalises
modes before building and the resulting packages are verified independently.

## Security impact

**Positive.** The split follows the dependency boundaries that matter for
privilege: the core is the only package that installs a system service and a
system user; the desktop package runs as the user and declares no service; the
extension package cannot be installed without a Shell. No package installs a
setuid or setgid file, and the only system user created is unprivileged with
`/usr/sbin/nologin`.

**Positive, packaging-specific.** `scripts/check-packaging.py` includes a module
coverage check: a Python module that exists in `src/agentos` but appears in no
install list fails the build. Without it, a refactor can add a module that works in
a checkout and is missing on a target machine — a defect that only appears after
release.

## Maintenance cost

Adding a package means a `control` stanza, an install list, and an entry in
`EXPECTED_PACKAGES` in the checker. Adding a module means adding a line to an
install list, which the coverage check enforces. Both are cheap, and the checker
turns forgetting into a build failure rather than a support ticket.

## Review trigger

Revisit if the Python modules inside `agentos-core` acquire a genuine need for
independent release cadence, or if a deployment appears that needs the policy engine
without the system bridge — at which point a package boundary would create an
enforced interface rather than a nominal one. Also revisit when the ISO work begins,
because the ISO's package list becomes a second consumer of this split.
