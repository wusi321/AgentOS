# DSH integration

This page records what AgentOS verified about DeepSeek Harness, how the adapter
is wired, and what the adapter deliberately does not do.

## Locked revision

| Fact | Value |
|---|---|
| Version | `0.2.1-alpha.2` |
| Git tag | `dsh-v0.2.1-alpha.2` |
| Commit | `d743267388641bc76f17c45ce8b4c231aed1d32c` |
| Repository | <https://github.com/deepseek-ai/deepseek-harness> |
| License | MIT |
| Node.js engines | `^22.19.0 \|\| >=24.0.0` |
| SDK profile | `dsh --profile sdk` |
| SDK server identity | `deepseek-harness-sdk-runtime`, reported version `0.0.1` |

These values are constants in `src/agentos/constants.py`
(`DSH_LOCKED_VERSION`, `DSH_LOCKED_COMMIT`, `DSH_NODE_ENGINES`) and are asserted
by the packaging metadata and `agentosctl doctor`.

Upstream is pre-stable. AgentOS pins the revision, checks the Node.js engine
requirement at runtime, and never installs `@latest`.

## Wire protocol

The SDK profile serves JSON-RPC 2.0 over newline-delimited stdio. AgentOS
implements the client side itself, in `src/agentos/runtime/dsh_transport.py`,
because the shipped Debian package must run on a bare system Python with no
third-party wheels available at runtime.

Framing rules the transport implements, taken from
`@deepseek-ai/dsh-sdk-protocol`:

* one JSON-RPC message per `\n`-terminated line;
* a frame with both `id` and `method` is a request;
* a frame with `id` alone is a response;
* a frame with `method` alone is a notification;
* a malformed line is ignored rather than treated as fatal;
* a request with no registered handler is answered with an error.

### Methods

Client to server:

| Method | Parameters | Result |
|---|---|---|
| `initialize` | `cwd`, `provider`, `model`, optional `reasoningEffort`, optional `maxTokens` | `serverInfo{name,version}` |
| `session/prompt` | `sessionId`, `contentBlocks` | `messageId` (durable enqueue receipt) |
| `session/wait` | `sessionId` | `{}` |
| `session/working-directory/get` | `sessionId` | `cwd` |
| `session/working-directory/set` | `sessionId`, `path` | `cwd` |
| `shutdown` | none | `{}` |

Server to client notifications:

| Method | Meaning |
|---|---|
| `session.event` | one committed session-log event, for every session in the runtime |
| `session.status` | whole-agent `running` / `idle` transition |
| `subagent.started` | an in-runtime child session was created |
| `subagent.finished` | an in-process child run ended |

The server never sends a server-to-client request. The transport nonetheless
answers one with a method-not-found error rather than leaving a peer waiting.

## Verified behaviour

`scripts/probe-dsh-sdk.sh` installs the exact locked revision from npm, checks the
local Node.js requirement, and runs `scripts/dsh_sdk_probe.py` against the real
CLI. Observed on Ubuntu 24.04, x86_64, Node.js `v22.22.2`:

| Observation | Result |
|---|---|
| `dsh --profile sdk` starts under a restricted environment allowlist | yes |
| `initialize` answers with `serverInfo.name = deepseek-harness-sdk-runtime` | yes |
| `initialize` rejects an unregistered provider route with a JSON-RPC error | yes — `no adapter registered for provider "…"` |
| An unknown method produces a JSON-RPC error frame | yes — observed `code = -32603` |
| `session/working-directory/get` answers | yes |
| `session.event` notifications arrive on stdio | yes — observed before any prompt |
| `shutdown` returns and the child exits with status 0 | yes |
| The packaged route names are `deepseek-official` / `deepseek-flash` | yes — from `--dump-config` |

### Correction to the upstream protocol documentation

`packages/sdk/protocol/README.md` states that a request with no registered
handler answers `-32601`. On the locked revision an unknown method was observed
to answer `-32603` instead. AgentOS does not depend on which code arrives: the
transport surfaces the wire `code` and maps the failure to
`RuntimeProtocolError` either way.

### Cancellation

The locked protocol has **no cancel method**. Upstream documents that "a client
abandons a turn by closing the runtime process".

`DshAdapter.capabilities()` therefore reports `cancel = False`, and
`DshAdapter.cancel()` raises `CapabilityUnsupported`. The task layer honours a
cancellation by stopping the runtime process
(`DshAdapter.interrupt_process()`), which abandons the in-flight turn; the
session remains durable inside DSH and is reopened by id on the next prompt.
This is a real limitation, not a simulation, and it is surfaced in
`agentosctl runtime probe`.

### Approvals

The SDK profile exposes no approval notification. AgentOS does not fabricate one:
`capabilities().approval_events` is `False`, and the only approval authority in
the system is the AgentOS policy engine. The adapter republishes `subagent.*`
notifications as `runtime.warning` events so that delegation is visible.

## Event mapping

The adapter is the only module that understands DSH's session vocabulary. It maps
DSH session events onto the AgentOS runtime event set:

| DSH session event | AgentOS runtime event | Notes |
|---|---|---|
| `assistant/message` | `message.completed` | visible text and reasoning are extracted from content blocks; `interrupted` and `usage` are preserved |
| `assistant/attempt`, `developer/message` | `message.delta` | DSH packs the model stream into delta runs; each run is republished once and labelled `batched_run`, so AgentOS never claims per-token granularity it does not have |
| `tool/call` | `tool.call.started` | the raw model argument string is decoded as untrusted JSON, never echoed as a shell string |
| `tool/result` | `tool.call.completed` | error name and code are preserved outside the model-facing text |
| `session/status` transition to `idle` | `message.completed` with `synthetic: true` | emitted only when no assistant message was committed for that turn |
| `subagent.started`, `subagent.finished` | `runtime.warning` | the child session id is carried in the payload |
| `turn/start`, `turn/end`, `step/end` | not republished | they carry no user-visible state AgentOS needs |

An unrecognized DSH event type is dropped with the native type preserved on the
event it did produce; the adapter never invents an AgentOS event for a DSH event
it does not understand.

## Launch contract

The adapter spawns, without a shell:

```
<node> <dsh-cli-entry> --profile sdk
```

with a restricted environment built by `minimal_child_environment()`: `PATH`,
`LANG`, `LC_ALL`, `TERM`, `TZ`, `HOME` passed through, plus `DSH_HOME` and
`NODE_ENV=production`. The parent environment is never inherited wholesale, so
unrelated deployment variables and credentials cannot leak into the runtime.

`DSH_HOME` is always explicit. DSH itself never falls back to `~/.dsh`
implicitly, and AgentOS never relies on that fallback.

Model credentials reach the child only through the process environment, which the
systemd unit reads from `/etc/agentos/credentials.env` (mode `0600`,
root-owned). A credential never appears in a command line, a configuration file,
the database, an event payload, a log line, or the audit trail.

## Failure behaviour

| Condition | Adapter response |
|---|---|
| Node.js missing or below `^22.19.0 \|\| >=24.0.0` | `health_check()` reports unavailable with the exact requirement; `start()` raises `RuntimeUnavailable` |
| CLI entry missing | `health_check()` reports unavailable naming the path |
| No provider/model configured | `RuntimeUnavailable` naming `runtime.provider` and `runtime.model` |
| Unknown provider route | the runtime's own JSON-RPC error is surfaced verbatim as `RuntimeUnavailable` |
| Credential missing or rejected by the provider | the runtime's error is surfaced; the task fails with that message and no side effect occurs |
| Runtime crash mid-turn | `RuntimeUnavailable` with the exit code and a bounded stderr tail |
| Turn exceeds its budget | `RuntimeUnavailable` naming the timeout; the task is failed, not silently retried |
| Malformed protocol line | ignored by the transport, as upstream specifies |

Every one of these paths leaves the desktop and the read-only system tools
working, because they do not depend on the runtime at all.

## Not done, deliberately

* No vendoring of the upstream Python SDK. The shipped package must not require
  `pydantic` or any other wheel on a bare Ubuntu system Python.
* No HTTP or Web UI path. The SDK profile is a stdio contract; the Web profile is
  not used, and nothing binds a network address.
* No automatic DSH upgrade. Upgrading the revision requires re-running the probe
  and the adapter contract tests, and updating the constants.
* No attempt to disable DSH's own tools from the adapter. AgentOS policy is
  enforced on the AgentOS side, where the authority lives; the runtime's tools
  run in the runtime's own account and sandbox, whose configuration is a
  deployment decision recorded in `docs/threat-model.md`.
