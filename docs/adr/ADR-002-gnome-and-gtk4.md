# ADR-002: A GNOME Shell extension plus a GTK4/libadwaita application

**Status:** accepted
**Date:** 2026-10-13

## Context

AgentOS needs a presence in the desktop: a way to see that the agent exists and
what it is doing, to be interrupted when a decision is needed, and to hold a
conversation. The first release targets Ubuntu 22.04 Desktop with GNOME Shell 42.

## Options considered

1. **Fork GNOME Shell.** Full control over the panel and the session experience.
2. **A Shell extension plus a separate native application.** The extension is thin;
   the application does the work.
3. **A cross-platform toolkit application, for example PySide6.** One codebase for
   every desktop, no Shell integration.
4. **A Web UI in a browser or an Electron shell.** Fast to build, alien to the
   desktop.

## Decision

Option 2. A GJS Shell extension in the top bar, and a GTK4 + libadwaita
application for everything substantial.

## Reasoning

* Forking GNOME Shell means merging every GNOME security update by hand. For a
  product whose value proposition is a safe agent, that is the wrong kind of work.
* A Shell extension runs inside the Shell process, so a fault there degrades the
  user's desktop. Keeping it to presentation and one D-Bus call makes that risk
  small and bounded, which the threat model depends on.
* GTK4 and libadwaita give the platform's own behaviour for windows, notifications,
  theming, settings widgets, and accessibility for free. A toolkit that fights the
  desktop platform costs more than it saves.
* A Web UI would need an HTTP endpoint, an authentication story, and CSRF and
  origin handling. The design explicitly avoids opening a network-facing service,
  and a browser UI would contradict that for no user benefit.

## Security impact

**Positive.** The most exposed component — the extension, which runs inside the
Shell — holds no authority at all. It reads a status document and asks for a
window to be opened. The desktop application runs as the user, holds no secret, and
never decides policy. Concentrating authority in one unprivileged system service is
what makes the trust story explicable.

**Negative.** Three components instead of one means three things to keep working.
The contract between them (the session-bus status document) is therefore documented
in `docs/gnome-integration.md` and treated as a public interface.

## Maintenance cost

Moderate, and concentrated at the boundaries rather than in the code:

* the extension must be reviewed against each new GNOME Shell major version, which
  is why `metadata.json` declares 42–44 and nothing else;
* GTK4 and libadwaita API drift affects the views, which is why the GTK imports
  happen inside `require_gtk()` rather than at module import: a machine without
  libadwaita gets a clear message instead of an import traceback.

## Review trigger

Revisit if libadwaita's development pace outruns the target Ubuntu release, or if a
required integration genuinely needs a Shell-side change that no public extension
API provides. In the second case, add a new ADR rather than editing this one.
