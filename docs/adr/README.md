# Architecture decision records

Each record states the context, the options considered, the decision, the
reasoning, the security impact, the maintenance cost, and the condition that
should trigger a review.

| ADR | Decision |
|---|---|
| [001](ADR-001-userspace-not-kernel.md) | Build on the Ubuntu userspace rather than modifying the kernel |
| [002](ADR-002-gnome-and-gtk4.md) | GNOME Shell extension plus a GTK4/libadwaita application, not a Shell fork |
| [003](ADR-003-process-identities.md) | Separate the core service, the runtime worker, and the desktop into distinct identities |
| [004](ADR-004-runtime-protocol.md) | Define an `AgentRuntime` protocol and drive DSH through its SDK JSON-RPC stdio profile |
| [005](ADR-005-dsh-and-node-pinning.md) | Pin the DSH revision and treat Node.js as a deployment choice |
| [006](ADR-006-ipc-authentication.md) | Authenticate IPC with `SO_PEERCRED` on a Unix socket, with a user session D-Bus broker |
| [007](ADR-007-privileged-executor.md) | No privileged executor in the first release; two structured operations only |
| [008](ADR-008-task-persistence.md) | Persist tasks in SQLite behind one state machine, and never retry an unconfirmed side effect |
| [009](ADR-009-deb-split.md) | Five binary packages from one native source tree, with conffiles and data retention |
| [010](ADR-010-iso-strategy.md) | Cubic for the ISO prototype, a repeatable chain for production |
| [011](ADR-011-credentials.md) | Credentials from a root-owned environment file; no cross-logout credential model yet |

## How to write a new one

Copy the structure of an existing record. State the **review trigger** honestly: an
ADR whose revisit condition is "never" is a decision nobody can challenge, which
is not what this log is for.

Record a new ADR when a change affects one of these:

* a trust boundary or the identity a component runs as;
* the wire or storage format of durable data;
* the runtime protocol or an adapter's capability claims;
* what a package installs, or what a removal preserves;
* a security control's presence or absence.
