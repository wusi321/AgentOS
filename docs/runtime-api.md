# Agent Runtime API

AgentOS Core never imports a runtime's internals. It talks to the `AgentRuntime`
protocol declared in `src/agentos/runtime/api.py`, so a second runtime can be
added without touching task, policy, or audit code. DSH is the first adapter; the
mock runtime is the second, and it exists so the whole system can be developed and
tested without a model credential.

## The protocol

```python
@runtime_checkable
class AgentRuntime(Protocol):
    @property
    def runtime_id(self) -> str: ...

    async def start(self) -> None: ...
    async def create_session(self, *, workspace: str, session_id: str | None = None) -> str: ...
    def send_message(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]: ...
    async def cancel(self, session_id: str) -> None: ...
    async def health_check(self) -> RuntimeHealth: ...
    async def capabilities(self) -> RuntimeCapabilities: ...
    async def close(self) -> None: ...
```

`send_message` is deliberately a **synchronous** function returning an async
iterator. That shape means an adapter cannot do work before the caller starts
consuming, so a probe or a policy check can be interleaved with the stream without
the adapter having already started something.

## Capabilities

An adapter declares what it supports, and every flag defaults to `False`:

| Flag | Meaning |
|---|---|
| `streaming` | incremental assistant text arrives as `message.delta` |
| `tool_events` | tool start and finish events are emitted |
| `cancel` | a running turn can be interrupted without losing the session |
| `resume` | a persisted session can be reopened by id |
| `session_persistence` | the runtime durably stores sessions itself |
| `approval_events` | the runtime surfaces its own approval requirements |
| `multi_session` | one runtime process serves more than one session |
| `notes` | honest limitations, printed by `agentosctl runtime probe` |

The rule is that an adapter must **not fabricate** a capability. If the wire
protocol has no cancel method, `capabilities().cancel` is `False` and `cancel()`
raises `CapabilityUnsupported`. That is what makes the capability set trustworthy
enough for the rest of the system to make decisions with.

The DSH adapter's real answer, verified against the locked revision:

```sh
$ agentosctl runtime probe
runtime      dsh
available    True
cancel               False        # the SDK profile has no cancel method
approval_events      False        # no approval notification exists
streaming            True
tool_events          True
session_persistence  True
resume               True
note         cancellation is unsupported by the SDK profile; AgentOS interrupts
             a turn by stopping the runtime process, after which the session is
             reopened from persisted history
```

## Event vocabulary

Normalized events, shared by every adapter:

| Event | Payload keys | Produced when |
|---|---|---|
| `session.created` | `workspace`, `runtime_version` | the turn opens |
| `message.delta` | `text`, `channel` (`text`/`reasoning`), `batched_run`, `index` | incremental assistant output |
| `message.completed` | `text`, `reasoning`, `usage`, `interrupted`, `synthetic` | the turn's assistant message is committed |
| `tool.call.started` | `call_id`, `name`, `arguments` | the model requested a tool |
| `tool.call.completed` | `call_id`, `is_error`, `summary`, `error_name`, `error_code` | the tool answered |
| `approval.required` | `approval_id`, `operation`, `target` | the runtime itself needs a decision |
| `runtime.warning` | `reason`, plus adapter detail | a non-fatal anomaly |
| `runtime.error` | `reason`, `retryable` | the turn failed |
| `runtime.exited` | `reason`, `exit_code` | the runtime process ended |

Every event carries `session_id`, `runtime_id`, a monotonic per-session
`sequence`, and `native_event_type` when the adapter translated one. The
vocabulary is closed: an adapter that emits a type outside it raises at
construction, which is how a typo becomes a test failure instead of a silent gap.

### Why `message.delta` is not per-token

DSH compacts a model stream into *runs* of delta chunks. An adapter that split a
run into individual characters would be inventing granularity the wire does not
carry, and one that reported the run as a single token would be misreporting it.
AgentOS republishes each run once with `batched_run` set to the number of chunks
it contained, so a consumer knows exactly what it received.

## Adapters in this release

### `MockRuntime`

* deterministic scripted replies, so tests assert on exact text;
* injectable faults: a runtime error after N events, a malformed frame, or a hang
  that never completes, so timeout and cancellation paths are testable;
* no network, no credential, and no filesystem access;
* `session_persistence` is `False` and `approval_events` is `False`, because
  claiming otherwise would let a caller pass a contract test it should fail.

Selected with `runtime_id = "mock"`, which is the shipped default.

### `DshAdapter`

Drives `dsh --profile sdk` over JSON-RPC stdio. See
`docs/dsh-integration.md` for the wire contract, the verified behaviour, and the
honest limitations.

## Contract tests

Every adapter is asserted against the same test set, parametrised over the
registered factories in `tests/contract/test_runtime_contract.py`:

* the protocol surface exists;
* a session can be created only after `start`, and an empty workspace is refused;
* a session id is stable across reopens;
* an empty prompt is a caller error (`ValidationError`), not a runtime error;
* an event stream opens with `session.created`, ends with `message.completed`,
  carries monotonic sequences, and matches the requested session;
* the mock's deltas reassemble into its completed message;
* tool start and finish events pair by `call_id`;
* health always answers with a report rather than raising;
* `close()` is idempotent;
* `cancel()` either works or the capability set says it cannot.

The DSH adapter joins the set automatically when the packaged runtime is present,
so a machine with the runtime installed tests both from one command:

```sh
python3 -m pytest tests/contract -q
```

## Adding a runtime

1. Implement the protocol in `src/agentos/runtime/<name>.py`.
2. Report capabilities and health honestly; do not claim cancellation or approval
   events the transport cannot deliver.
3. Map the runtime's native events onto the vocabulary above, and preserve the
   native name in `native_event_type`.
4. Register a factory in `tests/contract/test_runtime_contract.py` so it is
   asserted against the shared contract.
5. Add it to `RUNTIME_IDS` and to `create_runtime` in
   `src/agentos/runtime/factory.py`.
6. Record the decision and the version-pinning policy in an ADR, and document the
   runtime in this page.

A runtime that needs a process, a socket, or a credential must not be started
during import: `start()` is the only place that spawns anything.
