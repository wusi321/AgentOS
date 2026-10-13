# ADR-004: An `AgentRuntime` protocol, with DSH driven over its SDK JSON-RPC stdio profile

**Status:** accepted
**Date:** 2026-10-13

## Context

AgentOS needs to run a model-driven agent. The first runtime is DeepSeek Harness.
The design requires that Core not depend on one runtime's private interfaces, so
that a second runtime can be added later without touching task, policy, or audit
code. DSH's SDK surface had to be verified rather than assumed, because upstream is
pre-stable and its documentation is not the same as its behaviour.

## Options considered

1. **Call the DSH SDK directly from Core.** Fewest layers.
2. **Shell out to `dsh --profile headless "job"` per task.** Simple, no protocol.
3. **Define an `AgentRuntime` protocol and implement a DSH adapter over the SDK
   JSON-RPC stdio profile.**
4. **Vendor the upstream Python SDK.** Least code to write.

## Decision

Option 3, with a `MockRuntime` as the second adapter and the contract tests shared
between them.

## Reasoning

* Option 1 would make DSH's plugin objects, Cordis internals, or CLI flags part of
  AgentOS's own API surface. Upstream states that its interfaces may change
  incompatibly; a boundary is what contains that.
* Option 2 gives no incremental output, no tool-call events, and no session
  identity, so the desktop could not stream an answer and the policy engine could
  not see a tool call before it happens. It is also one process per prompt, which
  makes a conversation's session state awkward.
* Option 3 was verified to work. `scripts/probe-dsh-sdk.sh` installs the locked
  revision and probes the real wire protocol: the profile starts, `initialize`
  answers with the wire-stable identity `deepseek-harness-sdk-runtime`, an unknown
  method produces a JSON-RPC error frame, `session/working-directory/get` answers,
  `session.event` notifications arrive on stdio, and `shutdown` returns with exit
  status 0. The observations are recorded in `docs/dsh-integration.md`.
* Option 4 was rejected because the shipped Debian package must run on a bare
  Ubuntu system Python. The upstream SDK depends on Pydantic, which is not
  guaranteed present, and adding a wheel dependency to a distribution package for
  a few hundred lines of framing code is the wrong trade.

## The protocol

```python
class AgentRuntime(Protocol):
    runtime_id: str
    async def start(self) -> None: ...
    async def create_session(self, *, workspace: str, session_id: str | None = None) -> str: ...
    def send_message(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]: ...
    async def cancel(self, session_id: str) -> None: ...
    async def health_check(self) -> RuntimeHealth: ...
    async def capabilities(self) -> RuntimeCapabilities: ...
    async def close(self) -> None: ...
```

`send_message` is a **synchronous** function returning an async iterator, so an
adapter cannot start work before the caller begins consuming.

## Honesty about capabilities

The protocol's most important rule is that an adapter must not fabricate a
capability. The locked DSH protocol has **no cancel method** and sends **no
approval notification**, so the adapter reports `cancel = False` and
`approval_events = False`, and `cancel()` raises `CapabilityUnsupported` rather
than pretending. Cancellation is honoured at the task layer by stopping the runtime
process, which is what upstream documents as the way to abandon a turn; the task is
then marked cancelled and the durable session can be reopened.

This matters because the alternative — a `cancel()` that returns successfully and
does nothing — would make every caller's cancellation test pass while the turn kept
running.

## Verified limitation, recorded rather than hidden

Upstream's SDK protocol page states that a request with no registered handler
answers `-32601`. On the locked revision an unknown method was observed to answer
`-32603`. The transport surfaces whichever code arrives and maps the failure to
`RuntimeProtocolError` either way, so the discrepancy is recorded in
`docs/dsh-integration.md` and does not affect behaviour.

## Security impact

**Positive.** Core's authority does not extend into the runtime, and the runtime
cannot reach the system except through what Core permits. The adapter runs the
child with an allowlisted environment, so the service's other environment variables
and its credential file cannot leak into a model-driven process.

**Negative.** The adapter is a real protocol implementation and therefore a place
where a malformed frame could cause a fault. It is written to ignore a malformed
line (as upstream specifies), to bound every frame, and to fail every pending
request with a typed error when the child exits.

## Maintenance cost

A protocol adapter is the highest-maintenance component in the system, because it
is where an external, pre-stable interface meets our code. The mitigations are the
pinned revision, a machine-checkable probe, a shared contract test suite, and one
module boundary (`agentos/runtime/`) that nothing else in the system imports
through.

## Review trigger

Revisit if DSH adds a cancel method or an approval notification, if a second
runtime is added (which is the case this ADR exists to enable), or if the adapter
grows behaviour that the protocol does not describe. If the SDK profile turns out
to be unstable in practice, the documented fallback is a limited subprocess
adapter over the headless CLI — and that would need its own ADR, not a quiet
change to this one.
