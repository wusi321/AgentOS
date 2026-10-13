"""Typed AgentOS failures.

Every layer raises ``AgentOSError`` subclasses so that the CLI, the IPC server,
and the policy engine can turn a failure into a stable machine code without
inspecting message text.
"""

from __future__ import annotations

from typing import Any, Final


class AgentOSError(Exception):
    """Base class for all AgentOS failures.

    :param message: Operator-facing explanation; never contains credentials.
    :param code: Stable machine-readable identifier, reported over IPC.
    :param details: Optional JSON-serializable context (identifiers, paths).
    """

    code: Final[str] = "agentos.error"
    exit_status: Final[int] = 1

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this failure."""
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigurationError(AgentOSError):
    """Configuration is missing, malformed, or internally inconsistent."""

    code = "agentos.configuration_error"


class ValidationError(AgentOSError):
    """An externally supplied value failed schema validation."""

    code = "agentos.validation_error"


class PolicyDenied(AgentOSError):
    """The policy engine refused an operation.

    ``denied`` is the normal outcome for an unauthorized request; it is a
    security-relevant event and is always audited.
    """

    code = "agentos.policy_denied"


class ApprovalRequired(AgentOSError):
    """The operation needs an explicit human approval before it may run."""

    code = "agentos.approval_required"

    def __init__(
        self,
        message: str,
        *,
        approval_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        if approval_id is not None:
            merged["approval_id"] = approval_id
        super().__init__(message, details=merged)
        self.approval_id = approval_id


class ApprovalInvalid(AgentOSError):
    """An approval is missing, expired, already consumed, or bound elsewhere."""

    code = "agentos.approval_invalid"


class TaskStateError(AgentOSError):
    """An illegal task state transition was requested."""

    code = "agentos.task_state_error"


class RuntimeUnavailable(AgentOSError):
    """The selected Agent Runtime cannot be started or has exited."""

    code = "agentos.runtime_unavailable"


class RuntimeProtocolError(AgentOSError):
    """A runtime violated the wire protocol or reported a JSON-RPC error."""

    code = "agentos.runtime_protocol_error"

    def __init__(
        self,
        message: str,
        *,
        rpc_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        if rpc_code is not None:
            merged["rpc_code"] = rpc_code
        super().__init__(message, details=merged)
        self.rpc_code = rpc_code


class RuntimeTimeout(RuntimeUnavailable):
    """A runtime call exceeded its deadline."""

    code = "agentos.runtime_timeout"
    exit_status = 1


class CapabilityUnsupported(AgentOSError):
    """The active runtime does not implement the requested capability.

    Adapters must raise this instead of fabricating an event or result.
    """

    code = "agentos.capability_unsupported"


class StorageError(AgentOSError):
    """The audit or task store failed an operation."""

    code = "agentos.storage_error"


class IPCAccessDenied(AgentOSError):
    """A peer presented credentials that may not act on the requested object."""

    code = "agentos.ipc_access_denied"


class IPCTimeout(AgentOSError):
    """A Core request exceeded the client deadline."""

    code = "agentos.ipc_timeout"


class SystemBridgeError(AgentOSError):
    """A read-only system query failed or was refused."""

    code = "agentos.system_bridge_error"


__all__ = [
    "AgentOSError",
    "ApprovalInvalid",
    "ApprovalRequired",
    "CapabilityUnsupported",
    "ConfigurationError",
    "IPCAccessDenied",
    "IPCTimeout",
    "PolicyDenied",
    "RuntimeProtocolError",
    "RuntimeTimeout",
    "RuntimeUnavailable",
    "StorageError",
    "SystemBridgeError",
    "TaskStateError",
    "ValidationError",
]
