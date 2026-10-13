"""AgentOS AgentRuntime protocol: the only interface Core uses to drive a model.

Core never imports DSH objects, Cordis internals, or CLI private flags. It
depends on the protocol declared here, so a second runtime can be added without
touching task, policy, or audit code.

A runtime declares what it supports through :class:`RuntimeCapabilities`. An
adapter that cannot provide a capability must report it as unsupported and raise
:class:`~agentos.errors.CapabilityUnsupported`; it must never fabricate events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from ..errors import CapabilityUnsupported
from ..events import Severity

#: Normalized runtime event types every adapter maps onto.
RUNTIME_EVENT_TYPES: tuple[str, ...] = (
    "session.created",
    "message.delta",
    "message.completed",
    "tool.call.started",
    "tool.call.completed",
    "approval.required",
    "runtime.warning",
    "runtime.error",
    "runtime.exited",
)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One normalized event streamed out of a runtime.

    :param type: Member of :data:`RUNTIME_EVENT_TYPES`.
    :param session_id: Runtime-side session the event belongs to.
    :param runtime_id: Adapter identity, for example ``dsh``.
    :param payload: Normalized body; key names are owned by this module.
    :param sequence: Monotonic per-session counter, useful for ordering and gaps.
    :param native_event_type: The runtime's own event name, kept for forensics.
    """

    type: str
    session_id: str
    runtime_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0
    native_event_type: str | None = None

    def __post_init__(self) -> None:
        if self.type not in RUNTIME_EVENT_TYPES:
            raise CapabilityUnsupported(
                f"runtime event type {self.type!r} is not part of the AgentOS vocabulary",
                details={"known": list(RUNTIME_EVENT_TYPES)},
            )

    @property
    def severity(self) -> Severity:
        """Return the severity implied by this event's type."""
        if self.type == "runtime.error":
            return Severity.ERROR
        if self.type == "runtime.warning":
            return Severity.WARNING
        if self.type in {"approval.required", "runtime.exited"}:
            return Severity.NOTICE
        return Severity.INFO


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    """Result of a runtime health probe.

    :param available: Whether the runtime can currently serve a request.
    :param runtime_id: Adapter identity.
    :param detail: Human-readable explanation, safe to show in the UI.
    :param version: Runtime version when it reported one.
    :param node_version: Node.js version when the runtime needs one.
    :param diagnostics: Extra machine-readable facts for ``agentosctl doctor``.
    """

    available: bool
    runtime_id: str
    detail: str = ""
    version: str | None = None
    node_version: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this health result."""
        return {
            "available": self.available,
            "runtime_id": self.runtime_id,
            "detail": self.detail,
            "version": self.version,
            "node_version": self.node_version,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """What an adapter honestly supports.

    Every flag defaults to ``False``; an adapter opts in only after a reproducible
    test proves the behavior. The CLI reports this set through ``agentosctl doctor``.

    :param runtime_id: Adapter identity.
    :param version: Adapter version string.
    :param streaming: Incremental assistant text arrives as ``message.delta``.
    :param tool_events: Tool call start/finish events are emitted.
    :param cancel: A running turn can be interrupted without losing the session.
    :param resume: A persisted session can be reopened by id.
    :param session_persistence: The runtime durably stores sessions itself.
    :param approval_events: The runtime surfaces its own approval requirements.
    :param multi_session: One runtime process serves more than one session.
    :param notes: Honest limitations, surfaced by the doctor command.
    """

    runtime_id: str
    version: str = ""
    streaming: bool = False
    tool_events: bool = False
    cancel: bool = False
    resume: bool = False
    session_persistence: bool = False
    approval_events: bool = False
    multi_session: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of these capabilities."""
        return {
            "runtime_id": self.runtime_id,
            "version": self.version,
            "streaming": self.streaming,
            "tool_events": self.tool_events,
            "cancel": self.cancel,
            "resume": self.resume,
            "session_persistence": self.session_persistence,
            "approval_events": self.approval_events,
            "multi_session": self.multi_session,
            "notes": list(self.notes),
        }

    def require(self, capability: str) -> None:
        """Raise :class:`CapabilityUnsupported` unless ``capability`` is supported."""
        if not getattr(self, capability, False):
            raise CapabilityUnsupported(
                f"runtime {self.runtime_id!r} does not support {capability!r}",
                details={"runtime_id": self.runtime_id, "capability": capability},
            )


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """One prompt handed to a runtime.

    :param session_id: Session to send on; the adapter creates it when unknown.
    :param message: User instruction, already policy-checked by Core.
    :param workspace: Absolute working directory for the session.
    :param provider: Model provider route, when the deployment pins one.
    :param model: Model name, when the deployment pins one.
    :param reasoning_effort: Optional adapter-owned reasoning effort id.
    :param max_tokens: Optional positive output-token cap.
    :param task_id: Owning AgentOS task, propagated into events for correlation.
    """

    session_id: str
    message: str
    workspace: str
    provider: str = ""
    model: str = ""
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    task_id: str | None = None


@runtime_checkable
class AgentRuntime(Protocol):
    """The contract Core uses to run an agent.

    Implementations are async. Adapters that wrap thread-based or
    process-based transports convert at their own boundary.
    """

    @property
    def runtime_id(self) -> str:
        """Return the stable adapter identity."""
        ...

    async def start(self) -> None:
        """Prepare the runtime so that :meth:`send_message` can be called."""
        ...

    async def create_session(self, *, workspace: str, session_id: str | None = None) -> str:
        """Create or reattach a session and return its id.

        :param workspace: Absolute working directory recorded on the session.
        :param session_id: Existing session to reopen; omitted for a new one.
        """
        ...

    def send_message(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]:
        """Send one prompt and stream normalized events until the turn settles.

        :param request: The prompt and its routing facts.
        :returns: An async iterator of :class:`RuntimeEvent`.
        """
        ...

    async def cancel(self, session_id: str) -> None:
        """Interrupt the running turn on ``session_id``.

        :raises CapabilityUnsupported: When :attr:`RuntimeCapabilities.cancel` is false.
        """
        ...

    async def health_check(self) -> RuntimeHealth:
        """Report whether the runtime can serve a request right now."""
        ...

    async def capabilities(self) -> RuntimeCapabilities:
        """Report exactly what this adapter supports."""
        ...

    async def close(self) -> None:
        """Release processes, sockets, and temporary state."""
        ...


__all__ = [
    "RUNTIME_EVENT_TYPES",
    "AgentRuntime",
    "RuntimeCapabilities",
    "RuntimeEvent",
    "RuntimeHealth",
    "RuntimeRequest",
]
