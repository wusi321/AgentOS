# AgentOS GNOME Shell extension

`agentos@agentos.dev` — a top-bar indicator that shows what the AgentOS agent
runtime is doing and gives you one-click access to the AgentOS desktop
application.

Supported Shell versions: **GNOME 42, 43 and 44** (Ubuntu 22.04 LTS ships
GNOME 42, which is the primary target).

---

## 1. Scope

The extension is a **read-only status client and a launcher**. It:

* renders core/runtime availability, active task counts, pending approvals and
  recent activity in the GNOME Shell panel;
* raises a desktop notification when a task **fails** or when a task is
  **waiting for approval**;
* asks the AgentOS session broker to launch or focus the AgentOS desktop
  application (`OpenDesktop()`), which is where every real action happens.

It is deliberately **not** able to do anything else. See section 3.

### What the user sees

| Panel element | Meaning |
| --- | --- |
| Icon (`utilities-system-monitor-symbolic`) | AgentOS indicator; falls back to `dialog-information-symbolic` when the icon theme lacks it. |
| Label `AgentOS 2` | Number of active tasks (queued + running + approval-waiting). Hidden when the count is 0 or when the preference is turned off. |
| Menu header | "AgentOS Core ok · runtime mock ready" (green) or "AgentOS service unavailable" / "AgentOS Core offline" (red). |
| `Open AgentOS` | Calls the broker's `OpenDesktop()`. |
| `Active tasks` submenu | One row per active task: `title — STATE`; `No active tasks` when empty. |
| `Waiting for approval` submenu | One row per pending approval: `operation on target`. Clicking opens the desktop app — the extension never approves. |
| `Recent activity` submenu | Up to `max-recent-events` events, severity-prefixed (`!`, `▲`, `·`). |

Status is polled every `poll-interval-seconds` (default 5) **and** refreshed
immediately whenever the broker delivers an `Event` signal. At most one
`GetStatus()` call is in flight at a time.

---

## 2. Interfaces used

Two D-Bus entry points on the **user session bus**. No other IPC, no sockets, no
files, no network.

### `org.agentos.Session1` (session broker)

* Bus name: `org.agentos.Session1`
* Object path: `/org/agentos/Session1`
* Interface: `org.agentos.Session1`

| Member | Signature | Use |
| --- | --- | --- |
| `OpenDesktop` | `() -> ()` | Launch or focus the AgentOS desktop application. |
| `GetStatus` | `() -> (s state, s payload_json)` | Status snapshot. `state` is `ok`, `unavailable` or `error`; anything other than `ok` is rendered as "AgentOS service unavailable" and is never treated as a failure of the Shell. |
| `Subscribe` | `() -> (s token)` | Obtain the token that authenticates our event stream. |
| `Unsubscribe` | `(s token) -> ()` | Release the token during `disable()`. |
| `Event` (signal) | `(s token, s event_json)` | Push notification of a task/approval change. Handled only when `token` equals our own subscription token. |

`GetStatus()` payload (JSON, treated as untrusted input, every field
defensive-defaulted):

```json
{
  "core_available": true,
  "runtime_id": "mock",
  "runtime_available": true,
  "runtime_detail": "mock runtime ready",
  "tasks": {"QUEUED": 0, "RUNNING": 1, "WAITING_APPROVAL": 1,
            "SUCCEEDED": 4, "FAILED": 0, "CANCELLED": 0},
  "active_tasks": [{"task_id": "…", "title": "…", "state": "RUNNING"}],
  "pending_approvals": [{"approval_id": "…", "operation": "system.service.restart",
                         "target": "cups.service", "reason": "…"}],
  "recent_events": [{"event_type": "task.failed", "task_id": "…",
                     "severity": "error", "summary": "…"}]
}
```

### `org.gnome.shell.extensions.agentos` (GSettings)

Local UI state, declared in
`schemas/org.gnome.shell.extensions.agentos.gschema.xml`:

| Key | Type | Default | Range |
| --- | --- | --- | --- |
| `poll-interval-seconds` | `u` | 5 | 2–60 |
| `show-task-count-in-panel` | `b` | true | — |
| `notify-on-task-failure` | `b` | true | — |
| `notify-on-approval` | `b` | true | — |
| `max-recent-events` | `u` | 5 | 1–10 |

`prefs.js` binds these keys two-way to an Adwaita (Gtk4) preferences page.
The extension re-reads `poll-interval-seconds` when the key changes (it connects
to `changed::poll-interval-seconds` in `enable()` and disconnects in
`disable()`), and also re-checks it on every timer tick as a safety net.

**Notification throttling.** At most one notification per 30 seconds, and a
given signature (`event_type` + `task_id`, or `approval_id`) is never repeated
within 5 minutes. The signature map lives in memory only; nothing is persisted.

---

## 3. Security constraints

These are hard requirements, not preferences. Any change that violates them is a
bug:

1. **No shell commands.** The extension never imports `imports.misc.util`, never
   calls `GLib.spawn_*`, never runs `bash -c`, and never evaluates a string as
   code. It has no code path that executes anything.
2. **No file access.** It never opens, reads, writes or stats a file. The only
   persistent state is GSettings UI state (see the table above).
3. **No secrets.** It stores no model API keys, no audit database, no tokens
   belonging to AgentOS Core. The D-Bus subscription token returned by
   `Subscribe()` is held in memory for the lifetime of the session and is
   discarded on `disable()`; it is not a credential and is not written anywhere.
4. **No approvals.** The extension cannot approve, deny, cancel or modify a
   task. Those controls exist only in the AgentOS desktop application. Clicking
   an approval row calls `OpenDesktop()` and nothing else.
5. **Shell stability.** A dead, missing or misbehaving broker must never break
   GNOME Shell. Every D-Bus call is asynchronous and wrapped in `try`/`catch`;
   every reply handler runs inside a guard; a failure turns into the visible
   non-fatal state "AgentOS service unavailable" — the menu still opens and the
   `Open AgentOS` action still starts the desktop application.
   `enable()`/`disable()` are paired and reversible: `disable()` removes every
   signal subscription, timeout source, GSettings handler and actor the
   extension added.

---

## 4. Files

| File | Purpose |
| --- | --- |
| `metadata.json` | UUID, name, `shell-version` (`42`, `43`, `44`), `settings-schema`. |
| `extension.js` | Panel indicator, D-Bus client, notifications, enable/disable. |
| `prefs.js` | Adwaita/Gtk4 preferences window (`fillPreferencesWindow`). |
| `utils.js` | Untrusted-input coercion, JSON parsing, formatting helpers. |
| `stylesheet.css` | Theme-aware CSS (`agentos-panel-label`, `agentos-status-ok`, `agentos-status-bad`, `agentos-approval-item`, `agentos-event-warning`, `agentos-event-error`). |
| `schemas/…gschema.xml` | GSettings schema `org.gnome.shell.extensions.agentos`. |
| `README.md` | This document. |

The JavaScript deliberately uses the classic `imports.gi.*` / `imports.ui.*`
module style with `var`/`const` and `class … extends PanelMenu.Button`, because
ESModules are not reliable on GNOME 42. Optional chaining (`?.`) and nullish
coalescing (`??`) are avoided for the same reason (GJS 42 predates them).

---

## 5. Installing on Ubuntu 22.04 (GNOME 42)

### From the AgentOS package (normal path)

The `agentos-desktop` package installs the extension tree into the system
extension directory:

```
/usr/share/gnome-shell/extensions/agentos@agentos.dev/
├── extension.js
├── metadata.json
├── prefs.js
├── stylesheet.css
├── utils.js
└── schemas/org.gnome.shell.extensions.agentos.gschema.xml
```

It also compiles the schema into
`/usr/share/glib-2.0/schemas/gschemas.compiled`. If you copied the files by
hand, do that step yourself:

```bash
sudo cp -r gnome-extension/. /usr/share/gnome-shell/extensions/agentos@agentos.dev/
sudo cp /usr/share/gnome-shell/extensions/agentos@agentos.dev/schemas/*.gschema.xml \
        /usr/share/glib-2.0/schemas/
sudo glib-compile-schemas /usr/share/glib-2.0/schemas/
```

Then enable it:

```bash
gnome-extensions enable agentos@agentos.dev
```

On Ubuntu 22.04 with GNOME 42 (Xorg **or** Wayland), a newly installed extension
is not visible to the already-running Shell. Log out and log back in, or — on
Xorg only — restart the Shell with `Alt`+`F2`, then `r`, then `Enter`. Wayland
sessions must be restarted; there is no in-place Shell reload.

Verify the state:

```bash
gnome-extensions info agentos@agentos.dev      # State: ENABLED
gnome-extensions prefs agentos@agentos.dev     # open the preferences window
```

### Development install (no root)

```bash
# Copy into the per-user directory instead
UUID=agentos@agentos.dev
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"
mkdir -p "$DEST"
cp -r gnome-extension/. "$DEST/"
glib-compile-schemas "$DEST/schemas/"
gnome-extensions enable "$UUID"
```

Watch the Shell log while developing:

```bash
journalctl -f -o cat /usr/bin/gnome-shell | grep -i agentos
```

### Uninstalling

```bash
gnome-extensions disable agentos@agentos.dev
sudo rm -rf /usr/share/gnome-shell/extensions/agentos@agentos.dev
sudo rm -f /usr/share/glib-2.0/schemas/org.gnome.shell.extensions.agentos.gschema.xml
sudo glib-compile-schemas /usr/share/glib-2.0/schemas/
```

---

## 6. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Menu header says "AgentOS service unavailable" | The session broker `org.agentos.Session1` is not on your session bus. Start the AgentOS desktop session, or use `Start AgentOS` in the menu. The Shell stays healthy either way. |
| Header says "AgentOS Core offline" | The broker answered but AgentOS Core is not running (`core_available: false`). Start the Core service. |
| Header says "runtime unavailable" | Core is up but no task runtime is registered. Check `runtime_detail` in the AgentOS desktop app. |
| Counters do not update | Increase nothing; the extension polls every `poll-interval-seconds`. If the number never changes, `GetStatus()` is failing — check the journal for `[AgentOS]`. |
| No notifications | Check the two notification switches in the preferences window, and remember the 30-second / 5-minute throttles. |
| Preferences window is empty | The GSettings schema is not installed; see section 5 and run `glib-compile-schemas`. |

---

## 7. License

GPL-2.0-or-later, matching the AgentOS project.
