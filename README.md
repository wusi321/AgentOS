# AgentOS — An Agent-Native Operating Environment for Linux

An agent-native operating environment for Linux: persistent agents, system
observability, task orchestration, desktop integration, and policy-controlled
actions.

Target platform: Ubuntu 22.04 LTS Desktop and later, x86_64, system Python 3.10+.
Shipped as Debian packages; the first Agent Runtime is DeepSeek Harness (DSH).

## What it is

AgentOS is not a new kernel and not a new desktop. It is a userspace platform on
top of Ubuntu that gives an AI agent an identity, a background lifetime, system
visibility, a task queue, and a **bounded** ability to act:

* the **Core service** owns tasks, policy decisions, and the audit trail, and
  runs as an unprivileged system user;
* the **System Bridge** exposes read-only system facts and a closed set of two
  controlled write operations;
* the **Policy Engine** is the only authority for what may run. A model's output
  is never an authorization;
* every side-effecting operation needs an **approval bound to its exact
  operation, target, and parameter set**;
* the **GNOME Shell extension** and the **GTK4 desktop application** present
  state and collect decisions, and hold no secret and no authority;
* the **runtime adapter** drives DSH over its SDK JSON-RPC stdio profile and
  reports honestly what that profile cannot do.

## Design posture

| Principle | Concrete consequence |
|---|---|
| Least privilege | the service runs as `agentos`, never root; no privileged executor exists in this release |
| Default deny | the shipped policy permits P0 read-only inspection only |
| Human in the loop | a P2 operation needs an approval bound to one target and one parameter digest, consumed once |
| No shell | AgentOS never runs a caller-supplied command string; helpers run with a fixed argv |
| Fail closed | an undeclared tool is denied; an unreadable credential file is refused; a configuration error keeps the previous configuration |
| Fail safe | read-only tools, the desktop, and GNOME login keep working when the model is unreachable |
| Auditable | append-only records of every decision, approval, and execution |

## Repository layout

```text
src/agentos/            the Python package
  constants.py          identity, locked versions, filesystem paths
  errors.py             typed failures, one machine code per condition
  events.py             the versioned event envelope and severity vocabulary
  jsonutil.py           redaction, canonical JSON, payload bounding
  schema.py             parameter schemas and the checks that reject shell shapes
  runtime/              AgentRuntime protocol, mock runtime, DSH adapter
  system_bridge/        /proc collectors, systemd and journald access, executor
  policy/               capability manifests, rules, approvals, the engine
  audit/                append-only audit store
  tasks/                state machine, SQLite storage, the task manager
  core/                 configuration, IPC, and the service host
  session/              the per-login D-Bus broker
  desktop/              the GTK4/libadwaita application
  cli.py                agentosctl
bin/                    the installed entry points
gnome-extension/        the GNOME Shell 42+ extension
packaging/              units, D-Bus policy, schemas, man page, desktop assets
debian/                 Debian packaging
configs/policies/       the shipped policy and a documented example
docs/                   architecture, threat model, permissions, ADRs
tests/                  unit, contract, security, and packaging checks
scripts/                build, test, probe, and packaging verification
upstream/               the cloned DSH repository (a verification input, not packaged)
```

## Build

```sh
sudo apt install build-essential devscripts debhelper dh-python fakeroot \
                 lintian python3-all python3-pytest python3-pytest-asyncio

# Verify the packaging metadata, then build.
python3 scripts/check-packaging.py
bash scripts/build-debs-native.sh        # writes build/debs/*.deb
```

`scripts/build-debs-native.sh` builds from a copy on the native Linux filesystem,
because a Windows drive mounted through WSL cannot express Unix permissions and
Debian packaging depends on the executable bit.

## Run from a source checkout

```sh
export PYTHONPATH="$PWD/src"
python3 -m pytest -q                     # unit, contract, and security tests
python3 scripts/smoke-core.py            # end-to-end IPC smoke test
python3 -m agentos                       # run the Core service in the foreground
python3 -m agentos.cli doctor            # diagnose an installation
python3 -m agentos.cli --socket /tmp/core.sock demo
```

## Verify the DSH integration

The design outline requires that the runtime revision, its Node.js requirement,
and its wire behaviour are **proven**, not assumed:

```sh
bash scripts/probe-dsh-sdk.sh    # installs the locked revision and probes the wire protocol
bash scripts/probe-dsh.sh        # reports the locked revision, the checkout, and Node.js
bash scripts/dump-dsh-config.sh  # prints the profile tree and the registered model routes
```

The result on Ubuntu 24.04 / x86_64 / Node.js 22.22.2 is recorded in
[docs/dsh-integration.md](docs/dsh-integration.md), including a correction to the
upstream protocol document.

## Documentation

| Page | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | processes, boundaries, data flow, and the module map |
| [docs/threat-model.md](docs/threat-model.md) | assets, adversaries, trust boundaries, and residual risk |
| [docs/permissions.md](docs/permissions.md) | privilege levels, the shipped policy, and how to raise it |
| [docs/dsh-integration.md](docs/dsh-integration.md) | the locked revision, the wire contract, and verified behaviour |
| [docs/gnome-integration.md](docs/gnome-integration.md) | the extension, the session broker, and the D-Bus surface |
| [docs/runtime-api.md](docs/runtime-api.md) | the `AgentRuntime` protocol and the event vocabulary |
| [docs/packaging.md](docs/packaging.md) | packages, paths, upgrade and removal behaviour |
| [docs/adr/](docs/adr/) | the architecture decision records |

## Verify an installed system

```sh
sudo apt install ./agentos-meta_*.deb
agentosctl doctor        # no credential and no network needed
sudo systemctl start agentos-core
agentosctl demo          # task -> runtime -> policy -> approval -> audit
```

## License

Apache-2.0. See [debian/copyright](debian/copyright).
DeepSeek Harness is not redistributed by these packages; its licence is recorded
in `third_party/notices/`.
