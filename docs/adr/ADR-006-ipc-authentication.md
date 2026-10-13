# ADR-006: Authenticate IPC with `SO_PEERCRED`, and reach the Shell through a session broker

**Status:** accepted
**Date:** 2026-10-13

## Context

Four components need to talk to each other: the Shell extension, the desktop
application, the session broker, and the core service. They have different trust
levels and different lifetimes, and the design forbids opening a network-facing
management interface. The core has to know *who* is asking, because authorization
is per user and per task.

## Options considered

For core-to-client transport:

1. **A TCP listener on `127.0.0.1` with a token.** Easy to reach from anything.
2. **A Unix domain socket with `SO_PEERCRED`.**
3. **System D-Bus with a polkit action per method.**

For the Shell's access:

4. **The extension talks to the core service directly.**
5. **A per-login session broker owns the Shell's interface.**

## Decision

Option 2, plus option 5.

* The core listens on `/run/agentos/core.sock`, mode 0660, group `agentos`.
* Every request's identity comes from `SO_PEERCRED`: the pid, uid, and gid the
  kernel recorded when the peer connected.
* `agentos-session` runs per login as a systemd user unit and owns
  `org.agentos.Session1` on the session bus.
* The Shell extension speaks only to that session-bus interface.

## Reasoning

**Why not a TCP listener.** A loopback listener is a network service. It is
reachable by every local user, its authentication would have to be invented
(a token, which then has to be stored somewhere and rotated), it appears in
`ss -ltnp`, and it is one `iptables` misconfiguration from being reachable
remotely. A Unix socket gets filesystem permissions and kernel-verified peer
credentials for free. This is also the behaviour the design requires: no `0.0.0.0`
listener, no HTTP service.

**Why `SO_PEERCRED` rather than a claimed identity.** A client cannot lie about
its uid to the kernel. Nothing in the request frame is trusted for identity, which
means there is no token to steal, no header to forge, and no way for a client to
act as another user by editing a field.

**Why a session broker rather than direct Shell access.** The extension runs inside
the GNOME Shell process. Giving it the core's protocol means every protocol change
touches the Shell, and a Shell-side fault becomes a core-facing fault. The broker
localizes both: the extension has one small interface that reports a status
document and starts a window, and the core's protocol can change without the Shell
noticing. It also puts a process boundary between the Shell and the system service,
which is what makes "a dead service must not disturb the Shell" a structural
property rather than a promise.

**Why not D-Bus with polkit for everything.** It was considered and is not
excluded forever: option 3 is a reasonable home for the P3 privileged executor,
where a system-level policy decision and a bus name are both useful. For the
high-traffic, per-request core API it adds a daemon, a bus round trip, and a policy
file per method — for an interface whose policy decisions are already made by an
engine that has more context than polkit does.

## Security impact

**Positive.**

* Identity is kernel-verified, so authorization cannot be spoofed.
* The socket's group ownership is the coarse gate; per-object ownership is the fine
  one. A task that does not exist and a task owned by another user produce the
  **same** denial, so the interface cannot be used to enumerate.
* A user outside the `agentos` group is refused by the filesystem, with a message
  that says exactly what to do.
* No network listener exists, which removes an entire class of attack.

**Negative and accepted.**

* Group membership is required for access. The packages print the command
  (`sudo adduser "$USER" agentos`) rather than adding users automatically, because
  granting access silently is not the same as telling someone how to grant it.
* A group change requires a new login session, since the kernel checks the
  credentials a process already has. The denial message says so.
* An unknown protocol version is refused explicitly rather than negotiated, so a
  newer client cannot silently mis-speak to an older service. The version is a
  constant in `constants.py` and in both frames.

## Evidence

The behaviour is tested and was verified on an installed system:

* `tests/security/test_security.py::test_ipc_denies_another_users_task` — a peer
  that is not the owner is refused, and cannot distinguish a missing task from a
  foreign one.
* `test_ipc_rejects_an_unknown_protocol_version`, `test_ipc_reports_an_unknown_method`,
  `test_ipc_rejects_a_non_object_frame` — malformed input is answered, not dropped.
* `test_ipc_socket_is_never_a_network_listener` — a source-level guard fails the
  build if `AF_INET`, `0.0.0.0`, or `start_server` appears in the package.
* `scripts/verify-live.sh` — on the installed system, a user outside the group is
  refused with an actionable message, and after the documented `adduser` step the
  CLI reports the kernel-recorded uid through `agentosctl hello`.

## Maintenance cost

Low. The socket protocol is twelve methods over newline-delimited JSON, versioned
by one integer. The session-bus interface is four methods and one signal, with
introspection data so `gdbus` works without reading the code.

## Review trigger

Revisit if a client must reach the core from outside the local machine (which would
need an authentication story this design deliberately avoids), if per-method polkit
authorization becomes a requirement for a P3 operation, or if the protocol needs
negotiation rather than an exact version match — for instance to support an LTS
client against a newer service.
