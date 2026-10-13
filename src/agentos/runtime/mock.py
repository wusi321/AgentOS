"""MockRuntime: a keyless, network-free runtime used by tests and offline installs.

The adapter is deliberately deterministic so that unit, integration, and
packaging tests can drive the full Core path — task creation, policy checks,
audit writes, IPC, and UI updates — without a model credential. Scripted
scenarios inject timeouts, protocol violations, and crashes through the same
interface a real runtime uses, which is what makes the MockRuntime useful for
failure testing rather than only for happy-path tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, AsyncIterator

from ..errors import RuntimeUnavailable, ValidationError
from ..jsonutil import truncate_text
from .api import RuntimeCapabilities, RuntimeEvent, RuntimeHealth, RuntimeRequest

#: Tool names the mock can emit, mapped to a canned result body.
DEFAULT_MOCK_TOOLS: Mapping[str, str] = {
    "system.metrics.read": "cpu 12.4% mem 41.2% disk 63.0%",
    "system.processes.list": "3 processes above the requested threshold",
    "system.service.status": "unit is active (running)",
}


@dataclass(slots=True)
class MockScenario:
    """A scripted conversation the mock replays instead of a model.

    :param replies: Assistant replies emitted in order; the last one repeats.
    :param chunk_size: Characters per ``message.delta`` event.
    :param chunk_delay_seconds: Delay between deltas, for cancellation tests.
    :param tool_calls: Tool calls emitted before each reply, in order.
    :param fail_after_chunks: Raise a runtime error after this many deltas.
    :param malformed_after_chunks: Emit a non-JSON event after this many deltas.
    :param hang_after_chunks: Stop emitting without completing, to force a timeout.
    """

    replies: tuple[str, ...] = (
        "Mock runtime reply: the requested read-only system query was interpreted "
        "and no side-effecting action was proposed.",
    )
    chunk_size: int = 24
    chunk_delay_seconds: float = 0.0
    tool_calls: tuple[str, ...] = ()
    fail_after_chunks: int | None = None
    malformed_after_chunks: int | None = None
    hang_after_chunks: int | None = None

    def __post_init__(self) -> None:
        if not self.replies:
            raise ValidationError("MockScenario requires at least one reply")
        if self.chunk_size < 1:
            raise ValidationError("MockScenario.chunk_size must be at least 1")
        for field_name in ("fail_after_chunks", "malformed_after_chunks", "hang_after_chunks"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValidationError(f"MockScenario.{field_name} must not be negative")


class MockRuntime:
    """An :class:`~agentos.runtime.api.AgentRuntime` that fabricates nothing real."""

    def __init__(
        self,
        *,
        scenario: MockScenario | None = None,
        runtime_id: str = "mock",
        available: bool = True,
        start_failure: str | None = None,
    ) -> None:
        self._scenario = scenario or MockScenario()
        self._runtime_id = runtime_id
        self._available = available
        self._start_failure = start_failure
        self._started = False
        self._sessions: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self._turn_count = 0

    @property
    def runtime_id(self) -> str:
        """Return the stable adapter identity."""
        return self._runtime_id

    async def start(self) -> None:
        """Mark the mock as running, unless a scripted start failure is configured."""
        if self._start_failure is not None:
            raise RuntimeUnavailable(self._start_failure, details={"runtime_id": self._runtime_id})
        self._started = True

    async def create_session(self, *, workspace: str, session_id: str | None = None) -> str:
        """Register a session id and return it.

        The workspace is validated exactly as the DSH adapter validates it, so a
        caller cannot pass the contract against the mock and fail against a real
        runtime.
        """
        if not self._started:
            raise RuntimeUnavailable("MockRuntime.start() has not been called")
        if not workspace:
            raise ValidationError("workspace must be a non-empty absolute path")
        resolved = session_id or f"mock-{uuid.uuid4()}"
        self._sessions[resolved] = workspace
        self._sequences.setdefault(resolved, 0)
        return resolved

    def _emit(self, event_type: str, session_id: str, payload: Mapping[str, Any], native: str | None = None) -> RuntimeEvent:
        """Build the next sequenced event for ``session_id``."""
        self._sequences[session_id] = self._sequences.get(session_id, 0) + 1
        return RuntimeEvent(
            type=event_type,
            session_id=session_id,
            runtime_id=self._runtime_id,
            payload=dict(payload),
            sequence=self._sequences[session_id],
            native_event_type=native,
        )

    async def _send_message(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]:
        """Stream a scripted turn for ``request``."""
        if not self._started:
            raise RuntimeUnavailable("MockRuntime.start() has not been called")
        if not request.message.strip():
            raise ValidationError("prompt message must not be empty")
        if request.session_id not in self._sessions:
            await self.create_session(workspace=request.workspace, session_id=request.session_id)
        session_id = request.session_id
        yield self._emit("session.created", session_id, {"workspace": request.workspace, "runtime_id": self._runtime_id})

        reply = self._scenario.replies[min(self._turn_count, len(self._scenario.replies) - 1)]
        self._turn_count += 1

        emitted = 0
        for tool_name in self._scenario.tool_calls:
            call_id = f"call-{uuid.uuid4().hex[:12]}"
            yield self._emit(
                "tool.call.started",
                session_id,
                {"call_id": call_id, "name": tool_name, "arguments": {}},
                native="tool/call",
            )
            emitted += 1
            if self._maybe_fault(emitted) is not None:
                yield self._maybe_fault(emitted)  # type: ignore[misc]
                return
            yield self._emit(
                "tool.call.completed",
                session_id,
                {
                    "call_id": call_id,
                    "name": tool_name,
                    "is_error": False,
                    "summary": DEFAULT_MOCK_TOOLS.get(tool_name, "ok"),
                },
                native="tool/result",
            )

        for start in range(0, len(reply), self._scenario.chunk_size):
            chunk = reply[start : start + self._scenario.chunk_size]
            emitted += 1
            fault = self._maybe_fault(emitted)
            if fault is not None:
                if self._scenario.malformed_after_chunks is not None and fault.type == "runtime.error":
                    raise RuntimeUnavailable(
                        "mock runtime emitted a malformed protocol frame",
                        details={"session_id": session_id},
                    )
                yield fault
                return
            if self._scenario.chunk_delay_seconds:
                await asyncio.sleep(self._scenario.chunk_delay_seconds)
            yield self._emit("message.delta", session_id, {"text": chunk, "index": start})

        yield self._emit("message.completed", session_id, {"text": reply}, native="assistant/message")

    def _maybe_fault(self, emitted: int) -> RuntimeEvent | None:
        """Return the injected fault for this emission count, when one applies."""
        scenario = self._scenario
        if scenario.fail_after_chunks is not None and emitted > scenario.fail_after_chunks:
            return self._emit("runtime.error", "", {"reason": "injected failure", "retryable": False})
        if scenario.malformed_after_chunks is not None and emitted > scenario.malformed_after_chunks:
            return self._emit("runtime.error", "", {"reason": "malformed frame", "retryable": False})
        if scenario.hang_after_chunks is not None and emitted > scenario.hang_after_chunks:
            return None
        return None

    def send_message(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]:
        """Return the scripted event stream for ``request``.

        The injected-fault variants are produced here rather than inside the
        generator so that a scenario can raise a transport error without the
        generator swallowing it.
        """
        if self._scenario.hang_after_chunks is not None:
            return self._hanging_stream(request)
        return self._send_message(request)

    async def _hanging_stream(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]:
        """Emit a prefix and then never complete, to exercise timeouts."""
        limit = self._scenario.hang_after_chunks or 0
        emitted = 0
        async for event in self._send_message(request):
            yield event
            if event.type == "message.delta":
                emitted += 1
                if emitted >= limit:
                    break
        await asyncio.sleep(3600)

    async def cancel(self, session_id: str) -> None:
        """Accept cancellation; the mock has no long-running work to interrupt."""
        if session_id not in self._sessions:
            raise RuntimeUnavailable(
                f"unknown mock session {session_id!r}",
                details={"session_id": session_id},
            )

    async def health_check(self) -> RuntimeHealth:
        """Report configured availability without touching the network."""
        return RuntimeHealth(
            available=self._available,
            runtime_id=self._runtime_id,
            detail="mock runtime ready" if self._available else "mock runtime configured unavailable",
            version="mock-1",
            diagnostics={"scenario_replies": len(self._scenario.replies)},
        )

    async def capabilities(self) -> RuntimeCapabilities:
        """Report the mock's full capability set, including injected faults."""
        return RuntimeCapabilities(
            runtime_id=self._runtime_id,
            version="mock-1",
            streaming=True,
            tool_events=True,
            cancel=True,
            resume=True,
            session_persistence=False,
            approval_events=False,
            multi_session=True,
            notes=("no model is contacted; replies are scripted",),
        )

    async def close(self) -> None:
        """Drop session bookkeeping."""
        self._started = False
        self._sessions.clear()
        self._sequences.clear()


def load_scenario(document: str | Mapping[str, Any]) -> MockScenario:
    """Build a :class:`MockScenario` from JSON text or an already-decoded mapping.

    :param document: JSON object with any of the :class:`MockScenario` members.
    :raises ValidationError: When the document is not an object of the right types.
    """
    if isinstance(document, str):
        try:
            decoded = json.loads(document)
        except json.JSONDecodeError as error:
            raise ValidationError(f"mock scenario is not valid JSON: {error}") from error
    else:
        decoded = dict(document)
    if not isinstance(decoded, Mapping):
        raise ValidationError("mock scenario must be a JSON object")
    kwargs: dict[str, Any] = {}
    if "replies" in decoded:
        replies = decoded["replies"]
        if not isinstance(replies, list) or not all(isinstance(item, str) for item in replies):
            raise ValidationError("mock scenario 'replies' must be a list of strings")
        kwargs["replies"] = tuple(replies)
    if "tool_calls" in decoded:
        calls = decoded["tool_calls"]
        if not isinstance(calls, list) or not all(isinstance(item, str) for item in calls):
            raise ValidationError("mock scenario 'tool_calls' must be a list of strings")
        kwargs["tool_calls"] = tuple(calls)
    for key in ("chunk_size",):
        if key in decoded:
            if not isinstance(decoded[key], int) or isinstance(decoded[key], bool):
                raise ValidationError(f"mock scenario {key!r} must be an integer")
            kwargs[key] = decoded[key]
    if "chunk_delay_seconds" in decoded:
        delay = decoded["chunk_delay_seconds"]
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            raise ValidationError("mock scenario 'chunk_delay_seconds' must be a number")
        kwargs["chunk_delay_seconds"] = float(delay)
    for key in ("fail_after_chunks", "malformed_after_chunks", "hang_after_chunks"):
        if key in decoded and decoded[key] is not None:
            if not isinstance(decoded[key], int) or isinstance(decoded[key], bool):
                raise ValidationError(f"mock scenario {key!r} must be an integer or null")
            kwargs[key] = decoded[key]
    return MockScenario(**kwargs)


def default_scenario_description() -> str:
    """Return a short operator-facing description of the default scenario."""
    return truncate_text(" ".join(MockScenario().replies), limit=200)


__all__ = ["DEFAULT_MOCK_TOOLS", "MockRuntime", "MockScenario", "default_scenario_description", "load_scenario"]
