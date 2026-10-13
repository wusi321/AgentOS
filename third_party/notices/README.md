# Third-party notices

AgentOS itself is licensed under Apache-2.0. This directory records the licence
position of the components AgentOS talks to or verifies against, so that an
operator or distributor can see what is and is not redistributed.

## Not redistributed by these packages

### DeepSeek Harness (`@deepseek-ai/dsh`)

| Fact | Value |
|---|---|
| Version | `0.2.1-alpha.2` |
| Git tag | `dsh-v0.2.1-alpha.2` |
| Commit | `d743267388641bc76f17c45ce8b4c231aed1d32c` |
| Repository | <https://github.com/deepseek-ai/deepseek-harness> |
| Licence | MIT |
| Node.js engines | `^22.19.0 \|\| >=24.0.0` |

The `agentos-runtime-dsh` package contains an **adapter** that spawns a harness
installation the deployment provides. No harness source, bundle, or licence text
is redistributed by the AgentOS packages. A deployment that installs the harness
must comply with the MIT licence itself and must preserve the upstream copyright
notice; `packaging/dsh/README.dsh.md` documents the pinned installation.

The harness is pre-stable upstream. AgentOS pins the revision, checks the Node.js
engine requirement at runtime, refuses to start on an unsupported interpreter,
and never installs `@latest`.

### Node.js

Not redistributed. The `agentos-runtime-dsh` package declares Node.js as a
*Recommends* rather than a *Depends*, because no Node.js available in Ubuntu
22.04 or 24.04 satisfies the locked harness requirement. A deployment chooses and
installs its own qualifying Node.js and inherits that distribution's licence and
security-update policy.

## Redistributed runtime dependencies

AgentOS ships no third-party Python code. It depends only on:

| Dependency | Role | Licence |
|---|---|---|
| `python3-psutil` | not used; the System Bridge reads `/proc` directly | — |
| GTK4, libadwaita, PyGObject, python3-dbus | desktop application, session broker | LGPL-2.1+ |

The `python3-psutil` entry above is listed only because the packaging declares it:
the first release reads `/proc` and `statvfs` directly, and the declaration is kept
so that a future collector has an obvious place to land. Nothing in
`src/agentos` imports it.

## Verification inputs

`upstream/deepseek-harness/` in a development checkout is a clone of the upstream
repository used to verify the wire protocol and the event vocabulary. It is not a
packaging input and is not shipped in any `.deb`.

`third_party/` contains no vendored source. If a future release vendors code, this
directory gains the upstream licence text and the exact version, and
`debian/copyright` gains a matching stanza.
