"""Newline-delimited JSON-RPC 2.0 client over a child process's stdio.

This is the AgentOS-side implementation of the DSH SDK wire protocol described
by ``@deepseek-ai/dsh-sdk-protocol``: one JSON-RPC message per ``\\n``-terminated
line, requests carrying ``id`` + ``method``, responses carrying ``id``,
notifications carrying ``method`` alone. Malformed lines are ignored, as the
upstream transport specifies.

The client deliberately owns its own framing instead of vendoring the upstream
Python SDK, because the shipped Debian package must run on a bare system Python
with no third-party wheels available at runtime.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import signal
from collections import deque
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from ..errors import RuntimeProtocolError, RuntimeTimeout, RuntimeUnavailable
from ..jsonutil import truncate_text

#: JSON-RPC error code the upstream transport returns for an unknown method.
RPC_METHOD_NOT_FOUND: Final = -32601
#: JSON-RPC error code the upstream transport returns for a handler failure.
RPC_INTERNAL_ERROR: Final = -32603

#: Maximum bytes accepted for one protocol line before it is treated as a violation.
MAX_FRAME_BYTES: Final = 8 * 1024 * 1024
#: Retained stderr lines used to explain a transport failure.
STDERR_HISTORY_LINES: Final = 200


@dataclass(slots=True)
class JsonRpcNotification:
    """One server-to-client notification."""

    method: str
    payload: Mapping[str, Any] = field(default_factory=dict)


NotificationHandler = Callable[[JsonRpcNotification], None]


class AsyncJsonRpcProcess:
    """Drive one JSON-RPC-over-stdio child process.

    :param argv: Executable and arguments; never a shell string.
    :param cwd: Working directory for the child.
    :param env: Complete child environment; the parent environment is never inherited wholesale.
    :param request_timeout_seconds: Default deadline for :meth:`request`.
    :param description: Human-readable label used in error messages.
    """

    def __init__(
        self,
        argv: "tuple[str, ...] | list[str]",
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        request_timeout_seconds: float | None = 120.0,
        description: str = "json-rpc child",
    ) -> None:
        if not argv:
            raise ValueError("argv must not be empty")
        self._argv = tuple(str(item) for item in argv)
        self._cwd = cwd
        self._env = dict(env or {})
        self._request_timeout = request_timeout_seconds
        self._description = description

        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._waiters: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self._notification_handlers: list[NotificationHandler] = []
        self._stderr: deque[str] = deque(maxlen=STDERR_HISTORY_LINES)
        self._closing = False
        self._exit_code: int | None = None
        self._fatal: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        """Return whether the child process is still alive."""
        return self._process is not None and self._process.returncode is None

    @property
    def exit_code(self) -> int | None:
        """Return the child exit code once it has exited."""
        return self._exit_code

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        """Register a synchronous notification callback."""
        self._notification_handlers.append(handler)

    async def start(self) -> None:
        """Spawn the child and begin reading its streams.

        :raises RuntimeUnavailable: When the executable cannot be spawned.
        """
        if self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=dict(self._env),
                limit=MAX_FRAME_BYTES,
            )
        except FileNotFoundError as error:
            raise RuntimeUnavailable(
                f"{self._description}: executable not found: {self._argv[0]}",
                details={"executable": self._argv[0]},
            ) from error
        except PermissionError as error:
            raise RuntimeUnavailable(
                f"{self._description}: executable is not runnable: {self._argv[0]}",
                details={"executable": self._argv[0]},
            ) from error
        except OSError as error:
            raise RuntimeUnavailable(
                f"{self._description}: cannot start child process: {error}",
                details={"executable": self._argv[0]},
            ) from error
        self._reader_task = asyncio.create_task(self._read_stdout(), name=f"{self._description}-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"{self._description}-stderr")

    async def close(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop the child, escalating from stdin close to terminate to kill."""
        process = self._process
        if process is None:
            return
        self._closing = True
        if process.stdin is not None and not process.stdin.is_closing():
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                self._signal_child(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    self._signal_child(signal.SIGKILL)
                    await process.wait()
        await self._cancel_tasks()
        self._fail_pending(
            RuntimeUnavailable(f"{self._description}: transport closed", details={"reason": "closed"})
        )
        self._process = None

    def _signal_child(self, signum: int) -> None:
        """Send ``signum`` to the child, ignoring an already-dead process."""
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            process.send_signal(signum)
        except (ProcessLookupError, PermissionError):
            pass

    async def _cancel_tasks(self) -> None:
        """Cancel the reader tasks and wait for them to settle."""
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - reader failures are already recorded
                    pass
        self._reader_task = None
        self._stderr_task = None

    # -- request/notification plumbing ------------------------------------

    async def request(
        self,
        method: str,
        params: Any | None = None,
        *,
        timeout_seconds: float | None = None,
        description: str | None = None,
    ) -> Any:
        """Send a request and await its result.

        :param method: JSON-RPC method name declared by the peer.
        :param params: Request parameters; omitted from the frame when ``None``.
        :param timeout_seconds: Overrides the transport default.
        :param description: Overrides the default timeout explanation.
        :raises RuntimeProtocolError: When the peer answers with an error frame.
        :raises RuntimeTimeout: When the peer does not answer in time.
        :raises RuntimeUnavailable: When the child exited before answering.
        """
        process = self._process
        if process is None or process.returncode is not None:
            raise RuntimeUnavailable(
                self._unavailable_reason(),
                details={"method": method, "exit_code": self._exit_code},
            )
        request_id = next(self._ids)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[Any] = loop.create_future()
        self._waiters[request_id] = waiter
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            await self._write_frame(frame)
        except Exception:
            self._waiters.pop(request_id, None)
            raise
        effective_timeout = self._request_timeout if timeout_seconds is None else timeout_seconds
        try:
            if effective_timeout is None:
                return await waiter
            return await asyncio.wait_for(waiter, timeout=effective_timeout)
        except asyncio.TimeoutError as error:
            self._waiters.pop(request_id, None)
            explanation = description or f"{self._description}: {method} did not answer"
            raise RuntimeTimeout(
                f"{explanation} within {effective_timeout:g}s",
                details={"method": method, "timeout_seconds": effective_timeout, "stderr_tail": self.stderr_tail()},
            ) from error
        finally:
            self._waiters.pop(request_id, None)

    async def _write_frame(self, frame: Mapping[str, Any]) -> None:
        """Serialize and write one frame, flushing immediately."""
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeUnavailable(self._unavailable_reason(), details={"reason": "no_stdin"})
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            process.stdin.write(encoded.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise RuntimeUnavailable(
                self._unavailable_reason(),
                details={"reason": "broken_pipe", "exit_code": self._exit_code},
            ) from error

    def _unavailable_reason(self) -> str:
        """Compose an operator-facing explanation of why the transport is unusable."""
        if self._fatal is not None:
            return f"{self._description}: transport failed: {self._fatal}"
        tail = self.stderr_tail()
        base = f"{self._description}: child process is not running (exit code {self._exit_code})"
        return f"{base}\nstderr tail:\n{tail}" if tail else base

    async def _read_stdout(self) -> None:
        """Read protocol lines until EOF, dispatching frames as they complete."""
        process = self._process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    self._stderr.append(f"oversized protocol frame dropped ({len(line)} bytes)")
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                self._dispatch(text)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the loop must fail waiters rather than die silently
            self._fatal = error
        finally:
            code = await process.wait()
            self._exit_code = code
            self._fail_pending(
                RuntimeUnavailable(
                    self._unavailable_reason(),
                    details={"reason": "eof", "exit_code": code},
                )
            )

    async def _read_stderr(self) -> None:
        """Retain a bounded tail of the child's stderr for diagnostics."""
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                self._stderr.append(line.decode("utf-8", errors="replace").rstrip("\n"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stderr diagnostics are best effort
            pass

    def _dispatch(self, text: str) -> None:
        """Route one decoded line to a waiter or to the notification handlers.

        Malformed lines are ignored: the upstream transport treats them as noise
        rather than as a fatal protocol violation.
        """
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            self._stderr.append("ignored malformed JSON-RPC line")
            return
        if not isinstance(frame, Mapping):
            self._stderr.append("ignored non-object JSON-RPC frame")
            return
        has_id = "id" in frame and frame.get("id") is not None
        has_method = isinstance(frame.get("method"), str)
        if has_id and has_method:
            # Server-to-client request. The upstream server never sends one, so
            # answer with a method-not-found error rather than hanging the peer.
            asyncio.create_task(self._answer_unsupported(frame))
            return
        if has_id:
            self._settle(frame)
            return
        if has_method:
            notification = JsonRpcNotification(
                method=str(frame["method"]),
                payload=frame.get("params") if isinstance(frame.get("params"), Mapping) else {},
            )
            for handler in list(self._notification_handlers):
                try:
                    handler(notification)
                except Exception as error:  # noqa: BLE001 - one bad consumer must not stop the reader
                    self._stderr.append(f"notification handler failed: {error}")
            return
        self._stderr.append("ignored JSON-RPC frame with neither id nor method")

    async def _answer_unsupported(self, frame: Mapping[str, Any]) -> None:
        """Answer an unexpected server-to-client request with method-not-found."""
        response = {
            "jsonrpc": "2.0",
            "id": frame.get("id"),
            "error": {"code": RPC_METHOD_NOT_FOUND, "message": f"unknown method {frame.get('method')!r}"},
        }
        try:
            await self._write_frame(response)
        except RuntimeUnavailable:
            pass

    def _settle(self, frame: Mapping[str, Any]) -> None:
        """Resolve or reject the pending request named by ``frame``."""
        request_id = frame.get("id")
        if not isinstance(request_id, int):
            self._stderr.append(f"ignored response with non-integer id {request_id!r}")
            return
        waiter = self._waiters.pop(request_id, None)
        if waiter is None:
            self._stderr.append(f"ignored response for unknown request id {request_id}")
            return
        if waiter.done():
            return
        error = frame.get("error")
        if isinstance(error, Mapping):
            waiter.set_exception(
                RuntimeProtocolError(
                    str(error.get("message") or "remote error"),
                    rpc_code=int(error["code"]) if isinstance(error.get("code"), int) else None,
                    details={"data": error.get("data")} if "data" in error else None,
                )
            )
            return
        waiter.set_result(frame.get("result"))

    def _fail_pending(self, error: BaseException) -> None:
        """Reject every outstanding request with ``error``."""
        pending = list(self._waiters.values())
        self._waiters.clear()
        for waiter in pending:
            if not waiter.done():
                waiter.set_exception(error)

    def stderr_tail(self, *, lines: int = 20) -> str:
        """Return the retained stderr tail, truncated for log safety."""
        recent = list(self._stderr)[-lines:]
        return truncate_text("\n".join(recent), limit=4000)


def minimal_child_environment(
    *,
    additions: Mapping[str, str] | None = None,
    pass_through: "tuple[str, ...]" = ("LANG", "LC_ALL", "TERM", "TZ", "HOME"),
    baseline_path: str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
) -> dict[str, str]:
    """Build an allowlisted child environment.

    A runtime child never inherits the parent environment wholesale: doing so
    would leak service credentials into a model-driven process, and it would make
    the runtime's behaviour depend on unrelated deployment variables. ``PATH`` is
    not passed through by default for the same reason: a developer's shell PATH
    must not decide which tools the runtime can find.

    :param additions: Explicit values this deployment requires, such as ``DSH_HOME``.
    :param pass_through: Parent variable names safe to forward when set.
    :param baseline_path: ``PATH`` value the child receives.
    :returns: The complete environment for the child.
    """
    environment: dict[str, str] = {"PATH": baseline_path}
    for name in pass_through:
        value = os.environ.get(name)
        if value:
            environment[name] = value
    for name, value in (additions or {}).items():
        environment[str(name)] = str(value)
    return environment


__all__ = [
    "MAX_FRAME_BYTES",
    "RPC_INTERNAL_ERROR",
    "RPC_METHOD_NOT_FOUND",
    "AsyncJsonRpcProcess",
    "JsonRpcNotification",
    "minimal_child_environment",
]
