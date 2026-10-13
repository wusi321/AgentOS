# ADR-011: Credentials from a root-owned environment file, and no cross-logout credential model yet

**Status:** accepted
**Date:** 2026-10-13

## Context

AgentOS needs a model credential to do model-backed work, and it deliberately runs
its runtime in a system service that can outlive the user's login. That combination
is the hard part: the natural place for a user's credential on a Linux desktop is
the login keyring, and the keyring is locked or unavailable after logout.

The design requires that a key never be written to the database, a log, a
configuration file, a command line, or a shell history.

## Options considered

1. **The user's keyring (Secret Service / libsecret).** The desktop-native answer.
2. **A credential in `/etc/agentos/agentos.toml`.** Convenient.
3. **A systemd `EnvironmentFile`** the service reads.
4. **A command-line argument or an environment variable set by the user's shell.**
5. **A service-level credential flow** with an explicit owner, unlocking policy, and
   revocation.

## Decision

Option 3 for this release, with the strict conditions below, and option 5 explicitly
deferred rather than half-built.

* The credential lives in `/etc/agentos/credentials.env`, which **no package
  installs** — only a `credentials.env.example` template.
* The file must be mode `0600` and root-owned. The service **refuses to read it at
  all** when it is readable by group or other, and says why.
* Only an allowlisted set of variable names is read (`DEEPSEEK_API_KEY`,
  `DEEPSEEK_BASE_URL`, proxy variables). An unrelated variable in that file cannot
  reach the runtime child.
* The value is placed in the process environment and never persisted, never
  returned by any IPC method, and never logged.

## Reasoning

**Why not the keyring.** It is the right answer for a *user-session* agent and the
wrong one for a service that must survive logout. A service cannot unlock a user's
keyring after the user has gone, and pretending otherwise would produce a feature
that works in testing and fails overnight. The design states this explicitly, and
this ADR takes it at its word: a service-level credential needs its own design, not
a keyring call.

**Why not the config file.** `agentos.toml` is a conffile: dpkg shows it in diffs,
administrators copy it between machines, and it is readable by design. Put a
credential in it and it will end up in a paste buffer, a backup, and a bug report.

**Why the permission refusal rather than a warning.** A credential file that is
world-readable is a live incident, not a style problem. Refusing to start model work
makes the situation visible immediately, with a message that names the file and its
mode. A warning buried in a journal is how a leaked key goes unnoticed for a month.

**Why the allowlist.** A file that accumulates variables is a file that
accidentally passes something else to a model-driven process. Reading only known
names bounds what can leak.

**Why no command line and no shell profile.** Arguments are visible in `ps` to
every user on the machine, and a profile-exported variable is captured in shell
history and in every child process the user starts. The design forbids both, and the
reason is concrete rather than stylistic.

## What is deferred, and why that is the honest choice

A model that must keep working after logout needs a credential whose *owner* is the
service, not the human. That means deciding:

* who owns it and who may read it;
* how it is unlocked at boot without a human;
* how it is rotated;
* how it is revoked when it leaks;
* what the threat model is for a key that exists on disk in a form the service can
  read unattended.

None of those has an answer that is obviously right, and building a partial version
would produce a credential store that *looks* secure and is not. The first release
therefore requires a root-owned, mode-0600 file and no more, and documents that a
credential in such a file is readable by anyone who becomes root or who reads the
disk.

This is an accepted limitation, recorded in `docs/threat-model.md`, not a solved
problem.

## Security impact

**Positive.** No credential is ever written by a package, returned by an IPC method,
stored in the database, or placed in an event payload. The audit trail redacts any
member whose key looks credential-shaped, so even a caller that supplies one cannot
persist it. The permission check turns the most common misconfiguration into a loud
failure.

**Negative and accepted.** The credential is at rest in a file that root and disk
access can read. Full-disk encryption is the platform's mitigation and is standard
on Ubuntu Desktop; the operator has to enable it.

## Evidence

* `read_credential_environment` refuses a file whose mode grants group or other
  access, and reads only allowlisted names.
* `agentosctl doctor` reports whether a credential is configured, and its
  diagnostics include a `credential_configured` flag — never the value.
* `tests/security/test_security.py` asserts that credential-shaped keys are redacted
  from persisted payloads, including when the payload is also oversized.
* `scripts/verify-debs.sh` asserts that **no package installs a
  `credentials.env`**, only the example.

## Maintenance cost

Low. The cost arrives with the deferred design, and it should arrive as a new ADR
with a threat model, not as an extension of this one.

## Review trigger

Revisit when any of these becomes true:

1. a task must run model-backed work after the user's logout in a supported way;
2. a requirement appears for per-user rather than per-machine credentials;
3. the project adopts a service-level secret store (for example a system
   credential store with an explicit unlock policy);
4. a deployment needs credential rotation without a service restart.
