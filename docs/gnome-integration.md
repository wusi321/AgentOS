# GNOME integration

AgentOS integrates with GNOME in two layers: a Shell extension that puts an entry
in the top bar, and a native GTK4 application that does the work. The extension is
deliberately the least capable component in the system.

## Layer 1: the GNOME Shell extension

| Fact | Value |
|---|---|
| UUID | `agentos@agentos.dev` |
| Supported Shell versions | 42, 43, 44 |
| Installed to | `/usr/share/gnome-shell/extensions/agentos@agentos.dev/` |
| Files | `extension.js`, `utils.js`, `prefs.js`, `stylesheet.css`, `metadata.json`, `schemas/` |

### What it does

* a top-bar entry with a symbolic icon and an active-task count;
* a menu with the core and runtime status, an **Open AgentOS** item, the active
  tasks, the approvals waiting on a decision, and the most recent events;
* GNOME notifications for a task failure and for a new approval request, throttled
  to at most one per 30 seconds globally and to at most one per
  `(event_type, task_id)` every 5 minutes;
* a preferences window with five UI settings.

### What it must never do

These are enforced by checks in `scripts/verify-debs.sh`, which fail the release
if the shipped extension contains them:

| Rule | Check |
|---|---|
| no shell: no `imports.misc.util`, no `GLib.spawn_*`, no `Gio.Subprocess` | `the extension imports no shell helper`, `the extension has no subprocess API` |
| no file access: no `Gio.File`, no file read helper | `the extension reads no file` |
| never approves anything | `the extension never approves anything` |
| no secret in the package | `the extension does not ship a secret` |
| a dead service must not break the Shell | every D-Bus call is wrapped; a failure renders an unavailable state |

Clicking an approval row opens the desktop application. It never decides the
approval: deciding requires showing the exact operation, target, and parameters,
and that is the desktop window's job.

### Settings

| Key | Type | Default | Meaning |
|---|---|---|---|
| `poll-interval-seconds` | uint, 2–60 | 5 | status poll interval |
| `show-task-count-in-panel` | boolean | true | show the count next to the icon |
| `notify-on-task-failure` | boolean | true | notify when a task fails |
| `notify-on-approval` | boolean | true | notify when an approval is requested |
| `max-recent-events` | uint, 1–10 | 5 | rows in the recent-activity section |

## Layer 2: the session broker

`agentos-session` runs once per graphical login as a systemd **user** service and
owns `org.agentos.Session1` on the session bus. It exists so that the extension
never speaks the system service's protocol, and so that nothing in the Shell needs
to know how Core works.

### D-Bus surface

| Member | Signature | Purpose |
|---|---|---|
| `OpenDesktop()` | `→ b` | start or focus the desktop application |
| `GetStatus()` | `→ ss` | `(state, payload_json)` where state is `ok`, `unavailable`, or `error` |
| `Subscribe()` | `→ s` | register a subscriber, returning a token |
| `Unsubscribe(s token)` | `→` | drop a subscriber |
| `Event(s token, s event_json)` | signal | deliver one Core event to one subscriber |

The broker also serves the same introspection data through `gdbus`:

```sh
gdbus introspect --session --dest org.agentos.Session1 --object-path /org/agentos/Session1
gdbus call --session --dest org.agentos.Session1 --object-path /org/agentos/Session1 \
      --method org.agentos.Session1.GetStatus
```

### The status document

```json
{
  "core_available": true,
  "runtime_id": "mock",
  "runtime_available": true,
  "runtime_detail": "mock runtime ready",
  "tasks": {"QUEUED": 0, "RUNNING": 1, "WAITING_APPROVAL": 1, "SUCCEEDED": 4},
  "active_tasks": [{"task_id": "…", "title": "…", "state": "RUNNING"}],
  "pending_approvals": [
    {"approval_id": "…", "operation": "system.service.restart", "target": "cups.service",
     "reason": "…", "state": "pending"}
  ],
  "recent_events": [{"event_type": "task.failed", "task_id": "…", "severity": "error",
                     "summary": "…", "timestamp": "…"}]
}
```

Every field is optional as far as the extension is concerned: a missing field
renders as empty rather than raising, because a Shell extension that throws takes
the Shell's stability with it.

The broker holds no credential and performs no privileged action. Its only verb
besides reporting is "start the desktop application".

## Layer 3: the desktop application

`agentos-desktop` is a GTK4 and libadwaita window with six views, reachable from
the application grid or the panel entry.

| View | Contents |
|---|---|
| Chat | compose a prompt, choose a privilege cap, watch the runtime stream its answer and its tool calls |
| Tasks | every task with its state, runtime, cap, and a cancel action |
| System | CPU, memory, swap, uptime, filesystems, and the highest-CPU processes; read-only |
| Approvals | the pending queue with the operation, target, parameters, digest, and the plain statement of effect, plus Approve, Deny, and Revoke |
| Activity | the audit trail and the recent events |
| Settings | the effective policy inventory, the locked DSH revision, and the runtime facts; read-only |

Design rules the application follows:

* **It never decides policy.** It asks Core and shows the answer.
* **It never executes anything.** There is no system call in the desktop package
  beyond starting itself.
* **Closing the window does not stop a task.** The window hides; Core owns task
  lifetime.
* **No credential is ever displayed or stored.**
* **Blocking work never touches the main loop.** Every Core call goes through
  `AsyncIpc`, one worker thread with its own connection, and results come back
  through `GLib.idle_add`.

## Installing and enabling

```sh
sudo apt install agentos-meta

# The panel entry appears only after the extension is enabled in your own session.
gnome-extensions enable agentos@agentos.dev

# Log out and back in. On Xorg, Alt+F2 and `r` also works.
```

A package cannot enable a Shell extension for a user: extension state is per-user
dconf, and a postinst runs outside any session. The packages therefore print the
exact command rather than pretending to do it. This is the behaviour the design
outline requires, and it is why the install emits a message instead of an error.

Verify it afterwards:

```sh
gnome-extensions info agentos@agentos.dev      # state: ENABLED
systemctl --user status agentos-session        # active (running)
agentosctl hello                               # reports your kernel-recorded uid
```

## Testing the extension

The extension cannot be exercised by the automated suite, because that suite does
not run a GNOME Shell. What is checked automatically:

* the shipped files live in a directory named exactly after the UUID;
* `metadata.json` declares the UUID the package installs it under, and Shell 42;
* the settings schema id in `metadata.json` appears in the schema file;
* the schema compiles with `glib-compile-schemas --strict --dry-run`;
* the security checks in the table above;
* the desktop entry is valid.

What must be tested on a real GNOME 42 desktop, and is therefore on the
verification checklist in `docs/packaging.md`:

1. enabling, disabling, and re-enabling the extension;
2. restarting the Shell with the extension enabled, on Wayland and on Xorg;
3. logging out and back in, and confirming the panel re-synchronises;
4. stopping `agentos-core` and confirming the panel degrades to a message without
   disturbing the Shell;
5. stopping `agentos-session` and confirming the same;
6. confirming the panel entry opens the desktop application;
7. confirming a task failure and an approval request each produce one notification,
   and that a repeated failure does not produce a second one within five minutes.

## Known limitations

* GNOME Shell 45 and later changed the extension API substantially (ESM modules,
  a different extension object). This release targets 42–44 and declares exactly
  that range, so a newer Shell refuses the extension rather than half-loading it.
* The extension shows the five most recent events and the approvals awaiting a
  decision. Deeper history is the desktop application's Activity view.
* Notification throttling is per Shell session and in memory: restarting the Shell
  clears the history, so one repeated alert may appear again after a Shell restart.
