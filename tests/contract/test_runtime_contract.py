"""Runtime contract tests shared by every AgentRuntime adapter.

Every adapter must satisfy the same contract, so this module is written once.
Adapters are registered as factories, and each test receives a fresh instance.
The DSH adapter is registered only when its packaged runtime is present, so a
machine without it still runs the whole suite.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator, Awaitable, Callable, Iterator

import pytest

from agentos.errors import CapabilityUnsupported, RuntimeUnavailable, ValidationError
from agentos.runtime import (
    RUNTIME_EVENT_TYPES,
    AgentRuntime,
    MockRuntime,
    MockScenario,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeHealth,
    RuntimeRequest,
)


def mock_factory() -> AgentRuntime:
    """Build a mock runtime with a short, deterministic script."""
    return MockRuntime(scenario=MockScenario(replies=("contract reply",), chunk_size=4))


#: Adapter factories the contract is asserted against.
ADAPTER_FACTORIES: dict[str, Callable[[], AgentRuntime]] = {"mock": mock_factory}


def _dsh_runtime_available() -> bool:
    """Return whether the packaged DSH runtime is installed on this machine."""
    from agentos.constants import DSH_CLI_ENTRY
    from agentos.runtime.dsh import DshSettings, resolve_node_binary

    settings = DshSettings()
    if resolve_node_binary(settings) is None:
        return False
    return os.path.isfile(DSH_CLI_ENTRY)


if _dsh_runtime_available():  # pragma: no cover - depends on the host package set

    def dsh_factory() -> AgentRuntime:
        """Build the DSH adapter with the locked revision's default route."""
        from agentos.runtime.dsh import DshAdapter, DshSettings

        return DshAdapter(DshSettings(provider="deepseek-official", model="deepseek-flash"))

    ADAPTER_FACTORIES["dsh"] = dsh_factory


@pytest.fixture(params=sorted(ADAPTER_FACTORIES))
async def adapter(request: pytest.FixtureRequest) -> AsyncIterator[AgentRuntime]:
    """Yield a fresh adapter for each registered factory."""
    instance = ADAPTER_FACTORIES[request.param]()
    try:
        yield instance
    finally:
        await instance.close()


async def collect(stream: AsyncIterator[RuntimeEvent]) -> list[RuntimeEvent]:
    """Drain an async iterator into a list."""
    events: list[RuntimeEvent] = []
    async for event in stream:
        events.append(event)
    return events


# --- structure ----------------------------------------------------------------


def test_adapter_exposes_every_protocol_member() -> None:
    """A structural check that a runtime implements the whole protocol."""
    runtime = mock_factory()
    for member in (
        "runtime_id",
        "start",
        "create_session",
        "send_message",
        "cancel",
        "health_check",
        "capabilities",
        "close",
    ):
        assert hasattr(runtime, member), member


def test_runtime_id_is_stable() -> None:
    """The identity does not change between calls."""
    runtime = mock_factory()
    assert runtime.runtime_id == runtime.runtime_id == "mock"


def test_event_vocabulary_is_closed() -> None:
    """An adapter cannot emit an event type outside the AgentOS vocabulary."""
    with pytest.raises(CapabilityUnsupported):
        RuntimeEvent(type="invented.event", session_id="s", runtime_id="mock")


def test_event_severity_mapping() -> None:
    """Error and warning events report the matching severity."""
    assert RuntimeEvent(type="runtime.error", session_id="s", runtime_id="mock").severity.value == "error"
    assert RuntimeEvent(type="runtime.warning", session_id="s", runtime_id="mock").severity.value == "warning"
    assert RuntimeEvent(type="message.delta", session_id="s", runtime_id="mock").severity.value == "info"


def test_capabilities_are_json_serializable() -> None:
    """Capabilities cross the IPC boundary, so they must serialize."""
    document = RuntimeCapabilities(runtime_id="mock", streaming=True, notes=("a note",)).to_dict()
    assert json.loads(json.dumps(document))["notes"] == ["a note"]


def test_capability_require_raises_for_unsupported() -> None:
    """``require`` is the honest gate an adapter uses instead of faking a feature."""
    with pytest.raises(CapabilityUnsupported):
        RuntimeCapabilities(runtime_id="mock").require("cancel")


def test_health_is_json_serializable() -> None:
    """Health reports cross the IPC boundary too."""
    document = RuntimeHealth(available=True, runtime_id="mock", detail="ready", diagnostics={"a": 1}).to_dict()
    assert json.loads(json.dumps(document))["available"] is True


def test_capabilities_do_not_overclaim_for_the_mock() -> None:
    """The mock declares no runtime-side approvals, because it has none."""
    runtime = MockRuntime()
    capabilities = _sync(runtime.capabilities())
    assert capabilities.approval_events is False
    assert capabilities.session_persistence is False


def test_capabilities_do_not_overclaim_for_dsh() -> None:
    """The DSH adapter must not claim cancellation or approval events."""
    from agentos.runtime.dsh import DshAdapter, DshSettings

    adapter = DshAdapter(DshSettings(provider="deepseek-official", model="deepseek-flash"))
    capabilities = _sync(adapter.capabilities())
    assert capabilities.cancel is False
    assert capabilities.approval_events is False
    assert capabilities.streaming is True
    assert capabilities.tool_events is True
    assert capabilities.session_persistence is True
    assert any("cancel" in note for note in capabilities.notes)


def test_dsh_settings_reject_a_non_sdk_profile() -> None:
    """AgentOS drives DSH through the SDK profile only."""
    from agentos.runtime.dsh import DshSettings

    with pytest.raises(ValidationError):
        DshSettings(provider="p", model="m", profile="web").validate()


def test_dsh_settings_reject_unsafe_route_names() -> None:
    """A route name is validated before it reaches argv."""
    from agentos.runtime.dsh import DshSettings

    with pytest.raises(ValidationError):
        DshSettings(provider="deepseek; rm -rf /", model="m").validate()
    with pytest.raises(ValidationError):
        DshSettings(provider="p", model="m", max_tokens=0).validate()


def test_node_version_range_is_enforced() -> None:
    """The locked engine requirement is checked, not assumed."""
    from agentos.runtime.dsh import node_version_satisfies, parse_node_version

    assert node_version_satisfies(parse_node_version("v22.19.0")) is True
    assert node_version_satisfies(parse_node_version("v22.22.2")) is True
    assert node_version_satisfies(parse_node_version("v24.1.0")) is True
    assert node_version_satisfies(parse_node_version("v22.18.9")) is False
    assert node_version_satisfies(parse_node_version("v20.11.0")) is False
    assert node_version_satisfies(parse_node_version("v18.19.1")) is False


def test_child_environment_is_allowlisted() -> None:
    """The runtime child never inherits the parent environment wholesale."""
    from agentos.runtime.dsh_transport import minimal_child_environment

    os.environ["AGENTOS_TEST_LEAK"] = "must-not-pass"
    try:
        environment = minimal_child_environment(additions={"DSH_HOME": "/tmp/dsh-home"})
    finally:
        del os.environ["AGENTOS_TEST_LEAK"]
    assert environment["DSH_HOME"] == "/tmp/dsh-home"
    assert "AGENTOS_TEST_LEAK" not in environment
    assert environment["PATH"].startswith("/usr/local/sbin:")


# --- lifecycle ----------------------------------------------------------------


async def test_start_then_create_session(adapter: AgentRuntime) -> None:
    """A session can be created after start."""
    await adapter.start()
    session_id = await adapter.create_session(workspace="/tmp")
    assert isinstance(session_id, str) and session_id


async def test_mock_refuses_a_session_before_start() -> None:
    """The mock's strict ordering is the behaviour Core relies on."""
    runtime = mock_factory()
    with pytest.raises(RuntimeUnavailable):
        await runtime.create_session(workspace="/tmp")


async def test_create_session_rejects_an_empty_workspace(adapter: AgentRuntime) -> None:
    """A session without a workspace has nowhere to run."""
    await adapter.start()
    with pytest.raises(ValidationError):
        await adapter.create_session(workspace="")


async def test_create_session_is_idempotent_for_a_known_id(adapter: AgentRuntime) -> None:
    """Reopening a session by id returns the same id."""
    await adapter.start()
    first = await adapter.create_session(workspace="/tmp", session_id="fixed-id")
    second = await adapter.create_session(workspace="/tmp", session_id="fixed-id")
    assert first == second == "fixed-id"


async def test_send_message_rejects_an_empty_prompt(adapter: AgentRuntime) -> None:
    """An empty prompt is a caller error, not a runtime error."""
    await adapter.start()
    session_id = await adapter.create_session(workspace="/tmp")
    with pytest.raises(ValidationError):
        await collect(adapter.send_message(RuntimeRequest(session_id=session_id, message="   ", workspace="/tmp")))


async def test_event_stream_is_sequenced_and_completes(adapter: AgentRuntime) -> None:
    """Every stream opens with a created event and closes with a completed one."""
    await adapter.start()
    session_id = await adapter.create_session(workspace="/tmp")
    events = await collect(
        adapter.send_message(RuntimeRequest(session_id=session_id, message="hello", workspace="/tmp"))
    )
    assert events, "the adapter produced no events"
    assert events[0].type == "session.created"
    assert events[-1].type == "message.completed"
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences), "event sequences must be monotonic"
    assert all(event.session_id == session_id for event in events)
    assert all(event.runtime_id == adapter.runtime_id for event in events)
    assert all(event.type in RUNTIME_EVENT_TYPES for event in events)


async def test_mock_streamed_text_reassembles(adapter: AgentRuntime) -> None:
    """The deltas and the final message describe the same answer for the mock.

    Only the mock is asserted here: a real runtime may stream nothing at all for
    a turn, which is a legitimate outcome the adapter must not paper over.
    """
    if adapter.runtime_id != "mock":
        pytest.skip("delta reassembly is only guaranteed by the scripted mock")
    await adapter.start()
    session_id = await adapter.create_session(workspace="/tmp")
    events = await collect(
        adapter.send_message(RuntimeRequest(session_id=session_id, message="hello", workspace="/tmp"))
    )
    deltas = "".join(str(event.payload.get("text", "")) for event in events if event.type == "message.delta")
    completed = next(event for event in events if event.type == "message.completed")
    assert deltas == completed.payload.get("text")


async def test_mock_tool_events_are_paired() -> None:
    """Every started tool call has a matching completion when tools are scripted."""
    runtime = MockRuntime(
        scenario=MockScenario(replies=("done",), chunk_size=8, tool_calls=("system.metrics.read",))
    )
    try:
        await runtime.start()
        session_id = await runtime.create_session(workspace="/tmp")
        events = await collect(
            runtime.send_message(RuntimeRequest(session_id=session_id, message="check", workspace="/tmp"))
        )
        started = [event for event in events if event.type == "tool.call.started"]
        completed = [event for event in events if event.type == "tool.call.completed"]
        assert len(started) == len(completed) == 1
        assert started[0].payload["call_id"] == completed[0].payload["call_id"]
    finally:
        await runtime.close()


async def test_mock_injected_failure_raises() -> None:
    """A scripted transport failure surfaces as a typed runtime error."""
    runtime = MockRuntime(scenario=MockScenario(replies=("x" * 100,), chunk_size=10, fail_after_chunks=2))
    try:
        await runtime.start()
        session_id = await runtime.create_session(workspace="/tmp")
        events = await collect(
            runtime.send_message(RuntimeRequest(session_id=session_id, message="go", workspace="/tmp"))
        )
        assert any(event.type == "runtime.error" for event in events)
    finally:
        await runtime.close()


async def test_mock_scripted_hang_can_be_timed_out() -> None:
    """A stalled runtime must be interruptible by the caller's own deadline."""
    runtime = MockRuntime(
        scenario=MockScenario(replies=("x" * 400,), chunk_size=10, chunk_delay_seconds=0.01, hang_after_chunks=2)
    )
    try:
        await runtime.start()
        session_id = await runtime.create_session(workspace="/tmp")

        async def consume() -> int:
            count = 0
            async for _event in runtime.send_message(
                RuntimeRequest(session_id=session_id, message="go", workspace="/tmp")
            ):
                count += 1
            return count

        with pytest.raises(asyncio.TimeoutError):
            async with asyncio.timeout(1.0):
                await consume()
    finally:
        await runtime.close()


async def test_health_check_does_not_raise(adapter: AgentRuntime) -> None:
    """Health always answers with a report; unavailability is data."""
    health = await adapter.health_check()
    assert health.runtime_id == adapter.runtime_id
    assert isinstance(health.available, bool)
    assert health.detail


async def test_close_is_idempotent(adapter: AgentRuntime) -> None:
    """Closing twice is not an error."""
    await adapter.start()
    await adapter.close()
    await adapter.close()


async def test_cancel_is_honest_about_capability(adapter: AgentRuntime) -> None:
    """Cancellation either works or reports the missing capability."""
    await adapter.start()
    session_id = await adapter.create_session(workspace="/tmp")
    try:
        await adapter.cancel(session_id)
    except CapabilityUnsupported:
        capabilities = await adapter.capabilities()
        assert capabilities.cancel is False


async def test_cancel_of_an_unknown_session_is_refused(adapter: AgentRuntime) -> None:
    """Cancelling an unknown session is an error, not a silent no-op."""
    await adapter.start()
    with pytest.raises((RuntimeUnavailable, CapabilityUnsupported)):
        await adapter.cancel("no-such-session")


def _sync(awaitable: Awaitable[object]) -> object:
    """Resolve an awaitable on a private event loop, for non-async tests."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)  # type: ignore[arg-type]
    finally:
        loop.close()
