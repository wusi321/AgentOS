# ADR-007: No privileged executor in the first release

**Status:** accepted
**Date:** 2026-10-13

## Context

The design reserves P3 administrative operations — package management, network
configuration, user management, power control — and describes a separate,
minimally privileged executor with polkit actions for them. It also requires that
the model never obtain root, that a shell string never be accepted, and that
approvals bind to a target and a parameter digest.

The first release has to decide what to build now and what to defer, and it has to
be honest about the difference.

## Options considered

1. **Build the full privileged executor** with polkit actions for a broad operation
   set.
2. **Give the core service `CAP_SYS_ADMIN` or a sudo rule** for convenience.
3. **Implement a minimal executor for two operations that need no privilege, and
   declare P3 operations as denied rather than unimplemented.**
4. **Implement nothing that writes, so the product is purely observational.**

## Decision

Option 3.

Implemented, with approvals:

| Operation | Level | Why it needs no privilege |
|---|---|---|
| `system.service.restart` | P2 | `systemctl restart` on a unit the operator is permitted to manage; the allowlist is the control |
| `system.process.terminate` | P2 | `SIGTERM` to a process the **requesting user** already owns |

Declared and denied, so a model receives a clear refusal rather than a missing
tool:

| Operation | Level |
|---|---|
| `system.packages.install` | P3 |
| `system.network.configure` | P3 |
| `system.shell.execute` | P3 |
| `system.power.manage` | P3 |
| `system.files.write` | P3 |

## Reasoning

* **Option 1 is where a security incident would come from.** A privileged executor
  is the component that turns a model's text into a root action. Building a broad
  one before the policy engine and the approval binding have been exercised in
  production is the wrong order. The design outline says the same thing: the
  executor arrives in a later phase, with the smallest possible surface.
* **Option 2 contradicts the core requirement.** A capability on the core service
  is privilege the model's process is one bug away from. The whole point of the
  process-identity split (ADR-003) is that no model-driven code holds privilege.
* **Option 3 delivers working controlled operation now.** Restarting an allowlisted
  service is the single most useful repair action for the product's primary use
  case, and it needs no privilege at all: it needs a policy that names the unit and
  a human who approves the specific restart. Signalling a process the user owns is
  the second, and its ownership check is a property of the kernel rather than of
  our code.
* **Option 4 would make the product a monitor.** The design's stated experience is
  "diagnose, propose, and, on approval, repair". Option 3 reaches that without
  privilege.
* **Declared-and-denied is better than absent.** A model that asks for
  `system.packages.install` receives `deny` with a reason, and the attempt is
  audited. A model that finds no such tool might try something adjacent; a model
  that is told the operation exists and is refused learns the boundary.

## How the two operations are controlled

* **A closed operation set.** `EXECUTABLE_OPERATIONS` contains exactly two names.
  Anything else raises `ValidationError`. There is no dispatch by string and no
  generic "run this" path.
* **A proof object, not parameters.** The executor receives an
  `ExecutionAuthorization` produced by the policy engine for these exact
  parameters, and re-checks that it covers the operation and the parameter set
  before acting.
* **An allowlist for the target.** `system.service.restart` refuses a unit that is
  not in the policy's `allowlisted_service_units`; an empty allowlist refuses
  everything, so the operation cannot run "because nobody configured a denylist".
* **Hard refusals in the executor.** Never pid 0 or 1, never itself, and — for a
  non-root caller — only a process whose uid matches the caller's, read from
  `/proc/<pid>/status` rather than from the request.
* **No shell.** The helpers run with a fixed `argv` and `shell=False`; the source
  guard test fails the build if that changes.

## Security impact

**Positive.** No component holds privilege, so there is no privileged component to
compromise. The worst a fully compromised model process can achieve is the two
declared operations on an allowlisted target, each requiring a human approval
bound to that exact target.

**Negative and accepted.** A user who wants AgentOS to install a package or fix the
network cannot ask it to. The product is less capable than its roadmap, and says so
in `agentosctl policy inventory`, in the threat model, and here.

## Maintenance cost

Low now, real later. Adding the privileged executor means: a new service with its
own identity, polkit actions with narrowly scoped arguments, a second IPC surface,
its own auditing path, and a security review of each operation. The design's
requirement applies — only a very small set of easily verified allowlisted
operations, with the operator, the parameter digest, the approval, the exit code,
and the result all recorded.

## Review trigger

Build this ADR's successor when the policy engine and the approval binding have
been exercised in production, when at least one P3 operation has a genuinely
narrow definition (an operation with a fixed target and no free-form input), and
when there is a test suite that proves the executor refuses an arbitrary shell
string. Any widening of the operation set before those conditions is a change to
this decision and needs its own record.
