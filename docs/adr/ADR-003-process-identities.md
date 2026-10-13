# ADR-003: Separate process identities for the core, the runtime, and the desktop

**Status:** accepted
**Date:** 2026-10-13

## Context

AgentOS runs a model-driven process, serves a system API, and shows a desktop UI.
Those three have very different trust levels and lifetimes. The design has to
decide whether they share a process, share an identity, or are separated.

## Options considered

1. **One process.** The GTK application also runs the agent. Simplest to build.
2. **One service identity for everything.** The core and the runtime run as the
   same account.
3. **Separate identities and lifetimes.** A system service account for the core,
   the model process inside it, and the desktop as the logged-in user.

## Decision

Option 3, with the identities the design outline specifies:

| Component | Identity | Lifetime |
|---|---|---|
| core service | system user `agentos`, never root | systemd system unit |
| runtime child | same account, allowlisted environment | per task |
| session broker | the logged-in user | systemd **user** unit |
| desktop application | the logged-in user | the user's session |
| Shell extension | the GNOME Shell process | the Shell's lifetime |

## Reasoning

* **Lifetime.** A task must survive a logout. A core that lives in the user's
  session cannot do that, and the user's session bus and keyring are unavailable
  after logout — so a core that depended on them would break the requirement
  silently.
* **Authority.** The desktop application must not be able to perform a system
  operation directly, or the policy engine is decorative. Running it as the user
  means the worst it can do is what that user could already do, and every actual
  system action goes through the core.
* **Blast radius.** A compromised runtime process gets the `agentos` account, not
  root and not the human's account. That account owns one database, one socket, and
  one state directory.
* **Least privilege.** The core runs unprivileged because nothing it does needs
  privilege; the two controlled operations are evaluated and executed by a
  component that only ever calls `systemctl restart` on an allowlisted unit or
  signals a process the requesting user owns.

## Security impact

**Positive.** This split is the mechanism behind most of the threat model's
mitigations. Because the model's process is not the policy engine and not the
executor, model output is a proposal rather than an action. Because the extension
is not the core, a Shell-side fault cannot escalate.

**Negative.** Three processes mean three failure modes and a real IPC surface. The
mitigation is that the surface is small: one Unix socket with kernel-verified peer
credentials, plus one session-bus interface that only reports state.

**Known gap.** The core and the runtime child share the `agentos` account. A
compromised runtime can therefore read the core's database file. Separating them
needs a workspace-isolation identity, which is deferred, and is recorded as an
accepted risk in the threat model rather than claimed as solved.

## Maintenance cost

Low in code, real in deployment. `agentos` must exist before the service starts,
which is why the postinst creates it and the unit declares it. A future change to
the account name is a data migration, not a rename, and must be treated as such.

## Review trigger

Revisit when a runtime with materially weaker trustworthiness is added, when the P3
privileged executor is introduced, or when a requirement appears to run a task
under a different user's authority. Any of those makes the shared `agentos`
identity for the core and the runtime insufficient.
