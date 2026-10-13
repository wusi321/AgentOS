"""AgentOS Core IPC: newline-delimited JSON over a Unix domain socket.

The transport is deliberately not an HTTP service. A Unix domain socket gives
the Core the peer's kernel-verified credentials through ``SO_PEERCRED``, so
authorization never depends on a value the client supplied. Nothing listens on a
network address.

Frame grammar, one JSON object per line:

request   ``{"v": 1, "id": "...", "method": "...", "params": {...}}``
response  ``{"v": 1, "id": "...", "result": {...}}``
          ``{"v": 1, "id": "...", "error": {"code": "...", "message": "...", "details": {...}}}``
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import uuid
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from ..constants import IPC_PROTOCOL_VERSION, PROJECT_VERSION
from ..errors import AgentOSError, IPCAccessDenied, ValidationError
from ..jsonutil import is_json_value, truncate_text
from ..schema import require_keys

#: Maximum bytes accepted for one request line.
MAX_FRAME_BYTES: Final = 1024 * 1024
#: Methods a peer may call without further authorization.
PUBLIC_METHODS: Final[frozenset[str]] = frozenset({"hello", "health"})

#: ``SO_PEERCRED`` is Linux-specific; the constant is 17 on x86_64 and arm64.
_SO_PEERCRED: Final = getattr(socket, "SO_PEERCRED", 17)


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    """Kernel-verified identity of a connected peer.

    :param pid: Peer process id.
    :param uid: Peer user id, as recorded by the kernel at connect time.
    :param gid: Peer group id.
    """

    pid: int
    uid: int
    gid: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of these credentials."""
        return {"pid": self.pid, "uid": self.uid, "gid": self.gid}


@dataclass(slots=True)
class IpcRequest:
    """One decoded request.

    :param request_id: Correlation id echoed in the response.
    :param method: Method name.
    :param params: Request parameters.
    :param peer: Kernel-verified credentials of the caller.
    :param protocol_version: Version the caller declared.
    """

    request_id: str
    method: str
    params: dict[str, Any]
    peer: PeerCredentials
    protocol_version: int = IPC_PROTOCOL_VERSION

    @property
    def is_root(self) -> bool:
        """Return whether the caller is the superuser."""
        return self.peer.uid == 0

    def param(self, name: str, default: Any = None) -> Any:
        """Return one parameter value."""
        return self.params.get(name, default)

    def require(self, *names: str) -> None:
        """Raise :class:`ValidationError` when a required parameter is missing."""
        require_keys(self.params, names, context=f"method {self.method}")


MethodHandler = Callable[[IpcRequest], Awaitable[Any] | Any]


class IpcServer:
    """Serve the Core request set on a Unix domain socket.

    :param socket_path: Path of the listening socket.
    :param group: Group name given ownership of the socket so that human users
        may connect; ``None`` leaves the socket owned by the service account only.
    :param socket_mode: Permission bits applied to the socket.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        group: str | None = None,
        socket_mode: int = 0o660,
    ) -> None:
        self._socket_path = socket_path
        self._group = group
        self._socket_mode = socket_mode
        self._handlers: dict[str, MethodHandler] = {}
        self._server: asyncio.AbstractServer | None = None
        self._closing = False

    @property
    def socket_path(self) -> str:
        """Return the socket path."""
        return self._socket_path

    def register(self, method: str, handler: MethodHandler) -> None:
        """Register a handler for ``method``.

        :raises ValidationError: On a duplicate registration.
        """
        if method in self._handlers:
            raise ValidationError(f"IPC method {method!r} is already registered")
        self._handlers[method] = handler

    def methods(self) -> list[str]:
        """Return the registered method names."""
        return sorted(self._handlers)

    async def start(self) -> None:
        """Create the listening socket and begin accepting peers.

        A stale socket file is removed only when nothing is listening on it, so a
        second service instance fails loudly instead of stealing the socket.

        :raises AgentOSError: When the socket cannot be created.
        """
        directory = os.path.dirname(self._socket_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self._socket_path):
            if _socket_is_live(self._socket_path):
                raise AgentOSError(
                    f"another AgentOS Core is already listening on {self._socket_path}",
                    code="agentos.ipc_in_use",
                    details={"socket_path": self._socket_path},
                )
            os.unlink(self._socket_path)
        self._server = await asyncio.start_unix_server(self._handle_connection, path=self._socket_path, limit=MAX_FRAME_BYTES)
        os.chmod(self._socket_path, self._socket_mode)
        if self._group:
            _chown_to_group(self._socket_path, self._group)

    async def serve_forever(self) -> None:
        """Run until :meth:`close` is called."""
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        """Stop accepting peers and remove the socket file."""
        self._closing = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
                pass
        try:
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
        except OSError:
            pass

    # -- connection handling ----------------------------------------------

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve every request on one connection until the peer disconnects."""
        peer = _peer_credentials(writer)
        if peer is None:
            writer.close()
            return
        try:
            while not self._closing:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await _write_response(
                        writer,
                        _error_response("", "agentos.validation_error", "request frame exceeds the size limit"),
                    )
                    break
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await _write_response(
                        writer,
                        _error_response("", "agentos.validation_error", "request frame exceeds the size limit"),
                    )
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                response = await self._dispatch_line(text, peer)
                if response is not None:
                    await _write_response(writer, response)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    async def _dispatch_line(self, text: str, peer: PeerCredentials) -> dict[str, Any] | None:
        """Decode and run one request line, returning the response frame."""
        request_id = ""
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return _error_response("", "agentos.validation_error", "request is not valid JSON")
        if not isinstance(frame, Mapping):
            return _error_response("", "agentos.validation_error", "request must be a JSON object")
        raw_id = frame.get("id")
        if isinstance(raw_id, str):
            request_id = raw_id
        elif isinstance(raw_id, int):
            request_id = str(raw_id)
        else:
            return _error_response("", "agentos.validation_error", "request id must be a string")
        method = frame.get("method")
        if not isinstance(method, str) or not method:
            return _error_response(request_id, "agentos.validation_error", "request method must be a non-empty string")
        version = frame.get("v", IPC_PROTOCOL_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            return _error_response(request_id, "agentos.validation_error", "request protocol version must be an integer")
        if version != IPC_PROTOCOL_VERSION:
            return _error_response(
                request_id,
                "agentos.protocol_mismatch",
                f"protocol version {version} is not supported; this Core speaks {IPC_PROTOCOL_VERSION}",
            )
        params = frame.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            return _error_response(request_id, "agentos.validation_error", "request params must be a JSON object")
        if not is_json_value(params):
            return _error_response(request_id, "agentos.validation_error", "request params are not JSON-serializable")
        handler = self._handlers.get(method)
        if handler is None:
            return _error_response(
                request_id,
                "agentos.unknown_method",
                f"unknown method {method!r}",
                details={"known": sorted(self._handlers)},
            )
        request = IpcRequest(
            request_id=request_id,
            method=method,
            params=dict(params),
            peer=peer,
            protocol_version=version,
        )
        try:
            result = handler(request)
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result
        except AgentOSError as error:
            return _error_response(request_id, error.code, error.message, details=error.details)
        except Exception as error:  # noqa: BLE001 - the server must answer, never drop the peer silently
            return _error_response(
                request_id,
                "agentos.internal_error",
                f"{type(error).__name__}: {error}",
            )
        if not is_json_value(result):
            return _error_response(
                request_id,
                "agentos.internal_error",
                f"handler for {method!r} returned a non-JSON value",
            )
        return {"v": IPC_PROTOCOL_VERSION, "id": request_id, "result": result}


def _error_response(
    request_id: str,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an error response frame."""
    error: dict[str, Any] = {"code": code, "message": truncate_text(message, limit=2048)}
    if details:
        error["details"] = dict(details)
    return {"v": IPC_PROTOCOL_VERSION, "id": request_id, "error": error}


async def _write_response(writer: asyncio.StreamWriter, frame: Mapping[str, Any]) -> None:
    """Serialize and send one response frame."""
    encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    writer.write(encoded.encode("utf-8"))
    try:
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass


def _peer_credentials(writer: asyncio.StreamWriter) -> PeerCredentials | None:
    """Read kernel-verified peer credentials from a connected socket.

    :returns: The peer's identity, or ``None`` when the platform did not report one.
    """
    transport = writer.transport
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None:
        return None
    try:
        pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i")))
    except OSError:
        return None
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def _socket_is_live(path: str) -> bool:
    """Return whether something is accepting connections on ``path``."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError, socket.timeout, OSError):
        return False
    finally:
        probe.close()
    return True


def _chown_to_group(path: str, group: str) -> None:
    """Set the socket's group ownership, tolerating a missing group.

    A missing group is not fatal for the service itself: the socket stays usable
    by the service account, and the install step is what creates the group.
    """
    try:
        import grp

        gid = grp.getgrnam(group).gr_gid
    except (KeyError, ImportError):
        return
    try:
        os.chown(path, -1, gid)
    except OSError:
        pass


def new_request_id() -> str:
    """Return a fresh request correlation id."""
    return str(uuid.uuid4())


__all__ = [
    "MAX_FRAME_BYTES",
    "PUBLIC_METHODS",
    "IpcRequest",
    "IpcServer",
    "MethodHandler",
    "PeerCredentials",
    "new_request_id",
]
