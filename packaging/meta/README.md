# AgentOS

An agent-native operating environment for Linux: persistent agents, system
observability, task orchestration, desktop integration, and policy-controlled
actions.

## What this metapackage installs

| Package | Contents |
|---|---|
| `agentos-core` | task manager, policy engine, audit trail, read-only System Bridge, `agentosctl` |
| `agentos-runtime-dsh` | the DeepSeek Harness runtime adapter |
| `agentos-desktop` | the GTK4/libadwaita application and the per-login session broker |
| `agentos-gnome-extension` | the GNOME Shell 42+ panel entry |

## After installing

```sh
# 1. Check the installation. This needs no model credential and no network.
agentosctl doctor

# 2. Start the service and confirm it is healthy.
sudo systemctl start agentos-core
agentosctl health

# 3. Enable the panel as your own user (not with sudo), then log out and back in.
gnome-extensions enable agentos@agentos.dev

# 4. Run the keyless end-to-end demonstration.
agentosctl demo
```

The shipped policy is **read-only**. Until an administrator raises
`max_level` in `/etc/agentos/policies/`, AgentOS can inspect the system and
explain it, and cannot change anything.

## Enabling model-backed analysis

1. Install a Node.js runtime that satisfies `^22.19.0 || >=24.0.0`. The Ubuntu
   22.04 `nodejs` package is Node.js 12 and does not qualify.
2. Install the locked DeepSeek Harness revision; see
   `/usr/share/doc/agentos-runtime-dsh/README.dsh.md`.
3. Put the model credential in `/etc/agentos/credentials.env`, mode `0600`,
   root-owned.
4. Set `runtime_id = "dsh"` in `/etc/agentos/agentos.local.toml`.
5. Restart the service and re-check: `sudo systemctl restart agentos-core && agentosctl doctor`.

## Access

`agentosctl` and the desktop application reach the core service through
`/run/agentos/core.sock`, which is group-owned by `agentos`. A user who is not a
member of that group cannot connect. Add yourself with:

```sh
sudo adduser "$USER" agentos
```

and start a new login session for the group change to apply.

## Where state lives

| Path | Contents |
|---|---|
| `/etc/agentos/` | configuration and policy; administrator-owned |
| `/etc/agentos/credentials.env` | the model credential; never installed by a package |
| `/var/lib/agentos/agentos.sqlite3` | tasks, approvals, task history, audit trail |
| `/var/lib/agentos/workspaces/` | one directory per task |
| `/var/lib/agentos/dsh/` | the harness home directory |
| `/var/log/agentos/` | service logs |

Removing the package stops the service and **keeps** `/var/lib/agentos`.
Purging removes configuration and the service account and still keeps the task
and audit database, so that no uninstall can silently destroy the record of what
an agent did. Deleting that directory is a deliberate administrator action.

## Safety model in one paragraph

The model never holds authority. Every operation AgentOS can perform is declared
in a capability manifest with an explicit privilege level, every request is
checked by the policy engine before it runs and again at execution time, every
side-effecting operation needs an approval bound to its exact operation, target,
and parameters, and every decision and result is written to an append-only audit
trail. No configuration grants a model a root shell, and the administrative
operations are refused outright in this release.
