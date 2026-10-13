"""Window views for the AgentOS desktop application.

Each view owns one stack page, exposes ``widget`` and ``refresh()``, and treats
every Core field as untrusted: a missing or mistyped field renders as an empty
value rather than an exception inside a GTK callback.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from ..constants import (
    DSH_LOCKED_COMMIT,
    DSH_LOCKED_VERSION,
    DSH_NODE_ENGINES,
    PROJECT_NAME,
    PROJECT_VERSION,
)
from ..errors import AgentOSError
from .ipc_async import AsyncIpc


def _require_gtk() -> tuple[Any, Any, Any]:
    """Import and return ``(Gtk, Adw, GLib)`` for the views."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk

    return Gtk, Adw, GLib


class BaseView:
    """Shared behaviour for every view: one widget, one refresh entry point.

    :param ipc: IPC bridge used for Core calls.
    :param title: Human title used in status messages.
    """

    def __init__(self, ipc: AsyncIpc, title: str) -> None:
        Gtk, Adw, GLib = _require_gtk()
        self._Gtk = Gtk
        self._Adw = Adw
        self._GLib = GLib
        self.ipc = ipc
        self.title = title
        self.status = Gtk.Label(label="")
        self.status.add_css_class("dim-label")
        self.status.set_xalign(0.0)
        self.status.set_wrap(True)

    def refresh(self) -> None:
        """Reload this view's data. Subclasses override."""

    def _set_status(self, message: str) -> None:
        """Show a short status line under the content."""
        self.status.set_label(message)

    def _report_error(self, error: AgentOSError) -> None:
        """Render a Core failure without raising inside a callback."""
        self._set_status(f"{error.code}: {error.message.splitlines()[0]}")


class ChatView(BaseView):
    """Conversation view: compose a prompt, watch the runtime stream its answer."""

    def __init__(self, ipc: AsyncIpc) -> None:
        super().__init__(ipc, "Chat")
        Gtk, Adw, GLib = self._Gtk, self._Adw, self._GLib
        self._task_id: str | None = None
        self._transcript = Gtk.TextView()
        self._transcript.set_editable(False)
        self._transcript.set_monospace(True)
        self._transcript.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._buffer = self._transcript.get_buffer()
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self._transcript)
        self._entry = Gtk.Entry()
        self._entry.set_hexpand(True)
        self._entry.set_placeholder_text("Ask AgentOS to inspect this system…")
        self._entry.connect("activate", self._on_send)
        self._level = Gtk.DropDown.new_from_strings(["P0 read-only", "P1 restricted", "P2 controlled"])
        self._level.set_selected(0)
        self._send = Gtk.Button(label="Send")
        self._send.add_css_class("suggested-action")
        self._send.connect("clicked", self._on_send)
        composer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        composer.append(self._entry)
        composer.append(self._level)
        composer.append(self._send)
        self._notice = Gtk.Label(
            label=(
                "A task keeps running when this window closes. The Core service owns its lifetime."
            )
        )
        self._notice.add_css_class("dim-label")
        self._notice.set_xalign(0.0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(scroller)
        content.append(self._notice)
        content.append(composer)
        content.append(self.status)
        self.widget = content

    def refresh(self) -> None:
        """Nothing to poll: the transcript is driven by events."""

    def _append(self, text: str) -> None:
        """Append a line to the transcript."""
        end = self._buffer.get_end_iter()
        self._buffer.insert(end, text + "\n")
        mark = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
        self._transcript.scroll_mark_onscreen(mark)

    def _on_send(self, *_args: Any) -> None:
        """Submit the composed prompt as a task."""
        prompt = self._entry.get_text().strip()
        if not prompt:
            return
        level = ["P0", "P1", "P2"][self._level.get_selected()]
        self._entry.set_text("")
        self._append(f"you> {prompt}")
        self._set_status("submitting…")

        def ok(result: Any) -> None:
            task = result.get("task", {}) if isinstance(result, dict) else {}
            self._task_id = task.get("task_id")
            self._append(f"agentos> task {self._task_id} queued (level cap {task.get('level_cap_label')})")
            self._set_status("task queued; streaming events")
            self.ipc.call("events.subscribe", {}, on_success=self._on_subscribed, on_error=self._report_error)

        self.ipc.call(
            "task.create",
            {"prompt": prompt, "level_cap": level},
            on_success=ok,
            on_error=self._report_error,
        )

    def _on_subscribed(self, result: Any) -> None:
        """Begin polling the subscription for this task's events."""
        token = str(result.get("token", ""))
        if not token:
            return

        def poll() -> bool:
            """Fetch and render any new events for this task."""
            self.ipc.call(
                "events.poll",
                {"token": token, "limit": 50},
                on_success=self._on_events,
                on_error=self._report_error,
            )
            return self._task_id is not None

        self._GLib.timeout_add(500, poll)

    def _on_events(self, result: Any) -> None:
        """Render a batch of events related to this conversation."""
        for item in result.get("events", []):
            event = item.get("event", {}) if isinstance(item, Mapping) else {}
            if event.get("task_id") not in {self._task_id, None}:
                continue
            event_type = str(event.get("event_type", ""))
            payload = event.get("payload", {}) if isinstance(event.get("payload"), Mapping) else {}
            if event_type == "runtime.message_delta":
                self._append(str(payload.get("text", "")))
            elif event_type == "policy.decision":
                self._append(
                    f"policy> {payload.get('tool')} -> {payload.get('decision')}: {payload.get('reason')}"
                )
            elif event_type == "approval.requested":
                self._append(
                    f"approval> {payload.get('operation')} on {payload.get('target')} "
                    f"(decide it in the Approvals view)"
                )
            elif event_type in {"task.completed", "task.failed", "task.cancelled"}:
                self._append(f"agentos> {event_type}")
                self._set_status(event_type)
                self._task_id = None
                return


class TasksView(BaseView):
    """Task list: state, title, runtime, and a cancel action for active tasks."""

    def __init__(self, ipc: AsyncIpc) -> None:
        super().__init__(ipc, "Tasks")
        Gtk, Adw, GLib = self._Gtk, self._Adw, self._GLib
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self._list)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(scroller)
        content.append(self.status)
        self.widget = content

    def refresh(self) -> None:
        """Reload the task list."""
        self.ipc.call("task.list", {"limit": 50}, on_success=self._on_tasks, on_error=self._report_error)

    def _clear(self) -> None:
        """Remove every row."""
        while True:
            row = self._list.get_first_child()
            if row is None:
                break
            self._list.remove(row)

    def _on_tasks(self, result: Any) -> None:
        """Render the task list."""
        self._clear()
        tasks = result.get("tasks", []) if isinstance(result, dict) else []
        counts = result.get("by_state", {}) if isinstance(result, dict) else {}
        self._set_status(
            "  ".join(f"{state}: {count}" for state, count in sorted(counts.items()) if count)
            or "no tasks yet"
        )
        if not tasks:
            row = self._Adw.ActionRow(title="No tasks")
            row.set_subtitle("Submit one from the Chat view.")
            self._list.append(row)
            return
        for task in tasks:
            row = self._Adw.ActionRow(title=str(task.get("title") or task.get("task_id")))
            row.set_subtitle(
                f"{task.get('state')} · {task.get('runtime_id')} · {task.get('level_cap_label')} · "
                f"{task.get('created_at')}"
            )
            if task.get("error_message"):
                row.set_subtitle(f"{row.get_subtitle()} — {task.get('error_message')}")
            if task.get("state") in {"QUEUED", "RUNNING", "WAITING_APPROVAL", "RECOVERING"}:
                button = Gtk.Button(label="Cancel")
                button.set_valign(Gtk.Align.CENTER)
                button.connect("clicked", self._on_cancel, str(task.get("task_id")))
                row.add_suffix(button)
            self._list.append(row)

    def _on_cancel(self, _button: Any, task_id: str) -> None:
        """Request cancellation of one task."""
        self.ipc.call(
            "task.cancel",
            {"task_id": task_id},
            on_success=lambda result: self._set_status(
                f"task {task_id} is now {result.get('task', {}).get('state')}"
            ),
            on_error=self._report_error,
        )


class SystemView(BaseView):
    """Read-only system view: CPU, memory, disk, and the highest-load processes."""

    def __init__(self, ipc: AsyncIpc) -> None:
        super().__init__(ipc, "System")
        Gtk, Adw, GLib = self._Gtk, self._Adw, self._GLib
        self._summary = Gtk.Label()
        self._summary.set_xalign(0.0)
        self._summary.set_wrap(True)
        self._filesystems = Gtk.ListBox()
        self._filesystems.set_selection_mode(Gtk.SelectionMode.NONE)
        self._processes = Gtk.ListBox()
        self._processes.set_selection_mode(Gtk.SelectionMode.NONE)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(self._summary)
        content.append(self._section("Filesystems"))
        content.append(self._filesystems)
        content.append(self._section("Highest CPU processes"))
        content.append(self._processes)
        content.append(self.status)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(content)
        self.widget = scroller

    def _section(self, text: str) -> Any:
        """Return a bold section heading."""
        label = self._Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.add_css_class("heading")
        return label

    def refresh(self) -> None:
        """Reload the snapshot. The Core samples deltas between calls."""
        self.ipc.call("system.snapshot", {}, on_success=self._on_snapshot, on_error=self._report_error)

    def _on_snapshot(self, result: Any) -> None:
        """Render the snapshot."""
        if not isinstance(result, dict):
            return
        cpu = result.get("cpu", {})
        memory = result.get("memory", {})
        self._summary.set_label(
            f"CPU {cpu.get('utilised_percent')}% over {cpu.get('core_count')} cores "
            f"(load {cpu.get('load_average_1m')})\n"
            f"Memory {memory.get('used_percent')}% used "
            f"({memory.get('used_mib')} of {memory.get('total_mib')} MiB), "
            f"swap {memory.get('swap_used_percent')}% of {memory.get('swap_total_mib')} MiB\n"
            f"Uptime {result.get('uptime_seconds')}s"
        )
        _fill_rows(self._filesystems, self._Adw, [
            (
                str(item.get("mount_point")),
                f"{item.get('used_percent')}% of {item.get('total_gib')} GiB · {item.get('filesystem_type')}",
            )
            for item in result.get("filesystems", [])
        ], "No filesystems reported")
        _fill_rows(self._processes, self._Adw, [
            (
                f"{item.get('name')} (pid {item.get('pid')})",
                f"{item.get('cpu_percent')}% CPU · {item.get('rss_mib')} MiB RSS · {item.get('state')}",
            )
            for item in result.get("top_processes", [])
        ], "No processes reported")
        self._set_status("read-only snapshot from /proc and statvfs")


class ApprovalsView(BaseView):
    """Approval queue: the exact operation, target, parameters, and effect."""

    def __init__(self, ipc: AsyncIpc) -> None:
        super().__init__(ipc, "Approvals")
        Gtk, Adw, GLib = self._Gtk, self._Adw, self._GLib
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self._list)
        self._explanation = Gtk.Label(
            label=(
                "Approving authorizes exactly the operation, target, and parameters shown here, once. "
                "Changing any of them requires a new approval. AgentOS never executes an operation "
                "that was not approved as shown."
            )
        )
        self._explanation.set_xalign(0.0)
        self._explanation.set_wrap(True)
        self._explanation.add_css_class("dim-label")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(self._explanation)
        content.append(scroller)
        content.append(self.status)
        self.widget = content

    def refresh(self) -> None:
        """Reload the approval queue."""
        self.ipc.call(
            "approval.list",
            {"states": ["pending", "granted"], "limit": 50},
            on_success=self._on_approvals,
            on_error=self._report_error,
        )

    def _clear(self) -> None:
        """Remove every row."""
        while True:
            row = self._list.get_first_child()
            if row is None:
                break
            self._list.remove(row)

    def _on_approvals(self, result: Any) -> None:
        """Render the approval rows with their bound parameters."""
        self._clear()
        approvals = result.get("approvals", []) if isinstance(result, dict) else []
        self._set_status(f"{len(approvals)} approval(s) awaiting a decision" if approvals else "nothing awaiting a decision")
        if not approvals:
            row = self._Adw.ActionRow(title="No pending approvals")
            row.set_subtitle("A P2 operation appears here before it can run.")
            self._list.append(row)
            return
        for approval in approvals:
            parameters = approval.get("parameters", {})
            row = self._Adw.ExpanderRow(title=str(approval.get("operation")))
            row.set_subtitle(
                f"target {approval.get('target')} · level {approval.get('level')} · "
                f"state {approval.get('state')} · expires {approval.get('expires_at')}"
            )
            for key, value in sorted(parameters.items()):
                detail = self._Adw.ActionRow(title=str(key), subtitle=str(value))
                detail.set_activatable(False)
                row.add_row(detail)
            effect = self._Adw.ActionRow(
                title="Effect", subtitle=str(approval.get("effect") or "not stated")
            )
            effect.set_activatable(False)
            row.add_row(effect)
            digest = self._Adw.ActionRow(
                title="Parameter digest", subtitle=str(approval.get("parameter_digest"))
            )
            digest.set_activatable(False)
            row.add_row(digest)
            if approval.get("state") == "pending":
                grant = Gtk.Button(label="Approve")
                grant.add_css_class("suggested-action")
                grant.set_valign(Gtk.Align.CENTER)
                grant.connect("clicked", self._on_decide, str(approval.get("approval_id")), True)
                deny = Gtk.Button(label="Deny")
                deny.add_css_class("destructive-action")
                deny.set_valign(Gtk.Align.CENTER)
                deny.connect("clicked", self._on_decide, str(approval.get("approval_id")), False)
                row.add_suffix(deny)
                row.add_suffix(grant)
            else:
                revoke = Gtk.Button(label="Revoke")
                revoke.set_valign(Gtk.Align.CENTER)
                revoke.connect("clicked", self._on_revoke, str(approval.get("approval_id")))
                row.add_suffix(revoke)
            self._list.append(row)

    def _on_decide(self, _button: Any, approval_id: str, grant: bool) -> None:
        """Record a human decision on one approval."""
        self.ipc.call(
            "approval.decide",
            {"approval_id": approval_id, "grant": grant},
            on_success=lambda result: self._after_decision(result, grant),
            on_error=self._report_error,
        )

    def _after_decision(self, result: Any, grant: bool) -> None:
        """Report the decision and refresh the queue."""
        approval = result.get("approval", {}) if isinstance(result, dict) else {}
        state = approval.get("state")
        if grant:
            self._set_status(
                f"{approval.get('approval_id')} is {state}; the nonce is single-use and bound to this "
                "target and parameter set"
            )
        else:
            self._set_status(f"{approval.get('approval_id')} is {state}")
        self.refresh()

    def _on_revoke(self, _button: Any, approval_id: str) -> None:
        """Revoke a granted approval."""
        self.ipc.call(
            "approval.revoke",
            {"approval_id": approval_id},
            on_success=lambda result: self._set_status(
                f"{approval_id} is {result.get('approval', {}).get('state')}"
            ),
            on_error=self._report_error,
        )


class ActivityView(BaseView):
    """Audit trail and recent events, newest first."""

    def __init__(self, ipc: AsyncIpc) -> None:
        super().__init__(ipc, "Activity")
        Gtk, Adw, GLib = self._Gtk, self._Adw, self._GLib
        self._audit = Gtk.ListBox()
        self._audit.set_selection_mode(Gtk.SelectionMode.NONE)
        self._events = Gtk.ListBox()
        self._events.set_selection_mode(Gtk.SelectionMode.NONE)
        self._summary = Gtk.Label()
        self._summary.set_xalign(0.0)
        self._summary.set_wrap(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(self._summary)
        heading = Gtk.Label(label="Audit trail")
        heading.set_xalign(0.0)
        heading.add_css_class("heading")
        content.append(heading)
        content.append(self._audit)
        heading = Gtk.Label(label="Recent events")
        heading.set_xalign(0.0)
        heading.add_css_class("heading")
        content.append(heading)
        content.append(self._events)
        content.append(self.status)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(content)
        self.widget = scroller

    def refresh(self) -> None:
        """Reload the audit summary, the audit trail, and recent events."""
        self.ipc.call("audit.summary", {}, on_success=self._on_summary, on_error=self._report_error)
        self.ipc.call(
            "audit.query", {"limit": 40}, on_success=self._on_audit, on_error=self._report_error
        )
        self.ipc.call(
            "events.recent", {"limit": 20}, on_success=self._on_events, on_error=self._report_error
        )

    def _on_summary(self, result: Any) -> None:
        """Render audit counts."""
        if not isinstance(result, dict):
            return
        kinds = ", ".join(f"{key}: {value}" for key, value in sorted(result.get("by_kind", {}).items()))
        self._summary.set_label(f"total {result.get('total')} records — {kinds or 'none'}")

    def _on_audit(self, result: Any) -> None:
        """Render the audit trail."""
        rows = [
            (
                f"{record.get('kind')} · {record.get('outcome')}",
                f"{record.get('timestamp')} · {record.get('operation') or record.get('action')}"
                + (f" · target {record.get('target')}" if record.get("target") else "")
                + (f" · uid {record.get('actor_uid')}" if record.get("actor_uid") is not None else "")
                + (f"\n{record.get('reason')}" if record.get("reason") else ""),
            )
            for record in (result.get("records", []) if isinstance(result, dict) else [])
        ]
        _fill_rows(self._audit, self._Adw, rows, "No audit records")

    def _on_events(self, result: Any) -> None:
        """Render recent events."""
        rows = [
            (
                str(event.get("event_type")),
                f"{event.get('timestamp')} · {event.get('severity')} · "
                + json.dumps(event.get("payload", {}), ensure_ascii=False)[:200],
            )
            for event in (result.get("events", []) if isinstance(result, dict) else [])
        ]
        _fill_rows(self._events, self._Adw, rows, "No events")


class SettingsView(BaseView):
    """Deployment facts and the effective configuration, rendered read-only."""

    def __init__(self, ipc: AsyncIpc) -> None:
        super().__init__(ipc, "Settings")
        Gtk, Adw, GLib = self._Gtk, self._Adw, self._GLib
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        note = Gtk.Label(
            label=(
                "These values are owned by /etc/agentos/agentos.toml and the policy directory. "
                "Change them with an editor and reload the service with 'systemctl reload agentos-core'. "
                "Model credentials are never shown here: they live in /etc/agentos/credentials.env."
            )
        )
        note.set_xalign(0.0)
        note.set_wrap(True)
        note.add_css_class("dim-label")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.append(note)
        content.append(self._list)
        content.append(self.status)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(content)
        self.widget = scroller

    def refresh(self) -> None:
        """Reload runtime capabilities and the policy inventory."""
        self._set_status("loading…")
        self.ipc.call("policy.inventory", {}, on_success=self._on_policy, on_error=self._report_error)

    def _on_policy(self, result: Any) -> None:
        """Render identity, runtime facts, and the policy inventory."""
        rows: list[tuple[str, str]] = [
            (PROJECT_NAME, f"version {PROJECT_VERSION}"),
            ("Locked DSH revision", f"{DSH_LOCKED_VERSION} ({DSH_LOCKED_COMMIT[:12]})"),
            ("DSH Node.js requirement", DSH_NODE_ENGINES),
        ]
        if isinstance(result, dict):
            policies = result.get("policies", [])
            rows.append(
                (
                    "Policy documents",
                    ", ".join(str(policy.get("name")) for policy in policies)
                    or "none loaded — only P0 read-only tools are permitted",
                )
            )
            units = result.get("service_unit_allowlist", [])
            rows.append(("Restartable units", ", ".join(str(unit) for unit in units) or "none configured"))
            for tool in result.get("tools", []):
                approval = "approval required" if tool.get("requires_approval") else "no approval"
                rows.append(
                    (
                        str(tool.get("name")),
                        f"{tool.get('level_label')} · {approval}"
                        + (f" · rule {tool.get('rule_name')}" if tool.get("rule_name") else " · no rule")
                        + (" · DENIED" if tool.get("denied") else ""),
                    )
                )
        _fill_rows(self._list, self._Adw, rows, "No configuration reported")
        self._set_status("read-only; edit /etc/agentos/agentos.toml to change these")


def _fill_rows(listbox: Any, Adw: Any, rows: list[tuple[str, str]], empty_text: str) -> None:
    """Replace the contents of ``listbox`` with action rows."""
    while True:
        child = listbox.get_first_child()
        if child is None:
            break
        listbox.remove(child)
    if not rows:
        row = Adw.ActionRow(title=empty_text)
        listbox.append(row)
        return
    for title, subtitle in rows:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_activatable(False)
        listbox.append(row)


__all__ = [
    "ActivityView",
    "ApprovalsView",
    "BaseView",
    "ChatView",
    "SettingsView",
    "SystemView",
    "TasksView",
]
