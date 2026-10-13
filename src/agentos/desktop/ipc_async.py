"""Threaded IPC bridge for the GTK main loop.

Every Core call is a blocking socket operation, so a view must never make one
from the main loop. This module owns one worker thread with its own connection
and marshals each result back through ``GLib.idle_add``, which is the only
thread-safe way to touch a widget from the worker.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any

from ..core.ipc_client import IpcClient
from ..errors import AgentOSError

#: Maximum queued requests before new work is rejected.
MAX_PENDING = 64


@dataclass(slots=True)
class PendingCall:
    """One queued Core call.

    :param method: IPC method name.
    :param params: Request parameters.
    :param on_success: Called on the main loop with the result.
    :param on_error: Called on the main loop with the failure.
    """

    method: str
    params: Mapping[str, Any]
    on_success: Callable[[Any], None]
    on_error: Callable[[AgentOSError], None]


class AsyncIpc:
    """Run Core calls on a worker thread and deliver results on the main loop.

    :param socket_path: Core IPC socket.
    :param timeout_seconds: Per-call deadline.
    :param context: Optional GLib main context used to dispatch results; the
        default main context is correct for a GTK application.
    """

    def __init__(self, socket_path: str, *, timeout_seconds: float = 20.0, context: Any = None) -> None:
        self._socket_path = socket_path
        self._timeout = timeout_seconds
        self._queue: queue.Queue[PendingCall | None] = queue.Queue(maxsize=MAX_PENDING)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._context = context
        self._client = IpcClient(socket_path, timeout_seconds=timeout_seconds)

    @property
    def socket_path(self) -> str:
        """Return the Core socket path."""
        return self._socket_path

    def start(self) -> None:
        """Start the worker thread once."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agentos-desktop-ipc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker to finish and join it."""
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        on_success: Callable[[Any], None],
        on_error: Callable[[AgentOSError], None],
    ) -> bool:
        """Queue one call.

        :returns: ``False`` when the queue is saturated, so the caller can report
            a busy state instead of blocking the main loop.
        """
        try:
            self._queue.put_nowait(PendingCall(method, dict(params or {}), on_success, on_error))
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        """Consume queued calls until stopped."""
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            try:
                result = self._client.call(item.method, item.params)
            except AgentOSError as error:
                self._dispatch(lambda error=error, callback=item.on_error: callback(error))
                continue
            except Exception as error:  # noqa: BLE001 - the worker must never die
                wrapped = AgentOSError(
                    f"{type(error).__name__}: {error}",
                    code="agentos.desktop_worker_error",
                )
                self._dispatch(lambda wrapped=wrapped, callback=item.on_error: callback(wrapped))
                continue
            self._dispatch(lambda result=result, callback=item.on_success: callback(result))

    def _dispatch(self, callback: Callable[[], None]) -> None:
        """Run a callback on the main loop, falling back to a direct call."""
        try:
            from gi.repository import GLib

            if self._context is not None:
                GLib.idle_add(callback, priority=GLib.PRIORITY_DEFAULT, context=self._context)
            else:
                GLib.idle_add(callback)
        except Exception:  # noqa: BLE001 - without GLib there is no main loop to protect
            callback()


__all__ = ["MAX_PENDING", "AsyncIpc", "PendingCall"]
