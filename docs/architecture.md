# Architecture

AgentOS is a userspace platform on Ubuntu. It adds an agent that can observe a
system and act on it under policy, without giving a model authority. This page
describes the processes, the boundaries between them, and how a request travels.

## Processes and identities

| Process | Identity | Lifetime | Holds |
|---|---|---|---|
| `agentos-core` | system user `agentos`, never root | systemd system service | tasks, approvals, policy decisions, audit trail, read-only system access |
| `agentos-session` | the logged-in user | systemd **user** service per login | the session bus name `org.agentos.Session1`; status relay only |
| `agentos-desktop` | the logged-in user | the user's session | the GTK4 window; no secret, no authority |
| GNOME Shell extension | the GNOME Shell process | the Shell's lifetime | UI state in GSettings only |
| runtime worker | child of `agentos-core`, allowlisted environment | one per task | the model conversation |

Separating the core from the session is what lets a task survive a logout. The
core is a system service, so closing a window, logging out, or restarting GNOME
Shell does not stop a task.

## Boundaries

```text
 GNOME Shell ──session D-Bus──▶ agentos-session ──Unix socket──▶ agentos-core
                                                                      │
 GTK4 window ─────────────────────────Unix socket─────────────────────┘
                                                                      │
                                                              policy engine
                                                                      │
                                                     ┌────────────────┴──────────────┐
                                                     ▼                               ▼
                                             System Bridge                  controlled executor
                                          (/proc, systemctl,               (closed operation set,
                                           journalctl; read-only)           approval-bound)
```

Every arrow crosses a real boundary, and each one is enforced where the authority
lives rather than where the request is made:

* **GNOME Shell → session broker.** The extension is the least trusted component:
  it runs inside the Shell, and a fault there degrades the desktop. It therefore
  holds no authority at all. It asks the broker for a status document and asks it
  to open the desktop window; it cannot approve anything, execute anything, or
  read a file.
* **Session broker → core.** The broker is a per-login process, so it speaks for
  one user. It forwards the status and the events that user is entitled to see.
* **Desktop and CLI → core.** Over a Unix domain socket. The core reads the
  caller's identity from the kernel (`SO_PEERCRED`), never from the request, so a
  client cannot claim to be someone else.
* **Core → system.** The policy engine decides, and the System Bridge or the
  controlled executor acts. A runtime never reaches the system directly: it
  produces a proposal, and the proposal is evaluated.

## Why the core is not root

The core never needs to be root, because the operations it performs do not
require it:

* reading `/proc`, `statvfs`, `systemctl show`, `journalctl` needs no privilege;
* writing its own database needs only ownership of its own state directory;
* the two controlled operations are `systemctl restart <allowlisted unit>` and
  signalling a process the **requesting user** owns.

There is no privileged executor in this release. When one is added for the P3
operations the design reserves, it will be a separate service with a minimal
polkit action set, and the core will keep running unprivileged.

## Request flow

A user instruction travels the whole chain:

```text
 user instruction
   → desktop / CLI
   → core IPC (peer uid from the kernel)
   → task manager: CREATED → QUEUED
   → runtime adapter (mock or DSH) starts a turn
   → normalized runtime events stream back
   → each tool call the runtime reports is evaluated by the policy engine
   → a P0 read executes immediately
   → a P2 operation becomes an approval request bound to target + parameter digest
   → a human approves; the approval is consumed exactly once
   → the executor re-checks policy, then acts
   → the result and every decision are written to the audit trail
   → the desktop and the GNOME panel show the new state
```

Two properties of that flow matter more than the sequence:

1. **The policy check happens twice.** Once when the operation is proposed, and
   again immediately before execution, against the parameters actually about to
   be used. A policy change between the two, or a parameter change, is caught.
2. **The runtime's output is data.** A tool name and its parameters are input to
   the policy engine; the model's prose is never an instruction to the system.

## Module map

| Module | Responsibility | Depends on |
|---|---|---|
| `agentos.constants` | identity, locked versions, filesystem paths | — |
| `agentos.errors` | typed failures with one machine code each | — |
| `agentos.events` | the versioned event envelope and severity vocabulary | `jsonutil` |
| `agentos.jsonutil` | redaction, canonical JSON, payload bounding | — |
| `agentos.schema` | parameter schemas; rejects shell-shaped values | `errors`, `jsonutil` |
| `agentos.yaml_subset`, `agentos.toml` | strict readers for the policy and configuration formats | `errors` |
| `agentos.runtime` | the `AgentRuntime` protocol, capabilities, and event vocabulary | `errors`, `events` |
| `agentos.runtime.mock` | a keyless, scripted runtime | `runtime.api` |
| `agentos.runtime.dsh` | the DeepSeek Harness adapter and its wire transport | `runtime.api`, `dsh_transport` |
| `agentos.system_bridge` | `/proc` collectors, systemd and journald reads, the controlled executor | `errors`, `policy` |
| `agentos.policy` | capability manifests, rules, approvals, the engine | `schema`, `events`, `yaml_subset` |
| `agentos.audit` | the append-only audit store | `events`, `jsonutil` |
| `agentos.tasks` | the state machine, SQLite storage, the task manager | `policy`, `runtime`, `audit` |
| `agentos.core` | configuration, IPC, and the service host that owns the singletons | everything above |
| `agentos.session` | the per-login D-Bus broker | `core.ipc_client` |
| `agentos.desktop` | the GTK4 window and its views | `core.ipc_client` |
| `agentos.cli` | `agentosctl` | `core.ipc_client` |

Dependencies point one way. `agentos.core` is the only module that knows the
whole system, and nothing depends on it except the entry points.

## Data flow into durable state

| Store | Contents | Written by | Read by |
|---|---|---|---|
| `/var/lib/agentos/agentos.sqlite3` | tasks, task history, approvals, audit trail | the core | the core |
| `/var/lib/agentos/workspaces/<id>/` | one working directory per task | the runtime account | the runtime account |
| `/var/lib/agentos/dsh/` | the harness home directory | the harness | the harness |
| `/run/agentos/core.sock` | the IPC endpoint | systemd | CLI, desktop, broker |
| `$XDG_CONFIG_HOME/agentos/` | user preferences | the desktop app | the desktop app |

Configuration, state, cache, and user data are separate directories so that a
backup, a permission change, or a purge can treat them differently.

## Configuration layers

Later layers win, and a reload that fails validation keeps the previous
configuration:

```text
 built-in defaults
   → /etc/agentos/agentos.toml            (package-managed, replaced on upgrade)
   → /etc/agentos/agentos.toml.d/*.toml   (fragments, lexical order)
   → /etc/agentos/agentos.local.toml      (administrator overrides, never replaced)
```

Policy is a separate tree, `/etc/agentos/policies/*.yaml`, loaded in lexical
order. A document named `50-…` is the conventional place for a site policy,
because it sorts after the shipped `default-policy.yaml` and every rule from both
is considered.

## Failure behaviour

| Failure | Consequence |
|---|---|
| no model credential | the core, the read-only tools, the desktop, and the panel all work; a model-backed task fails with the reason |
| runtime process crashes | the task fails with the exit code and a bounded stderr tail; the service keeps running |
| DSH revision or Node.js unsuitable | `agentosctl doctor` says exactly which requirement is unmet; the runtime refuses to start |
| core service stopped | the panel shows an unavailable state; the desktop reports it; systemd restarts the service |
| core restarted mid-task | a task that may already have acted is **failed**, not re-run; one that had not acted is requeued |
| credential file world-readable | the core refuses to read it and says why |
| configuration invalid on reload | the previous configuration stays in effect; the reason is written to the journal |
| database schema newer than the build | the service refuses to start rather than misreading it |

## What is deliberately not here

* No kernel changes. Every capability uses an existing userspace interface.
* No GNOME Shell fork. The extension uses only public extension APIs.
* No network listener. The IPC transport is a Unix domain socket.
* No shell execution path. There is no code in AgentOS that runs a
  caller-supplied command string.
* No privileged executor yet. The P3 operations are declared and refused, not
  implemented.
