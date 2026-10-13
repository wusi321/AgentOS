# ADR-001: Build on the Ubuntu userspace rather than modifying the kernel

**Status:** accepted
**Date:** 2026-10-13

## Context

AgentOS needs to observe a system (CPU, memory, disks, processes, services, logs)
and, under policy, act on it. Every one of those needs has an existing Linux
interface. The product is intended to be installable on an ordinary Ubuntu Desktop
machine by an ordinary user.

## Options considered

1. **A kernel module or a patched kernel.** Would give complete visibility and
   enforcement, and would make eBPF-style tracing trivial.
2. **A userspace platform on unmodified Ubuntu.** Uses `/proc`, `/sys`,
   `statvfs`, systemd D-Bus, and journald.
3. **A container-first platform.** Would isolate the agent, at the cost of not
   seeing the host it is supposed to diagnose.

## Decision

Option 2. AgentOS is a userspace platform. It modifies no kernel, ships no module,
and requires no boot parameter.

## Reasoning

* An out-of-tree module must be rebuilt for every kernel the user's machine
  receives, and Ubuntu Desktop updates its kernel regularly. A platform that breaks
  on a security update is not a platform.
* Every fact the product needs is already published. `/proc/stat`,
  `/proc/meminfo`, `/proc/mounts`, `statvfs`, `systemctl show`, and `journalctl`
  answer the diagnostic questions the product exists to answer.
* Signed-module requirements, Secure Boot, and DKMS make a module a support burden
  rather than a feature.
* A container cannot diagnose the host's kernel pressure, which is the primary use
  case.

## Security impact

**Positive.** No kernel code means no kernel attack surface added by AgentOS, and
no path to a privilege escalation that a kernel bug would provide. The service runs
unprivileged and its containment is systemd's, which is well understood and
independently maintained.

**Negative.** Enforcement is cooperative rather than absolute: a sufficiently
privileged adversary can bypass a userspace policy engine. This is why the ADR
below on the privileged executor keeps that executor small and separate, and why
the threat model names local root as out of scope.

## Maintenance cost

Low. No kernel compatibility matrix. The cost that remains is keeping up with
systemd and journald output formats, which is one place in one module
(`agentos/system_bridge/services.py`) and is covered by tests that skip cleanly
when those tools are absent.

## Review trigger

Revisit if a required capability genuinely cannot be expressed through an existing
userspace interface. The specific candidate is fine-grained file-access
enforcement for the runtime account, which would be Landlock or a mount namespace
rather than a new module; evaluate those first.
