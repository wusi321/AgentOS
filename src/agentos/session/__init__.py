"""Session subsystem: the per-login broker that serves the GNOME panel.

``SessionBroker`` collects status and relays events; ``SessionDbusService``
exports it on the user session bus. Neither holds a model credential, a
privileged operation, or an approval decision.
"""

from __future__ import annotations

from .broker import (
    EVENT_POLL_SECONDS,
    INTROSPECTION_XML,
    MAX_RECENT_EVENTS,
    SESSION_BUS_NAME,
    SESSION_INTERFACE,
    SESSION_PATH,
    SUBSCRIBER_BACKLOG,
    BrokerStatus,
    SessionBroker,
    SessionDbusService,
    main,
)

__all__ = [
    "EVENT_POLL_SECONDS",
    "INTROSPECTION_XML",
    "MAX_RECENT_EVENTS",
    "SESSION_BUS_NAME",
    "SESSION_INTERFACE",
    "SESSION_PATH",
    "SUBSCRIBER_BACKLOG",
    "BrokerStatus",
    "SessionBroker",
    "SessionDbusService",
    "main",
]
