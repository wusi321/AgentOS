# Threat model

This page states what AgentOS protects, from whom, and what remains exposed. It
is written to be falsifiable: each mitigation names the code that implements it,
and each residual risk is one an operator has to accept knowingly.

## Assets

| Asset | Why it matters | Where it lives |
|---|---|---|
| Model credential | bills the owner and grants model access | `/etc/agentos/credentials.env`, mode 0600, root-owned |
| Authority to change the system | a wrong action can break a machine | the policy engine and the approval records |
| Audit trail | the only record of what an agent did | `/var/lib/agentos/agentos.sqlite3` |
| Task contents | a prompt can contain confidential material | the same database |
| User's own authority | the desktop app runs as the user, not as root | the session broker boundary |
| System confidentiality | process lists and logs reveal activity | `ProtectProc=invisible`, journal scoping |

## Adversaries

| Adversary | Capability | In scope |
|---|---|---|
| A model that produces hostile output | any text, tool name, or tool argument | **yes** — the primary threat |
| A prompt-injected document or web page | instructions embedded in data the agent reads | **yes** |
| A local user without authorization | can connect to the socket if group permissions allow | **yes** |
| A compromised runtime process | arbitrary code as the `agentos` account | **yes** |
| A user who is in the `agentos` group | full CLI access as themselves | partly — authorization is per-object, not per-human |
| A local root user | everything | **no** — root can do anything already |
| A remote attacker | network access to the service | **no listener exists** |
| Malicious package content | code in the `.deb` | out of scope — install only from a trusted build |

## Trust boundaries and what enforces them

### 1. Model output is never authority

**Attack.** The model asks for `system.packages.install`, or invents a tool name,
or passes `{"unit": "cups.service; rm -rf /"}`.

**Enforcement.**

* every tool has a capability manifest with an explicit privilege level; a tool
  with no manifest is **denied** (`PolicyEngine.evaluate`, `find_manifest`);
* parameters are validated against a closed schema before any decision is made,
  so an unknown, missing, or malformed parameter is a denial;
* the parameter validator rejects shell metacharacters, NUL bytes, and any
  parameter whose *name* looks like a credential, as defence in depth for the
  executor;
* administrative tools are declared `denied` in the manifest itself, so they are
  refused even at P3 authority.

**Residual risk.** A model can still ask for a permitted operation. That is why a
permitted P2 operation needs an approval.

### 2. An approval cannot be redirected or replayed

**Attack.** Obtain an approval for a harmless target and use it for a harmful one;
or reuse a consumed approval.

**Enforcement.** An approval binds five facts: the operation, the target, the
SHA-256 digest of the canonical parameters, the requesting user, and the task
(`Approval.check_binding`). It also carries a single-use nonce compared with
`secrets.compare_digest`, and an expiry instant. Consumption is a conditional
`UPDATE … WHERE state = 'granted' AND expires_at > ?` inside a transaction, so two
concurrent attempts cannot both succeed. The policy engine re-evaluates at
execution time (`authorize_execution`), so a policy that was tightened after the
approval still applies.

**Residual risk.** A human can approve a harmful operation after reading an
accurate description of it. The UI's job is to make the description accurate,
which is why it shows the operation, target, parameters, digest, and a plain
statement of the effect, and never only the model's summary.

### 3. No shell, ever

**Attack.** Get AgentOS to run a command string.

**Enforcement.**

* there is no code path that runs a shell: `subprocess` is only used with a fixed
  `argv` and `shell=False`; `os.system`, `os.popen`, and the `exec` family are
  absent;
* a source-level guard test fails the build if `shell=` or those calls appear;
* the two helpers that do run (`systemctl`, `journalctl`) receive validated unit
  names, and `validate_unit_name` enforces the systemd grammar, refuses a leading
  dash, and refuses path separators;
* the capability manifest rejects a parameter named `command`, `shell`, `script`,
  `argv`, `eval`, or `exec` at manifest construction time, so such a tool cannot
  even be declared.

### 4. The IPC surface cannot be used to impersonate or enumerate

**Attack.** Connect as one user and act as another; or probe for tasks that exist.

**Enforcement.** The core reads `SO_PEERCRED` from the connected socket, so the
uid is the kernel's, not the client's. Every task and approval carries an owner,
and `_require_task_access` refuses a caller who is not the owner or root. A task
that does not exist and a task owned by someone else produce the **same** denial,
so the surface cannot be used to enumerate. An unknown protocol version and an
unknown method are answered explicitly rather than ignored.

**Residual risk.** Membership in the `agentos` group is the gate for reaching the
socket at all. Adding a user to that group grants them their own authority, not
another user's, but it is still a deliberate act; the packages print the exact
command instead of adding users automatically.

### 5. The runtime child is contained

**Attack.** The runtime reads the service's credentials from the environment, or
inherits an unrelated deployment variable.

**Enforcement.** The child gets an allowlisted environment built by
`minimal_child_environment`: a fixed `PATH`, a handful of locale and terminal
variables, and `DSH_HOME`. The parent environment is never inherited wholesale.
The credential reaches the child only as `DEEPSEEK_API_KEY`, and only from the
root-owned, mode-0600 file.

**Residual risk.** The runtime is a real program with its own tools, and it runs
as the `agentos` account. Its own tool set is not disabled by AgentOS: that is the
runtime's configuration and sandbox, and it is a deployment decision recorded
below in "Accepted risks".

### 6. Secrets do not leak into durable state

**Attack.** A credential appears in a log line, an event payload, an audit row, or
a database column.

**Enforcement.**

* `redact` replaces any member whose key matches a credential marker
  (`api_key`, `token`, `password`, `secret`, `authorization`, …) with
  `<redacted>`, recursively;
* `bounded_payload` redacts **before** it measures, and shortens oversized strings
  without overwriting the redaction; a payload that still does not fit becomes a
  digest summary rather than raw text;
* the credential file is read into the process environment only, is never stored,
  and is never returned by any IPC method;
* the core refuses to read the file at all when it is group- or world-readable;
* the GNOME extension and the desktop application never see a credential.

### 7. System state cannot be written outside the declared paths

**Attack.** The service is tricked into writing to a system path.

**Enforcement.** `ProtectSystem=strict`, `ProtectHome=yes`, an empty
`CapabilityBoundingSet`, `NoNewPrivileges=yes`, and `SystemCallFilter=@system-service`
surround the unit. The only writable locations are the `StateDirectory`,
`CacheDirectory`, `LogsDirectory`, and `RuntimeDirectory` that systemd creates.

**Tested, not copied.** `ProcSubset=pid` was removed from the unit because it made
every metrics tool fail with "/proc/stat is not available on this system" —
exactly the outcome the design outline warns about when sandbox directives are
applied without testing. `ProtectProc=invisible` is kept, because the bridge only
needs this account's processes.

### 8. A crash cannot silently re-run a side effect

**Attack.** Crash the service between "operation started" and "operation
recorded", then let recovery re-run it.

**Enforcement.** `TaskRecord.side_effects_possible` is set before a side-effecting
operation begins. Recovery requeues a task only when that flag is false; otherwise
it **fails** the task with a message telling the operator to check the target's
state (`recovery_action`). Retrying an unconfirmed operation is worse than
reporting it.

## Accepted risks

| Risk | Why accepted | Mitigation available to an operator |
|---|---|---|
| The runtime's own tools run with the `agentos` account's privileges | DSH's tool set is the harness's configuration, not AgentOS's, and pretending to disable it would be a false claim | restrict the harness profile; run the service on a host whose `agentos` account has nothing else to reach |
| A user in the `agentos` group can create P2 tasks | that is what the group means; the approval gate is the real control | keep the group small; leave the policy read-only |
| `systemctl` and `journalctl` output can contain sensitive text | an operator needs it to diagnose a machine | scope journal reads by unit and priority; the audit trail redacts credential-shaped keys |
| The audit trail is local and writable by the service account | there is no remote collector in this release | ship `/var/lib/agentos` to a remote log store; `ProtectSystem=strict` already limits where it can be written |
| The database is not encrypted at rest | full-disk encryption is the platform's job and is standard on Ubuntu Desktop | enable LUKS at install time |
| A model can read whatever `/proc` and `journalctl` show the `agentos` account | that is the P0 read surface the product exists to provide | `ProtectProc=invisible` limits it to this account's processes; do not enable P2 until the machine's threat model allows it |

## Out of scope for the first release

* Protection against a local root user. Root can already do anything.
* Protection against a malicious AgentOS package. Install from a trusted build;
  the packaging is reproducible and audited in `docs/packaging.md`.
* Kernel-level enforcement. There is no kernel module and no eBPF.
* Network-facing security. No component listens on a network address.
* Supply-chain verification of the DSH revision beyond the pinned commit and the
  reproducible verification in `docs/dsh-integration.md`.

## Verifying these claims

Each mitigation above has a test that fails when it is removed:

| Claim | Test |
|---|---|
| undeclared tools are denied | `tests/unit/test_policy.py::test_undeclared_tool_is_denied` |
| shell-shaped parameters are rejected | `tests/security/test_security.py::test_shell_metacharacters_are_rejected_in_free_form_strings` |
| no code path runs a shell | `tests/security/test_source_guards.py::test_no_shell_execution_in_source` |
| the transport is not a network listener | `tests/security/test_source_guards.py::test_no_network_listener_in_source` |
| approvals are bound and single-use | `tests/security/test_security.py` (`test_approval_*`) |
| another user's task is refused | `tests/security/test_security.py::test_ipc_denies_another_users_task` |
| credentials are redacted before storage | `tests/security/test_security.py::test_bounded_payload_redacts_before_it_measures` |
| the runtime environment is allowlisted | `tests/contract/test_runtime_contract.py::test_child_environment_is_allowlisted` |
| an unconfirmed side effect is not retried | `tests/unit/test_task_states.py::test_recovery_fails_when_side_effect_is_unconfirmed` |
| the service does not run as root | `scripts/check-packaging.py` and `scripts/verify-debs.sh` |
