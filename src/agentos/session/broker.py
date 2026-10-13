"""Session broker: the per-login bridge between the GNOME Shell extension and Core.

The broker runs inside the user's session, so it holds nothing but the current
user's authorization, and the extension never talks to the system service
directly. That split is what lets the extension stay as thin as the design
requires: the broker is the only component that both speaks D-Bus and knows the
Core IPC protocol.

The broker is deliberately boring. It reports state, forwards a request to open
the desktop application, and forwards Core events. It does not approve anything,
does not execute anything, and does not persist a secret.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Final

from ..constants import CORE_SESSION_OBJECT_PATH, CORE_SESSION_SERVICE_NAME, SYSTEM_SOCKET
from ..core.ipc_client import IpcClient
from ..errors import AgentOSError
from ..jsonutil import bounded_payload, truncate_text

#: D-Bus interface the extension calls.
SESSION_INTERFACE: Final = CORE_SESSION_SERVICE_NAME
#: Object path the extension calls.
SESSION_PATH: Final = CORE_SESSION_OBJECT_PATH
#: Well-known bus name the broker owns.
SESSION_BUS_NAME: Final = CORE_SESSION_SERVICE_NAME

#: D-Bus introspection data, so the extension and `gdbus` work without the code.
INTROSPECTION_XML: Final = """<!DOCTYPE node PUBLIC
 "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node name="/org/agentos/Session1">
  <interface name="org.agentos.Session1">
    <method name="OpenDesktop">
      <arg name="launched" type="b" direction="out"/>
    </method>
    <method name="GetStatus">
      <arg name="state" type="s" direction="out"/>
      <arg name="payload_json" type="s" direction="out"/>
    </method>
    <method name="Subscribe">
      <arg name="token" type="s" direction="out"/>
    </method>
    <method name="Unsubscribe">
      <arg name="token" type="s" direction="in"/>
    </method>
    <signal name="Event">
      <arg name="token" type="s"/>
      <arg name="event_json" type="s"/>
    </signal>
  </interface>
</node>
"""

#: Cap on events retained for a subscriber before the oldest are dropped.
SUBSCRIBER_BACKLOG: Final = 128
#: Cap on recent events summarised in one status payload.
MAX_RECENT_EVENTS: Final = 10
#: Poll interval for pulling Core events into the session bus.
EVENT_POLL_SECONDS: Final = 0.5


@dataclass(slots=True)
class BrokerStatus:
    """A status payload the extension renders.

    :param core_available: Whether the Core IPC socket answered.
    :param runtime_id: Selected runtime, when Core was reachable.
    :param runtime_available: Whether that runtime can serve a request.
    :param runtime_detail: Why the runtime is unavailable, when it is.
    :param tasks: Task counts keyed by state.
    :param active_tasks: Queued and running tasks.
    :param pending_approvals: Approvals awaiting a human decision.
    :param recent_events: Bounded recent event summaries.
    :param error: Explanation when Core could not be queried.
    """

    core_available: bool = False
    runtime_id: str = ""
    runtime_available: bool = False
    runtime_detail: str = ""
    tasks: dict[str, int] = field(default_factory=dict)
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON document the extension parses."""
        return {
            "core_available": self.core_available,
            "runtime_id": self.runtime_id,
            "runtime_available": self.runtime_available,
            "runtime_detail": truncate_text(self.runtime_detail, limit=280),
            "tasks": dict(self.tasks),
            "active_tasks": self.active_tasks,
            "pending_approvals": self.pending_approvals,
            "recent_events": self.recent_events,
            "error": truncate_text(self.error, limit=280),
        }

    def to_json(self) -> str:
        """Return the status payload as JSON text."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


class SessionBroker:
    """Collect Core state for one login session and publish it over session D-Bus.

    :param socket_path: Core IPC socket.
    :param desktop_launcher: Command used to start the desktop application; the
        default resolves ``agentos-desktop`` on ``PATH``.
    :param timeout_seconds: Per-call Core timeout.
    """

    def __init__(
        self,
        *,
        socket_path: str = SYSTEM_SOCKET,
        desktop_launcher: "tuple[str, ...] | None" = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = IpcClient(socket_path, timeout_seconds=timeout_seconds)
        self._launcher = desktop_launcher or self._default_launcher()
        self._subscribers: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._core_token: str | None = None
        self._last_poll = 0.0

    @staticmethod
    def _default_launcher() -> tuple[str, ...]:
        """Return the command that starts the desktop application."""
        explicit = os.environ.get("AGENTOS_DESKTOP_COMMAND")
        if explicit:
            return tuple(explicit.split())
        found = shutil.which("agentos-desktop")
        if found:
            return (found,)
        return (sys.executable or "python3", "-m", "agentos.desktop")

    # -- status ------------------------------------------------------------

    def status(self) -> BrokerStatus:
        """Collect the current status, degrading to an explanatory payload."""
        if not self._client.available():
            return BrokerStatus(
                core_available=False,
                error="the AgentOS Core service is not reachable",
            )
        try:
            health = self._client.call("health")
            tasks = self._client.call("task.list", {"limit": 20})
            approvals = self._client.call("approval.list", {"states": ["pending", "granted"], "limit": 20})
            events = self._client.call("events.recent", {"limit": MAX_RECENT_EVENTS})
        except AgentOSError as error:
            return BrokerStatus(
                core_available=False,
                error=f"{error.code}: {error.message}",
            )
        runtime = health.get("runtime", {}) if isinstance(health, Mapping) else {}
        active = [
            {
                "task_id": task.get("task_id"),
                "title": truncate_text(str(task.get("title") or ""), limit=80),
                "state": task.get("state"),
            }
            for task in tasks.get("tasks", [])
            if task.get("state") in {"QUEUED", "RUNNING", "WAITING_APPROVAL", "RECOVERING"}
        ]
        return BrokerStatus(
            core_available=True,
            runtime_id=str(runtime.get("runtime_id") or ""),
            runtime_available=bool(runtime.get("available")),
            runtime_detail=str(runtime.get("detail") or ""),
            tasks={str(key): int(value) for key, value in (tasks.get("by_state") or {}).items()},
            active_tasks=active,
            pending_approvals=[
                {
                    "approval_id": approval.get("approval_id"),
                    "operation": approval.get("operation"),
                    "target": approval.get("target"),
                    "reason": truncate_text(str(approval.get("reason") or ""), limit=120),
                    "state": approval.get("state"),
                }
                for approval in approvals.get("approvals", [])
            ],
            recent_events=[
                _summarise_event(event)
                for event in events.get("events", [])[-MAX_RECENT_EVENTS:]
            ],
        )

    def status_state(self) -> str:
        """Return the coarse state string the extension switches on."""
        status = self.status()
        if not status.core_available:
            return "unavailable"
        return "ok"

    # -- desktop launch ----------------------------------------------------

    def open_desktop(self) -> bool:
        """Start the desktop application detached from this process.

        The child is started with ``start_new_session`` so that it survives the
        broker and is not killed with the D-Bus activation context. Failures are
        reported rather than raised, because a D-Bus caller cannot act on an
        exception from a launch.
        """
        try:
            subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                list(self._launcher),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=_desktop_environment(),
            )
        except (OSError, ValueError) as error:
            sys.stderr.write(f"agentos-session: cannot start the desktop application: {error}\n")
            return False
        return True

    # -- subscriptions -----------------------------------------------------

    def subscribe(self) -> str:
        """Register a subscriber and return its token."""
        import uuid

        token = str(uuid.uuid4())
        with self._lock:
            self._subscribers[token] = []
        return token

    def unsubscribe(self, token: str) -> None:
        """Drop a subscriber."""
        with self._lock:
            self._subscribers.pop(token, None)

    def publish(self, event: Mapping[str, Any]) -> None:
        """Offer one event to every subscriber, dropping the oldest when full."""
        payload = dict(event)
        with self._lock:
            for token, backlog in self._subscribers.items():
                backlog.append(payload)
                if len(backlog) > SUBSCRIBER_BACKLOG:
                    del backlog[: len(backlog) - SUBSCRIBER_BACKLOG]

    def drain(self, token: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Remove and return the events queued for one subscriber."""
        with self._lock:
            backlog = self._subscribers.get(token)
            if backlog is None:
                return []
            taken = backlog[:limit]
            del backlog[:limit]
            return taken

    def subscriber_count(self) -> int:
        """Return the number of live subscribers."""
        with self._lock:
            return len(self._subscribers)

    def subscriber_tokens(self) -> list[str]:
        """Return the live subscriber tokens."""
        with self._lock:
            return list(self._subscribers)

    # -- Core event relay --------------------------------------------------

    def poll_core_events(self, *, timeout_seconds: float = EVENT_POLL_SECONDS) -> list[dict[str, Any]]:
        """Return new Core events, keeping one subscription open between polls.

        The Core subscription is created lazily and recreated when the service
        restarts, so a broker that outlives a Core restart recovers by itself.

        :param timeout_seconds: Per-call Core deadline.
        """
        now = time.monotonic()
        if now - self._last_poll < timeout_seconds * 0.5:
            return []
        self._last_poll = now
        if self._core_token is None:
            try:
                self._core_token = str(self._client.call("events.subscribe").get("token", ""))
            except AgentOSError:
                self._core_token = None
                return []
            if not self._core_token:
                self._core_token = None
                return []
        try:
            batch = self._client.call("events.poll", {"token": self._core_token, "limit": 100})
        except AgentOSError:
            # The service restarted or the socket went away; forget the token so
            # the next poll creates a new subscription instead of failing forever.
            self._core_token = None
            return []
        events: list[dict[str, Any]] = []
        for item in batch.get("events", []):
            if isinstance(item, Mapping) and isinstance(item.get("event"), Mapping):
                events.append(dict(item["event"]))
        return events


def _summarise_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a full event to the fields the panel shows."""
    payload = event.get("payload")
    summary = ""
    if isinstance(payload, Mapping):
        for key in ("error", "reason", "text", "summary", "title", "from"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                summary = value
                break
        if not summary and payload:
            summary = json.dumps(bounded_payload(dict(payload), limit=512), ensure_ascii=False)
    return {
        "event_type": event.get("event_type"),
        "task_id": event.get("task_id"),
        "severity": event.get("severity", "info"),
        "timestamp": event.get("timestamp"),
        "summary": truncate_text(summary, limit=200),
    }


def _desktop_environment() -> dict[str, str]:
    """Return the environment the desktop application needs to display a window."""
    environment = dict(os.environ)
    environment.setdefault("DISPLAY", os.environ.get("DISPLAY", ""))
    environment.setdefault("XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR", ""))
    return environment


# -- D-Bus service ---------------------------------------------------------


class SessionDbusService:
    """Serve :class:`SessionBroker` over the session bus.

    :param broker: Status and subscription source.
    :param connection: An existing session bus connection, or ``None`` to obtain one.
    """

    def __init__(self, broker: SessionBroker, *, connection: Any = None) -> None:
        self._broker = broker
        self._connection = connection
        self._bus = None
        self._loop = None
        self._dbus = None
        self._module = None
        self._event_source_id: int | None = None
        self._last_event_key: tuple[str, str, str | None] | None = None

    def available(self) -> bool:
        """Return whether the D-Bus bindings are importable on this system."""
        try:
            import dbus  # noqa: F401
        except ImportError:
            return False
        return True

    def serve_forever(self) -> int:
        """Own the bus name and run the GLib main loop until interrupted.

        :returns: A process exit status.
        """
        try:
            import dbus
            import dbus.mainloop.glib
            import dbus.service
            from gi.repository import GLib
        except ImportError as error:
            sys.stderr.write(
                "agentos-session: python3-dbus and python3-gi are required to serve the "
                f"session interface: {error}\n"
            )
            return 3
        self._dbus = dbus
        self._module = (dbus.service, GLib)
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = self._connection or dbus.SessionBus()
        broker = self._broker

        class _Service(dbus.service.Object):  # type: ignore[misc]
            """The exported D-Bus object; delegates every method to the broker."""

            @dbus.service.method(SESSION_INTERFACE, in_signature="", out_signature="b")
            def OpenDesktop(self) -> bool:
                """Start or focus the AgentOS desktop application."""
                return broker.open_desktop()

            @dbus.service.method(SESSION_INTERFACE, in_signature="", out_signature="ss")
            def GetStatus(self):
                """Return ``(state, payload_json)``."""
                try:
                    status = broker.status()
                except Exception as error:  # noqa: BLE001 - a D-Bus method must answer
                    return ("error", json.dumps({"error": f"{type(error).__name__}: {error}"}))
                if not status.core_available:
                    return ("unavailable", status.to_json())
                return ("ok", status.to_json())

            @dbus.service.method(SESSION_INTERFACE, in_signature="", out_signature="s")
            def Subscribe(self) -> str:
                """Register a subscriber and return its token."""
                return broker.subscribe()

            @dbus.service.method(SESSION_INTERFACE, in_signature="s", out_signature="")
            def Unsubscribe(self, token: str) -> None:
                """Drop a subscriber."""
                broker.unsubscribe(str(token))

            @dbus.service.signal(SESSION_INTERFACE, signature="ss")
            def Event(self, token: str, event_json: str) -> None:
                """Deliver one Core event to one subscriber."""

        name = dbus.service.BusName(SESSION_BUS_NAME, bus=self._bus)
        service = _Service(self._bus, SESSION_PATH)
        self._service = service
        self._name = name
        self._loop = GLib.MainLoop()

        def poll() -> bool:
            """Forward queued Core events onto the bus, once per interval."""
            if self._broker.subscriber_count() == 0:
                return True
            try:
                for event in self._broker.poll_core_events():
                    key = (
                        str(event.get("event_id")),
                        str(event.get("event_type")),
                        None if event.get("task_id") is None else str(event.get("task_id")),
                    )
                    if key == self._last_event_key:
                        continue
                    self._last_event_key = key
                    for token in self._broker.subscriber_tokens():
                        service.Event(token, json.dumps(event, ensure_ascii=False))
            except Exception as error:  # noqa: BLE001 - the loop must never die
                sys.stderr.write(f"agentos-session: event poll failed: {error}\n")
            return True

        self._event_source_id = GLib.timeout_add(500, poll)
        try:
            self._loop.run()
        except KeyboardInterrupt:
            return 0
        finally:
            if self._event_source_id is not None:
                GLib.source_remove(self._event_source_id)
                self._event_source_id = None
        return 0

    def stop(self) -> None:
        """Quit the GLib main loop if it is running."""
        if self._loop is not None:
            self._loop.quit()


def main(argv: "list[str] | None" = None) -> int:
    """Run the session broker as a process.

    The process exits non-zero only when the session bus itself is unusable: a
    missing Core service is a normal, reported state, not a startup failure.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="agentos-session",
        description="AgentOS session broker: serves the AgentOS panel over the user session bus.",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("AGENTOS_SOCKET", SYSTEM_SOCKET),
        help=f"Core IPC socket path (default: {SYSTEM_SOCKET})",
    )
    parser.add_argument("--status", action="store_true", help="print the status payload and exit")
    parser.add_argument("--check", action="store_true", help="report whether the session bus is usable and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    broker = SessionBroker(socket_path=args.socket)
    if args.status:
        print(broker.status().to_json())
        return 0
    service = SessionDbusService(broker)
    if args.check:
        if not service.available():
            print("python3-dbus or python3-gi is not installed", file=sys.stderr)
            return 3
        print("session broker dependencies are available")
        return 0
    return service.serve_forever()


__all__ = [
    "BrokerStatus",
    "EVENT_POLL_SECONDS",
    "INTROSPECTION_XML",
    "MAX_RECENT_EVENTS",
    "SESSION_BUS_NAME",
    "SESSION_INTERFACE",
    "SESSION_PATH",
    "SUBSCRIBER_BACKLOG",
    "SessionBroker",
    "SessionDbusService",
    "main",
]
