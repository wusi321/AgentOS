"""GTK4/libadwaita desktop application: the AgentOS window.

The window is a thin client. It renders Core state, sends prompts, and presents
approvals as structured operations with their exact target and parameters. It
never decides a policy question itself and never executes a system operation: it
asks the Core, and shows what the Core answered.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any, Final

from ..constants import PROJECT_NAME, PROJECT_TAGLINE, PROJECT_VERSION, SYSTEM_SOCKET
from ..errors import AgentOSError
from .ipc_async import AsyncIpc

#: Application id; also the desktop file basename.
APPLICATION_ID: Final = "org.agentos.Desktop"
#: Interval between status refreshes.
STATUS_INTERVAL_SECONDS: Final = 2.0


class MissingDependency(RuntimeError):
    """Raised when GTK4, libadwaita, or their bindings are unavailable."""


def require_gtk() -> tuple[Any, Any, Any, Any]:
    """Import and return ``(Gtk, Adw, GLib, Gio)``.

    :raises MissingDependency: When any of them is unavailable, naming the
        Debian packages the user needs and the reason, rather than crashing with
        an ImportError inside a GTK callback.
    """
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, GLib, Gtk
    except (ImportError, ValueError) as error:
        raise MissingDependency(
            "the AgentOS desktop application needs GTK4 and libadwaita Python bindings.\n"
            "Install them with:\n"
            "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
            f"import error: {error}"
        ) from error
    return Gtk, Adw, GLib, Gio


def status_label(state: str) -> str:
    """Return the human label for a Core health state."""
    return {
        "ok": "Core connected",
        "unavailable": "Core unavailable",
        "error": "Core error",
    }.get(state, state)


class DesktopApplication:
    """Own the GTK application, its window, and its view stack.

    :param socket_path: Core IPC socket path.
    """

    def __init__(self, socket_path: str = SYSTEM_SOCKET, *, timeout_seconds: float = 20.0) -> None:
        self._socket_path = socket_path
        self._timeout = timeout_seconds
        self._ipc: AsyncIpc | None = None
        self._refresh_source: int | None = None

    def run(self, argv: list[str] | None = None) -> int:
        """Build and run the application.

        :returns: A process exit status.
        """
        Gtk, Adw, GLib, Gio = require_gtk()
        from .views import ActivityView, ApprovalsView, ChatView, SettingsView, SystemView, TasksView

        self._ipc = AsyncIpc(self._socket_path, timeout_seconds=self._timeout)

        class _Window(Adw.ApplicationWindow):  # type: ignore[misc]
            """The main window: a split view with the navigation sidebar."""

            def __init__(self, application: Any, ipc: AsyncIpc) -> None:
                super().__init__(application=application, title=PROJECT_NAME)
                self.set_default_size(1080, 720)
                self.set_size_request(720, 480)
                self.ipc = ipc
                self.views: dict[str, Any] = {}
                self.stack = Adw.ViewStack()
                self.status_label = Gtk.Label(label="Connecting…")
                self.status_label.add_css_class("dim-label")
                self.runtime_label = Gtk.Label(label="")
                self.runtime_label.add_css_class("dim-label")
                switch = Adw.ViewSwitcher()
                switch.set_stack(self.stack)
                switch.set_policy(Adw.ViewSwitcherPolicy.WIDE)
                header = Adw.HeaderBar()
                header.set_title_widget(switch)
                header.pack_start(self.status_label)
                header.pack_end(self.runtime_label)
                toolbar = Adw.ToolbarView()
                toolbar.add_top_bar(header)
                toolbar.set_content(self.stack)
                self.set_content(toolbar)
                for title, icon, view in (
                    ("Chat", "user-available-symbolic", ChatView(ipc)),
                    ("Tasks", "view-list-bullet-symbolic", TasksView(ipc)),
                    ("System", "utilities-system-monitor-symbolic", SystemView(ipc)),
                    ("Approvals", "dialog-password-symbolic", ApprovalsView(ipc)),
                    ("Activity", "document-open-recent-symbolic", ActivityView(ipc)),
                    ("Settings", "preferences-system-symbolic", SettingsView(ipc)),
                ):
                    page = self.stack.add_titled(view.widget, title.lower(), title)
                    page.set_icon_name(icon)
                    self.views[title.lower()] = view
                self.connect("close-request", self._on_close)

            def _on_close(self, *_args: Any) -> bool:
                """Hide instead of quitting, so the application can be reopened.

                The AgentOS design requires that closing the window does not stop
                a background task; hiding the window keeps the tray-less GNOME
                flow intact while the Core keeps working.
                """
                self.set_visible(False)
                return True

            def refresh(self) -> None:
                """Refresh the currently visible view."""
                current = self.stack.get_visible_child_name()
                view = self.views.get(current or "")
                if view is not None:
                    view.refresh()

            def set_status(self, state: str, runtime_text: str) -> None:
                """Apply a Core health state to the header."""
                self.status_label.set_label(status_label(state))
                for css in ("success", "warning", "error"):
                    self.status_label.remove_css_class(css)
                self.status_label.add_css_class({"ok": "success", "unavailable": "warning"}.get(state, "error"))
                self.runtime_label.set_label(runtime_text)

        class _Application(Adw.Application):  # type: ignore[misc]
            """The Adw application that owns the window and the IPC worker."""

            def __init__(self) -> None:
                super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
                self.window: Any = None

            def do_activate(self) -> None:  # noqa: N802 - GObject naming
                if self.window is None:
                    self.window = _Window(self, self.ipc)
                self.window.present()
                self._install_refresh()

            def _install_refresh(self) -> None:
                window = self.window
                if window is None or getattr(window, "did_install_refresh", False):
                    return
                window.did_install_refresh = True

                def tick() -> bool:
                    """Refresh the header and the visible view."""
                    self._refresh_status()
                    window.refresh()
                    return True

                GLib.timeout_add(int(STATUS_INTERVAL_SECONDS * 1000), tick)
                self._refresh_status()

            def _refresh_status(self) -> None:
                """Ask the Core for health and apply it to the header."""
                window = self.window
                if window is None:
                    return

                def ok(result: Any) -> None:
                    runtime = result.get("runtime", {}) if isinstance(result, dict) else {}
                    text = str(runtime.get("runtime_id") or "")
                    if runtime and runtime.get("available") is False:
                        text = f"{text} unavailable"
                    window.set_status("ok", text)

                def bad(error: AgentOSError) -> None:
                    window.set_status("unavailable", error.message.splitlines()[0][:60])

                window.ipc.call("health", {}, on_success=ok, on_error=bad)

        class _Application(Adw.Application):  # type: ignore[misc]
            """The Adw application that owns the window and the IPC worker.

            :param ipc: The shared IPC bridge; the application starts and stops it.
            """

            def __init__(self, ipc: AsyncIpc) -> None:
                super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
                self.ipc = ipc
                self.window: Any = None

        self._ipc.start()
        application = _Application(self._ipc)
        try:
            return int(application.run(argv or [sys.argv[0]]))
        finally:
            self._ipc.stop()


def main(argv: list[str] | None = None) -> int:
    """Run the desktop application.

    :param argv: Argument vector without the program name.
    :returns: A process exit status; ``3`` when GTK4 or libadwaita is missing.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="agentos-desktop",
        description=f"{PROJECT_NAME} desktop application — {PROJECT_TAGLINE}",
        epilog="Closing the window does not stop a running task; the Core service owns task lifetime.",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("AGENTOS_SOCKET", SYSTEM_SOCKET),
        help=f"Core IPC socket path (default: {SYSTEM_SOCKET})",
    )
    parser.add_argument("--version", action="version", version=f"{PROJECT_NAME} {PROJECT_VERSION}")
    parser.add_argument("--check", action="store_true", help="verify that the GUI dependencies are present and exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check:
        try:
            require_gtk()
        except MissingDependency as error:
            print(error, file=sys.stderr)
            return 3
        print("GTK4 and libadwaita bindings are available")
        return 0
    try:
        return DesktopApplication(args.socket).run()
    except MissingDependency as error:
        print(error, file=sys.stderr)
        return 3


__all__ = [
    "APPLICATION_ID",
    "STATUS_INTERVAL_SECONDS",
    "DesktopApplication",
    "MissingDependency",
    "main",
    "require_gtk",
    "status_label",
]
