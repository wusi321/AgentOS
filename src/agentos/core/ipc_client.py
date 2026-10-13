"""Synchronous IPC client used by the CLI and the desktop application.

The client is intentionally synchronous: the CLI is a short-lived process and the
desktop application drives it from a worker thread so that a slow Core can never
freeze the GTK main loop.
"""

from __future__ import annotations

import json
import socket
import uuid
from collections.abc import Mapping
from typing import Any, Final

from ..constants import IPC_PROTOCOL_VERSION
from ..errors import (
    AgentOSError,
    ApprovalInvalid,
    ApprovalRequired,
    CapabilityUnsupported,
    IPCAccessDenied,
    IPCTimeout,
    PolicyDenied,
    StorageError,
    TaskStateError,
    ValidationError,
)
from .ipc import MAX_FRAME_BYTES, PeerCredentials

#: Default deadline for one request.
DEFAULT_TIMEOUT_SECONDS: Final = 30.0

#: Map wire error codes onto the exception types the CLI already handles.
_ERROR_TYPES: Final[Mapping[str, type[AgentOSError]]] = {
    "agentos.policy_denied": PolicyDenied,
    "agentos.approval_required": ApprovalRequired,
    "agentos.approval_invalid": ApprovalInvalid,
    "agentos.ipc_access_denied": IPCAccessDenied,
    "agentos.validation_error": ValidationError,
    "agentos.task_state_error": TaskStateError,
    "agentos.storage_error": StorageError,
    "agentos.capability_unsupported": CapabilityUnsupported,
}


class IpcClientError(AgentOSError):
    """A failure reported by the Core service over IPC."""

    code = "agentos.ipc_error"


class IpcClient:
    """Call the AgentOS Core over its Unix domain socket.

    :param socket_path: Socket to connect to.
    :param timeout_seconds: Default deadline for one request.
    """

    def __init__(self, socket_path: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._socket_path = socket_path
        self._timeout = timeout_seconds

    @property
    def socket_path(self) -> str:
        """Return the socket path this client targets."""
        return self._socket_path

    def available(self) -> bool:
        """Return whether the Core socket accepts a connection right now."""
        try:
            with self._connect(timeout=1.0):
                return True
        except AgentOSError:
            return False

    def _connect(self, *, timeout: float | None = None):
        """Open a connected socket, translating OS failures into AgentOS errors."""
        effective = self._timeout if timeout is None else timeout
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(effective)
        try:
            sock.connect(self._socket_path)
        except FileNotFoundError as error:
            sock.close()
            raise IpcClientError(
                f"the AgentOS Core service is not running: {self._socket_path} does not exist",
                code="agentos.core_unavailable",
                details={"socket_path": self._socket_path},
            ) from error
        except (ConnectionRefusedError, ConnectionResetError) as error:
            sock.close()
            raise IpcClientError(
                f"the AgentOS Core service refused the connection on {self._socket_path}",
                code="agentos.core_unavailable",
                details={"socket_path": self._socket_path},
            ) from error
        except socket.timeout as error:
            sock.close()
            raise IPCTimeout(
                f"connecting to {self._socket_path} timed out",
                details={"socket_path": self._socket_path},
            ) from error
        except PermissionError as error:
            sock.close()
            raise IPCAccessDenied(
                f"permission denied connecting to {self._socket_path}; "
                "add this user to the agentos group and start a new login session",
                details={"socket_path": self._socket_path},
            ) from error
        except OSError as error:
            sock.close()
            raise IpcClientError(
                f"cannot connect to {self._socket_path}: {error}",
                code="agentos.core_unavailable",
                details={"socket_path": self._socket_path},
            ) from error
        return sock

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Send one request and return its result.

        :param method: Registered Core method name.
        :param params: Request parameters.
        :param timeout_seconds: Overrides the client default.
        :raises IpcClientError: On a transport failure or protocol violation.
        :raises IPCTimeout: When the Core does not answer in time.
        :raises AgentOSError: A subtype matching the Core's reported error code.
        """
        request_id = str(uuid.uuid4())
        frame = {
            "v": IPC_PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        sock = self._connect(timeout=timeout_seconds)
        try:
            sock.sendall(encoded.encode("utf-8"))
            line = _read_line(sock, timeout=timeout_seconds if timeout_seconds is not None else self._timeout)
        except socket.timeout as error:
            raise IPCTimeout(
                f"{method} did not answer within {timeout_seconds or self._timeout:g}s",
                details={"method": method},
            ) from error
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if not line:
            raise IpcClientError(
                f"the Core closed the connection while handling {method!r}",
                code="agentos.core_unavailable",
                details={"method": method},
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise IpcClientError(
                f"the Core sent a malformed response to {method!r}",
                code="agentos.protocol_error",
                details={"method": method},
            ) from error
        if not isinstance(response, Mapping):
            raise IpcClientError("the Core response is not a JSON object", code="agentos.protocol_error")
        if str(response.get("id")) != request_id:
            raise IpcClientError(
                "the Core response does not match the request id",
                code="agentos.protocol_error",
                details={"method": method},
            )
        error = response.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or "agentos.ipc_error")
            message = str(error.get("message") or "the Core reported an error")
            details = error.get("details") if isinstance(error.get("details"), Mapping) else {}
            error_type = _ERROR_TYPES.get(code, IpcClientError)
            if error_type is IpcClientError:
                raise IpcClientError(message, code=code, details=dict(details))
            raise error_type(message, details=dict(details))
        return response.get("result")

    def peer_identity(self) -> PeerCredentials | None:
        """Return this client's own credentials as the Core would see them.

        Used by ``agentosctl doctor`` to explain an access denial without asking
        the Core for information it may refuse to give.
        """
        try:
            return PeerCredentials(pid=0, uid=_current_uid(), gid=_current_gid())
        except OSError:
            return None


def _read_line(sock: socket.socket, *, timeout: float) -> str:
    """Read one newline-terminated frame, bounded by the protocol frame limit."""
    sock.settimeout(timeout)
    buffer = bytearray()
    while True:
        if len(buffer) > MAX_FRAME_BYTES:
            raise IpcClientError("the Core response exceeds the frame size limit", code="agentos.protocol_error")
        chunk = sock.recv(65536)
        if not chunk:
            break
        buffer.extend(chunk)
        newline = buffer.find(b"\n")
        if newline >= 0:
            return buffer[:newline].decode("utf-8", errors="replace")
    return buffer.decode("utf-8", errors="replace").strip()


def _current_uid() -> int:
    """Return the effective user id."""
    import os

    return os.geteuid() if hasattr(os, "geteuid") else os.getuid()


def _current_gid() -> int:
    """Return the effective group id."""
    import os

    return os.getegid() if hasattr(os, "getegid") else os.getgid()


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "IpcClient", "IpcClientError"]
