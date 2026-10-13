# Permissions and the policy engine

AgentOS grants nothing by default. Every operation a model can ask for has a
declared privilege level, and the shipped policy permits only read-only
inspection. This page explains the levels, the rule language, the approval
mechanism, and how to enable more.

## Privilege levels

| Level | Name | Meaning | Shipped posture |
|---|---|---|---|
| P0 | read-only | reads `/proc`, `statvfs`, `systemctl show`, bounded `journalctl` | **allowed** |
| P1 | restricted write | writes AgentOS's own state or the task's workspace | no tool requires it yet |
| P2 | controlled operation | one real system side effect on a fixed, allowlisted target | declared, needs an approval, **refused** by the shipped policy |
| P3 | administrative | packages, network, users, power, system configuration | declared and **denied** |

The numeric order is meaningful: a policy rule may cap a task at a level, and a
level is a strict superset of the risk below it.

## Where authority is declared

Two places, and both must agree:

1. **The capability manifest** (`src/agentos/policy/capabilities.py`) describes
   what a tool *is*: its name, its level, its closed parameter set, whether it
   acts on one target, and whether an approval is mandatory. A tool with no
   manifest is denied, so adding a tool without declaring its blast radius is a
   hard failure rather than a silent grant.
2. **A policy rule** (`/etc/agentos/policies/*.yaml`) decides what this
   deployment *permits*.

A tool must be permitted by a rule and must not exceed either the policy ceiling
or the task's own cap. Any of those failing is a denial with a reason naming which
one failed.

## The shipped policy

`/etc/agentos/policies/default-policy.yaml` caps the deployment at **P1** and
explicitly denies every P2 and P3 operation:

```yaml
name: default-read-only
max_level: P1
allowlisted_service_units: []
rules:
  - name: allow-list-read-tools
    action: allow
    tool: "system.processes.list"
    max_level: P0
  - name: deny-terminate-under-read-only-policy
    action: deny
    tool: "system.process.terminate"
    max_level: P2
```

Consequences you can rely on: `agentosctl policy evaluate system.packages.install`
reports `deny`, and an approval *cannot even be created* for an operation the
policy denies. A model cannot manufacture an approval prompt for an action
AgentOS would never take.

## Rule language

```yaml
name: <document name>
max_level: P0..P3            # hard ceiling for this document
allowlisted_service_units:   # targets system.service.restart may use
  - cups.service
minimum_approval_ttl_seconds: 30
maximum_approval_ttl_seconds: 900
rules:
  - name: <stable rule name>
    action: allow | require_approval | deny
    tool: "system.processes.list"   # exact name, or a trailing "*" prefix pattern
    max_level: P0..P3               # highest level this rule is willing to cover
    requires_approval: true         # force a gate even below P2
    allowed_targets: [cups.service] # closed target set for this rule
    description: "<operator-facing rationale>"
    priority: 0                     # higher wins; specificity breaks ties
```

Rules that fail to parse are a **load-time error**, not a silently ignored line.
That includes an unknown key, an unknown action, an unknown level, and an inverted
approval window. A policy the reader does not fully understand is never applied
partially.

### Rule selection

For one tool call, the engine gathers every rule whose pattern matches, sorts them
by `(priority, specificity)`, and uses the first. A rule that names the tool
exactly beats a prefix pattern; `priority` overrides that.

### What a rule cannot do

* A rule cannot lower a tool's declared level. `_load_manifest_override` rejects
  that, because relaxing authority is a policy change and not a capability
  description.
* A rule cannot invent a tool AgentOS does not implement.
* A blanket `allow` at P3 is rejected at rule construction.
* A `tool` pattern may use a trailing `.*` (a prefix) or an exact name. A wildcard
  in the middle is refused on purpose: an over-broad pattern is how a policy
  quietly grants more than its author intended.

## Approvals

An approval is the authorization for **one** operation. It binds:

| Bound fact | Why |
|---|---|
| operation | an approval for a restart cannot authorize a kill |
| target | an approval for `cups.service` cannot authorize `sshd.service` |
| SHA-256 digest of the canonical parameters | any parameter change invalidates it |
| requesting user | another local user cannot use it |
| task | it is not transferable between tasks |
| expiry instant | a grant is not permanent |
| single-use nonce | it cannot be replayed |

The parameter digest is computed from canonical JSON, so key order and formatting
cannot change an approval's identity.

### Lifecycle

```text
 pending ──grant──▶ granted ──consume──▶ consumed      (usable exactly once)
    │                  │
    └──deny──▶ denied  └──revoke──▶ revoked
    └──expiry──▶ expired
```

Only a `granted`, unexpired, unconsumed approval with the right nonce authorizes
an execution. Consumption is a conditional update inside a transaction, so two
concurrent executions cannot both consume it.

### What the human sees

`agentosctl approval show <id>` and the desktop's Approvals view print the
operation, the target, the exact parameters, the parameter digest, the level, the
requesting user, the expiry, and a plain statement of the effect written by
AgentOS rather than by the model:

> Send SIGTERM to process '4242'. Unsaved state in that process is lost.

An approval UI that showed only the model's natural-language summary would let a
model describe a harmful operation as a harmless one. AgentOS generates the
description from the operation identifier and the validated target.

## Task-level authority

A task is created with a privilege cap, defaulting to `P0`:

```sh
agentosctl task run "diagnose the slow filesystem"                  # P0 (default)
agentosctl task run "restart the print service" --level-cap P2      # needs a P2-capable policy
```

The cap is independent of policy. Both must allow the operation, and the task's
cap is what stops a model from escalating within its own conversation: a task
created at P0 cannot obtain a P2 approval even when the policy would permit one.

Only the superuser may create a P3 task, and only when the policy reaches P3.

## Enabling a controlled operation

Enabling a P2 operation is a deliberate, auditable act. On a Desktop install:

```sh
# 1. Review what the operation does.
agentosctl policy inventory
agentosctl policy evaluate system.service.restart --params '{"unit": "cups.service"}'

# 2. Install a site policy that permits exactly the targets you accept.
sudo install -m 0644 /usr/share/doc/agentos-core/examples/50-site-policy.yaml.example \
                     /etc/agentos/policies/50-site-policy.yaml
sudoedit /etc/agentos/policies/50-site-policy.yaml     # set max_level and the unit list

# 3. Reload. A rejected policy leaves the previous one in effect.
sudo systemctl reload agentos-core
agentosctl policy inventory | head -5

# 4. Submit a task with the matching authority.
agentosctl task run "restart the print service" --level-cap P2
agentosctl approval list          # review and decide
```

A policy that fails to load is reported in the journal and the service keeps the
previous configuration, so a mistake cannot silently widen authority.

## What the executor does with an authorization

The executor is a closed set of two operations:

| Operation | Target rule | Additional refusal |
|---|---|---|
| `system.service.restart` | must be in the allowlist | — |
| `system.process.terminate` | one pid, validated by the schema | never pid 0 or 1, never itself, and only a process the **requesting user** owns |

The executor re-checks that the authorization covers exactly the operation and
parameters it is about to use, then acts. It accepts a proof object, not raw
parameters, so a caller cannot execute something the policy never saw.

## Environment variables

Only `AGENTOS_SOCKET` affects authorization-adjacent behaviour, and it changes
which socket the *client* is asked to use; it cannot change who the caller is.

## Auditing

Every decision is recorded, including denials:

```sh
agentosctl audit summary
agentosctl audit query --kind policy.decision --limit 20
agentosctl audit query --task-id <id>
```

Each record carries the timestamp, task, actor uid, operation, target, decision,
outcome, reason, and the parameter digest. Removing a record is not possible
through any interface: the store is append-only, and retention is an explicit
prune with a configured age and capacity bound. A removal of the package keeps the
trail; a purge keeps it too, and says so.

## See also

* `docs/threat-model.md` — what each of these controls is defending against.
* `docs/adr/ADR-007-privileged-executor.md` — why there is no privileged executor
  in this release and what the next one will look like.
