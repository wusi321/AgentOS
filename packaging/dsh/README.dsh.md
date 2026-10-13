# DeepSeek Harness runtime adapter

This package installs the AgentOS `DshAdapter`, which drives DeepSeek Harness
through its SDK JSON-RPC stdio profile. It does **not** redistribute, download,
or build the harness.

## What you must provide

1. A Node.js runtime that satisfies the locked harness requirement
   (`^22.19.0 || >=24.0.0`). The `nodejs` package from Ubuntu 22.04 is Node.js
   12 and does **not** satisfy it.
2. The harness itself, at the locked revision `0.2.1-alpha.2`
   (commit `d743267388641bc76f17c45ce8b4c231aed1d32c`).
3. A model credential, in `/etc/agentos/credentials.env`, mode `0600`,
   root-owned.

The locked facts are recorded in `/usr/share/agentos/dsh-locked.json`, and the
adapter reports them through `agentosctl doctor` and `agentosctl runtime probe`.

## Installing the harness

The adapter launches a JavaScript entry point. Install the pinned revision into
a directory the service account can read:

```sh
# Example: install into the AgentOS program directory under a private prefix.
sudo install -d -m 0755 -o root -g root /usr/lib/agentos/dsh
sudo npm install --prefix /usr/lib/agentos/dsh \
     --no-audit --no-fund @deepseek-ai/dsh@0.2.1-alpha.2
```

Then point the configuration at the installed entry point:

```toml
# /etc/agentos/agentos.local.toml
[runtime]
runtime_id = "dsh"
provider = "deepseek-official"
model = "deepseek-flash"
node_binary = "/usr/bin/node"
cli_entry = "/usr/lib/agentos/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js"
```

```sh
sudo systemctl restart agentos-core
agentosctl doctor          # reports whether the Node.js requirement is satisfied
agentosctl runtime probe   # reports the adapter's capability set
```

## Verifying the installation

The verification the design outline requires is repeatable without a model
credential:

```sh
# Checks the locked revision, the Node.js requirement, and the wire protocol.
sudo scripts/probe-dsh-sdk.sh
```

## Capabilities and honest limits

| Capability | Supported | Note |
|---|---|---|
| streaming assistant text | yes | DSH compacts the model stream into delta runs; each run is republished as one event and labelled `batched_run` |
| tool call events | yes | `tool/call` and `tool/result` are mapped to `tool.call.started` and `tool.call.completed` |
| session persistence | yes | DSH stores sessions under `DSH_HOME/sessions` |
| reopen a session by id | yes | `session/prompt` with an existing id reattaches |
| cancel a running turn | **no** | the SDK profile has no cancel method; AgentOS stops the runtime process and marks the task cancelled, leaving the session durable for a later resume |
| runtime-side approvals | **no** | the SDK profile sends no approval notification; the AgentOS policy engine is the only approval authority |

The adapter never fabricates an event for a capability it does not have. Ask it
what it supports with `agentosctl runtime probe`.

## Environment

The adapter spawns the harness with an allowlisted environment, never the
parent's. `PATH` is a fixed system path, so a developer's shell `PATH` cannot
decide which tools the runtime resolves. `DSH_HOME` is always explicit: the
harness never falls back to `~/.dsh`, and AgentOS never relies on that fallback.

## Upgrading the harness

A harness upgrade is a deliberate act:

1. install the new revision and record its tag and commit;
2. update `DSH_LOCKED_VERSION` and `DSH_LOCKED_COMMIT` in
   `src/agentos/constants.py` and `/usr/share/agentos/dsh-locked.json`;
3. re-run `scripts/probe-dsh-sdk.sh` and the adapter contract tests
   (`pytest tests/contract -q`);
4. record the result in `docs/dsh-integration.md`.

Never install `@latest` on a production system.
