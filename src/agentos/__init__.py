"""AgentOS: An Agent-Native Operating Environment for Linux.

The package is organized by authority boundary rather than by layer:

``agentos.runtime``
    The AgentRuntime protocol and the adapters that implement it.
``agentos.system_bridge``
    Read-only system facts and the small set of controlled write operations.
``agentos.policy``
    Capability manifests, rules, approvals, and the authorization engine.
``agentos.audit``
    Append-only records of decisions and actions.
``agentos.tasks``
    The task state machine, durable records, and the task manager.
``agentos.core``
    Configuration, IPC, and the Core service host that ties the rest together.
``agentos.desktop``
    The GTK4/libadwaita desktop application.
``agentos.session``
    The per-login session broker that serves the GNOME Shell extension.
"""

from __future__ import annotations

from .constants import (
    EVENT_SCHEMA_VERSION,
    IPC_PROTOCOL_VERSION,
    PROJECT_NAME,
    PROJECT_TAGLINE,
    PROJECT_VERSION,
    STORAGE_SCHEMA_VERSION,
)

__version__ = PROJECT_VERSION

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "IPC_PROTOCOL_VERSION",
    "PROJECT_NAME",
    "PROJECT_TAGLINE",
    "PROJECT_VERSION",
    "STORAGE_SCHEMA_VERSION",
    "__version__",
]
